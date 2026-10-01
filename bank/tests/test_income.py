"""« Entrées d'argent »: what came IN on the account, beside what the till was
paid over the same days (bank/income.py).

What the page promises, and what each test below holds it to:

* a card payout is recognised by the till rules - here the seeded ones,
  « TOTAL ENCAISSE <number> EURO(S) » in its label, never a name; its gross
  is that number, its net the line, its commission the difference, said as
  it comes - and a label whose number cannot be read whole is no payout at
  all, rather than a payout with a wrong gross;
* cash and cheque deposits are read off the bank type (the seeded rules);
  everything else is « Autres entrées », named by a person, « Sans
  catégorie » first;
* the running balance « ventes carte pas encore versées » counts from an
  anchor the statement's first payout chooses, over the WHOLE history - a
  window never moves it, and a payout that never came is a step it never
  comes back down from;
* an exact run is two amounts equal to the cent over consecutive card days,
  latest first then shortest, each day claimed once, never further back
  than eight days;
* the till side says what it could not read rather than counting it as a
  quiet day;
* « En caisse »: the line's own choice beats everything, then the rules
  where they recognise the line, then its payer's - a payer never
  un-recognises a payout, and a choice retained from one stays on it - a
  card credit printing no gross counts its amount as the gross and its
  commission as unknown (never 0), « Avoir » and meal vouchers get a bank
  side once a credit is said to be one, and every credit of the window lands
  in exactly one list.

The pure tests read with the seeded rules handed in (`support.SEEDED`);
those through the database with the rules migration 0006 seeds in it.
bank/tests/test_recognition.py holds the rules themselves.

Every payee, provider, amount and date below is invented.
"""

import itertools
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from bank import income, recognition
from bank.models import BankTransaction, IncomePayer
from bank.spending import NO_CATEGORY
from bank.tests.support import SEEDED
from common import DateRange
from recipes.models import PosDailyPayment, PosProduct, PosProductDailyQuantity

_PKS = itertools.count(1)

#: An invented merchant and payment provider: the real ones are nobody's
#: business in a public repository, and the rule reads neither.
MERCHANT = "BAR EXEMPLE"
PROVIDER = "PRESTATAIRE INVENTE"
#: An invented terminal whose payouts print no « TOTAL ENCAISSE »: the seeded
#: rules file them under « Autres entrées » until a person says what they are.
TERMINAL = "TERMINAL EXEMPLE"
TERMINAL_LABEL = f"VIR SEPA RECU /FRM {TERMINAL} REMISE CARTES"
#: An invented meal-voucher issuer, refunding the vouchers the till took.
ISSUER = "EMETTEUR TITRES EXEMPLE"
ISSUER_LABEL = f"VIR SEPA RECU /FRM {ISSUER} REMBOURSEMENT"

JUNE = DateRange(date(2026, 6, 1), date(2026, 6, 30))
DAY = date(2026, 6, 2)


def euros(value) -> Decimal:
    return Decimal(str(value))


def payout_label(gross: str, number: int = 1) -> str:
    """A payout's label, shaped like the statement's: who pays, a reference,
    the provider, then « TOTAL ENCAISSE <gross> EUROS »."""
    return (
        f"VIR SEPA RECU /FRM {MERCHANT} /EID /RNF TRANSFERT {PROVIDER} {number:07d} "
        f"TOTAL ENCAISSE {gross} EUROS {MERCHANT}"
    )


def unsaved(
    day,
    amount,
    label="VIR SEPA RECU /FRM CLIENT EXEMPLE",
    bank_type="VIREMENT",
    category="",
    income_source="",
    counterparty="",
):
    """A credit as the pure functions see it - no database. No counterparty
    by default: the payer is then read off the label's words."""
    return BankTransaction(
        pk=next(_PKS),
        operation_date=day,
        bank_type=bank_type,
        label=label,
        counterparty=counterparty,
        amount=euros(amount),
        category=category,
        income_source=income_source,
    )


# `bank.income`'s readings, by the seeded rules - pure, no database.


def entry_for(line, payers=None) -> income.Entry:
    return income.entry_for(line, payers, SEEDED)


def reading_of(line, payers=None, payer=None) -> tuple[str, str]:
    """(source, how) - what `income.reading_of` says and who said it."""
    return income.reading_of(line, payers, payer, SEEDED)[:2]


def source_of(line, payers=None) -> str:
    return income.source_of(line, payers, SEEDED)


def follows_its_payer(line) -> bool:
    return income.follows_its_payer(line, SEEDED)


def payout_gross(label):
    """The gross a till rule reads on a payout's label - None where no rule
    recognises a payout there."""
    reading = recognition.till_reading(SEEDED, label or "")
    return reading.gross if reading is not None and reading.source == income.CARD else None


def payout(day, gross, net=None) -> income.Entry:
    """A payout entry, net equal to gross unless said."""
    return entry_for(unsaved(day, net if net is not None else gross, payout_label(gross)))


class PayoutRecognitionTests(SimpleTestCase):
    def test_the_gross_is_the_number_the_label_prints_with_a_dot(self):
        self.assertEqual(payout_gross(payout_label("987.65")), euros("987.65"))
        self.assertEqual(payout_gross(payout_label("120.0")), euros("120.00"))

    def test_a_whole_number_and_a_comma_are_read_too(self):
        self.assertEqual(payout_gross(payout_label("300")), euros("300"))
        self.assertEqual(payout_gross(payout_label("12,50")), euros("12.50"))

    def test_euro_singular_and_thousands_grouped_by_a_space(self):
        self.assertEqual(payout_gross("TOTAL ENCAISSE 1 234.50 EURO"), euros("1234.50"))

    def test_no_number_is_no_payout(self):
        self.assertIsNone(payout_gross("VIR SEPA RECU TOTAL ENCAISSE EUROS"))
        self.assertIsNone(payout_gross("VIR SEPA RECU /FRM CLIENT EXEMPLE"))
        self.assertIsNone(payout_gross(""))
        self.assertIsNone(payout_gross(None))

    def test_a_number_the_rule_cannot_read_whole_is_no_payout_rather_than_a_wrong_gross(self):
        """« 1,234.50 » read as far as it goes would be 1,234 - a payout of
        one euro. Refused, the line lands in « Autres entrées », in sight."""
        self.assertIsNone(payout_gross("TOTAL ENCAISSE 1,234.50 EUROS"))

    def test_the_rule_reads_the_words_not_the_provider(self):
        label = "VIR RECU /FRM UN AUTRE PRESTATAIRE TOTAL ENCAISSE 80.00 EUROS"
        self.assertEqual(source_of(unsaved(date(2026, 6, 2), "79.50", label)), income.CARD)

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
                self.assertEqual(source_of(unsaved(date(2026, 6, 2), "10", "DEPOT", bank_type)), expected)

    def test_what_the_bank_page_calls_each_one(self):
        self.assertEqual(payout(date(2026, 6, 2), "10.00").name, "Versement carte")
        cash = entry_for(unsaved(date(2026, 6, 2), "100", "VERSEMENT", "VERSEMENT ESPECES"))
        self.assertEqual(cash.name, "Dépôt d'espèces")
        named = entry_for(unsaved(date(2026, 6, 2), "500", category="Privatisation"))
        self.assertEqual((named.name, named.unnamed), ("Privatisation", False))
        blank = entry_for(unsaved(date(2026, 6, 2), "500", category="  "))
        self.assertEqual((blank.name, blank.unnamed), (NO_CATEGORY, True))

    def test_a_category_typed_on_a_payout_does_not_rename_it(self):
        entry = entry_for(unsaved(date(2026, 6, 2), "10", payout_label("10.00"), category="Divers"))
        self.assertEqual((entry.source, entry.name, entry.unnamed), (income.CARD, "Versement carte", False))


class ReadingTests(SimpleTestCase):
    """What a credit is, and who said so: the line's own choice, else the
    rules where they recognise it, else its payer's - pure, a payers dict
    in."""

    def test_the_lines_own_choice_beats_its_payer_and_the_rules(self):
        line = unsaved(DAY, "198.00", payout_label("200.00"), income_source=income.CASH)
        payers = {income.payer_key(line): income.VOUCHER}
        self.assertEqual(reading_of(line, payers), (income.CASH, income.BY_LINE))

    def test_a_payer_never_unrecognises_a_line_the_rules_recognise(self):
        """The provider prints the bar's own name as the payee of its
        payouts: « Pas une vente » retained for a transfer from the bar's
        other account, under that name, moved every payout out of the card
        figures when the payer came before the rules."""
        payout_line = unsaved(DAY, "198.00", payout_label("200.00"), counterparty=MERCHANT)
        deposit = unsaved(DAY, "100.00", "VERSEMENT 0042", "VERSEMENT ESPECES", counterparty=MERCHANT)
        payers = {MERCHANT: income.OTHER}
        self.assertEqual(reading_of(payout_line, payers), (income.CARD, income.BY_RULE))
        self.assertEqual(reading_of(deposit, payers), (income.CASH, income.BY_RULE))

    def test_the_payers_choice_decides_what_the_rules_do_not_recognise(self):
        line = unsaved(DAY, "198.00", TERMINAL_LABEL)
        payers = {income.payer_key(line): income.CARD}
        self.assertEqual(reading_of(line, payers), (income.CARD, income.BY_PAYER))
        self.assertTrue(follows_its_payer(line))
        self.assertFalse(follows_its_payer(unsaved(DAY, "198.00", payout_label("200.00"))))
        self.assertFalse(follows_its_payer(unsaved(DAY, "198.00", TERMINAL_LABEL, income_source=income.CASH)))

    def test_the_rules_decide_where_nobody_said(self):
        line = unsaved(DAY, "198.00", payout_label("200.00"))
        for payers in (None, {}, {"UN AUTRE PAYEUR": income.CASH}):
            with self.subTest(payers=payers):
                self.assertEqual(reading_of(line, payers), (income.CARD, income.BY_RULE))

    def test_automatic_on_the_line_hands_it_to_its_payer(self):
        line = unsaved(DAY, "120.00", TERMINAL_LABEL, income_source=income.AUTOMATIC)
        self.assertEqual(reading_of(line, {income.payer_key(line): income.CARD}), (income.CARD, income.BY_PAYER))
        self.assertEqual(reading_of(line), (income.OTHER, income.BY_RULE))

    def test_a_payer_key_handed_over_is_the_one_read(self):
        line = unsaved(DAY, "50.00")
        own = income.payer_key(line)
        given = {"CLE DONNEE": income.CREDIT}
        self.assertEqual(reading_of(line, given, payer="CLE DONNEE"), (income.CREDIT, income.BY_PAYER))
        # Handed over, the line's own key is not worked out again.
        self.assertEqual(reading_of(line, {own: income.CREDIT}, payer="CLE DONNEE"), (income.OTHER, income.BY_RULE))

    def test_an_entry_carries_the_reading_and_its_payer(self):
        line = unsaved(DAY, "42.00", ISSUER_LABEL, counterparty=ISSUER)
        entry = entry_for(line, {ISSUER: income.VOUCHER})
        self.assertEqual(
            (entry.source, entry.how, entry.payer, entry.name, entry.gross),
            (income.VOUCHER, income.BY_PAYER, ISSUER, "Remboursement de titres-restaurant", None),
        )
        self.assertEqual(source_of(line, {ISSUER: income.VOUCHER}), income.VOUCHER)

    def test_a_stored_value_that_is_no_source_falls_back_without_raising(self):
        """Written by hand in the database, on the line or the payer: passed
        over, never raised on."""
        for stored in ("bitcoin", "CARD", " card", "<script>", None):
            with self.subTest(stored=stored):
                line = unsaved(DAY, "198.00", payout_label("200.00"), income_source=stored)
                key = income.payer_key(line)
                self.assertEqual(reading_of(line, {key: stored}), (income.CARD, income.BY_RULE))
                entry = entry_for(line, {key: stored})
                self.assertEqual(
                    (entry.source, entry.how, entry.choice, entry.gross),
                    (income.CARD, income.BY_RULE, "", euros("200")),
                )
                # Unrecognised, the line goes on to its payer, and a payer
                # holding no source to « Autres entrées ».
                unread = unsaved(DAY, "198.00", TERMINAL_LABEL, income_source=stored)
                key = income.payer_key(unread)
                self.assertEqual(reading_of(unread, {key: income.CASH}), (income.CASH, income.BY_PAYER))
                self.assertEqual(reading_of(unread, {key: stored}), (income.OTHER, income.BY_RULE))


class PayerKeyTests(SimpleTestCase):
    """Who paid a credit, as `IncomePayer.key` holds it."""

    def test_the_counterparty_the_bank_prints_is_the_payer(self):
        line = unsaved(DAY, "50.00", "VIR SEPA RECU /FRM AUTRE NOM 000123", counterparty="Société Exemple")
        self.assertEqual(income.payer_key(line), "SOCIETE EXEMPLE")

    def test_accents_and_case_are_folded(self):
        names = ("Café Exemple", "CAFE EXEMPLE", "café  exemple")
        self.assertEqual({income.payer_key(unsaved(DAY, "1", counterparty=name)) for name in names}, {"CAFE EXEMPLE"})

    def test_with_no_counterparty_the_labels_words_without_their_digits(self):
        first = unsaved(DAY, "1", "VIR SEPA RECU /FRM CLIENT EXEMPLE N° 000123 DU 05/06/26")
        next_month = unsaved(DAY, "1", "VIR SEPA RECU /FRM CLIENT EXEMPLE N° 000987 DU 05/07/26")
        self.assertEqual(income.payer_key(first), "VIR SEPA RECU FRM CLIENT EXEMPLE N DU")
        self.assertEqual(income.payer_key(next_month), income.payer_key(first))

    def test_every_payout_of_one_provider_is_one_payer(self):
        """Its number and its gross change every time; its words do not."""
        labels = (payout_label("10.00", 1), payout_label("987.65", 2), payout_label("1 234.50", 3))
        self.assertEqual(len({income.payer_key(unsaved(DAY, "1", label)) for label in labels}), 1)

    def test_cut_to_the_column_never_ending_on_a_space(self):
        self.assertEqual(income.PAYER_KEY_MAX, 255)
        # « AB » 120 times: the 255th character is the space after the 85th.
        key = income.payer_key(unsaved(DAY, "1", "AB " * 120))
        self.assertEqual((key, len(key)), (("AB " * 85).rstrip(), 254))
        self.assertEqual(income.payer_key(unsaved(DAY, "1", counterparty="X" * 300)), "X" * 255)

    def test_nothing_naming_anybody_is_an_empty_key(self):
        for label in ("000123 456", "0001 / 2026-06-05", ""):
            with self.subTest(label=label):
                self.assertEqual(income.payer_key(unsaved(DAY, "1", label)), "")


class MarkedEntryTests(SimpleTestCase):
    """A credit a person said is a card payout, or said is not one."""

    def test_marked_card_with_no_printed_gross_counts_its_amount_and_an_unknown_commission(self):
        entry = entry_for(unsaved(DAY, "150.00", TERMINAL_LABEL, income_source=income.CARD))
        self.assertEqual(
            (entry.source, entry.how, entry.gross, entry.gross_from_amount, entry.name),
            (income.CARD, income.BY_LINE, euros("150.00"), True, "Versement carte"),
        )
        self.assertIsNone(entry.commission)
        self.assertIsNone(entry.commission_rate)

    def test_the_same_through_its_payer(self):
        line = unsaved(DAY, "150.00", TERMINAL_LABEL, counterparty=TERMINAL)
        entry = entry_for(line, {TERMINAL: income.CARD})
        self.assertEqual(
            (entry.how, entry.gross, entry.gross_from_amount, entry.commission),
            (income.BY_PAYER, euros("150.00"), True, None),
        )

    def test_zero_and_negative_amounts_marked_card_raise_nothing(self):
        for amount in ("0.00", "-10.00"):
            with self.subTest(amount=amount):
                entry = entry_for(unsaved(DAY, amount, TERMINAL_LABEL, income_source=income.CARD))
                self.assertEqual((entry.gross, entry.commission, entry.commission_rate), (euros(amount), None, None))

    def test_marked_card_keeps_the_gross_its_label_prints(self):
        entry = entry_for(unsaved(DAY, "198.00", payout_label("200.00"), income_source=income.CARD))
        self.assertEqual(
            (entry.how, entry.gross, entry.gross_from_amount, entry.commission, entry.commission_rate),
            (income.BY_LINE, euros("200.00"), False, euros("2.00"), euros("1.00")),
        )

    def test_a_payout_marked_as_no_sale_is_no_payout(self):
        entry = entry_for(unsaved(DAY, "198.00", payout_label("200.00"), income_source=income.OTHER))
        self.assertEqual(
            (entry.source, entry.how, entry.gross, entry.gross_from_amount, entry.commission),
            (income.OTHER, income.BY_LINE, None, False, None),
        )
        self.assertEqual((entry.name, entry.unnamed), (NO_CATEGORY, True))


class ChoiceTests(SimpleTestCase):
    """What the « En caisse » menu shows chosen, whether « retenir pour ce
    payeur » is drawn ticked, and who the row says decided."""

    def entry(self, line, payers=None):
        return entry_for(line, payers)

    def test_each_way_of_deciding(self):
        printed = payout_label("200.00")
        unnamed = unsaved(DAY, "120.00", TERMINAL_LABEL)
        cases = [
            # (entry, choice, remember_by_default, how_label)
            (
                self.entry(unsaved(DAY, "198.00", printed, income_source=income.CASH)),
                income.CASH,
                False,
                "choisi pour cette entrée",
            ),
            (
                self.entry(unsaved(DAY, "120.00", TERMINAL_LABEL, income_source=income.OTHER)),
                income.OTHER,
                False,
                "choisi pour cette entrée",
            ),
            (
                self.entry(unnamed, {income.payer_key(unnamed): income.VOUCHER}),
                income.VOUCHER,
                True,
                "payeur retenu",
            ),
            # The rule that recognised it, by its name.
            (
                self.entry(unsaved(DAY, "198.00", printed)),
                income.AUTOMATIC,
                False,
                "règle « Versement carte (TOTAL ENCAISSE) »",
            ),
            (
                self.entry(unsaved(DAY, "40.00", "VERSEMENT", "VERSEMENT ESPECES")),
                income.AUTOMATIC,
                False,
                "règle « Dépôt d'espèces (VERSEMENT ESPECES) »",
            ),
            (
                self.entry(unsaved(DAY, "80.00", "REMISE", "REMISE CHEQUES")),
                income.AUTOMATIC,
                False,
                "règle « Remise de chèques (REMISE CHEQUE) »",
            ),
            # Nothing recognised: the transfer of a terminal the rules do
            # not know, where retaining its payer is the point.
            (self.entry(unnamed), income.AUTOMATIC, True, "non reconnue"),
        ]
        for entry, choice, remember, said in cases:
            with self.subTest(line=entry.line.label, how=entry.how):
                self.assertEqual((entry.choice, entry.remember_by_default, entry.how_label), (choice, remember, said))

    def test_no_payer_key_is_never_ticked(self):
        entry = self.entry(unsaved(DAY, "120.00", "000123 / 2026"))
        self.assertEqual((entry.payer, entry.source, entry.how), ("", income.OTHER, income.BY_RULE))
        self.assertFalse(entry.remember_by_default)


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
        self.assertEqual([balance.pending[one.line.pk] for one in payouts], [euros("0"), euros("0"), euros("400")])

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
        self.assertEqual(runs_of(card, [payout(date(2026, 6, 4), "135.00")]), [(date(2026, 6, 2), date(2026, 6, 3))])

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
        self.assertEqual(
            runs_of(card, [payout(payday, "100.00")]), [(payday - timedelta(days=8), payday - timedelta(days=1))]
        )

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

    def credit(
        self, day, amount, label="VIR SEPA RECU /FRM CLIENT EXEMPLE", bank_type="VIREMENT", category="", **kwargs
    ):
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

    def payout(self, day, gross, net, **kwargs):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            bank_type="VIREMENT",
            label=payout_label(gross, self.counter),
            counterparty=kwargs.pop("counterparty", MERCHANT),
            amount=euros(net),
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint=f"payout-{self.counter}",
            **kwargs,
        )

    def debit(self, day, amount, **kwargs):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            label=f"PRLV SEPA FOURNISSEUR EXEMPLE {self.counter}",
            amount=-euros(amount),
            kind=BankTransaction.Kind.DEBIT,
            fingerprint=f"debit-{self.counter}",
            **kwargs,
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
        self.assertEqual(
            (report.card_net, report.card_gross, report.card_commission),
            (euros("297.40"), euros("300.00"), euros("2.60")),
        )
        self.assertEqual(report.card_commission_rate, euros("0.87"))
        self.assertEqual((report.cash_total, report.cash_count), (euros("300.00"), 1))
        self.assertEqual((report.cheque_total, report.cheque_count), (euros("150.00"), 1))
        self.assertEqual((report.others_total, len(report.others)), (euros("1010.00"), 4))

    def test_the_other_entries_by_category_unnamed_first(self):
        report = income.income_for(JUNE)
        self.assertEqual(
            [(one.name, one.amount, one.count) for one in report.other_categories],
            [
                (NO_CATEGORY, euros("50.00"), 2),
                ("Privatisation", euros("900.00"), 1),
                ("Remboursement", euros("60.00"), 1),
            ],
        )
        # The list: the work first, then the biggest.
        self.assertEqual(
            [one.net for one in report.others], [euros("40.00"), euros("10.00"), euros("900.00"), euros("60.00")]
        )
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
        self.assertEqual(
            (card.till, card.bank, card.net, card.commission),
            (euros("200"), euros("195"), euros("193.80"), euros("1.20")),
        )
        self.assertEqual(card.difference, euros("-5.00"))
        self.assertEqual(rows["Espèces"].difference, euros("-5.00"))
        # The till alone knows an « Avoir » while no credit is said to be
        # one: its bank side is « — », never 0 - an Écart of the whole till
        # figure would accuse the transfer still in « Autres entrées »
        # (MarkedMeansTests fills it).
        credit = rows["Avoir"]
        self.assertEqual(
            (credit.key, credit.till, credit.bank, credit.bank_count, credit.difference),
            (income.CREDIT, euros("500"), None, 0, None),
        )
        self.assertEqual(credit.note, income.NOTES[income.CREDIT])
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
        self.assertEqual(
            (may.takings, may.card_sold, may.cash_sold, may.payouts_gross), (euros("140"), euros("120"), euros("20"), 0)
        )
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

    def test_the_till_before_the_statement_is_counted_apart_and_left_out_of_the_gap(self):
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
        self.assertEqual(
            (report.first_statement_day, report.till_before_statement, report.covered_since), (None, {}, None)
        )


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
    def test_what_arrived_before_the_till_is_counted_apart_and_left_out_of_the_gap(self):
        report = income.income_for(DateRange())
        # « Autres entrées » is compared with nothing: not in it.
        self.assertEqual(report.bank_before_till, {income.CARD: euros("80.00"), income.CASH: euros("30.00")})
        self.assertEqual(report.bank_before_till_total, euros("110.00"))
        self.assertEqual((report.bank_before_from, report.bank_before_to), (date(2026, 6, 5), date(2026, 6, 6)))
        self.assertEqual(report.covered_since, date(2026, 6, 10))
        card = {row.label: row for row in report.rows}["Carte"]
        self.assertEqual(
            (card.bank, card.bank_uncovered, card.till), (euros("180.00"), euros("80.00"), euros("100.00"))
        )
        self.assertEqual(card.difference, euros("0.00"))
        self.assertEqual(report.till_before_statement, {})

    def test_no_till_payment_read_at_all_is_no_edge_to_speak_of(self):
        PosDailyPayment.objects.all().delete()
        report = income.income_for(DateRange())
        self.assertEqual((report.first_payment_day, report.bank_before_till), (None, {}))


def stored(line) -> BankTransaction:
    """The line as the database holds it now."""
    return BankTransaction.objects.get(pk=line.pk)


def payers() -> list[tuple[str, str]]:
    return list(IncomePayer.objects.values_list("key", "source"))


class SetSourceTests(Fixtures, TestCase):
    """« En caisse », as the page posts it: a value of the menu and the
    « retenir pour ce payeur » box."""

    def setUp(self):
        super().setUp()
        self.line = self.credit(date(2026, 6, 10), "120.00", TERMINAL_LABEL, counterparty=TERMINAL)

    def test_for_this_line_only(self):
        change = income.set_source(self.line, income.CARD, remember=False)
        self.assertEqual(stored(self.line).income_source, income.CARD)
        self.assertEqual(payers(), [])
        self.assertEqual(
            (change.payer, change.remembered, change.forgotten, change.followers, change.kept),
            (TERMINAL, False, False, 0, 0),
        )
        self.assertEqual(
            (change.entry.source, change.entry.how, change.entry.gross_from_amount),
            (income.CARD, income.BY_LINE, True),
        )

    def test_automatic_for_this_line_hands_it_back_to_its_payer(self):
        IncomePayer.objects.create(key=TERMINAL, source=income.VOUCHER)
        BankTransaction.objects.filter(pk=self.line.pk).update(income_source=income.CASH)
        change = income.set_source(stored(self.line), income.AUTOMATIC, remember=False)
        self.assertEqual(stored(self.line).income_source, "")
        self.assertEqual(payers(), [(TERMINAL, income.VOUCHER)])
        self.assertEqual((change.entry.source, change.entry.how), (income.VOUCHER, income.BY_PAYER))

    def test_remembering_retains_the_payer_and_the_line_follows_it(self):
        BankTransaction.objects.filter(pk=self.line.pk).update(income_source=income.CHEQUE)
        change = income.set_source(stored(self.line), income.CARD, remember=True)
        # Its own choice cleared: « Oublier » then undoes it whole.
        self.assertEqual(stored(self.line).income_source, "")
        self.assertEqual(payers(), [(TERMINAL, income.CARD)])
        self.assertEqual((change.remembered, change.forgotten), (True, False))
        self.assertEqual((change.entry.source, change.entry.how), (income.CARD, income.BY_PAYER))

    def test_remembering_again_replaces_the_payers_choice(self):
        IncomePayer.objects.create(key=TERMINAL, source=income.CASH)
        income.set_source(self.line, income.VOUCHER, remember=True)
        self.assertEqual(payers(), [(TERMINAL, income.VOUCHER)])

    def test_remembering_automatic_forgets_the_payer(self):
        IncomePayer.objects.create(key=TERMINAL, source=income.CARD)
        BankTransaction.objects.filter(pk=self.line.pk).update(income_source=income.CASH)
        change = income.set_source(stored(self.line), income.AUTOMATIC, remember=True)
        self.assertEqual((payers(), stored(self.line).income_source), ([], ""))
        self.assertEqual((change.remembered, change.forgotten), (False, True))
        self.assertEqual((change.entry.source, change.entry.how), (income.OTHER, income.BY_RULE))
        # Nothing left to forget is no forgetting.
        self.assertFalse(income.set_source(stored(self.line), income.AUTOMATIC, remember=True).forgotten)

    def test_followers_and_kept_count_the_payers_other_credits(self):
        self.credit(date(2026, 6, 1), "80.00", TERMINAL_LABEL, counterparty=TERMINAL)
        self.credit(date(2026, 5, 3), "60.00", TERMINAL_LABEL, counterparty=TERMINAL)
        self.credit(date(2026, 6, 2), "70.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CASH)
        self.credit(date(2026, 6, 3), "90.00", counterparty="AUTRE CLIENT EXEMPLE")
        self.debit(date(2026, 6, 4), "30.00", counterparty=TERMINAL)
        retained = income.set_source(self.line, income.CARD, remember=True)
        self.assertEqual((retained.followers, retained.kept), (2, 1))
        forgotten = income.set_source(stored(self.line), income.AUTOMATIC, remember=True)
        self.assertEqual((forgotten.forgotten, forgotten.followers, forgotten.kept), (True, 2, 1))
        # For this line alone the payer is not touched, and nothing counted.
        alone = income.set_source(stored(self.line), income.CARD, remember=False)
        self.assertEqual((alone.followers, alone.kept), (0, 0))

    def test_remembering_where_nothing_names_a_payer_is_for_this_line_only(self):
        BankTransaction.objects.filter(pk=self.line.pk).update(label="000123 / 2026", counterparty="")
        line = stored(self.line)
        self.assertEqual(income.payer_key(line), "")
        change = income.set_source(line, income.CREDIT, remember=True)
        self.assertEqual((stored(self.line).income_source, payers()), (income.CREDIT, []))
        self.assertEqual((change.payer, change.remembered, change.entry.how), ("", False, income.BY_LINE))

    def test_a_debit_and_a_zero_amount_are_refused_and_nothing_is_written(self):
        debit = self.debit(date(2026, 6, 5), "30.00", counterparty=TERMINAL)
        zero = self.credit(date(2026, 6, 5), "0.00", TERMINAL_LABEL, counterparty=TERMINAL)
        for line in (debit, zero):
            for remember in (False, True):
                with (
                    self.subTest(amount=line.amount, remember=remember),
                    self.assertRaisesMessage(income.SourceRefused, income.NOT_A_CREDIT),
                ):
                    income.set_source(line, income.CARD, remember=remember)
            self.assertEqual(stored(line).income_source, "")
        self.assertEqual(payers(), [])

    def test_a_value_the_menu_does_not_offer_is_refused_and_nothing_is_written(self):
        """The menu posts exact values: nothing is trimmed, folded or guessed."""
        BankTransaction.objects.filter(pk=self.line.pk).update(income_source=income.CASH)
        line = stored(self.line)
        for value in (None, " card", "card ", "CARD", "Carte", "<script>", "bitcoin", 1, ["card"], b"card"):
            for remember in (False, True):
                with (
                    self.subTest(value=value, remember=remember),
                    self.assertRaisesMessage(income.SourceRefused, income.UNKNOWN_CHOICE),
                ):
                    income.set_source(line, value, remember=remember)
        self.assertEqual((stored(self.line).income_source, payers()), (income.CASH, []))

    def test_a_choice_settles_nothing(self):
        """`settled_by_hand` is never written, whatever the copy in hand
        says: a credit pays no invoice."""
        stale = stored(self.line)
        BankTransaction.objects.filter(pk=self.line.pk).update(settled_by_hand=True)
        income.set_source(stale, income.CARD, remember=True)
        income.set_source(stale, income.AUTOMATIC, remember=True)
        income.set_source(stale, income.OTHER, remember=False)
        self.assertTrue(stored(self.line).settled_by_hand)
        other = self.credit(date(2026, 6, 11), "42.00", ISSUER_LABEL, counterparty=ISSUER)
        income.set_source(other, income.VOUCHER, remember=False)
        self.assertFalse(stored(other).settled_by_hand)


class RetainingFromARecognisedLineTests(Fixtures, TestCase):
    """« retenir pour ce payeur » sent from a line the rules recognise. The
    provider prints the bar's own name as the payee of its payouts, and the
    bar's other account pays in under that same name: one payer, whose
    payouts the label recognises, whose deposit the bank type names, and
    whose transfers nothing recognises (review, 01/10/2026). No payer
    reaches a recognised line, so a choice sent from one stays on it."""

    def setUp(self):
        super().setUp()
        self.first = self.payout(date(2026, 6, 2), "200.00", "198.00")
        self.siblings = [
            self.payout(date(2026, 6, 3), "100.00", "99.30"),
            self.payout(date(2026, 6, 4), "50.00", "49.65"),
        ]
        self.deposit = self.credit(
            date(2026, 6, 5), "80.00", "VERSEMENT 0042", "VERSEMENT ESPECES", counterparty=MERCHANT
        )
        self.transfer = self.credit(
            date(2026, 6, 6), "500.00", f"VIR SEPA RECU /FRM {MERCHANT} VIREMENT INTERNE", counterparty=MERCHANT
        )
        self.own = self.credit(
            date(2026, 6, 7),
            "30.00",
            f"VIR SEPA RECU /FRM {MERCHANT} ACOMPTE",
            counterparty=MERCHANT,
            income_source=income.CREDIT,
        )
        self.debit(date(2026, 6, 8), "20.00", counterparty=MERCHANT)

    def test_they_all_share_one_payer(self):
        lines = [self.first, *self.siblings, self.deposit, self.transfer, self.own]
        self.assertEqual({income.payer_key(line) for line in lines}, {MERCHANT})

    def test_no_sale_retained_from_a_payout_stays_on_that_payout(self):
        change = income.set_source(self.first, income.OTHER, remember=True)
        self.assertEqual(stored(self.first).income_source, income.OTHER)
        self.assertEqual(payers(), [(MERCHANT, income.OTHER)])
        self.assertEqual((change.payer, change.remembered, change.forgotten), (MERCHANT, True, False))
        self.assertEqual((change.entry.source, change.entry.how), (income.OTHER, income.BY_LINE))
        # The transfer nothing recognises follows the payer; the payouts and
        # the deposit are no payer's, and the credit chosen alone keeps it.
        self.assertEqual((change.followers, change.kept), (1, 1))

        report = income.income_for(JUNE)
        self.assertEqual(
            [(row.entry.line.pk, row.entry.source, row.entry.how) for row in report.payouts],
            [(line.pk, income.CARD, income.BY_RULE) for line in self.siblings],
        )
        self.assertEqual(
            (report.card_count, report.card_gross, report.card_net, report.card_commission),
            (2, euros("150.00"), euros("148.95"), euros("1.05")),
        )
        self.assertEqual(
            {one.line.pk: one.how for one in report.others},
            {self.first.pk: income.BY_LINE, self.transfer.pk: income.BY_PAYER},
        )
        self.assertEqual(
            {one.line.pk: (one.source, one.how) for one in report.other_means},
            {self.deposit.pk: (income.CASH, income.BY_RULE), self.own.pk: (income.CREDIT, income.BY_LINE)},
        )
        # The payer decides the transfer, and nothing else.
        self.assertEqual(
            [(row.payer.key, row.count, row.total, row.count_all) for row in report.payers],
            [(MERCHANT, 1, euros("500.00"), 1)],
        )

    def test_another_source_retained_from_a_deposit_stays_on_that_deposit(self):
        change = income.set_source(self.deposit, income.CHEQUE, remember=True)
        self.assertEqual(stored(self.deposit).income_source, income.CHEQUE)
        self.assertEqual(payers(), [(MERCHANT, income.CHEQUE)])
        self.assertEqual((change.entry.source, change.entry.how), (income.CHEQUE, income.BY_LINE))
        self.assertEqual((change.followers, change.kept), (1, 1))
        report = income.income_for(JUNE)
        self.assertEqual([row.entry.line.pk for row in report.payouts], [self.first.pk, *(o.pk for o in self.siblings)])

    def test_forgetting_it_counts_only_the_credits_it_decided(self):
        """The payouts and the deposit never followed it, and the payout it
        was retained from, like the credit chosen alone, keeps the choice of
        its own: one credit goes back to the rules - the transfer."""
        income.set_source(self.first, income.OTHER, remember=True)
        self.assertEqual(income.forget_payer(IncomePayer.objects.get()), 1)
        self.assertEqual(payers(), [])
        self.assertEqual(stored(self.first).income_source, income.OTHER)
        known = income.known_payers()
        self.assertEqual(reading_of(stored(self.transfer), known), (income.OTHER, income.BY_RULE))
        self.assertEqual(reading_of(stored(self.own), known), (income.CREDIT, income.BY_LINE))
        self.assertEqual(
            [reading_of(stored(line), known) for line in self.siblings], [(income.CARD, income.BY_RULE)] * 2
        )

    def test_retaining_what_the_rules_read_clears_the_lines_own_choice(self):
        """« Carte » retained from a payout the label recognises as card: the
        line goes back to its rules - nothing of its own left to beat them -
        and so does « Automatique » with the box ticked."""
        BankTransaction.objects.filter(pk=self.first.pk).update(income_source=income.OTHER)
        change = income.set_source(stored(self.first), income.CARD, remember=True)
        self.assertEqual(stored(self.first).income_source, "")
        self.assertEqual(payers(), [(MERCHANT, income.CARD)])
        self.assertEqual(
            (change.entry.source, change.entry.how, change.entry.gross, change.entry.gross_from_amount),
            (income.CARD, income.BY_RULE, euros("200.00"), False),
        )
        self.assertEqual((change.followers, change.kept), (1, 1))

        BankTransaction.objects.filter(pk=self.first.pk).update(income_source=income.OTHER)
        change = income.set_source(stored(self.first), income.AUTOMATIC, remember=True)
        self.assertEqual((stored(self.first).income_source, payers()), ("", []))
        self.assertEqual((change.forgotten, change.entry.source, change.entry.how), (True, income.CARD, income.BY_RULE))

    def test_a_recognised_sibling_with_a_choice_of_its_own_is_not_kept(self):
        """`kept` counts the credits whose own choice beats the payer: a
        payout the label recognises is no payer's to begin with
        (`SourceChange`: « Those the rules recognise are neither »), whatever
        it was chosen as, and the message must not say it kept a choice
        against a payer that never reached it."""
        BankTransaction.objects.filter(pk=self.siblings[0].pk).update(income_source=income.OTHER)
        change = income.set_source(self.transfer, income.OTHER, remember=True)
        self.assertEqual(stored(self.transfer).income_source, "")
        # Only the credit chosen alone that the payer would otherwise decide.
        self.assertEqual((change.followers, change.kept), (0, 1))


class ForgetPayerTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.payer = IncomePayer.objects.create(key=TERMINAL, source=income.CARD)
        self.followers = [
            self.credit(date(2026, 6, 1), "80.00", TERMINAL_LABEL, counterparty=TERMINAL),
            self.credit(date(2026, 5, 3), "60.00", TERMINAL_LABEL, counterparty=TERMINAL),
        ]
        self.own = self.credit(
            date(2026, 6, 2), "70.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CASH
        )
        self.credit(date(2026, 6, 3), "90.00", counterparty="AUTRE CLIENT EXEMPLE")
        self.debit(date(2026, 6, 4), "30.00", counterparty=TERMINAL)

    def test_its_credits_go_back_to_the_rules_and_a_choice_of_their_own_stays(self):
        self.assertEqual(income.forget_payer(self.payer), 2)
        self.assertEqual(payers(), [])
        known = income.known_payers()
        self.assertEqual(
            [reading_of(stored(line), known) for line in self.followers],
            [(income.OTHER, income.BY_RULE)] * 2,
        )
        self.assertEqual(reading_of(stored(self.own), known), (income.CASH, income.BY_LINE))

    def test_a_payer_nothing_follows_any_more_goes_too(self):
        nobody = IncomePayer.objects.create(key="PAYEUR DISPARU EXEMPLE", source=income.VOUCHER)
        self.assertEqual(income.forget_payer(nobody), 0)
        self.assertEqual(payers(), [(TERMINAL, income.CARD)])


class MarkedCardTests(Fixtures, TestCase):
    """A terminal printing no gross: one of its payouts said to be card, beside
    a payout the rules recognise. The till sold 100, 150 and 80 by card on
    1-3 June; the printed payout of the 2nd pays the 1st, the marked one of
    the 4th pays the 2nd, and the 3rd is not paid yet."""

    def setUp(self):
        super().setUp()
        self.debit(date(2026, 6, 1), "1.00")  # the statement covers the till's first day
        self.paid(date(2026, 6, 1), CB=("100.00", 2))
        self.paid(date(2026, 6, 2), CB=("150.00", 3))
        self.paid(date(2026, 6, 3), CB=("80.00", 1))
        self.printed = self.payout(date(2026, 6, 2), "100.00", "99.00")
        self.marked = self.credit(
            date(2026, 6, 4), "150.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CARD
        )

    def test_its_amount_counts_in_the_gross_and_the_net_never_in_the_commission(self):
        report = income.income_for(JUNE)
        self.assertEqual(
            (report.card_count, report.card_gross, report.card_net, report.card_printed_gross),
            (2, euros("250.00"), euros("249.00"), euros("100.00")),
        )
        # Counted as 0 it would read as « no fee » and halve the rate.
        self.assertEqual(
            (report.card_commission, report.card_commission_rate, report.card_from_amount),
            (euros("1.00"), euros("1.00"), 1),
        )
        card = report.rows[0]
        self.assertEqual(
            (card.key, card.till, card.bank, card.net, card.commission, card.from_amount, card.difference),
            (income.CARD, euros("330"), euros("250"), euros("249"), euros("1.00"), 1, euros("-80.00")),
        )
        self.assertEqual((report.others, report.other_means), ([], []))

    def test_it_counts_in_the_running_balance(self):
        report = income.income_for(JUNE)
        self.assertEqual(
            [(row.entry.line.pk, row.pending) for row in report.payouts],
            [(self.printed.pk, euros("0")), (self.marked.pk, euros("80"))],
        )
        self.assertEqual((report.balance.anchor, report.last_payout.entry.line.pk), (date(2026, 6, 1), self.marked.pk))
        # Unmarked, it is a transfer the balance knows nothing of.
        BankTransaction.objects.filter(pk=self.marked.pk).update(income_source="")
        report = income.income_for(JUNE)
        self.assertEqual([row.entry.line.pk for row in report.payouts], [self.printed.pk])
        self.assertEqual([one.line.pk for one in report.others], [self.marked.pk])

    def test_its_month_adds_the_known_commission_only(self):
        (june,) = income.income_for(JUNE).months
        self.assertEqual(
            (june.card_sold, june.payouts_gross, june.net, june.commission, june.payouts, june.from_amount),
            (euros("330"), euros("250"), euros("249"), euros("1.00"), 2, 1),
        )

    def test_a_month_of_such_payouts_alone_has_its_commission_unknown(self):
        """Not 0,00 €, which reads as « no fee »: the month says every one of
        its payouts printed no gross, and the window's commission is None."""
        self.credit(date(2026, 7, 3), "40.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CARD)
        report = income.income_for(DateRange(date(2026, 7, 1), date(2026, 7, 31)))
        (july,) = report.months
        self.assertEqual(
            (july.payouts_gross, july.net, july.commission, july.payouts, july.from_amount),
            (euros("40"), euros("40"), euros("0"), 1, 1),
        )
        self.assertEqual((report.card_commission, report.card_from_amount), (None, 1))
        self.assertIsNone(report.card_commission_rate)
        (card,) = [row for row in report.rows if row.key == income.CARD]
        self.assertEqual((card.commission, card.from_amount), (None, 1))


class MarkedMeansTests(Fixtures, TestCase):
    """« Avoir » and meal vouchers: the till's alone until a credit is said
    to be one."""

    def setUp(self):
        super().setUp()
        self.debit(date(2026, 6, 1), "1.00")

    def rows(self):
        return {row.key: row for row in income.income_for(JUNE).rows}

    def test_credits_said_to_be_avoir_and_vouchers_fill_their_rows(self):
        self.paid(date(2026, 6, 2), CB=("50.00", 1), Avoir=("300.00", 1), TR=("45.00", 3))
        deposit = self.credit(date(2026, 6, 5), "300.00", category="Acompte", income_source=income.CREDIT)
        IncomePayer.objects.create(key=ISSUER, source=income.VOUCHER)
        refund = self.credit(date(2026, 6, 20), "42.00", ISSUER_LABEL, counterparty=ISSUER)
        report = income.income_for(JUNE)
        rows = {row.key: row for row in report.rows}
        self.assertEqual(list(rows), [income.CARD, income.CASH, income.VOUCHER, income.CREDIT])
        vouchers, credit = rows[income.VOUCHER], rows[income.CREDIT]
        self.assertEqual(
            (vouchers.label, vouchers.till, vouchers.till_payments, vouchers.bank, vouchers.bank_count),
            ("Titres-restaurant", euros("45"), 3, euros("42"), 1),
        )
        self.assertEqual((vouchers.difference, vouchers.note), (euros("-3.00"), income.NOTES[income.VOUCHER]))
        self.assertEqual(
            (credit.label, credit.till, credit.bank, credit.bank_count, credit.difference),
            ("Avoir", euros("300"), euros("300"), 1, euros("0.00")),
        )
        self.assertEqual(credit.note, income.NOTES[income.CREDIT])
        self.assertEqual([one.line.pk for one in report.other_means], [deposit.pk, refund.pk])
        self.assertEqual(report.others, [])

    def test_a_row_is_drawn_when_the_till_has_none(self):
        self.paid(date(2026, 6, 2), CB=("50.00", 1))
        self.credit(date(2026, 6, 5), "300.00", category="Acompte", income_source=income.CREDIT)
        self.credit(date(2026, 6, 20), "42.00", ISSUER_LABEL, counterparty=ISSUER, income_source=income.VOUCHER)
        rows = self.rows()
        self.assertEqual(list(rows), [income.CARD, income.CASH, income.VOUCHER, income.CREDIT])
        self.assertEqual(
            [
                (one.till, one.till_payments, one.bank, one.difference)
                for one in (rows[income.VOUCHER], rows[income.CREDIT])
            ],
            [(euros("0"), 0, euros("42"), euros("42.00")), (euros("0"), 0, euros("300"), euros("300.00"))],
        )

    def test_nothing_said_and_nothing_in_the_till_is_no_row(self):
        self.paid(date(2026, 6, 2), CB=("50.00", 1))
        self.credit(date(2026, 6, 5), "300.00", category="Acompte")
        self.assertEqual(list(self.rows()), [income.CARD, income.CASH, income.OTHER])


class PayoutMarkedAsNoSaleTests(Fixtures, TestCase):
    def test_it_leaves_the_card_figures_and_the_balance_and_joins_the_other_entries(self):
        self.debit(date(2026, 6, 1), "1.00")
        self.paid(date(2026, 6, 1), CB=("100.00", 2))
        self.paid(date(2026, 6, 4), CB=("80.00", 1))
        first = self.payout(date(2026, 6, 2), "100.00", "99.00")
        no_sale = self.payout(date(2026, 6, 5), "80.00", "79.20", income_source=income.OTHER)
        report = income.income_for(JUNE)
        self.assertEqual(
            (report.card_count, report.card_gross, report.card_net, report.card_commission),
            (1, euros("100"), euros("99"), euros("1.00")),
        )
        self.assertEqual([row.entry.line.pk for row in report.payouts], [first.pk])
        self.assertEqual(report.balance.pending, {first.pk: euros("0")})
        self.assertEqual([one.line.pk for one in report.others], [no_sale.pk])
        self.assertEqual(
            [(one.name, one.amount, one.count) for one in report.other_categories], [(NO_CATEGORY, euros("79.20"), 1)]
        )
        rows = {row.key: row for row in report.rows}
        self.assertEqual((rows[income.CARD].bank, rows[income.CARD].difference), (euros("100"), euros("-80.00")))
        self.assertEqual(rows[income.OTHER].bank, euros("79.20"))


class EveryCreditOnceTests(Fixtures, TestCase):
    def test_every_credit_of_the_window_is_in_exactly_one_list(self):
        IncomePayer.objects.create(key=ISSUER, source=income.VOUCHER)
        payouts = [
            self.payout(date(2026, 6, 2), "100.00", "99.00"),
            self.credit(date(2026, 6, 3), "60.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CARD),
        ]
        others = [
            self.credit(date(2026, 6, 8), "500.00", category="Privatisation"),
            self.payout(date(2026, 6, 9), "50.00", "49.50", income_source=income.OTHER),
        ]
        other_means = [
            self.credit(date(2026, 6, 4), "40.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES"),
            self.credit(date(2026, 6, 5), "80.00", "REMISE CHEQUES", "REMISE CHEQUES"),
            self.credit(date(2026, 6, 6), "42.00", ISSUER_LABEL, counterparty=ISSUER),
            self.credit(date(2026, 6, 7), "300.00", category="Acompte", income_source=income.CREDIT),
            self.credit(
                date(2026, 6, 10), "20.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES", income_source=income.CHEQUE
            ),
        ]
        self.credit(date(2026, 5, 31), "10.00")
        self.credit(date(2026, 7, 1), "10.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CARD)
        self.debit(date(2026, 6, 15), "75.00", income_source=income.CARD)
        report = income.income_for(JUNE)
        found = (
            [row.entry.line.pk for row in report.payouts],
            [one.line.pk for one in report.others],
            [one.line.pk for one in report.other_means],
        )
        expected = ([line.pk for line in payouts], [line.pk for line in others], [line.pk for line in other_means])
        self.assertEqual([sorted(one) for one in found], [sorted(one) for one in expected])
        june = payouts + others + other_means
        self.assertEqual(sorted(pk for one in found for pk in one), sorted(line.pk for line in june))
        self.assertEqual(
            (report.received_count, report.received_total), (len(june), sum((line.amount for line in june), euros(0)))
        )
        self.assertEqual(sum(count for _total, count in report.by_source.values()), len(june))


class PayerFollowedTests(Fixtures, TestCase):
    def test_a_payer_remembered_moves_its_past_and_future_credits(self):
        past = self.credit(date(2026, 5, 20), "70.00", TERMINAL_LABEL, counterparty=TERMINAL)
        chosen = self.credit(date(2026, 6, 10), "120.00", TERMINAL_LABEL, counterparty=TERMINAL)
        self.assertEqual(income.income_for(DateRange()).payouts, [])
        income.set_source(chosen, income.CARD, remember=True)
        future = self.credit(date(2026, 6, 20), "90.00", TERMINAL_LABEL, counterparty=TERMINAL)
        report = income.income_for(DateRange())
        self.assertEqual(
            [(row.entry.line.pk, row.entry.how, row.entry.gross_from_amount) for row in report.payouts],
            [(line.pk, income.BY_PAYER, True) for line in (past, chosen, future)],
        )
        self.assertEqual((report.others, report.card_from_amount, report.card_gross), ([], 3, euros("280")))

    def test_a_debit_sharing_the_payers_key_stays_out_of_everything(self):
        IncomePayer.objects.create(key=TERMINAL, source=income.CARD)
        credit = self.credit(date(2026, 6, 10), "120.00", TERMINAL_LABEL, counterparty=TERMINAL)
        # Its choice written by hand in the database: a debit is no entry.
        self.debit(date(2026, 6, 11), "45.00", counterparty=TERMINAL, income_source=income.CARD)
        report = income.income_for(JUNE)
        self.assertEqual(
            (report.received_count, report.received_total, report.card_count, report.card_net),
            (1, euros("120"), 1, euros("120")),
        )
        self.assertEqual([row.entry.line.pk for row in report.payouts], [credit.pk])
        self.assertEqual((report.others, report.other_means), ([], []))
        self.assertEqual(
            [(row.payer.key, row.count, row.total, row.count_all) for row in report.payers],
            [(TERMINAL, 1, euros("120"), 1)],
        )
        # Nor does a choice or « Oublier » count it.
        change = income.set_source(credit, income.CARD, remember=True)
        self.assertEqual((change.followers, change.kept), (0, 0))
        self.assertEqual(income.forget_payer(IncomePayer.objects.get()), 1)


class MarkedBeforeTheTillTests(Fixtures, TestCase):
    #: The rows read, in the page's order.
    ROWS = (income.CARD, income.VOUCHER, income.CREDIT)

    def test_marked_credits_before_the_first_till_day_are_counted_apart(self):
        """Card at its gross - the amount, where none is printed - « Avoir »
        and vouchers at what arrived; « Pas une vente » compared with
        nothing."""
        self.credit(date(2026, 6, 5), "70.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CARD)
        self.credit(date(2026, 6, 6), "200.00", category="Acompte", income_source=income.CREDIT)
        self.credit(date(2026, 6, 7), "30.00", ISSUER_LABEL, counterparty=ISSUER, income_source=income.VOUCHER)
        self.payout(date(2026, 6, 8), "60.00", "59.40", income_source=income.OTHER)
        self.credit(date(2026, 6, 9), "500.00", category="Privatisation")
        self.paid(date(2026, 6, 10), CB=("100.00", 2), Avoir=("150.00", 1), TR=("45.00", 3))
        self.payout(date(2026, 6, 12), "100.00", "99.00")
        self.credit(date(2026, 6, 12), "150.00", category="Acompte", income_source=income.CREDIT)
        report = income.income_for(JUNE)
        self.assertEqual(
            report.bank_before_till,
            {income.CARD: euros("70"), income.CREDIT: euros("200"), income.VOUCHER: euros("30")},
        )
        self.assertEqual((report.bank_before_from, report.bank_before_to), (date(2026, 6, 5), date(2026, 6, 7)))
        rows = {row.key: row for row in report.rows}
        self.assertEqual(
            [(rows[key].bank, rows[key].bank_uncovered, rows[key].till, rows[key].difference) for key in self.ROWS],
            [
                (euros("170"), euros("70"), euros("100"), euros("0.00")),
                (euros("30"), euros("30"), euros("45"), euros("-45.00")),
                (euros("350"), euros("200"), euros("150"), euros("0.00")),
            ],
        )


class TheBalanceWithMarkedPayoutsTests(Fixtures, TestCase):
    """Two weeks of card sales, 50 € a day. The provider printing its gross
    pays 2-9 June; from the 10th a new terminal, retained for its payer,
    pays the day before - but the payout for the 10th never came."""

    NEW_TERMINAL = "NOUVEAU TERMINAL EXEMPLE"

    def setUp(self):
        super().setUp()
        for offset in range(14):
            self.paid(date(2026, 6, 1) + timedelta(days=offset), CB=("50.00", 2))
        for day in range(2, 10):
            self.payout(date(2026, 6, day), "50.00", "49.70")
        IncomePayer.objects.create(key=self.NEW_TERMINAL, source=income.CARD)
        for day in (10, 12, 13, 14):
            label = f"VIR SEPA RECU /FRM {self.NEW_TERMINAL}"
            self.credit(date(2026, 6, day), "50.00", label, counterparty=self.NEW_TERMINAL)

    def pending(self, window):
        return {row.entry.day.day: row.pending for row in income.income_for(window).payouts}

    def test_a_window_does_not_change_the_balance(self):
        whole = self.pending(DateRange())
        late = self.pending(DateRange(date(2026, 6, 12), None))
        self.assertEqual(whole[10], euros("0"))
        # The marked payout of the 10th, outside the window, still counts.
        self.assertEqual(late, {12: euros("50"), 13: euros("50"), 14: euros("50")})
        self.assertEqual(late, {day: whole[day] for day in (12, 13, 14)})


class PayerRowsTests(Fixtures, TestCase):
    def test_each_payer_counts_what_it_decides_over_the_window_and_the_history(self):
        IncomePayer.objects.create(key=TERMINAL, source=income.CARD)
        IncomePayer.objects.create(key=ISSUER, source=income.VOUCHER)
        # Written by hand: no source - listed as it is, deciding nothing.
        IncomePayer.objects.create(key="PAYEUR ILLISIBLE EXEMPLE", source="bitcoin")
        for day, amount in ((date(2026, 5, 20), "40.00"), (date(2026, 6, 10), "60.00"), (date(2026, 6, 15), "25.00")):
            self.credit(day, amount, TERMINAL_LABEL, counterparty=TERMINAL)
        self.credit(date(2026, 7, 2), "10.00", TERMINAL_LABEL, counterparty=TERMINAL)
        # A choice of its own beats its payer: not the payer's to count.
        self.credit(date(2026, 6, 20), "30.00", TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CASH)
        self.debit(date(2026, 6, 21), "5.00", counterparty=TERMINAL)
        unread = self.credit(date(2026, 6, 22), "12.00", counterparty="PAYEUR ILLISIBLE EXEMPLE")
        report = income.income_for(JUNE)
        self.assertEqual(
            [(row.payer.key, row.source_label, row.count, row.total, row.count_all) for row in report.payers],
            [
                (ISSUER, "Titres-restaurant", 0, euros("0"), 0),
                ("PAYEUR ILLISIBLE EXEMPLE", "bitcoin", 0, euros("0"), 0),
                (TERMINAL, "Carte", 2, euros("85.00"), 4),
            ],
        )
        self.assertEqual([one.line.pk for one in report.others], [unread.pk])

    def test_no_payer_retained_is_no_row(self):
        self.credit(date(2026, 6, 10), "60.00", TERMINAL_LABEL, counterparty=TERMINAL)
        self.assertEqual(income.income_for(JUNE).payers, [])


class QueryCountTests(Fixtures, TestCase):
    def build(self, weeks):
        start = date(2026, 1, 5) + timedelta(weeks=self.built)
        for offset in range(weeks * 7):
            day = start + timedelta(days=offset)
            self.sold(day, "30.00")
            self.paid(day, CB=("25.00", 2), Cash=("5.00", 1))
            self.payout(day + timedelta(days=1), "25.00", "24.80")
        self.credit(start, "200.00")
        # A payer every payout shares (the rules recognise them first, so it
        # decides none), a payer a credit follows, and a line chosen on its
        # own: read against the payers in Python, never one query a line.
        IncomePayer.objects.update_or_create(key=MERCHANT, defaults={"source": income.CARD})
        issuer = f"EMETTEUR {chr(ord('A') + self.built)} EXEMPLE"
        IncomePayer.objects.create(key=issuer, source=income.VOUCHER)
        self.credit(start + timedelta(days=2), "45.00", ISSUER_LABEL, counterparty=issuer)
        self.credit(start + timedelta(days=3), "300.00", category="Acompte", income_source=income.CREDIT)
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
        self.assertEqual((report.source_count(income.VOUCHER), report.source_count(income.CREDIT)), (2, 2))
        self.assertEqual(
            [(row.payer.key, row.count_all) for row in report.payers],
            [(MERCHANT, 0), ("EMETTEUR A EXEMPLE", 1), ("EMETTEUR B EXEMPLE", 1)],
        )
