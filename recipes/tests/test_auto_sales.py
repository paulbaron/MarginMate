"""The automatic sales imports' ticks (recipes/auto_sales.py, on the slot
machinery of notifications/automation.py): their instants (calendar days ×
times), the 12-hour catch-up, the claim, the slot given back while another
import runs, the deploy mark, the dev server, an unreadable rule, one rule's
failure kept to itself, « à jour » without a job, and an unknown source.

Nothing is imported for real: the import's thread is patched
(`recipes.importing.threading.Thread`), so `download_sales_lines` is never
reached. Dates invented, in the future.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

from django.db import DatabaseError, OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.module_loading import import_string

from invoices.models import GatherCoverage
from notifications import automation
from notifications.models import NotificationSettings
from recipes import auto_sales
from recipes.models import AutoSalesImport, SalesImportJob
from recipes.tasks import import_laddition_sales_task

#: Wednesday 18/11/2026, 07:00 in Paris (winter time: 06:00 UTC).
WEDNESDAY = date(2026, 11, 18)
SEVEN = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
YESTERDAY = WEDNESDAY - timedelta(days=1)
CODE = "ventes-laddition"


def at(hours=0, minutes=0) -> datetime:
    return SEVEN + timedelta(hours=hours, minutes=minutes)


def cover(until) -> None:
    """The till's sales imported without a gap up to `until`."""
    GatherCoverage.objects.update_or_create(code=CODE, defaults={"searched_until": until})


class RunDueCase(TestCase):
    def setUp(self):
        super().setUp()
        self.base_dir = tempfile.mkdtemp()
        settings = override_settings(BASE_DIR=Path(self.base_dir))
        settings.enable()
        self.addCleanup(settings.disable)

    def rule(self, **fields) -> AutoSalesImport:
        values = {
            "name": "Ventes exemple",
            "source": "laddition",
            "weekdays": "0,1,2,3,4,5,6",
            "times": "07:00",
            "created_at": SEVEN - timedelta(days=7),
            **fields,
        }
        return AutoSalesImport.objects.create(**values)

    def run_due(self, now, *, enabled=True):
        with (
            mock.patch("notifications.webpush.sending_enabled", return_value=enabled),
            mock.patch("recipes.importing.threading.Thread") as thread,
        ):
            auto_sales.run_due(now)
        return thread

    def result(self, rule) -> str:
        rule.refresh_from_db()
        return rule.last_result


class SlotTests(RunDueCase):
    def test_the_instants_are_calendar_days_times_their_times(self):
        rule = self.rule(weekdays="2", times="07:00 12:00", created_at=at(hours=-1))
        self.assertIsNone(auto_sales.due_slot(rule, at(minutes=-1)))
        self.assertEqual(auto_sales.due_slot(rule, at(minutes=30)), at())
        self.assertEqual(auto_sales.due_slot(rule, at(hours=5, minutes=30)), at(hours=5))

    def test_a_day_not_ticked_has_no_slot(self):
        rule = self.rule(weekdays="0,4", created_at=at(hours=-1))
        self.assertIsNone(auto_sales.due_slot(rule, at(minutes=5)))

    def test_no_night_shifts_a_time_before_the_night_s_end(self):
        """The reminders' night is not the imports': 02:00 on Wednesday is
        Wednesday 02:00, whatever « La nuit se termine à » says."""
        NotificationSettings.objects.create(pk=NotificationSettings.SINGLETON_PK, night_ends_at=time(6, 0))
        rule = self.rule(weekdays="2", times="02:00", created_at=at(hours=-6))
        self.assertEqual(auto_sales.due_slot(rule, at(hours=-4)), at(hours=-5))

    def test_the_scheduler_finds_the_job_by_its_name(self):
        self.assertIs(import_string("recipes.auto_sales.run_due"), auto_sales.run_due)


class LaunchTests(RunDueCase):
    def test_a_due_slot_starts_the_import_of_the_period(self):
        cover(WEDNESDAY - timedelta(days=3))
        rule = self.rule()
        thread = self.run_due(at(minutes=1))
        job = SalesImportJob.objects.get()
        self.assertEqual((job.range_start, job.range_end), (date(2026, 11, 12), YESTERDAY))
        self.assertEqual((job.trigger, job.auto_rule_id), (SalesImportJob.Trigger.AUTOMATIC, rule.pk))
        self.assertEqual(self.result(rule), f"lancé à 07:01 (import n° {job.pk}, du 12/11 au 17/11)")
        self.assertEqual(rule.last_slot_at, at())
        thread.return_value.start.assert_called_once()
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, import_laddition_sales_task)
        self.assertEqual(thread.call_args.kwargs["args"], (job.pk, date(2026, 11, 12), YESTERDAY))

    def test_a_slot_is_caught_up_within_twelve_hours(self):
        rule = self.rule()
        thread = self.run_due(at(hours=11, minutes=59))
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.result(rule).startswith("lancé à 18:59"))

    def test_an_older_slot_is_missed(self):
        rule = self.rule()
        thread = self.run_due(at(hours=12, minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "manquée : serveur arrêté à 07:00")
        self.assertFalse(SalesImportJob.objects.exists())

    def test_the_limit(self):
        self.assertEqual(auto_sales.SALES.catch_up_limit(self.rule()), timedelta(hours=12))


class ClaimTests(RunDueCase):
    def test_a_slot_is_taken_once_by_two_ticks(self):
        rule = self.rule()
        first = self.run_due(at(minutes=1))
        # Started at the tick's moment (the clock is the test's), so the
        # prune of the second tick keeps it.
        SalesImportJob.objects.update(status=SalesImportJob.Status.SUCCESS, started_at=at(minutes=1))
        second = self.run_due(at(minutes=1))
        first.return_value.start.assert_called_once()
        second.assert_not_called()
        rule.refresh_from_db()
        self.assertEqual(rule.last_slot_at, at())
        self.assertEqual(SalesImportJob.objects.count(), 1)

    def test_the_claim_is_conditional_on_what_was_read(self):
        self.rule()
        mine, theirs = AutoSalesImport.objects.get(), AutoSalesImport.objects.get()
        self.assertTrue(auto_sales.SALES.claim(mine, at()))
        self.assertFalse(auto_sales.SALES.claim(theirs, at()))


class WaitingTests(RunDueCase):
    def running(self) -> SalesImportJob:
        job = SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        job.beat()
        return job

    def test_an_import_running_gives_the_slot_back(self):
        self.running()
        rule = self.rule()
        thread = self.run_due(at(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), auto_sales.WAITING)
        self.assertEqual(auto_sales.WAITING, "en attente : un import des ventes est en cours")
        self.assertIsNone(rule.last_slot_at)
        self.assertEqual(SalesImportJob.objects.count(), 1)

    def test_the_next_tick_starts_it_once_the_other_has_finished(self):
        other = self.running()
        rule = self.rule()
        self.run_due(at(minutes=1))
        SalesImportJob.objects.filter(pk=other.pk).update(status=SalesImportJob.Status.SUCCESS)
        thread = self.run_due(at(minutes=2))
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.result(rule).startswith("lancé à 07:02"))
        self.assertEqual(rule.last_slot_at, at())

    def test_waiting_past_the_limit_is_said_as_such(self):
        self.running()
        rule = self.rule()
        self.run_due(at(minutes=1))
        SalesImportJob.objects.update(last_heartbeat=timezone.now())
        self.run_due(at(hours=12, minutes=1))
        self.assertEqual(self.result(rule), "manquée : un import des ventes était en cours à 07:00")

    def test_a_database_locked_at_the_launch_gives_the_slot_back(self):
        rule = self.rule()
        with (
            mock.patch("recipes.auto_sales.sales_sources.start", side_effect=OperationalError("database is locked")),
            self.assertLogs("recipes.auto_sales", "WARNING"),
        ):
            self.run_due(at(minutes=1))
        self.assertEqual(self.result(rule), automation.BUSY)
        self.assertIsNone(rule.last_slot_at)


class SkipTests(RunDueCase):
    def test_a_development_server_never_imports(self):
        rule = self.rule()
        thread = self.run_due(at(minutes=1), enabled=False)
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "sautée : serveur de développement")
        self.assertEqual(rule.last_slot_at, at())
        self.assertFalse(SalesImportJob.objects.exists())

    def test_the_deploy_mark_skips_the_slot(self):
        (Path(self.base_dir) / ".git" / "marginmate-deploy").mkdir(parents=True)
        rule = self.rule()
        thread = self.run_due(at(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "sautée : mise à jour du site en cours")

    def test_an_unreadable_rule_is_claimed_once_and_said(self):
        for fields in ({"weekdays": "9"}, {"times": "25:00"}, {"times": ""}):
            with self.subTest(fields=fields):
                broken = self.rule(**fields)
                with self.assertLogs("recipes.auto_sales", "WARNING") as logs:
                    self.run_due(at(minutes=1))
                self.assertEqual(len(logs.output), 1)
                self.assertEqual(self.result(broken), "sautée : jours ou heures illisibles")
                self.assertEqual(broken.last_slot_at, at(minutes=1))
                with self.assertNoLogs("recipes.auto_sales", "WARNING"):
                    self.run_due(at(minutes=2))
                broken.refresh_from_db()
                self.assertEqual(broken.last_slot_at, at(minutes=1))
                broken.delete()

    def test_an_unknown_source_is_skipped_never_a_500(self):
        rule = self.rule(source="caisse-disparue")
        thread = self.run_due(at(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "sautée : source inconnue")

    def test_an_inactive_rule_is_not_run(self):
        rule = self.rule(is_active=False)
        self.run_due(at(minutes=1))
        self.assertEqual(self.result(rule), "")

    def test_nothing_where_the_till_may_not_be_used(self):
        rule = self.rule()
        with mock.patch("recipes.auto_sales.till_allowed", return_value=False):
            thread = self.run_due(at(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "")


class UpToDateTests(RunDueCase):
    def test_nothing_new_claims_the_slot_and_starts_no_import(self):
        cover(YESTERDAY)
        rule = self.rule()
        thread = self.run_due(at(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "à jour : ventes importées jusqu'au 17/11")
        self.assertEqual(rule.last_slot_at, at())
        self.assertFalse(SalesImportJob.objects.exists())

    def test_a_second_slot_the_same_day_after_a_success_signs_in_nowhere(self):
        """Never Metro-like hammering: a rule at 07:00 and 12:00 imports once
        a day - the second slot finds yesterday already in."""
        cover(WEDNESDAY - timedelta(days=4))
        rule = self.rule(times="07:00 12:00")
        first = self.run_due(at(minutes=1))
        first.return_value.start.assert_called_once()
        job = SalesImportJob.objects.get()
        # The import ends well (its thread is the task's: finished() is what
        # the task calls once its status is saved).
        SalesImportJob.objects.filter(pk=job.pk).update(
            status=SalesImportJob.Status.SUCCESS, started_at=at(minutes=1), finished_at=at(minutes=20), recorded=12
        )
        job.refresh_from_db()
        with mock.patch("notifications.events.emit"):
            auto_sales.finished(job, source_key="laddition", own=WEDNESDAY - timedelta(days=90))
        self.assertEqual(GatherCoverage.objects.get(code=CODE).searched_until, YESTERDAY)
        second = self.run_due(at(hours=5, minutes=1))
        second.assert_not_called()
        self.assertEqual(self.result(rule), "à jour : ventes importées jusqu'au 17/11")
        self.assertEqual(SalesImportJob.objects.count(), 1)


class IsolationTests(RunDueCase):
    def test_a_launch_raising_says_so_and_keeps_its_slot(self):
        rule = self.rule()
        with (
            mock.patch("recipes.auto_sales.sales_sources.start", side_effect=RuntimeError("panne")),
            self.assertLogs("recipes.auto_sales", "ERROR"),
        ):
            self.run_due(at(minutes=1))
        self.assertEqual(self.result(rule), "échec : erreur interne à 07:01")
        self.assertEqual(rule.last_slot_at, at())

    def test_one_rule_raising_anywhere_leaves_the_others_alone(self):
        self.rule()
        other = self.rule(name="Ventes du midi")
        real = auto_sales.run_rule

        def run_rule(rule, now, *, enabled):
            if rule.pk != other.pk:
                raise DatabaseError("database is locked")
            return real(rule, now, enabled=enabled)

        with (
            mock.patch("recipes.auto_sales.run_rule", side_effect=run_rule),
            self.assertLogs("recipes.auto_sales", "ERROR"),
        ):
            self.run_due(at(minutes=1))
        self.assertTrue(self.result(other).startswith("lancé"))

    def test_a_result_that_cannot_be_written_is_logged_never_raised(self):
        self.rule()
        with (
            mock.patch.object(auto_sales.SALES, "say", side_effect=DatabaseError("database is locked")),
            self.assertLogs("recipes.auto_sales", "WARNING") as logs,
        ):
            self.run_due(at(minutes=1))
        self.assertIn("résultat non enregistré", "\n".join(logs.output))


class PruneTests(RunDueCase):
    def test_automatic_imports_older_than_thirty_days_go_never_the_manual_ones(self):
        old = SalesImportJob.objects.create(
            trigger=SalesImportJob.Trigger.AUTOMATIC, status=SalesImportJob.Status.SUCCESS
        )
        recent = SalesImportJob.objects.create(
            trigger=SalesImportJob.Trigger.AUTOMATIC, status=SalesImportJob.Status.SUCCESS
        )
        manual = SalesImportJob.objects.create(status=SalesImportJob.Status.SUCCESS)
        now = timezone.now()
        SalesImportJob.objects.filter(pk__in=[old.pk, manual.pk]).update(started_at=now - timedelta(days=31))
        self.assertEqual(auto_sales.prune(now), 1)
        self.assertEqual(set(SalesImportJob.objects.values_list("pk", flat=True)), {recent.pk, manual.pk})
