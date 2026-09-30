"""A touch screen in a real (headless) Chrome: the touch section at the end of
static/css/marginmate.css, which only `(pointer: coarse)` reaches - never a
width, never the test client.

* **16 px fields.** Under that, iOS Safari zooms the whole page in on the
  field a finger taps and leaves it zoomed: the app's fields were 0.92rem
  (14.7 px) on nearly every page, 164 of 164 on « Produits & charges »
  (UX review, 30/09). Only « Consignes » had its own 1rem rule. datatable's
  search boxes, made by the script after the page is drawn, are fields too,
  and so is an opened article's conversion box.
* **Thumb-sized controls.** 44 px tall in `<main>`, 36 in a table's row,
  where .btn-small was 30 and « Masquer », « Annuler » or a summary a line of
  text (1264 of 1339 controls on / under 32 px, measured 30/09). A recipe's
  till chips (« Vendue en caisse sous ») 36 too, where they were 22.
* **A phone held sideways** is wider than 860 px and some 430 tall: the
  desktop's layout, and none of the phone's width rules. Its table box left
  it a strip of rows scrolling inside the page, under a pinned header; and
  there only the touch section makes the article's row actions 36 px, 8 px
  apart, and its conversion box wide enough for 16 px digits.
* **A mouse changes nothing**, at any width - a desktop's or a phone's:
  14.72 px fields and 30 px small buttons, and a short window or a touch
  tablet keeps its table box and its pinned header.

Every touch test first asserts that `(pointer: coarse)` matches: without the
DevTools touch emulation it does not, and a test measuring nothing would
pass for the wrong reason. On « Produits & charges » the article's category
and its purchases are unfolded first: folded, as the page is drawn, the
catalogue's rows are not drawn and would be measured on nothing.

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver (webdriver-manager looks the latest one up online).
Skipped where Chrome or its driver is missing. Data invented.
"""

import tempfile
from datetime import date

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from bank import reconcile
from bank.tests.test_reconcile import debit_row, statement
from inventory.models import UnitChoices
from invoices.scrapers import website
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_type,
    make_supplier,
)
from tests.runner import log_in_the_browser

#: A phone held upright, the same phone held sideways (a 6.7" one), a
#: tablet held sideways and a desktop.
PHONE = (375, 812)
SIDEWAYS = (932, 430)
TABLET = (1024, 768)
DESKTOP = (1400, 900)

#: Every field a finger types in: not a hidden one, and not a box or a file
#: picker, which draw no text to zoom in on. datatable.js marks the search
#: box it makes (`data-persist="table-search:…"`).
FIELDS = """
    var made = function (e) { return (e.getAttribute('data-persist') || '').indexOf('table-search:') === 0; };
    var describe = function (e) {
        return e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (e.name ? '[name=' + e.name + ']' : '')
            + (e.className ? '.' + String(e.className).trim().split(/\\s+/).join('.') : '')
            + (made(e) ? ' (datatable.js)' : '');
    };
    return Array.from(document.querySelectorAll(
        'main input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]),'
        + ' main select, main textarea'
    )).filter(function (e) { return e.getClientRects().length; }).map(function (e) {
        return {what: describe(e), size: parseFloat(getComputedStyle(e).fontSize), generated: made(e),
                inCatalogue: !!e.closest('#catalogue')};
    });
"""

#: Every control a finger taps in <main>, drawn: its words, its height,
#: whether it sits in a table's cell.
CONTROLS = """
    return Array.from(document.querySelectorAll(arguments[0]))
        .filter(function (e) { return e.getClientRects().length; })
        .map(function (e) {
            return {text: (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60) || e.tagName,
                    height: e.getBoundingClientRect().height, inCell: !!e.closest('td, th')};
        });
"""

TAPPED = "main .btn, main .link-button, main summary, main .tabs a.tab, main .review-panel-jump"

#: The drawn checkboxes and radio buttons of <main>, with their size.
BOXES = """
    return Array.from(document.querySelectorAll('main input[type=checkbox], main input[type=radio]'))
        .filter(function (e) { return e.getClientRects().length; })
        .map(function (e) { var r = e.getBoundingClientRect();
                            return {name: e.name, width: r.width, height: r.height}; });
"""

#: The one article's category unfolded, then its row opened on its
#: purchases. Only what is still folded is clicked: the page reopens by
#: itself what this browser opened before (`marginmate:…stock:rows`), and a
#: second click would fold it again.
OPEN_THE_ARTICLE = """
    var header = document.querySelector('#catalogue .category-header[data-toggle]');
    var table = document.getElementById(header.dataset.toggle);
    if (table.hidden) header.click();
    var row = table.querySelector('tr.stock-row[data-stock-type-id]');
    if (document.getElementById(row.dataset.toggle).hidden) row.click();
"""

#: An opened purchase's « 1 produit = [ ] Litre » box.
CONVERSION = "#catalogue form.conversion-form input[name=stock_equivalent]"

#: The first drawn table of <main> with a header: its box's max-height and
#: its header cells' position.
TABLE_BOX = """
    var wrap = Array.from(document.querySelectorAll('main .table-wrap'))
        .filter(function (e) { return e.getClientRects().length && e.querySelector('thead th'); })[0];
    return wrap ? [getComputedStyle(wrap).maxHeight, getComputedStyle(wrap.querySelector('thead th')).position] : null;
"""


@tag("browser")
class TouchScreenInBrowserTests(StaticLiveServerTestCase):
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
        # « Produits & charges »: one article bought, one product waiting in
        # « À classer ». Its invoice is Achats' one document with a line.
        supplier = make_supplier(name="Grossiste Exemple")
        vodka = make_stock_type(name="Vodka Exemple", unit=UnitChoices.LITRE, category="Spiritueux")
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 1, 10))
        classified = make_product(
            supplier=supplier, raw_name="VODKA EXEMPLE 70CL", stock_type=vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        line = make_invoice_line(invoice=invoice, product=classified, quantity=10, total_ht="120")
        make_movement(stock_type=vodka, invoice_line=line, quantity=7, unit_cost_ht="12")
        waiting = make_product(supplier=supplier, raw_name="GIN EXEMPLE 1L")
        make_invoice_line(invoice=invoice, product=waiting, quantity=6, total_ht="90")
        # Banque: one debit of a statement, its table and its search box.
        reconcile.import_statement(statement(debit_row(date(2026, 1, 12), "GROSSISTE EXEMPLE", "150,00")))
        log_in_the_browser(self.driver, self.live_server_url)
        # What is open is remembered per browser, shared by the class.
        self.open(reverse("inventory:stock_list"))
        self.script("localStorage.clear(); sessionStorage.clear();")

    def pages(self):
        return (
            ("Produits & charges", reverse("inventory:stock_list")),
            ("Achats", reverse("invoices:invoice_list")),
            ("Banque", reverse("bank:bank_home")),
        )

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

    def visit(self, path):
        """A page as a reader has it in front of them - on « Produits &
        charges », the article unfolded on its purchases."""
        self.open(path)
        if path == reverse("inventory:stock_list"):
            self.script(OPEN_THE_ARTICLE)
            self.wait_for(lambda: self.script(
                "var input = document.querySelector(arguments[0]); return !!input && input.getClientRects().length > 0;",
                CONVERSION,
            ))

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def coarse(self) -> bool:
        return self.script("return window.matchMedia('(pointer: coarse)').matches;")

    def assert_a_touch_screen(self):
        """Loud, before anything is measured: without the emulation's coarse
        pointer the touch section is never read, and « nothing too small »
        would be measured on the desktop's rules."""
        self.assertTrue(self.coarse(), "(pointer: coarse) ne correspond pas : l'écran tactile n'est pas émulé")

    def small_fields(self, fields):
        return [f"{field['what']} : {field['size']} px" for field in fields if field["size"] < 16]

    def short_controls(self):
        """The page's controls, and those under 44 px outside a table's row
        or 36 in it - a chip or a segmented choice 36 too, a box 20 px a
        side - named with their words."""
        controls = self.script(CONTROLS, TAPPED)
        # A chip and a segmented choice sit in a wrapping row: 36 too.
        wrapped = self.script(CONTROLS, "main .chip, main .segmented button")
        boxes = self.script(BOXES)
        short = [
            f"{control['text']} : {control['height']:.1f} px{' (cellule)' if control['inCell'] else ''}"
            for control in controls
            if control["height"] < (35.5 if control["inCell"] else 43.5)
        ]
        short += [f"{chip['text']} : {chip['height']:.1f} px" for chip in wrapped if chip["height"] < 35.5]
        short += [
            f"case {box['name']} : {box['width']:.1f} × {box['height']:.1f} px"
            for box in boxes
            if min(box["width"], box["height"]) < 19.5
        ]
        return short, controls, wrapped, boxes

    # -- 16 px fields ---------------------------------------------------------------------------------

    def test_every_field_is_16_px_on_a_touch_screen(self):
        """iOS zooms the page in on a field under 16 px and leaves it zoomed:
        every field of these pages, datatable's search boxes and an opened
        article's conversion box included."""
        self.device(*PHONE, touch=True)
        generated = in_catalogue = 0
        for name, path in self.pages():
            self.visit(path)
            self.assert_a_touch_screen()
            fields = self.script(FIELDS)
            with self.subTest(page=name):
                self.assertTrue(fields, "aucun champ dessiné")
                self.assertEqual(self.small_fields(fields), [])
            generated += sum(1 for field in fields if field["generated"])
            in_catalogue += sum(1 for field in fields if field["inCatalogue"])
        # The boxes the script adds after the page is drawn were measured
        # too, and so was a field inside the opened catalogue.
        self.assertGreater(generated, 0)
        self.assertGreater(in_catalogue, 0)

    def test_a_mouse_keeps_the_desktop_fields(self):
        """A desktop is unchanged at any width: 0.92rem fields - in a window
        as narrow as a phone too, since a touch screen is `(pointer:
        coarse)`, never a width."""
        for size in (DESKTOP, PHONE):
            with self.subTest(size=size):
                self.device(*size, touch=False)
                self.open(reverse("inventory:stock_list"))
                self.assertFalse(self.coarse())
                self.assertEqual(
                    self.script("return getComputedStyle(document.getElementById('stock-search')).fontSize;"),
                    "14.72px",
                )

    # -- thumb-sized ----------------------------------------------------------------------------------

    def test_controls_are_thumb_sized_on_a_touch_screen(self):
        """44 px outside a table, 36 in a table's row - the row being mostly
        tapped whole - where .btn-small was 30 and a summary or a « Masquer »
        one line of text (UX review, 30/09); a chip or a segmented choice 36,
        a checkbox 20 px a side where it was 13. The offenders are listed
        with their words."""
        self.device(*PHONE, touch=True)
        in_cells = chips = boxes = 0
        for name, path in self.pages():
            self.visit(path)
            self.assert_a_touch_screen()
            short, controls, wrapped, ticked = self.short_controls()
            with self.subTest(page=name):
                self.assertTrue(controls, "aucun bouton dessiné")
                self.assertEqual(short, [])
            in_cells += sum(1 for control in controls if control["inCell"])
            chips += len(wrapped)
            boxes += len(ticked)
        # Each rule was measured on something: a button in a row, a chip, a box.
        self.assertGreater(in_cells, 0)
        self.assertGreater(chips, 0)
        self.assertGreater(boxes, 0)

    def test_a_recipe_s_till_chips_are_thumb_sized(self):
        """The recipe form's « Vendue en caisse sous » draws each till
        product linked as a chip that removes it (ui.js, `.pick-chip`): 22 px
        tall on a touch screen, some 27 px apart - a thumb aimed at one took
        its neighbour's link off (review of 30/09). 36 px, as a chip is; a
        mouse keeps them as they were."""
        from recipes.models import PosProduct

        recipe = make_recipe(name="Spritz exemple", selling_price_ttc="8.00")
        for name in ("SPRITZ EXEMPLE", "SPRITZ EXEMPLE HH"):
            PosProduct.objects.create(name=name, total_quantity=12, recipe=recipe)
        chips = "main .pick-chips .pick-chip"
        path = reverse("recipes:recipe_update", args=[recipe.pk])

        self.device(*PHONE, touch=True)
        self.open(path)
        self.assert_a_touch_screen()
        self.wait_for(lambda: len(self.script(CONTROLS, chips)) == 2)
        drawn = self.script(CONTROLS, chips)
        self.assertEqual([chip for chip in drawn if chip["height"] < 35.5], [], drawn)

        self.device(*DESKTOP, touch=False)
        self.open(path)
        self.assertFalse(self.coarse())
        self.wait_for(lambda: len(self.script(CONTROLS, chips)) == 2)
        self.assertTrue(all(chip["height"] < 30 for chip in self.script(CONTROLS, chips)))

    def test_a_mouse_keeps_the_small_buttons(self):
        """A desktop's .btn-small stays small, at a desktop's width and at a
        phone's: a table of figures is not doubled in height where nobody
        taps, and a narrow window is no touch screen."""
        for size in (DESKTOP, PHONE):
            self.device(*size, touch=False)
            found = 0
            for name, path in self.pages():
                self.open(path)
                self.assertFalse(self.coarse())
                small = [control for control in self.script(CONTROLS, "main .btn-small") if not control["inCell"]]
                with self.subTest(size=size, page=name):
                    tall = [
                        f"{control['text']} : {control['height']:.1f} px" for control in small if control["height"] >= 36
                    ]
                    self.assertEqual(tall, [])
                found += len(small)
            with self.subTest(size=size):
                self.assertGreater(found, 0)

    # -- a phone held sideways ------------------------------------------------------------------------

    def test_a_phone_held_sideways_is_a_touch_screen_all_the_same(self):
        """932 × 430 is wider than 860 px: the desktop's layout, where none of
        the phone's width rules make anything bigger - the catalogue is a
        table again, not cards. Its fields are 16 px and its controls
        thumb-sized all the same, because the touch section asks the
        pointer, not the width: the article's « Modifier » 36 px and its
        three actions 8 px apart (a line of text, 4 px apart, with a mouse),
        its conversion box 4.75rem so six digits still fit at 16 px (68 px
        with a mouse)."""
        self.device(*SIDEWAYS, touch=True)
        for name, path in self.pages():
            self.visit(path)
            self.assert_a_touch_screen()
            with self.subTest(page=name):
                self.assertEqual(self.small_fields(self.script(FIELDS)), [])
                self.assertEqual(self.short_controls()[0], [])
        self.visit(reverse("inventory:stock_list"))
        self.assertNotEqual(
            self.script("return getComputedStyle(document.querySelector('#catalogue table.stock-table thead')).display;"),
            "none",
        )
        actions = self.script(
            "var cell = document.querySelector('#catalogue .row-actions');"
            "return {links: Array.from(cell.querySelectorAll(':scope > a'))"
            "            .map(function (e) { return e.getBoundingClientRect().height; }),"
            "        gaps: Array.from(cell.querySelectorAll(':scope > * + *'))"
            "            .map(function (e) { return parseFloat(getComputedStyle(e).marginLeft); })};"
        )
        self.assertTrue(actions["links"])
        self.assertTrue(all(height >= 35.5 for height in actions["links"]), actions)
        self.assertTrue(actions["gaps"])
        self.assertTrue(all(gap >= 8 for gap in actions["gaps"]), actions)
        width = self.script("return document.querySelector(arguments[0]).getBoundingClientRect().width;", CONVERSION)
        self.assertGreaterEqual(width, 75.5)

    def test_a_phone_held_sideways_scrolls_its_tables_with_the_page(self):
        """932 × 430: the desktop's layout, and its table box of 100vh - 13rem
        left a strip of rows scrolling inside the page. Touch AND short: a
        mouse keeps the box and its pinned header in a window as short, and
        so does a tablet held sideways, tall enough for them."""
        self.device(*SIDEWAYS, touch=True)
        self.open(reverse("invoices:invoice_list"))
        self.assert_a_touch_screen()
        self.assertEqual(self.script(TABLE_BOX), ["none", "static"])

        for size, touch in ((SIDEWAYS, False), (TABLET, True), (DESKTOP, False)):
            with self.subTest(size=size, touch=touch):
                self.device(*size, touch=touch)
                self.open(reverse("invoices:invoice_list"))
                self.assertEqual(self.coarse(), touch)
                max_height, position = self.script(TABLE_BOX)
                self.assertNotEqual(max_height, "none")
                self.assertEqual(position, "sticky")
