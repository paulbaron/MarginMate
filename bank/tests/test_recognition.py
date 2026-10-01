"""Recognising what an operation of the statement is, and what a credit is in
the till, by rules a person edits (bank/recognition.py, `OperationRule`,
migration 0006) - never by a bank's words written in the code.

This codebase's bugs have been silently wrong money, and a rule misread is
exactly that: a card payment stored as « Autre » loses its three-day window,
a payee misread feeds the aliases and the payers, a payout misread moves the
card balance over the whole history. So each test holds one promise:

* a pattern is checked before it is stored or run (`check`), and refused in
  French: what is not a regular expression, what finds an empty text, a
  named group its meaning does not read, a day without its month;
* the first active rule of its kind, in their order, decides; case never
  matters, an accent only where the pattern spells one; the payee and the
  card date are read whole or not at all;
* the seeded rules recognise exactly what the code recognised before they
  existed - replayed against that code, copied here as the historical
  reference (`OracleTests`), its few deliberate differences said apart;
* another bank's wording is read by editing rules, a TPE payout printing
  no gross included;
* a rule that cannot be applied - refused by the check since it was stored,
  or running past its time limit - recognises nothing, is said, and stops an
  import before anything is written;
* re-reading the stored lines writes the kind, the payee and the card date,
  only what was shown, and nothing else.

Every label, payee, amount and date below is invented. A slow pattern is
SIMULATED: a pattern that really freezes a machine is never run here.
"""

from __future__ import annotations

import inspect
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from unittest import mock

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, migrations
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from bank import income, recognition, reconcile
from bank.models import BankTransaction, CounterpartyAlias, IncomePayer, IncomeSource, InvoicePayment, OperationRule
from bank.statements import parse_statement
from bank.tests.support import (
    SEED,
    SEEDED,
    SEEDED_FORMAT,
    SEEDED_NAMES,
    make_rule,
    pause_seeded_rules,
    rule,
    rules_of,
    seeded,
)
from bank.tests.test_income import Fixtures as IncomeFixtures
from bank.tests.test_reconcile import Fixtures as StatementFixtures
from bank.tests.test_reconcile import card_row, debit_row, statement
from common import DateRange, search_key
from returnables import patterns
from returnables.patterns import PatternError

CARD, DEBIT, TRANSFER, OTHER = (kind.value for kind in BankTransaction.Kind)
BOOKED = date(2026, 7, 3)

#: The seeded rules' names, as the pages show them.
CARD_RULE = "Paiement par carte (FACTURE CARTE)"
DEBIT_RULE = "Prélèvement (PRLV SEPA)"
SENT_RULE = "Virement émis (/BEN)"
RECEIVED_RULE = "Virement reçu (/FRM)"
ANY_TRANSFER_RULE = "Autre virement (VIR)"
PAYOUT_RULE = "Versement carte (TOTAL ENCAISSE)"
CASH_RULE = "Dépôt d'espèces (VERSEMENT ESPECES)"
CHEQUE_RULE = "Remise de chèques (REMISE CHEQUE)"

#: Invented labels shaped like the owner's bank's.
CARD_LABEL = "FACTURE CARTE DU 010726 WING SENG PARIS CARTE 4974XXXXXXXX1111"
DEBIT_LABEL = "PRLV SEPA METRO FRANCE S.A.S.-METRO FRANCE ECH/090726 ID EMETTEUR/FR00ZZZ000000 REF/0000"
PAYOUT_LABEL = "VIR SEPA RECU /FRM BAR EXEMPLE /EID /RNF TRANSFERT 0000001 TOTAL ENCAISSE 987.65 EUROS BAR EXEMPLE"

#: One card rule reading every way a year can be printed - or not.
CARD_ANY_YEAR = r"^CB (?P<jour>[0-9]{1,2})/(?P<mois>[0-9]{1,2})(?:/(?P<annee>[0-9]+))? (?P<tiers>.+)$"


def described(rules, label, bank_type="", booked=BOOKED) -> tuple:
    found = recognition.describe(rules, label, bank_type, booked)
    return (found.kind, found.counterparty, found.card_date, found.rule)


def till(rules, label, bank_type="") -> tuple | None:
    found = recognition.till_reading(rules, label, bank_type)
    return None if found is None else (found.source, found.gross, found.rule)


def unsaved(label, amount="100.00", bank_type="VIREMENT", counterparty="", income_source=""):
    """A credit as the pure functions see it - no database."""
    return BankTransaction(
        operation_date=BOOKED,
        label=label,
        bank_type=bank_type,
        counterparty=counterparty,
        amount=Decimal(amount),
        income_source=income_source,
    )


@contextmanager
def too_slow(*slow):
    """`returnables.patterns.search` running past its time limit on the
    patterns `slow` - simulated. Yields the texts each was asked about."""
    real = patterns.search
    asked = []

    def search(pattern, text, budget):
        if pattern.pattern in slow:
            asked.append(text)
            raise PatternError(f"Le motif « {pattern.pattern} » est trop lent : simplifiez-le.")
        return real(pattern, text, budget)

    with mock.patch.object(patterns, "search", side_effect=search):
        yield asked


class FreezingPattern:
    """A compiled pattern whose every search runs out of time, as `regex`
    says it: TimeoutError."""

    pattern = "(?:lent)"
    groupindex: dict = {}

    def search(self, text, timeout=None, concurrent=None):
        raise TimeoutError


# -- The check ------------------------------------------------------------------------------------------------------


class CheckTests(SimpleTestCase):
    def refusal(self, meaning, pattern, searched="label") -> str:
        with self.assertRaises(PatternError) as refused:
            recognition.check(meaning, searched, pattern)
        return refused.exception.message

    def test_what_is_no_regular_expression_is_refused(self):
        self.assertEqual(self.refusal("card_payment", "CARTE ("), "Motif : parenthèse non fermée (position 7).")

    def test_a_pattern_finding_an_empty_text_finds_everything_and_is_refused(self):
        everything = "Motif : le motif accepte une ligne vide : il trouverait quelque chose sur n'importe quelle ligne."
        for pattern in ("X*", "CB|", "^", "(?P<tiers>.*)"):
            with self.subTest(pattern=pattern):
                meaning = "debit" if "tiers" in pattern else "cash"
                self.assertEqual(self.refusal(meaning, pattern), everything)
        for blank in ("", "   "):
            with self.subTest(pattern=blank):
                self.assertEqual(self.refusal("cash", blank), "Motif : le motif est vide.")

    def test_the_guard_refuses_before_anything_is_compiled(self):
        """The motif guard of returnables/patterns.py: a pattern whose
        compiling alone would freeze the machine never reaches `regex`."""
        with mock.patch.object(patterns.regex, "compile", side_effect=AssertionError("compiled")):
            self.assertEqual(
                self.refusal("card_payment", "(?:x{65535}){65535}"),
                "Motif : répétition trop grande : 100 fois au plus.",
            )

    def test_each_meaning_reads_its_own_named_groups_and_no_other(self):
        card = "seuls (?P<tiers>…), (?P<jour>…), (?P<mois>…), (?P<annee>…) servent à « Paiement par carte »"
        cases = [
            ("card_payment", r"CB (?P<encaisse>[0-9]+)", f"le groupe (?P<encaisse>…) ne sert à rien ; {card}"),
            ("card_payment", r"CB (?P<x>\w+)", f"le groupe (?P<x>…) ne sert à rien ; {card}"),
            (
                "debit",
                r"PRLV (?P<jour>[0-9]{2})",
                "le groupe (?P<jour>…) ne sert à rien ; seuls (?P<tiers>…) servent à « Prélèvement »",
            ),
            (
                "transfer",
                r"VIR (?P<foo>\w+)",
                "le groupe (?P<foo>…) ne sert à rien ; seuls (?P<tiers>…) servent à « Virement »",
            ),
            (
                "other_operation",
                r"FRAIS (?P<encaisse>[0-9]+)",
                "le groupe (?P<encaisse>…) ne sert à rien ; seuls (?P<tiers>…) servent à « Autre opération »",
            ),
            (
                "payout",
                r"TPE (?P<tiers>\w+)",
                "le groupe (?P<tiers>…) ne sert à rien ; seuls (?P<encaisse>…) servent à « Versement de carte (TPE) »",
            ),
        ]
        silent = {
            "cash": "Dépôt d'espèces",
            "cheque": "Remise de chèques",
            "voucher": "Titres-restaurant",
            "credit": "Avoir",
            "not_a_sale": "Pas une vente",
        }
        for meaning, label in silent.items():
            cases.append(
                (
                    meaning,
                    r"DEPOT (?P<tiers>\w+)",
                    f"le groupe (?P<tiers>…) ne sert à rien ; « {label} » ne lit aucun groupe nommé",
                )
            )
        for meaning, pattern, said in cases:
            with self.subTest(meaning=meaning, pattern=pattern):
                self.assertEqual(self.refusal(meaning, pattern), f"Motif : {said}.")

    def test_the_groups_a_meaning_reads_are_accepted(self):
        accepted = [
            ("card_payment", CARD_ANY_YEAR),
            ("card_payment", r"CB (?P<jour>[0-9]{2})(?P<mois>[0-9]{2}) (?P<tiers>.+)"),
            ("card_payment", r"^CB\b"),
            ("debit", r"^PRELEVEMENT (?P<tiers>.+)"),
            ("transfer", r"^VIREMENT (?:DE|A) (?P<tiers>.+)"),
            ("other_operation", r"^FRAIS (?P<tiers>.+)"),
            ("payout", r"REMISE TPE(?: BRUT (?P<encaisse>[0-9.,]+))?"),
            ("payout", r"REMISE TPE"),
            ("cash", r"^(VERSEMENT|DEPOT) ESPECES"),
            ("not_a_sale", r"INDEMNITE"),
        ]
        for meaning, pattern in accepted:
            with self.subTest(meaning=meaning, pattern=pattern):
                self.assertEqual(recognition.check(meaning, "label", pattern).pattern, pattern)

    def test_the_day_and_the_month_go_together_and_the_year_only_beside_them(self):
        both = "Motif : (?P<jour>…) et (?P<mois>…) vont ensemble."
        self.assertEqual(self.refusal("card_payment", r"CB (?P<jour>[0-9]{2})"), both)
        self.assertEqual(self.refusal("card_payment", r"CB (?P<mois>[0-9]{2})"), both)
        self.assertEqual(self.refusal("card_payment", r"CB (?P<jour>[0-9]{2}) (?P<annee>[0-9]{2})"), both)
        self.assertEqual(
            self.refusal("card_payment", r"CB (?P<annee>[0-9]{2})"),
            "Motif : (?P<annee>…) ne sert qu'avec (?P<jour>…) et (?P<mois>…).",
        )

    def test_an_unknown_meaning_or_field_is_refused(self):
        self.assertEqual(self.refusal("refund", "CB"), "Motif : signification inconnue (« refund »).")
        self.assertEqual(
            self.refusal("cash", "CB", searched="counterparty"), "Motif : champ inconnu (« counterparty »)."
        )

    def test_the_model_refuses_what_the_check_refuses_on_its_pattern(self):
        refused = OperationRule(name="Carte", meaning="card_payment", searched="label", pattern=r"CB (?P<jour>\d\d)")
        with self.assertRaises(ValidationError) as caught:
            refused.clean()
        self.assertEqual(
            caught.exception.message_dict, {"pattern": ["Motif : (?P<jour>…) et (?P<mois>…) vont ensemble."]}
        )
        OperationRule(name="Carte", meaning="card_payment", searched="label", pattern=r"^CB\b").clean()

    def test_a_stored_rule_the_check_refuses_is_listed_never_raised(self):
        rules = rules_of(
            rule("Carte cassée", "card_payment", "CARTE ("),
            rule("Dépôt vide", "cash", "X*", "bank_type"),
            rule("Carte", "card_payment", r"^CB\b"),
        )
        self.assertEqual([one.name for one in rules.kinds], ["Carte"])
        self.assertEqual(rules.till, ())
        self.assertEqual(
            rules.invalid,
            {
                "Carte cassée": "Motif : parenthèse non fermée (position 7).",
                "Dépôt vide": "Motif : le motif accepte une ligne vide : il trouverait quelque chose sur n'importe "
                "quelle ligne.",
            },
        )
        self.assertEqual(
            rules.problems,
            [
                "Règle « Carte cassée » : parenthèse non fermée (position 7) - elle ne reconnaît rien.",
                (
                    "Règle « Dépôt vide » : le motif accepte une ligne vide : il trouverait quelque chose sur "
                    "n'importe quelle ligne - elle ne reconnaît rien."
                ),
            ],
        )
        self.assertEqual(
            rules.refusal,
            "Import annulé : les règles de reconnaissance « Carte cassée », « Dépôt vide » ne peuvent pas être "
            "appliquées (motifs invalides ou trop lents). Corrigez-les sur « Reconnaissance des opérations », puis "
            "importez à nouveau.",
        )

    def test_rules_that_all_pass_have_no_problem_and_no_refusal(self):
        self.assertEqual((SEEDED.invalid, SEEDED.slow, SEEDED.problems, SEEDED.refusal), ({}, [], [], ""))
        self.assertEqual((rules_of().problems, rules_of().refusal), ([], ""))


# -- What the operation is ------------------------------------------------------------------------------------------


class DescribeTests(SimpleTestCase):
    KINDS = rules_of(
        rule("Carte CB", "card_payment", CARD_ANY_YEAR),
        rule("Prélèvement européen", "debit", r"^PRELEVEMENT EUROPEEN (?P<tiers>.+)$"),
        rule("Virement de", "transfer", r"^VIREMENT DE (?P<tiers>.+)$"),
        rule("Frais", "other_operation", r"^FRAIS (?P<tiers>.+)$"),
        rule("Retrait", "card_payment", r"^RETRAIT DAB\b"),
    )

    def test_each_meaning_is_its_kind_with_its_payee(self):
        cases = {
            "CB 01/07/26 BOULANGERIE EXEMPLE": (CARD, "BOULANGERIE EXEMPLE", date(2026, 7, 1), "Carte CB"),
            "PRELEVEMENT EUROPEEN FOURNISSEUR EXEMPLE": (DEBIT, "FOURNISSEUR EXEMPLE", None, "Prélèvement européen"),
            "VIREMENT DE ASSOCIATION EXEMPLE": (TRANSFER, "ASSOCIATION EXEMPLE", None, "Virement de"),
            "FRAIS BANQUE EXEMPLE": (OTHER, "BANQUE EXEMPLE", None, "Frais"),
            # A rule reading no payee names nobody.
            "RETRAIT DAB 0042": (CARD, "", None, "Retrait"),
        }
        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(described(self.KINDS, label), expected)

    def test_what_no_rule_finds_is_other_with_no_payee_and_no_date(self):
        for rules in (self.KINDS, rules_of()):
            with self.subTest(rules=len(rules.kinds)):
                self.assertIs(recognition.describe(rules, "COTISATION CARTE EXEMPLE", "", BOOKED), recognition.NOTHING)
        self.assertEqual(described(self.KINDS, "", ""), (OTHER, "", None, ""))
        self.assertEqual(described(self.KINDS, None, None), (OTHER, "", None, ""))

    def test_the_payee_has_its_spaces_folded_and_is_cut_to_the_column(self):
        self.assertEqual(described(self.KINDS, "VIREMENT DE   ASSOCIATION \t EXEMPLE  ")[1], "ASSOCIATION EXEMPLE")
        payee = described(self.KINDS, "VIREMENT DE " + "AB " * 150)[1]
        # The 255th character is a space: cut there, and never kept.
        self.assertEqual((payee, len(payee)), (("AB " * 85).rstrip(), 254))
        self.assertEqual(len(described(self.KINDS, "VIREMENT DE " + "X" * 400)[1]), recognition.PAYEE_MAX)

    def test_a_card_date_printed_with_its_year_is_read_as_printed(self):
        cases = {
            "CB 01/07/26 X": date(2026, 7, 1),
            "CB 01/07/2026 X": date(2026, 7, 1),
            "CB 1/7/26 X": date(2026, 7, 1),
            # Printed, a year is not second-guessed by the booking day.
            "CB 01/07/2025 X": date(2025, 7, 1),
            "CB 31/12/99 X": date(2099, 12, 31),
            "CB 29/02/28 X": date(2028, 2, 29),
        }
        for label, used in cases.items():
            with self.subTest(label=label):
                self.assertEqual(described(self.KINDS, label)[2], used)

    def test_a_card_date_without_its_year_is_the_latest_that_is_not_after_the_booking(self):
        cases = [
            # Used on New Year's Eve, booked on the 2nd: last year.
            ("CB 31/12 X", date(2027, 1, 2), date(2026, 12, 31)),
            ("CB 02/01 X", date(2027, 1, 2), date(2027, 1, 2)),
            # A day after the booking cannot be this year's.
            ("CB 03/01 X", date(2027, 1, 2), date(2026, 1, 3)),
            ("CB 15/06 X", date(2026, 6, 14), date(2025, 6, 15)),
            ("CB 1/7 X", date(2026, 7, 3), date(2026, 7, 1)),
            ("CB 29/02 X", date(2028, 3, 1), date(2028, 2, 29)),
        ]
        for label, booked, used in cases:
            with self.subTest(label=label, booked=booked):
                self.assertEqual(described(self.KINDS, label, booked=booked)[:3], (CARD, "X", used))

    def test_no_year_and_no_booking_day_is_no_card_date(self):
        self.assertEqual(described(self.KINDS, "CB 01/07 X", booked=None)[:3], (CARD, "X", None))

    def test_what_is_no_date_is_no_card_date_and_the_payment_stays_a_card(self):
        """Read whole or not at all: a day of 32 is no card date - never the
        1st of the next month - and the line is still a card payment, with
        its payee."""
        for label in (
            "CB 32/07/26 X",
            "CB 31/04/26 X",
            "CB 00/07/26 X",
            "CB 15/13/26 X",
            "CB 29/02/27 X",
            "CB 31/04 X",
            "CB 15/00 X",
        ):
            with self.subTest(label=label):
                self.assertEqual(described(self.KINDS, label)[:3], (CARD, "X", None))

    def test_a_day_or_a_month_of_ten_digits_is_no_card_date_never_a_crash(self):
        # `date` raises OverflowError, not ValueError, past a C long: an
        # unbounded group on a long reference took the import, the rules page
        # and « Tester » down with a 500 (review, 01/10/2026).
        rules = rules_of(rule("Carte", "card_payment", r"^CB (?P<jour>[0-9]+)/(?P<mois>[0-9]+) (?P<tiers>.+)$"))
        for label in ("CB 99999999999/07 BOUTIQUE", "CB 07/99999999999 BOUTIQUE", "CB 20260715123456/2 BOUTIQUE"):
            with self.subTest(label=label):
                self.assertEqual(described(rules, label)[:3], (CARD, "BOUTIQUE", None))
        parsed = parse_statement(
            statement("03/07/2026;PAIEMENT CB;CB;CB 20260715123456/2 BOUTIQUE;03/07/2026;-4,10"), rules, SEEDED_FORMAT
        )
        self.assertEqual(
            [(line.kind, line.counterparty, line.card_date) for line in parsed.lines], [(CARD, "BOUTIQUE", None)]
        )

    def test_a_year_outside_2000_to_2099_is_no_card_date(self):
        for label in ("CB 01/07/1999 X", "CB 01/07/2100 X", "CB 01/07/202 X", "CB 01/07/20260 X", "CB 01/07/0 X"):
            with self.subTest(label=label):
                self.assertEqual(described(self.KINDS, label)[:3], (CARD, "X", None))
        # Nor a year inferred before 2000.
        self.assertEqual(described(self.KINDS, "CB 31/12 X", booked=date(2000, 1, 2))[:3], (CARD, "X", None))

    def test_digits_that_are_not_ascii_are_no_date(self):
        """`\\d` finds Arabic-Indic digits; `int` would read them. Never a
        card date."""
        rules = rules_of(rule("Carte", "card_payment", r"^CB (?P<jour>\d{2})/(?P<mois>\d{2}) (?P<tiers>.+)$"))
        label = "CB \N{ARABIC-INDIC DIGIT ONE}\N{ARABIC-INDIC DIGIT TWO}/07 X"
        self.assertEqual(described(rules, label)[:3], (CARD, "X", None))

    def test_case_never_matters(self):
        rules = rules_of(rule("Prélèvement", "debit", r"^prelevement europeen (?P<tiers>.+)$"))
        self.assertEqual(described(rules, "PRELEVEMENT EUROPEEN FOURNISSEUR")[:2], (DEBIT, "FOURNISSEUR"))
        self.assertEqual(described(self.KINDS, "prelevement europeen fournisseur")[:2], (DEBIT, "fournisseur"))

    def test_a_pattern_without_accents_finds_the_accented_text(self):
        rules = rules_of(
            rule("Prélèvement", "debit", r"^PRELEVEMENT (?P<tiers>.+)$"),
            rule("Espèces", "other_operation", r"VERSEMENT ESPECES", "bank_type"),
        )
        # Found on the text without its accents - and read back on the text
        # as printed: the payee keeps its accents and its case.
        self.assertEqual(described(rules, "PRÉLÈVEMENT CAFÉ ÉTOILE")[:2], (DEBIT, "CAFÉ ÉTOILE"))
        for bank_type in ("VERSEMENT ESPÈCES", "Versement espèces", "versement especes"):
            with self.subTest(bank_type=bank_type):
                self.assertEqual(described(rules, "DEPOT", bank_type)[0], OTHER)
                self.assertEqual(described(rules, "DEPOT", bank_type)[3], "Espèces")

    def test_a_text_whose_letters_do_not_fold_one_for_one_is_read_on_the_folded_text(self):
        # Accents written as separate marks vanish when folded: the positions
        # no longer line up, so the capture is the folded text's own.
        rules = rules_of(rule("Prélèvement", "debit", r"^PRELEVEMENT (?P<tiers>.+)$"))
        label = "PRE\N{COMBINING ACUTE ACCENT}LE\N{COMBINING GRAVE ACCENT}VEMENT CAFE\N{COMBINING ACUTE ACCENT}"
        self.assertEqual(described(rules, label)[:2], (DEBIT, "cafe"))

    def test_a_rule_slow_over_many_texts_is_set_aside_though_no_match_timed_out(self):
        rules = rules_of(rule("Lente", "debit", r"^PRLV (?P<tiers>.+)$"))
        ticks = iter(range(0, 1000, 3))
        with mock.patch("bank.recognition.thread_time", side_effect=lambda: float(next(ticks))):
            self.assertEqual(described(rules, "PRLV UN")[:2], (DEBIT, "UN"))
            self.assertEqual(rules.slow, [])
            # Three seconds a match: the second passes RULE_SECONDS.
            self.assertEqual(described(rules, "PRLV DEUX")[:2], (DEBIT, "DEUX"))
            self.assertEqual(rules.slow, ["Lente"])
            self.assertEqual(described(rules, "PRLV TROIS")[:2], (OTHER, ""))
        self.assertIn("Lente", rules.refusal)

    def test_a_capture_comes_from_the_text_as_printed_wherever_it_is_found_so(self):
        rules = rules_of(rule("Carte", "card_payment", r"^CB (?P<tiers>.+)$"))
        self.assertEqual(described(rules, "CB CAFÉ ÉTOILE")[1], "CAFÉ ÉTOILE")

    def test_an_accent_the_pattern_spells_is_required(self):
        rules = rules_of(rule("Espèces", "other_operation", r"ESPÈCES (?P<tiers>.+)$"))
        self.assertEqual(described(rules, "ESPÈCES GUICHET")[:2], (OTHER, "GUICHET"))
        self.assertEqual(described(rules, "espèces guichet")[:2], (OTHER, "guichet"))
        self.assertEqual(described(rules, "ESPECES GUICHET"), (OTHER, "", None, ""))

    def test_the_first_rule_in_their_order_decides(self):
        first = rule("Virement nommé", "transfer", r"^VIREMENT DE (?P<tiers>.+)$")
        second = rule("Tout virement", "other_operation", r"^VIREMENT")
        label = "VIREMENT DE ASSOCIATION EXEMPLE"
        self.assertEqual(
            described(rules_of(first, second), label), (TRANSFER, "ASSOCIATION EXEMPLE", None, "Virement nommé")
        )
        self.assertEqual(described(rules_of(second, first), label), (OTHER, "", None, "Tout virement"))

    def test_the_first_rule_by_position_in_the_text_is_not_what_decides(self):
        """Whichever rule matches FIRST IN THE TEXT does not matter: the
        rules' order does."""
        rules = rules_of(
            rule("Fin", "debit", r"FOURNISSEUR (?P<tiers>\w+)$"),
            rule("Début", "transfer", r"^VIREMENT (?P<tiers>\w+)"),
        )
        self.assertEqual(described(rules, "VIREMENT EXEMPLE FOURNISSEUR ALPHA")[:2], (DEBIT, "ALPHA"))

    def test_an_inactive_rule_recognises_nothing(self):
        rules = rules_of(rule("Virement de", "transfer", r"^VIREMENT DE (?P<tiers>.+)$", is_active=False))
        self.assertEqual(rules.kinds, ())
        self.assertIs(recognition.describe(rules, "VIREMENT DE ASSOCIATION", "", BOOKED), recognition.NOTHING)

    def test_each_rule_searches_its_own_field(self):
        rules = rules_of(
            rule("Prélèvement par type", "debit", r"^PRELEVEMENT$", "bank_type"),
            rule("Virement par libellé", "transfer", r"^VIREMENT"),
        )
        self.assertEqual(described(rules, "ECHEANCE EXEMPLE", "PRELEVEMENT")[0], DEBIT)
        # The label says « PRELEVEMENT »: the rule reading the type does not see it.
        self.assertEqual(described(rules, "PRELEVEMENT", "ECHEANCE")[0], OTHER)
        self.assertEqual(described(rules, "VIREMENT X", "PRELEVEMENT")[0], DEBIT)
        self.assertEqual(described(rules, "AUTRE", "VIREMENT")[0], OTHER)

    def test_a_text_is_read_up_to_its_limit(self):
        rules = rules_of(rule("Carte", "card_payment", r"\bCB\b"))
        limit = recognition.TEXT_LIMIT
        self.assertEqual(described(rules, "X" * (limit - 3) + " CB")[0], CARD)
        self.assertEqual(described(rules, "X" * (limit - 2) + " CB")[0], OTHER)

    def test_a_till_rule_never_says_what_an_operation_is_nor_a_kind_rule_what_a_credit_is(self):
        rules = rules_of(
            rule("Versement TPE", "payout", r"REMISE TPE"),
            rule("Virement", "transfer", r"^VIREMENT DE (?P<tiers>.+)$"),
        )
        self.assertEqual(described(rules, "REMISE TPE 0042"), (OTHER, "", None, ""))
        self.assertIsNone(till(rules, "VIREMENT DE ASSOCIATION"))
        self.assertEqual(till(rules, "VIREMENT DE REMISE TPE"), (income.CARD, None, "Versement TPE"))


# -- What a credit is in the till -----------------------------------------------------------------------------------


class TillReadingTests(SimpleTestCase):
    def test_each_till_meaning_is_its_source(self):
        cases = {
            "payout": income.CARD,
            "cash": income.CASH,
            "cheque": income.CHEQUE,
            "voucher": income.VOUCHER,
            "credit": income.CREDIT,
            "not_a_sale": income.OTHER,
        }
        for meaning, source in cases.items():
            with self.subTest(meaning=meaning):
                rules = rules_of(rule(f"Règle {meaning}", meaning, r"MOT EXEMPLE"))
                self.assertEqual(till(rules, "VIR RECU MOT EXEMPLE"), (source, None, f"Règle {meaning}"))
                self.assertIsNone(till(rules, "VIR RECU AUTRE"))

    def test_a_payout_rule_reading_the_gross_reads_it_whole(self):
        rules = rules_of(rule("TPE", "payout", r"REMISE TPE BRUT (?P<encaisse>[0-9][0-9 .,]*) EUR"))
        cases = {
            "REMISE TPE BRUT 200.00 EUR": Decimal("200.00"),
            "REMISE TPE BRUT 1 234,50 EUR": Decimal("1234.50"),
            "REMISE TPE BRUT 1.234,50 EUR": Decimal("1234.50"),
            "REMISE TPE BRUT 0 EUR": Decimal("0"),
        }
        for label, gross in cases.items():
            with self.subTest(label=label):
                self.assertEqual(till(rules, label), (income.CARD, gross, "TPE"))

    def test_a_gross_that_cannot_be_read_whole_is_no_payout_of_that_rule(self):
        """« 12.345 » read as far as it goes would be a gross of 12,34: a
        wrong commission. That rule does not recognise the line; the next
        one is asked."""
        reading = rule("TPE brut", "payout", r"REMISE TPE BRUT (?P<encaisse>[0-9.,]+)")
        fallback = rule("TPE", "payout", r"REMISE TPE")
        for printed in ("12.345", "1,234.567", "1.2.3"):
            with self.subTest(printed=printed):
                label = f"REMISE TPE BRUT {printed}"
                self.assertIsNone(till(rules_of(reading), label))
                self.assertEqual(till(rules_of(reading, fallback), label), (income.CARD, None, "TPE"))

    def test_a_gross_group_taking_no_part_in_the_match_reads_no_gross(self):
        rules = rules_of(rule("TPE", "payout", r"^REMISE TPE(?: BRUT (?P<encaisse>[0-9.,]+))?"))
        self.assertEqual(till(rules, "REMISE TPE 0042"), (income.CARD, None, "TPE"))
        self.assertEqual(till(rules, "REMISE TPE BRUT 80.00"), (income.CARD, Decimal("80.00"), "TPE"))

    def test_the_first_till_rule_in_their_order_decides(self):
        cash = rule("Espèces", "cash", r"DEPOT", "bank_type")
        none = rule("Pas une vente", "not_a_sale", r"^DEPOT ASSOCIATION", "bank_type")
        self.assertEqual(till(rules_of(none, cash), "X", "DEPOT ASSOCIATION"), (income.OTHER, None, "Pas une vente"))
        self.assertEqual(till(rules_of(cash, none), "X", "DEPOT ASSOCIATION"), (income.CASH, None, "Espèces"))

    def test_the_gross_printed_for_a_card_a_person_said(self):
        """The first payout rule READING a gross that reads one - a rule
        reading none is passed over, and so is one whose capture is no
        amount."""
        rules = rules_of(
            rule("Espèces", "cash", r"BRUT"),
            rule("TPE", "payout", r"REMISE TPE"),
            rule("Brut illisible", "payout", r"BRUT (?P<encaisse>[0-9.,]+) EUR"),
            rule("Brut", "payout", r"BRUT (?P<encaisse>[0-9.,]+)"),
        )
        self.assertEqual(recognition.printed_gross(rules, "REMISE TPE BRUT 80.00 EUR"), Decimal("80.00"))
        self.assertEqual(recognition.printed_gross(rules, "BRUT 12.345 EUR BRUT 80.00"), None)
        self.assertEqual(recognition.printed_gross(rules, "VIR BRUT 12.345"), None)
        self.assertIsNone(recognition.printed_gross(rules, "REMISE TPE 0042"))
        self.assertIsNone(recognition.printed_gross(rules_of(), "BRUT 80.00"))


class IncomeReadingTests(SimpleTestCase):
    """The till rules in `income`'s order: the line's own choice, then a
    rule that recognises the line, then its payer, then « Autres entrées »."""

    INDEMNITY = rule("Indemnité d'assurance", "not_a_sale", r"INDEMNITE SINISTRE")
    TPE = rule("TPE sans brut", "payout", r"REMISE TPE")

    def test_a_rule_saying_no_sale_beats_a_retained_payer(self):
        """« Recognised » is « a rule found it », not « it is a sale »: a
        payer « Carte » retained for the insurer's other transfers does not
        turn its indemnity into card takings."""
        line = unsaved("VIR SEPA RECU /FRM ASSUREUR EXEMPLE INDEMNITE SINISTRE 0042", counterparty="ASSUREUR EXEMPLE")
        payers = {"ASSUREUR EXEMPLE": income.CARD}
        entry = income.entry_for(line, payers, rules_of(self.INDEMNITY))
        self.assertEqual(
            (entry.source, entry.how, entry.rule, entry.choice, entry.gross),
            (income.OTHER, income.BY_RULE, "Indemnité d'assurance", income.AUTOMATIC, None),
        )
        self.assertEqual(entry.how_label, "règle « Indemnité d'assurance »")
        self.assertFalse(entry.remember_by_default)
        self.assertFalse(income.follows_its_payer(line, rules_of(self.INDEMNITY)))
        # Without the rule, the payer decides.
        self.assertEqual(income.reading_of(line, payers), (income.CARD, income.BY_PAYER, None))
        self.assertTrue(income.follows_its_payer(line))

    def test_a_payout_rule_reading_no_gross_keeps_the_amount_though_a_later_rule_reads_one(self):
        """The rule that recognised the payout decides its gross: one placed
        after it, reading a gross on the same label, lends it none - the
        commission would come from a figure the deciding rule never read."""
        gross = rule("Brut", "payout", r"TOTAL ENCAISSE (?P<encaisse>[0-9.]+) EUROS")
        rules = rules_of(self.TPE, gross)
        label = "REMISE TPE 0042 TOTAL ENCAISSE 200.00 EUROS"
        entry = income.entry_for(unsaved(label, "198.00"), None, rules)
        self.assertEqual(
            (entry.source, entry.how, entry.rule, entry.gross, entry.gross_from_amount, entry.commission),
            (income.CARD, income.BY_RULE, "TPE sans brut", Decimal("198.00"), True, None),
        )
        # The later rule alone reads its gross; and a card a PERSON said takes
        # the first gross a payout rule reads (`printed_gross`).
        entry = income.entry_for(unsaved(label, "198.00"), None, rules_of(gross))
        self.assertEqual((entry.rule, entry.gross, entry.commission), ("Brut", Decimal("200.00"), Decimal("2.00")))
        entry = income.entry_for(unsaved(label, "198.00", income_source=income.CARD), None, rules)
        self.assertEqual((entry.how, entry.gross, entry.gross_from_amount), (income.BY_LINE, Decimal("200.00"), False))

    def test_a_payout_rule_reading_no_gross_counts_the_amount_received(self):
        line = unsaved("REMISE TPE 0042 COMMERCANT EXEMPLE", "245.50")
        entry = income.entry_for(line, None, rules_of(self.TPE))
        self.assertEqual(
            (entry.source, entry.how, entry.gross, entry.gross_from_amount, entry.commission, entry.commission_rate),
            (income.CARD, income.BY_RULE, Decimal("245.50"), True, None, None),
        )
        self.assertEqual(
            (entry.name, entry.how_label, entry.choice), ("Versement carte", "règle « TPE sans brut »", "")
        )
        self.assertFalse(entry.remember_by_default)

    def test_a_card_a_person_said_takes_the_gross_a_payout_rule_reads_else_the_amount(self):
        printed = unsaved(PAYOUT_LABEL.replace("987.65", "200.00"), "198.00", income_source=income.CARD)
        entry = income.entry_for(printed, None, SEEDED)
        self.assertEqual(
            (entry.how, entry.gross, entry.gross_from_amount, entry.commission, entry.rule),
            (income.BY_LINE, Decimal("200.00"), False, Decimal("2.00"), ""),
        )
        # A rule reading no gross gives none: the amount received.
        entry = income.entry_for(printed, None, rules_of(self.TPE))
        self.assertEqual((entry.gross, entry.gross_from_amount), (Decimal("198.00"), True))
        # Through the payer, the same.
        by_payer = unsaved("VIR /FRM TERMINAL EXEMPLE TOTAL ENCAISSE 50.00 EUROS", "49.00", counterparty="TERMINAL")
        entry = income.entry_for(by_payer, {"TERMINAL": income.CARD}, rules_of(self.INDEMNITY))
        self.assertEqual((entry.how, entry.gross, entry.gross_from_amount), (income.BY_PAYER, Decimal("49.00"), True))

    def test_the_rule_that_recognised_a_line_is_said_by_its_name(self):
        entry = income.entry_for(unsaved(PAYOUT_LABEL, "980.00"), None, SEEDED)
        self.assertEqual(
            (entry.rule, entry.how_label, entry.gross), (PAYOUT_RULE, f"règle « {PAYOUT_RULE} »", Decimal("987.65"))
        )
        reading = income.reading_of(unsaved(PAYOUT_LABEL), rules=SEEDED)
        self.assertEqual((reading.source, reading.how, reading.till.rule), (income.CARD, income.BY_RULE, PAYOUT_RULE))

    def test_without_rules_nothing_is_recognised_and_nothing_is_read(self):
        """No rules handed in is no rules - never a query to fetch them
        (this class has no database)."""
        line = unsaved(PAYOUT_LABEL, counterparty="BAR EXEMPLE")
        self.assertIsNone(income.automatic_source(line, None))
        entry = income.entry_for(line)
        self.assertEqual(
            (entry.source, entry.how, entry.rule, entry.how_label, entry.remember_by_default),
            (income.OTHER, income.BY_RULE, "", "non reconnue", True),
        )
        entry = income.entry_for(unsaved(PAYOUT_LABEL, "1.00", income_source=income.CARD))
        self.assertEqual((entry.gross, entry.gross_from_amount), (Decimal("1.00"), True))


# -- Too slow -------------------------------------------------------------------------------------------------------


class SlowRuleTests(SimpleTestCase):
    def test_a_rule_running_past_its_limit_recognises_nothing_from_then_on(self):
        rules = seeded()
        card_pattern = SEED.RULES[0][4]
        with too_slow(card_pattern) as asked:
            self.assertEqual(described(rules, CARD_LABEL), (OTHER, "", None, ""))
            self.assertEqual(described(rules, CARD_LABEL), (OTHER, "", None, ""))
            # The others are still asked.
            self.assertEqual(described(rules, DEBIT_LABEL)[:2], (DEBIT, "METRO FRANCE S.A.S.-METRO FRANCE"))
        # Asked twice - a match is tried again before its rule is set aside,
        # the limit being the clock's - then never again.
        self.assertEqual(asked, [CARD_LABEL, CARD_LABEL])
        self.assertEqual(rules.slow, [CARD_RULE])
        self.assertEqual(rules.problems, [f"Règle « {CARD_RULE} » : motif trop lent, ignoré - simplifiez-le."])
        self.assertEqual(
            rules.refusal,
            f"Import annulé : la règle de reconnaissance « {CARD_RULE} » ne peut pas être appliquée (motif invalide "
            "ou trop lent). Corrigez-la sur « Reconnaissance des opérations », puis importez à nouveau.",
        )
        # Another reading starts afresh.
        self.assertEqual(seeded().slow, [])

    def test_a_timeout_regex_raises_is_a_slow_rule_not_a_crash(self):
        rules = recognition.Rules(kinds=(recognition.Rule("Lent", "debit", "label", FreezingPattern()),))
        self.assertIs(recognition.describe(rules, "PRLV X", "", BOOKED), recognition.NOTHING)
        self.assertEqual(rules.slow, ["Lent"])
        rules = recognition.Rules(till=(recognition.Rule("Lent", "cash", "bank_type", FreezingPattern()),))
        self.assertIsNone(recognition.till_reading(rules, "X", "VERSEMENT"))
        self.assertIsNone(recognition.printed_gross(rules, "X", "VERSEMENT"))
        self.assertEqual(rules.slow, ["Lent"])

    def test_a_match_out_of_time_once_is_asked_again_before_its_rule_is_set_aside(self):
        """The per-match limit is the clock's: one match can lose it to a busy
        server rather than to its pattern. Out of time once, it is asked
        again; only out of time twice is its rule too slow."""
        out_of_time = PatternError("Le motif « ^PRLV » est trop lent : simplifiez-le.")
        debit = rule("Prélèvement", "debit", r"^PRLV (?P<tiers>.+)$")
        rules = rules_of(debit)
        with mock.patch.object(patterns, "search", side_effect=[out_of_time, None]) as search:
            self.assertEqual(described(rules, "PRLV EXEMPLE"), (OTHER, "", None, ""))
        self.assertEqual(search.call_count, 2)
        self.assertEqual(rules.slow, [])
        # Still applied to the next text.
        self.assertEqual(described(rules, "PRLV EXEMPLE")[:2], (DEBIT, "EXEMPLE"))

        rules = rules_of(debit)
        with mock.patch.object(patterns, "search", side_effect=[out_of_time, out_of_time]) as search:
            self.assertEqual(described(rules, "PRLV EXEMPLE"), (OTHER, "", None, ""))
        self.assertEqual(search.call_count, 2)
        self.assertEqual(rules.slow, ["Prélèvement"])
        # Set aside: never asked again in this reading.
        self.assertEqual(described(rules, "PRLV EXEMPLE"), (OTHER, "", None, ""))

    def test_a_rule_is_billed_its_own_time_never_the_wait_behind_busy_threads(self):
        """The server is one process whose gathers and imports are threads of
        pure Python: a match that releases the GIL waits to get it back behind
        them, and that wait is not its pattern's (review, 01/10/2026: billed
        by the clock, a seeded rule beside two busy threads was found « trop
        lent » and the import refused).

        Fifty texts and not hundreds: each match waits a GIL switch for real,
        a timer tick on Windows (15 ms and more), and the test stays under two
        seconds. The limit is lowered so the clock would pass it many times
        over; the rule's own time comes nowhere near it."""
        limit = 0.1
        labels = [
            f"FACTURE CARTE DU {number % 28 + 1:02d}0626 COMMERCE EXEMPLE {number:04d} CARTE 4974XXXXXXXX1111"
            for number in range(50)
        ]
        rules = seeded()
        stop = threading.Event()
        rounds = [0, 0]

        def busy(index):
            while not stop.is_set():
                total = 0
                for number in range(200):
                    total += number
                rounds[index] += 1

        threads = [threading.Thread(target=busy, args=(index,), daemon=True) for index in range(2)]
        with mock.patch.object(recognition, "RULE_SECONDS", limit):
            for thread in threads:
                thread.start()
            try:
                started = time.monotonic()
                read = [described(rules, label)[0] for label in labels]
                waited = time.monotonic() - started
            finally:
                stop.set()
                for thread in threads:
                    thread.join()
        # They did compete: the clock passed the limit while the rule read.
        self.assertTrue(all(rounds))
        self.assertGreater(waited, limit)
        self.assertEqual(read, [CARD] * len(labels))
        self.assertLess(rules.spent[CARD_RULE], limit)
        self.assertEqual((rules.slow, rules.refusal), ([], ""))

    def test_a_statement_meeting_a_slow_rule_is_refused_after_its_last_row(self):
        rows = (
            card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06"),
            debit_row(date(2026, 7, 9), "U.B.A.", "120,35"),
        )
        with too_slow(SEED.RULES[1][4]), self.assertRaises(ValueError) as refused:
            parse_statement(statement(*rows), seeded(), SEEDED_FORMAT)
        self.assertIn(f"« {DEBIT_RULE} » ne peut pas être appliquée", str(refused.exception))


# -- The seeded rules are the old code ------------------------------------------------------------------------------
#
# The historical reference: what bank/statements.py and bank/income.py
# recognised before migration 0006, copied as it was. The seeded rules must
# read every label of the corpus below exactly as this did.

OLD_CARD_RE = re.compile(r"FACTURE CARTE DU (\d{2})(\d{2})(\d{2}) (.*?)\s+CARTE\s+\d{4}X+\d{4}")
OLD_DEBIT_RE = re.compile(r"^PRLV SEPA (?:B2B )?(.*?) ECH/")
OLD_TRANSFER_OUT_RE = re.compile(r"/BEN (.*?) /REFDO")
OLD_TRANSFER_IN_RE = re.compile(r"/FRM (.*?) /")
OLD_PAYOUT_RE = re.compile(r"TOTAL\s+ENCAISS[EÉ]\s+(\d{1,3}(?:\s\d{3})+|\d+)(?:[.,](\d+))?\s+EUROS?\b", re.IGNORECASE)
OLD_CASH_TYPE = "versement especes"
OLD_CHEQUE_TYPE = "remise cheque"


def old_describe(label: str) -> tuple:
    card = OLD_CARD_RE.search(label)
    if card:
        day, month, year, merchant = card.groups()
        try:
            card_date = date(2000 + int(year), int(month), int(day))
        except ValueError:
            card_date = None
        return "CARD", merchant.strip(), card_date
    debit = OLD_DEBIT_RE.search(label)
    if debit:
        return "DEBIT", debit.group(1).strip(), None
    if label.startswith("VIR"):
        found = OLD_TRANSFER_OUT_RE.search(label) or OLD_TRANSFER_IN_RE.search(label)
        return "TRANSFER", found.group(1).strip() if found else "", None
    return "OTHER", "", None


def old_payout_gross(label):
    found = OLD_PAYOUT_RE.search(label or "")
    if found is None:
        return None
    whole, decimals = found.groups()
    try:
        return Decimal(f"{''.join(whole.split())}.{decimals or '0'}")
    except InvalidOperation:  # pragma: no cover - the pattern holds digits only
        return None


def old_source(label, bank_type) -> tuple:
    """(source, gross) as `income.automatic_source` and `entry_for` read a
    credit nobody said anything about."""
    gross = old_payout_gross(label)
    if gross is not None:
        return "card", gross
    kind = search_key(bank_type or "")
    if OLD_CASH_TYPE in kind:
        return "cash", None
    if OLD_CHEQUE_TYPE in kind:
        return "cheque", None
    return "other", None


#: Labels as the import keeps them (spaces collapsed).
KIND_CORPUS = [
    CARD_LABEL,
    "FACTURE CARTE DU 150726 FRANPRIX 5333 PARIS CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 311226 BOULANGERIE EXEMPLE CARTE 4974XXXXXXXX1111",
    "PAIEMENT FACTURE CARTE DU 010726 MAGASIN EXEMPLE CARTE 4974XXXXXXXX2222",
    "FACTURE CARTE DU 010726 CAFÉ DE LA GARE CARTE 4974XXXXXXXX1111",
    # Impossible card dates: still card payments.
    "FACTURE CARTE DU 320726 WING SENG PARIS CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 310426 X CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 290227 X CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 290228 X CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 000726 X CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 011326 X CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 010799 X CARTE 4974XXXXXXXX1111",
    # Not quite a card payment.
    "FACTURE CARTE DU 010726 CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 01072 X CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 010726 X CARTE 497XXXXXXXX1111",
    "FACTURE CARTE DU 010726 X 4974XXXXXXXX1111",
    "COTISATION CARTE 4974XXXXXXXX1111",
    # Direct debits.
    DEBIT_LABEL,
    "PRLV SEPA B2B DGFIP IMPOT 0750750 ECH/250826 ID EMETTEUR/FR00ZZZ000000 MDT/NN000000 REF/000000 LIB/TLR SEPA",
    "PRLV SEPA U.B.A. ECH/090726 ID EMETTEUR/FR00ZZZ000000 REF/0000",
    "PRLV SEPA ASSURANCE EXEMPLE ECH/010726 ECH/020726",
    "PRLV SEPA FOURNISSEUR SANS ECHEANCE REF 000123",
    "REJET PRLV SEPA FOURNISSEUR EXEMPLE ECH/010726",
    "PRLV SEPA B2B ECH/010726",
    "PRLV SEPA CAFÉ EXEMPLE ECH/010726",
    # Transfers.
    "VIR SEPA INST EMIS /MOTIF FACTURE EN RETARD /BEN SCEA EXEMPLE ET FILS /REFDO 0000000000000000 /REF NOTPROVIDED",
    "VIR SEPA RECU /FRM AU COMPTOIR /EID /RNF TRANSFERT 1000001 TOTAL ENCAISSE 1127.5 EUROS",
    "VIR INST RECU /FRM CLIENT EXEMPLE /REFDO REF",
    "VIR SEPA EMIS /FRM BAR EXEMPLE /BEN FOURNISSEUR EXEMPLE /REFDO 0001",
    "VIR SEPA EMIS /BEN FOURNISSEUR EXEMPLE /REF 0001",
    "VIR SEPA RECU /FRM CLIENT EXEMPLE",
    "VIR SEPA RECU /FRM CLIENT/EXEMPLE /REF",
    "VIR PERMANENT LOYER JUILLET",
    "VIREMENT INTERNE EXEMPLE",
    "VIR",
    "RETOUR /BEN FOURNISSEUR EXEMPLE /REFDO 0001",
    "AVOIR /FRM CLIENT EXEMPLE /",
    "VIR FACTURE CARTE DU 010726 X CARTE 4974XXXXXXXX1111",
    "VIR SEPA RECU /FRM TERMINAL EXEMPLE REMISE CARTES",
    # Anything else.
    "ECHEANCE PRET 00000 00000000",
    "FRAIS TENUE DE COMPTE N° 000123 DU 05/06/26",
    "COMMISSION INTERVENTION",
    "REMISE CHEQUES 0000123",
    "VERSEMENT ESPECES GUICHET",
    "PRÉLÈVEMENT EXEMPLE",
    "",
]

#: (label, bank type) of credits.
TILL_CORPUS = [
    (PAYOUT_LABEL, "VIREMENT"),
    (PAYOUT_LABEL.replace("987.65", "120.0"), "VIREMENT"),
    (PAYOUT_LABEL.replace("987.65", "300"), "VIREMENT"),
    (PAYOUT_LABEL.replace("987.65", "12,50"), "VIREMENT"),
    (PAYOUT_LABEL.replace("987.65", "0.00"), "VIREMENT"),
    ("TOTAL ENCAISSE 1 234.50 EURO", ""),
    ("TOTAL ENCAISSE 1 234 567.89 EUROS", ""),
    ("TOTAL  ENCAISSE   45.00   EUROS", ""),
    ("TOTAL ENCAISSÉ 45.00 EUROS", ""),
    ("total encaissé 45,00 euros", ""),
    ("Total Encaisse 45 Euros", ""),
    # No payout: no number, one the pattern cannot read whole, no currency.
    ("TOTAL ENCAISSE 1,234.50 EUROS", ""),
    ("TOTAL ENCAISSE 1.234,50 EUROS", ""),
    ("TOTAL ENCAISSE 12 34.50 EUROS", ""),
    ("TOTAL ENCAISSE 45 . 00 EUROS", ""),
    ("VIR SEPA RECU TOTAL ENCAISSE EUROS", ""),
    ("TOTAL ENCAISSE 45.00 EUROSX", ""),
    ("TOTAL ENCAISSE 45.00", ""),
    ("ENCAISSE 45.00 EUROS", ""),
    # Deposits, by the operation type.
    ("VERSEMENT ESPECES", "VERSEMENT ESPECES"),
    ("DEPOT", "Versement espèces"),
    ("DEPOT", "VERSEMENT ESPÈCES"),
    ("DEPOT", "versement especes guichet"),
    ("REMISE CHEQUES", "REMISE CHEQUES"),
    ("DEPOT", "REMISE CHÈQUE"),
    ("DEPOT", "Remise chèques"),
    ("DEPOT", "REMISE CHQ"),
    ("DEPOT", "VERSEMENT"),
    ("DEPOT", "ESPECES VERSEMENT"),
    ("DEPOT", "VERSEMENT  ESPECES"),
    ("DEPOT", ""),
    # The type is what is read, never the label.
    ("VERSEMENT ESPECES", "VIREMENT"),
    ("REMISE CHEQUE", "VIREMENT"),
    # A payout comes first, whatever its type.
    (PAYOUT_LABEL, "VERSEMENT ESPECES"),
    # What the other tests rely on staying unrecognised.
    ("VIR SEPA RECU /FRM TERMINAL EXEMPLE REMISE CARTES", "VIREMENT"),
    ("VIR SEPA RECU /FRM EMETTEUR TITRES EXEMPLE REMBOURSEMENT", "VIREMENT"),
    ("VIR SEPA RECU /FRM TERMINAL EXEMPLE REMISE", "VIREMENT"),
    ("VIR SEPA RECU /FRM CLIENT EXEMPLE /RNF VERSEMENT 0000002", "VIREMENT"),
    ("VERSEMENT 0042", "VIREMENT"),
    ("REMISE", "VIREMENT"),
    ("OPERATION REMBOURSEMENT TERMINAL EXEMPLE", "VIREMENT"),
]


class OracleTests(SimpleTestCase):
    def test_the_seeded_rules_say_what_each_operation_is_as_the_old_code_did(self):
        for label in KIND_CORPUS:
            with self.subTest(label=label):
                found = recognition.describe(SEEDED, label, "", BOOKED)
                self.assertEqual(found.stored, old_describe(label))

    def test_the_seeded_rules_say_what_each_credit_is_as_the_old_code_did(self):
        for label, bank_type in TILL_CORPUS:
            with self.subTest(label=label, bank_type=bank_type):
                reading = recognition.till_reading(SEEDED, label, bank_type)
                now = (reading.source, reading.gross) if reading is not None else ("other", None)
                self.assertEqual(now, old_source(label, bank_type))
                # And so does the page's whole reading of the credit.
                entry = income.entry_for(unsaved(label, "1.00", bank_type), None, SEEDED)
                was_source, was_gross = old_source(label, bank_type)
                self.assertEqual((entry.source, entry.gross, entry.gross_from_amount), (was_source, was_gross, False))

    def test_the_corpus_holds_every_kind_and_every_source(self):
        """A corpus that missed a branch would prove nothing about it."""
        self.assertEqual({old_describe(label)[0] for label in KIND_CORPUS}, {CARD, DEBIT, TRANSFER, OTHER})
        self.assertIn(None, {old_describe(label)[2] for label in KIND_CORPUS if old_describe(label)[0] == CARD})
        self.assertIn("", {old_describe(label)[1] for label in KIND_CORPUS if old_describe(label)[0] == TRANSFER})
        self.assertEqual({old_source(*credit)[0] for credit in TILL_CORPUS}, {"card", "cash", "cheque", "other"})

    def test_what_reads_otherwise_now_on_purpose(self):
        """The few labels the rules read otherwise than the old code - none
        a statement of the owner's bank prints. Each is the safer reading."""
        # A gross with three decimals, or wider than an amount, is read
        # whole or not at all: no payout rather than a doubtful gross.
        for gross in ("12.345", "12345678901.00"):
            with self.subTest(gross=gross):
                label = f"TOTAL ENCAISSE {gross} EUROS"
                self.assertEqual(old_source(label, "")[0], "card")
                self.assertIsNone(recognition.till_reading(SEEDED, label, ""))
        # Case never matters to a rule: the old card and transfer tests
        # were case-sensitive.
        lower_card = "facture carte du 010726 wing seng carte 4974xxxxxxxx1111"
        self.assertEqual(old_describe(lower_card), (OTHER, "", None))
        self.assertEqual(recognition.describe(SEEDED, lower_card).stored, (CARD, "wing seng", date(2026, 7, 1)))
        self.assertEqual(old_describe("Vir permanent loyer"), (OTHER, "", None))
        self.assertEqual(recognition.describe(SEEDED, "Vir permanent loyer").stored, (TRANSFER, "", None))
        # A payee is cut to its column (255), where it was stored whole.
        long_payee = "VIR SEPA RECU /FRM " + "X" * 300 + " /REF"
        self.assertEqual(len(old_describe(long_payee)[1]), 300)
        self.assertEqual(len(recognition.describe(SEEDED, long_payee).counterparty), 255)


# -- The database ---------------------------------------------------------------------------------------------------


class NoBankWordsTests(SimpleTestCase):
    def test_the_models_say_a_credit_is_read_by_the_till_rules_not_by_one_bank_s_words(self):
        """What a credit nobody chose is decided by the till rules a person
        edits: the models no longer describe the words the code once read
        as the mechanism (review, 01/10/2026)."""
        for model in (IncomeSource, BankTransaction, IncomePayer):
            source = inspect.getsource(model)
            for words in ("TOTAL ENCAISSE", "VERSEMENT ESPECES", "REMISE CHEQUE", "deposit type"):
                with self.subTest(model=model.__name__, words=words):
                    self.assertNotIn(words, source)
        self.assertIn("Reconnaissance des opérations", " ".join(IncomeSource.__doc__.split()))


class SeedTests(TestCase):
    """Pinned with literals, like returnables/tests/test_models.py: a slip in
    the migration shows here rather than being copied into it."""

    def test_the_owners_bank_is_seeded_as_eight_active_rules(self):
        self.assertEqual(
            list(
                OperationRule.objects.order_by("position").values_list(
                    "position", "name", "meaning", "searched", "pattern", "is_active"
                )
            ),
            [
                (
                    1,
                    "Paiement par carte (FACTURE CARTE)",
                    "card_payment",
                    "label",
                    (
                        r"FACTURE CARTE DU (?P<jour>[0-9]{2})(?P<mois>[0-9]{2})(?P<annee>[0-9]{2}) (?P<tiers>.*?)"
                        r"\s+CARTE\s+[0-9]{4}X+[0-9]{4}"
                    ),
                    True,
                ),
                (2, "Prélèvement (PRLV SEPA)", "debit", "label", r"^PRLV SEPA (?:B2B )?(?P<tiers>.*?) ECH/", True),
                (3, "Virement émis (/BEN)", "transfer", "label", r"^VIR.*?/BEN (?P<tiers>.*?) /REFDO", True),
                (4, "Virement reçu (/FRM)", "transfer", "label", r"^VIR.*?/FRM (?P<tiers>.*?) /", True),
                (5, "Autre virement (VIR)", "transfer", "label", r"^VIR", True),
                (
                    6,
                    "Versement carte (TOTAL ENCAISSE)",
                    "payout",
                    "label",
                    r"TOTAL\s+ENCAISS[EÉ]\s+(?P<encaisse>(?:[0-9]{1,3}(?:\s[0-9]{3})+|[0-9]+)(?:[.,][0-9]+)?)\s+EUROS?\b",
                    True,
                ),
                (7, "Dépôt d'espèces (VERSEMENT ESPECES)", "cash", "bank_type", r"VERSEMENT ESPECES", True),
                (8, "Remise de chèques (REMISE CHEQUE)", "cheque", "bank_type", r"REMISE CHEQUE", True),
            ],
        )

    def test_every_seeded_rule_passes_the_check_and_is_loaded(self):
        for seeded_rule in OperationRule.objects.all():
            with self.subTest(rule=seeded_rule.name):
                seeded_rule.full_clean()
        with self.assertNumQueries(1):
            rules = recognition.load()
        self.assertEqual(rules.invalid, {})
        self.assertEqual(
            [one.name for one in rules.kinds], [CARD_RULE, DEBIT_RULE, SENT_RULE, RECEIVED_RULE, ANY_TRANSFER_RULE]
        )
        self.assertEqual([one.name for one in rules.till], [PAYOUT_RULE, CASH_RULE, CHEQUE_RULE])

    def test_the_tests_pure_copy_is_the_databases(self):
        """`support.SEEDED` (the pure tests' rules) is what every database
        holds."""
        rules = recognition.load()
        for mine, stored in ((SEEDED.kinds, rules.kinds), (SEEDED.till, rules.till)):
            self.assertEqual(
                [(one.name, one.meaning, one.searched, one.pattern.pattern) for one in mine],
                [(one.name, one.meaning, one.searched, one.pattern.pattern) for one in stored],
            )
        self.assertEqual(
            SEEDED_NAMES,
            (CARD_RULE, DEBIT_RULE, SENT_RULE, RECEIVED_RULE, ANY_TRANSFER_RULE, PAYOUT_RULE, CASH_RULE, CHEQUE_RULE),
        )

    def test_the_migration_follows_0005_and_reverses_to_nothing(self):
        migration = SEED.Migration
        self.assertEqual(migration.dependencies, [("bank", "0005_income_sources")])
        create, seed = migration.operations
        self.assertIsInstance(create, migrations.CreateModel)
        self.assertEqual(create.name, "OperationRule")
        self.assertIs(seed.code, SEED.seed)
        self.assertIs(seed.reverse_code, migrations.RunPython.noop)

    def test_seeding_again_leaves_what_exists_as_it_is(self):
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern=r"^PRLV (?P<tiers>.+)$", position=20)
        OperationRule.objects.filter(name=CASH_RULE).update(is_active=False)
        SEED.seed(apps, None)
        self.assertEqual(OperationRule.objects.count(), 8)
        self.assertEqual(
            OperationRule.objects.filter(name=DEBIT_RULE).values_list("pattern", "position").get(),
            (r"^PRLV (?P<tiers>.+)$", 20),
        )
        self.assertFalse(OperationRule.objects.get(name=CASH_RULE).is_active)

    def test_load_reads_the_active_rules_in_their_order(self):
        """By position, not by when they were made; a rule turned off is
        not read."""
        pause_seeded_rules()
        make_rule("Second", "transfer", r"^VIREMENT", position=2)
        make_rule("Premier", "debit", r"^VIREMENT DE", position=1)
        make_rule("Éteint", "debit", r"^VIREMENT", position=0, is_active=False)
        with self.assertNumQueries(1):
            rules = recognition.load()
        self.assertEqual([one.name for one in rules.kinds], ["Premier", "Second"])
        self.assertEqual(described(rules, "VIREMENT DE X")[0], DEBIT)


class TenantTests(TwoTenantsTestCase):
    """Real espaces (accounts.provisioning): each is copied from the
    migrated _template."""

    def test_every_new_espace_is_given_the_eight_rules(self):
        for bar in (self.bar_a, self.bar_b):
            with self.subTest(bar=bar.name), bound_tenant(bar):
                self.assertEqual(
                    list(OperationRule.objects.order_by("position").values_list("name", "is_active")),
                    [(name, True) for name in SEEDED_NAMES],
                )
                self.assertEqual(recognition.load().invalid, {})


class InvalidStoredRuleTests(StatementFixtures, TestCase):
    """A rule written past the form (a hand edit, an old archive): never
    trusted, never applied."""

    def setUp(self):
        super().setUp()
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern="PRLV (")
        self.rows = (
            card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06"),
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,35"),
        )
        self.refusal = (
            f"Import annulé : la règle de reconnaissance « {DEBIT_RULE} » ne peut pas être appliquée (motif "
            "invalide ou trop lent). Corrigez-la sur « Reconnaissance des opérations », puis importez à nouveau."
        )

    def test_it_recognises_nothing_and_is_said(self):
        rules = recognition.load()
        self.assertEqual(rules.invalid, {DEBIT_RULE: "Motif : parenthèse non fermée (position 6)."})
        self.assertEqual(described(rules, DEBIT_LABEL), (OTHER, "", None, ""))
        self.assertEqual(
            rules.problems, [f"Règle « {DEBIT_RULE} » : parenthèse non fermée (position 6) - elle ne reconnaît rien."]
        )

    def test_an_import_is_refused_with_nothing_written(self):
        with self.assertRaises(ValueError) as refused:
            self.load(*self.rows)
        self.assertEqual(str(refused.exception), self.refusal)
        self.assertFalse(BankTransaction.objects.exists())

    def test_the_page_says_why_and_imports_nothing(self):
        upload = SimpleUploadedFile("releve-juillet.csv", statement(*self.rows))
        response = self.client.post(reverse("bank:bank_home"), {"files": [upload]}, follow=True)
        self.assertEqual(response.status_code, 200)
        said = [str(message) for message in response.context["messages"]]
        self.assertEqual(said, [f"releve-juillet.csv : {self.refusal}"])
        self.assertFalse(BankTransaction.objects.exists())

    def test_entrees_d_argent_says_it(self):
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern=r"^PRLV SEPA (?P<tiers>.*?) ECH/")
        OperationRule.objects.filter(name=CASH_RULE).update(pattern="ESPECES|")
        deposit = BankTransaction.objects.create(
            operation_date=date(2026, 6, 10),
            bank_type="VERSEMENT ESPECES",
            label="VERSEMENT ESPECES 0001",
            amount=Decimal("40.00"),
            fingerprint="depot-1",
        )
        report = income.income_for(DateRange())
        self.assertEqual(
            report.rule_problems,
            [
                (
                    f"Règle « {CASH_RULE} » : le motif accepte une ligne vide : il trouverait quelque chose sur "
                    "n'importe quelle ligne - elle ne reconnaît rien."
                )
            ],
        )
        # Unrecognised, in sight under « Autres entrées » - not a deposit.
        self.assertEqual([(entry.line.pk, entry.how_label) for entry in report.others], [(deposit.pk, "non reconnue")])


class SlowStoredRuleTests(StatementFixtures, TestCase):
    def test_a_file_meeting_a_slow_rule_on_its_last_row_leaves_none_of_its_lines(self):
        rows = (
            card_row(date(2026, 7, 15), "FRANPRIX 5333 PARIS", "13,06"),
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,35"),
        )
        upload = SimpleUploadedFile("releve.csv", statement(*rows))
        with too_slow(SEED.RULES[1][4]):
            response = self.client.post(reverse("bank:bank_home"), {"files": [upload]}, follow=True)
        said = [str(message) for message in response.context["messages"]]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith(f"releve.csv : Import annulé : la règle de reconnaissance « {DEBIT_RULE} »"))
        self.assertFalse(BankTransaction.objects.exists())
        # The next import reads afresh, and goes through.
        self.assertEqual(self.load(*rows).created, 2)

    def test_entrees_d_argent_says_a_slow_rule_and_costs_the_same(self):
        BankTransaction.objects.create(
            operation_date=date(2026, 6, 10),
            bank_type="VIREMENT",
            label=PAYOUT_LABEL,
            amount=Decimal("980.00"),
            fingerprint="versement-1",
        )
        with too_slow(SEED.RULES[5][4]), self.assertNumQueries(income.QUERIES):
            report = income.income_for(DateRange())
        self.assertEqual(report.rule_problems, [f"Règle « {PAYOUT_RULE} » : motif trop lent, ignoré - simplifiez-le."])
        self.assertEqual(report.payouts, [])

    def test_with_the_seeded_rules_there_is_nothing_to_say(self):
        self.assertEqual(income.income_for(DateRange()).rule_problems, [])


class NotASaleRuleTests(IncomeFixtures, TestCase):
    """A rule saying « Pas une vente » recognises its line: a payer never
    reaches it, choosing for the payer from it keeps the choice on the line,
    and « Oublier » never counted it."""

    def setUp(self):
        super().setUp()
        make_rule("Indemnité d'assurance", "not_a_sale", r"INDEMNITE SINISTRE")
        self.indemnity = self.credit(
            date(2026, 6, 3),
            "500.00",
            "VIR SEPA RECU /FRM ASSUREUR EXEMPLE INDEMNITE SINISTRE",
            counterparty="ASSUREUR",
        )
        self.refund = self.credit(
            date(2026, 6, 4), "40.00", "VIR SEPA RECU /FRM ASSUREUR EXEMPLE REMBOURSEMENT", counterparty="ASSUREUR"
        )
        # The same payer's other indemnity, with no choice of its own: the
        # rule recognises it, so no payer reaches it - and only it tells the
        # « a rule matched » reading from the old « its source is not OTHER »
        # in the counts below (review, 01/10/2026).
        self.second_indemnity = self.credit(
            date(2026, 6, 5),
            "300.00",
            "VIR SEPA RECU /FRM ASSUREUR EXEMPLE INDEMNITE SINISTRE 2",
            counterparty="ASSUREUR",
        )

    def stored(self, line):
        return BankTransaction.objects.get(pk=line.pk)

    def assertReadByItsRule(self, report, line):
        entry = {one.line.pk: one for one in report.others}[line.pk]
        self.assertEqual((entry.how, entry.how_label), (income.BY_RULE, "règle « Indemnité d'assurance »"))

    def test_a_payer_retained_from_it_keeps_the_choice_on_that_line(self):
        change = income.set_source(self.indemnity, income.CARD, remember=True)
        self.assertEqual(list(IncomePayer.objects.values_list("key", "source")), [("ASSUREUR", income.CARD)])
        self.assertEqual(self.stored(self.indemnity).income_source, income.CARD)
        # The refund follows; the second indemnity is its rule's, neither
        # following nor keeping a choice.
        self.assertEqual((change.entry.how, change.followers, change.kept), (income.BY_LINE, 1, 0))
        report = income.income_for(DateRange())
        self.assertEqual(
            [(row.entry.line.pk, row.entry.how) for row in report.payouts],
            [(self.indemnity.pk, income.BY_LINE), (self.refund.pk, income.BY_PAYER)],
        )
        self.assertReadByItsRule(report, self.second_indemnity)
        # « Oublier » sends back the refund only: neither indemnity followed.
        self.assertEqual(income.forget_payer(IncomePayer.objects.get()), 1)

    def test_saying_what_its_rule_says_hands_it_back_to_the_rule(self):
        change = income.set_source(self.indemnity, income.OTHER, remember=True)
        self.assertEqual(self.stored(self.indemnity).income_source, "")
        self.assertEqual((change.followers, change.kept), (1, 0))
        report = income.income_for(DateRange())
        self.assertReadByItsRule(report, self.indemnity)
        self.assertReadByItsRule(report, self.second_indemnity)
        # The refund follows the payer « Pas une vente » - the rule had no part in it.
        self.assertEqual({one.line.pk: one.how for one in report.others}[self.refund.pk], income.BY_PAYER)
        self.assertEqual(income.forget_payer(IncomePayer.objects.get()), 1)


class AnotherBankTests(StatementFixtures, TestCase):
    """An espace at another bank: the seeded rules turned off, its own
    written - one reading the operation type, a card date printed without
    its year, and a terminal whose payouts print no gross."""

    HEADER = '"Compte courant";"Compte courant";****0077;14/09/2026;;1 000,00\n'
    #: Only the type column says it is a direct debit.
    TYPED_DEBIT = "ECHEANCE ASSURANCE EXEMPLE 0001"

    def setUp(self):
        super().setUp()
        pause_seeded_rules()
        make_rule("Carte", "card_payment", r"^CB (?P<tiers>.+?) (?P<jour>[0-9]{2})/(?P<mois>[0-9]{2})$")
        make_rule("Prélèvement", "debit", r"^PRELEVEMENT EUROPEEN (?P<tiers>.+)$")
        make_rule("Virement reçu", "transfer", r"^VIREMENT DE (?P<tiers>.+)$")
        make_rule("TPE sans montant", "payout", r"^REMISE TPE\b")
        make_rule("Espèces", "cash", r"^VERSEMENT$", "bank_type")
        make_rule("Prélèvement par type", "debit", r"^PRELEVEMENT$", "bank_type")
        rows = [
            "02/01/2027;CARTE;CB;CB BOULANGERIE   EXEMPLE 31/12;02/01/2027;-4,20",
            "14/06/2026;CARTE;CB;CB EPICERIE EXEMPLE 15/06;14/06/2026;-9,80",
            "05/06/2026;PRELEVEMENT;PRLV;PRELEVEMENT EUROPEEN FOURNISSEUR EXEMPLE;05/06/2026;-120,00",
            f"06/06/2026;PRELEVEMENT;PRLV;{self.TYPED_DEBIT};06/06/2026;-35,00",
            "08/06/2026;VIREMENT RECU;VIR;VIREMENT DE ASSOCIATION EXEMPLE;08/06/2026;300,00",
            "09/06/2026;VIREMENT RECU;VIR;REMISE TPE 0042 COMMERCANT EXEMPLE;09/06/2026;245,50",
            "10/06/2026;VERSEMENT;DEP;DEPOT AGENCE EXEMPLE;10/06/2026;150,00",
            "11/06/2026;PAIEMENT CB;FACTURE CARTE;FACTURE CARTE DU 100626 X CARTE 4974XXXXXXXX1111;11/06/2026;-5,00",
        ]
        content = (self.HEADER + "".join(row + "\n" for row in rows)).encode()
        self.summary = reconcile.import_statement(content)

    def card_dates(self):
        return list(
            BankTransaction.objects.filter(kind=CARD).order_by("operation_date").values_list("card_date", flat=True)
        )

    def test_its_statement_is_read_by_its_rules(self):
        self.assertEqual(self.summary.created, 8)
        self.assertEqual(
            list(
                BankTransaction.objects.order_by("operation_date", "pk").values_list(
                    "label", "kind", "counterparty", "card_date"
                )
            ),
            [
                # The label rule comes first; the type says « PRELEVEMENT » too.
                ("PRELEVEMENT EUROPEEN FOURNISSEUR EXEMPLE", DEBIT, "FOURNISSEUR EXEMPLE", None),
                # Only the type says it: the rule reading « Type d'opération ».
                (self.TYPED_DEBIT, DEBIT, "", None),
                ("VIREMENT DE ASSOCIATION EXEMPLE", TRANSFER, "ASSOCIATION EXEMPLE", None),
                ("REMISE TPE 0042 COMMERCANT EXEMPLE", OTHER, "", None),
                ("DEPOT AGENCE EXEMPLE", OTHER, "", None),
                # The owner's bank's wording means nothing here any more.
                ("FACTURE CARTE DU 100626 X CARTE 4974XXXXXXXX1111", OTHER, "", None),
                # The year a card date does not print: the latest that is
                # not after the booking.
                ("CB EPICERIE EXEMPLE 15/06", CARD, "EPICERIE EXEMPLE", date(2025, 6, 15)),
                ("CB BOULANGERIE EXEMPLE 31/12", CARD, "BOULANGERIE EXEMPLE", date(2026, 12, 31)),
            ],
        )
        self.assertEqual(BankTransaction.objects.values_list("account", flat=True).distinct().get(), "****0077")

    def test_its_terminal_s_payout_printing_no_gross_is_a_card_payout(self):
        report = income.income_for(DateRange())
        (payout,) = report.payouts
        entry = payout.entry
        self.assertEqual(entry.line.label, "REMISE TPE 0042 COMMERCANT EXEMPLE")
        self.assertEqual(
            (entry.source, entry.how, entry.rule, entry.gross, entry.gross_from_amount, entry.commission),
            (income.CARD, income.BY_RULE, "TPE sans montant", Decimal("245.50"), True, None),
        )
        self.assertEqual((report.card_from_amount, report.card_commission), (1, None))
        self.assertEqual(report.card_gross, Decimal("245.50"))
        self.assertEqual(
            [(entry.line.label, entry.source, entry.rule) for entry in report.other_means],
            [("DEPOT AGENCE EXEMPLE", income.CASH, "Espèces")],
        )
        self.assertEqual([entry.line.label for entry in report.others], ["VIREMENT DE ASSOCIATION EXEMPLE"])
        self.assertEqual(report.rule_problems, [])

    def test_a_rule_reading_the_operation_type_reads_the_stored_lines_as_it_read_the_import(self):
        """The type column reaches « Relire » and the rules page as it reached
        the import: right after it, nothing reads otherwise (review,
        01/10/2026: dropped from either, every debit only the type names
        would be offered back to « Autre »)."""
        self.assertEqual(BankTransaction.objects.get(label=self.TYPED_DEBIT).kind, DEBIT)
        self.assertEqual(recognition.stored_changes().changes, [])
        self.assertEqual(self.client.get(reverse("bank:recognition")).context["pending"], 0)

    def test_a_card_date_printed_without_its_year_reads_the_stored_lines_as_it_read_the_import(self):
        """The booking day reaches « Relire » and the rules page as it reached
        the import, so the year a card date does not print is inferred the
        same: nothing reads otherwise, and « Relire » run writes nothing and
        keeps every card date (review, 01/10/2026: without it, every card
        line lost its date - and its receipts' window)."""
        self.assertEqual(self.card_dates(), [date(2025, 6, 15), date(2026, 12, 31)])
        pending = recognition.stored_changes()
        self.assertEqual(pending.changes, [])
        self.assertEqual(self.client.get(reverse("bank:recognition")).context["pending"], 0)
        self.assertEqual(recognition.apply_changes(pending.digest), 0)
        self.assertEqual(self.card_dates(), [date(2025, 6, 15), date(2026, 12, 31)])


class StoredChangesTests(StatementFixtures, TestCase):
    """« Réappliquer »: what the rules now read otherwise on the lines
    already imported, shown, then written - only that, and only as shown."""

    def setUp(self):
        super().setUp()
        loan = "10/07/2026;VIREMENT EMIS;VIR;VIR PERMANENT LOYER EXEMPLE;10/07/2026;-700,00"
        credit = "11/07/2026;VIREMENT;VIR RECU;VIR SEPA RECU /FRM CLIENT EXEMPLE /RNF 0001;11/07/2026;50,00"
        self.load(
            card_row(date(2026, 7, 15), "MONOPRIX PARIS", "7,76"),
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,35"),
            debit_row(date(2026, 7, 12), "U.B.A.", "80,00"),
            loan,
            credit,
        )
        line = BankTransaction.objects.get
        self.card = line(kind=CARD)
        self.metro = line(counterparty="METRO FRANCE")
        self.uba = line(counterparty="U.B.A.")
        self.loan = line(label__startswith="VIR PERMANENT")
        self.credit = line(amount__gt=0)
        # What a person did on the lines, none of which a re-reading touches.
        reconcile.link(self.card, [self.invoice("MONOPRIX", date(2026, 7, 15), "7.36", rate=Decimal("0.055"))])
        BankTransaction.objects.filter(pk=self.metro.pk).update(category="Achats", no_invoice=True)
        BankTransaction.objects.filter(pk=self.credit.pk).update(income_source="cash", category="Privatisation")

    def edit_the_rules(self):
        """The debit's payee cut to its first word; the card date no longer
        read; « VIR » alone no longer a transfer."""
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern=r"^PRLV SEPA (?:B2B )?(?P<tiers>[^ ]+)")
        OperationRule.objects.filter(name=CARD_RULE).update(pattern=r"FACTURE CARTE DU [0-9]{6} (?P<tiers>.*?)\s+CARTE")
        OperationRule.objects.filter(name=ANY_TRANSFER_RULE).update(is_active=False)

    def readings(self):
        return list(BankTransaction.objects.order_by("pk").values_list("pk", "kind", "counterparty", "card_date"))

    def everything_else(self):
        """Every column but the three a rule reads, the links and the aliases."""
        kept = [
            field.attname
            for field in BankTransaction._meta.concrete_fields
            if field.name not in ("kind", "counterparty", "card_date")
        ]
        return (
            list(BankTransaction.objects.order_by("pk").values(*kept)),
            list(InvoicePayment.objects.order_by("pk").values()),
            list(CounterpartyAlias.objects.order_by("pk").values()),
        )

    def test_right_after_an_import_nothing_reads_otherwise(self):
        with self.assertNumQueries(2):
            pending = recognition.stored_changes()
        self.assertEqual(pending.changes, [])
        self.assertEqual(recognition.apply_changes(pending.digest), 0)

    def test_what_reads_otherwise_is_shown_then_written_and_nothing_else(self):
        self.edit_the_rules()
        rules = recognition.load()
        with self.assertNumQueries(1):
            pending = recognition.stored_changes(rules)
        # Oldest first; U.B.A.'s first word is its whole payee: unchanged, not listed.
        self.assertEqual(
            [
                (one.line.pk, one.now.stored, one.kind_changes, one.payee_changes, one.date_changes)
                for one in pending.changes
            ],
            [
                (self.metro.pk, (DEBIT, "METRO", None), False, True, False),
                (self.loan.pk, (OTHER, "", None), True, False, False),
                (self.card.pk, (CARD, "MONOPRIX PARIS", None), False, False, True),
            ],
        )
        before = self.readings()
        untouched = self.everything_else()
        self.assertEqual(recognition.apply_changes(pending.digest), 3)
        after = {pk: rest for pk, *rest in self.readings()}
        self.assertEqual(after[self.metro.pk], [DEBIT, "METRO", None])
        self.assertEqual(after[self.loan.pk], [OTHER, "", None])
        self.assertEqual(after[self.card.pk], [CARD, "MONOPRIX PARIS", None])
        self.assertEqual(after[self.uba.pk], [DEBIT, "U.B.A.", None])
        self.assertNotEqual(self.readings(), before)
        # Links, decisions, categories, « En caisse », aliases: as they were.
        self.assertEqual(self.everything_else(), untouched)
        # And read again, nothing more to change.
        self.assertEqual(recognition.stored_changes().changes, [])

    def test_the_digest_is_what_was_shown(self):
        self.edit_the_rules()
        shown = recognition.stored_changes()
        self.assertEqual(recognition.stored_changes().digest, shown.digest)
        OperationRule.objects.filter(name=ANY_TRANSFER_RULE).update(is_active=True)
        self.assertNotEqual(recognition.stored_changes().digest, shown.digest)

    def test_a_change_made_meanwhile_writes_nothing(self):
        self.edit_the_rules()
        shown = recognition.stored_changes()
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern=r"^PRLV SEPA (?:B2B )?(?P<tiers>[^ ]{2})")
        before = self.readings()
        with self.assertRaises(recognition.ChangedMeanwhile):
            recognition.apply_changes(shown.digest)
        self.assertEqual(self.readings(), before)

    def test_a_line_added_meanwhile_writes_nothing(self):
        """The digest holds the lines as well as the rules: a line arriving
        between the preview and the confirm - a statement imported in another
        tab, a « Données » merge - is one nobody was shown, and its payee
        feeds the aliases, the payers and the matching."""
        self.edit_the_rules()
        shown = recognition.stored_changes()
        added = BankTransaction.objects.create(
            operation_date=date(2026, 7, 20),
            bank_type="PRELEVEMENT",
            kind=DEBIT,
            label="PRLV SEPA AUTRE FOURNISSEUR ECH/200726 ID EMETTEUR/FR00ZZZ000000 REF/0000",
            counterparty="AUTRE FOURNISSEUR",
            amount=Decimal("-5.00"),
            fingerprint="arrivee-entre-temps",
        )
        # The rules as edited read it otherwise (its payee cut to « AUTRE »).
        self.assertIn(added.pk, [one.line.pk for one in recognition.stored_changes().changes])
        before = self.readings()
        with self.assertRaises(recognition.ChangedMeanwhile):
            recognition.apply_changes(shown.digest)
        self.assertEqual(self.readings(), before)

    def test_refused_while_a_rule_cannot_be_applied(self):
        self.edit_the_rules()
        shown = recognition.stored_changes()
        OperationRule.objects.filter(name=CHEQUE_RULE).update(pattern="(")
        before = self.readings()
        with self.assertRaisesMessage(
            ValueError, f"Rien n'a changé : la règle de reconnaissance « {CHEQUE_RULE} » ne peut pas être appliquée"
        ):
            recognition.apply_changes(shown.digest)
        self.assertEqual(self.readings(), before)

    def test_nothing_is_matched_again(self):
        """A debit re-read as METRO is not linked to Metro's invoice of the
        same amount: the automatic pass is not run here."""
        self.invoice("METRO", date(2026, 6, 29), "100.29")
        BankTransaction.objects.filter(pk=self.metro.pk).update(no_invoice=False)
        self.edit_the_rules()
        recognition.apply_changes(recognition.stored_changes().digest)
        self.assertFalse(InvoicePayment.objects.filter(transaction=self.metro).exists())


class QueryTests(TestCase):
    def credit(self, number, label=PAYOUT_LABEL, counterparty="BAR EXEMPLE"):
        return BankTransaction.objects.create(
            operation_date=date(2026, 6, 1) + timedelta(days=number),
            bank_type="VIREMENT",
            label=label.replace("0000001", f"{number:07d}"),
            counterparty=counterparty,
            amount=Decimal("980.00"),
            fingerprint=f"versement-{number}",
        )

    def test_the_income_page_reads_the_rules_once(self):
        """`income.QUERIES` counts the rules (bank/tests/test_income.py holds
        it); here, that they are read once however many credits there are."""
        for number in range(5):
            self.credit(number)
        self.assertEqual(income.QUERIES, 7)
        with self.assertNumQueries(income.QUERIES):
            report = income.income_for(DateRange())
        self.assertEqual(len(report.payouts), 5)

    def test_saying_what_a_credit_is_and_forgetting_its_payer_cost_the_same_with_more_credits(self):
        """The rules are read once, and a credit read against them needs no
        field its query did not fetch."""
        terminal = "VIR SEPA RECU /FRM TERMINAL EXEMPLE REMISE 0000001"
        first = self.credit(0, terminal, "TERMINAL EXEMPLE")
        self.credit(1, terminal, "TERMINAL EXEMPLE")
        self.credit(2)

        def costs():
            with CaptureQueriesContext(connection) as chosen:
                income.set_source(BankTransaction.objects.get(pk=first.pk), income.CARD, remember=True)
            with CaptureQueriesContext(connection) as forgotten:
                income.forget_payer(IncomePayer.objects.get())
            return len(chosen.captured_queries), len(forgotten.captured_queries)

        few = costs()
        for number in range(3, 13):
            self.credit(number, terminal if number % 2 else PAYOUT_LABEL, "TERMINAL EXEMPLE")
        self.assertEqual(costs(), few)
