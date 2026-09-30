"""A request runs bound to the logged-in user's espace, rendering included
(accounts/middleware.py), and the navigation's badges count nothing when
no espace is bound."""

import tempfile
from pathlib import Path

from django.db import connections
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.template import engines
from django.template.response import TemplateResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.middleware import TenantMiddleware
from accounts.tenancy import bound_tenant, current_tenant
from accounts.tests.support import TwoTenantsTestCase
from config.navigation import navigation
from inventory.context_processors import review_count
from invoices.context_processors import receipt_review_count
from recipes.models import PosProduct
from tests.factories import make_product, make_supplier


#: A made-up value per path converter (« <int:pk> », « <staff_month:month> »).
SAMPLES = {
    "int": "1",
    "str": "essai",
    "path": "essai/fichier.pdf",
    "slug": "essai",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "staff_month": "2026-01",
}


def routes():
    """(route, view) for every route of the project's apps (not the admin),
    as the URLconf writes it: « /invoices/<int:pk>/ »."""
    from django.urls import URLPattern, URLResolver, get_resolver
    from django.urls.resolvers import RoutePattern

    found = []

    def walk(patterns, prefix):
        for entry in patterns:
            if isinstance(entry, URLResolver):
                if entry.app_name == "admin":
                    continue
                walk(entry.url_patterns, prefix + str(entry.pattern))
            elif isinstance(entry, URLPattern) and isinstance(entry.pattern, RoutePattern):
                found.append(("/" + prefix + str(entry.pattern), entry.callback))

    walk(get_resolver().url_patterns, "")
    return found


def sample_urls():
    """(url, view) for every route of `routes()`, its parameters filled with
    made-up values: the login is asked for before any view runs, so they
    need not exist."""
    import re

    return [
        (re.sub(r"<(?:(\w+):)?\w+>", lambda m: SAMPLES[m.group(1) or "str"], route), view) for route, view in routes()
    ]


class RealPagesTests(TwoTenantsTestCase):
    def setUp(self):
        super().setUp()
        with bound_tenant(self.bar_a):
            alpha = make_supplier(code="T-ALPHA", name="Grossiste Alpha")
            for n in range(2):
                make_product(supplier=alpha, raw_name=f"Sirop Alpha {n}")
        with bound_tenant(self.bar_b):
            beta = make_supplier(code="T-BETA", name="Grossiste Beta")
            for n in range(3):
                make_product(supplier=beta, raw_name=f"Sirop Beta {n}")
            PosProduct.objects.create(name="Pinte Beta")

    def test_anonymous_is_sent_to_the_login_page(self):
        for url in (reverse("inventory:stock_list"), reverse("invoices:supplier_list")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith("/connexion/?next="), response["Location"])

    def test_an_htmx_request_sends_the_whole_page_to_the_login_page(self):
        """Not a 302 the XHR would follow, swapping the login page into a
        job's card: a 401 whose HX-Redirect htmx obeys."""
        response = self.client.get(
            reverse("invoices:supplier_list"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_CURRENT_URL="http://testserver/invoices/?onglet=achats",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["HX-Redirect"], "/connexion/?next=/invoices/%3Fonglet%3Dachats")
        # A page on another site is never where the login sends back to.
        response = self.client.get(
            reverse("invoices:supplier_list"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_CURRENT_URL="https://ailleurs.example/piege/",
        )
        self.assertEqual(response["HX-Redirect"], "/connexion/?next=/")

    def test_every_route_wants_a_login_unless_it_says_it_is_public(self):
        """Deny by default: every route of the app, walked from the URLconf,
        answers an anonymous visitor with the login page - but for the views
        marked @login_not_required."""
        checked = 0
        for url, view in sample_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                if getattr(view, "login_required", True):
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith("/connexion/?next="), response["Location"])
                else:
                    self.assertNotEqual(response.status_code, 302)
                checked += 1
        self.assertGreater(checked, 100)

    def test_each_user_sees_his_espace_only(self):
        for user, mine, other in (
            (self.user_a, "Grossiste Alpha", "Grossiste Beta"),
            (self.user_b, "Grossiste Beta", "Grossiste Alpha"),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("invoices:supplier_list"))
                self.assertContains(response, mine)
                self.assertNotContains(response, other)

    def test_the_badges_count_the_user_s_espace(self):
        """StockListView answers a TemplateResponse: rendered after the view
        returned - inside the binding, or the badges would count nothing
        (or somebody else's queue)."""
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(response.context["review_count_nav"], 2)
        self.assertEqual(response.context["pos_pending_count_nav"], 0)
        self.assertEqual(response.context["nav_section"], "produits")
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(response.context["review_count_nav"], 3)
        self.assertEqual(response.context["pos_pending_count_nav"], 1)
        self.assertContains(response, '<span id="nav-count-produits"> <span class="badge">3</span></span>', html=False)

    def test_the_binding_ends_with_the_request(self):
        before = connections["default"]
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.get(reverse("invoices:supplier_list")).status_code, 200)
        self.assertIsNone(current_tenant())
        self.assertIs(connections["default"], before)

    def test_a_user_with_no_espace_gets_a_plain_page(self):
        closed = self.make_tenant("Bar Fermé")
        lost = self.make_member(closed, "ferme@example.invalid")
        closed.is_active = False
        closed.save(update_fields=["is_active"])
        nobody = self.make_member(self.bar_b, "sans-espace@example.invalid")
        nobody.memberships.all().delete()
        for user in (lost, nobody):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("invoices:supplier_list"))
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "Aucun espace", status_code=403)
                self.assertNotContains(response, "Grossiste", status_code=403)

    def test_a_public_page_still_answers_a_user_with_no_espace(self):
        nobody = self.make_member(self.bar_b, "sans-espace@example.invalid")
        nobody.memberships.all().delete()
        self.client.force_login(nobody)
        response = self.client.get(reverse("admin:login"))
        self.assertNotEqual(response.status_code, 403)
        self.assertNotContains(response, "Aucun espace", status_code=response.status_code)

    def test_an_unknown_address_is_a_plain_404(self):
        self.assertEqual(self.client.get("/nulle-part-ici/").status_code, 404)
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.get("/nulle-part-ici/").status_code, 404)

    def test_a_logged_in_request_costs_three_queries_on_the_accounts_database(self):
        """Every request, whatever the page: the session, the user, and the
        membership WITH its espace in one query (tenant_of's
        select_related). A binding replaces `default`, never `accounts`, so
        the count holds around a client request. Twice: nothing is cached
        across requests, and nothing more is asked the second time."""
        self.client.force_login(self.user_a)
        for visit in (1, 2):
            with self.subTest(visit=visit):
                with self.assertNumQueries(3, using="accounts"), CaptureQueriesContext(connections["accounts"]) as seen:
                    self.assertEqual(self.client.get(reverse("inventory:stock_list")).status_code, 200)
                espace = [
                    query["sql"]
                    for query in seen.captured_queries
                    if "accounts_membership" in query["sql"] or "accounts_tenant" in query["sql"]
                ]
                self.assertEqual(len(espace), 1, espace)


class NeverKeptByTheBrowserTests(TwoTenantsTestCase):
    """A bar's page is never kept by the browser: on a shared device, Back
    after a logout - or after another bar's login - showed it from the
    back/forward cache, the bar's rows and its name in the topbar
    (test_back_button_browser.py). Unbound answers are left as they are."""

    def test_a_bar_s_page_is_marked_no_store(self):
        with bound_tenant(self.bar_a):
            make_supplier(code="T-ALPHA", name="Grossiste Alpha")
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertContains(response, "Grossiste Alpha")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_the_file_view_s_own_header_is_kept(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        with bound_tenant(self.bar_a):
            name = default_storage.save("invoices/essai.pdf", ContentFile(b"%PDF-1.4 essai"))
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("accounts:media", args=[name]))
        self.assertEqual(response.status_code, 200)
        for word in ("private", "no-store"):
            self.assertIn(word, response["Cache-Control"])

    def test_an_anonymous_answer_is_left_as_it_is(self):
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Cache-Control", response)


class MiddlewareDirectTests(TwoTenantsTestCase):
    """The middleware alone, around views written for the occasion."""

    def request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_an_unrendered_template_response_is_rendered_inside_the_binding(self):
        with bound_tenant(self.bar_a):
            make_product(raw_name="Sirop Alpha")
        template = engines["django"].from_string("{{ review_count_nav }}|{{ request.tenant.name }}")

        def view(request):
            return TemplateResponse(request, template)

        response = TenantMiddleware(view)(self.request(self.user_a))
        self.assertTrue(response.is_rendered)
        self.assertEqual(response.content.decode(), "1|Bar Alpha")
        self.assertIsNone(current_tenant())

    def test_a_streamed_body_is_read_inside_the_binding(self):
        with bound_tenant(self.bar_b):
            make_supplier(code="T-BETA", name="Grossiste Beta")

        def chunks():
            from invoices.models import Supplier

            yield current_tenant().name.encode()
            yield b"|" + ",".join(Supplier.objects.filter(code__startswith="T-").values_list("name", flat=True)).encode()

        response = TenantMiddleware(lambda request: StreamingHttpResponse(chunks()))(self.request(self.user_b))
        self.assertIsNone(current_tenant())
        body = b"".join(response.streaming_content)
        self.assertEqual(body, b"Bar Beta|Grossiste Beta")
        self.assertIsNone(current_tenant())

    def test_an_open_file_is_streamed_as_it_is(self):
        folder = Path(tempfile.mkdtemp(dir=self._espaces_tmp))
        (folder / "export.zip").write_bytes(b"PK-essai")
        handle = open(folder / "export.zip", "rb")
        self.addCleanup(handle.close)
        response = TenantMiddleware(lambda request: FileResponse(handle))(self.request(self.user_a))
        self.assertIsNotNone(response.file_to_stream)
        self.assertEqual(b"".join(response.streaming_content), b"PK-essai")

    def test_a_public_view_runs_unbound_for_a_logged_in_user_too(self):
        """@login_not_required means « not this login's business »: the
        employee's signing pages bind the LINK's espace themselves - whoever
        is logged in on that browser - and the login, the logout and the
        signup need none. Bound to the visitor's own, the link of another
        espace could not be opened in the same request."""
        for url in (
            reverse("accounts:login"),
            reverse("accounts:signup"),
            reverse("staff:sign", args=["jeton-d-essai"]),
            reverse("staff:sign_unknown", kwargs={"rest": "ailleurs/"}),
        ):
            with self.subTest(url=url):
                seen = []
                request = RequestFactory().get(url)
                request.user = self.user_a
                TenantMiddleware(lambda r: seen.append((current_tenant(), r.tenant)) or HttpResponse())(request)
                self.assertEqual(seen, [(None, None)])
        # A page that is not public is still the login's espace.
        seen = []
        request = RequestFactory().get(reverse("invoices:supplier_list"))
        request.user = self.user_a
        TenantMiddleware(lambda r: seen.append(current_tenant()) or HttpResponse())(request)
        self.assertEqual(seen, [self.bar_a])

    def test_anonymous_runs_unbound(self):
        from django.contrib.auth.models import AnonymousUser

        seen = []
        request = self.request(AnonymousUser())
        TenantMiddleware(lambda r: seen.append(current_tenant()) or HttpResponse())(request)
        self.assertEqual(seen, [None])
        self.assertIsNone(request.tenant)


class UnboundContextProcessorsTests(TwoTenantsTestCase):
    def test_nothing_is_counted_when_no_espace_is_bound(self):
        request = RequestFactory().get("/")
        for processor in (review_count, receipt_review_count, navigation):
            with self.subTest(processor=processor.__name__):
                with self.assertNumQueries(0):
                    self.assertEqual(processor(request), {})

    def test_bound_they_count(self):
        request = RequestFactory().get(reverse("inventory:stock_list"))
        with bound_tenant(self.bar_a):
            make_product(raw_name="Sirop Alpha")
            self.assertEqual(review_count(request), {"review_count_nav": 1})
            self.assertIn("receipt_review_count_nav", receipt_review_count(request))
            self.assertIn("pos_pending_count_nav", navigation(request))
