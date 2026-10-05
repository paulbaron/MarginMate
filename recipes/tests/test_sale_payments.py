"""What the bank has paid of each « facture de vente » (recipes/sale_payments.py).

A link says « this credit pays this document », never how much: one
transfer may pay two invoices, a cheque deposit of 1 200 € may hold one
customer's 300 € cheque. So one pure allocator spreads each credit's amount
over its documents - credits by date, each over its documents oldest first,
never beyond what a document still asks - and every reader goes through it:
the tab's pill, the document's « Règlement », Banque's gap, the matcher's
« due », the payer history and « Entrées d'argent ». Summing each linked
credit's WHOLE amount read « 1 200 € reçus » on a 300 € invoice, and gave a
document part-paid by a shared credit a wrong remaining due.

Every customer, number, date and amount is invented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from bank.models import BankTransaction
from recipes import sale_payments
from recipes.models import SaleDocument
from recipes.sale_payments import (
    CREDIT_NOTE_SAID,
    NOTHING_DUE_SAID,
    PAID_SAID,
    PART_PAID_SAID,
    PILL_CREDIT_NOTE,
    PILL_NOTHING_DUE,
    PILL_PAID,
    PILL_PART_PAID,
    PILL_UNPAID,
    PREPAID_SAID,
    UNPAID_LINKED_SAID,
    UNPAID_ONE_LINKED_SAID,
    UNPAID_SAID,
    LinkFact,
    allocate,
    document_to_pay,
    payment_state,
    read_links,
)
from tests.factories import make_credit, make_recipe, make_sale_document, make_sale_line, make_sale_payment

D = Decimal
COUNTED, TILL = SaleDocument.Counting.COUNTED, SaleDocument.Counting.TILL


def fact(credit_pk, document_pk, *, amount, to_pay, day=date(2026, 6, 10), sold_on=date(2026, 6, 1), **fields):
    """One link as `read_links` gives it - no database."""
    values = {
        "payment_pk": credit_pk * 100 + document_pk,
        "method": "MANUAL",
        "credit_pk": credit_pk,
        "credit_day": day,
        "credit_amount": D(amount),
        "counterparty": "EXEMPLE EVENEMENTS SARL",
        "label": "VIR SEPA RECU /FRM EXEMPLE EVENEMENTS SARL",
        "bank_type": "VIREMENT",
        "income_source": "",
        "settled_by_hand": True,
        "document_pk": document_pk,
        "reference": f"FV-{document_pk}",
        "customer": "Exemple Événements SARL",
        "sold_on": sold_on,
        "counting": COUNTED,
        "to_pay": D(to_pay),
    }
    values.update(fields)
    return LinkFact(**values)


class AllocationTests(SimpleTestCase):
    """Each credit spread over ITS documents, oldest first, each taking at
    most what it still asks - the Code civil's default when the payer says
    nothing (art. 1342-10)."""

    def test_one_credit_for_two_documents_oldest_first_each_to_what_it_asks(self):
        allocation = allocate(
            [
                fact(1, 20, amount="1000.00", to_pay="400.00", sold_on=date(2026, 6, 5)),
                fact(1, 10, amount="1000.00", to_pay="600.00", sold_on=date(2026, 6, 1)),
            ]
        )
        self.assertEqual(allocation.shares, {(1, 10): D("600.00"), (1, 20): D("400.00")})
        self.assertEqual(allocation.paid, {10: D("600.00"), 20: D("400.00")})
        self.assertEqual(allocation.left, {1: D("0.00")})
        self.assertEqual((allocation.due(10, D("600.00")), allocation.due(20, D("400.00"))), (D("0"), D("0")))

    def test_a_credit_short_of_both_pays_the_oldest_and_leaves_the_other_its_whole_due(self):
        """1 000 € linked to A (3 000 €) and B (500 €): A is owed 2 000 € more,
        B its 500 € - not « 2 000 € » and « 0 € » each, as a sum of whole
        amounts said."""
        allocation = allocate(
            [
                fact(1, 10, amount="1000.00", to_pay="3000.00", sold_on=date(2026, 5, 1)),
                fact(1, 20, amount="1000.00", to_pay="500.00", sold_on=date(2026, 5, 20)),
            ]
        )
        self.assertEqual(allocation.share(1, 10), D("1000.00"))
        self.assertEqual(allocation.share(1, 20), D("0"))
        self.assertEqual(allocation.due(10, D("3000.00")), D("2000.00"))
        self.assertEqual(allocation.due(20, D("500.00")), D("500.00"))

    def test_a_cheque_deposit_of_1200_linked_to_a_300_invoice(self):
        allocation = allocate([fact(1, 10, amount="1200.00", to_pay="300.00")])
        self.assertEqual(allocation.share(1, 10), D("300.00"))
        self.assertEqual(allocation.paid[10], D("300.00"))
        self.assertEqual(allocation.left[1], D("900.00"))

    def test_a_deposit_then_the_balance(self):
        allocation = allocate(
            [
                fact(2, 10, amount="700.00", to_pay="1000.00", day=date(2026, 6, 20)),
                fact(1, 10, amount="300.00", to_pay="1000.00", day=date(2026, 5, 20)),
            ]
        )
        self.assertEqual((allocation.share(1, 10), allocation.share(2, 10)), (D("300.00"), D("700.00")))
        self.assertEqual(allocation.paid[10], D("1000.00"))
        self.assertEqual(allocation.due(10, D("1000.00")), D("0"))
        self.assertEqual(allocation.left, {1: D("0.00"), 2: D("0.00")})

    def test_one_document_linked_to_two_credits_is_paid_once(self):
        """The second credit gives it nothing: it is paid already."""
        allocation = allocate(
            [
                fact(1, 10, amount="500.00", to_pay="500.00", day=date(2026, 6, 10)),
                fact(2, 10, amount="500.00", to_pay="500.00", day=date(2026, 6, 12)),
            ]
        )
        self.assertEqual((allocation.share(1, 10), allocation.share(2, 10)), (D("500.00"), D("0")))
        self.assertEqual(allocation.left[2], D("500.00"))
        self.assertEqual(allocation.paid[10], D("500.00"))

    def test_a_credit_note_asks_nothing(self):
        allocation = allocate([fact(1, 10, amount="100.00", to_pay="-50.00")])
        self.assertEqual(allocation.share(1, 10), D("0"))
        self.assertEqual(allocation.left[1], D("100.00"))
        self.assertEqual(allocation.due(10, D("-50.00")), D("0"))

    def test_credits_by_day_then_pk_documents_by_day_then_pk(self):
        """Two credits of one day: the smaller pk first; two documents of one
        day: the smaller pk first."""
        allocation = allocate(
            [
                fact(7, 30, amount="100.00", to_pay="100.00", day=date(2026, 6, 10), sold_on=date(2026, 6, 1)),
                fact(7, 20, amount="100.00", to_pay="100.00", day=date(2026, 6, 10), sold_on=date(2026, 6, 1)),
                fact(5, 30, amount="60.00", to_pay="100.00", day=date(2026, 6, 10), sold_on=date(2026, 6, 1)),
            ]
        )
        # Credit 5 first: 60 € to document 30. Then credit 7: document 20
        # (its pk the smaller) takes 100 €, document 30 the 40 € it still asks.
        self.assertEqual(allocation.share(5, 30), D("60.00"))
        self.assertEqual(allocation.share(7, 20), D("100.00"))
        self.assertEqual(allocation.share(7, 30), D("0.00"))
        allocation = allocate(
            [
                fact(7, 30, amount="140.00", to_pay="100.00", day=date(2026, 6, 10), sold_on=date(2026, 6, 1)),
                fact(7, 20, amount="140.00", to_pay="100.00", day=date(2026, 6, 10), sold_on=date(2026, 6, 1)),
                fact(5, 30, amount="60.00", to_pay="100.00", day=date(2026, 6, 10), sold_on=date(2026, 6, 1)),
            ]
        )
        self.assertEqual((allocation.share(7, 20), allocation.share(7, 30)), (D("100.00"), D("40.00")))
        self.assertEqual(allocation.left[7], D("0.00"))

    def test_nothing_linked(self):
        allocation = allocate([])
        self.assertEqual((allocation.shares, allocation.paid, allocation.left), ({}, {}, {}))
        self.assertEqual(allocation.due(10, D("120.00")), D("120.00"))
        self.assertEqual((allocation.of_document(10), allocation.of_credit(1)), ([], []))

    def test_each_side_lists_its_links(self):
        first = fact(1, 10, amount="100.00", to_pay="100.00")
        second = fact(1, 20, amount="100.00", to_pay="50.00", sold_on=date(2026, 6, 3))
        third = fact(2, 20, amount="80.00", to_pay="50.00", day=date(2026, 6, 11))
        allocation = allocate([third, first, second])
        self.assertEqual(allocation.of_credit(1), [first, second])
        self.assertEqual(allocation.of_document(20), [second, third])


class PaymentStateTests(SimpleTestCase):
    """What the tab's pill and the document's « Règlement » say (spec §5.5):
    the amounts the allocation gives THIS document - never a credit's whole
    amount."""

    def state(self, *facts, pk=10, **fields):
        document = SaleDocument(pk=pk, sold_on=date(2026, 6, 1), **fields)
        return payment_state(document, allocate(facts), [])

    def test_a_credit_note_has_nothing_to_receive(self):
        state = self.state(stated_total_ttc=D("-20.00"))
        self.assertEqual((state.pill, state.sentence), (PILL_CREDIT_NOTE, CREDIT_NOTE_SAID))

    def test_nothing_to_receive(self):
        state = self.state(stated_total_ttc=D("50.00"), prepaid_ttc=D("50.00"))
        self.assertEqual((state.pill, state.css), (PILL_NOTHING_DUE, ""))
        self.assertTrue(state.sentence.startswith(NOTHING_DUE_SAID.removesuffix(".")))

    def test_nothing_linked(self):
        state = self.state(stated_total_ttc=D("1234.50"))
        self.assertEqual((state.pill, state.css), (PILL_UNPAID, "status-ignored"))
        self.assertEqual(state.sentence, UNPAID_SAID.format(due="1\N{NO-BREAK SPACE}234.50"))
        self.assertEqual(state.paid, D("0"))

    def test_linked_but_its_credit_pays_other_invoices_first(self):
        older = fact(1, 5, amount="100.00", to_pay="100.00", sold_on=date(2026, 5, 1))
        mine = fact(1, 10, amount="100.00", to_pay="120.00")
        state = self.state(older, mine, stated_total_ttc=D("120.00"))
        self.assertEqual(state.pill, PILL_UNPAID)
        self.assertEqual(state.sentence, UNPAID_ONE_LINKED_SAID.format(due="120.00"))
        # A second credit, all of it to another older invoice first.
        third = fact(2, 6, amount="10.00", to_pay="50.00", sold_on=date(2026, 5, 2), day=date(2026, 6, 11))
        again = fact(2, 10, amount="10.00", to_pay="120.00", day=date(2026, 6, 11))
        state = self.state(older, mine, third, again, stated_total_ttc=D("120.00"))
        self.assertEqual(state.sentence, UNPAID_LINKED_SAID.format(due="120.00"))

    def test_paid_in_part(self):
        state = self.state(fact(1, 10, amount="50.00", to_pay="120.00"), stated_total_ttc=D("120.00"))
        self.assertEqual((state.pill, state.css), (PILL_PART_PAID, "status-NEEDS_REVIEW"))
        self.assertEqual(state.sentence, PART_PAID_SAID.format(paid="50.00", to_pay="120.00"))
        self.assertEqual(state.paid, D("50.00"))

    def test_paid_and_never_beyond(self):
        """A cheque deposit of 1 200 € on a 300 € invoice reads « 300 € reçus »."""
        state = self.state(fact(1, 10, amount="1200.00", to_pay="300.00"), stated_total_ttc=D("300.00"))
        self.assertEqual((state.pill, state.css), (PILL_PAID, "status-linked"))
        self.assertEqual(state.sentence, PAID_SAID.format(paid="300.00"))

    def test_a_deposit_the_invoice_deducts_is_said_after_it(self):
        state = self.state(
            fact(1, 10, amount="700.00", to_pay="700.00"), stated_total_ttc=D("1000.00"), prepaid_ttc=D("300.00")
        )
        self.assertEqual(
            state.sentence, PAID_SAID.format(paid="700.00").removesuffix(".") + PREPAID_SAID.format(prepaid="300.00")
        )
        self.assertEqual(state.to_pay, D("700.00"))

    def test_the_state_reads_the_lines_given_never_a_query(self):
        document = SaleDocument(pk=10, sold_on=date(2026, 6, 1))
        lines = [type("Line", (), {"total_ttc": D("40.004")})(), type("Line", (), {"total_ttc": D("10.00")})()]
        state = payment_state(document, allocate([]), lines)
        self.assertEqual(state.to_pay, D("50.00"))


class ReadLinksTests(TestCase):
    """Every link read once, with what its two ends say - two queries,
    always: the links, and the lines of the documents they pay."""

    def test_two_queries_with_links_and_two_without(self):
        with self.assertNumQueries(2):
            allocation = read_links()
        self.assertEqual(allocation.facts, ())
        mule = make_recipe(name="Mule exemple", selling_price_ttc="8.50")
        for number in range(3):
            document = make_sale_document(reference=f"FV-{number}", customer="Mariage Exemple")
            make_sale_line(document, recipe=mule, quantity="2")
            make_sale_line(document, label="Location de salle", unit_price_ttc="100.00")
            make_sale_payment(document, make_credit("117.00", date(2026, 3, 10 + number)))
        with self.assertNumQueries(2):
            allocation = read_links()
        self.assertEqual(len(allocation.facts), 3)
        self.assertEqual({one.to_pay for one in allocation.facts}, {D("117.00")})

    def test_each_link_carries_both_ends(self):
        document = make_sale_document(
            reference="FV-31", customer="Exemple Événements SARL", sold_on=date(2026, 3, 2), counting=TILL
        )
        make_sale_line(document, label="Formule cocktail", unit_price_ttc="45.00", quantity="2")
        credit = make_credit(
            "90.00",
            date(2026, 3, 9),
            counterparty="EXEMPLE EVENEMENTS SARL",
            label="VIR SEPA RECU /FRM EXEMPLE EVENEMENTS SARL /REF 31",
            income_source="credit",
            settled_by_hand=True,
        )
        payment = make_sale_payment(document, credit, method="AUTO")
        (one,) = read_links().facts
        self.assertEqual(
            one,
            LinkFact(
                payment_pk=payment.pk,
                method="AUTO",
                credit_pk=credit.pk,
                credit_day=date(2026, 3, 9),
                credit_amount=D("90.00"),
                counterparty="EXEMPLE EVENEMENTS SARL",
                label="VIR SEPA RECU /FRM EXEMPLE EVENEMENTS SARL /REF 31",
                bank_type="VIREMENT",
                income_source="credit",
                settled_by_hand=True,
                document_pk=document.pk,
                reference="FV-31",
                customer="Exemple Événements SARL",
                sold_on=date(2026, 3, 2),
                counting=TILL,
                to_pay=D("90.00"),
            ),
        )

    def test_to_pay_from_the_values_without_an_instance(self):
        """`document_to_pay` - what `to_pay_of` computes - on the columns
        read: the invoice's own « reste à payer », else its total less what
        was already paid, the total its stated one, else its lines'."""
        mule = make_recipe(name="Mule exemple", selling_price_ttc="8.50")
        typed = make_sale_document(reference="FV-41", prepaid_ttc="10.00")
        make_sale_line(typed, recipe=mule, quantity="3")
        stated = make_sale_document(reference="FV-42", stated_total_ttc="230.52", payable_ttc="200.52")
        make_sale_line(stated, label="Formule cocktail", quantity="2", total_ht="169.00", vat_rate="0.20")
        credit = make_credit("500.00", date(2026, 3, 20))
        for document in (typed, stated):
            make_sale_payment(document, credit)
        found = {one.document_pk: one.to_pay for one in read_links().facts}
        for document in (typed, stated):
            with self.subTest(reference=document.reference):
                self.assertEqual(found[document.pk], SaleDocument.objects.get(pk=document.pk).to_pay)
        self.assertEqual(found, {typed.pk: D("15.50"), stated.pk: D("200.52")})
        self.assertEqual(document_to_pay(D("230.52"), None, D("30.00"), []), D("200.52"))
        self.assertEqual(document_to_pay(None, None, None, []), D("0.00"))

    def test_a_link_only_its_own_transaction_s_sign(self):
        """A debit is never linked (bank.sale_reconcile refuses it); one put
        there by hand gives nothing."""
        document = make_sale_document(reference="FV-51", stated_total_ttc="40.00")
        debit = BankTransaction.objects.create(
            operation_date=date(2026, 3, 9), label="PRLV SEPA EXEMPLE", amount=D("-40.00"), fingerprint="debit-51"
        )
        make_sale_payment(document, debit)
        allocation = read_links()
        self.assertEqual(allocation.share(debit.pk, document.pk), D("0"))
        self.assertEqual(allocation.left[debit.pk], D("0"))
        self.assertEqual(sale_payments.payment_state(document, allocation, []).pill, PILL_UNPAID)
