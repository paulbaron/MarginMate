"""« Ajouter des factures » (/invoices/ajouter/), and what an employee given
that page alone sees of what he sends.

The owner opens « Accès des employés » and ticks « Ajouter des factures »
for a waiter (accounts/access.py, `invoices_add`): he photographs a ticket
or drops a PDF, and the rest - checking it, filing it under a shop, every
other document of the bar - stays his employer's. So:

- the page is the import's form on its own, the camera first, no folder
  (a phone cannot pick one), and for him no way into « Factures »;
- an import is stamped with the login that sent it (`ReceiptBatch.sent_by`),
  and he follows his own alone - another's, or one from before the stamp,
  answers as an import that does not exist (404), never as a refusal that
  would say it does;
- what he sees of his own is what each file became, in his words (« Reçu »,
  « Déjà envoyé »), never a way to check it, nor the list of every shop;
- the owner's « Derniers imports » says who sent each, once someone else
  does;
- « Récupérer les bons » of Consignes goes through Achats' gather: for one
  given Consignes and not « Factures », a gather of slips and nothing else.

Every name and address is invented, no OCR runs and nothing is fetched: the
import's and the gather's threads are replaced before they could start.
"""

import os
import shutil
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from accounts import paths
from accounts.access import REFUSED
from invoices.models import Invoice, ReceiptBatch, ScrapeJob
from invoices.views import ADDING_SENT
from invoices.workspace import batch_rows, batch_status_context
from tests.factories import make_invoice, make_invoice_line, make_supplier
from tests.runner import employee_of_the_test_tenant, test_user

ADD = reverse("invoices:invoice_add")
UPLOAD = reverse("invoices:receipt_upload")
INVOICE_LIST = reverse("invoices:invoice_list")
RETURNABLES_HOME = reverse("returnables:home")
GATHER = reverse("invoices:gather")
FAILED = [{"label": "Somme des lignes = total imprimé", "passed": False, "detail": "écart"}]
E_INVOICING = "plateforme de facturation"
#: What the full « Factures » workspace draws and the add page never does.
WORKSPACE_MARKERS = ('id="import-title"', "Récupérer depuis les sources", 'id="workspace"')
STATUS_PART = 'id="receipt-batch-status"'


def upload(name="ticket-comptoir.pdf", content=b"%PDF-1.4 a receipt"):
    return SimpleUploadedFile(name, content)


def adder(email="serveur@example.invalid", name="Lina"):
    """An employee given « Ajouter des factures » and nothing else."""
    return employee_of_the_test_tenant(email, ["invoices_add"], name=name)


def pending(name):
    return {"name": name, "status": "pending", "stored": f"receipt_batches/0/{name}"}


def sent_batch(sent_by, *results, status=ReceiptBatch.Status.SUCCESS, log=""):
    """An import as its thread leaves it: running (heard from just now), or
    ended. `results` as receipt_batches writes them."""
    active = status in (ReceiptBatch.Status.PENDING, ReceiptBatch.Status.RUNNING)
    return ReceiptBatch.objects.create(
        sent_by=sent_by,
        status=status,
        results=list(results) or [pending("ticket-sans-nom.jpg")],
        log=log,
        last_heartbeat=timezone.now() if active else None,
        finished_at=None if active else timezone.now(),
    )


class StagedFilesCleanUp:
    """Removes the folder an upload staged (accounts.paths' imports, the
    run's temporary TENANTS_ROOT): the next test must not find it."""

    def forget_staged(self, batch):
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)


class AddPageTests(TestCase):
    """The page itself, for the owner and for an employee."""

    def test_the_owner_s_page_has_the_camera_and_the_files_and_no_folder(self):
        """A phone cannot pick a folder, and a folder is the PC's import, on
        « Factures »."""
        page = self.client.get(ADD)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "invoices/invoice_add.html")
        self.assertContains(page, "Prendre une photo")
        self.assertContains(page, 'capture="environment"')
        self.assertContains(page, "Des fichiers")
        self.assertContains(page, 'name="files" multiple')
        self.assertNotContains(page, "Un dossier entier")
        self.assertNotContains(page, "webkitdirectory")
        # The form posts back here, and the owner may go on to every document.
        self.assertContains(page, f'action="{UPLOAD}"')
        self.assertContains(page, f'<input type="hidden" name="retour" value="{ADD}">', html=True)
        self.assertContains(
            page, f'<a class="btn btn-secondary" href="{INVOICE_LIST}">Toutes les factures</a>', html=True
        )
        self.assertContains(page, E_INVOICING)

    def test_an_employee_who_may_only_add_gets_the_page_without_the_rest(self):
        self.client.force_login(adder())
        page = self.client.get(ADD)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Prendre une photo")
        self.assertContains(page, 'name="files" multiple')
        self.assertNotContains(page, "Un dossier entier")
        self.assertContains(page, "Votre employeur vérifie ensuite ce qui a été lu.")
        # No way into « Factures »: not the button, not a link of the bar.
        self.assertNotContains(page, "Toutes les factures")
        self.assertNotContains(page, f'href="{INVOICE_LIST}"')
        # The e-invoicing platform is the owner's business.
        self.assertNotContains(page, E_INVOICING)
        # « Factures » in his bar leads here.
        self.assertContains(page, f'href="{ADD}"')

    def test_an_employee_given_factures_gets_the_owner_s_page(self):
        """« Factures » opens everything « Ajouter » does and more: nothing
        is held back from him here."""
        self.client.force_login(employee_of_the_test_tenant("caisse@example.invalid", ["invoices"]))
        page = self.client.get(ADD)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Toutes les factures")
        self.assertContains(page, E_INVOICING)
        self.assertNotContains(page, "Votre employeur vérifie ensuite")

    def test_an_employee_without_the_area_is_refused_the_page_and_the_upload(self):
        self.client.force_login(employee_of_the_test_tenant("plonge@example.invalid", ["stock_takes"]))
        self.assertContains(self.client.get(ADD), escape(REFUSED), status_code=403)
        with mock.patch("invoices.receipt_batches.threading.Thread") as thread:
            response = self.client.post(UPLOAD, {"files": [upload()]})
        self.assertContains(response, escape(REFUSED), status_code=403)
        thread.assert_not_called()
        self.assertFalse(ReceiptBatch.objects.exists())


class UploadTests(StagedFilesCleanUp, TestCase):
    """receipt_upload, posted from « Ajouter des factures » or by one who may
    only add: it comes back to the add page, the import his."""

    def setUp(self):
        self.member = adder()

    def post(self, data):
        with mock.patch("invoices.receipt_batches.threading.Thread") as thread:
            response = self.client.post(UPLOAD, data)
        return response, thread

    def test_a_get_goes_to_the_add_page(self):
        self.client.force_login(self.member)
        self.assertRedirects(self.client.get(UPLOAD), ADD)

    def test_his_files_start_an_import_of_his_and_he_comes_back_to_it(self):
        self.client.force_login(self.member)
        response, thread = self.post({"files": [upload("ticket-du-matin.pdf")]})
        batch = ReceiptBatch.objects.get()
        self.forget_staged(batch)
        self.assertEqual(batch.sent_by, self.member.username)
        self.assertEqual([entry["status"] for entry in batch.results], ["pending"])
        self.assertRedirects(response, f"{ADD}?lot={batch.pk}", fetch_redirect_response=False)
        thread.return_value.start.assert_called_once()
        page = self.client.get(response["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, ADDING_SENT)
        self.assertTrue(ADDING_SENT.startswith("Envoyé : la lecture continue"))
        self.assertContains(page, STATUS_PART)
        self.assertContains(page, "ticket-du-matin.pdf")

    def test_nothing_sent_draws_the_add_page_again_and_no_other_document(self):
        """The full « Factures » workspace - every document of the bar, its
        supplier and its total - is never drawn for him, not even to say
        what was wrong with his post."""
        other = make_invoice(
            supplier=make_supplier(name="Brasserie Imaginaire du Port"),
            invoice_number="BIP-48213",
            invoice_date=date(2026, 9, 14),
        )
        make_invoice_line(invoice=other, total_ht="987.65")
        self.assertEqual(Invoice.objects.get(pk=other.pk).total_ttc, Decimal("987.65"))
        recognisable = ("Brasserie Imaginaire du Port", "BIP-48213", "987.65", *WORKSPACE_MARKERS)
        # The owner's own post with nothing in it draws « Factures », which
        # prints every one of them: their absence below means something.
        owners = self.post({})[0]
        for shown in recognisable:
            self.assertContains(owners, shown)
        self.client.force_login(self.member)
        response, thread = self.post({})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "invoices/invoice_add.html")
        self.assertTemplateNotUsed(response, "invoices/purchases.html")
        self.assertContains(response, "Aucun PDF, XML ni photo dans la sélection.")
        for shown in recognisable:
            with self.subTest(shown=shown):
                self.assertNotContains(response, shown)
        thread.assert_not_called()
        self.assertFalse(ReceiptBatch.objects.exists())

    def test_the_owner_posting_from_the_add_page_comes_back_to_it(self):
        owner = test_user()
        response, thread = self.post({"files": [upload()], "retour": ADD})
        batch = ReceiptBatch.objects.get()
        self.forget_staged(batch)
        self.assertEqual(batch.sent_by, owner.username)
        self.assertRedirects(response, f"{ADD}?lot={batch.pk}", fetch_redirect_response=False)
        thread.return_value.start.assert_called_once()

    def test_the_owner_sending_nothing_from_the_add_page_stays_on_it(self):
        response, _thread = self.post({"retour": ADD})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "invoices/invoice_add.html")
        self.assertContains(response, "Aucun PDF, XML ni photo dans la sélection.")

    def test_the_owner_posting_from_factures_still_goes_to_the_import_page(self):
        response, thread = self.post({"files": [upload()]})
        batch = ReceiptBatch.objects.get()
        self.forget_staged(batch)
        self.assertRedirects(response, reverse("invoices:receipt_batch", args=[batch.pk]))
        thread.return_value.start.assert_called_once()
        # Stamped all the same: « Derniers imports » says « Vous ».
        self.assertEqual(batch.sent_by, test_user().username)


class FollowingTests(TestCase):
    """An employee who may only add follows the imports his login sent and
    no other: another's - or one from before the stamp - is not found."""

    def setUp(self):
        self.member = adder()
        self.client.force_login(self.member)
        self.own = sent_batch(
            self.member.username, pending("le-mien.jpg"), pending("le-mien-2.jpg"), status=ReceiptBatch.Status.RUNNING
        )
        self.owners = sent_batch(
            test_user().username, pending("celui-du-patron.jpg"), status=ReceiptBatch.Status.RUNNING
        )
        self.colleagues = sent_batch(
            adder("barmaid@example.invalid", "Tomas").username,
            pending("celui-de-tomas.jpg"),
            status=ReceiptBatch.Status.RUNNING,
        )
        self.unstamped = sent_batch("", pending("celui-d-avant.jpg"), status=ReceiptBatch.Status.RUNNING)

    def others(self):
        return {"owner": self.owners, "colleague": self.colleagues, "unstamped": self.unstamped}

    def test_another_login_s_import_is_not_found(self):
        for who, batch in self.others().items():
            with self.subTest(who=who):
                status = self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk]))
                self.assertEqual(status.status_code, 404)
                cancel = reverse("invoices:receipt_batch_cancel", args=[batch.pk])
                self.assertEqual(self.client.get(cancel).status_code, 404)
                self.assertEqual(self.client.post(cancel).status_code, 404)
                with mock.patch("invoices.receipt_batches.threading.Thread") as thread:
                    resume = self.client.post(reverse("invoices:receipt_batch_resume", args=[batch.pk]))
                self.assertEqual(resume.status_code, 404)
                thread.assert_not_called()
                batch.refresh_from_db()
                self.assertFalse(batch.cancel_requested)

    def test_a_number_that_is_no_import_answers_the_same(self):
        """A 404 like another's: the numbers follow each other, and a
        different answer would tell him which exist."""
        missing = max(batch.pk for batch in ReceiptBatch.objects.all()) + 1
        self.assertEqual(self.client.get(reverse("invoices:receipt_batch_status", args=[missing])).status_code, 404)

    def test_asking_for_another_s_import_on_the_add_page_shows_no_import(self):
        for who, batch in self.others().items():
            with self.subTest(who=who):
                page = self.client.get(f"{ADD}?lot={batch.pk}")
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, STATUS_PART)
                self.assertNotContains(page, batch.results[0]["name"])
                self.assertNotContains(page, f'?lot={batch.pk}"')

    def test_his_own_import_is_followed(self):
        status = self.client.get(reverse("invoices:receipt_batch_status", args=[self.own.pk]))
        self.assertEqual(status.status_code, 200)
        self.assertContains(status, "le-mien.jpg")
        page = self.client.get(f"{ADD}?lot={self.own.pk}")
        self.assertContains(page, STATUS_PART)
        self.assertContains(page, "le-mien-2.jpg")
        here = f"{ADD}?lot={self.own.pk}"
        cancel = reverse("invoices:receipt_batch_cancel", args=[self.own.pk])
        self.assertRedirects(self.client.get(cancel), here)
        stopped = self.client.post(cancel)
        self.assertEqual(stopped.status_code, 200)
        self.assertContains(stopped, "Arrêt après le ticket en cours")
        self.own.refresh_from_db()
        self.assertTrue(self.own.cancel_requested)

    def test_his_own_import_resumes_on_the_add_page(self):
        """Stopped by a restart, it carries on - and he is sent back where he
        follows it, not to « Factures »."""
        ReceiptBatch.objects.filter(pk=self.own.pk).update(
            status=ReceiptBatch.Status.FAILED,
            last_heartbeat=timezone.now() - timedelta(minutes=5),
            finished_at=timezone.now(),
        )
        with mock.patch("invoices.receipt_batches.threading.Thread") as thread:
            response = self.client.post(reverse("invoices:receipt_batch_resume", args=[self.own.pk]))
        self.assertRedirects(response, f"{ADD}?lot={self.own.pk}")
        thread.return_value.start.assert_called_once()
        # Nothing to resume: the same way back, said.
        response = self.client.post(reverse("invoices:receipt_batch_resume", args=[self.own.pk]), follow=True)
        self.assertRedirects(response, f"{ADD}?lot={self.own.pk}")
        self.assertContains(response, "Rien à reprendre")

    def test_the_import_page_of_factures_is_refused_him(self):
        for batch in (self.own, self.owners):
            with self.subTest(batch=batch.sent_by):
                response = self.client.get(reverse("invoices:receipt_batch", args=[batch.pk]))
                self.assertContains(response, escape(REFUSED), status_code=403)

    def test_the_owner_follows_every_import(self):
        self.client.force_login(test_user())
        for batch in (self.own, self.colleagues, self.unstamped):
            with self.subTest(batch=batch.sent_by):
                status = self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk]))
                self.assertContains(status, batch.results[0]["name"])


class StatusPartTests(TestCase):
    """What the live part of his own import draws for one who may only add:
    what each file became, and nothing to check, file or read in a log."""

    def setUp(self):
        self.member = adder()
        shop = make_supplier(name="Épicerie Exemple")
        self.ticket = make_invoice(supplier=shop, invoice_number="T-5521", parse_checks=FAILED)
        self.invoice = make_invoice(supplier=make_supplier(name="Grossiste Exemple"), invoice_number="F-3307")
        self.batch = sent_batch(
            self.member.username,
            {
                "name": "ticket-epicerie.jpg",
                "status": "ok",
                "invoice_id": self.ticket.pk,
                "shop": "Épicerie Exemple",
                "total": "12.40",
                "date": "2026-09-30",
                "verified": False,
            },
            {
                "name": "facture-grossiste.pdf",
                "status": "ok",
                "invoice_id": self.invoice.pk,
                "receipt": False,
                "shop": "Grossiste Exemple",
                "total": "48.00",
                "date": "2026-09-29",
                "verified": False,
            },
            {"name": "ticket-deja-vu.jpg", "status": "duplicate", "message": "Fichier déjà importé : T-5521."},
            {
                "name": "ticket-efface.jpg",
                "status": "unrecognised",
                "kept": True,
                "message": "Enseigne non reconnue.",
                "header": "CAVE DU COIN",
                "read_date": "30/09/2026",
                "read_total": "8.20",
                "stored": "receipt_batches/0/0003.jpg",
            },
            log="[+   0.4s] Lecture de ticket-epicerie.jpg\n",
        )
        self.status_url = reverse("invoices:receipt_batch_status", args=[self.batch.pk])
        self.checking_urls = (
            reverse("invoices:receipt_review", args=[self.ticket.pk]),
            reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]),
            reverse("invoices:receipt_batch_assign", args=[self.batch.pk, 3]),
        )

    def assertDrawnForAnAdder(self, response):
        self.assertContains(response, "ticket-epicerie.jpg")
        self.assertContains(response, '<span class="status-pill status-COMPLETE">Reçu</span>', count=2, html=True)
        self.assertContains(response, "Déjà envoyé")
        self.assertNotContains(response, "Déjà importé")
        self.assertContains(response, "Terminé : votre employeur vérifiera ces documents.")
        self.assertContains(response, "le gérant le rangera.")
        self.assertNotContains(response, "Vérifier")
        self.assertNotContains(response, "Voir les lignes")
        for url in self.checking_urls:
            with self.subTest(url=url):
                self.assertNotContains(response, url)
        self.assertNotContains(response, "shop-choice")
        self.assertNotContains(response, "Journal technique")
        self.assertNotContains(response, "Lecture de ticket-epicerie.jpg")

    def test_he_sees_what_each_file_became_and_nothing_to_check(self):
        self.client.force_login(self.member)
        response = self.client.get(self.status_url)
        self.assertEqual(response.status_code, 200)
        self.assertDrawnForAnAdder(response)

    def test_the_add_page_draws_it_the_same_way(self):
        self.client.force_login(self.member)
        page = self.client.get(f"{ADD}?lot={self.batch.pk}")
        self.assertDrawnForAnAdder(page)

    def test_the_owner_keeps_every_way_to_check_the_same_import(self):
        response = self.client.get(self.status_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vérifier")
        for url in self.checking_urls:
            with self.subTest(url=url):
                self.assertContains(response, url)
        self.assertContains(response, "Déjà importé")
        self.assertNotContains(response, "Déjà envoyé")
        self.assertNotContains(response, "Reçu</span>")
        self.assertNotContains(response, "votre employeur vérifiera")
        self.assertContains(response, "Journal technique")

    def test_the_status_of_an_adder_asks_nothing_about_the_shops(self):
        """The list of every shop is not his to read - every second, while
        his import polls - nor where checking starts."""
        with CaptureQueriesContext(connection) as rows_alone:
            batch_rows(self.batch)
        with self.assertNumQueries(len(rows_alone)):
            context = batch_status_context(self.batch, add_only=True)
        self.assertEqual(context["shop_groups"], [])
        self.assertIsNone(context["batch_first_to_check"])
        with CaptureQueriesContext(connection) as everything:
            full = batch_status_context(self.batch, add_only=False)
        self.assertGreater(len(everything), len(rows_alone))
        self.assertTrue(full["shop_groups"])
        self.assertEqual(full["batch_first_to_check"], self.ticket.pk)


class SentListTests(TestCase):
    """« Vos derniers envois »: the imports this login sent, whichever
    session it opens."""

    def setUp(self):
        self.member = adder()
        self.mine = [
            sent_batch(self.member.username, pending("lundi.jpg")),
            sent_batch(self.member.username, pending("mardi.jpg")),
        ]
        self.not_mine = [
            sent_batch(test_user().username, pending("patron.jpg")),
            sent_batch(adder("barmaid@example.invalid", "Tomas").username, pending("tomas.jpg")),
            sent_batch("", pending("avant.jpg")),
        ]

    def listed(self, page):
        return {batch.pk for batch in page.context["sent_batches"]}

    def assertListsHisOwn(self, page):
        self.assertContains(page, "Vos derniers envois")
        self.assertEqual(self.listed(page), {batch.pk for batch in self.mine})
        for batch in self.mine:
            self.assertContains(page, f'href="{ADD}?lot={batch.pk}"')
        for batch in self.not_mine:
            self.assertNotContains(page, f'href="{ADD}?lot={batch.pk}"')

    def test_only_his_own_imports_are_listed(self):
        self.client.force_login(self.member)
        self.assertListsHisOwn(self.client.get(ADD))

    def test_the_list_is_his_login_s_not_his_session_s(self):
        """Sent from the phone this morning, looked at from the bar's PC
        tonight: another session, the same list."""
        self.client.force_login(self.member)
        first = self.client.session.session_key
        self.client.logout()
        self.client.force_login(self.member)
        self.assertNotEqual(self.client.session.session_key, first)
        self.assertListsHisOwn(self.client.get(ADD))

    def test_the_owner_s_list_is_his_own_too(self):
        page = self.client.get(ADD)
        self.assertEqual(self.listed(page), {self.not_mine[0].pk})

    def test_no_import_sent_no_list(self):
        self.client.force_login(adder("nouveau@example.invalid", "Noa"))
        page = self.client.get(ADD)
        self.assertNotContains(page, "Vos derniers envois")


class SendersOnFacturesTests(TestCase):
    """The owner's « Derniers imports » on « Factures »: « Envoyé par » once
    an import of the last ones was sent by another login."""

    HEADER = "<th>Envoyé par</th>"

    def page(self):
        return self.client.get(INVOICE_LIST)

    def test_the_column_names_the_employee_and_says_vous_for_the_owner(self):
        member = adder()
        sent_batch(member.username, pending("lina.jpg"))
        sent_batch(test_user().username, pending("patron.jpg"))
        sent_batch("", pending("avant.jpg"))
        page = self.page()
        self.assertContains(page, self.HEADER, html=True)
        self.assertContains(page, "<td>Lina</td>", html=True)
        self.assertContains(page, "<td>Vous</td>", html=True)
        self.assertContains(page, "<td>—</td>", html=True)

    def test_an_employee_with_no_first_name_is_named_by_his_address(self):
        member = employee_of_the_test_tenant("sans-prenom@example.invalid", ["invoices_add"])
        sent_batch(member.username, pending("ticket.jpg"))
        self.assertContains(self.page(), "<td>sans-prenom@example.invalid</td>", html=True)

    def test_a_login_gone_since_is_named_by_the_address_it_sent_with(self):
        sent_batch("parti@example.invalid", pending("ticket.jpg"))
        self.assertContains(self.page(), "<td>parti@example.invalid</td>", html=True)

    def test_no_column_while_every_import_is_the_owner_s_or_unstamped(self):
        sent_batch(test_user().username, pending("patron.jpg"))
        sent_batch("", pending("avant.jpg"))
        page = self.page()
        self.assertContains(page, "Derniers imports")
        self.assertNotContains(page, "Envoyé par")
        self.assertNotContains(page, "<td>Vous</td>", html=True)


class SlipsGatherTests(TestCase):
    """« Récupérer les bons » for one given Consignes and not « Factures »:
    the gather of slips is his, every other source - the bar's invoices,
    whose card names them - is not."""

    def setUp(self):
        self.member = employee_of_the_test_tenant("reprises@example.invalid", ["returnables"], name="Lina")
        self.client.force_login(self.member)

    def gather(self, **data):
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(GATHER, {"start_date": "2026-09-01", "retour": RETURNABLES_HOME, **data})
        return response, thread

    def job(self, progress, kind=ScrapeJob.Kind.GATHER):
        return ScrapeJob.objects.create(
            kind=kind, status=ScrapeJob.Status.RUNNING, progress=progress, last_heartbeat=timezone.now()
        )

    def test_a_gather_of_slips_starts(self):
        response, thread = self.gather(sources=["bons-1", "bons-2"])
        self.assertEqual(response["Location"], RETURNABLES_HOME)
        thread.return_value.start.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"][3], {"bons-1", "bons-2"})
        self.assertEqual(ScrapeJob.objects.count(), 1)

    def test_any_other_source_is_refused_and_nothing_starts(self):
        for data in (
            {"sources": ["METRO"]},
            {"sources": ["type-1"]},
            {"sources": ["bons-1", "type-1"]},
            {"sources": ["bons-1"], "metro_now": "on"},
            {"metro_now": "on"},
        ):
            with self.subTest(data=data):
                response, thread = self.gather(**data)
                self.assertContains(response, escape(REFUSED), status_code=403)
                thread.assert_not_called()
                self.assertFalse(ScrapeJob.objects.exists())

    def test_a_gather_naming_other_sources_is_not_his_to_follow(self):
        """Its card was drawn on Consignes before it named the bar's
        invoices: the poll ends there (286, htmx's « stop polling », an empty
        answer that takes the card away) - a 404 was asked again every
        second. Stopping it stays refused."""
        for progress in ({"METRO": {"label": "Metro"}}, {"bons-1": {"label": "Bons"}, "type-4": {"label": "Eau"}}):
            with self.subTest(progress=progress):
                job = self.job(progress)
                status = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
                self.assertEqual(status.status_code, 286)
                self.assertEqual(status.content, b"")
                self.assertEqual(self.client.post(reverse("invoices:gather_cancel", args=[job.pk])).status_code, 404)
                job.refresh_from_db()
                self.assertFalse(job.cancel_requested)

    def test_a_pattern_test_is_not_his_to_follow(self):
        """« Tester » of a mailbox source lists the senders and subjects it
        matched: the owner's, whatever its progress says."""
        job = self.job({}, kind=ScrapeJob.Kind.TEST)
        self.assertEqual(self.client.get(reverse("invoices:gather_status", args=[job.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("invoices:gather_cancel", args=[job.pk])).status_code, 404)

    def test_a_gather_of_slips_is_his_to_follow_and_to_stop(self):
        job = self.job({"bons-1": {"label": "Bons", "found": 2}})
        self.assertEqual(self.client.get(reverse("invoices:gather_status", args=[job.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("invoices:gather_cancel", args=[job.pk])).status_code, 200)
        job.refresh_from_db()
        self.assertTrue(job.cancel_requested)

    def test_a_gather_that_named_no_source_yet_is_his_to_follow_not_to_stop(self):
        """His own, a moment after he tapped « Récupérer les bons » - or the
        owner's invoices', just started: its card says nothing yet, and
        stopping it is left until its sources say whose it is."""
        job = self.job({})
        self.assertEqual(self.client.get(reverse("invoices:gather_status", args=[job.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("invoices:gather_cancel", args=[job.pk])).status_code, 404)
        job.refresh_from_db()
        self.assertFalse(job.cancel_requested)

    def test_an_employee_given_factures_gathers_and_follows_as_before(self):
        self.client.force_login(employee_of_the_test_tenant("caisse@example.invalid", ["invoices"]))
        response, thread = self.gather(sources=["METRO", "type-1"])
        self.assertEqual(response.status_code, 302)
        thread.return_value.start.assert_called_once()
        ScrapeJob.objects.all().delete()
        gather = self.job({"METRO": {"label": "Metro"}})
        self.assertEqual(self.client.get(reverse("invoices:gather_status", args=[gather.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("invoices:gather_cancel", args=[gather.pk])).status_code, 200)

    def test_a_source_s_test_is_the_owner_s_even_for_one_given_factures(self):
        """« Tester » lists the senders, subjects and attachments of the
        owner's mailbox (or a portal's links), and its log the scan: the
        source form is his alone, and so is what it found - its job's
        number is no way round (review of 02/10/2026)."""
        self.client.force_login(employee_of_the_test_tenant("caisse@example.invalid", ["invoices"]))
        job = self.job({}, kind=ScrapeJob.Kind.TEST)
        ScrapeJob.objects.filter(pk=job.pk).update(
            test_matches=[{"sender": "factures@example.invalid", "subject": "Facture d'essai"}]
        )
        self.assertEqual(self.client.get(reverse("invoices:gather_status", args=[job.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("invoices:gather_cancel", args=[job.pk])).status_code, 404)
        job.refresh_from_db()
        self.assertFalse(job.cancel_requested)
        self.client.force_login(test_user())
        self.assertContains(
            self.client.get(reverse("invoices:gather_status", args=[job.pk])), "factures@example.invalid"
        )
        self.assertEqual(self.client.post(reverse("invoices:gather_cancel", args=[job.pk])).status_code, 200)

    def test_another_s_ended_gather_card_is_not_drawn_on_consignes(self):
        """An Achats gather that also fetched the slips (every box ticked
        there) stays on Consignes ten minutes once ended: its card lists the
        bar's invoice sources and its log the mails matched - not his. An
        ended card is not polled, so the gather's own check never ran."""
        ScrapeJob.objects.create(
            kind=ScrapeJob.Kind.GATHER,
            status=ScrapeJob.Status.SUCCESS,
            progress={"bons-1": {"label": "Bons", "found": 1}, "type-4": {"label": "Eau Exemple", "found": 2}},
            log="Retenu : « Facture Exemple » de factures@example.invalid (1 pièce(s) jointe(s)).",
            finished_at=timezone.now(),
        )
        page = self.client.get(RETURNABLES_HOME)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Eau Exemple")
        self.assertNotContains(page, "factures@example.invalid")
        self.client.force_login(test_user())
        self.assertContains(self.client.get(RETURNABLES_HOME), "Eau Exemple")

    def test_another_s_running_gather_holds_his_button_and_says_where(self):
        """Its card withheld, a stand-in that polls nothing holds the button
        (ui.js reads data-job-active); a tap is told the gather runs on
        Factures, not that it « s'affiche ci-dessous »."""
        from invoices.views import GATHER_ELSEWHERE

        running = self.job({"METRO": {"label": "Metro"}})
        page = self.client.get(RETURNABLES_HOME).content.decode()
        self.assertIn('<div id="gather-status" data-job-active hidden></div>', page)
        self.assertNotIn(reverse("invoices:gather_status", args=[running.pk]), page)
        response, thread = self.gather(sources=["bons-1"])
        thread.assert_not_called()
        said = [str(message) for message in response.wsgi_request._messages]
        self.assertEqual(said, [GATHER_ELSEWHERE])

    def test_the_consignes_page_does_not_draw_another_gather_s_card_for_him(self):
        """Its card names the bar's invoices; it still holds his button
        (one gather at a time)."""
        invoices_gather = self.job({"METRO": {"label": "Metro"}})
        card = reverse("invoices:gather_status", args=[invoices_gather.pk])
        page = self.client.get(RETURNABLES_HOME)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, card)
        # The owner sees it.
        self.client.force_login(test_user())
        self.assertContains(self.client.get(RETURNABLES_HOME), card)

    def test_the_consignes_page_draws_his_gather_of_slips(self):
        slips = self.job({"bons-1": {"label": "Bons", "found": 1}})
        page = self.client.get(RETURNABLES_HOME)
        self.assertContains(page, reverse("invoices:gather_status", args=[slips.pk]))
