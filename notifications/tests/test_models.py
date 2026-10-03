"""The espace's notification rows (notifications/models.py) and the central
`accounts.PushDevice`: defaults, the singleton, the unique keys, the
savepoint that never poisons a caller, and an admin that shows no secret."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from django.contrib.admin.sites import site
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from accounts.admin import PushDeviceAdmin
from accounts.models import Membership, PushDevice
from notifications import models
from notifications.models import Dispatch, EventRule, NotificationSettings, Reminder
from notifications.tests.support import make_device, make_login
from tests.runner import TEST_TENANT_PK

PARIS = ZoneInfo("Europe/Paris")


def dispatch_fields(**fields):
    values = {
        "kind": Dispatch.Kind.REMINDER,
        "rule_name": "Consignes avant livraison",
        "dedupe_key": "reminder:1:20271002T2200Z",
        "title": "Consignes",
        "ttl": 3600,
    }
    values.update(fields)
    return values


class SettingsTests(TestCase):
    def test_one_row_made_on_first_use_with_its_defaults(self):
        self.assertFalse(NotificationSettings.objects.exists())
        first = NotificationSettings.get_solo()
        self.assertEqual(first.pk, NotificationSettings.SINGLETON_PK)
        self.assertEqual(first.night_ends_at, time(6, 0))
        self.assertIsNone(first.last_tick_at)
        first.night_ends_at = time(4, 0)
        first.save()
        self.assertEqual(NotificationSettings.get_solo().night_ends_at, time(4, 0))
        self.assertEqual(NotificationSettings.objects.count(), 1)


class ReminderTests(TestCase):
    def test_defaults(self):
        reminder = Reminder.objects.create(
            name="Consignes avant livraison", title="Consignes", target="/consignes/", weekdays="0,2,5", times="00:00"
        )
        self.assertEqual(reminder.skip_hours, 6)
        self.assertEqual(reminder.skip_if, "")
        self.assertIsNone(reminder.skip_supplier_id)
        self.assertTrue(reminder.all_members)
        self.assertTrue(reminder.is_active)
        self.assertEqual(reminder.recipient_ids, [])
        self.assertIsNone(reminder.recipients())

    def test_its_days_times_and_recipients_as_stored(self):
        reminder = Reminder(
            weekdays="0,2,5", times="00:00 02:00", all_members=False, recipient_ids=[3, "4", 3, True, 0]
        )
        self.assertEqual(reminder.weekday_list(), (0, 2, 5))
        self.assertEqual(reminder.time_list(), (time(0, 0), time(2, 0)))
        self.assertEqual(reminder.recipients(), [3])
        for weekdays, times in (("lundi", "00:00"), ("1", "")):
            with self.subTest(weekdays=weekdays, times=times), self.assertRaises(ValueError):
                broken = Reminder(weekdays=weekdays, times=times)
                broken.weekday_list()
                broken.time_list()

    def test_skip_hours_are_1_to_48(self):
        from django.core.exceptions import ValidationError

        for hours in (0, 49):
            reminder = Reminder(name="R", title="T", target="/", weekdays="1", times="00:00", skip_hours=hours)
            with self.subTest(hours=hours), self.assertRaises(ValidationError):
                reminder.full_clean()

    def test_the_caps_are_said(self):
        self.assertEqual(models.MAX_REMINDERS, 50)
        self.assertEqual(models.TOO_MANY_REMINDERS, "50 rappels au plus.")


class EventRuleTests(TestCase):
    def test_one_rule_per_event(self):
        rule = EventRule.objects.create(event="returnables-comparison", outcomes=["match", "differs"])
        self.assertFalse(rule.all_members)
        self.assertTrue(rule.is_active)
        self.assertTrue(rule.wants("differs"))
        self.assertFalse(rule.wants("no_pickup"))
        with transaction.atomic(), self.assertRaises(IntegrityError):
            EventRule.objects.create(event="returnables-comparison", outcomes=["match"])

    def test_outcomes_that_are_no_list_want_nothing(self):
        self.assertFalse(EventRule(outcomes="match").wants("match"))


class DispatchTests(TestCase):
    def test_defaults(self):
        dispatch = Dispatch.objects.create(**dispatch_fields())
        self.assertEqual(dispatch.status, Dispatch.Status.PENDING)
        self.assertEqual((dispatch.devices, dispatch.delivered), (0, 0))
        self.assertIsNone(dispatch.recipient_ids)
        self.assertIsNone(dispatch.sent_at)
        self.assertEqual(dispatch.get_status_display(), "en attente")

    def test_a_dedupe_key_is_inserted_once(self):
        first = models.create_dispatch(**dispatch_fields())
        self.assertIsNotNone(first)
        self.assertIsNone(models.create_dispatch(**dispatch_fields(title="Autre")))
        self.assertEqual(Dispatch.objects.get().title, "Consignes")

    def test_a_duplicate_inside_a_caller_s_transaction_leaves_its_work_alone(self):
        models.create_dispatch(**dispatch_fields())
        with transaction.atomic():
            Reminder.objects.create(name="Gardé", title="T", target="/", weekdays="1", times="00:00")
            self.assertIsNone(models.create_dispatch(**dispatch_fields()))
            # The transaction is still usable.
            Reminder.objects.create(name="Gardé aussi", title="T", target="/", weekdays="1", times="00:00")
        self.assertEqual(sorted(Reminder.objects.values_list("name", flat=True)), ["Gardé", "Gardé aussi"])

    def test_another_integrity_error_is_raised(self):
        with self.assertRaises(IntegrityError):
            models.create_dispatch(**dispatch_fields(ttl=None))

    def test_texts_are_cut_to_their_columns(self):
        dispatch = models.create_dispatch(**dispatch_fields(title="T" * 100, body="B" * 500, detail="D" * 400))
        dispatch.refresh_from_db()
        self.assertEqual(len(dispatch.title), 80)
        self.assertTrue(dispatch.title.endswith("…"))
        self.assertEqual(len(dispatch.body), 400)
        self.assertEqual(len(dispatch.detail), 300)


class KeyTests(TestCase):
    def test_a_reminder_instant_is_keyed_in_utc(self):
        local = datetime(2027, 10, 3, 0, 0, tzinfo=PARIS)
        self.assertEqual(models.reminder_key(7, local), "reminder:7:20271002T2200Z")
        self.assertEqual(models.reminder_key(7, local.astimezone(UTC)), "reminder:7:20271002T2200Z")

    def test_an_event_key_fits_its_column(self):
        self.assertEqual(models.event_key(3, "consignes:abc"), "event:3:consignes:abc")
        long = "gather-failed:1:" + ",".join(f"type-{n}" for n in range(60)) + ":2027-10-02"
        key = models.event_key(3, long)
        self.assertLessEqual(len(key), models.DEDUPE_KEY_MAX_LENGTH)
        self.assertEqual(key, models.event_key(3, long))
        self.assertNotEqual(key, models.event_key(3, long + "x"))

    def test_every_test_is_its_own(self):
        self.assertNotEqual(models.test_key(), models.test_key())
        self.assertTrue(models.test_key().startswith("test:"))

    def test_ids_are_cleaned(self):
        self.assertEqual(models.clean_ids([2, 2, "3", None, -1, 0, True, 5]), [2, 5])
        self.assertEqual(models.clean_ids("2,3"), [])


class PushDeviceTests(TestCase):
    def setUp(self):
        self.membership = make_login(TEST_TENANT_PK, "serveur@example.invalid")

    def test_defaults_and_what_it_says_of_itself(self):
        device = make_device(self.membership)
        self.assertEqual(device.failures, 0)
        self.assertIsNone(device.gone_at)
        self.assertIsNone(device.logged_out_at)
        self.assertEqual(device.endpoint_host, "fcm.googleapis.com")
        self.assertNotIn("jeton-invente", str(device))
        self.assertEqual(PushDevice.objects.using("accounts").filter(pk=device.pk).count(), 1)

    def test_an_endpoint_is_one_device(self):
        device = make_device(self.membership)
        with transaction.atomic(using="accounts"), self.assertRaises(IntegrityError):
            make_device(self.membership, endpoint=device.endpoint)

    def test_a_membership_takes_its_devices_along(self):
        make_device(self.membership)
        Membership.objects.filter(pk=self.membership.pk).delete()
        self.assertFalse(PushDevice.objects.exists())

    def test_the_admin_shows_no_secret_and_adds_nothing(self):
        device = make_device(self.membership)
        admin = site._registry[PushDevice]
        self.assertIsInstance(admin, PushDeviceAdmin)
        request = RequestFactory().get("/admin/")
        request.user = self.membership.user
        fields = admin.get_fields(request, device)
        for secret in ("endpoint", "p256dh", "auth"):
            self.assertNotIn(secret, fields)
            self.assertNotIn(secret, admin.list_display)
        self.assertIn("endpoint_host", fields)
        self.assertEqual(admin.endpoint_host(device), "fcm.googleapis.com")
        self.assertFalse(admin.has_add_permission(request))


class MigrationTests(TestCase):
    def test_the_models_and_their_migrations_agree(self):
        call_command("makemigrations", "accounts", "notifications", check=True, dry_run=True, verbosity=0)
