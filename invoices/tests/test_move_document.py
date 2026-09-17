"""A document filed under the wrong supplier is moved from its own page -
an invoice as well as a ticket.

A ticket has always had "Changer d'enseigne"; a digital invoice had nothing,
so seven Free invoices recognised as UBA's (both print the same mobile
number, the customer's own) could not be put right at all. Moving one takes
its identifiers away from the supplier it leaves, which is what stops the
next one going the same way.

Data invented.
"""

from datetime import date

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from invoices.models import Invoice, Supplier
from invoices.receipts import move_to_shop
from tests.factories import make_invoice, make_invoice_line, make_supplier

INVOICE_TEXT = """FREE MOBILE
16 rue de la Ville l'Evêque 75008 PARIS
SIREN 900 000 019
Votre facture du 18/05/2026
Contact : 06 12 34 56 78
Forfait mobile  9,99
TOTAL A PAYER  9,99"""


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class MoveAnInvoiceTests(TestCase):
    def setUp(self):
        self.wrong = make_supplier(code="UBA_X", name="UBA Exemple", parser_key="", ticket_identifiers=["tel:0612345678"])
        self.right = make_supplier(code="FREE_X", name="Free Exemple", parser_key="")
        self.invoice = make_invoice(
            supplier=self.wrong, invoice_number="", invoice_date=date(2026, 5, 18), source_text=INVOICE_TEXT
        )
        make_invoice_line(invoice=self.invoice, raw_name="Forfait mobile", total_ht="8.33", vat_rate="0.20")
        self.url = reverse("invoices:invoice_edit_lines", args=[self.invoice.pk])

    def test_the_page_of_an_invoice_offers_to_change_its_supplier(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="action" value="move_shop"')
        self.assertContains(page, "Changer de fournisseur")
        self.assertContains(page, f'<option value="{self.right.pk}">Free Exemple</option>', html=True)

    def test_moving_it_keeps_its_lines_and_leaves_it_an_invoice(self):
        response = self.client.post(self.url, {"action": "move_shop", "supplier": self.right.pk})
        self.assertRedirects(response, self.url)
        moved = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual((moved.supplier, moved.lines.count()), (self.right, 1))
        self.assertFalse(moved.is_receipt)  # no check was added to a digital invoice
        self.assertIn("Facture rangée chez Free Exemple.", messages_of(response))

    def test_it_takes_what_named_it_away_from_the_supplier_it_leaves(self):
        move_to_shop(self.invoice, self.right)
        self.wrong.refresh_from_db()
        self.assertEqual(self.wrong.ticket_identifiers, [])

    def test_a_ticket_still_says_ticket(self):
        ticket = make_invoice(
            supplier=self.wrong, ocr_text=INVOICE_TEXT,
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        )
        page = self.client.get(reverse("invoices:receipt_review", args=[ticket.pk]))
        self.assertContains(page, "Changer d'enseigne")
        response = self.client.post(
            reverse("invoices:receipt_review", args=[ticket.pk]),
            {"action": "move_shop", "supplier": self.right.pk},
        )
        self.assertIn("Ticket rangé chez Free Exemple.", messages_of(response))
        self.assertIn(
            "Enseigne choisie à la main", [check["label"] for check in Invoice.objects.get(pk=ticket.pk).parse_checks]
        )


class NotFooledAgainTests(TestCase):
    """What the seven Free invoices taught: a number two companies print -
    the customer's own - is not enough to file a document that names a
    company nobody knows."""

    def setUp(self):
        self.wrong = make_supplier(code="UBA_X", name="UBA Exemple", parser_key="", ticket_identifiers=["tel:0612345678"])

    def test_a_document_naming_an_unknown_company_is_not_filed_by_a_phone(self):
        from invoices.receipts import detect_parser

        self.assertIsNone(detect_parser(INVOICE_TEXT))

    def test_the_same_phone_still_names_it_when_no_other_company_is_printed(self):
        from invoices.receipts import detect_parser

        text = "TICKET\nContact : 06 12 34 56 78\nARTICLE  9,99\nTOTAL  9,99"
        self.assertEqual(detect_parser(text).supplier_code, "UBA_X")

    def test_a_company_number_it_knows_still_names_it(self):
        self.wrong.ticket_identifiers = ["siren:900000019", "tel:0612345678"]
        self.wrong.save()
        from invoices.receipts import detect_parser

        self.assertEqual(detect_parser(INVOICE_TEXT).supplier_code, "UBA_X")


class OwnReaderNeedsMoreThanAPhoneTests(TestCase):
    """A supplier's own reader turns a whole document into lines: it runs on
    a document that names that supplier by its company number or by the text
    it prints at the top, never on one that merely shares a phone number."""

    def test_a_phone_alone_does_not_hand_a_document_to_a_dedicated_reader(self):
        from invoices.receipts import document_supplier

        metro = Supplier.objects.get(code="METRO")
        metro.ticket_identifiers = ["tel:0164191715"]
        metro.save()
        text = "AUTRE SOCIETE\nSIREN 900 000 019\nTel 01 64 19 17 15\nARTICLE  10,00\nTOTAL  10,00"
        self.assertIsNone(document_supplier(text))
        # Its own company number, and the reader is its own again.
        metro.ticket_identifiers = ["siren:900000019"]
        metro.save()
        self.assertEqual(document_supplier(text), metro)
