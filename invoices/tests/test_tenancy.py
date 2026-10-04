"""The invoices app with one database per bar.

Real tenants in temporary files (accounts/tests/support.py): a bar's jobs,
files and caches stay its own, whichever thread or process-global structure
they pass through. The connectors (invoices/integrations.py): the mailbox
and the AI reading run in every espace, each with what it typed on its own
« Identifiants » page - never with the server's .env values, which stand in
for the platform owner's espace alone -, and Metro and the customer portals
stay the owner's, refused at every entry point elsewhere (views, task
bodies, connectors) with « à configurer ».

Primary keys restart at 1 in every tenant's database, so every test here
gives both bars a row with the SAME pk: that is how one bar's job, batch or
download folder would have been taken for the other's. Data invented.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from io import StringIO
from unittest import mock

from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import paths, vault
from accounts.tenancy import NoTenantBound, bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from common import SERVER_ERROR
from invoices import integrations
from invoices.deletion import delete_invoice
from invoices.integrations import TO_CONFIGURE_PLURAL
from invoices.models import Invoice, InvoiceType, ReceiptBatch, ScrapeJob, Supplier, WebsiteInvoiceSource
from invoices.parsers import LLM_PARSER_KEY
from invoices.scrapers.generic_email import MAILBOX_MISSING
from invoices.tasks import gather_invoices_task, test_email_pattern_task, test_website_task
from invoices.tests.pdf_files import write_pdf
from tests.factories import make_invoice, make_supplier

START, END = date(2026, 1, 1), date(2026, 1, 31)

#: What the platform owner's .env holds on the server - in the settings of
#: the process every espace runs in. Invented.
SERVER_ENV = {
    "METRO_EMAIL": "acheteur@exemple.invalid",
    "METRO_PASSWORD": "secret-metro-essai",
    "INVOICE_EMAIL_ADDRESS": "factures@exemple.invalid",
    "INVOICE_EMAIL_APP_PASSWORD": "secret-boite-essai",
    "INVOICE_IMAP_HOST": "imap.exemple.invalid",
    "LADDITION_EMAIL": "caisse@exemple.invalid",
    "LADDITION_PASSWORD": "secret-caisse-essai",
    "ANTHROPIC_API_KEY": "cle-serveur-essai",
}


def run_in_a_thread(target, args=()) -> None:
    """Run `target` the way the server does: in a thread of its own, which
    starts with nothing bound."""
    errors = []

    def run():
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 - raised again in the test's thread
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)
    assert not worker.is_alive(), "the thread never ended"
    if errors:
        raise errors[0]


def wait_for(condition, seconds=10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def portal_recipe():
    from invoices.scrapers.website import WebsiteRecipe

    return WebsiteRecipe(
        name="Box Exemple",
        login_url="https://box.exemple.invalid/login",
        username_env="BOX_LOGIN",
        password_env="BOX_PASSWORD",
    )


class _Reached(Exception):
    """The connector got past its guard, to what would contact the account."""


class GateTests(TwoTenantsTestCase):
    """Bar Alpha is the platform owner's espace; Bar Beta another bar, which
    runs the mailbox and the AI reading with its own « Identifiants », and
    neither Metro nor a portal."""

    owner_a = True

    def reopen_everything_in_b(self):
        """What a « Données » import or the admin could do to Beta's rows -
        Metro marked to fetch, every source active, a portal among them: the
        data must not be what keeps Metro and the portals out of reach."""
        with bound_tenant(self.bar_b):
            Supplier.objects.filter(code="METRO").update(is_scrapable=True)
            box = make_supplier(code="BOX_B", name="Box Beta", parser_key="")
            portal = InvoiceType.objects.create(
                name="Box Beta - Factures", supplier=box, source_kind=InvoiceType.SourceKind.WEBSITE
            )
            WebsiteInvoiceSource.objects.create(
                invoice_type=portal,
                login_url="https://box.exemple.invalid/login",
                username_env="BOX_LOGIN",
                password_env="BOX_PASSWORD",
            )
            InvoiceType.objects.update(is_active=True)
            mailbox = InvoiceType.objects.filter(source_kind=InvoiceType.SourceKind.EMAIL)
            return {
                "mailbox": [f"type-{pk}" for pk in mailbox.values_list("pk", flat=True)],
                "portal": f"type-{portal.pk}",
            }

    # ------------------------------------------------------------ the views

    def test_a_hosted_bar_s_gather_runs_in_a_thread_bound_to_it_and_never_asks_metro_now(self):
        codes = self.reopen_everything_in_b()
        self.client.force_login(self.user_b)
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(
                reverse("invoices:gather"), {"sources": [*codes["mailbox"], codes["portal"]], "metro_now": "on"}
            )
        thread.assert_called_once()
        target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
        self.assertIs(target.__wrapped__, gather_invoices_task)
        self.assertEqual(target.tenant.pk, self.bar_b.pk)
        # « Réessayer Metro maintenant » is the owner's: neither Metro nor
        # the pause's bypass reach the thread.
        self.assertNotIn("METRO", args[3])
        self.assertFalse(args[4])

    def test_the_owners_gather_runs_in_a_thread_bound_to_the_owners_tenant(self):
        self.client.force_login(self.user_a)
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(reverse("invoices:gather"), {"sources": ["type-999"]})
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, gather_invoices_task)
        self.assertEqual(target.tenant.pk, self.bar_a.pk)

    def test_the_import_card_offers_a_hosted_bar_its_mailbox_and_says_metro_and_the_portals(self):
        codes = self.reopen_everything_in_b()
        with bound_tenant(self.bar_b):
            # Its mailbox, on its « Identifiants » (invoices/tests/test_mailbox_guards.py
            # has the card before it is).
            vault.save(
                {"INVOICE_EMAIL_ADDRESS": "beta@exemple.invalid", "INVOICE_EMAIL_APP_PASSWORD": "secret-beta"},
                bindings={"INVOICE_EMAIL_APP_PASSWORD": "imap.beta.invalid"},
            )
        gather = f'action="{reverse("invoices:gather")}"'
        self.client.force_login(self.user_b)
        page = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")
        self.assertContains(page, gather)
        for code in codes["mailbox"]:
            self.assertContains(page, f'value="{code}"')
        self.assertNotContains(page, f'value="{codes["portal"]}"')
        self.assertNotContains(page, 'value="METRO"')
        self.assertNotContains(page, 'name="metro_now"')
        self.assertContains(page, integrations.METRO)
        self.assertContains(page, integrations.PORTALS)
        self.client.force_login(self.user_a)
        page = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")
        self.assertContains(page, gather)
        self.assertNotContains(page, integrations.PORTALS)

    def test_the_source_form_takes_a_mailbox_source_and_refuses_a_portal(self):
        self.client.force_login(self.user_b)
        create = reverse("invoices:invoice_type_create")
        page = self.client.get(create)
        self.assertContains(page, 'value="test"')
        self.assertContains(page, integrations.PORTALS)
        self.assertNotContains(page, 'name="site-login_url"')
        with bound_tenant(self.bar_b):
            supplier = make_supplier(code="TRAITEUR_B", name="Traiteur Beta", parser_key="")
            types_before = InvoiceType.objects.count()
        mailbox = {
            "name": "Traiteur Beta - Factures",
            "supplier": str(supplier.pk),
            "source_kind": "EMAIL",
            "parser_key": "",
            "is_active": "on",
            "sender_pattern": r"traiteur",
            "subject_pattern": "",
            "body_pattern": "",
            "attachment_pattern": r"\.pdf$",
        }
        portal = {
            "name": "Box Beta",
            "supplier": str(supplier.pk),
            "source_kind": "WEBSITE",
            "parser_key": "",
            "is_active": "on",
            "site-login_url": "https://box.exemple.invalid/login",
            "site-username_env": "BOX_LOGIN",
            "site-password_env": "BOX_PASSWORD",
        }
        for data in ({**portal, "action": "test"}, {**portal, "action": "save"}):
            with self.subTest(kind="portal", action=data["action"]):
                with mock.patch("invoices.views.threading.Thread") as thread:
                    response = self.client.post(create, data, follow=True)
                thread.assert_not_called()
                self.assertContains(response, TO_CONFIGURE_PLURAL)
        with bound_tenant(self.bar_b):
            self.assertEqual(InvoiceType.objects.count(), types_before)
            self.assertFalse(ScrapeJob.objects.exists())
        # The mailbox: « Tester » runs bound to Beta, and the source saves.
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(create, {**mailbox, "action": "test"})
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, test_email_pattern_task)
        self.assertEqual(target.tenant.pk, self.bar_b.pk)
        self.client.post(create, {**mailbox, "action": "save"})
        with bound_tenant(self.bar_b):
            self.assertTrue(InvoiceType.objects.filter(name="Traiteur Beta - Factures").exists())

    def test_the_sources_tab_offers_a_new_source_and_says_the_portals(self):
        self.client.force_login(self.user_b)
        page = self.client.get(reverse("invoices:invoice_type_list"))
        self.assertContains(page, reverse("invoices:invoice_type_create"))
        self.assertContains(page, integrations.PORTALS)
        self.client.force_login(self.user_a)
        self.assertNotContains(self.client.get(reverse("invoices:invoice_type_list")), integrations.PORTALS)

    def test_the_ai_reading_is_offered_and_taken_on_upload_in_every_espace(self):
        with bound_tenant(self.bar_b):
            ai = Supplier.objects.get(parser_key=LLM_PARSER_KEY)
            # Its key, on its « Identifiants » (invoices/tests/test_ai_reading.py
            # has the page before it is).
            vault.save({"ANTHROPIC_API_KEY": "cle-beta-essai"})
        self.client.force_login(self.user_b)
        page = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(page, '<optgroup label="Analyse IA">')
        upload = SimpleUploadedFile("facture.pdf", b"%PDF-1.4 essai", content_type="application/pdf")
        with (
            mock.patch("invoices.ocr.check_page_count"),
            mock.patch("invoices.receipts.route_to_returnables"),
            mock.patch("invoices.views.parse_and_import", side_effect=_Reached) as parse,
        ):
            self.client.post(reverse("invoices:invoice_upload"), {"supplier": str(ai.pk), "source_file": upload})
        parse.assert_called_once()

    def test_metro_is_not_said_to_be_fetched_by_its_own_module_outside_the_owners_tenant(self):
        self.reopen_everything_in_b()
        own = "Récupérées par son propre module"
        with bound_tenant(self.bar_b):
            metro_b = Supplier.objects.get(code="METRO").pk
        with bound_tenant(self.bar_a):
            metro_a = Supplier.objects.get(code="METRO").pk
        self.client.force_login(self.user_b)
        self.assertNotContains(self.client.get(reverse("invoices:supplier_detail", args=[metro_b])), own)
        self.assertNotContains(self.client.get(reverse("invoices:supplier_list")), "son propre module")
        self.client.force_login(self.user_a)
        self.assertContains(self.client.get(reverse("invoices:supplier_detail", args=[metro_a])), own)

    def test_a_supplier_s_page_offers_a_new_source_in_every_espace(self):
        with bound_tenant(self.bar_b):
            shop_b = make_supplier(code="EPICERIE_B", name="Epicerie Beta", parser_key="")
        self.client.force_login(self.user_b)
        page = self.client.get(reverse("invoices:supplier_detail", args=[shop_b.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "+ Nouvelle source pour Epicerie Beta")
        self.assertNotContains(page, TO_CONFIGURE_PLURAL)

    # ------------------------------------------------------- the task bodies

    def test_the_gather_task_searches_the_mailbox_and_neither_metro_nor_a_portal(self):
        codes = self.reopen_everything_in_b()
        with bound_tenant(self.bar_b):
            job = ScrapeJob.objects.create()
            with (
                mock.patch("invoices.tasks.scrape_metro_invoices") as metro,
                mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]) as mailbox,
                mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]) as portal,
                mock.patch("invoices.tasks._GatherHeartbeat"),
            ):
                gather_invoices_task(job.pk, START, END, {"METRO", *codes["mailbox"], codes["portal"]}, True)
            job.refresh_from_db()
        metro.assert_not_called()
        portal.assert_not_called()
        mailbox.assert_called()
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertEqual(job.progress["METRO"]["error"], integrations.METRO)
        self.assertEqual(job.progress[codes["portal"]]["error"], integrations.PORTALS)

    def test_the_tests_of_a_source_mailbox_runs_and_portal_refuses_by_itself(self):
        with bound_tenant(self.bar_b):
            mail_job, portal_job = ScrapeJob.objects.create(kind="TEST"), ScrapeJob.objects.create(kind="TEST")
            with mock.patch("invoices.tasks.find_matching_emails", return_value=[]) as mailbox:
                test_email_pattern_task(mail_job.pk, START, END, "traiteur", "", "", r"\.pdf$")
            with mock.patch("invoices.tasks.list_website_invoices") as portal:
                test_website_task(portal_job.pk, portal_recipe(), 0, START, END)
            mail_job.refresh_from_db()
            portal_job.refresh_from_db()
        mailbox.assert_called_once()
        portal.assert_not_called()
        self.assertEqual((mail_job.status, portal_job.status), (ScrapeJob.Status.SUCCESS, ScrapeJob.Status.FAILED))
        self.assertIn(integrations.PORTALS, portal_job.log)

    # ------------------------------------------------------ the connectors
    # The server's .env values are in every espace's settings: a hosted bar
    # with nothing typed on its « Identifiants » page must reach none of them.

    @override_settings(**SERVER_ENV)
    def test_metro_refuses_before_its_pause_or_a_browser(self):
        from invoices.scrapers.metro import MetroError, scrape_metro_invoices

        with bound_tenant(self.bar_b):
            with (
                mock.patch("invoices.scrapers.metro.metro_pause", side_effect=_Reached) as pause,
                mock.patch("invoices.scrapers.metro._build_driver", side_effect=_Reached),
            ):
                with self.assertRaises(MetroError) as refused:
                    scrape_metro_invoices(str(paths.downloads_dir() / "metro"), START, END)
        pause.assert_not_called()
        self.assertEqual(str(refused.exception), integrations.METRO)
        with bound_tenant(self.bar_a):
            with mock.patch("invoices.scrapers.metro.metro_pause", side_effect=_Reached):
                with self.assertRaises(_Reached):
                    scrape_metro_invoices(str(paths.downloads_dir() / "metro"), START, END)

    @override_settings(**SERVER_ENV)
    def test_the_mailbox_never_signs_in_with_the_server_s_account(self):
        from invoices.scrapers.generic_email import find_matching_emails

        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL", side_effect=_Reached) as imap:
            with bound_tenant(self.bar_b):
                with self.assertRaises(RuntimeError) as refused:
                    find_matching_emails(START, END, "traiteur", "", "", "")
            imap.assert_not_called()
            self.assertEqual(str(refused.exception), MAILBOX_MISSING)
            with bound_tenant(self.bar_a):
                with self.assertRaises(_Reached):
                    find_matching_emails(START, END, "traiteur", "", "", "")
        self.assertEqual(imap.call_args.args[0], "imap.exemple.invalid")

    @override_settings(**SERVER_ENV)
    def test_the_mailbox_signs_in_with_what_the_bar_typed(self):
        from invoices.scrapers.generic_email import find_matching_emails

        with bound_tenant(self.bar_b):
            vault.save(
                {"INVOICE_EMAIL_ADDRESS": "beta@exemple.invalid", "INVOICE_EMAIL_APP_PASSWORD": "secret-beta"},
                bindings={"INVOICE_EMAIL_APP_PASSWORD": "imap.beta.invalid"},
            )
            imap = mock.Mock()
            imap.login.side_effect = _Reached
            public = [(2, 1, 6, "", ("8.8.8.8", 993))]
            with (
                mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL", return_value=imap) as opened,
                mock.patch("invoices.scrapers.egress.resolve", return_value=public),
                self.assertRaises(_Reached),
            ):
                find_matching_emails(START, END, "traiteur", "", "", "")
        self.assertEqual(opened.call_args.args[0], "imap.beta.invalid")
        imap.login.assert_called_once_with("beta@exemple.invalid", "secret-beta")

    def test_a_portal_reads_no_credential_and_names_no_variable(self):
        from invoices.scrapers.website import WebsiteError, credentials

        environ = {"BOX_LOGIN": "gerant@exemple.invalid", "BOX_PASSWORD": "secret-essai"}
        with bound_tenant(self.bar_b):
            with self.assertRaises(WebsiteError) as refused:
                credentials(portal_recipe(), env_file=None, environ=environ)
        self.assertEqual(str(refused.exception), integrations.PORTALS)
        self.assertNotIn("BOX_", str(refused.exception))
        with bound_tenant(self.bar_a):
            # The owner's espace: the .env's values, once their site is
            # confirmed on « Identifiants » (accounts/vault.py env_bindings).
            vault.save({}, env_bindings={"BOX_LOGIN": "box.exemple.invalid", "BOX_PASSWORD": "box.exemple.invalid"})
            self.assertEqual(
                credentials(portal_recipe(), env_file=None, environ=environ),
                ("gerant@exemple.invalid", "secret-essai"),
            )

    @override_settings(**SERVER_ENV)
    def test_the_ai_reading_never_bills_the_server_s_key(self):
        from invoices.parsers.llm_fallback import LLMFallbackParser

        anthropic = mock.Mock()
        anthropic.Anthropic.side_effect = _Reached
        with (
            mock.patch.dict(sys.modules, {"anthropic": anthropic}),
            mock.patch("invoices.parsers.llm_fallback._extract_text", return_value="FACTURE ESSAI") as extract,
        ):
            with bound_tenant(self.bar_b):
                with self.assertRaises(integrations.AiReadingRefused) as refused:
                    LLMFallbackParser().parse("facture.pdf")
            extract.assert_not_called()
            anthropic.Anthropic.assert_not_called()
            self.assertEqual(str(refused.exception), integrations.AI_KEY_MISSING)
            with bound_tenant(self.bar_b):
                # Its own key, typed on its « Identifiants »: that one is sent.
                vault.save({"ANTHROPIC_API_KEY": "cle-beta-essai"})
                with self.assertRaises(_Reached):
                    LLMFallbackParser().parse("facture.pdf")
            self.assertEqual(anthropic.Anthropic.call_args.kwargs["api_key"], "cle-beta-essai")
            with bound_tenant(self.bar_a):
                with self.assertRaises(_Reached):
                    LLMFallbackParser().parse("facture.pdf")
            self.assertEqual(anthropic.Anthropic.call_args.kwargs["api_key"], "cle-serveur-essai")

    def test_unbound_every_connector_refuses(self):
        from invoices.parsers.llm_fallback import LLMFallbackParser
        from invoices.scrapers.generic_email import find_matching_emails

        with self.assertRaises(RuntimeError) as mailbox:
            find_matching_emails(START, END, "traiteur", "", "", "")
        self.assertEqual(str(mailbox.exception), integrations.MAILBOX)
        with self.assertRaises(integrations.AiReadingRefused) as ai:
            LLMFallbackParser().parse("facture.pdf")
        self.assertEqual(str(ai.exception), integrations.AI_READING)

    def test_refused_agrees_with_a_plural_subject(self):
        self.assertTrue(integrations.PORTALS.endswith("disponibles prochainement dans les réglages de votre espace."))
        self.assertTrue(integrations.METRO.endswith(" disponible prochainement dans les réglages de votre espace."))


class ThreadTests(TwoTenantsTestCase):
    """A job's thread, and the heartbeat each job starts, work in the
    database of the tenant that started them - never in another bar's row
    that happens to share its pk."""

    owner_a = True

    def test_a_gathers_thread_updates_its_own_tenants_job(self):
        with bound_tenant(self.bar_b):
            other = ScrapeJob.objects.create()
        self.client.force_login(self.user_a)
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(reverse("invoices:gather"), {"sources": ["type-999"]})
        target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
        self.assertEqual(args[0], other.pk, "both jobs share a pk, or this proves nothing")
        with (
            mock.patch("invoices.tasks.scrape_metro_invoices") as metro,
            mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]),
            mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]),
        ):
            run_in_a_thread(target, args)
        metro.assert_not_called()
        with bound_tenant(self.bar_a):
            self.assertEqual(ScrapeJob.objects.get(pk=args[0]).status, ScrapeJob.Status.SUCCESS)
        with bound_tenant(self.bar_b):
            other.refresh_from_db()
        self.assertEqual((other.status, other.log), (ScrapeJob.Status.PENDING, ""))

    def test_a_folder_imports_thread_reads_its_own_tenants_batch(self):
        from invoices.receipt_batches import stage_batch, start_batch

        with bound_tenant(self.bar_b):
            other = stage_batch([], ignored_names=["Thumbs.db"])
        with bound_tenant(self.bar_a):
            batch = stage_batch([], ignored_names=["desktop.ini"])
            self.assertEqual(batch.pk, other.pk)
            with mock.patch("invoices.receipt_batches.threading.Thread") as thread:
                start_batch(batch)
        target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
        self.assertEqual(target.tenant.pk, self.bar_a.pk)
        run_in_a_thread(target, args)
        with bound_tenant(self.bar_a):
            self.assertEqual(ReceiptBatch.objects.get(pk=batch.pk).status, ReceiptBatch.Status.SUCCESS)
        with bound_tenant(self.bar_b):
            self.assertEqual(ReceiptBatch.objects.get(pk=other.pk).status, ReceiptBatch.Status.PENDING)

    def test_a_gathers_heartbeat_beats_in_its_own_tenant(self):
        """Unbound, a beat flipped FAILED back to RUNNING on whichever row had
        the job's pk - another bar's failed job resurrected, and blocking."""
        from invoices.tasks import _GatherHeartbeat

        self.assert_beats_its_own(ScrapeJob, "invoices.tasks.HEARTBEAT_SECONDS", _GatherHeartbeat)

    def test_a_folder_imports_heartbeat_beats_in_its_own_tenant(self):
        from invoices.receipt_batches import _Heartbeat

        self.assert_beats_its_own(ReceiptBatch, "invoices.receipt_batches.HEARTBEAT_SECONDS", _Heartbeat)

    def assert_beats_its_own(self, model, period, heartbeat_class):
        with bound_tenant(self.bar_b):
            other = model.objects.create(status=model.Status.FAILED)
        with bound_tenant(self.bar_a):
            mine = model.objects.create(status=model.Status.FAILED)
            self.assertEqual(mine.pk, other.pk)
            with mock.patch(period, 0.01):
                heartbeat = heartbeat_class(mine.pk)
                heartbeat.start()
                try:
                    beaten = wait_for(lambda: model.objects.get(pk=mine.pk).status == model.Status.RUNNING)
                finally:
                    heartbeat.stop()
        self.assertTrue(beaten, "the beat never reached its own job")
        with bound_tenant(self.bar_b):
            other.refresh_from_db()
        self.assertEqual((other.status, other.last_heartbeat), (model.Status.FAILED, None))

    def test_a_heartbeat_never_starts_unbound(self):
        from invoices.receipt_batches import _Heartbeat
        from invoices.tasks import _GatherHeartbeat

        for heartbeat_class in (_GatherHeartbeat, _Heartbeat):
            with self.subTest(heartbeat=heartbeat_class.__name__), self.assertRaises(NoTenantBound):
                heartbeat_class(1)


class FolderTests(TwoTenantsTestCase):
    """What a tenant stages, downloads and deletes is in its own folder."""

    owner_a = True

    def test_a_folder_import_is_staged_in_its_own_tenants_folder(self):
        from invoices.receipt_batches import run_receipt_batch, stage_batch

        staged = {}
        for bar, content in ((self.bar_a, b"photo alpha"), (self.bar_b, b"photo beta")):
            with bound_tenant(bar):
                batch = stage_batch([SimpleUploadedFile("ticket.jpg", content)])
                staged[bar.pk] = paths.tenant_dir(bar) / "imports" / batch.results[0]["stored"]
        self.assertEqual(staged[self.bar_a.pk].name, staged[self.bar_b.pk].name)
        for bar, content in ((self.bar_a, b"photo alpha"), (self.bar_b, b"photo beta")):
            self.assertEqual(staged[bar.pk].read_bytes(), content)
        self.assertFalse(staged[self.bar_a.pk].is_relative_to(paths.tenant_dir(self.bar_a) / "media"))
        # Alpha's import runs to its end and removes ITS folder, Beta's file
        # waiting in its own untouched.
        with (
            bound_tenant(self.bar_a),
            mock.patch("invoices.receipt_batches.import_document", side_effect=ValueError("illisible")),
        ):
            run_receipt_batch(ReceiptBatch.objects.get().pk)
        self.assertFalse(staged[self.bar_a.pk].parent.exists())
        self.assertEqual(staged[self.bar_b.pk].read_bytes(), b"photo beta")

    def test_a_file_imported_by_hand_in_one_tenant_holds_back_no_other_tenants_file(self):
        """The set of files being imported by hand is one per process: keyed
        by the batch's pk alone, Alpha's hand import of its batch 1, file 0
        left Beta's batch 1, file 0 unrecognised for good."""
        from invoices import receipt_batches

        batches = {}
        for bar in (self.bar_a, self.bar_b):
            with bound_tenant(bar):
                batch = receipt_batches.stage_batch([SimpleUploadedFile("ticket.jpg", b"photo")])
                batch.results[0].update(status="unrecognised", kept=True)
                batch.save(update_fields=["results"])
                batches[bar.pk] = batch.pk
        requeued = {}

        def requeue_meanwhile(bar):
            with bound_tenant(bar):
                requeued[bar.pk] = receipt_batches.requeue_unrecognised(ReceiptBatch.objects.get(pk=batches[bar.pk]))

        def by_hand(batch, index, path, supplier):
            # Alpha's file is being imported by hand now: both bars' requeue
            # run meanwhile, each in a thread of its own.
            for bar in (self.bar_a, self.bar_b):
                run_in_a_thread(requeue_meanwhile, (bar,))
            return {"status": "ok"}

        with bound_tenant(self.bar_a):
            shop = make_supplier(code="EPICERIE_A", name="Épicerie Alpha", parser_key="")
            with (
                mock.patch("invoices.receipt_batches._import_with_shop", side_effect=by_hand),
                mock.patch("invoices.receipt_batches.start_batch"),
            ):
                receipt_batches.import_with_shop(ReceiptBatch.objects.get(), 0, shop)
        self.assertEqual(requeued, {self.bar_a.pk: 0, self.bar_b.pk: 1})

    def test_each_tenants_gather_downloads_into_its_own_folder(self):
        """Two tenants using the server's accounts (the owner's, and one
        given them) share type ids - the seeded mailbox source is type 1 in
        both - and still never share a download folder."""
        bar_c = self.make_tenant("Bar Gamma", owner=True)
        folders = {}
        for bar in (self.bar_a, bar_c):
            with bound_tenant(bar):
                code = f"type-{InvoiceType.objects.get(source_kind=InvoiceType.SourceKind.EMAIL).pk}"
                gather, test = ScrapeJob.objects.create(), ScrapeJob.objects.create(kind="TEST")
                with (
                    mock.patch("invoices.tasks.scrape_metro_invoices", return_value=[]) as metro,
                    mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]) as mailbox,
                    mock.patch("invoices.tasks.list_website_invoices", return_value=[]) as portal,
                    mock.patch("invoices.tasks._GatherHeartbeat"),
                ):
                    gather_invoices_task(gather.pk, START, END, {"METRO", code})
                    test_website_task(test.pk, portal_recipe(), 0, START, END)
                downloads = str(paths.tenant_dir(bar) / "downloads")
                folders[bar.pk] = [metro.call_args.args[0], mailbox.call_args.args[0], portal.call_args.args[1]]
                for folder in folders[bar.pk]:
                    self.assertTrue(folder.startswith(downloads + os.sep), folder)
        for mine, theirs in zip(folders[self.bar_a.pk], folders[bar_c.pk]):
            self.assertEqual(os.path.basename(mine), os.path.basename(theirs))
            self.assertNotEqual(mine, theirs)

    def test_learn_shop_identifiers_reads_its_own_tenants_files(self):
        with bound_tenant(self.bar_a):
            supplier = make_supplier(code="GROSSISTE_A", name="Grossiste Alpha", parser_key="")
            (paths.media_root() / "invoices").mkdir(parents=True, exist_ok=True)
            write_pdf(
                str(paths.media_root() / "invoices" / "facture-alpha.pdf"),
                [
                    "GROSSISTE ALPHA",
                    "12 rue de l'Exemple 75000 Paris",
                    "Facture F-0001 du 05/01/2026",
                    "Sirop de fraise 2 x 6,00 12,00",
                    "Total TTC 12,00",
                ],
            )
            make_invoice(supplier=supplier, source_file="invoices/facture-alpha.pdf")
            out = StringIO()
            call_command("learn_shop_identifiers", "--dry-run", stdout=out)
        self.assertIn("1 facture(s) numérique(s) lue(s)", out.getvalue())

    def test_deleting_a_document_leaves_another_tenants_file_of_the_same_name(self):
        name = "invoices/2026/01/facture.pdf"
        for bar, content in ((self.bar_a, b"%PDF alpha"), (self.bar_b, b"%PDF beta")):
            with bound_tenant(bar):
                (paths.media_root() / "invoices" / "2026" / "01").mkdir(parents=True, exist_ok=True)
                (paths.media_root() / name).write_bytes(content)
                make_invoice(source_file=name)
        with bound_tenant(self.bar_a):
            delete_invoice(Invoice.objects.get())
            self.assertFalse((paths.media_root() / name).exists())
        with bound_tenant(self.bar_b):
            self.assertEqual((paths.media_root() / name).read_bytes(), b"%PDF beta")


class ServerDetailsTests(TwoTenantsTestCase):
    def test_a_folder_imports_log_keeps_the_servers_traceback_to_itself(self):
        """The log is drawn on the import's page: a traceback there names the
        server's own files to whichever bar sent a broken photo."""
        from invoices.receipt_batches import run_receipt_batch, stage_batch

        with bound_tenant(self.bar_b):
            batch = stage_batch([SimpleUploadedFile("ticket.jpg", b"photo")])
            with (
                mock.patch("invoices.receipt_batches.import_document", side_effect=RuntimeError("illisible")),
                self.assertLogs("invoices.receipt_batches", "ERROR") as logged,
            ):
                run_receipt_batch(batch.pk)
            batch.refresh_from_db()
        # The file and a fixed sentence; the exception's own words - a
        # library's name the staged file's path - stay in the server's log
        # with the traceback (security audit LB-3).
        self.assertIn(f"ticket.jpg : {SERVER_ERROR}", batch.log)
        self.assertNotIn("illisible", batch.log)
        self.assertNotIn("Traceback", batch.log)
        self.assertNotIn(".py", batch.log)
        self.assertIn("illisible", "\n".join(logged.output))


class CorpusTests(TwoTenantsTestCase):
    def test_two_tenants_never_share_the_documents_read(self):
        """Kept between requests by a fingerprint of the documents (count,
        last pk, last import, length read), which two bars can share - two
        fresh tenants, or two restored from one archive: Beta's header chips
        were offered from Alpha's texts."""
        from invoices.receipts import document_corpus

        moment = timezone.make_aware(datetime(2026, 3, 1, 12, 0))
        for bar, text in ((self.bar_a, "EPICERIE ALPHA"), (self.bar_b, "EPICERIE BRAVO")):
            with bound_tenant(bar):
                make_invoice(ocr_text=text)
                Invoice.objects.update(imported_at=moment)
        with bound_tenant(self.bar_a):
            alpha = [text for _pk, text in document_corpus()]
        with bound_tenant(self.bar_b):
            beta = [text for _pk, text in document_corpus()]
        self.assertEqual((alpha, beta), (["EPICERIE ALPHA"], ["EPICERIE BRAVO"]))


class StartupReaperTests(TwoTenantsTestCase):
    def test_the_server_starting_reaps_every_tenant_and_nothing_unbound(self):
        """Multi mode: the unbound `default` is nobody's (an empty in-memory
        database in production), so the reaper binds each tenant in turn -
        and one whose file is missing is stepped over, not a crash."""
        # Their server died a minute ago (invoices/tests/test_startup_reaper.py:
        # only a gather not heard from since this server started is reaped).
        a_minute_ago = timezone.now() - timedelta(minutes=1)
        nobodys = ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=a_minute_ago)
        for bar in (self.bar_a, self.bar_b):
            with bound_tenant(bar):
                ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=a_minute_ago)
        gone = self.make_tenant("Bar Fermé")
        paths.tenant_database(gone).unlink()
        with (
            mock.patch("invoices.apps.sys.argv", ["manage.py", "runserver"]),
            mock.patch.dict("invoices.apps.os.environ", {"RUN_MAIN": "true"}),
            mock.patch("invoices.apps.threading.Thread") as thread,
        ):
            apps.get_app_config("invoices").ready()
        # The reaper's own thread, as the server runs it: started unbound,
        # after its wait (patched out here).
        with mock.patch("invoices.apps._wait"):
            run_in_a_thread(thread.call_args.kwargs["target"], thread.call_args.kwargs["args"])
        for bar in (self.bar_a, self.bar_b):
            with bound_tenant(bar):
                job = ScrapeJob.objects.get()
            self.assertEqual(job.status, ScrapeJob.Status.FAILED)
            self.assertIn("Interrompu par un redémarrage du serveur.", job.log)
        nobodys.refresh_from_db()
        self.assertEqual(nobodys.status, ScrapeJob.Status.RUNNING)
