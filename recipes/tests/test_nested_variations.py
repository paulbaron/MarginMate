"""Tests for "OU" between recipes that themselves use "OU".

A choice group's options no longer each mean one thing. An option can be a
sub-recipe with alternatives of its own, so picking it isn't one choice but
several - and a group offers not `len(options)` ways to satisfy it but the
SUM of what each option offers. Everything else (counts, cost ranges,
selection) follows from that.

The counting stays linear in the number of ingredients: a group's size is a
sum, the recipe's count is a product, and neither enumerates anything. That
matters here more than anywhere, because nesting is what makes the numbers
explode - two nested layers of ten either/ors is 10^20 variations.
"""

from decimal import Decimal

from django.test import TestCase

from recipes.models import RecipeIngredient
from tests.factories import make_priced_stock_type, make_recipe


def ingredient(recipe, group=0, quantity="1", stock_cost=None, sub_recipe=None):
    return RecipeIngredient.objects.create(
        recipe=recipe,
        group=group,
        quantity=Decimal(quantity),
        stock_type=(
            make_priced_stock_type(unit_cost_ht=stock_cost, quantity="1000")
            if stock_cost is not None
            else None
        ),
        sub_recipe=sub_recipe,
    )


class NestedCountingTests(TestCase):
    def setUp(self):
        # A syrup that can be made two ways: sugar (2) or honey (5).
        self.syrup = make_recipe(name="Sirop maison", yield_quantity="1")
        ingredient(self.syrup, group=0, stock_cost="2")
        ingredient(self.syrup, group=0, stock_cost="5")

    def test_the_sub_recipe_has_its_own_variations(self):
        self.assertEqual(self.syrup.variation_count, 2)

    def test_a_group_offers_the_sum_of_what_its_options_offer(self):
        """"Vodka OU <syrup>" is three ways, not two: vodka, syrup-with-sugar
        and syrup-with-honey."""
        cocktail = make_recipe(name="Cocktail")
        ingredient(cocktail, group=0, stock_cost="10")
        ingredient(cocktail, group=0, sub_recipe=self.syrup)
        self.assertEqual(cocktail.variation_count, 3)

    def test_a_fixed_sub_recipe_ingredient_still_multiplies(self):
        """Not a choice in the parent at all, but the syrup's own choice is
        still a choice, so the cocktail has two variations."""
        cocktail = make_recipe(name="Cocktail")
        ingredient(cocktail, group=0, stock_cost="10")
        ingredient(cocktail, group=1, sub_recipe=self.syrup)
        self.assertEqual(cocktail.variation_count, 2)

    def test_counts_multiply_across_groups(self):
        cocktail = make_recipe(name="Cocktail")
        ingredient(cocktail, group=0, stock_cost="10")
        ingredient(cocktail, group=0, sub_recipe=self.syrup)   # 1 + 2 = 3
        ingredient(cocktail, group=1, stock_cost="1")
        ingredient(cocktail, group=1, stock_cost="2")          # 2
        self.assertEqual(cocktail.variation_count, 6)

    def test_three_levels_deep(self):
        base = make_recipe(name="Base")
        ingredient(base, group=0, stock_cost="1")
        ingredient(base, group=0, stock_cost="2")              # 2
        middle = make_recipe(name="Milieu")
        ingredient(middle, group=0, sub_recipe=base)
        ingredient(middle, group=0, stock_cost="3")            # 2 + 1 = 3
        top = make_recipe(name="Haut")
        ingredient(top, group=0, sub_recipe=middle)
        ingredient(top, group=0, stock_cost="4")               # 3 + 1 = 4
        self.assertEqual(top.variation_count, 4)

    def test_the_enumerated_variations_match_the_count(self):
        cocktail = make_recipe(name="Cocktail")
        ingredient(cocktail, group=0, stock_cost="10")
        ingredient(cocktail, group=0, sub_recipe=self.syrup)
        self.assertEqual(len(cocktail.variations()), cocktail.variation_count)

    def test_a_cycle_still_terminates(self):
        a = make_recipe(name="A")
        b = make_recipe(name="B")
        ingredient(a, group=0, sub_recipe=b)
        ingredient(b, group=0, sub_recipe=a)
        self.assertGreaterEqual(a.variation_count, 1)  # must return, not hang


class NestedCostTests(TestCase):
    def setUp(self):
        self.syrup = make_recipe(name="Sirop maison", yield_quantity="1")
        ingredient(self.syrup, group=0, stock_cost="2")
        ingredient(self.syrup, group=0, stock_cost="5")

    def test_the_sub_recipe_contributes_a_range_not_a_number(self):
        cocktail = make_recipe(name="Cocktail", selling_price_ttc="12")
        ingredient(cocktail, group=0, sub_recipe=self.syrup, quantity="2")
        # 2 units of a syrup costing 2 to 5 per unit.
        self.assertEqual(cocktail.summary()["cost_range"], (Decimal("4"), Decimal("10")))

    def test_the_parent_range_spans_the_option_and_the_sub_recipe(self):
        cocktail = make_recipe(name="Cocktail", selling_price_ttc="12")
        ingredient(cocktail, group=0, stock_cost="7")           # exactly 7
        ingredient(cocktail, group=0, sub_recipe=self.syrup)    # 2 to 5
        self.assertEqual(cocktail.summary()["cost_range"], (Decimal("2"), Decimal("7")))

    def test_the_range_matches_enumerating_every_variation(self):
        """The analytic bounds have to agree with brute force, which is the
        definition of what a variation costs."""
        cocktail = make_recipe(name="Cocktail", selling_price_ttc="12")
        ingredient(cocktail, group=0, stock_cost="7")
        ingredient(cocktail, group=0, sub_recipe=self.syrup)
        ingredient(cocktail, group=1, stock_cost="1")
        ingredient(cocktail, group=1, stock_cost="3")

        costs = [v["cost_ht"] for v in cocktail.variations()]
        self.assertEqual(cocktail.summary()["cost_range"], (min(costs), max(costs)))

    def test_a_batch_sub_recipe_range_is_divided_by_its_yield(self):
        batch = make_recipe(name="Batch", yield_quantity="10")
        ingredient(batch, group=0, stock_cost="20")
        ingredient(batch, group=0, stock_cost="50")
        cocktail = make_recipe(name="Cocktail", selling_price_ttc="12")
        ingredient(cocktail, group=0, sub_recipe=batch, quantity="1")
        # 20..50 per batch of 10 => 2..5 per unit.
        self.assertEqual(cocktail.summary()["cost_range"], (Decimal("2"), Decimal("5")))


class NestedSelectionTests(TestCase):
    def setUp(self):
        self.syrup = make_recipe(name="Sirop maison", yield_quantity="1")
        self.sugar = ingredient(self.syrup, group=0, stock_cost="2")
        self.honey = ingredient(self.syrup, group=0, stock_cost="5")
        self.cocktail = make_recipe(name="Cocktail", selling_price_ttc="12")
        ingredient(self.cocktail, group=0, stock_cost="7")
        ingredient(self.cocktail, group=0, sub_recipe=self.syrup)

    def test_each_index_selects_a_distinct_variation(self):
        costs = [self.cocktail.variation_at(i)["cost_ht"] for i in range(3)]
        self.assertEqual(sorted(costs), [Decimal("2"), Decimal("5"), Decimal("7")])

    def test_the_index_addresses_the_sub_recipes_own_variations(self):
        """Index 1 and 2 are the syrup option, made two different ways."""
        self.assertEqual(self.cocktail.variation_at(1)["cost_ht"], Decimal("2"))
        self.assertEqual(self.cocktail.variation_at(2)["cost_ht"], Decimal("5"))

    def test_an_index_past_the_end_is_none(self):
        self.assertIsNone(self.cocktail.variation_at(3))

    def test_variation_for_takes_the_same_flat_index(self):
        """One number per group however deep the nesting - which is what
        keeps the detail page's ?v= links working unchanged."""
        self.assertEqual(self.cocktail.variation_for([2])["cost_ht"], Decimal("5"))

    def test_the_names_distinguish_the_sub_recipes_variants(self):
        """"Sirop maison" alone would read identically for both."""
        names = [name for _indices, name in self.cocktail.variation_selections()]
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3, f"ambiguous variation names: {names}")


class NestedScaleTests(TestCase):
    """Nesting is what makes the numbers explode, and none of it may enumerate."""

    def test_ten_by_ten_nesting_is_counted_instantly(self):
        inner = make_recipe(name="Inner")
        for _ in range(10):
            ingredient(inner, group=0, stock_cost="2")
        self.assertEqual(inner.variation_count, 10)

        outer = make_recipe(name="Outer", selling_price_ttc="50")
        for group in range(20):
            ingredient(outer, group=group, sub_recipe=inner)
        # 10 ways per group, 20 groups.
        self.assertEqual(outer.variation_count, 10**20)
        self.assertEqual(outer.summary()["cost_range"], (Decimal("40"), Decimal("40")))

    def test_the_pages_render_for_an_astronomically_nested_recipe(self):
        inner = make_recipe(name="Inner")
        ingredient(inner, group=0, stock_cost="2")
        ingredient(inner, group=0, stock_cost="3")
        outer = make_recipe(name="Outer", selling_price_ttc="50")
        for group in range(40):
            ingredient(outer, group=group, sub_recipe=inner)

        self.assertEqual(outer.variation_count, 2**40)
        self.assertEqual(self.client.get("/recipes/").status_code, 200)
        self.assertEqual(self.client.get(f"/recipes/{outer.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/recipes/{outer.pk}/edit/").status_code, 200)


class NestedFormTests(TestCase):
    """The form used to refuse a sub-recipe that had alternatives."""

    def test_a_recipe_with_variations_is_offered_as_an_ingredient(self):
        from recipes.forms import ingredient_source_choices

        syrup = make_recipe(name="Sirop maison")
        ingredient(syrup, group=0, stock_cost="2")
        ingredient(syrup, group=0, stock_cost="5")
        self.assertTrue(syrup.has_variations)

        labels = str(ingredient_source_choices())
        self.assertIn("Sirop maison", labels)

    def test_a_recipe_is_never_offered_as_its_own_ingredient(self):
        from recipes.forms import ingredient_source_choices

        recipe = make_recipe(name="Lui-même")
        self.assertNotIn("Lui-même", str(ingredient_source_choices(parent_recipe=recipe)))


class NestedBruteForceTests(TestCase):
    """The analytic summary against enumeration, with nesting in the mix.

    test_variations.py already sweeps flat shapes exhaustively. Nesting adds
    the case that broke: an option that spans a cost RANGE rather than
    sitting at one price, including a range whose bottom is zero.
    """

    def brute(self, recipe):
        variations = recipe.variations()

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

    def build(self, sub_costs, own_costs, **kwargs):
        # Names are auto-generated: subtests share one transaction, so fixed
        # names collide with the recipe the previous iteration made.
        kwargs.setdefault("selling_price_ttc", "12")
        sub = make_recipe(yield_quantity="1")
        for cost in sub_costs:
            ingredient(sub, group=0, stock_cost=cost)
        recipe = make_recipe(**kwargs)
        ingredient(recipe, group=0, sub_recipe=sub)
        for cost in own_costs:
            ingredient(recipe, group=0, stock_cost=cost)
        return recipe

    def test_matches_brute_force_across_nested_shapes(self):
        shapes = [
            (["2"], [], "one way, no alternative"),
            (["2", "5"], [], "a nested choice on its own"),
            (["2", "5"], ["7"], "nested choice beside a plain option"),
            (["2", "5"], ["1"], "the plain option is the cheapest"),
            (["0", "5"], [], "a nested option that can be free"),
            (["0", "0"], [], "every nested option is free"),
            (["0", "5"], ["3"], "free nested option beside a priced one"),
            (["2", "2"], ["2"], "everything the same price"),
            (["1", "2", "3"], ["4", "5"], "several of each"),
        ]
        for sub_costs, own_costs, label in shapes:
            with self.subTest(shape=label):
                recipe = self.build(sub_costs, own_costs)
                self.assertEqual(recipe.summary(), self.brute(recipe))

    def test_a_nested_option_that_can_be_free_still_has_a_factor(self):
        """The regression the bounds change fixed. A single option ranging
        from 0 to 5 used to report no factor at all, even though the
        5-costing variation has a perfectly good one."""
        recipe = self.build(["0", "5"], [])
        self.assertEqual(recipe.summary()["cost_range"], (Decimal("0"), Decimal("5")))
        self.assertIsNotNone(recipe.summary()["price_factor_range"])
        self.assertEqual(recipe.summary()["price_factor_range"], self.brute(recipe)["price_factor_range"])

    def test_matches_brute_force_without_a_selling_price(self):
        for sub_costs in (["2", "5"], ["0", "5"]):
            with self.subTest(sub_costs=sub_costs):
                recipe = self.build(sub_costs, [], selling_price_ttc="0")
                self.assertEqual(recipe.summary(), self.brute(recipe))


class NestedQueryCountTests(TestCase):
    """Nesting makes "how many variations does this option have?" a question
    asked over and over - once per variation built, per name resolved, per
    cost bound taken. Each one was a query: 55 to render one detail page with
    three variations. variation_scope() memoises them for the page."""

    def setUp(self):
        syrup = make_recipe(name="Sirop")
        ingredient(syrup, group=0, stock_cost="2")
        ingredient(syrup, group=0, stock_cost="5")
        self.recipe = make_recipe(name="Cocktail", selling_price_ttc="12")
        for group in range(4):
            ingredient(self.recipe, group=group, stock_cost="1")
        ingredient(self.recipe, group=0, sub_recipe=syrup)

    def test_the_detail_page_stays_cheap(self):
        with self.assertNumQueries(7):
            self.assertEqual(self.client.get(f"/recipes/{self.recipe.pk}/").status_code, 200)

    def test_the_list_page_stays_cheap(self):
        with self.assertNumQueries(8):
            self.assertEqual(self.client.get("/recipes/").status_code, 200)

    def test_the_scope_never_serves_a_stale_grouping(self):
        """The cache lasts exactly as long as the call that opened it - an
        instance-level cache would go stale the moment someone presses "OU"."""
        from recipes.models import variation_scope

        with variation_scope():
            before = self.recipe.variation_count
        RecipeIngredient.objects.filter(recipe=self.recipe, group=3).update(group=0)
        with variation_scope():
            after = self.recipe.variation_count
        self.assertNotEqual(before, after)


class VariationScopeSafetyTests(TestCase):
    """The scope cache is keyed by primary key, which needs care."""

    def test_an_unsaved_recipe_reports_no_variations_instead_of_raising(self):
        """An unsaved Recipe() is what the "new recipe" page builds, and
        Django refuses the reverse relation on one outright. Asking it for
        its variations must answer "none", not 500 the page - and two of
        them must not share a cache entry under the pk they both lack."""
        from recipes.models import Recipe, variation_scope

        saved = make_recipe(name="Réelle")
        ingredient(saved, group=0, stock_cost="3")

        with variation_scope():
            self.assertEqual(Recipe().variation_count, 0)
            self.assertEqual(saved.variation_count, 1)
            self.assertEqual(Recipe().variation_count, 0)
            self.assertEqual(Recipe().summary()["cost_range"], None)
            self.assertEqual(Recipe().variations(), [])
            self.assertIsNone(Recipe().variation_at(0))

    def test_the_new_recipe_page_renders(self):
        self.assertEqual(self.client.get("/recipes/new/").status_code, 200)

    def test_the_scope_is_cleaned_up_after_an_error(self):
        """A leaked cache would serve stale groups to every later request on
        this thread."""
        from recipes.models import _variation_counts, variation_scope

        with self.assertRaises(ValueError), variation_scope():
            raise ValueError("boom")
        self.assertIsNone(getattr(_variation_counts, "cache", None))

    def test_nested_scopes_reuse_the_outer_one(self):
        from recipes.models import _variation_counts, variation_scope

        with variation_scope() as outer, variation_scope() as inner:
            self.assertIs(inner, outer)
        self.assertIsNone(getattr(_variation_counts, "cache", None))
