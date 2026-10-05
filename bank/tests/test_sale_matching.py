"""Which « facture de vente » a credit of the statement paid
(bank/sale_matching.py) - plain values in and out, no database.

The debit side's rules, the other way round: the amount exact to the cent of
what is still to be received, the date in the window - a late payment months
after the invoice, a deposit before it -, and the payer the bank prints
naming the customer. All three, in exactly one way, and the invoice paid by
nothing yet: « certaine », what the automatic pass links. Anything short of
it is a suggestion with its reason.

Customers are named by their own words - never a civility, a public body,
an event word or the bar's own name, which a payout prints as its payee - or
by the payer of an earlier link that added up.

Every customer, payer, date and amount is invented.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from bank import matching
from bank.sale_matching import (
    ADVANCE_WINDOW,
    LATE_PAYMENT_WINDOW,
    MAX_DOCUMENTS_PER_CREDIT,
    MAX_DOCUMENTS_SEARCHED,
    NEAR_SURE_GAP,
    TIER_RULES,
    Credit,
    SaleCandidate,
    customer_key,
    customer_words,
    match,
)
from common import format_money

D = Decimal
DAY = date(2026, 6, 15)
CUSTOMER = "Exemple Événements SARL"
PAYER = "EXEMPLE EVENEMENTS SARL"


def candidate(pk, customer=CUSTOMER, sold_on=date(2026, 6, 1), due="500.00", fresh=True, reference=""):
    label = " · ".join([f"n° {reference or f'FV-{pk}'}", *([customer] if customer else []), f"{sold_on:%d/%m/%Y}"])
    return SaleCandidate(pk=pk, customer=customer, sold_on=sold_on, due=D(due), fresh=fresh, label=label)


def credit(amount="500.00", counterparty=PAYER, label=None, day=DAY, recognised=False):
    label = label if label is not None else f"VIR SEPA RECU /FRM {counterparty or 'INCONNU'} /REF 0042"
    return Credit(operation_date=day, counterparty=counterparty, label=label, amount=D(amount), recognised=recognised)


def naming(*candidates, bar=frozenset(), aliases=None) -> dict:
    """How the bank may name each candidate's customer: its own words, and
    `aliases` ({customer: payer keys}) taught by earlier links."""
    aliases = aliases or {}
    return {
        customer_key(one.customer): matching.Naming(
            customer_words(one.customer, bar), frozenset(aliases.get(one.customer, ()))
        )
        for one in candidates
        if one.customer
    }


def found(credit_, *candidates, **kwargs):
    return match(credit_, list(candidates), naming(*candidates, **kwargs))


def pks(found_match) -> list[tuple[int, ...]]:
    return [tuple(one.pk for one in option) for option in found_match.options]


class WindowTests(SimpleTestCase):
    """An invoice dated up to 180 days before the credit (a late payment)
    or up to 90 days after it (a deposit for an event to come)."""

    def test_ninety_days_ahead_in_ninety_one_out(self):
        self.assertEqual(ADVANCE_WINDOW, timedelta(days=90))
        inside = candidate(1, sold_on=DAY + timedelta(days=90))
        self.assertEqual(found(credit(), inside).tier, matching.SURE)
        self.assertIsNone(found(credit(), candidate(1, sold_on=DAY + timedelta(days=91))))

    def test_a_hundred_and_eighty_days_behind_in_one_more_out(self):
        self.assertEqual(LATE_PAYMENT_WINDOW, matching.LATER_PAYMENT_WINDOW)
        self.assertEqual(found(credit(), candidate(1, sold_on=DAY - timedelta(days=180))).tier, matching.SURE)
        self.assertIsNone(found(credit(), candidate(1, sold_on=DAY - timedelta(days=181))))


class NamingTests(SimpleTestCase):
    def test_a_word_of_the_customer_names_it(self):
        self.assertTrue(found(credit(counterparty="EXEMPLE"), candidate(1)).confident)

    def test_five_letters_or_more_may_be_one_letter_off(self):
        caterer = candidate(1, customer="Traiteur Exemplaro")
        self.assertTrue(found(credit(counterparty="EXEMPLAR"), caterer).confident)
        self.assertTrue(found(credit(counterparty="EXEMPLAROS"), caterer).confident)
        self.assertFalse(found(credit(counterparty="EXEMPLAIRE"), caterer).confident)
        # Four letters: exact or nothing.
        nova = candidate(2, customer="Nova Exemplaire")
        self.assertFalse(found(credit(counterparty="NOVO"), nova).confident)

    def test_a_generic_word_never_names_a_customer(self):
        club = candidate(1, customer="Association Club Exemplaire")
        result = found(credit(counterparty="ASSOCIATION CLUB AUTREPART"), club)
        self.assertEqual((result.tier, result.confident), (matching.TO_CONFIRM, False))

    def test_a_document_naming_no_customer_is_never_named(self):
        result = found(credit(counterparty=PAYER), candidate(1, customer=""))
        self.assertEqual(result.tier, matching.TO_CONFIRM)

    def test_a_payer_taught_by_an_earlier_link_names_it(self):
        parent = candidate(1, customer="Mariage Exemplaire")
        payer = "M OU MME AUTRENOM"
        self.assertFalse(found(credit(counterparty=payer), parent).confident)
        taught = {"Mariage Exemplaire": {matching.alias_key(payer)}}
        self.assertTrue(found(credit(counterparty=payer), parent, aliases=taught).confident)

    def test_a_blank_counterparty_names_through_history_only(self):
        """A cheque deposit or the bank's own line: its label stands in for
        the payee, and a word of it naming the customer is a coincidence."""
        deposit = credit(counterparty="", label="REMISE EXEMPLE EVENEMENTS 0001234")
        self.assertFalse(found(deposit, candidate(1)).confident)
        taught = {CUSTOMER: {matching.alias_key(matching.payee_of("", deposit.label))}}
        # Named through history, yet never SURE: a label key is the same for
        # every deposit of that wording, so the amount alone would link.
        taught_match = found(deposit, candidate(1), aliases=taught)
        self.assertFalse(taught_match.confident)
        self.assertEqual(taught_match.tier, matching.NEAR_SURE)
        self.assertEqual(pks(taught_match), [(1,)])
        self.assertTrue(deposit.payee_from_label)

    def test_a_sum_named_by_the_label_alone_is_never_sure(self):
        deposit = credit(amount="500.00", counterparty="", label="REMISE CHEQUES N 0001234")
        taught = {CUSTOMER: {matching.alias_key(matching.payee_of("", deposit.label))}}
        summed = found(
            deposit, candidate(1, due="200.00"), candidate(2, due="300.00", sold_on=date(2026, 6, 2)), aliases=taught
        )
        self.assertFalse(summed.confident)
        self.assertEqual(summed.tier, matching.NEAR_SURE)
        self.assertEqual(pks(summed), [(1, 2)])

    def test_the_customer_key_is_its_words(self):
        self.assertEqual(customer_key("Exemple  Événements, SARL"), "EXEMPLE EVENEMENTS SARL")


class CustomerWordsTests(SimpleTestCase):
    """matching.GENERIC_WORDS is a SUPPLIER list: it holds no civility, no
    public body, no event word - and a customer is often one of those."""

    def test_a_civility_never_names(self):
        madame = candidate(1, customer="Mme Exemplaire")
        self.assertEqual(found(credit(counterparty="MME AUTREFOIS"), madame).tier, matching.TO_CONFIRM)
        self.assertNotIn("MME", customer_words("Mme Exemplaire", frozenset()))

    def test_a_public_body_never_names(self):
        mairie = candidate(1, customer="Mairie de Villexemple")
        self.assertEqual(found(credit(counterparty="MAIRIE DE BOURGEXEMPLE"), mairie).tier, matching.TO_CONFIRM)

    def test_an_event_word_never_names(self):
        wedding = candidate(1, customer="Mariage Exemplaire")
        self.assertEqual(found(credit(counterparty="MARIAGE AUTREFOIS"), wedding).tier, matching.TO_CONFIRM)

    def test_the_bar_s_own_name_never_names(self):
        """A payout prints the bar's name as its payee: a customer sharing a
        word with it is not named by every payout nobody recognised."""
        bar = matching.supplier_words("Le Comptoir Exemplaire")
        events = candidate(1, customer="Comptoir Événements")
        self.assertEqual(customer_words("Comptoir Événements", bar), frozenset())
        result = found(credit(counterparty="LE COMPTOIR EXEMPLAIRE"), events, bar=bar)
        self.assertEqual(result.tier, matching.TO_CONFIRM)
        self.assertTrue(found(credit(counterparty="COMPTOIR EVENEMENTS"), events).confident)


class TierTests(SimpleTestCase):
    def test_sure_only_fresh_exact_named_and_alone(self):
        result = found(credit(), candidate(1, sold_on=date(2026, 6, 1)))
        self.assertEqual((result.tier, result.confident, pks(result)), (matching.SURE, True, [(1,)]))
        self.assertIn(CUSTOMER, result.reason)
        self.assertIn("01/06/2026", result.reason)

    def test_a_partly_paid_invoice_with_the_same_balance_beside_a_fresh_one(self):
        """The pass must not link the fresh one: the older invoice's balance
        is the likelier truth, and the page would say « plusieurs »."""
        result = found(
            credit(), candidate(1, sold_on=date(2026, 6, 1)), candidate(2, sold_on=date(2026, 4, 1), fresh=False)
        )
        self.assertEqual((result.tier, result.confident), (matching.TO_CONFIRM, False))
        self.assertEqual(sorted(pks(result)), [(1,), (2,)])

    def test_the_exact_balance_of_one_invoice_paid_in_part(self):
        result = found(credit(), candidate(1, fresh=False, reference="FV-77"))
        self.assertEqual((result.tier, result.confident), (matching.NEAR_SURE, False))
        self.assertIn("FV-77", result.reason)

    def test_a_few_cents_off(self):
        result = found(credit("500.00"), candidate(1, due="500.04"))
        self.assertEqual((result.tier, pks(result)), (matching.NEAR_SURE, [(1,)]))
        result = found(credit("500.00"), candidate(1, due="499.96"))
        self.assertEqual((result.tier, pks(result)), (matching.NEAR_SURE, [(1,)]))
        # Six cents more is a deposit possible; a few euros less, nothing.
        self.assertEqual(found(credit("500.00"), candidate(1, due="500.06")).tier, matching.TO_CONFIRM)
        self.assertIsNone(found(credit("500.00"), candidate(1, due="497.00")))

    def test_two_invoices_a_few_cents_off_are_a_question(self):
        result = found(credit("500.00"), candidate(1, due="500.03"), candidate(2, due="499.98"))
        self.assertEqual(result.tier, matching.TO_CONFIRM)
        self.assertEqual(pks(result), [(2,), (1,)])

    def test_several_invoices_of_that_customer_at_that_amount(self):
        result = found(credit(), candidate(1, sold_on=date(2026, 6, 1)), candidate(2, sold_on=date(2026, 6, 14)))
        self.assertEqual(result.tier, matching.TO_CONFIRM)
        # By date distance: the one of the 14th first.
        self.assertEqual(pks(result), [(2,), (1,)])

    def test_a_deposit(self):
        result = found(credit("300.00"), candidate(1, due="1000.00"))
        self.assertEqual((result.tier, pks(result)), (matching.TO_CONFIRM, [(1,)]))
        self.assertIn(format_money(D("300.00")), result.reason)
        self.assertIn(format_money(D("1000.00")), result.reason)

    def test_the_same_amount_from_a_payer_the_bank_does_not_name_as_the_customer(self):
        result = found(credit(counterparty="AUTRE PAYEUR"), candidate(1))
        self.assertEqual((result.tier, pks(result)), (matching.TO_CONFIRM, [(1,)]))

    def test_at_most_five_options(self):
        many = [candidate(pk, sold_on=date(2026, 6, pk)) for pk in range(1, 9)]
        self.assertEqual(len(found(credit(), *many).options), 5)


class CombinationTests(SimpleTestCase):
    """One transfer for several invoices of one customer."""

    def test_one_sum_is_sure(self):
        result = found(credit("500.00"), candidate(1, due="300.00"), candidate(2, due="200.00"))
        self.assertEqual((result.tier, pks(result)), (matching.SURE, [(1, 2)]))

    def test_two_sums_are_a_question(self):
        result = found(
            credit("500.00"),
            candidate(1, due="300.00"),
            candidate(2, due="200.00"),
            candidate(3, due="250.00"),
            candidate(4, due="250.00"),
        )
        self.assertEqual((result.tier, len(result.options)), (matching.TO_CONFIRM, 2))

    def test_never_more_than_four_documents(self):
        self.assertEqual(MAX_DOCUMENTS_PER_CREDIT, 4)
        fives = [candidate(pk, due="100.00", sold_on=date(2026, 6, pk)) for pk in range(1, 6)]
        self.assertIsNone(found(credit("500.00"), *fives))
        self.assertEqual(found(credit("400.00"), *fives[:4]).tier, matching.SURE)

    def test_never_among_more_than_the_twelve_most_recent(self):
        self.assertEqual(MAX_DOCUMENTS_SEARCHED, 12)
        recent = [candidate(pk, due="10.00", sold_on=date(2026, 6, pk)) for pk in range(2, 14)]
        oldest = candidate(1, due="7.00", sold_on=date(2026, 5, 1))
        self.assertIsNone(found(credit("17.00"), oldest, *recent))

    def test_only_fresh_invoices_are_summed(self):
        self.assertIsNone(found(credit("500.00"), candidate(1, due="300.00"), candidate(2, due="200.00", fresh=False)))

    def test_none_when_an_exact_single_one_exists(self):
        result = found(
            credit("500.00"), candidate(1, due="500.00"), candidate(2, due="300.00"), candidate(3, due="200.00")
        )
        self.assertEqual((result.tier, pks(result)), (matching.SURE, [(1,)]))


class EdgeTests(SimpleTestCase):
    def test_nothing_for_nothing_or_money_out(self):
        for amount in ("0.00", "-500.00"):
            with self.subTest(amount=amount):
                self.assertIsNone(found(credit(amount), candidate(1)))

    def test_an_invoice_with_nothing_to_receive_is_skipped(self):
        self.assertIsNone(found(credit("500.00"), candidate(1, due="0.00")))

    def test_a_recognised_credit_gets_no_unnamed_suggestion(self):
        """A payout or a deposit a till rule reads - or a payer retained for a
        compared source - is never offered an invoice on its amount alone."""
        unnamed = credit(counterparty="AUTRE PAYEUR", recognised=True)
        self.assertIsNone(found(unnamed, candidate(1)))
        self.assertIsNotNone(found(credit(counterparty="AUTRE PAYEUR"), candidate(1)))

    def test_the_gap_is_the_debit_side_s(self):
        self.assertEqual(NEAR_SURE_GAP, matching.NEAR_SURE_GAP)


class TierRulesTests(SimpleTestCase):
    """The rules the page states, built from the constants: the words cannot
    drift from the thresholds."""

    def test_the_words_carry_the_figures(self):
        rules = {tier: (title, text) for tier, title, text in TIER_RULES}
        self.assertEqual(
            [tier for tier, _title, _text in TIER_RULES], [matching.SURE, matching.NEAR_SURE, matching.TO_CONFIRM]
        )
        self.assertEqual([title for title, _text in rules.values()], ["Certaine", "Quasi-sûre", "À confirmer"])
        self.assertIn(f"{LATE_PAYMENT_WINDOW.days} jours avant", rules[matching.SURE][1])
        self.assertIn(f"{ADVANCE_WINDOW.days} jours après", rules[matching.SURE][1])
        self.assertIn(f"{format_money(NEAR_SURE_GAP)} €", rules[matching.NEAR_SURE][1])
        self.assertIn("Rien n'est rattaché seul", rules[matching.TO_CONFIRM][1])
