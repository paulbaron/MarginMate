"""« À lier » drawn once per request instead of once per row - and to the
character what it was.

* `RecipeSuggester` gives the answer the plain loop gave (every recipe
  measured in turn, kept below), name for name: the first exact name, else
  the first of the best ratios, kept from the threshold up - ties, the
  threshold itself, empty names and long ones included;
* a row's <option>s are what the template loop printed, names escaped;
* `url_for_each` is `reverse()`, pk for pk, under a script prefix too.

Every name invented.
"""

import difflib
import random
from datetime import date

from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse, set_script_prefix

from recipes.links import HAPPY_HOUR_RE, SUGGESTION_THRESHOLD, RecipeSuggester, plain, suggest_recipe
from recipes.menu import ToLinkRows, url_for_each
from recipes.models import PosProduct, Recipe, RecipeSale
from tests.factories import make_recipe


def loop_suggestion(name, recipes):
    """suggest_recipe as it was written - every recipe measured in turn: the
    reference the suggester is held to."""
    target = plain(name)
    happy_hour = bool(HAPPY_HOUR_RE.search(target))
    target = " ".join(HAPPY_HOUR_RE.sub(" ", target).split())
    if not target:
        return None, happy_hour
    best, best_score = None, 0.0
    for recipe in recipes:
        candidate = plain(recipe.name)
        if candidate == target:
            return recipe, happy_hour
        score = difflib.SequenceMatcher(None, target, candidate).ratio()
        if score > best_score:
            best, best_score = recipe, score
    return (best if best_score >= SUGGESTION_THRESHOLD else None), happy_hour


def recipes_named(*names):
    return [Recipe(pk=index, name=name) for index, name in enumerate(names, start=1)]


class SuggesterTests(SimpleTestCase):
    def assertSameAsTheLoop(self, name, recipes, suggest=None):
        expected = loop_suggestion(name, recipes)
        found = (suggest or RecipeSuggester(recipes))(name)
        # The very recipe, not one equal to it.
        self.assertIs(found[0], expected[0], name)
        self.assertEqual(found[1], expected[1], name)
        self.assertEqual(suggest_recipe(name, recipes)[0], expected[0])
        return found

    def test_a_tie_goes_to_the_first(self):
        recipes = recipes_named("Mojito A", "Pinte", "Mojito B")
        self.assertEqual(
            difflib.SequenceMatcher(None, "mojito c", "mojito a").ratio(),
            difflib.SequenceMatcher(None, "mojito c", "mojito b").ratio(),
        )
        self.assertIs(self.assertSameAsTheLoop("Mojito C", recipes)[0], recipes[0])
        self.assertIs(self.assertSameAsTheLoop("Mojito C", recipes[::-1])[0], recipes[2])

    def test_two_recipes_of_one_name_the_first(self):
        recipes = recipes_named("Spritz", "SPRITZ", "Spritz Apérol")
        self.assertIs(self.assertSameAsTheLoop("spritz", recipes)[0], recipes[0])

    def test_the_same_name_beats_a_close_one_listed_before_it(self):
        recipes = recipes_named("Mojitos", "Mojito")
        self.assertIs(self.assertSameAsTheLoop("Mojito", recipes)[0], recipes[1])

    def test_the_threshold_itself_is_a_suggestion(self):
        recipes = recipes_named("Abcdx", "Abxyz")
        self.assertEqual(difflib.SequenceMatcher(None, "abcde", "abcdx").ratio(), SUGGESTION_THRESHOLD)
        self.assertIs(self.assertSameAsTheLoop("Abcde", recipes)[0], recipes[0])
        self.assertIsNone(self.assertSameAsTheLoop("Abcde", recipes[1:])[0])

    def test_nothing_to_compare(self):
        recipes = recipes_named("Pinte Blonde", "!!!", "")
        for name in ("", "HH", "Happy Hour", "???", "Planche"):
            with self.subTest(name=name):
                self.assertSameAsTheLoop(name, recipes)
        self.assertEqual(RecipeSuggester([])("Mojito"), (None, False))

    def test_long_names(self):
        """Past 200 characters difflib sets popular characters aside: the
        suggester asks it the same question, so it is set aside the same."""
        long_name = "Cocktail " + "a" * 120 + " maison " + "b" * 90
        recipes = recipes_named(long_name, long_name[:-3] + "xyz", "Cocktail maison")
        for name in (long_name, long_name[:-1], long_name.replace("maison", "maisons"), "Cocktail maisons"):
            with self.subTest(name=name[:20]):
                self.assertSameAsTheLoop(name, recipes)

    def test_one_suggester_asked_again_and_again(self):
        """Its matchers keep what they learnt of each recipe's name between
        two names - never of the name before."""
        recipes = recipes_named("Pinte Blonde", "Pinte Ambrée", "Demi Blonde", "Mojito")
        suggest = RecipeSuggester(recipes)
        name = "Pinte Blond"
        for asked in (name, "Mojitos", name, "Demi blonde HH", "Pinte blonde", name):
            with self.subTest(asked=asked):
                self.assertSameAsTheLoop(asked, recipes, suggest)

    def test_the_loop_and_the_suggester_agree_on_any_list(self):
        """Names made of a bar's words, misspelt at random: thousands of
        comparisons, every answer the loop's."""
        words = ["pinte", "demi", "blonde", "blanche", "ambrée", "spritz", "apérol", "mojito", "vin", "rouge"]
        words += ["rosé", "blanc", "pastis", "planche", "mixte", "HH", "happy hour", "verre", "sirop", "menthe"]
        rng = random.Random(20261001)

        def misspelt(text):
            letters = list(text)
            for _ in range(rng.choice((0, 0, 1, 2))):
                position = rng.randrange(len(letters) + 1)
                action = rng.choice(("drop", "add", "swap"))
                if action == "drop" and position < len(letters):
                    del letters[position]
                elif action == "add":
                    letters.insert(position, rng.choice("aeiouyrst "))
                elif position < len(letters):
                    letters[position] = rng.choice("aeiouyrst")
            return "".join(letters)

        def a_name():
            return misspelt(" ".join(rng.choice(words) for _ in range(rng.choice((1, 2, 2, 3)))))

        for trial in range(30):
            recipes = recipes_named(*(a_name() for _ in range(rng.randrange(0, 25))))
            suggest = RecipeSuggester(recipes)
            for _ in range(40):
                name = rng.choice([a_name(), a_name(), rng.choice(recipes).name if recipes else ""])
                with self.subTest(trial=trial, name=name):
                    self.assertSameAsTheLoop(name, recipes, suggest)


#: The loop _pos_row.html drew the recipes with, as it was.
ROW_OPTIONS_LOOP = Template(
    "{% for recipe in recipes %}\n"
    '                        <option value="{{ recipe.pk }}"{% if recipe.pk == product.suggested_recipe.pk %}'
    " selected{% endif %}>{{ recipe.name }}</option>\n"
    "                    {% endfor %}"
)


class RowOptionsTests(SimpleTestCase):
    def setUp(self):
        self.recipes = recipes_named(
            "Pinte <Blonde>", 'Spritz "Apérol"', "Gin & Tonic", "L'Apéro du Chef", "Mojito", "Mojito"
        )

    def assertPrintedAsTheLoopDid(self, product):
        ToLinkRows(self.recipes).prepare(product)
        expected = ROW_OPTIONS_LOOP.render(Context({"recipes": self.recipes, "product": product}))
        self.assertEqual(product.recipe_options, expected)
        return product

    def test_a_row_with_a_suggestion(self):
        product = self.assertPrintedAsTheLoopDid(PosProduct(pk=7, name="Mojitos HH"))
        self.assertIs(product.suggested_recipe, self.recipes[4])
        self.assertTrue(product.suggested_happy_hour)
        self.assertEqual(product.recipe_options.count(" selected"), 1)

    def test_a_row_without_one(self):
        product = self.assertPrintedAsTheLoopDid(PosProduct(pk=8, name="Planche mixte"))
        self.assertIsNone(product.suggested_recipe)
        self.assertNotIn(" selected", product.recipe_options)

    def test_names_are_escaped(self):
        product = self.assertPrintedAsTheLoopDid(PosProduct(pk=9, name="Gin Tonic"))
        self.assertIn("Pinte &lt;Blonde&gt;", product.recipe_options)
        self.assertIn("Spritz &quot;Apérol&quot;", product.recipe_options)
        self.assertIn("Gin &amp; Tonic", product.recipe_options)
        self.assertNotIn("<Blonde>", product.recipe_options)

    def test_its_addresses(self):
        product = ToLinkRows(self.recipes).prepare(PosProduct(pk=12, name="Café"))
        self.assertEqual(product.assign_url, reverse("recipes:pos_product_assign", args=[12]))
        self.assertEqual(product.create_url, f"{reverse('recipes:recipe_create')}?caisse=12")
        linked = ToLinkRows(self.recipes).address(PosProduct(pk=13, name="Mojito", recipe=self.recipes[4]))
        self.assertEqual(linked.recipe_url, reverse("recipes:recipe_detail", args=[self.recipes[4].pk]))


class UrlForEachTests(SimpleTestCase):
    NAMES = (
        "recipes:recipe_detail",
        "recipes:pos_product_assign",
        "recipes:sales_delete",
        "recipes:sale_document_update",
    )
    PKS = (0, 1, 7, 42, 1234, 9_876_543_210_123, 10**18)

    def assertSameAsReverse(self):
        for name in self.NAMES:
            address = url_for_each(name)
            for pk in self.PKS:
                with self.subTest(name=name, pk=pk):
                    self.assertEqual(address(pk), reverse(name, args=[pk]))

    def test_the_address_reverse_gives(self):
        self.assertSameAsReverse()

    def test_under_a_script_prefix(self):
        set_script_prefix("/sous-dossier/")
        try:
            self.assertSameAsReverse()
            self.assertTrue(url_for_each(self.NAMES[0])(3).startswith("/sous-dossier/"))
        finally:
            set_script_prefix("/")


class PagesLinkTheirRowsTests(TestCase):
    def test_the_rows_to_link_post_to_themselves(self):
        recipe = make_recipe(name="Pinte Blonde")
        pending = PosProduct.objects.create(name="Pinte Blonde HH", total_quantity=4)
        linked = PosProduct.objects.create(name="Pinte", recipe=recipe, total_quantity=2)
        ignored = PosProduct.objects.create(name="Café", ignored=True)
        response = self.client.get(reverse("recipes:pos_product_list"))
        for product in (pending, linked, ignored):
            self.assertContains(response, f'action="{reverse("recipes:pos_product_assign", args=[product.pk])}"')
        self.assertContains(response, f'hx-post="{reverse("recipes:pos_product_assign", args=[pending.pk])}"')
        self.assertContains(response, f'href="{reverse("recipes:recipe_detail", args=[recipe.pk])}"')

    def test_every_sale_links_to_its_recipe(self):
        recipes = [make_recipe(name=f"Recette {index}") for index in range(3)]
        for index, recipe in enumerate(recipes):
            RecipeSale.objects.create(
                recipe=recipe, sold_on=date(2026, 6, index + 1), quantity=index + 1, source="test"
            )
        for query in ("", "?ventes=toutes"):
            response = self.client.get(reverse("recipes:sales_list") + query)
            for recipe in recipes:
                with self.subTest(query=query, recipe=recipe.name):
                    self.assertContains(response, f'<a href="{reverse("recipes:recipe_detail", args=[recipe.pk])}">')
