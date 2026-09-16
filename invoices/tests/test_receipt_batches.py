"""Scanning a folder of receipts: the upload, the background job, and what
makes scanning the same folder again cheap.

The OCR engine never runs here - `import_receipt` is replaced wherever a
batch runs, and the thread wherever one would start - so these test the
plumbing: every file ends in exactly one reported state, a bad file stops
nothing, Stop halts between files, a folder's other files are set aside
rather than refusing the upload, a phone photo is turned upright, and more
than Django's default 100 files get through.
"""

import os
from datetime import date
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.datastructures import MultiValueDict
from PIL import Image

from invoices.forms import ReceiptBatchUploadForm
from invoices.importing import DuplicateInvoiceError
from invoices.models import ReceiptBatch, Supplier
from invoices.ocr import page_images
from invoices.parsers.base import ParsedInvoice, ParsedLine
from invoices.parsers.wingseng import WingSengParser
from invoices.receipt_batches import run_receipt_batch, stage_batch
from invoices.receipts import ReceiptRead, file_sha256, import_receipt
from tests.factories import make_invoice


def upload(name, content=b"%PDF-1.4 a receipt"):
    return SimpleUploadedFile(name, content)


class PhotoPagesTests(SimpleTestCase):
    def _photo(self, orientation=None):
        path = os.path.join(settings.MEDIA_ROOT, f"photo-{orientation or 'plain'}.jpg")
        exif = Image.Exif()
        if orientation:
            exif[0x0112] = orientation
        Image.new("RGB", (40, 20), "white").save(path, exif=exif)
        return path

    def test_a_photo_is_one_page(self):
        (page,) = list(page_images(self._photo()))
        self.assertEqual(page.size, (40, 20))

    def test_a_phone_photo_is_turned_upright_by_its_exif_orientation(self):
        """A phone stores a portrait photo on its side and records the
        rotation separately. Read as stored, the receipt is sideways - noise."""
        (page,) = list(page_images(self._photo(orientation=6)))
        self.assertEqual(page.size, (20, 40))


class UploadFormTests(SimpleTestCase):
    def form(self, *uploads):
        return ReceiptBatchUploadForm(data={}, files=MultiValueDict({"files": list(uploads)}))

    def test_a_folders_other_files_are_set_aside_not_refused(self):
        form = self.form(upload("a.pdf"), upload("b.JPG", b"jpg"), upload("Thumbs.db", b"x"), upload("notes.txt", b"x"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([item.name for item in form.cleaned_data["files"]], ["a.pdf", "b.JPG"])
        self.assertEqual(form.ignored_names, ["Thumbs.db", "notes.txt"])

    def test_a_selection_with_nothing_usable_is_refused(self):
        self.assertFalse(self.form(upload("Thumbs.db", b"x")).is_valid())

    def test_no_file_at_all_is_refused(self):
        self.assertFalse(self.form().is_valid())


class StageBatchTests(TestCase):
    def test_the_files_outlive_the_request_and_each_is_listed(self):
        batch = stage_batch([upload("a.pdf"), upload("b.jpg", b"jpg")], ["Thumbs.db"])
        self.assertEqual([entry["status"] for entry in batch.results], ["pending", "pending", "ignored"])
        for entry in batch.results[:2]:
            self.assertTrue(os.path.exists(os.path.join(settings.MEDIA_ROOT, entry["stored"])))
        self.assertEqual((batch.to_read, batch.progress_percent), (2, 0))


class RunBatchTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="WINGSENG")

    def _receipt(self):
        return make_invoice(
            supplier=self.shop,
            invoice_date=date(2026, 6, 2),
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        )

    def test_every_file_ends_in_one_reported_state_and_a_bad_one_stops_nothing(self):
        batch = stage_batch([upload("ok.pdf"), upload("dup.pdf"), upload("other.pdf"), upload("broken.pdf")])
        outcomes = [
            self._receipt(),
            DuplicateInvoiceError("Fichier déjà importé"),
            ValueError("Enseigne non reconnue"),
            RuntimeError("fichier illisible"),
        ]
        with mock.patch("invoices.receipt_batches.import_receipt", side_effect=outcomes) as importer:
            batch = run_receipt_batch(batch.pk)
        self.assertEqual(importer.call_count, 4)
        self.assertEqual([entry["status"] for entry in batch.results], ["ok", "duplicate", "unrecognised", "error"])
        self.assertEqual(batch.status, ReceiptBatch.Status.SUCCESS)
        self.assertEqual((batch.imported_count, batch.duplicate_count, batch.failed_count), (1, 1, 2))
        self.assertIn("fichier illisible", batch.results[3]["message"])
        self.assertEqual(batch.progress_percent, 100)

    def test_an_imported_file_links_to_its_receipt(self):
        receipt = self._receipt()
        batch = stage_batch([upload("a.pdf")])
        with mock.patch("invoices.receipt_batches.import_receipt", return_value=receipt):
            batch = run_receipt_batch(batch.pk)
        entry = batch.results[0]
        self.assertEqual(
            (entry["invoice_id"], entry["shop"], entry["date"], entry["verified"]),
            (receipt.pk, self.shop.name, "02/06/2026", True),
        )

    def test_the_staged_files_are_cleaned_up(self):
        batch = stage_batch([upload("a.pdf")])
        folder = os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(batch.pk))
        with mock.patch("invoices.receipt_batches.import_receipt", side_effect=ValueError("x")):
            run_receipt_batch(batch.pk)
        self.assertFalse(os.path.exists(folder))

    def test_stop_halts_the_batch_between_files(self):
        batch = stage_batch([upload("a.pdf"), upload("b.pdf")])
        ReceiptBatch.objects.filter(pk=batch.pk).update(cancel_requested=True)
        with mock.patch("invoices.receipt_batches.import_receipt") as importer:
            batch = run_receipt_batch(batch.pk)
        importer.assert_not_called()
        self.assertEqual([entry["status"] for entry in batch.results], ["cancelled", "cancelled"])
        self.assertEqual(batch.status, ReceiptBatch.Status.CANCELLED)


class SameFileTwiceTests(TestCase):
    """Scanning the same folder again must not cost seconds of OCR per
    receipt already in."""

    def _file(self):
        path = os.path.join(settings.MEDIA_ROOT, "ticket-again.pdf")
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4 the same receipt photo")
        return path

    def _read(self):
        parsed = ParsedInvoice(
            supplier_code="WINGSENG",
            invoice_number="000999",
            invoice_date=date(2026, 6, 2),
            lines=[
                ParsedLine(
                    raw_name="MENTHE",
                    quantity=1,
                    total_volume=Decimal("0"),
                    unit_cost_ht=Decimal("0.95"),
                    total_ht=Decimal("0.95"),
                    vat_rate=Decimal("0.055"),
                )
            ],
        )
        return ReceiptRead(parser=WingSengParser(), parsed=parsed, preview=None, text="WING SENG")

    def test_a_file_already_imported_is_refused_before_any_ocr(self):
        path = self._file()
        with mock.patch("invoices.receipts.read_receipt", return_value=self._read()) as reader:
            invoice = import_receipt(path, display_filename="ticket.pdf")
            self.assertEqual(invoice.source_sha256, file_sha256(path))
            # Said the way the rest of the app says dates: the batch page
            # shows this message beside rows dated 02/06/2026.
            with self.assertRaisesMessage(DuplicateInvoiceError, "Wing Seng n° 000999 du 02/06/2026"):
                import_receipt(path, display_filename="ticket.pdf")
        self.assertEqual(reader.call_count, 1)

    def test_a_known_file_without_a_date_is_still_named(self):
        """A receipt whose date was unreadable is still a receipt already in."""
        path = self._file()
        known = make_invoice(
            supplier=Supplier.objects.get(code="WINGSENG"), invoice_number="000777", source_sha256=file_sha256(path)
        )
        known.invoice_date = None
        known.save(update_fields=["invoice_date"])
        with mock.patch("invoices.receipts.read_receipt") as reader:
            with self.assertRaises(DuplicateInvoiceError) as caught:
                import_receipt(path)
        reader.assert_not_called()
        self.assertEqual(str(caught.exception), "Fichier déjà importé : Wing Seng n° 000777.")

    def test_the_same_ticket_photographed_twice_is_refused_by_its_number(self):
        """A second photo is a different file, so it is read - and then
        refused by the ticket number, in the operator's language."""
        first, second = self._file(), os.path.join(settings.MEDIA_ROOT, "ticket-second-photo.pdf")
        with open(second, "wb") as handle:
            handle.write(b"%PDF-1.4 another photo of the same receipt")
        with mock.patch("invoices.receipts.read_receipt", return_value=self._read()) as reader:
            import_receipt(first, display_filename="ticket.pdf")
            with self.assertRaisesMessage(DuplicateInvoiceError, "Déjà dans MarginMate : Wing Seng n° 000999"):
                import_receipt(second, display_filename="ticket-bis.pdf")
        self.assertEqual(reader.call_count, 2)


class UploadViewTests(TestCase):
    def test_an_upload_starts_a_batch_and_goes_to_its_page(self):
        with mock.patch("invoices.receipt_batches.threading.Thread") as thread:
            response = self.client.post(
                reverse("invoices:receipt_upload"), {"files": [upload("a.pdf"), upload("Thumbs.db", b"x")]}
            )
        batch = ReceiptBatch.objects.get()
        self.assertRedirects(response, reverse("invoices:receipt_batch", args=[batch.pk]))
        thread.return_value.start.assert_called_once()
        self.assertEqual([entry["status"] for entry in batch.results], ["pending", "ignored"])

    def test_a_selection_with_nothing_usable_stays_on_the_form(self):
        response = self.client.post(reverse("invoices:receipt_upload"), {"files": [upload("Thumbs.db", b"x")]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ReceiptBatch.objects.exists())


class BigFolderUploadTests(TestCase):
    def test_more_files_than_djangos_default_100_get_through(self):
        """Over the cap, Django answers 400 before any view runs: no batch, no
        message, and nothing to say which photos were lost."""
        files = [upload(f"ticket-{index:03d}.pdf") for index in range(150)]
        with mock.patch("invoices.receipt_batches.threading.Thread"):
            response = self.client.post(reverse("invoices:receipt_upload"), {"files": files})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReceiptBatch.objects.get().to_read, 150)


class BatchPageTests(TestCase):
    def setUp(self):
        self.batch = ReceiptBatch.objects.create(results=[{"name": "a.pdf", "status": "pending", "stored": "x"}])

    def test_the_page_and_its_live_part_render(self):
        for name in ("invoices:receipt_batch", "invoices:receipt_batch_status"):
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(name, args=[self.batch.pk])), "a.pdf")

    def test_stop_only_answers_a_post(self):
        url = reverse("invoices:receipt_batch_cancel", args=[self.batch.pk])
        self.assertRedirects(self.client.get(url), reverse("invoices:receipt_batch", args=[self.batch.pk]))
        self.batch.refresh_from_db()
        self.assertFalse(self.batch.cancel_requested)
        self.client.post(url)
        self.batch.refresh_from_db()
        self.assertTrue(self.batch.cancel_requested)
