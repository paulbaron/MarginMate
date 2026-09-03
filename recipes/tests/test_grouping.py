"""Tests for how ingredients are grouped into alternatives, and how that
grouping survives a save/reload round trip.

This is where "it didn't save what I entered" lives. An ingredient's `group`
decides whether it's a required ingredient or one of several alternatives -
get it wrong and the recipe means something completely different, with no
error anywhere to say so.
"""

from decimal import Decimal

from django.test import TestCase

from recipes.forms import RecipeIngredientFormSet
from recipes.models import RecipeIngredient
from tests.factories import make_recipe, make_stock_type


class GroupingRoundTripTests(TestCase):
    """Enter a recipe through the form, reopen it, and check it still says
    the same thing. Every test here is a full save -> reload cycle, because
    that's the loop the user actually experiences."""

    def setUp(self):
        self.recipe = make_recipe(name="Mule")
        self.vodka = make_stock_type(name="Vodka")
        self.gin = make_stock_type(name="Gin")
        self.ginger = make_stock_type(name="Ginger beer")
        self.lime = make_stock_type(name="Citrons verts")

    def post(self, rows, total_forms=None, initial_forms=0):
        data = {
            "ingredients-TOTAL_FORMS": str(total_forms if total_forms is not None else len(rows)),
            "ingredients-INITIAL_FORMS": str(initial_forms),
            "ingredients-MIN_NUM_FORMS": "0",
            "ingredients-MAX_NUM_FORMS": "1000",
        }
        for index, fields in rows.items():
            for name, value in fields.items():
                data[f"ingredients-{index}-{name}"] = str(value)
        formset = RecipeIngredientFormSet(data, instance=self.recipe, form_kwargs={"parent_recipe": self.recipe})
        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()
        return formset

    def reopen(self):
        """The formset as the edit page would rebuild it - the same order
        and grouping the user will see."""
        return RecipeIngredientFormSet(instance=self.recipe, form_kwargs={"parent_recipe": self.recipe})

    def saved_rows(self):
        return [
            (i.stock_type.name, i.group)
            for i in self.recipe.ingredients.select_related("stock_type").order_by("group", "id")
        ]

    def row(self, stock_type, quantity="0.04", group=0, **extra):
        return {"source": f"stock:{stock_type.id}", "quantity": quantity, "group": group, **extra}

    # --- the blank row at the bottom of the page -------------------------
    def test_the_trailing_blank_row_does_not_default_into_an_existing_group(self):
        """The always-present empty row at the bottom of the form must add a
        NEW independent ingredient.

        Its `group` renders as 0, which is a real group number that an
        existing ingredient probably already occupies - so filling it in
        silently made the new ingredient an ALTERNATIVE to the first one
        rather than an ingredient in its own right. The recipe then reopens
        saying something the user never entered, with no error to explain it.
        """
        vodka = RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.vodka, quantity=Decimal("0.04"), group=0
        )
        formset = self.reopen()
        blank = formset.forms[-1]
        self.assertTrue(blank.empty_permitted, "the last form should be the spare blank row")
        self.assertNotEqual(
            blank["group"].value(),
            vodka.group,
            "the blank row defaults into an existing ingredient's group",
        )

    def test_filling_the_blank_row_adds_an_ingredient_not_an_alternative(self):
        RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.vodka, quantity=Decimal("0.04"), group=0
        )
        blank_group = self.reopen().forms[-1]["group"].value()

        self.post(
            {
                0: dict(self.row(self.vodka, group=0), id=self.recipe.ingredients.get().id, recipe=self.recipe.id),
                1: self.row(self.ginger, quantity="0.20", group=blank_group),
            },
            initial_forms=1,
        )
        # Two required ingredients, so exactly one variation - not a choice.
        self.assertEqual(self.recipe.variation_count, 1)
        self.assertEqual(self.saved_rows(), [("Vodka", 0), ("Ginger beer", 1)])

    # --- the "OU" button -------------------------------------------------
    def test_regrouping_an_existing_ingredient_is_saved(self):
        """Clicking "OU" on a saved row changes ONLY its group. Formsets skip
        writing back a row whose has_changed() is False, so anything that
        excludes `group` from that check silently drops the edit."""
        vodka = RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.vodka, quantity=Decimal("0.04"), group=0
        )
        gin = RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.gin, quantity=Decimal("0.04"), group=1
        )
        self.post(
            {
                0: dict(self.row(self.vodka, group=0), id=vodka.id, recipe=self.recipe.id),
                1: dict(self.row(self.gin, group=0), id=gin.id, recipe=self.recipe.id),
            },
            initial_forms=2,
        )
        gin.refresh_from_db()
        self.assertEqual(gin.group, 0)
        self.assertEqual(self.recipe.variation_count, 2)

    def test_splitting_a_group_back_apart_is_saved(self):
        vodka = RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.vodka, quantity=Decimal("0.04"), group=0
        )
        gin = RecipeIngredient.objects.create(
            recipe=self.recipe, stock_type=self.gin, quantity=Decimal("0.04"), group=0
        )
        self.assertEqual(self.recipe.variation_count, 2)
        self.post(
            {
                0: dict(self.row(self.vodka, group=0), id=vodka.id, recipe=self.recipe.id),
                1: dict(self.row(self.gin, group=5), id=gin.id, recipe=self.recipe.id),
            },
            initial_forms=2,
        )
        self.assertEqual(self.recipe.variation_count, 1)

    # --- ordering --------------------------------------------------------
    def test_alternatives_stay_next_to_each_other_when_reopened(self):
        """Adding an alternative to the FIRST ingredient gives it the highest
        pk, so ordering rows by pk scatters a group across the page. The "OU"
        connector only joins visually adjacent rows, so the recipe reopens
        looking like three unrelated ingredients instead of "A or B, plus C".
        """
        self.post(
            {
                0: self.row(self.vodka, group=0),
                1: self.row(self.ginger, quantity="0.20", group=1),
            }
        )
        vodka_id = self.recipe.ingredients.get(stock_type=self.vodka).id
        ginger_id = self.recipe.ingredients.get(stock_type=self.ginger).id
        # Now "OU" on the vodka row: a third ingredient, group 0, newest pk.
        self.post(
            {
                0: dict(self.row(self.vodka, group=0), id=vodka_id, recipe=self.recipe.id),
                1: dict(self.row(self.ginger, quantity="0.20", group=1), id=ginger_id, recipe=self.recipe.id),
                2: self.row(self.gin, group=0),
            },
            initial_forms=2,
        )

        rendered = [f.instance.stock_type.name for f in self.reopen().forms if f.instance.pk]
        self.assertEqual(
            rendered,
            ["Vodka", "Gin", "Ginger beer"],
            "the two group-0 alternatives must render next to each other",
        )

    def test_form_order_matches_the_order_shown_on_the_detail_page(self):
        """Otherwise the edit page and the detail page disagree about what
        the recipe looks like."""
        self.post(
            {
                0: self.row(self.vodka, group=0),
                1: self.row(self.ginger, quantity="0.20", group=1),
                2: self.row(self.gin, group=0),
            }
        )
        from_form = [f.instance.stock_type.name for f in self.reopen().forms if f.instance.pk]
        from_detail = [entry["ingredient"].stock_type.name for entry in self.recipe.variations()[0]["breakdown"]]
        self.assertEqual(from_form[: len(from_detail)][:1], from_detail[:1])
        self.assertEqual(sorted(from_form), sorted(["Vodka", "Gin", "Ginger beer"]))
