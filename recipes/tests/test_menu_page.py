"""The "Recettes & ventes" page: recipes, the till products to link to one,
and sales, in one place.

A till product is linked where it is listed, without the page reloading: its
row says where it went (with an undo), and the recipe it went to shows it. A
recipe is created from the till product it is for, and saved, it is linked to
it - and the recipe form links and unlinks till products itself. The likely
recipe is chosen already. Data invented.
"""

import json
from datetime import date

from django.test import TestCase
from django.urls import reverse

from recipes.links import suggest_recipe
from recipes.models import PosProduct, PosProductDailyQuantity, Recipe, RecipeSale
from recipes.sales import record_sales
from tests.factories import make_recipe

HTMX = {"HTTP_HX_REQUEST": "true"}


def recipe_post(name, **extra):
    """The recipe form as a browser posts it, with no ingredient row."""
    data = {
        "name": name,
        "happy_hour_name": "",
        "category": "",
        "yield_quantity": "1",
        "yield_unit": "UNIT",
        "selling_price_ttc": "8.00",
        "happy_hour_price_ttc": "",
        "vat_rate": "0.20",
        "ingredients-TOTAL_FORMS": "0",
        "ingredients-INITIAL_FORMS": "0",
        "ingredients-MIN_NUM_FORMS": "0",
        "ingredients-MAX_NUM_FORMS": "1000",
    }
    data.update(extra)
    return data


class SuggestRecipeTests(TestCase):
    def setUp(self):
        self.recipes = [Recipe(pk=1, name="Pinte Blonde"), Recipe(pk=2, name="Spritz Apérol"), Recipe(pk=3, name="Mojito")]

    def test_the_same_name_whatever_the_case_and_accents(self):
        self.assertEqual(suggest_recipe("SPRITZ APEROL", self.recipes), (self.recipes[1], False))

    def test_a_happy_hour_name_is_its_recipe_at_happy_hour(self):
        self.assertEqual(suggest_recipe("Pinte Blonde HH", self.recipes), (self.recipes[0], True))
        self.assertEqual(suggest_recipe("Pinte blonde (happy hour)", self.recipes), (self.recipes[0], True))

    def test_a_close_spelling_is_suggested(self):
        self.assertEqual(suggest_recipe("Mojitos", self.recipes), (self.recipes[2], False))

    def test_nothing_close_is_nothing(self):
        for name in ("Planche mixte", "", "HH"):
            with self.subTest(name=name):
                self.assertEqual(suggest_recipe(name, self.recipes)[0], None)
        self.assertEqual(suggest_recipe("Mojito", []), (None, False))


class MenuPageTests(TestCase):
    def setUp(self):
        self.pinte = make_recipe(name="Pinte Blonde")
        self.mojito = make_recipe(name="Mojito")
        self.pending = PosProduct.objects.create(name="Pinte Blonde HH", total_quantity=40)
        self.linked = PosProduct.objects.create(name="Mojito maison", recipe=self.mojito, total_quantity=12)
        PosProduct.objects.create(name="Café", ignored=True)

    def test_the_three_tabs_are_one_page(self):
        for name, active in (
            ("recipes:recipe_list", "Recettes"),
            ("recipes:pos_product_list", "À lier"),
            ("recipes:sales_list", "Ventes"),
            ("recipes:sales_import", "Ventes"),
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                tabs = response.context["tabs"]
                self.assertEqual([tab["label"] for tab in tabs], ["Recettes", "À lier", "Ventes"])
                self.assertEqual([tab["count"] for tab in tabs], [2, 1, None])
                self.assertEqual([tab["label"] for tab in tabs if tab["active"]], [active])

    def test_the_sales_tab_imports_from_the_till(self):
        response = self.client.get(reverse("recipes:sales_list"))
        self.assertContains(response, f'action="{reverse("recipes:trigger_sales_import")}"')

    def test_an_import_started_comes_back_to_the_sales_tab(self):
        from unittest import mock

        with mock.patch("recipes.views.threading.Thread"):
            response = self.client.post(
                reverse("recipes:trigger_sales_import"), {"start_date": "2026-06-01", "end_date": "2026-06-30"}
            )
        self.assertRedirects(response, reverse("recipes:sales_list"), fetch_redirect_response=False)

    def test_a_recipe_says_what_the_till_sells_it_as(self):
        record_sales([("Mojito maison", date(2026, 6, 1), 7)])
        response = self.client.get(reverse("recipes:recipe_list"))
        self.assertEqual(response.context["till_names"][self.mojito.pk], ["Mojito maison"])
        self.assertEqual(response.context["units_sold"][self.mojito.pk], 7)
        self.assertContains(response, "Mojito maison")
        detail = self.client.get(reverse("recipes:recipe_detail", args=[self.mojito.pk]))
        self.assertContains(detail, "Mojito maison")

    def test_the_likely_recipe_is_chosen_already(self):
        response = self.client.get(reverse("recipes:pos_product_list"))
        row = response.context["pending"][0]
        self.assertEqual((row.suggested_recipe, row.suggested_happy_hour), (self.pinte, True))
        self.assertContains(response, f'<option value="{self.pinte.pk}" selected>', html=False)
        self.assertContains(response, 'name="as_happy_hour" value="1" checked')

    def test_a_recipe_is_created_for_the_till_product(self):
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertContains(response, f'{reverse("recipes:recipe_create")}?caisse={self.pending.pk}')

    def test_linking_in_place_says_where_it_went(self):
        PosProductDailyQuantity.objects.create(product=self.pending, sold_on=date(2026, 6, 1), quantity=4)
        response = self.client.post(
            reverse("recipes:pos_product_assign", args=[self.pending.pk]),
            {"action": "link", "recipe": self.pinte.pk, "as_happy_hour": "1"},
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<html")
        self.assertContains(response, "lié à")
        self.assertContains(response, reverse("recipes:recipe_detail", args=[self.pinte.pk]))
        self.assertContains(response, 'value="reset"')  # the undo
        self.assertEqual(json.loads(response["HX-Trigger"]), {"to-link-count": 0})
        self.pending.refresh_from_db()
        self.pinte.refresh_from_db()
        self.assertEqual((self.pending.recipe, self.pinte.happy_hour_name), (self.pinte, "Pinte Blonde HH"))
        self.assertEqual(RecipeSale.objects.get(recipe=self.pinte).quantity, 4)

    def test_ignoring_and_undoing_in_place(self):
        url = reverse("recipes:pos_product_assign", args=[self.pending.pk])
        ignored = self.client.post(url, {"action": "ignore"}, **HTMX)
        self.assertContains(ignored, "ignoré")
        back = self.client.post(url, {"action": "reset"}, **HTMX)
        # The row as it was, to link again.
        self.assertContains(back, 'name="recipe"')
        self.assertContains(back, "Pinte Blonde HH")
        self.pending.refresh_from_db()
        self.assertTrue(self.pending.needs_review)

    def test_a_refused_link_says_why_in_the_row(self):
        response = self.client.post(
            reverse("recipes:pos_product_assign", args=[self.pending.pk]), {"action": "link", "recipe": ""}, **HTMX
        )
        self.assertContains(response, "Choisissez une recette.")
        self.assertContains(response, 'name="recipe"')
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.recipe)


class RecipeFormLinksTests(TestCase):
    def setUp(self):
        self.pending = PosProduct.objects.create(name="Spritz Maison", total_quantity=30)
        PosProductDailyQuantity.objects.create(product=self.pending, sold_on=date(2026, 6, 1), quantity=6)

    def test_created_for_a_till_product_it_is_linked_to_it(self):
        url = reverse("recipes:recipe_create") + f"?caisse={self.pending.pk}"
        page = self.client.get(url)
        self.assertEqual(page.context["form"]["name"].value(), "Spritz Maison")
        self.assertEqual(list(page.context["form"]["pos_products"].value()), [self.pending.pk])
        response = self.client.post(url, recipe_post("Spritz", pos_products=[self.pending.pk]))
        self.assertRedirects(response, reverse("recipes:pos_product_list"), fetch_redirect_response=False)
        recipe = Recipe.objects.get(name="Spritz")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.recipe, recipe)
        self.assertEqual(RecipeSale.objects.get(recipe=recipe).quantity, 6)

    def test_the_form_links_and_unlinks(self):
        recipe = make_recipe(name="Spritz", happy_hour_name="Spritz HH")
        happy = PosProduct.objects.create(name="Spritz HH", recipe=recipe)
        elsewhere = PosProduct.objects.create(name="Pinte", recipe=make_recipe(name="Pinte"))
        url = reverse("recipes:recipe_update", args=[recipe.pk])
        page = self.client.get(url)
        offered = {choice.pk for choice in page.context["form"].fields["pos_products"].queryset}
        self.assertEqual(offered, {self.pending.pk, happy.pk})
        self.assertNotIn(elsewhere.pk, offered)
        response = self.client.post(
            url, recipe_post("Spritz", happy_hour_name="Spritz HH", pos_products=[self.pending.pk])
        )
        self.assertRedirects(response, reverse("recipes:recipe_detail", args=[recipe.pk]), fetch_redirect_response=False)
        self.pending.refresh_from_db()
        happy.refresh_from_db()
        recipe.refresh_from_db()
        self.assertEqual(self.pending.recipe, recipe)
        self.assertIsNone(happy.recipe)
        # Its till name goes with it, or the import would keep counting it here.
        self.assertEqual(recipe.happy_hour_name, "")

    def test_a_product_linked_elsewhere_meanwhile_is_refused(self):
        other = make_recipe(name="Autre")
        url = reverse("recipes:recipe_create") + f"?caisse={self.pending.pk}"
        self.client.get(url)
        PosProduct.objects.filter(pk=self.pending.pk).update(recipe=other)
        response = self.client.post(url, recipe_post("Spritz", pos_products=[self.pending.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Recipe.objects.filter(name="Spritz").exists())
