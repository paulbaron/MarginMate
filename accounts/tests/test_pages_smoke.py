"""Smoke GETs in multi mode, logged in to each of two bars: the accounts'
pages, the pages the navigation links to (the bar's own name in the topbar,
never the other's), then EVERY plain route of the URLconf and one page per
app on the bar's own rows - each answers without an error, renders its
template completely and holds nothing of the other bar. Both databases
number their rows from 1, so every pk names a row in BOTH espaces: a page
reading the wrong database would show the other bar's row, not a 404."""

from datetime import date
from decimal import Decimal

from django.core.cache import cache
from django.urls import reverse

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from accounts.tests.test_middleware import routes
from bank.models import BankTransaction
from returnables.tests.support import make_pickup
from staff.tests.support import employee
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

NAVIGATION = (
    "inventory:stock_list",
    "invoices:invoice_list",
    "bank:bank_home",
    "recipes:recipe_list",
    "margins:margins_home",
    "staff:home",
    "inventory:stock_take_list",
    "returnables:home",
    "transfer:data_home",
)


def plain_routes() -> list[str]:
    """Every route of the project's apps (not the admin) that takes no
    parameter."""
    return sorted({route for route, _view in routes() if "<" not in route})


class AccountsPagesSmokeTests(TwoTenantsTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_anonymous(self):
        for name in ("accounts:login", "accounts:signup"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, name)

    def test_logged_in_the_accounts_pages_send_home(self):
        self.client.force_login(self.user_a)
        for name in ("accounts:login", "accounts:signup"):
            with self.subTest(page=name):
                self.assertRedirects(self.client.get(reverse(name)), "/", fetch_redirect_response=False)

    def test_every_page_of_the_navigation_is_its_own_bar_s(self):
        with bound_tenant(self.bar_a):
            make_product(supplier=make_supplier(code="T-ALPHA", name="Grossiste Alpha"), raw_name="SIROP ALPHA")
        with bound_tenant(self.bar_b):
            make_product(supplier=make_supplier(code="T-BETA", name="Grossiste Beta"), raw_name="SIROP BETA")
        for user, mine, other in ((self.user_a, "Alpha", "Beta"), (self.user_b, "Beta", "Alpha")):
            self.client.force_login(user)
            for name in NAVIGATION:
                with self.subTest(bar=mine, page=name):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
                    assertNoUnrenderedTemplateSyntax(self, response, name)
                    self.assertContains(response, f'<span class="topbar-espace" title="Bar {mine}">Bar {mine}</span>')
                    self.assertNotContains(response, other)


class EveryPageSmokeTests(TwoTenantsTestCase):
    """The spec's « every page answers », as it words it."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def fill(self, tenant, word) -> list[str]:
        """A few rows named after `word`, and one page per app showing them."""
        with bound_tenant(tenant):
            supplier = make_supplier(name=f"Grossiste {word}")
            article = make_stock_type(name=f"Sirop {word}")
            product = make_product(supplier=supplier, raw_name=f"SIROP {word.upper()}", stock_type=article)
            invoice = make_invoice(supplier=supplier, invoice_number=f"F-{word}")
            make_invoice_line(invoice=invoice, product=product)
            recipe = make_recipe(name=f"Cocktail {word}")
            make_ingredient(recipe, stock_type=article)
            take = make_stock_take()
            make_stock_take_line(stock_take=take, product=product)
            line = BankTransaction.objects.create(
                operation_date=date(2026, 6, 3),
                label=f"PRLV {word.upper()}",
                amount=Decimal("-12.00"),
                fingerprint=f"empreinte-{word}",
            )
            person = employee(last_name=word, first_name="Essai")
            # A reprise the bar's supplier took back, with a note of its own.
            pickup = make_pickup(supplier=supplier, counts={"Fûts": 4}, note=f"Vides {word}")
        return [
            reverse("invoices:invoice_detail", args=[invoice.pk]),
            reverse("recipes:recipe_detail", args=[recipe.pk]),
            reverse("inventory:stock_take_detail", args=[take.pk]),
            reverse("inventory:stock_type_update", args=[article.pk]),
            # The documents a search finds for the bank line: the bar's invoice.
            reverse("bank:invoice_search", args=[line.pk]) + f"?recherche={word}",
            reverse("staff:month", args=[person.pk, date(2026, 6, 1)]),
            reverse("returnables:pickup_detail", args=[pickup.pk]),
        ]

    def test_every_plain_route_and_one_page_per_app_is_its_own_bar_s(self):
        pages = {"Alphaville": self.fill(self.bar_a, "Alphaville"), "Betaville": self.fill(self.bar_b, "Betaville")}
        plain = plain_routes()
        self.assertGreater(len(plain), 40)
        for user, mine, other in ((self.user_a, "Alphaville", "Betaville"), (self.user_b, "Betaville", "Alphaville")):
            self.client.force_login(user)
            own = pages[mine]
            for url in plain + own:
                with self.subTest(bar=mine, url=url):
                    response = self.client.get(url)
                    # A GET of a POST-only route is a 405 (the logout); a
                    # page for later a 302 - never an error.
                    self.assertLess(response.status_code, 500)
                    streamed = getattr(response, "streaming", False)
                    body = (b"".join(response.streaming_content) if streamed else response.content).lower()
                    self.assertNotIn(other.lower().encode(), body)
                    if url in own:
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(mine.lower().encode(), body)
                    if (
                        response.status_code == 200
                        and not streamed
                        and response.get("Content-Type", "").startswith("text/html")
                    ):
                        assertNoUnrenderedTemplateSyntax(self, response, url)
