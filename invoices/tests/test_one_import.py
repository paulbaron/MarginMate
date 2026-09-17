"""One import for every document.

Tickets and PDF invoices went in through two different cards, and the person
importing had to know which. There is one now: files, or a whole folder, of
anything - and each file says how it is to be read. A photo or a scan is read
as a ticket; a digital invoice goes through its supplier's own reader when
that supplier has one and the document says who it is (its SIREN, its phone,
the text it prints at the top), and through the ticket reader otherwise.

OCR never runs here; the PDFs are written by hand (pdf_files.py). Data
invented.
"""

import os
import shutil
from datetime import date
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from invoices.importing import DuplicateInvoiceError, parse_and_import
from invoices.models import Invoice, Supplier
from invoices.parsers.base import ParsedInvoice, ParsedLine
from invoices.receipt_batches import run_receipt_batch, stage_batch
from invoices.receipts import document_supplier, import_document
from invoices.tests.pdf_files import write_pdf
from invoices.tests.test_generic_receipt import UNKNOWN_SHOP
from invoices.tests.test_receipt_shop_choice import recognised
from tests.factories import make_supplier

D = Decimal
# A digital invoice, as its text layer reads.
METRO_INVOICE = [
    "METRO FRANCE",
    "5 rue des Inventions 94000 INVENTEVILLE",
    "N TVA FR 25 900 000 019",
    "FACTURE N 134 52 14645 du 12/02/2026",
    "Reference  Designation  Qte  Prix unitaire HT  Total HT",
    "112233  EAU DE SOURCE 1L  6  0,50  3,00",
    "Total HT  3,00",
    "TVA 5,5%  0,17",
    "Total TTC  3,17",
]
PARSED = ParsedInvoice(
    supplier_code="METRO",
    invoice_number="134-52-14645",
    invoice_date=date(2026, 2, 12),
    lines=[
        ParsedLine(
            raw_name="EAU DE SOURCE 1L", quantity=6, total_volume=D("6"), unit_cost_ht=D("0.50"),
            total_ht=D("3.00"), vat_rate=D("0.055"),
        )
    ],
)


def pdf_upload(test, name, lines):
    path = os.path.join(settings.MEDIA_ROOT, f"{name}")
    write_pdf(path, lines)
    test.addCleanup(lambda: os.path.exists(path) and os.remove(path))
    with open(path, "rb") as handle:
        return SimpleUploadedFile(name, handle.read(), content_type="application/pdf")


class OneCardTests(TestCase):
    def test_the_card_takes_files_or_a_folder_of_anything(self):
        page = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(page, 'data-import-tab="documents"')
        self.assertNotContains(page, 'data-import-tab="pdf"')
        self.assertContains(page, "webkitdirectory")
        self.assertContains(page, "Photos de tickets, scans, factures PDF")
        # The two addresses the two cards had still open this one.
        for name in ("invoices:receipt_upload", "invoices:invoice_upload"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).context["import_tab"], "documents")


class WhichReaderTests(TestCase):
    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        # What Metro's invoices print, as learned from the ones already in.
        self.metro.ticket_identifiers = ["siren:900000019"]
        self.metro.save()

    def test_a_digital_invoice_is_recognised_by_what_it_prints(self):
        self.assertEqual(document_supplier("\n".join(METRO_INVOICE)), self.metro)

    def test_and_goes_through_its_supplier_own_reader(self):
        path = os.path.join(settings.MEDIA_ROOT, "facture.pdf")
        write_pdf(path, METRO_INVOICE)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch("invoices.parsers.metro.MetroParser.parse", return_value=PARSED) as reader, \
                mock.patch("invoices.receipts.recognise") as ocr:
            invoice = import_document(path, display_filename="facture.pdf")
        reader.assert_called_once()
        ocr.assert_not_called()
        self.assertEqual((invoice.supplier, invoice.invoice_number, invoice.lines.count()), (self.metro, "134-52-14645", 1))
        self.assertFalse(invoice.is_receipt)
        self.assertIn("EAU DE SOURCE", invoice.source_text)

    def test_a_file_already_imported_is_refused_before_it_is_read(self):
        """A folder scanned again is mostly documents already in: the file's
        own digest answers, and nothing is opened."""
        path = os.path.join(settings.MEDIA_ROOT, "facture.pdf")
        write_pdf(path, METRO_INVOICE)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch("invoices.parsers.metro.MetroParser.parse", return_value=PARSED):
            import_document(path, display_filename="facture.pdf")
        with mock.patch("invoices.receipts.document_text") as read, \
                self.assertRaisesMessage(DuplicateInvoiceError, "Fichier déjà importé"):
            import_document(path, display_filename="facture.pdf")
        read.assert_not_called()

    def test_a_photo_is_read_as_a_ticket(self):
        make_supplier(code="EPICERIE", name="Épicerie du coin", ticket_header="EPICERIE DU COIN")
        path = os.path.join(settings.MEDIA_ROOT, "ticket.jpg")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xd8\xff a photo")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)):
            invoice = import_document(path, display_filename="ticket.jpg")
        self.assertEqual(invoice.supplier.code, "EPICERIE")
        self.assertTrue(invoice.is_receipt)
        self.assertEqual(invoice.lines.count(), 3)

    def test_an_invoice_of_a_supplier_without_a_reader_is_read_as_a_ticket(self):
        """Nisbets has no parser of its own: its PDF is read by the ticket
        reader, from its text - never by OCR."""
        shop = make_supplier(code="CUISIPRO", name="Cuisipro", parser_key="", ticket_header="CUISIPRO FRANCE")
        path = os.path.join(settings.MEDIA_ROOT, "cuisipro.pdf")
        write_pdf(path, [
            "CUISIPRO FRANCE SARL",
            "FACTURE N 7654321 du 07/11/2024",
            "PRODUIT  DESCRIPTION  QTE  PRIX UNITAIRE  VALEUR",
            "WEBF -AB123  Verre a shot (lot de 12)  8  3,50  28,00",
            "TOTAL HT  EURO  28,00",
            "TVA  5,60",
            "TOTAL TTC  EURO  33,60",
        ])
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch("invoices.receipts.ocr_prepared_image", side_effect=AssertionError("no OCR on a text layer")):
            invoice = import_document(path, display_filename="cuisipro.pdf")
        self.assertEqual((invoice.supplier, invoice.lines.count()), (shop, 1))
        self.assertTrue(invoice.is_receipt)


class WhatTheDocumentSaysTests(TestCase):
    """Every digital document keeps its text, whichever way it came in: it is
    what teaches a supplier the figures that name it."""

    def test_an_invoice_gathered_keeps_its_text(self):
        metro = Supplier.objects.get(code="METRO")
        path = os.path.join(settings.MEDIA_ROOT, "gathered.pdf")
        write_pdf(path, METRO_INVOICE)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch("invoices.parsers.metro.MetroParser.parse", return_value=PARSED):
            invoice = parse_and_import(path, metro, display_filename="gathered.pdf")
        self.assertIn("EAU DE SOURCE", invoice.source_text)
        self.assertFalse(invoice.is_receipt)

    def test_a_photo_filed_by_hand_keeps_none(self):
        """A photo has its reading (`ocr_text`); nothing else is kept."""
        path = os.path.join(settings.MEDIA_ROOT, "photo.jpg")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xd8\xff a photo")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        shop = make_supplier(code="EPICERIE", name="Épicerie du coin", parser_key="")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)):
            invoice = import_document(path, display_filename="photo.jpg", supplier=shop)
        self.assertEqual(invoice.source_text, "")
        self.assertTrue(invoice.ocr_text)


class OneBatchTests(TestCase):
    """A folder holding both: each file takes its own path, and the import
    reports what each one became."""

    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        self.metro.ticket_identifiers = ["siren:900000019"]
        self.metro.save()
        make_supplier(code="EPICERIE", name="Épicerie du coin", ticket_header="EPICERIE DU COIN")
        photo = SimpleUploadedFile("ticket.jpg", b"\xff\xd8\xff a photo")
        self.batch = stage_batch([photo, pdf_upload(self, "facture.pdf", METRO_INVOICE)])
        self.addCleanup(shutil.rmtree, os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(self.batch.pk)), True)

    def test_each_file_takes_its_own_reader(self):
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)), \
                mock.patch("invoices.parsers.metro.MetroParser.parse", return_value=PARSED):
            batch = run_receipt_batch(self.batch.pk)
        self.assertEqual([entry["status"] for entry in batch.results], ["ok", "ok"])
        self.assertEqual([entry["shop"] for entry in batch.results], ["Épicerie du coin", "Metro"])
        self.assertEqual([entry["receipt"] for entry in batch.results], [True, False])
        self.assertEqual(batch.imported_count, 2)

    def test_the_import_links_each_to_where_it_is_checked(self):
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)), \
                mock.patch("invoices.parsers.metro.MetroParser.parse", return_value=PARSED):
            batch = run_receipt_batch(self.batch.pk)
        page = self.client.get(reverse("invoices:receipt_batch", args=[batch.pk]))
        ticket, invoice = (Invoice.objects.get(pk=entry["invoice_id"]) for entry in batch.results)
        self.assertContains(page, reverse("invoices:receipt_review", args=[ticket.pk]) + f"?lot={batch.pk}")
        self.assertContains(page, reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, "Facture à vérifier")  # its products are new

    def test_a_pdf_of_nobody_known_waits_for_its_supplier(self):
        batch = stage_batch([pdf_upload(self, "inconnu.pdf", [
            "PAPETERIE INVENTEE",
            "FACTURE N 4242 du 03/03/2026",
            "Ramette A4  2  4,00  8,00",
            "TOTAL TTC  8,00",
        ])])
        self.addCleanup(shutil.rmtree, os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(batch.pk)), True)
        batch = run_receipt_batch(batch.pk)
        entry = batch.results[0]
        self.assertEqual((entry["status"], entry["kept"]), ("unrecognised", True))
        self.assertEqual(entry["header"], "PAPETERIE INVENTEE")
