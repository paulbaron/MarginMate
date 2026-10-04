"""The till in multi mode: one tenant's sales never land in another's, and
the server's L'Addition account works for the owner's tenant only.

`TwoTenantsTestCase` (accounts/tests/support.py) gives two real tenants in
temporary files: bar A is the owner's (`owner_a`), bar B another bar. The
rule is the spec's: the .env integrations are the owner's own accounts, so
everywhere else the import is « à configurer », refused by the page, the
thread, the session and the commands alike - each is reachable on its own
(a stale page's POST, a thread's target called directly, laddition_open).

Nothing is downloaded and nothing contacts L'Addition: the download and the
browser are patched, and the exports are hand-written workbooks
(test_pos_revenue.write_workbook). Every name, price and day is invented.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import paths
from accounts.tenancy import bound, bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from recipes.models import PosProduct, PosProductDailyQuantity, SalesImportJob
from recipes.pos import laddition_session as session_module
from recipes.tasks import import_laddition_sales_task
from recipes.tests.test_pos_revenue import HEADER, line, write_workbook
from recipes.tests.till_support import LADDITION_ACCOUNT

JUNE = {"start_date": "2026-06-01", "end_date": "2026-06-30"}


def an_export(folder, name="ventes-essai.xlsx", price="7.50") -> str:
    """One till day of one product, written where `folder` says."""
    return write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", price, "20%")], folder=folder, name=name)


def till_day(tenant) -> None:
    """A (till product, day) already imported in `tenant`, its money unread."""
    with bound_tenant(tenant):
        product = PosProduct.objects.create(name="Pinte Exemple", total_quantity=1)
        PosProductDailyQuantity.objects.create(product=product, sold_on=date(2026, 6, 1), quantity=1)


class SalesTabTests(TwoTenantsTestCase):
    """What « Recettes & ventes · Ventes » offers each tenant."""

    owner_a = True

    def tab(self, user) -> str:
        self.client.force_login(user)
        return self.client.get(reverse("recipes:sales_list")).content.decode()

    def test_another_bar_is_told_the_import_is_to_configure(self):
        page = self.tab(self.user_b)
        self.assertIn("à configurer — disponible prochainement dans les réglages de votre espace", page)
        # No form to post, and nothing about the server's own settings.
        self.assertNotIn(reverse("recipes:trigger_sales_import"), page)
        self.assertNotIn("LADDITION_EMAIL", page)
        self.assertNotIn(".env", page)

    @LADDITION_ACCOUNT
    def test_the_owner_s_tenant_keeps_the_form(self):
        page = self.tab(self.user_a)
        self.assertIn(reverse("recipes:trigger_sales_import"), page)
        self.assertNotIn("à configurer", page)


@LADDITION_ACCOUNT
class TriggerTests(TwoTenantsTestCase):
    """The POST that starts the import."""

    owner_a = True

    def post(self, user):
        self.client.force_login(user)
        with mock.patch("recipes.views.threading.Thread") as thread:
            response = self.client.post(reverse("recipes:trigger_sales_import"), JUNE, follow=True)
        return response, thread

    def test_another_bar_s_post_is_refused_and_starts_nothing(self):
        response, thread = self.post(self.user_b)
        thread.assert_not_called()
        self.assertContains(response, "à configurer")
        with bound_tenant(self.bar_b):
            self.assertFalse(SalesImportJob.objects.exists())

    def test_the_owner_s_thread_works_for_the_owner_s_tenant(self):
        _response, thread = self.post(self.user_a)
        thread.assert_called_once()
        target = thread.call_args.kwargs["target"]
        # target=bound(...): the thread binds the tenant that started it,
        # and the arguments are the ones the task has always taken.
        self.assertIs(target.__wrapped__, import_laddition_sales_task)
        self.assertEqual(target.tenant.pk, self.bar_a.pk)
        with bound_tenant(self.bar_a):
            job = SalesImportJob.objects.get()
        self.assertEqual(thread.call_args.kwargs["args"], (job.pk, date(2026, 6, 1), date(2026, 6, 30)))
        with bound_tenant(self.bar_b):
            self.assertFalse(SalesImportJob.objects.exists())

    def test_a_job_running_in_another_tenant_blocks_nothing_here(self):
        """« Une récupération est déjà en cours » is about this tenant's
        jobs: a job of the same pk in another file is another bar's."""
        with bound_tenant(self.bar_b):
            SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        _response, thread = self.post(self.user_a)
        thread.assert_called_once()


class TaskTests(TwoTenantsTestCase):
    """The thread's body, bound as the view binds it."""

    owner_a = True

    def test_the_task_refuses_in_another_bar_before_anything_is_downloaded(self):
        with bound_tenant(self.bar_b):
            job = SalesImportJob.objects.create()
            with mock.patch("recipes.tasks.download_sales_lines") as download:
                import_laddition_sales_task(job.pk, date(2026, 6, 1), date(2026, 6, 30))
            job.refresh_from_db()
        download.assert_not_called()
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertIn("à configurer", job.log)
        self.assertNotIn("Traceback", job.log)
        self.assertIsNotNone(job.finished_at)

    def test_a_real_thread_imports_into_its_tenant_from_its_tenant_s_folder(self):
        seen = {}

        def download(start, end, download_dir, **kwargs):
            seen["folder"] = Path(download_dir)
            return [an_export(download_dir)]

        with bound_tenant(self.bar_a):
            job = SalesImportJob.objects.create(range_start=date(2026, 6, 1), range_end=date(2026, 6, 30))
            target = bound(import_laddition_sales_task)
            own_folder = paths.downloads_dir()
        errors = []

        def guarded(*args):
            try:
                target(*args)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        with mock.patch("recipes.tasks.download_sales_lines", side_effect=download):
            thread = threading.Thread(target=guarded, args=(job.pk, date(2026, 6, 1), date(2026, 6, 30)))
            thread.start()
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(seen["folder"], own_folder)
        with bound_tenant(self.bar_a):
            job.refresh_from_db()
            self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
            self.assertEqual(list(PosProduct.objects.values_list("name", flat=True)), ["Pinte Exemple"])
        with bound_tenant(self.bar_b):
            self.assertFalse(PosProduct.objects.exists())
            self.assertFalse(SalesImportJob.objects.exists())


class SessionTests(TwoTenantsTestCase):
    """The last guard: the L'Addition session itself, whoever calls it."""

    owner_a = True

    def test_no_browser_starts_for_another_bar(self):
        with bound_tenant(self.bar_b):
            with (
                mock.patch.object(session_module, "build_driver") as build,
                mock.patch.object(session_module, "open_report") as open_report,
                self.assertRaises(session_module.LadditionNotAllowed) as caught,
            ):
                with session_module.laddition_session(tempfile.mkdtemp()):
                    pass
        build.assert_not_called()
        open_report.assert_not_called()
        self.assertIn("à configurer", str(caught.exception))
        self.assertNotIn("LADDITION", str(caught.exception))

    def test_no_password_is_typed_for_another_bar(self):
        driver = mock.Mock()
        with (
            bound_tenant(self.bar_b),
            override_settings(LADDITION_EMAIL="caisse@example.invalid", LADDITION_PASSWORD="mot-de-passe-essai"),
            self.assertRaises(session_module.LadditionNotAllowed),
        ):
            session_module.log_in(driver, log=lambda *args: None)
        driver.get.assert_not_called()
        driver.find_element.assert_not_called()

    def test_the_owner_s_tenant_opens_one(self):
        with bound_tenant(self.bar_a):
            with (
                mock.patch.object(session_module, "build_driver") as build,
                mock.patch.object(session_module, "open_report"),
            ):
                with session_module.laddition_session(tempfile.mkdtemp()) as driver:
                    self.assertIs(driver, build.return_value)


class CommandsTests(TwoTenantsTestCase):
    """The L'Addition commands, run for one tenant with
    `manage.py tenant <folder> <command>`."""

    owner_a = True

    def downloads_of(self, tenant) -> Path:
        with bound_tenant(tenant):
            return paths.downloads_dir()

    def run_for(self, tenant, *arguments) -> str:
        out = StringIO()
        call_command("tenant", tenant.dir_name, *arguments, stdout=out)
        return out.getvalue()

    def test_the_revenue_backfill_reads_the_tenant_s_own_exports(self):
        till_day(self.bar_a)
        till_day(self.bar_b)
        an_export(self.downloads_of(self.bar_a))

        output = self.run_for(self.bar_a, "laddition_backfill_revenue")
        self.assertIn("ventes-essai.xlsx", output)
        with bound_tenant(self.bar_a):
            day = PosProductDailyQuantity.objects.get()
        self.assertTrue(day.revenue_read)
        self.assertEqual(day.revenue_ttc, Decimal("7.50"))

        # Bar B's folder is its own, and empty: A's till is not B's money.
        with self.assertRaisesMessage(CommandError, "Aucun fichier .xlsx"):
            self.run_for(self.bar_b, "laddition_backfill_revenue")
        with bound_tenant(self.bar_b):
            self.assertFalse(PosProductDailyQuantity.objects.get().revenue_read)

    def test_the_payments_backfill_reads_the_tenant_s_own_exports(self):
        an_export(self.downloads_of(self.bar_a))
        self.assertIn("ventes-essai.xlsx", self.run_for(self.bar_a, "laddition_backfill_payments", "--dry-run"))
        with self.assertRaisesMessage(CommandError, "Aucun fichier .xlsx"):
            self.run_for(self.bar_b, "laddition_backfill_payments", "--dry-run")

    def test_run_for_no_tenant_they_say_how_to_run_them(self):
        for name, arguments in (
            ("laddition_backfill_revenue", ["--dry-run"]),
            ("laddition_backfill_payments", ["--dry-run"]),
            ("laddition_import", ["--from", "2026-06-01", "--to", "2026-06-30"]),
            ("laddition_open", []),
        ):
            with self.subTest(command=name), self.assertRaises(CommandError) as caught:
                call_command(name, *arguments, stdout=StringIO())
            self.assertIn(f"manage.py tenant <dossier> {name}", str(caught.exception))

    def test_the_import_downloads_nothing_for_another_bar(self):
        with (
            mock.patch("recipes.management.commands.laddition_import.download_sales_lines") as download,
            self.assertRaisesMessage(CommandError, "à configurer"),
        ):
            self.run_for(self.bar_b, "laddition_import", "--from", "2026-06-01", "--to", "2026-06-30")
        download.assert_not_called()

    def test_the_owner_s_import_downloads_into_the_owner_s_folder(self):
        seen = {}

        def download(start, end, download_dir, **kwargs):
            seen["folder"] = Path(download_dir)
            return [an_export(download_dir)]

        with mock.patch("recipes.management.commands.laddition_import.download_sales_lines", side_effect=download):
            self.run_for(self.bar_a, "laddition_import", "--from", "2026-06-01", "--to", "2026-06-30")
        self.assertEqual(seen["folder"], self.downloads_of(self.bar_a))
        with bound_tenant(self.bar_a):
            self.assertTrue(PosProduct.objects.filter(name="Pinte Exemple").exists())
        with bound_tenant(self.bar_b):
            self.assertFalse(PosProduct.objects.exists())

    def test_a_file_named_by_hand_is_read_in_any_tenant(self):
        """--file uses no account: it reads what the operator names."""
        path = an_export(tempfile.mkdtemp())
        self.run_for(self.bar_b, "laddition_import", "--file", path)
        with bound_tenant(self.bar_b):
            self.assertTrue(PosProduct.objects.filter(name="Pinte Exemple").exists())

    def test_laddition_open_signs_in_for_nobody_but_the_owner(self):
        with (
            mock.patch("recipes.management.commands.laddition_open.laddition_session") as session,
            self.assertRaisesMessage(CommandError, "à configurer"),
        ):
            self.run_for(self.bar_b, "laddition_open")
        session.assert_not_called()


class ImportCommandBusyTests(TestCase):
    """`laddition_import` downloading beside the page's own import shares its
    folder and signs in to the same account at the same time: it waits."""

    def run_import(self, *arguments):
        call_command("laddition_import", *arguments, stdout=StringIO())

    def test_a_download_waits_for_the_page_s_import(self):
        SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        with (
            mock.patch("recipes.management.commands.laddition_import.download_sales_lines") as download,
            self.assertRaisesMessage(CommandError, "déjà en cours"),
        ):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30")
        download.assert_not_called()

    def test_a_dead_job_holds_nothing_up(self):
        job = SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        from recipes.tests.test_import_job_recovery import age

        age(job, minutes=30)
        with (
            mock.patch(
                "recipes.management.commands.laddition_import.download_sales_lines",
                side_effect=lambda start, end, folder, **kwargs: [an_export(folder)],
            ) as download,
            override_settings(TENANTS_ROOT=tempfile.mkdtemp()),
        ):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30")
        download.assert_called_once()

    def test_a_file_read_by_hand_is_not_held_up(self):
        SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        self.run_import("--file", an_export(tempfile.mkdtemp()))
        self.assertTrue(PosProduct.objects.filter(name="Pinte Exemple").exists())

    def test_it_downloads_into_the_tenant_s_own_folder(self):
        seen = {}

        def download(start, end, download_dir, **kwargs):
            seen["folder"] = download_dir
            return [an_export(download_dir)]

        with (
            mock.patch("recipes.management.commands.laddition_import.download_sales_lines", side_effect=download),
            override_settings(TENANTS_ROOT=tempfile.mkdtemp()),
        ):
            self.run_import("--from", "2026-06-01", "--to", "2026-06-30")
            self.assertEqual(Path(seen["folder"]), paths.downloads_dir())
