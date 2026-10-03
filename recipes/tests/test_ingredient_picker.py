"""The recipe form's ingredient picker: a search box, and article categories
offered as « Catégorie : … » (the owner, 03/10/2026).

A category is never a form choice. Picked in the browser, it becomes one row
per article of it, all in the row's « OU » group - so what reaches the server
is the payload rows added one by one already make, and that is what is
pinned here: the categories the page is handed, and the save of such a
group with the gaps the browser leaves when some of its rows are removed.
"""

from django.test import TestCase
from django.urls import reverse

from recipes.forms import RecipeIngredientFormSet, ingredient_categories
from recipes.tests.test_forms import formset_payload
from tests.factories import make_recipe, make_stock_type
from tests.test_json_islands import island


class IngredientCategoriesTests(TestCase):
    def test_each_category_lists_its_articles_by_name(self):
        rum_b = make_stock_type(name="Rhum blanc", category="Rhums")
        rum_a = make_stock_type(name="Rhum ambré", category="Rhums")
        gin = make_stock_type(name="Gin", category="Gins")
        self.assertEqual(
            ingredient_categories(),
            [
                {"name": "Gins", "sources": [f"stock:{gin.pk}"]},
                {"name": "Rhums", "sources": [f"stock:{rum_a.pk}", f"stock:{rum_b.pk}"]},
            ],
        )

    def test_an_article_with_no_category_belongs_to_none(self):
        make_stock_type(name="Citron", category="")
        self.assertEqual(ingredient_categories(), [])

    def test_the_page_hands_them_over_as_data(self):
        rum = make_stock_type(name="Rhum blanc", category="Rhums")
        page = self.client.get(reverse("recipes:recipe_create")).content.decode()
        self.assertEqual(
            island(page, "ingredient-categories-data"), [{"name": "Rhums", "sources": [f"stock:{rum.pk}"]}]
        )


class CategoryGroupSaveTests(TestCase):
    """What the browser posts once a category was picked and two of its
    articles removed: one group, indices with gaps, the same quantity."""

    def test_the_articles_left_save_as_one_choice(self):
        recipe = make_recipe(name="Ti punch")
        rums = [make_stock_type(name=f"Rhum {n}", category="Rhums") for n in range(5)]
        lime = make_stock_type(name="Citron vert")
        kept = {0: rums[0], 2: rums[2], 4: rums[4]}
        rows = {index: {"source": f"stock:{rum.pk}", "quantity": "0.05", "group": "3"} for index, rum in kept.items()}
        rows[5] = {"source": f"stock:{lime.pk}", "quantity": "0.01", "group": "4"}
        formset = RecipeIngredientFormSet(
            formset_payload(rows, total_forms=6), instance=recipe, form_kwargs={"parent_recipe": recipe}
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        self.assertEqual(
            sorted((i.group, i.stock_type.name) for i in recipe.ingredients.all()),
            [(3, "Rhum 0"), (3, "Rhum 2"), (3, "Rhum 4"), (4, "Citron vert")],
        )
        self.assertEqual(len(recipe.variations()), 3)
