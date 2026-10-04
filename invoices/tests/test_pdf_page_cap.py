"""A PDF's page count is refused before any of its pages is read (security
review HARDEN-01).

The page cap of UPLOAD-1 (`ocr.MAX_PAGES`) lived in `ocr.page_images`, and
two readers ran before it or without it: the text layer
(`ocr.text_layer_pages`, which `receipts.import_document` asks first) and a
supplier's own reader (`InvoiceParser.parse`, which never renders). Both
looped over every page with pdfplumber, which keeps each page it read until
the file is closed - measured at 0,57 MB a page of 300 glyphs and 5,7 MB one
of 3 000, and a PDF under the 25 MB upload cap can carry tens of thousands of
pages sharing one content stream: gigabytes in the one process serving every
bar, before DocumentTooBig could be raised.

Now `ocr.check_page_count` counts first, the way pdfplumber will iterate
(pdfminer's walk of the page tree, stopped one past the cap - pdfium's count
believes the /Count a file declares), and `ocr.pdf_pages` releases each page
once read. The refusal is the app's own French sentence, on the file's line.

Machine safety: every file here is a few hundred bytes, three pages at most
past a cap patched down to 2; nothing is rendered."""

import os
import shutil
import tempfile
import zlib
from unittest import mock

import pdfplumber
import pypdfium2 as pdfium
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.html import escape
from pdfminer.pdfpage import PDFPage

from accounts import paths
from invoices import ocr
from invoices.models import Invoice, Supplier
from invoices.parsers import get_parser
from invoices.parsers.base import InvoiceParser
from invoices.receipt_batches import run_receipt_batch, stage_batch
from invoices.receipts import RereadError, import_document, reread_document
from returnables import reading
from tests.factories import make_invoice

LINE = "ARTICLE EXEMPLE CONDITIONNE PAR SIX  2  12,50  25,00"
TOO_LONG = "Document trop long pour être lu : 3 pages, 2 au plus."


def pdf_of_pages(pages: int, declared: int | None = None, line: str = LINE) -> bytes:
    """`pages` pages sharing one content stream that prints `line` - a text
    layer on every page. `declared` is the /Count the page tree states (the
    real count when None)."""
    content = f"BT /F1 10 Tf 40 700 Td ({line}) Tj ET".encode("latin-1")
    kids = " ".join(f"{5 + index} 0 R" for index in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages if declared is None else declared} >>".encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 3 0 R /Resources << /Font << /F1 4 0 R >> >> >>"
    ] * pages
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)


def never(what):
    return mock.Mock(side_effect=AssertionError(f"{what} : une page a été lue"))


class PageReadings:
    """Records, in order, every page pdfplumber reads and every page it
    releases - through the real methods."""

    def __init__(self, reading_methods=("extract_words", "extract_text", "extract_tables")):
        self.events = []
        self.patches = []
        for name in reading_methods:
            self.patches.append(
                mock.patch.object(
                    pdfplumber.page.Page, name, self._recording(name, getattr(pdfplumber.page.Page, name))
                )
            )
        self.patches.append(
            mock.patch.object(pdfplumber.page.Page, "close", self._recording("close", pdfplumber.page.Page.close))
        )

    def _recording(self, name, original):
        events = self.events

        def recorded(page, *args, **kwargs):
            events.append((name, page.page_number))
            return original(page, *args, **kwargs)

        return recorded

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()


class Recorder(InvoiceParser):
    """A supplier's reader that keeps the pages it was handed."""

    supplier_code = "ESSAI"
    needs_tables = True

    def parse_pages(self, pages, date_hint=None, source_name=""):
        self.pages = pages
        return pages


class CheckedBeforeAnyPageTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def pdf(self, name, pages, declared=None):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as handle:
            handle.write(pdf_of_pages(pages, declared))
        return path

    def test_the_text_layer_refuses_before_reading_a_page(self):
        path = self.pdf("long.pdf", 3)
        with (
            mock.patch.object(ocr, "MAX_PAGES", 2),
            mock.patch.object(pdfplumber.page.Page, "extract_words", never("extract_words")) as words,
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                ocr.text_layer_pages(path)
        words.assert_not_called()
        self.assertEqual(str(refused.exception), TOO_LONG)
        with mock.patch.object(ocr, "MAX_PAGES", 2):
            with self.assertRaises(ocr.DocumentTooBig):
                ocr.document_text(path)

    def test_nothing_of_pdfplumber_runs_on_a_document_over_the_cap(self):
        """Not even its opening: closing a pdfplumber document makes every
        page it has not made yet."""
        path = self.pdf("long.pdf", 3)
        with mock.patch.object(ocr, "MAX_PAGES", 2), mock.patch("pdfplumber.open", never("pdfplumber.open")) as opened:
            with self.assertRaises(ocr.DocumentTooBig):
                ocr.text_layer_pages(path)
        opened.assert_not_called()

    def test_a_file_declaring_fewer_pages_than_it_holds_is_refused_all_the_same(self):
        """/Count 1 over three pages: one page to pdfium, which believes it,
        three to pdfplumber, which walks the tree - and reads them all."""
        path = self.pdf("menteur.pdf", 3, declared=1)
        document = pdfium.PdfDocument(path)
        try:
            self.assertEqual(len(document), 1)
        finally:
            document.close()
        with (
            mock.patch.object(ocr, "MAX_PAGES", 2),
            mock.patch.object(pdfplumber.page.Page, "extract_words", never("extract_words")) as words,
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                ocr.text_layer_pages(path)
        words.assert_not_called()
        self.assertEqual(str(refused.exception), "Document trop long pour être lu : plus de 2 pages, 2 au plus.")

    def test_the_count_walks_one_page_past_the_cap_whatever_the_length(self):
        path = self.pdf("tres-long.pdf", 40)
        walked = []
        real = PDFPage.create_pages.__func__

        def counting(cls, document):
            for page in real(cls, document):
                walked.append(page)
                yield page

        with mock.patch.object(ocr, "MAX_PAGES", 2), mock.patch.object(PDFPage, "create_pages", classmethod(counting)):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                ocr.check_page_count(path)
        self.assertEqual(len(walked), 3)
        self.assertEqual(str(refused.exception), "Document trop long pour être lu : 40 pages, 2 au plus.")

    def test_a_supplier_s_reader_refuses_before_reading_a_page(self):
        path = self.pdf("long.pdf", 3)
        for parser in (Recorder(), get_parser("METRO")):
            with (
                self.subTest(parser=type(parser).__name__),
                mock.patch.object(ocr, "MAX_PAGES", 2),
                mock.patch.object(pdfplumber.page.Page, "extract_text", never("extract_text")) as text,
                mock.patch.object(pdfplumber.page.Page, "extract_tables", never("extract_tables")) as tables,
            ):
                with self.assertRaises(ocr.DocumentTooBig) as refused:
                    parser.parse(path)
                text.assert_not_called()
                tables.assert_not_called()
                self.assertEqual(str(refused.exception), TOO_LONG)

    def test_at_the_cap_the_text_layer_releases_each_page_once_read(self):
        path = self.pdf("deux-pages.pdf", 2)
        with mock.patch.object(ocr, "MAX_PAGES", 2), PageReadings() as readings:
            pages = ocr.text_layer_pages(path)
        self.assertEqual(len(pages), 2)
        self.assertIn("ARTICLE EXEMPLE", pages[1].text)
        self.assertEqual(
            readings.events[:4],
            [("extract_words", 1), ("close", 1), ("extract_words", 2), ("close", 2)],
        )

    def test_at_the_cap_a_supplier_s_reader_releases_each_page_once_read(self):
        path = self.pdf("deux-pages.pdf", 2)
        parser = Recorder()
        with mock.patch.object(ocr, "MAX_PAGES", 2), PageReadings() as readings:
            parser.parse(path)
        self.assertEqual([("ARTICLE EXEMPLE" in page.text) for page in parser.pages], [True, True])
        self.assertEqual(
            [event for event in readings.events[:6]],
            [
                ("extract_text", 1),
                ("extract_tables", 1),
                ("close", 1),
                ("extract_text", 2),
                ("extract_tables", 2),
                ("close", 2),
            ],
        )

    def test_what_is_no_pdf_is_not_counted(self):
        """A photo is page_images' to weigh; a file that is not there, or
        not a PDF, is said by what reads it next."""
        ocr.check_page_count(os.path.join(self.folder, "absente.jpg"))
        ocr.check_page_count(os.path.join(self.folder, "absent.pdf"))
        broken = os.path.join(self.folder, "casse.pdf")
        with open(broken, "wb") as handle:
            handle.write(b"%PDF-1.4 cut short")
        ocr.check_page_count(broken)
        self.assertEqual(ocr.text_layer_pages(broken), [])


class PagesMadeCounter:
    """Counts every page pdfminer makes (`PDFPage.create_pages`, what
    pdfplumber iterates - and what closing a pdfplumber document walks,
    whether or not a page was read)."""

    def __init__(self):
        self.made = 0
        real = PDFPage.create_pages.__func__
        counter = self

        def counting(cls, document):
            for page in real(cls, document):
                counter.made += 1
                yield page

        self.patch = mock.patch.object(PDFPage, "create_pages", classmethod(counting))

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class NoReaderMakesEveryPageTests(SimpleTestCase):
    """What runs before the page count - or has no page cap of its own -
    makes no page, or the pages of a document within the cap only (review
    of the HARDEN-01 fix)."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_the_e_invoice_reader_makes_no_page(self):
        """`einvoice.document_xml` runs first on every PDF, before the count.
        Opened through pdfplumber, whose close() makes every page it has not
        made yet, it walked all of them - 4,5 s and 15 MB for 5 000 light
        pages - to read an attachment that lives in the catalog."""
        from invoices import einvoice
        from invoices.tests import einvoice_files
        from invoices.tests.pdf_files import write_pdf_with_attachments

        xml = einvoice_files.CII_TWO_RATES.encode("utf-8")
        path = write_pdf_with_attachments(
            os.path.join(self.folder, "factur-x.pdf"), ["Facture", "Total 229,39 EUR"], [("factur-x.xml", xml, "Data")]
        )
        with PagesMadeCounter() as pages:
            self.assertEqual(einvoice.document_xml(path), xml)
        self.assertEqual(pages.made, 0)
        # An ordinary PDF: no attachment, no page made either.
        path = os.path.join(self.folder, "ordinaire.pdf")
        with open(path, "wb") as handle:
            handle.write(pdf_of_pages(3))
        with PagesMadeCounter() as pages:
            self.assertIsNone(einvoice.document_xml(path))
        self.assertEqual(pages.made, 0)

    def test_the_ai_reader_refuses_before_reading_a_page(self):
        """`llm_fallback._extract_text` read every page of the document it
        sends to the API, with no cap (the « Analyse IA » upload)."""
        from invoices.parsers import llm_fallback

        path = os.path.join(self.folder, "long.pdf")
        with open(path, "wb") as handle:
            handle.write(pdf_of_pages(3))
        with (
            mock.patch.object(ocr, "MAX_PAGES", 2),
            mock.patch.object(pdfplumber.page.Page, "extract_text", never("extract_text")) as text,
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                llm_fallback._extract_text(path)
        text.assert_not_called()
        self.assertEqual(str(refused.exception), TOO_LONG)

    def test_the_ai_reader_reads_a_document_within_the_cap_page_by_page(self):
        from invoices.parsers import llm_fallback

        path = os.path.join(self.folder, "deux-pages.pdf")
        with open(path, "wb") as handle:
            handle.write(pdf_of_pages(2))
        with mock.patch.object(ocr, "MAX_PAGES", 2), PageReadings(("extract_text",)) as readings:
            text = llm_fallback._extract_text(path)
        self.assertEqual(text.count("ARTICLE EXEMPLE"), 2)
        self.assertEqual(readings.events[:4], [("extract_text", 1), ("close", 1), ("extract_text", 2), ("close", 2)])


class RefusedWhereTheDocumentArrivesTests(TestCase):
    """Each way a PDF comes in says the refusal in French, and files
    nothing."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.content = pdf_of_pages(3)

    def test_the_one_import_refuses_before_any_reader_reads_a_page(self):
        """Before the slip reader (the returnables guard, which reads up to
        five pages) and before the text layer."""
        path = os.path.join(self.folder, "long.pdf")
        with open(path, "wb") as handle:
            handle.write(self.content)
        with (
            mock.patch.object(ocr, "MAX_PAGES", 2),
            mock.patch.object(pdfplumber.page.Page, "extract_words", never("extract_words")) as words,
            mock.patch.object(pdfplumber.page.Page, "extract_text", never("extract_text")) as text,
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                import_document(path, display_filename="long.pdf")
        words.assert_not_called()
        text.assert_not_called()
        self.assertEqual(str(refused.exception), TOO_LONG)
        self.assertFalse(Invoice.objects.exists())

    def test_the_single_upload_says_it(self):
        """Filed under Metro, whose reader never reaches page_images: an
        empty invoice used to be filed, « aucune ligne »."""
        metro = Supplier.objects.get(code="METRO")
        with mock.patch.object(ocr, "MAX_PAGES", 2):
            response = self.client.post(
                reverse("invoices:invoice_upload"),
                {"supplier": metro.pk, "source_file": SimpleUploadedFile("facture.pdf", self.content)},
                follow=True,
            )
        self.assertContains(response, escape(f"Échec de l'import. {TOO_LONG}"))
        self.assertFalse(Invoice.objects.exists())

    def test_the_ai_upload_refuses_before_the_api_is_called(self):
        """« Analyse IA »: the API was billed for every page, the invoice
        filed, and only then did the text kept beside it refuse the file -
        reported as a failed upload after all."""
        from django.test import override_settings

        from invoices.parsers import LLM_PARSER_KEY

        ai = Supplier.objects.get(parser_key=LLM_PARSER_KEY)
        with (
            override_settings(ANTHROPIC_API_KEY="cle-de-test"),
            mock.patch.object(ocr, "MAX_PAGES", 2),
            mock.patch("anthropic.Anthropic", never("l'API")) as api,
        ):
            response = self.client.post(
                reverse("invoices:invoice_upload"),
                {"supplier": ai.pk, "source_file": SimpleUploadedFile("facture.pdf", self.content)},
                follow=True,
            )
        api.assert_not_called()
        self.assertContains(response, escape(f"Échec de l'import. {TOO_LONG}"))
        self.assertFalse(Invoice.objects.exists())

    def test_a_reader_s_import_refuses_before_filing_anything(self):
        """`parse_and_import` with no reader (a mailbox source's supplier
        without one) filed the invoice EMPTY, then asked for its text, which
        refused - an invoice left behind by a failed import."""
        from invoices.importing import parse_and_import

        supplier = Supplier.objects.create(code="SANSLECTEUR", name="Sans lecteur", parser_key="")
        path = os.path.join(self.folder, "long.pdf")
        with open(path, "wb") as handle:
            handle.write(self.content)
        with mock.patch.object(ocr, "MAX_PAGES", 2):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                parse_and_import(path, supplier)
        self.assertEqual(str(refused.exception), TOO_LONG)
        self.assertFalse(Invoice.objects.exists())

    def test_a_folder_says_it_on_that_file_s_line(self):
        """It used to say it too - from page_images, once the text layer
        had read every page, twice."""
        batch = stage_batch([SimpleUploadedFile("long.pdf", self.content)])
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches", str(batch.pk)), True)
        with (
            mock.patch.object(ocr, "MAX_PAGES", 2),
            mock.patch.object(pdfplumber.page.Page, "extract_words", never("extract_words")) as words,
        ):
            batch = run_receipt_batch(batch.pk)
        words.assert_not_called()
        (entry,) = batch.results
        self.assertEqual((entry["status"], entry["message"]), ("error", TOO_LONG))
        self.assertFalse(Invoice.objects.exists())

    def test_reading_a_supplier_s_invoice_again_says_it(self):
        """Metro's reader read every page and found no line; the refusal
        was never said."""
        invoice = make_invoice(supplier=Supplier.objects.get(code="METRO"))
        invoice.source_file.save("facture-longue.pdf", ContentFile(self.content), save=True)
        self.addCleanup(invoice.source_file.delete, save=False)
        with mock.patch.object(ocr, "MAX_PAGES", 2):
            with self.assertRaises(RereadError) as refused:
                reread_document(invoice)
        self.assertEqual(str(refused.exception), f"{TOO_LONG} La facture n'a pas été modifiée.")


TOO_DENSE = (
    "Document trop chargé pour être lu : plus de 1\N{NO-BREAK SPACE}000 caractères, traits ou images sur une page."
)


class TooManyGlyphsTests(TestCase):
    """A page drawing hundreds of thousands of glyphs from one compressed
    Tj (1 KB of PDF, 1,4 GB once pdfminer had made a character of each):
    every Achats reader stops at `returnables.reading.MAX_PAGE_GLYPHS` a
    page and says it on the file's line - never « no text layer », which
    sent the file on to be rendered. Machine safety: the cap is patched down
    to 1 000, the page draws 5 000."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "dense.pdf")
        with open(self.path, "wb") as handle:
            handle.write(pdf_of_pages(1, line="A" * 5_000))
        patch = mock.patch.object(reading, "MAX_PAGE_GLYPHS", 1_000)
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_reader_says_it(self):
        from invoices.parsers import llm_fallback

        readers = {
            "text_layer_pages": ocr.text_layer_pages,
            "document_text": ocr.document_text,
            "InvoiceParser.parse": Recorder().parse,
            "llm_fallback": llm_fallback._extract_text,
        }
        for name, reader in readers.items():
            with self.subTest(reader=name), self.assertRaises(ocr.DocumentTooBig) as refused:
                reader(self.path)
            self.assertEqual(str(refused.exception), TOO_DENSE)

    def test_images_drawn_say_it_too(self):
        """A figure and an image kept for every image drawn, about 2 KB:
        100 000 draws of one 1x1 image took 263 MB in the text layer."""
        from returnables.tests.test_reading import drawing_images

        for inline in (False, True):
            with open(self.path, "wb") as handle:
                handle.write(drawing_images(1_500, inline=inline))
            with self.subTest(inline=inline):
                with self.assertRaises(ocr.DocumentTooBig) as refused:
                    ocr.text_layer_pages(self.path)
                self.assertEqual(str(refused.exception), TOO_DENSE)

    def test_the_one_import_files_nothing(self):
        with mock.patch("invoices.receipts.page_images", never("page_images")) as rendered:
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                import_document(self.path, display_filename="dense.pdf")
        rendered.assert_not_called()
        self.assertEqual(str(refused.exception), TOO_DENSE)
        self.assertFalse(Invoice.objects.exists())

    def test_a_page_within_the_cap_reads(self):
        path = os.path.join(self.folder, "facture.pdf")
        with open(path, "wb") as handle:
            handle.write(pdf_of_pages(2))
        self.assertEqual(ocr.document_text(path).count("ARTICLE EXEMPLE"), 2)


def drawn_line(text: str, top: int, size: int) -> bytes:
    """A content stream printing `text` at `top`, padded with spaces to
    `size` bytes."""
    return f"BT /F1 10 Tf 40 {top} Td ({text}) Tj ET\n".encode().ljust(size)


HEAVY = "Document trop lourd pour être lu : plus de 6 Ko une fois décompressé."


class InflatedContentTests(TestCase):
    """Only a bon's reading shared one inflate budget: Achats' readers gave
    every stream the 64 MB of the per-stream bound, with no total, and
    pdfminer keeps what it decoded until the file closes - a 718 KB PDF
    listing twelve 60 MB content streams took 842 MB in the text layer,
    then PDFium decoded them all again to render (120 MB a stream). Now a
    PDF's content streams are weighed under `ocr.MAX_INFLATE_TOTAL` before
    any page is read or rendered, and the readers share that budget too.
    Machine safety: the bounds are patched down to a few KB."""

    def setUp(self):
        from returnables.tests.test_reading import pdf_with_streams

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "lourd.pdf")
        streams = [(["FlateDecode"], zlib.compress(drawn_line(LINE, top, 4_000))) for top in (700, 600)]
        with open(self.path, "wb") as handle:
            handle.write(pdf_with_streams(streams))

    def test_every_reader_refuses_before_reading_a_page(self):
        from invoices.parsers import llm_fallback

        readers = {
            "check_page_count": ocr.check_page_count,
            "text_layer_pages": ocr.text_layer_pages,
            "InvoiceParser.parse": Recorder().parse,
            "llm_fallback": llm_fallback._extract_text,
            "page_images": lambda path: list(ocr.page_images(path)),
        }
        for name, reader in readers.items():
            with (
                self.subTest(reader=name),
                mock.patch.object(ocr, "MAX_INFLATE_TOTAL", 6_000),
                mock.patch.object(pdfplumber.page.Page, "extract_words", never("extract_words")),
                mock.patch.object(pdfplumber.page.Page, "extract_text", never("extract_text")),
                mock.patch("invoices.pdfium_sandbox.run", never("PDFium")),
            ):
                with self.assertRaises(ocr.DocumentTooBig) as refused:
                    reader(self.path)
                self.assertEqual(str(refused.exception), HEAVY)

    def test_within_the_budget_the_document_reads(self):
        with mock.patch.object(ocr, "MAX_INFLATE_TOTAL", 9_000):
            ocr.check_page_count(self.path)
            self.assertEqual(ocr.document_text(self.path).count("ARTICLE EXEMPLE"), 2)

    def test_a_stream_listed_again_is_weighed_again(self):
        """pdfminer decodes a stream once, however often /Contents lists
        it; PDFium and the interpreter go through it every time."""
        from returnables.tests.test_reading import pdf_with_streams

        streams = [(["FlateDecode"], zlib.compress(drawn_line(LINE, 700, 4_000)))] * 3
        content = pdf_with_streams(streams).replace(b"/Contents [4 0 R 5 0 R 6 0 R]", b"/Contents [4 0 R 4 0 R 4 0 R]")
        self.assertIn(b"/Contents [4 0 R 4 0 R 4 0 R]", content)
        with open(self.path, "wb") as handle:
            handle.write(content)
        with mock.patch.object(ocr, "MAX_INFLATE_TOTAL", 9_000):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                ocr.check_page_count(self.path)
        self.assertEqual(str(refused.exception), HEAVY.replace("6 Ko", "9 Ko"))

    def test_a_form_drawn_again_is_counted_again_by_the_readers(self):
        """Weighed once, a form the page draws over and over is interpreted
        each time (6 to 11 s of CPU a MB): the readers count every run."""
        from invoices.parsers import llm_fallback
        from returnables.tests.test_reading import pdf_drawing_a_form

        with open(self.path, "wb") as handle:
            handle.write(pdf_drawing_a_form(3, 4_000))
        # Should the weighing (DrawnContentTests) miss it.
        with (
            mock.patch.object(ocr, "MAX_RUN_TOTAL", 9_000),
            mock.patch.object(ocr, "_weigh_contents", lambda path: None),
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                ocr.text_layer_pages(self.path)
        self.assertEqual(str(refused.exception), "Document trop long à lire : plus de 9 Ko de contenu à dessiner.")
        with mock.patch.object(ocr, "MAX_RUN_TOTAL", 13_000):
            self.assertEqual(llm_fallback._extract_text(self.path).count("REPRISE VIDE"), 3)

    def test_the_readers_share_the_budget_too(self):
        """Should a stream escape the weighing (a form a page draws, a
        stream pdfminer decodes for itself), the reading is bounded all the
        same - and never « no text layer », which sent it on to PDFium."""
        with (
            mock.patch.object(ocr, "MAX_INFLATE_TOTAL", 6_000),
            mock.patch.object(ocr, "_weigh_contents", lambda path: None),
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                ocr.text_layer_pages(self.path)
        self.assertEqual(str(refused.exception), HEAVY)

    def test_a_stream_past_the_per_stream_bound_is_refused_not_unread(self):
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 1_000):
            with self.assertRaises(ocr.DocumentTooBig):
                ocr.text_layer_pages(self.path)

    def test_the_one_import_files_nothing(self):
        with (
            mock.patch.object(ocr, "MAX_INFLATE_TOTAL", 6_000),
            mock.patch("invoices.receipts.page_images", never("page_images")),
        ):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                import_document(self.path, display_filename="lourd.pdf")
        self.assertEqual(str(refused.exception), HEAVY)
        self.assertFalse(Invoice.objects.exists())


def pdf_with_annotations(count: int, size: int) -> bytes:
    """A one-page PDF whose `count` annotations share one appearance: a form
    of DRAWN padded to `size` bytes."""
    from returnables.tests.test_reading import DRAWN, pdf_of_objects

    form = zlib.compress(DRAWN.ljust(size))
    annotations = " ".join(f"{7 + index} 0 R" for index in range(count)).encode()
    return pdf_of_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                b"/Resources << /Font << /F1 6 0 R >> >> /Annots [" + annotations + b"] >>"
            ),
            b"<< /Length " + str(len(DRAWN)).encode() + b" >>\nstream\n" + DRAWN + b"\nendstream",
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 595 842] /Filter /FlateDecode /Length "
            + str(len(form)).encode()
            + b" >>\nstream\n"
            + form
            + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        ]
        + [b"<< /Type /Annot /Subtype /Square /Rect [0 0 595 842] /AP << /N 5 0 R >> >>"] * count
    )


DRAWN_TOO_LONG = "Document trop long à lire : plus de 9 Ko de contenu à dessiner."


class DrawnContentTests(TestCase):
    """PDFium draws more than pdfminer reads, and parses a form again every
    time it is drawn, keeping about 20 bytes of page objects a byte of path:
    a 7,6 KB PDF whose 40 annotations share one 1 MB appearance took 850 MB
    under PDFIUM_LOCK, past every reader's bound (pdfminer never reads an
    annotation); a form drawn 20 times by Do took 512 MB on the path that
    renders with no text layer first (`ocr_pdf`). Now what a page draws -
    its /Contents, every form each time it is drawn, every annotation's
    appearance - is weighed under `ocr.MAX_RUN_TOTAL` before PDFium opens
    the file, and every XObject drawn or annotation counts against
    reading.MAX_PAGE_GLYPHS. Machine safety: the bounds are patched down
    to a few KB; nothing is rendered."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "dessin.pdf")

    def write(self, content: bytes) -> None:
        with open(self.path, "wb") as handle:
            handle.write(content)

    def assert_refused_before_pdfium(self, said: str) -> None:
        readers = {
            "check_page_count": ocr.check_page_count,
            "page_images": lambda path: list(ocr.page_images(path)),
            "ocr_pdf": ocr.ocr_pdf,
        }
        for name, reader in readers.items():
            with self.subTest(reader=name), mock.patch("invoices.pdfium_sandbox.run", never("PDFium")):
                with self.assertRaises(ocr.DocumentTooBig) as refused:
                    reader(self.path)
                self.assertEqual(str(refused.exception), said)

    def test_annotations_sharing_one_appearance_are_weighed_each(self):
        self.write(pdf_with_annotations(3, 4_000))
        with mock.patch.object(ocr, "MAX_RUN_TOTAL", 9_000):
            self.assert_refused_before_pdfium(DRAWN_TOO_LONG)
        with mock.patch.object(ocr, "MAX_RUN_TOTAL", 13_000):
            ocr.check_page_count(self.path)

    def test_a_form_drawn_again_is_weighed_again(self):
        from returnables.tests.test_reading import pdf_drawing_a_form

        self.write(pdf_drawing_a_form(3, 4_000))
        with mock.patch.object(ocr, "MAX_RUN_TOTAL", 9_000):
            self.assert_refused_before_pdfium(DRAWN_TOO_LONG)
        with mock.patch.object(ocr, "MAX_RUN_TOTAL", 13_000):
            ocr.check_page_count(self.path)

    def test_drawings_past_the_cap_are_refused_before_pdfium(self):
        """An image drawn 1 500 times by Do or inline, or 1 500 annotations:
        PDFium keeps an object for each."""
        from returnables.tests.test_reading import drawing_images

        for content in (drawing_images(1_500), drawing_images(1_500, inline=True), pdf_with_annotations(1_500, 0)):
            self.write(content)
            with self.subTest(size=len(content)), mock.patch.object(reading, "MAX_PAGE_GLYPHS", 1_000):
                self.assert_refused_before_pdfium(TOO_DENSE)
        self.write(drawing_images(900))
        with mock.patch.object(reading, "MAX_PAGE_GLYPHS", 1_000):
            ocr.check_page_count(self.path)

    def test_what_draws_an_xobject_is_read_as_pdfium_reads_it(self):
        drawn = ocr._drawn(b"/Fm1 Do q /Fm1%comment\nDo Q /Im#201 Do (Fm2) Do <466D33> Do BI /W 1 ID x EI /Do Do")
        self.assertEqual(drawn, ({"Fm1": 2, "Im 1": 1, "Fm2": 1, "Fm3": 1, "Do": 1}, 1))
        # « Do » or « BI » inside a longer word draws nothing.
        self.assertEqual(ocr._drawn(b"/Fm1 Done /BIG (Dodo) Tj"), ({}, 0))

    def test_a_form_drawing_itself_is_weighed_once(self):
        """pdfminer and PDFium draw no form inside itself: 24 bytes of
        page, 54 of form, weighed once each."""
        from returnables.tests.test_reading import DRAWN, pdf_of_objects

        page, form = b"/Fm1 Do 1 0 0 1 0 -20 cm", b"/Fm1 Do\n" + DRAWN
        self.write(
            pdf_of_objects(
                [
                    b"<< /Type /Catalog /Pages 2 0 R >>",
                    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    (
                        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                        b"/Resources << /Font << /F1 6 0 R >> /XObject << /Fm1 5 0 R >> >> >>"
                    ),
                    b"<< /Length " + str(len(page)).encode() + b" >>\nstream\n" + page + b"\nendstream",
                    (
                        b"<< /Type /XObject /Subtype /Form /BBox [0 0 595 842] /Resources << /XObject << /Fm1 5 0 R >> "
                        b"/Font << /F1 6 0 R >> >> /Length "
                        + str(len(form)).encode()
                        + b" >>\nstream\n"
                        + form
                        + b"\nendstream"
                    ),
                    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                ]
            )
        )
        with mock.patch.object(ocr, "MAX_RUN_TOTAL", 100):
            ocr.check_page_count(self.path)
        from invoices.parsers import llm_fallback

        self.assertEqual(llm_fallback._extract_text(self.path).count("REPRISE VIDE"), 1)


class TooManyCodesTests(TestCase):
    """pdfminer expands a font's ToUnicode ranges code by code: an 872-byte
    file took 360 MB in the text layer. Every Achats reader stops a
    document's fonts at `returnables.reading.MAX_CMAP_CODES` codes and says
    it on the file's line. Machine safety: the cap is patched down to
    1 000, the font maps 5 000."""

    def setUp(self):
        from returnables.tests.test_reading import bfrange, mapping_codes

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "polices.pdf")
        with open(self.path, "wb") as handle:
            handle.write(mapping_codes(bfrange(0, 5_000)))
        patch = mock.patch.object(reading, "MAX_CMAP_CODES", 1_000)
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_reader_says_it(self):
        from invoices.parsers import llm_fallback

        readers = {
            "text_layer_pages": ocr.text_layer_pages,
            "InvoiceParser.parse": Recorder().parse,
            "llm_fallback": llm_fallback._extract_text,
        }
        for name, reader in readers.items():
            with self.subTest(reader=name), self.assertRaises(ocr.DocumentTooBig) as refused:
                reader(self.path)
            self.assertEqual(
                str(refused.exception),
                "Document trop chargé pour être lu : ses polices déclarent plus de 1\N{NO-BREAK SPACE}000 caractères.",
            )

    def test_the_one_import_files_nothing(self):
        with mock.patch("invoices.receipts.page_images", never("page_images")):
            with self.assertRaises(ocr.DocumentTooBig):
                import_document(self.path, display_filename="polices.pdf")
        self.assertFalse(Invoice.objects.exists())

    def test_within_the_cap_the_document_reads(self):
        with mock.patch.object(reading, "MAX_CMAP_CODES", 6_000):
            self.assertEqual(ocr.text_layer_pages(self.path), [None])
