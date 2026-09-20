"""Searching the list while classifying a product, in a real (headless)
Chrome: the results stay a list.

« When linking a product bought in an invoice with a product in the
« Produits & charges » page, there are a lot of scrolling happening when
searching for an existing product » (owner, 20/09). Two reasons, both here:
classifying a product opened that article's purchases and **kept** it open
for good (revealStockType wrote it into this browser's storage), so a
session of classifying left a page of open panels; and a search showed
those panels among its results instead of the plain rows it is read for.

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
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type, make_supplier

WAIT_SECONDS = 10
EXPANDED_KEY = "marginmate:stock:rows"


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
        self.supplier = make_supplier(name="Grossiste Exemple")
        self.vodka = make_stock_type(name="Vodka Exemple", unit=UnitChoices.LITRE, category="Spiritueux")
        self.gin = make_stock_type(name="Gin Exemple", unit=UnitChoices.LITRE, category="Spiritueux")
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, 10))
        for stock_type, name in ((self.vodka, "VODKA EXEMPLE 70CL"), (self.gin, "GIN EXEMPLE 70CL")):
            classified = make_product(
                supplier=self.supplier, raw_name=name, stock_type=stock_type,
                unit=UnitChoices.UNIT, stock_equivalent="0.7",
            )
            make_invoice_line(invoice=invoice, product=classified, quantity=10, total_ht="120")
        # The one waiting in the panel, to be classified as the vodka.
        waiting = make_product(supplier=self.supplier, raw_name="VODKA EXEMPLE 1L")
        make_invoice_line(invoice=invoice, product=waiting, quantity=6, total_ht="90")

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

    def kept_open(self) -> list:
        return sorted(self.script("return JSON.parse(localStorage.getItem(arguments[0]) || '[]')", EXPANDED_KEY))

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
        self.wait_for(lambda: self.script(
            "return !!document.querySelector('#catalogue .stock-row') &&"
            " document.querySelector('#catalogue .stock-row').offsetParent !== null"
        ))

    def test_a_search_answers_with_rows_not_with_open_panels(self):
        self.open_list()
        self.open_category()
        self.script(
            "var row = document.querySelector('#catalogue .stock-row[data-stock-type-id=\"%s\"]');"
            "row.querySelector('td').click()" % self.vodka.pk
        )
        self.wait_for(lambda: f"details-{self.vodka.pk}" in self.details_shown())
        self.assertIn(f"details-{self.vodka.pk}", self.kept_open())  # their own choice, remembered

        self.search("vodka")
        self.wait_for(lambda: self.details_shown() == [])
        rows = self.script(
            "return Array.from(document.querySelectorAll('#catalogue .stock-row'))"
            ".filter(function (row) { return row.offsetParent !== null; })"
            ".map(function (row) { return row.textContent.trim().split('\\n')[0].trim(); })"
        )
        self.assertEqual([name.strip("▸▾ ").strip() for name in rows], ["Vodka Exemple"])

        # Cleared, the page is as the reader left it: their own row open.
        self.search("")
        self.wait_for(lambda: self.details_shown() == [f"details-{self.vodka.pk}"])
        self.assertIn(f"details-{self.vodka.pk}", self.kept_open())
