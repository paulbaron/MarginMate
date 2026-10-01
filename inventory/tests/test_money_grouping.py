"""Money on « Produits & charges » and the inventories is grouped by three.

The owner, 01/10/2026: « des espaces tous les 3 chiffres » - 12345.60 €
reads « 12 345.60 € ». Every amount these pages print goes through the
`money` filter or common.format_money, with a no-break space so a figure
never wraps across two lines; the decimal point stays what the pages already
print. Only what a person reads: a `data-sort` the table script sorts by, and
the figures the stock-take form's script adds up, stay as they were.

Data invented.
"""

import json
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.forms import product_display_name
from inventory.models import StockTakeLineSource, UnitChoices
from inventory.views import _build_price_history_svg
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from tests.test_json_islands import island

D = Decimal
NBSP = "\N{NO-BREAK SPACE}"


class TheListOfArticlesTests(TestCase):
    """Ten bottles of a rare whisky: 12 345.60 € HT, 14 814.72 € TTC."""

    def setUp(self):
        self.whisky = make_stock_type(name="Whisky rare", unit=UnitChoices.UNIT, category="Spiritueux")
        self.supplier = make_supplier(code="CAVE_X", name="Cave Exemple")
        product = make_product(supplier=self.supplier, raw_name="WHISKY RARE 70CL", stock_type=self.whisky)
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 2, 1))
        line = make_invoice_line(invoice=invoice, product=product, quantity=10, total_ht="12345.60", vat_rate=D("0.20"))
        make_movement(stock_type=self.whisky, quantity="10", unit_cost_ht="1234.56", invoice_line=line)

    def test_the_row_its_category_and_the_headline_group_their_thousands(self):
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, f'data-label="Total HT">12{NBSP}345.60 €</td>')
        self.assertContains(response, f'data-label="Total TTC">14{NBSP}814.72 €</td>')
        self.assertContains(response, f"— 12{NBSP}345.60 € HT / 14{NBSP}814.72 € TTC")
        self.assertContains(response, f'<div class="stat-value">12{NBSP}345.60 €</div>')
        self.assertContains(response, f'<div class="stat-value">14{NBSP}814.72 €</div>')
        self.assertNotContains(response, "12345.60 €")

    def test_the_window_note_groups_them_too(self):
        response = self.client.get(reverse("inventory:stock_list"), {"du": "2026-02-01", "au": "2026-02-28"})
        self.assertRegex(response.content.decode(), rf"12{NBSP}345\.60 € HT\s+/ 14{NBSP}814\.72 € TTC\.")

    def test_the_purchases_a_row_opens_group_the_price_and_the_totals(self):
        response = self.client.get(reverse("inventory:stock_type_movements", args=[self.whisky.pk]))
        self.assertContains(response, f">1{NBSP}234.56 €</td>")
        self.assertContains(response, f'data-label="Total HT">12{NBSP}345.60 €</td>')
        self.assertContains(response, f'data-label="Total TTC">14{NBSP}814.72 €</td>')
        # The conversion beside them is a field a form reads back: untouched.
        self.assertContains(response, 'name="stock_equivalent" inputmode="decimal" value="1"')

    def test_a_product_to_classify_says_its_purchase_grouped(self):
        unclassified = make_product(supplier=self.supplier, raw_name="CAISSE DE GRANDS CRUS")
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 3, 1))
        make_invoice_line(invoice=invoice, product=unclassified, quantity=1, total_ht="1234.50")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, f"1{NBSP}234.50 € HT")


class ThePriceCurveTests(TestCase):
    """The tooltip (`data-value`, which static/js/charts.js shows as it is)
    and the axis are read; the coordinates are geometry."""

    def setUp(self):
        self.svg = _build_price_history_svg([(date(2026, 1, 5), D("1234.5")), (date(2026, 2, 5), D("2345.6789"))])

    def test_the_tooltips_group_the_price_and_keep_four_decimals(self):
        self.assertIn(f'data-value="1{NBSP}234.5000 €"', self.svg)
        self.assertIn(f'data-value="2{NBSP}345.6789 €"', self.svg)

    def test_the_axis_groups_its_two_bounds(self):
        self.assertIn(f">2{NBSP}345.68 €</text>", self.svg)
        self.assertIn(f">1{NBSP}234.50 €</text>", self.svg)

    def test_the_coordinates_are_left_alone(self):
        self.assertIn('<polyline points="55.0,190.0 620.0,20.0"', self.svg)
        self.assertIn('<circle class="chart-point" cx="55.0" cy="190.0"', self.svg)


class TheChargesTests(TestCase):
    """A rent of 1 000.00 € HT a month, 1 200.00 € TTC."""

    def setUp(self):
        self.landlord = make_supplier(code="BAIL_X", name="Bailleur Exemple", expenses_only=True)
        rent = make_product(supplier=self.landlord, raw_name="LOYER", is_expense=True)
        for month in (1, 2):
            bill = make_invoice(supplier=self.landlord, invoice_date=date(2026, month, 5))
            make_invoice_line(invoice=bill, product=rent, total_ht="1000", vat_rate=D("0.20"))
        self.window = {"du": "2026-01-01", "au": "2026-12-31"}

    def test_the_fold_its_row_and_the_headline_group_the_total(self):
        response = self.client.get(reverse("inventory:stock_list"), self.window)
        self.assertContains(response, f"2{NBSP}400.00 € TTC du 01/01/2026 au 31/12/2026")
        self.assertContains(response, f'<td class="num" data-label="Total TTC">2{NBSP}400.00 €</td>')
        self.assertContains(response, f'<div class="stat-value">2{NBSP}400.00 €</div>')

    def test_the_documents_a_row_opens_group_every_amount(self):
        response = self.client.get(reverse("inventory:charge_supplier_documents", args=[self.landlord.pk]))
        self.assertContains(response, f'data-label="HT">1{NBSP}000.00 €</td>', count=2)
        self.assertContains(response, f'data-label="TTC">1{NBSP}200.00 €</td>', count=2)
        self.assertContains(response, f'data-label="TTC">2{NBSP}400.00 €</td>')
        self.assertContains(response, 'data-label="TVA">200.00 €</td>', count=2)

    def test_its_curve_groups_what_each_document_cost(self):
        response = self.client.get(reverse("inventory:charge_supplier_history", args=[self.landlord.pk]))
        self.assertContains(response, f'data-value="1{NBSP}200.0000 €"', count=2)


def at(day: int) -> datetime:
    return timezone.make_aware(datetime(2026, 3, day, 12, 0))


class TheStockTakeWindowTests(TestCase):
    """12 L of a dear spirit counted, 2 L left a month later, half of the
    10 L that went explained by sales: 5 L missing at 2 500 € the litre."""

    def setUp(self):
        self.spirit = make_stock_type(name="Cognac hors d'âge", unit=UnitChoices.LITRE)
        self.opening = make_stock_take(taken_at=at(1))
        self.closing = make_stock_take(taken_at=at(31))
        for take, quantity in ((self.opening, "12"), (self.closing, "2")):
            make_stock_take_line(
                stock_take=take,
                product=None,
                stock_type=self.spirit,
                counted_quantity=quantity,
                unit=UnitChoices.LITRE,
            )
        make_movement(stock_type=self.spirit, quantity="12", unit_cost_ht="2500", occurred_on=date(2026, 2, 1))
        recipe = make_recipe(name="Dégustation")
        make_ingredient(recipe, stock_type=self.spirit, quantity="0.05", group=0)
        record_sales([("Dégustation", date(2026, 3, 5), 100)])

    def test_the_missing_value_is_grouped_and_its_sort_key_is_not(self):
        response = self.client.get(reverse("inventory:stock_list"), {"inventaire": self.closing.pk})
        self.assertContains(response, f'<span class="stock-missing">12{NBSP}500.00 €</span>')
        # The sort key the table's script reads stays a bare number.
        self.assertRegex(response.content.decode(), r'data-label="Manquant \(HT\)" data-sort="12500\.0+"')
        self.assertContains(response, f"— manquant : 12{NBSP}500.00 € HT")
        self.assertContains(response, f'<div class="stat-value bad">12{NBSP}500.00 €</div>')

    def test_the_variance_page_groups_its_totals_and_its_price(self):
        response = self.client.get(reverse("inventory:stock_take_variance", args=[self.closing.pk]))
        # 10 L left the shelf, 5 L sold, 1 L of estimated loss: 4 L at 2 500 €.
        self.assertContains(response, f"Manquant : au moins 10{NBSP}000.00 €")
        self.assertContains(response, f'<span class="filter-tab-figure">10{NBSP}000.00 €</span>', count=2)
        # The two tabs, the heading and the group's own « Valeur mini ».
        self.assertContains(response, f"10{NBSP}000.00 €", count=4)
        self.assertContains(response, f"(2{NBSP}500.00 € / L")
        self.assertNotContains(response, "10000.00 €")


class AStockTakeTests(TestCase):
    """A count worth 1 500.00 € HT, priced from a purchase at 1 234.5678 €."""

    def setUp(self):
        supplier = make_supplier(code="CAVE_Y", name="Cave Exemple")
        self.wine = make_stock_type(name="Grand cru", unit=UnitChoices.UNIT)
        self.product = make_product(supplier=supplier, raw_name="GRAND CRU 75CL", stock_type=self.wine)
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 1, 10))
        purchase = make_invoice_line(invoice=invoice, product=self.product, quantity=2, total_ht="2469.1356")
        self.take = make_stock_take(taken_at=datetime(2026, 6, 30, 12, 0))
        self.line = make_stock_take_line(
            stock_take=self.take, product=self.product, counted_quantity="1", value_ht="1500.00"
        )
        StockTakeLineSource.objects.create(
            stock_take_line=self.line, invoice_line=purchase, quantity_used=D("1"), unit_cost_ht=D("1234.5678")
        )

    def test_its_page_groups_the_total_the_line_and_the_price_it_came_from(self):
        response = self.client.get(reverse("inventory:stock_take_detail", args=[self.take.pk]))
        self.assertContains(response, f'<span class="stat-value">1{NBSP}500.00 €</span>')
        self.assertContains(response, f'<td class="num">1{NBSP}500.00 €</td>')
        self.assertContains(response, f"à 1{NBSP}234.5678 €")

    def test_the_list_of_inventories_groups_it(self):
        response = self.client.get(reverse("inventory:stock_take_list"))
        self.assertContains(response, f'<td class="num">1{NBSP}500.00 €</td>')

    def test_what_the_form_s_script_adds_up_stays_a_plain_number(self):
        """The running total is summed by the page's script (parseFloat),
        and drawn by Intl.NumberFormat("fr-FR"), which groups by itself."""
        response = self.client.get(reverse("inventory:stock_take_update", args=[self.take.pk]))
        self.assertEqual(island(response.content.decode(), "saved-values"), {str(self.line.pk): "1500.00"})
        priced = self.client.get(
            reverse("inventory:value_stock_take_line"),
            {
                "entry": product_display_name(self.product),
                "quantity": "2",
                "unit": UnitChoices.UNIT,
                "as_of": "2026-06-30",
            },
        )
        data = json.loads(priced.content)
        self.assertEqual((data["value_ht"], data["unit_cost_ht"]), ("2469.14", "1234.57"))
