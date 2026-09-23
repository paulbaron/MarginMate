"""What the e-invoice reader refuses, cuts and says out loud.

`test_einvoice.py` pins that an EN 16931 document is read EXACTLY. This
module pins what happens when the document is not one an honest accounting
package produced - because since 1 September 2026 anything that can put a
file in the owner's mailbox can put one of these in front of this reader,
and the file it reads is the whole input.

Four promises, each of them a way the application was measured to break:

- **a figure that does not fit the column behind it is refused**, naming
  itself. Written to the database it is accepted by SQLite and then raises
  `decimal.InvalidOperation` on every READ, which puts the document - and
  Achats, and Marges - beyond the reach of the application for good: it can
  no longer be opened, corrected, re-read or even deleted, and only raw SQL
  gets it out;
- **every refusal is an `EInvoiceError`, which is a `ValueError`**, carrying
  a sentence in French. `decimal.InvalidOperation` and `decimal.Overflow`
  are `ArithmeticError`s and went past every handler the import has, so the
  owner saw a traceback;
- **the encoding is decided before the DOCTYPE guard runs.** That guard is a
  byte grep, and in UTF-16 the bytes are `<\\x00!\\x00D\\x00…`: a 2 KB
  billion-laughs walked straight through it and came back with a
  one-million-character seller name. What stopped the machine was libexpat's
  own limit, not this code;
- **what comes out of a PDF attachment is capped on the way out**, not only
  on the way in. 8 MB of double-deflated zeros cost 148 MB of memory, and 8
  MB behind a PNG predictor minutes of CPU, for files of a few kilobytes -
  single-threaded, so fifty of them is a folder import that does nothing for
  hours.

And one thing that is reported rather than refused: a line whose quantity
and amount disagree in sign. That is the supplier's arithmetic, which this
module never repairs.

Fixtures hand-written (`einvoice_files.py`) and PDFs built here: data
invented, nothing off a real document.
"""

import os
import tempfile
import time
import tracemalloc
import zlib
from decimal import Decimal

from django.test import SimpleTestCase

from invoices import einvoice
from invoices.tests.einvoice_files import (
    CII_ADJUSTMENT_TOO_WIDE,
    CII_AMOUNT_TOO_WIDE,
    CII_ENDLESS_NAME,
    CII_EXPONENT_LINE,
    CII_EXPONENT_TOTAL,
    CII_NEGATIVE_QUANTITY,
    CII_QUANTITY_TOO_WIDE,
    CII_RATE_TOO_WIDE,
    CII_TWO_RATES,
    CII_UNIT_PRICE_TOO_WIDE,
    XML_UTF16_DOCTYPE,
)

D = Decimal


def read(fixture: str):
    return einvoice.read(fixture.encode("utf-8"))


def check(parsed, label):
    found = [item for item in parsed.checks if item.label == label]
    return found[0] if found else None


class FiguresWiderThanTheirColumnTests(SimpleTestCase):
    """One stated number, in an invoice that otherwise balances.

    Each of these passed every check before: the document's own arithmetic
    holds, and only the width is wrong. That is precisely what made it
    dangerous - nothing on any screen said a word.
    """

    def refusal(self, fixture: str) -> str:
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            read(fixture)
        self.assertIsInstance(caught.exception, ValueError)
        return str(caught.exception)

    def test_a_line_amount_too_wide_is_refused_naming_itself(self):
        said = self.refusal(CII_AMOUNT_TOO_WIDE)
        self.assertIn("10000000000.00", said)
        self.assertIn("n'est pas importée", said)

    def test_a_rate_too_wide_is_refused(self):
        # InvoiceLine.vat_rate is five digits, four of them decimals: 1000 %
        # is 10.0000, which needs six.
        self.assertIn("taux de TVA", self.refusal(CII_RATE_TOO_WIDE))

    def test_a_unit_price_too_wide_is_refused(self):
        self.assertIn("prix unitaire", self.refusal(CII_UNIT_PRICE_TOO_WIDE))

    def test_a_quantity_too_wide_is_refused(self):
        self.assertIn("quantité", self.refusal(CII_QUANTITY_TOO_WIDE))

    def test_a_document_charge_too_wide_is_refused(self):
        self.assertIn("frais ou remises", self.refusal(CII_ADJUSTMENT_TOO_WIDE))

    def test_the_widest_figure_each_column_holds_is_still_read(self):
        """The cliff is exactly the column, not a round number below it: an
        invoice for 9 999 999 999,99 € is absurd and it is not this module's
        business to say so - it fits, so it is read."""
        widest = CII_TWO_RATES.replace(
            "<ram:LineTotalAmount>169.00</ram:LineTotalAmount>",
            "<ram:LineTotalAmount>9999999999.99</ram:LineTotalAmount>", 1
        )
        self.assertEqual(read(widest).lines[0].total_ht, D("9999999999.99"))


class ArithmeticThatIsNotAValueErrorTests(SimpleTestCase):
    """`1E+500` parses as a Decimal and explodes on the first sum.

    `EInvoiceError` is a `ValueError` on purpose: `receipt_batches` reports a
    ValueError as one file's error and carries on with the folder. An
    `ArithmeticError` is neither reported nor caught anywhere, so the owner
    was shown `[<class 'decimal.InvalidOperation'>]` at best and a traceback
    at worst.
    """

    def test_an_impossible_exponent_on_a_line_is_a_french_refusal(self):
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            read(CII_EXPONENT_LINE)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertIn("n'est pas importée", str(caught.exception))

    def test_an_impossible_exponent_on_the_total_is_a_french_refusal(self):
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            read(CII_EXPONENT_TOTAL)
        self.assertIsInstance(caught.exception, ValueError)

    def test_every_hostile_fixture_refuses_as_a_value_error(self):
        """The contract, pinned once over all of them: never an
        ArithmeticError, never a traceback, never a hang."""
        for name, fixture in (
            ("amount", CII_AMOUNT_TOO_WIDE), ("rate", CII_RATE_TOO_WIDE),
            ("unit", CII_UNIT_PRICE_TOO_WIDE), ("quantity", CII_QUANTITY_TOO_WIDE),
            ("adjustment", CII_ADJUSTMENT_TOO_WIDE), ("exponent", CII_EXPONENT_LINE),
            ("overflow", CII_EXPONENT_TOTAL),
        ):
            with self.subTest(fixture=name):
                with self.assertRaises(ValueError):
                    read(fixture)


class EncodingTests(SimpleTestCase):
    """A billion laughs in UTF-16 walked past a grep for b"<!DOCTYPE"."""

    def test_a_utf16_document_is_refused_without_being_parsed(self):
        data = XML_UTF16_DOCTYPE.encode("utf-16")
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            einvoice.read(data)
        self.assertIn("UTF-8", str(caught.exception))

    def test_a_utf16_document_is_not_offered_as_an_invoice(self):
        # Asked of every attachment of every PDF, so it answers rather than
        # raising - and the answer is no.
        self.assertFalse(einvoice.looks_like_an_invoice(XML_UTF16_DOCTYPE.encode("utf-16")))

    def test_a_utf16_document_expands_no_entity(self):
        """The measurement that made this a finding: before the guard, the
        seller's name came back a million characters long."""
        data = XML_UTF16_DOCTYPE.encode("utf-16")
        started = time.monotonic()
        with self.assertRaises(einvoice.EInvoiceError):
            einvoice.read(data)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_declared_utf16_encoding_is_refused_even_without_a_bom(self):
        declared = CII_TWO_RATES.replace('encoding="UTF-8"', 'encoding="UTF-16"')
        with self.assertRaises(einvoice.EInvoiceError):
            einvoice.read(declared.encode("utf-8"))

    def test_an_ordinary_utf8_invoice_is_untouched(self):
        self.assertEqual(read(CII_TWO_RATES).invoice_number, "FA-2026-0042")


def _pdf_with_stream(path: str, raw: bytes, filters: bytes, parms: bytes = b"") -> str:
    """A minimal PDF whose one embedded file carries `raw` behind `filters`.

    Hand-written rather than produced: what is being tested is a stream this
    project's own PDF builder would never make, which is the point.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles "
        b"<< /Names [(factur-x.xml) 5 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
        b"<< /Type /Filespec /F (factur-x.xml) /UF (factur-x.xml) /EF << /F 6 0 R >> >>",
        b"<< /Type /EmbeddedFile /Subtype /text#2Fxml /Filter " + filters + parms
        + b" /Length " + str(len(raw)).encode() + b" >>\nstream\n" + raw + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n").encode()
    with open(path, "wb") as handle:
        handle.write(bytes(out))
    return path


class AttachmentBombTests(SimpleTestCase):
    """The cap belongs on what comes OUT of a stream.

    MAX_ATTACHMENT_BYTES bounds the compressed stream, which bounds nothing:
    a megabyte of double-deflated zeros is terabytes. These are small enough
    to run in a suite and large enough that the old code's cost is visible
    against the new one's.
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(self._clean)

    def _clean(self):
        for name in os.listdir(self.folder):
            os.remove(os.path.join(self.folder, name))
        os.rmdir(self.folder)

    def _in(self, name: str) -> str:
        return os.path.join(self.folder, name)

    def _peak_megabytes(self, path: str) -> float:
        """What reading this file costs. Memory rather than the clock: the
        cost is what the finding is about, and a wall-clock assertion in a
        suite is a flake on a busy machine."""
        tracemalloc.start()
        try:
            try:
                einvoice.embedded_xml(path)
            except einvoice.EInvoiceError:
                pass
            return tracemalloc.get_traced_memory()[1] / 1e6
        finally:
            tracemalloc.stop()

    def test_a_chained_deflate_costs_no_more_than_the_cap(self):
        """32 MB of zeros behind two FlateDecodes, in a file of under 4 KB.

        `MAX_ATTACHMENT_BYTES` bounds the COMPRESSED stream, which bounds
        nothing at all. Measured at 64 MB before the bound moved to the
        output: 148 MB of memory for a 970-byte file.
        """
        twice = zlib.compress(zlib.compress(b"\0" * (32 * 1024 * 1024), 9), 9)
        path = _pdf_with_stream(self._in("chained.pdf"), twice, b"[/FlateDecode /FlateDecode]")
        self.assertLess(os.path.getsize(path), 4096)
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            einvoice.embedded_xml(path)
        self.assertIn("trop volumineuse", str(caught.exception))
        self.assertLess(self._peak_megabytes(path), 32)

    def test_a_predictor_is_not_decoded_at_all(self):
        """Minutes of CPU for a file of kilobytes, and no Factur-X producer
        puts an XML attachment behind one. Skipped, not refused: the PDF is
        then read as the ordinary document it looks like."""
        rows = b"".join(b"\x00" + b"\0" * 1024 for _ in range(4096))
        path = _pdf_with_stream(
            self._in("predictor.pdf"), zlib.compress(rows, 9), b"/FlateDecode",
            b" /DecodeParms << /Predictor 12 /Columns 1024 >>",
        )
        self.assertIsNone(einvoice.embedded_xml(path))
        self.assertLess(self._peak_megabytes(path), 4)

    def test_a_plain_deflated_invoice_is_still_read(self):
        path = _pdf_with_stream(
            self._in("good.pdf"), zlib.compress(CII_TWO_RATES.encode("utf-8"), 9), b"/FlateDecode"
        )
        data = einvoice.embedded_xml(path)
        self.assertIsNotNone(data)
        self.assertEqual(einvoice.read(data).invoice_number, "FA-2026-0042")


class NameLengthTests(SimpleTestCase):
    """A name is not money: it is cut, not refused."""

    def test_an_endless_product_name_is_cut_to_its_column(self):
        parsed = read(CII_ENDLESS_NAME)
        self.assertLessEqual(len(parsed.lines[0].raw_name), 255)
        self.assertTrue(parsed.lines[0].raw_name.startswith("BIERE"))

    def test_the_figures_beside_it_are_untouched(self):
        parsed = read(CII_ENDLESS_NAME)
        self.assertEqual(parsed.lines[0].total_ht, D("169.00"))
        self.assertEqual(parsed.printed_total_ttc, D("229.39"))

    def test_an_ordinary_name_is_not_touched(self):
        self.assertEqual(read(CII_TWO_RATES).lines[0].raw_name, "BIERE BLONDE FUT 30L")


class LineSignTests(SimpleTestCase):
    """A negative count at a positive amount is neither a purchase nor a
    return, and nothing downstream can decide which it meant.

    Not repaired: reported with both figures, like every other disagreement
    with a supplier's arithmetic. Left silent, the ledger takes 500 units out
    while 100 € is charged.
    """

    def test_a_negative_quantity_at_a_positive_amount_fails_a_check(self):
        parsed = read(CII_NEGATIVE_QUANTITY)
        failed = check(parsed, einvoice.LINE_SIGN_CHECK)
        self.assertIsNotNone(failed)
        self.assertFalse(failed.passed)
        self.assertIn("-2", failed.detail)
        self.assertIn("169.00", failed.detail)

    def test_it_reaches_the_document_as_a_warning(self):
        self.assertTrue(
            [said for said in read(CII_NEGATIVE_QUANTITY).warnings
             if einvoice.LINE_SIGN_CHECK in said]
        )

    def test_the_totals_still_check_out(self):
        """The trap: the header agrees with the crooked line, so every check
        that works on the totals passes and this is the only one that does
        not."""
        parsed = read(CII_NEGATIVE_QUANTITY)
        self.assertTrue(check(parsed, einvoice.TOTAL_CHECK).passed)
        self.assertTrue(check(parsed, einvoice.VAT_CHECK).passed)

    def test_an_ordinary_invoice_says_nothing(self):
        self.assertIsNone(check(read(CII_TWO_RATES), einvoice.LINE_SIGN_CHECK))

    def test_a_credit_note_says_nothing(self):
        """Every line of one is a negative count at a negative amount, which
        is this codebase's own shape for a return."""
        from invoices.tests.einvoice_files import CII_CREDIT_NOTE

        self.assertIsNone(check(read(CII_CREDIT_NOTE), einvoice.LINE_SIGN_CHECK))
