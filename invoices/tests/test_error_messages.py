"""What a bar reads of an error: never the server's paths (security audit
LB-3).

A ticket that could not be read stored `str(exc)` as its line and in the
batch's log, both drawn on the bar's batch page - and PIL's words are
« cannot identify image file '<TENANTS_ROOT>\\<tenant>\\imports\\
receipt_batches/1/0000.jpg' »: the server's layout and the tenant's folder.
Now each kind of error is one fixed French sentence (`common.error_for_page`)
- an image, a PDF, anything else - and its detail, path and traceback, goes
to the server's log. The app's own refusals, written in French for the
person, keep their words.

The files here are a few bytes; the OCR engine never runs (every one of
them is refused before it would)."""

import io
import logging
import os
import shutil
from unittest import mock

import pypdfium2 as pdfium
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image

from accounts import paths
from common import SERVER_ERROR, UNREADABLE_IMAGE, UNREADABLE_PDF, error_for_page
from invoices import ocr, receipts
from invoices.einvoice import EInvoiceError
from invoices.models import Supplier
from invoices.receipt_batches import run_receipt_batch, stage_batch
from invoices.receipts import RereadError
from tests.factories import make_invoice


def upload(name, content):
    return SimpleUploadedFile(name, content)


def truncated_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    data = buffer.getvalue()
    return data[: len(data) // 2]


def two_blank_pages() -> bytes:
    document = pdfium.PdfDocument.new()
    for _ in range(2):
        document.new_page(72, 72)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


class ErrorKindTests(SimpleTestCase):
    def test_a_library_s_words_are_never_said(self):
        secret = os.path.join("C:\\", "serveur", "tenants", "abc123", "imports", "0000.jpg")
        for exc in (
            OSError(2, "No such file or directory", secret),
            RuntimeError(f"cannot open {secret}"),
            ValueError(secret),
        ):
            with self.subTest(exc=type(exc).__name__), self.assertLogs("tests.erreurs", "ERROR") as logged:
                said = error_for_page(exc, log=logging.getLogger("tests.erreurs"), what="Essai")
                self.assertEqual(said, SERVER_ERROR)
                self.assertNotIn("abc123", said)
                self.assertIn("Essai", "\n".join(logged.output))

    def test_an_image_or_a_pdf_its_library_cannot_read_says_so(self):
        from PIL import UnidentifiedImageError

        self.assertEqual(
            error_for_page(UnidentifiedImageError("cannot identify image file 'C:\\x.jpg'")), UNREADABLE_IMAGE
        )
        self.assertEqual(error_for_page(pdfium.PdfiumError("Failed to load document")), UNREADABLE_PDF)

    def test_the_app_s_own_refusals_keep_their_words(self):
        refusal = EInvoiceError("Ce fichier XML n'est pas une facture électronique au format EN 16931.")
        self.assertEqual(error_for_page(refusal, said=(EInvoiceError,)), str(refusal))


class BatchLineTests(TestCase):
    """A folder of tickets with broken files, read by the real import."""

    def run_batch(self, *files):
        batch = stage_batch([upload(name, content) for name, content in files])
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
        with self.assertLogs("invoices.receipt_batches", "ERROR") as logged:
            batch = run_receipt_batch(batch.pk)
        return batch, "\n".join(logged.output)

    def assertNoServerPath(self, text):
        for path in (
            str(paths.imports_dir()),
            str(paths.tenants_root()),
            os.path.basename(str(paths.tenants_root())),
            os.path.basename(str(paths.imports_dir().parent)),
        ):
            self.assertNotIn(path, text)
        self.assertNotIn("receipt_batches", text)

    def test_a_broken_photo_a_truncated_one_and_a_broken_pdf_say_what_they_are_and_no_path(self):
        batch, log = self.run_batch(
            ("a.jpg", b"not an image"),
            ("b.png", truncated_png()),
            ("c.pdf", b"%PDF-1.4 cut short"),
        )
        self.assertEqual([entry["status"] for entry in batch.results], ["error", "error", "error"])
        self.assertEqual(
            [entry["message"] for entry in batch.results], [UNREADABLE_IMAGE, UNREADABLE_IMAGE, UNREADABLE_PDF]
        )
        self.assertNoServerPath(batch.log)
        for name in ("invoices:receipt_batch", "invoices:receipt_batch_status"):
            with self.subTest(page=name):
                page = self.client.get(reverse(name, args=[batch.pk]))
                self.assertContains(page, "Image illisible")
                self.assertNoServerPath(page.content.decode())
        # The server's log has it all: which file, and the library's words.
        self.assertIn("cannot identify image file", log)
        self.assertIn("a.jpg", log)

    def test_a_document_too_long_is_said_in_the_app_s_own_words(self):
        with mock.patch.object(ocr, "MAX_PAGES", 1):
            batch = stage_batch([upload("long.pdf", two_blank_pages())])
            self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
            batch = run_receipt_batch(batch.pk)
        self.assertEqual(batch.results[0]["status"], "error")
        self.assertEqual(batch.results[0]["message"], "Document trop long pour être lu : 2 pages, 1 au plus.")

    def test_an_electronic_invoice_refused_keeps_its_sentence(self):
        refusal = EInvoiceError("Cette facture électronique n'est pas un XML lisible (ligne 1).")
        batch = stage_batch([upload("facture.xml", b"<x/>")])
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
        with mock.patch("invoices.receipt_batches.import_document", side_effect=refusal):
            batch = run_receipt_batch(batch.pk)
        self.assertEqual(batch.results[0]["message"], str(refusal))


class PdfImportErrorTests(TestCase):
    def test_the_import_card_says_no_path(self):
        secret = os.path.join(str(paths.tenants_root()), "secret", "facture.pdf")
        upload_ = SimpleUploadedFile("facture.pdf", b"%PDF-1.4", content_type="application/pdf")
        with (
            mock.patch("invoices.receipts.import_document", side_effect=OSError(2, "No such file", secret)),
            self.assertLogs("invoices.views", "ERROR") as logged,
        ):
            response = self.client.post(
                reverse("invoices:invoice_upload"),
                {"supplier": Supplier.objects.get(code="METRO").pk, "source_file": upload_},
                follow=True,
            )
        self.assertContains(response, "Échec de l&#x27;import. Erreur inattendue sur le serveur")
        self.assertNotContains(response, "secret")
        self.assertIn("secret", "\n".join(logged.output))


class RereadErrorTests(TestCase):
    def test_a_supplier_reader_failing_on_the_stored_file_says_no_path(self):
        metro = Supplier.objects.get(code="METRO")
        invoice = make_invoice(supplier=metro)
        secret = os.path.join(str(paths.media_root()), "invoices", "2026", "06", "secret.pdf")
        parser = type(receipts.get_parser(metro.parser_key))
        with (
            mock.patch.object(parser, "parse", side_effect=FileNotFoundError(2, "No such file", secret)),
            self.assertLogs("invoices.receipts", "ERROR"),
        ):
            with self.assertRaises(RereadError) as refused:
                receipts._reread_invoice_file(invoice, secret)
        self.assertIn("La relecture a échoué", str(refused.exception))
        self.assertNotIn("secret", str(refused.exception))

    def test_a_ticket_whose_photo_cannot_be_opened_is_a_sentence_not_a_500(self):
        from PIL import UnidentifiedImageError

        shop = Supplier.objects.get(code="WINGSENG")
        invoice = make_invoice(supplier=shop)
        secret = os.path.join(str(paths.media_root()), "invoices", "secret.jpg")
        with (
            mock.patch.object(
                receipts, "read_receipt", side_effect=UnidentifiedImageError(f"cannot identify image file {secret!r}")
            ),
            self.assertLogs("invoices.receipts", "ERROR"),
        ):
            with self.assertRaises(RereadError) as refused:
                receipts._reread_receipt_file(invoice, secret)
        self.assertIn(UNREADABLE_IMAGE, str(refused.exception))
        self.assertNotIn("secret", str(refused.exception))
