"""The database side of a credit paying a « facture de vente »
(bank/sale_reconcile.py): the automatic pass, the history that names a
customer, the links a person makes and takes off, and what a document's
page proposes.

The debit side's rules, kept (CLAUDE.md « Bank statements »): the pass links
only what is SURE - one exact named invoice nothing pays yet, among EVERY
invoice still due -, a person's decision freezes the line
(`settled_by_hand`), a credit a till rule or a payer retained for a compared
source reads is a payout or a deposit and never linked alone, a payer is
taught only by links that ADD UP, and a credit's own « En caisse » choice is
left alone.

Every customer, payer, number, date and amount is invented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from bank import income, matching
from bank.models import BankTransaction, IncomePayer
from bank.sale_matching import customer_key
from bank.sale_reconcile import (
    DEBIT_PAYS_NO_SALE,
    OWN_CHOICE_KEPT,
    SaleLinkRefused,
    credits_for,
    credits_matching,
    customer_naming,
    link,
    reconcile_sales,
    reopen,
    unlink_as_shown,
    unlink_document,
)
from bank.tests.test_reconcile import statement
from recipes.models import SaleDocument, SaleDocumentPayment
from recipes.sale_payments import read_links
from tests.factories import make_credit, make_sale_document, make_sale_payment

D = Decimal
CUSTOMER = "Exemple Événements SARL"
PAYER = "EXEMPLE EVENEMENTS SARL"
#: A payer the bank prints that names nothing of the customer: a parent
#: paying a wedding, a colleague paying a leaving party.
STRANGER = "M OU MME AUTRENOM"


def sale(reference, total="500.00", sold_on=date(2026, 6, 1), customer=CUSTOMER, **fields) -> SaleDocument:
    return make_sale_document(reference=reference, customer=customer, sold_on=sold_on, stated_total_ttc=total, **fields)


def received(amount="500.00", day=date(2026, 6, 10), counterparty=PAYER, **fields) -> BankTransaction:
    """A transfer received, its sender read into `counterparty` as the seeded
    rule « Virement reçu (/FRM) » reads it."""
    fields.setdefault("label", f"VIR SEPA RECU /FRM {counterparty} /REF {amount}")
    return make_credit(amount, day, counterparty=counterparty, **fields)


def linked_pairs() -> set[tuple[int, int]]:
    return set(SaleDocumentPayment.objects.values_list("transaction_id", "document_id"))


class PassTests(TestCase):
    """`reconcile_sales`: what it links on its own, and what it never does."""

    def test_a_sure_one_is_linked_once_and_automatically(self):
        document = sale("FV-1")
        line = received()
        self.assertEqual(reconcile_sales(), [(line, [document])])
        payment = SaleDocumentPayment.objects.get()
        self.assertEqual((payment.transaction_id, payment.document_id, payment.method), (line.pk, document.pk, "AUTO"))
        line.refresh_from_db()
        self.assertFalse(line.settled_by_hand)
        self.assertEqual(reconcile_sales(), [])
        self.assertEqual(SaleDocumentPayment.objects.count(), 1)

    def test_one_transfer_for_two_invoices_of_one_customer(self):
        first = sale("FV-1", total="300.00", sold_on=date(2026, 5, 20))
        second = sale("FV-2", total="200.00")
        line = received("500.00")
        self.assertEqual(reconcile_sales(), [(line, [first, second])])
        self.assertEqual(linked_pairs(), {(line.pk, first.pk), (line.pk, second.pk)})

    def test_what_a_person_decided_and_what_the_till_reads_are_left_alone(self):
        """Settled by hand, chosen « En caisse », read by a till rule, read
        by a payer retained « Carte », already paying an invoice: each beside
        a fresh invoice of its exact amount, its customer named."""
        other = sale("FV-0", total="999.00", sold_on=date(2026, 5, 2))
        cases = {
            "settled by hand": {"settled_by_hand": True},
            "chosen": {"income_source": income.CREDIT},
            "a cheque deposit the rules read": {"bank_type": "REMISE CHEQUE"},
            "a payer retained Carte": {"counterparty": "TERMINAL EXEMPLE"},
            "already linked": {},
        }
        lines = {}
        for number, (case, fields) in enumerate(cases.items(), start=1):
            amount = f"{100 + number}.00"
            sale(f"FV-{number}", total=amount, customer="Terminal Exemple" if "Carte" in case else CUSTOMER)
            lines[case] = received(amount, **fields)
        IncomePayer.objects.create(key=income.payer_key(lines["a payer retained Carte"]), source=income.CARD)
        make_sale_payment(other, lines["already linked"])
        self.assertEqual(reconcile_sales(), [])
        self.assertEqual(linked_pairs(), {(lines["already linked"].pk, other.pk)})

    def test_an_invoice_taken_is_withdrawn(self):
        document = sale("FV-1")
        first = received(day=date(2026, 6, 10))
        received(day=date(2026, 6, 12))
        self.assertEqual(reconcile_sales(), [(first, [document])])

    def test_nothing_when_a_partly_paid_invoice_s_balance_equals_the_credit_beside_a_fresh_one(self):
        older = sale("FV-1", total="800.00", sold_on=date(2026, 4, 1))
        link(received("300.00", day=date(2026, 4, 10)), [older])
        sale("FV-2", total="500.00")
        received("500.00")
        self.assertEqual(reconcile_sales(), [])

    def test_a_document_whatever_it_counts_in_is_paid_by_its_credit(self):
        """« Déjà comptée par la caisse » is a tab paid by transfer: linked
        all the same (it then reads as the till took it)."""
        document = sale("FV-1", counting=SaleDocument.Counting.TILL)
        line = received()
        self.assertEqual(reconcile_sales(), [(line, [document])])

    def test_it_runs_after_an_import(self):
        document = sale("FV-1", sold_on=date(2026, 7, 1))
        row = f"09/07/2026;VIREMENT RECU;VIR SEPA RECU;VIR SEPA RECU /FRM {PAYER} /REF FV-1;09/07/2026;500,00"
        self.client.post(reverse("bank:bank_home"), {"files": [SimpleUploadedFile("juillet.csv", statement(row))]})
        line = BankTransaction.objects.get()
        self.assertEqual(line.counterparty, PAYER)
        self.assertEqual(linked_pairs(), {(line.pk, document.pk)})

    def test_it_runs_on_rapprocher_automatiquement(self):
        document = sale("FV-1")
        line = received()
        self.client.post(reverse("bank:bank_reconcile"))
        self.assertEqual(linked_pairs(), {(line.pk, document.pk)})


class HistoryTests(TestCase):
    """A payer the bank prints for a customer is learnt from links that ADD
    UP, measured over each group of credits and invoices joined by links -
    nothing stored, so unlinking forgets."""

    def taught(self) -> frozenset:
        return customer_naming(read_links(), {CUSTOMER}, frozenset())[customer_key(CUSTOMER)].aliases

    def test_a_link_that_adds_up_teaches_its_payer_to_the_customer(self):
        link(
            received("400.00", day=date(2026, 3, 5), counterparty=STRANGER), [sale("FV-1", "400.00", date(2026, 3, 1))]
        )
        self.assertEqual(self.taught(), {matching.alias_key(STRANGER)})
        later = sale("FV-2", "250.00")
        line = received("250.00", counterparty=STRANGER)
        self.assertEqual(reconcile_sales(), [(line, [later])])

    def test_one_transfer_for_two_invoices_teaches(self):
        first = sale("FV-1", "400.00", date(2026, 3, 1))
        second = sale("FV-2", "200.00", date(2026, 3, 3))
        link(received("600.00", day=date(2026, 3, 5), counterparty=STRANGER), [first, second])
        self.assertEqual(self.taught(), {matching.alias_key(STRANGER)})

    def test_two_credits_on_one_invoice_teach_when_they_add_up(self):
        document = sale("FV-1", "400.00", date(2026, 3, 1))
        link(received("100.00", day=date(2026, 3, 5), counterparty=STRANGER), [document])
        self.assertEqual(self.taught(), frozenset())
        link(received("300.00", day=date(2026, 3, 9), counterparty="AUTRE PAYEUR EXEMPLE"), [document])
        self.assertEqual(self.taught(), {matching.alias_key(STRANGER), "AUTRE PAYEUR EXEMPLE"})

    def test_a_part_payment_teaches_nothing(self):
        link(
            received("300.00", day=date(2026, 3, 5), counterparty=STRANGER), [sale("FV-1", "400.00", date(2026, 3, 1))]
        )
        self.assertEqual(self.taught(), frozenset())
        sale("FV-2", "250.00")
        received("250.00", counterparty=STRANGER)
        self.assertEqual(reconcile_sales(), [])

    def test_unlinking_forgets(self):
        document = sale("FV-1", "400.00", date(2026, 3, 1))
        line = received("400.00", day=date(2026, 3, 5), counterparty=STRANGER)
        link(line, [document])
        unlink_document(line, document)
        self.assertEqual(self.taught(), frozenset())

    def deposit(self, amount, number, day):
        """A cheque deposit nothing recognises: no payer printed, a generic
        label whose digits differ from one deposit to the next."""
        return make_credit(amount, day, counterparty="", label=f"REMISE CHEQUES N° {number}", bank_type="DEPOT")

    def test_a_label_key_alone_never_links(self):
        """Taught by one deposit that added up, the label key « names » the
        customer on every later deposit: never enough to link by itself."""
        link(self.deposit("300.00", "1234567", date(2026, 3, 5)), [sale("FV-1", "300.00", date(2026, 3, 1))])
        self.assertEqual(self.taught(), {"REMISE CHEQUES N"})
        sale("FV-2", "150.00")
        self.deposit("150.00", "7654321", date(2026, 6, 10))
        self.assertEqual(reconcile_sales(), [])

    def test_a_label_key_taught_to_two_customers_names_neither(self):
        other = "Autre Exemple SAS"
        link(self.deposit("300.00", "1234567", date(2026, 3, 5)), [sale("FV-1", "300.00", date(2026, 3, 1))])
        link(
            self.deposit("200.00", "2345678", date(2026, 3, 6)),
            [sale("FV-2", "200.00", date(2026, 3, 2), customer=other)],
        )
        naming = customer_naming(read_links(), {CUSTOMER, other}, frozenset())
        self.assertEqual(naming[customer_key(CUSTOMER)].aliases, frozenset())
        self.assertEqual(naming[customer_key(other)].aliases, frozenset())
        sale("FV-3", "150.00")
        self.deposit("150.00", "7654321", date(2026, 6, 10))
        self.assertEqual(reconcile_sales(), [])

    def test_a_printed_payer_taught_to_two_customers_is_kept(self):
        """Only a key read off a label is dropped: a payer the bank prints
        (one person paying for two companies) stays each one's."""
        other = "Autre Exemple SAS"
        link(
            received("300.00", day=date(2026, 3, 5), counterparty=STRANGER), [sale("FV-1", "300.00", date(2026, 3, 1))]
        )
        link(
            received("200.00", day=date(2026, 3, 6), counterparty=STRANGER),
            [sale("FV-2", "200.00", date(2026, 3, 2), customer=other)],
        )
        naming = customer_naming(read_links(), {CUSTOMER, other}, frozenset())
        self.assertEqual(naming[customer_key(other)].aliases, {matching.alias_key(STRANGER)})


class LinkServiceTests(TestCase):
    """What a person does from Banque or from a document's page."""

    def test_a_debit_pays_no_sale(self):
        document = sale("FV-1")
        debit = make_credit("-500.00", date(2026, 6, 10), label="PRLV SEPA EXEMPLE", kind=BankTransaction.Kind.DEBIT)
        for action in (
            lambda: link(debit, [document]),
            lambda: unlink_document(debit, document),
            lambda: unlink_as_shown(debit, {document.pk}),
            lambda: reopen(debit),
        ):
            with self.assertRaisesMessage(SaleLinkRefused, DEBIT_PAYS_NO_SALE):
                action()
        debit.refresh_from_db()
        self.assertFalse(debit.settled_by_hand)
        self.assertFalse(SaleDocumentPayment.objects.exists())

    def test_a_link_by_hand_settles_the_line_and_touches_nothing_else(self):
        document = sale("FV-1", total="900.00")
        line = received(category="Fête de fin d'année")
        self.assertEqual(link(line, [document]), "")
        self.assertEqual(link(line, [document]), "")
        payment = SaleDocumentPayment.objects.get()
        self.assertEqual(payment.method, "MANUAL")
        line.refresh_from_db()
        self.assertEqual(
            (line.settled_by_hand, line.no_invoice, line.income_source, line.category),
            (True, False, "", "Fête de fin d'année"),
        )

    def test_the_line_s_own_choice_is_said(self):
        document = sale("FV-1")
        chosen = received(income_source=income.OTHER)
        self.assertEqual(link(chosen, [document]), OWN_CHOICE_KEPT.format(source="Pas une vente"))
        till = sale("FV-2", counting=SaleDocument.Counting.TILL)
        other = received("120.00", income_source=income.CREDIT)
        # A « Déjà comptée par la caisse » invoice never reads as a sale: nothing to say.
        self.assertEqual(link(other, [till]), "")

    def test_one_invoice_comes_off_at_a_time(self):
        first, second = sale("FV-1", "300.00"), sale("FV-2", "200.00")
        line = received()
        reconcile_sales()
        self.assertTrue(unlink_document(line, first))
        self.assertEqual(linked_pairs(), {(line.pk, second.pk)})
        line.refresh_from_db()
        self.assertTrue(line.settled_by_hand)
        self.assertFalse(unlink_document(line, first))
        self.assertTrue(unlink_document(line, second))
        line.refresh_from_db()
        # Nothing left, and still decided: the pass never puts it back.
        self.assertTrue(line.settled_by_hand)
        self.assertEqual(reconcile_sales(), [])

    def test_a_stale_tout_delier_takes_off_what_its_row_showed_or_nothing(self):
        first, second = sale("FV-1", "300.00"), sale("FV-2", "200.00")
        line = received()
        link(line, [first])
        link(line, [second])
        self.assertFalse(unlink_as_shown(line, {first.pk}))
        self.assertFalse(unlink_as_shown(line, set()))
        self.assertEqual(len(linked_pairs()), 2)
        self.assertTrue(unlink_as_shown(line, {first.pk, second.pk}))
        self.assertEqual(linked_pairs(), set())

    def test_reopen_hands_the_line_back_unless_it_was_linked_meanwhile(self):
        document = sale("FV-1")
        line = received(settled_by_hand=True)
        self.assertEqual(reconcile_sales(), [])
        self.assertTrue(reopen(line))
        line.refresh_from_db()
        self.assertFalse(line.settled_by_hand)
        self.assertEqual(reconcile_sales(), [(line, [document])])
        self.assertFalse(reopen(line))


class CreditsForTests(TestCase):
    """What a document's « Règlement » proposes: the credits near its date
    that could pay it, matched on what the allocation leaves of each - and
    never « certaine » where the pass would not act."""

    def offers(self, document):
        return [(offer.line.pk, offer.match.tier, offer.amount) for offer in credits_for(document, read_links())]

    def test_a_fresh_credit_is_proposed_sure(self):
        document = sale("FV-1")
        line = received()
        self.assertEqual(self.offers(document), [(line.pk, matching.SURE, D("500.00"))])

    def test_matched_on_what_the_allocation_leaves(self):
        first = sale("FV-1", total="300.00", sold_on=date(2026, 5, 1))
        second = sale("FV-2", total="500.00")
        line = received("800.00")
        link(line, [first])
        (offer,) = credits_for(second, read_links())
        self.assertEqual((offer.line.pk, offer.amount, offer.match.tier), (line.pk, D("500.00"), matching.NEAR_SURE))
        self.assertIn("déjà rattachée à la facture de vente n° FV-1", offer.match.tier_reason)

    def test_a_credit_wholly_used_elsewhere_is_dropped(self):
        first = sale("FV-1", total="300.00", sold_on=date(2026, 5, 1))
        second = sale("FV-2", total="300.00")
        link(received("300.00"), [first])
        self.assertEqual(self.offers(second), [])

    def test_a_credit_unlinked_by_hand_is_never_sure(self):
        document = sale("FV-1")
        line = received(settled_by_hand=True)
        (offer,) = credits_for(document, read_links())
        self.assertEqual((offer.line.pk, offer.match.tier), (line.pk, matching.NEAR_SURE))
        self.assertIn("déliée à la main", offer.match.tier_reason)

    def test_a_credit_chosen_or_read_by_the_till_is_not_proposed(self):
        document = sale("FV-1")
        received(income_source=income.OTHER)
        received(bank_type="VERSEMENT ESPECES")
        self.assertEqual(self.offers(document), [])

    def test_a_credit_already_paying_it_is_not_proposed_again(self):
        document = sale("FV-1", total="900.00")
        link(received(), [document])
        self.assertEqual(self.offers(document), [])

    def test_a_combination_carries_all_its_documents(self):
        first = sale("FV-1", total="300.00", sold_on=date(2026, 5, 20))
        second = sale("FV-2", total="200.00")
        received("500.00")
        (offer,) = credits_for(second, read_links())
        self.assertEqual([candidate.pk for candidate in offer.match.options[0]], [first.pk, second.pk])

    def test_sure_first_then_by_date_at_most_five(self):
        document = sale("FV-1", total="300.00")
        sure = received("300.00", day=date(2026, 6, 20))
        for day in range(2, 9):
            received("100.00", day=date(2026, 6, day))
        offers = credits_for(document, read_links())
        self.assertEqual(len(offers), 5)
        self.assertEqual(offers[0].line.pk, sure.pk)
        self.assertEqual([offer.line.operation_date.day for offer in offers[1:]], [2, 3, 4, 5])

    def test_the_credit_search(self):
        received("120.00", day=date(2026, 6, 3), counterparty="TRAITEUR EXEMPLARO")
        found = received("250.00", day=date(2026, 6, 12))
        make_credit("-250.00", date(2026, 6, 12), label="PRLV SEPA EXEMPLE EVENEMENTS")
        self.assertEqual(credits_matching("evenements 12/06/2026"), ([found], 0))
        self.assertEqual(credits_matching("250"), ([found], 0))
        self.assertEqual(credits_matching("250,00"), ([found], 0))
        self.assertEqual(credits_matching("rien du tout"), ([], 0))
