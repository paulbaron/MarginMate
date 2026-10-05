"""The unit select that follows the name typed - the shopping list's add form
and the stock take's rows, one script (static/js/entry_units.js) - in a real
(headless) Chrome.

The owner, 04/10: « Dans la liste des courses, je voudrais pouvoir, comme
dans l'inventaire, rentrer en terme de bouteilles plutôt que litres ». The
list page ships every name it offers with its units (a json_script island,
inventory/entries.py's choices); entry_units.js fills the select as a name
is typed, and puts back what the server drew (« habituelle ») for a name it
does not know. The stock take's rows go through the same script: a name with
spaces around it now gets its units, and a name edited into an unknown one
gives the select its server-drawn options back. Only the page's script sees
that; what is stored is pinned in test_shopping_lists_page.py.

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver (webdriver-manager looks the latest one up online).
Skipped where Chrome or its driver is missing. Data invented
(tests.test_views_smoke.make_shopping_history and make_shopping_bottles).
"""

import tempfile

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from inventory.models import ShoppingListItem
from invoices.scrapers import website
from tests.runner import log_in_the_browser
from tests.test_views_smoke import SHOPPING_VODKA_PRODUCT, make_shopping_bottles, make_shopping_history

WAIT_SECONDS = 10
#: The shopping list's add form: its name field and its unit select.
NAME_FIELD = "form.shopping-add input[name='nom']"
UNIT_SELECT = "form.shopping-add select[name='unite']"
#: The stock take's first row (a new count starts with one, drawn by its script).
ROW_ENTRY = "#line-rows input[name$='-entry_search']"
ROW_UNIT = "#line-rows select[name$='-unit']"
#: A select's options as drawn: [value, words, selected].
OPTIONS = (
    "return Array.prototype.map.call(document.querySelector(arguments[0]).options,"
    " function (option) { return [option.value, option.textContent, option.selected]; });"
)


@tag("browser")
class EntryUnitsInBrowserTests(StaticLiveServerTestCase):
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
        self.made = make_shopping_history()
        self.bottles = make_shopping_bottles(self.made)
        log_in_the_browser(self.driver, self.live_server_url)
        # On this site's login page: the stock take's draft net keeps what
        # the last test typed.
        self.script("localStorage.clear(); sessionStorage.clear();")
        self.list_page = f"{reverse('inventory:shopping_list_page')}?fournisseur={self.made.wholesaler.pk}"

    # -- helpers ------------------------------------------------------------------------------------

    def open(self, path):
        self.driver.get(self.live_server_url + path)
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def element(self, css):
        return self.driver.find_element("css selector", css)

    def type_into(self, css, text):
        """`text` typed into the field, whatever it held: every key a real
        one, so the page hears each `input` as it would a person's."""
        from selenium.webdriver.common.keys import Keys

        field = self.element(css)
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(Keys.DELETE)
        field.send_keys(text)

    def options(self, css) -> list:
        return self.script(OPTIONS, css)

    # -- the shopping list ----------------------------------------------------------------------------

    def test_a_name_typed_fills_the_unit_select(self):
        """The vodka, never bought at the wholesaler's: its 70 cl bottle is
        the one bought at the grocer's, and an article in litres is counted
        in bottles by default."""
        self.open(self.list_page)
        self.assertEqual(self.options(UNIT_SELECT), [["", "habituelle", True]])
        self.type_into(NAME_FIELD, "Vodka exemple (article)")
        self.wait_for(lambda: len(self.options(UNIT_SELECT)) == 2)
        self.assertEqual(self.options(UNIT_SELECT), [["UNIT", "bouteilles de 70 cl", True], ["L", "litres", False]])

    def test_a_free_text_puts_habituelle_back(self):
        self.open(self.list_page)
        self.type_into(NAME_FIELD, "Gin exemple (article)")
        self.wait_for(lambda: len(self.options(UNIT_SELECT)) == 2)
        self.type_into(NAME_FIELD, "Pain exemple")
        self.wait_for(lambda: len(self.options(UNIT_SELECT)) == 1)
        self.assertEqual(self.options(UNIT_SELECT), [["", "habituelle", True]])
        # Another store's product is no name of this list's.
        self.type_into(NAME_FIELD, f"{SHOPPING_VODKA_PRODUCT} — Épicerie exemple")
        self.assertEqual(self.options(UNIT_SELECT), [["", "habituelle", True]])

    def test_a_name_typed_as_the_lists_always_took_it_fills_the_select_too(self):
        """« vodka exemple », typed rather than picked: no name of the menu,
        but one the server resolves to the vodka - the select follows it
        (the aliases' island), and litres chosen with 2 typed add « 2 L »."""
        self.open(self.list_page)
        self.type_into(NAME_FIELD, "vodka exemple")
        self.wait_for(lambda: len(self.options(UNIT_SELECT)) == 2)
        self.assertEqual(self.options(UNIT_SELECT), [["UNIT", "bouteilles de 70 cl", True], ["L", "litres", False]])
        self.script("var select = document.querySelector(arguments[0]); select.value = 'L';", UNIT_SELECT)
        self.type_into("form.shopping-add input[name='quantite']", "2")
        self.element("form.shopping-add button[type='submit']").click()
        self.wait_for(lambda: "ajouté à la liste" in self.script("return document.body.textContent"))
        self.assertIn("« Vodka exemple » ajouté à la liste (2 L).", self.script("return document.body.textContent"))
        item = ShoppingListItem.objects.get(label="Vodka exemple")
        self.assertEqual((item.quantity, item.unit, item.item_size), (2, "L", None))

    def test_the_unit_chosen_is_what_is_added(self):
        """Litres chosen for the vodka, 2 typed: « 2 L » on the list."""
        self.open(self.list_page)
        self.type_into(NAME_FIELD, "Vodka exemple (article)")
        self.wait_for(lambda: len(self.options(UNIT_SELECT)) == 2)
        self.script("var select = document.querySelector(arguments[0]); select.value = 'L';", UNIT_SELECT)
        self.type_into("form.shopping-add input[name='quantite']", "2")
        self.element("form.shopping-add button[type='submit']").click()
        self.wait_for(lambda: "ajouté à la liste" in self.script("return document.body.textContent"))
        self.assertIn("« Vodka exemple » ajouté à la liste (2 L).", self.script("return document.body.textContent"))
        item = ShoppingListItem.objects.get(label="Vodka exemple")
        self.assertEqual((item.quantity, item.unit, item.item_size), (2, "L", None))

    # -- the stock take -------------------------------------------------------------------------------

    def test_the_stock_take_s_row_still_fills_and_a_padded_name_too(self):
        """The same script on a count's row: a product typed with spaces
        around it - which the save takes off - gets its units now; a name
        edited into an unknown one gives the select back the options the
        server drew, never the previous entry's."""
        self.open(reverse("inventory:stock_take_create"))
        self.wait_for(lambda: self.script("return !!document.querySelector(arguments[0])", ROW_ENTRY))
        drawn = self.options(ROW_UNIT)
        self.type_into(ROW_ENTRY, f"  {SHOPPING_VODKA_PRODUCT} — Épicerie exemple  ")
        self.wait_for(lambda: self.options(ROW_UNIT) != drawn)
        self.assertEqual([value for value, _words, _selected in self.options(ROW_UNIT)], ["UNIT", "L"])
        self.assertEqual(self.script("return document.querySelector(arguments[0]).value", ROW_UNIT), "UNIT")
        self.type_into(ROW_ENTRY, "Inconnu exemple")
        self.wait_for(lambda: self.options(ROW_UNIT) == drawn)
        # An article: its own unit only.
        self.type_into(ROW_ENTRY, "Vodka exemple (article)")
        self.wait_for(lambda: self.options(ROW_UNIT) != drawn)
        self.assertEqual([value for value, _words, _selected in self.options(ROW_UNIT)], ["L"])
