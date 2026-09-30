"""Searching the list while classifying a product, in a real (headless)
Chrome: the results stay a list.

« When linking a product bought in an invoice with a product in the
« Produits & charges » page, there are a lot of scrolling happening when
searching for an existing product » (owner, 20/09). Two reasons, both here:
classifying a product opened that article's purchases and **kept** it open
for good (revealStockType wrote it into this browser's storage), so a
session of classifying left a page of open panels; and a search showed
those panels among its results instead of the plain rows it is read for.

And the other half, five days later: « I want to be able to unroll the
article and see the products bought even if their name doesn't match the
search query » (owner, 25/09). Setting every panel aside for as long as a
query was typed answered that click with nothing at all - the query is how
an article is reached, and what it bought is the article's own business.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

import tempfile
from datetime import date

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from inventory.models import UnitChoices
from invoices.scrapers import website
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_type,
    make_supplier,
)
from tests.runner import log_in_the_browser

WAIT_SECONDS = 10


@tag("browser")
class CatalogueSearchInBrowserTests(StaticLiveServerTestCase):
    # Its flush then fires no post_migrate: recreated content types broke
    # every later class restoring its snapshot (tests/test_transaction_cases.py).
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
        # A computer's window, the list beside the panel: these tests are
        # about the desktop's table. Headless Chrome's own window is 800 ×
        # 600, under 860 px, where since 30/09 the rows are cards and the
        # panel shows one product at a time - the phone's page, which
        # test_products_phone_browser.py drives.
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1400, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.supplier = make_supplier(name="Grossiste Exemple")
        self.vodka = make_stock_type(name="Vodka Exemple", unit=UnitChoices.LITRE, category="Spiritueux")
        self.gin = make_stock_type(name="Gin Exemple", unit=UnitChoices.LITRE, category="Spiritueux")
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, 10))
        for stock_type, name in (
            (self.vodka, "VODKA EXEMPLE 70CL"),
            # Bought under the vodka and sharing not one letter with it: a
            # search for « vodka » finds the article, and this is what the
            # article has to open on.
            (self.vodka, "SMIRNOFF RED 70CL"),
            (self.gin, "GIN EXEMPLE 70CL"),
        ):
            classified = make_product(
                supplier=self.supplier,
                raw_name=name,
                stock_type=stock_type,
                unit=UnitChoices.UNIT,
                stock_equivalent="0.7",
            )
            line = make_invoice_line(invoice=invoice, product=classified, quantity=10, total_ht="120")
            # The row and the panel it opens both read the stock ledger, not
            # the invoice lines: without a movement the panel answers
            # « Aucun achat enregistré » over a product that was bought.
            make_movement(stock_type=stock_type, invoice_line=line, quantity=7, unit_cost_ht="12")
        # The one waiting in the panel, to be classified as the vodka.
        waiting = make_product(supplier=self.supplier, raw_name="VODKA EXEMPLE 1L")
        make_invoice_line(invoice=invoice, product=waiting, quantity=6, total_ht="90")
        # What is open is remembered per browser, and the browser is shared
        # by the whole class: a category another test left open is one this
        # one's « open it » click would close. Storage needs an origin, so
        # the page is loaded before it can be cleared. Logged in first, as
        # the test tenant's owner: every page wants a login.
        log_in_the_browser(self.driver, self.live_server_url)
        self.driver.get(self.live_server_url + reverse("inventory:stock_list"))
        self.script("localStorage.clear()")
        # The pin held, or every test below passes on the phone's cards and
        # nothing drives the desktop's table any more (they do pass there).
        self.assertEqual(self.script("return window.innerWidth;"), 1400)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def open_list(self):
        self.driver.get(self.live_server_url + reverse("inventory:stock_list"))
        self.wait_for(lambda: self.script("return !!document.getElementById('catalogue')"))

    def details_shown(self) -> list:
        """The purchases panels a reader sees, by article."""
        return self.script(
            "return Array.from(document.querySelectorAll('#catalogue tr[data-child-row]'))"
            ".filter(function (row) { return row.offsetParent !== null; })"
            ".map(function (row) { return row.id; })"
        )

    def visible_row_names(self) -> list:
        """The articles a reader sees, in the order the list draws them."""
        rows = self.script(
            "return Array.from(document.querySelectorAll('#catalogue .stock-row'))"
            ".filter(function (row) { return row.offsetParent !== null; })"
            ".map(function (row) { return row.textContent.trim().split('\\n')[0].trim(); })"
        )
        return [name.strip("▸▾ ").strip() for name in rows]

    def panel_products(self, stock_type) -> list:
        """The products listed by an article's open purchases panel - or
        None while that panel is not on screen."""
        return self.script(
            "var panel = document.getElementById('details-' + arguments[0]);"
            "if (!panel || panel.offsetParent === null) return null;"
            "return Array.from(panel.querySelectorAll('.movements-table tbody tr'))"
            ".map(function (row) { return row.querySelector('td').textContent.trim(); });",
            str(stock_type.pk),
        )

    def click_row(self, stock_type):
        self.script(
            "document.querySelector('#catalogue .stock-row[data-stock-type-id=\"' + arguments[0] + '\"]')"
            ".querySelector('td').click()",
            str(stock_type.pk),
        )

    def click_chart(self, stock_type):
        self.script(
            "document.querySelector('#catalogue .stock-row[data-stock-type-id=\"' + arguments[0] + '\"]')"
            ".querySelector('.js-price-history-toggle').click()",
            str(stock_type.pk),
        )

    def kept_open(self) -> list:
        # Under the key the page itself uses (its tenant's scope included).
        return sorted(self.script("return JSON.parse(localStorage.getItem(EXPANDED_STORAGE_KEY) || '[]')"))

    def search(self, text):
        self.script(
            "var box = document.getElementById('stock-search');"
            "box.value = arguments[0];"
            "box.dispatchEvent(new Event('input', { bubbles: true }));",
            text,
        )

    def test_classifying_does_not_leave_the_purchases_open_for_good(self):
        self.open_list()
        self.script(
            "var input = document.querySelector('#review-panel-body .js-stock-type-name');"
            "input.value = arguments[0];"
            "input.dispatchEvent(new Event('input', { bubbles: true }));"
            "input.closest('form').querySelector('button[type=\"submit\"]').click()",
            "Vodka Exemple",
        )
        # The purchases of the article it went to open, to be seen…
        self.wait_for(lambda: f"details-{self.vodka.pk}" in self.details_shown())
        # … but nothing is kept open behind the reader's back.
        self.assertNotIn(f"details-{self.vodka.pk}", self.kept_open())

        self.open_list()
        self.assertEqual(self.details_shown(), [])

    def open_category(self):
        """A category starts collapsed: its rows are reachable once open."""
        self.script("document.querySelector('#catalogue .category-header td, #catalogue .category-header').click()")
        self.wait_for(
            lambda: self.script(
                "return !!document.querySelector('#catalogue .stock-row') &&"
                " document.querySelector('#catalogue .stock-row').offsetParent !== null"
            )
        )

    def test_a_search_answers_with_rows_not_with_open_panels(self):
        self.open_list()
        self.open_category()
        self.script(
            f"var row = document.querySelector('#catalogue .stock-row[data-stock-type-id=\"{self.vodka.pk}\"]');"
            "row.querySelector('td').click()"
        )
        self.wait_for(lambda: f"details-{self.vodka.pk}" in self.details_shown())
        self.assertIn(f"details-{self.vodka.pk}", self.kept_open())  # their own choice, remembered

        self.search("vodka")
        self.wait_for(lambda: self.details_shown() == [])
        self.assertEqual(self.visible_row_names(), ["Vodka Exemple"])

        # Cleared, the page is as the reader left it: their own row open.
        self.search("")
        self.wait_for(lambda: self.details_shown() == [f"details-{self.vodka.pk}"])
        self.assertIn(f"details-{self.vodka.pk}", self.kept_open())

    def test_a_row_opened_while_searching_shows_every_product_it_bought(self):
        """« I want to be able to unroll the article and see the products
        bought even if their name doesn't match the search query » (owner,
        25/09).

        The search is how an article is reached; what that article bought is
        the article's own business. SMIRNOFF RED is filed under the vodka
        and shares not a letter with « vodka », and the panel is where one
        goes to find out that it is."""
        self.open_list()
        self.search("vodka")
        self.wait_for(lambda: self.visible_row_names() == ["Vodka Exemple"])

        self.click_row(self.vodka)
        self.wait_for(lambda: self.panel_products(self.vodka))
        self.assertEqual(
            sorted(self.panel_products(self.vodka)),
            ["SMIRNOFF RED 70CL", "VODKA EXEMPLE 70CL"],
        )

    def test_a_panel_the_search_set_aside_reopens_in_one_click(self):
        """Set aside is not closed. Read as closed, the first click would
        « close » what is already out of sight: the reader clicks, nothing
        happens, and they have to click a second time to see anything."""
        self.open_list()
        self.open_category()
        self.click_row(self.vodka)
        self.wait_for(lambda: f"details-{self.vodka.pk}" in self.details_shown())

        self.search("vodka")
        self.wait_for(lambda: self.details_shown() == [])

        self.click_row(self.vodka)  # once
        self.wait_for(lambda: self.details_shown() == [f"details-{self.vodka.pk}"])

    def test_a_curve_the_search_set_aside_reopens_in_one_click(self):
        """Same trap, the other panel a row opens."""
        self.open_list()
        self.open_category()
        self.click_chart(self.vodka)
        self.wait_for(lambda: f"price-history-{self.vodka.pk}" in self.details_shown())

        self.search("vodka")
        self.wait_for(lambda: self.details_shown() == [])

        self.click_chart(self.vodka)  # once
        self.wait_for(lambda: self.details_shown() == [f"price-history-{self.vodka.pk}"])

    def test_the_list_reloading_under_a_search_does_not_spill_its_panels(self):
        """The list replaces itself whole when a product is classified or
        sent back, reopens what it had open, and re-runs the search on the
        new rows. Which is why what is set aside is a CSS rule and a class
        on the body, and not a pass over the rows: written as a pass, the
        panels are reopened after the one that would have set them aside and
        sit among the results until the box is cleared (measured: the pass
        ran while htmx was still settling the swap)."""
        self.open_list()
        self.open_category()
        self.click_row(self.vodka)
        self.wait_for(lambda: f"details-{self.vodka.pk}" in self.details_shown())

        self.search("vodka")
        self.wait_for(lambda: self.details_shown() == [])

        self.script("document.getElementById('catalogue').dataset.before = '1'")
        self.script("document.body.dispatchEvent(new CustomEvent('catalogue-changed', { bubbles: true }))")
        # The list that comes back is a different element: the marker is gone.
        self.wait_for(lambda: not self.script("return !!document.getElementById('catalogue').dataset.before"))
        self.wait_for(lambda: self.visible_row_names() == ["Vodka Exemple"])
        self.assertEqual(self.details_shown(), [])
        # Still open, only set aside: one click brings it back.
        self.click_row(self.vodka)
        self.wait_for(lambda: self.details_shown() == [f"details-{self.vodka.pk}"])

    def test_what_a_row_has_open_leaves_with_the_row(self):
        """Narrowing the search drops an article from the results: what that
        article had open goes with it, or a curve sits among the results
        explaining a row that is no longer one of them."""
        self.open_list()
        self.search("exemple")
        self.wait_for(lambda: sorted(self.visible_row_names()) == ["Gin Exemple", "Vodka Exemple"])

        self.click_chart(self.gin)
        self.wait_for(lambda: f"price-history-{self.gin.pk}" in self.details_shown())

        self.search("vodka")
        self.wait_for(lambda: self.visible_row_names() == ["Vodka Exemple"])
        self.assertEqual(self.details_shown(), [])
