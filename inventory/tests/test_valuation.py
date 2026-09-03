"""Tests for stock-take valuation - the DB-backed layer over _fifo_value
(see test_fifo.py for the pure part).

These are the numbers a stock take reports as the value of what's on the
shelf, frozen into StockTakeLine.value_ht and never recomputed, so a wrong
answer here is a wrong answer forever.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from inventory.models import UnitChoices
from inventory.services import (
    value_counted_quantity,
    value_counted_stock_type_quantity,
)
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_purchase_history,
    make_stock_type,
    make_supplier,
)


class ValueCountedProductQuantityTests(TestCase):
    """Counting one specific product."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)

    def test_counting_bottles_prices_them_from_the_newest_purchases(self):
        product = make_product(stock_type=self.vodka, raw_name="VODKA 70CL")
        make_purchase_history(
            product,
            [("2026-01-10", 6, "60.00"), ("2026-02-10", 6, "90.00")],  # 10/btl then 15/btl
        )
        result = value_counted_quantity(product, Decimal("4"), UnitChoices.UNIT)
        self.assertEqual(result["value_ht"], Decimal("60"))  # 4 x the newest 15.00
        self.assertFalse(result["has_shortfall"])

    def test_counting_bottles_uses_the_line_quantity_not_the_measured_volume(self):
        """A bottle count has to be matched against invoice_line.quantity;
        matching it against total_volume would price 4 bottles as 4 litres."""
        product = make_product(stock_type=self.vodka, raw_name="VODKA 70CL", unit=UnitChoices.LITRE)
        invoice = make_invoice(supplier=product.supplier, invoice_date=date(2026, 2, 10))
        make_invoice_line(invoice=invoice, product=product, quantity=6, total_volume="4.2", total_ht="90.00")

        result = value_counted_quantity(product, Decimal("6"), UnitChoices.UNIT)
        self.assertEqual(result["value_ht"], Decimal("90"))
        self.assertFalse(result["has_shortfall"])

    def test_counting_in_the_stock_unit_converts_through_stock_equivalent(self):
        """B1. Counting "2.1 litres left" of a product whose invoices print
        no volume column - so its own amounts are a bottle count, converted
        to litres by stock_equivalent (0.7 per bottle).

        Without that conversion the 2.1 is matched against a ladder measured
        in BOTTLES: 2.1 "bottles" at 15 EUR is 31.50 instead of the correct
        3 bottles' worth of litres priced at 45.00. 148 of the 505 real
        products have a stock_equivalent other than 1, up to a factor of 100.
        """
        product = make_product(
            stock_type=self.vodka, raw_name="VODKA 70CL", unit=UnitChoices.UNIT, stock_equivalent="0.7"
        )
        # 6 bottles for 90 EUR = 15 EUR/bottle = 4.2 litres.
        make_purchase_history(product, [("2026-02-10", 6, "90.00")])

        result = value_counted_quantity(product, Decimal("2.1"), UnitChoices.LITRE)
        # 2.1 L is 3 bottles; 3 x 15.00.
        self.assertEqual(result["value_ht"], Decimal("45.00"))
        self.assertFalse(result["has_shortfall"])

    def test_stock_unit_and_stock_type_counting_agree(self):
        """B1, stated as the invariant that makes it obvious: counting a
        product in the stock type's unit must give the same answer as
        counting the stock type itself, when that product is the only one
        under it. Those two code paths disagreed."""
        product = make_product(
            stock_type=self.vodka, raw_name="VODKA 70CL", unit=UnitChoices.UNIT, stock_equivalent="0.7"
        )
        make_purchase_history(product, [("2026-02-10", 6, "90.00")])

        via_product = value_counted_quantity(product, Decimal("2.1"), UnitChoices.LITRE)
        via_stock_type = value_counted_stock_type_quantity(self.vodka, Decimal("2.1"))
        self.assertEqual(via_product["value_ht"], via_stock_type["value_ht"])

    def test_counting_more_than_was_bought_flags_a_shortfall(self):
        product = make_product(stock_type=self.vodka)
        make_purchase_history(product, [("2026-02-10", 2, "30.00")])
        result = value_counted_quantity(product, Decimal("5"), UnitChoices.UNIT)
        self.assertTrue(result["has_shortfall"])
        self.assertEqual(result["shortfall_quantity"], Decimal("3"))
        self.assertEqual(result["value_ht"], Decimal("75"))  # 5 x 15.00

    def test_a_product_never_bought_is_all_shortfall(self):
        product = make_product(stock_type=self.vodka)
        result = value_counted_quantity(product, Decimal("3"), UnitChoices.UNIT)
        self.assertEqual(result["value_ht"], Decimal("0"))
        self.assertTrue(result["has_shortfall"])

    def test_sources_are_traceable_back_to_real_invoice_lines(self):
        product = make_product(stock_type=self.vodka)
        old, new = make_purchase_history(product, [("2026-01-10", 6, "60.00"), ("2026-02-10", 6, "90.00")])
        result = value_counted_quantity(product, Decimal("8"), UnitChoices.UNIT)
        self.assertEqual([s["invoice_line"].id for s in result["sources"]], [new.id, old.id])
        self.assertEqual([s["quantity_used"] for s in result["sources"]], [Decimal("6"), Decimal("2")])


class ValuationDateTests(TestCase):
    """B5/B6 - which purchases a count is allowed to be priced from."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.product = make_product(stock_type=self.vodka, raw_name="VODKA 70CL")

    def test_a_back_dated_count_ignores_later_purchases(self):
        """B5. Counting stock as it was in January must not be priced from a
        delivery that only arrived in February."""
        make_purchase_history(
            self.product,
            [("2026-01-10", 6, "60.00"), ("2026-02-10", 6, "180.00")],  # 10/btl then 30/btl
        )
        result = value_counted_quantity(
            self.product, Decimal("4"), UnitChoices.UNIT, as_of=date(2026, 1, 31)
        )
        self.assertEqual(result["value_ht"], Decimal("40"))  # 4 x 10.00, January's price

    def test_without_a_date_every_purchase_counts(self):
        make_purchase_history(self.product, [("2026-01-10", 6, "60.00"), ("2026-02-10", 6, "180.00")])
        result = value_counted_quantity(self.product, Decimal("4"), UnitChoices.UNIT)
        self.assertEqual(result["value_ht"], Decimal("120"))  # 4 x 30.00

    def test_a_purchase_on_the_count_date_itself_is_included(self):
        make_purchase_history(self.product, [("2026-01-31", 6, "60.00")])
        result = value_counted_quantity(
            self.product, Decimal("4"), UnitChoices.UNIT, as_of=date(2026, 1, 31)
        )
        self.assertFalse(result["has_shortfall"])

    def test_stock_type_counting_respects_the_date_too(self):
        make_purchase_history(
            self.product,
            [("2026-01-10", 6, "60.00"), ("2026-02-10", 6, "180.00")],
        )
        result = value_counted_stock_type_quantity(self.vodka, Decimal("4"), as_of=date(2026, 1, 31))
        self.assertEqual(result["value_ht"], Decimal("40"))

    def test_undated_invoices_sort_last_not_first(self):
        """B6. invoice_date is nullable, and NULL ordering differs between
        SQLite and Postgres - so it has to be pinned explicitly rather than
        left to the backend. An undated invoice is the one we know least
        about; it belongs at the OLDEST end of the ladder, never at the
        newest where it would price the whole count.
        """
        dated = make_invoice(supplier=self.product.supplier, invoice_date=date(2026, 1, 10))
        make_invoice_line(invoice=dated, product=self.product, quantity=6, total_ht="60.00")
        undated = make_invoice(supplier=self.product.supplier, invoice_date=None)
        make_invoice_line(invoice=undated, product=self.product, quantity=6, total_ht="600.00")

        result = value_counted_quantity(self.product, Decimal("4"), UnitChoices.UNIT)
        self.assertEqual(result["value_ht"], Decimal("40"))  # the dated purchase's 10.00/btl

    def test_undated_invoices_are_still_usable_for_a_back_dated_count(self):
        """Unknown-date purchases aren't dropped by an as_of filter - we
        can't prove they're too new, and dropping them would understate the
        count. They just sort last, so they're only reached as a fallback."""
        undated = make_invoice(supplier=self.product.supplier, invoice_date=None)
        make_invoice_line(invoice=undated, product=self.product, quantity=6, total_ht="60.00")
        result = value_counted_quantity(
            self.product, Decimal("4"), UnitChoices.UNIT, as_of=date(2026, 1, 31)
        )
        self.assertEqual(result["value_ht"], Decimal("40"))
        self.assertFalse(result["has_shortfall"])


class ValueCountedStockTypeTests(TestCase):
    def test_merges_every_product_under_the_stock_type_into_one_ladder(self):
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        supplier = make_supplier()
        sobieski = make_product(supplier=supplier, stock_type=vodka, raw_name="SOBIESKI", stock_equivalent="0.7")
        wyborowa = make_product(supplier=supplier, stock_type=vodka, raw_name="WYBOROWA", stock_equivalent="0.7")
        make_purchase_history(sobieski, [("2026-01-10", 10, "70.00")])   # 7 L at 10/L
        make_purchase_history(wyborowa, [("2026-02-10", 10, "140.00")])  # 7 L at 20/L

        result = value_counted_stock_type_quantity(vodka, Decimal("10"))
        # Newest first: 7 L at 20.00, then 3 L at 10.00.
        self.assertEqual(result["value_ht"], Decimal("170"))

    def test_a_refund_line_does_not_corrupt_the_ladder(self):
        """B2 end-to-end: a déconsigne line really does reach this ladder."""
        kegs = make_stock_type(name="Fûts", unit=UnitChoices.UNIT)
        product = make_product(stock_type=kegs, raw_name="FÛT 30L", unit=UnitChoices.UNIT)
        purchase = make_invoice(supplier=product.supplier, invoice_date=date(2026, 1, 10))
        make_invoice_line(invoice=purchase, product=product, quantity=10, total_ht="100.00")
        refund = make_invoice(supplier=product.supplier, invoice_date=date(2026, 2, 10))
        make_invoice_line(invoice=refund, product=product, quantity=-9, total_ht="-270.00")

        result = value_counted_stock_type_quantity(kegs, Decimal("4"))
        self.assertEqual(result["value_ht"], Decimal("40"))
        self.assertFalse(result["has_shortfall"])
