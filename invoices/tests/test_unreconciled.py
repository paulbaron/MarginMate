"""Which documents no bank line pays yet, on « Achats ».

« Non rapprochée » only means something inside the period the statements
actually cover. Measured on the real data (read-only): most documents have
no payment, and nearly all of those only because they are older than the
first statement imported - those can never be linked, by nobody's fault.
Counted whole, the chip would print hundreds of « factures non
rapprochées » over the handful a person can do something about, which is a
number that teaches the reader to ignore it.

So the rule is the bank's own (`reconcile.unpaid_invoices`: no payment at
all, never « no payment from this line ») narrowed to the days the
statements speak for. An undated document is kept: it is in no window, and
the matching only ever suggests those, so leaving it out would hide exactly
the documents that need a person.

Every figure and every name below is invented.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from bank.models import BankTransaction, InvoicePayment
from invoices.workspace import bank_state, unreconciled_q
from invoices.models import Invoice
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

FIRST_STATEMENT = date(2025, 10, 6)


class Fixtures:
    def setUp(self):
        self.url = reverse("invoices:invoice_list")
        self.supplier = make_supplier(code="GROSSISTE", name="Grossiste Inventé")

    def statement(self, day=FIRST_STATEMENT, amount="-120.00", label="PRLV GROSSISTE INVENTE"):
        return BankTransaction.objects.create(
            operation_date=day,
            amount=Decimal(amount),
            label=label,
            fingerprint=f"fp-{day}-{amount}-{label}",
        )

    def invoice(self, day, total_ht="100.00", **kwargs):
        invoice = make_invoice(supplier=self.supplier, invoice_date=day, **kwargs)
        make_invoice_line(
            invoice=invoice,
            product=make_product(supplier=self.supplier),
            total_ht=Decimal(total_ht),
            vat_rate=Decimal("0.20"),
        )
        return invoice

    def pay(self, invoice, line=None):
        InvoicePayment.objects.create(
            transaction=line or self.statement(day=date(2026, 1, 5), amount="-120.00", label="PRLV PAYE"),
            invoice=invoice,
            method=InvoicePayment.Method.MANUAL,
        )

    def unreconciled(self):
        return set(Invoice.objects.filter(unreconciled_q()).values_list("pk", flat=True))


class UnreconciledRuleTests(Fixtures, TestCase):
    def test_an_invoice_no_line_pays_is_counted(self):
        self.statement()
        invoice = self.invoice(date(2026, 2, 3))
        self.assertEqual(self.unreconciled(), {invoice.pk})

    def test_an_invoice_a_line_pays_is_not(self):
        self.statement()
        invoice = self.invoice(date(2026, 2, 3))
        self.pay(invoice)
        self.assertEqual(self.unreconciled(), set())

    def test_an_invoice_older_than_the_statements_is_left_out(self):
        """It can never be linked: the statements do not go back that far,
        and most unpaid documents are only that."""
        self.statement()
        old = self.invoice(date(2024, 5, 6))
        recent = self.invoice(date(2026, 2, 3))
        self.assertEqual(self.unreconciled(), {recent.pk})
        self.assertNotIn(old.pk, self.unreconciled())

    def test_a_document_dated_the_first_day_covered_is_counted(self):
        self.statement()
        same_day = self.invoice(FIRST_STATEMENT)
        self.assertEqual(self.unreconciled(), {same_day.pk})

    def test_an_undated_document_is_counted(self):
        """It is in no window, and the matching only ever suggests those."""
        self.statement()
        undated = self.invoice(None)
        self.assertEqual(self.unreconciled(), {undated.pk})

    def test_with_no_statement_imported_nothing_is_counted(self):
        """Nothing can be reconciled against statements that do not exist,
        and every document « non rapprochée » on a fresh database is a false
        alarm."""
        self.invoice(date(2026, 2, 3))
        self.invoice(None)
        self.assertEqual(self.unreconciled(), set())

    def test_a_credit_note_is_counted_like_any_other(self):
        """Credit notes do get linked to a bank line, so a refund is a
        person's to reconcile; hiding avoirs would decide that for them."""
        self.statement()
        avoir = self.invoice(date(2026, 2, 3), total_ht="-50.00")
        self.assertIn(avoir.pk, self.unreconciled())


class BankStateTests(Fixtures, TestCase):
    """What a row says about its settlement, beside what it says about its
    own reading (Invoice.review_state)."""

    def test_a_settled_document_says_when(self):
        self.statement()
        invoice = self.invoice(date(2026, 2, 3))
        self.pay(invoice)
        invoice = Invoice.objects.prefetch_related("payments__transaction").get(pk=invoice.pk)
        self.assertEqual(bank_state(invoice, FIRST_STATEMENT)["label"], "Réglée le 05/01/2026")

    def test_an_unsettled_document_the_statements_cover_says_so(self):
        self.statement()
        invoice = self.invoice(date(2026, 2, 3))
        self.assertEqual(bank_state(invoice, FIRST_STATEMENT)["label"], "Non rapprochée")

    def test_a_document_older_than_the_statements_says_nothing(self):
        """The bank has nothing to say about it, and « Non rapprochée » on
        every document of 2024 is noise that hides the few real ones."""
        self.statement()
        invoice = self.invoice(date(2024, 5, 6))
        self.assertIsNone(bank_state(invoice, FIRST_STATEMENT))

    def test_without_statements_no_row_says_anything(self):
        invoice = self.invoice(date(2026, 2, 3))
        self.assertIsNone(bank_state(invoice, None))


class UnreconciledChipTests(Fixtures, TestCase):
    def chip(self, response):
        return next((chip for chip in response.context["chips"] if chip["key"] == "non-rapprochees"), None)

    def test_the_chip_counts_what_the_statements_could_pay(self):
        self.statement()
        self.invoice(date(2026, 2, 3))
        self.invoice(date(2026, 3, 4))
        self.invoice(date(2024, 5, 6))  # older than the statements
        self.pay(self.invoice(date(2026, 4, 5)))
        self.assertEqual(self.chip(self.client.get(self.url))["count"], 2)

    def test_the_filter_lists_exactly_what_the_chip_counts(self):
        self.statement()
        wanted = self.invoice(date(2026, 2, 3), invoice_number="F-NEUVE")
        self.invoice(date(2024, 5, 6), invoice_number="F-VIEILLE")
        self.pay(self.invoice(date(2026, 4, 5), invoice_number="F-REGLEE"))
        page = self.client.get(self.url, {"filtre": "non-rapprochees"})
        self.assertEqual([invoice.pk for invoice in page.context["invoices"]], [wanted.pk])
        self.assertContains(page, "F-NEUVE")
        self.assertNotContains(page, "F-VIEILLE")
        self.assertNotContains(page, "F-REGLEE")

    def test_an_invoice_settled_by_two_lines_is_counted_once_everywhere(self):
        """The chip asks about `payments`, a multi-valued relation. Folded
        into the same aggregate as the others, its LEFT JOIN turns an invoice
        settled in two goes into two rows and EVERY chip on the page counts
        it twice - « Tous 5 » over four documents."""
        self.statement()
        twice = self.invoice(date(2026, 2, 3), invoice_number="F-DEUX-FOIS")
        self.pay(twice, self.statement(day=date(2026, 2, 10), amount="-60.00", label="PRLV A"))
        self.pay(twice, self.statement(day=date(2026, 2, 11), amount="-60.00", label="PRLV B"))
        self.invoice(date(2026, 3, 4))
        chips = {chip["key"]: chip["count"] for chip in self.client.get(self.url).context["chips"]}
        self.assertEqual(chips[""], 2)
        self.assertEqual(chips["non-rapprochees"], 1)

    def test_with_no_statement_the_chip_is_not_drawn(self):
        self.invoice(date(2026, 2, 3))
        self.assertIsNone(self.chip(self.client.get(self.url)))

    def test_the_page_says_why_older_documents_are_out_of_it(self):
        """A list that silently drops hundreds of documents reads as a bug."""
        self.statement()
        self.invoice(date(2026, 2, 3))
        self.invoice(date(2024, 5, 6))
        page = self.client.get(self.url, {"filtre": "non-rapprochees"})
        self.assertContains(page, "06/10/2025")
