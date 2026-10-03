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

Every login and name here is invented; the addresses end in
@example.invalid.
"""

import re
from pathlib import Path
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
from accounts.models import Membership
from accounts.tests.test_middleware import SAMPLES, routes
from invoices.models import Invoice
from tests.factories import make_invoice, make_product
from tests.runner import TEST_TENANT_NAME, employee_of_the_test_tenant, test_user
from tests.test_navigation import LABELS, active_labels, label_of, nav_links

NO_ACCESS = reverse("accounts:no_access")
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
        """Counting alone shows no price; any area already showing what
        articles cost shows an inventory's values too."""
        self.assertFalse(Access.of(member(["stock_takes", "returnables", "invoices_add", "staff", "bank"])).sees_costs)
        for area in sorted(COST_AREAS):
            with self.subTest(area=area):
                self.assertTrue(Access.of(member(["stock_takes", area])).sees_costs)

    def test_where_each_login_starts(self):
        cases = [
            (Access.of(Membership(role=Membership.Role.OWNER)), "/"),
            (Access.of(member(["returnables", "products"])), "/"),
            (Access.of(member(["invoices_add"])), reverse("invoices:invoice_add")),
            (Access.of(member(["stock_takes"])), reverse("inventory:stock_take_list")),
            # The first area in the order the owner reads them.
            (Access.of(member(["bank", "returnables", "stock_takes"])), reverse("inventory:stock_take_list")),
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
