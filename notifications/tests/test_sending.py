"""Sending (notifications/sending.py): which devices a dispatch reaches, what
one send writes on the dispatch and the device, the delivery thread, and
« Envoyer un essai ». The push services are faked (support.push_service);
everything else is the real code on the test espace."""

from __future__ import annotations

import threading
from datetime import timedelta
from unittest import mock

from django.db import DatabaseError, OperationalError
from django.db.models import QuerySet
from django.test import TestCase
from django.utils import timezone

from accounts.models import PushDevice, Tenant
from accounts.tenancy import tenant_key
from notifications import sending, webpush
from notifications.models import Dispatch, create_dispatch, test_key
from notifications.tests.support import PRODUCTION, SITE, QuietLogs, make_device, make_login, push_service
from tests.runner import TEST_TENANT_PK
from tests.support import ForbiddenNetworkCall


def queue(**fields) -> Dispatch:
    values = {
        "kind": Dispatch.Kind.REMINDER,
        "rule_name": "Consignes avant livraison",
        "dedupe_key": test_key(),
        "title": "Consignes",
        "body": "Comptez les vides.",
        "target": "/consignes/#new-pickup",
        "ttl": webpush.TTL_REMINDER,
        "topic": "r1",
        "status": Dispatch.Status.PENDING,
    }
    values.update(fields)
    created = create_dispatch(**values)
    assert created is not None
    return created


class Logins(QuietLogs, TestCase):
    def setUp(self):
        super().setUp()
        self.member = make_login(TEST_TENANT_PK, "serveur@example.invalid")
        self.other = make_login(TEST_TENANT_PK, "barman@example.invalid")


class DevicesForTests(Logins):
    def test_the_espace_s_active_logins_current_devices(self):
        mine = make_device(self.member)
        theirs = make_device(self.other)
        retired = make_device(make_login(TEST_TENANT_PK, "parti@example.invalid", active=False))
        elsewhere = Tenant.objects.create(name="Bar Ailleurs", dir_name="bar-ailleurs-essai")
        foreign = make_device(make_login(elsewhere.pk, "ailleurs@example.invalid"))
        gone = make_device(self.member, gone_at=timezone.now())
        logged_out = make_device(self.member, logged_out_at=timezone.now())
        old_key = make_device(self.member, server_key="BAutre-cle-d-un-autre-serveur")
        selected = sending.devices_for()
        self.assertEqual({device.pk for device in selected}, {mine.pk, theirs.pk})
        for left_out in (retired, foreign, gone, logged_out, old_key):
            self.assertNotIn(left_out, selected)
        self.assertEqual(sending.stale_count(), 2)

    def test_only_the_recipients(self):
        mine = make_device(self.member)
        make_device(self.other)
        self.assertEqual(sending.devices_for([self.member.user_id]), [mine])
        self.assertEqual(sending.devices_for([]), [])

    def test_at_most_50_the_last_seen_first(self):
        now = timezone.now()
        for number in range(51):
            make_device(self.member, seen_at=now - timedelta(minutes=number))
        selected = sending.devices_for()
        self.assertEqual(len(selected), sending.MAX_DEVICES_PER_DISPATCH)
        self.assertEqual([d.seen_at for d in selected], sorted((d.seen_at for d in selected), reverse=True))
        self.assertEqual(selected[0].seen_at, now)


@PRODUCTION
class DeliverOneTests(Logins):
    def test_sent_to_every_device_and_said(self):
        first = make_device(self.member, failures=3, last_error="délai dépassé")
        second = make_device(self.other)
        dispatch = queue()
        with push_service(201) as service, mock.patch.object(webpush, "payload_for", wraps=webpush.payload_for) as made:
            sending.deliver_one(dispatch)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, Dispatch.Status.SENT)
        self.assertEqual((dispatch.devices, dispatch.delivered), (2, 2))
        self.assertEqual(dispatch.detail, "2 appareils sur 2")
        self.assertIsNotNone(dispatch.sent_at)
        self.assertEqual(sorted(service.urls), sorted([first.endpoint, second.endpoint]))
        # One payload for every device: the absolute link, a tag of its own.
        made.assert_called_once_with(
            "Consignes", "Comptez les vides.", f"{SITE}/consignes/#new-pickup", f"d{dispatch.pk}"
        )
        headers = service.calls[0][1]["headers"]
        self.assertEqual((headers["TTL"], headers["Topic"]), ("3600", "r1"))
        first.refresh_from_db()
        self.assertIsNotNone(first.last_success_at)
        self.assertEqual((first.failures, first.last_error), (0, ""))

    def test_two_dispatches_of_one_reminder_carry_two_tags(self):
        make_device(self.member)
        tags = []
        real = webpush.payload_for
        with (
            push_service(201),
            mock.patch.object(webpush, "payload_for", side_effect=lambda *a: tags.append(a[3]) or real(*a)),
        ):
            sending.deliver_one(queue())
            sending.deliver_one(queue())
        self.assertEqual(len(set(tags)), 2)

    def test_a_device_gone_is_tombstoned(self):
        device = make_device(self.member)
        dispatch = queue()
        with push_service(410):
            sending.deliver_one(dispatch)
        dispatch.refresh_from_db()
        device.refresh_from_db()
        self.assertEqual(dispatch.status, Dispatch.Status.FAILED)
        self.assertEqual(dispatch.detail, f"0 appareil sur 1 — {webpush.GONE}")
        self.assertIsNotNone(device.gone_at)
        self.assertEqual((device.failures, device.last_error), (1, webpush.GONE))
        self.assertEqual(sending.devices_for(), [])

    def test_some_devices_some_not(self):
        now = timezone.now()
        make_device(self.member, seen_at=now)
        failing = make_device(self.other, seen_at=now - timedelta(hours=1), failures=4)
        dispatch = queue()
        # The first device's answer, then the second's and its one retry.
        with push_service(201, 500, 500) as service:
            sending.deliver_one(dispatch)
        self.assertEqual(len(service.calls), 3)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, Dispatch.Status.PARTIAL)
        self.assertEqual(dispatch.detail, "1 appareil sur 2 — refusé par le service (code 500)")
        failing.refresh_from_db()
        self.assertEqual(failing.failures, 5)
        self.assertIsNotNone(failing.last_error_at)
        self.assertIsNone(failing.gone_at)

    def test_no_device_is_said_with_those_to_reactivate(self):
        dispatch = queue()
        sending.deliver_one(dispatch)
        dispatch.refresh_from_db()
        self.assertEqual((dispatch.status, dispatch.detail), (Dispatch.Status.NO_DEVICE, "aucun appareil inscrit"))
        make_device(self.member, server_key="BAutre-cle-d-un-autre-serveur")
        dispatch = queue()
        sending.deliver_one(dispatch)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.detail, "aucun appareil inscrit (1 à réactiver)")

    def test_a_dispatch_past_its_ttl_is_not_sent(self):
        make_device(self.member)
        dispatch = queue(created_at=timezone.now() - timedelta(hours=2))
        sending.deliver_one(dispatch)
        dispatch.refresh_from_db()
        self.assertEqual((dispatch.status, dispatch.detail), (Dispatch.Status.FAILED, sending.EXPIRED))

    def test_our_own_bug_is_the_dispatch_s_internal_error_never_raised(self):
        dispatch = queue()
        with (
            mock.patch.object(sending, "devices_for", side_effect=RuntimeError("bogue")),
            self.assertLogs("notifications.sending", "ERROR"),
        ):
            sending.deliver_one(dispatch)
        dispatch.refresh_from_db()
        self.assertEqual((dispatch.status, dispatch.detail), (Dispatch.Status.FAILED, "erreur interne"))

    def test_a_device_s_statistics_never_cost_the_result(self):
        device = make_device(self.member)
        dispatch = queue()
        with push_service(201), mock.patch.object(sending.PushDevice.objects, "filter", side_effect=DatabaseError):
            sending.deliver_one(dispatch, devices=[device])
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, Dispatch.Status.SENT)

    def test_the_result_is_written_again_once_when_the_database_is_locked(self):
        dispatch = queue()
        real, calls = QuerySet.update, []

        def locked_once(queryset, **fields):
            calls.append(fields)
            if len(calls) == 1:
                raise OperationalError("database is locked")
            return real(queryset, **fields)

        with mock.patch.object(QuerySet, "update", locked_once):
            sending.deliver_one(dispatch)
        self.assertEqual(len(calls), 2)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, Dispatch.Status.NO_DEVICE)


class DisabledTests(Logins):
    def test_a_development_server_sends_nothing_and_says_so(self):
        make_device(self.member)
        dispatch = queue()
        # No push_service: sending_enabled() is False under the test
        # settings, so nothing reaches the transport (the run's guard would
        # fail this test if it did: tests.support.ForbiddenNetworkCall).
        sending.deliver_one(dispatch)
        dispatch.refresh_from_db()
        self.assertEqual((dispatch.status, dispatch.detail), (Dispatch.Status.DISABLED, webpush.DISABLED))
        self.assertEqual(dispatch.detail, "non envoyé : les envois sont désactivés sur ce serveur")


@PRODUCTION
class DeliverPendingTests(Logins):
    def test_every_pending_dispatch_oldest_first_each_claimed_once(self):
        make_device(self.member)
        now = timezone.now()
        later = queue(created_at=now)
        earlier = queue(created_at=now - timedelta(minutes=5))
        busy = queue(status=Dispatch.Status.SENDING, sent_at=now)
        order = []
        real = sending.deliver_one
        with (
            push_service(201),
            mock.patch.object(sending, "deliver_one", side_effect=lambda d: order.append(d.pk) or real(d)),
        ):
            self.assertEqual(sending.deliver_pending(), 2)
        self.assertEqual(order, [earlier.pk, later.pk])
        for dispatch in (earlier, later):
            dispatch.refresh_from_db()
            self.assertEqual(dispatch.status, Dispatch.Status.SENT)
        busy.refresh_from_db()
        self.assertEqual(busy.status, Dispatch.Status.SENDING)
        self.assertEqual(sending.deliver_pending(), 0)

    def test_a_database_that_does_not_answer_stops_the_thread_quietly(self):
        with (
            mock.patch.object(sending.Dispatch.objects, "filter", side_effect=DatabaseError),
            self.assertLogs("notifications.sending", "ERROR"),
        ):
            self.assertEqual(sending.deliver_pending(), 0)


class StartDeliveryTests(QuietLogs, TestCase):
    def test_one_live_thread_per_espace(self):
        with mock.patch("notifications.sending.threading.Thread") as thread:
            thread.return_value.is_alive.return_value = True
            self.assertTrue(sending.start_delivery())
            self.assertFalse(sending.start_delivery())
            thread.assert_called_once()
            kwargs = thread.call_args.kwargs
            self.assertTrue(kwargs["daemon"])
            self.assertEqual(kwargs["target"].tenant.pk, TEST_TENANT_PK)
            thread.return_value.start.assert_called_once_with()
            # Once it has ended, the next one starts.
            thread.return_value.is_alive.return_value = False
            self.assertTrue(sending.start_delivery())
            self.assertEqual(thread.call_count, 2)

    def running_here(self):
        """This test's thread stands for the espace's delivery thread,
        alive (`deliver_pending` is called in it)."""
        sending._delivery_threads[tenant_key()] = threading.current_thread()

    def test_a_dispatch_made_while_the_thread_finishes_is_sent_by_it(self):
        # The thread's last query found nothing; before it is gone, an
        # event inserts a dispatch and asks for delivery: the thread still
        # looks alive, so none is started - the running one must look again.
        self.running_here()
        made = []
        real_first = QuerySet.first

        def first_then_an_event(queryset):
            found = real_first(queryset)
            if found is None and not made and queryset.model is Dispatch:
                made.append(queue())
                with mock.patch("notifications.sending.threading.Thread") as thread:
                    self.assertFalse(sending.start_delivery())
                    thread.assert_not_called()
            return found

        with mock.patch.object(QuerySet, "first", first_then_an_event):
            self.assertEqual(sending.deliver_pending(), 1)
        made[0].refresh_from_db()
        self.assertNotEqual(made[0].status, Dispatch.Status.PENDING)

    def test_a_thread_that_found_nothing_leaves_its_place_before_it_ends(self):
        self.running_here()
        self.assertEqual(sending.deliver_pending(), 0)
        # Still alive (tearing down), but no longer the espace's thread.
        self.assertNotIn(tenant_key(), sending._delivery_threads)
        with mock.patch("notifications.sending.threading.Thread") as thread:
            self.assertTrue(sending.start_delivery())
            thread.return_value.start.assert_called_once_with()

    def test_another_thread_s_place_is_never_taken_away(self):
        with mock.patch("notifications.sending.threading.Thread") as thread:
            thread.return_value.is_alive.return_value = True
            self.assertTrue(sending.start_delivery())
            self.assertEqual(sending.deliver_pending(), 0)
            self.assertIs(sending._delivery_threads[tenant_key()], thread.return_value)


@PRODUCTION
class SendTestTests(Logins):
    def test_my_devices_now_without_retry_recorded(self):
        mine = make_device(self.member)
        make_device(self.other)
        with push_service(500) as service:
            sent = sending.send_test(self.member, title="Consignes", body="Essai", target="/consignes/")
        self.assertEqual(service.urls, [mine.endpoint])
        self.assertEqual(sent, (0, 1, "Essai non reçu (0 appareil sur 1 — refusé par le service (code 500))."))
        dispatch = Dispatch.objects.get()
        self.assertEqual((dispatch.kind, dispatch.status), (Dispatch.Kind.TEST, Dispatch.Status.FAILED))
        self.assertEqual(dispatch.recipient_ids, [self.member.user_id])
        self.assertEqual(dispatch.ttl, webpush.TTL_TEST)

    def test_one_every_30_seconds_per_login(self):
        make_device(self.member)
        make_device(self.other)
        with push_service(201):
            self.assertEqual(sending.send_test(self.member, title="T", body="", target="/"), (1, 1, ""))
            self.assertEqual(
                sending.send_test(self.member, title="T", body="", target="/"), (0, 0, sending.TEST_TOO_SOON)
            )
            self.assertEqual(sending.send_test(self.other, title="T", body="", target="/"), (1, 1, ""))
        self.assertEqual(Dispatch.objects.count(), 2)

    def test_one_device_or_none(self):
        stale = make_device(self.member, gone_at=timezone.now())
        self.assertEqual(
            sending.send_test(self.member, title="T", body="", target="/", device=stale),
            (0, 0, sending.TEST_DEVICE_STALE),
        )
        sending.cache.clear()
        self.assertEqual(sending.send_test(self.other, title="T", body="", target="/"), (0, 0, sending.TEST_NO_DEVICE))
        self.assertFalse(Dispatch.objects.exists())


@PRODUCTION
class RunWideGuardTests(Logins):
    """The run's forbidden push transport (tests/runner.py) is not an
    Exception: a test that forgets to mock fails loudly through the delivery
    path too, never as a dispatch quietly « failed, erreur interne »."""

    def test_a_delivery_that_was_not_mocked_fails_the_test(self):
        make_device(self.member)
        dispatch = queue()
        with self.assertRaises(ForbiddenNetworkCall):
            sending.deliver_one(dispatch)
        with self.assertRaises(ForbiddenNetworkCall):
            sending.deliver_pending()
        self.assertFalse(issubclass(ForbiddenNetworkCall, Exception))

    def test_an_essai_that_was_not_mocked_fails_the_test(self):
        make_device(self.member)
        with self.assertRaises(ForbiddenNetworkCall):
            sending.send_test(self.member, title="T", body="", target="/")


class SendTestDisabledTests(Logins):
    def test_a_development_server_says_so(self):
        make_device(self.member)
        self.assertEqual(
            sending.send_test(self.member, title="T", body="", target="/"),
            (0, 0, "Envois désactivés sur ce serveur (serveur de développement)."),
        )


class StaleCountTests(Logins):
    def test_a_device_of_another_key_or_gone_is_to_reactivate_a_logged_out_one_is_not(self):
        make_device(self.member, server_key="BAutre")
        make_device(self.member, gone_at=timezone.now())
        make_device(self.member, logged_out_at=timezone.now())
        make_device(self.member)
        self.assertEqual(sending.stale_count(), 2)
        self.assertEqual(sending.stale_count([self.other.user_id]), 0)
        self.assertEqual(PushDevice.objects.count(), 4)
