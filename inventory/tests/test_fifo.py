"""Tests for the FIFO ending-inventory valuation.

`_fifo_value` is pure - no DB, no Django - and every wrong answer it gives
gets frozen into a StockTakeLine.value_ht that nothing later recomputes. So
it gets tested hard, and on its own.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.test import SimpleTestCase

from inventory.services import _fifo_value


@dataclass
class FakeLine:
    """Stands in for an InvoiceLine - _fifo_value only ever reads cost_ht,
    which is InvoiceLine.cost_ht's own arithmetic: what the line was billed
    at plus its share of the invoice's spread charges (a delivery), and
    nothing at all for the charge line itself."""

    total_ht: Decimal
    label: str = ""
    spread_ht: Decimal = Decimal("0")
    is_spread_charge: bool = False

    @property
    def cost_ht(self) -> Decimal:
        if self.is_spread_charge:
            return Decimal("0")
        return self.total_ht + self.spread_ht

    def __repr__(self):
        return self.label or f"<{self.total_ht}>"


def line(total_ht: str, label: str = "", spread_ht: str = "0") -> FakeLine:
    return FakeLine(Decimal(total_ht), label, Decimal(spread_ht))


def ladder(*entries):
    """(line, quantity) pairs, newest purchase first. An entry may carry a
    fourth value: the line's share of the invoice's delivery."""
    return [(line(total_ht, label, *rest), Decimal(quantity)) for label, quantity, total_ht, *rest in entries]


class FifoValueTests(SimpleTestCase):
    def test_count_inside_the_newest_purchase_uses_only_that_price(self):
        result = _fifo_value(ladder(("recent", "10", "100.00"), ("old", "10", "50.00")), Decimal("4"))
        self.assertEqual(result["value_ht"], Decimal("40"))  # 4 x 10.00, not the older 5.00
        self.assertFalse(result["has_shortfall"])

    def test_count_spanning_two_purchases_splits_at_the_boundary(self):
        result = _fifo_value(ladder(("recent", "10", "100.00"), ("old", "10", "50.00")), Decimal("14"))
        self.assertEqual(result["value_ht"], Decimal("120"))  # 10 x 10.00 + 4 x 5.00

    def test_count_landing_exactly_on_a_boundary(self):
        result = _fifo_value(ladder(("recent", "10", "100.00"), ("old", "10", "50.00")), Decimal("10"))
        self.assertEqual(result["value_ht"], Decimal("100"))
        self.assertFalse(result["has_shortfall"])
        self.assertEqual(len(result["sources"]), 1)

    def test_sources_record_where_each_slice_came_from(self):
        result = _fifo_value(ladder(("recent", "10", "100.00"), ("old", "10", "50.00")), Decimal("14"))
        self.assertEqual(
            [(s["invoice_line"].label, s["quantity_used"], s["unit_cost_ht"]) for s in result["sources"]],
            [("recent", Decimal("10"), Decimal("10")), ("old", Decimal("4"), Decimal("5"))],
        )

    def test_counting_more_than_was_ever_bought_flags_a_shortfall(self):
        """Still valued - at the oldest known price - rather than silently
        understating the total."""
        result = _fifo_value(ladder(("recent", "10", "100.00"), ("old", "10", "50.00")), Decimal("25"))
        self.assertTrue(result["has_shortfall"])
        self.assertEqual(result["shortfall_quantity"], Decimal("5"))
        self.assertEqual(result["value_ht"], Decimal("100") + Decimal("50") + Decimal("25"))

    def test_shortfall_has_no_source_row(self):
        """It's an extrapolation, not something drawn from a real purchase."""
        result = _fifo_value(ladder(("only", "2", "10.00")), Decimal("5"))
        self.assertEqual(sum(s["quantity_used"] for s in result["sources"]), Decimal("2"))

    def test_no_purchase_history_at_all(self):
        result = _fifo_value([], Decimal("5"))
        self.assertEqual(result["value_ht"], Decimal("0"))
        self.assertTrue(result["has_shortfall"])
        self.assertEqual(result["shortfall_quantity"], Decimal("5"))
        self.assertEqual(result["sources"], [])

    def test_counting_nothing_is_worth_nothing(self):
        result = _fifo_value(ladder(("recent", "10", "100.00")), Decimal("0"))
        self.assertEqual(result["value_ht"], Decimal("0"))
        self.assertFalse(result["has_shortfall"])
        self.assertEqual(result["sources"], [])

    def test_zero_quantity_lines_are_skipped(self):
        result = _fifo_value(ladder(("empty", "0", "0.00"), ("real", "10", "100.00")), Decimal("3"))
        self.assertEqual(result["value_ht"], Decimal("30"))

    def test_refund_lines_are_not_treated_as_stock_on_the_shelf(self):
        """B2. A deposit refund (returning empty kegs) is an invoice line
        with a NEGATIVE quantity and total. It is not stock sitting on the
        shelf, so it must not enter the price ladder at all.

        Left in, min(-9, remaining) picks the negative, so `remaining` GROWS
        instead of shrinking - the count is under-valued AND reports a
        fictitious shortfall on top.
        """
        result = _fifo_value(
            ladder(("refund", "-9", "-270.00"), ("purchase", "10", "100.00")),
            Decimal("4"),
        )
        self.assertEqual(result["value_ht"], Decimal("40"))
        self.assertFalse(result["has_shortfall"])
        self.assertEqual([s["invoice_line"].label for s in result["sources"]], ["purchase"])

    def test_refund_line_does_not_become_the_oldest_price_for_a_shortfall(self):
        """A shortfall priced at a refund line's negative unit cost would
        SUBTRACT value from the count."""
        result = _fifo_value(ladder(("purchase", "2", "20.00"), ("refund", "-9", "-270.00")), Decimal("5"))
        self.assertTrue(result["has_shortfall"])
        # 2 x 10 real + 3 x 10 extrapolated - never 3 x 30 from the refund.
        self.assertEqual(result["value_ht"], Decimal("50"))

    def test_fractional_counts_keep_full_decimal_precision(self):
        result = _fifo_value(ladder(("recent", "3", "10.00")), Decimal("0.333"))
        self.assertEqual(result["value_ht"], Decimal("0.333") * (Decimal("10") / Decimal("3")))


class FifoWithSpreadChargesTests(SimpleTestCase):
    """A count is priced delivery included, exactly as the stock movement is.

    The two are independent readings of « what did one of these cost »
    (`compute_movement_amounts` writes the ledger, this prices a count), and
    a count priced on one basis off a shelf valued on the other is the
    oldest bug in this codebase wearing a new hat.
    """

    def test_a_count_is_priced_with_the_delivery_the_line_carries(self):
        result = _fifo_value([(line("100.00", "gin", "10.00"), Decimal("10"))], Decimal("4"))
        self.assertEqual(result["value_ht"], Decimal("44"))  # 4 x 11.00, not 4 x 10.00
        self.assertEqual(result["sources"][0]["unit_cost_ht"], Decimal("11"))

    def test_a_shortfall_is_extrapolated_at_the_delivered_price_too(self):
        result = _fifo_value([(line("20.00", "gin", "2.00"), Decimal("2"))], Decimal("5"))
        self.assertTrue(result["has_shortfall"])
        self.assertEqual(result["value_ht"], Decimal("55"))  # 5 x 11.00

    def test_the_charge_line_itself_prices_nothing(self):
        # Its money is on the goods already; priced here it would be paid
        # for twice - and it is not stock in the first place.
        charge = FakeLine(Decimal("10.00"), "livraison", is_spread_charge=True)
        result = _fifo_value([(charge, Decimal("1"))], Decimal("1"))
        self.assertEqual(result["value_ht"], Decimal("0"))
