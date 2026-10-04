"""What an employee may open (accounts/access.py, the owner, 02/10/2026:
« séparer les opérations employés et employeur … choisir depuis la page
employeur à quelles pages l'employé peut avoir accès »).

The gate is `AccessMiddleware.process_view`, and it denies by default: a
route its app's areas (`APP_AREAS`) and its own (`VIEW_AREAS`) do not name is
the owner's. So the tests here walk EVERY route of the project: one whose app
is named nowhere fails `ClassificationTests` (a new app is classified on
purpose, never let through), and an employee given no area meets a refusal
on every one of them (`MemberSweepTests`) - a refusal drawn inside the site,
with his own links, that never runs the view (nothing is written).

Hiding a link is never the boundary, but the links are what an employee
reads first: `TopbarTests` checks that he sees his own pages and no other,
and that the owner's bar is drawn exactly as before.

« Liste de courses » (`shopping`, 04/10/2026) opens « Prévoir les courses »
and the shopping lists as « Produits & charges » does; the forecast's
settings and exclusions stay « Produits & charges »' alone
(`ShoppingAreaTests`). Accounts 0005 gave it to every employee already
invited (`ShoppingAreaMigrationTests`).

Every login and name here is invented; the addresses end in
@example.invalid.
"""

import importlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.models import Session
from django.db import router
from django.http import HttpResponse
from django.template import engines
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import Resolver404, URLResolver, get_resolver, resolve, reverse
from django.utils.html import escape

from accounts import access, paths
from accounts.access import (
    APP_AREAS,
    AREA_KEYS,
    AREAS,
    COST_AREAS,
    DEFAULT_AREAS,
    EVERYONE,
    FULL,
    HOME,
    MEDIA_AREAS,
    OWNER_ONLY,
    VIEW_AREAS,
    Access,
    AccessMiddleware,
    access_of,
    areas_of_file,
    areas_of_route,
)
from accounts.models import Membership, Tenant
from accounts.router import ACCOUNTS_ALIAS
from accounts.tests.test_middleware import SAMPLES, routes
from inventory.models import ShoppingExclusion, ShoppingList, ShoppingListItem
from invoices.models import Invoice
from tests.factories import make_invoice, make_product
from tests.runner import TEST_TENANT_NAME, TEST_TENANT_PK, employee_of_the_test_tenant, test_user
from tests.test_navigation import LABELS, active_labels, label_of, nav_links, section_shown
from tests.test_views_smoke import make_shopping_history, make_shopping_lists

NO_ACCESS = reverse("accounts:no_access")
#: « Prévoir les courses » and the shopping lists: they open with « Liste de
#: courses » or with « Produits & charges ».
SHOPPING_ROUTES = (
    "inventory:shopping_list",
    "inventory:shopping_rhythm",
    "inventory:shopping_lists",
    "inventory:shopping_list_page",
    "inventory:shopping_list_add",
    "inventory:shopping_list_add_all",
    "inventory:shopping_list_item_edit",
    "inventory:shopping_list_item_delete",
    "inventory:shopping_list_item_tick",
    "inventory:shopping_list_finish",
)
#: The forecast's settings and exclusions: « Produits & charges »' alone.
TUNING_ROUTES = ("inventory:shopping_settings", "inventory:shopping_exclude", "inventory:shopping_include")
#: The data migration giving every employee already invited the lists.
SHOPPING_AREA_MIGRATION = importlib.import_module("accounts.migrations.0005_shopping_area")
#: The routes every login of the espace opens, whatever is ticked.
EVERYONE_ROUTES = [
    "accounts:confirm_password",
    "accounts:media",
    "accounts:no_access",
    # Each login's own notification devices (notifications/devices.py).
    "notifications:device_delete",
    "notifications:home",
    "notifications:key",
    "notifications:subscribe",
    "notifications:sync",
    "notifications:test",
]
#: The reminders, alerts and automatic runs: the owner's, whatever is ticked.
AUTOMATION_ROUTES = (
    "notifications:reminders",
    "notifications:reminder_edit",
    "notifications:reminder_delete",
    "notifications:night",
    "notifications:events",
    "notifications:event_edit",
    "invoices:auto_gathers",
    "invoices:auto_gather_edit",
    "invoices:auto_gather_delete",
    "recipes:auto_sales",
    "recipes:auto_sales_edit",
    "recipes:auto_sales_delete",
)


def member(pages=()):
    """A Membership of an employee opening `pages`, in memory: `Access.of`
    reads its role and its pages, nothing else."""
    return Membership(role=Membership.Role.MEMBER, pages=list(pages) if isinstance(pages, tuple) else pages)


def walk(resolver=None, prefix="", namespace=""):
    """(route, URLPattern, namespaced name) for every route of the
    project, the admin's included (as test_no_single_mode's sweep walks
    them)."""
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        pattern = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            inner = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from walk(entry, pattern, inner)
        else:
            yield pattern, entry, f"{namespace}{entry.name or ''}"


def app_of(name: str) -> str:
    return name.rsplit(":", 1)[0] if ":" in name else ""


def sample_routes():
    """(url, ResolverMatch) for every route of the project's apps but the
    admin, its parameters filled with made-up values (test_middleware's
    SAMPLES) - once per address, as the URLconf resolves it. A route with a
    converter no sample fills, or whose sample resolves nowhere, is left
    out (and counted by the caller)."""
    found, seen = [], set()
    for route, _view in routes():
        try:
            url = re.sub(r"<(?:(\w+):)?\w+>", lambda m: SAMPLES[m.group(1) or "str"], route)
            match = resolve(url)
        except (KeyError, Resolver404):
            continue
        if url not in seen:
            seen.add(url)
            found.append((url, match))
    return found


def row_counts() -> dict:
    """How many rows every model holds, in the database the router sends it
    to - the tenant's and the accounts'. The login's own session is left
    out: the test client saves it as it pleases."""
    counts = {}
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed or model is Session:
            continue
        counts[model._meta.label] = model._default_manager.using(router.db_for_read(model)).count()
    return counts


def topbar_tenant(response) -> str:
    html = response.content.decode()
    found = re.search(r'<span class="topbar-tenant"[^>]*>.*?</span>', html, flags=re.DOTALL)
    assert found is not None, "no topbar-tenant"
    return found.group(0)


def brand_href(response) -> str:
    found = re.search(r'<a class="brand" href="([^"]*)">', response.content.decode())
    assert found is not None, "no brand"
    return found.group(1)


def link_labelled(response, label):
    links = [link for link in nav_links(response) if label_of(link) == label]
    assert len(links) == 1, (label, nav_links(response))
    return links[0]


class AreasOfFileTests(SimpleTestCase):
    """A stored file opens by the folder it RESOLVES to, the way the file
    view resolves it (accounts.views.open_stored): read by its first folder
    as written, « consignes/../invoices/f.pdf » - and on the server's own
    separator « consignes\\..\\invoices\\f.pdf » - gave an employee given
    Consignes the invoices' PDFs."""

    def test_a_pickup_s_photo_is_the_returnables(self):
        self.assertEqual(areas_of_file("consignes/photos/2026/01/a.jpg"), frozenset({"returnables"}))

    def test_an_invoice_s_file_and_a_ticket_s_photo_are_the_invoices(self):
        for name in ("invoices/2026/01/f.pdf", "receipts/x.jpg"):
            with self.subTest(name=name):
                self.assertEqual(areas_of_file(name), frozenset({"invoices"}))

    def test_a_name_climbing_into_another_folder_is_that_folder_s(self):
        for name in ("consignes/../invoices/f.pdf", "consignes/..\\invoices\\f.pdf", "consignes\\..\\invoices/f.pdf"):
            with self.subTest(name=name):
                self.assertEqual(areas_of_file(name), frozenset({"invoices"}))
                self.assertNotIn("returnables", areas_of_file(name))

    def test_a_name_out_of_the_media_folder_is_the_owner_s(self):
        """Climbing out, absolute, a drive, an NTFS stream (« : »), the
        folder itself: nobody but the owner, whatever is ticked."""
        for name in ("../x", "/etc/x", "C:/x", "consignes/a:b", ".", "consignes/../../x", ""):
            with self.subTest(name=name):
                self.assertEqual(areas_of_file(name), OWNER_ONLY)

    def test_the_top_folder_is_read_whatever_its_case(self):
        """Windows opens « CONSIGNES » as « consignes »: one folder, one
        answer."""
        self.assertEqual(areas_of_file("CONSIGNES/photos/x.jpg"), frozenset({"returnables"}))
        self.assertEqual(areas_of_file("Invoices/f.pdf"), frozenset({"invoices"}))

    def test_a_folder_named_nowhere_is_the_owner_s(self):
        for name in ("staging/archive.zip", "photos/x.jpg", "x.pdf"):
            with self.subTest(name=name):
                self.assertEqual(areas_of_file(name), OWNER_ONLY)

    def test_every_media_folder_names_real_areas(self):
        for folder, areas in MEDIA_AREAS.items():
            with self.subTest(folder=folder):
                self.assertTrue(areas)
                self.assertLessEqual(areas, AREA_KEYS)


class AreasOfRouteTests(SimpleTestCase):
    def test_a_route_of_its_own_wins_over_its_app(self):
        self.assertEqual(areas_of_route("invoices:invoice_add", "invoices"), frozenset({"invoices", "invoices_add"}))
        self.assertEqual(areas_of_route("inventory:stock_take_list", "inventory"), frozenset({"stock_takes"}))
        self.assertEqual(areas_of_route("invoices:invoice_type_create", "invoices"), OWNER_ONLY)

    def test_any_other_route_takes_its_app_s(self):
        self.assertEqual(areas_of_route("bank:bank_home", "bank"), frozenset({"bank"}))
        self.assertEqual(areas_of_route("invoices:invoice_list", "invoices"), frozenset({"invoices"}))
        self.assertEqual(areas_of_route("accounts:members", "accounts"), OWNER_ONLY)

    def test_an_app_named_nowhere_is_the_owner_s(self):
        """Deny by default: a new app opens to nobody but the owner until it
        is classified."""
        self.assertEqual(areas_of_route("nouvelle:page", "nouvelle"), OWNER_ONLY)
        self.assertEqual(areas_of_route("page_sans_app", ""), OWNER_ONLY)

    def test_the_shopping_pages_open_with_the_lists_or_the_products(self):
        """The forecast, its rhythm and every list route: « Liste de
        courses » or « Produits & charges ». The forecast's settings and
        exclusions change the list for everybody: its app's area alone, no
        entry of their own."""
        for name in SHOPPING_ROUTES:
            with self.subTest(name=name):
                self.assertEqual(areas_of_route(name, "inventory"), frozenset({"products", "shopping"}))
        for name in TUNING_ROUTES:
            with self.subTest(name=name):
                self.assertNotIn(name, VIEW_AREAS)
                self.assertEqual(areas_of_route(name, "inventory"), frozenset({"products"}))


class AccessTests(SimpleTestCase):
    def test_an_owner_opens_everything(self):
        owner = Access.of(Membership(role=Membership.Role.OWNER, pages=[]))
        self.assertTrue(owner.owner)
        self.assertEqual(owner.areas, AREA_KEYS)
        self.assertTrue(owner.sees_costs)
        self.assertTrue(owner.allows_any(OWNER_ONLY))

    def test_a_member_opens_the_areas_he_was_given_and_nothing_else(self):
        """A key no area has (an area renamed, a hand-edited row) and a value
        that is no key are passed over - never a TypeError, never an area."""
        given = Access.of(member(["returnables", "inconnue", 7, None, ["invoices"], {"invoices": True}]))
        self.assertFalse(given.owner)
        self.assertEqual(given.areas, frozenset({"returnables"}))
        self.assertTrue(given.allows("returnables"))
        self.assertFalse(given.allows("invoices"))
        self.assertFalse(given.allows_any(OWNER_ONLY))
        self.assertTrue(given.allows_any(EVERYONE))

    def test_pages_that_are_no_list_open_nothing(self):
        """« returnables » stored as a bare string must not open the areas
        its letters spell."""
        for pages in ("returnables", {"returnables": True}, None, 3):
            with self.subTest(pages=pages):
                self.assertEqual(Access.of(member(pages)).areas, frozenset())

    def test_a_template_reads_an_area_then_the_attributes(self):
        given = Access.of(member(["invoices"]))
        self.assertIs(given["invoices"], True)
        self.assertIs(given["returnables"], False)
        with self.assertRaises(KeyError):
            given["owner"]
        with self.assertRaises(KeyError):
            given["nope"]
        template = engines["django"].from_string(
            "{{ can.invoices }}|{{ can.returnables }}|{{ can.owner }}|{{ can.sees_costs }}|{{ can.home_url }}"
        )
        self.assertEqual(template.render({"can": given}), "True|False|False|True|/invoices/")

    def test_who_sees_what_articles_cost(self):
        """Counting alone shows no price, nor do the shopping lists; any
        area already showing what articles cost shows an inventory's values
        too."""
        self.assertNotIn("shopping", COST_AREAS)
        self.assertFalse(
            Access.of(member(["stock_takes", "returnables", "invoices_add", "staff", "bank", "shopping"])).sees_costs
        )
        for area in sorted(COST_AREAS):
            with self.subTest(area=area):
                self.assertTrue(Access.of(member(["stock_takes", area])).sees_costs)

    def test_the_lists_are_ticked_for_a_new_employee(self):
        """« Liste de courses » goes with the defaults, after « Consignes »:
        the boxes ticked are the first ones of the page."""
        self.assertEqual(DEFAULT_AREAS, ("invoices_add", "stock_takes", "returnables", "shopping"))
        self.assertEqual([area.key for area in AREAS][: len(DEFAULT_AREAS)], list(DEFAULT_AREAS))
        (lists,) = [area for area in AREAS if area.key == "shopping"]
        self.assertEqual(lists.label, "Liste de courses")
        self.assertEqual(lists.entry, "inventory:shopping_lists")
        # Its help says what it shows, and that it shows no price.
        self.assertIn("sans les prix", lists.help)

    def test_where_each_login_starts(self):
        lists = reverse("inventory:shopping_lists")
        cases = [
            (Access.of(Membership(role=Membership.Role.OWNER)), "/"),
            (Access.of(member(["returnables", "products"])), "/"),
            (Access.of(member(["invoices_add"])), reverse("invoices:invoice_add")),
            (Access.of(member(["stock_takes"])), reverse("inventory:stock_take_list")),
            # The first area in the order the owner reads them.
            (Access.of(member(["bank", "returnables", "stock_takes"])), reverse("inventory:stock_take_list")),
            # The lists are the start page only of one given nothing else:
            # beside another area, his usual page (accounts 0005 moved none).
            (Access.of(member(["shopping"])), lists),
            (Access.of(member(["shopping", "bank"])), reverse("bank:bank_home")),
            (Access.of(member(["shopping", "invoices"])), reverse("invoices:invoice_list")),
            (Access.of(member(["shopping", "products"])), "/"),
            # A new employee's boxes: « Ajouter des factures » first.
            (Access.of(member(list(DEFAULT_AREAS))), reverse("invoices:invoice_add")),
            (Access.of(member([])), NO_ACCESS),
        ]
        self.assertEqual(reverse(HOME), "/")
        for given, home in cases:
            with self.subTest(access=repr(given)):
                self.assertEqual(given.home_url, home)

    def test_full_is_what_a_request_with_no_membership_draws(self):
        self.assertTrue(FULL.owner)
        self.assertEqual(FULL.areas, AREA_KEYS)
        self.assertEqual(FULL.home_url, "/")
        request = RequestFactory().get("/")
        self.assertIs(access_of(request), FULL)
        request.access = Access.of(member(["bank"]))
        self.assertIs(access_of(request), request.access)


class ClassificationTests(SimpleTestCase):
    """Every route is classified on purpose: a route of an app APP_AREAS does
    not name would be the owner's in silence - right by default, but a new
    page an employee should open would never open, and nobody would know
    why. And a stale VIEW_AREAS entry is a rule about a page that is gone."""

    def test_every_route_s_app_is_classified(self):
        unclassified = []
        checked = 0
        for pattern, entry, name in walk():
            if name.startswith("admin:") or not getattr(entry.callback, "login_required", True):
                continue
            checked += 1
            if app_of(name) not in APP_AREAS:
                unclassified.append((pattern, name))
        self.assertEqual(unclassified, [])
        self.assertGreater(checked, 150)

    def test_every_route_named_on_its_own_exists(self):
        names = {name for _pattern, _entry, name in walk()}
        self.assertEqual(sorted(set(VIEW_AREAS) - names), [])

    def test_every_area_opens_on_a_route_that_exists(self):
        names = {name for _pattern, _entry, name in walk()}
        for area in AREAS:
            with self.subTest(area=area.key):
                self.assertIn(area.entry, names)
                # The entry opens with the area alone.
                match = resolve(reverse(area.entry))
                self.assertTrue(Access(owner=False, areas=[area.key]).opens(match))
        self.assertIn(HOME, names)

    def test_every_rule_names_real_areas(self):
        """A typo in an area's key opens nothing, silently."""
        for name, areas in [*APP_AREAS.items(), *VIEW_AREAS.items()]:
            with self.subTest(name=name):
                self.assertLessEqual(areas, AREA_KEYS | {"*"})

    def test_only_these_routes_open_for_every_login(self):
        """A page every login opens is a decision, not a default."""
        self.assertEqual(sorted(name for name, areas in VIEW_AREAS.items() if areas == EVERYONE), EVERYONE_ROUTES)
        self.assertNotIn("*", set().union(*APP_AREAS.values()))

    def test_the_owner_s_pages_open_to_no_area(self):
        """What OWNER_ONLY_PAGES promises on « Accès des employés »."""
        everything = Access(owner=False, areas=AREA_KEYS)
        for name in (
            "accounts:members",
            "accounts:credentials",
            "transfer:data_home",
            "invoices:invoice_type_create",
            "invoices:invoice_type_update",
            "inventory:stock_take_delete",
            "returnables:format_list",
            "returnables:type_list",
            "returnables:line_classify",
            "staff:signature_send",
            "staff:signature_countersign",
            "staff:signature_delete",
            *AUTOMATION_ROUTES,
        ):
            with self.subTest(name=name):
                self.assertFalse(everything.allows_any(areas_of_route(name, app_of(name))))


class MemberSweepTests(TestCase):
    """An employee given no area, on every route of the project, GET and
    POST: the gate answers before any view - a 403 drawn inside the site, or
    « / » sending him to his own first page - never a page of an area, never
    a 500. Only the pages of every login (`EVERYONE_ROUTES`) run their view.
    And nothing was written anywhere: a refused POST must not have reached a
    view first."""

    def test_an_employee_with_no_area_opens_no_page(self):
        employee = employee_of_the_test_tenant("sans-acces@example.invalid", name="Sans Accès")
        self.client.force_login(employee)
        before = row_counts()
        checked = 0
        for url, match in sample_routes():
            if not getattr(match.func, "login_required", True):
                continue
            everyone = VIEW_AREAS.get(match.view_name) == EVERYONE
            for method in ("get", "post"):
                with self.subTest(route=match.view_name, url=url, method=method):
                    response = getattr(self.client, method)(url)
                    checked += 1
                    if everyone:
                        self.assertLess(response.status_code, 500)
                    elif response.status_code == 302:
                        self.assertEqual(response["Location"], NO_ACCESS)
                    else:
                        self.assertContains(response, "Page non accessible", status_code=403)
        self.assertEqual(row_counts(), before)
        self.assertGreater(checked, 300)

    def test_an_employee_given_every_area_opens_none_of_the_owner_s_pages(self):
        """What no box opens - « Données », « Identifiants », « Accès des
        employés », and inside the areas the sources of invoices, the slips'
        formats, the timesheets' signatures, deleting a count - stays the
        owner's with every box ticked."""
        employee = employee_of_the_test_tenant("toutes-pages@example.invalid", sorted(AREA_KEYS))
        self.client.force_login(employee)
        before = row_counts()
        refused = set()
        for url, match in sample_routes():
            if not getattr(match.func, "login_required", True) or match.view_name == "accounts:media":
                continue
            if areas_of_route(match.view_name, match.app_name) != OWNER_ONLY:
                continue
            refused.add(match.view_name)
            for method in ("get", "post"):
                with self.subTest(route=match.view_name, url=url, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertContains(response, "Page non accessible", status_code=403)
        self.assertEqual(row_counts(), before)
        for name in (
            "accounts:members",
            "accounts:credentials",
            "transfer:data_home",
            "invoices:invoice_type_create",
            "invoices:invoice_type_update",
            "inventory:stock_take_delete",
            "returnables:format_list",
            "returnables:format_edit",
            "returnables:type_list",
            "returnables:line_classify",
            "staff:signature_send",
            "staff:signature_countersign",
            "staff:signature_delete_confirm",
            "staff:month_reopen",
            *AUTOMATION_ROUTES,
        ):
            with self.subTest(owner_only=name):
                self.assertIn(name, refused)

    def test_every_route_is_reached_by_the_sweep(self):
        """A route the sweep cannot build an address for is a route it does
        not check: there must be none."""
        built = {match.view_name for _url, match in sample_routes()}
        expected = {name for _pattern, _entry, name in walk() if not name.startswith("admin:")}
        # The one regex route (« signer/<rest> ») is public.
        self.assertEqual(sorted(expected - built), ["staff:sign_unknown"])


class AreaEntryTests(TestCase):
    """Given exactly one area, an employee reaches that area's first page and
    is refused another's; the owner reaches every one, as before."""

    def setUp(self):
        # A document to fix and a product to classify: the badges have
        # something to count.
        make_invoice(status=Invoice.Status.ERROR)
        make_product(raw_name="Sirop de test")

    def test_each_area_opens_its_own_first_page_only(self):
        for area in AREAS:
            with self.subTest(area=area.key):
                employee = employee_of_the_test_tenant(f"{area.key}@example.invalid", [area.key])
                self.client.force_login(employee)
                self.assertEqual(self.client.get(reverse(area.entry)).status_code, 200)
                other = "margins:margins_home" if area.key == "bank" else "bank:bank_home"
                self.assertContains(self.client.get(reverse(other)), "Page non accessible", status_code=403)

    def test_the_owner_opens_every_first_page(self):
        self.client.force_login(test_user())
        for area in AREAS:
            with self.subTest(area=area.key):
                self.assertEqual(self.client.get(reverse(area.entry)).status_code, 200)

    def test_home_sends_an_employee_without_products_to_his_first_page(self):
        """« / » is the login's landing, the brand and the error pages' way
        back: refused there, an employee would land on a refusal after every
        login."""
        employee = employee_of_the_test_tenant("consignes@example.invalid", ["returnables"])
        self.client.force_login(employee)
        response = self.client.get("/")
        self.assertRedirects(response, reverse("returnables:home"), fetch_redirect_response=False)
        with_products = employee_of_the_test_tenant("produits@example.invalid", ["returnables", "products"])
        self.client.force_login(with_products)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_an_employee_given_nothing_lands_on_no_access(self):
        employee = employee_of_the_test_tenant("rien@example.invalid")
        self.client.force_login(employee)
        response = self.client.get("/", follow=True)
        self.assertRedirects(response, NO_ACCESS)
        self.assertContains(response, "Aucune page ouverte")


class RefusedPageTests(TestCase):
    """The refusal is drawn inside the site, his own links on top, with the
    way to his first page - and, refusing a POST, says nothing was saved. It
    lights no link: the page asked for is none of his."""

    def setUp(self):
        employee = employee_of_the_test_tenant(
            "refus@example.invalid", ["stock_takes", "returnables"], name="Camille Exemple"
        )
        self.home = reverse("inventory:stock_take_list")
        self.client.force_login(employee)

    def test_a_get_is_refused_with_the_way_back(self):
        # An owner's page inside an area he opens: Consignes would be lit.
        response = self.client.get(reverse("returnables:format_list"))
        self.assertContains(response, "Page non accessible", status_code=403)
        self.assertContains(response, escape(access.REFUSED), status_code=403)
        self.assertContains(response, f'<a class="btn" href="{self.home}">', status_code=403)
        self.assertNotContains(response, escape(access.REFUSED_POST), status_code=403)
        self.assertEqual(active_labels(response), [])
        self.assertEqual([label_of(link) for link in nav_links(response)], ["Inventaires", "Consignes"])

    def test_a_post_says_nothing_was_saved(self):
        response = self.client.post(reverse("returnables:type_list"), {"name": "Fûts"})
        self.assertContains(response, "Page non accessible", status_code=403)
        self.assertContains(response, escape(access.REFUSED_POST), status_code=403)
        self.assertEqual(active_labels(response), [])

    def test_htmx_is_sent_to_his_first_page(self):
        """A poll answered a bare 403 would ask again every second."""
        response = self.client.get(reverse("invoices:invoice_list"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["HX-Redirect"], self.home)
        self.assertNotContains(response, "Page non accessible", status_code=403)

    def test_no_access_redirects_an_employee_who_has_pages(self):
        response = self.client.get(NO_ACCESS)
        self.assertRedirects(response, self.home, fetch_redirect_response=False)


class NoAccessPageTests(TestCase):
    def test_an_employee_given_nothing_is_told_so(self):
        employee = employee_of_the_test_tenant("aucune@example.invalid")
        self.client.force_login(employee)
        response = self.client.get(NO_ACCESS)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aucune page ouverte")
        self.assertEqual(nav_links(response), [])
        self.assertEqual(brand_href(response), NO_ACCESS)


class FailsClosedTests(TestCase):
    """The gate alone (process_view), around the URLconf's own views: a
    logged-in request that reached a page of the espace with no access read
    - a middleware moved, a request built another way - is never taken for
    the owner."""

    def gate(self, path, user, access_given=None):
        request = RequestFactory().get(path)
        request.user = user
        request.resolver_match = match = resolve(path)
        if access_given is not None:
            request.access = access_given
        return AccessMiddleware(lambda request: HttpResponse()).process_view(
            request, match.func, match.args, match.kwargs
        )

    def test_a_logged_in_request_without_access_is_refused(self):
        response = self.gate(reverse("bank:bank_home"), test_user())
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 403)

    def test_what_passes(self):
        employee = employee_of_the_test_tenant("passe@example.invalid", ["bank"])
        nothing = Access(owner=False)
        # Anonymous: the login's business (LoginRequiredMiddleware, before).
        self.assertIsNone(self.gate(reverse("bank:bank_home"), AnonymousUser()))
        # A public view: no login's business.
        self.assertIsNone(self.gate(reverse("accounts:login"), employee, nothing))
        # The admin keeps its own gate (superusers).
        self.assertIsNone(self.gate("/admin/", employee, nothing))
        # An area he opens.
        self.assertIsNone(self.gate(reverse("bank:bank_home"), employee, Access(owner=False, areas=["bank"])))
        self.assertIsNone(self.gate(reverse("bank:bank_home"), test_user(), Access(owner=True)))


class StoredFilesTests(TestCase):
    """/fichiers/ is one route for every folder: the gate reads the file's
    folder as the file view resolves it, and refuses BEFORE the view - the
    view itself would serve « consignes/..\\invoices\\… » on Windows, where
    a backslash is a separator."""

    def setUp(self):
        media = Path(paths.media_root())
        self.photo = "consignes/photos/test-acces-photo.jpg"
        self.invoice = "invoices/test-acces-facture.pdf"
        for name, content in ((self.photo, b"\xff\xd8\xff photo"), (self.invoice, b"%PDF-1.4 facture")):
            path = media / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.addCleanup(path.unlink, missing_ok=True)

    def fetch(self, url):
        response = self.client.get(url)
        if response.streaming:
            body = b"".join(response.streaming_content)
            response.close()
            return response, body
        return response, response.content

    def test_an_employee_given_returnables_opens_the_pickups_photos_only(self):
        self.client.force_login(employee_of_the_test_tenant("photos@example.invalid", ["returnables"]))
        response, body = self.fetch(reverse("accounts:media", args=[self.photo]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"\xff\xd8\xff photo")
        file_name = Path(self.invoice).name
        for url in (
            reverse("accounts:media", args=[self.invoice]),
            f"/fichiers/consignes/../invoices/{file_name}",
            f"/fichiers/consignes/..%5Cinvoices/{file_name}",
            f"/fichiers/consignes/..%5Cinvoices%5C{file_name}",
        ):
            # Reached, the view would find nothing (a 404) rather than stream
            # a mock for ever.
            with self.subTest(url=url), mock.patch("accounts.views.open_stored", return_value=None) as view_opened:
                response, body = self.fetch(url)
                self.assertEqual(response.status_code, 403)
                self.assertIn(b"Page non accessible", body)
                self.assertNotIn(b"%PDF", body)
                view_opened.assert_not_called()

    def test_an_employee_given_invoices_opens_their_files(self):
        self.client.force_login(employee_of_the_test_tenant("factures@example.invalid", ["invoices"]))
        response, body = self.fetch(reverse("accounts:media", args=[self.invoice]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"%PDF-1.4 facture")
        response, _body = self.fetch(reverse("accounts:media", args=[self.photo]))
        self.assertEqual(response.status_code, 403)

    def test_the_owner_opens_every_file(self):
        """The view resolves a climbing name into the folder it lands in:
        the gate, not the view, is what keeps it from an employee."""
        self.client.force_login(test_user())
        for name in (self.photo, self.invoice):
            with self.subTest(name=name):
                response, _body = self.fetch(reverse("accounts:media", args=[name]))
                self.assertEqual(response.status_code, 200)
        response, body = self.fetch(f"/fichiers/consignes/../invoices/{Path(self.invoice).name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"%PDF-1.4 facture")


class TopbarTests(TestCase):
    """The links an employee may follow, and only those; the owner's bar as
    it always was (tests/test_navigation.py LABELS)."""

    def setUp(self):
        make_invoice(status=Invoice.Status.ERROR)

    def labels(self, response):
        return [label_of(link) for link in nav_links(response)]

    def test_the_owner_s_bar_is_unchanged(self):
        self.client.force_login(test_user())
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertEqual(self.labels(response), LABELS)
        self.assertIn('<span class="badge">1</span>', link_labelled(response, "Factures"))
        self.assertEqual(brand_href(response), "/")
        self.assertEqual(
            topbar_tenant(response),
            f'<span class="topbar-tenant" title="{TEST_TENANT_NAME}">{TEST_TENANT_NAME}</span>',
        )

    def test_an_employee_sees_his_pages_only(self):
        employee = employee_of_the_test_tenant(
            "liens@example.invalid", ["returnables", "bank", "stock_takes"], name="Dominique Exemple"
        )
        self.client.force_login(employee)
        response = self.client.get(reverse("returnables:home"))
        self.assertEqual(self.labels(response), ["Banque", "Inventaires", "Consignes"])
        self.assertEqual(active_labels(response), ["Consignes"])
        self.assertEqual(brand_href(response), reverse("inventory:stock_take_list"))
        self.assertEqual(
            topbar_tenant(response),
            f'<span class="topbar-tenant" title="Dominique Exemple · {TEST_TENANT_NAME}">'
            f"Dominique Exemple · {TEST_TENANT_NAME}</span>",
        )

    def test_an_employee_with_no_name_is_shown_by_his_address(self):
        self.client.force_login(employee_of_the_test_tenant("anonyme@example.invalid", ["returnables"]))
        response = self.client.get(reverse("returnables:home"))
        self.assertIn(f"anonyme@example.invalid · {TEST_TENANT_NAME}</span>", topbar_tenant(response))

    def test_adding_invoices_only_leads_factures_to_the_add_page_without_a_badge(self):
        self.client.force_login(employee_of_the_test_tenant("ajout@example.invalid", ["invoices_add"]))
        response = self.client.get(reverse("invoices:invoice_add"))
        self.assertEqual(self.labels(response), ["Factures"])
        link = link_labelled(response, "Factures")
        self.assertIn(f'href="{reverse("invoices:invoice_add")}"', link)
        self.assertNotIn('class="badge"', link)
        self.assertEqual(brand_href(response), reverse("invoices:invoice_add"))

    def test_the_gaps_only_lead_inventaires_to_the_gap_filler(self):
        self.client.force_login(employee_of_the_test_tenant("ecarts@example.invalid", ["stock_gaps"]))
        response = self.client.get(reverse("inventory:stock_gap_filler"))
        self.assertEqual(self.labels(response), ["Inventaires"])
        self.assertIn(f'href="{reverse("inventory:stock_gap_filler")}"', link_labelled(response, "Inventaires"))

    def test_data_is_never_an_employee_s(self):
        """Given every area, an employee still has no « Données »: it is the
        owner's alone (OWNER_ONLY_PAGES)."""
        self.client.force_login(employee_of_the_test_tenant("tout@example.invalid", sorted(AREA_KEYS)))
        response = self.client.get(reverse("returnables:home"))
        self.assertEqual(self.labels(response), [label for label in LABELS if label != "Données"])

    def test_the_lists_only_lead_courses_to_the_lists(self):
        """Given « Liste de courses » without « Produits & charges », his
        link reads « Courses » - « Produits & charges » would promise the
        prices and charges his employer did not open - and leads to the
        lists, with no badge: the products to classify are not his."""
        make_product(raw_name="Sirop de test")
        self.client.force_login(employee_of_the_test_tenant("courses@example.invalid", ["shopping"]))
        lists = reverse("inventory:shopping_lists")
        response = self.client.get(lists)
        self.assertEqual(self.labels(response), ["Courses"])
        link = link_labelled(response, "Courses")
        self.assertIn(f'href="{lists}"', link)
        self.assertNotIn('class="badge"', link)
        self.assertEqual(active_labels(response), ["Courses"])
        self.assertEqual(section_shown(response), "Courses")
        self.assertEqual(brand_href(response), lists)

    def test_with_the_products_the_link_is_produits_et_charges(self):
        self.client.force_login(employee_of_the_test_tenant("deux@example.invalid", ["shopping", "products"]))
        response = self.client.get(reverse("inventory:shopping_lists"))
        self.assertEqual(self.labels(response), ["Produits &amp; charges"])
        self.assertEqual(active_labels(response), ["Produits &amp; charges"])
        self.assertEqual(brand_href(response), "/")

    def test_courses_takes_the_place_of_produits_et_charges(self):
        """« Courses » is drawn where « Produits & charges » is, and never
        beside it: no employee's bar holds more links than with every box
        ticked - the bar the topbar's browser tests measure. And a new
        employee's boxes lead to his four pages."""
        everything = sorted(AREA_KEYS)
        for email, pages, labels in (
            ("tout@example.invalid", everything, [label for label in LABELS if label != "Données"]),
            (
                "sans-produits@example.invalid",
                [key for key in everything if key != "products"],
                ["Courses", *[label for label in LABELS[1:] if label != "Données"]],
            ),
            ("nouveau@example.invalid", list(DEFAULT_AREAS), ["Courses", "Factures", "Inventaires", "Consignes"]),
        ):
            with self.subTest(email=email):
                self.client.force_login(employee_of_the_test_tenant(email, pages))
                self.assertEqual(self.labels(self.client.get(reverse("inventory:shopping_lists"))), labels)


class QueryCostTests(TestCase):
    """The access is read in the membership's own row, the one the tenant
    comes in (accounts.middleware.membership_of): an employee's request asks
    the accounts database exactly what an owner's does."""

    def test_an_employee_s_page_costs_three_accounts_queries_as_the_owner_s(self):
        employee = employee_of_the_test_tenant("requetes@example.invalid", ["returnables"])
        for user in (employee, test_user()):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                with self.assertNumQueries(3, using="accounts"):
                    self.assertEqual(self.client.get(reverse("returnables:home")).status_code, 200)

    def test_the_context_processor_reads_nothing(self):
        request = RequestFactory().get("/")
        for given in (None, Access.of(member(["invoices_add"]))):
            if given is not None:
                request.access = given
            with self.subTest(access=repr(given)):
                with self.assertNumQueries(0), self.assertNumQueries(0, using="accounts"):
                    can = access.context(request)["can"]
                    drawn = (can.home_url, can["invoices"], can["invoices_add"], can.owner, can.sees_costs)
                self.assertTrue(drawn[0])
                self.assertIs(drawn[2], True)


class ShoppingAreaTests(TestCase):
    """« Liste de courses » (`shopping`): the shopping lists and « Prévoir
    les courses » open with it as with « Produits & charges »; what changes
    the forecast for everybody - its settings, its exclusions - stays
    « Produits & charges »', and its forms are not drawn for him; its money
    stays for one shown what articles cost. Without either area, every
    shopping route is refused before its view: nothing written. The data:
    tests/test_views_smoke.py's invented history and lists."""

    def setUp(self):
        self.made = make_shopping_history()
        self.lists = make_shopping_lists(self.made)
        self.store = self.made.wholesaler.pk
        self.exclusion = ShoppingExclusion.objects.create(stock_type=self.made.rum)

    def log_in(self, *pages, email="courses@example.invalid"):
        employee = employee_of_the_test_tenant(email, pages, name="Sacha Exemple")
        self.client.force_login(employee)
        return employee

    def posted(self) -> dict:
        """What each shopping route is posted, as its page's form posts it:
        reaching its view, every one of them writes."""
        return {
            "inventory:shopping_list": {},
            "inventory:shopping_rhythm": {},
            "inventory:shopping_lists": {},
            "inventory:shopping_list_page": {},
            "inventory:shopping_list_add": {"fournisseur": self.store, "nom": "Serviettes exemple"},
            "inventory:shopping_list_add_all": {"fournisseur": self.store},
            "inventory:shopping_list_item_edit": {
                "fournisseur": self.store,
                "ligne": self.lists.bread.pk,
                "quantite": "5",
                "note": "Complet",
            },
            "inventory:shopping_list_item_delete": {"fournisseur": self.store, "ligne": self.lists.bread.pk},
            "inventory:shopping_list_item_tick": {"fournisseur": self.store, "ligne": self.lists.beer.pk, "pris": "1"},
            "inventory:shopping_list_finish": {"liste": self.lists.open.pk, "garder": "1"},
            "inventory:shopping_settings": {"fournisseur": self.store, "seuil": "30", "memoire": "6"},
            "inventory:shopping_exclude": {
                "fournisseur": self.store,
                "article": self.made.syrup.pk,
                "chez": self.store,
                "retour": "liste",
            },
            "inventory:shopping_include": {"fournisseur": self.store, "exclusion": self.exclusion.pk},
        }

    def forecast(self) -> str:
        return self.client.get(reverse("inventory:shopping_list"), {"fournisseur": self.store}).content.decode()

    def test_the_lists_alone_open_the_shopping_routes_and_no_other_page_of_the_app(self):
        """Swept over every route: among the inventory app's, exactly the
        shopping ones; elsewhere, only the pages of every login."""
        given = Access(owner=False, areas=["shopping"])
        opened = {match.view_name for _url, match in sample_routes() if given.opens(match)}
        self.assertEqual(sorted(name for name in opened if name.startswith("inventory:")), sorted(SHOPPING_ROUTES))
        self.assertLessEqual({name for name in opened if not name.startswith("inventory:")}, set(EVERYONE_ROUTES))

    def test_the_lists_alone_open_the_lists_the_forecast_and_the_rhythm(self):
        self.log_in("shopping")
        page = reverse("inventory:shopping_list_page")
        for url in (
            reverse("inventory:shopping_lists"),
            f"{page}?fournisseur={self.store}",
            f"{page}?fournisseur={self.store}&mode=courses",
            f"{page}?fournisseur={self.store}&ligne={self.lists.beer.pk}",
            f"{page}?liste={self.lists.finished.pk}",
            reverse("inventory:shopping_list"),
            f"{reverse('inventory:shopping_list')}?fournisseur={self.store}&dans=30",
            reverse("inventory:shopping_rhythm"),
            f"{reverse('inventory:shopping_rhythm')}?fournisseur={self.store}",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_lists_alone_write_the_lists(self):
        """The gate lets his posts through to their views: an item added, a
        tick, the run finished - each under his own login."""
        employee = self.log_in("shopping")
        posted = self.posted()
        for name in (
            "inventory:shopping_list_add",
            "inventory:shopping_list_item_tick",
            "inventory:shopping_list_finish",
        ):
            with self.subTest(route=name):
                self.assertEqual(self.client.post(reverse(name), posted[name]).status_code, 302)
        added = ShoppingListItem.objects.get(label="Serviettes exemple", shopping_list=self.lists.open)
        self.assertEqual(added.added_by, employee.get_username())
        self.lists.beer.refresh_from_db()
        self.assertEqual(self.lists.beer.checked_by, employee.get_username())
        self.lists.open.refresh_from_db()
        self.assertEqual(self.lists.open.finished_by, employee.get_username())
        # What he did not tick waits on the next list, made by him.
        carried = ShoppingList.objects.get(supplier=self.made.wholesaler, finished_at__isnull=True)
        self.assertEqual(carried.created_by, employee.get_username())
        self.assertTrue(carried.items.filter(label="Serviettes exemple").exists())

    def test_every_shopping_route_answers_him_without_a_refusal(self):
        """GET and POST, as his pages post them: a page, or the redirect a
        form answers with - never the gate's refusal, never a 500."""
        self.log_in("shopping")
        posted = self.posted()
        for name in SHOPPING_ROUTES:
            for method in ("get", "post"):
                with self.subTest(route=name, method=method):
                    data = posted[name] if method == "post" else {}
                    response = getattr(self.client, method)(reverse(name), data)
                    self.assertIn(response.status_code, (200, 302))

    def test_the_lists_alone_are_refused_the_forecast_s_settings_and_exclusions(self):
        """Refused by the gate, before their views: nothing written, and the
        refusal says so."""
        self.log_in("shopping")
        posted = self.posted()
        before = row_counts()
        for name in TUNING_ROUTES:
            for method in ("get", "post"):
                with self.subTest(route=name, method=method):
                    response = getattr(self.client, method)(reverse(name), posted[name] if method == "post" else {})
                    self.assertContains(response, "Page non accessible", status_code=403)
                    if method == "post":
                        self.assertContains(response, escape(access.REFUSED_POST), status_code=403)
                    # His one link, lit nowhere: the page is none of his.
                    self.assertEqual([label_of(link) for link in nav_links(response)], ["Courses"])
                    self.assertEqual(active_labels(response), [])
        self.assertEqual(row_counts(), before)
        self.assertTrue(ShoppingExclusion.objects.filter(pk=self.exclusion.pk).exists())

    def test_the_rest_of_produits_et_charges_is_refused(self):
        """« / » - « Produits & charges »' list - sends him to the lists; an
        article's form is refused."""
        self.log_in("shopping")
        self.assertRedirects(
            self.client.get(reverse("inventory:stock_list")),
            reverse("inventory:shopping_lists"),
            fetch_redirect_response=False,
        )
        for name in ("inventory:stock_type_create", "inventory:review_queue"):
            with self.subTest(route=name):
                self.assertContains(self.client.get(reverse(name)), "Page non accessible", status_code=403)

    def test_the_forecast_draws_no_tuning_form_and_no_money_for_him(self):
        """He may add to the list from the forecast; he is not shown what he
        may not post - « Pas ici », « Ne plus proposer », « Réglages »,
        « Exclusions » - nor « Total HT », and « Rythme d'achat » draws no
        « Ne jamais proposer »."""
        self.log_in("shopping")
        html = self.forecast()
        self.assertIn(f'action="{reverse("inventory:shopping_list_add")}"', html)
        for route in TUNING_ROUTES:
            with self.subTest(route=route):
                self.assertNotIn(f'action="{reverse(route)}"', html)
        for words in ("Pas ici", "Ne plus proposer", "Réglages", 'id="reglages"', 'id="exclusions"', "Total HT"):
            with self.subTest(words=words):
                self.assertNotIn(words, html)
        rhythm = self.client.get(reverse("inventory:shopping_rhythm"), {"fournisseur": self.store}).content.decode()
        self.assertNotIn("Ne jamais proposer", rhythm)
        self.assertNotIn(f'action="{reverse("inventory:shopping_exclude")}"', rhythm)

    def test_with_an_area_showing_costs_he_sees_the_money_and_still_no_tuning_form(self):
        self.log_in("shopping", "invoices")
        html = self.forecast()
        self.assertIn("Total HT", html)
        for route in TUNING_ROUTES:
            with self.subTest(route=route):
                self.assertNotIn(f'action="{reverse(route)}"', html)

    def test_the_owner_is_drawn_every_form(self):
        self.client.force_login(test_user())
        html = self.forecast()
        for route in (*TUNING_ROUTES, "inventory:shopping_list_add"):
            with self.subTest(route=route):
                self.assertIn(f'action="{reverse(route)}"', html)
        self.assertIn("Total HT", html)

    def test_without_the_lists_every_shopping_route_is_refused(self):
        """An employee given « Faire un inventaire » alone: GET and POST,
        every shopping route refused before its view - nothing written - and
        a tick sent by htmx to his own first page."""
        self.log_in("stock_takes")
        posted = self.posted()
        before = row_counts()
        for name in (*SHOPPING_ROUTES, *TUNING_ROUTES):
            for method in ("get", "post"):
                with self.subTest(route=name, method=method):
                    response = getattr(self.client, method)(reverse(name), posted[name] if method == "post" else {})
                    self.assertContains(response, "Page non accessible", status_code=403)
        response = self.client.post(
            reverse("inventory:shopping_list_item_tick"),
            posted["inventory:shopping_list_item_tick"],
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["HX-Redirect"], reverse("inventory:stock_take_list"))
        self.assertEqual(row_counts(), before)

    def test_the_products_alone_open_the_lists_too(self):
        employee = self.log_in("products")
        self.assertEqual(self.client.get(reverse("inventory:shopping_lists")).status_code, 200)
        page = reverse("inventory:shopping_list_page")
        self.assertEqual(self.client.get(page, {"fournisseur": self.store}).status_code, 200)
        posted = self.posted()["inventory:shopping_list_add"]
        self.assertEqual(self.client.post(reverse("inventory:shopping_list_add"), posted).status_code, 302)
        self.assertEqual(ShoppingListItem.objects.get(label="Serviettes exemple").added_by, employee.get_username())
        # And the forecast's settings, his app's.
        self.assertIn(f'action="{reverse("inventory:shopping_settings")}"', self.forecast())


class ShoppingAreaMigrationTests(TestCase):
    """accounts/migrations/0005_shopping_area: every employee already
    invited, of every espace, opens « Liste de courses » (the owner,
    04/10/2026); the pages are kept in the page's order, the new key at its
    place, and anything stored that is no key stays, after them. The owner's
    rows are left as they are. Run on the central database, as `migrate`
    runs it there (the router sends accounts' rows to ACCOUNTS_ALIAS)."""

    def migrate(self):
        SHOPPING_AREA_MIGRATION.give_every_employee_the_shopping_lists(
            apps, SimpleNamespace(connection=SimpleNamespace(alias=ACCOUNTS_ALIAS))
        )

    def employee(self, pages, email, tenant_id=TEST_TENANT_PK) -> Membership:
        """An employee's membership storing `pages` exactly - a value no form
        would store included."""
        user = employee_of_the_test_tenant(email)
        membership = Membership.objects.get(user=user)
        Membership.objects.filter(pk=membership.pk).update(pages=pages, tenant_id=tenant_id)
        return membership

    def pages_of(self, membership) -> object:
        return Membership.objects.get(pk=membership.pk).pages

    def test_an_employee_given_the_areas_of_0003_gets_the_lists_in_their_place(self):
        before_0005 = [key for key in SHOPPING_AREA_MIGRATION.AREAS_IN_0005 if key != "shopping"]
        given = self.employee(before_0005, "tout-0003@example.invalid")
        self.migrate()
        self.assertEqual(self.pages_of(given), SHOPPING_AREA_MIGRATION.AREAS_IN_0005)
        self.assertEqual(len(self.pages_of(given)), 11)

    def test_each_employee_s_pages_gain_the_lists_in_the_page_s_order(self):
        cases = [
            (["returnables", "invoices_add"], ["invoices_add", "returnables", "shopping"]),
            ([], ["shopping"]),
            (["bank"], ["shopping", "bank"]),
            (["returnables", "returnables"], ["returnables", "shopping"]),
            # A key no area has, a value that is no key: kept, after.
            (["returnables", "inconnue", 7], ["returnables", "shopping", "inconnue", 7]),
            (["staff", {"bank": True}, ["products"]], ["shopping", "staff", {"bank": True}, ["products"]]),
            # Stored as no list: it opened nothing; now the lists.
            ({"returnables": True}, ["shopping"]),
            ("returnables", ["shopping"]),
            (3, ["shopping"]),
        ]
        given = [
            (self.employee(pages, f"cas-{number}@example.invalid"), wanted)
            for number, (pages, wanted) in enumerate(cases)
        ]
        self.migrate()
        for membership, wanted in given:
            with self.subTest(email=membership.user.username):
                self.assertEqual(self.pages_of(membership), wanted)

    def test_an_employee_already_given_the_lists_is_left_as_he_is(self):
        given = self.employee(["shopping", "bank", "inconnue"], "deja@example.invalid")
        self.migrate()
        self.assertEqual(self.pages_of(given), ["shopping", "bank", "inconnue"])

    def test_an_employee_of_another_espace_gets_them_too_and_the_owner_is_left_alone(self):
        """The logins are central: every espace's employees. An owner opens
        everything already - his row is not rewritten."""
        neighbour = Tenant.objects.create(name="Bar Voisin", dir_name="voisin000005")
        elsewhere = self.employee(["returnables"], "voisin@example.invalid", tenant_id=neighbour.pk)
        owner = Membership.objects.get(user=test_user(), tenant_id=TEST_TENANT_PK)
        self.assertEqual(owner.role, Membership.Role.OWNER)
        Membership.objects.filter(pk=owner.pk).update(pages=["returnables"])
        self.migrate()
        self.assertEqual(self.pages_of(elsewhere), ["returnables", "shopping"])
        self.assertEqual(self.pages_of(owner), ["returnables"])

    def test_no_employee_s_start_page_moves(self):
        """The owner asked that every employee get the lists, not that his
        start page change: one already invited logs in, clicks the brand or
        « Revenir à l'accueil » and lands where he did before 0005."""
        cases = [
            ["invoices"],
            ["bank"],
            ["staff"],
            ["recipes", "margins"],
            ["stock_gaps"],
            [key for key in DEFAULT_AREAS if key != "shopping"],
            ["returnables", "bank"],
        ]
        given = []
        for number, pages in enumerate(cases):
            membership = self.employee(pages, f"accueil-{number}@example.invalid")
            given.append((membership, Access.of(Membership.objects.get(pk=membership.pk)).home_url))
        self.migrate()
        for membership, home in given:
            with self.subTest(email=membership.user.username):
                now = Membership.objects.get(pk=membership.pk)
                self.assertIn("shopping", now.pages)
                self.assertEqual(Access.of(now).home_url, home)

    def test_run_twice_it_changes_nothing_more(self):
        given = self.employee(["staff", "invoices_add", "inconnue"], "deux-fois@example.invalid")
        self.migrate()
        once = self.pages_of(given)
        self.migrate()
        self.assertEqual(self.pages_of(given), once)
        self.assertEqual(once, ["invoices_add", "shopping", "staff", "inconnue"])

    def test_the_areas_it_knows_are_the_page_s_in_their_order(self):
        """AREAS as the migration found them: a later area joins AREAS, never
        this list (a migration replays what it did)."""
        known = SHOPPING_AREA_MIGRATION.AREAS_IN_0005
        self.assertIn("shopping", known)
        self.assertEqual([area.key for area in AREAS if area.key in known], known)
        self.assertLessEqual(set(known), AREA_KEYS)

    def test_it_follows_the_push_devices_and_goes_back_doing_nothing(self):
        migration = SHOPPING_AREA_MIGRATION.Migration
        self.assertEqual(migration.dependencies, [("accounts", "0004_pushdevice")])
        (operation,) = migration.operations
        self.assertIs(operation.reverse_code, type(operation).noop)
        self.assertIs(operation.code, SHOPPING_AREA_MIGRATION.give_every_employee_the_shopping_lists)
        self.assertIn("Liste de courses", SHOPPING_AREA_MIGRATION.__doc__)
