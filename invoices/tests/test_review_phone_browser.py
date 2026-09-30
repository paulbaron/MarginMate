"""The correction page of a ticket and of a PDF invoice in a real (headless)
Chrome, on a phone: what the stylesheet does once the page stacks (900 px
and under) and on a touch screen, which the test client cannot see.

* **The photo is part of the page.** Stacked above the lines, the photo was
  still sticky, a box as tall as the screen (100vh - 2rem) scrolling on its
  own around a tall receipt: a finger on the receipt - most of a phone's
  screen - scrolled the photo and not the lines, a second page inside the
  page (UX review, 30/09, « there are scroll issues with some panels »).
  There it is drawn whole and scrolls with the page; beside the lines, from
  901 px, it still follows them.
* **Nothing is wider than the phone**: the one column grew to its widest
  table (a bare 1fr is minmax(auto, 1fr)) - the shop's known prices, open
  wherever a price was recorded: the page was 507 px wide at 375.
* **A PDF's frame is 60 % of the screen at most**: a finger on the frame
  scrolls the PDF - or nothing, where a phone draws no PDF in a frame - and
  the page has to stay reachable around it.
* **Re-typing a ticket is done with a thumb**: this is the page with the
  most fields of all, and on a touch screen they are 16 px (iOS zooms the
  page in on a smaller one and leaves it zoomed), the « Frais » boxes 20 px
  a side and every control 44 px tall (the « touch » section of
  marginmate.css; tests/test_touch_browser.py for the lists).

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver (webdriver-manager looks the latest one up online).
Skipped where Chrome or its driver is missing. Data invented; the photo is
a PNG of 20 × 120 pixels, the PDF a hand-written page of a few hundred bytes.
"""

import io
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.base import ContentFile
from django.test import tag
from django.urls import reverse
from PIL import Image

from invoices.models import ShopItemPrice
from invoices.scrapers import website
from invoices.tests.pdf_files import write_pdf
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier
from tests.runner import log_in_the_browser
from tests.test_touch_browser import CONTROLS, FIELDS, TAPPED

PHONE = (375, 812)
DESKTOP = (1280, 900)

#: A receipt is tall and narrow: drawn at a phone's width, far taller than
#: its screen. A few pixels of data all the same.
PHOTO_SIZE = (20, 120)

LINES = (
    ("PAIN DE CAMPAGNE EXEMPLE", "2.84", "3.00"),
    ("CITRONS EXEMPLE 500G", "1.80", "1.90"),
    ("SUCRE EXEMPLE 1KG", "1.23", "1.30"),
    ("EAU GAZEUSE EXEMPLE 1L", "0.85", "0.90"),
)

#: What the shop's till prints for an unnamed line, recorded once: the
#: price list is then drawn open, its table under the lines.
KNOWN_PRICES = (
    ("0.70", "PAIN PITA EXEMPLE", date(2026, 1, 5)),
    ("1.20", "BOUQUET DE MENTHE FRAÎCHE EXEMPLE", None),
)


def tiny_png(size=PHOTO_SIZE) -> bytes:
    """A PNG of a few pixels - never a large image in a test."""
    output = io.BytesIO()
    Image.new("RGB", size, (235, 230, 220)).save(output, "PNG")
    return output.getvalue()


@tag("browser")
class ReviewPageOnAPhoneInBrowserTests(StaticLiveServerTestCase):
    # Its flush then fires no post_migrate (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        log_in_the_browser(self.driver, self.live_server_url)

    # -- the documents ------------------------------------------------------------------------------

    def ticket(self):
        """A ticket that passed its check, a few lines and its photo, saved
        on the invoice the way the import does (receipts.import_receipt);
        its shop has known prices, so their table is drawn open."""
        shop = make_supplier(name="Épicerie Exemple")
        invoice = make_invoice(
            supplier=shop, invoice_date=date(2026, 1, 12), ocr_text="EPICERIE EXEMPLE",
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        )
        for name, total_ht, printed_ttc in LINES:
            make_invoice_line(
                invoice=invoice, product=make_product(supplier=shop, raw_name=name), raw_name=name,
                total_ht=total_ht, vat_rate="0.055", printed_ttc=printed_ttc,
            )
        for price, label, valid_from in KNOWN_PRICES:
            ShopItemPrice.objects.create(supplier=shop, unit_price_ttc=Decimal(price), label=label, valid_from=valid_from)
        invoice.preview_image.save("ticket-exemple.png", ContentFile(tiny_png()), save=False)
        invoice.save(update_fields=["preview_image"])
        self.addCleanup(invoice.preview_image.storage.delete, invoice.preview_image.name)
        return invoice

    def pdf_invoice(self):
        """A supplier's PDF invoice, not a ticket: its PDF framed beside its lines."""
        supplier = make_supplier(name="Grossiste Exemple")
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 1, 12))
        make_invoice_line(invoice=invoice, product=make_product(supplier=supplier), total_ht="42.00", vat_rate="0.20")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "facture.pdf"
            write_pdf(str(path), ["FACTURE EXEMPLE", "TOTAL HT 42,00"])
            invoice.source_file.save("facture-exemple.pdf", ContentFile(path.read_bytes()), save=False)
        invoice.save(update_fields=["source_file"])
        self.addCleanup(invoice.source_file.storage.delete, invoice.source_file.name)
        return invoice

    # -- helpers ------------------------------------------------------------------------------------

    def device(self, width, height, *, touch):
        """A phone (mobile, a touch screen) or a desktop (a mouse)."""
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": touch},
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": touch, "maxTouchPoints": 5})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})

    def open(self, path):
        self.driver.get(self.live_server_url + path)
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def assert_a_touch_screen(self):
        """Loud, before anything is measured: without the emulation's coarse
        pointer the touch section is never read."""
        self.assertTrue(
            self.script("return window.matchMedia('(pointer: coarse)').matches;"),
            "(pointer: coarse) ne correspond pas : l'écran tactile n'est pas émulé",
        )

    def page_width(self) -> int:
        return self.script("return document.documentElement.scrollWidth;")

    def photo(self):
        """The photo's box as the page draws it, once its image has loaded
        (an image not loaded yet is 0 px tall and nothing overflows)."""
        self.wait_for(lambda: self.script(
            "var img = document.querySelector('.receipt-photo img');"
            "return !!img && img.complete && img.naturalWidth > 0;"
        ))
        return self.script(
            "var box = document.querySelector('.receipt-photo'), style = getComputedStyle(box);"
            "return {position: style.position, overflowY: style.overflowY,"
            " scrollHeight: box.scrollHeight, clientHeight: box.clientHeight,"
            " image: box.querySelector('img').getBoundingClientRect().height,"
            " screen: window.innerHeight};"
        )

    # -- the ticket ---------------------------------------------------------------------------------

    def test_on_a_phone_the_photo_scrolls_with_the_page(self):
        """Stacked above the lines, the photo is a part of the page: not
        sticky, not a box scrolling on its own around a receipt taller than
        the screen - and a finger on a line touches the line."""
        invoice = self.ticket()
        self.device(*PHONE, touch=True)
        self.open(reverse("invoices:receipt_review", args=[invoice.pk]))
        self.assert_a_touch_screen()
        photo = self.photo()
        # What makes the question real: the receipt is taller than the screen.
        self.assertGreater(photo["image"], photo["screen"])
        self.assertEqual(photo["position"], "static")
        # Not a box scrolling on its own: the page scrolls it.
        self.assertLessEqual(photo["scrollHeight"], photo["clientHeight"] + 1, photo)
        # A line brought to the middle of the screen is what a finger there
        # touches - never the photo, pinned over the lines.
        touched = self.script(
            "var field = document.querySelector('.correction-row input[name$=\"-product_name\"]');"
            "field.scrollIntoView({block: 'center'});"
            "var r = field.getBoundingClientRect();"
            "var hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
            "return {field: hit === field, photo: !!(hit && hit.closest('.receipt-photo'))};"
        )
        self.assertEqual(touched, {"field": True, "photo": False})

    def test_on_a_phone_the_page_is_no_wider_than_the_screen(self):
        """The shop's known prices, drawn open under the lines, are a table
        in its own sideways box: in a column of a bare 1fr, the column grew
        to that table and took the page past a 375 px phone (UX review,
        30/09) - Chrome then zooms the whole page out sideways."""
        invoice = self.ticket()
        self.device(*PHONE, touch=True)
        self.open(reverse("invoices:receipt_review", args=[invoice.pk]))
        self.assertTrue(self.script(
            "var table = document.querySelector('.known-prices table'); return !!table && table.getClientRects().length > 0;"
        ), "la table des prix connus n'est pas dessinée")
        self.assertLessEqual(self.page_width(), PHONE[0])

    def test_beside_the_lines_the_photo_still_follows_them(self):
        """From 901 px, with a mouse: the desktop's side-by-side, unchanged -
        a receipt is tall and the lines are short, and scrolling the form
        must not scroll the evidence out of view."""
        invoice = self.ticket()
        self.device(*DESKTOP, touch=False)
        self.open(reverse("invoices:receipt_review", args=[invoice.pk]))
        photo = self.photo()
        self.assertEqual(photo["position"], "sticky")
        self.assertEqual(photo["overflowY"], "auto")

    def test_on_a_phone_its_fields_are_16_px_and_its_boxes_20(self):
        """Every field of a ticket being re-typed - the lines' names, counts
        and amounts, the VAT table, the date - at 16 px, where 0.92rem made
        iOS zoom in on each; the « Frais » box of every line 20 px a side
        where it was 13 (the touch rule names it: its own `min-width: 0`
        outweighs the rule for every box, and left it 13 wide)."""
        invoice = self.ticket()
        self.device(*PHONE, touch=True)
        self.open(reverse("invoices:receipt_review", args=[invoice.pk]))
        self.assert_a_touch_screen()
        fields = self.script(FIELDS)
        self.assertGreater(len(fields), len(LINES))
        self.assertEqual([f"{field['what']} : {field['size']} px" for field in fields if field["size"] < 16], [])
        boxes = self.script(
            "return Array.from(document.querySelectorAll('.correction-row input[type=checkbox]'))"
            ".filter(function (e) { return e.getClientRects().length; })"
            ".map(function (e) { var r = e.getBoundingClientRect(); return [e.name, r.width, r.height]; });"
        )
        self.assertEqual(len(boxes), len(LINES))
        self.assertEqual([box for box in boxes if min(box[1], box[2]) < 19.5], [])

    def test_on_a_phone_its_controls_are_thumb_sized(self):
        """44 px outside a table, 36 in one: « Renommer partout », the ✕ of a
        line, « + Ajouter une ligne » and « Pas de chez … ? Changer
        d'enseigne » were a line of text or 30 px under a thumb. On a ticket
        and on a PDF invoice's page alike. The offenders are listed with
        their words."""
        pages = (
            ("ticket", reverse("invoices:receipt_review", args=[self.ticket().pk])),
            ("facture PDF", reverse("invoices:invoice_edit_lines", args=[self.pdf_invoice().pk])),
        )
        self.device(*PHONE, touch=True)
        for name, path in pages:
            self.open(path)
            self.assert_a_touch_screen()
            controls = self.script(CONTROLS, TAPPED)
            with self.subTest(page=name):
                self.assertTrue(controls)
                self.assertEqual(
                    [
                        f"{control['text']} : {control['height']:.1f} px{' (cellule)' if control['inCell'] else ''}"
                        for control in controls
                        if control["height"] < (35.5 if control["inCell"] else 43.5)
                    ],
                    [],
                )

    # -- a PDF invoice ------------------------------------------------------------------------------

    def test_on_a_phone_the_pdf_frame_leaves_room_for_the_page(self):
        """Under the lines' column, the PDF's frame is 60 % of the screen at
        most (it was 70vh): a finger on it scrolls the PDF, or nothing, and
        the rest of the page has to stay within reach. Beside the lines it
        takes the screen's height as before."""
        invoice = self.pdf_invoice()
        frame = (
            "var frame = document.querySelector('.document-frame');"
            "return frame ? [frame.getBoundingClientRect().height, window.innerHeight] : null;"
        )
        self.device(*PHONE, touch=True)
        self.open(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        height, screen = self.script(frame)
        self.assertGreater(height, 0)
        self.assertLessEqual(height, screen * 0.6 + 1)
        self.assertLessEqual(self.page_width(), PHONE[0])

        self.device(*DESKTOP, touch=False)
        self.open(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        height, screen = self.script(frame)
        self.assertGreater(height, screen * 0.6 + 1)
