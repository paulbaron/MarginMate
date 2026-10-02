"""What drawing « Produits & charges » and the stock-take pages costs - and
that each cheaper way prints exactly what the dearer one did.

* `views.pk_url` reverses a route once and formats every pk into it: the
  same string as `reverse()`, under a script prefix too, and `reverse()`
  itself wherever the marker shows up twice;
* the catalogue's rows carry their id, unit and four URLs from the view:
  every attribute is still the one the `{% url %}` tags printed;
* the all-time scan reads no dates: it adds up exactly what a window
  holding every date adds up;
* `StockType.current_unit_cost_ht` is worked out once per prefetched list,
  and never served once that list has been replaced or dropped;
* the stock-take form, its datalist and the charges fold cost the same
  number of queries whatever they hold.

Data invented.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.db.models import prefetch_related_objects
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import get_script_prefix, reverse, set_script_prefix
from django.utils import timezone

from inventory import views
from inventory.forms import product_display_name, stock_take_entry_lookup, stock_type_entry_name
from inventory.models import MovementKind, StockMovement, StockType, UnitChoices
from inventory.tests.test_purchase_window import PurchaseWindowTestCase
from inventory.views import charge_suppliers, pk_url
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)

D = Decimal

ROUTES = (
    "inventory:stock_type_movements",
    "inventory:stock_type_update",
    "inventory:stock_type_price_history",
    "inventory:stock_type_delete",
    "inventory:edit_product_conversion",
    "inventory:remove_product",
    "invoices:invoice_detail",
)


class PkUrlTests(SimpleTestCase):
    def test_it_is_reverse_for_every_pk(self):
        for name in ROUTES:
            url = pk_url(name)
            for pk in (1, 9, 10, 2147483647, 123456789012):
                with self.subTest(name=name, pk=pk):
                    self.assertEqual(url(pk), reverse(name, args=[pk]))

    def test_it_keeps_the_script_prefix(self):
        before = get_script_prefix()
        set_script_prefix("/bar-exemple/")
        try:
            for name in ROUTES:
                self.assertEqual(pk_url(name)(42), reverse(name, args=[42]))
                self.assertTrue(pk_url(name)(42).startswith("/bar-exemple/"))
        finally:
            set_script_prefix(before)

    def test_a_marker_found_twice_falls_back_on_reverse(self):
        """A prefix holding the marker's digits: splitting there would put
        the pk in the prefix, so every URL is reversed as before."""
        before = get_script_prefix()
        set_script_prefix("/9/")
        try:
            with mock.patch.object(views, "_PK_MARKER", 9):
                url = pk_url("inventory:stock_type_update")
                self.assertEqual(url(5), reverse("inventory:stock_type_update", args=[5]))
                self.assertTrue(url(5).startswith("/9/"))
        finally:
            set_script_prefix(before)


class CatalogueRowsTests(PurchaseWindowTestCase):
    """Every attribute a row used to get from a `{% url %}` or a localised
    id, read off the page and compared with `reverse()`."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.cups = make_stock_type(name="Gobelets", unit=UnitChoices.UNIT, category="Matériel")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.buy(self.cups, date(2026, 2, 12), quantity="100", total_ht="15")

    def assert_rows(self, html, query="", columns="7"):
        for stock_type, unit in ((self.vodka, "Litre"), (self.cups, "Unité")):
            pk = stock_type.pk
            for attribute in (
                f'data-toggle="details-{pk}"',
                f'data-stock-type-id="{pk}"',
                f'data-movements-url="{reverse("inventory:stock_type_movements", args=[pk])}{query}"',
                f'<a href="{reverse("inventory:stock_type_update", args=[pk])}">Modifier</a>',
                f'data-price-history-url="{reverse("inventory:stock_type_price_history", args=[pk])}"',
                f'data-target="price-history-{pk}"',
                f'action="{reverse("inventory:stock_type_delete", args=[pk])}"',
                f'<tr id="price-history-{pk}" data-child-row',
                f'<tr id="details-{pk}" data-child-row',
                f'data-child-of="{pk}" hidden>',
                f'<td data-label="Unité">{unit}</td>',
            ):
                with self.subTest(stock_type=stock_type.name, attribute=attribute):
                    self.assertIn(attribute, html)
        self.assertIn(f'<td colspan="{columns}"', html)
        self.assertNotIn("{%", html)

    def test_all_time(self):
        self.assert_rows(self.page().content.decode())

    def test_a_window_rides_on_the_rows_it_opens(self):
        # The query is HTML-escaped where it is printed, as before.
        self.assert_rows(
            self.page(du="2026-02-01", au="2026-02-28").content.decode(), "?du=2026-02-01&amp;au=2026-02-28"
        )

    def test_under_a_stock_take(self):
        take = make_stock_take(taken_at=datetime(2026, 3, 1, 12, 0))
        make_stock_take_line(stock_take=take, stock_type=self.vodka, counted_quantity="2", unit=UnitChoices.LITRE)
        self.assert_rows(self.page(inventaire=take.pk).content.decode(), columns="10")


class AllTimeScanTests(PurchaseWindowTestCase):
    """With no window the scan fetches no date at all: what it adds up must
    be what a window holding every date adds up - a delivery dated by its
    invoice, one by its own date, a correction typed by hand (dated by when
    it was typed), a loss, a receipt printing its TTC and a promotion."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.syrup = make_stock_type(name="Sirop", unit=UnitChoices.LITRE, category="Sirops")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240.33", vat_rate="0.20")
        moved = self.buy(self.vodka, date(2025, 11, 3), quantity="6", total_ht="121.17", vat_rate="0.20")
        StockMovement.objects.filter(pk=moved.pk).update(occurred_on=date(2025, 11, 20))
        bought = self.buy(self.syrup, date(2026, 1, 5), quantity="3", total_ht="11.37", vat_rate="0.055")
        bought.invoice_line.printed_ttc = D("12.00")
        bought.invoice_line.discount_ttc = D("0.40")
        bought.invoice_line.save()
        make_movement(stock_type=self.vodka, quantity="-1", unit_cost_ht="20.0275", kind=MovementKind.LOSS)
        make_movement(stock_type=self.syrup, quantity="0.5", unit_cost_ht="3.79", kind=MovementKind.CORRECTION)

    def figures(self, response):
        rows = self.rows(response)
        return (
            {name: (row["quantity"], row["value_ht"], row["value_ttc"]) for name, row in rows.items()},
            response.context["total_value_ht"],
            response.context["total_value_ttc"],
        )

    def test_all_time_adds_up_what_every_date_holds(self):
        everything = self.figures(self.page(du="2000-01-01", au="2100-12-31"))
        self.assertEqual(self.figures(self.page()), everything)
        self.assertEqual(everything[0]["Sirop"], (D("3"), D("11.37"), D("11.60")))

    def scan(self, **parameters):
        """The catalogue's own scan: the one query reading every movement's
        line amounts."""
        with CaptureQueriesContext(connection) as queries:
            self.page(**parameters)
        (sql,) = [
            query["sql"]
            for query in queries
            if 'FROM "inventory_stockmovement"' in query["sql"]
            and '"invoices_invoiceline"."discount_ttc"' in query["sql"]
        ]
        return sql

    def test_only_a_window_reads_the_dates(self):
        self.assertNotIn('"invoices_invoice"', self.scan())
        self.assertNotIn('"inventory_stockmovement"."occurred_on"', self.scan())
        windowed = self.scan(du="2026-02-01", au="2026-02-28")
        self.assertIn('"invoices_invoice"."invoice_date"', windowed)
        self.assertIn('"inventory_stockmovement"."occurred_on"', windowed)


class UnitCostMemoTests(TestCase):
    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        make_movement(stock_type=self.vodka, quantity="12", unit_cost_ht="20.3333")
        make_movement(stock_type=self.vodka, quantity="0.700", unit_cost_ht="19.9999")
        make_movement(stock_type=self.vodka, quantity="-1", unit_cost_ht="20.0001", kind=MovementKind.LOSS)
        self.empty = make_stock_type(name="Rien", unit=UnitChoices.UNIT)
        self.even = make_stock_type(name="Rendu", unit=UnitChoices.UNIT)
        make_movement(stock_type=self.even, quantity="2", unit_cost_ht="5")
        make_movement(stock_type=self.even, quantity="-2", unit_cost_ht="5", kind=MovementKind.CORRECTION)

    @staticmethod
    def by_hand(stock_type) -> Decimal:
        movements = list(StockMovement.objects.filter(stock_type=stock_type))
        quantity = sum((m.quantity for m in movements), start=Decimal("0"))
        value = sum((m.quantity * m.unit_cost_ht for m in movements), start=Decimal("0"))
        return value / quantity if quantity else Decimal("0")

    def prefetched(self, *stock_types):
        fresh = list(StockType.objects.filter(pk__in=[st.pk for st in stock_types]).order_by("pk"))
        prefetch_related_objects(fresh, "movements")
        return fresh

    def test_a_prefetched_item_costs_what_an_unprefetched_one_does(self):
        for prefetched in self.prefetched(self.vodka, self.empty, self.even):
            with self.subTest(prefetched.name):
                unprefetched = StockType.objects.get(pk=prefetched.pk)
                self.assertEqual(prefetched.current_unit_cost_ht, unprefetched.current_unit_cost_ht)
                self.assertEqual(prefetched.current_unit_cost_ht, self.by_hand(prefetched))
        self.assertEqual(StockType.objects.get(pk=self.empty.pk).current_unit_cost_ht, Decimal("0"))
        self.assertEqual(StockType.objects.get(pk=self.even.pk).current_unit_cost_ht, Decimal("0"))

    def test_asked_again_it_adds_nothing_up_again(self):
        (vodka,) = self.prefetched(self.vodka)
        sums = []
        original = StockType.current_quantity

        def counted(stock_type):
            sums.append(stock_type.pk)
            return original.fget(stock_type)

        with mock.patch.object(StockType, "current_quantity", property(counted)), self.assertNumQueries(0):
            answers = {vodka.current_unit_cost_ht for _ in range(25)}
        self.assertEqual(answers, {self.by_hand(self.vodka)})
        self.assertEqual(sums, [vodka.pk])

    def test_a_movement_added_through_the_item_is_counted(self):
        (vodka,) = self.prefetched(self.vodka)
        before = vodka.current_unit_cost_ht
        vodka.movements.create(quantity=D("10"), unit_cost_ht=D("31.5"))
        self.assertNotEqual(vodka.current_unit_cost_ht, before)
        self.assertEqual(vodka.current_unit_cost_ht, self.by_hand(self.vodka))

    def test_a_new_prefetch_is_counted(self):
        (vodka,) = self.prefetched(self.vodka)
        before = vodka.current_unit_cost_ht
        make_movement(stock_type=self.vodka, quantity="10", unit_cost_ht="31.5")
        vodka.refresh_from_db()
        prefetch_related_objects([vodka], "movements")
        self.assertNotEqual(vodka.current_unit_cost_ht, before)
        self.assertEqual(vodka.current_unit_cost_ht, self.by_hand(self.vodka))

    def test_without_a_prefetch_every_ask_reads_the_ledger(self):
        vodka = StockType.objects.get(pk=self.vodka.pk)
        before = vodka.current_unit_cost_ht
        make_movement(stock_type=self.vodka, quantity="10", unit_cost_ht="31.5")
        self.assertNotEqual(vodka.current_unit_cost_ht, before)
        self.assertEqual(vodka.current_unit_cost_ht, self.by_hand(self.vodka))


def queries_to(fetch) -> int:
    with CaptureQueriesContext(connection) as queries:
        fetch()
    return len(queries)


class StockTakeFormCostTests(TestCase):
    """Every row names its product with its supplier: read per row, that was
    two queries a line - 209 of the 220 an inventory of 104 lines took."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.suppliers = [make_supplier(name=f"Grossiste {letter}") for letter in "ABC"]
        self.take = make_stock_take(taken_at=datetime(2026, 6, 30, 12, 0))
        self.count = 0

    def add_lines(self, how_many):
        for _ in range(how_many):
            self.count += 1
            if self.count % 4 == 0:
                stock_type = make_stock_type(name=f"Article {self.count:03d}", unit=UnitChoices.LITRE)
                make_stock_take_line(
                    stock_take=self.take, stock_type=stock_type, counted_quantity="1", unit=UnitChoices.LITRE
                )
                continue
            product = make_product(
                supplier=self.suppliers[self.count % 3],
                raw_name=f"PRODUIT {self.count:03d}",
                stock_type=self.vodka,
                stock_equivalent="0.7",
            )
            make_stock_take_line(stock_take=self.take, product=product, counted_quantity="2", value_ht="10.00")

    def page(self):
        return self.client.get(reverse("inventory:stock_take_update", args=[self.take.pk]))

    def test_three_times_the_lines_cost_no_more_queries(self):
        self.add_lines(8)
        few = queries_to(self.page)
        self.add_lines(16)
        self.assertEqual(queries_to(self.page), few)

    def test_every_row_still_names_what_it_counted(self):
        self.add_lines(8)
        html = self.page().content.decode()
        for line in self.take.lines.select_related("product__supplier", "stock_type"):
            name = product_display_name(line.product) if line.product_id else stock_type_entry_name(line.stock_type)
            self.assertIn(f'value="{name}"', html.replace("&#x27;", "'"))


class EntryLookupCostTests(TestCase):
    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.cups = make_stock_type(name="Gobelets", unit=UnitChoices.UNIT)
        self.supplier = make_supplier(name="Grossiste Exemple")
        self.number = 0

    def add_products(self, how_many):
        for _ in range(how_many):
            self.number += 1
            product = make_product(
                supplier=self.supplier,
                raw_name=f"PRODUIT {self.number:03d}",
                stock_type=self.vodka if self.number % 2 else self.cups,
                stock_equivalent="0.7",
            )
            invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, self.number % 28 + 1))
            make_invoice_line(invoice=invoice, product=product, quantity=2, total_volume="1.4")

    def test_four_queries_whatever_the_products(self):
        self.add_products(5)
        with self.assertNumQueries(4):  # products, counting ratios, first purchases, stock types
            stock_take_entry_lookup()
        self.add_products(15)
        with self.assertNumQueries(4):
            entries = stock_take_entry_lookup()
        entry = entries["PRODUIT 001 — Grossiste Exemple"]
        self.assertEqual(entry["kind"], "product")
        self.assertEqual(
            entry["unit_choices"], [[UnitChoices.UNIT, "Unité (bouteilles/packs)"], ["L", "Litre (mesuré directement)"]]
        )
        self.assertEqual(entries["PRODUIT 002 — Grossiste Exemple"]["unit_choices"], [[UnitChoices.UNIT, "Unité"]])


class ChargeSuppliersCostTests(TestCase):
    def setUp(self):
        self.number = 0

    def add_supplier(self, documents, lines_each):
        self.number += 1
        supplier = make_supplier(name=f"Charge Exemple {self.number}", expenses_only=True)
        for document in range(documents):
            invoice = make_invoice(supplier=supplier, invoice_date=timezone.localdate().replace(day=1))
            for line in range(lines_each):
                product = make_product(supplier=supplier, raw_name=f"POSTE {document}-{line}", is_expense=True)
                make_invoice_line(
                    invoice=invoice, product=product, total_ht="16.66", vat_rate=D("0.20"), printed_ttc=D("19.99")
                )
        return supplier

    def test_four_queries_whatever_the_documents_and_their_lines(self):
        self.add_supplier(documents=1, lines_each=1)
        with self.assertNumQueries(4):  # suppliers, lines, the window's documents, every document
            charge_suppliers()
        self.add_supplier(documents=3, lines_each=2)
        self.add_supplier(documents=2, lines_each=3)
        with self.assertNumQueries(4):
            rows = charge_suppliers()
        totals = {row["supplier"].name: (row["documents"], row["documents_all"], row["total_ttc"]) for row in rows}
        self.assertEqual(totals["Charge Exemple 2"], (3, 3, D("119.94")))
        # A charge item opens on its own product, which is read whole.
        (item, *_) = next(row for row in rows if row["supplier"].name == "Charge Exemple 3")["charge_items"]
        with self.assertNumQueries(0):
            self.assertTrue(item["product"].raw_name.startswith("POSTE"))
            self.assertTrue(item["product"].is_expense)

    def test_a_supplier_with_no_document_is_no_row(self):
        make_supplier(name="Charge Muette", expenses_only=True)
        self.add_supplier(documents=1, lines_each=1)
        self.assertEqual([row["supplier"].name for row in charge_suppliers()], ["Charge Exemple 1"])


class MovementPanelLinksTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(name="Grossiste Exemple")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.lines = []
        for day in (3, 17):
            product = make_product(supplier=self.supplier, raw_name=f"VODKA {day}", stock_type=self.vodka)
            invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 2, day))
            line = make_invoice_line(invoice=invoice, product=product, quantity=6, total_ht="60")
            make_movement(stock_type=self.vodka, quantity="4.2", unit_cost_ht="14.2857", invoice_line=line)
            self.lines.append(line)
        # A correction typed by hand: no line, so no link at all.
        make_movement(stock_type=self.vodka, quantity="-0.7", unit_cost_ht="14.2857", kind=MovementKind.CORRECTION)

    def test_each_line_links_where_reverse_says(self):
        response = self.client.get(reverse("inventory:stock_type_movements", args=[self.vodka.pk]))
        html = response.content.decode()
        for line in self.lines:
            self.assertIn(f'<a href="{reverse("invoices:invoice_detail", args=[line.invoice_id])}" title=', html)
            self.assertIn(f'action="{reverse("inventory:edit_product_conversion", args=[line.product_id])}"', html)
            self.assertIn(f'action="{reverse("inventory:remove_product", args=[line.product_id])}"', html)
        self.assertEqual(html.count('class="conversion-form"'), 2)
        self.assertIn("Correction manuelle", html)
        self.assertEqual(html.count("Prix (HT) / Litre"), 4)  # the header and three cards


class LiveValueCostTests(TestCase):
    """A row priced as it is typed (value_stock_take_line) resolves its name
    and prices it - it never asks when anything was first delivered, and
    more products cost it no more queries."""

    def setUp(self):
        self.supplier = make_supplier(name="Grossiste Exemple")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.number = 0
        self.product = self.add_products(1)

    def add_products(self, how_many):
        for _ in range(how_many):
            self.number += 1
            product = make_product(
                supplier=self.supplier,
                raw_name=f"VODKA {self.number:03d}",
                stock_type=self.vodka,
                stock_equivalent="0.7",
            )
            invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 3, self.number % 28 + 1))
            make_invoice_line(invoice=invoice, product=product, quantity=6, total_ht="60")
        return product

    def priced(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse("inventory:value_stock_take_line"),
                {"entry": product_display_name(self.product), "quantity": "2", "unit": UnitChoices.UNIT},
            )
        self.assertTrue(response.json()["ok"])
        return [query["sql"] for query in queries]

    def test_no_first_purchase_is_read(self):
        self.assertFalse([sql for sql in self.priced() if 'MIN("invoices_invoice"."invoice_date")' in sql])

    def test_more_products_cost_no_more_queries(self):
        few = len(self.priced())
        self.add_products(12)
        self.assertEqual(len(self.priced()), few)
