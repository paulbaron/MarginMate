"""Bank lines in the database: importing statements, the automatic pass, and
what a person decides on the page."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from bank import reconcile
from bank.models import BankTransaction, CounterpartyAlias, InvoicePayment
from invoices.deletion import delete_invoice
from invoices.models import Supplier
from tests.factories import make_invoice, make_invoice_line, make_product

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
        rows = (card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06"), debit_row(date(2026, 7, 9), "U.B.A.", "120,35"))
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
        self.load(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"), debit_row(date(2026, 7, 16), "METRO FRANCE", "120,00"))
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

    def test_an_invoice_already_paid_is_refused(self):
        invoice = self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"), debit_row(date(2026, 7, 16), "METRO FRANCE", "120,00"))
        reconcile.reconcile()
        other = BankTransaction.objects.get(payments__isnull=True)
        with self.assertRaises(reconcile.AlreadyPaidError):
            reconcile.link(other, [invoice])

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


class UndatedReceiptTests(Fixtures, TestCase):
    def test_an_undated_receipt_is_a_candidate_but_never_linked_on_its_own(self):
        receipt = self.invoice("MONOPRIX", date(2026, 8, 27), "7.36", rate=FIVE_FIVE)
        type(receipt).objects.filter(pk=receipt.pk).update(invoice_date=None)
        self.load(card_row(date(2026, 8, 27), "MONOPRIX PARIS", "7,76"))
        self.assertEqual(reconcile.reconcile(), 0)
        candidates = reconcile.candidates_from(reconcile.unpaid_invoices(date(2026, 8, 1), date(2026, 8, 31)))
        self.assertIn(receipt.pk, [candidate.pk for candidate in candidates])
