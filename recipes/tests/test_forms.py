"""Tests for the recipe ingredient formset.

These test the formset against the exact POST payloads the page's JavaScript
actually produces - including the awkward ones. Adding and removing rows
client-side is the single richest source of bugs in this app: three separate
forms have now shipped the same "invisible row blocks the save" defect, and
none of it is visible from a normal happy-path test, because a normal test
posts tidy, contiguous indices that the JS never actually emits.
"""

from decimal import Decimal

from django.test import TestCase

from recipes.forms import RecipeIngredientFormSet
from recipes.models import Recipe, RecipeIngredient
from tests.factories import make_recipe, make_stock_type


def formset_payload(rows, total_forms=None, initial_forms=0):
    """Build a formset POST dict from {index: {field: value}}.

    Deliberately takes indices explicitly rather than enumerating a list, so
    a test can post the non-contiguous indices ("0, 1, 3") that removing a
    row in the browser really produces.
    """
    data = {
        "ingredients-TOTAL_FORMS": str(total_forms if total_forms is not None else len(rows)),
        "ingredients-INITIAL_FORMS": str(initial_forms),
        "ingredients-MIN_NUM_FORMS": "0",
        "ingredients-MAX_NUM_FORMS": "1000",
    }
    for index, fields in rows.items():
        for name, value in fields.items():
            data[f"ingredients-{index}-{name}"] = str(value)
    return data


class RecipeIngredientFormSetTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Moscow Mule")
        self.vodka = make_stock_type(name="Vodka")
        self.gin = make_stock_type(name="Gin")
        self.ginger = make_stock_type(name="Ginger Beer")

    def build(self, data):
        return RecipeIngredientFormSet(
            data, instance=self.recipe, form_kwargs={"parent_recipe": self.recipe}
        )

    def test_a_single_ingredient_saves(self):
        formset = self.build(
            formset_payload({0: {"source": f"stock:{self.vodka.id}", "quantity": "0.04", "group": "0"}})
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        self.assertEqual(self.recipe.ingredients.count(), 1)
        self.assertEqual(self.recipe.ingredients.get().stock_type, self.vodka)

    def test_alternatives_share_a_group(self):
        """The "OU" button's whole job: two rows, same group number."""
        formset = self.build(
            formset_payload(
                {
                    0: {"source": f"stock:{self.vodka.id}", "quantity": "0.04", "group": "1"},
                    1: {"source": f"stock:{self.gin.id}", "quantity": "0.05", "group": "1"},
                    2: {"source": f"stock:{self.ginger.id}", "quantity": "0.20", "group": "2"},
                }
            )
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        self.assertEqual(sorted(i.group for i in self.recipe.ingredients.all()), [1, 1, 2])
        self.assertEqual(len(self.recipe.variations()), 2)

    def test_removing_an_added_row_leaves_a_gap_that_must_not_block_the_save(self):
        """B7. Adding three rows then removing the middle one leaves the
        browser posting indices 0 and 2 with TOTAL_FORMS=3 - index 1 is
        simply absent.

        Django skips an empty extra form only when has_changed() is False.
        A hidden `group` field declared with initial=0 makes has_changed()
        True even for a row where NOTHING was submitted, so the gap gets
        validated and fails with "Choisissez un ingrédient." on a row the
        user cannot see, and the recipe becomes unsaveable.
        """
        formset = self.build(
            formset_payload(
                {
                    0: {"source": f"stock:{self.vodka.id}", "quantity": "0.04", "group": "0"},
                    2: {"source": f"stock:{self.ginger.id}", "quantity": "0.20", "group": "0"},
                },
                total_forms=3,
            )
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        self.assertEqual(self.recipe.ingredients.count(), 2)

    def test_a_trailing_untouched_empty_row_is_ignored(self):
        """The `extra=1` blank row that's always on the page."""
        formset = self.build(
            formset_payload(
                {
                    0: {"source": f"stock:{self.vodka.id}", "quantity": "0.04", "group": "0"},
                    1: {"source": "", "quantity": "", "group": "0"},
                }
            )
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        self.assertEqual(self.recipe.ingredients.count(), 1)

    def test_a_row_with_a_quantity_but_no_ingredient_is_still_an_error(self):
        """The gap-row fix must not silently swallow a half-filled row."""
        formset = self.build(
            formset_payload({0: {"source": "", "quantity": "0.04", "group": "0"}})
        )
        self.assertFalse(formset.is_valid())
        self.assertIn("source", formset.errors[0])

    def test_a_row_with_an_ingredient_but_no_quantity_is_an_error(self):
        formset = self.build(
            formset_payload({0: {"source": f"stock:{self.vodka.id}", "quantity": "", "group": "0"}})
        )
        self.assertFalse(formset.is_valid())
        self.assertIn("quantity", formset.errors[0])

    def test_editing_an_existing_recipe_with_a_gap(self):
        """Same gap, but now with saved rows in the formset - the indices
        below initial_form_count have to keep their intact -id."""
        existing = RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.vodka, quantity=Decimal("0.04"), group=0
        )
        formset = self.build(
            formset_payload(
                {
                    0: {
                        "id": existing.id,
                        "recipe": self.recipe.id,
                        "source": f"stock:{self.vodka.id}",
                        "quantity": "0.05",
                        "group": "0",
                    },
                    2: {"source": f"stock:{self.ginger.id}", "quantity": "0.20", "group": "0"},
                },
                total_forms=3,
                initial_forms=1,
            )
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        existing.refresh_from_db()
        self.assertEqual(existing.quantity, Decimal("0.05"))
        self.assertEqual(self.recipe.ingredients.count(), 2)

    def test_deleting_an_existing_row(self):
        existing = RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.vodka, quantity=Decimal("0.04"), group=0
        )
        formset = self.build(
            formset_payload(
                {
                    0: {
                        "id": existing.id,
                        "recipe": self.recipe.id,
                        "source": f"stock:{self.vodka.id}",
                        "quantity": "0.04",
                        "group": "0",
                        "DELETE": "on",
                    }
                },
                initial_forms=1,
            )
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        self.assertEqual(self.recipe.ingredients.count(), 0)

    def test_a_recipe_with_variations_can_be_used_as_an_ingredient(self):
        """It used to be refused - "which variation's cost?" had no answer.
        It does now: the option contributes a cost RANGE, and the parent
        simply gains that many more variations of its own. See
        test_nested_variations.py."""
        sub = make_recipe(name="Sirop Maison")
        RecipeIngredient.objects.create(recipe=sub, stock_type=self.vodka, quantity=Decimal("1"), group=1)
        RecipeIngredient.objects.create(recipe=sub, stock_type=self.gin, quantity=Decimal("1"), group=1)
        self.assertTrue(sub.has_variations)

        formset = self.build(
            formset_payload({0: {"source": f"recipe:{sub.id}", "quantity": "0.02", "group": "0"}})
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        # The parent inherits the sub-recipe's two ways of being made.
        self.assertEqual(self.recipe.variation_count, 2)

    def test_a_recipe_cannot_contain_itself(self):
        formset = self.build(
            formset_payload({0: {"source": f"recipe:{self.recipe.id}", "quantity": "0.02", "group": "0"}})
        )
        self.assertFalse(formset.is_valid())

    def test_a_cycle_between_two_recipes_is_rejected(self):
        syrup = make_recipe(name="Sirop")
        RecipeIngredient.objects.create(recipe=syrup, sub_recipe=self.recipe, quantity=Decimal("1"), group=0)
        formset = self.build(
            formset_payload({0: {"source": f"recipe:{syrup.id}", "quantity": "0.02", "group": "0"}})
        )
        self.assertFalse(formset.is_valid())
        self.assertEqual(Recipe.objects.filter(name="Sirop").count(), 1)
