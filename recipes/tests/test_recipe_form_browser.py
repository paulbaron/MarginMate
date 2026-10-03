"""The recipe form's ingredients in a real (headless) Chrome (the owner,
03/10/2026):

* **a choice reads as one**: each ingredient is a frame, and the options of
  an « OU » sit in one amber-edged frame, « OU » between them - two
  ingredients side by side looked like a choice, and a choice like two;
* **a search box** in place of a <select> of every article and recipe,
  accents and case aside, Enter picking;
* **a category** (« Catégorie : Rhums ») puts all its articles in the row's
  « OU » group, sharing the quantity until one is given its own, and each
  is removed with its « Retirer »;
* **every result of a search** (« sirop ») goes into the group in one click,
  « Ajouter les N résultats en « OU » » - Enter still takes one.

What is saved is checked in the database: the page posts plain rows.

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver. Skipped where Chrome or its driver is missing. Data
invented.
"""

import tempfile

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from invoices.scrapers import website
from recipes.models import Recipe
from tests.factories import make_recipe, make_stock_type
from tests.runner import log_in_the_browser

WAIT_SECONDS = 10
#: Set by hand to look at the form as the test leaves it; never committed set.
SCREENSHOT = ""


@tag("browser")
class RecipeFormInBrowserTests(StaticLiveServerTestCase):
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001 - no Chrome is a skip, whatever the reason
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1400, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.rums = [
            make_stock_type(name=name, category="Rhums") for name in ("Rhum ambré", "Rhum blanc", "Rhum vieux")
        ]
        self.lime = make_stock_type(name="Citron vert")
        log_in_the_browser(self.driver, self.live_server_url)
        self.driver.get(self.live_server_url + reverse("recipes:recipe_create"))

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def frames(self):
        """Each visible frame: whether it is a choice, and its articles."""
        return self.script(
            """
            return Array.from(document.querySelectorAll('.ingredient-group')).filter(f => !f.hidden).map(f => ({
                choice: f.classList.contains('is-choice'),
                title: f.querySelector('.ingredient-group-title').textContent,
                rows: Array.from(f.querySelectorAll('.ingredient-row-wrapper')).filter(w => !w.hidden).map(w => ({
                    label: w.querySelector('.ingredient-search').value,
                    quantity: w.querySelector("input[name$='-quantity']").value,
                })),
            }));
            """
        )

    def results(self, search):
        return self.script(
            "return Array.from(arguments[0].parentNode.querySelectorAll('[role=option]')).map(o => o.textContent);",
            search,
        )

    def test_a_category_becomes_a_choice_pruned_by_hand_then_saved(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        self.driver.find_element(By.ID, "id_name").send_keys("Ti punch")
        # The <select> is still the field, hidden; the box takes its place.
        self.assertFalse(
            self.driver.find_element(By.CSS_SELECTOR, "select[name='ingredients-0-source']").is_displayed()
        )
        search = self.driver.find_element(By.CSS_SELECTOR, ".ingredient-search")
        search.click()
        search.send_keys("RHUM")
        self.assertEqual(self.results(search)[0], "Catégorie : Rhums3 articles")
        search.send_keys(Keys.ENTER)

        frames = self.frames()
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0]["choice"])
        self.assertEqual(frames[0]["title"], "Au choix : une option parmi 3")
        self.assertEqual([row["label"] for row in frames[0]["rows"]], [f"{r.name} (Litre)" for r in self.rums])
        # The quantity is typed once, for every row the category made.
        self.driver.switch_to.active_element.send_keys("0.05")
        self.assertEqual([row["quantity"] for row in self.frames()[0]["rows"]], ["0.05"] * 3)
        # « OU » is drawn between the options, not above the first.
        separators = self.script(
            "return Array.from(document.querySelectorAll('.ingredient-group .ingredient-row-wrapper'))"
            ".map(w => getComputedStyle(w, '::before').content);"
        )
        self.assertEqual(separators, ["none", '"OU"', '"OU"'])

        # One given a quantity of its own no longer follows the others.
        quantities = self.driver.find_elements(By.CSS_SELECTOR, ".ingredient-group input[name$='-quantity']")
        quantities[2].clear()
        quantities[2].send_keys("0.06")
        quantities[0].send_keys(Keys.BACKSPACE, "4")
        self.assertEqual([row["quantity"] for row in self.frames()[0]["rows"]], ["0.04", "0.04", "0.06"])

        # The white rum is not wanted.
        self.driver.find_elements(By.CSS_SELECTOR, ".js-remove-ingredient")[1].click()
        self.assertEqual(self.frames()[0]["title"], "Au choix : une option parmi 2")

        # A second ingredient is a frame of its own, never part of the choice.
        self.driver.find_element(By.ID, "add-ingredient-row").click()
        search = self.driver.switch_to.active_element
        search.send_keys("citron")
        self.assertEqual(self.results(search), ["Citron vert (Litre)Article"])
        search.send_keys(Keys.ENTER)
        self.driver.switch_to.active_element.send_keys("0.02")
        frames = self.frames()
        self.assertEqual(len(frames), 2)
        self.assertFalse(frames[1]["choice"])
        self.assertEqual(frames[1]["rows"], [{"label": "Citron vert (Litre)", "quantity": "0.02"}])
        if SCREENSHOT:
            self.driver.save_screenshot(SCREENSHOT)

        self.driver.find_element(By.CSS_SELECTOR, "form button[type=submit].btn:not(.btn-secondary)").click()
        self.wait_for(lambda: Recipe.objects.filter(name="Ti punch").exists())
        recipe = Recipe.objects.get(name="Ti punch")
        groups = {}
        for ingredient in recipe.ingredients.all():
            groups.setdefault(ingredient.group, []).append((ingredient.stock_type.name, str(ingredient.quantity)))
        self.assertEqual(
            sorted(sorted(rows) for rows in groups.values()),
            [[("Citron vert", "0.0200")], [("Rhum ambré", "0.0400"), ("Rhum vieux", "0.0600")]],
        )

    def test_the_search_ignores_accents_and_leaving_it_half_typed_keeps_the_choice(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        search = self.driver.find_element(By.CSS_SELECTOR, ".ingredient-search")
        search.click()
        search.send_keys("AMBRE")
        self.assertEqual(self.results(search), ["Rhum ambré (Litre)Article"])
        search.send_keys(Keys.ENTER)
        select = self.driver.find_element(By.CSS_SELECTOR, "select[name='ingredients-0-source']")
        self.assertEqual(select.get_attribute("value"), f"stock:{self.rums[0].pk}")
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, ".ingredient-unit-label").text, "Litre")

        search.click()
        search.send_keys("vie")
        self.driver.find_element(By.ID, "id_name").click()
        self.assertEqual(search.get_attribute("value"), "Rhum ambré (Litre)")
        self.assertEqual(select.get_attribute("value"), f"stock:{self.rums[0].pk}")

    def test_every_result_of_a_search_goes_in_as_one_choice(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        syrups = [make_stock_type(name=name) for name in ("Sirop de canne", "Sirop d'orgeat", "Sirop de fraise")]
        make_recipe(name="Sirop maison gingembre", selling_price_ttc=None)
        self.driver.get(self.live_server_url + reverse("recipes:recipe_create"))
        self.driver.find_element(By.ID, "id_name").send_keys("Mojito fraise")
        search = self.driver.find_element(By.CSS_SELECTOR, ".ingredient-search")
        search.click()
        search.send_keys("sirop")
        results = self.results(search)
        self.assertEqual(results[0], "Ajouter les 4 résultats en « OU »tout « sirop »")
        self.assertEqual(len(results), 5)
        # Enter would take the first ingredient found, not all of them.
        active = self.script("return document.querySelector('[role=option].is-active').textContent;")
        self.assertEqual(active, results[1])

        self.driver.find_element(By.CSS_SELECTOR, "[role=option].is-every").click()
        frames = self.frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["title"], "Au choix : une option parmi 4")
        self.driver.switch_to.active_element.send_keys("0.02")
        self.assertEqual([row["quantity"] for row in self.frames()[0]["rows"]], ["0.02"] * 4)

        # The recipe is not wanted after all.
        recipe_row = [i for i, row in enumerate(self.frames()[0]["rows"]) if row["label"].startswith("Sirop maison")]
        self.driver.find_elements(By.CSS_SELECTOR, ".js-remove-ingredient")[recipe_row[0]].click()
        # Searching again adds nothing twice.
        search = self.driver.find_elements(By.CSS_SELECTOR, ".ingredient-search")[0]
        search.click()
        search.send_keys(Keys.CONTROL, "a")
        search.send_keys("sirop de")
        self.driver.find_element(By.CSS_SELECTOR, "[role=option].is-every").click()
        self.assertEqual(self.frames()[0]["title"], "Au choix : une option parmi 3")

        self.driver.find_element(By.CSS_SELECTOR, "form button[type=submit].btn:not(.btn-secondary)").click()
        self.wait_for(lambda: Recipe.objects.filter(name="Mojito fraise").exists())
        saved = Recipe.objects.get(name="Mojito fraise").ingredients.all()
        self.assertEqual(len({i.group for i in saved}), 1)
        self.assertEqual(sorted(i.stock_type.name for i in saved), sorted(s.name for s in syrups))
