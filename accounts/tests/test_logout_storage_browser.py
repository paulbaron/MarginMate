"""« Se déconnecter » forgets the bar's drafts and keeps its preferences - in
a real (headless) Chrome (security audit LOAD-2, 29/09/2026; review of the
same day, LOGOUT-PREFS).

The logout cleared the server's session and left the bar's own data in the
browser's storage: the unsaved stock count (articles, quantities, note), a
pickup's counts, readable on the public login page of a shared device. The
first fix answered ``Clear-Site-Data: "storage"``, which emptied everything
with it: the gather's sources left unticked (Metro, a portal asking for a
code every time) came back ticked, the folds and the tables' sort reset. Now:

* no ``Clear-Site-Data`` (accounts/tests/test_sessions.py);
* static/js/ui.js forgets this tenant's DRAFTS as the form is sent - under
  its scope and the old id of a session from before - and nothing else: its
  preferences stay, and another bar's count on the same device is that
  bar's.

Multi mode for real (accounts/tests/support.py), the login typed into the
page like a person would. Tagged "browser": `--exclude-tag=browser` for the
fast loop. Skipped where Chrome or its driver is missing. Data invented.
"""

from __future__ import annotations

import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from accounts.tenancy import storage_scope
from accounts.tests.support import TwoTenantsTestCase
from accounts.tests.test_back_button_browser import PASSWORD, chrome

WAIT_SECONDS = 10


@tag("browser")
class LogoutStorageInBrowserTests(TwoTenantsTestCase, StaticLiveServerTestCase):
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        try:
            self.driver = chrome()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Chrome indisponible : {exc}")
        self.addCleanup(self.driver.quit)
        self.alpha = storage_scope(self.bar_a)
        self.beta = storage_scope(self.bar_b)

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def path(self) -> str:
        return self.script("return location.pathname")

    def keys(self) -> list[str]:
        return sorted(self.script("return Object.keys(localStorage).concat(Object.keys(sessionStorage));"))

    def local(self, key, value):
        self.script("localStorage.setItem(arguments[0], arguments[1]);", key, value)

    def log_in_and_count(self):
        """Alpha's login typed in, a row counted on a new stock take (the
        draft written as it is typed) - plus what else the device holds:
        Alpha's pickup not sent, Alpha's preferences (a gather source left
        unticked, the import tab, a table's sort for the tab) and bar
        Beta's own count."""
        from selenium.webdriver.common.by import By

        self.driver.get(self.live_server_url + reverse("accounts:login"))
        self.driver.find_element(By.NAME, "username").send_keys("alpha@example.invalid")
        self.driver.find_element(By.NAME, "password").send_keys(PASSWORD)
        self.driver.find_element(By.CSS_SELECTOR, "form.public-form button[type=submit]").click()
        self.wait_for(lambda: self.path() != reverse("accounts:login"))

        self.driver.get(self.live_server_url + reverse("inventory:stock_take_create"))
        self.wait_for(lambda: self.script("return document.querySelectorAll(\"input[name$='-entry_search']\").length"))
        self.driver.find_element(By.CSS_SELECTOR, "input[name$='-entry_search']").send_keys("Vodka Alpha")
        self.draft = self.script("return DRAFT_KEY")
        self.assertIn(self.alpha, self.draft)
        self.wait_for(lambda: self.script("return localStorage.getItem(DRAFT_KEY)"))

        self.drafts = [self.draft, f"marginmate:espace-{self.alpha}:consignes:brouillon"]
        self.local(self.drafts[1], '{"counts": {"1": "4"}}')
        self.kept = [
            f"mm:espace-{self.alpha}:/invoices/:source:METRO",
            f"marginmate:espace-{self.alpha}:achats:import-tab",
            f"mm:espace-{self.alpha}:/stock-takes/:table-sort:t0",
            f"marginmate:espace-{self.beta}:stock-take-draft:new",
        ]
        self.local(self.kept[0], "0")
        self.local(self.kept[1], "pdf")
        self.script("sessionStorage.setItem(arguments[0], '2:1');", self.kept[2])
        self.local(self.kept[3], '{"rows": []}')
        before = self.keys()
        self.assertLessEqual({*self.drafts, *self.kept}, set(before))
        # What the logout must leave: everything but Alpha's two drafts
        # (the page's own preferences, if it wrote any, included).
        self.left = sorted(key for key in before if key not in self.drafts)

    def test_the_logout_forgets_the_drafts_and_keeps_the_rest(self):
        self.log_in_and_count()
        self.script("document.querySelector('.topbar-logout button').click()")
        self.wait_for(lambda: self.path() == reverse("accounts:login"))
        self.assertIn("Vous êtes déconnecté", self.script("return document.body.innerText"))
        self.assertEqual(self.keys(), self.left)
        # The source Alpha left unticked is still unticked for his next gather.
        self.assertEqual(self.script("return localStorage.getItem(arguments[0])", self.kept[0]), "0")

    def test_it_is_the_script_that_forgets_them(self):
        """The form is sent nowhere (a listener after ui.js's cancels it):
        no answer - what is gone, the script took. A session from before
        29/09 carries the tenant's old id too (data-tenant-legacy): its
        drafts under that id go, its preferences under it stay."""
        self.log_in_and_count()
        legacy = str(self.bar_a.pk)
        self.script("document.body.setAttribute('data-tenant-legacy', arguments[0]);", legacy)
        self.local(f"marginmate:espace-{legacy}:stock-take-draft:7", '{"rows": []}')
        self.local(f"mm:espace-{legacy}:/invoices/:source:PORTAIL", "0")
        self.script("document.addEventListener('submit', function (event) { event.preventDefault(); });")
        self.script("document.querySelector('.topbar-logout button').click()")
        self.assertEqual(self.path(), reverse("inventory:stock_take_create"))
        self.assertEqual(self.keys(), sorted([*self.left, f"mm:espace-{legacy}:/invoices/:source:PORTAIL"]))
