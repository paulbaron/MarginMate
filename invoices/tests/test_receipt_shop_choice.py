"""Tickets whose shop the header doesn't give away.

A torn or faded top line is enough for `detect_parser` to find nothing, and a
ticket reported "Enseigne inconnue" used to be a dead end: its file went with
the rest of the batch's. It is now kept, and the operator names the shop -
that shop's reader then runs, or, for a supplier with none, the ticket is filed
empty to be typed in from the photo. Either way it lands on the review screen
like any other scan.

OCR never runs here: `receipts.recognise` is replaced.
"""

import os
import shutil
from datetime import date
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices import receipts
from invoices.importing import DuplicateInvoiceError, replace_invoice_lines
from invoices.models import Invoice, ReceiptBatch, Supplier
from invoices.ocr import OcrCell, OcrLine, OcrPage
from invoices.parsers.base import ParsedLine
from invoices.receipt_batches import (
    MISSING_FILE,
    resume_batch,
    run_receipt_batch,
    stage_batch,
)
from invoices.receipts import (
    UnrecognisedShopError,
    detect_parser,
    import_receipt,
    pending_receipts,
)
from invoices.tests.test_parser_sabbh import BASIC
from tests.factories import make_invoice

# BASIC without its first line: the shop's name is what the photo lost.
HEADLESS = BASIC.split("\n", 1)[1]


def recognised(text):
    """What `receipts.recognise` hands over for `text`: no image, one page."""
    lines = [OcrLine(cells=[OcrCell(text=line, x0=0, x1=1, confidence=0.9)]) for line in text.splitlines()]
    return [], [OcrPage(lines=lines)]


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class ImportAsChosenShopTests(TestCase):
    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.path = os.path.join(settings.MEDIA_ROOT, "ticket-sans-entete.pdf")
        with open(self.path, "wb") as handle:
            handle.write(b"%PDF-1.4 a ticket whose header is gone")

    def _import(self, text=HEADLESS, **kwargs):
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)):
            return import_receipt(self.path, display_filename="124_Sabbah.pdf", **kwargs)

    def _failed(self, invoice):
        return {check["label"]: check["detail"] for check in invoice.parse_checks if not check["passed"]}

    def test_a_ticket_with_no_readable_header_is_refused_as_unknown(self):
        self.assertIsNone(detect_parser(HEADLESS))
        with self.assertRaises(UnrecognisedShopError):
            self._import()
        self.assertFalse(Invoice.objects.exists())

    def test_an_unknown_shop_is_still_a_value_error(self):
        """What callers caught before the error had its own class."""
        self.assertTrue(issubclass(UnrecognisedShopError, ValueError))

    def test_the_chosen_shops_reader_reads_it(self):
        invoice = self._import(supplier=self.sabbh)
        self.assertEqual(invoice.supplier, self.sabbh)
        self.assertEqual(sorted(line.quantity for line in invoice.lines.all()), [2, 3])
        self.assertEqual(invoice.invoice_date, date(2026, 7, 14))
        self.assertEqual(invoice.total_ttc.quantize(Decimal("0.01")), Decimal("11.40"))
        checks = {check["label"]: check for check in invoice.parse_checks}
        self.assertTrue(checks["Enseigne choisie à la main"]["passed"])
        self.assertIn("Numero de ticket", invoice.ocr_text)
        self.assertIn(invoice, pending_receipts())

    def test_a_paper_ticket_of_a_supplier_with_invoices_is_read_too(self):
        """Metro's own till: no shop settings, the same reader."""
        metro = Supplier.objects.get(code="METRO")
        invoice = self._import(supplier=metro)
        self.assertEqual((invoice.supplier, invoice.lines.count()), (metro, 2))
        self.assertIn("Numero de ticket", invoice.ocr_text)
        self.assertTrue(invoice.source_file)
        self.assertIn(invoice, pending_receipts())

    def test_the_ai_pseudo_supplier_files_it_empty_to_type_in(self):
        invoice = self._import(supplier=Supplier.objects.get(code="OTHER"))
        self.assertEqual(invoice.lines.count(), 0)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)
        failed = self._failed(invoice)
        self.assertEqual(sorted(failed), ["Date du ticket", "Lecture automatique"])
        self.assertIn("saisissez les lignes", failed["Lecture automatique"])

    def test_a_reader_that_fails_still_files_the_ticket_to_type_in(self):
        with mock.patch(
            "invoices.parsers.generic_receipt.GenericReceiptParser.parse_pages", side_effect=RuntimeError("colonne introuvable")
        ):
            invoice = self._import(supplier=self.sabbh)
        self.assertEqual(invoice.lines.count(), 0)
        self.assertIn("colonne introuvable", self._failed(invoice)["Lecture automatique"])
        self.assertIn(invoice, pending_receipts())

    def test_a_reader_that_finds_nothing_says_so(self):
        invoice = self._import(text="un ticket délavé\nrien de lisible", supplier=self.sabbh)
        self.assertEqual(invoice.lines.count(), 0)
        self.assertIn("Lecture automatique", self._failed(invoice))

    def test_the_same_file_is_still_refused_twice(self):
        self._import(supplier=self.sabbh)
        with self.assertRaisesMessage(DuplicateInvoiceError, "Fichier déjà importé"):
            self._import(supplier=self.sabbh)


def upload(name):
    return SimpleUploadedFile(name, f"%PDF-1.4 {name}".encode())


class ChooseShopInBatchTests(TestCase):
    """The batch page, once the batch has finished: a file no shop was
    recognised on offers a list of shops."""

    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.receipt = make_invoice(
            supplier=self.sabbh,
            invoice_date=date(2024, 8, 13),
            parse_checks=[{"label": "Enseigne choisie à la main", "passed": True, "detail": ""}],
        )
        batch = stage_batch([upload("124_Sabbah.pdf"), upload("deja.pdf")])
        self.folder = os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(batch.pk))
        self.addCleanup(shutil.rmtree, self.folder, True)
        outcomes = [UnrecognisedShopError("Enseigne non reconnue sur ce ticket."), DuplicateInvoiceError("Déjà là.")]
        with mock.patch("invoices.receipt_batches.import_receipt", side_effect=outcomes):
            self.batch = run_receipt_batch(batch.pk)
        self.url = reverse("invoices:receipt_batch_assign", args=[self.batch.pk, 0])
        self.page = reverse("invoices:receipt_batch", args=[self.batch.pk])
        self.file = os.path.join(settings.MEDIA_ROOT, self.batch.results[0]["stored"])

    def choose(self, supplier=None, url=None, **outcome):
        outcome = outcome or {"return_value": self.receipt}
        pk = supplier if supplier is not None else self.sabbh.pk
        with mock.patch("invoices.receipt_batches.import_receipt", **outcome) as importer:
            response = self.client.post(url or self.url, {"supplier": pk})
        self.batch.refresh_from_db()
        return response, importer

    def test_the_file_is_kept_until_its_shop_is_chosen(self):
        self.assertEqual(self.batch.results[0]["status"], "unrecognised")
        self.assertTrue(os.path.exists(self.file))
        self.assertEqual(self.batch.awaiting_shop_count, 1)

    def test_a_file_that_fails_otherwise_is_not_kept(self):
        """A generic ValueError is a broken file, not an unknown shop:
        choosing a shop would not make it readable."""
        batch = stage_batch([upload("casse.pdf")])
        self.addCleanup(shutil.rmtree, os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(batch.pk)), True)
        with mock.patch("invoices.receipt_batches.import_receipt", side_effect=ValueError("image tronquée")):
            batch = run_receipt_batch(batch.pk)
        self.assertEqual(batch.results[0]["status"], "error")
        self.assertEqual(batch.awaiting_shop_count, 0)
        self.assertFalse(os.path.exists(os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(batch.pk))))

    def test_the_batch_page_offers_the_shops(self):
        response = self.client.get(self.page)
        self.assertContains(response, f'action="{self.url}"')
        self.assertContains(response, f'<option value="{self.sabbh.pk}">Sabbh Oriental</option>', html=True)
        metro = Supplier.objects.get(code="METRO")
        self.assertContains(response, f'<option value="{metro.pk}">Metro</option>', html=True)
        self.assertContains(response, '<option value="new">+ Nouvelle enseigne…</option>', html=True)
        # The AI pseudo-supplier reads no ticket: not a shop to choose (the
        # page's PDF import offers it, for invoices).
        html = response.content.decode()
        start = html.index(f'action="{self.url}"')
        self.assertNotIn("analyse IA", html[start:html.index("</form>", start)])
        self.assertContains(response, "choisissez son enseigne")

    def test_choosing_the_shop_imports_the_file_and_opens_it_for_review(self):
        response, importer = self.choose()
        self.assertRedirects(response, reverse("invoices:receipt_review", args=[self.receipt.pk]))
        importer.assert_called_once()
        self.assertEqual(importer.call_args.args, (self.file,))
        self.assertEqual(importer.call_args.kwargs, {"display_filename": "124_Sabbah.pdf", "supplier": self.sabbh})
        entry = self.batch.results[0]
        self.assertEqual((entry["status"], entry["invoice_id"], entry["shop"]), ("ok", self.receipt.pk, "Sabbh Oriental"))
        self.assertEqual((self.batch.imported_count, self.batch.awaiting_shop_count), (1, 0))
        self.assertFalse(os.path.exists(self.folder))
        self.assertIn("Sabbh Oriental", self.batch.log)

    def test_a_ticket_already_in_is_said_so(self):
        response, _ = self.choose(side_effect=DuplicateInvoiceError("Déjà dans MarginMate : Sabbh Oriental n° 6800001."))
        self.assertRedirects(response, self.page)
        self.assertEqual(self.batch.results[0]["status"], "duplicate")
        self.assertIn("Déjà dans MarginMate : Sabbh Oriental n° 6800001.", messages_of(response))
        self.assertFalse(os.path.exists(self.file))

    def test_a_failed_import_keeps_the_file_to_try_again(self):
        response, _ = self.choose(side_effect=RuntimeError("photo illisible"))
        self.assertRedirects(response, self.page)
        self.assertEqual(self.batch.results[0]["status"], "unrecognised")
        self.assertTrue(os.path.exists(self.file))
        self.assertTrue(any("photo illisible" in message for message in messages_of(response)))
        self.assertIn("photo illisible", self.batch.log)

    def test_nothing_is_imported_while_the_batch_runs(self):
        """The running thread owns the batch's results: it would write its
        own copy over the change."""
        ReceiptBatch.objects.filter(pk=self.batch.pk).update(
            status=ReceiptBatch.Status.RUNNING, last_heartbeat=timezone.now()
        )
        page = self.client.get(reverse("invoices:receipt_batch_status", args=[self.batch.pk]))
        self.assertNotContains(page, self.url)
        response, importer = self.choose()
        importer.assert_not_called()
        self.assertRedirects(response, self.page)
        self.assertEqual(self.batch.results[0]["status"], "unrecognised")

    def test_a_file_from_before_files_were_kept_asks_for_a_new_upload(self):
        self.batch.results[0].pop("kept")
        self.batch.save(update_fields=["results"])
        page = self.client.get(self.page)
        self.assertNotContains(page, self.url)
        self.assertContains(page, "réimportez")
        _, importer = self.choose()
        importer.assert_not_called()

    def test_a_file_that_has_gone_is_said_so(self):
        os.remove(self.file)
        response, importer = self.choose()
        importer.assert_not_called()
        self.assertIn(MISSING_FILE, messages_of(response))
        self.assertEqual(self.batch.awaiting_shop_count, 0)

    def test_only_a_file_waiting_for_its_shop_can_be_imported(self):
        for index in (1, 7):
            with self.subTest(index=index):
                url = reverse("invoices:receipt_batch_assign", args=[self.batch.pk, index])
                response, importer = self.choose(url=url)
                importer.assert_not_called()
                self.assertRedirects(response, self.page)

    def test_an_unknown_supplier_is_refused(self):
        for supplier in (987654, "", Supplier.objects.get(code="OTHER").pk):
            with self.subTest(supplier=supplier):
                response, importer = self.choose(supplier=supplier)
                importer.assert_not_called()
                self.assertRedirects(response, self.page)
        self.assertEqual(self.batch.results[0]["status"], "unrecognised")

    def test_a_second_choice_waits_for_the_first(self):
        """Two tabs, or a double click: the same file imported twice."""
        receipts.OCR_LOCK.acquire()
        try:
            with mock.patch("invoices.receipt_batches.SHOP_CHOICE_WAIT_SECONDS", 0.01):
                response, importer = self.choose()
        finally:
            receipts.OCR_LOCK.release()
        importer.assert_not_called()
        self.assertIn("Un autre ticket est en cours d'import : réessayez dans un instant.", messages_of(response))
        self.assertEqual(self.batch.results[0]["status"], "unrecognised")

    def test_no_batch_is_resumed_while_a_ticket_is_imported_by_hand(self):
        """The resumed thread would write its own copy of the results over
        the ticket's outcome."""
        ReceiptBatch.objects.filter(pk=self.batch.pk).update(status=ReceiptBatch.Status.FAILED)
        receipts.OCR_LOCK.acquire()
        try:
            with mock.patch("invoices.receipt_batches.threading.Thread") as thread:
                self.assertEqual(resume_batch(self.batch), 0)
        finally:
            receipts.OCR_LOCK.release()
        thread.assert_not_called()

    def test_choosing_only_answers_a_post(self):
        with mock.patch("invoices.receipt_batches.import_receipt") as importer:
            response = self.client.get(self.url)
        self.assertRedirects(response, self.page)
        importer.assert_not_called()


class TypedLinesMatchingTests(TestCase):
    """OCR's forgiving name matching is for what OCR read. A paper ticket
    filed by hand under Metro is typed in, against Metro's catalogue."""

    def _save(self, code):
        receipt = make_invoice(
            supplier=Supplier.objects.get(code=code), parse_checks=[{"label": "x", "passed": True, "detail": ""}]
        )
        line = ParsedLine(
            raw_name="C0CA 33CL", quantity=1, total_volume=Decimal("0"), unit_cost_ht=Decimal("1"),
            total_ht=Decimal("1"), vat_rate=Decimal("0.055"),
        )
        from inventory.matching import resolve_products

        with mock.patch("invoices.importing.resolve_products", wraps=resolve_products) as resolver:
            replace_invoice_lines(receipt, [line])
        return resolver.call_args.kwargs["ocr_tolerant"]

    def test_a_ticket_shop_matches_tolerantly(self):
        self.assertTrue(self._save("FRANPRIX"))

    def test_a_digital_supplier_matches_strictly(self):
        self.assertFalse(self._save("METRO"))
