"""A PDF that carries its own text is read from it, not photographed.

A web shop's invoice is a digital PDF with one image on it - its logo - and
the ticket import used to take that single image for a phone scan of the
page: the recogniser read the logo, seven characters, and the invoice came
out empty. Now an embedded image is the page only when it covers the page,
and a page with a text layer is read from that layer (no OCR at all). Files
written by hand, data invented.
"""

import os
import shutil
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from invoices import ocr
from invoices.receipts import recognise
from invoices.tests.pdf_files import write_pdf

INVOICE = [
    "CUISINE PRO EXEMPLE",
    "Facture n F-1042 du 07/11/2024",
    "Reference  Designation  Qte  Prix unitaire HT  Total HT",
    "AB123  Bac gastro 1/1  2  12,50  25,00",
    "CD456  Pince inox 30cm  1  4,20  4,20",
    "Total HT  29,20",
    "TVA 20%  5,84",
    "Total TTC  35,04",
]


class TextLayerTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.folder, name)

    def test_a_small_image_is_not_the_page(self):
        self.assertFalse(ocr.covers_page((40, 700, 160, 730), 595, 842))
        self.assertTrue(ocr.covers_page((0, 0, 595, 842), 595, 842))
        self.assertTrue(ocr.covers_page((10, 20, 580, 830), 595, 842))
        self.assertFalse(ocr.covers_page((0, 0, 0, 0), 595, 842))

    def test_a_page_with_a_logo_is_rendered_whole(self):
        pdf = write_pdf(self.path("facture.pdf"), INVOICE, logo=True)
        (image,) = list(ocr.page_images(pdf))
        # The whole A4 page at 300 dpi, not the 2x2 logo.
        self.assertGreater(image.width, 2000)
        self.assertGreater(image.height, 3000)

    def test_the_text_layer_is_read_line_by_line(self):
        pdf = write_pdf(self.path("facture.pdf"), INVOICE, logo=True)
        (page,) = ocr.text_layer_pages(pdf)
        self.assertEqual([line.text.split("  ")[0] for line in page.lines][:2], ["CUISINE PRO EXEMPLE", "Facture n F-1042 du 07/11/2024"])
        self.assertIn("Bac gastro 1/1", page.text)
        self.assertIn("25,00", page.lines[3].text)
        self.assertEqual(page.confidence, 1.0)

    def test_a_page_without_text_has_no_layer(self):
        pdf = write_pdf(self.path("photo.pdf"), [], logo=True)
        self.assertEqual(ocr.text_layer_pages(pdf), [None])
        photo = self.path("photo.png")
        with open(photo, "wb"):
            pass
        self.assertEqual(ocr.text_layer_pages(photo), [])

    def test_a_text_pdf_is_never_sent_to_the_recogniser(self):
        pdf = write_pdf(self.path("facture.pdf"), INVOICE, logo=True)
        with mock.patch("invoices.receipts.ocr_prepared_image") as recogniser:
            images, pages = recognise(pdf)
        recogniser.assert_not_called()
        self.assertEqual(len(images), 1)
        self.assertIn("Total TTC  35,04", pages[0].text)

    def test_a_scan_still_is(self):
        pdf = write_pdf(self.path("scan.pdf"), [], logo=True)
        with mock.patch("invoices.receipts.ocr_prepared_image", return_value=ocr.OcrPage(lines=[])) as recogniser:
            recognise(pdf)
        recogniser.assert_called_once()
