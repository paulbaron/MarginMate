"""A stock take's draft kept in the browser, two tenants on one device - in
a real (headless) Chrome.

The inventory form mirrors every row into the browser's storage as it is
typed (stock_take_form.html, "the draft net"), under a key built from the
stock take's pk - « new » for one not saved yet. Storage belongs to the
origin, not to the login: before the tenant was part of the key, the person
opening a new stock take for bar B on a device bar A had counted on was
offered A's rows, and B's page, finding nothing to offer, wiped A's draft
the moment it loaded.

The key carries the tenant's opaque scope (<body data-tenant>,
accounts.tenancy.storage_scope), not its id (security audit LB-6): a
session opened before that change finds its count moved from the id's key
to the scope's (static/js/tenant_storage_legacy.js), and nothing else is
ever moved.

For real (accounts/tests/support.py): two tenants in temporary files, each
login handed to Chrome as its session cookie.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

from __future__ import annotations

import tempfile
import unittest

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from accounts.middleware import TENANT_SESSION_KEY
from accounts.tenancy import storage_scope
from accounts.tests.support import TwoTenantsTestCase
from invoices.scrapers import website

WAIT_SECONDS = 10


def new_draft_key(scope) -> str:
    """The key of a new stock take's draft, as stock_take_form.html builds it."""
    return f"marginmate:espace-{scope}:stock-take-draft:new"


@tag("browser")
class DraftPerTenantInBrowserTests(TwoTenantsTestCase, StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Chrome indisponible : {exc}")
        try:
            super().setUpClass()
        except BaseException:
            cls.driver.quit()
            raise
        # Registered last, so run first: Chrome lets go of the live server's
        # sockets before the server stops sharing its database connection
        # (the other way round, each request thread still open fails on it).
        cls.addClassCleanup(cls.driver.quit)

    def setUp(self):
        super().setUp()
        # One Chrome for the class: each test starts from an empty storage.
        self.driver.get(self.live_server_url + "/static/css/marginmate.css")
        self.script("localStorage.clear(); sessionStorage.clear();")

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def log_in(self, user):
        """Chrome takes the session the test client opened."""
        self.client.force_login(user)
        session = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        # A cookie is set on the origin's own page (a static file will do).
        self.driver.get(self.live_server_url + "/static/css/marginmate.css")
        self.driver.delete_all_cookies()
        self.driver.add_cookie({"name": settings.SESSION_COOKIE_NAME, "value": session, "path": "/"})

    def open_new_stock_take(self):
        self.driver.get(self.live_server_url + reverse("inventory:stock_take_create"))
        # The first row is added once the page has loaded (DOMContentLoaded).
        self.wait_for(lambda: self.script("return document.querySelectorAll(\"input[name$='-entry_search']\").length"))

    def drafts(self):
        return sorted(
            self.script(
                "return Object.keys(localStorage).filter(function (key) {"
                " return key.indexOf('stock-take-draft') !== -1; })"
            )
        )

    def test_bar_b_is_never_offered_bar_a_s_counts(self):
        from selenium.webdriver.common.by import By

        self.log_in(self.user_a)
        self.open_new_stock_take()
        self.driver.find_element(By.CSS_SELECTOR, "input[name$='-entry_search']").send_keys("Vodka Alpha")
        alpha_draft = new_draft_key(storage_scope(self.bar_a))
        self.assertEqual(self.script("return DRAFT_KEY"), alpha_draft)
        self.wait_for(lambda: self.drafts() == [alpha_draft])

        self.log_in(self.user_b)
        self.open_new_stock_take()
        # Nothing offered to B, and B's page left A's draft where it was.
        self.assertTrue(self.script("return document.getElementById('draft-offer').hidden"))
        self.assertEqual(self.drafts(), [alpha_draft])

        # Back in A, the count is still there to restore.
        self.log_in(self.user_a)
        self.open_new_stock_take()
        self.wait_for(lambda: not self.script("return document.getElementById('draft-offer').hidden"))

    # -- the keys of a session from before the scope (LB-6) --------------------------------------

    def keys(self, area):
        return sorted(self.script(f"return Object.keys({area});"))

    def plant(self, area, key, value):
        self.script(f"{area}.setItem(arguments[0], arguments[1]);", key, value)

    def plant_a_count(self, key, entry):
        """A count left under `key` a minute ago, as the form writes one."""
        self.script(
            "localStorage.setItem(arguments[0], JSON.stringify({savedAt: Date.now() - 60000, takenAt: '',"
            " note: '', rows: [{entry: arguments[1], quantity: '3', unit: ''}]}));",
            key,
            entry,
        )

    def as_before_the_scope(self):
        """The session as one opened before 29/09: no tenant written in it."""
        session = self.client.session
        del session[TENANT_SESSION_KEY]
        session.save()

    def test_a_session_from_before_finds_its_count_under_the_scope(self):
        self.log_in(self.user_a)
        self.as_before_the_scope()
        old_alpha, old_beta = new_draft_key(self.bar_a.pk), new_draft_key(self.bar_b.pk)
        self.plant_a_count(old_alpha, "Vodka Alpha")
        self.plant_a_count(old_beta, "Gin Beta")
        # ui.js's and datatable.js's keys ("mm:"), in both stores.
        tick = f"mm:espace-{self.bar_a.pk}:/invoices/:source-3"
        sort = f"mm:espace-{self.bar_a.pk}:/stock-takes/:table-sort:t0"
        self.plant("localStorage", tick, "1")
        self.plant("sessionStorage", sort, "2:1")
        # A key the scope already holds is newer than the old one: kept.
        scope = storage_scope(self.bar_a)
        newer = f"marginmate:espace-{scope}:achats:import-tab"
        self.plant("localStorage", newer, "pdf")
        self.plant("localStorage", f"marginmate:espace-{self.bar_a.pk}:achats:import-tab", "photos")

        self.open_new_stock_take()
        # The count typed under the old key is offered, and restores.
        self.wait_for(lambda: not self.script("return document.getElementById('draft-offer').hidden"))
        self.assertEqual(self.script("return DRAFT_KEY"), new_draft_key(scope))
        self.script("document.getElementById('draft-restore').click();")
        self.assertIn(
            "Vodka Alpha",
            self.script(
                "return Array.from(document.querySelectorAll(\"input[name$='-entry_search']\")).map(function (i) { return i.value; })"
            ),
        )
        # Moved, not copied; bar B's left where it was, untouched.
        self.assertEqual(
            self.keys("localStorage"),
            sorted([old_beta, newer, new_draft_key(scope), f"mm:espace-{scope}:/invoices/:source-3"]),
        )
        self.assertEqual(self.script("return localStorage.getItem(arguments[0]);", newer), "pdf")
        self.assertIn("Gin Beta", self.script("return localStorage.getItem(arguments[0]);", old_beta))
        self.assertEqual(self.keys("sessionStorage"), [f"mm:espace-{scope}:/stock-takes/:table-sort:t0"])

    def test_a_session_of_today_moves_nothing(self):
        """Its pages never carry the id: an old key on the device is nobody's
        it can tell, and stays where it is."""
        self.log_in(self.user_a)
        old_alpha = new_draft_key(self.bar_a.pk)
        self.plant_a_count(old_alpha, "Vodka Alpha")
        self.open_new_stock_take()
        self.assertIsNone(self.script("return document.body.getAttribute('data-tenant-legacy')"))
        self.assertTrue(self.script("return document.getElementById('draft-offer').hidden"))
        self.assertEqual(self.drafts(), [old_alpha])
