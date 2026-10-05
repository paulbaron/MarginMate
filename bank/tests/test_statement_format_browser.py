"""« Format du relevé » (bank/_statement_format_fields.html) clicked in a real
(headless) Chrome.

What a CSV alone needs - its separator, dates, decimals, columns and account
pattern - sits in a fieldset `static/js/statement_format.js` hides AND
disables when the menu says OFX or CAMT.053. Hidden only, its empty required
columns would stop the browser sending the form, silently: « Enregistrer »
would do nothing for an OFX format (the source form's lesson,
invoices/tests/test_invoice_type_form_browser.py). The test client posts
whatever it is given, and test_statement_format_views.py only simulates the
script, so only a browser sees that.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

import tempfile

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from bank.models import StatementFormat
from invoices.scrapers import website
from tests.runner import log_in_the_browser

WAIT_SECONDS = 10


@tag("browser")
class StatementFormatInBrowserTests(StaticLiveServerTestCase):
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
        # A desktop's window: headless Chrome's default is under 860 px.
        cls.driver.set_window_size(1280, 1000)

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        # Every page wants a login: the test tenant's owner.
        log_in_the_browser(self.driver, self.live_server_url)
        self.driver.get(self.live_server_url + reverse("bank:statement_formats"))

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def new_format_form(self):
        """« Nouveau format »: the one form of the list page holding a menu
        of kinds of file."""
        from selenium.webdriver.common.by import By

        return self.driver.find_element(By.CSS_SELECTOR, "#nouveau-format form")

    def choose(self, form, file_type):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select

        Select(form.find_element(By.NAME, "file_type")).select_by_value(file_type)

    def csv_fields(self, form):
        from selenium.webdriver.common.by import By

        return form.find_element(By.CSS_SELECTOR, 'fieldset[data-file-types="csv"]')

    def test_an_ofx_format_is_saved_with_a_csv_s_fields_hidden_and_never_sent(self):
        from selenium.webdriver.common.by import By

        form = self.new_format_form()
        fieldset = self.csv_fields(form)
        self.assertTrue(fieldset.is_displayed())
        self.choose(form, "ofx")
        self.wait_for(lambda: not fieldset.is_displayed())
        self.assertIsNotNone(fieldset.get_attribute("disabled"))
        form.find_element(By.NAME, "name").send_keys("Relevé OFX de la banque")
        form.find_element(By.CSS_SELECTOR, 'button[name="action"][value="enregistrer"]').click()
        self.wait_for(lambda: StatementFormat.objects.filter(name="Relevé OFX de la banque").exists())
        made = StatementFormat.objects.get(name="Relevé OFX de la banque")
        self.assertEqual(
            (made.file_type, made.date_column, made.label_columns, made.account_pattern), ("ofx", None, "", "")
        )

    def test_back_to_csv_a_csv_s_fields_are_shown_and_asked_for_again(self):
        form = self.new_format_form()
        fieldset = self.csv_fields(form)
        self.choose(form, "camt053")
        self.wait_for(lambda: not fieldset.is_displayed())
        self.choose(form, "csv")
        self.wait_for(fieldset.is_displayed)
        self.assertIsNone(fieldset.get_attribute("disabled"))

    def test_a_stored_format_s_page_draws_its_kind_fixed_and_its_fields_as_stored(self):
        from selenium.webdriver.common.by import By

        seeded = StatementFormat.objects.get(file_type="csv")
        self.driver.get(self.live_server_url + reverse("bank:statement_format", args=[seeded.pk]))
        select = self.driver.find_element(By.NAME, "file_type")
        self.assertFalse(select.is_enabled())
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, 'fieldset[data-file-types="csv"]').is_displayed())
