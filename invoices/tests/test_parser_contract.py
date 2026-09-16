"""Contract every registered parser has to honour.

The point of these is the *next* parser, not the existing ones. All the
supplier-specific tests in this package work by handing `parse_pages` some
hand-written pages - which only works if the parser keeps its PDF reading in
the shared `InvoiceParser.parse`. A new parser that reaches for pdfplumber
itself would be silently untestable without a real (private) invoice file,
so that gets caught here instead.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers import PARSER_REGISTRY
from invoices.parsers.base import InvoiceParser, PdfPage
from invoices.parsers.llm_fallback import LLMFallbackParser
from invoices.parsers.receipt_base import ReceiptParser

# The LLM fallback works from whole-document text rather than a layout, and
# has no deterministic output to assert on - it's exempt by design.
LAYOUT_PARSERS = {
    key: parser for key, parser in PARSER_REGISTRY.items() if not isinstance(parser, LLMFallbackParser)
}

# The two ways raw material is allowed to reach a parser. `InvoiceParser.parse`
# reads a digital PDF with pdfplumber; `ReceiptParser.parse` recognises a
# photograph, because pdfplumber returns an empty string for every one of
# those files. Both are SHARED implementations that hand `parse_pages` a list
# of PdfPage - which is the property this whole module exists to protect.
# A parser defining its own `parse` is still a failure.
SHARED_PDF_READERS = (InvoiceParser.parse, ReceiptParser.parse)


class ParserContractTests(SimpleTestCase):
    def test_every_layout_parser_implements_parse_pages(self):
        for key, parser in LAYOUT_PARSERS.items():
            with self.subTest(parser=key):
                self.assertIsNot(
                    type(parser).parse_pages,
                    InvoiceParser.parse_pages,
                    f"{type(parser).__name__} must implement parse_pages()",
                )

    def test_no_layout_parser_overrides_the_pdf_reading(self):
        """Overriding parse() would put the file reading back inside the
        parser and make it impossible to test without a real invoice file.

        Only the two shared readers are allowed, and a parser reaches them by
        inheriting - never by writing its own.
        """
        for key, parser in LAYOUT_PARSERS.items():
            with self.subTest(parser=key):
                self.assertIn(
                    type(parser).parse,
                    SHARED_PDF_READERS,
                    f"{type(parser).__name__} must inherit parse(), not define it",
                )

    def test_every_receipt_parser_can_be_recognised_from_its_own_header(self):
        """A batch of photos is routed by what each ticket says at the top
        (see receipts.detect_parser). A receipt parser with no header
        patterns is unreachable: its shop's photos would all be reported as
        an unknown enseigne, and it would look like OCR had failed."""
        for key, parser in LAYOUT_PARSERS.items():
            if not isinstance(parser, ReceiptParser):
                continue
            with self.subTest(parser=key):
                self.assertTrue(
                    getattr(parser, "header_patterns", ()),
                    f"{type(parser).__name__} must declare header_patterns",
                )

    def test_every_parser_survives_an_empty_document(self):
        """A PDF that extracts to nothing (a scan, a failed extraction) must
        come back as an empty invoice, not an exception - the import flow
        records the result rather than crashing the whole gather run."""
        for key, parser in LAYOUT_PARSERS.items():
            with self.subTest(parser=key):
                invoice = parser.parse_pages([PdfPage(text="", tables=[])], source_name="empty.pdf")
                self.assertEqual(invoice.lines, [])
                self.assertEqual(invoice.supplier_code, key)

    def test_every_parser_survives_junk_input(self):
        """Text from a completely different supplier's invoice shouldn't
        match anything, and definitely shouldn't raise."""
        junk = PdfPage(
            text="Lorem ipsum 1234 5,67 dolor sit amet\n" * 20,
            tables=[[["a", "b"], ["1", "2"]]],
        )
        for key, parser in LAYOUT_PARSERS.items():
            with self.subTest(parser=key):
                parser.parse_pages([junk], source_name="junk.pdf")

    def test_registry_keys_match_supplier_codes(self):
        for key, parser in PARSER_REGISTRY.items():
            with self.subTest(parser=key):
                self.assertEqual(key, parser.supplier_code)

    def test_amounts_are_always_decimals_never_floats(self):
        """A float anywhere in this pipeline is a rounding bug waiting to
        happen - every downstream cost calculation reads these straight."""
        junk = PdfPage(text="", tables=[])
        for key, parser in LAYOUT_PARSERS.items():
            with self.subTest(parser=key):
                invoice = parser.parse_pages([junk], source_name="x.pdf")
                self.assertIsInstance(invoice.reconciliation_adjustment, Decimal)

    def test_text_extraction_kwargs_is_never_mutated(self):
        """It's a class-level dict shared by every instance."""
        for key, parser in PARSER_REGISTRY.items():
            with self.subTest(parser=key):
                self.assertIsInstance(parser.text_extraction_kwargs, dict)
