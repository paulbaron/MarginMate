"""The till in multi mode: one tenant's sales never land in another's, and
each fetches them with its own L'Addition account.

`TwoTenantsTestCase` (accounts/tests/support.py) gives two real tenants in
temporary files: bar A is the platform owner's (`owner_a`), bar B another
bar. Every espace fetches its sales with the account typed on its own
« Identifiants » page; the server's .env values stand in for the owner's
espace alone - B, with nothing typed, never signs in with them. Unbound,
the page, the thread, the session and the commands refuse alike - each is
reachable on its own. `laddition_open`, which shows a till in a browser of
the server, is the owner's.

Nothing is downloaded and nothing contacts L'Addition: the download and the
browser are patched, and the exports are hand-written workbooks
(test_pos_revenue.write_workbook). Every name, price and day is invented.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from accounts import paths
from accounts.tenancy import bound, bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from common import SERVER_ERROR
from recipes import auto_sales
from recipes.integration import TILL_LOGIN_MISSING
from recipes.management.commands.laddition_open import OWNER_ONLY
from recipes.models import AutoSalesImport, PosProduct, PosProductDailyQuantity, SalesImportJob
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

    def beta_s_account(self) -> None:
        """Beta's L'Addition account, typed on its own « Identifiants »."""
        from accounts import vault

        with bound_tenant(self.bar_b):
            vault.save({"LADDITION_EMAIL": "caisse-beta@example.invalid", "LADDITION_PASSWORD": "secret-beta"})

    def test_another_bar_is_offered_the_import(self):
        # Its own account (its « Identifiants »): the form is drawn there too.
        self.beta_s_account()
        page = self.tab(self.user_b)
        self.assertIn(reverse("recipes:trigger_sales_import"), page)
        self.assertNotIn("à configurer", page)

    @LADDITION_ACCOUNT
    def test_another_bar_s_tab_names_no_server_variable(self):
        # The owner's .env values in the settings, Beta's own account typed:
        # the card is drawn, and says nothing of the server's.
        self.beta_s_account()
        page = self.tab(self.user_b)
        self.assertIn(reverse("recipes:trigger_sales_import"), page)
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

    def test_another_bar_s_thread_works_for_another_bar(self):
        # Its own account: the owner's .env values (LADDITION_ACCOUNT) are
        # never another bar's (vault.server_setting).
        from accounts import vault

        with bound_tenant(self.bar_b):
            vault.save({"LADDITION_EMAIL": "caisse-beta@example.invalid", "LADDITION_PASSWORD": "secret-beta"})
        _response, thread = self.post(self.user_b)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["target"].tenant.pk, self.bar_b.pk)
        with bound_tenant(self.bar_b):
            self.assertTrue(SalesImportJob.objects.exists())
        with bound_tenant(self.bar_a):
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

    def test_the_task_refuses_unbound_before_anything_is_downloaded(self):
        job = SalesImportJob.objects.create()
        with mock.patch("recipes.tasks.download_sales_lines") as download:
            import_laddition_sales_task(job.pk, date(2026, 6, 1), date(2026, 6, 30))
        job.refresh_from_db()
        download.assert_not_called()
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertIn("à configurer", job.log)
        self.assertNotIn("Traceback", job.log)
        self.assertIsNotNone(job.finished_at)

    def test_another_bar_s_task_downloads_into_its_own_folder(self):
        seen = {}

        def download(start, end, download_dir, **kwargs):
            seen["folder"] = Path(download_dir)
            return [an_export(download_dir)]

        with bound_tenant(self.bar_b):
            job = SalesImportJob.objects.create()
            own_folder = paths.downloads_dir()
            with mock.patch("recipes.tasks.download_sales_lines", side_effect=download):
                import_laddition_sales_task(job.pk, date(2026, 6, 1), date(2026, 6, 30))
            job.refresh_from_db()
            self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
        self.assertEqual(seen["folder"], own_folder)

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

    def test_no_browser_starts_unbound(self):
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

    def test_the_server_s_account_is_never_typed_for_another_bar(self):
        """B typed nothing on its « Identifiants »: the .env's values - in
        the settings of the one process every espace runs in - are the owner's
        alone (accounts.vault.server_setting)."""
        driver = mock.Mock()
        with (
            bound_tenant(self.bar_b),
            override_settings(LADDITION_EMAIL="caisse@example.invalid", LADDITION_PASSWORD="mot-de-passe-essai"),
            self.assertRaises(session_module.LadditionAuthError) as caught,
        ):
            session_module.log_in(driver, log=lambda *args: None)
        driver.get.assert_not_called()
        driver.find_element.assert_not_called()
        self.assertIn("page Identifiants", str(caught.exception))
        self.assertNotIn("LADDITION", str(caught.exception))

    def test_another_bar_s_own_account_is_typed(self):
        from accounts import vault

        driver = mock.Mock()
        typed = []
        driver.find_element.return_value.send_keys.side_effect = typed.append
        with bound_tenant(self.bar_b):
            vault.save({"LADDITION_EMAIL": "caisse-beta@example.invalid", "LADDITION_PASSWORD": "secret-beta"})
            with (
                mock.patch.object(session_module, "navigate"),
                mock.patch.object(session_module, "WebDriverWait"),
                override_settings(LADDITION_EMAIL="caisse@example.invalid", LADDITION_PASSWORD="mot-de-passe-essai"),
            ):
                session_module.log_in(driver, log=lambda *args: None)
        self.assertEqual(typed, ["caisse-beta@example.invalid", "secret-beta"])

    def test_another_bar_opens_one_with_its_own_account(self):
        from accounts import vault

        with bound_tenant(self.bar_b):
            vault.save({"LADDITION_EMAIL": "caisse-beta@example.invalid", "LADDITION_PASSWORD": "secret-beta"})
            with (
                mock.patch.object(session_module, "build_driver") as build,
                mock.patch.object(session_module, "open_report"),
            ):
                with session_module.laddition_session(tempfile.mkdtemp()) as driver:
                    self.assertIs(driver, build.return_value)

    def test_no_browser_starts_for_another_bar_with_no_account(self):
        """Its « Identifiants » holds no L'Addition login: said before one of
        the server's browsers is started for nothing - the .env's values in
        the settings change nothing."""
        with (
            bound_tenant(self.bar_b),
            override_settings(LADDITION_EMAIL="caisse@example.invalid", LADDITION_PASSWORD="mot-de-passe-essai"),
            mock.patch.object(session_module, "build_driver") as build,
            mock.patch.object(session_module, "open_report") as open_report,
            self.assertRaises(session_module.LadditionAuthError) as caught,
        ):
            with session_module.laddition_session(tempfile.mkdtemp()):
                pass
        build.assert_not_called()
        open_report.assert_not_called()
        self.assertIn("page Identifiants", str(caught.exception))
        self.assertNotIn("LADDITION", str(caught.exception))

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

    def test_the_import_downloads_into_another_bar_s_own_folder(self):
        seen = {}

        def download(start, end, download_dir, **kwargs):
            seen["folder"] = Path(download_dir)
            return [an_export(download_dir)]

        with mock.patch("recipes.management.commands.laddition_import.download_sales_lines", side_effect=download):
            self.run_for(self.bar_b, "laddition_import", "--from", "2026-06-01", "--to", "2026-06-30")
        self.assertEqual(seen["folder"], self.downloads_of(self.bar_b))
        with bound_tenant(self.bar_a):
            self.assertFalse(PosProduct.objects.exists())

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
            self.assertRaisesMessage(CommandError, OWNER_ONLY),
        ):
            self.run_for(self.bar_b, "laddition_open")
        session.assert_not_called()
        with mock.patch("recipes.management.commands.laddition_open.laddition_session") as session:
            session.return_value.__enter__.return_value.find_element.return_value.text = "Caisse"
            self.run_for(self.bar_a, "laddition_open")
        session.assert_called_once()


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


#: What the platform owner's .env holds on the server, in the settings of the
#: process every espace runs in. Invented.
SERVER_TILL = {"LADDITION_EMAIL": "caisse@example.invalid", "LADDITION_PASSWORD": "mot-de-passe-essai"}


class AutomaticSalesImportGateTests(TwoTenantsTestCase):
    """GitHub's main's automatic sales imports (recipes/auto_sales.py) in
    every espace since the connectors opened to every bar: Bar Alpha is the
    platform owner's espace, Bar Beta another bar. Beta's rules run only once
    ITS L'Addition account is on its « Identifiants » - the .env values in
    the settings are the owner's alone -, sign in with it in a headless
    browser, and leave a log and an alert another bar may read: the till's
    own refusal or one fixed sentence, no traceback, no server path."""

    owner_a = True
    #: Wednesday 18/11/2026, 07:00 in Paris (06:00 UTC).
    SEVEN = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        # No deploy mark of this checkout's: a slot would say « mise à jour ».
        self.enterContext(override_settings(BASE_DIR=Path(tempfile.mkdtemp())))
        # The whole line goes to the server's log (common.job_line, tasks.fail).
        for name in ("marginmate.jobs", "recipes.tasks"):
            logger = logging.getLogger(name)
            self.enterContext(mock.patch.object(logger, "propagate", False))
            handler = logging.NullHandler()
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)

    def rule_in(self, tenant) -> AutoSalesImport:
        with bound_tenant(tenant):
            return AutoSalesImport.objects.create(
                name="Ventes de la veille",
                source="laddition",
                weekdays="0,1,2,3,4,5,6",
                times="07:00",
                created_at=self.SEVEN - timedelta(days=7),
            )

    def type_beta_s_account(self):
        from accounts import vault

        with bound_tenant(self.bar_b):
            vault.save({"LADDITION_EMAIL": "caisse-beta@example.invalid", "LADDITION_PASSWORD": "secret-beta"})

    def tick(self, tenant):
        with (
            bound_tenant(tenant),
            mock.patch("notifications.webpush.sending_enabled", return_value=True),
            mock.patch("recipes.importing.threading.Thread") as thread,
        ):
            auto_sales.run_due(self.SEVEN)
        return thread

    @override_settings(**SERVER_TILL)
    def test_another_bar_s_rule_waits_for_its_own_account_never_the_server_s(self):
        rule = self.rule_in(self.bar_b)
        thread = self.tick(self.bar_b)
        thread.assert_not_called()
        with bound_tenant(self.bar_b):
            rule.refresh_from_db()
            self.assertEqual(rule.last_result, auto_sales.SOURCE_UNAVAILABLE)
            self.assertFalse(SalesImportJob.objects.exists())
        # Its page says where the account is typed - never a variable, nor
        # the .env -, and draws no form.
        self.client.force_login(self.user_b)
        page = self.client.get(reverse("recipes:auto_sales")).content.decode()
        self.assertIn(escape(TILL_LOGIN_MISSING), page)
        for server_word in ("LADDITION_", ".env", "manage.py", SERVER_TILL["LADDITION_EMAIL"]):
            self.assertNotIn(server_word, page)
        self.assertNotIn('name="nouveau-name"', page)
        # The owner's espace: his .env stands, as on GitHub's main.
        self.rule_in(self.bar_a)
        thread = self.tick(self.bar_a)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["target"].tenant.pk, self.bar_a.pk)

    @override_settings(**SERVER_TILL)
    def test_another_bar_s_rule_imports_in_a_thread_bound_to_it_once_its_account_is_typed(self):
        self.rule_in(self.bar_b)
        self.type_beta_s_account()
        thread = self.tick(self.bar_b)
        thread.assert_called_once()
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, import_laddition_sales_task)
        self.assertEqual(target.tenant.pk, self.bar_b.pk)
        with bound_tenant(self.bar_b):
            job = SalesImportJob.objects.get()
            self.assertEqual((job.trigger, job.source), (SalesImportJob.Trigger.AUTOMATIC, "laddition"))
        with bound_tenant(self.bar_a):
            self.assertFalse(SalesImportJob.objects.exists())

    @override_settings(**SERVER_TILL, SCRAPER_HEADLESS=False)
    def test_another_bar_s_automatic_import_signs_in_headless_with_its_own_account_and_fails_cleanly(self):
        rule = self.rule_in(self.bar_b)
        self.type_beta_s_account()
        typed, options = [], []

        def chrome_started(*args, **kwargs):
            options.append(kwargs["options"].arguments)
            driver = mock.Mock()
            driver.find_element.return_value.send_keys.side_effect = typed.append
            return driver

        def report(driver, path, log=print):
            with mock.patch.object(session_module, "navigate"), mock.patch.object(session_module, "WebDriverWait"):
                session_module.log_in(driver, log=log)
            # The till's page then breaks, as a library says it: English, a
            # path of the server's.
            raise RuntimeError(r"chrome not reachable C:\MarginMate\app\chromedriver.exe")

        with bound_tenant(self.bar_b):
            job = SalesImportJob.objects.create(
                trigger=SalesImportJob.Trigger.AUTOMATIC,
                auto_rule_id=rule.pk,
                range_start=date(2026, 11, 14),
                range_end=date(2026, 11, 17),
            )
            with (
                mock.patch.object(session_module.webdriver, "Chrome", side_effect=chrome_started),
                mock.patch.object(session_module, "ChromeDriverManager"),
                mock.patch.object(session_module, "Service"),
                mock.patch.object(session_module, "open_report", side_effect=report),
                mock.patch("notifications.events.emit") as alert,
            ):
                import_laddition_sales_task(job.pk, date(2026, 11, 14), date(2026, 11, 17))
            job.refresh_from_db()
        # Its own account, never the .env's; a window never opens on the
        # server's desktop (invoices/scrapers/chrome.py).
        self.assertEqual(typed, ["caisse-beta@example.invalid", "secret-beta"])
        self.assertIn("--headless=new", options[0])
        # Its log: the fixed sentence, no traceback, no path, no English.
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertIn(f"Échec : {SERVER_ERROR}", job.log)
        for word in ("Traceback", "MarginMate", "chrome not reachable"):
            self.assertNotIn(word, job.log)
        # Its alert says the same sentence (auto_sales._notify), never the
        # library's words.
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[1], "failed")
        self.assertEqual(alert.call_args.kwargs["body"], f"Échec : {SERVER_ERROR}")

    def test_the_owner_s_automatic_import_still_says_its_exception(self):
        """GitHub's main's alert, in the platform owner's espace: the
        exception's own first line, any address masked."""
        rule = self.rule_in(self.bar_a)
        with bound_tenant(self.bar_a):
            job = SalesImportJob.objects.create(trigger=SalesImportJob.Trigger.AUTOMATIC, auto_rule_id=rule.pk)
            with (
                mock.patch(
                    "recipes.tasks.download_sales_lines",
                    side_effect=RuntimeError("export refusé par https://app.laddition.invalid/x"),
                ),
                mock.patch("notifications.events.emit") as alert,
            ):
                import_laddition_sales_task(job.pk, date(2026, 11, 14), date(2026, 11, 17))
        self.assertEqual(alert.call_args.kwargs["body"], "Échec : export refusé par (adresse masquée)")
