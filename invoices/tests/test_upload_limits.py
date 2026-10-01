"""What an upload may weigh, and what a PDF may cost to read (security
audit UPLOAD-1).

Nothing capped a file's size - a 64 MB file named .jpg was staged whole in
the tenant's imports/ - nor what reading a PDF costs: a page with no text is
rendered at 300 dpi at whatever size its MediaBox declares, every page of
it, so a few hundred bytes decided a bitmap of gigabytes in the one process
that serves every bar.

Now: 25 MB a file on every form (`common.UPLOAD_MAX_FILE_BYTES`), refused by
its name - in a folder of tickets, that file's own error and the others read
- a total per request (`invoices.forms.RECEIPT_BATCH_MAX_BYTES` for the
folder), and in `ocr.page_images` a page cap and a pixel budget worked out
from the page's declared size BEFORE anything is rendered.

Machine safety: every file here is a few bytes, with the caps patched down,
or with its `size` said bigger than it is; no render is bigger than a few
hundred thousand pixels."""

import os
import shutil
from unittest import mock

import pypdfium2 as pdfium
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.datastructures import MultiValueDict
from PIL import Image

from accounts import paths
from common import MEGABYTE, UPLOAD_MAX_FILE_BYTES, file_too_big, selection_too_big, weight
from invoices import ocr
from invoices.forms import InvoiceUploadForm, ManualInvoiceForm, ReceiptBatchUploadForm
from invoices.models import Invoice, ReceiptBatch, Supplier
from invoices.receipt_batches import stage_batch


def upload(name, content=b"%PDF-1.4 un ticket", size=None):
    """A few bytes - `size` is what Django is told it weighs."""
    item = SimpleUploadedFile(name, content)
    if size is not None:
        item.size = size
    return item


class WeightTests(SimpleTestCase):
    def test_said_in_french(self):
        self.assertEqual(weight(25 * MEGABYTE), "25 Mo")
        self.assertEqual(weight(612 * MEGABYTE + MEGABYTE // 2), "612,5 Mo")
        self.assertEqual(weight(300 * 1024), "300 Ko")
        self.assertEqual(weight(1), "1 Ko")

    def test_a_file_over_the_cap_is_named(self):
        big = upload("photo.jpg", size=UPLOAD_MAX_FILE_BYTES + 1)
        self.assertEqual(file_too_big(big), "« photo.jpg » pèse 25 Mo : 25 Mo au plus par fichier.")
        self.assertEqual(file_too_big(upload("photo.jpg", size=UPLOAD_MAX_FILE_BYTES)), "")

    def test_a_selection_over_its_total(self):
        files = [upload(f"{n}.jpg", size=40 * MEGABYTE) for n in range(3)]
        self.assertIn("La sélection pèse 120 Mo : 100 Mo au plus", selection_too_big(files))
        self.assertEqual(selection_too_big(files[:2]), "")


class ReceiptFolderFormTests(SimpleTestCase):
    def form(self, *uploads):
        return ReceiptBatchUploadForm(data={}, files=MultiValueDict({"files": list(uploads)}))

    def test_a_file_over_25_mb_is_set_aside_by_name_and_the_rest_is_read(self):
        form = self.form(upload("a.pdf"), upload("enorme.jpg", size=30 * MEGABYTE), upload("b.jpg"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([item.name for item in form.cleaned_data["files"]], ["a.pdf", "b.jpg"])
        ((name, message),) = form.refused
        self.assertEqual(name, "enorme.jpg")
        self.assertEqual(message, "« enorme.jpg » pèse 30 Mo : 25 Mo au plus par fichier.")

    def test_nothing_under_the_cap_is_refused_naming_the_files(self):
        form = self.form(upload("enorme.jpg", size=30 * MEGABYTE), upload("Thumbs.db", b"x"))
        self.assertFalse(form.is_valid())
        self.assertIn("« enorme.jpg » pèse 30 Mo", str(form.errors["files"]))

    def test_the_folder_as_a_whole_has_a_total(self):
        files = [upload(f"photo-{n:03d}.jpg", size=6 * MEGABYTE) for n in range(90)]
        form = self.form(*files)
        self.assertFalse(form.is_valid())
        self.assertIn("La sélection pèse 540 Mo : 500 Mo au plus en une fois", str(form.errors["files"]))

    def test_a_file_only_ignored_is_not_in_the_total(self):
        """A video beside a ticket (review UPLOAD-TOTAL-IGNORED): listed as
        ignored and never written to the server, it refused the whole folder
        by weighing on a total that bounds what is written."""
        form = self.form(upload("ticket.pdf"), upload("VID_0001.mp4", size=600 * MEGABYTE))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([item.name for item in form.cleaned_data["files"]], ["ticket.pdf"])
        self.assertEqual(form.ignored_names, ["VID_0001.mp4"])

    def test_files_refused_on_their_own_are_not_in_the_total_either(self):
        """Each over 25 MB, each that file's error, none written."""
        scans = [upload(f"scan-{n:02d}.pdf", size=30 * MEGABYTE) for n in range(20)]
        form = self.form(upload("ticket.pdf"), *scans)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([item.name for item in form.cleaned_data["files"]], ["ticket.pdf"])
        self.assertEqual(len(form.refused), 20)

    def test_the_total_said_is_what_would_be_written(self):
        photos = [upload(f"photo-{n:03d}.jpg", size=6 * MEGABYTE) for n in range(90)]
        form = self.form(*photos, upload("VID_0001.mp4", size=300 * MEGABYTE))
        self.assertFalse(form.is_valid())
        self.assertIn("La sélection pèse 540 Mo : 500 Mo au plus en une fois", str(form.errors["files"]))

    def test_a_normal_folder_of_phone_photos_goes_through(self):
        """Eighty photos of 5 MB - several months of tickets - or 1 500 scans
        like the owner's (0.3 MB): both under the total."""
        photos = [upload(f"photo-{n:03d}.jpg", size=5 * MEGABYTE) for n in range(80)]
        self.assertTrue(self.form(*photos).is_valid())
        scans = [upload(f"scan-{n:04d}.pdf", size=300 * 1024) for n in range(1500)]
        self.assertTrue(self.form(*scans).is_valid())


def clear_staged_batches():
    """SQLite gives a rolled-back pk out again, and the folders an earlier test
    staged stay on disk: a batch's folder may already hold a file another
    test's batch of the same pk wrote (test_receipt_batches run first, in the
    same process, did - the parallel runner's order)."""
    shutil.rmtree(os.path.join(paths.imports_dir(), "receipt_batches"), ignore_errors=True)


class StagedRefusalsTests(TestCase):
    def setUp(self):
        clear_staged_batches()

    def test_a_refused_file_is_that_file_s_error_and_never_written(self):
        batch = stage_batch(
            [upload("a.pdf")], ["Thumbs.db"], [("enorme.jpg", "« enorme.jpg » pèse 30 Mo : 25 Mo au plus par fichier.")]
        )
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
        self.assertEqual([entry["status"] for entry in batch.results], ["pending", "error", "ignored"])
        refused = batch.results[1]
        self.assertEqual(refused["name"], "enorme.jpg")
        self.assertIn("25 Mo au plus", refused["message"])
        self.assertNotIn("stored", refused)
        self.assertEqual(os.listdir(os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk))), ["0000.pdf"])
        self.assertEqual((batch.to_read, batch.failed_count), (2, 1))


class ReceiptUploadPageTests(TestCase):
    def setUp(self):
        clear_staged_batches()

    def test_the_folder_upload_stages_only_what_fits(self):
        with mock.patch("common.UPLOAD_MAX_FILE_BYTES", 30), mock.patch("invoices.receipt_batches.threading.Thread"):
            response = self.client.post(
                reverse("invoices:receipt_upload"),
                {"files": [upload("petit.pdf", b"%PDF-1.4 x"), upload("gros.jpg", b"x" * 64)]},
            )
        batch = ReceiptBatch.objects.get()
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
        self.assertRedirects(
            response, reverse("invoices:receipt_batch", args=[batch.pk]), fetch_redirect_response=False
        )
        self.assertEqual(
            [(entry["name"], entry["status"]) for entry in batch.results],
            [("petit.pdf", "pending"), ("gros.jpg", "error")],
        )
        self.assertEqual(os.listdir(os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk))), ["0000.pdf"])
        page = self.client.get(reverse("invoices:receipt_batch", args=[batch.pk]))
        self.assertContains(page, "gros.jpg")
        self.assertContains(page, "au plus par fichier")

    def test_two_shots_of_one_name_are_two_files(self):
        """What arrives when a browser could not rename the camera's shots
        (static/js/photos.js keeps the names where DataTransfer is missing):
        iOS names every one image.jpg. Staged by their place in the post,
        neither writes over the other, and both keep the name they came
        with - the duplicates are found by content, not by name."""
        first, second = b"premier ticket", b"second ticket, autre contenu"
        with mock.patch("invoices.receipt_batches.start_batch") as started:
            response = self.client.post(
                reverse("invoices:receipt_upload"),
                {"files": [upload("image.jpg", first), upload("image.jpg", second)]},
            )
        batch = ReceiptBatch.objects.get()
        folder = os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk))
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertRedirects(
            response, reverse("invoices:receipt_batch", args=[batch.pk]), fetch_redirect_response=False
        )
        started.assert_called_once()
        self.assertEqual(
            [(entry["name"], entry["status"]) for entry in batch.results],
            [("image.jpg", "pending"), ("image.jpg", "pending")],
        )
        stored = [entry["stored"] for entry in batch.results]
        self.assertEqual(len(set(stored)), 2)
        self.assertEqual(sorted(os.listdir(folder)), ["0000.jpg", "0001.jpg"])
        with open(os.path.join(folder, "0000.jpg"), "rb") as handle:
            self.assertEqual(handle.read(), first)
        with open(os.path.join(folder, "0001.jpg"), "rb") as handle:
            self.assertEqual(handle.read(), second)

    def test_a_folder_over_its_total_stages_nothing(self):
        with (
            mock.patch("invoices.forms.RECEIPT_BATCH_MAX_BYTES", 40),
            mock.patch("invoices.receipt_batches.threading.Thread") as thread,
        ):
            response = self.client.post(
                reverse("invoices:receipt_upload"),
                {"files": [upload(f"{n}.pdf", b"%PDF-1.4 " + b"x" * 20) for n in range(3)]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "La sélection pèse")
        self.assertFalse(ReceiptBatch.objects.exists())
        thread.assert_not_called()


class PdfImportFormTests(TestCase):
    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")

    def test_the_supplier_pdf_is_capped_by_its_name(self):
        big = upload("facture.pdf", size=26 * MEGABYTE)
        form = InvoiceUploadForm(data={"supplier": str(self.metro.pk)}, files=MultiValueDict({"source_file": [big]}))
        self.assertFalse(form.is_valid())
        self.assertIn("« facture.pdf » pèse 26 Mo : 25 Mo au plus par fichier.", str(form.errors["source_file"]))

    def test_the_page_refuses_it_before_anything_is_read(self):
        with mock.patch("common.UPLOAD_MAX_FILE_BYTES", 10), mock.patch("invoices.receipts.import_document") as reader:
            response = self.client.post(
                reverse("invoices:invoice_upload"),
                {"supplier": self.metro.pk, "source_file": upload("facture.pdf", b"%PDF-1.4 trop long")},
            )
        reader.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "au plus par fichier")
        self.assertFalse(Invoice.objects.exists())

    def test_the_receipt_of_a_hand_typed_invoice_too(self):
        big = upload("justificatif.jpg", size=26 * MEGABYTE)
        form = ManualInvoiceForm(
            data={"supplier": str(self.metro.pk), "invoice_number": "F-1", "invoice_date": "2026-06-02"},
            files=MultiValueDict({"source_file": [big]}),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("« justificatif.jpg » pèse 26 Mo", str(form.errors["source_file"]))


class PdfCostTests(SimpleTestCase):
    """`ocr.page_images` decides what a PDF may cost before it renders it."""

    def setUp(self):
        self.folder = os.path.join(paths.media_root(), "limites-ocr")
        os.makedirs(self.folder, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.folder, True)

    def blank_pdf(self, name, pages, size=(300, 300)):
        """`pages` empty pages of `size` points: no text, no image - every
        one would be rendered."""
        document = pdfium.PdfDocument.new()
        for _ in range(pages):
            document.new_page(*size)
        path = os.path.join(self.folder, name)
        document.save(path)
        document.close()
        return path

    def photo_pdf(self, name, pixels=(40, 20)):
        """A phone scan: one image covering its page."""
        path = os.path.join(self.folder, name)
        Image.new("RGB", pixels, "white").save(path, "PDF")
        return path

    def test_more_pages_than_the_cap_is_refused_before_any_render(self):
        path = self.blank_pdf("long.pdf", 3)
        with mock.patch.object(ocr, "MAX_PAGES", 2), mock.patch.object(pdfium.PdfPage, "render") as render:
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                list(ocr.page_images(path))
        render.assert_not_called()
        self.assertEqual(str(refused.exception), "Document trop long pour être lu : 3 pages, 2 au plus.")

    def test_thirty_pages_is_the_cap(self):
        self.assertEqual(ocr.MAX_PAGES, 30)
        self.assertEqual(ocr.RENDER_MAX_PIXELS, 40_000_000)

    def test_a_small_page_is_still_rendered_at_300_dpi(self):
        path = self.blank_pdf("petit.pdf", 1, size=(72, 36))
        (image,) = list(ocr.page_images(path))
        self.assertEqual(image.size, (300, 150))

    def test_a_page_over_the_budget_is_rendered_smaller(self):
        """300 pt square is 1250 px at 300 dpi (1.56 Mpx); with a budget of
        0.25 Mpx it comes out at 500 px, and never above the budget."""
        path = self.blank_pdf("grande.pdf", 1)
        with mock.patch.object(ocr, "RENDER_MAX_PIXELS", 250_000):
            (image,) = list(ocr.page_images(path))
        self.assertLessEqual(image.width * image.height, 250_000)
        self.assertGreaterEqual(image.width, 490)

    def test_a_page_too_big_even_at_the_lowest_scale_is_refused_before_any_render(self):
        path = self.blank_pdf("immense.pdf", 2)
        with (
            mock.patch.object(ocr, "RENDER_MAX_PIXELS", 100_000),
            mock.patch.object(pdfium.PdfPage, "render") as render,
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                list(ocr.page_images(path))
        render.assert_not_called()
        self.assertIn("Page trop grande pour être lue (page 1 : 10,6 × 10,6 cm)", str(refused.exception))

    def test_a_declared_page_of_the_audit_s_size_is_refused_without_a_render(self):
        """The audit's bomb: a page of 14 400 pt (5 m) is 60 000 px square at
        300 dpi, about 11 GB. Its size is read from a file of a few hundred
        bytes and refused - nothing is rendered."""
        path = self.blank_pdf("bombe.pdf", 1, size=(14400, 14400))
        self.assertLess(os.path.getsize(path), 2000)
        with mock.patch.object(pdfium.PdfPage, "render") as render:
            with self.assertRaises(ocr.DocumentTooBig):
                list(ocr.page_images(path))
        render.assert_not_called()

    def test_a_scan_s_own_image_is_checked_before_it_is_decoded(self):
        path = self.photo_pdf("scan.pdf")
        (image,) = list(ocr.page_images(path))
        self.assertEqual(image.size, (40, 20))
        with (
            mock.patch.object(ocr, "IMAGE_MAX_PIXELS", 100),
            mock.patch.object(pdfium.PdfImage, "get_bitmap") as bitmap,
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                list(ocr.page_images(path))
        bitmap.assert_not_called()
        self.assertIn("Image trop grande pour être lue", str(refused.exception))

    def test_a_photo_file_too_many_pixels_is_refused_from_its_header(self):
        path = os.path.join(self.folder, "photo.png")
        Image.new("RGB", (40, 20), "white").save(path)
        with mock.patch.object(ocr, "IMAGE_MAX_PIXELS", 100):
            with self.assertRaises(ocr.DocumentTooBig):
                list(ocr.page_images(path))

    def test_a_refusal_is_the_app_s_own_sentence(self):
        """French, for the person: a batch shows it on that file's line."""
        self.assertTrue(issubclass(ocr.DocumentTooBig, ValueError))

    def test_every_call_into_pdfium_holds_the_process_s_lock_and_no_page_keeps_it(self):
        """PDFium is not thread-safe, and a folder's thread read its files
        with no lock (receipt_batches._read_file) beside a request's OCR, a
        gather's, a shop chosen - in the one process serving every bar. Each
        call into it now takes `ocr.PDFIUM_LOCK`; a page handed to the
        caller - who OCRs it for seconds - holds nothing."""
        calls = []

        def held(name, original):
            def wrapped(*args, **kwargs):
                calls.append((name, ocr.PDFIUM_LOCK._is_owned()))
                return original(*args, **kwargs)

            return wrapped

        patches = [
            mock.patch.object(pdfium.PdfPage, "render", held("render", pdfium.PdfPage.render)),
            mock.patch.object(pdfium.PdfPage, "get_size", held("get_size", pdfium.PdfPage.get_size)),
            mock.patch.object(pdfium.PdfImage, "get_bitmap", held("get_bitmap", pdfium.PdfImage.get_bitmap)),
            mock.patch.object(pdfium.PdfDocument, "close", held("close", pdfium.PdfDocument.close)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for path in (self.blank_pdf("deux.pdf", 2, size=(72, 36)), self.photo_pdf("scan-verrou.pdf")):
            with self.subTest(path=os.path.basename(path)):
                calls.clear()
                pages = 0
                for image in ocr.page_images(path):
                    pages += 1
                    self.assertFalse(ocr.PDFIUM_LOCK._is_owned(), "a page handed over still holds the lock")
                    self.assertEqual(image.mode, "RGB")
                self.assertGreaterEqual(pages, 1)
                self.assertIn("close", [name for name, _ in calls])
                self.assertEqual([name for name, owned in calls if not owned], [])
        self.assertFalse(ocr.PDFIUM_LOCK._is_owned())

    def test_a_document_left_half_read_is_closed_under_the_lock(self):
        path = self.blank_pdf("abandon.pdf", 2, size=(72, 36))
        with mock.patch.object(
            pdfium.PdfDocument,
            "close",
            autospec=True,
            side_effect=lambda document: closed.append(ocr.PDFIUM_LOCK._is_owned()),
        ):
            closed = []
            pages = ocr.page_images(path)
            next(pages)
            pages.close()
        self.assertEqual(closed, [True])
