"""How much of a recipe goes out with each sale.

`yield_quantity`/`yield_unit` say how much one full preparation PRODUCES - a
terrine of 1,6 kg. They never said how much ONE SALE takes out of it, and two
pages read them as if they did: `margins.computation._recipe_costs` divides
the batch cost by the yield and calls the result the cost of a sale, and
`inventory.variance._usage_terms` divides the batch's consumption the same way
and calls it consumption per serving sold. Both are right only while the yield
is counted in servings - which is why nothing is wrong today (every recipe on
the real database is in « Unité », read-only) and why a
terrine would be out by the portion ratio the moment one is filed: a 1,6 kg
batch sold in 150 g plates would be charged a whole kilo a plate, ten times
what it costs.

`sale_quantity` is that missing figure, in the unit the recipe produces. One
sale is `sale_quantity / yield_quantity` of a preparation, and that ratio is
written once (`Recipe.sold_share`) so the two pages cannot drift apart.

It defaults to 1, which is exactly what both pages assume today: every recipe
already filed keeps the cost and the consumption it has, to the last decimal.

What it must NOT touch is the per-yield-unit figures - `unit_cost_ht`,
`unit_cost_bounds`, `_sub_recipe_usage_per_yield_unit`. A parent recipe buys
its sub-recipe BY THE UNIT PRODUCED; how that sub-recipe happens to be sold
over the counter is none of the parent's business, and folding the portion in
there would price a syrup by the glass inside a cocktail that uses 2 cl.

Every name and figure below is invented.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from inventory.models import UnitChoices
from inventory.variance import recipe_usage_terms
from margins.computation import _recipe_costs
from recipes.models import Recipe, variation_scope
from tests.factories import make_ingredient, make_priced_stock_type, make_recipe

CENT = Decimal("0.01")


def usage_of(recipe) -> dict[int, Decimal]:
    """What ONE SALE of `recipe` takes off the shelf, for a recipe with no
    alternatives - `recipe_usage_terms` returns one option per choice group,
    and these fixtures have exactly one of each."""
    with variation_scope():
        terms = recipe_usage_terms(recipe)
    usage: dict[int, Decimal] = {}
    for term in terms:
        (only,) = term
        for stock_type_id, amount in only.items():
            usage[stock_type_id] = usage.get(stock_type_id, Decimal("0")) + amount
    return usage


class Fixtures:
    def terrine(self, sale_quantity="0.15"):
        """A 1,6 kg terrine of one article costing 20,00 € the kilo: the
        batch costs 20,00 €, a 150 g plate costs 1,875 €."""
        pork = make_priced_stock_type(name="Porc", unit=UnitChoices.KILOGRAM, unit_cost_ht="20", quantity="10")
        recipe = make_recipe(
            name="Terrine",
            yield_quantity="1.6",
            yield_unit=UnitChoices.KILOGRAM,
            sale_quantity=Decimal(sale_quantity),
            selling_price_ttc="6.00",
        )
        make_ingredient(recipe, stock_type=pork, quantity="1")
        return recipe, pork


class TheRatioTests(Fixtures, TestCase):
    def test_one_sale_is_its_share_of_a_preparation(self):
        recipe, _ = self.terrine()
        self.assertEqual(recipe.sold_share, Decimal("0.15") / Decimal("1.6"))

    def test_a_preparation_makes_that_many_servings(self):
        recipe, _ = self.terrine()
        self.assertEqual(recipe.servings_per_batch.quantize(CENT), (Decimal("1.6") / Decimal("0.15")).quantize(CENT))

    def test_a_recipe_sold_whole_sells_one_for_one(self):
        """The default, and what every recipe already filed means."""
        recipe = make_recipe(name="Mojito")
        self.assertEqual(recipe.sale_quantity, Decimal("1"))
        self.assertEqual(recipe.sold_share, Decimal("1"))

    def test_a_batch_of_several_servings_still_divides_by_the_yield(self):
        """« Basilic » yields 11 and sells 1 of them: the shape the two
        existing non-unit recipes already have."""
        recipe = make_recipe(name="Basilic", yield_quantity="11")
        self.assertEqual(recipe.sold_share, Decimal("1") / Decimal("11"))

    def test_a_sale_of_nothing_is_refused(self):
        recipe = make_recipe(name="Vide", sale_quantity=Decimal("0"))
        with self.assertRaises(ValidationError):
            recipe.full_clean()

    def test_a_negative_sale_is_refused(self):
        recipe = make_recipe(name="Négatif", sale_quantity=Decimal("-1"))
        with self.assertRaises(ValidationError):
            recipe.full_clean()


class TheCostOfOneSaleTests(Fixtures, TestCase):
    def costs(self, recipe):
        with variation_scope():
            costs, uncosted = _recipe_costs([recipe.pk])
        return costs.get(recipe.pk), uncosted.get(recipe.pk)

    def test_a_portion_costs_its_share_of_the_batch(self):
        """The regression that matters: charged a whole kilo, a 150 g plate
        costs 12,50 € against the 1,875 € it really takes."""
        recipe, _ = self.terrine()
        (low, high), reason = self.costs(recipe)
        self.assertIsNone(reason)
        self.assertEqual(high.quantize(Decimal("0.0001")), Decimal("1.8750"))
        self.assertEqual(low, high)

    def test_a_recipe_sold_whole_costs_what_it_always_did(self):
        article = make_priced_stock_type(name="Rhum", unit_cost_ht="30", quantity="10")
        recipe = make_recipe(name="Ti-punch")
        make_ingredient(recipe, stock_type=article, quantity="0.05")
        (_, high), _ = self.costs(recipe)
        self.assertEqual(high.quantize(Decimal("0.0001")), Decimal("1.5000"))

    def test_a_batch_of_eleven_costs_a_eleventh_a_sale(self):
        article = make_priced_stock_type(name="Sirop", unit_cost_ht="11", quantity="100")
        recipe = make_recipe(name="Basilic", yield_quantity="11")
        make_ingredient(recipe, stock_type=article, quantity="1")
        (_, high), _ = self.costs(recipe)
        self.assertEqual(high.quantize(Decimal("0.0001")), Decimal("1.0000"))

    def test_a_recipe_selling_nothing_is_not_costed_rather_than_free(self):
        """A sale of zero costs zero, which reads as a 100 % margin - the
        one thing « Marges » must never print. It is uncosted instead."""
        recipe, _ = self.terrine(sale_quantity="0")
        costs, reason = self.costs(recipe)
        self.assertIsNone(costs)
        self.assertTrue(reason)


class ASubRecipeIsBoughtByTheUnitTests(Fixtures, TestCase):
    """A parent buys its sub-recipe BY THE UNIT PRODUCED. How that
    sub-recipe is sold over the counter is none of the parent's business:
    folded in there, a syrup sold by the glass would price a cocktail that
    uses 2 cl of it as if it drank the glass.
    """

    def syrup_and_cocktail(self, sale_quantity):
        """The pair built twice in one test, so every name is its own: a
        stock item's name is unique, and the point is that only the portion
        differs between the two."""
        sugar = make_priced_stock_type(
            name=f"Sucre {sale_quantity}", unit=UnitChoices.LITRE, unit_cost_ht="10", quantity="100"
        )
        syrup = make_recipe(
            name=f"Sirop maison {sale_quantity}",
            yield_quantity="2",
            yield_unit=UnitChoices.LITRE,
            sale_quantity=Decimal(sale_quantity),
            selling_price_ttc="4.00",
        )
        make_ingredient(syrup, stock_type=sugar, quantity="2")
        cocktail = make_recipe(name=f"Cocktail {sale_quantity}")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02")
        return syrup, cocktail

    def test_the_parent_pays_the_same_whatever_the_portion_sold(self):
        whole, cocktail_whole = self.syrup_and_cocktail("1")
        portion, cocktail_portion = self.syrup_and_cocktail("0.05")
        with variation_scope():
            self.assertEqual(whole.unit_cost_ht(), portion.unit_cost_ht())
            self.assertEqual(cocktail_whole.summary()["cost_range"], cocktail_portion.summary()["cost_range"])

    def test_the_parent_consumes_the_same_whatever_the_portion_sold(self):
        _, cocktail_whole = self.syrup_and_cocktail("1")
        _, cocktail_portion = self.syrup_and_cocktail("0.05")
        self.assertEqual(sorted(usage_of(cocktail_whole).values()), sorted(usage_of(cocktail_portion).values()))


class WhatOneSaleConsumesTests(Fixtures, TestCase):
    """The variance engine counts what a sale took off the shelf. Read per
    batch, a terrine sold in plates would report ten times its consumption
    as missing stock."""

    def test_a_portion_consumes_its_share(self):
        recipe, pork = self.terrine()
        self.assertEqual(usage_of(recipe)[pork.pk], Decimal("1") * Decimal("0.15") / Decimal("1.6"))

    def test_a_recipe_sold_whole_consumes_what_it_always_did(self):
        article = make_priced_stock_type(name="Gin", unit_cost_ht="30", quantity="10")
        recipe = make_recipe(name="Gin tonic", yield_quantity="4")
        make_ingredient(recipe, stock_type=article, quantity="1")
        self.assertEqual(usage_of(recipe)[article.pk], Decimal("1") / Decimal("4"))


class EveryRecipeAlreadyFiledIsUntouchedTests(TestCase):
    """The default has to be the assumption the two pages already make, or
    every recipe on the real database silently changes cost the day this
    ships."""

    def test_the_default_is_one(self):
        self.assertEqual(Recipe._meta.get_field("sale_quantity").default, Decimal("1"))

    def test_a_recipe_made_without_saying_sells_one_for_one(self):
        self.assertEqual(make_recipe(name="Sans rien dire").sold_share, Decimal("1"))


class TheMarginOnTheRecipePagesTests(Fixtures, TestCase):
    """The margin, the % and the factor on « Recettes » and on a recipe's own
    page put the price of ONE SALE against the cost of a WHOLE PREPARATION.

    That is wrong today, before any portion exists: a recipe yielding 10,
    sold at 4,00 € HT and costing 6,00 € the batch, has its own page read
    « marge -2,00 € », « facteur x0,67 » - sold at a loss - while
    `margins.computation` costs it at one tenth and « Marges » reads a
    healthy margin. Two pages of this app, a factor of the yield apart, on a
    kind of recipe the till really sells. The portion only makes it worse: a
    terrine costing 20,00 € the batch and sold at 5,00 € the plate would read
    -15,00 € where it really makes +3,13 €.

    `cost_range` itself stays the whole preparation: it is what the detail
    page prints as « Coût total pour 1,6 kg », and what `unit_cost_bounds`
    divides by the yield for a parent recipe.
    """

    def terrine_costing(self, batch="20.00", price_ttc="6.00", sale="0.15"):
        pork = make_priced_stock_type(
            name=f"Porc {batch}-{sale}", unit=UnitChoices.KILOGRAM, unit_cost_ht=batch, quantity="100"
        )
        recipe = make_recipe(
            name=f"Terrine {batch}-{sale}",
            yield_quantity="1.6",
            yield_unit=UnitChoices.KILOGRAM,
            sale_quantity=Decimal(sale),
            selling_price_ttc=price_ttc,
            vat_rate="0.20",
        )
        make_ingredient(recipe, stock_type=pork, quantity="1")
        return recipe

    def test_the_margin_puts_one_sale_against_one_sale(self):
        recipe = self.terrine_costing()
        with variation_scope():
            summary = recipe.summary()
        cost_per_sale = Decimal("20.00") * Decimal("0.15") / Decimal("1.6")
        self.assertEqual(summary["margin_range"][0], recipe.selling_price_ht - cost_per_sale)

    def test_the_price_factor_is_per_sale_too(self):
        recipe = self.terrine_costing()
        with variation_scope():
            low, high = recipe.summary()["price_factor_range"]
        cost_per_sale = Decimal("20.00") * Decimal("0.15") / Decimal("1.6")
        self.assertEqual(low, recipe.selling_price_ht / cost_per_sale)
        self.assertEqual(high, low)

    def test_a_batch_of_several_servings_no_longer_reads_as_sold_at_a_loss(self):
        """The shape of the class docstring's example: a batch of ten dearer
        than one sale's price, cheap per sale."""
        article = make_priced_stock_type(name="Sirop maison", unit_cost_ht="6.00", quantity="100")
        recipe = make_recipe(name="Cocktail au sirop", yield_quantity="10", selling_price_ttc="4.80", vat_rate="0.20")
        make_ingredient(recipe, stock_type=article, quantity="1")
        with variation_scope():
            summary = recipe.summary()
        self.assertGreater(summary["margin_range"][0], 0)
        self.assertGreater(summary["price_factor_range"][0], 1)

    def test_the_chosen_variation_says_the_same(self):
        recipe = self.terrine_costing()
        with variation_scope():
            variation = recipe.variation_at(0)
        cost_per_sale = Decimal("20.00") * Decimal("0.15") / Decimal("1.6")
        self.assertEqual(variation["cost_per_sale_ht"], cost_per_sale)
        self.assertEqual(variation["margin_ht"], recipe.selling_price_ht - cost_per_sale)

    def test_the_happy_hour_margin_is_per_sale_too(self):
        recipe = self.terrine_costing()
        recipe.happy_hour_price_ttc = Decimal("4.80")
        recipe.save(update_fields=["happy_hour_price_ttc"])
        with variation_scope():
            variation = recipe.variation_at(0)
        cost_per_sale = Decimal("20.00") * Decimal("0.15") / Decimal("1.6")
        self.assertEqual(variation["happy_hour"]["margin_ht"], recipe.happy_hour_price_ht - cost_per_sale)

    def test_the_cost_range_stays_the_whole_preparation(self):
        """What the detail page prints as « Coût total pour 1,60 kg », and
        what `unit_cost_bounds` divides by the yield for a parent."""
        recipe = self.terrine_costing()
        with variation_scope():
            low, high = recipe.summary()["cost_range"]
            variation = recipe.variation_at(0)
        self.assertEqual(high, Decimal("20.00"))
        self.assertEqual(variation["cost_ht"], Decimal("20.00"))
        self.assertEqual(low, high)

    def test_a_recipe_sold_whole_keeps_the_margin_it_had(self):
        recipe = self.terrine_costing(sale="1.6")
        with variation_scope():
            summary = recipe.summary()
        self.assertEqual(summary["margin_range"][0], recipe.selling_price_ht - Decimal("20.00"))
