"""« Si le sucre augmente, qu'est-ce que je dois reprendre ? » — the recipes
that use one article, on the « Recettes » tab.

What this promises:

* an article an ingredient names directly is used by that recipe;
* an article reached through a sub-recipe counts, however deep, and the row
  says through which sub-recipe it was reached;
* an article that is one option of an « OU » group only MAY be used, and so
  is one reached through a sub-recipe that has choices of its own: a
  shopping decision taken on a maybe would be wrong;
* the picker offers the articles at least one recipe uses, with the count of
  recipes beside each;
* `?article=` that is not an id, or an article nothing uses, is the whole
  list with a sentence saying so — never a 500, never an empty page;
* the filter travels with the tab's own link;
* a cycle between two recipes does not hang;
* the work does not grow with the number of recipes on the page.

Every name and figure invented.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from inventory.models import StockType
from recipes.models import Recipe, RecipeIngredient, variation_scope
from recipes.usage import Use, article_uses
from tests.factories import make_ingredient, make_recipe, make_stock_type

LIST_URL = reverse("recipes:recipe_list")


def uses_of(recipes=None):
    """`article_uses` over every recipe, the way the page asks it."""
    return article_uses(list(recipes if recipes is not None else Recipe.objects.all()))


class ArticleUsesTests(TestCase):
    """The pure question, with no page around it."""

    def test_a_direct_ingredient_is_a_use_and_is_certain(self):
        sugar = make_stock_type(name="Sucre semoule")
        recipe = make_recipe(name="Citronnade")
        make_ingredient(recipe, stock_type=sugar, quantity="0.05")

        uses = uses_of()

        self.assertEqual(uses[sugar.pk], {recipe.pk: [Use(via="", certain=True)]})

    def test_an_article_reached_through_a_sub_recipe_says_through_which(self):
        sugar = make_stock_type(name="Sucre semoule")
        syrup = make_recipe(name="Sirop maison")
        make_ingredient(syrup, stock_type=sugar, quantity="1")
        cocktail = make_recipe(name="Mojito maison")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02")

        uses = uses_of()

        # Both the cocktail and the syrup itself use the sugar, and the
        # cocktail's row says which syrup carried it there.
        self.assertEqual(uses[sugar.pk][syrup.pk], [Use(via="", certain=True)])
        self.assertEqual(uses[sugar.pk][cocktail.pk], [Use(via="Sirop maison", certain=True)])

    def test_two_levels_of_sub_recipe_still_count(self):
        sugar = make_stock_type(name="Sucre semoule")
        base = make_recipe(name="Sirop de base")
        make_ingredient(base, stock_type=sugar, quantity="1")
        syrup = make_recipe(name="Sirop maison")
        make_ingredient(syrup, sub_recipe=base, quantity="0.5")
        cocktail = make_recipe(name="Mojito maison")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02")

        uses = uses_of()

        self.assertIn(cocktail.pk, uses[sugar.pk])
        # Named by the sub-recipe the recipe itself lists: that is the row
        # the reader opens next, and « Sirop de base » is inside it.
        self.assertEqual(uses[sugar.pk][cocktail.pk], [Use(via="Sirop maison", certain=True)])

    def test_an_or_group_is_only_a_maybe(self):
        vodka = make_stock_type(name="Vodka maison")
        gin = make_stock_type(name="Gin maison")
        mule = make_recipe(name="Mule du comptoir")
        make_ingredient(mule, stock_type=vodka, quantity="0.04", group=1)
        make_ingredient(mule, stock_type=gin, quantity="0.04", group=1)

        uses = uses_of()

        self.assertEqual(uses[vodka.pk][mule.pk], [Use(via="", certain=False)])
        self.assertEqual(uses[gin.pk][mule.pk], [Use(via="", certain=False)])

    def test_a_choice_inside_a_sub_recipe_is_only_a_maybe_too(self):
        sugar = make_stock_type(name="Sucre semoule")
        honey = make_stock_type(name="Miel de printemps")
        syrup = make_recipe(name="Sirop maison")
        make_ingredient(syrup, stock_type=sugar, quantity="1", group=1)
        make_ingredient(syrup, stock_type=honey, quantity="1", group=1)
        cocktail = make_recipe(name="Mojito maison")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02")

        uses = uses_of()

        self.assertEqual(uses[sugar.pk][cocktail.pk], [Use(via="Sirop maison", certain=False)])

    def test_the_same_article_twice_is_one_recipe_said_once(self):
        sugar = make_stock_type(name="Sucre semoule")
        recipe = make_recipe(name="Citronnade")
        make_ingredient(recipe, stock_type=sugar, quantity="0.05", group=0)
        make_ingredient(recipe, stock_type=sugar, quantity="0.01", group=2)

        uses = uses_of()

        self.assertEqual(list(uses[sugar.pk]), [recipe.pk])
        self.assertEqual(uses[sugar.pk][recipe.pk], [Use(via="", certain=True)])

    def test_an_article_no_recipe_uses_is_not_in_the_answer(self):
        make_stock_type(name="Tonic oublié")
        sugar = make_stock_type(name="Sucre semoule")
        recipe = make_recipe(name="Citronnade")
        make_ingredient(recipe, stock_type=sugar, quantity="0.05")

        self.assertEqual(set(uses_of()), {sugar.pk})

    def test_a_cycle_between_two_recipes_does_not_hang(self):
        sugar, first, second = self._cycle()

        uses = uses_of()

        # Both are listed, and the walk came back.
        self.assertEqual(set(uses[sugar.pk]), {first.pk, second.pk})

    def test_the_page_draws_a_cycle(self):
        sugar, first, second = self._cycle()

        response = self.client.get(LIST_URL, {"article": sugar.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual({recipe.pk for recipe in response.context["recipes"]}, {first.pk, second.pk})

    @staticmethod
    def _cycle():
        """A uses B, B uses A - bad data the pages must survive rather than
        hang on. Each ingredient is alone in its group, so the certainty
        question walks the cycle too, not only the reachability one."""
        sugar = make_stock_type(name="Sucre semoule")
        first = make_recipe(name="Recette A")
        second = make_recipe(name="Recette B")
        make_ingredient(first, stock_type=sugar, quantity="1", group=0)
        make_ingredient(first, sub_recipe=second, quantity="1", group=1)
        make_ingredient(second, sub_recipe=first, quantity="1", group=0)
        return sugar, first, second


class ArticlePickerPageTests(TestCase):
    """The « Recettes » tab itself."""

    def setUp(self):
        self.sugar = make_stock_type(name="Sucre semoule")
        self.mint = make_stock_type(name="Menthe fraîche")
        self.syrup = make_recipe(name="Sirop maison")
        make_ingredient(self.syrup, stock_type=self.sugar, quantity="1")
        self.cocktail = make_recipe(name="Mojito maison")
        # Two groups, not one: ingredients sharing a group are alternatives
        # to each other, and « sirop OU menthe » is a different recipe.
        make_ingredient(self.cocktail, sub_recipe=self.syrup, quantity="0.02", group=0)
        make_ingredient(self.cocktail, stock_type=self.mint, quantity="0.01", group=1)
        self.other = make_recipe(name="Bière pression")

    def test_the_picker_lists_the_articles_used_with_their_counts(self):
        response = self.client.get(LIST_URL)

        offered = {entry["article"].pk: entry["count"] for entry in response.context["article_choices"]}
        self.assertEqual(offered, {self.sugar.pk: 2, self.mint.pk: 1})
        self.assertContains(response, "Sucre semoule")
        # Not « date-range »: that class means « du … au … » on every page,
        # and a second form wearing it is how a test finds the wrong one.
        self.assertContains(response, 'class="filter-row"')
        self.assertNotContains(response, 'class="date-range"')

    def test_an_article_filters_the_list(self):
        response = self.client.get(LIST_URL, {"article": self.sugar.pk})

        listed = {recipe.name for recipe in response.context["recipes"]}
        self.assertEqual(listed, {"Sirop maison", "Mojito maison"})
        self.assertNotContains(response, "Bière pression")

    def test_the_wording_carries_the_count_and_the_name(self):
        response = self.client.get(LIST_URL, {"article": self.sugar.pk})

        self.assertContains(response, "2 recettes utilisent")
        self.assertContains(response, "Sucre semoule")

    def test_one_recipe_is_said_in_the_singular(self):
        response = self.client.get(LIST_URL, {"article": self.mint.pk})

        self.assertContains(response, "1 recette utilise")

    def test_the_row_says_through_which_sub_recipe(self):
        response = self.client.get(LIST_URL, {"article": self.sugar.pk})

        self.assertContains(response, "Sirop maison")
        self.assertContains(response, "Via")

    def test_a_maybe_is_said_to_be_a_maybe(self):
        gin = make_stock_type(name="Gin maison")
        vodka = make_stock_type(name="Vodka maison")
        mule = make_recipe(name="Mule du comptoir")
        make_ingredient(mule, stock_type=gin, quantity="0.04", group=1)
        make_ingredient(mule, stock_type=vodka, quantity="0.04", group=1)

        response = self.client.get(LIST_URL, {"article": gin.pk})

        self.assertEqual([recipe.name for recipe in response.context["recipes"]], ["Mule du comptoir"])
        self.assertFalse(response.context["recipes"][0].article_certain)
        self.assertContains(response, "peut")

    def test_the_all_maybe_sentence_does_not_say_where_the_choice_is(self):
        """A cocktail whose only ingredient is a « sucre OU miel » syrup
        offers no alternative of its own: the choice is inside its syrup, and
        the « Utilisé » column is what says so. « chacune en alternative
        "OU" » named it on the recipe itself, which is false in that shape -
        and that shape is the one the cautious rule was written for.
        """
        honey = make_stock_type(name="Miel de fleurs")
        sugar = make_stock_type(name="Sucre roux")
        syrup = make_recipe(name="Sirop inventé")
        make_ingredient(syrup, stock_type=sugar, quantity="1", group=0)
        make_ingredient(syrup, stock_type=honey, quantity="1", group=0)
        cocktail = make_recipe(name="Punch inventé")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02", group=0)

        response = self.client.get(LIST_URL, {"article": sugar.pk})

        self.assertEqual(response.context["article_count"], response.context["article_maybe_count"])
        self.assertContains(response, "par un choix « OU » quelque part")
        self.assertNotContains(response, "chacune en alternative")

    def test_some_certain_and_some_only_maybe_is_said_as_both(self):
        vermouth = make_stock_type(name="Vermouth maison")
        sure = make_recipe(name="Spritz du comptoir")
        make_ingredient(sure, stock_type=vermouth, quantity="0.06")
        perhaps = make_recipe(name="Américain du comptoir")
        make_ingredient(perhaps, stock_type=vermouth, quantity="0.06", group=1)
        make_ingredient(perhaps, stock_type=make_stock_type(name="Vin cuit maison"), quantity="0.06", group=1)

        response = self.client.get(LIST_URL, {"article": vermouth.pk})

        self.assertEqual(response.context["article_count"], 2)
        self.assertEqual(response.context["article_maybe_count"], 1)
        self.assertContains(response, "2 recettes utilisent")
        self.assertContains(response, "Dont 1 qui peut seulement l'utiliser")

    def test_no_recipe_uses_anything_offers_no_picker(self):
        RecipeIngredient.objects.all().delete()

        response = self.client.get(LIST_URL)

        self.assertEqual(response.context["article_choices"], [])
        self.assertNotContains(response, "Tous les articles")

    def test_the_way_back_is_offered(self):
        response = self.client.get(LIST_URL, {"article": self.sugar.pk})

        self.assertEqual(response.context["all_recipes_url"], LIST_URL)
        self.assertContains(response, "Toutes les recettes")

    def test_a_garbled_article_is_the_whole_list(self):
        for garbage in ("", "abc", "²", "-3", "9" * 25, "1.5"):
            with self.subTest(garbage=garbage):
                response = self.client.get(LIST_URL, {"article": garbage})
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["chosen_article"])
                self.assertEqual(len(response.context["recipes"]), 3)

    def test_an_article_nothing_uses_is_the_whole_list_and_says_so(self):
        forgotten = make_stock_type(name="Tonic oublié")

        response = self.client.get(LIST_URL, {"article": forgotten.pk})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["chosen_article"])
        self.assertEqual(len(response.context["recipes"]), 3)
        self.assertEqual(response.context["unused_article"], forgotten)
        self.assertContains(response, "Tonic oublié")

    def test_an_article_that_does_not_exist_is_the_whole_list(self):
        missing = StockType.objects.order_by("-pk").first().pk + 100

        response = self.client.get(LIST_URL, {"article": missing})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["chosen_article"])
        self.assertIsNone(response.context["unused_article"])
        self.assertEqual(len(response.context["recipes"]), 3)

    def test_the_filter_travels_with_the_tab_link(self):
        response = self.client.get(LIST_URL, {"article": self.sugar.pk})

        tabs = {entry["key"]: entry["url"] for entry in response.context["tabs"]}
        self.assertEqual(tabs["recipes"], f"{LIST_URL}?article={self.sugar.pk}")
        self.assertContains(response, f'href="{LIST_URL}?article={self.sugar.pk}"')

    def test_the_other_tabs_drop_it_because_it_means_nothing_there(self):
        """« À lier » is till products and « Ventes » is sales: neither has
        an article to be filtered on. Carried there, the parameter would
        come back on the « Recettes » link and reappear as a filter the
        reader had left behind."""
        response = self.client.get(LIST_URL, {"article": self.sugar.pk})

        tabs = {entry["key"]: entry["url"] for entry in response.context["tabs"]}
        self.assertNotIn("article", tabs["to-link"])
        self.assertNotIn("article", tabs["sales"])

    def test_a_garbled_filter_never_travels(self):
        response = self.client.get(LIST_URL, {"article": "abc"})

        tabs = {entry["key"]: entry["url"] for entry in response.context["tabs"]}
        self.assertEqual(tabs["recipes"], LIST_URL)


class ArticlePickerCostTests(TestCase):
    """The page must not read the database once per recipe."""

    def _build(self, how_many):
        """`how_many` cocktails, all reaching the sugar through one shared
        house syrup, each with an article of its own beside it."""
        sugar = make_stock_type(name=f"Sucre {how_many}")
        syrup = make_recipe(name=f"Sirop {how_many}")
        make_ingredient(syrup, stock_type=sugar, quantity="1")
        for index in range(how_many):
            cocktail = make_recipe(name=f"Cocktail {how_many}-{index}")
            make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02", group=0)
            own = make_stock_type(name=f"Article {how_many}-{index}")
            make_ingredient(cocktail, stock_type=own, quantity="1", group=1)
        return sugar

    def _clear(self):
        RecipeIngredient.objects.all().delete()
        Recipe.objects.all().delete()
        StockType.objects.all().delete()

    def _queries_for_the_page(self):
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(LIST_URL).status_code, 200)
        return len(captured)

    def test_three_recipes_and_thirty_cost_the_same(self):
        self._build(3)
        small = self._queries_for_the_page()
        self._clear()
        self._build(30)
        self.assertEqual(self._queries_for_the_page(), small)

    def test_one_syrup_in_thirty_cocktails_is_walked_once(self):
        sugar = self._build(30)
        recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type", "ingredients__sub_recipe"))
        with CaptureQueriesContext(connection) as captured, variation_scope():
            uses = article_uses(recipes)

        self.assertEqual(len(uses[sugar.pk]), 31)
        # The syrup is read for its own ingredients and for whether it has
        # choices of its own, and that is all. A walk per cocktail would be
        # thirty times this.
        self.assertLess(len(captured), 6)


class NestedGraphQueryTests(TestCase):
    """The walks below the tab's recipes read the ingredients the tab
    prefetched: a sub-recipe costs no query at any depth (walked from the
    database, ten syrups of three levels under sixty cocktails were a
    hundred queries) - and reaches exactly what the database walk reaches."""

    def _build(self, syrups):
        """`syrups` syrups of three levels (syrup -> base -> extract ->
        article), each in two cocktails; a cycle between two of them."""
        for n in range(syrups):
            article = make_stock_type(name=f"Article {n}")
            extract = make_recipe(name=f"Extrait {n}")
            make_ingredient(extract, stock_type=article, quantity="1")
            base = make_recipe(name=f"Base {n}")
            make_ingredient(base, sub_recipe=extract, quantity="1")
            make_ingredient(base, stock_type=make_stock_type(name=f"Sucre {n}"), quantity="1")
            syrup = make_recipe(name=f"Sirop {n}")
            make_ingredient(syrup, sub_recipe=base, quantity="1")
            for k in range(2):
                cocktail = make_recipe(name=f"Cocktail {n}-{k}")
                make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02")
        first, second = make_recipe(name="Cycle A"), make_recipe(name="Cycle B")
        make_ingredient(first, sub_recipe=second, quantity="1")
        make_ingredient(second, sub_recipe=first, quantity="1", group=0)
        make_ingredient(second, stock_type=make_stock_type(name="Article du cycle"), quantity="1")

    def _walk(self):
        recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type", "ingredients__sub_recipe"))
        with variation_scope():
            # Every recipe's groups in the scope, as the page puts them there
            # (menu._recipes): whether a sub-recipe has choices costs nothing.
            Recipe.load_choice_groups(recipes)
            with CaptureQueriesContext(connection) as captured:
                uses = article_uses(recipes)
        return uses, len(captured)

    def test_depth_and_breadth_cost_no_more_queries(self):
        self._build(2)
        _, small = self._walk()
        RecipeIngredient.objects.all().delete()
        Recipe.objects.all().delete()
        StockType.objects.all().delete()
        self._build(10)
        _, large = self._walk()
        self.assertEqual(large, small)

    def test_the_handed_ingredients_reach_what_the_database_walk_reaches(self):
        from inventory.variance import ingredients_by_recipe, reachable_stock_types

        self._build(3)
        recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type", "ingredients__sub_recipe"))
        handed = ingredients_by_recipe(recipes)
        ingredients = RecipeIngredient.objects.filter(sub_recipe__isnull=False).select_related("sub_recipe")
        self.assertGreater(len(ingredients), 10)
        for ingredient in ingredients:
            with self.subTest(ingredient=str(ingredient)):
                self.assertEqual(
                    reachable_stock_types(ingredient, ingredients_of=handed), reachable_stock_types(ingredient)
                )
