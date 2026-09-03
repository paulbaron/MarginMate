"""Hand-written sale documents: what was sold off the till.

A line is either a recipe or a stock item sold as itself. Both have to reach
the variance report - a tab settled off the books consumed exactly as much
stock as one rung up on the till - but by different routes: a recipe through
its ingredients, a stock item directly.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase
from django.urls import reverse

from inventory.models import UnitChoices
from inventory.variance import quantities_sold
from recipes.models import SaleDocument, SaleDocumentLine
from recipes.sales import record_sales, sales_between, stock_type_sales_between
from tests.factories import make_ingredient, make_recipe, make_stock_type


class SaleDocumentModelTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Mule", selling_price_ttc="8.50")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.document = SaleDocument.objects.create(sold_on=date(2026, 3, 5), reference="T-1")

    def test_a_line_must_be_exactly_one_of_the_two(self):
        line = SaleDocumentLine(document=self.document, quantity=Decimal("1"))
        with self.assertRaises(ValidationError):
            line.clean()
        line.recipe = self.recipe
        line.stock_type = self.vodka
        with self.assertRaises(ValidationError):
            line.clean()

    def test_the_database_refuses_a_line_with_neither(self):
        with self.assertRaises(IntegrityError):
            SaleDocumentLine.objects.create(document=self.document, quantity=Decimal("1"))

    def test_a_recipe_line_falls_back_to_the_recipes_own_price(self):
        line = SaleDocumentLine.objects.create(
            document=self.document, recipe=self.recipe, quantity=Decimal("3")
        )
        self.assertEqual(line.total_ttc, Decimal("25.50"))

    def test_an_explicit_price_wins(self):
        line = SaleDocumentLine.objects.create(
            document=self.document, recipe=self.recipe, quantity=Decimal("3"),
            unit_price_ttc=Decimal("5.00"),
        )
        self.assertEqual(line.total_ttc, Decimal("15.00"))

    def test_a_stock_item_has_no_default_price(self):
        """There is no "price" on a stock item, only a cost."""
        line = SaleDocumentLine.objects.create(
            document=self.document, stock_type=self.vodka, quantity=Decimal("2")
        )
        self.assertEqual(line.total_ttc, Decimal("0"))

    def test_the_document_total_sums_its_lines(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("2"))
        SaleDocumentLine.objects.create(
            document=self.document, stock_type=self.vodka, quantity=Decimal("1"),
            unit_price_ttc=Decimal("20.00"),
        )
        self.assertEqual(self.document.total_ttc, Decimal("37.00"))


class SaleDocumentsReachTheVarianceReportTests(TestCase):
    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.recipe = make_recipe(name="Mule")
        make_ingredient(self.recipe, stock_type=self.vodka, quantity="0.04", group=0)
        self.document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))

    def test_a_recipe_line_counts_as_a_sale_of_that_recipe(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("10"))
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {self.recipe.pk: 10})

    def test_it_adds_to_till_sales_rather_than_replacing_them(self):
        record_sales([("Mule", date(2026, 3, 5), 20)], source="laddition")
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("10"))
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {self.recipe.pk: 30})

    def test_a_stock_line_is_reported_separately(self):
        SaleDocumentLine.objects.create(
            document=self.document, stock_type=self.vodka, quantity=Decimal("0.7")
        )
        self.assertEqual(
            stock_type_sales_between(date(2026, 3, 1), date(2026, 3, 10)),
            {self.vodka.pk: Decimal("0.7")},
        )
        # ...and never as a recipe sale.
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {})

    def test_the_window_bounds_apply(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("10"))
        self.assertEqual(sales_between(date(2026, 3, 5), date(2026, 3, 10)), {})  # on the opening day
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 4)), {})  # after the close


class QuantitiesSoldTests(TestCase):
    """How much of a stock item was sold has two honest answers when recipes
    offer alternatives, and this reports both rather than picking one."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        self.lime = make_stock_type(name="Citrons", unit=UnitChoices.KILOGRAM)

    def sell(self, recipe, count):
        record_sales([(recipe.name, date(2026, 3, 5), count)])

    def with_alternatives(self):
        recipe = make_recipe(name="Mule")
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=self.gin, quantity="0.04", group=0)
        return recipe

    def test_an_ingredient_with_no_alternatives_is_exact(self):
        recipe = make_recipe(name="Caipi")
        make_ingredient(recipe, stock_type=self.lime, quantity="0.05", group=0)
        self.sell(recipe, 100)

        sold = quantities_sold()[self.lime.pk]
        self.assertEqual(sold.exact, Decimal("5.00"))
        self.assertEqual(sold.shared, Decimal("0"))
        self.assertFalse(sold.is_ambiguous)

    def test_alternatives_are_reported_as_shared_not_split(self):
        """Halving it between the two would read as a measurement."""
        self.sell(self.with_alternatives(), 100)

        sold = quantities_sold()
        for stock_type in (self.vodka, self.gin):
            with self.subTest(stock_type=stock_type.name):
                self.assertEqual(sold[stock_type.pk].exact, Decimal("0"))
                self.assertEqual(sold[stock_type.pk].shared, Decimal("4.00"))
                self.assertTrue(sold[stock_type.pk].is_ambiguous)
                self.assertEqual(sold[stock_type.pk].pool_partners, 1)

    def test_a_direct_sale_is_always_exact(self):
        """Sold as itself there is nothing to be ambiguous about, even for an
        item that is an alternative elsewhere."""
        self.with_alternatives()
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))
        SaleDocumentLine.objects.create(document=document, stock_type=self.vodka, quantity=Decimal("0.7"))

        self.assertEqual(quantities_sold()[self.vodka.pk].exact, Decimal("0.7"))

    def test_exact_and_shared_add_up_to_the_upper_bound(self):
        self.sell(self.with_alternatives(), 100)
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))
        SaleDocumentLine.objects.create(document=document, stock_type=self.vodka, quantity=Decimal("1"))

        self.assertEqual(quantities_sold()[self.vodka.pk].upper_bound, Decimal("5.00"))

    def test_an_item_that_was_never_sold_is_absent(self):
        self.assertNotIn(self.vodka.pk, quantities_sold())

    def test_the_stock_page_renders_the_column(self):
        self.sell(self.with_alternatives(), 100)
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vendu")


class SaleDocumentPageTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Mule", selling_price_ttc="8.50")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)

    def payload(self, rows):
        data = {
            "sold_on": "2026-03-05",
            "reference": "",
            "note": "",
            "lines-TOTAL_FORMS": str(len(rows)),
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"lines-{index}-{key}"] = str(value)
        return data

    def test_a_document_can_hold_both_kinds_of_line(self):
        response = self.client.post(
            reverse("recipes:sale_document_create"),
            self.payload(
                [
                    {"source": f"recipe:{self.recipe.pk}", "quantity": "3", "unit_price_ttc": ""},
                    {"source": f"stock:{self.vodka.pk}", "quantity": "0.7", "unit_price_ttc": "20"},
                ]
            ),
        )
        self.assertEqual(response.status_code, 302)
        document = SaleDocument.objects.get()
        self.assertEqual(document.lines.filter(recipe__isnull=False).count(), 1)
        self.assertEqual(document.lines.filter(stock_type__isnull=False).count(), 1)

    def test_a_document_with_no_lines_is_refused(self):
        response = self.client.post(reverse("recipes:sale_document_create"), self.payload([]))
        self.assertContains(response, "Ajoutez au moins une ligne")
        self.assertEqual(SaleDocument.objects.count(), 0)

    def test_a_line_with_no_source_is_refused(self):
        response = self.client.post(
            reverse("recipes:sale_document_create"),
            self.payload([{"source": "", "quantity": "3", "unit_price_ttc": ""}]),
        )
        self.assertContains(response, "Choisissez une recette")

    def test_the_page_renders(self):
        self.assertEqual(self.client.get(reverse("recipes:sale_document_create")).status_code, 200)

    def test_the_date_defaults_to_today(self):
        """A ModelForm seeds self.initial from the instance, so setting
        fields["sold_on"].initial is silently ignored and the box renders
        empty - which is a small thing you notice only by looking."""
        from django.utils import timezone

        from recipes.forms import SaleDocumentForm

        self.assertEqual(SaleDocumentForm()["sold_on"].value(), timezone.localdate())

    def test_documents_appear_on_the_ventes_page(self):
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5), reference="T-9")
        SaleDocumentLine.objects.create(document=document, recipe=self.recipe, quantity=Decimal("2"))
        html = self.client.get(reverse("recipes:sales_list")).content.decode()
        self.assertIn("T-9", html)
        self.assertIn("Mule", html)

    def test_a_document_can_be_deleted(self):
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))
        SaleDocumentLine.objects.create(document=document, recipe=self.recipe, quantity=Decimal("2"))
        self.client.post(reverse("recipes:sale_document_delete", kwargs={"pk": document.pk}))
        self.assertEqual(SaleDocument.objects.count(), 0)
