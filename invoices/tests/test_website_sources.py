"""A supplier's customer portal as a source of invoices, configured on the
"Sources" tab with no code: its settings, the "Tester" button,
and the gather that reads it beside the mailbox and Metro.

The browser never runs here (test_website_scraper_browser.py drives a real
one against a portal made up for it): the scraper is replaced.
Data invented.
"""

import os
from datetime import date
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from invoices.models import EmailInvoiceSource, InvoiceType, ScrapeJob, WebsiteInvoiceSource
from invoices.scrapers.website import NeedsAPerson, WebsiteRecipe
from invoices.tasks import gather_invoices_task, test_website_task
from tests.factories import make_invoice, make_invoice_type, make_supplier


def website_type(supplier, name="Box Exemple - Factures", **settings):
    invoice_type = InvoiceType.objects.create(
        supplier=supplier, name=name, source_kind=InvoiceType.SourceKind.WEBSITE
    )
    WebsiteInvoiceSource.objects.create(
        invoice_type=invoice_type,
        login_url="https://box.exemple.fr/login",
        username_env="BOX_LOGIN",
        password_env="BOX_PASSWORD",
        **settings,
    )
    return invoice_type


class TypeFormTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="BOX_X", name="Box Exemple", parser_key="", expenses_only=True)
        self.url = reverse("invoices:invoice_type_create")

    def post(self, url=None, **fields):
        data = {
            "name": "Box Exemple - Factures", "supplier": self.supplier.pk, "source_kind": "WEBSITE",
            "parser_key": "", "is_active": "on", "action": "save",
            "site-login_url": "https://box.exemple.fr/login", "site-username_env": "box_login",
            "site-password_env": "BOX_PASSWORD", "site-invoices_url": "", "site-navigation": "Mes factures",
        }
        data.update(fields)
        return self.client.post(url or self.url, data)

    def test_a_website_type_is_saved_with_the_names_of_its_env_variables(self):
        response = self.post()
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))
        site = WebsiteInvoiceSource.objects.get()
        self.assertEqual(
            (site.invoice_type.source_kind, site.username_env, site.password_env, site.navigation),
            ("WEBSITE", "BOX_LOGIN", "BOX_PASSWORD", "Mes factures"),
        )

    def test_a_password_typed_in_place_of_its_variable_name_is_refused(self):
        """The database is copied and shown; the .env file is not."""
        response = self.post(**{"site-password_env": "mon mot de passe!"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        self.assertContains(response, "jamais l&#x27;identifiant ou le mot de passe lui-même")

    def test_a_portal_naming_the_apps_own_variables_is_refused(self):
        """A gather types what the variables hold into the portal's page:
        Metro's password into any site, and Metro signed in to outside its
        firewall's pause. One list with the « Données » import
        (models.APP_ENV_PREFIXES)."""
        response = self.post(**{"site-username_env": "metro_email", "site-password_env": "METRO_PASSWORD"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        for name in ("METRO_EMAIL", "METRO_PASSWORD"):
            self.assertContains(response, f"« {name} » est une variable de l&#x27;application elle-même")

    def test_a_saved_portal_cannot_be_given_the_apps_variables(self):
        invoice_type = website_type(self.supplier)
        url = reverse("invoices:invoice_type_update", args=[invoice_type.pk])
        response = self.post(url, **{"site-password_env": "LADDITION_PASSWORD"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "« LADDITION_PASSWORD » est une variable de l&#x27;application elle-même")
        self.assertEqual(WebsiteInvoiceSource.objects.get().password_env, "BOX_PASSWORD")

    def test_testing_a_portal_with_the_apps_variables_signs_in_nowhere(self):
        """« Tester » signs in with the settings as typed, unsaved."""
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.post(action="test", **{"site-password_env": "INVOICE_EMAIL_APP_PASSWORD"})
        self.assertEqual(response.status_code, 200)
        thread.assert_not_called()
        self.assertFalse(ScrapeJob.objects.exists())

    def test_a_name_that_only_looks_like_the_apps_is_a_portals(self):
        response = self.post(**{"site-username_env": "METROPOLE_LOGIN", "site-password_env": "METROPOLE_PASSWORD"})
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))
        self.assertEqual(WebsiteInvoiceSource.objects.get().username_env, "METROPOLE_LOGIN")

    def test_the_page_shows_the_kind_it_is_editing(self):
        invoice_type = website_type(self.supplier)
        page = self.client.get(reverse("invoices:invoice_type_update", args=[invoice_type.pk]))
        self.assertTrue(page.context["is_website"])
        self.assertContains(page, 'value="https://box.exemple.fr/login"')
        self.assertContains(page, "Réglages avancés")

    def test_the_other_kinds_settings_are_disabled_not_only_hidden(self):
        """A browser refuses to send a form holding an empty required field,
        hidden or not: "Tester" did nothing (test_invoice_type_form_browser)."""
        invoice_type = website_type(self.supplier)
        page = self.client.get(reverse("invoices:invoice_type_update", args=[invoice_type.pk]))
        self.assertContains(page, 'data-source-kind="EMAIL" hidden disabled')
        self.assertContains(page, '<fieldset class="source-kind" data-source-kind="WEBSITE">')

    def test_after_a_test_the_other_kind_is_shown_as_saved(self):
        """Its fields were not sent: blank would look like they were lost."""
        invoice_type = website_type(self.supplier)
        EmailInvoiceSource.objects.create(invoice_type=invoice_type, sender_pattern="factures@box")
        url = reverse("invoices:invoice_type_update", args=[invoice_type.pk])
        with mock.patch("invoices.views.threading.Thread"):
            response = self.client.post(url, {
                "name": "Box Exemple - Factures", "supplier": self.supplier.pk, "source_kind": "WEBSITE",
                "action": "test", "test_start_date": "2026-04-01", "test_end_date": "2026-05-31",
                "site-login_url": "https://box.exemple.fr/login", "site-username_env": "BOX_LOGIN",
                "site-password_env": "BOX_PASSWORD",
            })
        self.assertContains(response, 'value="factures@box"')
        self.assertContains(response, 'value="2026-04-01"')

    def test_an_email_type_is_saved_as_before(self):
        response = self.client.post(self.url, {
            "name": "Grossiste - Factures", "supplier": self.supplier.pk, "source_kind": "EMAIL",
            "parser_key": "", "is_active": "on", "action": "save", "sender_pattern": "factures@",
            "attachment_pattern": r"(?i)\.pdf$",
        })
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))
        self.assertEqual(InvoiceType.objects.get(name="Grossiste - Factures").source_kind, "EMAIL")
        self.assertFalse(WebsiteInvoiceSource.objects.exists())

    def test_testing_a_website_starts_a_listing_and_saves_nothing(self):
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.post(action="test", test_start_date="2026-04-01", test_end_date="2026-05-31")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        job = ScrapeJob.objects.get(kind=ScrapeJob.Kind.TEST)
        self.assertEqual(response.context["test_job"], job)
        _job_id, recipe, supplier_id, start, end = thread.call_args.kwargs["args"]
        self.assertEqual((recipe.login_url, recipe.username_env, recipe.navigation), ("https://box.exemple.fr/login", "BOX_LOGIN", ["Mes factures"]))
        self.assertEqual((supplier_id, start, end), (self.supplier.pk, date(2026, 4, 1), date(2026, 5, 31)))


class TestTaskTests(TestCase):
    def test_its_rows_are_what_the_page_shows(self):
        supplier = make_supplier(code="BOX_X", name="Box Exemple", parser_key="")
        make_invoice(supplier=supplier, invoice_number="F-2026-0417")
        job = ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST)
        rows = [{"row": "Facture de mai 2026", "link": "Télécharger", "date": "01/05/2026", "decision": "à télécharger"}]
        recipe = WebsiteRecipe(name="Box", login_url="https://box.exemple.fr", username_env="A", password_env="B")
        with mock.patch("invoices.tasks.list_website_invoices", return_value=rows) as listing:
            test_website_task(job.id, recipe, supplier.pk, date(2026, 4, 1), date(2026, 5, 31))
        job.refresh_from_db()
        self.assertEqual((job.status, job.test_matches), (ScrapeJob.Status.SUCCESS, rows))
        # What is already imported is said as such.
        self.assertEqual(listing.call_args.kwargs["known_numbers"], {"F-2026-0417"})
        page = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
        self.assertContains(page, "Ligne de la page")
        self.assertContains(page, "à télécharger")


class GatherTests(TestCase):
    def setUp(self):
        self.box = make_supplier(code="BOX_X", name="Box Exemple", parser_key="", expenses_only=True)
        self.box_type = website_type(self.box)
        self.water = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)
        self.water_type = website_type(self.water, name="Eau Exemple - Factures")

    def gather(self, fetch, imported=None):
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.fetch_website_invoices", side_effect=fetch), mock.patch(
            "invoices.receipts.import_document", side_effect=imported or (lambda path, **kwargs: None)
        ) as import_document:
            gather_invoices_task(
                job.id, date(2026, 4, 1), date(2026, 5, 31),
                {f"type-{self.box_type.id}", f"type-{self.water_type.id}"},
            )
        job.refresh_from_db()
        return job, import_document

    def test_each_file_is_imported_as_the_suppliers_document(self):
        def fetch(recipe, download_dir, start, end, **kwargs):
            return [os.path.join(download_dir, f"{recipe.name}.pdf")]

        job, import_document = self.gather(fetch)
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertEqual(
            sorted(call.kwargs["supplier"].name for call in import_document.call_args_list), ["Box Exemple", "Eau Exemple"]
        )
        self.assertEqual(job.progress[f"type-{self.box_type.id}"]["imported"], 1)
        self.assertEqual(job.invoices_created, 2)

    def test_one_site_failing_is_said_on_its_line_and_the_others_go_on(self):
        def fetch(recipe, download_dir, start, end, **kwargs):
            if recipe.name.startswith("Box"):
                raise NeedsAPerson("Box Exemple demande une vérification (code reçu par SMS).")
            return [os.path.join(download_dir, "eau.pdf")]

        job, import_document = self.gather(fetch)
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertIn("code reçu par SMS", job.progress[f"type-{self.box_type.id}"]["error"])
        self.assertEqual([call.kwargs["supplier"] for call in import_document.call_args_list], [self.water])

    def test_what_is_already_imported_is_passed_on_not_to_be_clicked(self):
        make_invoice(supplier=self.box, invoice_number="F-2026-0417")
        seen = {}

        def fetch(recipe, download_dir, start, end, **kwargs):
            seen[recipe.name] = kwargs["known_numbers"]
            return []

        self.gather(fetch)
        self.assertEqual(seen["Box Exemple - Factures"], {"F-2026-0417"})

    def test_a_duplicate_is_skipped_not_failed(self):
        from invoices.importing import DuplicateInvoiceError

        def fetch(recipe, download_dir, start, end, **kwargs):
            return [os.path.join(download_dir, "deja.pdf")]

        def imported(path, **kwargs):
            raise DuplicateInvoiceError("Fichier déjà importé")

        job, _ = self.gather(fetch, imported)
        self.assertEqual((job.status, job.invoices_created), (ScrapeJob.Status.SUCCESS, 0))
        self.assertIn("already imported", job.log)


class GatherCardTests(TestCase):
    def test_a_website_type_is_offered_among_the_sources(self):
        box = make_supplier(code="BOX_X", name="Box Exemple", parser_key="")
        invoice_type = website_type(box)
        make_invoice_type(supplier=make_supplier(code="GROS_X"), name="Grossiste - Factures", sender_pattern="x@")
        page = self.client.get(reverse("invoices:invoice_list"))
        codes = [source["code"] for source in page.context["gather_sources"]]
        self.assertIn(f"type-{invoice_type.id}", codes)
