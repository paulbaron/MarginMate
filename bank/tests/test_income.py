"""« Entrées d'argent »: what came IN on the account, beside what the till was
paid over the same days (bank/income.py).

What the page promises, and what each test below holds it to:

* a card payout is recognised by the words « TOTAL ENCAISSE <number>
  EURO(S) » in its label, never by a name; its gross is that number, its
  net the line, its commission the difference, said as it comes - and a
  label whose number cannot be read whole is no payout at all, rather than
  a payout with a wrong gross;
* cash and cheque deposits are read off the bank type; everything else is
  « Autres entrées », named by a person, « Sans catégorie » first;
* the running balance « ventes carte pas encore versées » counts from an
  anchor the statement's first payout chooses, over the WHOLE history - a
  window never moves it, and a payout that never came is a step it never
  comes back down from;
* an exact run is two amounts equal to the cent over consecutive card days,
  latest first then shortest, each day claimed once, never further back
  than eight days;
* the till side says what it could not read rather than counting it as a
  quiet day.

Every payee, provider, amount and date below is invented.
"""

import itertools
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from bank import income
from bank.models import BankTransaction
from bank.spending import NO_CATEGORY
from common import DateRange
from recipes.models import PosDailyPayment, PosProduct, PosProductDailyQuantity

_PKS = itertools.count(1)

#: An invented merchant and payment provider: the real ones are nobody's
#: business in a public repository, and the rule reads neither.
MERCHANT = "BAR EXEMPLE"
PROVIDER = "PRESTATAIRE INVENTE"

JUNE = DateRange(date(2026, 6, 1), date(2026, 6, 30))


def euros(value) -> Decimal:
    return Decimal(str(value))


def payout_label(gross: str, number: int = 1) -> str:
    """A payout's label, shaped like the statement's: who pays, a reference,
    the provider, then « TOTAL ENCAISSE <gross> EUROS »."""
    return (
        f"VIR SEPA RECU /FRM {MERCHANT} /EID /RNF TRANSFERT {PROVIDER} {number:07d} "
        f"TOTAL ENCAISSE {gross} EUROS {MERCHANT}"
    )


def unsaved(day, amount, label="VIR SEPA RECU /FRM CLIENT EXEMPLE", bank_type="VIREMENT", category=""):
    """A credit as the pure functions see it - no database."""
    return BankTransaction(
        pk=next(_PKS),
        operation_date=day,
        bank_type=bank_type,
        label=label,
        amount=euros(amount),
        category=category,
    )


def payout(day, gross, net=None) -> income.Entry:
    """A payout entry, net equal to gross unless said."""
    return income.entry_for(unsaved(day, net if net is not None else gross, payout_label(gross)))


class PayoutRecognitionTests(SimpleTestCase):
    def test_the_gross_is_the_number_the_label_prints_with_a_dot(self):
        self.assertEqual(income.payout_gross(payout_label("987.65")), euros("987.65"))
        self.assertEqual(income.payout_gross(payout_label("120.0")), euros("120.00"))

    def test_a_whole_number_and_a_comma_are_read_too(self):
        self.assertEqual(income.payout_gross(payout_label("300")), euros("300"))
        self.assertEqual(income.payout_gross(payout_label("12,50")), euros("12.50"))

    def test_euro_singular_and_thousands_grouped_by_a_space(self):
        self.assertEqual(income.payout_gross("TOTAL ENCAISSE 1 234.50 EURO"), euros("1234.50"))

    def test_no_number_is_no_payout(self):
        self.assertIsNone(income.payout_gross("VIR SEPA RECU TOTAL ENCAISSE EUROS"))
        self.assertIsNone(income.payout_gross("VIR SEPA RECU /FRM CLIENT EXEMPLE"))
        self.assertIsNone(income.payout_gross(""))
        self.assertIsNone(income.payout_gross(None))

    def test_a_number_the_rule_cannot_read_whole_is_no_payout_rather_than_a_wrong_gross(self):
        """« 1,234.50 » read as far as it goes would be 1,234 - a payout of
        one euro. Refused, the line lands in « Autres entrées », in sight."""
        self.assertIsNone(income.payout_gross("TOTAL ENCAISSE 1,234.50 EUROS"))

    def test_the_rule_reads_the_words_not_the_provider(self):
        label = "VIR RECU /FRM UN AUTRE PRESTATAIRE TOTAL ENCAISSE 80.00 EUROS"
        self.assertEqual(income.source_of(unsaved(date(2026, 6, 2), "79.50", label)), income.CARD)

    def test_the_commission_is_gross_less_net(self):
        entry = payout(date(2026, 6, 2), "200.00", "198.60")
        self.assertEqual((entry.gross, entry.net, entry.commission), (euros("200.00"), euros("198.60"), euros("1.40")))
        self.assertEqual(entry.commission_rate, euros("0.70"))

    def test_an_odd_net_above_the_gross_is_said_as_it_is(self):
        entry = payout(date(2026, 6, 2), "50.00", "50.25")
        self.assertEqual(entry.commission, euros("-0.25"))
        self.assertEqual(entry.commission_rate, euros("-0.50"))

    def test_a_payout_of_nothing_has_no_rate(self):
        self.assertIsNone(payout(date(2026, 6, 2), "0.00", "0.00").commission_rate)


class SourceTests(SimpleTestCase):
    def test_deposits_are_read_off_the_bank_type(self):
        cases = {
            "VERSEMENT ESPECES": income.CASH,
            "Versement espèces": income.CASH,
            "REMISE CHEQUES": income.CHEQUE,
            "REMISE CHÈQUE": income.CHEQUE,
            "VIREMENT": income.OTHER,
            "": income.OTHER,
        }
        for bank_type, expected in cases.items():
            with self.subTest(bank_type=bank_type):
                self.assertEqual(income.source_of(unsaved(date(2026, 6, 2), "10", "DEPOT", bank_type)), expected)

    def test_what_banque_calls_each_one(self):
        self.assertEqual(payout(date(2026, 6, 2), "10.00").name, "Versement carte")
        cash = income.entry_for(unsaved(date(2026, 6, 2), "100", "VERSEMENT", "VERSEMENT ESPECES"))
        self.assertEqual(cash.name, "Dépôt d'espèces")
        named = income.entry_for(unsaved(date(2026, 6, 2), "500", category="Privatisation"))
        self.assertEqual((named.name, named.unnamed), ("Privatisation", False))
        blank = income.entry_for(unsaved(date(2026, 6, 2), "500", category="  "))
        self.assertEqual((blank.name, blank.unnamed), (NO_CATEGORY, True))

    def test_a_category_typed_on_a_payout_does_not_rename_it(self):
        entry = income.entry_for(unsaved(date(2026, 6, 2), "10", payout_label("10.00"), category="Divers"))
        self.assertEqual((entry.source, entry.name, entry.unnamed), (income.CARD, "Versement carte", False))


class RateTests(SimpleTestCase):
    def test_edges(self):
        self.assertIsNone(income.rate(euros("1"), euros("0")))
        self.assertIsNone(income.rate(euros("1"), euros("-10")))
        self.assertIsNone(income.rate(None, euros("10")))
        self.assertIsNone(income.rate(euros("1"), None))
        self.assertEqual(income.rate(euros("0"), euros("10")), euros("0.00"))
        self.assertEqual(income.rate(euros("1"), euros("3")), euros("33.33"))


def days_from(first: date, *amounts) -> dict:
    """{day: card sold}, one day after another from `first`; None skips a
    day (the bar shut, no card row at all)."""
    out = {}
    for offset, amount in enumerate(amounts):
        if amount is not None:
            out[first + timedelta(days=offset)] = euros(amount)
    return out


class AnchorTests(SimpleTestCase):
    """Where the balance counts from: the day, in the week before the
    statement's first payout, whose card sold up to it is closest to what
    that payout paid."""

    def test_the_anchor_is_where_the_first_payout_adds_up(self):
        # 1-5 June: 10, 20, 30, 40, 50. Paid on the 6th: 90, the 4th and 5th.
        card = days_from(date(2026, 6, 1), 10, 20, 30, 40, 50)
        balance = income.running_balance(card, [payout(date(2026, 6, 6), "90.00")])
        self.assertEqual((balance.anchor, balance.first_payout), (date(2026, 6, 4), date(2026, 6, 6)))
        self.assertEqual(list(balance.pending.values()), [euros("0.00")])

    def test_a_tie_keeps_the_latest_day(self):
        # A shut day or three before the 1st: from any of 29/05 to 01/06 the
        # payout of the 3rd adds up exactly. The latest counts the fewest
        # days the payout may not have paid.
        card = {date(2026, 5, 28): euros("30"), date(2026, 6, 1): euros("100")}
        balance = income.running_balance(card, [payout(date(2026, 6, 3), "100.00")])
        self.assertEqual(balance.anchor, date(2026, 6, 1))

    def test_the_anchor_is_looked_for_in_the_week_before_and_no_further(self):
        # 500 sold eight days before would make the payout add up; out of
        # reach, the closest in the week is the 100 two days before.
        payday = date(2026, 6, 10)
        card = {payday - timedelta(days=8): euros("500"), payday - timedelta(days=2): euros("100")}
        balance = income.running_balance(card, [payout(payday, "600.00")])
        self.assertEqual(balance.anchor, payday - timedelta(days=2))
        self.assertEqual(balance.pending[next(iter(balance.pending))], euros("-500.00"))

    def test_a_till_history_starting_after_the_statement_counts_from_its_first_day(self):
        """Payouts before the till's first card day pay days the till never
        recorded: not counted, and the anchor never goes before that day."""
        first = date(2026, 6, 10)
        card = days_from(first, 40, 60, 80)
        early = [payout(date(2026, 6, 1), "70.00"), payout(date(2026, 6, 5), "90.00")]
        on_the_first_day = payout(first, "25.00")
        paid = payout(date(2026, 6, 12), "100.00")
        balance = income.running_balance(card, early + [on_the_first_day, paid])
        self.assertEqual(balance.first_payout, date(2026, 6, 12))
        self.assertEqual(balance.anchor, first)
        self.assertEqual(list(balance.pending), [paid.line.pk])

    def test_no_card_day_or_no_payout_is_no_balance_and_says_why(self):
        self.assertEqual(income.running_balance({}, [payout(date(2026, 6, 2), "10")]).reason, income.NO_CARD_DAYS)
        card = days_from(date(2026, 6, 1), 10)
        self.assertEqual(income.running_balance(card, []).reason, income.NO_PAYOUT)
        self.assertEqual(income.running_balance(card, [payout(date(2026, 6, 1), "10")]).reason, income.NO_PAYOUT)
        self.assertIsNone(income.running_balance(card, []).anchor)


class BalanceTests(SimpleTestCase):
    def test_payouts_paying_what_was_sold_keep_the_balance_level(self):
        card = days_from(date(2026, 6, 1), 100, 200, 300, 400)
        payouts = [
            payout(date(2026, 6, 2), "100.00"),
            payout(date(2026, 6, 3), "200.00"),
            payout(date(2026, 6, 5), "300.00"),
        ]
        balance = income.running_balance(card, payouts)
        # The 4th's 400 is sold before the 5th and not paid yet: the level.
        self.assertEqual(
            [balance.pending[one.line.pk] for one in payouts], [euros("0"), euros("0"), euros("400")]
        )

    def test_a_missing_payout_is_a_step_that_never_comes_back_down(self):
        card = days_from(date(2026, 6, 1), *([50] * 10))
        paid_days = [2, 3, 4, 6, 7, 8, 9, 10, 11]  # the payout for the 4th never came
        payouts = [payout(date(2026, 6, day), "50.00") for day in paid_days]
        pending = [income.running_balance(card, payouts).pending[one.line.pk] for one in payouts]
        self.assertEqual(pending[:3], [euros("0")] * 3)
        self.assertEqual(pending[3:], [euros("50")] * 6)

    def test_every_payout_of_one_day_has_its_own_balance(self):
        card = days_from(date(2026, 6, 1), 30, 70)
        first, second = payout(date(2026, 6, 3), "30.00"), payout(date(2026, 6, 3), "70.00")
        balance = income.running_balance(card, [first, second])
        self.assertEqual((balance.pending[first.line.pk], balance.pending[second.line.pk]), (euros("70"), euros("0")))


def runs_of(card, payouts):
    found = income.exact_runs(card, payouts)
    return [found.get(one.line.pk) for one in payouts]


class ExactRunTests(SimpleTestCase):
    def test_one_day_and_two_consecutive_days(self):
        card = days_from(date(2026, 6, 1), 45, 60, 75)
        self.assertEqual(runs_of(card, [payout(date(2026, 6, 3), "60.00")]), [(date(2026, 6, 2), date(2026, 6, 2))])
        self.assertEqual(
            runs_of(card, [payout(date(2026, 6, 4), "135.00")]), [(date(2026, 6, 2), date(2026, 6, 3))]
        )

    def test_the_run_ending_latest_wins(self):
        card = days_from(date(2026, 6, 1), 50, 50)
        self.assertEqual(runs_of(card, [payout(date(2026, 6, 4), "50.00")]), [(date(2026, 6, 2), date(2026, 6, 2))])

    def test_then_the_shortest(self):
        # A refund day nets negative: 30 − 30 + 50 is 50 too, over three days.
        card = days_from(date(2026, 6, 1), 30, -30, 50)
        self.assertEqual(runs_of(card, [payout(date(2026, 6, 4), "50.00")]), [(date(2026, 6, 3), date(2026, 6, 3))])

    def test_a_claimed_day_is_never_paid_twice(self):
        card = days_from(date(2026, 6, 1), 80, 80)
        first, second, third = (payout(date(2026, 6, 4), "80.00") for _ in range(3))
        self.assertEqual(
            runs_of(card, [first, second, third]),
            [(date(2026, 6, 2), date(2026, 6, 2)), (date(2026, 6, 1), date(2026, 6, 1)), None],
        )

    def test_a_claimed_day_breaks_a_run(self):
        card = days_from(date(2026, 6, 1), 10, 20, 30)
        claim_the_middle = payout(date(2026, 6, 4), "20.00")
        across_it = payout(date(2026, 6, 5), "40.00")
        self.assertEqual(runs_of(card, [claim_the_middle, across_it])[1], None)

    def test_no_match_is_none(self):
        card = days_from(date(2026, 6, 1), 45, 60)
        self.assertEqual(runs_of(card, [payout(date(2026, 6, 3), "59.99")]), [None])

    def test_a_shut_day_does_not_break_a_run(self):
        # Saturday and Monday sold by card, Sunday shut: one run.
        card = days_from(date(2026, 6, 6), 60, None, 40)
        self.assertEqual(runs_of(card, [payout(date(2026, 6, 10), "100.00")]), [(date(2026, 6, 6), date(2026, 6, 8))])

    def test_nothing_further_back_than_eight_days(self):
        payday = date(2026, 6, 20)
        card = {payday - timedelta(days=9): euros("40"), payday - timedelta(days=1): euros("60")}
        self.assertEqual(runs_of(card, [payout(payday, "100.00")]), [None])
        card[payday - timedelta(days=8)] = card.pop(payday - timedelta(days=9))
        self.assertEqual(runs_of(card, [payout(payday, "100.00")]), [(payday - timedelta(days=8), payday - timedelta(days=1))])

    def test_the_payout_day_itself_is_never_in_its_run(self):
        card = days_from(date(2026, 6, 1), 70)
        self.assertEqual(runs_of(card, [payout(date(2026, 6, 1), "70.00")]), [None])


class Fixtures:
    """Credits on the statement and the till's days, all invented."""

    def setUp(self):
        super().setUp()
        self.counter = 0
        self.product = PosProduct.objects.create(name="Pinte Exemple")
        self.other_product = PosProduct.objects.create(name="Planche Exemple")

    def credit(self, day, amount, label="VIR SEPA RECU /FRM CLIENT EXEMPLE", bank_type="VIREMENT", category="", **kwargs):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            bank_type=bank_type,
            label=f"{label} REF{self.counter:04d}",
            counterparty=kwargs.pop("counterparty", "CLIENT EXEMPLE"),
            amount=euros(amount),
            category=category,
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint=f"credit-{self.counter}",
            **kwargs,
        )

    def payout(self, day, gross, net):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            bank_type="VIREMENT",
            label=payout_label(gross, self.counter),
            counterparty=MERCHANT,
            amount=euros(net),
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint=f"payout-{self.counter}",
        )

    def debit(self, day, amount):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            label=f"PRLV SEPA FOURNISSEUR EXEMPLE {self.counter}",
            amount=-euros(amount),
            kind=BankTransaction.Kind.DEBIT,
            fingerprint=f"debit-{self.counter}",
        )

    def paid(self, day, **methods):
        """The till's payments of a day: paid(day, CB=("12.50", 3), Cash=…)."""
        for method, (amount, count) in methods.items():
            PosDailyPayment.objects.create(sold_on=day, method=method, amount=euros(amount), payments=count)

    def sold(self, day, ttc, read=True, product=None):
        PosProductDailyQuantity.objects.create(
            product=product or self.product,
            sold_on=day,
            quantity=1,
            revenue_ttc=euros(ttc if read else 0),
            revenue_read=read,
        )


class SourcesTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.payout(date(2026, 6, 3), "200.00", "198.00")
        self.payout(date(2026, 6, 9), "100.00", "99.40")
        self.credit(date(2026, 6, 10), "300.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES")
        self.credit(date(2026, 6, 11), "150.00", "REMISE CHEQUES", "REMISE CHEQUES")
        self.credit(date(2026, 6, 12), "900.00", category="Privatisation")
        self.credit(date(2026, 6, 13), "40.00")
        self.credit(date(2026, 6, 14), "60.00", category="Remboursement")
        self.credit(date(2026, 6, 15), "10.00")
        self.debit(date(2026, 6, 15), "75.00")

    def test_every_credit_of_the_window_and_nothing_else(self):
        report = income.income_for(JUNE)
        # 198,00 + 99,40 (net) + 300 + 150 + 900 + 40 + 60 + 10; the debit is no entry.
        self.assertEqual((report.received_total, report.received_count), (euros("1757.40"), 8))

    def test_each_source(self):
        report = income.income_for(JUNE)
        self.assertEqual((report.card_net, report.card_gross, report.card_commission), (euros("297.40"), euros("300.00"), euros("2.60")))
        self.assertEqual(report.card_commission_rate, euros("0.87"))
        self.assertEqual((report.cash_total, report.cash_count), (euros("300.00"), 1))
        self.assertEqual((report.cheque_total, report.cheque_count), (euros("150.00"), 1))
        self.assertEqual((report.others_total, len(report.others)), (euros("1010.00"), 4))

    def test_the_other_entries_by_category_unnamed_first(self):
        report = income.income_for(JUNE)
        self.assertEqual(
            [(one.name, one.amount, one.count) for one in report.other_categories],
            [(NO_CATEGORY, euros("50.00"), 2), ("Privatisation", euros("900.00"), 1), ("Remboursement", euros("60.00"), 1)],
        )
        # The list: the work first, then the biggest.
        self.assertEqual([one.net for one in report.others], [euros("40.00"), euros("10.00"), euros("900.00"), euros("60.00")])
        self.assertEqual(report.unnamed_others, 2)

    def test_the_categories_offered_are_the_ones_typed_on_credits(self):
        debit = self.debit(date(2026, 6, 16), "20.00")
        debit.category = "Loyer"
        debit.save()
        self.assertEqual(income.known_categories(), ["Privatisation", "Remboursement"])


class TillSideTests(Fixtures, TestCase):
    def test_takings_count_the_read_days_and_name_the_others(self):
        self.sold(date(2026, 6, 2), "100.00")
        self.sold(date(2026, 6, 2), "20.00", product=self.other_product)
        self.sold(date(2026, 6, 3), "80.00", read=False)
        self.paid(date(2026, 6, 2), CB=("110.00", 4), Cash=("12.00", 1))
        self.paid(date(2026, 6, 3), CB=("80.00", 2))
        report = income.income_for(JUNE)
        self.assertEqual(report.takings, euros("120.00"))
        self.assertEqual(report.unread_revenue_days, [date(2026, 6, 3)])
        self.assertEqual(report.days_without_payments, [])

    def test_tips_are_payments_less_takings_on_the_days_both_are_read(self):
        self.sold(date(2026, 6, 2), "100.00")
        self.paid(date(2026, 6, 2), CB=("103.50", 3))
        # Unread takings: its payments would read as a tip of 60 €.
        self.sold(date(2026, 6, 3), "60.00", read=False)
        self.paid(date(2026, 6, 3), CB=("60.00", 1))
        # No payments read: it says nothing about tips either.
        self.sold(date(2026, 6, 4), "45.00")
        report = income.income_for(JUNE)
        self.assertEqual((report.tips, report.tips_days), (euros("3.50"), 1))
        self.assertEqual(report.days_without_payments, [date(2026, 6, 4)])
        self.assertEqual(report.till_total, euros("163.50"))

    def test_a_day_that_took_nothing_is_no_tip_and_no_gap(self):
        self.sold(date(2026, 6, 2), "0.00")
        self.paid(date(2026, 6, 2), CB=("0.00", 0))
        report = income.income_for(JUNE)
        self.assertEqual((report.tips, report.tips_days, report.days_without_payments), (euros("0"), 1, []))

    def test_one_row_per_means_of_payment(self):
        # The statement covers the till's day: one before its first line is
        # compared with nothing (TillBeforeTheStatementTests).
        self.debit(date(2026, 6, 1), "1.00")
        self.paid(
            date(2026, 6, 2),
            CB=("200.00", 5),
            Cash=("30.00", 2),
            Avoir=("500.00", 1),
            **{"Bon cadeau": ("25.00", 1), PosDailyPayment.UNREAD: ("9.00", 1)},
        )
        self.payout(date(2026, 6, 4), "195.00", "193.80")
        self.credit(date(2026, 6, 5), "25.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES")
        self.credit(date(2026, 6, 6), "500.00", category="Acompte")
        rows = {row.label: row for row in income.income_for(JUNE).rows}
        self.assertEqual(list(rows), ["Carte", "Espèces", "Avoir", "Bon cadeau", "Illisible", "Autres entrées"])
        card = rows["Carte"]
        self.assertEqual((card.till, card.bank, card.net, card.commission), (euros("200"), euros("195"), euros("193.80"), euros("1.20")))
        self.assertEqual(card.difference, euros("-5.00"))
        self.assertEqual(rows["Espèces"].difference, euros("-5.00"))
        # The till alone knows an « Avoir » - paid days before, by another way.
        self.assertEqual((rows["Avoir"].till, rows["Avoir"].bank, rows["Avoir"].difference), (euros("500"), None, None))
        self.assertIn("Autres entrées", rows["Avoir"].note)
        self.assertEqual((rows["Autres entrées"].till, rows["Autres entrées"].bank), (None, euros("500")))

    def test_cheques_have_a_row_only_where_there_are_any(self):
        self.paid(date(2026, 6, 2), CB=("10.00", 1))
        self.assertNotIn("Chèques", [row.label for row in income.income_for(JUNE).rows])
        self.credit(date(2026, 6, 5), "80.00", "REMISE CHEQUES", "REMISE CHEQUES")
        self.assertIn("Chèques", [row.label for row in income.income_for(JUNE).rows])

    def test_where_each_side_stops(self):
        self.sold(date(2026, 7, 20), "10.00")
        self.paid(date(2026, 7, 19), CB=("10.00", 1))
        self.debit(date(2026, 7, 25), "5.00")
        report = income.income_for(JUNE)
        self.assertEqual(
            (report.last_till_day, report.last_payment_day, report.last_statement_day),
            (date(2026, 7, 20), date(2026, 7, 19), date(2026, 7, 25)),
        )

    def test_nothing_at_all(self):
        report = income.income_for(JUNE)
        self.assertEqual((report.received_total, report.takings, report.till_total, report.rows[0].till), (0, 0, 0, 0))
        self.assertEqual((report.months, report.payouts, report.last_till_day), ([], [], None))
        self.assertEqual(report.balance.reason, income.NO_CARD_DAYS)


class WindowTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        for day in (date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 30), date(2026, 7, 1)):
            self.credit(day, "10.00")
            self.sold(day, "7.00")
            self.paid(day, Cash=("7.00", 1))

    def test_both_ends_are_included_on_both_sides(self):
        report = income.income_for(JUNE)
        self.assertEqual(report.received_count, 2)
        self.assertEqual((report.takings, report.till_total), (euros("14.00"), euros("14.00")))

    def test_an_empty_window_is_everything(self):
        report = income.income_for(DateRange())
        self.assertEqual((report.received_count, report.takings), (4, euros("28.00")))

    def test_the_months_run_from_the_first_to_the_last_holding_anything(self):
        self.credit(date(2026, 9, 3), "5.00")
        months = income.income_for(DateRange()).months
        self.assertEqual(
            [month.first_day for month in months],
            [date(2026, 5, 1), date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)],
        )
        self.assertEqual((months[3].takings, months[3].cash_sold), (0, 0))
        self.assertEqual((months[1].takings, months[1].cash_sold), (euros("14.00"), euros("14.00")))

    def test_a_window_with_nothing_has_no_months(self):
        self.assertEqual(income.income_for(DateRange(date(2020, 1, 1), date(2020, 12, 31))).months, [])


class MonthTests(Fixtures, TestCase):
    def test_each_side_in_its_own_month(self):
        """The till by the day of the sale, the bank by the day it received:
        the payout of the 1st pays the month before."""
        self.paid(date(2026, 5, 30), CB=("120.00", 3), Cash=("20.00", 1))
        self.sold(date(2026, 5, 30), "140.00")
        self.payout(date(2026, 6, 1), "120.00", "119.20")
        self.credit(date(2026, 6, 2), "20.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES")
        may, june = income.income_for(DateRange()).months
        self.assertEqual((may.takings, may.card_sold, may.cash_sold, may.payouts_gross), (euros("140"), euros("120"), euros("20"), 0))
        self.assertEqual(
            (june.payouts_gross, june.commission, june.net, june.cash_deposited, june.card_sold),
            (euros("120"), euros("0.80"), euros("119.20"), euros("20"), 0),
        )


class TheBalanceOverTheWholeHistoryTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        # Two weeks of card sales, 50 € a day; each payout pays the day
        # before it, but the one for 10/06 never came.
        for offset in range(14):
            self.paid(date(2026, 6, 1) + timedelta(days=offset), CB=("50.00", 2))
        self.lines = {}
        for day in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
            self.lines[day] = self.payout(date(2026, 6, day), "50.00", "49.70")

    def pending(self, window):
        return {row.entry.day.day: row.pending for row in income.income_for(window).payouts}

    def test_a_window_does_not_change_the_balance(self):
        whole = self.pending(DateRange())
        late = self.pending(DateRange(date(2026, 6, 12), date(2026, 6, 30)))
        self.assertEqual(late, {day: whole[day] for day in (12, 13, 14)})
        self.assertEqual(late, {12: euros("50.00"), 13: euros("50.00"), 14: euros("50.00")})

    def test_the_headline_is_the_windows_last_payout(self):
        report = income.income_for(DateRange(date(2026, 6, 1), date(2026, 6, 9)))
        self.assertEqual((report.last_payout.entry.day, report.last_payout.pending), (date(2026, 6, 9), euros("0.00")))
        self.assertEqual(report.balance_points[-1], (date(2026, 6, 9), euros("0.00")))

    def test_the_runs_do_not_depend_on_the_window_either(self):
        whole = {row.entry.day: row.run for row in income.income_for(DateRange()).payouts}
        late = {row.entry.day: row.run for row in income.income_for(DateRange(date(2026, 6, 12), None)).payouts}
        self.assertEqual(late, {day: whole[day] for day in late})


class TillBeforeTheStatement(Fixtures):
    """The till's history reaching back further than the statement's.

    Card and cash sold on the 10th of every month from January to June; the
    statement's first line is a debit on 1 June, and its only payout pays
    June's card sales to the cent, its only deposit June's cash. Compared
    over « tout », the till's January to May had nothing on the account to
    be compared with, and the card row said most of the card takings never
    arrived. They are counted apart instead: the till column still shows
    them, the Écart leaves them out, and the page says where each side
    starts. (A mixin: the page's tests reuse it.)"""

    def setUp(self):
        super().setUp()
        for month in range(1, 7):
            self.paid(date(2026, month, 10), CB=("100.00", 2), Cash=("20.00", 1))
        self.debit(date(2026, 6, 1), "30.00")
        self.payout(date(2026, 6, 12), "100.00", "99.00")
        self.credit(date(2026, 6, 15), "20.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES")


class TillBeforeTheStatementTests(TillBeforeTheStatement, TestCase):
    def rows(self, window):
        return {row.label: row for row in income.income_for(window).rows}

    def test_each_side_says_where_it_starts(self):
        report = income.income_for(DateRange())
        self.assertEqual(
            (report.first_statement_day, report.last_statement_day, report.first_payment_day, report.last_payment_day),
            (date(2026, 6, 1), date(2026, 6, 15), date(2026, 1, 10), date(2026, 6, 10)),
        )
        self.assertEqual(report.covered_since, date(2026, 6, 1))

    def test_the_till_before_the_statement_is_counted_apart_and_left_out_of_the_ecart(self):
        report = income.income_for(DateRange())
        self.assertEqual(
            report.till_before_statement,
            {PosDailyPayment.CARD: euros("500.00"), PosDailyPayment.CASH: euros("100.00")},
        )
        self.assertEqual(report.till_before_statement_total, euros("600.00"))
        self.assertEqual((report.till_before_from, report.till_before_to), (date(2026, 1, 10), date(2026, 5, 10)))
        rows = {row.label: row for row in report.rows}
        card, cash = rows["Carte"], rows["Espèces"]
        # The till column still shows the whole window...
        self.assertEqual((card.till, card.uncovered, card.bank), (euros("600.00"), euros("500.00"), euros("100.00")))
        self.assertEqual((cash.till, cash.uncovered, cash.bank), (euros("120.00"), euros("100.00"), euros("20.00")))
        # ...and the Écart is taken over the days both sides cover.
        self.assertEqual((card.difference, cash.difference), (euros("0.00"), euros("0.00")))

    def test_a_window_straddling_the_start_counts_its_own_days_before_it(self):
        report = income.income_for(DateRange(date(2026, 4, 1), date(2026, 6, 30)))
        self.assertEqual(
            report.till_before_statement,
            {PosDailyPayment.CARD: euros("200.00"), PosDailyPayment.CASH: euros("40.00")},
        )
        self.assertEqual((report.till_before_from, report.till_before_to), (date(2026, 4, 10), date(2026, 5, 10)))
        self.assertEqual(self.rows(DateRange(date(2026, 4, 1), date(2026, 6, 30)))["Carte"].difference, euros("0.00"))

    def test_a_window_starting_on_the_statements_first_day_has_nothing_before_it(self):
        report = income.income_for(DateRange(date(2026, 6, 1), date(2026, 6, 30)))
        self.assertEqual((report.till_before_statement, report.bank_before_till), ({}, {}))
        card = {row.label: row for row in report.rows}["Carte"]
        self.assertEqual((card.till, card.uncovered, card.difference), (euros("100.00"), euros("0"), euros("0.00")))

    def test_a_window_wholly_before_the_statement(self):
        report = income.income_for(DateRange(date(2026, 3, 1), date(2026, 5, 31)))
        self.assertEqual(report.till_before_statement_total, euros("360.00"))
        card = {row.label: row for row in report.rows}["Carte"]
        self.assertEqual((card.till, card.bank, card.difference), (euros("300.00"), euros("0"), euros("0.00")))

    def test_a_day_that_paid_nothing_is_not_named(self):
        """A till day whose payments net to nothing has nothing the
        statement could be missing."""
        self.paid(date(2026, 5, 20), TR=("0.00", 0))
        self.assertNotIn("TR", income.income_for(DateRange()).till_before_statement)

    def test_no_statement_at_all_is_no_edge_to_speak_of(self):
        from bank.models import BankTransaction

        BankTransaction.objects.all().delete()
        report = income.income_for(DateRange())
        self.assertEqual((report.first_statement_day, report.till_before_statement, report.covered_since), (None, {}, None))


class StatementBeforeTheTill(Fixtures):
    """The mirror: money on the account before the first till day whose
    payments were read. A payout of 80 € on 5 June pays card sales the till
    never read; June's own sales start on the 10th. (A mixin, like
    TillBeforeTheStatement.)"""

    def setUp(self):
        super().setUp()
        self.payout(date(2026, 6, 5), "80.00", "79.20")
        self.credit(date(2026, 6, 6), "30.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES")
        self.credit(date(2026, 6, 7), "500.00", category="Privatisation")
        self.paid(date(2026, 6, 10), CB=("100.00", 2))
        self.payout(date(2026, 6, 12), "100.00", "99.00")


class TheStatementBeforeTheTillTests(StatementBeforeTheTill, TestCase):
    def test_what_arrived_before_the_till_is_counted_apart_and_left_out_of_the_ecart(self):
        report = income.income_for(DateRange())
        # « Autres entrées » is compared with nothing: not in it.
        self.assertEqual(report.bank_before_till, {income.CARD: euros("80.00"), income.CASH: euros("30.00")})
        self.assertEqual(report.bank_before_till_total, euros("110.00"))
        self.assertEqual((report.bank_before_from, report.bank_before_to), (date(2026, 6, 5), date(2026, 6, 6)))
        self.assertEqual(report.covered_since, date(2026, 6, 10))
        card = {row.label: row for row in report.rows}["Carte"]
        self.assertEqual((card.bank, card.bank_uncovered, card.till), (euros("180.00"), euros("80.00"), euros("100.00")))
        self.assertEqual(card.difference, euros("0.00"))
        self.assertEqual(report.till_before_statement, {})

    def test_no_till_payment_read_at_all_is_no_edge_to_speak_of(self):
        PosDailyPayment.objects.all().delete()
        report = income.income_for(DateRange())
        self.assertEqual((report.first_payment_day, report.bank_before_till), (None, {}))


class QueryCountTests(Fixtures, TestCase):
    def build(self, weeks):
        start = date(2026, 1, 5) + timedelta(weeks=self.built)
        for offset in range(weeks * 7):
            day = start + timedelta(days=offset)
            self.sold(day, "30.00")
            self.paid(day, CB=("25.00", 2), Cash=("5.00", 1))
            self.payout(day + timedelta(days=1), "25.00", "24.80")
        self.credit(start, "200.00")
        self.built += weeks

    def test_three_times_the_history_costs_no_more_queries(self):
        self.built = 0
        self.build(1)
        with self.assertNumQueries(income.QUERIES):
            income.income_for(DateRange())
        self.build(3)
        with self.assertNumQueries(income.QUERIES):
            report = income.income_for(DateRange())
        self.assertEqual(report.card_count, 28)
