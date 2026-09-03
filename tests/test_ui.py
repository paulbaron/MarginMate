"""Tests for the shared UI treatment.

The searching and sorting themselves are done in the browser by
static/js/datatable.js, which these can't execute. What they CAN pin down is
the contract that script relies on - and every one of these has a failure mode
that looks fine on a page you happen to be looking at and is broken on one you
aren't:

  * a table without `data-table` silently has no search box and no sortable
    headers, and nothing about the page looks wrong;
  * a date cell without `data-sort` sorts as the text "31/03/2026", so
    everything from March lands together regardless of year;
  * a continuation row without `data-child-row` is treated as a row in its own
    right and gets separated from the row it explains the moment you sort.
"""

from datetime import date, datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import UnitChoices
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_priced_stock_type,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_supplier,
)


class BaseTemplateTests(TestCase):
    def test_the_table_script_is_loaded_everywhere(self):
        """Every enhanced table depends on it, so it belongs in the base
        template rather than being remembered per page."""
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "js/datatable.js")

    def test_the_stylesheet_is_loaded(self):
        self.assertContains(self.client.get(reverse("inventory:stock_list")), "css/marginmate.css")


class SearchableSortableTableTests(TestCase):
    """Every list page gets a search box and sortable columns."""

    @classmethod
    def setUpTestData(cls):
        supplier = make_supplier(name="Metro")
        stock_type = make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        product = make_product(supplier=supplier, raw_name="VODKA 70CL", stock_type=stock_type)
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 3, 31))
        make_invoice_line(invoice=invoice, product=product, quantity=6, total_ht="72")

        recipe = make_recipe(name="Vodka tonic", selling_price_ttc="8.50")
        make_ingredient(recipe, stock_type=stock_type, quantity="0.04", group=0)
        record_sales([("Vodka tonic", date(2026, 3, 15), 20)])

        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(
            stock_take=take, stock_type=stock_type, counted_quantity="4", unit=UnitChoices.LITRE
        )
        cls.take = take

    def assertEnhancedTable(self, url_name, **kwargs):
        response = self.client.get(reverse(url_name, kwargs=kwargs) if kwargs else reverse(url_name))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "data-table",
            msg_prefix=f"{url_name} has a table with no search or sorting",
        )
        return response

    def test_invoice_list(self):
        self.assertEnhancedTable("invoices:invoice_list")

    def test_invoice_detail(self):
        from invoices.models import Invoice

        self.assertEnhancedTable("invoices:invoice_detail", pk=Invoice.objects.get().pk)

    def test_invoice_type_list(self):
        from tests.factories import make_invoice_type

        make_invoice_type(name="UBA")
        self.assertEnhancedTable("invoices:invoice_type_list")

    def test_recipe_list(self):
        self.assertEnhancedTable("recipes:recipe_list")

    def test_sales_list(self):
        self.assertEnhancedTable("recipes:sales_list")

    def test_pos_product_list(self):
        from recipes.models import PosProduct

        PosProduct.objects.create(name="Mule", total_quantity=12)
        self.assertEnhancedTable("recipes:pos_product_list")

    def test_stock_take_list(self):
        self.assertEnhancedTable("inventory:stock_take_list")

    def test_stock_take_detail(self):
        self.assertEnhancedTable("inventory:stock_take_detail", pk=self.take.pk)

    def test_stock_list_sorts_without_a_second_search_box(self):
        """It already has a server-backed fuzzy search; a second box filtering
        the same rows by a different rule would be worse than none."""
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "data-table-sort-only")
        self.assertContains(response, 'id="stock-search"')


class SortKeyTests(TestCase):
    """Cells whose displayed text doesn't sort correctly must say what does."""

    def test_invoice_dates_carry_an_iso_sort_key(self):
        supplier = make_supplier()
        product = make_product(supplier=supplier)
        for day in (date(2026, 3, 31), date(2025, 4, 1)):
            invoice = make_invoice(supplier=supplier, invoice_date=day)
            make_invoice_line(invoice=invoice, product=product, quantity=1, total_ht="10")

        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(response, 'data-sort="2026-03-31"')
        self.assertContains(response, 'data-sort="2025-04-01"')
        # ...and still READS as a French date.
        self.assertContains(response, "31/03/2026")

    def test_sale_dates_carry_an_iso_sort_key(self):
        make_recipe(name="Mule")
        record_sales([("Mule", date(2026, 3, 5), 4)])
        response = self.client.get(reverse("recipes:sales_list"))
        self.assertContains(response, 'data-sort="2026-03-05"')
        self.assertContains(response, "05/03/2026")


class ChildRowTests(TestCase):
    """Rows that explain the row above them have to travel with it."""

    def test_stock_list_detail_rows_are_marked_as_children(self):
        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "data-child-row")

    def test_the_variance_valuation_note_is_marked_as_a_child(self):
        """"Valorisé en Rhum (…)" belongs to the pool row above it; sorted
        apart, it would sit under an unrelated group and read as its
        valuation."""
        supplier = make_supplier()
        stock_type = make_priced_stock_type(name="Rhum", unit_cost_ht="15", quantity="100")
        make_product(supplier=supplier, stock_type=stock_type, unit=UnitChoices.UNIT, stock_equivalent="0.7")

        opening = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        make_stock_take_line(
            stock_take=opening, stock_type=stock_type, counted_quantity="20", unit=UnitChoices.LITRE
        )
        closing = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(
            stock_take=closing, stock_type=stock_type, counted_quantity="15", unit=UnitChoices.LITRE
        )

        response = self.client.get(reverse("inventory:stock_take_variance", kwargs={"pk": closing.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-child-row")


class PageChromeTests(TestCase):
    def test_list_pages_explain_themselves(self):
        """A subtitle saying what the page is for - this is a tool someone
        comes back to monthly, not daily."""
        for url_name in (
            "inventory:stock_list",
            "invoices:invoice_list",
            "recipes:recipe_list",
            "recipes:sales_list",
            "inventory:stock_take_list",
        ):
            with self.subTest(page=url_name):
                self.assertContains(self.client.get(reverse(url_name)), "page-subtitle")

    def test_the_stock_page_leads_with_its_headline_figures(self):
        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "stat-value")
        self.assertContains(response, "Valeur du stock (HT)")

    def test_an_empty_list_offers_the_next_step(self):
        """An empty state that only says "nothing here" leaves the reader to
        work out what to do about it."""
        response = self.client.get(reverse("recipes:recipe_list"))
        self.assertContains(response, "empty-state")
        self.assertContains(response, reverse("recipes:recipe_create"))

    def test_action_buttons_use_the_shared_class(self):
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(response, 'class="actions"')
        self.assertNotContains(response, 'style="display:flex; gap:0.5rem;"')
