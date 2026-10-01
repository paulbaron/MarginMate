"""Every amount « Recettes & ventes » prints has its thousands grouped by a
no-break space (the owner, 01/10/2026: « 10000€ -> 10 000€ »): a recipe's
page, its pie's tooltip, the recipes tab, the sale invoices, the import's
log - and nothing a script or a form reads back.

Data invented throughout: names and prices are made up.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from recipes.management.commands.laddition_backfill_revenue import euros
from recipes.models import Recipe, SaleDocument, SaleDocumentLine, SalesImportJob
from recipes.pos.laddition_xlsx import parse_rows
from recipes.tasks import import_laddition_sales_task
from recipes.tests.test_pos_revenue import HEADER, line
from tests.factories import make_ingredient, make_priced_stock_type, make_recipe

NBSP = "\N{NO-BREAK SPACE}"


def magnum() -> Recipe:
    """A recipe sold 3 000,00 € TTC (2 500,00 € HT at 20 %), happy hour
    2 400,00 €, whose one ingredient is a 1 200,00 € bottle - or, as its
    « OU », a 1 500,00 € one."""
    recipe = make_recipe(
        name="Magnum Exemple",
        selling_price_ttc="3000.00",
        vat_rate="0.20",
        happy_hour_price_ttc=Decimal("2400.00"),
    )
    make_ingredient(
        recipe, stock_type=make_priced_stock_type(name="Bouteille A", unit_cost_ht="1200", quantity="1"), group=0
    )
    make_ingredient(
        recipe, stock_type=make_priced_stock_type(name="Bouteille B", unit_cost_ht="1500", quantity="1"), group=0
    )
    return recipe


class RecipePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.recipe = magnum()

    def html(self) -> str:
        response = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": self.recipe.pk}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_prices_are_grouped(self):
        html = self.html()

        self.assertIn(f"Prix de vente : 3{NBSP}000.00 € TTC", html)
        self.assertIn(f"(2{NBSP}500.00 € HT, TVA 20 %)", html)
        self.assertIn(f"Happy hour : 2{NBSP}400.00 € TTC", html)
        self.assertIn(f"(2{NBSP}000.00 € HT)", html)

    def test_the_cost_range_and_the_margin_are_grouped(self):
        html = self.html()

        self.assertIn(f"1{NBSP}200.00 – 1{NBSP}500.00 €", html)
        self.assertIn(f'Marge HT : <strong style="color:var(--green);">1{NBSP}300.00 €</strong>', html)

    def test_the_ingredient_table_groups_four_places_as_well_as_two(self):
        cells = re.findall(r'<td class="num">([^<]*)</td>', self.html())

        self.assertIn(f"1{NBSP}200.0000 €", cells)
        self.assertIn(f"1{NBSP}200.00 €", cells)

    def test_the_pies_tooltip_is_grouped(self):
        """charts.js shows `data-value` as it is: it is text, not a figure."""
        self.assertIn(f'data-value="1{NBSP}200.00 € · 100.0 %"', self.html())


class RecipesTabTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        magnum()

    def test_the_cost_and_the_price_are_grouped_and_the_sort_key_is_not(self):
        html = self.client.get(reverse("recipes:recipe_list")).content.decode()
        row = next(row for row in re.findall(r"<tr>.*?</tr>", html, flags=re.DOTALL) if "Magnum Exemple" in row)

        self.assertIn(f"1{NBSP}200.00 – 1{NBSP}500.00 €", row)
        self.assertIn(f"3{NBSP}000.00 €", row)
        # datatable.js sorts the price column by this, as a number.
        self.assertIn('data-sort="3000.00"', row)


class SalesTabTests(TestCase):
    def test_a_sale_invoice_total_is_grouped(self):
        recipe = make_recipe(name="Cocktail Exemple", selling_price_ttc="9.00")
        document = SaleDocument.objects.create(sold_on=timezone.localdate() - timedelta(days=2), reference="Mariage")
        SaleDocumentLine.objects.create(document=document, recipe=recipe, quantity="2", unit_price_ttc="1250.00")

        response = self.client.get(reverse("recipes:sales_list"))

        self.assertContains(response, f'<td class="num">2{NBSP}500.00 €</td>')

    def test_the_import_log_shown_on_the_page_is_grouped(self):
        """The job's log is French read on this page: « Recettes lues »
        writes its amounts grouped (tasks._euros), comma decimals kept."""
        export = parse_rows(
            [
                HEADER,
                line("2026-06-01", "Magnum Exemple", "1500.00", "20%"),
                line("2026-06-02", "Magnum Exemple", "2340.00", "20%"),
            ]
        )
        job = SalesImportJob.objects.create(status=SalesImportJob.Status.PENDING)
        with (
            mock.patch("recipes.tasks.download_sales_lines", return_value=["ventes.xlsx"]),
            mock.patch("recipes.tasks.parse_sales_exports", return_value=export),
        ):
            import_laddition_sales_task(job.pk, date(2026, 6, 1), date(2026, 6, 30), download_dir="non-utilisé")
        said = f"Recettes lues : 3{NBSP}840,00 € TTC, 3{NBSP}200,00 € HT."

        job.refresh_from_db()
        self.assertIn(said, job.log)
        self.assertContains(self.client.get(reverse("recipes:sales_list")), said)


class BackfillEurosTests(SimpleTestCase):
    """The backfill commands' one way of writing an amount: it grouped by a
    plain space, alone in the application."""

    def test_the_thousands_are_grouped_by_a_no_break_space(self):
        self.assertEqual(euros(Decimal("1234.5")), f"1{NBSP}234,50 €")
        self.assertEqual(euros("16568684"), f"16{NBSP}568{NBSP}684,00 €")
        self.assertEqual(euros(Decimal("-10000")), f"-10{NBSP}000,00 €")

    def test_an_amount_under_a_thousand_is_as_it_was(self):
        self.assertEqual(euros(Decimal("7.5")), "7,50 €")
        self.assertEqual(euros(Decimal("0")), "0,00 €")
