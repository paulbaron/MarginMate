"""Tests for the recipe detail page.

The page shows ONE variation at a time, chosen by ?v=<index>.<index>... -
one option index per choice group. Everything about that selection is
server-side, so what's on screen is always a costing the server actually
computed, and the page's size doesn't depend on how many variations exist.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from recipes.models import RecipeIngredient
from tests.factories import make_priced_stock_type, make_recipe, make_stock_type
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax


def build(group_costs, **kwargs):
    recipe = make_recipe(**kwargs)
    for group_index, costs in enumerate(group_costs):
        for cost in costs:
            RecipeIngredient.objects.create(
                recipe=recipe,
                stock_type=make_priced_stock_type(unit_cost_ht=cost, quantity="1"),
                quantity=Decimal("1"),
                group=group_index,
            )
    return recipe


class VariationSelectionViewTests(TestCase):
    def setUp(self):
        self.recipe = build([["1", "2", "3"], ["10", "20"]], selling_price_ttc="120.00")

    def get(self, **params):
        return self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": self.recipe.pk}), params)

    def cost_shown(self, response):
        return response.context["variation"]["cost_ht"]

    def test_defaults_to_the_first_option_of_every_group(self):
        self.assertEqual(self.cost_shown(self.get()), Decimal("11"))

    def test_selecting_a_variation(self):
        self.assertEqual(self.cost_shown(self.get(v="2.1")), Decimal("23"))
        self.assertEqual(self.cost_shown(self.get(v="0.1")), Decimal("21"))
        self.assertEqual(self.cost_shown(self.get(v="1.0")), Decimal("12"))

    def test_an_out_of_range_selection_falls_back_rather_than_erroring(self):
        """An old bookmark must survive the recipe being edited."""
        response = self.get(v="9.9")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cost_shown(response), Decimal("11"))

    def test_a_malformed_selection_does_not_crash(self):
        for value in ("", "abc", "1.", ".", "..", "-1.-1", "1.2.3.4.5", "999999999999999999999"):
            with self.subTest(v=value):
                response = self.get(v=value)
                self.assertEqual(response.status_code, 200)

    def test_the_selected_variation_is_the_one_marked_selected_in_the_picker(self):
        response = self.get(v="2.1")
        self.assertEqual(response.context["selection_key"], "2.1")
        self.assertContains(response, '<option value="2.1" selected>')

    def test_the_picker_lists_every_variation_when_there_are_few(self):
        response = self.get()
        self.assertEqual(len(response.context["listed_variations"]), 6)
        self.assertContains(response, 'id="variation-select"')

    def test_the_range_summary_is_shown(self):
        response = self.get()
        summary = response.context["summary"]
        self.assertEqual(summary["variation_count"], 6)
        self.assertEqual(summary["cost_range"], (Decimal("11"), Decimal("23")))

    def test_a_single_variation_recipe_shows_no_picker(self):
        recipe = build([["5"], ["3"]])
        response = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))
        self.assertNotContains(response, 'id="variation-select"')
        self.assertNotContains(response, 'class="js-group-picker"')
        self.assertEqual(response.context["variation"]["cost_ht"], Decimal("8"))

    def test_a_recipe_with_no_ingredients(self):
        recipe = make_recipe()
        response = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["variation"])
        self.assertContains(response, "Aucun ingrédient")


class ManyVariationsViewTests(TestCase):
    """Past the listing cap, the page switches to one dropdown per choice."""

    def setUp(self):
        # 3 groups of 5 = 125 variations, over the cap of 60.
        self.recipe = build([["1", "2", "3", "4", "5"]] * 3, selling_price_ttc="120.00")

    def get(self, **params):
        return self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": self.recipe.pk}), params)

    def test_switches_to_per_group_pickers(self):
        response = self.get()
        self.assertIsNone(response.context["listed_variations"])
        self.assertContains(response, 'class="js-group-picker"', count=3)

    def test_the_page_has_no_unrendered_template_syntax(self):
        """This branch is only reached past the listing cap, so the smoke
        tests (which use small recipes) never render it - and a stray
        multi-line {# comment #} here printed itself onto the page in full."""
        assertNoUnrenderedTemplateSyntax(self, self.get(), "recipe detail, many variations")

    def test_each_picker_offers_that_group_s_options(self):
        response = self.get()
        self.assertEqual([len(p["options"]) for p in response.context["group_pickers"]], [5, 5, 5])

    def test_the_selection_still_drives_the_costing(self):
        self.assertEqual(self.get(v="4.4.4").context["variation"]["cost_ht"], Decimal("15"))
        self.assertEqual(self.get(v="0.0.0").context["variation"]["cost_ht"], Decimal("3"))

    def test_the_pickers_show_the_current_selection(self):
        response = self.get(v="2.0.4")
        self.assertEqual([p["selected"] for p in response.context["group_pickers"]], [2, 0, 4])

    def test_groups_without_a_choice_get_no_picker(self):
        recipe = build([["1", "2", "3", "4", "5"]] * 3 + [["7"]], selling_price_ttc="120.00")
        response = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))
        self.assertEqual(len(response.context["group_pickers"]), 3)
        # ...but the fixed ingredient is still costed in.
        self.assertEqual(response.context["variation"]["cost_ht"], Decimal("3") + Decimal("7"))

    def test_the_picker_positions_index_into_all_groups_not_just_the_choices(self):
        """A fixed ingredient in the middle must not shift the ?v= indices."""
        recipe = build([["1", "2"], ["7"], ["10", "20"]], selling_price_ttc="120.00")
        response = self.client.get(
            reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}), {"v": "1.0.1"}
        )
        self.assertEqual([p["position"] for p in response.context["group_pickers"]], [0, 2])
        self.assertEqual(response.context["variation"]["cost_ht"], Decimal("2") + 7 + 20)


class PieChartEscapingTests(TestCase):
    def test_an_ingredient_name_cannot_inject_markup(self):
        """The chart is hand-built and rendered with |safe, and stock item
        names come from invoice text - so they have to be escaped."""
        recipe = make_recipe(selling_price_ttc="120.00")
        evil = make_priced_stock_type(name='<img src=x onerror="alert(1)">', unit_cost_ht="5", quantity="1")
        RecipeIngredient.objects.create(recipe=recipe, stock_type=evil, quantity=Decimal("1"), group=0)
        other = make_priced_stock_type(name="Normal", unit_cost_ht="5", quantity="1")
        RecipeIngredient.objects.create(recipe=recipe, stock_type=other, quantity=Decimal("1"), group=1)

        response = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<img src=x")
        self.assertContains(response, "&lt;img src=x")

    def test_a_single_ingredient_name_cannot_inject_markup(self):
        """The one-ingredient chart is a separate code path."""
        recipe = make_recipe(selling_price_ttc="120.00")
        evil = make_priced_stock_type(name="<script>alert(1)</script>", unit_cost_ht="5", quantity="1")
        RecipeIngredient.objects.create(recipe=recipe, stock_type=evil, quantity=Decimal("1"), group=0)

        response = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))
        self.assertNotContains(response, "<script>alert(1)</script>")


class QueryCountTests(TestCase):
    """Guards against the N+1s that made these pages slow enough to look
    broken - each was a per-row rebuild of a whole-table query."""

    def test_the_list_page_does_not_query_per_recipe(self):
        url = reverse("recipes:recipe_list")
        for _ in range(3):
            build([["1", "2"], ["3"]])
        with_three = self._query_count(url)

        for _ in range(17):
            build([["1", "2"], ["3"]])
        self.assertEqual(
            with_three,
            self._query_count(url),
            "the number of queries grows with the number of recipes",
        )

    def test_the_edit_form_does_not_rebuild_its_choices_per_row(self):
        """The "pick an ingredient" dropdown is two full table scans. It used
        to be rebuilt for every row on the page, so a recipe with thirty
        ingredients ran it thirty times - and each rebuild also computed
        every other recipe's full cartesian product to decide which were
        usable as sub-recipes."""
        recipe = build([["1", "2"], ["3"], ["4"]])
        url = reverse("recipes:recipe_update", kwargs={"pk": recipe.pk})
        with_four_rows = self._query_count(url)

        for index in range(12):
            RecipeIngredient.objects.create(
                recipe=recipe, stock_type=make_stock_type(), quantity=Decimal("1"), group=50 + index
            )
        with_sixteen_rows = self._query_count(url)

        self.assertEqual(
            with_four_rows,
            with_sixteen_rows,
            "the number of queries grows with the number of ingredient rows",
        )

    def test_the_detail_page_query_count_is_flat(self):
        recipe = build([["1", "2"], ["3"]])
        url = reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})
        small = self._query_count(url)

        for index in range(10):
            RecipeIngredient.objects.create(
                recipe=recipe,
                stock_type=make_priced_stock_type(unit_cost_ht="2", quantity="1"),
                quantity=Decimal("1"),
                group=50 + index,
            )
        self.assertEqual(small, self._query_count(url))

    def _query_count(self, url) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries)
