"""Bank lines in the database: importing statements, the automatic pass, and
what a person decides on the page."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from bank import matching, reconcile
from bank.models import BankTransaction, CounterpartyAlias, IgnoreRule, InvoicePayment
from invoices.deletion import delete_invoice
from invoices.models import Supplier
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

TWENTY = Decimal("0.20")
FIVE_FIVE = Decimal("0.055")


def statement(*rows):
    header = '"Compte de ch&egrave;ques";"Compte de ch&amp;egrave;ques";****0042;14/09/2026;;1 000,00\n'
    return (header + "".join(row + "\n" for row in rows)).encode()


def card_row(day, merchant, amount):
    booked = day + timedelta(days=1)
    return (
        f"{booked:%d/%m/%Y};PAIEMENT CB;FACTURE CARTE;FACTURE CARTE DU {day:%d%m%y} {merchant}   "
        f"CARTE   4974XXXXXXXX1111;{booked:%d/%m/%Y};-{amount}"
    )


def debit_row(day, creditor, amount):
    return (
        f"{day:%d/%m/%Y};PRELEVEMENT;PRLV SEPA;PRLV SEPA {creditor} ECH/{day:%d%m%y} "
        f"ID EMETTEUR/FR00ZZZ000000 REF/0000;{day:%d/%m/%Y};-{amount}"
    )


class Fixtures:
    def invoice(self, code, day, total_ht, rate=TWENTY, **kwargs):
        supplier = Supplier.objects.get(code=code)
        invoice = make_invoice(supplier=supplier, invoice_date=day, **kwargs)
        make_invoice_line(invoice=invoice, product=make_product(supplier=supplier), total_ht=total_ht, vat_rate=rate)
        return invoice

    def load(self, *rows):
        return reconcile.import_statement(statement(*rows))


class ImportTests(Fixtures, TestCase):
    def test_each_operation_becomes_a_line(self):
        summary = self.load(
            card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06"), debit_row(date(2026, 7, 9), "U.B.A.", "120,35")
        )
        self.assertEqual((summary.lines, summary.created), (2, 2))
        card = BankTransaction.objects.get(kind=BankTransaction.Kind.CARD)
        self.assertEqual(
            (card.card_date, card.counterparty, card.amount, card.account),
            (date(2026, 7, 15), "FRANPRIX 5333 PARIS", Decimal("-13.06"), "****0042"),
        )

    def test_the_same_statement_twice_imports_nothing_new(self):
        rows = (
            card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06"),
            debit_row(date(2026, 7, 9), "U.B.A.", "120,35"),
        )
        self.load(*rows)
        summary = self.load(*rows)
        self.assertEqual((summary.created, summary.known), (0, 2))
        self.assertEqual(BankTransaction.objects.count(), 2)

    def test_overlapping_exports_add_only_what_is_new(self):
        first = card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06")
        self.load(first)
        summary = self.load(first, card_row(date(2026, 8, 1), "FRANPRIX 5333 PARIS", "3,92"))
        self.assertEqual((summary.created, summary.known), (1, 1))


class AutomaticPassTests(Fixtures, TestCase):
    def test_a_debit_is_linked_to_the_invoice_it_paid(self):
        invoice = self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(debit_row(date(2026, 7, 9), "METRO FRANCE S.A.S.-METRO FRANCE", "120,00"))
        self.assertEqual(reconcile.reconcile(), 1)
        payment = InvoicePayment.objects.get()
        self.assertEqual((payment.invoice, payment.method), (invoice, InvoicePayment.Method.AUTO))

    def test_duty_billed_on_top_of_the_lines_is_paid_with_its_vat(self):
        """UBA's July debits came to the cent only once the invoice's duty
        adjustment took VAT: 100 at 20% is 120, and 0,29 of duty is 0,35 with
        its VAT - 120,35 left the account, not 120,29."""
        invoice = self.invoice("UBA", date(2026, 6, 17), "100.00", reconciliation_adjustment=Decimal("0.29"))
        self.load(debit_row(date(2026, 7, 2), "U.B.A.", "120,35"))
        reconcile.reconcile()
        self.assertEqual(InvoicePayment.objects.get().invoice, invoice)

    def test_a_receipt_goes_to_its_card_payment(self):
        receipt = self.invoice("FRANPRIX", date(2026, 7, 15), "12.38", rate=FIVE_FIVE)
        self.load(card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06"))
        reconcile.reconcile()
        self.assertEqual(InvoicePayment.objects.get().invoice, receipt)

    def test_an_invoice_pays_one_line_only(self):
        self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 16), "METRO FRANCE", "120,00"),
        )
        reconcile.reconcile()
        self.assertEqual(InvoicePayment.objects.count(), 1)

    def test_what_is_not_certain_is_not_linked(self):
        """Same amount, same day - but the bank names another payee."""
        self.invoice("MONOPRIX", date(2026, 7, 15), "12.38", rate=FIVE_FIVE)
        self.load(card_row(date(2026, 7, 15), "SUMUP *BK PREM", "13,06"))
        self.assertEqual(reconcile.reconcile(), 0)

    def test_a_line_someone_unlinked_is_left_alone(self):
        self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"))
        reconcile.reconcile()
        reconcile.unlink(BankTransaction.objects.get())
        self.assertEqual(reconcile.reconcile(), 0)
        self.assertFalse(InvoicePayment.objects.exists())


class PersonTests(Fixtures, TestCase):
    def test_a_payee_linked_by_hand_is_recognised_next_time(self):
        first = self.invoice("MONOPRIX", date(2026, 7, 15), "12.38", rate=FIVE_FIVE)
        self.load(card_row(date(2026, 7, 15), "SUMUP *BK PREM", "13,06"))
        reconcile.link(BankTransaction.objects.get(card_date=date(2026, 7, 15)), [first])
        self.assertTrue(CounterpartyAlias.objects.filter(supplier=first.supplier, name="SUMUP BK PREM").exists())

        second = self.invoice("MONOPRIX", date(2026, 8, 13), "18.96", rate=FIVE_FIVE)
        self.load(card_row(date(2026, 8, 13), "SUMUP *BK PREM", "20,00"))
        self.assertEqual(reconcile.reconcile(), 1)
        self.assertEqual(InvoicePayment.objects.get(invoice=second).method, InvoicePayment.Method.AUTO)

    def test_an_invoice_already_paid_may_be_linked_again_by_a_person(self):
        """An invoice settled in two goes is a thing that happens, and the
        page has to be able to record it. Only a person does it - the
        automatic pass above still refuses (test_an_invoice_pays_one_line_only).
        See bank/tests/test_links.py for what the page then says."""
        invoice = self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 16), "METRO FRANCE", "120,00"),
        )
        reconcile.reconcile()
        other = BankTransaction.objects.get(payments__isnull=True)
        reconcile.link(other, [invoice])
        self.assertEqual(InvoicePayment.objects.filter(invoice=invoice).count(), 2)

    def test_no_invoice_takes_a_line_out_and_reopening_puts_it_back(self):
        self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"))
        line = BankTransaction.objects.get()
        reconcile.mark_no_invoice(line)
        self.assertFalse(reconcile.open_lines().exists())
        self.assertEqual(reconcile.reconcile(), 0)
        reconcile.reopen(line)
        self.assertEqual(reconcile.reconcile(), 1)

    def test_deleting_an_invoice_frees_its_payment(self):
        invoice = self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"))
        reconcile.reconcile()
        delete_invoice(invoice)
        self.assertEqual(list(reconcile.open_lines()), [BankTransaction.objects.get()])


#: The alias key of a fee line's label: its words without their digits.
FEE_KEY = "FRAIS TENUE DE COMPTE N DU"


class BlankPayeeTests(Fixtures, TestCase):
    """The bank's own fee line: kind OTHER, no counterparty, a label that is
    the same words under a new number every month. Nothing names a supplier
    from an empty payee - until a person links one, when the label's words
    become an alias and next month's links on its own. Every figure and every
    name invented; the label is structurally faithful only."""

    def setUp(self):
        self.bank = make_supplier(code="BANQUE", name="Banque Exemple")
        self.may = self.invoice("BANQUE", date(2026, 5, 5), "6.08", invoice_number="C-05")
        self.june = self.invoice("BANQUE", date(2026, 6, 5), "6.08", invoice_number="C-06")
        self.may_line = self.fee(date(2026, 5, 5), "000123", "7.30")
        self.june_line = self.fee(date(2026, 6, 5), "000456", "7.30")

    def fee(self, day, number, amount):
        return BankTransaction.objects.create(
            operation_date=day,
            kind=BankTransaction.Kind.OTHER,
            label=f"FRAIS TENUE DE COMPTE N° {number} DU {day:%d/%m/%y}",
            counterparty="",
            amount=-Decimal(amount),
            fingerprint=f"fee-{number}",
        )

    def test_an_empty_payee_names_nobody_until_a_person_links_it(self):
        self.assertEqual(reconcile.reconcile(), 0)
        reconcile.link(self.may_line, [self.may])
        self.assertEqual(
            list(CounterpartyAlias.objects.filter(supplier=self.bank).values_list("name", flat=True)), [FEE_KEY]
        )
        # Next month: the same words under another number, the same amount,
        # one unpaid invoice of the bank's in the window.
        self.assertEqual(reconcile.reconcile(), 1)
        payment = InvoicePayment.objects.get(invoice=self.june)
        self.assertEqual((payment.transaction, payment.method), (self.june_line, InvoicePayment.Method.AUTO))

    def test_the_label_teaches_nothing_unless_the_link_adds_up(self):
        dearer = self.invoice("BANQUE", date(2026, 5, 5), "10.00", invoice_number="C-05b")
        reconcile.link(self.may_line, [dearer])
        self.assertFalse(CounterpartyAlias.objects.exists())

    def test_with_both_months_unpaid_the_month_s_invoice_is_near_sure(self):
        CounterpartyAlias.objects.create(supplier=self.bank, name=FEE_KEY)
        candidates = reconcile.candidates_from(reconcile.unpaid_invoices(date(2026, 1, 1), date(2026, 12, 31)))
        found = matching.match(reconcile.payment_of(self.june_line), candidates, reconcile.supplier_naming())
        self.assertEqual(
            (found.confident, found.tier, found.options[0][0].pk), (False, matching.NEAR_SURE, self.june.pk)
        )

    def premium(self, day, number, amount):
        """An insurer's premium the bank debits with no counterparty: the
        label prints the insurer's own name beside a contract number."""
        return BankTransaction.objects.create(
            operation_date=day,
            kind=BankTransaction.Kind.OTHER,
            label=f"COTISATION ASSUREUR EXEMPLE CONTRAT {number}",
            counterparty="",
            amount=-Decimal(amount),
            fingerprint=f"premium-{number}",
        )

    def test_a_label_carrying_a_supplier_s_own_word_links_nothing_until_taught(self):
        """« ASSUREUR » in the premium's label IS the insurer's own name, and
        the pass still may not act on it: no line the bank printed no payee
        on was ever linked without a person, and a label carries the bank's
        text and a reference beside the payee's name - one word of a supplier's
        name in it is a coincidence the pass would act on (a supplier named
        « Assurance Exemple », a fee line reading « ASSURANCE MOYENS DE
        PAIEMENT »). Once a person links one such line, its words are the
        alias and next month's links on its own. Failing first: June's
        premium was linked AUTO from the label alone, and nothing learnt."""
        insurer = make_supplier(code="ASSUREUR", name="Assureur Exemple")
        may = self.invoice("ASSUREUR", date(2026, 5, 5), "35.00", invoice_number="P-05")
        june = self.invoice("ASSUREUR", date(2026, 6, 5), "35.00", invoice_number="P-06")
        may_line = self.premium(date(2026, 5, 5), "0001234", "42.00")
        june_line = self.premium(date(2026, 6, 5), "0001235", "42.00")
        self.assertEqual(reconcile.reconcile(), 0)
        self.assertFalse(CounterpartyAlias.objects.exists())
        reconcile.link(may_line, [may])
        self.assertEqual(
            list(CounterpartyAlias.objects.filter(supplier=insurer).values_list("name", flat=True)),
            ["COTISATION ASSUREUR EXEMPLE CONTRAT"],
        )
        self.assertEqual(reconcile.reconcile(), 1)
        payment = InvoicePayment.objects.get(invoice=june)
        self.assertEqual((payment.transaction, payment.method), (june_line, InvoicePayment.Method.AUTO))

    def test_a_label_a_letter_off_a_supplier_s_name_links_nothing_until_taught(self):
        """« COMPTE » in the fee label is one letter from « COMPTA »: for a
        label that is a coincidence, not a spelling, and the automatic pass
        must not act on it. Once a person links the line, the alias is what
        names the supplier - and it IS learnt, since the one-letter-off rule
        never named it. Failing first: the pass linked the bookkeeper's
        invoice to the bank's fee, and taught nothing."""
        # The bank's own invoices stay in the pool: nothing in the label
        # names « Banque Exemple », so they are not this line's candidates.
        bookkeeper = make_supplier(code="COMPTA", name="Compta Exemple")
        may = self.invoice("COMPTA", date(2026, 5, 5), "6.08", invoice_number="K-05")
        june = self.invoice("COMPTA", date(2026, 6, 5), "6.08", invoice_number="K-06")
        self.assertEqual(reconcile.reconcile(), 0)
        reconcile.link(self.may_line, [may])
        self.assertEqual(
            list(CounterpartyAlias.objects.filter(supplier=bookkeeper).values_list("name", flat=True)), [FEE_KEY]
        )
        self.assertEqual(reconcile.reconcile(), 1)
        self.assertEqual(InvoicePayment.objects.get(invoice=june).transaction, self.june_line)


class PassOrderTests(TestCase):
    """The one order lines are taken in - by the pass, by the bulk accept,
    and by the page deciding which of two lines wanting one invoice is
    ticked: card payments first, then by date, then by pk."""

    def test_card_payments_first_then_by_date_then_by_pk(self):
        card = BankTransaction(pk=9, kind=BankTransaction.Kind.CARD, operation_date=date(2026, 7, 20))
        early = BankTransaction(pk=5, kind=BankTransaction.Kind.DEBIT, operation_date=date(2026, 7, 1))
        same_day_later_pk = BankTransaction(pk=7, kind=BankTransaction.Kind.OTHER, operation_date=date(2026, 7, 1))
        late = BankTransaction(pk=1, kind=BankTransaction.Kind.TRANSFER, operation_date=date(2026, 7, 2))
        ordered = sorted([late, same_day_later_pk, early, card], key=reconcile.pass_order)
        self.assertEqual(ordered, [card, early, same_day_later_pk, late])


class AcceptProposalsTests(Fixtures, TestCase):
    """What « Propositions » hands `reconcile.accept_proposals`, and what it
    says back. The page-level checks are in bank/tests/test_proposals.py."""

    def setUp(self):
        self.receipt = self.invoice("MONOPRIX", date(2026, 7, 15), "12.38", rate=FIVE_FIVE)
        self.first = self.invoice("METRO", date(2026, 6, 20), "100.00", invoice_number="M-1")
        self.second = self.invoice("METRO", date(2026, 6, 25), "100.00", invoice_number="M-2")
        self.load(
            card_row(date(2026, 7, 15), "PAYTERM *EPICERIE 12", "13,06"),
            debit_row(date(2026, 7, 1), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 3), "METRO FRANCE", "120,00"),
        )
        self.card = BankTransaction.objects.get(kind=BankTransaction.Kind.CARD)
        self.early, self.late = BankTransaction.objects.filter(kind=BankTransaction.Kind.DEBIT).order_by(
            "operation_date"
        )

    def test_an_accepted_proposal_is_a_link_a_person_made(self):
        (outcome,) = reconcile.accept_proposals({self.card.pk: frozenset({self.receipt.pk})})
        self.assertEqual((outcome.status, outcome.invoices), (reconcile.ACCEPTED, [self.receipt]))
        payment = InvoicePayment.objects.get(invoice=self.receipt)
        self.card.refresh_from_db()
        self.assertEqual((payment.method, self.card.settled_by_hand), (InvoicePayment.Method.MANUAL, True))
        # It added up, so the payee the bank prints is learnt for the shop.
        self.assertTrue(
            CounterpartyAlias.objects.filter(supplier=self.receipt.supplier, name="PAYTERM EPICERIE 12").exists()
        )

    def test_two_lines_wanting_the_same_invoice_link_it_once_and_say_so(self):
        outcomes = reconcile.accept_proposals(
            {self.late.pk: frozenset({self.first.pk}), self.early.pk: frozenset({self.first.pk})}
        )
        by_line = {outcome.pk: outcome.status for outcome in outcomes}
        # In the pass's order - by date - so the earlier debit takes it.
        self.assertEqual(by_line, {self.early.pk: reconcile.ACCEPTED, self.late.pk: reconcile.PAID_MEANWHILE})
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.first).count(), 1)

    def test_an_option_the_matching_does_not_offer_is_refused(self):
        # The receipt is a card payment's, at another amount: not one of
        # this debit's options, whatever the POST says.
        (outcome,) = reconcile.accept_proposals({self.early.pk: frozenset({self.receipt.pk})})
        self.assertEqual(outcome.status, reconcile.CHANGED)
        self.assertFalse(InvoicePayment.objects.exists())

    def test_a_line_that_is_gone_income_settled_or_ruled_out_is_said(self):
        income = BankTransaction.objects.create(
            operation_date=date(2026, 7, 2), amount=Decimal("50.00"), label="VIR RECU EXEMPLE", fingerprint="in-1"
        )
        reconcile.mark_no_invoice(self.late)
        IgnoreRule.objects.create(pattern="METRO FRANCE", description="test")
        outcomes = reconcile.accept_proposals(
            {
                999999: frozenset({self.first.pk}),
                income.pk: frozenset({self.first.pk}),
                self.late.pk: frozenset({self.first.pk}),
                self.early.pk: frozenset({self.first.pk}),
            }
        )
        self.assertEqual(
            [outcome.status for outcome in outcomes],
            [reconcile.MISSING, reconcile.INCOME, reconcile.NOT_OPEN, reconcile.RULED_OUT],
        )
        self.assertFalse(InvoicePayment.objects.exists())

    def test_an_empty_option_changes_nothing(self):
        (outcome,) = reconcile.accept_proposals({self.early.pk: frozenset()})
        self.assertEqual(outcome.status, reconcile.CHANGED)
        self.assertFalse(InvoicePayment.objects.exists())


class UndatedReceiptTests(Fixtures, TestCase):
    def test_an_undated_receipt_is_a_candidate_but_never_linked_on_its_own(self):
        receipt = self.invoice("MONOPRIX", date(2026, 8, 27), "7.36", rate=FIVE_FIVE)
        type(receipt).objects.filter(pk=receipt.pk).update(invoice_date=None)
        self.load(card_row(date(2026, 8, 27), "MONOPRIX PARIS", "7,76"))
        self.assertEqual(reconcile.reconcile(), 0)
        candidates = reconcile.candidates_from(reconcile.unpaid_invoices(date(2026, 8, 1), date(2026, 8, 31)))
        self.assertIn(receipt.pk, [candidate.pk for candidate in candidates])
