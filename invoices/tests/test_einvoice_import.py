"""An electronic invoice, from the file to the screen.

`invoices/einvoice.py` reads an EN 16931 document exactly; this is about
what the rest of the application then does with it - and every assertion
here is about the seam, not about the reader:

- **the e-invoice check comes first.** A file carrying an EN 16931 XML is
  read from that XML whatever the supplier and whatever the file looks like:
  no OCR, no supplier parser, not even the PDF's text layer. A Factur-X
  invoice read by the ticket reader is a document whose exact figures were
  sitting inside it, thrown away and replaced by a guess;
- **a bare .xml is a document too.** The platform may forward the XML alone;
- **the supplier is the one the invoice names** - its stated SIREN, through
  `identifiers.py`. One nobody knows waits, exactly as a PDF of nobody known
  waits today;
- **it is not a ticket.** Nothing about it was read off a photograph, so it
  never joins the queue where receipts are re-typed - a document whose own
  totals disagree waits in « À corriger », where a supplier's arithmetic can
  be looked at;
- **it says what it is**, on the list, on its page and on the correction
  page, which must not pretend the figures came from a reading.

Nothing here touches a network or a real file of the owner's: the XML
fixtures are hand-written (`einvoice_files.py`) and the PDFs are built by
the test (`pdf_files.py`). Data invented.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from invoices import einvoice
from invoices.forms import ReceiptBatchUploadForm
from invoices.importing import DuplicateInvoiceError
from invoices.models import INVOICE_ATTACHMENT_PATTERN, Invoice, Supplier
from invoices.receipt_batches import STAGING_DIR, run_receipt_batch, stage_batch
from invoices.receipts import UnrecognisedShopError, import_document, pending_receipts
from invoices.scrapers.generic_email import _extract_attachments
from invoices.tests.einvoice_files import (
    CII_ALLOWANCE_AT_ITS_OWN_RATE,
    CII_CHARGE_AT_ITS_OWN_RATE,
    CII_CREDIT_NOTE,
    CII_DATE_FAR_FUTURE,
    CII_DATE_YEAR_ONE,
    CII_DISCOUNT_LINE,
    CII_DOCUMENT_CHARGE,
    CII_MINIMUM,
    CII_TOTALS_DISAGREE,
    CII_TWO_RATES,
    UBL_CHARGE_AT_ITS_OWN_RATE,
    UBL_TWO_RATES,
    XML_NOT_AN_INVOICE,
)
from invoices.tests.pdf_files import write_pdf, write_pdf_with_attachments
from invoices.workspace import DOCUMENT_TO_FIX, TICKET_TO_CHECK
from tests.factories import make_supplier

D = Decimal
SIREN = "siren:900000019"
# What a Factur-X PDF prints on its page: a perfectly readable invoice, which
# is exactly what must NOT be read once the XML is there.
PRINTED_PAGE = [
    "BRASSERIE DU CANAL",
    "FACTURE N FA-2026-0042 du 03/09/2026",
    "BIERE BLONDE FUT 30L  2  84,50  169,00",
    "SIROP CITRON 1L  6  4,20  25,20",
    "TOTAL TTC  229,39",
]


def _in(test, name: str) -> str:
    path = os.path.join(settings.MEDIA_ROOT, name)
    test.addCleanup(lambda: os.path.exists(path) and os.remove(path))
    return path


def write_xml(test, name: str, fixture: str) -> str:
    """A bare XML invoice on disk, as the platform or an e-mail delivers it."""
    path = _in(test, name)
    with open(path, "wb") as handle:
        handle.write(fixture.encode("utf-8"))
    return path


def write_factur_x(test, name: str, fixture: str = CII_TWO_RATES, lines=None) -> str:
    """A PDF/A-3 printing `lines` and carrying `fixture` as its attachment."""
    return write_pdf_with_attachments(
        _in(test, name),
        PRINTED_PAGE if lines is None else lines,
        [("factur-x.xml", fixture.encode("utf-8"), "Data")],
    )


def seller(**kwargs) -> Supplier:
    """The supplier the fixtures' SIREN names, as the application would know
    it: something it filed before taught it that company number."""
    return make_supplier(
        code="BRASSERIE", name="Brasserie du Canal", ticket_identifiers=[SIREN], **kwargs
    )


def labels(invoice) -> list[str]:
    return [check["label"] for check in invoice.parse_checks]


class TheXmlIsReadFirstTests(TestCase):
    """Before the text layer, before the supplier's own reader, before OCR."""

    def setUp(self):
        self.supplier = seller()

    def test_a_factur_x_pdf_is_read_from_its_xml_and_nothing_else_is_asked(self):
        path = write_factur_x(self, "facture-x.pdf")
        with mock.patch(
            "invoices.receipts.document_text", side_effect=AssertionError("le texte de la page a été lu")
        ), mock.patch("invoices.receipts.recognise", side_effect=AssertionError("l'OCR a tourné")):
            invoice = import_document(path, display_filename="facture-x.pdf")
        self.assertEqual(invoice.supplier, self.supplier)
        self.assertEqual(invoice.einvoice_format, "Factur-X")
        self.assertTrue(invoice.is_einvoice)

    def test_the_figures_are_the_documents_own_to_the_cent(self):
        path = write_factur_x(self, "facture-x.pdf")
        invoice = import_document(path, display_filename="facture-x.pdf")
        self.assertEqual(invoice.invoice_number, "FA-2026-0042")
        self.assertEqual(invoice.invoice_date, date(2026, 9, 3))
        self.assertEqual(
            [(line.raw_name, line.quantity, line.unit_cost_ht, line.total_ht, line.vat_rate)
             for line in invoice.lines.all()],
            [
                ("BIERE BLONDE FUT 30L", D("2.000"), D("84.5000"), D("169.00"), D("0.2000")),
                ("SIROP CITRON 1L", D("6.000"), D("4.2000"), D("25.20"), D("0.0550")),
            ],
        )
        self.assertEqual(invoice.total_ht, D("194.20"))
        # The invoice STATES its total (BT-112). Worked out from the lines it
        # is 229.386, and a document filed a fraction of a cent away from what
        # the bank will pay is the drift this reading exists to remove.
        self.assertEqual(invoice.total_ttc, D("229.39"))
        self.assertEqual(invoice.printed_total_ttc, D("229.39"))

    def test_the_vat_breakdown_the_invoice_states_is_stored(self):
        invoice = import_document(write_factur_x(self, "facture-x.pdf"))
        # Stored as the fraction this database holds, to four decimals: the
        # XML says 20.00 and 5.50, and read across every cost downstream
        # would be a hundredfold wrong.
        self.assertEqual(
            invoice.vat_breakdown, [["0.2000", "169.00", "33.80"], ["0.0550", "25.20", "1.39"]]
        )

    def test_nothing_says_this_was_a_reading(self):
        """`ocr_text` is what makes a document a receipt and `ocr_confidence`
        is a recogniser's doubt: an exact figure must claim neither."""
        invoice = import_document(write_factur_x(self, "facture-x.pdf"))
        self.assertEqual(invoice.ocr_text, "")
        self.assertIsNone(invoice.ocr_confidence)
        self.assertFalse(invoice.is_receipt)
        self.assertIn("Facture électronique", invoice.source_text)

    def test_an_ordinary_pdf_is_read_as_before(self):
        """The check costs a look at the file's attachments and changes
        nothing for the PDFs already filed, none of which carries one."""
        make_supplier(code="CUISIPRO", name="Cuisipro", ticket_header="CUISIPRO FRANCE")
        path = write_pdf(_in(self, "ordinaire.pdf"), [
            "CUISIPRO FRANCE SARL",
            "FACTURE N 7654321 du 07/11/2024",
            "Verre a shot  8  3,50  28,00",
            "TOTAL TTC  EURO  33,60",
        ])
        invoice = import_document(path, display_filename="ordinaire.pdf")
        self.assertEqual(invoice.einvoice_format, "")
        self.assertFalse(invoice.is_einvoice)
        self.assertTrue(invoice.is_receipt)


class ABareXmlIsADocumentTooTests(TestCase):
    """The platform may forward the XML alone, with no PDF around it."""

    def setUp(self):
        self.supplier = seller()

    def test_an_xml_file_imports_like_any_other_document(self):
        path = write_xml(self, "facture.xml", CII_TWO_RATES)
        invoice = import_document(path, display_filename="facture.xml")
        self.assertEqual(invoice.supplier, self.supplier)
        # CII on its own is not Factur-X: Factur-X is the PDF around it, and
        # naming one the other would be a claim about a file that never came.
        self.assertEqual(invoice.einvoice_format, "CII")
        self.assertEqual(invoice.lines.count(), 2)

    def test_a_ubl_invoice_reads_as_the_same_document(self):
        path = write_xml(self, "facture-ubl.xml", UBL_TWO_RATES)
        invoice = import_document(path, display_filename="facture-ubl.xml")
        self.assertEqual(invoice.einvoice_format, "UBL")
        self.assertEqual(invoice.total_ttc, D("229.39"))
        self.assertIn("Facture électronique (UBL)", labels(invoice))

    def test_the_file_kept_is_the_one_received(self):
        """It is the legal invoice: what is filed is the file itself, not a
        rendering of it."""
        path = write_xml(self, "facture.xml", CII_TWO_RATES)
        invoice = import_document(path, display_filename="facture.xml")
        self.assertTrue(invoice.source_file.name.endswith(".xml"))
        with invoice.source_file.open("rb") as stored:
            self.assertEqual(stored.read(), CII_TWO_RATES.encode("utf-8"))

    def test_the_folder_import_accepts_it(self):
        """A folder of invoices downloaded from the platform holds XML files,
        and a selection listing them as « ignorés » loses them silently."""
        form = ReceiptBatchUploadForm(
            files={"files": [SimpleUploadedFile("facture.xml", CII_TWO_RATES.encode("utf-8"))]}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([upload.name for upload in form.cleaned_data["files"]], ["facture.xml"])
        self.assertEqual(form.ignored_names, [])

    def test_well_formed_xml_that_is_not_an_invoice_is_refused_in_french(self):
        """Never OCR'd (there is no page to photograph) and never silently
        dropped: the import says what it is, and the folder carries on."""
        path = write_xml(self, "commande.xml", XML_NOT_AN_INVOICE)
        with self.assertRaises(einvoice.EInvoiceError) as refused:
            import_document(path, display_filename="commande.xml")
        self.assertIn("EN 16931", str(refused.exception))
        self.assertIsInstance(refused.exception, ValueError)
        self.assertFalse(Invoice.objects.exists())


class ThroughTheFolderImportTests(TestCase):
    """The background import, file by file: one bad file never stops the
    others, and each one ends in exactly one state."""

    def setUp(self):
        self.supplier = seller()

    def _run(self, *uploads):
        batch = stage_batch(list(uploads))
        self.addCleanup(
            shutil.rmtree, os.path.join(settings.MEDIA_ROOT, STAGING_DIR, str(batch.pk)), True
        )
        return run_receipt_batch(batch.pk)

    def test_an_xml_and_a_factur_x_pdf_go_in_side_by_side(self):
        batch = self._run(
            SimpleUploadedFile("facture.xml", CII_TWO_RATES.encode("utf-8")),
            SimpleUploadedFile(
                "droits.pdf",
                open(write_factur_x(self, "staged.pdf", CII_DOCUMENT_CHARGE), "rb").read(),
            ),
        )
        self.assertEqual([entry["status"] for entry in batch.results], ["ok", "ok"])
        # Not a receipt: the import links each one to its own lines, not to
        # the screen where a photograph is re-typed.
        self.assertEqual([entry["receipt"] for entry in batch.results], [False, False])
        self.assertEqual(batch.imported_count, 2)

    def test_an_xml_that_is_not_an_invoice_is_one_files_error(self):
        batch = self._run(
            SimpleUploadedFile("commande.xml", XML_NOT_AN_INVOICE.encode("utf-8")),
            SimpleUploadedFile("facture.xml", CII_TWO_RATES.encode("utf-8")),
        )
        self.assertEqual([entry["status"] for entry in batch.results], ["error", "ok"])
        self.assertIn("EN 16931", batch.results[0]["message"])
        self.assertEqual(Invoice.objects.count(), 1)


class FromTheMailboxTests(SimpleTestCase):
    """What a source takes out of an e-mail. Nothing here opens a mailbox."""

    def attached(self, name: str, pattern: str = INVOICE_ATTACHMENT_PATTERN) -> list[str]:
        message = EmailMessage()
        message["Subject"] = "Votre facture"
        message.set_content("Bonjour")
        message.add_attachment(b"<x/>", maintype="application", subtype="xml", filename=name)
        return [part.filename for part in _extract_attachments(message, re.compile(pattern))]

    def test_an_invoice_forwarded_as_xml_is_taken(self):
        """A source set up when every invoice was a PDF would leave its
        supplier's electronic invoices in the mailbox without a word."""
        self.assertEqual(self.attached("facture.xml"), ["facture.xml"])
        self.assertEqual(self.attached("facture.pdf"), ["facture.pdf"])

    def test_a_pattern_someone_tuned_by_hand_still_decides(self):
        self.assertEqual(self.attached("facture.xml", r"(?i)^releve.*\.pdf$"), [])


class WhichFileCarriesOneTests(SimpleTestCase):
    """`einvoice.document_xml` answers for both shapes, and refuses what it
    will not read into memory. No database."""

    def test_the_xml_of_each_shape(self):
        pdf = write_factur_x(self, "porteur.pdf")
        self.assertEqual(einvoice.document_xml(pdf), CII_TWO_RATES.encode("utf-8"))
        xml = write_xml(self, "seule.xml", CII_TWO_RATES)
        self.assertEqual(einvoice.document_xml(xml), CII_TWO_RATES.encode("utf-8"))

    def test_a_file_carrying_none(self):
        plain = write_pdf(_in(self, "ordinaire.pdf"), ["FACTURE", "TOTAL TTC 10,00"])
        self.assertIsNone(einvoice.document_xml(plain))
        photo = _in(self, "photo.jpg")
        with open(photo, "wb") as handle:
            handle.write(b"\xff\xd8\xff a photo")
        self.assertIsNone(einvoice.document_xml(photo))

    def test_an_xml_too_big_to_read_is_refused_not_truncated(self):
        """Cut to the cap it could parse as a smaller, well-formed invoice -
        which is money nobody typed."""
        path = _in(self, "enorme.xml")
        with open(path, "wb") as handle:
            handle.write(b"<x>" + b" " * (einvoice.MAX_XML_BYTES + 10) + b"</x>")
        with self.assertRaises(einvoice.EInvoiceError) as refused:
            einvoice.document_xml(path)
        self.assertIn("trop volumineux", str(refused.exception))


class WhoseInvoiceItIsTests(TestCase):
    """The seller's own company number, stated as data."""

    def test_the_supplier_is_the_one_the_invoice_names(self):
        supplier = seller()
        make_supplier(code="AUTRE", name="Autre fournisseur", ticket_header="BRASSERIE DU CANAL")
        path = write_factur_x(self, "facture-x.pdf")
        invoice = import_document(path, display_filename="facture-x.pdf")
        # The SIREN is stated by the invoice; a header is a guess about a
        # printed page, and two suppliers can print the same words.
        self.assertEqual(invoice.supplier, supplier)

    def test_a_siren_nobody_knows_waits_and_is_not_guessed_at(self):
        make_supplier(code="AUTRE", name="Autre fournisseur")
        path = write_factur_x(self, "facture-x.pdf")
        with self.assertRaises(UnrecognisedShopError) as waiting:
            import_document(path, display_filename="facture-x.pdf")
        self.assertIn("Brasserie du Canal", str(waiting.exception))
        self.assertFalse(Invoice.objects.exists())

    def test_named_by_hand_it_files_and_teaches_the_supplier_its_siren(self):
        """What an electronic invoice states about its seller is worth at
        least as much as the text of a PDF, which teaches at import."""
        supplier = make_supplier(code="BRASSERIE", name="Brasserie du Canal")
        path = write_factur_x(self, "facture-x.pdf")
        invoice = import_document(path, display_filename="facture-x.pdf", supplier=supplier)
        supplier.refresh_from_db()
        self.assertEqual(invoice.supplier, supplier)
        self.assertIn(SIREN, supplier.ticket_identifiers)

    def test_the_same_file_twice_is_refused_before_it_is_opened(self):
        seller()
        path = write_factur_x(self, "facture-x.pdf")
        import_document(path, display_filename="facture-x.pdf")
        with self.assertRaisesMessage(DuplicateInvoiceError, "Fichier déjà importé"):
            import_document(path, display_filename="facture-x.pdf")
        self.assertEqual(Invoice.objects.count(), 1)


class WhatItIsWorthTests(TestCase):
    """Credit notes, document-level charges, and a profile with no lines."""

    def setUp(self):
        self.supplier = seller()

    def test_a_credit_note_is_money_going_the_other_way(self):
        path = write_xml(self, "avoir.xml", CII_CREDIT_NOTE)
        invoice = import_document(path, display_filename="avoir.xml")
        line, = invoice.lines.all()
        # A negative count AND a negative amount, this codebase's own
        # convention for a return: read at face value an avoir would double
        # the purchase it cancels.
        self.assertEqual(line.quantity, D("-1.000"))
        self.assertEqual(line.total_ht, D("-84.50"))
        self.assertEqual(line.unit_cost_ht, D("84.5000"))
        self.assertEqual(invoice.total_ttc, D("-101.40"))

    def test_a_document_level_charge_lands_in_the_adjustment(self):
        """Duty the lines do not carry, and which the VAT base counts."""
        path = write_xml(self, "droits.xml", CII_DOCUMENT_CHARGE)
        invoice = import_document(path, display_filename="droits.xml")
        self.assertEqual(invoice.reconciliation_adjustment, D("12.00"))
        self.assertEqual(invoice.lines_total_ht, D("100.00"))
        self.assertEqual(invoice.total_ht, D("112.00"))
        self.assertEqual(invoice.total_ttc, D("134.40"))

    def test_a_profile_with_no_lines_is_filed_on_its_total_and_says_so(self):
        """MINIMUM states the totals and the VAT breakdown only. Filed as it
        stands, that is an invoice worth nothing at all; and it must never
        read like a document whose lines could not be read."""
        path = write_xml(self, "minimum.xml", CII_MINIMUM)
        invoice = import_document(path, display_filename="minimum.xml")
        line, = invoice.lines.all()
        self.assertEqual((line.total_ht, line.vat_rate), (D("120.00"), D("0.2000")))
        self.assertEqual(invoice.total_ttc, D("144.00"))
        said = [check for check in invoice.parse_checks if check["label"] == einvoice.NO_LINES_CHECK]
        self.assertEqual(len(said), 1, invoice.parse_checks)
        self.assertTrue(said[0]["passed"])
        self.assertIn("minimum", said[0]["detail"])

    def test_a_supplier_of_charges_is_filed_as_a_charge(self):
        """A subscription is a charge whichever format it arrives in: one
        line per VAT rate, on a poste, and its own checks - which every path
        that touches a charge rewrites, and which would drop an electronic
        invoice's the next time one ran."""
        Supplier.objects.filter(pk=self.supplier.pk).update(expenses_only=True)
        invoice = import_document(write_xml(self, "abonnement.xml", CII_MINIMUM))
        self.assertEqual(invoice.einvoice_format, "CII")
        self.assertEqual(labels(invoice), ["Total de la charge"])
        self.assertEqual(invoice.review_state["label"], "Charge")
        line, = invoice.lines.all()
        self.assertTrue(line.product.is_expense)
        self.assertEqual(invoice.total_ttc, D("144.00"))

    def test_a_credit_note_with_no_lines_is_still_a_return(self):
        """Its one rebuilt line comes from a negative VAT table: at a count
        of 1 a negative amount would book stock at a negative unit cost."""
        fixture = CII_MINIMUM.replace("<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>381</ram:TypeCode>")
        invoice = import_document(write_xml(self, "avoir-minimum.xml", fixture))
        line, = invoice.lines.all()
        self.assertEqual((line.quantity, line.total_ht), (D("-1.000"), D("-120.00")))
        self.assertEqual(line.unit_cost_ht, D("120.0000"))


class NotATicketTests(TestCase):
    """It carries checks, and none of them is about a reading."""

    def setUp(self):
        self.supplier = seller()

    def test_it_says_what_it_is_and_nothing_about_ocr(self):
        invoice = import_document(write_factur_x(self, "facture-x.pdf"))
        self.assertEqual(labels(invoice)[0], "Facture électronique (Factur-X)")
        self.assertTrue(invoice.parse_checks[0]["passed"])
        self.assertTrue(all(check["passed"] for check in invoice.parse_checks), invoice.parse_checks)
        for label in labels(invoice):
            self.assertNotIn("ticket", label.lower())

    def test_it_never_joins_the_queue_where_receipts_are_re_typed(self):
        invoice = import_document(write_factur_x(self, "facture-x.pdf"))
        self.assertFalse(pending_receipts().filter(pk=invoice.pk).exists())
        self.assertFalse(Invoice.objects.filter(TICKET_TO_CHECK, pk=invoice.pk).exists())
        self.assertFalse(invoice.waiting_check)

    def test_an_invoice_whose_own_totals_disagree_waits_in_a_corriger(self):
        """The supplier's arithmetic, reported with both figures and never
        repaired - and in the one list where a document nobody can re-type
        is seen."""
        invoice = import_document(write_xml(self, "faux-total.xml", CII_TOTALS_DISAGREE))
        self.assertTrue(Invoice.objects.filter(DOCUMENT_TO_FIX, pk=invoice.pk).exists())
        self.assertFalse(Invoice.objects.filter(TICKET_TO_CHECK, pk=invoice.pk).exists())
        self.assertIn("239.39", invoice.error_message)
        self.assertEqual(invoice.review_state["label"], "À corriger")

    def test_one_that_adds_up_is_in_no_list_of_things_to_fix(self):
        invoice = import_document(write_factur_x(self, "facture-x.pdf"))
        self.assertFalse(Invoice.objects.filter(DOCUMENT_TO_FIX, pk=invoice.pk).exists())
        self.assertEqual(invoice.error_message, "")


class OnTheScreenTests(TestCase):
    """Visible as what it is, everywhere it is listed."""

    def setUp(self):
        self.supplier = seller()
        self.invoice = import_document(write_factur_x(self, "facture-x.pdf"), display_filename="facture-x.pdf")

    def test_the_achats_list_calls_it_a_facture_electronique(self):
        page = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(page, "Facture électronique")
        self.assertNotContains(page, ">Ticket<")

    def test_its_own_page_says_so(self):
        page = self.client.get(reverse("invoices:invoice_detail", args=[self.invoice.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Facture électronique")

    def test_the_correction_page_does_not_pretend_it_was_read(self):
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Facture électronique (Factur-X)")
        self.assertContains(page, "données de la facture")
        self.assertNotContains(page, "Corrigez ce que le parseur a mal lu")

    def test_a_ticket_is_sent_to_its_review_screen_and_this_one_is_not(self):
        """`lignes/` is the invoice's own correction page, not a redirect to
        the queue a receipt is checked in."""
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertRedirects(
            self.client.get(reverse("invoices:receipt_review", args=[self.invoice.pk])),
            reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]),
        )

    def test_its_lines_stay_editable(self):
        """A person may still disagree with a supplier: the page is the same
        one, with the same rows."""
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        self.assertContains(page, "BIERE BLONDE FUT 30L")
        self.assertContains(page, 'id="document-lines"')

    def test_the_a_verifier_tab_lists_one_whose_totals_disagree(self):
        # Another number: this supplier's FA-2026-0042 went in at setUp, and
        # two documents of one number are the same document.
        fixture = CII_TOTALS_DISAGREE.replace("FA-2026-0042", "FA-2026-0043")
        invoice = import_document(write_xml(self, "faux-total.xml", fixture))
        page = self.client.get(reverse("invoices:receipt_queue"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Documents à corriger")
        self.assertContains(page, reverse("invoices:invoice_edit_lines", args=[invoice.pk]))

    def test_an_xml_has_no_page_to_frame_so_it_shows_what_it_states(self):
        """There is no photo and no PDF to put beside the lines: without the
        document's own figures on the page there would be nothing to check
        them against - and an <img> pointed at an XML file is a broken
        image."""
        invoice = import_document(
            write_xml(self, "seule.xml", CII_TWO_RATES.replace("FA-2026-0042", "FA-2026-0044"))
        )
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Ouvrir le fichier reçu")
        self.assertContains(page, "Total TTC 229.39 €")
        self.assertNotContains(page, "document-frame")

    def test_reading_it_again_reads_the_xml_and_not_a_parser(self):
        """The button is offered - the file says everything again, exactly -
        and what it reads again is the invoice's own data. Handed to its
        supplier's reader, an XML file would be opened as a PDF."""
        self.invoice.lines.update(total_ht=D("1.00"))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        self.assertContains(page, 'value="reread"')
        with mock.patch("invoices.receipts.recognise", side_effect=AssertionError("l'OCR a tourné")):
            self.client.post(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]), {"action": "reread"})
        self.invoice.refresh_from_db()
        self.assertEqual(
            [line.total_ht for line in self.invoice.lines.all()], [D("169.00"), D("25.20")]
        )

    def test_the_live_check_counts_the_duty_the_lines_do_not_carry(self):
        """A document-level charge is part of the total the invoice states:
        left out of the sum, an invoice that balances read as failing."""
        invoice = import_document(write_xml(self, "droits.xml", CII_DOCUMENT_CHARGE))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, 'class="check check-pass" id="live-lines-check"')


class TheChargesOwnRateTests(TestCase):
    """BG-20/BG-21 carry their own VAT rate, and the invoice is filed on it.

    Duty on alcohol is 20 % on an invoice whose soft drinks are at 5,5 %.
    Deduced from the lines - which is all a supplier's PDF ever offers - the
    charge is taxed at the lines' rate and the invoice is filed BELOW what
    the bank will debit, with every check green: the checks work on the
    figures the document states, and those all balance.
    """

    def setUp(self):
        self.supplier = seller()

    def test_the_stated_rate_is_what_the_invoice_is_filed_on(self):
        invoice = import_document(write_xml(self, "droits.xml", CII_CHARGE_AT_ITS_OWN_RATE))
        # 1 000,00 at 5,5 % + 100,00 of duty at 20 % = BT-112 1 175,00.
        self.assertEqual(invoice.printed_total_ttc, D("1175.00"))
        self.assertEqual(invoice.adjustment_vat_rate, D("0.2000"))
        self.assertEqual(invoice.adjustment_ttc.quantize(D("0.01")), D("120.00"))
        self.assertEqual(invoice.total_ttc.quantize(D("0.01")), D("1175.00"))

    def test_nothing_is_reported_because_nothing_is_wrong(self):
        invoice = import_document(write_xml(self, "droits.xml", CII_CHARGE_AT_ITS_OWN_RATE))
        self.assertEqual(invoice.error_message, "")
        self.assertEqual([c["label"] for c in invoice.parse_checks if not c["passed"]], [])

    def test_the_bank_looks_for_what_the_invoice_states(self):
        """The failure that made this a finding: 1 160,50 € filed against a
        1 175,00 € debit is an invoice the bank match can never find."""
        from bank.reconcile import rounded_total

        invoice = import_document(write_xml(self, "droits.xml", CII_CHARGE_AT_ITS_OWN_RATE))
        self.assertEqual(rounded_total(invoice), D("1175.00"))

    def test_an_allowance_at_its_own_rate_goes_the_other_way(self):
        """A year-end discount at 20 % off lines at 5,5 %: deduced, the
        invoice was filed ABOVE what is due."""
        invoice = import_document(write_xml(self, "remise.xml", CII_ALLOWANCE_AT_ITS_OWN_RATE))
        self.assertEqual(invoice.printed_total_ttc, D("935.00"))
        self.assertEqual(invoice.total_ttc.quantize(D("0.01")), D("935.00"))

    def test_ubl_states_it_too(self):
        invoice = import_document(write_xml(self, "droits.xml", UBL_CHARGE_AT_ITS_OWN_RATE))
        self.assertEqual(invoice.adjustment_vat_rate, D("0.2000"))
        self.assertEqual(invoice.total_ttc.quantize(D("0.01")), D("119.90"))

    def test_a_document_stating_no_rate_still_deduces_one(self):
        """None means « nothing stated it », and then Invoice.adjustment_ttc
        goes on working the rate out from the lines - which is what UBA's
        PDF and every till receipt need."""
        invoice = import_document(write_xml(self, "simple.xml", CII_TWO_RATES))
        self.assertIsNone(invoice.adjustment_vat_rate)

    def test_the_page_says_the_charge_and_the_lines_apart(self):
        """The live check counted the adjustment once and announced it
        twice: « lignes 134,40 € + 14,40 € de frais / ticket 134,40 € »."""
        invoice = import_document(write_xml(self, "droits.xml", CII_DOCUMENT_CHARGE))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        said = page.content.decode()
        self.assertIn("lignes 120.00 € + 14.40 € de frais facturés globalement = 134.40 €", said)
        self.assertNotIn("lignes 134.40 € + 14.40", said)

    def test_nothing_on_an_electronic_invoice_is_called_a_ticket(self):
        """Nothing was printed and there is no ticket - and this was the
        FIRST line of the page, above « Facture électronique »."""
        invoice = import_document(write_xml(self, "simple.xml", CII_TWO_RATES))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        said = page.content.decode()
        self.assertIn("/ total de la facture 229.39 €", said)
        self.assertNotIn("/ ticket", said)


class ALineTheSupplierSignedItsOwnWayTests(TestCase):
    """A count of 1 at a negative amount books stock BELOW nothing.

    `LineCorrectionForm` refuses exactly that shape for a supplier of goods -
    « Un retour a une quantité et un montant négatifs » - and the importer
    was filing a row the application's own form would reject, whenever the
    document stated the line itself rather than the reader rebuilding it.
    """

    def setUp(self):
        self.supplier = seller()

    def test_a_discount_line_becomes_a_return(self):
        invoice = import_document(write_xml(self, "remise.xml", CII_DISCOUNT_LINE))
        rows = [(line.raw_name, line.quantity, line.unit_cost_ht, line.total_ht)
                for line in invoice.lines.order_by("raw_name")]
        self.assertEqual(
            rows,
            [("BIERE BLONDE FUT 30L", D("2.000"), D("84.5000"), D("169.00")),
             ("REMISE COMMERCIALE", D("-1.000"), D("60.0000"), D("-60.00"))],
        )

    def test_no_line_is_a_positive_count_at_a_negative_amount(self):
        invoice = import_document(write_xml(self, "remise.xml", CII_DISCOUNT_LINE))
        self.assertEqual(
            [line.raw_name for line in invoice.lines.filter(quantity__gt=0, total_ht__lt=0)], []
        )

    def test_the_invoices_total_is_untouched(self):
        """The sign is the stock's business; the money must not move."""
        invoice = import_document(write_xml(self, "remise.xml", CII_DISCOUNT_LINE))
        self.assertEqual(invoice.printed_total_ttc, D("130.80"))
        self.assertEqual(invoice.total_ttc.quantize(D("0.01")), D("130.80"))
        self.assertEqual(invoice.error_message, "")

    def test_the_correction_form_would_accept_what_the_import_filed(self):
        """The contract behind this: the importer must not file a row the
        page refuses. Read back through the form the page builds."""
        from invoices.forms import LineCorrectionForm

        invoice = import_document(write_xml(self, "remise.xml", CII_DISCOUNT_LINE))
        for line in invoice.lines.all():
            form = LineCorrectionForm(
                {
                    "line_id": line.pk, "product_name": line.raw_name, "quantity": line.quantity,
                    "total_volume": line.total_volume, "total_ht": line.total_ht,
                    "vat_rate": D("0.20"), "amount_source": "ht",
                },
                charge=False,
            )
            self.assertTrue(form.is_valid(), form.errors)

    def test_a_charge_supplier_keeps_the_count_of_one(self):
        """A credit on a charge IS a count of 1 at a negative amount, and
        that convention must not be turned into a return."""
        self.supplier.expenses_only = True
        self.supplier.save(update_fields=["expenses_only"])
        invoice = import_document(write_xml(self, "remise.xml", CII_DISCOUNT_LINE))
        self.assertFalse(invoice.lines.filter(quantity__lt=0).exists())


class ADateNoInvoiceWasIssuedOnTests(TestCase):
    """Outside 2000-today, an invoice is in no window, no valuation, no bank
    match and no margin - and, said nowhere, in no queue either."""

    def setUp(self):
        self.supplier = seller()

    def test_year_one_is_said_on_the_document(self):
        invoice = import_document(write_xml(self, "vieille.xml", CII_DATE_YEAR_ONE))
        self.assertIn("Date invraisemblable", invoice.error_message)
        self.assertIn("01/01/0001", invoice.error_message)

    def test_a_far_future_date_is_said_too(self):
        invoice = import_document(write_xml(self, "future.xml", CII_DATE_FAR_FUTURE))
        self.assertIn("Date invraisemblable", invoice.error_message)

    def test_it_waits_in_the_documents_to_fix_and_not_in_the_queue(self):
        invoice = import_document(write_xml(self, "vieille.xml", CII_DATE_YEAR_ONE))
        self.assertTrue(Invoice.objects.filter(DOCUMENT_TO_FIX, pk=invoice.pk).exists())
        self.assertFalse(Invoice.objects.filter(TICKET_TO_CHECK, pk=invoice.pk).exists())
        self.assertEqual(invoice.review_state["label"], "À corriger")

    def test_the_file_is_still_filed(self):
        """Not a refusal: the invoice is real and its figures are exact. It
        is the date that has to be typed in."""
        invoice = import_document(write_xml(self, "vieille.xml", CII_DATE_YEAR_ONE))
        self.assertEqual(invoice.lines.count(), 2)
        self.assertEqual(invoice.printed_total_ttc, D("229.39"))

    def test_an_ordinary_date_says_nothing(self):
        invoice = import_document(write_xml(self, "simple.xml", CII_TWO_RATES))
        self.assertEqual(invoice.error_message, "")

    def test_the_correction_page_repeats_why_it_is_here(self):
        """Arrived from « Documents à corriger », the page said nothing: on
        a document with no usable date every check is green."""
        invoice = import_document(write_xml(self, "vieille.xml", CII_DATE_YEAR_ONE))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, "Date invraisemblable")


class WhatTheScreensCallItTests(TestCase):
    """« Avoir », « reconstituée », and no vocabulary of reading anywhere."""

    def setUp(self):
        self.supplier = seller()

    def test_a_credit_note_is_called_an_avoir_on_its_page(self):
        invoice = import_document(write_factur_x(self, "avoir.pdf", CII_CREDIT_NOTE))
        page = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(page, "Avoir électronique")

    def test_a_credit_note_is_called_an_avoir_on_the_correction_page(self):
        invoice = import_document(write_factur_x(self, "avoir.pdf", CII_CREDIT_NOTE))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        # The lead paragraph above the lines, which said « la facture ».
        self.assertContains(page, "de l'avoir")

    def test_an_ordinary_invoice_is_not(self):
        invoice = import_document(write_factur_x(self, "facture-x.pdf"))
        page = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertNotContains(page, "Avoir électronique")

    def test_the_declared_document_shows_the_credit_notes_totals_negative(self):
        """The panel beside the lines is the only evidence the owner has to
        check them against; its totals were positive beside negative lines."""
        invoice = import_document(write_xml(self, "avoir.xml", CII_CREDIT_NOTE))
        self.assertIn("Total TTC -101.40", invoice.source_text)
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, "Total TTC -101.40")

    def test_a_profile_with_no_lines_says_its_line_was_rebuilt(self):
        """The lead paragraph called it the supplier's own declaration, four
        lines above a check saying the invoice carries none."""
        invoice = import_document(write_xml(self, "minimum.xml", CII_MINIMUM))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, "ne détaille aucune ligne")
        self.assertContains(page, "reconstituée depuis sa table de TVA")

    def test_its_document_page_says_so_too(self):
        invoice = import_document(write_xml(self, "minimum.xml", CII_MINIMUM))
        page = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(page, "pas un article")

    def test_an_ordinary_invoice_says_its_lines_are_the_documents_data(self):
        invoice = import_document(write_xml(self, "simple.xml", CII_TWO_RATES))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, "données de la facture")
        self.assertNotContains(page, "ne détaille aucune ligne")

    def test_the_document_page_shows_the_state_every_list_shows(self):
        """The list said « Produits à classer » and the page « À vérifier »,
        on the same invoice - and an electronic invoice is never that."""
        invoice = import_document(write_factur_x(self, "facture-x.pdf"))
        page = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(page, invoice.review_state["label"])
        self.assertNotContains(page, ">À vérifier<")

    def test_the_header_does_not_promise_exact_figures_it_has_broken(self):
        """Where BT-112 disagrees with the lines, the page shows the lines -
        a defensible choice, and « montants issus des données de la
        facture » over it is a claim the page has just broken."""
        invoice = import_document(write_xml(self, "faux.xml", CII_TOTALS_DISAGREE))
        page = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(page, "qui ne s'accordent pas entre eux")
        self.assertNotContains(page, "montants issus des données de la facture")

    def test_the_vat_table_is_not_something_to_copy_out_by_hand(self):
        invoice = import_document(write_xml(self, "simple.xml", CII_TWO_RATES))
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, "rien n'est à recopier")
        self.assertNotContains(page, "Recopiez la table de TVA")

    def test_the_documents_to_fix_list_names_the_supplier_s_arithmetic(self):
        import_document(write_xml(self, "faux.xml", CII_TOTALS_DISAGREE))
        page = self.client.get(reverse("invoices:receipt_queue"))
        self.assertContains(page, "totaux du fournisseur")

    def test_the_sources_tab_says_the_platform_is_not_connected(self):
        """What the owner asked for and did not get: the app never said that
        the invoices still have to be fetched, from the accountant now."""
        page = self.client.get(reverse("invoices:invoice_type_list"))
        self.assertContains(page, "plateforme de facturation électronique n'est pas connectée")
        self.assertContains(page, "plateforme agréée")
