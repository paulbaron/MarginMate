"""The scheduler (notifications/scheduler.py): a tick runs every job for
every open espace, bound to it, one failure never stopping the rest; the
delivery thread starts once per espace; the loop starts and stops. Real
espaces in real files (TwoTenantsTestCase); no thread is started for real
but the loop's own, with its tick replaced."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest import mock

from django.db import DatabaseError, connection
from django.test import SimpleTestCase

from accounts import paths
from accounts.models import Membership, Tenant
from accounts.tenancy import bound_tenant, current_tenant
from accounts.tests.support import TwoTenantsTestCase
from notifications import scheduler, sending, webpush
from notifications.models import Dispatch, NotificationSettings, create_dispatch, test_key
from notifications.tests.support import QuietLogs, make_device

NOW = datetime(2027, 10, 2, 22, 0, 30, tzinfo=UTC)

#: What the test jobs saw: (espace's pk, now).
SEEN: list = []


def record(now):
    SEEN.append((current_tenant().pk, now))


#: The busy timeout each job's connection had, in milliseconds.
BUSY: list = []


def record_busy_timeout(now):
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        BUSY.append(cursor.fetchone()[0])


def fail_in_alpha(now):
    if current_tenant().name == "Bar Alpha":
        raise ValueError("données abîmées")


THIS = "notifications.tests.test_scheduler"


class TickTests(QuietLogs, TwoTenantsTestCase):
    def setUp(self):
        super().setUp()
        SEEN.clear()
        BUSY.clear()

    def jobs(self, *jobs):
        return mock.patch.object(scheduler, "JOBS", jobs)

    def test_every_job_for_every_open_espace_bound_to_it(self):
        closed = self.make_tenant("Bar Fermé")
        Tenant.objects.filter(pk=closed.pk).update(is_active=False)
        with self.jobs(f"{THIS}.record"):
            scheduler.tick(NOW)
        self.assertEqual(SEEN, [(self.bar_a.pk, NOW), (self.bar_b.pk, NOW)])
        self.assertIsNone(current_tenant())

    def test_a_failing_job_stops_neither_the_next_job_nor_the_next_espace(self):
        with self.jobs(f"{THIS}.fail_in_alpha", f"{THIS}.record"), self.assertLogs("notifications.scheduler") as said:
            scheduler.tick(NOW)
        self.assertEqual([pk for pk, _ in SEEN], [self.bar_a.pk, self.bar_b.pk])
        self.assertIn(f"{THIS}.fail_in_alpha en échec (espace {self.bar_a.pk})", "\n".join(said.output))

    def test_a_job_whose_module_is_not_there_is_said_and_passed_over(self):
        with self.jobs(f"{THIS}.nowhere_to_be_found", "invoices.not_a_module.run_due", f"{THIS}.record"):
            with self.assertLogs("notifications.scheduler", "WARNING") as said:
                scheduler.tick(NOW)
        self.assertEqual(len(SEEN), 2)
        self.assertIn("invoices.not_a_module.run_due introuvable", "\n".join(said.output))

    def test_an_espace_whose_database_is_gone_is_passed_over(self):
        paths.tenant_database(self.bar_a).unlink()
        with self.jobs(f"{THIS}.record"), self.assertLogs("notifications.scheduler", "WARNING"):
            scheduler.tick(NOW)
        self.assertEqual(SEEN, [(self.bar_b.pk, NOW)])

    def test_the_espaces_that_do_not_read_end_the_tick(self):
        with (
            self.jobs(f"{THIS}.record"),
            mock.patch.object(Tenant.objects, "filter", side_effect=DatabaseError("verrouillée")),
            self.assertLogs("notifications.scheduler", "ERROR"),
        ):
            scheduler.tick(NOW)
        self.assertEqual(SEEN, [])

    def test_the_reminders_job_writes_each_espace_s_heartbeat(self):
        with self.jobs("notifications.reminders.run_due"):
            scheduler.tick(NOW)
        for tenant in (self.bar_a, self.bar_b):
            with bound_tenant(tenant):
                self.assertEqual(NotificationSettings.get_solo().last_tick_at, NOW)

    def test_one_delivery_thread_per_espace_with_something_to_send(self):
        with bound_tenant(self.bar_a):
            create_dispatch(
                kind=Dispatch.Kind.TEST,
                rule_name="Essai",
                dedupe_key=test_key(),
                title="T",
                ttl=600,
                created_at=NOW,
            )
        with self.jobs(), mock.patch("notifications.sending.threading.Thread") as thread:
            thread.return_value.is_alive.return_value = True
            scheduler.tick(NOW)
            thread.assert_called_once()
            self.assertEqual(thread.call_args.kwargs["target"].tenant.pk, self.bar_a.pk)
            thread.return_value.start.assert_called_once_with()
            # Still alive at the next tick: no second one.
            scheduler.tick(NOW + timedelta(minutes=1))
            thread.assert_called_once()

    def test_each_espace_s_connection_waits_only_briefly_for_a_lock(self):
        with self.jobs(f"{THIS}.record_busy_timeout"):
            scheduler.tick(NOW)
        self.assertEqual(BUSY, [scheduler.BUSY_TIMEOUT_MS, scheduler.BUSY_TIMEOUT_MS])

    def test_an_espace_whose_database_is_held_does_not_hold_the_others(self):
        # Another connection holds Bar Alpha's write lock (a long import):
        # its jobs fail fast, Bar Beta's run on time.
        writer = sqlite3.connect(paths.tenant_database(self.bar_a))
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        self.addCleanup(writer.rollback)
        started = time.monotonic()
        with (
            self.jobs("notifications.reminders.run_due"),
            mock.patch.object(scheduler, "BUSY_TIMEOUT_MS", 100, create=True),
        ):
            scheduler.tick(NOW)
        self.assertLess(time.monotonic() - started, 3)
        writer.rollback()
        with bound_tenant(self.bar_b):
            self.assertEqual(NotificationSettings.get_solo().last_tick_at, NOW)

    def test_its_connections_are_closed_after_every_tick(self):
        with self.jobs(f"{THIS}.record"), mock.patch.object(scheduler.connections, "close_all") as close_all:
            scheduler.tick(NOW)
        close_all.assert_called()

    def test_a_device_of_one_espace_is_never_selected_in_the_other(self):
        membership = Membership.objects.get(user=self.user_a, tenant=self.bar_a)
        device = make_device(membership)
        with bound_tenant(self.bar_a):
            self.assertEqual(sending.devices_for(), [device])
        with bound_tenant(self.bar_b):
            self.assertEqual(sending.devices_for(), [])
            self.assertEqual(sending.devices_for([self.user_a.pk]), [])
        self.assertTrue(webpush.vapid_public_key())


class LoopTests(SimpleTestCase):
    def test_it_ticks_and_stops_at_once(self):
        ticked = threading.Event()
        with mock.patch.object(scheduler, "tick", side_effect=lambda now: ticked.set()):
            handle = scheduler.start()
            self.assertTrue(ticked.wait(5))
            self.assertTrue(handle.alive)
            self.assertTrue(handle.thread.daemon)
            handle.stop(timeout=5)
        self.assertFalse(handle.alive)

    def test_a_tick_that_fails_never_ends_the_loop(self):
        calls = []
        stopped = threading.Event()

        def failing(now):
            calls.append(now)
            if len(calls) == 2:
                stopped.set()
            raise RuntimeError("bogue")

        with (
            mock.patch.object(scheduler, "tick", side_effect=failing),
            mock.patch.object(scheduler, "seconds_to_next_tick", return_value=0.01),
            self.assertLogs("notifications.scheduler", "ERROR"),
        ):
            scheduler.run(stopped)
        self.assertEqual(len(calls), 2)

    def test_the_next_tick_is_two_seconds_past_the_next_minute(self):
        for now, seconds in (
            (datetime(2027, 10, 2, 22, 0, 30, 500000, tzinfo=UTC), 31.5),
            (datetime(2027, 10, 2, 22, 0, 2, tzinfo=UTC), 60.0),
            (datetime(2027, 10, 2, 22, 0, 59, tzinfo=UTC), 3.0),
            (datetime(2027, 10, 2, 22, 59, 59, 999999, tzinfo=UTC), 2.000001),
        ):
            with self.subTest(now=now):
                self.assertAlmostEqual(scheduler.seconds_to_next_tick(now), seconds, places=5)

    def test_the_jobs_are_the_spec_s(self):
        self.assertEqual(
            scheduler.JOBS,
            ("notifications.reminders.run_due", "invoices.auto_gather.run_due", "recipes.auto_sales.run_due"),
        )
