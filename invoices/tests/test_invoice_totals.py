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
