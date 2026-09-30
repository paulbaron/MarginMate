"""Back, on a device two bars share - in a real (headless) Chrome.

The server answers each login with its own tenant only; what a browser
KEEPS is another matter, and a device may well be shared (the signing pages
already say no-store for « a shared phone »):

* the back/forward cache: bar A's page, left for the login page, was kept
  whole in memory, and Back - after A's logout, or after bar B logged in on
  the same tab - drew it again, A's suppliers and « Bar Alpha » in the
  topbar, without asking the server. A bar's page is now sent with
  `Cache-Control: no-store` (accounts.middleware.TenantMiddleware), which
  Chrome never keeps across a change of login;
* htmx's own history cache: the boosted tabs (Achats) kept whole pages under
  ONE key of the origin's storage (`htmx-history-cache`), which B's pages
  could read and a Back could draw. base.html's `htmx-config` meta
  (`historyCacheSize` 0, multi mode) keeps none.

Each case runs twice: with Chrome's back/forward cache, and without it (a
page evicted from it is read again from the HTTP cache - or, no-store, from
the server). Multi mode for real (accounts/tests/support.py), the logins
typed into the pages like a person would.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

from __future__ import annotations

import time
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from tests.factories import make_invoice, make_supplier

#: accounts.tests.support.TenancyTestCase.make_member's.
PASSWORD = "mot-de-passe-essai"
WAIT_SECONDS = 10
#: How long a Back is given to draw whatever it draws (a page restored from
#: memory, an htmx snapshot, the server's answer).
SETTLE_SECONDS = 1.5
#: What bar A's pages show and bar B must never see: a row, and the topbar.
ALPHA = ("Alphaville", "Bar Alpha")


def chrome(*arguments):
    """Headless Chrome with `arguments` added, the driver found as
    invoices.scrapers.website.build_chrome finds it."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    for argument in arguments:
        options.add_argument(argument)
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


@tag("browser")
class BackButtonInBrowserTests(TwoTenantsTestCase, StaticLiveServerTestCase):
    serialized_rollback = True
    chrome_arguments: tuple = ()

    def setUp(self):
        super().setUp()
        with bound_tenant(self.bar_a):
            make_invoice(supplier=make_supplier(code="T-ALPHA", name="Grossiste Alphaville"), invoice_number="F-ALPHA")
        with bound_tenant(self.bar_b):
            make_invoice(supplier=make_supplier(code="T-BETA", name="Grossiste Betaville"), invoice_number="F-BETA")
        # A Chrome per test: a tab's history is what Back walks, and a fresh
        # profile has nothing stored.
        try:
            self.driver = chrome(*self.chrome_arguments)
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Chrome indisponible : {exc}")
        self.addCleanup(self.driver.quit)
        # Wide enough for the topbar to draw its links and the bar's name:
        # under 860 px (headless Chrome's window is 800 px) they fold into
        # « Menu » (30/09), out of innerText - and « Bar Alpha » in the topbar
        # is half of what Back must never show.
        self.driver.set_window_size(1280, 900)

    def script(self, source):
        return self.driver.execute_script(source)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def path(self) -> str:
        return self.script("return location.pathname")

    def text(self) -> str:
        return self.script("return document.body ? document.body.innerText : ''")

    def open(self, name):
        self.driver.get(self.live_server_url + reverse(name))

    def log_in(self, email):
        from selenium.webdriver.common.by import By

        if self.path() != reverse("accounts:login"):
            self.open("accounts:login")
        self.driver.find_element(By.NAME, "username").send_keys(email)
        self.driver.find_element(By.NAME, "password").send_keys(PASSWORD)
        self.driver.find_element(By.CSS_SELECTOR, "form.public-form button[type=submit]").click()
        self.wait_for(lambda: self.path() != reverse("accounts:login"))

    def log_out(self):
        self.script("document.querySelector('.topbar-logout button').click()")
        self.wait_for(lambda: self.path() == reverse("accounts:login"))

    def assertShowsBarA(self):
        """Bar A's page as it is drawn: its row and its name in the topbar -
        or Back finding neither would prove nothing."""
        text = self.text()
        self.assertEqual([word for word in ALPHA if word not in text], [])

    def assertBackNeverShowsBarA(self, steps):
        """Back, step by step, until the tab's first page: never a word of
        bar A's page."""
        seen = []
        for _ in range(steps):
            self.driver.back()
            time.sleep(SETTLE_SECONDS)
            if self.script("return location.protocol") not in ("http:", "https:"):
                break
            text = self.text()
            seen.append((self.script("return location.pathname + location.search"), [w for w in ALPHA if w in text]))
        self.assertEqual([step for step in seen if step[1]], [], seen)

    def test_back_after_another_bar_s_login_never_shows_the_first_bar_s_page(self):
        self.log_in("alpha@example.invalid")
        self.open("invoices:supplier_list")
        self.assertShowsBarA()
        self.log_out()
        self.log_in("beta@example.invalid")
        self.open("invoices:supplier_list")
        self.assertIn("Betaville", self.text())
        self.assertNotIn("Alphaville", self.text())
        self.assertBackNeverShowsBarA(steps=6)

    def test_back_after_a_logout_never_shows_the_page_left(self):
        self.log_in("alpha@example.invalid")
        self.open("invoices:supplier_list")
        self.assertShowsBarA()
        self.log_out()
        self.assertBackNeverShowsBarA(steps=2)

    def test_htmx_keeps_no_page_of_the_first_bar(self):
        """Achats' tabs are boosted: htmx stored the page each click left."""
        self.log_in("alpha@example.invalid")
        self.open("invoices:invoice_list")
        self.assertShowsBarA()
        tab = self.script("var tab = document.querySelectorAll('nav.tabs a')[1]; tab.click(); return tab.pathname;")
        self.wait_for(lambda: self.path() == tab)
        time.sleep(SETTLE_SECONDS)
        self.log_out()
        self.log_in("beta@example.invalid")
        self.assertIsNone(self.script("return localStorage.getItem('htmx-history-cache')"))
        self.assertBackNeverShowsBarA(steps=3)


@tag("browser")
class BackButtonWithoutBackForwardCacheInBrowserTests(BackButtonInBrowserTests):
    """The same, the back/forward cache switched off: a page left is read
    again from the HTTP cache, and htmx draws its own snapshot of a boosted
    tab."""

    chrome_arguments = ("--disable-features=BackForwardCache",)
