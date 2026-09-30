"""What the browser remembers carries the espace.

localStorage and sessionStorage belong to an ORIGIN, not to a login: one
device used for two bars, one after the other, reads the same storage. A key
built from a pk (« stock-take-draft:5 », the articles opened on the stock
page) or from an address holding one (ui.js's `data-persist`, datatable.js's
sort, both keyed by `location.pathname`) is therefore the OTHER bar's pk 5 as
well: bar A's unsaved counts offered in bar B's new stock take, with ids that
name other articles, and B's first save wiping A's draft.

So every key starts with the espace - base.html's ``<body data-tenant>``: an
opaque scope (`accounts.tenancy.storage_scope`, 16 hex characters), not the
espace's id, which told any bar how many espaces were opened before its own
(security audit LB-6, 29/09/2026) - empty only on a page no espace is bound
to. The keys of a session opened before, built with the id, are moved under
the scope once (static/js/espace_storage_legacy.js).

Here: the attribute on each espace's pages, and a check of the sources that no key is
a bare literal and every key is built through the espace. What the keys DO
in a browser is `test_stock_take_draft_espaces_browser.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

from accounts.tests.support import TwoTenantsTestCase

#: Every file of the application that reads or writes browser storage. A key
#: built from no pk is keyed all the same (Achats' import tab): one device
#: used for two bars would carry one bar's choice into the other's page, and
#: one rule is one check.
STORAGE_FILES = (
    "static/js/ui.js",
    "static/js/datatable.js",
    "inventory/templates/inventory/stock_list.html",
    "inventory/templates/inventory/stock_take_form.html",
    "inventory/templates/inventory/stock_take_detail.html",
    "invoices/templates/invoices/purchases.html",
    # « Consignes »: the reprise not sent yet (its counts, its note).
    "static/js/returnables.js",
)

#: The one script that builds no key: it moves a session-from-before's keys,
#: found by the old prefix, under the scope (LegacyKeysTests below).
LEGACY_KEY_MOVER = "static/js/espace_storage_legacy.js"


class NoFileIsForgottenTests(SimpleTestCase):
    def test_every_template_and_script_using_browser_storage_is_listed(self):
        """The application's own files. A minified library is not read:
        htmx's own key - `htmx-history-cache`, whole pages kept for Back -
        is switched off in multi mode by base.html's `htmx-config` meta
        (`historyCacheSize` 0, EachEspaceItsOwnTests below)."""
        base = Path(settings.BASE_DIR)
        found = sorted(
            path.relative_to(base).as_posix()
            for pattern in ("*/templates/**/*.html", "templates/**/*.html", "static/js/*.js")
            for path in base.glob(pattern)
            if not path.name.endswith(".min.js")
            and re.search(r"\b(?:localStorage|sessionStorage)\b", path.read_text(encoding="utf-8"))
        )
        self.assertEqual(found, sorted((*STORAGE_FILES, LEGACY_KEY_MOVER)))

#: A storage call, and the expression it names its key with.
STORAGE_CALL = re.compile(
    r"(?:localStorage|sessionStorage|store\(element\))\s*\.\s*(?:getItem|setItem|removeItem)\(\s*([^,)]+(?:\([^)]*\))?)"
)
#: How each file names the espace: a variable in a page's script, a function
#: in the shared scripts.
SCOPE = re.compile(r"ESPACE_SCOPE|espaceScope\(\)")


def source_of(relative: str) -> str:
    return (Path(settings.BASE_DIR) / relative).read_text(encoding="utf-8")


def definition_of(source: str, key: str) -> str:
    """`var KEY = …;` or `function key(…) { … }`, as written."""
    name = key.split("(")[0].strip()
    variable = re.search(rf"var {re.escape(name)} = [^;]+;", source)
    if variable:
        return variable.group(0)
    function = re.search(rf"function {re.escape(name)}\([^)]*\)\s*\{{[^}}]*\}}", source)
    return function.group(0) if function else ""


class EveryKeyCarriesTheEspaceTests(SimpleTestCase):
    def test_every_storage_file_reads_the_espace(self):
        for relative in STORAGE_FILES:
            with self.subTest(file=relative):
                self.assertIn("data-tenant", source_of(relative))

    def test_no_key_is_a_bare_literal(self):
        for relative in STORAGE_FILES:
            keys = STORAGE_CALL.findall(source_of(relative))
            with self.subTest(file=relative):
                self.assertTrue(keys, "no storage call found - is the pattern still right?")
            for key in keys:
                with self.subTest(file=relative, key=key):
                    self.assertNotRegex(key.strip(), r"^[\"']")

    def test_every_key_is_built_through_the_espace(self):
        for relative in STORAGE_FILES:
            source = source_of(relative)
            for key in set(STORAGE_CALL.findall(source)):
                with self.subTest(file=relative, key=key):
                    definition = definition_of(source, key)
                    self.assertTrue(definition, f"{key} is defined nowhere in {relative}")
                    self.assertRegex(definition, SCOPE)


class LegacyKeysTests(SimpleTestCase):
    """espace_storage_legacy.js touches the old id's keys only, and writes
    them under the scope - what it does in Chrome is
    test_stock_take_draft_espaces_browser.py."""

    def test_it_reads_the_old_id_and_the_scope_from_the_page(self):
        source = source_of(LEGACY_KEY_MOVER)
        self.assertIn('getAttribute("data-tenant-legacy")', source)
        self.assertIn('getAttribute("data-tenant")', source)
        self.assertRegex(source, r'var ESPACE_SCOPE = "espace-" \+ scope \+ ":";')

    def test_only_keys_of_the_old_id_are_moved(self):
        source = source_of(LEGACY_KEY_MOVER)
        self.assertRegex(source, r'new RegExp\("\^\(marginmate\|mm\):" \+ LEGACY_ESPACE_SCOPE')
        self.assertRegex(source, r"function newKey\(app, rest\) \{ return app \+ \":\" \+ ESPACE_SCOPE \+ rest; \}")


#: base.html's head in multi mode: htmx keeps no page in the browser.
NO_HTMX_HISTORY = "<meta name=\"htmx-config\" content='{\"historyCacheSize\":0}'>"


class EachEspaceItsOwnTests(TwoTenantsTestCase):
    def test_each_espace_s_pages_carry_its_own(self):
        """Its scope - never its id (accounts/tests/test_sessions.py has the
        rest: 16 hex characters, one per espace, the old id for a session
        from before only)."""
        from accounts.tenancy import storage_scope

        for user, tenant in ((self.user_a, self.bar_a), (self.user_b, self.bar_b)):
            with self.subTest(espace=tenant.name):
                self.client.force_login(user)
                page = self.client.get(reverse("inventory:stock_take_create")).content.decode()
                self.assertIn(f'<body data-tenant="{storage_scope(tenant)}">', page)
                self.assertNotIn(f'data-tenant="{tenant.pk}"', page)

    def test_htmx_keeps_no_page_of_any_espace(self):
        """htmx 1.9 keeps whole pages under ONE key of the origin
        (`htmx-history-cache`), the one key not built through the espace:
        Achats' boosted tabs left bar A's invoices, amounts and name there
        for bar B's pages to read. With `historyCacheSize` 0 it keeps none,
        removes what an older page left, and Back asks the server."""
        for user, tenant in ((self.user_a, self.bar_a), (self.user_b, self.bar_b)):
            with self.subTest(espace=tenant.name):
                self.client.force_login(user)
                page = self.client.get(reverse("invoices:invoice_list")).content.decode()
                head = page[: page.index("</head>")]
                self.assertIn(NO_HTMX_HISTORY, head)
                self.assertLess(head.index(NO_HTMX_HISTORY), head.index("htmx.min"))
