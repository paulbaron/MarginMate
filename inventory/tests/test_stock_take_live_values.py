"""Pricing an inventory while it is being typed, and refusing to count
things that didn't exist yet.

Two rules hold this together:

* **The preview and the save are the same code.** value_stock_take_line goes
  through value_counted_quantity / value_counted_stock_type_quantity as of
  the same date, so what a row shows while typing is what the row is worth
  once stored. A preview computed a second, simpler way is how this codebase
  has shipped silently wrong money before.
* **A date filter is validation, not decoration.** The datalist hides what
  hadn't been delivered yet, but the form refuses it too - a datalist is a
  suggestion the browser is free to ignore.
"""

import json
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from inventory.forms import (
    EntryResolver,
    StockTakeLineFormSet,
    product_display_name,
    stock_take_entry_lookup,
    stock_type_entry_name,
)
from inventory.models import StockTake, UnitChoices
from inventory.services import first_purchase_dates, value_counted_quantity
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)


class FirstPurchaseDateTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(name="Metro")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)

    def product_bought_on(self, name, *iso_dates):
        product = make_product(
            supplier=self.supplier, raw_name=name, stock_type=self.vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        for iso in iso_dates:
            invoice = make_invoice(supplier=self.supplier)
            # Set after creation: make_invoice fills a default in for None,
            # and an undated invoice is exactly what this is testing.
            invoice.invoice_date = date.fromisoformat(iso) if iso else None
            invoice.save(update_fields=["invoice_date"])
            make_invoice_line(invoice=invoice, product=product, quantity=6, total_ht="60")
        return product

    def test_the_earliest_delivery_wins(self):
        product = self.product_bought_on("SOBIESKI", "2026-05-04", "2026-02-11", "2026-08-20")
        self.assertEqual(first_purchase_dates()[product.id], date(2026, 2, 11))

    def test_a_product_with_no_dated_invoice_is_simply_absent(self):
        """We can't prove when it arrived, so there are no grounds to call it
        too new - and hiding it would drop real stock out of a count."""
        product = self.product_bought_on("SANS DATE", None)
        self.assertNotIn(product.id, first_purchase_dates())

    def test_a_product_never_bought_at_all_is_absent_too(self):
        product = self.product_bought_on("JAMAIS ACHETÉ")
        self.assertNotIn(product.id, first_purchase_dates())


class EntryAvailabilityTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(name="Metro")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.old = self.bought("VIEILLE VODKA", "2026-01-15")
        self.new = self.bought("NOUVELLE VODKA", "2026-06-01")

    def bought(self, name, iso, stock_type=None):
        product = make_product(
            supplier=self.supplier, raw_name=name, stock_type=stock_type or self.vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        invoice = make_invoice(supplier=self.supplier, invoice_date=date.fromisoformat(iso))
        make_invoice_line(invoice=invoice, product=product, quantity=6, total_ht="60")
        return product

    def test_the_lookup_ships_each_entrys_first_delivery(self):
        entries = stock_take_entry_lookup()
        self.assertEqual(entries[product_display_name(self.old)]["available_from"], "2026-01-15")
        self.assertEqual(entries[product_display_name(self.new)]["available_from"], "2026-06-01")

    def test_a_stock_item_exists_from_its_earliest_bottle(self):
        """Whichever brand that was - the stock item is on the shelf as soon
        as anything under it is."""
        entries = stock_take_entry_lookup()
        self.assertEqual(entries[stock_type_entry_name(self.vodka)]["available_from"], "2026-01-15")

    def test_a_stock_item_nobody_ever_bought_has_no_date(self):
        empty = make_stock_type(name="Rien", unit=UnitChoices.LITRE)
        entries = stock_take_entry_lookup()
        self.assertIsNone(entries[stock_type_entry_name(empty)]["available_from"])

    def test_the_page_ships_the_dates_to_the_browser(self):
        response = self.client.get(reverse("inventory:stock_take_create"))
        data = json.loads(response.context["entry_data"])
        self.assertEqual(data[product_display_name(self.new)]["available_from"], "2026-06-01")


class TooNewToCountTests(TestCase):
    """A datalist is a suggestion; the form has to refuse it as well."""

    def setUp(self):
        self.supplier = make_supplier(name="Metro")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.product = make_product(
            supplier=self.supplier, raw_name="NOUVELLE VODKA", stock_type=self.vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 6, 1))
        make_invoice_line(invoice=invoice, product=self.product, quantity=6, total_ht="60")

    def payload(self, entry, taken_at):
        return {
            "taken_at": taken_at,
            "note": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-entry_search": entry,
            "lines-0-counted_quantity": "2",
            "lines-0-unit": UnitChoices.UNIT,
        }

    def post(self, entry, taken_at):
        return self.client.post(reverse("inventory:stock_take_create"), self.payload(entry, taken_at))

    def test_counting_it_before_it_was_delivered_is_refused(self):
        response = self.post(product_display_name(self.product), "2026-03-31 12:00:00")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockTake.objects.count(), 0)
        self.assertContains(response, "après la date de cet inventaire")
        self.assertContains(response, "01/06/2026")

    def test_counting_it_after_it_was_delivered_is_fine(self):
        response = self.post(product_display_name(self.product), "2026-07-01 12:00:00")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockTake.objects.get().lines.count(), 1)

    def test_the_day_of_delivery_counts_as_delivered(self):
        response = self.post(product_display_name(self.product), "2026-06-01 12:00:00")
        self.assertEqual(response.status_code, 302)

    def test_a_stock_item_is_judged_the_same_way(self):
        response = self.post(stock_type_entry_name(self.vodka), "2026-03-31 12:00:00")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockTake.objects.count(), 0)

    def test_something_with_no_purchase_history_is_never_too_new(self):
        """Nothing on record says it is, and refusing it would make a stock
        item that predates the invoice history impossible to count."""
        untracked = make_stock_type(name="Sirop maison", unit=UnitChoices.LITRE)
        response = self.post(stock_type_entry_name(untracked), "2026-03-31 12:00:00")
        self.assertEqual(response.status_code, 302)

    def test_the_date_judged_against_is_the_one_being_SUBMITTED(self):
        """Moving an inventory's date forward and adding a line happen in the
        same POST. Judging the new line against the date still on the saved
        row would reject a row that is perfectly valid for the date being
        saved."""
        take = make_stock_take(taken_at=datetime(2026, 3, 31, 12, 0))
        data = self.payload(product_display_name(self.product), "2026-07-01 12:00:00")
        response = self.client.post(
            reverse("inventory:stock_take_update", kwargs={"pk": take.pk}), data
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(take.lines.count(), 1)

    def test_a_line_being_removed_is_not_re_judged(self):
        """An item counted before the rules tightened must still be
        removable - validating a row on its way out would trap the user."""
        take = make_stock_take(taken_at=datetime(2026, 3, 31, 12, 0))
        line = make_stock_take_line(
            stock_take=take, product=self.product, counted_quantity="2",
            unit=UnitChoices.UNIT, value_ht="20",
        )
        data = self.payload(product_display_name(self.product), "2026-03-31 12:00:00")
        data["lines-INITIAL_FORMS"] = "1"
        data["lines-0-id"] = str(line.pk)
        data["lines-0-DELETE"] = "on"

        response = self.client.post(
            reverse("inventory:stock_take_update", kwargs={"pk": take.pk}), data
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(take.lines.count(), 0)


class LiveLineValueTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(name="Metro")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.product = make_product(
            supplier=self.supplier, raw_name="SOBIESKI 70CL", stock_type=self.vodka,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        # 10 bottles at 12 € in January, 10 more at 20 € in May.
        for iso, total in (("2026-01-10", "120"), ("2026-05-10", "200")):
            invoice = make_invoice(supplier=self.supplier, invoice_date=date.fromisoformat(iso))
            make_invoice_line(invoice=invoice, product=self.product, quantity=10, total_ht=total)

    def value(self, **params):
        params.setdefault("entry", product_display_name(self.product))
        params.setdefault("quantity", "3")
        params.setdefault("unit", UnitChoices.UNIT)
        response = self.client.get(reverse("inventory:value_stock_take_line"), params)
        return json.loads(response.content)

    def test_it_prices_a_count_at_the_most_recent_purchases(self):
        """FIFO ending inventory: what's left is what arrived last, so three
        bottles are three of the 20 € ones."""
        data = self.value(quantity="3", as_of="2026-06-30")
        self.assertTrue(data["ok"])
        self.assertEqual(data["value_ht"], "60.00")
        self.assertEqual(data["unit_cost_ht"], "20.00")

    def test_the_date_changes_the_price(self):
        """Counted in February, the May delivery hadn't happened - the same
        three bottles are the 12 € ones."""
        data = self.value(quantity="3", as_of="2026-02-28")
        self.assertEqual(data["value_ht"], "36.00")

    def test_it_agrees_exactly_with_what_the_save_would_store(self):
        """The whole point: the preview and the stored value are the same
        function, so they cannot drift apart."""
        for as_of in ("2026-02-28", "2026-06-30"):
            with self.subTest(as_of=as_of):
                preview = self.value(quantity="7", as_of=as_of)
                stored = value_counted_quantity(
                    self.product, Decimal("7"), UnitChoices.UNIT, as_of=date.fromisoformat(as_of)
                )
                self.assertEqual(preview["value_ht"], f"{stored['value_ht']:.2f}")

    def test_counting_more_than_was_ever_bought_is_flagged(self):
        data = self.value(quantity="50", as_of="2026-06-30")
        self.assertTrue(data["has_shortfall"])
        self.assertEqual(data["shortfall_quantity"], "30.00")

    def test_a_stock_item_can_be_priced_directly(self):
        data = self.value(entry=stock_type_entry_name(self.vodka), quantity="2.1", as_of="2026-06-30")
        self.assertTrue(data["ok"])
        # 2.1 L is three 70cl bottles of the newest delivery.
        self.assertEqual(data["value_ht"], "60.00")

    def test_an_unknown_entry_says_so_rather_than_guessing(self):
        self.assertFalse(self.value(entry="RIEN DU TOUT")["ok"])

    def test_a_quantity_that_is_not_a_number_is_refused(self):
        self.assertFalse(self.value(quantity="deux")["ok"])

    def test_a_missing_date_falls_back_to_today_rather_than_erroring(self):
        self.assertTrue(self.value(as_of="")["ok"])

    def test_it_changes_nothing(self):
        """A GET that prices; it must never write."""
        before = list(StockTake.objects.values_list("pk", flat=True))
        self.value(quantity="3")
        self.assertEqual(list(StockTake.objects.values_list("pk", flat=True)), before)

    def test_the_form_page_seeds_the_saved_values(self):
        """A saved line already knows what it is worth, so the running total
        is right on open with nothing re-priced."""
        take = make_stock_take(taken_at=datetime(2026, 6, 30, 12, 0))
        line = make_stock_take_line(
            stock_take=take, product=self.product, counted_quantity="3",
            unit=UnitChoices.UNIT, value_ht="60.00",
        )
        response = self.client.get(reverse("inventory:stock_take_update", kwargs={"pk": take.pk}))
        self.assertEqual(json.loads(response.context["saved_values"]), {str(line.pk): "60.00"})


class ResolverQueryCountTests(TestCase):
    """Resolving what the user typed used to be a query per row."""

    def setUp(self):
        self.supplier = make_supplier(name="Metro")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.products = [
            make_product(
                supplier=self.supplier, raw_name=f"ARTICLE {index:03d}", stock_type=self.vodka,
                unit=UnitChoices.UNIT, stock_equivalent="0.7",
            )
            for index in range(40)
        ]

    def test_one_resolver_answers_every_row_without_a_query_each(self):
        resolver = EntryResolver()
        with self.assertNumQueries(3):  # products, stock types, first purchases
            for product in self.products:
                self.assertEqual(resolver.product(product_display_name(product)), product)

    def test_validating_a_whole_formset_does_not_scale_with_its_rows(self):
        data = {
            "lines-TOTAL_FORMS": str(len(self.products)),
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
        }
        for index, product in enumerate(self.products):
            data[f"lines-{index}-entry_search"] = product_display_name(product)
            data[f"lines-{index}-counted_quantity"] = "2"
            data[f"lines-{index}-unit"] = UnitChoices.UNIT

        formset = StockTakeLineFormSet(
            data,
            instance=StockTake(),
            form_kwargs={"as_of": date(2026, 12, 31), "resolver": EntryResolver()},
        )
        with self.assertNumQueries(3):
            self.assertTrue(formset.is_valid())
