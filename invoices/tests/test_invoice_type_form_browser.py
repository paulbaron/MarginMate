"""The source form (« Nouvelle source de factures »), clicked in a real (headless) Chrome.

The page holds both kinds' settings - the mailbox's patterns and the
customer portal's login page - and shows the chosen one. A required field
of the hidden kind, left empty, stopped the browser from sending the form
at all: "Tester" did nothing, no browser opened, and no mailbox type could
be saved once the portal's fields were in the page. The test client posts
whatever it is given, so only a browser sees that.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

import tempfile
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from invoices.models import InvoiceType, ScrapeJob, WebsiteInvoiceSource
from invoices.scrapers import website
from tests.factories import make_supplier

WAIT_SECONDS = 10


@tag("browser")
class InvoiceTypeFormInBrowserTests(StaticLiveServerTestCase):
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
        self.supplier = make_supplier(code="BOX_X", name="Box Exemple", parser_key="", expenses_only=True)

    def open(self, url):
        self.driver.get(self.live_server_url + url)

    def field(self, name):
        from selenium.webdriver.common.by import By

        return self.driver.find_element(By.NAME, name)

    def click(self, action):
        from selenium.webdriver.common.by import By

        # The visible button: the form's first, out of sight, is what Enter presses.
        self.driver.find_element(By.CSS_SELECTOR, f'button[name=action][value="{action}"]:not(.visually-hidden)').click()

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def choose_kind(self, value):
        from selenium.webdriver.support.ui import Select

        Select(self.field("source_kind")).select_by_value(value)

    def fill_general(self, name):
        from selenium.webdriver.support.ui import Select

        self.field("name").send_keys(name)
        Select(self.field("supplier")).select_by_value(str(self.supplier.pk))

    def test_tester_on_a_website_type_starts_the_test_with_the_browser_asked_for(self):
        invoice_type = InvoiceType.objects.create(
            supplier=self.supplier, name="Box Exemple - Factures", source_kind=InvoiceType.SourceKind.WEBSITE
        )
        WebsiteInvoiceSource.objects.create(
            invoice_type=invoice_type, login_url="https://box.exemple.fr/login",
            username_env="BOX_LOGIN", password_env="BOX_PASSWORD",
        )
        self.open(reverse("invoices:invoice_type_update", args=[invoice_type.pk]))
        self.field("site-show_browser").click()
        with mock.patch("invoices.tasks.test_website_task") as task:
            self.click("test")
            self.wait_for(lambda: ScrapeJob.objects.filter(kind=ScrapeJob.Kind.TEST).exists())
            self.wait_for(lambda: task.called)
        recipe = task.call_args.args[1]
        self.assertEqual((recipe.login_url, recipe.show_browser), ("https://box.exemple.fr/login", True))

    def test_a_mailbox_type_is_saved_with_the_portal_fields_hidden_and_empty(self):
        self.open(reverse("invoices:invoice_type_create"))
        self.fill_general("Grossiste - Factures")
        self.choose_kind("EMAIL")
        self.field("sender_pattern").send_keys("factures@grossiste")
        self.click("save")
        self.wait_for(lambda: InvoiceType.objects.filter(name="Grossiste - Factures").exists())
        self.assertEqual(InvoiceType.objects.get(name="Grossiste - Factures").email_source.sender_pattern, "factures@grossiste")

    def test_a_website_type_is_saved_with_the_mailbox_fields_hidden_and_empty(self):
        self.open(reverse("invoices:invoice_type_create"))
        self.fill_general("Box Exemple - Factures")
        self.choose_kind("WEBSITE")
        self.field("site-login_url").send_keys("https://box.exemple.fr/login")
        self.field("site-username_env").send_keys("BOX_LOGIN")
        self.field("site-password_env").send_keys("BOX_PASSWORD")
        self.click("save")
        self.wait_for(lambda: WebsiteInvoiceSource.objects.exists())
        self.assertEqual(WebsiteInvoiceSource.objects.get().invoice_type.source_kind, "WEBSITE")

    def new_supplier(self, name, charges=False):
        """« + Nouveau fournisseur… » picked: its name box shows, the only
        one that is then required."""
        from selenium.webdriver.support.ui import Select

        Select(self.field("supplier")).select_by_value("new")
        self.wait_for(lambda: self.field("new_name").is_displayed())
        self.field("new_name").send_keys(name)
        if charges:
            self.field("new_expenses").click()

    def test_a_mailbox_type_is_saved_with_a_new_supplier(self):
        from invoices.models import Supplier

        self.open(reverse("invoices:invoice_type_create"))
        self.field("name").send_keys("Traiteur - Factures")
        self.new_supplier("Traiteur Exemple")
        self.choose_kind("EMAIL")
        self.field("sender_pattern").send_keys("factures@traiteur")
        self.click("save")
        self.wait_for(lambda: InvoiceType.objects.filter(name="Traiteur - Factures").exists())
        self.assertEqual(InvoiceType.objects.get(name="Traiteur - Factures").supplier.name, "Traiteur Exemple")
        self.assertFalse(Supplier.objects.get(name="Traiteur Exemple").expenses_only)

    def test_a_website_type_is_saved_with_a_new_supplier_of_charges(self):
        from invoices.models import Supplier

        self.open(reverse("invoices:invoice_type_create"))
        self.field("name").send_keys("Eau - Espace client")
        self.new_supplier("Eau Exemple", charges=True)
        self.choose_kind("WEBSITE")
        self.field("site-login_url").send_keys("https://eau.exemple.fr/login")
        self.field("site-username_env").send_keys("EAU_LOGIN")
        self.field("site-password_env").send_keys("EAU_PASSWORD")
        self.click("save")
        self.wait_for(lambda: WebsiteInvoiceSource.objects.exists())
        self.assertTrue(Supplier.objects.get(name="Eau Exemple").expenses_only)

    def test_an_existing_supplier_picked_leaves_the_hidden_name_box_out_of_the_way(self):
        """The new supplier's box, hidden, must not stop the form: picked
        then left, it was required for a moment."""
        from selenium.webdriver.support.ui import Select

        self.open(reverse("invoices:invoice_type_create"))
        self.field("name").send_keys("Box - Factures")
        Select(self.field("supplier")).select_by_value("new")
        Select(self.field("supplier")).select_by_value(str(self.supplier.pk))
        self.choose_kind("EMAIL")
        self.field("sender_pattern").send_keys("factures@box")
        self.click("save")
        self.wait_for(lambda: InvoiceType.objects.filter(name="Box - Factures").exists())
        self.assertEqual(InvoiceType.objects.get(name="Box - Factures").supplier, self.supplier)

    def test_enter_in_a_field_saves_rather_than_tests(self):
        """« Tester » was the form's first submit button: Enter in a field
        started a sign-in on the portal instead of saving."""
        from selenium.webdriver.common.keys import Keys

        self.open(reverse("invoices:invoice_type_create"))
        self.fill_general("Grossiste - Factures")
        self.choose_kind("EMAIL")
        self.field("sender_pattern").send_keys("factures@grossiste")
        # Nothing may reach a mailbox, whichever button Enter presses.
        with mock.patch("invoices.views.threading.Thread"):
            self.field("name").send_keys(Keys.ENTER)
            self.wait_for(lambda: InvoiceType.objects.filter(name="Grossiste - Factures").exists())
        self.assertFalse(ScrapeJob.objects.filter(kind=ScrapeJob.Kind.TEST).exists())
