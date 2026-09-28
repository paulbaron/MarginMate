"""Une recette qui n'est pas vendue telle quelle.

A house syrup, an infusion: a preparation used inside other recipes and never
sold over the counter. It was filed at 0,00 € because the price was required,
and 0 is a price - so its own page put that 0 against what the preparation
costs and read a negative « marge » and « facteur x0,00 », a loss on
something nobody ever sold. A house preparation is exactly that on the real
data (read-only): a recipe with no till product and no sale, while a good
share of the recipes are used as a sub-recipe.

So a **blank** price is what says « pas vendue directement », and it is the
only thing that says it. Never inferred from being used as a sub-recipe:
nearly all of those ARE sold - a cocktail is poured into a jug and sold by
the glass - and reading the two as one would take the price off a recipe that
has one. Never inferred from a 0 either: a 0 is a price somebody typed.

With no price there is no margin, no percentage and no factor, and the pages
say so rather than printing a figure. What a sale takes (`sale_quantity`) is
not asked either - there is no sale.

What a blank price must NOT touch is what a PARENT pays for it:
`unit_cost_ht` is per yield unit and has nothing to do with a selling price.

Every name and figure below is invented.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from inventory.models import UnitChoices
from recipes.forms import RecipeForm
from recipes.models import Recipe, variation_scope
from recipes.tests.test_validation import form_data
from tests.factories import make_ingredient, make_priced_stock_type, make_recipe


class Fixtures:
    def syrup(self, **kwargs):
        """A 1,5 L batch of syrup costing 10,00 € HT, sold to nobody."""
        sugar = make_priced_stock_type(
            name=f"Sucre {kwargs.get('name', 'x')}", unit=UnitChoices.KILOGRAM, unit_cost_ht="10", quantity="100"
        )
        recipe = make_recipe(
            name=kwargs.pop("name", "Sirop maison"),
            yield_quantity="1.5",
            yield_unit=UnitChoices.LITRE,
            # None unless the caller wants a priced one, to compare against.
            selling_price_ttc=kwargs.pop("selling_price_ttc", None),
            **kwargs,
        )
        make_ingredient(recipe, stock_type=sugar, quantity="1")
        return recipe


class ARecipeWithNoPriceTests(Fixtures, TestCase):
    def test_it_is_saved_without_one(self):
        recipe = self.syrup()
        recipe.full_clean()
        self.assertIsNone(recipe.selling_price_ttc)
        self.assertIsNone(recipe.selling_price_ht)

    def test_it_is_not_sold_directly(self):
        self.assertFalse(self.syrup().is_sold_directly)
        self.assertTrue(make_recipe(name="Mojito").is_sold_directly)

    def test_a_price_of_zero_is_still_a_price(self):
        """Somebody typed it - a comped drink is sold, at nothing."""
        free = make_recipe(name="Offert", selling_price_ttc="0")
        self.assertTrue(free.is_sold_directly)
        self.assertEqual(free.selling_price_ht, Decimal("0"))

    def test_being_used_as_a_sub_recipe_says_nothing_about_being_sold(self):
        """Nearly every sub-recipe on the real data is sold as well: a
        cocktail is poured into a jug and sold by the glass. Read as one, the
        price would come off a recipe that has one."""
        syrup = self.syrup()
        cocktail = make_recipe(name="Cocktail", selling_price_ttc="8.00")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02")
        self.assertTrue(cocktail.is_sold_directly)
        self.assertFalse(syrup.is_sold_directly)


class NoPriceMeansNoMarginTests(Fixtures, TestCase):
    def test_the_summary_prints_no_margin_no_percentage_and_no_factor(self):
        with variation_scope():
            summary = self.syrup().summary()
        self.assertIsNone(summary["margin_range"])
        self.assertIsNone(summary["margin_percent_range"])
        self.assertIsNone(summary["price_factor_range"])

    def test_the_cost_is_still_worked_out(self):
        """What it costs to make is a fact about the preparation, and the
        page shows it - it is the whole reason a preparation is filed."""
        with variation_scope():
            summary = self.syrup().summary()
        self.assertEqual(summary["cost_range"][1], Decimal("10.00"))

    def test_the_chosen_variation_says_the_same(self):
        with variation_scope():
            variation = self.syrup().variation_at(0)
        self.assertEqual(variation["cost_ht"], Decimal("10.00"))
        self.assertIsNone(variation["margin_ht"])
        self.assertIsNone(variation["margin_percent"])
        self.assertIsNone(variation["price_factor"])
        self.assertIsNone(variation["happy_hour"])


class ItStillPricesItsParentTests(Fixtures, TestCase):
    """A parent pays its sub-recipe by the unit PRODUCED. Whether that
    sub-recipe carries a selling price is none of the parent's business."""

    def test_the_parent_pays_the_same_with_or_without_a_price(self):
        priced = self.syrup(name="Sirop vendu", selling_price_ttc="4.00")
        free = self.syrup(name="Sirop maison")
        with variation_scope():
            self.assertEqual(priced.unit_cost_ht(), free.unit_cost_ht())

    def test_a_cocktail_using_it_is_costed_normally(self):
        syrup = self.syrup()
        cocktail = make_recipe(name="Cocktail", selling_price_ttc="9.00")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.15")
        with variation_scope():
            summary = cocktail.summary()
        # 10,00 € for 1,5 L is 6,6667 € the litre; 0,15 L of it is 1,00 €.
        self.assertEqual(summary["cost_range"][1].quantize(Decimal("0.01")), Decimal("1.00"))
        self.assertIsNotNone(summary["margin_range"])


class TheFormTests(TestCase):
    def test_neither_the_price_nor_the_sale_quantity_has_to_be_filled(self):
        form = RecipeForm(form_data(selling_price_ttc="", sale_quantity=""))
        self.assertTrue(form.is_valid(), form.errors)
        recipe = form.save()
        self.assertIsNone(recipe.selling_price_ttc)
        self.assertEqual(recipe.sale_quantity, Decimal("1"))

    def test_a_sale_quantity_left_blank_on_a_sold_recipe_is_one_whole_sale(self):
        form = RecipeForm(form_data(selling_price_ttc="8.00", sale_quantity=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().sale_quantity, Decimal("1"))

    def test_a_sale_quantity_without_a_price_is_refused(self):
        """It says « one sale takes this much » of a recipe that is not
        sold - two answers that cannot both be true, and silently keeping
        one would leave the other on screen."""
        form = RecipeForm(form_data(selling_price_ttc="", sale_quantity="0.15"))
        self.assertFalse(form.is_valid())
        self.assertIn("sale_quantity", form.errors)

    def test_blanking_the_price_on_a_saved_recipe_is_accepted(self):
        """The regression that matters, and the exact thing asked for: open
        a preparation filed at 0,00 €, clear the price, save. The box draws
        the STORED « 1.0000 », so a guard comparing the posted string to
        « 1 » refuses the one edit this feature exists for."""
        make_recipe(name="Sirop", selling_price_ttc="0")
        saved = Recipe.objects.get(name="Sirop")
        drawn = str(RecipeForm(instance=saved)["sale_quantity"].value())
        self.assertEqual(drawn, "1.0000")
        form = RecipeForm(
            form_data(name="Sirop", selling_price_ttc="", sale_quantity=drawn), instance=saved
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().selling_price_ttc)

    def test_saving_an_unpriced_recipe_untouched_is_accepted(self):
        make_recipe(name="Sirop", selling_price_ttc=None)
        saved = Recipe.objects.get(name="Sirop")
        drawn = str(RecipeForm(instance=saved)["sale_quantity"].value())
        form = RecipeForm(
            form_data(name="Sirop", selling_price_ttc="", sale_quantity=drawn), instance=saved
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_happy_hour_price_without_a_selling_price_is_refused(self):
        """A price in happy hour on something that is not sold is the same
        contradiction, and it escaped the guard: the fiche would print
        « Pas vendue directement » above a happy-hour margin."""
        form = RecipeForm(form_data(selling_price_ttc="", happy_hour_price_ttc="4.00"))
        self.assertFalse(form.is_valid())
        self.assertIn("happy_hour_price_ttc", form.errors)

    def test_a_price_still_has_to_be_a_price(self):
        self.assertFalse(RecipeForm(form_data(selling_price_ttc="-1")).is_valid())


class ThePagesTests(Fixtures, TestCase):
    def test_the_recipe_page_says_it_is_not_sold_rather_than_printing_a_price(self):
        recipe = self.syrup()
        page = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Pas vendue directement")
        self.assertNotContains(page, "Marge HT")

    def test_the_list_says_it_too(self):
        self.syrup()
        page = self.client.get(reverse("recipes:recipe_list"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Pas vendue directement")

    def test_a_sold_recipe_still_shows_its_price_and_margin(self):
        article = make_priced_stock_type(name="Rhum", unit_cost_ht="30", quantity="10")
        recipe = make_recipe(name="Ti-punch", selling_price_ttc="8.00")
        make_ingredient(recipe, stock_type=article, quantity="0.05")
        page = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))
        self.assertContains(page, "Marge HT")
        self.assertNotContains(page, "Pas vendue directement")


class ItIsNotOfferedOnABonDeVenteTests(Fixtures, TestCase):
    """« Vendu » lists what can be sold, and a preparation cannot.

    Offered, a line naming it books its full cost against 0,00 € of revenue
    (`SaleDocumentLine.total_ttc` has nothing to fall back on) and
    `margins.computation` counts both - the exact asymmetry CLAUDE.md
    forbids for an article sold as itself: both sides out, or neither.
    """

    def source_values(self, **kwargs):
        from recipes.forms import sale_source_choices

        values = []
        for _group, entries in sale_source_choices(**kwargs)[1:]:
            values += [value for value, _label in entries]
        return values

    def test_a_preparation_is_not_offered(self):
        syrup = self.syrup()
        sold = make_recipe(name="Mojito", selling_price_ttc="8.00")
        offered = self.source_values()
        self.assertIn(f"recipe:{sold.pk}", offered)
        self.assertNotIn(f"recipe:{syrup.pk}", offered)

    def test_a_recipe_priced_at_zero_is_still_offered(self):
        free = make_recipe(name="Offert", selling_price_ttc="0")
        self.assertIn(f"recipe:{free.pk}", self.source_values())

    def test_a_line_already_naming_one_can_still_be_edited(self):
        """A price cleared after the line was written must not make the
        document unopenable - the choice is put back for that line alone."""
        syrup = self.syrup()
        self.assertIn(f"recipe:{syrup.pk}", self.source_values(keep=syrup.pk))


class ItSaysOnlyWhatItKnowsTests(Fixtures, TestCase):
    """A blank price says « not sold ». It says nothing about what uses the
    recipe, and the page must not assert what it has not checked - that is
    the forbidden inference run backwards.
    """

    def page(self, recipe):
        return self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk}))

    def test_a_preparation_nothing_uses_is_not_called_one(self):
        """A recipe being drafted has a blank price too, and nothing counts
        what it costs - which is the case worth saying."""
        page = self.page(self.syrup())
        self.assertContains(page, "Pas vendue directement")
        self.assertNotContains(page, "utilisée dans d'autres recettes")
        self.assertContains(page, "utilisée dans aucune recette")

    def test_a_preparation_something_uses_says_so(self):
        syrup = self.syrup()
        cocktail = make_recipe(name="Cocktail", selling_price_ttc="8.00")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.02")
        page = self.page(syrup)
        self.assertContains(page, "utilisée dans d'autres recettes")
        self.assertNotContains(page, "utilisée dans aucune recette")

    def test_what_a_sale_takes_is_not_printed_on_something_not_sold(self):
        """« Vendu par 1 Unité » on a preparation answers a question nobody
        asked - there is no sale."""
        page = self.page(self.syrup())
        self.assertNotContains(page, "Vendu par")

    def test_a_happy_hour_price_is_not_printed_on_something_not_sold(self):
        """The form refuses the pair now, but an archive or the admin can
        still leave one, and the header printed « Pas vendue directement ·
        Happy hour : 4,80 € » - one page saying both."""
        syrup = self.syrup()
        Recipe.objects.filter(pk=syrup.pk).update(happy_hour_price_ttc=Decimal("4.80"))
        page = self.page(Recipe.objects.get(pk=syrup.pk))
        self.assertNotContains(page, "Happy hour :")


class ASoldRecipeWithNoPriceIsSaidTests(Fixtures, TestCase):
    """A price cleared by mistake on a recipe the till really sells takes
    the margin off its page and off the Recettes tab with nothing saying it
    is odd - and « Marges » goes on costing it, since till revenue comes
    from the till and never from this field. The one figure that would give
    it away is on another page."""

    def test_the_page_says_the_till_sells_it_all_the_same(self):
        from recipes.models import PosProduct

        syrup = self.syrup()
        PosProduct.objects.create(name="Sirop", recipe=syrup)
        page = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": syrup.pk}))
        self.assertContains(page, "la caisse la vend")

    def test_a_preparation_the_till_does_not_sell_says_nothing_of_the_sort(self):
        page = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": self.syrup().pk}))
        self.assertNotContains(page, "la caisse la vend")


class AMistypedPriceAccusesOnlyItselfTests(TestCase):
    def test_a_bad_price_does_not_also_blame_the_quantity(self):
        """Django drops a field that raised from cleaned_data, so « no
        price » and « an unreadable price » looked the same - and a typo
        answered « cette recette n'est pas vendue telle quelle » on a
        quantity box that was perfectly correct."""
        for typo in ("12,50", "abc"):
            with self.subTest(price=typo):
                form = RecipeForm(form_data(selling_price_ttc=typo, sale_quantity="0.15"))
                self.assertFalse(form.is_valid())
                self.assertIn("selling_price_ttc", form.errors)
                self.assertNotIn("sale_quantity", form.errors)
