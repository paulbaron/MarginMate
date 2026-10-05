"""« Entrées d'argent » and a credit paying a « facture de vente »
(bank/income.py, spec §6.5).

A credit paying a sales invoice that counts off the till reads « Facture de
vente » - derived from the links, never stored: no « En caisse » choice, no
payer holds it. Where it comes in the order is the point:

* the line's own choice first, then the till rules: a payout stays a payout
  (its gross, its commission, the card balance, the exact runs);
* then a payer retained for a source the till compares (card, cash, cheque,
  vouchers, « Avoir »): a terminal recognised by its payer « Carte » and
  linked to an invoice is still a payout - « A payer never un-recognises a
  line » reached by a link instead;
* then the sale; then a payer retained « Pas une vente »; then « Autres
  entrées ».

A « Déjà comptée par la caisse » invoice's credit is no sale: the till took
that money, and it reads as the till took it. Every credit of the window is
still in exactly one of three lists.

Every payer, customer, number, date and amount is invented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from bank import income, recognition, views
from bank.models import BankTransaction, IncomePayer
from bank.tests.support import SEEDED
from bank.tests.test_income import DAY, JUNE, Fixtures, payout_label, unsaved
from common import DateRange
from recipes.models import PosDailyPayment, SaleDocument
from recipes.sale_payments import read_links
from tests.factories import make_sale_document, make_sale_payment

D = Decimal
COUNTED, TILL, DEPOSIT = SaleDocument.Counting.COUNTED, SaleDocument.Counting.TILL, SaleDocument.Counting.DEPOSIT


def refs(*countings, share="100.00") -> tuple[income.SaleRef, ...]:
    """What `income.sale_refs` gives a credit paying documents of these
    countings - no database."""
    return tuple(
        income.SaleRef(pk, f"n° FV-{pk}", counting != TILL, D(share)) for pk, counting in enumerate(countings, start=1)
    )


def read(line, payers=None, sales=()) -> tuple[str, str]:
    entry = income.entry_for(line, payers, SEEDED, sales)
    return entry.source, entry.how


class ReadingOrderTests(SimpleTestCase):
    """line > rule > a payer retained for a compared source > sale > any
    other payer > « Autres entrées »."""

    def test_the_line_s_own_choice_beats_the_sale(self):
        line = unsaved(DAY, "500.00", income_source=income.CASH)
        self.assertEqual(read(line, sales=refs(COUNTED)), (income.CASH, income.BY_LINE))
        line = unsaved(DAY, "500.00", income_source=income.OTHER)
        self.assertEqual(read(line, sales=refs(COUNTED)), (income.OTHER, income.BY_LINE))

    def test_a_till_rule_beats_the_sale(self):
        payout = unsaved(DAY, "980.00", payout_label("1000.00"))
        self.assertEqual(read(payout, sales=refs(COUNTED)), (income.CARD, income.BY_RULE))
        cheques = unsaved(DAY, "300.00", "REMISE CHEQUE 0000042", bank_type="REMISE CHEQUE")
        self.assertEqual(read(cheques, sales=refs(COUNTED)), (income.CHEQUE, income.BY_RULE))

    def test_a_payer_retained_for_a_compared_source_beats_the_sale(self):
        line = unsaved(DAY, "79.50", "VIR SEPA RECU /FRM TERMINAL EXEMPLE REMISE", counterparty="TERMINAL EXEMPLE")
        payers = {income.payer_key(line): income.CARD}
        self.assertEqual(read(line, payers, refs(COUNTED)), (income.CARD, income.BY_PAYER))

    def test_the_sale_beats_a_payer_retained_pas_une_vente(self):
        line = unsaved(DAY, "500.00", counterparty="EXEMPLE EVENEMENTS SARL")
        payers = {income.payer_key(line): income.OTHER}
        self.assertEqual(read(line, payers, refs(COUNTED)), (income.SALE, income.BY_SALE))
        self.assertEqual(read(line, payers), (income.OTHER, income.BY_PAYER))
        self.assertEqual(read(line), (income.OTHER, income.BY_RULE))

    def test_a_till_invoice_makes_no_sale_a_deposit_one_does(self):
        line = unsaved(DAY, "500.00", counterparty="EXEMPLE EVENEMENTS SARL")
        self.assertEqual(read(line, sales=refs(TILL)), (income.OTHER, income.BY_RULE))
        self.assertEqual(read(line, sales=refs(DEPOSIT)), (income.SALE, income.BY_SALE))
        self.assertEqual(read(line, sales=refs(TILL, COUNTED)), (income.SALE, income.BY_SALE))

    def test_what_a_sale_entry_says(self):
        line = unsaved(DAY, "500.00", counterparty="EXEMPLE EVENEMENTS SARL")
        payers = {income.payer_key(line): income.OTHER}
        one = income.entry_for(line, payers, SEEDED, refs(COUNTED))
        self.assertEqual(
            (one.name, one.choice, one.how_label), ("Facture de vente", income.AUTOMATIC, "facture de vente n° FV-1")
        )
        self.assertFalse(one.remember_by_default)
        self.assertFalse(one.unnamed)
        two = income.entry_for(line, payers, SEEDED, refs(COUNTED, DEPOSIT, TILL))
        self.assertEqual(two.how_label, "2 factures de vente")
        self.assertEqual(income.SOURCES[income.SALE], "Factures de vente")

    def test_a_credit_linked_but_read_otherwise_is_never_unnamed(self):
        """A person linked it: it is no work « à classer »."""
        line = unsaved(DAY, "500.00", counterparty="EXEMPLE EVENEMENTS SARL")
        entry = income.entry_for(line, None, SEEDED, refs(TILL))
        self.assertEqual(entry.source, income.OTHER)
        self.assertFalse(entry.unnamed)
        self.assertTrue(income.entry_for(line, None, SEEDED).unnamed)

    def test_who_follows_a_payer(self):
        line = unsaved(DAY, "500.00", counterparty="EXEMPLE EVENEMENTS SARL")
        self.assertTrue(income.follows_its_payer(line, SEEDED))
        self.assertFalse(income.follows_its_payer(line, SEEDED, sold=True, holds=income.OTHER))
        self.assertTrue(income.follows_its_payer(line, SEEDED, sold=True, holds=income.CARD))


class ThroughTheDatabase(Fixtures):
    """A sale document and the credit that pays it."""

    def setUp(self):
        super().setUp()
        self.document = make_sale_document(
            reference="FV-61", customer="Exemple Événements SARL", sold_on=date(2026, 6, 1), stated_total_ttc="300.00"
        )

    def report(self) -> income.IncomeReport:
        return income.income_for(JUNE)

    def entry_of(self, report, line) -> income.Entry:
        found = [
            entry
            for entry in [row.entry for row in report.payouts] + report.others + report.other_means
            if entry.line.pk == line.pk
        ]
        self.assertEqual(len(found), 1, "every credit in exactly one list")
        return found[0]


class ListsTests(ThroughTheDatabase, TestCase):
    def test_a_sale_credit_is_in_other_means_once(self):
        line = self.credit(DAY, "300.00", counterparty="EXEMPLE EVENEMENTS SARL")
        make_sale_payment(self.document, line)
        report = self.report()
        entry = self.entry_of(report, line)
        self.assertIn(entry, report.other_means)
        self.assertEqual((entry.source, entry.how, entry.name), (income.SALE, income.BY_SALE, "Facture de vente"))
        self.assertEqual(report.by_source[income.SALE], (D("300.00"), 1))
        self.assertEqual(report.received_total, D("300.00"))

    def test_a_payout_read_by_its_payer_carte_and_linked_stays_a_payout(self):
        terminal = self.credit(
            DAY, "79.50", "VIR SEPA RECU /FRM TERMINAL EXEMPLE REMISE", counterparty="TERMINAL EXEMPLE"
        )
        IncomePayer.objects.create(key=income.payer_key(terminal), source=income.CARD)
        make_sale_payment(self.document, terminal)
        report = self.report()
        entry = self.entry_of(report, terminal)
        self.assertEqual((entry.source, entry.how), (income.CARD, income.BY_PAYER))
        self.assertEqual([row.entry.line.pk for row in report.payouts], [terminal.pk])
        self.assertEqual(entry.sales, income.sale_refs(read_links(), terminal.pk))

    def test_a_credit_linked_to_a_till_invoice_is_never_a_classer(self):
        SaleDocument.objects.filter(pk=self.document.pk).update(counting=TILL)
        line = self.credit(DAY, "300.00", counterparty="EXEMPLE EVENEMENTS SARL")
        make_sale_payment(self.document, line)
        report = self.report()
        entry = self.entry_of(report, line)
        self.assertEqual(entry.source, income.OTHER)
        self.assertIn(entry, report.others)
        self.assertEqual(report.unnamed_others, 0)

    def test_every_list_s_row_names_its_invoices_with_a_link_to_them(self):
        line = self.credit(DAY, "300.00", counterparty="EXEMPLE EVENEMENTS SARL")
        payout = self.payout(DAY, "500.00", "495.00")
        till = make_sale_document(reference="FV-62", sold_on=date(2026, 6, 2), counting=TILL, stated_total_ttc="40.00")
        other = self.credit(DAY, "40.00", counterparty="AUTRE PAYEUR EXEMPLE")
        for document, credit in ((self.document, line), (self.document, payout), (till, other)):
            make_sale_payment(document, credit)
        html = self.client.get(reverse("bank:income_home"), {"du": "2026-06-01", "au": "2026-06-30"}).content.decode()
        # The sale's row, and the payout's - read as a payout all the same.
        address = reverse("recipes:sale_document_update", args=[self.document.pk])
        self.assertEqual(html.count(f'href="{address}"'), 2)
        # The till invoice's credit, under « Autres entrées ».
        self.assertEqual(html.count(f'href="{reverse("recipes:sale_document_update", args=[till.pk])}"'), 1)
        self.assertIn("facture de vente n° FV-62", html)


class MethodRowTests(ThroughTheDatabase, TestCase):
    def test_a_row_of_its_own_compared_with_nothing(self):
        self.paid(DAY, CB=("50.00", 2))
        line = self.credit(DAY, "300.00", counterparty="EXEMPLE EVENEMENTS SARL")
        make_sale_payment(self.document, line)
        self.credit(DAY, "20.00")
        rows = {row.key: row for row in self.report().rows}
        row = rows[income.SALE]
        self.assertEqual((row.label, row.till, row.bank, row.bank_count), ("Factures de vente", None, D("300.00"), 1))
        self.assertIsNone(row.difference)
        self.assertEqual(row.note, income.NOTES[income.SALE])
        self.assertEqual(list(rows)[-2:], [income.SALE, income.OTHER])

    def test_never_before_the_till(self):
        """Compared with nothing, a sale is never « arrived before the till's
        first day »."""
        self.paid(date(2026, 6, 20), CB=("50.00", 2))
        make_sale_payment(self.document, self.credit(DAY, "300.00", counterparty="EXEMPLE EVENEMENTS SARL"))
        report = self.report()
        self.assertNotIn(income.SALE, report.bank_before_till)

    def test_no_row_without_a_sale(self):
        self.credit(DAY, "20.00")
        self.assertNotIn(income.SALE, [row.key for row in self.report().rows])


class SalesInsideTests(ThroughTheDatabase, TestCase):
    """A counted invoice paid at the terminal or in a cheque deposit: the
    credit stays a payout or a deposit, and its row says how much of it is a
    sale the till never rang."""

    def test_the_card_row_says_it_and_its_ecart_is_unchanged(self):
        self.paid(date(2026, 6, 1), CB=("700.00", 7))
        payout = self.payout(DAY, "1000.00", "985.00")
        before = {row.key: row.difference for row in self.report().rows}
        make_sale_payment(self.document, payout)
        report = self.report()
        rows = {row.key: row for row in report.rows}
        self.assertEqual(report.sales_inside, {income.CARD: D("300.00")})
        self.assertEqual(rows[income.CARD].sales_inside, D("300.00"))
        self.assertEqual(rows[income.CARD].difference, before[income.CARD])
        self.assertEqual(rows[income.CASH].sales_inside, D("0"))
        html = self.client.get(reverse("bank:income_home"), {"du": "2026-06-01", "au": "2026-06-30"}).content.decode()
        self.assertIn("dont 300.00 € de factures de vente hors caisse", html)

    def test_a_till_invoice_is_none_of_it(self):
        SaleDocument.objects.filter(pk=self.document.pk).update(counting=TILL)
        make_sale_payment(self.document, self.payout(DAY, "1000.00", "985.00"))
        self.assertEqual(self.report().sales_inside, {})


class SetSourceTests(ThroughTheDatabase, TestCase):
    """« En caisse » on a credit paying a sales invoice."""

    def setUp(self):
        super().setUp()
        self.first = self.credit(DAY, "300.00", "VIR SEPA RECU /FRM PAYEUR EXEMPLE", counterparty="PAYEUR EXEMPLE")
        make_sale_payment(self.document, self.first)
        self.sold = self.credit(DAY, "120.00", "VIR SEPA RECU /FRM PAYEUR EXEMPLE", counterparty="PAYEUR EXEMPLE")
        other = make_sale_document(reference="FV-63", sold_on=date(2026, 6, 3), stated_total_ttc="120.00")
        make_sale_payment(other, self.sold)
        self.unsold = self.credit(DAY, "45.00", "VIR SEPA RECU /FRM PAYEUR EXEMPLE", counterparty="PAYEUR EXEMPLE")

    def test_a_sold_credit_keeps_pas_une_vente_retained_for_its_payer_as_its_own(self):
        change = income.set_source(self.first, income.OTHER, remember=True)
        self.first.refresh_from_db()
        self.assertEqual(self.first.income_source, income.OTHER)
        self.assertEqual((change.entry.source, change.entry.how), (income.OTHER, income.BY_LINE))
        # The other sold credit is no follower of a « Pas une vente » payer.
        self.assertEqual((change.followers, change.kept), (1, 0))

    def test_a_compared_source_retained_reaches_a_sold_credit(self):
        change = income.set_source(self.first, income.CARD, remember=True)
        self.first.refresh_from_db()
        self.assertEqual(self.first.income_source, income.AUTOMATIC)
        self.assertEqual((change.entry.source, change.entry.how), (income.CARD, income.BY_PAYER))
        self.assertEqual((change.followers, change.kept), (2, 0))

    def test_automatic_hands_it_back_to_its_invoice(self):
        income.set_source(self.first, income.OTHER, remember=False)
        change = income.set_source(self.first, income.AUTOMATIC, remember=False)
        self.assertEqual((change.entry.source, change.entry.how), (income.SALE, income.BY_SALE))
        self.assertEqual(views._source_said(change), views.BACK_TO_ITS_INVOICE)

    def test_forgetting_a_payer_counts_the_sold_credits_it_never_reached_out(self):
        income.set_source(self.unsold, income.OTHER, remember=True)
        change = income.set_source(self.unsold, income.AUTOMATIC, remember=True)
        self.assertTrue(change.forgotten)
        self.assertEqual(change.followers, 0)


class ForgetPayerTests(ThroughTheDatabase, TestCase):
    def test_the_credits_going_back_leave_out_those_a_sale_reads(self):
        sold = self.credit(DAY, "300.00", counterparty="PAYEUR EXEMPLE")
        make_sale_payment(self.document, sold)
        self.credit(DAY, "45.00", counterparty="PAYEUR EXEMPLE")
        payer = IncomePayer.objects.create(key=income.payer_key(sold), source=income.OTHER)
        self.assertEqual(income.forget_payer(payer), 1)
        payer = IncomePayer.objects.create(key=income.payer_key(sold), source=income.CHEQUE)
        self.assertEqual(income.forget_payer(payer), 2)


class DetachedTests(ThroughTheDatabase, TestCase):
    """« Relire »'s preview: the credits that stop following their payer when
    its key moves - a sold credit a « Pas une vente » payer never reached is
    not one of them. Three queries, as before."""

    def pending(self, *lines) -> recognition.Changes:
        stored = BankTransaction.objects.filter(pk__in=[line.pk for line in lines]).only(
            "pk", "operation_date", "label", "bank_type", "kind", "counterparty", "card_date", "amount"
        )
        return recognition.Changes(
            [recognition.Change(line, recognition.Description(line.kind, "PAYEUR")) for line in stored],
            recognition.load(),
        )

    def test_sold_credits_a_payer_never_reached_are_not_counted(self):
        sold = self.credit(DAY, "300.00", counterparty="PAYEUR EXEMPLE")
        make_sale_payment(self.document, sold)
        unsold = self.credit(DAY, "45.00", counterparty="PAYEUR EXEMPLE")
        payer = IncomePayer.objects.create(key=income.payer_key(sold), source=income.OTHER)
        pending = self.pending(sold, unsold)
        with self.assertNumQueries(3):
            self.assertEqual(views._detached(pending), (1, 0))
        payer.source = income.CARD
        payer.save()
        self.assertEqual(views._detached(self.pending(sold, unsold)), (2, 0))


class SaidTests(ThroughTheDatabase, TestCase):
    def test_forgotten_on_a_sold_credit(self):
        line = self.credit(DAY, "300.00", counterparty="PAYEUR EXEMPLE")
        make_sale_payment(self.document, line)
        income.set_source(line, income.CARD, remember=True)
        change = income.set_source(line, income.AUTOMATIC, remember=True)
        said = views._source_said(change)
        self.assertTrue(said.endswith("Celle-ci compte comme « Facture de vente »."), said)


class QueryTests(ThroughTheDatabase, TestCase):
    def test_nine_queries_whatever_the_links(self):
        self.assertEqual(income.QUERIES, 9)
        self.paid(DAY, CB=("50.00", 2))
        with self.assertNumQueries(income.QUERIES):
            income.income_for(DateRange())
        for number in range(4):
            document = make_sale_document(reference=f"FV-7{number}", sold_on=date(2026, 6, 2), stated_total_ttc="60.00")
            make_sale_payment(document, self.credit(DAY, "60.00", counterparty="PAYEUR EXEMPLE"))
        PosDailyPayment.objects.create(sold_on=DAY, method=PosDailyPayment.CASH, amount=D("10.00"), payments=1)
        with self.assertNumQueries(income.QUERIES):
            report = income.income_for(DateRange())
        self.assertEqual(report.source_count(income.SALE), 4)
