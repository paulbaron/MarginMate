"""An invoice's total with VAT - the figure a bank statement has to agree with.

The reconciliation adjustment is HT: duty the lines don't carry, or a
receipt's rounding. It takes VAT at the rate of the goods it belongs with.
Added flat, UBA's July 2026 invoices were each five or six cents short of the
amount the bank debited for them.
"""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier


class TotalTtcTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="UBA", name="UBA")
        self.invoice = make_invoice(supplier=self.supplier, reconciliation_adjustment=Decimal("0.30"))

    def line(self, total_ht, rate, taxes="0"):
        make_invoice_line(
            invoice=self.invoice,
            product=make_product(supplier=self.supplier),
            total_ht=total_ht,
            vat_rate=Decimal(rate),
            taxes=Decimal(taxes),
        )

    def test_the_adjustment_takes_the_rate_of_the_goods_that_carry_duty(self):
        """The beer carries the duty; the much bigger food line does not."""
        self.line("100.00", "0.20", taxes="3.50")
        self.line("300.00", "0.055")
        self.assertEqual(self.invoice.total_ttc, Decimal("120.00") + Decimal("316.50") + Decimal("0.36"))

    def test_without_duty_on_any_line_the_main_rate_applies(self):
        """A receipt's adjustment is the rounding of its printed HT base."""
        self.line("2.76", "0.055")
        self.assertEqual(self.invoice.total_ttc, (Decimal("2.76") + Decimal("0.30")) * Decimal("1.055"))

    def test_an_invoice_without_lines_keeps_its_adjustment_as_it_is(self):
        self.assertEqual(self.invoice.total_ttc, Decimal("0.30"))


class InvoiceListTotalsTests(TestCase):
    def add_invoice(self, total_ht="100.00", rate="0.20"):
        supplier = make_supplier()
        invoice = make_invoice(supplier=supplier)
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=supplier), total_ht=total_ht, vat_rate=Decimal(rate)
        )
        return invoice

    def test_both_totals_are_listed(self):
        self.add_invoice("100.00", "0.20")
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(response, "Total TTC")
        self.assertContains(response, "100.00 €")
        self.assertContains(response, "120.00 €")

    def test_the_list_costs_the_same_however_many_invoices_it_shows(self):
        """Per-invoice totals are where an N+1 hides: one query per row per
        total, invisible at the call site."""
        self.add_invoice()
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("invoices:invoice_list"))
        for _ in range(5):
            self.add_invoice()
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("invoices:invoice_list"))
        self.assertEqual(len(many), len(few))


class PrintedTotalWinsWithinRoundingTests(TestCase):
    """The document's own total, when the lines only disagree with it by the
    rounding.

    A French supplier rounds its VAT ONCE per rate, on that rate's HT
    subtotal, and adds the rounded figures. `total_ttc` multiplies each line
    out and rounds the sum at the very end. The two are the same arithmetic
    - summed exactly, `sum(ht * (1 + rate))` equals `sum(ht) + sum per
    bracket of base * rate` to the digit - so what separates them is only
    WHERE the cent is rounded, and that can move the total by at most one
    cent per VAT bracket.

    Inside that, the printed total is the right answer: it is what the bank
    debits, and `bank.matching` needs `candidate.total == due` exactly, so a
    cent of drift makes an invoice unmatchable for ever. Payments had
    already been reconciled by hand for this, and more Metro invoices were
    filed a cent away from what they will be debited.

    Past a cent a bracket, nothing is rescued: that is not rounding, it is a
    row that was not read, and it has to stay visible.
    """

    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")

    def invoice(self, lines, printed=None, **kwargs):
        # Decimal, not the string: create() keeps what it is given, and the
        # field only coerces on the way back out of the database.
        invoice = make_invoice(
            supplier=self.supplier,
            printed_total_ttc=Decimal(printed) if printed is not None else None,
            **kwargs,
        )
        for total_ht, rate, *rest in lines:
            make_invoice_line(
                invoice=invoice,
                product=make_product(supplier=self.supplier),
                total_ht=Decimal(total_ht),
                vat_rate=Decimal(rate),
                printed_ttc=Decimal(rest[0]) if rest else None,
            )
        return invoice

    # Invented: 18,18 + 39,94 at 5,5 % and 1,18 at 20 %: per line 62,7326,
    # per bracket 59,30 + 3,20 + 0,24 = 62,74, and 62,74 is what the bank
    # would debit.
    METRO_LINES = [("18.18", "0.055"), ("39.94", "0.055"), ("1.18", "0.20")]

    def test_the_printed_total_wins_over_a_cent_of_rounding(self):
        invoice = self.invoice(self.METRO_LINES, printed="62.74")
        self.assertEqual(invoice.total_ttc, Decimal("62.74"))

    def test_without_a_printed_total_the_lines_still_decide(self):
        invoice = self.invoice(self.METRO_LINES)
        self.assertEqual(invoice.total_ttc, Decimal("62.7326"))

    def test_a_real_gap_is_never_papered_over(self):
        """A dropped row leaves the lines tens of euros short of the printed
        total. Rescuing that would file the right total over wrong lines and
        hide the fault completely."""
        invoice = self.invoice([("500.00", "0.055")], printed="560.00")
        self.assertNotEqual(invoice.total_ttc, Decimal("560.00"))
        self.assertEqual(invoice.total_ttc, Decimal("500.00") * Decimal("1.055"))

    def test_the_slack_is_one_cent_per_vat_bracket_and_no_more(self):
        """It must not grow with the number of LINES: a long Metro invoice
        would then swallow most of a euro."""
        one_rate = [("10.00", "0.20")] * 8  # 96,00 exactly, eight lines
        self.assertEqual(self.invoice(one_rate, printed="96.01").total_ttc, Decimal("96.01"))
        self.assertEqual(self.invoice(one_rate, printed="96.03").total_ttc, Decimal("96.00"))

    def test_a_receipt_that_kept_only_some_printed_amounts_still_uses_its_own_total(self):
        """A Franprix ticket (figures invented) says 7,37 and the bank
        debited 7,37, but three of its five lines lost their printed amount
        to a discount, so the all-or-nothing branch above was skipped and the
        total was rebuilt from HT that had already lost the cents - 7,36."""
        invoice = self.invoice(
            [
                ("0.47", "0.055"),
                ("0.47", "0.055"),
                ("0.46", "0.055"),
                ("2.46", "0.055", "2.60"),
                ("3.12", "0.055", "3.29"),
            ],
            printed="7.37",
        )
        self.assertEqual(invoice.total_ttc, Decimal("7.37"))

    def test_an_invoice_with_no_lines_is_untouched(self):
        self.assertEqual(self.invoice([], printed="12.00").total_ttc, Decimal("0"))
