"""Alerts on an event (notifications/events.py): `wanted` before any work,
`emit` after the caller's commit, once per result, never raising into the
caller. The delivery thread is never started for real
(`notifications.events.threading.Thread` patched)."""

from __future__ import annotations

import sqlite3
import time
from unittest import mock

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connection, connections, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from accounts import paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from notifications import events, webpush
from notifications.models import Dispatch, EventRule, Reminder
from notifications.tests.support import QuietLogs

EVENT = "returnables-comparison"


def emit(outcome="differs", content_key="consignes:0123456789abcdef01234567", **fields):
    values = {
        "title": "Consignes : écart",
        "body": "Fûts — compté : 15 · sur le bon : 14",
        "target": "/consignes/bons/7/",
        "content_key": content_key,
    }
    values.update(fields)
    events.emit(EVENT, outcome, **values)


class EventTests(QuietLogs, TestCase):
    def setUp(self):
        super().setUp()
        self.thread = self.enterContext(mock.patch("notifications.events.threading.Thread"))
        self.thread.return_value.is_alive.return_value = False

    def rule(self, **fields) -> EventRule:
        values = {"event": EVENT, "outcomes": ["differs", "no_pickup"], "recipient_ids": [5]}
        values.update(fields)
        return EventRule.objects.create(**values)

    def test_no_rule_nothing_wanted_nothing_made(self):
        self.assertFalse(events.wanted(EVENT))
        with self.captureOnCommitCallbacks(execute=True):
            emit()
        self.assertFalse(Dispatch.objects.exists())
        self.thread.assert_not_called()

    def test_an_inactive_rule_is_no_rule(self):
        self.rule(is_active=False)
        self.assertFalse(events.wanted(EVENT))
        with self.captureOnCommitCallbacks(execute=True):
            emit()
        self.assertFalse(Dispatch.objects.exists())

    def test_a_result_the_rule_follows_is_queued_and_its_thread_started(self):
        rule = self.rule()
        self.assertTrue(events.wanted(EVENT))
        with self.captureOnCommitCallbacks(execute=True):
            emit()
        dispatch = Dispatch.objects.get()
        self.assertEqual((dispatch.kind, dispatch.status), (Dispatch.Kind.EVENT, Dispatch.Status.PENDING))
        self.assertEqual((dispatch.event, dispatch.outcome, dispatch.event_rule_id), (EVENT, "differs", rule.pk))
        self.assertEqual(dispatch.rule_name, "Consignes : bon du livreur comparé à la reprise")
        self.assertEqual(dispatch.dedupe_key, f"event:{rule.pk}:consignes:0123456789abcdef01234567")
        self.assertEqual((dispatch.title, dispatch.target), ("Consignes : écart", "/consignes/bons/7/"))
        self.assertEqual((dispatch.ttl, dispatch.topic), (webpush.TTL_EVENT, ""))
        self.assertEqual(dispatch.recipient_ids, [5])
        self.thread.assert_called_once()
        self.assertTrue(self.thread.call_args.kwargs["daemon"])
        self.thread.return_value.start.assert_called_once_with()

    def test_a_result_it_does_not_follow_is_left(self):
        self.rule()
        with self.captureOnCommitCallbacks(execute=True):
            emit("match")
        self.assertFalse(Dispatch.objects.exists())
        self.thread.assert_not_called()

    def test_the_rule_s_page_and_everyone(self):
        self.rule(target="/consignes/#new-pickup", all_members=True)
        with self.captureOnCommitCallbacks(execute=True):
            emit()
        dispatch = Dispatch.objects.get()
        self.assertEqual(dispatch.target, "/consignes/#new-pickup")
        self.assertIsNone(dispatch.recipient_ids)

    def test_the_same_result_is_sent_once(self):
        self.rule()
        for body in ("Fûts — compté : 15", "Fûts : 15 comptés"):
            with self.captureOnCommitCallbacks(execute=True):
                emit(body=body)
        self.assertEqual(Dispatch.objects.count(), 1)
        self.thread.assert_called_once()
        with self.captureOnCommitCallbacks(execute=True):
            emit(content_key="consignes:autre-resultat")
        self.assertEqual(Dispatch.objects.count(), 2)

    def test_nothing_before_the_caller_commits(self):
        self.rule()
        with self.captureOnCommitCallbacks() as callbacks:
            emit()
            self.assertFalse(Dispatch.objects.exists())
        self.assertEqual(len(callbacks), 1)
        self.assertFalse(Dispatch.objects.exists())
        callbacks[0]()
        self.assertEqual(Dispatch.objects.count(), 1)

    def test_a_duplicate_inside_a_caller_s_transaction_leaves_its_work(self):
        self.rule()
        with self.captureOnCommitCallbacks(execute=True):
            emit()
        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            Reminder.objects.create(name="Gardé", title="T", target="/", weekdays="1", times="00:00")
            emit()
            Reminder.objects.create(name="Gardé aussi", title="T", target="/", weekdays="1", times="00:00")
        self.assertEqual(Reminder.objects.count(), 2)
        self.assertEqual(Dispatch.objects.count(), 1)

    def test_a_database_that_does_not_answer_never_reaches_the_caller(self):
        self.rule()
        with (
            mock.patch.object(EventRule.objects, "filter", side_effect=DatabaseError("no such table")),
            self.assertLogs("notifications.events", "ERROR") as said,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.assertFalse(events.wanted(EVENT))
            emit()
        self.assertIn("Notification non créée", said.output[0])
        self.assertFalse(Dispatch.objects.exists())

    def test_emit_itself_never_raises(self):
        with (
            mock.patch.object(transaction, "on_commit", side_effect=RuntimeError("bogue")),
            self.assertLogs("notifications.events", "ERROR"),
        ):
            emit()

    def test_a_rule_that_cannot_be_read_is_said_a_missing_table_is_not(self):
        with mock.patch.object(EventRule.objects, "filter", side_effect=DatabaseError("database is locked")):
            with self.assertLogs("notifications.events", "WARNING") as said:
                self.assertFalse(events.wanted(EVENT))
        self.assertIn(EVENT, said.output[0])
        with mock.patch.object(EventRule.objects, "filter", side_effect=DatabaseError("no such table: x")):
            with self.assertNoLogs("notifications.events"):
                self.assertFalse(events.wanted(EVENT))

    def test_a_thread_that_will_not_start_never_reaches_the_caller(self):
        self.rule()
        self.thread.return_value.start.side_effect = RuntimeError("can't start new thread")
        with self.assertLogs("notifications.events", "ERROR"), self.captureOnCommitCallbacks(execute=True):
            emit()
        # The row is there: the next tick's delivery sends it.
        self.assertEqual(Dispatch.objects.count(), 1)


class WantedOutsideATransactionTests(QuietLogs, TenancyTestCase):
    """`wanted` called with no transaction open (a gather thread, a slip
    batch after its loop): a plain read, never the espace's write lock.
    Production's SQLite options (IMMEDIATE transactions, WAL), a short
    wait: the test settings carry none."""

    def setUp(self):
        super().setUp()
        self.tenant = self.make_tenant("Bar Alpha")
        production = {**settings.SQLITE_OPTIONS, "timeout": 1}
        self.enterContext(mock.patch.dict(connections.settings[DEFAULT_DB_ALIAS], {"OPTIONS": production}))
        self.binding = self.enterContext(bound_tenant(self.tenant))
        EventRule.objects.create(event=EVENT, outcomes=["differs"], recipient_ids=[5])

    def test_no_transaction_is_begun(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(events.wanted(EVENT))
        said = [query["sql"].upper() for query in queries.captured_queries]
        self.assertFalse([sql for sql in said if sql.startswith("BEGIN")], said)

    def test_inside_the_caller_s_transaction_a_savepoint_of_its_own(self):
        with transaction.atomic(), CaptureQueriesContext(connection) as queries:
            self.assertTrue(events.wanted(EVENT))
        self.assertTrue([query for query in queries.captured_queries if query["sql"].startswith("SAVEPOINT")])

    def test_another_writer_holding_the_espace_does_not_hold_it(self):
        writer = sqlite3.connect(paths.tenant_database(self.tenant))
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        self.addCleanup(writer.rollback)
        started = time.monotonic()
        self.assertTrue(events.wanted(EVENT))
        self.assertLess(time.monotonic() - started, 2)
