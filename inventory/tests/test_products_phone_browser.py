"""« Produits & charges » on a phone, in a real (headless) Chrome: 375 × 812,
touch, a dozen invented products waiting to be classified (30/09).

The owner's words, reviewing the app on his phone: « I cannot scroll down
when I have new products to classify », and the tables' « columns
overlapping and hard to read ». What was measured behind them at 375 px
(UX review, 30/09), and what each test here holds down:

* **The scroll trap.** Under 1280 px the panel « À classer » sits above the
  list, and it kept `overflow-y: auto` and `overscroll-behavior: contain`
  with nothing left to scroll inside it: Chrome takes such a box for a wall.
  A wheel over it moved nothing, and on a phone, where the panel was the
  whole screen, neither did a finger. A wheel and a finger over a card now
  move the page - on a phone, and in a laptop's window of 1100 px, where
  the panel is stacked the same way. Beside the list (1400 px) it is still
  the sticky column that scrolls on its own.
* **One product at a time.** Fifty whole cards came before the first
  article. The first is drawn; « Voir les 11 autres produits » unfolds the
  rest - with a finger and with the keyboard -, where the reader can see
  them, keeps them unfolded from one « Classer » to the next, and folds
  them back. « La liste ↓ » and « ↑ À classer » lead between the panel and
  the list, which are a screen apart; the second goes once nothing waits.
* **Cards, not columns.** Seven columns squeezed into 351 px printed over
  one another; between two counts ten of them scrolled sideways, and an
  article's purchases scrolled sideways inside that. Each row is a card
  now: every figure under its label, no cell spilling over the next, every
  button inside its card and a thumb's size. Measured cell by cell, and
  button by button against its card's box, because a table clips what
  overflows it: a check on the box around it passes while « Retirer » is
  cut off. The page's buttons and figures, two to a row.
* **Never wider than the phone.** The period's select, as wide as its
  longest stock take's note, made the page 551 px on a 375 px phone -
  Chrome then zooms out sideways and the sticky bar drifts off. At 375 and
  320 px, with a note of some 120 characters and a product whose name is
  one 48-letter word.
* **Classifying on a phone leaves the reader at the panel.** The next field
  was focused (the keyboard up over the list) and the page scrolled down
  to the article, screens away from the next product. Neither now - upright,
  and on its side, wider than 860 px -; the row still opens, and « classé
  dans … » takes the reader to it when asked. The toolbar's « Produits à
  classer » brings the panel to the reader instead of focusing a field out
  of sight. A touch screen's question, not a width's: a mouse's window of
  700 px (a laptop at 150 % zoom) still goes on to the next product's
  field. And the box « Voir les N autres produits » keeps that name ticked.
* **Beside the list, nothing changed.** At 1400 px, a mouse: the sticky
  panel with every card, the table with its header row, the next field
  focused and the article scrolled into sight.

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver (webdriver-manager looks the latest one up online).
Skipped where Chrome or its driver is missing. Data invented.
"""

import tempfile
import time
from datetime import date, datetime
from decimal import Decimal

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse
from django.utils import timezone

from inventory.models import Product, UnitChoices
from invoices.scrapers import website
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from tests.runner import log_in_the_browser

WIDTH, HEIGHT = 375, 812
WAITING = 12

#: A name printed as one word, as some invoices do: no space to break at.
LONG_WORD = "APERITIFEXEMPLESANSAUCUNESPACEPOURLECOUPERENDEUX"
#: A stock take's note as long as a sentence: the period's select was as
#: wide as its longest option.
LONG_NOTE = (
    "Comptage de fin de trimestre exemple, cave et réserve comprises, "
    "bouteilles entamées pesées une à une avant la fermeture"
)

#: Every drawn row of a table read as cards (`arguments[0]`, a selector),
#: measured: a card is a grid; every figure drawn under its label (the
#: header row is not drawn: a figure without its label is a number nobody
#: can place); no cell's content is wider than the cell (a figure run over
#: its neighbour); every link, button and field inside its card's box, and
#: the card inside the screen.
MEASURE_CARDS = r"""
var rows = Array.from(document.querySelectorAll(arguments[0])).filter(function (row) {
    return row.getClientRects().length > 0;
});
var screen = document.documentElement.clientWidth;
var found = {rows: rows.length, not_grid: [], unlabelled: [], spilling: [], outside: []};
function box(r) { return [r.left, r.right, r.top, r.bottom].map(Math.round).join(","); }
rows.forEach(function (row) {
    var name = row.querySelector("td").textContent.trim().replace(/\s+/g, " ").slice(0, 40);
    if (getComputedStyle(row).display !== "grid") found.not_grid.push(name + ": " + getComputedStyle(row).display);
    var card = row.getBoundingClientRect();
    if (card.left < -1 || card.right > screen + 1) found.outside.push(name + ": the card " + box(card) + " is off the screen");
    Array.from(row.children).forEach(function (cell) {
        if (!cell.getClientRects().length) return;
        if (cell.dataset.label !== undefined) {
            var drawn = getComputedStyle(cell, "::before").content;
            if (drawn !== JSON.stringify(cell.dataset.label)) found.unlabelled.push(name + " / " + cell.dataset.label + ": " + drawn);
        }
        if (cell.scrollWidth > cell.clientWidth + 1) {
            found.spilling.push(name + " / " + (cell.dataset.label || cell.className || "?") + ": " + cell.scrollWidth + " > " + cell.clientWidth);
        }
    });
    row.querySelectorAll("a, button, input:not([type=hidden]), select").forEach(function (control) {
        if (!control.getClientRects().length) return;
        var r = control.getBoundingClientRect();
        if (r.left < card.left - 1 || r.right > card.right + 1 || r.top < card.top - 1 || r.bottom > card.bottom + 1) {
            found.outside.push(name + " / " + (control.textContent.trim() || control.name || control.tagName) + ": " + box(r) + " not in " + box(card));
        }
    });
});
return found;
"""

#: A point over the first card's own words (never a field or a link), the
#: page scrolled so that it sits `arguments[0]` of the way down the screen;
#: `room`: how far the page could still scroll down from there.
POINT_IN_THE_PANEL = r"""
var card = document.querySelector("#review-panel-body .review-card");
var words = card.querySelector(".review-card-head strong") || card;
var r = words.getBoundingClientRect();
window.scrollBy(0, r.top + r.height / 2 - window.innerHeight * arguments[0]);
r = words.getBoundingClientRect();
var x = Math.round(r.left + Math.min(r.width / 2, 30)), y = Math.round(r.top + r.height / 2);
var hit = document.elementFromPoint(x, y);
return {
    x: x, y: y,
    over_the_panel: !!(hit && hit.closest("#a-classer") && !hit.closest("a, button, input, select, textarea, label")),
    panel: Math.round(document.getElementById("a-classer").getBoundingClientRect().height),
    room: document.documentElement.scrollHeight - window.scrollY - window.innerHeight
};
"""


@tag("browser")
class ProductsOnAPhoneInBrowserTests(StaticLiveServerTestCase):
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
        self.supplier = make_supplier(code="GROSSISTE_TEL", name="Grossiste Exemple")
        self.articles = {}
        for name, unit, category, raw_name, equivalent, quantity, total_ht in (
            ("Farine Exemple", UnitChoices.KILOGRAM, "Cuisine", "FARINE EXEMPLE SAC 25KG", "25", 4, "63.20"),
            ("Gin Exemple", UnitChoices.LITRE, "Spiritueux", "GIN EXEMPLE 70CL", "0.7", 6, "95.40"),
            (
                "Liqueur ambrée grillée exemple",
                UnitChoices.LITRE,
                "Spiritueux",
                "LIQUEUR AMBREE EXEMPLE 70CL",
                "0.7",
                3,
                "41.85",
            ),
            # Figures as wide as a bar's year of them.
            ("Vodka Exemple", UnitChoices.LITRE, "Spiritueux", "VODKA EXEMPLE 70CL", "0.7", 240, "12345.67"),
        ):
            article = make_stock_type(name=name, unit=unit, category=category)
            product = make_product(
                supplier=self.supplier,
                raw_name=raw_name,
                stock_type=article,
                stock_equivalent=equivalent,
            )
            invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, 10))
            line = make_invoice_line(invoice=invoice, product=product, quantity=quantity, total_ht=total_ht)
            make_movement(
                stock_type=article,
                invoice_line=line,
                quantity=Decimal(equivalent) * quantity,
                unit_cost_ht="1.00",
            )
            self.articles[name] = article
        # Waiting in the panel, one purchase each.
        for number in range(1, WAITING + 1):
            product = make_product(supplier=self.supplier, raw_name=f"PRODUIT EXEMPLE {number:02d}")
            make_invoice_line(
                invoice=make_invoice(supplier=self.supplier, invoice_date=date(2026, 2, number)),
                product=product,
                quantity=2,
                total_ht="18",
            )
        # The charges: a water bill, and a landlord whose statement names
        # two charge items (rows of their own under his).
        water = make_supplier(code="EAU_TEL", name="Eau Exemple", parser_key="", expenses_only=True)
        tap_water = make_product(supplier=water, raw_name="EAU", is_expense=True)
        for month in (1, 2):
            bill = make_invoice(supplier=water, invoice_date=date(2026, month, 5), invoice_number=f"EAU-2026-0{month}")
            make_invoice_line(invoice=bill, product=tap_water, total_ht="40", vat_rate="0.055")
        landlord = make_supplier(code="BAIL_TEL", name="Bailleur Exemple", parser_key="", expenses_only=True)
        statement = make_invoice(supplier=landlord, invoice_date=date(2026, 2, 3), invoice_number="BAIL-2026-02")
        for charge_item, amount in (("LOYER EXEMPLE", "500"), ("PROVISIONSURCHARGESEXEMPLE", "80")):
            product = make_product(supplier=landlord, raw_name=charge_item, is_expense=True)
            make_invoice_line(
                invoice=statement, product=product, raw_name=charge_item, total_ht=amount, vat_rate="0.20"
            )

        log_in_the_browser(self.driver, self.live_server_url)
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})
        self.as_a_phone()
        self.first_visit()

    # -- helpers ------------------------------------------------------------------------------------

    def as_a_phone(self, width=WIDTH, height=HEIGHT):
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": True},
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})

    def as_a_window(self, width, height):
        """A computer's window: a mouse, no touch screen."""
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})

    def open(self, query=""):
        self.driver.get(self.live_server_url + reverse("inventory:stock_list") + query)
        self.wait_for(
            lambda: self.script("return document.readyState === 'complete' && !!document.getElementById('catalogue');")
        )

    def first_visit(self, query=""):
        """The page as a first visit draws it. What is open, what is
        folded, is remembered per browser - and the browser is shared by
        the whole class. Storage needs an origin: the page comes first."""
        self.open(query)
        self.script("localStorage.clear(); sessionStorage.clear();")
        self.open(query)

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition, seconds=10, message=""):
        """`message`: what the page failed to do, said by the timeout."""
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, seconds).until(lambda driver: condition(), message)

    def settled_scroll(self) -> float:
        """window.scrollY once it holds still (a wheel's scroll is animated)."""
        last, deadline = self.script("return window.scrollY;"), time.monotonic() + 4
        while time.monotonic() < deadline:
            time.sleep(0.25)
            now = self.script("return window.scrollY;")
            if now == last:
                return now
            last = now
        return last

    def tap(self, css):
        """A finger's tap in the middle of the element, as the phone sends it."""
        self.script("document.querySelector(arguments[0]).scrollIntoView({block: 'center'});", css)
        r = self.script(
            "var r = document.querySelector(arguments[0]).getBoundingClientRect();"
            "return {x: r.left + r.width / 2, y: r.top + r.height / 2};",
            css,
        )
        self.driver.execute_cdp_cmd(
            "Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": r["x"], "y": r["y"]}]}
        )
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def drawn(self, css) -> bool:
        return self.script(
            "var element = document.querySelector(arguments[0]);"
            "return !!element && element.getClientRects().length > 0;",
            css,
        )

    def in_sight_under_the_bar(self, css) -> bool:
        """The element's top on the screen's upper half, under the sticky
        bar - where a link to it has to land."""
        return self.script(
            "var r = document.querySelector(arguments[0]).getBoundingClientRect();"
            "var bar = document.querySelector('.topbar').getBoundingClientRect().bottom;"
            "return r.top >= bar - 1 && r.top < window.innerHeight / 2;",
            css,
        )

    def cards_drawn(self) -> int:
        return self.script(
            "return Array.from(document.querySelectorAll('#review-panel-body .review-card'))"
            ".filter(function (card) { return card.getClientRects().length > 0; }).length;"
        )

    def unfold_says(self) -> str:
        """What the way to the other cards reads, as drawn ('' where it is not)."""
        return self.script(
            "var label = document.querySelector('label[for=\"review-unfold\"]');"
            "return label && label.getClientRects().length ? label.innerText.trim() : '';"
        )

    def unfold(self):
        """Every card drawn - through the panel's own label where the panel
        folds (and nothing to do where every card already is)."""
        if self.cards_drawn() < WAITING:
            self.script("document.querySelector('label[for=\"review-unfold\"]').click();")
            self.wait_for(lambda: self.cards_drawn() == WAITING)

    def open_every_category(self):
        self.script(
            "document.querySelectorAll('#catalogue .category-header').forEach(function (header) {"
            " var table = document.getElementById(header.dataset.toggle); if (table && table.hidden) header.click(); });"
        )
        self.wait_for(
            lambda: self.script(
                "return Array.from(document.querySelectorAll('#catalogue table.stock-table'))"
                ".every(function (table) { return !table.hidden; });"
            )
        )

    def open_the_charges(self):
        self.script("var fold = document.querySelector('details.charges'); fold.open = true;")
        self.wait_for(
            lambda: self.script("return document.querySelector('.charges-table tbody tr').getClientRects().length > 0;")
        )

    def assertTheCardsFit(self, selector, at_least=1):
        found = self.script(MEASURE_CARDS, selector)
        self.assertGreaterEqual(found["rows"], at_least, f"{selector}: nothing drawn")
        self.assertEqual(found["not_grid"], [], f"{selector}: not read as cards")
        self.assertEqual(found["unlabelled"], [], f"{selector}: a figure drawn without its label")
        self.assertEqual(found["spilling"], [], f"{selector}: a cell's content runs over its neighbour")
        self.assertEqual(found["outside"], [], f"{selector}: a card or a control off its box")

    def assertTheRowsButtonsAreThumbSized(self):
        heights = self.script(
            "var found = [];"
            "document.querySelectorAll('#catalogue .stock-table .stock-row .row-actions').forEach(function (cell) {"
            " if (!cell.getClientRects().length) return;"
            " cell.querySelectorAll('a, button').forEach(function (control) {"
            "  found.push([control.textContent.trim(), control.getBoundingClientRect().height]); }); });"
            "return found;"
        )
        self.assertTrue(heights)
        self.assertEqual({name for name, _height in heights}, {"Modifier", "📈", "Supprimer"})
        self.assertEqual([(name, height) for name, height in heights if height < 44], [])

    def assertThePageFitsTheScreen(self, width):
        inner, scroll = self.script("return [window.innerWidth, document.documentElement.scrollWidth];")
        self.assertEqual(inner, width, "Chrome widened the page's viewport: something is wider than the phone")
        self.assertLessEqual(scroll, width)

    def point_in_the_panel(self, down=0.5):
        point = self.script(POINT_IN_THE_PANEL, down)
        self.assertTrue(point["over_the_panel"], point)
        # Or the gesture proves nothing: a page with nothing left below.
        self.assertGreater(point["room"], 500, point)
        return point

    def assertTheWheelMovesThePage(self, where):
        point = self.point_in_the_panel()
        before = self.settled_scroll()
        self.driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": point["x"], "y": point["y"], "deltaX": 0, "deltaY": 400},
        )
        after = self.settled_scroll()
        self.assertGreater(
            after - before, 100, f"{where}: the page stayed at {before:.0f} px - the panel held the wheel"
        )

    def swipe_up(self, x, y, distance):
        """A finger put down, drawn up `distance` px in ten moves, lifted.
        Touch events by hand: Input.synthesizeScrollGesture moves nothing
        in this headless Chrome, anywhere on the page (measured 30/09), and
        a test on it would pass over the very trap it is for."""
        self.driver.execute_cdp_cmd(
            "Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]}
        )
        for step in range(1, 11):
            self.driver.execute_cdp_cmd(
                "Input.dispatchTouchEvent",
                {"type": "touchMove", "touchPoints": [{"x": x, "y": y - distance * step / 10}]},
            )
            time.sleep(0.016)
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    # -- the scroll trap ----------------------------------------------------------------------------

    def test_the_wheel_over_the_panel_scrolls_the_page(self):
        """How the trap was measured on 30/09: the wheel over the stacked
        panel's cards, and the page held still. Unfolded, as it was then:
        a panel taller than the screen, nothing but cards under the
        pointer."""
        self.unfold()
        self.assertTheWheelMovesThePage("375 px")

    def test_a_finger_over_the_panel_scrolls_the_page(self):
        """« I cannot scroll down when I have new products to classify »:
        on a phone the panel was the screen, and a finger on it moved
        nothing."""
        self.unfold()
        # Low on the screen, room above it for the finger to travel.
        point = self.point_in_the_panel(down=0.75)
        before = self.settled_scroll()
        self.swipe_up(point["x"], point["y"], 400)
        after = self.settled_scroll()
        self.assertGreater(after - before, 100, f"the page stayed at {before:.0f} px - the panel held the finger")

    def test_in_a_window_under_1280_px_the_wheel_scrolls_the_page_too(self):
        """A laptop's window, a mouse: under 1280 px the panel sits above
        the list the same way, and the same wheel stopped on it."""
        self.as_a_window(1100, 900)
        self.open()
        self.assertEqual(
            self.script("return getComputedStyle(document.getElementById('a-classer')).position;"), "static"
        )
        self.unfold()
        self.assertTheWheelMovesThePage("1100 px")

    def test_beside_the_list_the_panel_is_as_it_was(self):
        """From 1280 px the panel is the sticky column beside the list,
        scrolling in its own box, every product drawn, no fold to open."""
        self.as_a_window(1400, 900)
        self.open()
        position, overflow, overscroll = self.script(
            "var s = getComputedStyle(document.getElementById('a-classer'));"
            "return [s.position, s.overflowY, s.overscrollBehaviorY];"
        )
        self.assertEqual((position, overflow, overscroll), ("sticky", "auto", "contain"))
        self.assertEqual(self.cards_drawn(), WAITING)
        self.assertEqual(self.unfold_says(), "")
        self.assertEqual(
            self.script(
                "return Array.from(document.querySelectorAll('.review-panel-jump'))"
                ".filter(function (link) { return link.getClientRects().length > 0; }).length;"
            ),
            0,
        )

    # -- one product at a time ----------------------------------------------------------------------

    def test_the_panel_draws_one_product_then_the_others_on_demand(self):
        """Above the list, every card came before the first article -
        screens of them. One is drawn, and a finger's tap on the label
        under it draws the others, then folds them back."""
        self.assertEqual(self.cards_drawn(), 1)
        self.assertEqual(self.unfold_says(), f"Voir les {WAITING - 1} autres produits")

        self.tap('label[for="review-unfold"]')
        self.wait_for(lambda: self.cards_drawn() == WAITING)
        self.assertEqual(self.unfold_says(), "N'afficher que le premier")

        self.tap('label[for="review-unfold"]')
        self.wait_for(lambda: self.cards_drawn() == 1)
        self.assertEqual(self.unfold_says(), f"Voir les {WAITING - 1} autres produits")

    def test_unfolding_shows_the_products_it_unfolds(self):
        """« Voir les 11 autres produits » is tapped under the first card,
        and the second is drawn right there - on the screen. Measured on
        30/09, it was not: Chrome keeps the page still around a scroll
        anchor taken in the DOCUMENT's order, where the list
        (.products-main) comes before the panel it is drawn under
        (`order: -1`). Kept in place, the list carried the page 4 000 px
        down, past the eleven cards just drawn: the label under the finger
        again, now « N'afficher que le premier », and not one of the
        products asked for on the screen. Folded back from the foot of the
        twelfth, the first product is on the screen again."""
        label = 'label[for="review-unfold"]'
        self.tap(label)
        self.wait_for(lambda: self.cards_drawn() == WAITING)
        self.settled_scroll()
        top, screen = self.script(
            "var r = document.querySelectorAll('#review-panel-body .review-card')[1].getBoundingClientRect();"
            "return [r.top, window.innerHeight];"
        )
        self.assertTrue(0 <= top < screen, f"the second product is drawn at {top:.0f} px, off a screen of {screen}")

        self.tap(label)
        self.wait_for(lambda: self.cards_drawn() == 1)
        self.settled_scroll()
        self.assertTrue(
            self.script(
                "var r = document.querySelector('#review-panel-body .review-card').getBoundingClientRect();"
                "return r.bottom > 0 && r.top < window.innerHeight;"
            ),
            "folded back, the first product is off the screen",
        )

    def test_the_keyboard_unfolds_them_too(self):
        """Out of sight, the box is still the control a keyboard reaches."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        self.script("document.getElementById('review-unfold').focus();")
        self.assertEqual(self.script("return document.activeElement.id;"), "review-unfold")
        ActionChains(self.driver).send_keys(Keys.SPACE).perform()
        self.wait_for(lambda: self.cards_drawn() == WAITING)

    def test_the_box_s_name_says_what_ticking_it_shows(self):
        """Named from its label, whose words flip with it, the box read
        « N'afficher que le premier, case à cocher, cochée » once ticked -
        « only the first is shown: on » - with every card drawn (review of
        30/09). Its name, as Chrome computes it for a screen reader, is
        « Voir les N autres produits » ticked or not: « cochée » then means
        the others are shown."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        name = f"Voir les {WAITING - 1} autres produits"
        box = self.driver.find_element("id", "review-unfold")
        self.assertEqual((box.accessible_name, box.is_selected()), (name, False))
        self.script("document.getElementById('review-unfold').focus();")
        ActionChains(self.driver).send_keys(Keys.SPACE).perform()
        self.wait_for(lambda: self.cards_drawn() == WAITING)
        self.assertEqual(self.unfold_says(), "N'afficher que le premier")
        box = self.driver.find_element("id", "review-unfold")
        self.assertEqual((box.accessible_name, box.is_selected()), (name, True))

    def test_classifying_leaves_the_others_unfolded(self):
        """Every « Classer » swaps the panel's body; the box that unfolds it
        sits outside, so the reader working down the list keeps it unfolded
        from one product to the next - and the label, swapped with the
        cards, counts what is left."""
        self.unfold()
        self.classify_the_first_as("Vodka Exemple")
        self.assertEqual(self.cards_drawn(), WAITING - 1)
        self.assertEqual(self.unfold_says(), "N'afficher que le premier")

        self.tap('label[for="review-unfold"]')
        self.wait_for(lambda: self.cards_drawn() == 1)
        self.assertEqual(self.unfold_says(), f"Voir les {WAITING - 2} autres produits")

    # -- the ways between the panel and the list ----------------------------------------------------

    def test_the_ways_between_the_panel_and_the_list(self):
        """Above the list, the panel and the articles are a screen apart:
        « La liste ↓ » under the panel's title goes past it, the toolbar's
        « ↑ À classer » back up to it - each landing under the sticky bar,
        not behind it."""
        self.assertTrue(self.drawn(".review-panel-head .review-panel-jump"), "no « La liste ↓ » in the panel's head")
        self.assertFalse(self.in_sight_under_the_bar("#articles"))
        self.tap(".review-panel-head .review-panel-jump")
        self.wait_for(
            lambda: self.in_sight_under_the_bar("#articles"), message="« La liste ↓ » did not land on the list"
        )

        self.assertFalse(self.in_sight_under_the_bar("#a-classer"))
        self.tap(".table-toolbar [data-panel-jump]")
        self.wait_for(
            lambda: self.in_sight_under_the_bar("#a-classer"), message="« ↑ À classer » did not land on the panel"
        )

    def test_the_toolbars_button_brings_the_panel_back_to_the_reader(self):
        """« Masquer » folds the panel away; the toolbar's « Produits à
        classer » opens it again - above the list on a phone, over the
        reader's head, where focusing its first field (as beside the list)
        brought the keyboard up out of sight. The panel is brought to the
        reader instead, the keyboard left down."""
        self.tap("#review-panel-body .review-panel-head [data-panel-close]")
        self.wait_for(lambda: not self.drawn("#a-classer"))

        self.tap("button[data-panel-open]")
        self.wait_for(lambda: self.drawn("#a-classer"))
        self.settled_scroll()
        self.assertFalse(self.a_panel_field_is_focused(), "a field of the panel was focused: the keyboard comes up")
        self.assertTrue(self.in_sight_under_the_bar("#a-classer"), "the panel opened out of sight")
        self.assertFalse(self.drawn("button[data-panel-open]"))

    def test_the_way_up_goes_once_nothing_is_left(self):
        """The last product classified, the panel says « Tout est classé »
        and the toolbar's « ↑ À classer » has nothing left to lead to: it
        goes (syncPanel - the toolbar is not part of what reloads)."""
        Product.objects.filter(stock_type__isnull=True, is_expense=False).exclude(
            raw_name="PRODUIT EXEMPLE 01",
        ).update(stock_type=self.articles["Gin Exemple"])
        self.open()
        self.assertEqual(self.cards_drawn(), 1)
        # One product: nothing to unfold.
        self.assertEqual(self.unfold_says(), "")
        self.assertTrue(self.drawn(".table-toolbar [data-panel-jump]"), "no « ↑ À classer » in the toolbar")

        self.classify_the_first_as("Vodka Exemple")
        self.assertTrue(self.drawn("#review-panel-body .review-panel-done"))
        self.wait_for(
            lambda: not self.drawn(".table-toolbar [data-panel-jump]"),
            message="« ↑ À classer » still offered with nothing left to classify",
        )

    # -- the page's head ----------------------------------------------------------------------------

    def test_the_head_and_the_figures_two_to_a_row(self):
        """The page's buttons wrapped into ragged rows of three widths and
        the headline figures came one to a row, some 200 px each, before
        anything to act on. Two to a row now, each button as wide as its
        neighbour and a thumb's height. (The rule is written under
        .page-header in the stylesheet: `.page-header .actions` alone
        outweighs a lone class, and a first draft drew the buttons one to
        a line.)"""
        head = self.script(
            "var row = document.querySelector('.page-header').getBoundingClientRect();"
            "return {left: row.left, right: row.right, buttons:"
            " Array.from(document.querySelectorAll('.page-header .actions .btn')).map(function (b) {"
            "  var r = b.getBoundingClientRect();"
            "  return {top: r.top, left: r.left, right: r.right, width: r.width, height: r.height}; })};"
        )
        buttons = head["buttons"]
        self.assertGreaterEqual(len(buttons), 3)
        first, second, third = buttons[:3]
        self.assertAlmostEqual(first["top"], second["top"], delta=1, msg="the first two buttons are not side by side")
        # A column each, across the page: not two buttons of their own widths
        # wrapped wherever the line ran out.
        self.assertAlmostEqual(first["left"], head["left"], delta=1, msg="the buttons do not start at the page's edge")
        self.assertAlmostEqual(second["right"], head["right"], delta=1, msg="the first two buttons do not fill the row")
        self.assertAlmostEqual(first["width"], second["width"], delta=1, msg="two buttons of two widths")
        self.assertAlmostEqual(
            third["left"], first["left"], delta=1, msg="the third button does not start a second row"
        )
        self.assertGreater(third["top"], first["top"] + 1, "the third button does not start a second row")
        self.assertEqual([button["height"] for button in buttons if button["height"] < 44], [])

        figures = self.script(
            "return Array.from(document.querySelectorAll('#stock-stats > .stat')).map(function (s) {"
            " var r = s.getBoundingClientRect(); return {top: r.top, right: r.right}; });"
        )
        self.assertGreaterEqual(len(figures), 2)
        self.assertAlmostEqual(figures[0]["top"], figures[1]["top"], delta=1, msg="one figure to a row")
        self.assertLessEqual(figures[1]["right"], self.script("return document.documentElement.clientWidth;"))

    # -- cards, not columns -------------------------------------------------------------------------

    def test_an_article_reads_as_a_card(self):
        """« ARTICLEACHETÉVENDU TOTALUNITÉ TTC »: the header of seven
        columns squeezed into a phone. Every category opened, each article
        is a card, its header row not drawn, nothing over its neighbour,
        its three buttons inside it and a thumb's size."""
        self.open_every_category()
        self.assertTheCardsFit("#catalogue table.stock-table > tbody > tr.stock-row", at_least=4)
        self.assertEqual(
            self.script(
                "return Array.from(document.querySelectorAll('#catalogue table.stock-table > thead'))"
                ".map(function (head) { return getComputedStyle(head).display; });"
            ),
            ["none", "none"],
        )
        self.assertTheRowsButtonsAreThumbSized()
        self.assertThePageFitsTheScreen(WIDTH)

    def test_between_two_counts_an_article_is_a_card_too(self):
        """Ten columns there: they scrolled sideways in their own box."""
        opening = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 1, 1, 12, 0)))
        closing = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 1, 31, 12, 0)))
        for take, name, counted in (
            (opening, "Vodka Exemple", "12"),
            (closing, "Vodka Exemple", "150"),
            (closing, "Gin Exemple", "1.5"),  # absent from the opening count
        ):
            article = self.articles[name]
            make_stock_take_line(
                stock_take=take,
                product=None,
                stock_type=article,
                counted_quantity=counted,
                unit=article.unit,
            )
        self.open(f"?inventaire={closing.pk}")
        self.open_every_category()
        self.assertTheCardsFit("#catalogue table.stock-table-period > tbody > tr.stock-row", at_least=4)
        self.assertEqual(
            self.script(
                "return Array.from(document.querySelectorAll('#catalogue .stock-table-wrap'))"
                ".filter(function (wrap) { return wrap.scrollWidth > wrap.clientWidth + 1; }).length;"
            ),
            0,
            "a category still scrolls sideways",
        )
        self.assertTheRowsButtonsAreThumbSized()
        self.assertThePageFitsTheScreen(WIDTH)

    def test_an_articles_purchases_are_cards_that_fit(self):
        """The conversion and « Retirer » were off the phone, scrolled
        sideways inside a row itself too narrow; on one line « 1 produit =
        [ ] Kilogramme 💾 » was wider than a 320 px screen."""
        flour = self.articles["Farine Exemple"]
        for width in (WIDTH, 320):
            with self.subTest(width=width):
                self.as_a_phone(width)
                # The last width left the row open: a click would close it.
                self.first_visit()
                self.open_every_category()
                self.script(
                    "document.querySelector('#catalogue .stock-row[data-stock-type-id=\"' + arguments[0] + '\"] td').click();",
                    str(flour.pk),
                )
                purchases = f"#details-{flour.pk} .movements-table > tbody > tr"
                self.wait_for(
                    lambda rows=purchases: self.script("return !!document.querySelector(arguments[0]);", rows)
                )
                self.assertTheCardsFit(purchases)
                self.assertEqual(
                    sorted(
                        self.script(
                            "return Array.from(document.querySelectorAll(arguments[0] + ' button'))"
                            ".map(function (b) { return b.textContent.trim(); });",
                            purchases,
                        )
                    ),
                    ["Retirer", "💾"],
                )
                self.assertThePageFitsTheScreen(width)

    def test_the_charges_rows_fit(self):
        """The charges' fixed columns printed a date wider than its column
        on a phone. A supplier and his charge items - one a name with no space -
        as cards, « Documents (12 mois) » over its count."""
        self.open_the_charges()
        self.assertTheCardsFit("#catalogue .charges-table > tbody > tr.stock-row", at_least=4)
        self.assertEqual(
            self.script(
                "var cell = document.querySelector('#catalogue .charges-table > tbody > tr.stock-row > td[data-label]');"
                "return getComputedStyle(cell, '::before').content;"
            ),
            '"Documents (12 mois)"',
        )
        self.assertThePageFitsTheScreen(WIDTH)

    # -- never wider than the phone -----------------------------------------------------------------

    def test_the_page_is_never_wider_than_the_phone(self):
        """Wider than the screen, the page is zoomed out sideways and the
        sticky bar drifts off it. The period's select was as wide as its
        longest note; a product's name of one word, beside the panel's
        scroll box gone, has nothing to break at but its letters."""
        make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)), note=LONG_NOTE)
        waiting = make_product(supplier=self.supplier, raw_name=LONG_WORD)
        make_invoice_line(invoice=make_invoice(supplier=self.supplier), product=waiting, quantity=1, total_ht="9")
        for width in (WIDTH, 320):
            with self.subTest(width=width):
                self.as_a_phone(width)
                self.open()
                # The long word is the first product (they come by name).
                self.assertIn(
                    LONG_WORD,
                    self.script("return document.querySelector('#review-panel-body .review-card').textContent;"),
                )
                self.assertThePageFitsTheScreen(width)

    # -- classifying on a phone ---------------------------------------------------------------------

    def classify_the_first_as(self, name):
        self.script(
            "var input = document.querySelector('#review-panel-body .js-stock-type-name');"
            "input.value = arguments[0];"
            "input.dispatchEvent(new Event('input', { bubbles: true }));"
            "input.closest('form').querySelector('button[type=\"submit\"]').click();",
            name,
        )
        self.wait_for(lambda: self.script("return !!document.querySelector('#review-panel-body .classified-note');"))

    def a_panel_field_is_focused(self) -> bool:
        return self.script(
            "var active = document.activeElement;"
            "return !!(active && active.closest('#review-panel-body') && active.matches('input, select, textarea'));"
        )

    def row_on_screen(self, article) -> bool:
        return self.script(
            "var row = document.querySelector('#catalogue .stock-row[data-stock-type-id=\"' + arguments[0] + '\"]');"
            "if (!row || !row.getClientRects().length) return false;"
            "var r = row.getBoundingClientRect(), bar = document.querySelector('.topbar').getBoundingClientRect().bottom;"
            "return r.top >= bar - 1 && r.bottom <= window.innerHeight + 1;",
            str(article.pk),
        )

    def details_open(self, article) -> bool:
        return self.script(
            "var details = document.getElementById('details-' + arguments[0]);"
            "return !!details && !details.hidden && details.getClientRects().length > 0"
            " && !!details.querySelector('.movements-table');",
            str(article.pk),
        )

    def test_classifying_on_a_phone_leaves_the_reader_at_the_panel(self):
        """The next field focused brought the keyboard up over the list,
        and the page was taken down to the article, screens away from the
        next product to classify (UX review, 30/09)."""
        vodka = self.articles["Vodka Exemple"]
        self.script("document.querySelector('#review-panel-body .review-card-form').scrollIntoView({block: 'center'});")
        before = self.settled_scroll()

        self.classify_the_first_as("Vodka Exemple")
        # The row still opens on where the product went…
        self.wait_for(lambda: self.details_open(vodka))
        after = self.settled_scroll()
        # … but nothing pulls the reader away from the panel.
        self.assertFalse(self.a_panel_field_is_focused(), "a field of the panel was focused: the keyboard comes up")
        self.assertLess(abs(after - before), 150, f"the page moved from {before:.0f} to {after:.0f} px")
        self.assertTrue(
            self.script(
                "var field = document.querySelector('#review-panel-body .js-stock-type-name').getBoundingClientRect();"
                "return field.top >= 0 && field.bottom <= window.innerHeight;"
            ),
            "the next product's field is off the screen",
        )

        # « classé dans … » takes the reader to it, when asked.
        self.tap("#review-panel-body .classified-note a[data-reveal-stock-type]")
        self.wait_for(lambda: self.row_on_screen(vodka), message="« classé dans … » did not bring the row into sight")

    def test_a_phone_on_its_side_keeps_the_keyboard_down_too(self):
        """A phone held sideways is wider than 860 px - 915 × 412 here - and
        the panel is still above the list: whether a keyboard comes up is
        the touch screen's question, not the width's (KEYBOARD_ON_SCREEN).
        Keyed on the width alone, the next field was focused there."""
        self.as_a_phone(915, 412)
        self.open()
        self.assertTrue(
            self.script("return matchMedia('(hover: none) and (pointer: coarse)').matches;"),
            "no touch screen emulated",
        )
        self.assertEqual(
            self.script("return getComputedStyle(document.getElementById('a-classer')).position;"), "static"
        )
        vodka = self.articles["Vodka Exemple"]
        self.script(
            "document.querySelector('#review-panel-body .js-stock-type-name').scrollIntoView({block: 'center'});"
        )
        before = self.settled_scroll()

        self.classify_the_first_as("Vodka Exemple")
        self.wait_for(lambda: self.details_open(vodka))
        after = self.settled_scroll()
        self.assertFalse(self.a_panel_field_is_focused(), "a field of the panel was focused: the keyboard comes up")
        self.assertLess(abs(after - before), 150, f"the page moved from {before:.0f} to {after:.0f} px")

    def test_in_a_narrow_window_a_keyboard_goes_on_to_the_next_product(self):
        """A computer's window under 860 px - a laptop at 150 % zoom is some
        853 px - has a mouse and a keyboard, and no keyboard comes up on its
        screen: after « Classer » the next product's field is focused, as at
        1100 px, and « Produits à classer » opens the panel on its first
        field. Asked of the width too (KEYBOARD_ON_SCREEN), both dropped the
        focus on the page, and the next Tab went past the next product
        (review of 30/09)."""
        self.as_a_window(700, 800)
        self.open()
        self.assertFalse(
            self.script("return matchMedia('(hover: none) and (pointer: coarse)').matches;"),
            "a touch screen emulated",
        )
        self.assertEqual(
            self.script("return getComputedStyle(document.getElementById('a-classer')).position;"), "static"
        )

        self.classify_the_first_as("Vodka Exemple")
        self.wait_for(
            lambda: self.a_panel_field_is_focused(), message="after « Classer », no field of the next product focused"
        )

        self.driver.find_element("css selector", "#review-panel-body .review-panel-head [data-panel-close]").click()
        self.wait_for(lambda: not self.drawn("#a-classer"))
        self.driver.find_element("css selector", "button[data-panel-open]").click()
        self.wait_for(lambda: self.drawn("#a-classer"))
        self.wait_for(
            lambda: self.a_panel_field_is_focused(), message="« Produits à classer » focused no field of the panel"
        )

    def test_beside_the_list_classifying_is_as_it_was(self):
        """1400 px, a mouse: the next field is focused for the next name,
        and the article the product went to is brought into sight. The list
        is made longer than the window first, the reader at its top: with
        the row already on the screen, « brought into sight » would pass
        with nothing scrolled at all."""
        vodka = self.articles["Vodka Exemple"]
        # Sorted before the spirits (category, then name): the vodka's row
        # goes below the fold.
        for number in range(1, 21):
            make_stock_type(name=f"Ingrédient exemple {number:02d}", unit=UnitChoices.KILOGRAM, category="Cuisine")
        self.as_a_window(1400, 900)
        self.open()
        self.open_every_category()
        self.script("window.scrollTo(0, 0);")
        self.assertTrue(
            self.script(
                "var row = document.querySelector('#catalogue .stock-row[data-stock-type-id=\"' + arguments[0] + '\"]');"
                "return row.getBoundingClientRect().top > window.innerHeight;",
                str(vodka.pk),
            ),
            "the vodka's row is on the screen already",
        )

        self.classify_the_first_as("Vodka Exemple")
        self.wait_for(lambda: self.details_open(vodka))
        self.wait_for(lambda: self.a_panel_field_is_focused(), message="the next product's field was not focused")
        self.wait_for(lambda: self.row_on_screen(vodka), message="the article was not brought into sight")

    def test_beside_the_list_an_article_is_a_row_of_the_table(self):
        """1400 px, a mouse: the list, the charges and the page's head are
        as they were - a table with its header row, no label drawn in a
        cell, the page's buttons on one line."""
        self.as_a_window(1400, 900)
        self.open()
        self.open_every_category()
        self.open_the_charges()
        found = self.script(
            "var rows = Array.from(document.querySelectorAll("
            " '#catalogue table.stock-table > tbody > tr.stock-row, #catalogue .charges-table > tbody > tr.stock-row'));"
            "return {"
            " rows: rows.length,"
            " displays: Array.from(new Set(rows.map(function (row) { return getComputedStyle(row).display; }))),"
            " heads: Array.from(new Set(Array.from(document.querySelectorAll("
            "  '#catalogue table.stock-table > thead, #catalogue .charges-table > thead'))"
            "  .map(function (head) { return getComputedStyle(head).display; }))),"
            " labels: rows.reduce(function (drawn, row) {"
            "  return drawn.concat(Array.from(row.querySelectorAll('td[data-label]'))"
            "   .map(function (cell) { return getComputedStyle(cell, '::before').content; })"
            "   .filter(function (content) { return content !== 'none' && content !== 'normal'; })); }, []),"
            " buttonTops: Array.from(new Set(Array.from(document.querySelectorAll('.page-header .actions .btn'))"
            "  .map(function (b) { return Math.round(b.getBoundingClientRect().top); })))"
            "};"
        )
        self.assertGreaterEqual(found["rows"], 8)
        self.assertEqual(found["displays"], ["table-row"])
        self.assertEqual(found["heads"], ["table-header-group"])
        self.assertEqual(found["labels"], [])
        self.assertEqual(len(found["buttonTops"]), 1, "the page's buttons wrapped")
