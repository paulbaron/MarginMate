"""`Recipe.load_choice_groups`: a recipe's sub-recipes read a level at a time.

Inside a `variation_scope`, asking a recipe for its groups reads it once and
keeps it - but costing a recipe asks every sub-recipe below it, and each was
read the moment it was first asked: two queries per sub-recipe (its
ingredients, then their articles' movements). A recipe's own page with a
dozen syrups paid that dozen times over.

`load_choice_groups` reads them together instead. The promise it has to keep
is that nothing it reads differs from what `choice_groups()` would have read
one recipe at a time - the same rows in the same order, the same articles,
movements and sub-recipes - so every figure costed from it is the same. A
cycle (A uses B uses A, which the admin lets through) is read once and costs
what it always did.

Data invented throughout.
"""

from decimal import Decimal

from django.db import connection
from django.db.models.signals import post_init
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.models import StockMovement
from recipes.models import Recipe, RecipeIngredient, variation_scope
from tests.factories import make_movement, make_priced_stock_type, make_recipe, make_stock_type


def ingredient(recipe, group=0, quantity="1", stock_cost=None, sub_recipe=None):
    return RecipeIngredient.objects.create(
        recipe=recipe,
        group=group,
        quantity=Decimal(quantity),
        stock_type=(
            make_priced_stock_type(unit_cost_ht=stock_cost, quantity="1000") if stock_cost is not None else None
        ),
        sub_recipe=sub_recipe,
    )


def shape(groups) -> list:
    """What a recipe's groups say, row by row: enough to tell two readings
    apart if one lost a row, reordered one, or priced an article otherwise."""
    return [
        [
            (
                line.pk,
                line.recipe_id,
                line.group,
                line.quantity,
                line.stock_type_id,
                line.stock_type.current_unit_cost_ht if line.stock_type_id else None,
                line.sub_recipe_id,
            )
            for line in group
        ]
        for group in groups
    ]


def read_one_at_a_time(recipes) -> dict:
    """Each recipe's groups as `choice_groups()` reads them alone - outside
    any scope, so nothing is shared between two of them."""
    return {recipe.pk: shape(Recipe.objects.get(pk=recipe.pk).choice_groups()) for recipe in recipes}


class Graph:
    """Three levels, siblings, a shared syrup, an empty preparation and a
    cycle the form would refuse:

        Cocktail ── (Rhum OU Sirop de sucre) · Citron · Base
        Base ────── Sirop de sucre · (Eau OU Infusion)
        Infusion ── Thé
        Sirop de sucre ── (Sucre OU Miel)
        Vide (no ingredient)
        Boucle A ── Boucle B ── Boucle A
    """

    @classmethod
    def setUpTestData(cls):
        cls.syrup = make_recipe(name="Sirop de sucre", yield_quantity="10")
        ingredient(cls.syrup, group=0, stock_cost="2")
        ingredient(cls.syrup, group=0, stock_cost="5", quantity="2")
        cls.infusion = make_recipe(name="Infusion", selling_price_ttc=None, yield_quantity="4")
        ingredient(cls.infusion, stock_cost="0.30", quantity="3")
        cls.base = make_recipe(name="Base")
        ingredient(cls.base, group=0, sub_recipe=cls.syrup, quantity="0.5")
        ingredient(cls.base, group=1, stock_cost="0.01")
        ingredient(cls.base, group=1, sub_recipe=cls.infusion, quantity="0.25")
        cls.cocktail = make_recipe(name="Cocktail", selling_price_ttc="9.50")
        # Inserted out of group order: the rows still come back by group, then id.
        ingredient(cls.cocktail, group=2, sub_recipe=cls.base)
        ingredient(cls.cocktail, group=0, stock_cost="18", quantity="0.04")
        ingredient(cls.cocktail, group=1, stock_cost="0.40")
        ingredient(cls.cocktail, group=0, sub_recipe=cls.syrup, quantity="0.02")
        cls.empty = make_recipe(name="Vide")
        cls.loop_a = make_recipe(name="Boucle A")
        cls.loop_b = make_recipe(name="Boucle B")
        ingredient(cls.loop_a, stock_cost="1")
        ingredient(cls.loop_a, sub_recipe=cls.loop_b, group=1)
        ingredient(cls.loop_b, stock_cost="3")
        ingredient(cls.loop_b, sub_recipe=cls.loop_a, group=1)
        cls.everything = [cls.cocktail, cls.base, cls.infusion, cls.syrup, cls.empty, cls.loop_a, cls.loop_b]


class WhatIsReadTests(Graph, TestCase):
    def test_every_recipe_below_holds_what_its_own_choice_groups_reads(self):
        expected = read_one_at_a_time(self.everything)
        with variation_scope() as scope:
            Recipe.load_choice_groups([Recipe.objects.get(pk=self.cocktail.pk), self.empty, self.loop_a])
            self.assertEqual({pk: shape(groups) for pk, groups in scope["groups"].items()}, expected)

    def test_each_ingredient_points_back_at_its_recipe_without_a_query(self):
        with variation_scope() as scope:
            Recipe.load_choice_groups([self.cocktail])
            with self.assertNumQueries(0):
                owners = {line.recipe.pk for groups in scope["groups"].values() for group in groups for line in group}
        self.assertEqual(owners, {self.cocktail.pk, self.base.pk, self.infusion.pk, self.syrup.pk})

    def test_a_cycle_is_read_once(self):
        with variation_scope() as scope, CaptureQueriesContext(connection) as read:
            Recipe.load_choice_groups([self.loop_a])
        self.assertEqual(set(scope["groups"]), {self.loop_a.pk, self.loop_b.pk})
        self.assertEqual(len(read), 4, "two levels, two queries each")

    def test_what_the_scope_holds_is_not_read_again(self):
        with variation_scope():
            Recipe.load_choice_groups([self.cocktail])
            with self.assertNumQueries(0):
                Recipe.load_choice_groups([self.cocktail, self.base, self.syrup])
                self.cocktail.choice_groups()

    def test_outside_a_scope_nothing_is_read(self):
        with self.assertNumQueries(0):
            Recipe.load_choice_groups([self.cocktail])


class SameFiguresTests(Graph, TestCase):
    """Every figure costed from what was read together is the figure costed
    from each recipe read alone."""

    def figures(self):
        return {
            recipe.pk: (recipe.summary(), recipe.variation_at(0), recipe.unit_cost_bounds())
            for recipe in Recipe.objects.filter(pk__in=[recipe.pk for recipe in self.everything]).order_by("pk")
        }

    def test_the_summaries_are_those_read_one_at_a_time(self):
        """Outside a scope every question reads afresh what it needs; in one,
        everything below is read together first, once."""
        alone = self.figures()
        with variation_scope():
            Recipe.load_choice_groups(Recipe.objects.filter(pk__in=[recipe.pk for recipe in self.everything]))
            together = self.figures()
        self.assertEqual(together, alone)

    def test_a_summary_of_ingredients_handed_in_is_the_same(self):
        """A page reading a recipe's ingredients itself (its own page) and
        handing them in: its sub-recipes are read together, and cost the
        same."""
        ingredients = list(
            self.cocktail.ingredients.select_related("stock_type", "sub_recipe")
            .prefetch_related("stock_type__movements")
            .order_by("group", "id")
        )
        expected = Recipe.objects.get(pk=self.cocktail.pk).summary()
        with variation_scope():
            self.assertEqual(self.cocktail.summary(ingredients), expected)

    def test_the_cycle_still_prices_the_recipe_on_the_stack_at_nothing(self):
        alone = Recipe.objects.get(pk=self.loop_a.pk).summary()["cost_range"]
        with variation_scope():
            Recipe.load_choice_groups([self.loop_b])
            self.assertEqual(self.loop_a.summary()["cost_range"], alone)
        # Boucle A: its article (1) and Boucle B - its article (3) and Boucle
        # A once more, whose own Boucle B is on the stack by then, so 0: 1.
        self.assertEqual(alone, (Decimal("5"), Decimal("5")))


class TheCostsNotTheMovementsTests(Graph, TestCase):
    """An article's cost is all a recipe reads of its movements: inside a
    scope they are summed as they are read, not built into a model instance
    each - thousands of them on every page costing the recipes, for one
    average per article."""

    def test_no_movement_is_built(self):
        built = []

        def count(sender, instance, **kwargs):
            built.append(instance)

        post_init.connect(count, sender=StockMovement)
        try:
            with variation_scope():
                Recipe.load_choice_groups(Recipe.objects.filter(pk__in=[recipe.pk for recipe in self.everything]))
                for recipe in self.everything:
                    recipe.summary()
        finally:
            post_init.disconnect(count, sender=StockMovement)
        self.assertEqual(built, [])

    def test_costing_what_was_read_reads_nothing_more(self):
        with variation_scope():
            Recipe.load_choice_groups([self.cocktail])
            with self.assertNumQueries(0):
                self.cocktail.summary()
                self.cocktail.unit_cost_bounds()

    def test_an_article_with_nothing_on_hand_costs_nothing(self):
        """No movement at all, or as much gone as came in: the average has
        no quantity to divide by, and is 0 as `current_unit_cost_ht` says."""
        never_bought = make_stock_type(name="Jamais acheté")
        all_gone = make_stock_type(name="Tout perdu")
        make_movement(stock_type=all_gone, quantity="2", unit_cost_ht="7")
        make_movement(stock_type=all_gone, quantity="-2", unit_cost_ht="7", kind="LOSS")
        recipe = make_recipe(name="Rien en stock")
        RecipeIngredient.objects.create(recipe=recipe, group=0, quantity=Decimal("1"), stock_type=never_bought)
        RecipeIngredient.objects.create(recipe=recipe, group=1, quantity=Decimal("1"), stock_type=all_gone)
        self.assertEqual(never_bought.current_unit_cost_ht, Decimal("0"))
        self.assertEqual(all_gone.current_unit_cost_ht, Decimal("0"))
        with variation_scope() as scope:
            Recipe.load_choice_groups([recipe])
            costs = [line.unit_cost_ht() for group in scope["groups"][recipe.pk] for line in group]
        self.assertEqual(costs, [Decimal("0"), Decimal("0")])


class LevelsNotRecipesTests(TestCase):
    """What a recipe's page costs grows with how deep its sub-recipes nest,
    never with how many there are."""

    def setUp(self):
        self.cocktail = make_recipe(name="Cocktail à sirops", selling_price_ttc="12")
        self.syrups = 0

    def add_syrups(self, count):
        for _ in range(count):
            self.syrups += 1
            syrup = make_recipe(name=f"Sirop {self.syrups}", selling_price_ttc=None, yield_quantity="5")
            ingredient(syrup, group=0, stock_cost="2")
            ingredient(syrup, group=0, stock_cost="3")
            infusion = make_recipe(name=f"Infusion {self.syrups}", selling_price_ttc=None)
            ingredient(infusion, stock_cost="0.5")
            ingredient(syrup, group=1, sub_recipe=infusion, quantity="0.1")
            ingredient(self.cocktail, group=self.syrups, sub_recipe=syrup, quantity="0.03")

    def page_queries(self, handed_in: bool) -> int:
        recipe = Recipe.objects.get(pk=self.cocktail.pk)
        ingredients = (
            list(
                recipe.ingredients.select_related("stock_type", "sub_recipe").prefetch_related("stock_type__movements")
            )
            if handed_in
            else None
        )
        with CaptureQueriesContext(connection) as read, variation_scope():
            recipe.summary(ingredients)
            recipe.variation_at(0, ingredients)
            recipe.choice_groups(ingredients)
        return len(read)

    def test_three_times_the_sub_recipes_cost_no_more_queries(self):
        for handed_in in (False, True):
            with self.subTest(handed_in=handed_in):
                self.cocktail.ingredients.all().delete()
                self.add_syrups(2)
                few = self.page_queries(handed_in)
                self.add_syrups(4)
                many = self.page_queries(handed_in)
                self.assertEqual(many, few)
