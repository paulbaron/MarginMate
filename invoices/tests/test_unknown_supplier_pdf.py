"""A PDF invoice from a supplier nothing was set up for.

A supplier with a reader of its own (Metro, UBA...) keeps it. Any other one's
invoice - or a new supplier's, named in the import card - is read the way a
ticket is: lines, rates, totals checked against what the document prints,
then opened for checking beside the PDF. A digital invoice dropped among
ticket photos and filed by hand under a supplier with its own reader goes
through that reader.

Data invented; the PDFs are written by hand (pdf_files.py).
"""

import os
import shutil
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from invoices.importing import DuplicateInvoiceError
from invoices.models import Invoice, Supplier
from invoices.parsers.base import ParsedInvoice, ParsedLine
from invoices.receipt_batches import import_with_shop, run_receipt_batch, stage_batch
from invoices.receipts import (
    CHOSEN_SHOP_CHECK,
    UnrecognisedShopError,
    has_own_reader,
    import_invoice_pdf,
)
from invoices.tests.pdf_files import write_pdf
from invoices.tests.test_generic_tables import WEB_INVOICE
from invoices.tests.test_text_layer import INVOICE
from tests.factories import make_invoice, make_supplier

D = Decimal


def pdf_bytes(test, lines):
    path = os.path.join(settings.MEDIA_ROOT, f"source-{test.id()[-20:]}.pdf")
    write_pdf(path, lines)
    test.addCleanup(lambda: os.path.exists(path) and os.remove(path))
    with open(path, "rb") as handle:
        return handle.read()


class OwnReaderTests(TestCase):
    def test_who_has_a_reader_of_their_own(self):
        self.assertTrue(has_own_reader(Supplier.objects.get(code="METRO")))
        for code in ("SABBH", "OTHER"):
            with self.subTest(code=code):
                self.assertFalse(has_own_reader(Supplier.objects.get(code=code)))
        self.assertFalse(has_own_reader(make_supplier(code="CUISIPRO", parser_key="")))


class UploadTests(TestCase):
    def setUp(self):
        self.url = reverse("invoices:invoice_upload")
        # Helvetica in a hand-written PDF has no euro sign.
        self.pdf = pdf_bytes(self, WEB_INVOICE.replace("€", "EUR").split("\n"))

    def post(self, **data):
        upload = SimpleUploadedFile("facture-web.pdf", self.pdf, content_type="application/pdf")
        return self.client.post(self.url, {"source_file": upload, **data})

    def test_the_card_offers_every_supplier_and_a_new_one(self):
        response = self.client.get(self.url)
        metro = Supplier.objects.get(code="METRO")
        self.assertContains(response, f'<option value="{metro.pk}">Metro</option>', html=True)
        self.assertContains(response, '<option value="new">+ Nouveau fournisseur…</option>', html=True)
        self.assertContains(response, 'name="new_name"')
        self.assertContains(response, "lue comme un ticket")

    def test_a_supplier_without_a_reader_has_its_invoice_read(self):
        supplier = make_supplier(code="CUISIPRO", name="Cuisipro", parser_key="")
        response = self.post(supplier=supplier.pk)
        invoice = Invoice.objects.get(supplier=supplier)
        self.assertRedirects(response, reverse("invoices:receipt_review", args=[invoice.pk]), fetch_redirect_response=False)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.total_ht, line.vat_rate) for line in invoice.lines.order_by("pk")],
            [
                ("WEBF -AB123 Verre à shot Olympia (lot de 12)", 8, D("28.00"), D("0.2000")),
                ("WEBF -CD456 Pince à glaçons inox 20cm", 2, D("8.50"), D("0.2000")),
                ("Transport Charges", 1, D("9.95"), D("0.2000")),
            ],
        )
        self.assertEqual((invoice.invoice_number, invoice.printed_total_ttc), ("1234567", D("55.74")))
        self.assertTrue(invoice.source_file)
        checks = {check["label"]: check for check in invoice.parse_checks}
        self.assertIn("choisi à l'import", checks[CHOSEN_SHOP_CHECK]["detail"])
        page = self.client.get(response["Location"])
        self.assertEqual(page.status_code, 200)

    def test_a_new_supplier_is_created_and_recognised_next_time(self):
        response = self.post(supplier="new", new_name="Cuisipro France", new_header="CUISIPRO FRANCE")
        supplier = Supplier.objects.get(name="Cuisipro France")
        invoice = Invoice.objects.get(supplier=supplier)
        self.assertRedirects(response, reverse("invoices:receipt_review", args=[invoice.pk]), fetch_redirect_response=False)
        self.assertEqual((supplier.parser_key, supplier.ticket_header), ("", "CUISIPRO FRANCE"))
        # Its phone number, printed on its next invoices, names it too.
        self.assertIn("tel:0123456789", supplier.ticket_identifiers)
        self.assertEqual(invoice.lines.count(), 3)

    def test_a_new_supplier_needs_a_name_not_taken(self):
        for data, message in (
            ({"supplier": "new"}, "Donnez un nom"),
            ({"supplier": "new", "new_name": "Metro"}, "existe déjà"),
        ):
            with self.subTest(data=data):
                response = self.post(**data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["import_tab"], "pdf")
                self.assertContains(response, message)
        self.assertFalse(Invoice.objects.exists())

    def test_the_same_file_twice_is_said_so(self):
        supplier = make_supplier(code="CUISIPRO", name="Cuisipro", parser_key="")
        self.post(supplier=supplier.pk)
        response = self.post(supplier=supplier.pk)
        self.assertRedirects(response, f"{reverse('invoices:invoice_list')}?ajouter=pdf", fetch_redirect_response=False)
        self.assertEqual(Invoice.objects.filter(supplier=supplier).count(), 1)

    def test_a_supplier_with_its_own_reader_keeps_it(self):
        metro = Supplier.objects.get(code="METRO")
        new = make_invoice(supplier=metro, invoice_number="F-78")
        with mock.patch("invoices.views.parse_and_import", return_value=new) as parser, \
                mock.patch("invoices.receipts.recognise") as ocr:
            response = self.post(supplier=metro.pk)
        parser.assert_called_once()
        ocr.assert_not_called()
        self.assertRedirects(
            response, f"{reverse('invoices:invoice_list')}?surligner={new.pk}", fetch_redirect_response=False
        )


class FiledFromABatchTests(TestCase):
    """A digital invoice among ticket photos: no shop recognised, then filed
    by hand."""

    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        batch = stage_batch([SimpleUploadedFile("metro.pdf", pdf_bytes(self, INVOICE))])
        self.addCleanup(shutil.rmtree, os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(batch.pk)), True)
        with mock.patch("invoices.receipt_batches.import_receipt", side_effect=UnrecognisedShopError("?")):
            self.batch = run_receipt_batch(batch.pk)

    def test_a_supplier_with_its_own_reader_reads_it(self):
        invoice = make_invoice(supplier=self.metro, invoice_number="F-79")
        with mock.patch("invoices.receipt_batches.import_invoice_pdf", return_value=invoice) as reader, \
                mock.patch("invoices.receipt_batches.import_receipt") as ticket_reader:
            entry = import_with_shop(self.batch, 0, self.metro)
        reader.assert_called_once()
        self.assertEqual(reader.call_args.args[1:], (self.metro,))
        ticket_reader.assert_not_called()
        self.assertEqual((entry["status"], entry["invoice_id"]), ("ok", invoice.pk))

    def test_a_scan_is_read_as_a_ticket_whatever_the_supplier(self):
        path = os.path.join(settings.MEDIA_ROOT, self.batch.results[0]["stored"])
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4 a scan: no text in it")
        receipt = make_invoice(supplier=self.metro, invoice_number="T-1")
        with mock.patch("invoices.receipt_batches.import_invoice_pdf") as reader, \
                mock.patch("invoices.receipt_batches.import_receipt", return_value=receipt) as ticket_reader:
            import_with_shop(self.batch, 0, self.metro)
        reader.assert_not_called()
        ticket_reader.assert_called_once()


class ImportInvoicePdfTests(TestCase):
    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        self.path = os.path.join(settings.MEDIA_ROOT, "metro-facture.pdf")
        write_pdf(self.path, INVOICE)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.parsed = ParsedInvoice(
            supplier_code="METRO", invoice_number="F-80", invoice_date=None,
            lines=[ParsedLine(raw_name="EAU 1L", quantity=6, total_volume=D("6"), unit_cost_ht=D("0.50"),
                              total_ht=D("3.00"), vat_rate=D("0.055"))],
        )

    def test_its_reader_reads_it_and_the_file_is_known_after(self):
        with mock.patch("invoices.parsers.metro.MetroParser.parse", return_value=self.parsed):
            invoice = import_invoice_pdf(self.path, self.metro, display_filename="metro-facture.pdf")
            self.assertEqual((invoice.supplier, invoice.lines.count()), (self.metro, 1))
            self.assertTrue(invoice.source_sha256)
            with self.assertRaisesMessage(DuplicateInvoiceError, "Fichier déjà importé"):
                import_invoice_pdf(self.path, self.metro)
