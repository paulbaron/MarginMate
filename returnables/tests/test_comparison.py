"""Which slips count, what their lines are, and how a day's pickups compare
with them (returnables/comparison.py).

Every slip, date, number and count is invented (returnables/tests/support.py
and texts.py). A slow pattern is simulated by a spent budget, never run.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.safestring import SafeData

from returnables import patterns
from returnables.comparison import (
    DIFFERS,
    INVALID,
    NO_FORMAT,
    NO_SUPPLIER,
    REPLACED,
    RESENT,
    SAME,
    SLOW,
    STATUSES,
    TO_CHECK,
    UNREAD_CHECK,
    WAITING,
    Board,
    Classifier,
    SlipIndex,
    classify,
    compare,
    counts_summary,
    effective_slips,
    euros,
    latest_note,
    slip_label,
)
from returnables.models import Slip
from returnables.patterns import Budget
from returnables.tests import texts
from returnables.tests.support import (
    DELIVERY_DAY,
    KEG_LINE,
    make_format,
    make_pickup,
    make_slip,
    make_supplier,
    make_type,
    no_defaults,
    seeded_format,
    seeded_type,
)
from tests.support import NoNetworkTestCase

D = Decimal
DAY = DELIVERY_DAY
NBSP = "\N{NO-BREAK SPACE}"
CHECK = "\N{CHECK MARK}"
ARROW = "\N{RIGHTWARDS ARROW}"
DOT = "\N{MIDDLE DOT}"


def moment(day: date, hour: int = 8, minute: int = 0):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute))


def line(designation, quantity, unit=None, amount=None):
    return SimpleNamespace(designation=designation, quantity=quantity, unit_amount=unit, amount=amount)


def keg_line(quantity, unit=D("30.00")):  # noqa: B008 - a Decimal is immutable, built once on purpose
    amount = None if unit is None else quantity * unit
    return (texts.KEG, quantity, unit, amount)


# -- Words ------------------------------------------------------------------------------------------------------------


class WordsTests(SimpleTestCase):
    def test_euros_writes_a_comma_and_a_no_break_space(self):
        self.assertEqual(euros(D("30")), f"30,00{NBSP}€")
        self.assertEqual(euros(30), f"30,00{NBSP}€")
        self.assertEqual(euros(D("-12.30")), f"-12,30{NBSP}€")

    def test_euros_groups_the_thousands_by_a_no_break_space(self):
        self.assertEqual(euros(D("999.99")), f"999,99{NBSP}€")
        self.assertEqual(euros(D("1234.5")), f"1{NBSP}234,50{NBSP}€")
        self.assertEqual(euros(10000), f"10{NBSP}000,00{NBSP}€")
        self.assertEqual(euros(D("16568684")), f"16{NBSP}568{NBSP}684,00{NBSP}€")
        self.assertEqual(euros(D("-1234.567")), f"-1{NBSP}234,57{NBSP}€")

    def test_euros_rounds_half_away_from_zero_and_never_prints_minus_zero(self):
        self.assertEqual(euros(D("0.125")), f"0,13{NBSP}€")
        self.assertEqual(euros(D("-0.001")), f"0,00{NBSP}€")
        self.assertEqual(euros(D("-0.00")), f"0,00{NBSP}€")
        self.assertEqual(euros(None), "")

    def test_a_slip_s_label(self):
        self.assertEqual(slip_label("1001"), "bon n° 1001")
        self.assertEqual(slip_label("1001", date(2025, 5, 14), with_date=True), "bon n° 1001 du 14/05/2025")
        self.assertEqual(slip_label("", date(2025, 5, 14)), "bon sans numéro du 14/05/2025")
        self.assertEqual(slip_label("", None, capital=True), "Bon sans numéro")

    def test_counts_read_name_then_number_the_names_as_typed(self):
        types = texts.seed_types() + [SimpleNamespace(pk=9, name="bouteilles MIXtes", position=0, slip_patterns="")]
        self.assertEqual(
            counts_summary({1: 15, 3: 1, 2: 0, 9: 2}, types),
            f"bouteilles MIXtes 2 {DOT} Fûts 15 {DOT} Bouteilles CO2 1",
        )
        self.assertEqual(counts_summary({}, types), "")

    def test_the_statuses_and_their_pills(self):
        self.assertEqual(list(STATUSES), [NO_SUPPLIER, NO_FORMAT, WAITING, TO_CHECK, DIFFERS, SAME])
        self.assertEqual(
            [(status.pill, status.css) for status in STATUSES.values()],
            [
                ("fournisseur non précisé", "ignored"),
                ("pas de format de bon", "ignored"),
                ("en attente du bon", "pending"),
                ("à vérifier", "pending"),
                ("écart", "ERROR"),
                ("conforme", "COMPLETE"),
            ],
        )


# -- Classification ---------------------------------------------------------------------------------------------------


class ClassifyTests(SimpleTestCase):
    def setUp(self):
        patterns._checked.cache_clear()

    def test_every_invented_designation_gets_its_seeded_type(self):
        types = texts.seed_types()
        memo = {}
        for designation, expected in texts.EXPECTED_TYPE.items():
            with self.subTest(designation):
                found = classify(designation, types, Budget(1), memo)
                self.assertEqual(found.name if found else None, expected)

    def test_the_first_type_by_position_wins_and_an_inactive_type_still_classifies(self):
        first = SimpleNamespace(pk=7, name="Gaz", position=0, is_active=False, slip_patterns="CO2")
        found = Classifier(texts.seed_types() + [first]).classify(texts.CO2)
        self.assertEqual(found.returnable_type, first)
        self.assertEqual(found.pattern, "CO2")

    def test_the_memo_classifies_a_designation_once(self):
        classifier = Classifier(texts.seed_types())
        with mock.patch.object(patterns, "search", wraps=patterns.search) as searched:
            classifier.classify(texts.KEG)
            calls = searched.call_count
            classifier.classify(texts.KEG)
        self.assertEqual(searched.call_count, calls)

    def test_an_invalid_pattern_stops_the_search_it_never_files_the_line_under_the_next_type(self):
        broken = SimpleNamespace(pk=8, name="Cassé", position=0, slip_patterns="FÛT\n(")
        classifier = Classifier([broken] + texts.seed_types())
        found = classifier.classify(texts.KEG)
        self.assertIsNone(found.returnable_type)
        self.assertEqual((found.problem, found.type_in_error), (INVALID, broken))
        self.assertEqual(found.note, "non classée : motif invalide")
        self.assertIn(8, classifier.errors)

    def test_a_spent_budget_is_a_pattern_too_slow(self):
        found = Classifier(texts.seed_types(), Budget(0)).classify(texts.KEG)
        self.assertEqual((found.returnable_type, found.problem), (None, SLOW))
        self.assertEqual(found.note, "non classée : motif trop lent")
        memo = {}
        self.assertIsNone(classify(texts.KEG, texts.seed_types(), Budget(0), memo))
        self.assertEqual(memo[texts.KEG].problem, SLOW)

    def test_the_page_s_budget_starts_at_the_first_line_classified(self):
        classifier = Classifier(texts.seed_types())
        self.assertIsNone(classifier._budget)
        classifier.classify(texts.KEG)
        self.assertIsNotNone(classifier._budget)


# -- compare ------------------------------------------------------------------------------------------------------------


class CompareTests(SimpleTestCase):
    def setUp(self):
        self.types = texts.seed_types()

    def rows(self, counts, lines):
        return compare(counts, [line(*values) for values in lines], self.types)

    def test_the_three_sentences(self):
        self.assertEqual(
            self.rows({1: 15}, [keg_line(14)]).rows[0].sentence,
            f"Fûts — compté : 15 {DOT} sur le bon : 14 {ARROW} il en manque 1 sur le bon (30,00{NBSP}€)",
        )
        self.assertEqual(
            self.rows({2: 2}, [(texts.CRATE, 3, None, None)]).rows[0].sentence,
            f"Caisses verre — compté : 2 {DOT} sur le bon : 3 {ARROW} 1 de plus sur le bon",
        )
        self.assertEqual(
            self.rows({1: 15}, [keg_line(15)]).rows[0].sentence, f"Fûts — compté : 15 {DOT} sur le bon : 15 {CHECK}"
        )

    def test_a_gap_of_a_thousand_euros_or_more_has_its_thousands_grouped(self):
        """The counts are not money: « 1500 » stays whole."""
        self.assertEqual(
            self.rows({1: 1500}, [keg_line(1460)]).rows[0].sentence,
            f"Fûts — compté : 1500 {DOT} sur le bon : 1460 {ARROW} il en manque 40 sur le bon (1{NBSP}200,00{NBSP}€)",
        )

    def test_one_row_per_type_on_either_side_in_the_types_order(self):
        result = self.rows({3: 1, 1: 15}, [(texts.CRATE, 2, D("7.50"), D("15.00")), keg_line(15)])
        self.assertEqual(
            [(row.name, row.counted, row.on_slips) for row in result.rows],
            [("Fûts", 15, 15), ("Caisses verre", 0, 2), ("Bouteilles CO2", 1, 0)],
        )
        self.assertEqual(result.differing, result.rows[1:])
        self.assertFalse(result.agrees)

    def test_the_slip_s_quantities_are_summed_then_made_absolute(self):
        self.assertEqual(self.rows({1: 2}, [keg_line(3), keg_line(-1)]).rows[0].on_slips, 2)
        self.assertEqual(self.rows({1: 3}, [keg_line(-3)]).rows[0].on_slips, 3)
        self.assertTrue(self.rows({1: 3}, [keg_line(-3)]).agrees)

    def test_the_unit_price_printed_or_worked_out_to_the_cent(self):
        worked_out = self.rows({1: 4}, [(texts.KEG, 3, None, D("100.00"))]).rows[0]
        self.assertEqual((worked_out.unit, worked_out.money), (D("33.33"), D("33.33")))
        half_up = self.rows({1: 9}, [(texts.KEG, 8, None, D("1.00"))]).rows[0]
        self.assertEqual(half_up.unit, D("0.13"))
        printed = self.rows({1: 5}, [(texts.KEG, 3, D("30.0000"), D("95.00"))]).rows[0]
        self.assertEqual((printed.unit, printed.money), (D("30.00"), D("60.00")))

    def test_a_zero_quantity_gives_no_unit_and_no_division_by_zero(self):
        row = self.rows({1: 3}, [(texts.KEG, 0, None, D("5.00")), (texts.KEG, 2, None, D("60.00"))]).rows[0]
        self.assertEqual(row.unit, D("30.00"))
        self.assertIsNone(self.rows({1: 3}, [(texts.KEG, 0, None, D("5.00"))]).rows[0].unit)

    def test_a_counted_line_without_a_price_leaves_the_type_without_a_unit(self):
        """Spec §5: the unit only when EVERY line's unit is equal. A format
        whose line pattern makes the price optional: the 3 missing kegs may
        well be of the unpriced kind - never priced at the other line's."""
        row = self.rows({1: 8}, [(texts.KEG, 2, D("30.00"), D("60.00")), (texts.KEG_LONG, 3, None, None)]).rows[0]
        self.assertEqual((row.name, row.counted, row.on_slips), ("Fûts", 8, 5))
        self.assertIsNone(row.unit)
        self.assertIsNone(row.money)
        self.assertTrue(row.sentence.endswith(f"{ARROW} il en manque 3 sur le bon"), row.sentence)

    def test_units_that_disagree_show_no_money(self):
        row = self.rows({1: 5}, [keg_line(2, D("30.00")), keg_line(1, D("25.00"))]).rows[0]
        self.assertIsNone(row.unit)
        self.assertIsNone(row.money)
        self.assertTrue(row.sentence.endswith("il en manque 2 sur le bon"))

    def test_lines_no_type_takes_are_listed_and_the_day_differs(self):
        result = self.rows(
            {1: 15},
            [keg_line(15), (texts.PALLET, 1, D("12.00"), D("12.00")), (texts.PALLET, 2, D("12.00"), D("24.00"))],
        )
        self.assertEqual(len(result.unclassified), 1)
        self.assertEqual(result.unclassified[0].sentence, "PALETTE BOIS EUROPE — sur le bon : 3, sans type de consigne")
        self.assertEqual(result.incomplete, [])
        self.assertFalse(result.agrees)

    def test_the_names_are_shown_as_typed_and_every_sentence_is_plain_text(self):
        types = [SimpleNamespace(pk=5, name="fûts de BIÈRE (inox)", position=1, slip_patterns="F[ÛU]T")]
        result = compare({5: 1}, [line(*keg_line(2)), line(texts.PALLET, 1)], types)
        sentences = result.sentences
        self.assertTrue(sentences[0].startswith("fûts de BIÈRE (inox) — compté : 1"))
        for sentence in sentences:
            self.assertIs(type(sentence), str)
            self.assertNotIsInstance(sentence, SafeData)
            self.assertNotIn("Le bon compte", sentence)


# -- Which slips count ------------------------------------------------------------------------------------------------


class EffectiveSlipsTests(NoNetworkTestCase):
    def setUp(self):
        self.fmt = seeded_format()

    def new_slip(self, number, day=DAY, references=(), printed_at=None, replaces=False, fmt=None, lines=(KEG_LINE,)):
        return make_slip(
            fmt or self.fmt,
            number=number,
            delivery_date=day,
            references=references,
            printed_at=printed_at,
            replaces=replaces,
            lines=lines,
        )

    def superseded(self, *slips):
        return effective_slips(slips)[1]

    def test_a_resend_counts_once_and_the_latest_gives_the_content(self):
        first = self.new_slip("1001", references=["610001"], printed_at=moment(DAY, 8))
        again = self.new_slip("1001", references=["610001"], printed_at=moment(DAY + timedelta(days=1), 7))
        kept, superseded = effective_slips([first, again])
        self.assertEqual(kept, [again])
        by, reason = superseded[first.pk]
        self.assertEqual(by.pk, again.pk)
        self.assertEqual(superseded[first.pk].kind, RESENT)
        self.assertEqual(superseded[first.pk].pill, "renvoyé")
        self.assertEqual(reason, "Renvoyé : le bon n° 1001 a été reçu plusieurs fois, le plus récent fait foi.")

    def test_the_same_number_on_two_delivery_dates_is_two_slips(self):
        one = self.new_slip("1001", day=DAY)
        other = self.new_slip("1001", day=DAY + timedelta(days=30))
        self.assertEqual(effective_slips([one, other]), ([one, other], {}))

    def test_a_replacement_supersedes_the_original_in_both_arrival_orders(self):
        for replacement_first in (False, True):
            with self.subTest(replacement_first=replacement_first):
                reference = "620001" if replacement_first else "620002"
                make = [
                    lambda: self.new_slip(f"O{reference}", references=[reference], printed_at=moment(DAY, 7)),  # noqa: B023 - called before the loop moves on
                    lambda: self.new_slip(
                        f"R{reference}",  # noqa: B023 - called before the loop moves on
                        references=[reference],  # noqa: B023 - called before the loop moves on
                        printed_at=moment(DAY, 11),
                        replaces=True,
                    ),
                ]
                if replacement_first:
                    replacement, original = make[1](), make[0]()
                else:
                    original, replacement = make[0](), make[1]()
                kept, superseded = effective_slips([original, replacement])
                self.assertEqual(kept, [replacement])
                self.assertEqual(superseded[original.pk].kind, REPLACED)
                self.assertEqual(superseded[original.pk].pill, "annulé et remplacé")
                self.assertEqual(
                    superseded[original.pk].reason, f"Annulé et remplacé par le bon n° R{reference} du 10/02/2026."
                )

    def test_a_replacement_wins_even_when_neither_was_printed_with_a_time(self):
        replacement = self.new_slip("2", references=["630001"], replaces=True)
        original = self.new_slip("1", references=["630001"])
        self.assertEqual(effective_slips([original, replacement])[0], [replacement])

    def test_the_original_resent_after_its_replacement_still_does_not_count(self):
        original = self.new_slip("1005", references=["610006"], printed_at=moment(DAY, 7))
        replacement = self.new_slip("1006", references=["610006"], printed_at=moment(DAY, 11), replaces=True)
        resent = self.new_slip("1005", references=["610006"], printed_at=moment(DAY + timedelta(days=1), 6))
        kept, superseded = effective_slips([original, replacement, resent])
        self.assertEqual(kept, [replacement])
        self.assertEqual(superseded[resent.pk].kind, REPLACED)
        self.assertEqual(superseded[original.pk].kind, RESENT)
        self.assertEqual(superseded[original.pk].final.pk, replacement.pk)

    def test_among_replacements_sharing_a_reference_the_latest_wins(self):
        original = self.new_slip("1", references=["640001"], printed_at=moment(DAY, 7))
        first = self.new_slip("2", references=["640001"], printed_at=moment(DAY, 9), replaces=True)
        latest = self.new_slip("3", references=["640001"], printed_at=moment(DAY, 10), replaces=True)
        kept, superseded = effective_slips([original, first, latest])
        self.assertEqual(kept, [latest])
        self.assertEqual(superseded[first.pk].by.pk, latest.pk)
        self.assertEqual(superseded[original.pk].final.pk, latest.pk)

    def test_a_slip_without_reference_is_never_superseded_by_reference(self):
        unnamed = self.new_slip("1", references=[], printed_at=moment(DAY, 7))
        replacement = self.new_slip("2", references=["650001"], printed_at=moment(DAY, 9), replaces=True)
        self.assertEqual(effective_slips([unnamed, replacement]), ([unnamed, replacement], {}))

    def test_the_date_fallback_applies_only_to_a_format_with_no_reference_pattern(self):
        plain = make_format(name="Sans références", reference_patterns="")
        older = self.new_slip("1", printed_at=moment(DAY, 6), fmt=plain)
        original = self.new_slip("2", printed_at=moment(DAY, 7), fmt=plain)
        elsewhere = self.new_slip("4", day=DAY + timedelta(days=1), printed_at=moment(DAY, 8), fmt=plain)
        replacement = self.new_slip("3", printed_at=moment(DAY, 11), replaces=True, fmt=plain)
        kept, superseded = effective_slips([older, original, elsewhere, replacement])
        self.assertEqual(kept, [older, elsewhere, replacement])
        self.assertEqual(superseded[original.pk].by.pk, replacement.pk)
        # The seeded format reads references: no fallback, both count.
        same_day = [
            self.new_slip("5", printed_at=moment(DAY, 7)),
            self.new_slip("6", printed_at=moment(DAY, 11), replaces=True),
        ]
        self.assertEqual(effective_slips(same_day)[1], {})

    def test_two_deliveries_on_one_day_without_reference_both_count(self):
        plain = make_format(name="Sans références", reference_patterns="")
        for fmt in (self.fmt, plain):
            with self.subTest(fmt=fmt.name):
                slips = [self.new_slip(f"{fmt.pk}-1", fmt=fmt), self.new_slip(f"{fmt.pk}-2", fmt=fmt)]
                self.assertEqual(effective_slips(slips), (slips, {}))

    def test_a_slip_printed_without_a_time_beside_one_printed_with_one(self):
        printed = self.new_slip("1001", references=["660001"], printed_at=moment(DAY, 8))
        unprinted = self.new_slip("1001", references=["660001"])
        Slip.objects.filter(pk=unprinted.pk).update(received_at=moment(DAY, 9))
        kept, _superseded = effective_slips([printed, unprinted])
        self.assertEqual(kept, [unprinted])
        Slip.objects.filter(pk=unprinted.pk).update(received_at=moment(DAY, 7))
        self.assertEqual(effective_slips([printed, unprinted])[0], [printed])

    def test_every_slip_of_the_format_is_read_in_one_query_not_only_the_ones_shown(self):
        original = self.new_slip("1", references=["670001"], printed_at=moment(DAY, 7))
        replacement = self.new_slip("2", references=["670001"], printed_at=moment(DAY, 9), replaces=True)
        with self.assertNumQueries(1):
            kept, superseded = effective_slips([original])
        self.assertEqual(kept, [])
        self.assertEqual(superseded[original.pk].by.pk, replacement.pk)

    def test_another_format_s_slips_are_never_compared(self):
        other = make_format(name="Autre", supplier=make_supplier())
        original = self.new_slip("1", references=["680001"], printed_at=moment(DAY, 7))
        elsewhere = self.new_slip("2", references=["680001"], printed_at=moment(DAY, 9), replaces=True, fmt=other)
        self.assertEqual(effective_slips([original, elsewhere])[1], {})

    def test_an_index_already_loaded_costs_nothing(self):
        slips = [self.new_slip("1"), self.new_slip("2")]
        index = SlipIndex.load({self.fmt.pk})
        with self.assertNumQueries(0):
            self.assertEqual(effective_slips(slips, index=index)[0], slips)


# -- The days -----------------------------------------------------------------------------------------------------------


class BoardTests(NoNetworkTestCase):
    def setUp(self):
        self.kegs = seeded_type(texts.KEGS)

    def day(self, pickup, *others, slips=()):
        return Board.load(pickups=[pickup, *others], slips=slips).day(pickup)

    def test_two_pickups_of_one_day_are_one_side(self):
        morning = make_pickup(counts={texts.KEGS: 10})
        evening = make_pickup(counts={texts.KEGS: 5})
        make_slip(lines=[keg_line(15)])
        board = Board.load(pickups=[morning, evening])
        day = board.day(morning)
        self.assertIs(board.day(evening), day)
        self.assertEqual(day.status, SAME)
        self.assertEqual(day.pill, "conforme")
        self.assertEqual(day.summed_note, "2 reprises ce jour-là, additionnées")
        self.assertEqual(day.counts_summary, "Fûts 15")
        self.assertEqual(day.comparison.sentences, [f"Fûts — compté : 15 {DOT} sur le bon : 15 {CHECK}"])

    def test_no_supplier(self):
        day = self.day(make_pickup(supplier=None))
        self.assertEqual((day.status, day.pill, day.css), (NO_SUPPLIER, "fournisseur non précisé", "ignored"))

    def test_a_supplier_with_no_format_at_all(self):
        seller = make_supplier()
        pickup = make_pickup(supplier=seller)
        self.assertEqual(self.day(pickup).status, NO_FORMAT)
        make_format(name="Son format", supplier=seller, is_active=False)
        self.assertEqual(self.day(pickup).status, WAITING)

    def test_waiting_for_the_slip(self):
        day = self.day(make_pickup())
        self.assertEqual((day.status, day.pill, day.css), (WAITING, "en attente du bon", "pending"))
        self.assertIsNone(day.comparison)

    def test_a_gap(self):
        make_slip(lines=[keg_line(14)])
        day = self.day(make_pickup())
        self.assertEqual((day.status, day.pill, day.css), (DIFFERS, "écart", "ERROR"))
        self.assertEqual(
            day.comparison.differing[0].sentence,
            f"Fûts — compté : 15 {DOT} sur le bon : 14 {ARROW} il en manque 1 sur le bon (30,00{NBSP}€)",
        )

    def test_a_slip_with_an_unread_line_is_to_check_before_any_gap(self):
        unread = {"label": "Aucune ligne ignorée", "passed": False, "detail": "1 ligne non lue : « CASIER DIVERS »"}
        slip = make_slip(lines=[keg_line(14)], checks=[unread])
        day = self.day(make_pickup())
        self.assertEqual((day.status, day.pill), (TO_CHECK, "à vérifier"))
        self.assertEqual(day.reasons, [f"Le bon n° {slip.number} a une ligne non lue : comparaison incomplète."])
        self.assertIsNotNone(day.comparison)

    def test_a_slip_whose_reading_failed_or_a_check_failed_is_to_check(self):
        broken = make_slip(
            lines=[], read_error="Motif de ligne : parenthèse non fermée (position 3).", delivery_date=DAY
        )
        wrong = {"label": "Total des lignes = total imprimé", "passed": False, "detail": "lignes : 90,00"}
        other = make_slip(
            lines=[keg_line(15)],
            checks=[wrong, {"label": "Aucune ligne ignorée", "passed": False, "detail": "3 lignes non lues : …"}],
        )
        day = self.day(make_pickup())
        self.assertEqual(day.status, TO_CHECK)
        self.assertEqual(
            day.reasons,
            [
                (
                    f"Le bon n° {broken.number} n'a pas pu être lu (Motif de ligne : parenthèse non fermée (position 3)) : "
                    "comparaison impossible."
                ),
                (
                    f"Le bon n° {other.number} : contrôle « Total des lignes = total imprimé » en échec (lignes : 90,00) : "
                    "comparaison à vérifier."
                ),
                f"Le bon n° {other.number} a 3 lignes non lues : comparaison incomplète.",
            ],
        )

    def test_a_slip_with_no_delivery_date_is_never_paired(self):
        slip = make_slip(delivery_date=None)
        pickup = make_pickup()
        board = Board.load(pickups=[pickup], slips=[slip])
        self.assertEqual(board.day(pickup).status, WAITING)
        state = board.slip_state(slip)
        self.assertEqual((state.key, state.pill), ("no_date", "date de livraison non lue"))

    def test_every_format_of_the_supplier_counts_active_or_not(self):
        second = make_format(name="Ancien format UBA", is_active=False, section_start="")
        make_slip(second, lines=[keg_line(15)])
        self.assertEqual(self.day(make_pickup()).status, SAME)

    def test_another_supplier_s_slip_is_never_paired(self):
        seller = make_supplier()
        make_slip(make_format(name="Autre", supplier=seller), lines=[keg_line(15)])
        self.assertEqual(self.day(make_pickup()).status, WAITING)

    def test_only_the_slips_that_count_are_compared(self):
        original = make_slip(number="1005", references=["610006"], printed_at=moment(DAY, 7), lines=[keg_line(5)])
        replacement = make_slip(
            number="1006", references=["610006"], printed_at=moment(DAY, 11), replaces=True, lines=[keg_line(4)]
        )
        pickup = make_pickup(counts={texts.KEGS: 4})
        board = Board.load(pickups=[pickup], slips=[original, replacement])
        day = board.day(pickup)
        self.assertEqual(day.status, SAME)
        self.assertEqual([info.pk for info in day.slips], [replacement.pk])
        self.assertEqual(board.slip_state(original).key, REPLACED)
        self.assertEqual(board.slip_state(replacement).key, "paired")

    def test_a_line_no_type_takes_makes_a_gap(self):
        make_slip(lines=[keg_line(15), (texts.PALLET, 1, D("12.0000"), D("12.00"))])
        day = self.day(make_pickup())
        self.assertEqual(day.status, DIFFERS)
        self.assertEqual(
            day.sentence, "Reprise du 10/02/2026 : écart — PALETTE BOIS EUROPE — sur le bon : 1, sans type de consigne."
        )

    def test_a_type_whose_pattern_no_longer_compiles_is_said_never_a_500(self):
        broken = make_type(name="Cassé", position=0, slip_patterns="(")
        make_slip(lines=[keg_line(15)])
        pickup = make_pickup()
        board = Board.load(pickups=[pickup])
        day = board.day(pickup)
        self.assertEqual(day.status, TO_CHECK)
        self.assertEqual(day.reasons, ["Le type « Cassé » a un motif invalide — corrigez-le : comparaison incomplète."])
        self.assertEqual([kind for kind, _message in board.type_errors], [broken])

    def test_a_page_out_of_time_is_to_check(self):
        make_slip(lines=[keg_line(15)])
        pickup = make_pickup()
        day = Board.load(pickups=[pickup], budget=Budget(0)).day(pickup)
        self.assertEqual(day.status, TO_CHECK)
        self.assertEqual(day.reasons, ["1 ligne du bon non classée : motif trop lent — comparaison incomplète."])

    def test_an_inactive_type_still_classifies_old_lines(self):
        self.kegs.is_active = False
        self.kegs.save()
        make_slip(lines=[keg_line(15)])
        self.assertEqual(self.day(make_pickup()).status, SAME)

    def test_a_pickup_not_loaded_cannot_be_asked_about(self):
        loaded = make_pickup()
        other = make_pickup(date=DAY + timedelta(days=60))
        with self.assertRaises(LookupError):
            Board.load(pickups=[loaded]).day(other)


class HintTests(NoNetworkTestCase):
    def test_a_slip_is_offered_to_the_nearest_unpaired_day_only(self):
        first = make_pickup(date=DAY)
        second = make_pickup(date=DAY + timedelta(days=3))
        slip = make_slip(delivery_date=DAY + timedelta(days=2))
        board = Board.load(pickups=[first, second], slips=[slip])
        self.assertEqual(board.day(first).hints, [])
        hints = board.day(second).hints
        self.assertEqual([(hint.slip.pk, hint.date, hint.days) for hint in hints], [(slip.pk, slip.delivery_date, -1)])
        self.assertEqual(hints[0].sentence, f"Le bon n° {slip.number} du 12/02/2026 n'a pas de reprise ce jour-là.")
        state = board.slip_state(slip)
        self.assertEqual(
            (state.key, state.pill, state.hint_date), ("no_pickup", "sans reprise enregistrée", second.date)
        )

    def test_a_tie_goes_to_the_earlier_day(self):
        earlier = make_pickup(date=DAY)
        later = make_pickup(date=DAY + timedelta(days=2))
        slip = make_slip(delivery_date=DAY + timedelta(days=1))
        board = Board.load(pickups=[earlier, later])
        self.assertEqual([hint.slip.pk for hint in board.day(earlier).hints], [slip.pk])
        self.assertEqual(board.day(later).hints, [])

    def test_no_hint_beyond_three_days_nor_to_a_day_that_has_its_slip(self):
        paired = make_pickup(date=DAY)
        make_slip(delivery_date=DAY, lines=[keg_line(15)])
        make_slip(delivery_date=DAY + timedelta(days=1))
        lonely = make_pickup(date=DAY + timedelta(days=20))
        make_slip(delivery_date=DAY + timedelta(days=24))
        board = Board.load(pickups=[paired, lonely])
        self.assertEqual(board.day(paired).status, SAME)
        self.assertEqual(board.day(paired).hints, [])
        self.assertEqual(board.day(lonely).hints, [])

    def test_another_supplier_s_slip_is_never_offered(self):
        seller = make_supplier()
        make_slip(make_format(name="Autre", supplier=seller), delivery_date=DAY + timedelta(days=1))
        pickup = make_pickup(date=DAY)
        self.assertEqual(Board.load(pickups=[pickup]).day(pickup).hints, [])


class AsIfDatedTests(NoNetworkTestCase):
    """`Board.as_if_dated`: a hinted pickup day compared with the slip's day
    by the rules of a paired day - what a slip's notification says of a
    pickup counted the night before and dated that night."""

    def setUp(self):
        self.counted_on = DAY - timedelta(days=2)

    def test_a_misdated_pickup_is_compared_as_if_it_were_on_the_slip_s_day(self):
        pickup = make_pickup(date=self.counted_on)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(14)], number="4242")
        board = Board.load(slips=[slip])
        self.assertEqual(board.slip_state(slip).hint_date, self.counted_on)
        day = board.as_if_dated(pickup.supplier_id, self.counted_on, DAY)
        self.assertEqual((day.status, day.date, day.pickups), (DIFFERS, DAY, [pickup]))
        self.assertEqual([info.pk for info in day.slips], [slip.pk])
        self.assertEqual(
            day.sentence,
            f"Reprise du 10/02/2026 : écart — Fûts — compté : 15 {DOT} sur le bon : 14 {ARROW} "
            f"il en manque 1 sur le bon (30,00{NBSP}€).",
        )
        # The board's own day of that pickup is untouched: still waiting,
        # its hint still offered.
        own = board.day(pickup)
        self.assertEqual(own.status, WAITING)
        self.assertEqual([hint.slip.pk for hint in own.hints], [slip.pk])

    def test_the_same_counts_are_conforming(self):
        pickup = make_pickup(date=self.counted_on)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="4243")
        day = Board.load(slips=[slip]).as_if_dated(pickup.supplier_id, self.counted_on, DAY)
        self.assertEqual((day.status, day.sentence), (SAME, "Reprise du 10/02/2026 : conforme (bon n° 4243)."))

    def test_the_pickups_of_that_day_are_summed(self):
        morning = make_pickup(date=self.counted_on, counts={texts.KEGS: 10})
        make_pickup(date=self.counted_on, counts={texts.KEGS: 5})
        slip = make_slip(delivery_date=DAY, lines=[keg_line(15)])
        day = Board.load(slips=[slip]).as_if_dated(morning.supplier_id, self.counted_on, DAY)
        self.assertEqual((day.status, len(day.pickups), day.counts_summary), (SAME, 2, "Fûts 15"))

    def test_a_slip_whose_check_failed_is_to_check(self):
        pickup = make_pickup(date=self.counted_on)
        failed = {"label": "Total des lignes = total imprimé", "passed": False, "detail": "lignes : 0,00"}
        slip = make_slip(delivery_date=DAY, lines=[keg_line(15)], checks=[failed], number="4244")
        day = Board.load(slips=[slip]).as_if_dated(pickup.supplier_id, self.counted_on, DAY)
        self.assertEqual(day.status, TO_CHECK)
        self.assertTrue(day.sentence.startswith("Reprise du 10/02/2026 : à vérifier — Le bon n° 4244 : contrôle"))

    def test_on_its_own_day_it_is_the_paired_day(self):
        pickup = make_pickup(date=DAY)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(14)])
        board = Board.load(pickups=[pickup], slips=[slip])
        as_if = board.as_if_dated(pickup.supplier_id, DAY, DAY)
        paired = board.day(pickup)
        self.assertIsNot(as_if, paired)
        self.assertEqual((as_if.status, as_if.sentence, as_if.counts), (paired.status, paired.sentence, paired.counts))

    def test_a_day_with_no_pickup_loaded_cannot_be_asked_about(self):
        pickup = make_pickup(date=DAY - timedelta(days=40))
        slip = make_slip(delivery_date=DAY)
        board = Board.load(slips=[slip])
        for asked in (DAY - timedelta(days=40), DAY - timedelta(days=1)):
            with self.subTest(asked=asked), self.assertRaises(LookupError):
                board.as_if_dated(pickup.supplier_id, asked, DAY)


class SlipStateTests(NoNetworkTestCase):
    def states(self, *slips):
        board = Board.load(slips=slips)
        return [board.slip_state(slip) for slip in slips]

    def test_each_state_of_a_slip(self):
        resent_first = make_slip(
            number="1", references=["690001"], printed_at=moment(DAY, 7), delivery_date=DAY + timedelta(days=40)
        )
        resent_last = make_slip(
            number="1", references=["690001"], printed_at=moment(DAY, 9), delivery_date=DAY + timedelta(days=40)
        )
        unreadable = make_slip(lines=[], read_error="Motif de ligne : le motif est vide.", delivery_date=None)
        nothing = make_slip(lines=[], delivery_date=DAY + timedelta(days=50))
        alone = make_slip(delivery_date=DAY + timedelta(days=60))
        paired = make_slip(delivery_date=DAY, lines=[keg_line(14)])
        make_pickup(date=DAY)
        states = self.states(resent_first, resent_last, unreadable, nothing, alone, paired)
        self.assertEqual(
            [(state.key, state.pill, state.css) for state in states],
            [
                (RESENT, "renvoyé", "ignored"),
                ("no_pickup", "sans reprise enregistrée", "ignored"),
                ("unreadable", "à vérifier", "pending"),
                ("nothing_back", "aucun vide repris", "ignored"),
                ("no_pickup", "sans reprise enregistrée", "ignored"),
                ("paired", "écart", "ERROR"),
            ],
        )
        self.assertEqual(states[0].superseded.by.pk, resent_last.pk)
        self.assertEqual(states[2].reason, "Lecture impossible : Motif de ligne : le motif est vide.")
        self.assertEqual(states[5].day.status, DIFFERS)

    def test_a_slip_whose_lines_could_not_be_read_never_says_nothing_was_taken_back(self):
        """Its part found, its rows no longer matching the line pattern (a
        layout change): stored with no line and failed checks. Unpaired, it
        is « à vérifier » with why - « aucun vide repris » would say the
        opposite of what it lists. A slip read empty keeps its grey pill."""
        unread = {"label": UNREAD_CHECK, "passed": False, "detail": "2 lignes non lues : « FUT EXEMPLE » ; « CAISSE »"}
        total = {"label": "Total des lignes = total imprimé", "passed": False, "detail": "lignes : 0,00"}
        unreadable = make_slip(lines=[], checks=[unread, total], delivery_date=DAY + timedelta(days=50))
        nothing = make_slip(lines=[], delivery_date=DAY + timedelta(days=70))
        states = self.states(unreadable, nothing)
        self.assertEqual(
            [(state.key, state.pill, state.css) for state in states],
            [("to_check", "à vérifier", "pending"), ("nothing_back", "aucun vide repris", "ignored")],
        )
        self.assertEqual(
            states[0].reason, f"Le bon n° {unreadable.number} a 2 lignes non lues : comparaison incomplète."
        )
        self.assertNotIn("Rien n'a été repris", states[0].reason)
        self.assertEqual(states[1].reason, "Rien n'a été repris selon ce bon.")


class CalendarEndsTests(NoNetworkTestCase):
    """A pickup or a slip dated at either end of the calendar (a damaged
    archive imported before « Données » refused it): the page's window is
    cut at the calendar's ends, never an OverflowError - /consignes/ lists
    the newest pickup first, so one such row made every drawing a 500."""

    def test_a_pickup_on_the_calendar_s_last_day(self):
        pickup = make_pickup(date=date(9999, 12, 31))
        board = Board.load(pickups=[pickup])
        self.assertEqual(board.window, (date(9999, 12, 25), date.max))
        self.assertEqual(board.day(pickup).status, WAITING)

    def test_a_slip_delivered_on_the_calendar_s_first_days(self):
        first = make_slip(delivery_date=date(1, 1, 2), lines=[keg_line(15)])
        pickup = make_pickup(date=date(1, 1, 2))
        board = Board.load(pickups=[pickup], slips=[first])
        self.assertEqual(board.window, (date.min, date(1, 1, 8)))
        self.assertEqual(board.slip_state(first).key, "paired")
        self.assertEqual(board.day(pickup).status, SAME)

    def test_both_ends_on_one_page(self):
        old = make_slip(delivery_date=date(1, 1, 2))
        pickup = make_pickup(date=date(9999, 12, 31))
        board = Board.load(pickups=[pickup], slips=[old])
        self.assertEqual(board.window, (date.min, date.max))
        self.assertEqual(board.slip_state(old).key, "no_pickup")
        self.assertEqual(board.day(pickup).status, WAITING)


class QueryCountTests(NoNetworkTestCase):
    """The page costs the same whatever it shows - compared, never pinned."""

    def queries(self, days: int) -> int:
        pickups, slips = [], []
        for offset in range(days):
            day = DAY + timedelta(days=10 * offset)
            pickups.append(make_pickup(date=day, counts={texts.KEGS: 3, texts.BOTTLES: 1}, photos=0))
            slips.append(make_slip(delivery_date=day, lines=[keg_line(3), (texts.CO2, 1, D("85"), D("85"))]))
            slips.append(make_slip(delivery_date=day + timedelta(days=1)))
        with CaptureQueriesContext(connection) as captured:
            board = Board.load(pickups=pickups, slips=slips)
            for pickup in pickups:
                board.day(pickup).sentence  # noqa: B018 - read for the queries it costs
            for slip in slips:
                board.slip_state(slip)
        return len(captured)

    def test_one_day_or_eight_cost_the_same(self):
        self.assertEqual(self.queries(1), self.queries(8))


class LatestNoteTests(NoNetworkTestCase):
    def test_the_gather_s_note_in_one_sentence(self):
        self.assertEqual(latest_note(), "")
        make_pickup(date=DAY - timedelta(days=30), counts={texts.KEGS: 2})
        make_pickup(date=DAY)
        self.assertEqual(latest_note(), "Reprise du 10/02/2026 : en attente du bon.")
        make_slip(delivery_date=DAY, lines=[keg_line(14)], number="4242")
        self.assertEqual(
            latest_note(),
            f"Reprise du 10/02/2026 : écart — Fûts — compté : 15 {DOT} sur le bon : 14 {ARROW} "
            f"il en manque 1 sur le bon (30,00{NBSP}€).",
        )

    def test_a_conforming_day(self):
        make_pickup(date=DAY)
        make_slip(delivery_date=DAY, lines=[keg_line(15)], number="4243")
        self.assertEqual(latest_note(), "Reprise du 10/02/2026 : conforme (bon n° 4243).")


class NoAmbiguousWordsTests(NoNetworkTestCase):
    """Every sentence the comparison builds, on every path: plain str, never
    « Le bon compte » (read first as « the right count »)."""

    def test_every_sentence(self):
        no_defaults()
        fmt = make_format(name="Format exemple")
        kegs = make_type(name="fûts INOX", position=1, slip_patterns="F[ÛU]T")
        make_type(name="Caisses", position=2, slip_patterns="CAISSE")
        sentences = []
        for offset, (counted, lines) in enumerate(
            [
                (15, [keg_line(15)]),
                (15, [keg_line(14)]),
                (2, [keg_line(3), (texts.PALLET, 1, None, None)]),
            ]
        ):
            day = DAY + timedelta(days=10 * offset)
            make_slip(fmt, delivery_date=day, lines=lines)
            pickup = make_pickup(date=day, counts={kegs: counted})
            board_day = Board.load(pickups=[pickup]).day(pickup)
            sentences += [board_day.sentence, *board_day.reasons, *board_day.comparison.sentences]
        waiting = make_pickup(date=DAY + timedelta(days=90), counts={kegs: 1})
        sentences.append(Board.load(pickups=[waiting]).day(waiting).sentence)
        for sentence in sentences:
            self.assertIs(type(sentence), str)
            self.assertNotIn("Le bon compte", sentence)
            self.assertNotIn("le bon compte", sentence)
        self.assertTrue(any("fûts INOX — compté" in sentence for sentence in sentences))
