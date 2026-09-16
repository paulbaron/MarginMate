"""Behavioural tests for inventory/views.py beyond the plain 200-OK smoke
coverage in tests/test_views_smoke.py.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from inventory.models import UnitChoices
from inventory.views import _aggregate_price_points
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_type,
    make_supplier,
)


class SearchStockTypesTests(TestCase):
    """The Stock page's own search box - server-backed because the products
    filed under a stock type aren't in the DOM until its row is expanded."""

    def setUp(self):
        self.supplier = make_supplier()

    def search(self, query):
        response = self.client.get(reverse("inventory:search_stock_types"), {"q": query})
        self.assertEqual(response.status_code, 200)
        return set(response.json()["ids"])

    def test_matches_are_accent_insensitive(self):
        """"biere" has to find "Bière" - nobody reaches for the compose key
        while typing fast at a bar. SQLite's own icontains folds case but
        not accents, which is what made this fail before."""
        beer = make_stock_type(name="Bière triple", unit=UnitChoices.LITRE)
        self.assertEqual(self.search("bieres"), set())  # sanity: no such name exists
        self.assertEqual(self.search("biere triple"), {beer.id})
        self.assertEqual(self.search("bière triple"), {beer.id})
        self.assertEqual(self.search("BIÈRE"), {beer.id})

    def test_matches_by_category(self):
        beer = make_stock_type(name="Triple", category="Bières", unit=UnitChoices.LITRE)
        self.assertEqual(self.search("bieres"), {beer.id})

    def test_matches_by_a_products_raw_invoice_name(self):
        """Typing what's actually printed on an invoice finds the row even
        when the stock type itself was named something more generic."""
        stock_type = make_stock_type(name="Rhum", unit=UnitChoices.LITRE)
        make_product(supplier=self.supplier, raw_name="RHUM NÉGRITA 70CL", stock_type=stock_type)
        self.assertEqual(self.search("negrita"), {stock_type.id})

    def test_an_empty_query_matches_nothing(self):
        make_stock_type(name="Rhum", unit=UnitChoices.LITRE)
        self.assertEqual(self.search(""), set())

    def test_no_match_is_an_empty_list_not_an_error(self):
        make_stock_type(name="Rhum", unit=UnitChoices.LITRE)
        self.assertEqual(self.search("zzz introuvable"), set())


class PriceHistoryAggregationTests(TestCase):
    """Two movements on the SAME date must collapse to one point - the chart
    plots price against date, so its x-axis needs distinct dates, not just a
    row count. Missing this let "Bière triple" (bought twice in one delivery)
    slip past the "enough history" gate and collapse onto the left edge."""

    def test_two_movements_one_date_collapse_to_one_point(self):
        points = _aggregate_price_points(
            [(date(2026, 1, 1), Decimal("6"), Decimal("2.00")), (date(2026, 1, 1), Decimal("6"), Decimal("2.00"))]
        )
        self.assertEqual(points, [(date(2026, 1, 1), Decimal("2.00"))])

    def test_same_date_different_prices_are_quantity_weighted(self):
        points = _aggregate_price_points(
            [(date(2026, 1, 1), Decimal("10"), Decimal("2.00")), (date(2026, 1, 1), Decimal("2"), Decimal("2.60"))]
        )
        # (10*2.00 + 2*2.60) / 12 = 2.10
        self.assertEqual(points, [(date(2026, 1, 1), Decimal("2.10"))])

    def test_distinct_dates_stay_distinct(self):
        points = _aggregate_price_points(
            [(date(2026, 1, 1), Decimal("6"), Decimal("2.00")), (date(2026, 2, 1), Decimal("6"), Decimal("2.50"))]
        )
        self.assertEqual(points, [(date(2026, 1, 1), Decimal("2.00")), (date(2026, 2, 1), Decimal("2.50"))])

    def test_input_order_does_not_matter(self):
        """Movements arrive ordered by invoice date from the query, but
        nothing should depend on that - this sorts its own output."""
        points = _aggregate_price_points(
            [(date(2026, 3, 1), Decimal("1"), Decimal("3")), (date(2026, 1, 1), Decimal("1"), Decimal("1"))]
        )
        self.assertEqual([d for d, _ in points], [date(2026, 1, 1), date(2026, 3, 1)])

    def test_a_same_day_full_reversal_falls_back_to_a_plain_average(self):
        """A purchase and its same-day return net out to zero weight - there
        is nothing to weight by, so this must not divide by zero."""
        points = _aggregate_price_points(
            [(date(2026, 1, 1), Decimal("6"), Decimal("2.00")), (date(2026, 1, 1), Decimal("-6"), Decimal("2.40"))]
        )
        self.assertEqual(points, [(date(2026, 1, 1), Decimal("2.20"))])

    def test_no_movements_is_no_points(self):
        self.assertEqual(_aggregate_price_points([]), [])


class PriceHistoryViewTests(TestCase):
    """The view that feeds _aggregate_price_points from the real ledger."""

    def setUp(self):
        self.supplier = make_supplier()
        self.stock_type = make_stock_type(name="Bière triple", unit=UnitChoices.LITRE)
        self.product = make_product(supplier=self.supplier, stock_type=self.stock_type)

    def buy(self, invoice_date, quantity, total_ht):
        invoice = make_invoice(supplier=self.supplier, invoice_date=invoice_date)
        line = make_invoice_line(invoice=invoice, product=self.product, quantity=quantity, total_ht=total_ht)
        make_movement(stock_type=self.stock_type, quantity=quantity, unit_cost_ht=line.unit_cost_ht, invoice_line=line)

    def url(self):
        return reverse("inventory:stock_type_price_history", kwargs={"pk": self.stock_type.pk})

    def test_two_purchases_the_same_day_say_not_enough_history(self):
        """Reproduces the "Bière triple" report: bought twice in one
        delivery, and nowhere else. Two movements, one date - not a trend."""
        self.buy(date(2026, 1, 1), 6, "12.00")
        self.buy(date(2026, 1, 1), 6, "12.00")

        response = self.client.get(self.url())
        self.assertFalse(response.context["has_enough_data"])
        self.assertContains(response, "Pas assez d'historique")
        self.assertNotIn("chart-point", response.content.decode())

    def test_purchases_on_two_dates_render_a_two_point_chart(self):
        self.buy(date(2026, 1, 1), 6, "12.00")
        self.buy(date(2026, 2, 1), 6, "15.00")

        response = self.client.get(self.url())
        self.assertTrue(response.context["has_enough_data"])
        self.assertEqual(response.content.decode().count("chart-point"), 2)
