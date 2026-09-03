"""Tests for shrinkage detection: what left the shelf vs what sales explain.

The whole feature rests on one equation and one idea. The equation is

    unexplained = (opening + purchases - closing) - known_losses - sold

and the idea is that when a recipe says "vodka OR gin", you must not guess
which - you pool the two and account for them together, which turns an
unanswerable question into an exact one.

These tests are written against worked examples with round numbers, because
the output is a euro figure someone will confront a member of staff with.
It has to be defensible to the cent, and it has to be a FLOOR - never an
accusation bigger than the data supports.
"""

from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from inventory.models import MovementKind, StockMovement, UnitChoices
from inventory.variance import (
    build_pools,
    compute_variance,
    counted_quantity_in_stock_units,
    recipe_usage_terms,
    stock_units_per_item,
)
from recipes.models import RecipeIngredient
from recipes.sales import record_sales
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


def at(day: int) -> datetime:
    return timezone.make_aware(datetime(2026, 3, day, 12, 0))


class UsageExpansionTests(TestCase):
    """Turning a recipe into "how much stock does one sale consume"."""

    def test_a_fixed_ingredient_is_one_option(self):
        recipe = make_recipe()
        vodka = make_stock_type(name="Vodka")
        make_ingredient(recipe, stock_type=vodka, quantity="0.04", group=0)
        self.assertEqual(recipe_usage_terms(recipe), [[{vodka.id: Decimal("0.04")}]])

    def test_alternatives_are_several_options_in_one_term(self):
        recipe = make_recipe()
        vodka = make_stock_type(name="Vodka")
        gin = make_stock_type(name="Gin")
        make_ingredient(recipe, stock_type=vodka, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=gin, quantity="0.05", group=0)
        self.assertEqual(
            recipe_usage_terms(recipe),
            [[{vodka.id: Decimal("0.04")}, {gin.id: Decimal("0.05")}]],
        )

    def test_a_batch_recipe_divides_by_its_yield(self):
        """Selling one serving of a recipe that makes 10 consumes a tenth."""
        recipe = make_recipe(yield_quantity="10")
        sugar = make_stock_type(name="Sucre", unit=UnitChoices.KILOGRAM)
        make_ingredient(recipe, stock_type=sugar, quantity="2", group=0)
        self.assertEqual(recipe_usage_terms(recipe), [[{sugar.id: Decimal("0.2")}]])

    def test_a_sub_recipe_is_followed_down_to_real_stock(self):
        """Selling a cocktail that uses house syrup has to consume sugar,
        not "syrup" - there's no syrup on any invoice."""
        sugar = make_stock_type(name="Sucre", unit=UnitChoices.KILOGRAM)
        water = make_stock_type(name="Eau plate")
        syrup = make_recipe(name="Sirop", yield_quantity="2")  # 2 litres per batch
        make_ingredient(syrup, stock_type=sugar, quantity="1", group=0)
        make_ingredient(syrup, stock_type=water, quantity="1.5", group=1)

        cocktail = make_recipe(name="Cocktail")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02", group=0)

        # 0.02 L of syrup = 1% of a batch = 0.01 kg sugar + 0.015 L water.
        self.assertEqual(
            recipe_usage_terms(cocktail),
            [[{sugar.id: Decimal("0.01"), water.id: Decimal("0.015")}]],
        )

    def test_a_sub_recipe_cycle_does_not_recurse_for_ever(self):
        """The form forbids cycles; the admin doesn't."""
        a = make_recipe(name="A")
        b = make_recipe(name="B")
        RecipeIngredient.objects.create(recipe=a, sub_recipe=b, quantity=Decimal("1"), group=0)
        RecipeIngredient.objects.create(recipe=b, sub_recipe=a, quantity=Decimal("1"), group=0)
        recipe_usage_terms(a)  # must return, not blow the stack

    def test_a_recipe_with_no_ingredients_has_no_terms(self):
        self.assertEqual(recipe_usage_terms(make_recipe()), [])


class PoolingTests(TestCase):
    """Alternatives are indistinguishable once sold, so they're accounted
    for together."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka")
        self.gin = make_stock_type(name="Gin")
        self.rum = make_stock_type(name="Rhum")
        self.whisky = make_stock_type(name="Whisky")
        self.lime = make_stock_type(name="Citrons", unit=UnitChoices.KILOGRAM)

    def test_an_ingredient_with_no_alternatives_is_its_own_pool(self):
        recipe = make_recipe()
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.04", group=0)
        pools = build_pools([recipe])
        self.assertEqual(pools[self.vodka.id], frozenset({self.vodka.id}))

    def test_alternatives_share_a_pool(self):
        recipe = make_recipe()
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=self.gin, quantity="0.04", group=0)
        pools = build_pools([recipe])
        self.assertEqual(pools[self.vodka.id], frozenset({self.vodka.id, self.gin.id}))
        self.assertEqual(pools[self.gin.id], pools[self.vodka.id])

    def test_pools_merge_transitively_across_recipes(self):
        """This is the real shape of the data: "Alcool + Soda" offers
        Gin/Vodka/Whisky/Rum and "Mule" offers Vodka/Gin/Rum, so all four
        spirits become one pool even though no single recipe lists them
        together with the whisky."""
        soda = make_recipe(name="Alcool + Soda")
        for stock_type in (self.gin, self.whisky):
            make_ingredient(soda, stock_type=stock_type, quantity="0.04", group=0)
        mule = make_recipe(name="Mule")
        for stock_type in (self.vodka, self.gin, self.rum):
            make_ingredient(mule, stock_type=stock_type, quantity="0.04", group=0)

        pools = build_pools([soda, mule])
        self.assertEqual(
            pools[self.whisky.id],
            frozenset({self.gin.id, self.whisky.id, self.vodka.id, self.rum.id}),
        )

    def test_separate_choices_stay_separate(self):
        """Spirits and mixers must not end up in the same pool just because
        they appear in the same recipe."""
        recipe = make_recipe()
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=self.gin, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=self.lime, quantity="0.02", group=1)
        pools = build_pools([recipe])
        self.assertNotEqual(pools[self.vodka.id], pools[self.lime.id])
        self.assertEqual(pools[self.lime.id], frozenset({self.lime.id}))

    def test_a_stock_item_never_used_in_a_recipe_still_gets_a_pool(self):
        pools = build_pools([], [self.vodka.id])
        self.assertEqual(pools[self.vodka.id], frozenset({self.vodka.id}))


class CountConversionTests(TestCase):
    """A bottle counted on a shelf has to be worth the same as a bottle
    bought on an invoice, or every variance is wrong by the pack size."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)

    def test_a_stock_type_line_is_already_in_stock_units(self):
        line = make_stock_take_line(stock_type=self.vodka, counted_quantity="4.2", unit=UnitChoices.LITRE)
        self.assertEqual(counted_quantity_in_stock_units(line), Decimal("4.2"))

    def test_bottles_of_a_unit_product_convert_by_stock_equivalent(self):
        product = make_product(stock_type=self.vodka, unit=UnitChoices.UNIT, stock_equivalent="0.7")
        line = make_stock_take_line(product=product, counted_quantity="6", unit=UnitChoices.UNIT)
        self.assertEqual(stock_units_per_item(product), Decimal("0.7"))
        self.assertEqual(counted_quantity_in_stock_units(line), Decimal("4.2"))

    def test_bottles_of_a_volume_product_convert_through_its_invoice_history(self):
        """A product whose invoices print a measured volume gets its bottle
        size from that history, not from a guess."""
        product = make_product(stock_type=self.vodka, unit=UnitChoices.LITRE, stock_equivalent="1")
        invoice = make_invoice(supplier=product.supplier, invoice_date=date(2026, 1, 1))
        make_invoice_line(invoice=invoice, product=product, quantity=6, total_volume="4.2", total_ht="90")

        self.assertEqual(stock_units_per_item(product), Decimal("0.7"))
        line = make_stock_take_line(product=product, counted_quantity="6", unit=UnitChoices.UNIT)
        self.assertEqual(counted_quantity_in_stock_units(line), Decimal("4.2"))

    def test_a_measured_count_is_scaled_by_stock_equivalent(self):
        product = make_product(stock_type=self.vodka, unit=UnitChoices.LITRE, stock_equivalent="1")
        line = make_stock_take_line(product=product, counted_quantity="0.3", unit=UnitChoices.LITRE)
        self.assertEqual(counted_quantity_in_stock_units(line), Decimal("0.3"))


class VarianceArithmeticTests(TestCase):
    """The worked example, with round numbers throughout."""

    def setUp(self):
        self.supplier = make_supplier()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.product = make_product(
            supplier=self.supplier, raw_name="VODKA 70CL", stock_type=self.vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        self.recipe = make_recipe(name="Vodka tonic", selling_price_ttc="8.50")
        make_ingredient(self.recipe, stock_type=self.vodka, quantity="0.04", group=0)

    def buy(self, litres: str, on: date, cost="10"):
        invoice = make_invoice(supplier=self.supplier, invoice_date=on)
        line = make_invoice_line(invoice=invoice, product=self.product, quantity=1, total_ht=cost)
        return StockMovement.objects.create(
            stock_type=self.vodka, quantity=Decimal(litres), unit_cost_ht=Decimal(cost),
            invoice_line=line, kind=MovementKind.PURCHASE,
        )

    def report(self, opening_litres, closing_litres, sold=None, opening_day=1, closing_day=10):
        opening = make_stock_take(taken_at=at(opening_day))
        make_stock_take_line(
            stock_take=opening, stock_type=self.vodka,
            counted_quantity=opening_litres, unit=UnitChoices.LITRE,
        )
        closing = make_stock_take(taken_at=at(closing_day))
        make_stock_take_line(
            stock_take=closing, stock_type=self.vodka,
            counted_quantity=closing_litres, unit=UnitChoices.LITRE,
        )
        if sold:
            record_sales([(self.recipe.name, date(2026, 3, 5), sold)])
        return compute_variance(closing)

    def pool(self, report):
        return next(p for p in report.pools if self.vodka in p.stock_types)

    def test_perfectly_explained_stock_shows_nothing_missing(self):
        """10 L on hand, 100 drinks at 4cl = 4 L, 6 L left."""
        pool = self.pool(self.report("10", "6", sold=100))
        self.assertEqual(pool.actual_usage, Decimal("4"))
        self.assertEqual(pool.expected_usage_min, Decimal("4"))
        self.assertFalse(pool.is_missing)
        self.assertFalse(pool.is_impossible)

    def test_stock_that_left_without_being_sold_is_missing(self):
        """Only 5 L left instead of 6: one litre walked."""
        pool = self.pool(self.report("10", "5", sold=100))
        self.assertEqual(pool.actual_usage, Decimal("5"))
        self.assertEqual(pool.unexplained_min, Decimal("1"))
        self.assertTrue(pool.is_missing)

    def test_purchases_during_the_period_are_added(self):
        self.buy("5", on=date(2026, 3, 4))
        pool = self.pool(self.report("10", "11", sold=100))
        self.assertEqual(pool.purchases, Decimal("5"))
        self.assertEqual(pool.actual_usage, Decimal("4"))  # 10 + 5 - 11
        self.assertFalse(pool.is_missing)

    def test_a_delivery_outside_the_window_is_not_counted(self):
        """It's already in the opening count, or not yet in the closing one -
        either way, counting it here would invent stock."""
        self.buy("5", on=date(2026, 2, 20))   # before the opening count
        self.buy("5", on=date(2026, 3, 20))   # after the closing count
        pool = self.pool(self.report("10", "6", sold=100))
        self.assertEqual(pool.purchases, Decimal("0"))
        self.assertFalse(pool.is_missing)

    def test_a_delivery_on_the_opening_day_belongs_to_the_previous_period(self):
        self.buy("5", on=date(2026, 3, 1))
        self.assertEqual(self.pool(self.report("10", "6", sold=100)).purchases, Decimal("0"))

    def test_a_delivery_on_the_closing_day_is_in_this_period(self):
        self.buy("5", on=date(2026, 3, 10))
        self.assertEqual(self.pool(self.report("10", "6", sold=100)).purchases, Decimal("5"))

    def test_a_known_loss_is_not_counted_as_shrinkage(self):
        """A bottle you already know you broke shouldn't show up as theft."""
        StockMovement.objects.create(
            stock_type=self.vodka, quantity=Decimal("-1"), unit_cost_ht=Decimal("10"),
            kind=MovementKind.LOSS, note="Bouteille cassée", occurred_on=date(2026, 3, 5),
        )
        pool = self.pool(self.report("10", "5", sold=100))
        self.assertEqual(pool.known_losses, Decimal("1"))
        self.assertEqual(pool.actual_usage, Decimal("5"))
        self.assertEqual(pool.unexplained_min, Decimal("0"))
        self.assertFalse(pool.is_missing)

    def test_using_more_than_left_the_shelf_is_flagged_as_impossible(self):
        """You cannot pour stock you never had - so this is a data error, not
        shrinkage, and saying so is more useful than a negative number."""
        pool = self.pool(self.report("10", "9", sold=100))
        self.assertTrue(pool.is_impossible)
        self.assertFalse(pool.is_missing)

    def test_no_sales_at_all_makes_everything_that_moved_unexplained(self):
        pool = self.pool(self.report("10", "6"))
        self.assertEqual(pool.expected_usage_min, Decimal("0"))
        self.assertEqual(pool.unexplained_min, Decimal("4"))

    def test_the_first_ever_stock_take_measures_since_the_beginning(self):
        """It used to come back empty - "a baseline, not a result". But one
        count IS enough when the invoices go back far enough: opening stock
        is zero, and everything bought since then either sold or is on the
        shelf. See SingleInventoryTests."""
        self.buy("10", on=date(2026, 3, 4))
        closing = make_stock_take(taken_at=at(10))
        make_stock_take_line(
            stock_take=closing, stock_type=self.vodka, counted_quantity="6", unit=UnitChoices.LITRE
        )
        report = compute_variance(closing)
        self.assertIsNone(report.opening_take)
        self.assertTrue(report.since_beginning)
        pool = next(p for p in report.pools if self.vodka in p.stock_types)
        self.assertEqual(pool.opening, Decimal("0"))
        self.assertEqual(pool.actual_usage, Decimal("4"))  # 0 + 10 - 6

    def test_the_previous_take_is_picked_automatically(self):
        report = self.report("10", "5", sold=100)
        self.assertEqual(report.opening_take.taken_at, at(1))
        self.assertEqual(report.period_start, date(2026, 3, 1))
        self.assertEqual(report.period_end, date(2026, 3, 10))

    def test_an_item_missing_from_one_count_is_flagged_not_guessed(self):
        opening = make_stock_take(taken_at=at(1))
        make_stock_take_line(
            stock_take=opening, stock_type=self.vodka, counted_quantity="10", unit=UnitChoices.LITRE
        )
        closing = make_stock_take(taken_at=at(10))  # vodka simply not counted
        gin = make_stock_type(name="Gin")
        make_stock_take_line(stock_take=closing, stock_type=gin, counted_quantity="1", unit=UnitChoices.LITRE)

        report = compute_variance(closing)
        pool = next(p for p in report.pools if self.vodka in p.stock_types)
        self.assertFalse(pool.is_reliable)
        self.assertIn(self.vodka, pool.uncounted)

    def test_an_uncounted_item_is_never_reported_as_missing(self):
        """The one that nearly made the report useless. A case of beer bought
        this period but never counted has opening 0 and closing 0, so all of
        it looks like it evaporated - and on the real database that put 360
        litres, worth more than the genuine finding, straight at the top."""
        self.buy("50", on=date(2026, 3, 4))
        opening = make_stock_take(taken_at=at(1))  # vodka in neither count
        make_stock_take_line(
            stock_take=opening, stock_type=make_stock_type(name="Autre"),
            counted_quantity="1", unit=UnitChoices.LITRE,
        )
        closing = make_stock_take(taken_at=at(10))
        make_stock_take_line(
            stock_take=closing, stock_type=make_stock_type(name="Encore"),
            counted_quantity="1", unit=UnitChoices.LITRE,
        )

        report = compute_variance(closing)
        pool = next(p for p in report.pools if self.vodka in p.stock_types)
        self.assertNotIn(pool, report.missing)
        self.assertEqual(report.total_value_missing_min, Decimal("0"))
        # ...but it isn't hidden either - it's a "count this next time".
        self.assertIn(pool, report.incomplete)

    def test_an_uncounted_item_is_never_reported_as_impossible_either(self):
        """Sales of an item nobody counted would otherwise look like pouring
        stock that never existed."""
        opening = make_stock_take(taken_at=at(1))
        make_stock_take_line(
            stock_take=opening, stock_type=make_stock_type(name="Autre"),
            counted_quantity="1", unit=UnitChoices.LITRE,
        )
        closing = make_stock_take(taken_at=at(10))
        make_stock_take_line(
            stock_take=closing, stock_type=make_stock_type(name="Encore"),
            counted_quantity="1", unit=UnitChoices.LITRE,
        )
        record_sales([(self.recipe.name, date(2026, 3, 5), 100)])

        report = compute_variance(closing)
        pool = next(p for p in report.pools if self.vodka in p.stock_types)
        self.assertNotIn(pool, report.impossible)
        self.assertIn(pool, report.incomplete)

    def test_an_item_with_no_activity_at_all_is_not_reported(self):
        """There are hundreds of stock items; the ones that did nothing this
        period would bury the handful that matter."""
        make_stock_type(name="Jamais utilisé")
        report = self.report("10", "6", sold=100)
        self.assertEqual([pool.label for pool in report.incomplete], [])


class CheapestBottleTests(TestCase):
    """Stating the loss as "N bottles of the cheapest" - the floor."""

    def setUp(self):
        self.supplier = make_supplier()
        self.cheap = make_stock_type(name="Rhum", unit=UnitChoices.LITRE)
        self.dear = make_stock_type(name="Whisky Nikka", unit=UnitChoices.LITRE)
        for stock_type, unit_cost in ((self.cheap, "10"), (self.dear, "60")):
            product = make_product(
                supplier=self.supplier, raw_name=f"{stock_type.name} 70CL", stock_type=stock_type,
                unit=UnitChoices.UNIT, stock_equivalent="0.7",
            )
            # One purchase so the format registers as the one usually bought.
            make_invoice_line(
                invoice=make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, 1)),
                product=product, quantity=6, total_ht="60",
            )
            StockMovement.objects.create(
                stock_type=stock_type, quantity=Decimal("10"),
                unit_cost_ht=Decimal(unit_cost), kind=MovementKind.PURCHASE,
                occurred_on=date(2026, 1, 1),
            )
            setattr(self, f"product_{stock_type.pk}", product)

        self.recipe = make_recipe(name="Alcool + Soda", selling_price_ttc="8.50")
        make_ingredient(self.recipe, stock_type=self.cheap, quantity="0.04", group=0)
        make_ingredient(self.recipe, stock_type=self.dear, quantity="0.04", group=0)

    def build(self, opening, closing, sold):
        opening_take = make_stock_take(taken_at=at(1))
        closing_take = make_stock_take(taken_at=at(10))
        for stock_type, (open_qty, close_qty) in zip((self.cheap, self.dear), zip(opening, closing)):
            make_stock_take_line(
                stock_take=opening_take, stock_type=stock_type,
                counted_quantity=open_qty, unit=UnitChoices.LITRE,
            )
            make_stock_take_line(
                stock_take=closing_take, stock_type=stock_type,
                counted_quantity=close_qty, unit=UnitChoices.LITRE,
            )
        record_sales([(self.recipe.name, date(2026, 3, 5), sold)])
        report = compute_variance(closing_take)
        return next(p for p in report.pools if self.cheap in p.stock_types)

    def test_the_two_alternatives_are_reported_as_one_pool(self):
        pool = self.build(opening=("10", "10"), closing=("8", "8"), sold=100)
        self.assertEqual(sorted(st.name for st in pool.stock_types), ["Rhum", "Whisky Nikka"])
        # 2 L of each gone = 4 L; 100 drinks at 4cl explains 4 L.
        self.assertEqual(pool.actual_usage, Decimal("4"))
        self.assertEqual(pool.expected_usage_min, Decimal("4"))
        self.assertFalse(pool.is_missing)

    def test_the_split_between_alternatives_does_not_change_the_answer(self):
        """The whole point: it doesn't matter which bottle it came from."""
        balanced = self.build(opening=("10", "10"), closing=("8", "8"), sold=100)
        self.assertEqual(balanced.unexplained_min, Decimal("0"))

    def test_missing_stock_is_priced_at_the_cheapest_member(self):
        """2 litres unaccounted for, valued as if it were all the cheap rum -
        the smallest claim the data supports."""
        pool = self.build(opening=("10", "10"), closing=("7", "7"), sold=100)
        self.assertEqual(pool.unexplained_min, Decimal("2"))
        self.assertEqual(pool.cheapest, self.cheap)
        self.assertEqual(pool.cheapest_unit_cost, Decimal("10"))
        self.assertEqual(pool.value_missing_min, Decimal("20"))

    def test_missing_stock_is_counted_in_bottles_of_the_cheapest(self):
        pool = self.build(opening=("10", "10"), closing=("7", "7"), sold=100)
        self.assertEqual(pool.cheapest_item_size, Decimal("0.7"))
        # 2 L / 0.7 L per bottle
        self.assertAlmostEqual(pool.bottles_missing_min, Decimal("2") / Decimal("0.7"), places=6)

    def test_the_bottle_size_is_the_format_usually_bought(self):
        """A stock item can hold several formats - a vodka bought mostly in
        70cl but once in a 3-litre box. Taking the largest would quietly
        divide the bottle count by four; "how many bottles" is only useful
        if it means the bottle actually on the shelf."""
        from inventory.variance import typical_item_size

        big = make_product(
            supplier=self.supplier, raw_name="RHUM 3L", stock_type=self.cheap,
            unit=UnitChoices.UNIT, stock_equivalent="3",
        )
        usual = self.cheap.products.exclude(pk=big.pk).first()
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, 5))
        make_invoice_line(invoice=invoice, product=big, quantity=1, total_ht="30")
        for _ in range(4):
            make_invoice_line(invoice=invoice, product=usual, quantity=6, total_ht="60")

        self.assertEqual(typical_item_size(self.cheap), Decimal("0.7"))

    def test_the_value_is_a_floor_not_an_estimate(self):
        """Priced at the dear member it would be six times larger - which is
        exactly why the cheap one is used."""
        pool = self.build(opening=("10", "10"), closing=("7", "7"), sold=100)
        self.assertLess(pool.value_missing_min, Decimal("2") * Decimal("60"))


class DifferentQuantitiesTests(TestCase):
    """When alternatives use different amounts, the answer is a range - and
    the reported loss is the low end."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        self.recipe = make_recipe(name="Mule", selling_price_ttc="8.50")
        make_ingredient(self.recipe, stock_type=self.vodka, quantity="0.04", group=0)
        make_ingredient(self.recipe, stock_type=self.gin, quantity="0.05", group=0)

    def test_expected_usage_spans_the_options(self):
        opening = make_stock_take(taken_at=at(1))
        closing = make_stock_take(taken_at=at(10))
        for stock_type in (self.vodka, self.gin):
            make_stock_take_line(
                stock_take=opening, stock_type=stock_type, counted_quantity="10", unit=UnitChoices.LITRE
            )
            make_stock_take_line(
                stock_take=closing, stock_type=stock_type, counted_quantity="7", unit=UnitChoices.LITRE
            )
        record_sales([(self.recipe.name, date(2026, 3, 5), 100)])

        pool = next(p for p in compute_variance(closing).pools if self.vodka in p.stock_types)
        self.assertEqual(pool.expected_usage_min, Decimal("4"))   # all vodka
        self.assertEqual(pool.expected_usage_max, Decimal("5"))   # all gin
        self.assertEqual(pool.actual_usage, Decimal("6"))
        # The floor assumes the most generous reading (every drink was gin).
        self.assertEqual(pool.unexplained_min, Decimal("1"))
        self.assertEqual(pool.unexplained_max, Decimal("2"))


class ReportSummaryTests(TestCase):
    def test_pools_are_ranked_by_what_they_cost(self):
        supplier = make_supplier()
        cheap = make_stock_type(name="Limonade", unit=UnitChoices.LITRE)
        dear = make_stock_type(name="Whisky", unit=UnitChoices.LITRE)
        for stock_type, unit_cost in ((cheap, "1"), (dear, "40")):
            make_product(supplier=supplier, stock_type=stock_type, unit=UnitChoices.UNIT, stock_equivalent="1")
            StockMovement.objects.create(
                stock_type=stock_type, quantity=Decimal("100"), unit_cost_ht=Decimal(unit_cost),
                kind=MovementKind.PURCHASE, occurred_on=date(2026, 1, 1),
            )
        opening = make_stock_take(taken_at=at(1))
        closing = make_stock_take(taken_at=at(10))
        for stock_type in (cheap, dear):
            make_stock_take_line(
                stock_take=opening, stock_type=stock_type, counted_quantity="20", unit=UnitChoices.LITRE
            )
            make_stock_take_line(
                stock_take=closing, stock_type=stock_type, counted_quantity="18", unit=UnitChoices.LITRE
            )

        report = compute_variance(closing)
        self.assertEqual([pool.stock_types[0].name for pool in report.missing], ["Whisky", "Limonade"])
        self.assertEqual(report.total_value_missing_min, Decimal("2") * 40 + Decimal("2") * 1)

    def test_the_report_says_how_many_sales_it_used(self):
        vodka = make_stock_type(name="Vodka")
        recipe = make_recipe(name="Vodka tonic")
        make_ingredient(recipe, stock_type=vodka, quantity="0.04", group=0)
        other = make_recipe(name="Autre")
        make_ingredient(other, stock_type=vodka, quantity="0.04", group=0)

        opening = make_stock_take(taken_at=at(1))
        make_stock_take_line(stock_take=opening, stock_type=vodka, counted_quantity="10", unit=UnitChoices.LITRE)
        closing = make_stock_take(taken_at=at(10))
        make_stock_take_line(stock_take=closing, stock_type=vodka, counted_quantity="6", unit=UnitChoices.LITRE)
        record_sales([(recipe.name, date(2026, 3, 5), 60), (other.name, date(2026, 3, 6), 40)])

        report = compute_variance(closing)
        self.assertEqual(report.sales_counted, 100)
        self.assertEqual(report.recipes_sold, 2)


class SingleInventoryTests(TestCase):
    """One inventory is enough, when the invoices go back far enough.

    You know everything you bought, everything you sold, and what is on the
    shelf now - those three have to reconcile, and nothing about that needs
    an EARLIER count. Opening stock is simply zero: nothing existed before
    the first invoice.
    """

    def setUp(self):
        self.supplier = make_supplier()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.product = make_product(
            supplier=self.supplier, raw_name="VODKA 70CL", stock_type=self.vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        self.recipe = make_recipe(name="Vodka tonic")
        make_ingredient(self.recipe, stock_type=self.vodka, quantity="0.04", group=0)

    def buy(self, litres, on, cost="10"):
        invoice = make_invoice(supplier=self.supplier, invoice_date=on)
        line = make_invoice_line(invoice=invoice, product=self.product, quantity=1, total_ht=cost)
        StockMovement.objects.create(
            stock_type=self.vodka, quantity=Decimal(litres), unit_cost_ht=Decimal(cost),
            invoice_line=line, kind=MovementKind.PURCHASE,
        )

    def count(self, litres, day=31):
        take = make_stock_take(taken_at=at(day))
        make_stock_take_line(
            stock_take=take, stock_type=self.vodka,
            counted_quantity=litres, unit=UnitChoices.LITRE,
        )
        return take

    def pool(self, report):
        return next(p for p in report.pools if self.vodka in p.stock_types)

    def test_everything_bought_minus_everything_sold_should_be_on_the_shelf(self):
        self.buy("20", on=date(2026, 3, 2))
        record_sales([(self.recipe.name, date(2026, 3, 15), 100)])   # 4 L
        pool = self.pool(compute_variance(self.count("16")))
        self.assertEqual(pool.purchases, Decimal("20"))
        self.assertEqual(pool.actual_usage, Decimal("4"))   # 0 + 20 - 16
        self.assertEqual(pool.expected_usage_min, Decimal("4"))
        self.assertFalse(pool.is_missing)

    def test_a_shortfall_shows_with_only_one_count(self):
        self.buy("20", on=date(2026, 3, 2))
        record_sales([(self.recipe.name, date(2026, 3, 15), 100)])
        pool = self.pool(compute_variance(self.count("14")))
        self.assertEqual(pool.unexplained_min, Decimal("2"))
        self.assertTrue(pool.is_missing)

    def test_the_report_says_it_measured_since_the_beginning(self):
        """The page has to be able to say so out loud: this mode assumes the
        invoices reach back to the day the bar opened."""
        self.buy("20", on=date(2026, 3, 2))
        report = compute_variance(self.count("16"))
        self.assertTrue(report.since_beginning)
        self.assertIsNone(report.period_start)
        self.assertIsNone(report.opening_take)

    def test_every_purchase_counts_however_old(self):
        """No opening count means no lower bound on the window."""
        self.buy("10", on=date(2024, 1, 1))
        self.buy("10", on=date(2026, 3, 2))
        self.assertEqual(self.pool(compute_variance(self.count("20"))).purchases, Decimal("20"))

    def test_a_purchase_after_the_count_is_still_excluded(self):
        self.buy("20", on=date(2026, 3, 2))
        self.buy("50", on=date(2026, 4, 20))
        self.assertEqual(self.pool(compute_variance(self.count("20"))).purchases, Decimal("20"))

    def test_known_losses_still_apply(self):
        self.buy("20", on=date(2026, 3, 2))
        StockMovement.objects.create(
            stock_type=self.vodka, quantity=Decimal("-1"), unit_cost_ht=Decimal("10"),
            kind=MovementKind.LOSS, note="Cassée", occurred_on=date(2026, 3, 10),
        )
        pool = self.pool(compute_variance(self.count("19")))
        self.assertEqual(pool.known_losses, Decimal("1"))
        self.assertFalse(pool.is_missing)

    def test_an_uncounted_item_is_still_never_called_missing(self):
        self.buy("20", on=date(2026, 3, 2))
        take = make_stock_take(taken_at=at(31))
        make_stock_take_line(
            stock_take=take, stock_type=make_stock_type(name="Autre"),
            counted_quantity="1", unit=UnitChoices.LITRE,
        )
        report = compute_variance(take)
        pool = self.pool(report)
        self.assertNotIn(pool, report.missing)
        self.assertIn(pool, report.incomplete)

    def test_a_second_count_switches_back_to_measuring_the_window(self):
        """With two counts the invoice history before the opening one stops
        mattering - which is the whole point of having two."""
        self.buy("100", on=date(2024, 1, 1))       # long before, deliberately ignored
        first = make_stock_take(taken_at=at(1))
        make_stock_take_line(
            stock_take=first, stock_type=self.vodka, counted_quantity="20", unit=UnitChoices.LITRE
        )
        self.buy("10", on=date(2026, 3, 10))
        record_sales([(self.recipe.name, date(2026, 3, 15), 100)])   # 4 L

        report = compute_variance(self.count("26"))
        self.assertFalse(report.since_beginning)
        self.assertEqual(report.opening_take, first)
        pool = self.pool(report)
        self.assertEqual(pool.opening, Decimal("20"))
        self.assertEqual(pool.purchases, Decimal("10"))     # not the 100 from 2024
        self.assertEqual(pool.actual_usage, Decimal("4"))
        self.assertFalse(pool.is_missing)


class NestedAlternativePoolingTests(TestCase):
    """A sub-recipe with alternatives of its own is still a choice, so the
    stock it can draw on has to end up in one pool - otherwise each member
    is measured on its own and whatever the other one was used for reads as
    missing."""

    def setUp(self):
        self.sugar = make_stock_type(name="Sucre", unit=UnitChoices.KILOGRAM)
        self.honey = make_stock_type(name="Miel", unit=UnitChoices.KILOGRAM)
        self.syrup = make_recipe(name="Sirop maison", yield_quantity="1")
        make_ingredient(self.syrup, stock_type=self.sugar, quantity="1", group=0)
        make_ingredient(self.syrup, stock_type=self.honey, quantity="1", group=0)

    def test_a_nested_choice_pools_its_stock_items(self):
        cocktail = make_recipe(name="Cocktail")
        make_ingredient(cocktail, sub_recipe=self.syrup, quantity="0.02", group=0)
        pools = build_pools([cocktail])
        self.assertEqual(pools[self.sugar.id], frozenset({self.sugar.id, self.honey.id}))

    def test_pooling_reaches_through_two_levels(self):
        middle = make_recipe(name="Milieu", yield_quantity="1")
        make_ingredient(middle, sub_recipe=self.syrup, quantity="1", group=0)
        cocktail = make_recipe(name="Cocktail")
        make_ingredient(cocktail, sub_recipe=middle, quantity="0.02", group=0)
        pools = build_pools([cocktail])
        self.assertEqual(pools[self.honey.id], frozenset({self.sugar.id, self.honey.id}))

    def test_a_sub_recipe_without_choices_is_not_pooled(self):
        """Its ingredients are consumed in fixed proportions, so each one
        gets an exact answer of its own."""
        fixed = make_recipe(name="Fixe", yield_quantity="1")
        make_ingredient(fixed, stock_type=self.sugar, quantity="1", group=0)
        make_ingredient(fixed, stock_type=self.honey, quantity="1", group=1)
        cocktail = make_recipe(name="Cocktail")
        make_ingredient(cocktail, sub_recipe=fixed, quantity="0.02", group=0)
        pools = build_pools([cocktail])
        self.assertEqual(pools[self.sugar.id], frozenset({self.sugar.id}))

    def test_the_nested_options_appear_as_alternatives_in_the_usage_terms(self):
        cocktail = make_recipe(name="Cocktail")
        make_ingredient(cocktail, sub_recipe=self.syrup, quantity="0.02", group=0)
        terms = recipe_usage_terms(cocktail)
        self.assertEqual(len(terms), 1)
        self.assertEqual(
            sorted(sorted(option) for option in terms[0]),
            [[self.sugar.id], [self.honey.id]] if self.sugar.id < self.honey.id
            else [[self.honey.id], [self.sugar.id]],
        )

    def test_an_astronomically_nested_recipe_does_not_hang_the_report(self):
        """The amounts are capped (MAX_SUB_VARIATIONS); the pools never are."""
        deep = make_recipe(name="Profond", yield_quantity="1")
        for group in range(40):
            make_ingredient(deep, sub_recipe=self.syrup, quantity="1", group=group)
        cocktail = make_recipe(name="Cocktail")
        make_ingredient(cocktail, sub_recipe=deep, quantity="0.02", group=0)

        pools = build_pools([cocktail])
        self.assertEqual(pools[self.sugar.id], frozenset({self.sugar.id, self.honey.id}))
