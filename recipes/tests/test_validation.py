"""Tests for the values a recipe is allowed to hold.

These aren't cosmetic. Every field below feeds an arithmetic expression that
runs on the LIST page - so one bad row doesn't just break its own recipe, it
takes down every page that so much as mentions it, including the admin you'd
use to fix it.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from recipes.forms import RecipeForm
from recipes.models import Recipe, RecipeIngredient
from tests.factories import make_priced_stock_type, make_recipe, make_stock_type


def form_data(**overrides):
    data = {
        "name": "Test",
        "category": "",
        "yield_quantity": "1",
        "yield_unit": "UNIT",
        "selling_price_ttc": "8.50",
        "happy_hour_price_ttc": "",
        "vat_rate": "0.20",
    }
    data.update(overrides)
    return data


class RecipeFieldValidationTests(TestCase):
    def test_a_valid_recipe(self):
        self.assertTrue(RecipeForm(form_data()).is_valid())

    def test_a_vat_rate_of_minus_one_is_rejected(self):
        """(1 + vat_rate) is a divisor - at exactly -1 it's zero, and the
        recipe list, the detail page and the admin all 500 together."""
        form = RecipeForm(form_data(vat_rate="-1"))
        self.assertFalse(form.is_valid())
        self.assertIn("vat_rate", form.errors)

    def test_a_negative_vat_rate_is_rejected(self):
        self.assertFalse(RecipeForm(form_data(vat_rate="-0.5")).is_valid())

    def test_a_vat_rate_above_one_is_rejected(self):
        self.assertFalse(RecipeForm(form_data(vat_rate="1.5")).is_valid())

    def test_the_boundary_vat_rates_are_allowed(self):
        self.assertTrue(RecipeForm(form_data(vat_rate="0")).is_valid())
        self.assertTrue(RecipeForm(form_data(vat_rate="1")).is_valid())

    def test_a_zero_yield_is_rejected(self):
        """yield_quantity divides the batch cost (see unit_cost_ht)."""
        self.assertFalse(RecipeForm(form_data(yield_quantity="0")).is_valid())

    def test_a_negative_yield_is_rejected(self):
        self.assertFalse(RecipeForm(form_data(yield_quantity="-2")).is_valid())

    def test_a_negative_selling_price_is_rejected(self):
        self.assertFalse(RecipeForm(form_data(selling_price_ttc="-1")).is_valid())

    def test_a_negative_happy_hour_price_is_rejected(self):
        self.assertFalse(RecipeForm(form_data(happy_hour_price_ttc="-1")).is_valid())

    def test_a_free_recipe_is_allowed(self):
        self.assertTrue(RecipeForm(form_data(selling_price_ttc="0")).is_valid())


class IngredientQuantityValidationTests(TestCase):
    def test_a_negative_quantity_is_rejected(self):
        """It would subtract from the recipe's cost, which reads as a better
        margin - wrong in the most flattering possible direction."""
        ingredient = RecipeIngredient(
            recipe=make_recipe(), stock_type=make_stock_type(), quantity=Decimal("-1"), group=0
        )
        with self.assertRaises(ValidationError):
            ingredient.full_clean()

    def test_a_zero_quantity_is_rejected(self):
        ingredient = RecipeIngredient(
            recipe=make_recipe(), stock_type=make_stock_type(), quantity=Decimal("0"), group=0
        )
        with self.assertRaises(ValidationError):
            ingredient.full_clean()

    def test_a_tiny_positive_quantity_is_allowed(self):
        ingredient = RecipeIngredient(
            recipe=make_recipe(), stock_type=make_stock_type(), quantity=Decimal("0.0001"), group=0
        )
        ingredient.full_clean()


class BadDataStillRendersTests(TestCase):
    """Rows that predate the validators, or arrive through a raw update, must
    not be able to take a page down - the page is where you'd notice."""

    def test_a_vat_rate_of_minus_one_does_not_crash_the_pages(self):
        recipe = make_recipe(name="Cassée", selling_price_ttc="10")
        RecipeIngredient.objects.create(
            recipe=recipe,
            stock_type=make_priced_stock_type(unit_cost_ht="2", quantity="1"),
            quantity=Decimal("1"),
            group=0,
        )
        Recipe.objects.filter(pk=recipe.pk).update(vat_rate=Decimal("-1"))

        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})).status_code, 200
        )
        self.assertEqual(
            self.client.get(reverse("recipes:recipe_update", kwargs={"pk": recipe.pk})).status_code, 200
        )

    def test_a_zero_yield_does_not_divide_by_zero(self):
        recipe = make_recipe(name="Sans rendement")
        RecipeIngredient.objects.create(
            recipe=recipe,
            stock_type=make_priced_stock_type(unit_cost_ht="2", quantity="1"),
            quantity=Decimal("1"),
            group=0,
        )
        Recipe.objects.filter(pk=recipe.pk).update(yield_quantity=Decimal("0"))
        recipe.refresh_from_db()

        self.assertEqual(recipe.unit_cost_ht(), Decimal("0"))
        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})).status_code, 200
        )

    def test_a_sub_recipe_cycle_does_not_crash_the_pages(self):
        """The ingredient form refuses to create A -> B -> A, but the Django
        admin doesn't go through that form. Unguarded, costing recurses until
        the stack runs out - which 500s the recipe list and the admin page
        you'd need to fix it from, not just the two recipes involved."""
        a = make_recipe(name="A", selling_price_ttc="10")
        b = make_recipe(name="B", selling_price_ttc="10")
        RecipeIngredient.objects.create(recipe=a, sub_recipe=b, quantity=Decimal("1"), group=0)
        RecipeIngredient.objects.create(recipe=b, sub_recipe=a, quantity=Decimal("1"), group=0)

        self.assertEqual(a.cost_ht(), Decimal("0"))
        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)
        for recipe in (a, b):
            self.assertEqual(
                self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})).status_code, 200
            )
            self.assertEqual(
                self.client.get(reverse("recipes:recipe_update", kwargs={"pk": recipe.pk})).status_code, 200
            )

    def test_a_longer_cycle_is_also_survivable(self):
        a, b, c = (make_recipe(name=name, selling_price_ttc="10") for name in ("A", "B", "C"))
        RecipeIngredient.objects.create(recipe=a, sub_recipe=b, quantity=Decimal("1"), group=0)
        RecipeIngredient.objects.create(recipe=b, sub_recipe=c, quantity=Decimal("1"), group=0)
        RecipeIngredient.objects.create(recipe=c, sub_recipe=a, quantity=Decimal("1"), group=0)
        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)

    def test_a_recipe_that_uses_itself_does_not_crash(self):
        recipe = make_recipe(name="Auto", selling_price_ttc="10")
        RecipeIngredient.objects.create(recipe=recipe, sub_recipe=recipe, quantity=Decimal("1"), group=0)
        self.assertEqual(recipe.cost_ht(), Decimal("0"))
        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)

    def test_a_valid_chain_still_costs_correctly(self):
        """The guard must not break legitimate nesting."""
        base = make_recipe(name="Base", yield_quantity="1")
        RecipeIngredient.objects.create(
            recipe=base, stock_type=make_priced_stock_type(unit_cost_ht="4", quantity="10"),
            quantity=Decimal("2"), group=0,
        )
        middle = make_recipe(name="Milieu", yield_quantity="1")
        RecipeIngredient.objects.create(recipe=middle, sub_recipe=base, quantity=Decimal("3"), group=0)
        top = make_recipe(name="Haut")
        RecipeIngredient.objects.create(recipe=top, sub_recipe=middle, quantity=Decimal("2"), group=0)

        self.assertEqual(base.cost_ht(), Decimal("8"))     # 2 x 4
        self.assertEqual(middle.cost_ht(), Decimal("24"))  # 3 x 8
        self.assertEqual(top.cost_ht(), Decimal("48"))     # 2 x 24

    def test_a_negative_ingredient_quantity_does_not_crash_the_pages(self):
        recipe = make_recipe(name="Négative", selling_price_ttc="10")
        ingredient = RecipeIngredient.objects.create(
            recipe=recipe,
            stock_type=make_priced_stock_type(unit_cost_ht="2", quantity="1"),
            quantity=Decimal("1"),
            group=0,
        )
        RecipeIngredient.objects.filter(pk=ingredient.pk).update(quantity=Decimal("-5"))

        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})).status_code, 200
        )
