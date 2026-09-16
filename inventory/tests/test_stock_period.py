"""The stock page, scoped to the window between two inventories.

All time, "how much could this have been?" is answered by the ledger: nothing
deducts sales from it, so everything ever bought is the ceiling. Between two
counts there is a far better answer, because both ends were physically
measured:

    left the shelf = opening count + purchases - closing count

Stock still standing there at the closing count obviously wasn't poured, and a
bottle already recorded as broken can't have been either - so what sales are
measured against is `left the shelf - known losses`, and the part of it no
sale explains is the shrinkage figure.

The one rule that matters more than the arithmetic: an item that wasn't in
BOTH counts gets no verdict at all. Its opening and closing read as zero, so
everything it bought looks evaporated - on the real database that once put
360 litres of beer at the top of the report, worth more than the genuine
finding.
"""

from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import MovementKind, UnitChoices
from inventory.variance import quantities_sold, stock_between
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_movement,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
)


def at(day: int) -> datetime:
    return timezone.make_aware(datetime(2026, 3, day, 12, 0))


class StockBetweenTests(TestCase):
    """What moved, per item, between two counts."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.opening = make_stock_take(taken_at=at(1))
        self.closing = make_stock_take(taken_at=at(31))

    def count(self, take, stock_type, quantity):
        make_stock_take_line(
            stock_take=take, product=None, stock_type=stock_type,
            counted_quantity=quantity, unit=stock_type.unit,
        )

    def test_what_left_the_shelf_is_opening_plus_purchases_minus_closing(self):
        self.count(self.opening, self.vodka, "12")
        self.count(self.closing, self.vodka, "9")
        make_movement(stock_type=self.vodka, quantity="24", occurred_on=date(2026, 3, 10))

        item = stock_between(self.closing).items[self.vodka.pk]
        self.assertEqual(item.opening, Decimal("12"))
        self.assertEqual(item.purchases, Decimal("24"))
        self.assertEqual(item.closing, Decimal("9"))
        self.assertEqual(item.left_the_shelf, Decimal("27"))
        self.assertEqual(item.sellable, Decimal("27"))

    def test_a_known_loss_could_not_have_been_sold(self):
        """It left the shelf, so it's in `left_the_shelf` - but it left in a
        way that is already accounted for, so no sale may claim it."""
        self.count(self.opening, self.vodka, "12")
        self.count(self.closing, self.vodka, "9")
        make_movement(
            stock_type=self.vodka, quantity="-2", kind=MovementKind.LOSS,
            occurred_on=date(2026, 3, 10),
        )

        item = stock_between(self.closing).items[self.vodka.pk]
        self.assertEqual(item.known_losses, Decimal("2"))
        self.assertEqual(item.left_the_shelf, Decimal("3"))
        self.assertEqual(item.sellable, Decimal("1"))

    def test_an_impossible_count_cannot_produce_a_negative_ceiling(self):
        """Counting more at the end than ever existed is a miscount. A
        negative ceiling would let the item soak up sales it never covered,
        and read as a surplus rather than the data error it is."""
        self.count(self.opening, self.vodka, "1")
        self.count(self.closing, self.vodka, "40")

        item = stock_between(self.closing).items[self.vodka.pk]
        self.assertLess(item.left_the_shelf, 0)
        self.assertEqual(item.sellable, Decimal("0"))

    def test_an_item_in_both_counts_is_reliable(self):
        self.count(self.opening, self.vodka, "12")
        self.count(self.closing, self.vodka, "9")
        self.assertTrue(stock_between(self.closing).items[self.vodka.pk].counted)

    def test_an_item_missing_from_one_count_is_not(self):
        self.count(self.closing, self.vodka, "9")
        self.assertFalse(stock_between(self.closing).items[self.vodka.pk].counted)

    def test_the_previous_count_is_picked_automatically(self):
        period = stock_between(self.closing)
        self.assertEqual(period.opening_take, self.opening)
        self.assertEqual(period.start, date(2026, 3, 1))
        self.assertEqual(period.end, date(2026, 3, 31))
        self.assertFalse(period.since_beginning)

    def test_the_first_count_measures_since_the_beginning(self):
        period = stock_between(self.opening)
        self.assertIsNone(period.opening_take)
        self.assertIsNone(period.start)
        self.assertTrue(period.since_beginning)

    def test_since_the_beginning_only_the_one_count_has_to_exist(self):
        """There is no opening count to be missing from - opening stock is
        zero by definition."""
        self.count(self.opening, self.vodka, "12")
        self.assertTrue(stock_between(self.opening).items[self.vodka.pk].counted)

    def test_a_delivery_outside_the_window_is_not_counted(self):
        self.count(self.opening, self.vodka, "0")
        self.count(self.closing, self.vodka, "0")
        make_movement(stock_type=self.vodka, quantity="5", occurred_on=date(2026, 2, 20))
        make_movement(stock_type=self.vodka, quantity="7", occurred_on=date(2026, 4, 2))
        make_movement(stock_type=self.vodka, quantity="3", occurred_on=date(2026, 3, 10))

        self.assertEqual(stock_between(self.closing).items[self.vodka.pk].purchases, Decimal("3"))


class SalesAreCappedByWhatLeftTheShelfTests(TestCase):
    """The clamped allocation, with the period's ceiling instead of the
    ledger's. See test_sold_allocation.py for the allocator itself."""

    def setUp(self):
        self.opening = make_stock_take(taken_at=at(1))
        self.closing = make_stock_take(taken_at=at(31))

    def counted(self, name, opening, purchases, closing, unit_cost_ht="10"):
        stock_type = make_stock_type(name=name, unit=UnitChoices.LITRE)
        for take, quantity in ((self.opening, opening), (self.closing, closing)):
            make_stock_take_line(
                stock_take=take, product=None, stock_type=stock_type,
                counted_quantity=quantity, unit=UnitChoices.LITRE,
            )
        make_movement(
            stock_type=stock_type, quantity=purchases, unit_cost_ht=unit_cost_ht,
            occurred_on=date(2026, 3, 10),
        )
        return stock_type

    def sold(self, period):
        return quantities_sold(period.start, period.end, available=period.ceilings())

    def test_stock_still_on_the_shelf_does_not_absorb_sales(self):
        """20 L bought of each, but only 2 L of the vodka actually went while
        the whole 20 L of gin did. All time both look equally able to have
        covered the sales; between two counts, only the gin can."""
        vodka = self.counted("Vodka", opening="0", purchases="20", closing="18", unit_cost_ht="30")
        gin = self.counted("Gin", opening="0", purchases="20", closing="0", unit_cost_ht="10")
        recipe = make_recipe(name="Mule")
        make_ingredient(recipe, stock_type=vodka, quantity="0.05", group=0)
        make_ingredient(recipe, stock_type=gin, quantity="0.05", group=0)
        record_sales([("Mule", date(2026, 3, 5), 200)])  # 10 L of something

        period = stock_between(self.closing)
        sold = self.sold(period)
        # The vodka is the pricier bottle, so it goes first - but only 2 L
        # left the shelf, less its 10% allowance: 1.80 L. The gin covers the
        # other 8.20 L.
        self.assertEqual(sold[vodka.pk].headline, Decimal("1.80"))
        self.assertEqual(sold[gin.pk].headline, Decimal("8.20"))

    def test_what_left_and_no_sale_explains_is_the_missing_figure(self):
        vodka = self.counted("Vodka", opening="12", purchases="0", closing="2")
        recipe = make_recipe(name="Mule")
        make_ingredient(recipe, stock_type=vodka, quantity="0.05", group=0)
        record_sales([("Mule", date(2026, 3, 5), 100)])  # 5 L

        sold = self.sold(stock_between(self.closing))[vodka.pk]
        self.assertEqual(sold.available, Decimal("10"))  # 12 - 2 left the shelf
        self.assertEqual(sold.headline, Decimal("5.00"))
        self.assertEqual(sold.unexplained, Decimal("5.00"))
        self.assertFalse(sold.is_over)

    def test_an_item_that_left_and_was_never_sold_is_entirely_unexplained(self):
        vodka = self.counted("Vodka", opening="12", purchases="0", closing="2")
        sold = self.sold(stock_between(self.closing))
        self.assertNotIn(vodka.pk, sold)  # nothing sold it, so no entry at all

    def test_selling_more_than_left_the_shelf_is_flagged(self):
        vodka = self.counted("Vodka", opening="12", purchases="0", closing="10")
        recipe = make_recipe(name="Mule")
        make_ingredient(recipe, stock_type=vodka, quantity="0.05", group=0)
        record_sales([("Mule", date(2026, 3, 5), 100)])  # 5 L out of 2 L

        sold = self.sold(stock_between(self.closing))[vodka.pk]
        self.assertTrue(sold.is_over)
        self.assertEqual(sold.missing, Decimal("3.00"))
        self.assertEqual(sold.unexplained, Decimal("0"))

    def test_sales_outside_the_window_do_not_count(self):
        vodka = self.counted("Vodka", opening="12", purchases="0", closing="2")
        recipe = make_recipe(name="Mule")
        make_ingredient(recipe, stock_type=vodka, quantity="0.05", group=0)
        record_sales([("Mule", date(2026, 2, 20), 100)])
        record_sales([("Mule", date(2026, 4, 2), 100)])

        self.assertNotIn(vodka.pk, self.sold(stock_between(self.closing)))


class StockPagePeriodTests(TestCase):
    """The page itself: the picker, the columns, and the totals."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.opening = make_stock_take(taken_at=at(1))
        self.closing = make_stock_take(taken_at=at(31))
        for take, quantity in ((self.opening, "12"), (self.closing, "2")):
            make_stock_take_line(
                stock_take=take, product=None, stock_type=self.vodka,
                counted_quantity=quantity, unit=UnitChoices.LITRE,
            )
        make_movement(stock_type=self.vodka, quantity="12", unit_cost_ht="20", occurred_on=date(2026, 2, 1))
        self.recipe = make_recipe(name="Mule")
        make_ingredient(self.recipe, stock_type=self.vodka, quantity="0.05", group=0)
        record_sales([("Mule", date(2026, 3, 5), 100)])  # 5 L of the 10 that went

    def page(self, **params):
        return self.client.get(reverse("inventory:stock_list"), params)

    def row(self, response):
        rows = [row for category in response.context["categories"] for row in category["rows"]]
        return next(row for row in rows if row["stock_type"].pk == self.vodka.pk)

    def test_the_picker_offers_every_inventory(self):
        html = self.page().content.decode()
        self.assertIn("Depuis le début", html)
        self.assertIn(f'value="{self.closing.pk}"', html)
        self.assertIn(f'value="{self.opening.pk}"', html)

    def test_without_a_period_the_page_is_unchanged(self):
        response = self.page()
        self.assertIsNone(response.context["period"])
        self.assertContains(response, "Valeur du stock (HT)")
        # All time the ceiling is the ledger: 12 L bought, 5 L sold.
        self.assertEqual(self.row(response)["sold"].available, Decimal("12"))

    def test_selecting_an_inventory_scopes_everything_to_it(self):
        response = self.page(inventaire=self.closing.pk)
        self.assertEqual(response.context["period"].closing_take, self.closing)
        row = self.row(response)
        self.assertEqual(row["period"].opening, Decimal("12"))
        self.assertEqual(row["period"].purchases, Decimal("0"))  # the delivery was in February
        self.assertEqual(row["period"].closing, Decimal("2"))
        self.assertEqual(row["sold"].headline, Decimal("5.00"))
        self.assertEqual(row["sold"].unexplained, Decimal("5.00"))
        self.assertTrue(row["reliable"])
        self.assertTrue(row["in_period"])

    def test_the_period_columns_replace_the_stock_value_ones(self):
        response = self.page(inventaire=self.closing.pk)
        self.assertContains(response, "Ouverture")
        self.assertContains(response, "Sorti")
        self.assertContains(response, "Manquant")
        self.assertNotContains(response, "Valeur du stock (HT)")

    def test_the_missing_value_is_totalled(self):
        response = self.page(inventaire=self.closing.pk)
        # 5 L unexplained at the 20 €/L it was bought for.
        self.assertEqual(response.context["total_missing_value"], Decimal("100.00"))
        self.assertEqual(self.row(response)["missing_value_ht"], Decimal("100.00"))

    def test_an_uncounted_item_gets_no_verdict(self):
        """The rule that cost the most to learn: without both counts, an
        item's whole purchase history reads as evaporated."""
        rum = make_stock_type(name="Rhum", unit=UnitChoices.LITRE)
        make_movement(stock_type=rum, quantity="360", unit_cost_ht="15", occurred_on=date(2026, 3, 10))

        response = self.page(inventaire=self.closing.pk)
        rows = [row for category in response.context["categories"] for row in category["rows"]]
        row = next(row for row in rows if row["stock_type"].pk == rum.pk)
        self.assertTrue(row["in_period"])
        self.assertFalse(row["reliable"])
        self.assertNotIn("missing_value_ht", row)
        self.assertEqual(response.context["uncounted_count"], 1)
        # ...and its 360 L is nowhere near the missing total.
        self.assertEqual(response.context["total_missing_value"], Decimal("100.00"))
        self.assertContains(response, "non compté")

    def test_an_item_with_nothing_happening_is_not_in_the_period(self):
        make_stock_type(name="Sirop", unit=UnitChoices.LITRE)
        response = self.page(inventaire=self.closing.pk)
        rows = [row for category in response.context["categories"] for row in category["rows"]]
        row = next(row for row in rows if row["stock_type"].name == "Sirop")
        self.assertFalse(row["in_period"])
        self.assertEqual(response.context["uncounted_count"], 0)

    def test_only_the_wide_table_gets_a_scroll_box(self):
        """`overflow-x` makes an element a scroll container on both axes, and
        a sticky thead inside one sticks to THAT box rather than the viewport.
        Wrapping the 7-column table would quietly stop its header following
        you down a long category, for a scrollbar it never needs."""
        self.assertNotContains(self.page(), "stock-table-wrap")
        self.assertContains(self.page(inventaire=self.closing.pk), "stock-table-wrap")

    def test_an_unknown_inventory_falls_back_to_all_time(self):
        """A stale bookmark, or an inventory deleted since - the page still
        renders rather than 404ing on a query parameter."""
        response = self.page(inventaire=999999)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["period"])

    def test_a_nonsense_inventory_id_does_not_blow_up(self):
        response = self.page(inventaire="tomorrow")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["period"])

    def test_the_ecarts_page_links_to_the_per_item_view(self):
        html = self.client.get(
            reverse("inventory:stock_take_variance", kwargs={"pk": self.closing.pk})
        ).content.decode()
        self.assertIn(f"?inventaire={self.closing.pk}", html)
