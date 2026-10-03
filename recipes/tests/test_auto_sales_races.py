"""The automatic sales imports beside the other imports of the till: the
period read under the start's lock (an import ending while it is read
starts no second one), a SUCCESS and its coverage committed together, a
rule that waited behind an import that then failed or was cancelled keeping
its slot, and `manage.py laddition_import` holding the same lock as the
Ventes tab and the scheduler.

Nothing is imported for real: the import's thread is patched
(`recipes.importing.threading.Thread`), and so are `download_sales_lines`
and the file's reading. Dates invented, in the future for the ticks.
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from invoices.models import GatherCoverage
from recipes import auto_sales, importing
from recipes.models import SalesImportJob
from recipes.pos.laddition_download import DownloadCancelled
from recipes.pos.laddition_session import LadditionAuthError
from recipes.pos.laddition_xlsx import ParsedExport
from recipes.tasks import import_laddition_sales_task
from recipes.tests.test_auto_sales import CODE, WEDNESDAY, YESTERDAY, RunDueCase, at, cover
from recipes.tests.test_tenants import an_export
from tests.factories import make_recipe

Status = SalesImportJob.Status
COMMAND = "recipes.management.commands.laddition_import.download_sales_lines"


def running(**fields) -> SalesImportJob:
    job = SalesImportJob.objects.create(status=Status.RUNNING, **fields)
    job.beat()
    return job


class PeriodUnderTheLockTests(RunDueCase):
    def test_an_import_ending_as_the_period_is_read_starts_no_second_one(self):
        """The other import's SUCCESS and its coverage land right after this
        rule read its period: it must not start a second import of days just
        imported (one more sign-in to L'Addition)."""
        cover(WEDNESDAY - timedelta(days=4))
        other = running(range_start=WEDNESDAY - timedelta(days=7), range_end=YESTERDAY)
        rule = self.rule()
        real = auto_sales.period_for

        def period_for(key, now):
            period = real(key, now)
            SalesImportJob.objects.filter(pk=other.pk).update(status=Status.SUCCESS, finished_at=timezone.now())
            cover(YESTERDAY)
            return period

        with mock.patch("recipes.auto_sales.period_for", side_effect=period_for):
            thread = self.run_due(at(minutes=1))
        thread.assert_not_called()
        self.assertEqual(SalesImportJob.objects.count(), 1)
        self.assertEqual(self.result(rule), auto_sales.WAITING)

        # Read under the lock, the period was never read while the other ran:
        # it ends now, its status and its coverage together.
        SalesImportJob.objects.filter(pk=other.pk).update(status=Status.SUCCESS, finished_at=timezone.now())
        cover(YESTERDAY)
        thread = self.run_due(at(minutes=2))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "à jour : ventes importées jusqu'au 17/11")
        self.assertEqual(SalesImportJob.objects.count(), 1)

    def test_the_period_is_read_after_the_active_check_in_its_transaction(self):
        cover(WEDNESDAY - timedelta(days=4))
        self.rule()
        order = []
        real_check, real_period = importing.active_import, auto_sales.period_for

        def check(*args, **kwargs):
            order.append(("check", len(connection.savepoint_ids)))
            return real_check(*args, **kwargs)

        def period_for(*args, **kwargs):
            order.append(("period", len(connection.savepoint_ids)))
            return real_period(*args, **kwargs)

        outside = len(connection.savepoint_ids)
        with (
            mock.patch("recipes.importing.active_import", side_effect=check),
            mock.patch("recipes.auto_sales.period_for", side_effect=period_for),
        ):
            thread = self.run_due(at(minutes=1))
        self.assertEqual(order, [("check", outside + 1), ("period", outside + 1)])
        thread.return_value.start.assert_called_once()

    def test_up_to_date_under_the_lock_creates_nothing(self):
        cover(YESTERDAY)
        with mock.patch("recipes.importing.threading.Thread") as thread:
            result = importing.start_sales_import(plan=lambda: "à jour")
        self.assertEqual(result, "à jour")
        thread.assert_not_called()
        self.assertFalse(SalesImportJob.objects.exists())


class SuccessAndCoverageTests(TestCase):
    """The task's SUCCESS and the coverage it records commit together: no
    tick can see the import over and its days not covered yet."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        make_recipe(name="Pinte Exemple", selling_price_ttc="6.50")

    def day(self, back: int) -> date:
        return self.today - timedelta(days=back)

    def test_the_success_is_saved_in_the_transaction_that_records_the_coverage(self):
        start, end = self.day(10), self.day(2)
        cover(self.day(11))
        export = ParsedExport(
            products={"Pinte Exemple": {"quantity": 3, "category": "", "typology": "", "first": start, "last": start}},
            entries=[("Pinte Exemple", start, 3)],
        )
        job = SalesImportJob.objects.create(range_start=start, range_end=end)
        seen = {}
        real_save = SalesImportJob.save

        def save(instance, *args, **kwargs):
            if instance.status == Status.SUCCESS and "status" in (kwargs.get("update_fields") or ()):
                seen["status"] = tuple(connection.savepoint_ids)
            return real_save(instance, *args, **kwargs)

        from invoices import coverage

        real_searched = coverage.searched

        def searched(*args, **kwargs):
            seen["coverage"] = tuple(connection.savepoint_ids)
            return real_searched(*args, **kwargs)

        outside = len(connection.savepoint_ids)
        with (
            mock.patch("recipes.tasks.download_sales_lines", return_value=["ventes.xlsx"]),
            mock.patch("recipes.tasks.parse_sales_exports", return_value=export),
            mock.patch("notifications.events.emit"),
            mock.patch.object(SalesImportJob, "save", autospec=True, side_effect=save),
            mock.patch("invoices.coverage.searched", side_effect=searched),
        ):
            import_laddition_sales_task(job.pk, start, end, download_dir="non-utilisé")
        job.refresh_from_db()
        self.assertEqual(job.status, Status.SUCCESS)
        self.assertEqual(GatherCoverage.objects.get(code=CODE).searched_until, end)
        self.assertGreater(len(seen["status"]), outside)
        self.assertEqual(seen["coverage"][: len(seen["status"])], seen["status"])

    def test_a_coverage_that_cannot_be_recorded_keeps_the_success(self):
        start, end = self.day(10), self.day(2)
        cover(self.day(11))
        export = ParsedExport(
            products={"Pinte Exemple": {"quantity": 3, "category": "", "typology": "", "first": start, "last": start}},
            entries=[("Pinte Exemple", start, 3)],
        )
        job = SalesImportJob.objects.create(range_start=start, range_end=end)
        with (
            mock.patch("recipes.tasks.download_sales_lines", return_value=["ventes.xlsx"]),
            mock.patch("recipes.tasks.parse_sales_exports", return_value=export),
            mock.patch("notifications.events.emit"),
            mock.patch("invoices.coverage.searched", side_effect=RuntimeError("panne")),
            self.assertLogs("recipes.auto_sales", "ERROR") as logs,
        ):
            import_laddition_sales_task(job.pk, start, end, download_dir="non-utilisé")
        job.refresh_from_db()
        self.assertEqual(job.status, Status.SUCCESS)
        self.assertIsNotNone(job.finished_at)
        self.assertIn("couverture non enregistrée", "\n".join(logs.output))
        self.assertEqual(GatherCoverage.objects.get(code=CODE).searched_until, self.day(11))


class EndedWhileWaitingTests(RunDueCase):
    def test_an_import_that_failed_or_was_cancelled_while_it_waited_skips_the_slot(self):
        """The owner cancelled it, or L'Addition refused the sign-in: the
        rule waiting behind it does not sign in again a minute later. Its
        next slot does."""
        for status in (Status.CANCELLED, Status.FAILED):
            with self.subTest(status=status):
                other = running()
                rule = self.rule(times="07:00 12:00")
                self.run_due(at(minutes=1))
                self.assertEqual(self.result(rule), auto_sales.WAITING)
                SalesImportJob.objects.filter(pk=other.pk).update(status=status, finished_at=at(minutes=2))

                thread = self.run_due(at(minutes=3))
                thread.assert_not_called()
                self.assertEqual(self.result(rule), auto_sales.ENDED_WHILE_WAITING)
                self.assertEqual(rule.last_slot_at, at())
                self.assertEqual(SalesImportJob.objects.count(), 1)

                thread = self.run_due(at(hours=5, minutes=1))
                thread.return_value.start.assert_called_once()
                self.assertTrue(self.result(rule).startswith("lancé à 12:01"))
                rule.delete()
                SalesImportJob.objects.all().delete()

    def test_a_success_while_it_waited_still_lets_it_start(self):
        other = running()
        rule = self.rule()
        self.run_due(at(minutes=1))
        SalesImportJob.objects.filter(pk=other.pk).update(status=Status.SUCCESS, finished_at=at(minutes=2))
        thread = self.run_due(at(minutes=3))
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.result(rule).startswith("lancé à 07:03"))

    def test_a_failure_it_did_not_wait_for_does_not_skip_it(self):
        """Its first try at the slot: a failure it never waited behind is
        not its own."""
        SalesImportJob.objects.create(status=Status.FAILED, finished_at=at() + timedelta(seconds=30))
        rule = self.rule()
        thread = self.run_due(at(minutes=1))
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.result(rule).startswith("lancé à 07:01"))

    def test_the_sentence(self):
        self.assertEqual(auto_sales.ENDED_WHILE_WAITING, "sautée : l'import en cours vient d'échouer ou d'être annulé")


@override_settings(TENANTS_ROOT=tempfile.mkdtemp())
class CommandHoldsTheLockTests(TestCase):
    """`manage.py laddition_import` downloading holds the one sales import's
    lock: neither the scheduler nor the Ventes tab starts an import beside
    it - the two would sign in to one account at once and each could take
    the other's file from the shared folder."""

    def run_import(self, *arguments):
        call_command("laddition_import", *arguments, stdout=StringIO())

    def test_no_sales_import_starts_while_the_command_downloads(self):
        seen = {}

        def download(start, end, folder, **kwargs):
            with mock.patch("recipes.importing.threading.Thread") as thread:
                seen["started"] = importing.start_sales_import(date(2026, 6, 1), date(2026, 6, 30))
            seen["thread"] = thread
            return [an_export(folder)]

        with mock.patch(COMMAND, side_effect=download):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30")
        self.assertIsNone(seen["started"])
        seen["thread"].assert_not_called()
        job = SalesImportJob.objects.get()
        self.assertEqual(job.status, Status.SUCCESS)
        self.assertEqual((job.range_start, job.range_end), (date(2026, 6, 1), date(2026, 6, 30)))
        self.assertEqual(job.trigger, SalesImportJob.Trigger.MANUAL)
        self.assertIsNotNone(job.finished_at)
        self.assertFalse(importing.active_import())

    def test_its_download_keeps_the_job_alive(self):
        """The heartbeat: a run silent for ten minutes is reaped as dead."""
        seen = {}

        def download(start, end, folder, *, should_cancel, **kwargs):
            job = SalesImportJob.objects.get()
            SalesImportJob.objects.filter(pk=job.pk).update(last_heartbeat=None)
            seen["wanted"] = should_cancel()
            seen["beat"] = SalesImportJob.objects.get().last_heartbeat
            seen["status"] = SalesImportJob.objects.get().status
            return [an_export(folder)]

        with mock.patch(COMMAND, side_effect=download):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30")
        self.assertFalse(seen["wanted"])
        self.assertIsNotNone(seen["beat"])
        self.assertEqual(seen["status"], Status.RUNNING)

    def test_a_refused_sign_in_ends_its_job_failed(self):
        with (
            mock.patch(COMMAND, side_effect=LadditionAuthError("Identifiants refusés.")),
            self.assertRaisesMessage(CommandError, "Identifiants refusés."),
        ):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30")
        job = SalesImportJob.objects.get()
        self.assertEqual(job.status, Status.FAILED)
        self.assertIsNotNone(job.finished_at)

    def test_cancelled_from_the_ventes_tab(self):
        def download(start, end, folder, *, should_cancel, **kwargs):
            SalesImportJob.objects.update(cancel_requested=True)
            if should_cancel():
                raise DownloadCancelled()
            return [an_export(folder)]

        with mock.patch(COMMAND, side_effect=download), self.assertRaisesMessage(CommandError, "Annulé"):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30")
        self.assertEqual(SalesImportJob.objects.get().status, Status.CANCELLED)

    def test_a_dry_run_leaves_no_job(self):
        with mock.patch(COMMAND, side_effect=lambda start, end, folder, **kwargs: [an_export(folder)]):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30", "--dry-run")
        self.assertFalse(SalesImportJob.objects.exists())

    def test_a_file_read_by_hand_makes_no_job(self):
        self.run_import("--file", an_export(tempfile.mkdtemp()))
        self.assertFalse(SalesImportJob.objects.exists())
