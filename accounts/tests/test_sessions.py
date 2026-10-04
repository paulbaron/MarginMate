"""What a session and a browser keep, tenant by tenant (security audit,
29/09/2026):

* LOAD-1 - a session belongs to the tenant it was opened in: a login moved
  to another bar is logged out, never shown the new bar's pages without a
  password (accounts.middleware, `pin_the_tenant`);
* LOAD-2 - « Se déconnecter » forgets the tenant's DRAFTS in the browser
  (static/js/ui.js as the form is sent; in Chrome:
  test_logout_storage_browser.py) and keeps its preferences: no
  ``Clear-Site-Data``, which emptied everything (review of 29/09,
  LOGOUT-PREFS);
* LOAD-3 - a tenant whose database will not open answers a plain 503 page,
  and only the binding's own failure does;
* LB-6 - the pages carry an opaque storage scope, never the tenant's
  sequential id; a session from before is given the old prefix to move its
  keys (static/js/tenant_storage_legacy.js).

Real tenants in temporary files (accounts/tests/support.py). Data invented.
"""

from __future__ import annotations

import os
import re
from unittest import mock
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connections
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse

from accounts import paths
from accounts.middleware import (
    ACCESS_CHANGED,
    LEGACY_STORAGE_SESSION_KEY,
    TENANT_SESSION_KEY,
    TenantMiddleware,
)
from accounts.models import Membership
from accounts.tenancy import TenancyError, bound_tenant, current_tenant, storage_scope
from accounts.tests.support import TwoTenantsTestCase
from tests.factories import make_supplier

#: accounts.tests.support.TenancyTestCase.make_member's.
PASSWORD = "mot-de-passe-essai"
UNAVAILABLE = "Votre espace est momentanément indisponible"


def body_attributes(page: str) -> str:
    found = re.search(r"<body\b([^>]*)>", page)
    assert found, "no <body> on the page"
    return found.group(1)


class WithSuppliersMixin:
    def setUp(self):
        super().setUp()
        with bound_tenant(self.bar_a):
            make_supplier(code="T-ALPHA", name="Grossiste Alpha")
        with bound_tenant(self.bar_b):
            make_supplier(code="T-BETA", name="Grossiste Beta")
        self.suppliers = reverse("invoices:supplier_list")

    def move(self, user, tenant):
        """What the admin's membership form does (accounts/admin.py)."""
        Membership.objects.filter(user=user).update(tenant=tenant)


class OneTenantPerSessionTests(WithSuppliersMixin, TwoTenantsTestCase):
    def test_every_login_writes_its_tenant_into_its_session(self):
        # The login page, typed as a person does.
        self.client.post(reverse("accounts:login"), {"username": "alpha@example.invalid", "password": PASSWORD})
        self.assertEqual(self.client.session[TENANT_SESSION_KEY], self.bar_a.pk)
        # Another login on the same browser: its own.
        self.client.force_login(self.user_b)
        self.assertEqual(self.client.session[TENANT_SESSION_KEY], self.bar_b.pk)
        self.assertNotIn(LEGACY_STORAGE_SESSION_KEY, self.client.session)

    def test_a_login_moved_to_another_tenant_is_logged_out(self):
        """The audit's case: the membership edited in the admin, the same
        cookie, no password typed."""
        self.client.force_login(self.user_a)
        self.assertContains(self.client.get(self.suppliers), "Grossiste Alpha")

        self.move(self.user_a, self.bar_b)
        response = self.client.get(self.suppliers + "?q=x")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/connexion/?next=" + quote(self.suppliers + "?q=x", safe="/"))
        self.assertNotIn(b"Grossiste Beta", response.content)
        self.assertNotIn("_auth_user_id", self.client.session)
        # The login page says why, once.
        login = self.client.get(response["Location"])
        self.assertContains(login, ACCESS_CHANGED, count=1)
        # And the cookie opens nothing any more.
        again = self.client.get(self.suppliers)
        self.assertEqual(again.status_code, 302)
        self.assertNotIn(b"Grossiste Beta", again.content)

    def test_an_htmx_request_sends_the_whole_page_to_the_login_page(self):
        self.client.force_login(self.user_a)
        self.client.get(self.suppliers)
        self.move(self.user_a, self.bar_b)
        response = self.client.get(
            self.suppliers, HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL="http://testserver/invoices/?onglet=achats"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["HX-Redirect"], "/connexion/?next=/invoices/%3Fonglet%3Dachats")
        self.assertNotIn(b"Grossiste Beta", response.content)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_password_typed_again_opens_the_new_tenant(self):
        self.client.force_login(self.user_a)
        self.client.get(self.suppliers)
        self.move(self.user_a, self.bar_b)
        self.assertEqual(self.client.get(self.suppliers).status_code, 302)
        self.client.post(reverse("accounts:login"), {"username": "alpha@example.invalid", "password": PASSWORD})
        page = self.client.get(self.suppliers)
        self.assertContains(page, "Grossiste Beta")
        self.assertNotContains(page, "Grossiste Alpha")

    def test_a_login_again_in_the_same_session_is_pinned_again(self):
        """Django keeps a session's data when the same user logs in again:
        the pin is written again, never carried over."""
        self.client.force_login(self.user_a)
        self.move(self.user_a, self.bar_b)
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.session[TENANT_SESSION_KEY], self.bar_b.pk)
        self.assertContains(self.client.get(self.suppliers), "Grossiste Beta")

    def test_a_login_that_had_no_tenant_logs_in_again_once_given_one(self):
        nobody = self.make_member(self.bar_b, "sans-espace@example.invalid")
        nobody.memberships.all().delete()
        self.client.force_login(nobody)
        self.assertIsNone(self.client.session[TENANT_SESSION_KEY])
        self.assertEqual(self.client.get(self.suppliers).status_code, 403)
        Membership.objects.create(user=nobody, tenant=self.bar_b)
        self.assertEqual(self.client.get(self.suppliers).status_code, 302)

    def test_the_same_tenant_given_back_changes_nothing(self):
        self.client.force_login(self.user_a)
        Membership.objects.filter(user=self.user_a).delete()
        Membership.objects.create(user=self.user_a, tenant=self.bar_a)
        self.assertContains(self.client.get(self.suppliers), "Grossiste Alpha")

    def test_a_session_from_before_the_pins_is_pinned_where_it_is(self):
        """Opened before 29/09: no pin. It is not logged out - it is pinned
        on its next request, and from then on held to it."""
        self.client.force_login(self.user_a)
        session = self.client.session
        del session[TENANT_SESSION_KEY]
        session.save()

        self.assertContains(self.client.get(self.suppliers), "Grossiste Alpha")
        self.assertEqual(self.client.session[TENANT_SESSION_KEY], self.bar_a.pk)
        self.assertIs(self.client.session[LEGACY_STORAGE_SESSION_KEY], True)

        self.move(self.user_a, self.bar_b)
        self.assertEqual(self.client.get(self.suppliers).status_code, 302)

    def test_a_request_built_by_hand_has_no_session_to_tie(self):
        request = RequestFactory().get(self.suppliers)
        request.user = self.user_a
        response = TenantMiddleware(lambda r: HttpResponse(r.tenant.name))(request)
        self.assertEqual(response.content, b"Bar Alpha")


class UnavailableTenantTests(WithSuppliersMixin, TwoTenantsTestCase):
    def test_a_missing_database_is_a_plain_503(self):
        self.client.force_login(self.user_a)
        database = paths.tenant_database(self.bar_a)
        os.remove(database)
        with self.assertLogs("accounts.middleware", "ERROR") as logged:
            response = self.client.get(self.suppliers)
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, UNAVAILABLE, status_code=503)
        self.assertIn("no-store", response["Cache-Control"])
        page = response.content.decode()
        for internal in (self.bar_a.dir_name, str(database), "TenancyError", "Traceback", "sqlite3"):
            self.assertNotIn(internal, page)
        # The way out is there, and nothing of the bar's own.
        self.assertIn(f'action="{reverse("accounts:logout")}"', page)
        self.assertNotIn("Grossiste", page)
        # The cause is the administrator's, in the server's log.
        self.assertIsInstance(logged.records[0].exc_info[1], TenancyError)
        # SQLite was never asked to open the path: no empty database made.
        self.assertFalse(database.exists())

    def test_a_database_that_will_not_open_says_so_too_and_is_let_go(self):
        """A file that is no database fails as the connection opens - its
        PRAGMAs (production's SQLITE_OPTIONS, which the test settings leave
        out and a tenant's connection copies) read the file: the same page,
        and the binding is undone, the next request of this thread starts
        from nothing."""
        self.client.force_login(self.user_a)
        before = connections["default"]
        paths.tenant_database(self.bar_a).write_bytes(b"ceci n'est pas une base SQLite " * 64)
        production = {"init_command": settings.SQLITE_OPTIONS["init_command"]}
        with (
            mock.patch.dict(connections.settings["default"]["OPTIONS"], production),
            self.assertLogs("accounts.middleware", "ERROR"),
        ):
            response = self.client.get(self.suppliers)
        self.assertContains(response, UNAVAILABLE, status_code=503)
        self.assertNotIn(b"not a database", response.content)
        self.assertIsNone(current_tenant())
        self.assertIs(connections["default"], before)

    def test_an_emptied_database_file_says_so_too_and_stays_untouched(self):
        """A file of 0 bytes - a restore that died, a full disk - is what
        SQLite takes for a NEW database: it opened, and the view failed on
        « no such table », a 500 with no word to the bar. It is no tenant's
        database: the same page, and nothing written into the file."""
        self.client.force_login(self.user_a)
        database = paths.tenant_database(self.bar_a)
        database.write_bytes(b"")
        with self.assertLogs("accounts.middleware", "ERROR") as logged:
            response = self.client.get(self.suppliers)
        self.assertContains(response, UNAVAILABLE, status_code=503)
        self.assertIsInstance(logged.records[0].exc_info[1], TenancyError)
        self.assertEqual(database.stat().st_size, 0)
        self.assertIsNone(current_tenant())

    def test_its_logout_forgets_the_tenant_s_drafts_as_the_topbar_s_does(self):
        """With Clear-Site-Data gone (LOGOUT-PREFS), a draft leaves the
        browser only through ui.js's `form.topbar-logout` listener - and
        this page's « Se déconnecter » was no such form, on a page carrying
        no scope and no ui.js: an unsaved count stayed on the shared device
        (review of the LOGOUT-PREFS fix). The scope needs no database."""
        self.client.force_login(self.user_a)
        os.remove(paths.tenant_database(self.bar_a))
        with self.assertLogs("accounts.middleware", "ERROR"):
            page = self.client.get(self.suppliers).content.decode()
        self.assertIn(f'data-tenant="{storage_scope(self.bar_a)}"', body_attributes(page))
        self.assertNotIn("data-tenant-legacy", body_attributes(page))
        self.assertIn(f'<form method="post" action="{reverse("accounts:logout")}" class="topbar-logout">', page)
        self.assertRegex(page, r'<script src="/static/js/ui\.js\?v=\d+" defer></script>')
        # A session from before 29/09: its old id too, as base.html gives it.
        session = self.client.session
        del session[TENANT_SESSION_KEY]
        session.save()
        with self.assertLogs("accounts.middleware", "ERROR"):
            page = self.client.get(self.suppliers).content.decode()
        self.assertIn(f'data-tenant-legacy="{self.bar_a.pk}"', body_attributes(page))
        # The login page, bound to nobody, still carries no scope nor script.
        self.client.logout()
        login = self.client.get(reverse("accounts:login")).content.decode()
        self.assertNotIn("data-tenant", login)
        self.assertNotIn("ui.js", login)

    def test_every_request_of_that_tenant_says_so_and_the_others_work(self):
        self.client.force_login(self.user_a)
        os.remove(paths.tenant_database(self.bar_a))
        with self.assertLogs("accounts.middleware", "ERROR"):
            poll = self.client.get(self.suppliers, HTTP_HX_REQUEST="true")
            home = self.client.get("/")
        self.assertEqual((poll.status_code, home.status_code), (503, 503))
        self.client.force_login(self.user_b)
        self.assertContains(self.client.get(self.suppliers), "Grossiste Beta")

    def test_a_view_s_own_tenancy_error_is_not_the_tenant_s(self):
        """Only the binding is caught: a view that fails for its own reasons
        still fails."""

        def view(request):
            raise TenancyError("the view's own")

        request = RequestFactory().get(self.suppliers)
        request.user = self.user_a
        with self.assertRaisesMessage(TenancyError, "the view's own"):
            TenantMiddleware(view)(request)


class StorageScopeTests(WithSuppliersMixin, TwoTenantsTestCase):
    def page(self, user=None) -> str:
        if user is not None:
            self.client.force_login(user)
        return self.client.get(reverse("inventory:stock_take_create")).content.decode()

    def test_the_pages_carry_an_opaque_scope_never_the_tenant_s_id(self):
        seen = {}
        for user, tenant in ((self.user_a, self.bar_a), (self.user_b, self.bar_b)):
            with self.subTest(tenant=tenant.name):
                attributes = body_attributes(self.page(user))
                scope = re.search(r'data-tenant="([^"]*)"', attributes).group(1)
                self.assertRegex(scope, r"^[0-9a-f]{16}$")
                self.assertEqual(scope, storage_scope(tenant))
                self.assertNotEqual(scope, str(tenant.pk))
                self.assertNotIn("data-tenant-legacy", attributes)
                seen[tenant.pk] = scope
        self.assertNotEqual(seen[self.bar_a.pk], seen[self.bar_b.pk])

    def test_the_scope_is_the_same_on_every_page_and_changes_with_the_key(self):
        self.client.force_login(self.user_a)
        first = body_attributes(self.page())
        second = body_attributes(self.client.get(self.suppliers).content.decode())
        self.assertEqual(first, second)
        self.assertEqual(storage_scope(self.bar_a), storage_scope(self.bar_a))
        with override_settings(SECRET_KEY="une-autre-cle-de-test-" + "x" * 40):
            self.assertNotEqual(storage_scope(self.bar_a), re.search(r'data-tenant="([^"]*)"', first).group(1))

    def test_only_a_session_from_before_is_given_the_old_prefix(self):
        self.client.force_login(self.user_a)
        page = self.page()
        self.assertNotIn("data-tenant-legacy", page)
        self.assertNotIn("tenant_storage_legacy.js", page)

        session = self.client.session
        del session[TENANT_SESSION_KEY]
        session.save()
        for visit in (1, 2):
            with self.subTest(visit=visit):
                page = self.page()
                self.assertIn(f'data-tenant-legacy="{self.bar_a.pk}"', body_attributes(page))
                self.assertIn("tenant_storage_legacy.js", page)

        # A login again is a session of today: no old prefix.
        self.client.force_login(self.user_a)
        page = self.page()
        self.assertNotIn("data-tenant-legacy", page)
        self.assertNotIn("tenant_storage_legacy.js", page)

    def test_a_page_no_tenant_is_bound_to_carries_none(self):
        page = self.client.get(reverse("accounts:login")).content.decode()
        self.assertNotIn("data-tenant", page)


#: Every key a page builds under « marginmate:<tenant> » that a logout
#: KEEPS, with why: none holds the bar's data. The others are drafts, and
#: static/js/ui.js forgets them at the logout (its `DRAFTS`).
KEPT_AT_LOGOUT = {
    "stock:rows": "which articles are opened on Produits & charges - ids, no figure",
    "stock:panel-closed": "the side panel folded",
    "achats:import-tab": "the way of adding purchases chosen last",
    "achats:pending-photos": "how many camera shots wait on Achats' import card, in this tab - a count, no photo",
}
#: `"marginmate:" + TENANT_SCOPE + "<what>` in a page's script.
BUILT_KEY = re.compile(r"""["']marginmate:["']\s*\+\s*TENANT_SCOPE\s*\+\s*["']([^"'{]+)""")


def ui_js() -> str:
    return (settings.BASE_DIR / "static" / "js" / "ui.js").read_text(encoding="utf-8")


def logout_script() -> str:
    """ui.js's « Se déconnecter » block: the function expression holding
    the form.topbar-logout listener."""
    source = ui_js()
    listener = source.index('matches("form.topbar-logout")')
    start = source.rindex("(function () {", 0, listener)
    return source[start : source.index("})();", listener)]


class LogoutKeepsThePreferencesTests(TwoTenantsTestCase):
    """LOAD-2 asked for the bar's DATA to leave the browser with the
    session: an unsaved count's articles and quantities, a pickup's counts
    and note, read from the public login page of a shared device.
    ``Clear-Site-Data: "storage"`` took everything with it - the gather's
    sources left unticked (Metro, a portal asking for a code every time),
    the folds, the tables' sort - and the next gather signed in to Metro
    with nothing on screen saying the choice was reset (review of 29/09,
    LOGOUT-PREFS). Now no header, and ui.js forgets this tenant's drafts
    alone."""

    def test_the_logout_sends_no_clear_site_data(self):
        self.client.force_login(self.user_a)
        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn("Clear-Site-Data", response)

    def test_nor_does_the_admin_s(self):
        owner = get_user_model().objects.create_superuser(
            "proprio@example.invalid", "proprio@example.invalid", PASSWORD
        )
        Membership.objects.create(user=owner, tenant=self.bar_a)
        self.client.force_login(owner)
        response = self.client.post(reverse("admin:logout"))
        self.assertNotIn("Clear-Site-Data", response)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_script_forgets_the_drafts_of_this_tenant_only(self):
        script = logout_script()
        drafts = re.search(r"var DRAFTS = \[([^\]]*)\];", script)
        self.assertIsNotNone(drafts, "ui.js's logout names its drafts in `var DRAFTS = [...]`")
        self.assertEqual(
            sorted(re.findall(r'"([^"]+)"', drafts.group(1))), ["consignes:brouillon", "push:", "stock-take-draft:"]
        )
        # Under this tenant's scope, and a session from before's old id.
        self.assertIn('"data-tenant"', script)
        self.assertIn('"data-tenant-legacy"', script)
        self.assertIn('"marginmate:espace-" + value + ":" + draft', script)
        # Never the preferences' prefix, and no whole-storage wipe.
        self.assertNotIn('"mm:', script)
        self.assertNotIn(".clear()", script)

    def test_every_key_a_page_builds_is_a_draft_or_kept_on_purpose(self):
        """A new key under « marginmate:<tenant> » is a decision: the bar's
        data (a draft: add it to ui.js's DRAFTS) or not (KEPT_AT_LOGOUT,
        with why)."""
        from inventory.tests.test_tenants_storage import STORAGE_FILES

        drafts = set(re.findall(r'"([^"]+)"', re.search(r"var DRAFTS = \[([^\]]*)\];", logout_script()).group(1)))
        built = set()
        for relative in STORAGE_FILES:
            source = (settings.BASE_DIR / relative).read_text(encoding="utf-8")
            found = BUILT_KEY.findall(source)
            with self.subTest(file=relative):
                # Every « "marginmate:" » of the file is one the pattern read.
                self.assertEqual(len(found), len(re.findall(r"""["']marginmate:["']""", source)))
            built.update(found)
        stems = (*drafts, *KEPT_AT_LOGOUT)
        unclassified = {key for key in built if not any(key.startswith(stem) for stem in stems)}
        self.assertEqual(unclassified, set(), "a key neither a draft (ui.js's DRAFTS) nor in KEPT_AT_LOGOUT")
        # And no stale entry: every stem still names a key some page builds.
        for stem in stems:
            with self.subTest(stem=stem):
                self.assertTrue(any(key.startswith(stem) for key in built))
