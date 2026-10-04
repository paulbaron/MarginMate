"""Achats' guard: a driver's returnables slip dropped among the purchases.

The owner drops PDFs on Achats → « Ajouter des achats ». A slip dropped there
- one file, or a folder of history - was recognised as UBA's by its printed
phone number and read by the ticket reader: the empties taken back became
POSITIVE purchase lines, silently wrong money. `receipts.import_document`
now sends a PDF that one active slip format recognises to Consignes
(returnables.slips.store_slip) and raises RoutedToReturnablesError - no
Invoice - and every caller says so in its own place: the upload, a folder's
import, the gather.

PDFs written by hand (a few hundred bytes), every value invented.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from unittest import mock

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts import paths
from invoices.importing import DuplicateInvoiceError, RoutedToReturnablesError
from invoices.models import Invoice, ScrapeJob, Supplier
from invoices.receipt_batches import ShopChoiceError, import_with_shop, run_receipt_batch, stage_batch
from invoices.receipts import import_document
from invoices.tests.pdf_files import write_pdf
from invoices.tests.test_generic_tables import WEB_INVOICE
from returnables import reading
from returnables.models import Slip
from returnables.tests.support import (
    KEG_LINE,
    SEEDED_FORMAT_NAME,
    make_format,
    make_supplier,
    seeded_format,
    slip_text,
    tiny_pdf,
)
from tests.factories import make_supplier as make_invoice_supplier
from tests.support import NoNetworkTestCase

ROUTED = "Bon de consignes : rangé dans Consignes (bon n° 4243) — ce n'est pas une facture."
ALREADY = "Bon de consignes : déjà reçu dans Consignes (bon n° 4243) — ce n'est pas une facture."

#: The shape of an UBA invoice (the table rows its reader reads are not in
#: a hand-written PDF; what matters here is that it is no slip): invented.
UBA_INVOICE = [
    "U.B.A.",
    "Facture No : VE-0000000001",
    "DATE FACTURE",
    "10/02/2026",
    "CODE LIBELLE QTE PRIX",
    "EMB01 FUT INOX 30 L -3 EMB 30,00 -90,00",
    "DECONSIGNE TICKET NO : 4243",
    "TOTAL FACTURE 0,00",
]


def slip_text_lines(number="4243", reference="555001", copy=""):
    return slip_text(number=number, references=(reference,), lines=(KEG_LINE,)).split("\n") + ([copy] if copy else [])


class GuardCase(NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.folder = self.enterContext(tempfile.TemporaryDirectory())

    def pdf(self, lines, name="document.pdf") -> str:
        return write_pdf(os.path.join(self.folder, name), list(lines))

    def assertNoInvoice(self):
        self.assertFalse(Invoice.objects.exists())


class ImportDocumentTests(GuardCase):
    def test_a_slip_goes_to_returnables_and_never_becomes_an_invoice(self):
        with self.assertRaises(RoutedToReturnablesError) as routed:
            import_document(self.pdf(slip_text_lines()), display_filename="T0000000042.pdf")
        self.assertEqual(str(routed.exception), ROUTED)
        self.assertIsInstance(routed.exception, DuplicateInvoiceError)
        slip = Slip.objects.get()
        self.assertEqual(
            (slip.origin, slip.format, slip.original_name, slip.number),
            (Slip.Origin.UPLOAD, seeded_format(), "T0000000042.pdf", "4243"),
        )
        self.assertEqual(routed.exception.slip, slip)
        self.assertNoInvoice()

    def test_the_same_slip_again_is_already_there(self):
        path = self.pdf(slip_text_lines())
        with self.assertRaises(RoutedToReturnablesError):
            import_document(path)
        for again in (path, self.pdf(slip_text_lines(copy="reimpression"), "renvoi.pdf")):
            with self.subTest(again=again), self.assertRaises(RoutedToReturnablesError) as routed:
                import_document(again)
            self.assertEqual(str(routed.exception), ALREADY)
        self.assertEqual(Slip.objects.count(), 1)
        self.assertNoInvoice()

    def test_the_slip_text_is_read_once(self):
        with (
            mock.patch("returnables.reading.pdf_text", wraps=reading.pdf_text) as read,
            self.assertRaises(RoutedToReturnablesError),
        ):
            import_document(self.pdf(slip_text_lines()))
        self.assertEqual(read.call_count, 1)

    def test_it_is_a_slip_whoever_the_supplier_named(self):
        venue = make_invoice_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        with self.assertRaises(RoutedToReturnablesError):
            import_document(self.pdf(slip_text_lines()), supplier=venue, chosen_because="Fournisseur choisi.")
        self.assertEqual(Slip.objects.count(), 1)
        self.assertNoInvoice()

    def test_an_ordinary_invoice_is_imported_as_before(self):
        supplier = make_invoice_supplier(code="CUISIPRO", name="Cuisipro", parser_key="")
        invoice = import_document(
            self.pdf(WEB_INVOICE.replace("€", "EUR").split("\n")),
            display_filename="facture-web.pdf",
            supplier=supplier,
            chosen_because="Fournisseur choisi à l'import de la facture (Cuisipro).",
        )
        self.assertEqual(invoice.supplier, supplier)
        self.assertEqual(invoice.lines.count(), 3)
        self.assertFalse(Slip.objects.exists())

    def test_an_uba_invoice_still_goes_to_ubas_reader(self):
        uba = Supplier.objects.get(code="UBA")
        with (
            mock.patch("invoices.receipts.import_invoice_pdf", return_value="lue") as own_reader,
            mock.patch("invoices.receipts.import_receipt") as ticket_reader,
        ):
            self.assertEqual(import_document(self.pdf(UBA_INVOICE), supplier=uba), "lue")
        own_reader.assert_called_once()
        ticket_reader.assert_not_called()
        self.assertFalse(Slip.objects.exists())

    def test_several_formats_recognising_it_is_a_slip_all_the_same(self):
        """Which one is for a person to say, on the Consignes page - never a
        purchase in the meantime."""
        make_format(name="Autre format", supplier=make_supplier())
        with self.assertRaises(RoutedToReturnablesError) as routed:
            import_document(self.pdf(slip_text_lines()))
        self.assertIn("plusieurs formats le reconnaissent", str(routed.exception))
        self.assertIn(f"« {SEEDED_FORMAT_NAME} »", str(routed.exception))
        self.assertIn("« Autre format »", str(routed.exception))
        self.assertFalse(Slip.objects.exists())
        self.assertNoInvoice()

    def test_a_format_whose_pattern_runs_out_of_time_takes_no_part(self):
        slow = make_format(name="Format lent", supplier=make_supplier())
        original = reading.detect_format

        def detect(text, formats):
            (fmt,) = formats
            if fmt == slow:
                raise reading.SlipError("Le format « Format lent » n'a pas pu être essayé (trop lent).")
            return original(text, formats)

        with (
            mock.patch("returnables.reading.detect_format", side_effect=detect),
            self.assertRaises(RoutedToReturnablesError) as routed,
        ):
            import_document(self.pdf(slip_text_lines()))
        self.assertEqual(str(routed.exception), ROUTED)
        self.assertEqual(Slip.objects.get().format, seeded_format())


class CarriesOnTests(GuardCase):
    """Nothing changes for what is no slip, or cannot be read as one."""

    def import_untouched(self, path, **pdf_text):
        """Import `path` with the readers after the guard replaced: whether
        the import went on, and whether the slip reader was asked."""
        with (
            mock.patch("returnables.reading.pdf_text", **(pdf_text or {"wraps": reading.pdf_text})) as read,
            mock.patch("invoices.receipts.document_text", return_value=""),
            mock.patch("invoices.receipts.import_receipt", return_value="ticket") as ticket_reader,
        ):
            self.assertEqual(import_document(path), "ticket")
        ticket_reader.assert_called_once()
        self.assertFalse(Slip.objects.exists())
        return read

    def test_a_photo_is_never_read_as_a_slip(self):
        path = os.path.join(self.folder, "ticket.jpg")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xd8\xff\xe0 photo exemple")
        self.import_untouched(path, side_effect=AssertionError("a photo read as a bon")).assert_not_called()

    def test_no_format_with_a_start_pattern_reads_nothing(self):
        type(seeded_format()).objects.update(section_start="  ")
        self.import_untouched(self.pdf(slip_text_lines()), side_effect=AssertionError("read")).assert_not_called()

    def test_an_inactive_format_recognises_nothing(self):
        type(seeded_format()).objects.update(is_active=False)
        self.import_untouched(self.pdf(slip_text_lines()), side_effect=AssertionError("read")).assert_not_called()

    def test_a_pdf_the_slip_reader_cannot_read_carries_on(self):
        read = self.import_untouched(self.pdf(slip_text_lines()), side_effect=reading.SlipError(reading.NO_TEXT))
        read.assert_called_once()

    def test_a_pdf_too_heavy_for_a_slip_is_not_even_read(self):
        with mock.patch.object(reading, "MAX_PDF_BYTES", 50):
            self.import_untouched(self.pdf(slip_text_lines()), side_effect=AssertionError("read")).assert_not_called()

    def test_a_pdf_that_is_no_slip_carries_on(self):
        self.import_untouched(self.pdf(["FACTURE EXEMPLE", "Total 12,00"])).assert_called_once()


class CallersTests(GuardCase):
    """Each place a document is imported says it in its own words: the
    upload's message, a folder's row, the gather's log."""

    def test_the_upload_says_where_it_went(self):
        with open(self.pdf(slip_text_lines()), "rb") as handle:
            upload = SimpleUploadedFile("T0000000042.pdf", handle.read(), content_type="application/pdf")
        uba = Supplier.objects.get(code="UBA")
        response = self.client.post(reverse("invoices:invoice_upload"), {"source_file": upload, "supplier": uba.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], [ROUTED])
        self.assertEqual(Slip.objects.get().original_name, "T0000000042.pdf")
        self.assertNoInvoice()

    def test_a_folders_import_files_it_under_returnables(self):
        with open(self.pdf(slip_text_lines()), "rb") as handle:
            batch = stage_batch([SimpleUploadedFile("T0000000042.pdf", handle.read())])
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
        with mock.patch("invoices.receipt_batches._Heartbeat"):
            batch = run_receipt_batch(batch.pk)
        entry = batch.results[0]
        self.assertEqual((entry["status"], entry["message"], entry.get("consignes")), ("duplicate", ROUTED, True))
        self.assertEqual(Slip.objects.count(), 1)
        self.assertNoInvoice()
        page = self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk]))
        self.assertContains(page, "Rangé dans Consignes")
        self.assertNotContains(page, "Déjà importé")

    def test_an_ordinary_duplicate_in_a_folder_still_says_so(self):
        from invoices.receipt_batches import _read_file

        entry = {"name": "facture.pdf", "stored": "x.pdf", "status": "pending"}
        with (
            mock.patch("invoices.receipt_batches._staged", return_value=self.pdf(["x"])),
            mock.patch(
                "invoices.receipt_batches.import_document",
                side_effect=DuplicateInvoiceError("Fichier déjà importé : x."),
            ),
        ):
            _read_file(mock.Mock(), entry)
        self.assertEqual(entry["status"], "duplicate")
        self.assertNotIn("consignes", entry)

    def test_the_gather_logs_it_as_a_slip_not_as_already_imported(self):
        from invoices.tasks import _import_document_file

        job = ScrapeJob.objects.create()
        venue = make_invoice_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        path = self.pdf(slip_text_lines(), "T0000000042.pdf")
        brought_in = _import_document_file(job, venue, path, chosen_because="Reçue par e-mail (« Exemple »).")
        job.refresh_from_db()
        self.assertFalse(brought_in)
        self.assertIn(f"T0000000042.pdf : {ROUTED}", job.log)
        self.assertNotIn("déjà importé", job.log)
        self.assertEqual(Slip.objects.count(), 1)
        self.assertNoInvoice()


class FolderRowTests(GuardCase):
    """A folder's row says where the file went - and only where it went."""

    def stage(self, lines=None, name="T0000000042.pdf"):
        with open(self.pdf(lines or slip_text_lines()), "rb") as handle:
            batch = stage_batch([SimpleUploadedFile(name, handle.read())])
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
        return batch

    def page(self, batch):
        return self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk]))

    def kept_unrecognised(self, batch):
        """As a file read before its format existed is left: « Enseigne
        inconnue », kept for the operator to name its shop."""
        batch.results[0].update(status="unrecognised", message="Aucune enseigne reconnue.", kept=True)
        batch.save(update_fields=["results"])
        return batch

    def test_a_slip_several_formats_recognise_is_an_error_not_filed(self):
        """Nothing was stored: « Rangé dans Consignes » said the opposite, and
        the file was counted among the « Déjà connus »."""
        make_format(name="Autre format", supplier=make_supplier())
        batch = self.stage()
        with mock.patch("invoices.receipt_batches._Heartbeat"):
            batch = run_receipt_batch(batch.pk)
        entry = batch.results[0]
        self.assertEqual(entry["status"], "error")
        self.assertNotIn("consignes", entry)
        self.assertIn("plusieurs formats le reconnaissent", entry["message"])
        self.assertIn("déposez-le sur la page Consignes", entry["message"])
        self.assertEqual((batch.failed_count, batch.duplicate_count), (1, 0))
        self.assertFalse(Slip.objects.exists())
        self.assertNoInvoice()
        page = self.page(batch)
        self.assertNotContains(page, "Rangé dans Consignes")
        self.assertContains(page, "plusieurs formats le reconnaissent")

    def test_naming_the_shop_of_a_file_that_is_a_slip_files_it_under_returnables(self):
        batch = self.kept_unrecognised(self.stage())
        venue = make_invoice_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        entry = import_with_shop(batch, 0, venue)
        self.assertEqual((entry["status"], entry["message"], entry.get("consignes")), ("duplicate", ROUTED, True))
        batch.refresh_from_db()
        self.assertEqual(batch.results[0], entry)
        self.assertEqual(Slip.objects.count(), 1)
        self.assertNoInvoice()
        page = self.page(batch)
        self.assertContains(page, "Rangé dans Consignes")
        self.assertNotContains(page, "Déjà importé")

    def test_naming_the_shop_of_a_slip_several_formats_recognise_keeps_the_file(self):
        make_format(name="Autre format", supplier=make_supplier())
        batch = self.kept_unrecognised(self.stage())
        before = dict(batch.results[0])
        venue = make_invoice_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        with self.assertRaises(ShopChoiceError) as refused:
            import_with_shop(batch, 0, venue)
        self.assertIn("plusieurs formats le reconnaissent", str(refused.exception))
        batch.refresh_from_db()
        self.assertEqual(batch.results[0], before)
        self.assertTrue(os.path.exists(os.path.join(paths.imports_dir(), before["stored"])))
        self.assertFalse(Slip.objects.exists())
        self.assertNoInvoice()


class AiUploadTests(GuardCase):
    """« Analyse IA » chosen on Achats for a slip: the one supplier choice that
    went straight to parse_and_import, around the guard - the model asked
    for « every purchased product line » read the empties as purchases."""

    def test_a_slip_uploaded_for_the_ai_reading_goes_to_returnables(self):
        from invoices.parsers import LLM_PARSER_KEY

        ai = Supplier.objects.get(parser_key=LLM_PARSER_KEY)
        with open(self.pdf(slip_text_lines()), "rb") as handle:
            upload = SimpleUploadedFile("T0000000042.pdf", handle.read(), content_type="application/pdf")
        with mock.patch(
            "invoices.parsers.llm_fallback.LLMFallbackParser.parse", side_effect=AssertionError("a bon sent to the AI")
        ):
            response = self.client.post(reverse("invoices:invoice_upload"), {"source_file": upload, "supplier": ai.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], [ROUTED])
        self.assertEqual(Slip.objects.get().original_name, "T0000000042.pdf")
        self.assertNoInvoice()

    def test_an_invoice_for_the_ai_reading_still_goes_to_it(self):
        from invoices.parsers import LLM_PARSER_KEY

        ai = Supplier.objects.get(parser_key=LLM_PARSER_KEY)
        with open(self.pdf(["FACTURE EXEMPLE", "Total 12,00"]), "rb") as handle:
            upload = SimpleUploadedFile("facture.pdf", handle.read(), content_type="application/pdf")
        with (
            mock.patch("invoices.views.parse_and_import", side_effect=RuntimeError("lecture IA essayée")) as read,
            self.assertLogs("invoices.views", "ERROR") as logged,
        ):
            response = self.client.post(reverse("invoices:invoice_upload"), {"source_file": upload, "supplier": ai.pk})
        read.assert_called_once()
        # Reached the AI reading (whose failure is said by kind, its words in
        # the server's log - security audit LB-3).
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Échec de l'import. Erreur inattendue sur le serveur : elle est notée pour l'administrateur."],
        )
        self.assertIn("lecture IA essayée", "\n".join(logged.output))
        self.assertFalse(Slip.objects.exists())


class WithoutPdfTests(GuardCase):
    def test_a_slip_pdf_prints_what_the_seeded_format_reads(self):
        """The fixture itself: the guard's tests prove nothing if the seeded
        patterns do not read this invented slip."""
        text = reading.pdf_text(tiny_pdf(slip_text_lines()))
        self.assertEqual(reading.detect_format(text, [seeded_format()]), seeded_format())
        self.assertTrue(reading.read_slip_text(text, seeded_format()).all_passed)
