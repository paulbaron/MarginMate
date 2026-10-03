"""The reminders that are due (notifications/reminders.py `run_due`): due,
missed, skipped, never twice, the window a tick looks into, the nights
daylight saving changes - and the tick's housekeeping. Invented future
dates only; every instant is compared in UTC."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from accounts.models import PushDevice, Tenant
from notifications import reminders
from notifications.models import Dispatch, NotificationSettings, Reminder, create_dispatch, test_key
from notifications.tests.support import QuietLogs, make_device, make_login
from returnables.models import Pickup
from tests.factories import make_supplier
from tests.runner import TEST_TENANT_PK

PARIS = ZoneInfo("Europe/Paris")
#: When the reminders were made: long before any instant of the tests.
LONG_AGO = datetime(2027, 1, 4, 12, 0, tzinfo=UTC)
#: Sunday 03/10/2027 00:00 and 02:00 in Paris (summer time: UTC+2) - the night
#: of Saturday, one of the invented reminder's evenings.
SUNDAY_MIDNIGHT = datetime(2027, 10, 2, 22, 0, tzinfo=UTC)
SUNDAY_TWO = datetime(2027, 10, 3, 0, 0, tzinfo=UTC)


def three_evenings_reminder(**fields) -> Reminder:
    """An invented schedule: Monday, Wednesday and Saturday evenings, at
    00:00 and 02:00 - Saturday's land on Sunday, the day daylight saving
    changes on (DaylightSavingTests)."""
    values = {
        "name": "Consignes avant livraison",
        "title": "Consignes",
        "body": "Photographiez et comptez les vides avant la livraison.",
        "target": "/consignes/#new-pickup",
        "weekdays": "0,2,5",
        "times": "00:00 02:00",
        "created_at": LONG_AGO,
    }
    values.update(fields)
    return Reminder.objects.create(**values)


def last_tick(moment) -> None:
    NotificationSettings.get_solo()
    NotificationSettings.objects.update(last_tick_at=moment)


class RunDueTests(QuietLogs, TestCase):
    def test_a_due_instant_is_queued_with_its_texts(self):
        reminder = three_evenings_reminder()
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        now = SUNDAY_MIDNIGHT + timedelta(minutes=1)
        self.assertEqual(reminders.run_due(now), 1)
        dispatch = Dispatch.objects.get()
        self.assertEqual(dispatch.status, Dispatch.Status.PENDING)
        self.assertEqual(dispatch.kind, Dispatch.Kind.REMINDER)
        self.assertEqual(dispatch.reminder_id, reminder.pk)
        self.assertEqual(dispatch.scheduled_for, SUNDAY_MIDNIGHT)
        self.assertEqual(dispatch.dedupe_key, f"reminder:{reminder.pk}:20271002T2200Z")
        self.assertEqual((dispatch.title, dispatch.target), ("Consignes", "/consignes/#new-pickup"))
        self.assertEqual((dispatch.ttl, dispatch.topic), (3600, f"r{reminder.pk}"))
        self.assertIsNone(dispatch.recipient_ids)
        self.assertEqual(NotificationSettings.get_solo().last_tick_at, now)

    def test_chosen_recipients_travel(self):
        three_evenings_reminder(all_members=False, recipient_ids=[4, 9])
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        reminders.run_due(SUNDAY_MIDNIGHT)
        self.assertEqual(Dispatch.objects.get().recipient_ids, [4, 9])

    def test_never_twice_whoever_ticks(self):
        three_evenings_reminder()
        before = SUNDAY_MIDNIGHT - timedelta(minutes=1)
        now = SUNDAY_MIDNIGHT + timedelta(minutes=1)
        last_tick(before)
        reminders.run_due(now)
        # Another process that ticked from the same moment.
        last_tick(before)
        self.assertEqual(reminders.run_due(now), 0)
        self.assertEqual(Dispatch.objects.count(), 1)

    def test_thirty_minutes_late_is_sent_later_is_missed(self):
        three_evenings_reminder()
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        reminders.run_due(SUNDAY_MIDNIGHT + timedelta(minutes=30))
        self.assertEqual(Dispatch.objects.get().status, Dispatch.Status.PENDING)

        Dispatch.objects.all().delete()
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        reminders.run_due(SUNDAY_MIDNIGHT + timedelta(minutes=31))
        dispatch = Dispatch.objects.get()
        self.assertEqual(dispatch.status, Dispatch.Status.MISSED)
        self.assertEqual(dispatch.detail, "manqué : serveur arrêté ou ordinateur en veille à 00:00")

    def test_a_night_the_server_slept_through(self):
        three_evenings_reminder()
        last_tick(SUNDAY_MIDNIGHT - timedelta(hours=2))
        reminders.run_due(SUNDAY_TWO + timedelta(hours=5))
        rows = list(Dispatch.objects.order_by("scheduled_for").values_list("scheduled_for", "status", "detail"))
        self.assertEqual(
            rows,
            [
                (SUNDAY_MIDNIGHT, "missed", "manqué : serveur arrêté ou ordinateur en veille à 00:00"),
                (SUNDAY_TWO, "missed", "manqué : serveur arrêté ou ordinateur en veille à 02:00"),
            ],
        )

    def test_inactive_or_made_after_the_instant_queues_nothing(self):
        three_evenings_reminder(is_active=False)
        three_evenings_reminder(name="Fait trop tard", created_at=SUNDAY_MIDNIGHT + timedelta(seconds=1))
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        self.assertEqual(reminders.run_due(SUNDAY_MIDNIGHT + timedelta(minutes=5)), 0)
        self.assertFalse(Dispatch.objects.exists())

    def test_no_tick_yet_looks_back_thirty_minutes(self):
        three_evenings_reminder()
        reminders.run_due(SUNDAY_MIDNIGHT + timedelta(minutes=40))
        self.assertFalse(Dispatch.objects.exists())
        reminders.run_due(SUNDAY_TWO + timedelta(minutes=5))
        self.assertEqual(list(Dispatch.objects.values_list("scheduled_for", flat=True)), [SUNDAY_TWO])

    def test_never_more_than_a_day_back(self):
        three_evenings_reminder(weekdays="0,1,2,3,4,5,6", times="00:00")
        last_tick(SUNDAY_MIDNIGHT - timedelta(days=5))
        reminders.run_due(SUNDAY_MIDNIGHT + timedelta(hours=8))
        self.assertEqual(list(Dispatch.objects.values_list("scheduled_for", flat=True)), [SUNDAY_MIDNIGHT])

    def test_a_whole_week(self):
        """Ticked every hour for a week: Tuesday, Thursday and Sunday at
        00:00 and 02:00, never anything else."""
        three_evenings_reminder()
        start = datetime(2027, 9, 27, 0, 0, tzinfo=UTC)  # Monday
        last_tick(start)
        for hour in range(1, 7 * 24 + 1):
            reminders.run_due(start + timedelta(hours=hour))
        sent = [
            moment.astimezone(PARIS)
            for moment in Dispatch.objects.order_by("scheduled_for").values_list("scheduled_for", flat=True)
        ]
        self.assertEqual(
            [(moment.strftime("%a %d/%m %H:%M")) for moment in sent],
            [
                "Tue 28/09 00:00",
                "Tue 28/09 02:00",
                "Thu 30/09 00:00",
                "Thu 30/09 02:00",
                "Sun 03/10 00:00",
                "Sun 03/10 02:00",
            ],
        )
        # Queued on time, every one; nothing delivered them here, so those an
        # hour old were swept as expired by a later tick.
        self.assertLessEqual(
            set(Dispatch.objects.values_list("status", "detail")), {("pending", ""), ("failed", "expiré avant l'envoi")}
        )

    def test_a_row_that_does_not_read_is_passed_over(self):
        Reminder.objects.create(
            name="Abîmé", title="T", target="/", weekdays="lundi", times="00:00", created_at=LONG_AGO
        )
        three_evenings_reminder()
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        self.assertEqual(reminders.run_due(SUNDAY_MIDNIGHT), 1)


class DaylightSavingTests(QuietLogs, TestCase):
    def test_the_march_night_sends_the_missing_hour_once(self):
        # Saturday 27/03/2027; 02:00 does not exist on Sunday 28/03.
        three_evenings_reminder(times="02:00 03:00")
        last_tick(datetime(2027, 3, 27, 22, 0, tzinfo=UTC))
        reminders.run_due(datetime(2027, 3, 28, 1, 5, tzinfo=UTC))
        self.assertEqual(
            list(Dispatch.objects.values_list("scheduled_for", flat=True)), [datetime(2027, 3, 28, 1, 0, tzinfo=UTC)]
        )

    def test_the_march_night_s_midnight_and_two(self):
        three_evenings_reminder()
        last_tick(datetime(2027, 3, 27, 22, 0, tzinfo=UTC))
        reminders.run_due(datetime(2027, 3, 28, 1, 5, tzinfo=UTC))
        self.assertEqual(
            list(Dispatch.objects.order_by("scheduled_for").values_list("scheduled_for", flat=True)),
            [datetime(2027, 3, 27, 23, 0, tzinfo=UTC), datetime(2027, 3, 28, 1, 0, tzinfo=UTC)],
        )

    def test_the_october_night_s_doubled_hour_is_its_first(self):
        # Saturday 30/10/2027: 02:30 on Sunday 31/10 happens twice; the first
        # is 00:30 UTC.
        three_evenings_reminder(times="02:30")
        last_tick(datetime(2027, 10, 30, 23, 0, tzinfo=UTC))
        reminders.run_due(datetime(2027, 10, 31, 0, 40, tzinfo=UTC))
        dispatch = Dispatch.objects.get()
        self.assertEqual(dispatch.scheduled_for, datetime(2027, 10, 31, 0, 30, tzinfo=UTC))
        self.assertEqual(dispatch.status, Dispatch.Status.PENDING)

    def test_lateness_is_counted_in_utc(self):
        """02:40 the SECOND time (01:40 UTC) is seventy minutes after the
        first 02:30, though the wall clock says ten."""
        three_evenings_reminder(times="02:30")
        last_tick(datetime(2027, 10, 30, 23, 0, tzinfo=UTC))
        second_two_forty = datetime(2027, 10, 31, 2, 40, tzinfo=PARIS, fold=1)
        self.assertEqual(second_two_forty.astimezone(UTC), datetime(2027, 10, 31, 1, 40, tzinfo=UTC))
        reminders.run_due(second_two_forty.astimezone(UTC))
        self.assertEqual(Dispatch.objects.get().status, Dispatch.Status.MISSED)


class SkipTests(QuietLogs, TestCase):
    def setUp(self):
        super().setUp()
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        self.supplier = make_supplier(code="BRASSEUR", name="Brasseur Exemple")

    def pickup(self, created_at, supplier=None, **fields) -> Pickup:
        pickup = Pickup.objects.create(date=created_at.date(), supplier=supplier, **fields)
        Pickup.objects.filter(pk=pickup.pk).update(created_at=created_at)
        return pickup

    def test_a_pickup_entered_lately_skips_and_says_when(self):
        three_evenings_reminder(skip_if="returnables.recent_pickup", skip_hours=6)
        self.pickup(SUNDAY_MIDNIGHT - timedelta(minutes=45))
        reminders.run_due(SUNDAY_MIDNIGHT)
        dispatch = Dispatch.objects.get()
        self.assertEqual(dispatch.status, Dispatch.Status.SKIPPED)
        self.assertEqual(dispatch.detail, "sauté : une reprise a été enregistrée à 23:15")

    def test_only_the_chosen_supplier_s_and_only_within_the_hours(self):
        three_evenings_reminder(skip_if="returnables.recent_pickup", skip_hours=2, skip_supplier_id=self.supplier.pk)
        self.pickup(SUNDAY_MIDNIGHT - timedelta(minutes=30), supplier=make_supplier(code="AUTRE", name="Autre"))
        self.pickup(SUNDAY_MIDNIGHT - timedelta(hours=3), supplier=self.supplier)
        reminders.run_due(SUNDAY_MIDNIGHT)
        self.assertEqual(Dispatch.objects.get().status, Dispatch.Status.PENDING)

    def test_an_old_pickup_redated_does_not_skip(self):
        three_evenings_reminder(skip_if="returnables.recent_pickup", skip_hours=6)
        old = self.pickup(SUNDAY_MIDNIGHT - timedelta(days=4))
        old.date = SUNDAY_MIDNIGHT.date()
        old.save(update_fields=["date", "updated_at"])
        reminders.run_due(SUNDAY_MIDNIGHT)
        self.assertEqual(Dispatch.objects.get().status, Dispatch.Status.PENDING)

    def test_a_condition_that_cannot_be_read_sends(self):
        three_evenings_reminder(skip_if="returnables.recent_pickup")
        with mock.patch("returnables.models.Pickup.objects.filter", side_effect=RuntimeError("bogue")):
            reminders.run_due(SUNDAY_MIDNIGHT)
        self.assertEqual(Dispatch.objects.get().status, Dispatch.Status.PENDING)

    def test_a_missed_reminder_is_missed_whatever_the_condition(self):
        three_evenings_reminder(skip_if="returnables.recent_pickup")
        self.pickup(SUNDAY_MIDNIGHT - timedelta(minutes=45))
        reminders.run_due(SUNDAY_MIDNIGHT + timedelta(hours=1))
        self.assertEqual(Dispatch.objects.get().status, Dispatch.Status.MISSED)


def dispatch(created_at, status=Dispatch.Status.SENT, **fields) -> Dispatch:
    values = {
        "kind": Dispatch.Kind.REMINDER,
        "rule_name": "R",
        "dedupe_key": test_key(),
        "title": "T",
        "ttl": 3600,
        "status": status,
        "created_at": created_at,
    }
    values.update(fields)
    created = create_dispatch(**values)
    assert created is not None
    return created


class HousekeepingTests(QuietLogs, TestCase):
    NOW = datetime(2027, 10, 3, 9, 0, tzinfo=UTC)

    def test_abandoned_dispatches_say_why(self):
        expired = dispatch(self.NOW - timedelta(hours=2), Dispatch.Status.PENDING)
        waiting = dispatch(self.NOW - timedelta(minutes=10), Dispatch.Status.PENDING)
        interrupted = dispatch(
            self.NOW - timedelta(minutes=20), Dispatch.Status.SENDING, sent_at=self.NOW - timedelta(minutes=11)
        )
        sending = dispatch(
            self.NOW - timedelta(minutes=20), Dispatch.Status.SENDING, sent_at=self.NOW - timedelta(minutes=5)
        )
        self.assertEqual(reminders.sweep(self.NOW), 2)
        for row, status, detail in (
            (expired, "failed", "expiré avant l'envoi"),
            (waiting, "pending", ""),
            (interrupted, "failed", "interrompu (serveur redémarré)"),
            (sending, "sending", ""),
        ):
            row.refresh_from_db()
            self.assertEqual((row.status, row.detail), (status, detail))

    def test_ninety_days_and_a_thousand_rows_never_one_still_to_send(self):
        old = dispatch(self.NOW - timedelta(days=91))
        old_pending = dispatch(self.NOW - timedelta(days=91), Dispatch.Status.PENDING)
        kept = dispatch(self.NOW - timedelta(days=89))
        newest = [dispatch(self.NOW - timedelta(minutes=n)) for n in (3, 2, 1)]
        with mock.patch.object(reminders, "KEEP_DISPATCHES", 3):
            reminders.prune_history(self.NOW)
        remaining = set(Dispatch.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {old_pending.pk, *(row.pk for row in newest)})
        self.assertNotIn(old.pk, remaining)
        self.assertNotIn(kept.pk, remaining)
        with mock.patch.object(reminders, "KEEP_DISPATCHES", 10):
            reminders.prune_history(self.NOW)
        self.assertEqual(Dispatch.objects.count(), 4)

    def test_the_espace_s_dead_devices_go_and_no_other_espace_s(self):
        member = make_login(TEST_TENANT_PK, "serveur@example.invalid")
        long_gone = make_device(member, gone_at=self.NOW - timedelta(days=91))
        lately_gone = make_device(member, gone_at=self.NOW - timedelta(days=89))
        failing = make_device(member, failures=5, created_at=self.NOW - timedelta(days=30))
        failing_but_alive = make_device(member, failures=5, last_success_at=self.NOW - timedelta(days=2))
        failing_since_long = make_device(member, failures=6, last_success_at=self.NOW - timedelta(days=8))
        elsewhere = Tenant.objects.create(name="Bar Ailleurs", dir_name="bar-ailleurs-essai")
        foreign = make_device(
            make_login(elsewhere.pk, "ailleurs@example.invalid"), gone_at=self.NOW - timedelta(days=200)
        )
        reminders.housekeeping(self.NOW)
        remaining = set(PushDevice.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {lately_gone.pk, failing_but_alive.pk, foreign.pk})
        for gone in (long_gone, failing, failing_since_long):
            self.assertNotIn(gone.pk, remaining)

    def test_run_due_does_its_housekeeping(self):
        expired = dispatch(self.NOW - timedelta(hours=2), Dispatch.Status.PENDING)
        reminders.run_due(self.NOW)
        expired.refresh_from_db()
        self.assertEqual(expired.status, Dispatch.Status.FAILED)
        self.assertEqual(NotificationSettings.get_solo().night_ends_at, time(6, 0))


class NightTests(QuietLogs, TestCase):
    def test_the_calendar_night_sends_on_the_ticked_day(self):
        settings = NotificationSettings.get_solo()
        settings.night_ends_at = time(0, 0)
        settings.save()
        three_evenings_reminder(weekdays="6", times="00:00")
        last_tick(SUNDAY_MIDNIGHT - timedelta(minutes=1))
        reminders.run_due(SUNDAY_MIDNIGHT)
        self.assertEqual(Dispatch.objects.get().scheduled_for, SUNDAY_MIDNIGHT)
        self.assertEqual(timezone.localtime(SUNDAY_MIDNIGHT).weekday(), 6)
