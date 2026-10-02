"""The « Recettes » tab costs every recipe from one read of them all.

* the figures are those the tab gave when each sub-recipe was read by a
  query of its own - nested sub-recipes, « OU » at every level, a batch
  sold by the portion, a cycle - and, outside the cycle, those each recipe
  gives on its own;
* a sub-recipe's own groups come from that read: more sub-recipes cost no
  query of their own beyond the article picker's walk (recipes/usage.py),
  and more purchases behind an article cost none at all.

Every name and figure invented.
"""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from inventory.models import StockType
from recipes.models import Recipe, RecipeIngredient, variation_scope
from tests.factories import make_ingredient, make_movement, make_priced_stock_type, make_recipe

LIST_URL = reverse("recipes:recipe_list")


def priced(name, *purchases):
    """An article bought several times, at several prices."""
    stock_type = make_priced_stock_type(name=name, unit_cost_ht=purchases[0][0], quantity=purchases[0][1])
    for unit_cost_ht, quantity in purchases[1:]:
        make_movement(stock_type=stock_type, quantity=quantity, unit_cost_ht=unit_cost_ht)
    return stock_type


class RecipeListFiguresTests(TestCase):
    def setUp(self):
        sugar = priced("Sucre", ("1.2183", "6.000"), ("1.31", "2.500"))
        honey = priced("Miel", ("7.45", "1.000"))
        mint = priced("Menthe", ("0.0333", "30"), ("0.05", "12"))
        rum = priced("Rhum", ("11.10", "0.700"), ("12.35", "1.400"))
        lime = priced("Citron vert", ("2.99", "1.000"))
        syrup = make_recipe(name="Sirop maison", selling_price_ttc=None, yield_quantity="10")
        make_ingredient(syrup, stock_type=sugar, quantity="1", group=0)
        make_ingredient(syrup, stock_type=honey, quantity="0.8", group=0)
        cordial = make_recipe(name="Cordial", selling_price_ttc=None, yield_quantity="2")
        make_ingredient(cordial, sub_recipe=syrup, quantity="0.5", group=0)
        make_ingredient(cordial, stock_type=mint, quantity="12", group=1)
        mojito = make_recipe(name="Mojito", selling_price_ttc="9.50", yield_quantity="4", sale_quantity="1")
        make_ingredient(mojito, stock_type=rum, quantity="0.2", group=0)
        make_ingredient(mojito, sub_recipe=cordial, quantity="0.1", group=1)
        make_ingredient(mojito, sub_recipe=syrup, quantity="0.08", group=1)
        make_ingredient(mojito, stock_type=lime, quantity="0.25", group=2)
        portion = make_recipe(name="Planche", selling_price_ttc="14", yield_quantity="1.6", sale_quantity="0.15")
        make_ingredient(portion, sub_recipe=mojito, quantity="1")
        # A cycle the form refuses and the admin does not.
        loop_a = make_recipe(name="Boucle A")
        loop_b = make_recipe(name="Boucle B")
        make_ingredient(loop_a, sub_recipe=loop_b, quantity="1")
        make_ingredient(loop_b, sub_recipe=loop_a, quantity="1", group=0)
        make_ingredient(loop_b, stock_type=lime, quantity="1", group=0)

    def shown(self):
        return {recipe.pk: recipe.summary_data for recipe in self.client.get(LIST_URL).context["recipes"]}

    def test_the_figures_the_tab_worked_out_before(self):
        """Every recipe costed in one scope, each sub-recipe read by its own
        query - the tab as it was. A cycle's figures depend on which of its
        recipes the scope met first, so the order is the tab's, kept."""
        recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type__movements", "ingredients__sub_recipe"))
        with variation_scope():
            before = {recipe.pk: recipe.summary(list(recipe.ingredients.all())) for recipe in recipes}
        self.assertEqual(self.shown(), before)

    def test_each_recipe_as_it_costs_on_its_own(self):
        shown = self.shown()
        for recipe in Recipe.objects.exclude(name__startswith="Boucle"):
            with self.subTest(recipe=recipe.name):
                self.assertEqual(shown[recipe.pk], Recipe.objects.get(pk=recipe.pk).summary())
        self.assertGreater(shown[Recipe.objects.get(name="Mojito").pk]["variation_count"], 2)


class RecipeListCostTests(TestCase):
    def _build(self, syrups, purchases=1):
        """`syrups` house syrups - « sucre OU miel » - each in two cocktails;
        `purchases` movements behind every article."""
        for index in range(syrups):
            syrup = make_recipe(name=f"Sirop {index}", selling_price_ttc=None)
            for article in ("Sucre", "Miel"):
                stock_type = priced(f"{article} {index}", *[("1.50", "2")] * purchases)
                make_ingredient(syrup, stock_type=stock_type, group=0)
            for cocktail in range(2):
                recipe = make_recipe(name=f"Cocktail {index}-{cocktail}")
                make_ingredient(recipe, sub_recipe=syrup, quantity="0.02", group=0)
                make_ingredient(recipe, stock_type=priced(f"Alcool {index}-{cocktail}", ("9", "1")), group=1)

    def _clear(self):
        RecipeIngredient.objects.all().delete()
        Recipe.objects.all().delete()
        StockType.objects.all().delete()

    def _queries(self):
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(LIST_URL).status_code, 200)
        return len(captured)

    def test_more_sub_recipes_add_no_read_but_the_article_walk(self):
        self._build(1)
        one = self._queries()
        self._clear()
        self._build(4)
        # Three more syrups: at most the picker's walk, one query each. Each
        # cost three before - its ingredients, their movements, the walk.
        self.assertLessEqual(self._queries() - one, 3)

    def test_more_purchases_cost_nothing(self):
        """Only the fields an article's cost is worked out from are read: one
        more field read off a movement would be a query per movement."""
        self._build(2, purchases=1)
        few = self._queries()
        self._clear()
        self._build(2, purchases=6)
        self.assertEqual(self._queries(), few)

    def test_the_cost_is_read_off_every_purchase(self):
        self._build(1, purchases=3)
        syrup = Recipe.objects.get(name="Sirop 0")
        shown = {recipe.pk: recipe.summary_data for recipe in self.client.get(LIST_URL).context["recipes"]}
        self.assertEqual(shown[syrup.pk]["cost_range"], (Decimal("1.50"), Decimal("1.50")))
