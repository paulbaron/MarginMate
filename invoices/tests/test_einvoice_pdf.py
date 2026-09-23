"""Getting the XML back out of a Factur-X PDF.

A Factur-X invoice looks like an ordinary PDF and is one: the exact figures
sit in an XML file attached to it, in the catalog's `/Names /EmbeddedFiles`
tree. Read by the ticket reader instead, such a document has its own numbers
thrown away and replaced by an OCR's guess at what the page printed - which
is the single biggest accuracy loss available to this codebase.

So this is the check that runs BEFORE the ones in `receipts.import_document`,
and it has to survive everything a file arriving from a mailbox, a portal or
a platform can be: damaged, truncated, carrying attachments that are not
invoices, carrying several, or carrying none.

Every PDF here is built by the test (`pdf_files.write_pdf_with_attachments`).
There is no Factur-X file in this project to copy, and a real one would carry
a supplier's IBAN into a public repository.
"""

import os
import tempfile
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from invoices import einvoice
from invoices.tests.einvoice_files import CII_TWO_RATES, XML_NOT_AN_INVOICE
from invoices.tests.pdf_files import (
    truncate_file,
    write_pdf,
    write_pdf_with_attachments,
)

D = Decimal
XML = CII_TWO_RATES.encode("utf-8")
# Anything a sender bolts on beside the invoice: a logo, a delivery note, a
# spreadsheet of the month's orders.
LOGO = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class EmbeddedXmlTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def build(self, attachments, **kwargs):
        path = os.path.join(self.folder.name, "facture.pdf")
        return write_pdf_with_attachments(path, ["Facture", "Total 229,39 EUR"], attachments, **kwargs)

    def test_the_invoice_xml_comes_back_byte_for_byte(self):
        path = self.build([("factur-x.xml", XML, "Data")])
        self.assertEqual(einvoice.embedded_xml(path), XML)

    def test_an_uncompressed_attachment_is_read_too(self):
        """Every producer deflates, but the filter is optional in the
        format and a file without one must not read as "no invoice"."""
        path = self.build([("factur-x.xml", XML, "Data")], compress=False)
        self.assertEqual(einvoice.embedded_xml(path), XML)

    def test_several_attachments_and_the_invoice_is_not_the_first(self):
        path = self.build(
            [("logo.png", LOGO, "Supplement"), ("factur-x.xml", XML, "Data"), ("bl.txt", b"BL 12", "Supplement")]
        )
        self.assertEqual(einvoice.embedded_xml(path), XML)

    def test_a_name_tree_nested_deeper_than_one_level(self):
        """`/Names` may be split into `/Kids`, and is as soon as a file
        carries a few attachments. Read one level deep, the invoice of a
        multi-attachment PDF is simply not there."""
        path = self.build(
            [("logo.png", LOGO, "Supplement"), ("factur-x.xml", XML, "Data")], nested_names=True
        )
        self.assertEqual(einvoice.embedded_xml(path), XML)

    def test_declared_through_af_alone(self):
        path = self.build([("factur-x.xml", XML, "Data")], via_af_only=True)
        self.assertEqual(einvoice.embedded_xml(path), XML)

    def test_the_invoice_is_found_whatever_the_attachment_is_called(self):
        """ZUGFeRD says zugferd-invoice.xml, XRechnung says xrechnung.xml,
        and a sender is free to say anything at all. What decides is the
        root element of the file, never its name."""
        for name in ("zugferd-invoice.xml", "xrechnung.xml", "order-x.xml", "piece-jointe-3.dat"):
            with self.subTest(name=name):
                path = self.build([(name, XML, "Data")])
                self.assertEqual(einvoice.embedded_xml(path), XML)

    def test_an_attachment_that_is_not_an_invoice_is_not_one(self):
        path = self.build([("logo.png", LOGO, "Supplement"), ("commande.xml", XML_NOT_AN_INVOICE.encode(), "Data")])
        self.assertIsNone(einvoice.embedded_xml(path))

    def test_a_pdf_with_no_attachment_at_all(self):
        path = os.path.join(self.folder.name, "ordinaire.pdf")
        write_pdf(path, ["Facture", "Total 229,39 EUR"])
        self.assertIsNone(einvoice.embedded_xml(path))

    def test_a_truncated_pdf_is_not_a_traceback(self):
        """A download cut off part way. Every file in a folder import goes
        through here, so one broken file must not stop the folder."""
        path = self.build([("factur-x.xml", XML, "Data")])
        truncate_file(path, 300)
        self.assertIsNone(einvoice.embedded_xml(path))

    def test_a_file_that_is_not_a_pdf(self):
        path = os.path.join(self.folder.name, "photo.jpg")
        with open(path, "wb") as handle:
            handle.write(LOGO)
        self.assertIsNone(einvoice.embedded_xml(path))

    def test_a_file_that_is_not_there(self):
        self.assertIsNone(einvoice.embedded_xml(os.path.join(self.folder.name, "absent.pdf")))

    def test_an_attachment_that_would_not_fit_in_memory_is_refused_not_read(self):
        """A few kilobytes of deflate can decompress to gigabytes. Nothing
        is decompressed past the cap - the refusal is a message on the
        import, never a machine that stops answering."""
        with mock.patch.object(einvoice, "MAX_XML_BYTES", 64 * 1024):
            path = self.build([("factur-x.xml", b"<" + b"x" * 200_000, "Data")])
            with self.assertRaises(einvoice.EInvoiceError) as caught:
                einvoice.embedded_xml(path)
        self.assertIn("trop volumineuse", str(caught.exception).lower())
        self.assertIn("factur-x.xml", str(caught.exception))

    def test_an_oversized_attachment_beside_a_readable_invoice_costs_nothing(self):
        """A 30 MB delivery photo attached next to a 40 KB invoice is not a
        reason to refuse the invoice."""
        with mock.patch.object(einvoice, "MAX_XML_BYTES", 64 * 1024):
            path = self.build(
                [("scan.jpg", b"\x00" * 200_000, "Supplement"), ("factur-x.xml", XML, "Data")]
            )
            self.assertEqual(einvoice.embedded_xml(path), XML)


class EndToEndTests(SimpleTestCase):
    """A Factur-X PDF in, a ParsedInvoice out - the whole point."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def test_a_factur_x_pdf_reads_as_its_own_exact_figures(self):
        path = write_pdf_with_attachments(
            os.path.join(self.folder.name, "FA-2026-0042.pdf"),
            ["Brasserie du Canal", "Facture FA-2026-0042", "Total TTC 229,39 EUR"],
            [("factur-x.xml", XML, "Data")],
        )
        parsed = einvoice.read(einvoice.embedded_xml(path))
        self.assertEqual(parsed.invoice_number, "FA-2026-0042")
        self.assertEqual(parsed.printed_total_ttc, D("229.39"))
        self.assertEqual([line.total_ht for line in parsed.lines], [D("169.00"), D("25.20")])
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)
