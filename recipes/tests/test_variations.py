"""Tests for how a recipe's variations are counted, costed and bounded.

Two things are being pinned down here, and the second is what makes the
first safe:

* the numbers are right - checked against brute-force enumeration, which is
  the definition of what a variation is;
* they're computed WITHOUT enumerating, so a recipe with a million
  variations is no more expensive than one with two.

The shortcut only holds because every quantity involved is monotonic in the
total cost. If someone later adds a metric that isn't, these tests fail
against the brute-force reference rather than silently reporting a wrong
range - which is the entire reason the reference is written out longhand
below instead of reusing the same helpers.
"""

import itertools
import math
import time
from decimal import Decimal

from django.test import TestCase

from recipes.models import Recipe, RecipeIngredient
from tests.factories import make_priced_stock_type, make_recipe, make_stock_type


class FakeIngredient:
    """Stands in for a RecipeIngredient at a known cost, with no database
    behind it - `choice_groups`/`summary`/`variations` only ever ask an
    ingredient for its group, its cost and its name.

    This is what lets the exhaustive shape sweep below run in memory.
    """

    def __init__(self, group: int, cost: str, name: str):
        self.group = group
        self.quantity = Decimal("1")
        self.source_name = name
        self.sub_recipe_id = None
        self._cost = Decimal(cost)

    def unit_cost_ht(self, sub_index: int = 0) -> Decimal:
        return self._cost

    def cost_ht(self, sub_index: int = 0) -> Decimal:
        return self._cost

    def cost_bounds(self) -> tuple:
        # A plain stock item: one price, so both ends are the same.
        return self._cost, self._cost

    def variation_name(self, sub_index: int = 0) -> str:
        return self.source_name


def fake_ingredients(group_costs: list[list[str]]) -> list[FakeIngredient]:
    return [
        FakeIngredient(group_index, cost, f"g{group_index}o{option_index}")
        for group_index, costs in enumerate(group_costs)
        for option_index, cost in enumerate(costs)
    ]


def fake_recipe(group_costs, **kwargs) -> Recipe:
    """An UNSAVED recipe - only its own price fields are read."""
    kwargs.setdefault("name", "R")
    kwargs.setdefault("selling_price_ttc", Decimal("12.00"))
    kwargs.setdefault("vat_rate", Decimal("0.20"))
    kwargs.setdefault("yield_quantity", Decimal("1"))
    for key in ("selling_price_ttc", "happy_hour_price_ttc"):
        if isinstance(kwargs.get(key), str):
            kwargs[key] = Decimal(kwargs[key])
    return Recipe(**kwargs)


def brute_force_summary(recipe: Recipe, ingredients=None) -> dict:
    """What summary() must agree with, computed the obvious slow way: build
    every variation, then take the min and max of each column."""
    variations = recipe.variations(ingredients)
    if not variations:
        return {
            "variation_count": 0,
            "cost_range": None,
            "margin_range": None,
            "margin_percent_range": None,
            "price_factor_range": None,
        }

    def rng(key):
        values = [v[key] for v in variations if v[key] is not None]
        return (min(values), max(values)) if values else None

    return {
        "variation_count": len(variations),
        "cost_range": rng("cost_ht"),
        "margin_range": rng("margin_ht"),
        "margin_percent_range": rng("margin_percent"),
        "price_factor_range": rng("price_factor"),
    }


def build_recipe(group_costs: list[list[str]], **recipe_kwargs) -> Recipe:
    """A recipe shaped by cost alone: `group_costs[g][i]` is what option i of
    group g costs, via a stock item priced to make it so (quantity 1)."""
    recipe = make_recipe(**recipe_kwargs)
    for group_index, costs in enumerate(group_costs):
        for cost in costs:
            RecipeIngredient.objects.create(
                recipe=recipe,
                stock_type=make_priced_stock_type(unit_cost_ht=cost, quantity="1"),
                quantity=Decimal("1"),
                group=group_index,
            )
    return recipe


class VariationCountTests(TestCase):
    def test_no_ingredients_means_no_variations(self):
        self.assertEqual(make_recipe().variation_count, 0)

    def test_ingredients_all_in_their_own_group_give_one_variation(self):
        recipe = build_recipe([["1"], ["2"], ["3"]])
        self.assertEqual(recipe.variation_count, 1)
        self.assertFalse(recipe.has_variations)

    def test_one_choice_of_two(self):
        recipe = build_recipe([["1", "2"]])
        self.assertEqual(recipe.variation_count, 2)
        self.assertTrue(recipe.has_variations)

    def test_the_count_is_the_product_of_the_group_sizes(self):
        recipe = build_recipe([["1", "2", "3"], ["4", "5"], ["6"], ["7", "8", "9", "10"]])
        self.assertEqual(recipe.variation_count, 3 * 2 * 1 * 4)
        self.assertEqual(recipe.variation_count, len(recipe.variations()))


class SummaryMatchesBruteForceTests(TestCase):
    """The analytic shortcut against the definition, over many shapes."""

    SHAPES = [
        ([["5"]], "single ingredient"),
        ([["5"], ["3"]], "two fixed ingredients"),
        ([["5", "3"]], "one either/or"),
        ([["5", "3"], ["2", "7"]], "two either/ors"),
        ([["5", "3", "9"], ["2"], ["1", "4"]], "mixed group sizes"),
        ([["0", "3"]], "an option that costs nothing"),
        ([["0"], ["4", "6"]], "a whole group that costs nothing"),
        ([["0", "0"]], "every option costs nothing"),
        ([["0.0001", "12.3456"]], "tiny and large"),
        ([["5", "5"]], "identical options"),
        ([["1", "2"], ["3", "4"], ["5", "6"], ["7", "8"], ["9", "10"]], "five either/ors"),
    ]

    def test_summary_matches_brute_force(self):
        for group_costs, label in self.SHAPES:
            with self.subTest(shape=label):
                recipe = build_recipe(group_costs, selling_price_ttc="12.00")
                self.assertEqual(recipe.summary(), brute_force_summary(recipe))

    def test_summary_matches_brute_force_without_a_happy_hour_price(self):
        for group_costs, label in self.SHAPES:
            with self.subTest(shape=label):
                recipe = build_recipe(group_costs, selling_price_ttc="12.00", happy_hour_price_ttc=None)
                self.assertEqual(recipe.summary(), brute_force_summary(recipe))

    def test_summary_matches_brute_force_at_a_zero_selling_price(self):
        for group_costs, label in self.SHAPES:
            with self.subTest(shape=label):
                recipe = build_recipe(group_costs, selling_price_ttc="0")
                self.assertEqual(recipe.summary(), brute_force_summary(recipe))

    def test_summary_matches_brute_force_across_every_small_shape(self):
        """Exhaustive: every shape of up to 3 groups holding up to 3 options,
        with costs (including 0) drawn from a small pool - 7,239 recipes in
        all. That's the whole space where an off-by-one in the bounds, or a
        mishandled zero, could hide.

        Run entirely in memory (see FakeIngredient): the arithmetic is what's
        under test, and building 7,239 recipes in the database to check it
        would take minutes for no extra confidence.
        """
        pool = ["0", "2", "7"]
        option_sets = [
            list(itertools.combinations_with_replacement(pool, size)) for size in (1, 2, 3)
        ]
        checked = 0
        for group_count in (1, 2, 3):
            for sizes in itertools.product((0, 1, 2), repeat=group_count):
                for costs in itertools.product(*[option_sets[size] for size in sizes]):
                    group_costs = [list(c) for c in costs]
                    recipe = fake_recipe(group_costs, selling_price_ttc="12.00")
                    ingredients = fake_ingredients(group_costs)
                    if recipe.summary(ingredients) != brute_force_summary(recipe, ingredients):
                        self.fail(f"summary() disagrees with brute force for {group_costs}")
                    checked += 1
        self.assertEqual(checked, 7239, "the shape space changed - is that intended?")


class VariationSelectionTests(TestCase):
    def setUp(self):
        self.recipe = build_recipe([["1", "2", "3"], ["10", "20"]], selling_price_ttc="12.00")

    def test_variation_at_matches_the_enumerated_order(self):
        every = self.recipe.variations()
        for index, expected in enumerate(every):
            with self.subTest(index=index):
                self.assertEqual(self.recipe.variation_at(index)["cost_ht"], expected["cost_ht"])
                self.assertEqual(self.recipe.variation_at(index)["name"], expected["name"])

    def test_variation_at_out_of_range(self):
        self.assertIsNone(self.recipe.variation_at(6))
        self.assertIsNone(self.recipe.variation_at(-1))
        self.assertIsNone(make_recipe().variation_at(0))

    def test_variation_for_picks_one_option_per_group(self):
        variation = self.recipe.variation_for([2, 1])
        self.assertEqual(variation["cost_ht"], Decimal("3") + Decimal("20"))

    def test_variation_for_falls_back_on_an_out_of_range_pick(self):
        """A bookmarked link must survive the recipe being edited."""
        self.assertEqual(self.recipe.variation_for([99, 0])["cost_ht"], Decimal("1") + Decimal("10"))
        self.assertEqual(self.recipe.variation_for([-5, 0])["cost_ht"], Decimal("1") + Decimal("10"))

    def test_variation_for_pads_a_short_selection(self):
        self.assertEqual(self.recipe.variation_for([1])["cost_ht"], Decimal("2") + Decimal("10"))
        self.assertEqual(self.recipe.variation_for([])["cost_ht"], Decimal("1") + Decimal("10"))

    def test_variation_for_ignores_extra_indices(self):
        self.assertEqual(self.recipe.variation_for([0, 0, 7, 7])["cost_ht"], Decimal("11"))

    def test_variation_selections_names_every_combination(self):
        names = [name for _indices, name in self.recipe.variation_selections()]
        self.assertEqual(len(names), 6)
        self.assertEqual(names, [v["name"] for v in self.recipe.variations()])

    def test_only_groups_with_a_real_choice_appear_in_the_name(self):
        recipe = build_recipe([["1", "2"], ["10"]])
        for _indices, name in recipe.variation_selections():
            self.assertEqual(name.count(","), 0, f"{name} names a group that has no alternatives")


class LargeRecipeTests(TestCase):
    """The user's actual requirement: millions of combinations, no problem.

    20 either/or choices is 1,048,576 variations - not an exotic recipe, just
    twenty ingredients that each have a substitute. Enumerating those means
    building a million dicts each holding a 20-entry breakdown; these all
    used to hang for minutes or run out of memory.
    """

    @classmethod
    def setUpTestData(cls):
        cls.recipe = build_recipe([["2", "3"]] * 20, selling_price_ttc="500.00")

    def test_a_million_variations_are_counted_without_enumerating_them(self):
        started = time.perf_counter()
        count = self.recipe.variation_count
        elapsed = time.perf_counter() - started
        self.assertEqual(count, 2**20)
        self.assertLess(elapsed, 2.0, "counting should not depend on the number of variations")

    def test_ranges_over_a_million_variations(self):
        started = time.perf_counter()
        summary = self.recipe.summary()
        elapsed = time.perf_counter() - started
        # Cheapest picks 2 twenty times; dearest picks 3 twenty times.
        self.assertEqual(summary["cost_range"], (Decimal("40"), Decimal("60")))
        self.assertLess(elapsed, 2.0)

    def test_one_specific_variation_out_of_a_million(self):
        variation = self.recipe.variation_at(2**20 - 1)
        self.assertEqual(variation["cost_ht"], Decimal("60"))  # the last: all 3s
        self.assertEqual(self.recipe.variation_at(0)["cost_ht"], Decimal("40"))  # the first: all 2s

    def test_the_list_page_renders(self):
        started = time.perf_counter()
        response = self.client.get("/recipes/")
        elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1048576")
        self.assertLess(elapsed, 5.0)

    def test_the_detail_page_renders(self):
        started = time.perf_counter()
        response = self.client.get(f"/recipes/{self.recipe.pk}/")
        elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 5.0)

    def test_the_detail_page_does_not_list_a_million_options(self):
        response = self.client.get(f"/recipes/{self.recipe.pk}/")
        # One dropdown per choice, not one option per combination.
        self.assertContains(response, 'class="js-group-picker"', count=20)
        self.assertNotContains(response, 'id="variation-select"')  # the list-them-all picker
        self.assertLess(len(response.content), 100_000, "the page is enumerating variations")

    def test_the_edit_page_renders(self):
        started = time.perf_counter()
        response = self.client.get(f"/recipes/{self.recipe.pk}/edit/")
        elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 5.0)

    def test_an_astronomically_large_recipe_still_works(self):
        """60 either/ors is more variations than there are grains of sand on
        Earth. Nothing in the aggregate path should care."""
        recipe = build_recipe([["2", "3"]] * 60, selling_price_ttc="500.00")
        self.assertEqual(recipe.variation_count, 2**60)
        self.assertEqual(recipe.summary()["cost_range"], (Decimal("120"), Decimal("180")))
        self.assertEqual(self.client.get(f"/recipes/{recipe.pk}/").status_code, 200)


class SubRecipeCostingTests(TestCase):
    def test_a_sub_recipe_costs_per_unit_of_its_own_yield(self):
        syrup = make_recipe(name="Sirop", yield_quantity="2")
        RecipeIngredient.objects.create(
            recipe=syrup, stock_type=make_priced_stock_type(unit_cost_ht="4", quantity="10"),
            quantity=Decimal("3"), group=0,
        )
        # 3 units at 4.00 = 12.00 per batch, yielding 2 => 6.00 per unit.
        self.assertEqual(syrup.cost_ht(), Decimal("12"))
        self.assertEqual(syrup.unit_cost_ht(), Decimal("6"))

        cocktail = make_recipe(name="Cocktail")
        RecipeIngredient.objects.create(recipe=cocktail, sub_recipe=syrup, quantity=Decimal("0.5"), group=0)
        self.assertEqual(cocktail.cost_ht(), Decimal("3"))

    def test_a_zero_yield_sub_recipe_costs_nothing_rather_than_dividing_by_zero(self):
        syrup = make_recipe(name="Sirop", yield_quantity="0")
        RecipeIngredient.objects.create(
            recipe=syrup, stock_type=make_priced_stock_type(unit_cost_ht="4", quantity="10"),
            quantity=Decimal("3"), group=0,
        )
        self.assertEqual(syrup.unit_cost_ht(), Decimal("0"))

    def test_a_sub_recipe_with_alternatives_costs_from_its_first_variation(self):
        """cost_ht() has to answer for a recipe with choices even though the
        form won't let one be used as an ingredient - the data can still get
        there through the admin or a later edit."""
        syrup = build_recipe([["4", "9"]], name="Sirop")
        self.assertEqual(syrup.cost_ht(), Decimal("4"))


class ChoiceGroupTests(TestCase):
    def test_groups_come_out_in_group_order(self):
        recipe = make_recipe()
        for group in (5, 1, 3):
            RecipeIngredient.objects.create(
                recipe=recipe, stock_type=make_stock_type(), quantity=Decimal("1"), group=group
            )
        self.assertEqual([g[0].group for g in recipe.choice_groups()], [1, 3, 5])

    def test_group_numbers_need_not_be_contiguous(self):
        recipe = build_recipe([])
        for group in (0, 0, 99, 99):
            RecipeIngredient.objects.create(
                recipe=recipe, stock_type=make_stock_type(), quantity=Decimal("1"), group=group
            )
        self.assertEqual(recipe.variation_count, 4)
        self.assertEqual(len(recipe.choice_groups()), 2)

    def test_passing_ingredients_in_avoids_a_query(self):
        recipe = build_recipe([["1", "2"], ["3"]])
        ingredients = list(recipe.ingredients.select_related("stock_type", "sub_recipe"))
        with self.assertNumQueries(0):
            groups = recipe.choice_groups(ingredients)
        self.assertEqual([len(g) for g in groups], [2, 1])

    def test_math_prod_of_group_sizes_is_the_variation_count(self):
        recipe = build_recipe([["1", "2"], ["3", "4", "5"], ["6"]])
        self.assertEqual(
            recipe.variation_count, math.prod(len(g) for g in recipe.choice_groups())
        )
