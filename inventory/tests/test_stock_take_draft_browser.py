"""A count kept in the browser, restored after the StockType became an
« article » (19/09) - in a real (headless) Chrome.

The inventory form mirrors every row into this browser's storage as it is
typed (stock_take_form.html, "the draft net"). A draft started before the
rename names its article « Vodka (type de stock) », a name the page's list
no longer holds: restored as it was, the row found no unit and no price, and
the running total waited on it for good. Only the page's script sees that;
the server side of the old name is in test_stock_take_form.py.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

import json
import tempfile
from datetime import date

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from inventory.models import UnitChoices
from invoices.scrapers import website
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type, make_supplier

WAIT_SECONDS = 10
DRAFT_KEY = "marginmate:stock-take-draft:new"


@tag("browser")
class OldDraftInBrowserTests(StaticLiveServerTestCase):
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
        supplier = make_supplier(name="Grossiste Exemple")
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        product = make_product(
            supplier=supplier, raw_name="VODKA EXEMPLE 70CL", stock_type=vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        # Ten 70cl bottles at 12 €.
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 1, 10))
        make_invoice_line(invoice=invoice, product=product, quantity=10, total_ht="120")

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def test_a_draft_from_before_the_rename_comes_back_under_the_new_name(self):
        url = self.live_server_url + reverse("inventory:stock_take_create")
        self.driver.get(url)
        draft = {
            "takenAt": "2026-03-31T12:00",
            "note": "",
            "rows": [{"entry": "Vodka (type de stock)", "quantity": "2.1", "unit": UnitChoices.LITRE}],
        }
        self.script(
            "var draft = JSON.parse(arguments[1]); draft.savedAt = Date.now();"
            "localStorage.setItem(arguments[0], JSON.stringify(draft));",
            DRAFT_KEY, json.dumps(draft),
        )
        self.driver.get(url)
        self.wait_for(lambda: self.script("return !document.getElementById('draft-offer').hidden"))
        self.script("document.getElementById('draft-restore').click()")

        entries = self.script(
            "return Array.from(document.querySelectorAll(\"#line-rows input[name$='-entry_search']\"))"
            ".map(function (input) { return input.value; }).filter(Boolean)"
        )
        self.assertEqual(entries, ["Vodka (article)"])
        # Priced like any row: 2.1 L is three of the 12 € bottles.
        self.wait_for(lambda: "36,00" in self.script("return document.getElementById('inventory-total').textContent"))
