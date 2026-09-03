"""Tests for getting sales into the app.

Nothing fills this in yet - the till isn't connected, and when it is it might
be an API, a CSV export, or something nobody's thought of. So these tests are
about the SHAPE of the door rather than any particular importer: whatever
turns up, it produces (recipe name, date, count) triples and calls
record_sales, and everything below has to behave sanely when it does.

The two failure modes that matter are both silent, which is why they're
tested hardest: a name that doesn't match any recipe must never be quietly
dropped (it would understate every variance report afterwards), and
re-running an import must never double-count.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from recipes.models import RecipeSale
from recipes.sales import record_sales, sales_between
from tests.factories import make_recipe


class RecordSalesTests(TestCase):
    def setUp(self):
        self.mule = make_recipe(name="Mule")
        self.spritz = make_recipe(name="Spritz")

    def test_records_a_simple_import(self):
        result = record_sales([("Mule", date(2026, 3, 5), 12)])
        self.assertEqual(result.created, 1)
        self.assertEqual(result.unmatched, [])
        sale = RecipeSale.objects.get()
        self.assertEqual((sale.recipe, sale.sold_on, sale.quantity), (self.mule, date(2026, 3, 5), 12))

    def test_matches_recipe_names_case_insensitively(self):
        record_sales([("  mule  ", date(2026, 3, 5), 3)])
        self.assertEqual(RecipeSale.objects.get().recipe, self.mule)

    def test_an_unknown_name_is_reported_not_dropped(self):
        """A till item that matches no recipe has to be visible. Swallowed,
        it would look like stock vanished for no reason."""
        result = record_sales([("Mule", date(2026, 3, 5), 3), ("Café gourmand", date(2026, 3, 5), 8)])
        self.assertEqual(result.created, 1)
        self.assertEqual(result.unmatched, ["Café gourmand"])
        self.assertEqual(RecipeSale.objects.count(), 1)

    def test_no_fuzzy_matching(self):
        """Deliberately strict: a mis-linked sale moves stock consumption
        from one drink to another, and the variance report is only as
        trustworthy as its inputs."""
        result = record_sales([("Mulee", date(2026, 3, 5), 3)])
        self.assertEqual(result.unmatched, ["Mulee"])

    def test_re_importing_a_day_corrects_it_rather_than_doubling_it(self):
        record_sales([("Mule", date(2026, 3, 5), 12)])
        result = record_sales([("Mule", date(2026, 3, 5), 15)])
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.created, 0)
        self.assertEqual(RecipeSale.objects.get().quantity, 15)

    def test_repeated_entries_in_one_import_are_summed(self):
        """A till exporting one row per transaction is a normal shape."""
        record_sales([("Mule", date(2026, 3, 5), 1)] * 7)
        self.assertEqual(RecipeSale.objects.get().quantity, 7)

    def test_different_days_are_separate_rows(self):
        record_sales([("Mule", date(2026, 3, 5), 3), ("Mule", date(2026, 3, 6), 4)])
        self.assertEqual(RecipeSale.objects.count(), 2)

    def test_the_source_is_recorded(self):
        """So a bad import can be found and re-run without guessing which
        rows it wrote."""
        record_sales([("Mule", date(2026, 3, 5), 3)], source="api:lightspeed")
        self.assertEqual(RecipeSale.objects.get().source, "api:lightspeed")

    def test_an_empty_import_does_nothing(self):
        result = record_sales([])
        self.assertEqual((result.created, result.updated, result.unmatched), (0, 0, []))

    def test_zero_sales_are_recordable(self):
        """"We sold none today" is real information, not a missing row."""
        record_sales([("Mule", date(2026, 3, 5), 0)])
        self.assertEqual(RecipeSale.objects.get().quantity, 0)

    def test_the_result_totals_up(self):
        record_sales([("Mule", date(2026, 3, 5), 1)])
        result = record_sales([("Mule", date(2026, 3, 5), 2), ("Spritz", date(2026, 3, 5), 2)])
        self.assertEqual((result.created, result.updated, result.recorded), (1, 1, 2))


class PluggableSourceTests(TestCase):
    """Whatever the till turns out to be, it ends here. These stand in for
    the two shapes most likely to show up, to prove neither needs anything
    from the accounting side."""

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        self.spritz = make_recipe(name="Spritz")

    def test_a_csv_shaped_export(self):
        rows = [
            "date,item,qty",
            "2026-03-05,Mule,12",
            "2026-03-05,Spritz,7",
            "2026-03-06,Mule,9",
        ]
        entries = [
            (item, date.fromisoformat(day), int(qty))
            for day, item, qty in (row.split(",") for row in rows[1:])
        ]
        result = record_sales(entries, source="csv")
        self.assertEqual(result.created, 3)
        self.assertEqual(sales_between(None, date(2026, 3, 6))[self.mule.pk], 21)

    def test_an_api_shaped_payload(self):
        payload = {
            "period": "2026-03-05",
            "items": [{"name": "Mule", "sold": 12}, {"name": "Spritz", "sold": 7}],
        }
        entries = [
            (item["name"], date.fromisoformat(payload["period"]), item["sold"]) for item in payload["items"]
        ]
        result = record_sales(entries, source="api:demo")
        self.assertEqual(result.recorded, 2)

    def test_a_source_that_reports_only_weekly_totals(self):
        """It can post the total against any date in the week - the variance
        report only cares which stock-take window a sale falls into."""
        record_sales([("Mule", date(2026, 3, 8), 84)], source="csv-weekly")
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10))[self.mule.pk], 84)


class SalesWindowTests(TestCase):
    """Which sales belong to which stock-take period."""

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        record_sales(
            [
                ("Mule", date(2026, 2, 28), 1),  # before the opening count
                ("Mule", date(2026, 3, 1), 2),   # ON the opening count day
                ("Mule", date(2026, 3, 5), 4),   # inside
                ("Mule", date(2026, 3, 10), 8),  # ON the closing count day
                ("Mule", date(2026, 3, 11), 16),  # after
            ]
        )

    def test_the_window_excludes_the_opening_day_and_includes_the_closing_day(self):
        """A sale on the day of the opening count already happened before
        that count was taken, so it belongs to the previous period; counting
        it here would charge it twice."""
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {self.mule.pk: 12})

    def test_no_opening_count_means_everything_up_to_the_close(self):
        """The very first period has no earlier count to start from."""
        self.assertEqual(sales_between(None, date(2026, 3, 10)), {self.mule.pk: 15})

    def test_a_window_with_no_sales_is_empty_not_missing(self):
        self.assertEqual(sales_between(date(2026, 3, 5), date(2026, 3, 6)), {})

    def test_sales_of_several_recipes_are_kept_apart(self):
        spritz = make_recipe(name="Spritz")
        record_sales([("Spritz", date(2026, 3, 5), 3)])
        self.assertEqual(
            sales_between(date(2026, 3, 1), date(2026, 3, 10)),
            {self.mule.pk: 12, spritz.pk: 3},
        )


class HappyHourNameTests(TestCase):
    """The till sells "Alcool + soda HH" as its own product, separate from
    "Alcool + Soda". It's the same pour out of the same bottles, so for stock
    purposes it's one recipe - named explicitly on the recipe rather than
    guessed at from an "HH" suffix."""

    def setUp(self):
        self.soda = make_recipe(name="Alcool + Soda", happy_hour_name="Alcool + soda HH")
        self.mule = make_recipe(name="Mule")

    def test_the_happy_hour_name_resolves_to_the_same_recipe(self):
        record_sales([("Alcool + soda HH", date(2026, 3, 5), 3)])
        sale = RecipeSale.objects.get()
        self.assertEqual(sale.recipe, self.soda)
        self.assertEqual(sale.quantity, 3)

    def test_both_names_on_one_day_are_added_together(self):
        """They must be SUMMED, not written then overwritten - aggregating by
        raw name would silently lose every happy-hour sale."""
        record_sales(
            [("Alcool + Soda", date(2026, 3, 5), 20), ("Alcool + soda HH", date(2026, 3, 5), 3)]
        )
        self.assertEqual(RecipeSale.objects.count(), 1)
        self.assertEqual(RecipeSale.objects.get().quantity, 23)

    def test_the_order_they_arrive_in_does_not_matter(self):
        record_sales(
            [("Alcool + soda HH", date(2026, 3, 5), 3), ("Alcool + Soda", date(2026, 3, 5), 20)]
        )
        self.assertEqual(RecipeSale.objects.get().quantity, 23)

    def test_matching_is_case_insensitive(self):
        record_sales([("ALCOOL + SODA HH", date(2026, 3, 5), 3)])
        self.assertEqual(RecipeSale.objects.get().recipe, self.soda)

    def test_different_days_stay_separate(self):
        record_sales(
            [("Alcool + Soda", date(2026, 3, 5), 20), ("Alcool + soda HH", date(2026, 3, 6), 3)]
        )
        self.assertEqual(
            sorted(RecipeSale.objects.values_list("sold_on", "quantity")),
            [(date(2026, 3, 5), 20), (date(2026, 3, 6), 3)],
        )

    def test_a_recipe_without_a_happy_hour_name_is_unaffected(self):
        result = record_sales([("Mule HH", date(2026, 3, 5), 3)])
        self.assertEqual(result.unmatched, ["Mule HH"])

    def test_the_folded_sales_reach_the_variance_engine(self):
        record_sales(
            [("Alcool + Soda", date(2026, 3, 5), 20), ("Alcool + soda HH", date(2026, 3, 5), 3)]
        )
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {self.soda.pk: 23})


class HappyHourNameValidationTests(TestCase):
    """A till name may only ever mean one recipe - otherwise an import has to
    guess, and a wrong guess moves stock consumption between drinks."""

    def test_a_name_already_used_by_another_recipe_is_rejected(self):
        make_recipe(name="Mule")
        clashing = make_recipe(name="Alcool + Soda")
        clashing.happy_hour_name = "Mule"
        with self.assertRaises(ValidationError):
            clashing.full_clean()

    def test_a_happy_hour_name_claimed_by_another_recipe_is_rejected(self):
        make_recipe(name="Alcool + Soda", happy_hour_name="Alcool + soda HH")
        other = make_recipe(name="Autre")
        other.happy_hour_name = "Alcool + soda HH"
        with self.assertRaises(ValidationError):
            other.full_clean()

    def test_repeating_the_recipes_own_name_is_rejected(self):
        recipe = make_recipe(name="Mule")
        recipe.happy_hour_name = "Mule"
        with self.assertRaises(ValidationError):
            recipe.full_clean()

    def test_a_distinct_name_is_accepted(self):
        recipe = make_recipe(name="Mule")
        recipe.happy_hour_name = "Mule HH"
        recipe.full_clean()

    def test_leaving_it_blank_is_fine(self):
        make_recipe(name="Mule").full_clean()

    def test_editing_a_recipe_does_not_clash_with_itself(self):
        recipe = make_recipe(name="Mule", happy_hour_name="Mule HH")
        recipe.selling_price_ttc = Decimal("9.00")
        recipe.full_clean()


class ManualSalesTests(TestCase):
    """Sales the till never saw - a tab settled off the books, a private
    event - typed in by hand and kept apart from imported figures."""

    def setUp(self):
        self.mule = make_recipe(name="Mule")

    def test_a_manual_sale_survives_a_later_import_of_the_same_day(self):
        """The one that matters. Keyed on (recipe, day) alone, an import
        would overwrite the manual row for every day it covers - silently
        deleting the very sales entered BECAUSE the till doesn't know them."""
        record_sales([("Mule", date(2026, 3, 5), 4)], source="manual")
        record_sales([("Mule", date(2026, 3, 5), 20)], source="laddition")

        self.assertEqual(RecipeSale.objects.count(), 2)
        self.assertEqual(RecipeSale.objects.get(source="manual").quantity, 4)
        self.assertEqual(RecipeSale.objects.get(source="laddition").quantity, 20)

    def test_both_sources_are_added_together_for_the_variance_report(self):
        record_sales([("Mule", date(2026, 3, 5), 4)], source="manual")
        record_sales([("Mule", date(2026, 3, 5), 20)], source="laddition")
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {self.mule.pk: 24})

    def test_re_importing_still_corrects_its_own_rows(self):
        record_sales([("Mule", date(2026, 3, 5), 20)], source="laddition")
        record_sales([("Mule", date(2026, 3, 5), 25)], source="laddition")
        self.assertEqual(RecipeSale.objects.get(source="laddition").quantity, 25)
        self.assertEqual(RecipeSale.objects.count(), 1)


class ManualSaleFormTests(TestCase):
    def setUp(self):
        self.mule = make_recipe(name="Mule")

    def post(self, **fields):
        from recipes.forms import ManualSaleForm

        data = {"recipe": self.mule.pk, "sold_on": "2026-03-05", "quantity": 4}
        data.update(fields)
        return ManualSaleForm(data)

    def submit(self, **fields):
        form = self.post(**fields)
        self.assertTrue(form.is_valid(), form.errors)
        return form.save()

    def test_it_saves_under_the_manual_source(self):
        from recipes.forms import MANUAL_SALE_SOURCE

        form = self.post()
        self.assertTrue(form.is_valid(), form.errors)
        sale = form.save()
        self.assertEqual(sale.source, MANUAL_SALE_SOURCE)
        self.assertEqual(sale.quantity, 4)

    def test_re_entering_the_same_day_updates_rather_than_erroring(self):
        """A unique constraint would otherwise reject a correction with an
        unhelpful database error."""
        self.submit()
        self.submit(quantity=9)
        self.assertEqual(RecipeSale.objects.count(), 1)
        self.assertEqual(RecipeSale.objects.get().quantity, 9)

    def test_a_negative_quantity_is_rejected(self):
        self.assertFalse(self.post(quantity=-3).is_valid())


class SalesPageTests(TestCase):
    def setUp(self):
        self.mule = make_recipe(name="Mule")

    def test_the_page_renders(self):
        from django.urls import reverse

        record_sales([("Mule", date(2026, 3, 5), 4)], source="manual")
        response = self.client.get(reverse("recipes:sales_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mule")

    def test_adding_a_sale_through_the_page(self):
        from django.urls import reverse

        response = self.client.post(
            reverse("recipes:sales_list"),
            {"recipe": self.mule.pk, "sold_on": "2026-03-05", "quantity": 7},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RecipeSale.objects.get().quantity, 7)

    def test_an_imported_sale_cannot_be_deleted_from_the_page(self):
        """It's a record of what the till reported; correcting it means
        re-importing, not editing it away."""
        from django.urls import reverse

        record_sales([("Mule", date(2026, 3, 5), 20)], source="laddition")
        sale = RecipeSale.objects.get()
        self.client.post(reverse("recipes:sales_delete", kwargs={"pk": sale.pk}))
        self.assertTrue(RecipeSale.objects.filter(pk=sale.pk).exists())

    def test_a_manual_sale_can_be_deleted(self):
        from django.urls import reverse

        record_sales([("Mule", date(2026, 3, 5), 4)], source="manual")
        sale = RecipeSale.objects.get()
        self.client.post(reverse("recipes:sales_delete", kwargs={"pk": sale.pk}))
        self.assertFalse(RecipeSale.objects.filter(pk=sale.pk).exists())
