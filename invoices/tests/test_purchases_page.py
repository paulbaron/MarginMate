"""The "Achats" page: invoices, tickets and invoice types in one place.

One card adds purchases - ticket photos, a supplier's PDF, or a gather of
Metro and the mailbox - and shows the import as it runs; below it, three
tabs: every document, what waits to be checked, and where invoices come
from. What was just added is found in the list without leaving the page: an
import's documents are a filter of their own, a new PDF invoice is
highlighted and opened in place, and checking an import's tickets one after
the other ends on that import. OCR and parsers never run: they are replaced.
Data invented.
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices.models import Invoice, InvoiceType, ReceiptBatch, ScrapeJob, Supplier
from invoices.tests.page_posts import page_post
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_invoice_type,
    make_priced_stock_type,
    make_product,
)

FAILED = [{"label": "Somme des lignes = total imprimé", "passed": False, "detail": "écart"}]
PASSED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]
HTMX = {"HTTP_HX_REQUEST": "true"}


def undated(invoice):
    """The factory dates every invoice; an import may not."""
    Invoice.objects.filter(pk=invoice.pk).update(invoice_date=None)
    return invoice


class PurchasesPageTests(TestCase):
    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.invoice = make_invoice(supplier=self.metro, invoice_number="F-77", invoice_date=date(2026, 5, 2))
        self.ticket = make_invoice(supplier=self.sabbh, invoice_date=date(2026, 5, 3), parse_checks=FAILED)
        self.checked = make_invoice(
            supplier=self.sabbh, invoice_date=date(2026, 5, 4), parse_checks=PASSED, reviewed_at=timezone.now()
        )
        self.url = reverse("invoices:invoice_list")

    def rows(self, response):
        return [invoice.pk for invoice in response.context["invoices"]]

    def test_one_card_adds_every_kind_of_purchase(self):
        response = self.client.get(self.url)
        self.assertContains(response, f'action="{reverse("invoices:receipt_upload")}"')
        self.assertContains(response, f'action="{reverse("invoices:invoice_upload")}"')
        self.assertContains(response, f'action="{reverse("invoices:gather")}"')
        self.assertContains(response, reverse("invoices:invoice_create_manual"))
        self.assertEqual(response.context["pdf_form"].fields["supplier"].label, "Fournisseur")

    def test_only_the_import_choices_switch_the_card(self):
        """The page's script switches on `data-import-tab`: carried by the card
        itself, a click anywhere in it - choosing a folder - hid every panel."""
        html = self.client.get(self.url).content.decode()
        card = html[html.index('id="ajouter"') - 200:html.index('id="import-documents"')]
        opening = card[card.index("<section"):card.index(">", card.index("<section"))]
        self.assertNotIn("data-import-tab", opening)
        self.assertIn('data-initial-tab=""', opening)
        self.assertEqual(html.count("data-import-tab="), 2)

    def test_the_three_tabs_and_what_waits_in_them(self):
        undated(make_invoice(supplier=self.metro, invoice_number="SANS-DATE"))
        make_invoice_type(supplier=self.metro, name="Metro - Factures")
        response = self.client.get(self.url)
        tabs = response.context["tabs"]
        self.assertEqual([tab["label"] for tab in tabs], ["Documents", "À vérifier", "Sources"])
        self.assertEqual([tab["count"] for tab in tabs], [4, 2, InvoiceType.objects.count()])
        self.assertEqual([tab["active"] for tab in tabs], [True, False, False])

    def test_every_tab_is_the_same_page(self):
        make_invoice_type(supplier=self.metro, name="Metro - Factures")
        for name, label in (("invoices:receipt_queue", "À vérifier"), ("invoices:invoice_type_list", "Sources")):
            with self.subTest(tab=name):
                response = self.client.get(reverse(name))
                self.assertEqual([tab["label"] for tab in response.context["tabs"] if tab["active"]], [label])
                self.assertContains(response, f'action="{reverse("invoices:receipt_upload")}"')
        self.assertContains(self.client.get(reverse("invoices:invoice_type_list")), "Metro - Factures")

    def test_the_waiting_tab_lists_tickets_and_documents_to_fix(self):
        without_date = undated(make_invoice(supplier=self.metro, invoice_number="SANS-DATE"))
        broken = make_invoice(supplier=self.metro, invoice_number="ILLISIBLE", status=Invoice.Status.ERROR)
        response = self.client.get(reverse("invoices:receipt_queue"))
        self.assertEqual([receipt.pk for receipt in response.context["receipts"]], [self.ticket.pk])
        self.assertEqual({invoice.pk for invoice in response.context["to_fix"]}, {without_date.pk, broken.pk})
        self.assertContains(response, reverse("invoices:receipt_review", args=[self.ticket.pk]))
        self.assertContains(response, reverse("invoices:invoice_edit_lines", args=[without_date.pk]))

    def test_the_tab_counts_what_the_tab_lists(self):
        """It said "À vérifier 102" over an empty page: the count and the
        list were written twice, and only one of them left the charges out."""
        charge = Supplier.objects.create(code="BAILLEUR", name="Bailleur Exemple", expenses_only=True)
        rent = make_invoice(supplier=charge, invoice_date=date(2026, 5, 5), parse_checks=FAILED)
        response = self.client.get(reverse("invoices:receipt_queue"))
        waiting = [tab["count"] for tab in response.context["tabs"] if tab["key"] == "a-verifier"]
        self.assertEqual(waiting, [len(response.context["receipts"]) + len(response.context["to_fix"])])
        self.assertNotIn(rent.pk, [receipt.pk for receipt in response.context["receipts"]])

    def test_a_row_says_what_the_tab_counts(self):
        """The list said "À vérifier" on 107 charges the tab rightly left
        out: what a row is called and what the tab counts are one rule
        (Invoice.review_state)."""
        charge = Supplier.objects.create(code="BAILLEUR", name="Bailleur Exemple", expenses_only=True)
        make_invoice(supplier=charge, invoice_date=date(2026, 5, 5), parse_checks=FAILED)
        response = self.client.get(self.url)
        body = response.content.decode()
        waiting = [tab["count"] for tab in response.context["tabs"] if tab["key"] == "a-verifier"][0]
        self.assertEqual(body.count(">À vérifier<"), waiting)
        self.assertIn(">Charge<", body)
        # And no button offering to check what no queue holds.
        self.assertEqual(body.count(">Vérifier</a>"), waiting)

    def test_a_charge_whose_total_was_not_read_says_so_on_its_row(self):
        charge = Supplier.objects.create(code="BAILLEUR", name="Bailleur Exemple", expenses_only=True)
        make_invoice(
            supplier=charge, invoice_date=date(2026, 5, 5), parse_checks=FAILED,
            status=Invoice.Status.NEEDS_REVIEW,
        )
        self.assertContains(self.client.get(self.url), ">Total à vérifier<")

    def test_a_charge_that_could_not_be_read_is_shown_among_the_documents_to_fix(self):
        """It is in no queue, so it would be nowhere at all."""
        charge = Supplier.objects.create(code="BAILLEUR", name="Bailleur Exemple", expenses_only=True)
        unread = make_invoice(
            supplier=charge, invoice_date=date(2026, 5, 5), parse_checks=FAILED,
            status=Invoice.Status.NEEDS_REVIEW, error_message="Le total de ce document n'a pas été lu",
        )
        response = self.client.get(reverse("invoices:receipt_queue"))
        self.assertIn(unread.pk, [invoice.pk for invoice in response.context["to_fix"]])
        waiting = [tab["count"] for tab in response.context["tabs"] if tab["key"] == "a-verifier"]
        self.assertEqual(waiting, [len(response.context["receipts"]) + len(response.context["to_fix"])])

    def test_the_list_filters_by_kind_and_by_recency(self):
        old = make_invoice(supplier=self.metro, invoice_number="ANCIENNE")
        Invoice.objects.filter(pk=old.pk).update(imported_at=timezone.now() - timedelta(days=30))
        self.assertEqual(set(self.rows(self.client.get(self.url, {"filtre": "factures"}))), {self.invoice.pk, old.pk})
        self.assertEqual(set(self.rows(self.client.get(self.url, {"filtre": "tickets"}))), {self.ticket.pk, self.checked.pk})
        recent = self.rows(self.client.get(self.url, {"filtre": "recents"}))
        self.assertNotIn(old.pk, recent)
        self.assertIn(self.invoice.pk, recent)
        self.assertEqual(self.rows(self.client.get(self.url, {"filtre": "verifies"})), [self.checked.pk])
        # An unknown filter is all of them.
        self.assertEqual(len(self.rows(self.client.get(self.url, {"filtre": "n'importe"}))), 4)

    def test_the_list_shows_the_newest_documents_and_says_so(self):
        """823 rows was 1,2 Mo and half a second of template on every visit,
        and opening one moved a table 47 000 pixels tall."""
        with mock.patch("invoices.workspace.PAGE_SIZE", 2):
            response = self.client.get(self.url)
        self.assertEqual(len(response.context["invoices"]), 2)
        self.assertEqual(response.context["hidden_count"], 1)
        self.assertContains(response, "tout afficher")
        # The newest first: the two most recent of the three.
        self.assertEqual(self.rows(response), [self.checked.pk, self.ticket.pk])

    def test_everything_is_one_click_away(self):
        with mock.patch("invoices.workspace.PAGE_SIZE", 2):
            response = self.client.get(self.url, {"tout": "1"})
        self.assertEqual(len(response.context["invoices"]), 3)
        self.assertEqual(response.context["hidden_count"], 0)
        self.assertNotContains(response, "tout afficher")

    def test_the_document_just_imported_is_shown_whatever_its_date(self):
        """Dated last year, it sits past the rows this page renders - and the
        "importée" pill would point at nothing."""
        old_one = make_invoice(supplier=self.metro, invoice_number="F-OLD", invoice_date=date(2019, 1, 2))
        with mock.patch("invoices.workspace.PAGE_SIZE", 2):
            response = self.client.get(self.url, {"surligner": old_one.pk})
        self.assertEqual(self.rows(response)[0], old_one.pk)
        self.assertContains(response, "importée")

    def test_the_search_asks_the_database_not_the_page(self):
        """The Eau de Paris invoices sit at the 277th row and the Total
        Energies ones at the 575th: a box filtering what is rendered found
        nothing at all."""
        eau = Supplier.objects.create(code="EAU", name="Eau De Paris")
        old_one = make_invoice(supplier=eau, invoice_number="F-EAU", invoice_date=date(2019, 3, 4))
        with mock.patch("invoices.workspace.PAGE_SIZE", 1):
            response = self.client.get(self.url, {"q": "eau de paris"})
        self.assertEqual(self.rows(response), [old_one.pk])
        self.assertEqual(response.context["found_count"], 1)
        self.assertContains(response, "1 document pour")

    def test_the_search_takes_a_number_a_date_or_an_amount(self):
        eau = Supplier.objects.create(code="EAU", name="Eau De Paris")
        wanted = make_invoice(
            supplier=eau, invoice_number="2025106109524", invoice_date=date(2025, 7, 17),
            printed_total_ttc=Decimal("260.63"),
        )
        for query in ("2025106109524", "17/07/2025", "07/2025", "260,63", "260.63"):
            with self.subTest(query=query):
                response = self.client.get(self.url, {"q": query})
                self.assertIn(wanted.pk, self.rows(response))
        self.assertEqual(self.client.get(self.url, {"q": "introuvable"}).context["found_count"], 0)

    def test_the_search_keeps_the_filter_it_was_typed_under(self):
        response = self.client.get(self.url, {"q": "sabbh", "filtre": "tickets"})
        self.assertEqual(response.context["active_filter"], "tickets")
        self.assertEqual(self.rows(response), [self.checked.pk, self.ticket.pk])
        # And the chips carry the search, so switching filter keeps it.
        self.assertTrue(all("q=sabbh" in chip["url"] for chip in response.context["chips"] if chip["key"]))

    def test_an_import_is_a_filter_of_its_own(self):
        batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {"name": "a.jpg", "status": "ok", "invoice_id": self.ticket.pk, "shop": "Sabbh", "total": "1.00"},
                {"name": "b.jpg", "status": "duplicate", "message": "Déjà là."},
            ],
        )
        response = self.client.get(reverse("invoices:receipt_batch", args=[batch.pk]))
        self.assertEqual(self.rows(response), [self.ticket.pk])
        self.assertContains(response, "b.jpg")  # the import's own report, in the card
        # Checking its tickets starts on its first one, and stays within it.
        self.assertContains(response, reverse("invoices:receipt_review", args=[self.ticket.pk]) + f"?lot={batch.pk}")
        self.assertEqual(self.client.get(self.url, {"lot": "abc"}).status_code, 200)
        self.assertEqual(self.client.get(self.url, {"lot": "999"}).status_code, 200)

    def test_the_pages_that_were_their_own_open_the_right_import(self):
        tickets = self.client.get(reverse("invoices:receipt_upload"))
        self.assertEqual(tickets.context["import_tab"], "documents")
        pdf = self.client.get(reverse("invoices:invoice_upload"))
        self.assertEqual(pdf.context["import_tab"], "documents")

    def test_a_pdf_imported_is_highlighted_in_the_list(self):
        new = make_invoice(supplier=self.metro, invoice_number="F-78")
        upload = SimpleUploadedFile("facture.pdf", b"%PDF-1.4", content_type="application/pdf")
        with mock.patch("invoices.receipts.import_document", return_value=new):
            response = self.client.post(
                reverse("invoices:invoice_upload"), {"supplier": self.metro.pk, "source_file": upload}
            )
        self.assertRedirects(response, f"{self.url}?surligner={new.pk}", fetch_redirect_response=False)
        page = self.client.get(response["Location"])
        self.assertContains(page, f'data-document="{new.pk}" data-highlight')
        self.assertEqual(self.rows(page)[0], new.pk)

    def test_a_pdf_that_fails_says_so_on_the_page(self):
        upload = SimpleUploadedFile("facture.pdf", b"%PDF-1.4", content_type="application/pdf")
        with mock.patch("invoices.receipts.import_document", side_effect=ValueError("illisible")):
            response = self.client.post(
                reverse("invoices:invoice_upload"), {"supplier": self.metro.pk, "source_file": upload}, follow=True
            )
        self.assertContains(response, "Échec de l&#x27;import : illisible")
        self.assertEqual(response.context["import_tab"], "documents")

    def test_a_wrong_file_is_refused_in_the_card(self):
        upload = SimpleUploadedFile("facture.txt", b"x")
        response = self.client.post(
            reverse("invoices:invoice_upload"), {"supplier": self.metro.pk, "source_file": upload}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["import_tab"], "documents")
        self.assertContains(response, "Seuls les fichiers PDF sont acceptés.")
        empty = self.client.post(reverse("invoices:receipt_upload"), {})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.context["import_tab"], "documents")

    def test_a_row_opens_in_place(self):
        vodka = make_priced_stock_type(name="Vodka")
        make_invoice_line(invoice=self.invoice, product=make_product(supplier=self.metro, stock_type=vodka),
                          raw_name="VODKA 70CL", total_ht="12.00")
        make_invoice_line(invoice=self.invoice, product=make_product(supplier=self.metro), raw_name="RHUM X")
        response = self.client.get(reverse("invoices:invoice_preview", args=[self.invoice.pk]))
        self.assertNotContains(response, "<html")
        self.assertContains(response, "VODKA 70CL")
        self.assertContains(response, "Vodka")
        self.assertContains(response, f'{reverse("inventory:stock_list")}#a-classer')
        self.assertContains(response, reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        ticket = self.client.get(reverse("invoices:invoice_preview", args=[self.ticket.pk]))
        self.assertContains(ticket, "écart")
        self.assertContains(ticket, reverse("invoices:receipt_review", args=[self.ticket.pk]))

    def test_the_list_reloads_when_an_import_ends(self):
        batch = ReceiptBatch.objects.create(status=ReceiptBatch.Status.RUNNING, results=[])
        running = self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk]))
        self.assertNotIn("HX-Trigger", running)
        ReceiptBatch.objects.filter(pk=batch.pk).update(status=ReceiptBatch.Status.SUCCESS)
        done = self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk]))
        self.assertEqual(json.loads(done["HX-Trigger"]), {"documents-changed": True})
        job = ScrapeJob.objects.create(status=ScrapeJob.Status.SUCCESS)
        gathered = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
        self.assertEqual(json.loads(gathered["HX-Trigger"]), {"documents-changed": True})
        self.assertContains(self.client.get(self.url), 'hx-trigger="documents-changed from:body"')

    def test_the_sources_tab_names_the_ticket_shops(self):
        self.sabbh.ticket_header = "EPICERIE SABAH"
        self.sabbh.save()
        response = self.client.get(reverse("invoices:invoice_type_list"))
        self.assertContains(response, "EPICERIE SABAH")
        self.assertContains(response, reverse("invoices:invoice_type_create"))


class CheckingAnImportTests(TestCase):
    """Validating tickets one after the other, from an import or not."""

    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.first, self.second, self.elsewhere = (
            make_invoice(supplier=self.sabbh, invoice_date=date(2026, 5, day), parse_checks=FAILED)
            for day in (3, 4, 1)
        )
        for ticket in (self.first, self.second, self.elsewhere):
            make_invoice_line(invoice=ticket, product=make_product(supplier=self.sabbh), total_ht="1.00")
        self.batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {"name": "a.jpg", "status": "ok", "invoice_id": self.first.pk},
                {"name": "b.jpg", "status": "ok", "invoice_id": self.second.pk},
            ],
        )

    def validate(self, ticket, query=""):
        url = reverse("invoices:receipt_review", args=[ticket.pk]) + query
        return self.client.post(url, page_post(self.client.get(url)))

    def test_an_imports_tickets_follow_each_other_then_show_the_import(self):
        lot = f"?lot={self.batch.pk}"
        response = self.validate(self.first, lot)
        # The oldest ticket waiting is elsewhere: the import's next one comes first.
        self.assertRedirects(
            response, reverse("invoices:receipt_review", args=[self.second.pk]) + lot, fetch_redirect_response=False
        )
        response = self.validate(self.second, lot)
        self.assertRedirects(
            response, reverse("invoices:receipt_batch", args=[self.batch.pk]), fetch_redirect_response=False
        )

    def test_the_page_says_how_many_are_left_in_the_import(self):
        response = self.client.get(reverse("invoices:receipt_review", args=[self.first.pk]) + f"?lot={self.batch.pk}")
        self.assertEqual(response.context["remaining"], 2)
        self.assertContains(response, reverse("invoices:receipt_batch", args=[self.batch.pk]))

    def test_the_whole_queue_ends_on_what_was_checked(self):
        for ticket in (self.elsewhere, self.first):
            self.validate(ticket)
        response = self.validate(self.second)
        self.assertRedirects(
            response, reverse("invoices:invoice_list") + "?filtre=verifies", fetch_redirect_response=False
        )

    def test_an_action_on_the_page_keeps_the_import(self):
        url = reverse("invoices:receipt_review", args=[self.first.pk]) + f"?lot={self.batch.pk}"
        response = self.client.post(url, {"action": "forget_price", "price": "abc"})
        self.assertRedirects(response, url, fetch_redirect_response=False)
