"""« Trésorerie »'s rules of reading (bank/treasury.py), from plain values -
no database - then what `load()` costs and what the two models refuse.

Every amount, day and account here is invented."""

import importlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

import common
from bank import treasury
from bank.models import (
    TREASURY_AMOUNT_ZERO,
    BankTransaction,
    TreasuryAdjustment,
    TreasuryCheckpoint,
    new_reference,
)
from bank.treasury import (
    BALANCE_AMBIGUOUS,
    BALANCE_UNREADABLE,
    DATE_UNREADABLE,
    Correction,
    Line,
    Point,
    check_point_date,
    compute,
    read_balance,
)
from common import DateRange, last_twelve_months

ONE_DAY = timedelta(days=1)
NBSP = "\N{NO-BREAK SPACE}"
ACCOUNT = "****0042"


def euros(text: str) -> Decimal:
    return Decimal(text)


def point(day: date, balance: str, pk: int | None = None) -> Point:
    return Point(pk, day, euros(balance))


def line(day: date, amount: str, *, imported_on: date | None = None, account: str = ACCOUNT) -> Line:
    """An operation imported the day after it was booked, unless said: its
    day is then complete."""
    return Line(day, euros(amount), account, imported_on or day + ONE_DAY)


def adjustment(day: date, amount: str, pk: int | None = None, reason: str = "") -> Correction:
    return Correction(pk, day, euros(amount), reason)


#: A later statement's line, after every point: the days before it are
#: complete, so a gap is no longer pending (« relevé à importer »).
CLOSING = line(date(2026, 10, 31), "1.00")


def sept(day: int) -> date:
    return date(2026, 9, day)


def octo(day: int) -> date:
    return date(2026, 10, day)


class NoPointTests(SimpleTestCase):
    def test_nothing_at_all_gives_nothing_and_never_raises(self):
        result = compute([], [], [])
        self.assertIsNone(result.balance_on(sept(1)))
        self.assertFalse(result.provisional(sept(1)))
        self.assertEqual(
            (result.known_from, result.horizon, result.last_operation, result.complete_through, result.latest),
            (None, None, None, None, None),
        )
        self.assertEqual((result.gaps, result.to_resolve, result.pending, result.accounts), ([], [], [], 0))
        self.assertEqual((result.curve(DateRange()), result.months(DateRange())), ([], []))
        self.assertIsNone(result.span(DateRange()))
        self.assertIsNone(result.curve_provisional(DateRange()))

    def test_lines_without_a_point_give_no_balance(self):
        result = compute([], [line(sept(3), "120.00"), line(sept(9), "-40.00")], [adjustment(sept(5), "10.00")])
        self.assertEqual((result.known_from, result.last_operation), (sept(3), sept(9)))
        self.assertIsNone(result.horizon)
        self.assertIsNone(result.balance_on(sept(9)))
        self.assertIsNone(result.latest)
        self.assertEqual(result.months(DateRange()), [])
        # No pair of points: the adjustment counts nowhere.
        self.assertEqual((result.corrections, [one.day for one in result.orphans]), ([], [sept(5)]))


class OnePointTests(SimpleTestCase):
    def test_forward_from_the_point(self):
        result = compute([point(sept(10), "1000.00")], [line(sept(12), "200.00"), line(sept(15), "-50.00")], [])
        self.assertEqual(result.balance_on(sept(10)), euros("1000.00"))
        self.assertEqual(result.balance_on(sept(11)), euros("1000.00"))
        self.assertEqual(result.balance_on(sept(12)), euros("1200.00"))
        self.assertEqual(result.balance_on(sept(15)), euros("1150.00"))
        self.assertEqual(result.horizon, sept(15))
        # Nothing past the horizon: no extrapolation.
        self.assertIsNone(result.balance_on(sept(16)))

    def test_backward_from_the_point_before_it(self):
        lines = [line(sept(10), "30.00"), line(sept(12), "200.00"), line(sept(15), "-50.00")]
        result = compute([point(sept(15), "1000.00")], lines, [])
        self.assertEqual(result.known_from, sept(10))
        self.assertEqual(result.balance_on(sept(14)), euros("1050.00"))
        self.assertEqual(result.balance_on(sept(12)), euros("1050.00"))
        self.assertEqual(result.balance_on(sept(11)), euros("850.00"))
        self.assertEqual(result.balance_on(sept(10)), euros("850.00"))
        self.assertIsNone(result.balance_on(sept(9)))
        self.assertEqual(result.reference(sept(11)), point(sept(15), "1000.00"))

    def test_a_point_is_the_end_of_its_day(self):
        lines = [line(sept(9), "-5.00"), line(sept(10), "200.00"), line(sept(11), "30.00")]
        result = compute([point(sept(10), "1000.00")], lines, [])
        # Its own day's +200 is inside the typed figure, counted once.
        self.assertEqual(result.balance_on(sept(10)), euros("1000.00"))
        self.assertEqual(result.balance_on(sept(11)), euros("1030.00"))
        self.assertEqual(result.balance_on(sept(9)), euros("800.00"))


class TwoPointsTests(SimpleTestCase):
    LINES = (line(sept(5), "300.00"), line(sept(20), "-100.00"), CLOSING)

    def gap(self, balance: str):
        result = compute([point(sept(1), "1000.00", 1), point(sept(30), balance, 2)], self.LINES, [])
        self.assertEqual(len(result.gaps), 1)
        return result, result.gaps[0]

    def test_two_that_agree(self):
        result, gap = self.gap("1200.00")
        self.assertEqual((gap.operations, gap.adjusted, gap.explained, gap.missing), (euros("200.00"), 0, 200, 0))
        self.assertTrue(gap.agrees)
        self.assertFalse(gap.to_resolve)
        self.assertEqual(result.to_resolve, [])

    def test_two_that_disagree_one_way(self):
        result, gap = self.gap("1250.00")
        self.assertEqual(gap.missing, euros("50.00"))
        self.assertTrue(gap.to_resolve)
        self.assertEqual(result.to_resolve, [gap])
        # The jump lands on the later point's own day.
        self.assertEqual(result.balance_on(sept(29)), euros("1200.00"))
        self.assertEqual(result.balance_on(sept(30)), euros("1250.00"))
        self.assertTrue(result.unsettled(result.points[1]))

    def test_two_that_disagree_the_other_way(self):
        _result, gap = self.gap("1150.00")
        self.assertEqual(gap.missing, euros("-50.00"))
        self.assertTrue(gap.to_resolve)

    def test_only_consecutive_points_make_a_gap(self):
        points = [point(sept(1), "1000.00", 1), point(sept(15), "1300.00", 2), point(sept(30), "1200.00", 3)]
        result = compute(points, self.LINES, [])
        self.assertEqual([(gap.before.pk, gap.after.pk) for gap in result.gaps], [(1, 2), (2, 3)])
        self.assertIs(result.gap_between(1, 2), result.gaps[0])
        # Another point sits between them, or one is gone.
        self.assertIsNone(result.gap_between(1, 3))
        self.assertIsNone(result.gap_between(1, 99))
        self.assertIsNone(result.gap_between(None, 2))
        self.assertIs(result.gap_ending_on(sept(30)), result.gaps[1])
        self.assertIsNone(result.gap_ending_on(sept(1)))
        self.assertEqual(result.point(2), points[1])
        self.assertIsNone(result.point(99))

    def test_points_are_read_oldest_first_whatever_the_order_given(self):
        result = compute([point(sept(30), "1200.00"), point(sept(1), "1000.00")], self.LINES, [])
        self.assertEqual([one.day for one in result.points], [sept(1), sept(30)])
        self.assertTrue(result.gaps[0].agrees)


class PendingTests(SimpleTestCase):
    """A gap is « relevé à importer » while its later point is past the last
    day whose operations are all imported."""

    POINTS = (point(sept(1), "1000.00"), point(octo(2), "1300.00"))

    def test_a_point_after_the_statement_is_pending(self):
        result = compute(self.POINTS, [line(sept(5), "100.00"), line(sept(25), "50.00")], [])
        self.assertEqual(result.complete_through, sept(25))
        gap = result.gaps[0]
        self.assertTrue(gap.pending)
        self.assertEqual(gap.missing, euros("150.00"))
        self.assertFalse(gap.to_resolve)
        self.assertEqual((result.to_resolve, result.pending), ([], [gap]))

    def test_on_the_last_day_imported_that_same_day_it_is_pending(self):
        # An export made during a day carries only part of it.
        lines = [line(sept(5), "100.00"), line(octo(2), "50.00", imported_on=octo(2))]
        result = compute(self.POINTS, lines, [])
        self.assertEqual((result.last_operation, result.complete_through), (octo(2), octo(1)))
        self.assertTrue(result.gaps[0].pending)

    def test_on_the_last_day_imported_a_later_day_it_is_not(self):
        lines = [line(sept(5), "100.00"), line(octo(2), "50.00", imported_on=octo(3))]
        result = compute(self.POINTS, lines, [])
        self.assertEqual(result.complete_through, octo(2))
        gap = result.gaps[0]
        self.assertFalse(gap.pending)
        self.assertTrue(gap.to_resolve)
        self.assertEqual(gap.missing, euros("150.00"))

    def test_an_older_line_imported_later_does_not_complete_the_last_day(self):
        # Only the import of the lines DATED on the last day says whether
        # that day is whole: 05/09's import on 03/10 says nothing of 02/10.
        lines = [line(sept(5), "100.00", imported_on=octo(3)), line(octo(2), "50.00", imported_on=octo(2))]
        result = compute(self.POINTS, lines, [])
        self.assertEqual(result.complete_through, octo(1))
        self.assertTrue(result.gaps[0].pending)
        self.assertEqual(result.to_resolve, [])

    def test_an_old_statement_imported_later_leaves_today_pending(self):
        # Review C1: the card asks « Relevé manquant entre ces dates ?
        # Importez-le. »; a July export imported on 03/10 must not turn the
        # part of 02/10 not imported yet into 200 € to resolve.
        points = [point(sept(1), "1000.00", 1), point(octo(2), "1250.00", 2)]
        lines = [line(sept(5), "100.00", imported_on=sept(6)), line(octo(2), "-50.00", imported_on=octo(2))]
        before = compute(points, lines, [])
        self.assertEqual(before.complete_through, octo(1))
        self.assertTrue(before.gaps[0].pending)
        july = [*lines, line(date(2026, 7, 10), "-20.00", imported_on=octo(3))]
        after = compute(points, july, [])
        self.assertEqual(after.complete_through, octo(1))
        self.assertTrue(after.gaps[0].pending)
        self.assertEqual(after.to_resolve, [])

    def test_a_line_dated_after_its_import_day_completes_no_day_from_that_import_on(self):
        # An export made on 02/10 carrying an operation dated 03/10: 02/10,
        # the import's own day, is not whole either.
        lines = [line(sept(5), "100.00"), line(octo(3), "-50.00", imported_on=octo(2))]
        result = compute(self.POINTS, lines, [])
        self.assertEqual((result.last_operation, result.complete_through), (octo(3), octo(1)))
        self.assertTrue(result.gaps[0].pending)
        self.assertEqual(result.to_resolve, [])

    def test_no_line_at_all_every_gap_is_pending(self):
        result = compute([*self.POINTS, point(octo(9), "900.00")], [], [])
        self.assertIsNone(result.complete_through)
        self.assertEqual(len(result.pending), 2)
        self.assertEqual(result.to_resolve, [])
        # The balances are the points'.
        self.assertEqual(result.balance_on(sept(20)), euros("1000.00"))
        self.assertEqual(result.balance_on(octo(2)), euros("1300.00"))

    def test_the_import_that_completes_the_day_settles_it_both_ways(self):
        before = [line(sept(5), "100.00"), line(octo(1), "50.00", imported_on=octo(1))]
        self.assertTrue(compute(self.POINTS, before, []).gaps[0].pending)
        # The missing operation arrives: the gap vanishes.
        arrived = [*before, line(octo(2), "150.00", imported_on=octo(3))]
        self.assertTrue(compute(self.POINTS, arrived, []).gaps[0].agrees)
        # Nothing arrives but the day is complete: a real gap.
        nothing = [*before, line(octo(3), "-5.00", imported_on=octo(4))]
        gap = compute(self.POINTS, nothing, []).gaps[0]
        self.assertTrue(gap.to_resolve)
        self.assertEqual(gap.missing, euros("150.00"))


class BeforeTheStatementTests(SimpleTestCase):
    def test_a_point_before_the_first_operation_is_known_and_not_pending(self):
        points = [point(date(2026, 6, 1), "500.00"), point(date(2026, 7, 15), "480.00")]
        result = compute(points, [line(sept(1), "300.00"), line(sept(20), "50.00")], [])
        self.assertEqual(result.known_from, date(2026, 6, 1))
        self.assertEqual(result.balance_on(date(2026, 6, 15)), euros("500.00"))
        self.assertEqual(result.balance_on(sept(20)), euros("830.00"))
        gap = result.gaps[0]
        self.assertFalse(gap.pending)
        self.assertEqual(gap.missing, euros("-20.00"))
        self.assertTrue(gap.to_resolve)


class SuspectTests(SimpleTestCase):
    def test_a_point_without_which_its_neighbours_agree(self):
        # B is a typo: 1 300 for 1 100.
        points = [point(sept(1), "1000.00"), point(sept(15), "1300.00"), point(octo(1), "1100.00")]
        result = compute(points, [line(sept(10), "100.00"), CLOSING], [])
        self.assertEqual([gap.missing for gap in result.gaps], [euros("200.00"), euros("-200.00")])
        self.assertEqual([gap.suspect for gap in result.gaps], [points[1], points[1]])

    def test_an_adjustment_on_one_side_does_not_hide_it(self):
        points = [point(sept(1), "1000.00"), point(sept(15), "1300.00"), point(octo(1), "1100.00")]
        result = compute(points, [line(sept(10), "100.00"), CLOSING], [adjustment(sept(15), "200.00")])
        first, second = result.gaps
        self.assertTrue(first.agrees)
        self.assertTrue(second.to_resolve)
        self.assertEqual((first.suspect, second.suspect), (points[1], points[1]))

    def test_an_adjustment_does_not_make_a_correct_point_suspect(self):
        # Review money-7: a +100 operation of 10/09 is missing, the pair
        # A-C was adjusted by +100, then the TRUE balance of 15/09 is typed.
        points = [point(sept(1), "1000.00"), point(sept(15), "1150.00"), point(octo(1), "1300.00")]
        lines = [line(sept(5), "50.00"), line(sept(20), "150.00"), CLOSING]
        result = compute(points, lines, [adjustment(octo(1), "100.00")])
        self.assertEqual([gap.missing for gap in result.gaps], [euros("100.00"), euros("-100.00")])
        self.assertEqual([gap.raw_missing for gap in result.gaps], [euros("100.00"), euros("0.00")])
        self.assertEqual([gap.suspect for gap in result.gaps], [None, None])

    def test_never_beside_a_pending_gap(self):
        points = [point(sept(1), "1000.00"), point(sept(15), "1300.00"), point(octo(1), "1100.00")]
        lines = [line(sept(10), "100.00"), line(sept(20), "0.00", imported_on=sept(20))]
        result = compute(points, lines, [])
        self.assertTrue(result.gaps[1].pending)
        self.assertEqual([gap.suspect for gap in result.gaps], [None, None])

    def test_never_when_both_agree(self):
        points = [point(sept(1), "1000.00"), point(sept(15), "1100.00"), point(octo(1), "1100.00")]
        result = compute(points, [line(sept(10), "100.00"), CLOSING], [])
        self.assertEqual([gap.suspect for gap in result.gaps], [None, None])


class AroundTests(SimpleTestCase):
    """`Treasury.around(point)`: the counted adjustments between a point's
    two neighbours - what must go with a suspect point for its neighbours to
    agree (review C2/C12)."""

    LINES = (line(sept(10), "100.00"), CLOSING)

    def test_an_adjustment_made_on_one_side_of_the_typo_is_around_it(self):
        # Review C2: the typo of 15/09 was adjusted on its first side (+200,
        # dated 15/09); the second card lists no adjustment of its own.
        a, b, c = point(sept(1), "1000.00", 1), point(sept(15), "1300.00", 2), point(octo(1), "1100.00", 3)
        made = adjustment(sept(15), "200.00", pk=7)
        result = compute([a, b, c], self.LINES, [made])
        first, second = result.gaps
        self.assertEqual((first.agrees, second.corrections, second.suspect), (True, [], b))
        self.assertEqual(result.around(b), [made])
        # Without the point alone, the neighbours still disagree…
        self.assertEqual(compute([a, c], self.LINES, [made]).gaps[0].missing, euros("-200.00"))
        # … without the point AND that adjustment, they agree.
        self.assertTrue(compute([a, c], self.LINES, []).gaps[0].agrees)

    def test_an_adjustment_dated_on_the_suspect_s_own_day(self):
        # Review C12: « Ajouter un ajustement » on the first card dates it on
        # the typo's day - outside the second card's stretch.
        a, b, c = point(sept(1), "1000.00", 1), point(sept(10), "1300.00", 2), point(sept(15), "1150.00", 3)
        made = adjustment(sept(10), "200.00", pk=7)
        lines = [line(sept(5), "100.00"), line(sept(12), "50.00"), line(sept(20), "1.00")]
        result = compute([a, b, c], lines, [made])
        first, second = result.gaps
        self.assertTrue(first.agrees)
        self.assertEqual((second.missing, second.suspect, second.corrections), (euros("-200.00"), b, []))
        self.assertEqual(result.around(b), [made])

    def test_only_what_lies_between_its_two_neighbours(self):
        points = [
            point(sept(1), "1000.00", 1),
            point(sept(15), "1100.00", 2),
            point(octo(1), "1100.00", 3),
            point(octo(20), "1100.00", 4),
        ]
        before, on_first, inside, on_third, beyond = (
            adjustment(sept(1), "1.00", pk=5),  # on the first point's day: counts nowhere
            adjustment(sept(2), "2.00", pk=6),
            adjustment(sept(20), "3.00", pk=7),
            adjustment(octo(1), "4.00", pk=8),
            adjustment(octo(5), "5.00", pk=9),
        )
        result = compute(points, self.LINES, [beyond, on_third, inside, on_first, before])
        self.assertEqual(result.around(points[1]), [on_first, inside, on_third])
        self.assertEqual(result.around(points[2]), [inside, on_third, beyond])
        # An end point has one gap, and its corrections are those.
        self.assertEqual(result.around(points[0]), [on_first])
        self.assertEqual(result.around(points[3]), [beyond])
        self.assertEqual(result.around(points[0]), result.gaps[0].corrections)

    def test_a_point_it_does_not_hold_has_nothing_around_it(self):
        a, b = point(sept(1), "1000.00", 1), point(sept(30), "1150.00", 2)
        result = compute([a, b], self.LINES, [adjustment(sept(20), "50.00")])
        self.assertEqual(result.around(point(sept(15), "1.00", 3)), [])
        self.assertEqual(result.around(point(sept(30), "9.00", 2)), [])  # another balance that day
        self.assertEqual(compute([a], self.LINES, [adjustment(sept(20), "50.00")]).around(a), [])

    def test_without_a_suspect_and_what_is_around_it_its_neighbours_agree(self):
        """Whenever a point is suspect, the treasury computed again without
        it AND without `around(it)` has its two neighbours agreeing - the
        card's « Sans le point du … » hint, made true by naming them."""
        candidates = [
            adjustment(sept(5), "50.00", pk=11),
            adjustment(sept(15), "200.00", pk=12),
            adjustment(sept(20), "-30.00", pk=13),
            adjustment(octo(1), "10.00", pk=14),
            adjustment(octo(10), "5.00", pk=15),
        ]
        lines = [line(sept(10), "100.00"), line(octo(5), "-40.00"), CLOSING]
        balances = (
            ("1300.00", "1100.00", "1060.00"),  # 15/09 a typo
            ("1100.00", "1150.00", "1060.00"),  # 01/10 a typo
            ("1300.00", "1100.00", "1260.00"),  # both ends of the middle gap suspect
            ("1100.00", "1100.00", "1060.00"),  # every balance right
            ("1100.00", "1100.00", "1100.00"),  # the last one wrong: nothing cancels
        )
        suspects = settled_by_more = 0
        for b, c, d in balances:
            points = [
                point(sept(1), "1000.00", 1),
                point(sept(15), b, 2),
                point(octo(1), c, 3),
                point(octo(15), d, 4),
            ]
            for mask in range(2 ** len(candidates)):
                made = [one for bit, one in enumerate(candidates) if mask >> bit & 1]
                result = compute(points, lines, made)
                for suspect in {gap.suspect for gap in result.gaps if gap.suspect is not None}:
                    with self.subTest(balances=(b, c, d), adjustments=[one.pk for one in made], suspect=suspect.day):
                        index = result.points.index(suspect)
                        neighbours = result.points[index - 1], result.points[index + 1]
                        around = result.around(suspect)
                        again = compute(
                            [one for one in points if one != suspect],
                            lines,
                            [one for one in made if one not in around],
                        )
                        merged = again.gap_between(neighbours[0].pk, neighbours[1].pk)
                        self.assertIsNotNone(merged)
                        self.assertFalse(merged.pending)
                        self.assertTrue(merged.agrees)
                        suspects += 1
                        settled_by_more += sum((one.amount for one in around), Decimal("0")) != 0
        # Not vacuous: suspects were found, many with an adjustment that had
        # to go with them.
        self.assertGreater(suspects, 50)
        self.assertGreater(settled_by_more, 20)


class ReadBeforeTests(SimpleTestCase):
    """A balance read before its day's operations - « solde lu avant
    elles ? » and « Dater ce point du … »."""

    def test_the_later_point_read_before_its_day(self):
        lines = [line(sept(5), "100.00"), line(sept(30), "40.00"), line(sept(30), "-10.00")]
        result = compute([point(sept(1), "1000.00"), point(sept(30), "1100.00")], lines, [])
        gap = result.gaps[0]
        self.assertEqual(gap.missing, euros("-30.00"))
        self.assertEqual(result.operations_on(sept(30)), euros("30.00"))
        self.assertEqual(gap.read_before, point(sept(30), "1100.00"))
        self.assertEqual(gap.move_to, sept(29))

    def test_the_earlier_point_read_before_its_day(self):
        lines = [line(sept(1), "25.00"), line(sept(5), "100.00"), CLOSING]
        result = compute([point(sept(1), "1000.00"), point(sept(30), "1125.00")], lines, [])
        gap = result.gaps[0]
        self.assertEqual(gap.missing, euros("25.00"))
        self.assertEqual(gap.read_before, point(sept(1), "1000.00"))
        self.assertEqual(gap.move_to, date(2026, 8, 31))

    def test_a_day_whose_operations_net_zero_says_nothing(self):
        lines = [line(sept(5), "100.00"), line(sept(30), "20.00"), line(sept(30), "-20.00")]
        gap = compute([point(sept(1), "1000.00"), point(sept(30), "1085.00")], lines, []).gaps[0]
        self.assertEqual(gap.missing, euros("-15.00"))
        self.assertIsNone(gap.read_before)
        self.assertIsNone(gap.move_to)

    def test_only_an_exact_match(self):
        lines = [line(sept(5), "100.00"), line(sept(30), "30.00")]
        gap = compute([point(sept(1), "1000.00"), point(sept(30), "1100.01")], lines, []).gaps[0]
        self.assertEqual(gap.missing, euros("-29.99"))
        self.assertIsNone(gap.read_before)

    def test_never_on_a_pending_gap(self):
        lines = [line(sept(5), "100.00"), line(sept(30), "30.00", imported_on=sept(30))]
        gap = compute([point(sept(1), "1000.00"), point(sept(30), "1100.00")], lines, []).gaps[0]
        self.assertTrue(gap.pending)
        self.assertIsNone(gap.read_before)


class AdjustmentTests(SimpleTestCase):
    POINTS = (point(sept(1), "1000.00", 1), point(sept(30), "1150.00", 2))
    LINES = (line(sept(10), "100.00"), CLOSING)

    def test_an_adjustment_of_the_gap_closes_it(self):
        self.assertEqual(compute(self.POINTS, self.LINES, []).gaps[0].missing, euros("50.00"))
        made = adjustment(sept(30), "50.00", pk=7, reason="Écart non expliqué")
        result = compute(self.POINTS, self.LINES, [made])
        gap = result.gaps[0]
        self.assertTrue(gap.agrees)
        self.assertEqual((gap.operations, gap.adjusted, gap.explained), (euros("100.00"), euros("50.00"), 150))
        self.assertEqual(gap.corrections, [made])
        self.assertEqual((result.corrections, result.orphans), ([made], []))
        self.assertEqual(result.balance_on(sept(30)), euros("1150.00"))
        self.assertEqual(result.latest.reference, self.POINTS[1])

    def test_an_adjustment_moves_the_balances_from_its_day(self):
        result = compute(self.POINTS, self.LINES, [adjustment(sept(20), "50.00")])
        self.assertEqual(result.balance_on(sept(19)), euros("1100.00"))
        self.assertEqual(result.balance_on(sept(20)), euros("1150.00"))

    def test_the_real_operation_imported_afterwards_reverses_the_gap(self):
        made = adjustment(sept(30), "50.00", pk=7)
        result = compute(self.POINTS, [*self.LINES, line(sept(20), "50.00")], [made])
        gap = result.gaps[0]
        self.assertEqual(gap.missing, euros("-50.00"))
        self.assertTrue(gap.to_resolve)
        # The card lists it: deleting it is the fix.
        self.assertEqual(gap.corrections, [made])

    def test_a_gap_wider_than_the_column_cannot_be_adjusted(self):
        points = [point(sept(1), "-9999999999.99"), point(sept(30), "9999999999.99")]
        gap = compute(points, [line(sept(10), "0.00")], []).gaps[0]
        self.assertTrue(gap.too_big)
        self.assertFalse(self.gap_of("1150.00").too_big)

    def gap_of(self, balance: str):
        return compute([self.POINTS[0], point(sept(30), balance)], self.LINES, []).gaps[0]


class OrphanTests(SimpleTestCase):
    """An adjustment counts only between two points: one left outside counts
    nowhere and is listed (review money-2)."""

    LINES = (line(sept(10), "300.00"), line(sept(20), "-100.00"))

    def test_deleting_the_last_point_leaves_its_adjustment_counting_nowhere(self):
        a = point(sept(1), "1000.00", 1)
        made = adjustment(sept(30), "200.00", pk=7)
        with_b = compute([a, point(sept(30), "1400.00", 2)], self.LINES, [made])
        self.assertTrue(with_b.gaps[0].agrees)
        self.assertEqual(with_b.orphaned_by_deleting(2), [made])
        result = compute([a], self.LINES, [made])
        self.assertEqual((result.corrections, result.orphans), ([], [made]))
        # A plus the operations, never plus the adjustment.
        self.assertEqual(result.latest.balance, euros("1200.00"))

    def test_deleting_the_first_point_leaves_the_headline_as_it_was(self):
        points = [point(sept(1), "1000.00", 1), point(sept(15), "1340.00", 2), point(sept(30), "1240.00", 3)]
        made = adjustment(sept(15), "40.00", pk=7)
        whole = compute(points, self.LINES, [made])
        self.assertEqual([gap.missing for gap in whole.gaps], [0, 0])
        self.assertEqual(whole.orphaned_by_deleting(1), [made])
        result = compute(points[1:], self.LINES, [made])
        self.assertEqual(result.orphans, [made])
        without = compute(points[1:], self.LINES, [])
        self.assertEqual(result.latest, without.latest)
        # Counted, the adjustment of 15/09 would take 40 off every day before.
        self.assertEqual(result.balance_on(sept(12)), without.balance_on(sept(12)))
        self.assertEqual(result.balance_on(sept(12)), euros("1340.00"))

    def test_a_middle_point_deleted_orphans_nothing(self):
        points = [point(sept(1), "1000.00", 1), point(sept(15), "1340.00", 2), point(sept(30), "1240.00", 3)]
        self.assertEqual(compute(points, self.LINES, [adjustment(sept(15), "40.00")]).orphaned_by_deleting(2), [])

    def test_moving_the_last_point_back_leaves_an_adjustment_of_its_day_outside(self):
        made = adjustment(sept(30), "200.00", pk=7)
        result = compute([point(sept(1), "1000.00", 1), point(sept(30), "1400.00", 2)], self.LINES, [made])
        self.assertEqual(result.orphaned_by_moving(2, sept(29)), [made])
        self.assertEqual(result.orphaned_by_moving(1, date(2026, 8, 31)), [])

    def test_outside_every_pair_whatever_brought_it(self):
        points = [point(sept(1), "1000.00"), point(sept(30), "1200.00")]
        early, inside, late = (
            adjustment(sept(1), "5.00"),
            adjustment(sept(2), "-5.00"),
            adjustment(date(2099, 1, 1), "9.00"),
        )
        result = compute(points, self.LINES, [late, inside, early])
        self.assertEqual((result.corrections, result.orphans), ([inside], [early, late]))
        self.assertEqual(result.balance_on(sept(30)), euros("1200.00"))
        self.assertEqual(result.gaps[0].missing, euros("5.00"))


class ProvisionalTests(SimpleTestCase):
    def test_backward_across_days_not_imported_is_provisional_and_shown(self):
        # The everyday case: today's balance typed, the statement imported
        # up to yesterday that same day.
        lines = [line(sept(10), "300.00"), line(sept(30), "-50.00", imported_on=sept(30))]
        result = compute([point(octo(2), "5000.00")], lines, [])
        self.assertEqual(result.complete_through, sept(29))
        self.assertEqual(result.balance_on(sept(15)), euros("5050.00"))
        self.assertTrue(result.provisional(sept(15)))
        self.assertFalse(result.provisional(octo(2)))
        self.assertFalse(result.latest.provisional)
        self.assertEqual(result.curve_provisional(DateRange()), (sept(10), octo(1)))

    def test_importing_the_stretch_clears_it_and_moves_by_exactly_its_operations(self):
        lines = [line(sept(10), "300.00"), line(sept(30), "-50.00", imported_on=sept(30))]
        before = compute([point(octo(2), "5000.00")], lines, [])
        stretch = [line(octo(1), "-800.00", imported_on=octo(3)), line(octo(2), "12.50", imported_on=octo(3))]
        after = compute([point(octo(2), "5000.00")], [*lines, *stretch], [])
        self.assertEqual(after.complete_through, octo(2))
        self.assertFalse(after.provisional(sept(15)))
        self.assertEqual(after.balance_on(sept(15)) - before.balance_on(sept(15)), euros("787.50"))
        self.assertIsNone(after.curve_provisional(DateRange()))

    def test_forward_past_the_last_day_complete(self):
        lines = [line(sept(5), "100.00"), line(sept(25), "50.00"), line(sept(26), "20.00", imported_on=sept(26))]
        result = compute([point(sept(1), "10000.00")], lines, [])
        self.assertEqual((result.complete_through, result.horizon), (sept(25), sept(26)))
        self.assertFalse(result.provisional(sept(25)))
        self.assertTrue(result.provisional(sept(26)))
        self.assertEqual(result.latest, (sept(26), euros("10170.00"), True, point(sept(1), "10000.00")))
        self.assertEqual(result.curve_provisional(DateRange()), (sept(26), sept(26)))
        imported = compute([point(sept(1), "10000.00")], [*lines, line(sept(26), "0.00", imported_on=sept(27))], [])
        self.assertFalse(imported.latest.provisional)

    def test_nothing_imported_every_day_but_a_point_is_provisional(self):
        result = compute([point(sept(1), "100.00"), point(sept(5), "100.00")], [], [])
        self.assertTrue(result.provisional(sept(3)))
        self.assertFalse(result.provisional(sept(5)))
        self.assertEqual(result.curve_provisional(DateRange()), (sept(2), sept(4)))

    def test_no_balance_is_never_provisional(self):
        result = compute([point(sept(5), "100.00")], [], [])
        self.assertFalse(result.provisional(sept(1)))
        self.assertFalse(result.provisional(sept(6)))
        self.assertIsNone(result.curve_provisional(DateRange()))


class AccountsTests(SimpleTestCase):
    def test_several_accounts_count_and_the_earliest_complete_day_holds(self):
        lines = [
            line(sept(10), "100.00", account="****0042"),
            line(sept(30), "50.00", account="****0042", imported_on=octo(1)),
            line(sept(5), "-20.00", account="****7777"),
            line(sept(20), "-30.00", account="****7777", imported_on=sept(20)),
        ]
        result = compute([point(sept(1), "1000.00"), point(sept(25), "1050.00")], lines, [])
        self.assertEqual(result.accounts, 2)
        self.assertEqual((result.last_operation, result.complete_through), (sept(30), sept(19)))
        # Every account summed: the point is their total.
        self.assertEqual(result.balance_on(sept(30)), euros("1100.00"))
        self.assertTrue(result.gaps[0].pending)

    def test_a_blank_account_is_no_account(self):
        lines = [line(sept(10), "100.00", account=""), line(sept(12), "5.00", account="  ")]
        self.assertEqual(compute([], lines, []).accounts, 0)
        mixed = [line(sept(10), "100.00", account=""), line(sept(30), "1.00", imported_on=sept(30))]
        result = compute([], mixed, [])
        self.assertEqual((result.accounts, result.complete_through), (1, sept(29)))

    def test_each_account_s_last_day_is_completed_by_its_own_lines_of_that_day(self):
        lines = [
            # An older line of ****0042 imported later says nothing of its 30/09.
            line(sept(5), "10.00", account="****0042", imported_on=octo(3)),
            line(sept(30), "20.00", account="****0042", imported_on=sept(30)),
            line(octo(1), "30.00", account="****7777", imported_on=octo(2)),
        ]
        self.assertEqual(compute([], lines, []).complete_through, sept(29))

    def test_with_several_accounts_a_blank_one_holds_none_back(self):
        lines = [
            line(date(2026, 1, 3), "9.00", account="", imported_on=date(2026, 1, 3)),
            line(sept(10), "100.00", account="****0042"),
            line(sept(12), "100.00", account="****7777"),
        ]
        self.assertEqual(compute([], lines, []).complete_through, sept(10))


class AmountTests(SimpleTestCase):
    def test_zero_and_negative_balances_are_balances(self):
        result = compute([point(sept(1), "0.00"), point(sept(10), "-250.00")], [line(sept(5), "-250.00")], [])
        self.assertTrue(result.gaps[0].agrees)
        self.assertEqual(result.balance_on(sept(4)), euros("0.00"))
        self.assertEqual(result.balance_on(sept(5)), euros("-250.00"))
        self.assertEqual(result.months(DateRange())[0].closing, euros("-250.00"))

    def test_decimal_exact(self):
        lines = [line(sept(2), "0.10"), line(sept(3), "0.10"), line(sept(4), "0.10")]
        result = compute([point(sept(1), "0.00"), point(sept(4), "0.30")], lines, [])
        self.assertEqual(result.balance_on(sept(4)), euros("0.30"))
        self.assertEqual(str(result.balance_on(sept(3))), "0.20")
        self.assertTrue(result.gaps[0].agrees)
        self.assertIsInstance(result.gaps[0].missing, Decimal)


class HorizonTests(SimpleTestCase):
    def test_the_last_point_or_the_last_operation_whichever_is_later(self):
        lines = [line(sept(5), "100.00")]
        self.assertEqual(compute([point(sept(20), "1.00")], lines, []).horizon, sept(20))
        self.assertEqual(compute([point(sept(1), "1.00")], lines, []).horizon, sept(5))
        self.assertIsNone(compute([], lines, []).horizon)

    def test_the_headline(self):
        lines = [line(sept(5), "100.00"), line(sept(12), "-40.00")]
        a, b = point(sept(1), "900.00", 1), point(sept(10), "1000.00", 2)
        latest = compute([a, b], lines, []).latest
        self.assertEqual(latest, treasury.Latest(sept(12), euros("960.00"), False, b))
        self.assertIsNone(compute([a, b], lines, []).balance_on(sept(13)))

    def test_its_reference_point_is_lit_only_when_it_ends_a_gap_to_resolve(self):
        lines = [line(sept(5), "100.00"), line(sept(12), "1.00")]
        lit = compute([point(sept(1), "900.00"), point(sept(10), "1050.00")], lines, [])
        self.assertTrue(lit.unsettled(lit.latest.reference))
        # An old gap to resolve does not light the headline (review owner-8).
        points = [point(sept(1), "900.00"), point(sept(3), "950.00"), point(sept(10), "1050.00")]
        old = compute(points, lines, [])
        self.assertEqual([gap.to_resolve for gap in old.gaps], [True, False])
        self.assertFalse(old.unsettled(old.latest.reference))


class WindowTests(SimpleTestCase):
    """The curve and the months over a window: the history narrowed to
    [max(du, known_from), min(au, horizon)]."""

    TODAY = octo(2)
    LINES = (
        line(date(2026, 1, 10), "500.00"),
        line(date(2026, 3, 15), "-120.50"),
        line(date(2026, 6, 1), "1000.00"),
        line(date(2026, 6, 30), "-300.00"),
        line(sept(5), "200.00"),
        line(sept(20), "-80.00"),
        line(sept(30), "45.50", imported_on=octo(1)),
    )
    POINTS = (
        point(date(2026, 2, 1), "1500.00", 1),
        point(sept(10), "2279.50", 2),
        point(octo(2), "2000.00", 3),  # typed today: pending
    )

    def setUp(self):
        self.result = compute(self.POINTS, self.LINES, [])

    def test_the_default_window_starts_where_the_data_does(self):
        window = last_twelve_months(today=self.TODAY)
        self.assertEqual(self.result.span(window), (date(2026, 1, 10), self.TODAY))
        months = self.result.months(window)
        self.assertEqual([row.month for row in months], [date(2026, month, 1) for month in range(1, 11)])
        january = months[0]
        self.assertEqual((january.start, january.end), (date(2026, 1, 10), date(2026, 1, 31)))
        self.assertEqual((january.credits, january.debits, january.closing), (500, 0, euros("1500.00")))
        self.assertEqual(months[2].debits, euros("-120.50"))
        june = months[5]
        self.assertEqual((june.credits, june.debits, june.closing), (1000, euros("-300.00"), euros("2079.50")))
        september, october = months[8], months[9]
        self.assertEqual((september.closing, september.closing_provisional), (euros("2245.00"), False))
        self.assertEqual((september.gap_to_resolve, september.gap_pending), (0, False))
        self.assertEqual((october.start, october.end, october.closing), (octo(1), octo(2), euros("2000.00")))
        self.assertEqual((october.gap_to_resolve, october.gap_pending), (0, True))
        self.assertEqual((october.partial, september.partial), (True, False))
        self.assertEqual(self.result.curve_provisional(window), (octo(1), octo(1)))

    def test_tout_is_the_whole_history(self):
        self.assertEqual(self.result.span(DateRange()), (date(2026, 1, 10), self.TODAY))
        self.assertEqual(self.result.span(None), self.result.span(DateRange()))
        curve = self.result.curve(DateRange())
        self.assertEqual(curve[0], (date(2026, 1, 10), euros("1500.00")))
        self.assertEqual(curve[-1], (self.TODAY, euros("2000.00")))
        self.assertEqual(len(self.result.months(DateRange())), 10)

    def test_a_window_starting_mid_month_shows_no_false_gap(self):
        months = self.result.months(DateRange(sept(15), None))
        self.assertEqual([row.month for row in months], [date(2026, 9, 1), date(2026, 10, 1)])
        september = months[0]
        self.assertEqual(
            (september.start, september.credits, september.debits), (sept(15), euros("45.50"), euros("-80.00"))
        )
        self.assertEqual((september.gap_to_resolve, september.gap_pending), (0, False))
        self.assertEqual(self.result.curve(DateRange(sept(15), None))[0], (sept(15), euros("2279.50")))

    def test_a_window_before_any_data_is_empty(self):
        window = DateRange(date(1990, 1, 1), date(1990, 12, 31))
        self.assertIsNone(self.result.span(window))
        self.assertEqual((self.result.curve(window), self.result.months(window)), ([], []))
        self.assertIsNone(self.result.curve_provisional(window))
        after = DateRange(date(2030, 1, 1), None)
        self.assertEqual((self.result.curve(after), self.result.months(after)), ([], []))

    def test_a_far_start_costs_nothing_and_gives_no_row_before_the_data(self):
        window = DateRange(date(1, 1, 1), self.TODAY)
        months = self.result.months(window)
        self.assertEqual((months[0].month, months[0].start), (date(2026, 1, 1), date(2026, 1, 10)))
        self.assertEqual(self.result.curve(window)[0][0], date(2026, 1, 10))

    def test_an_end_before_the_horizon_ends_the_curve_there(self):
        window = DateRange(None, date(2026, 6, 30))
        self.assertEqual(self.result.curve(window)[-1], (date(2026, 6, 30), euros("2079.50")))
        months = self.result.months(window)
        self.assertEqual(
            (months[-1].month, months[-1].end, months[-1].partial), (date(2026, 6, 1), date(2026, 6, 30), False)
        )
        mid = self.result.months(DateRange(None, date(2026, 6, 15)))[-1]
        self.assertEqual((mid.end, mid.partial, mid.credits, mid.debits), (date(2026, 6, 15), True, 1000, 0))

    def test_one_value_a_day_oldest_first(self):
        days = [day for day, _balance in self.result.curve(DateRange())]
        self.assertEqual(days, sorted(set(days)))


class CurveStepsTests(SimpleTestCase):
    def test_a_balance_does_not_slope_between_two_movements(self):
        lines = [line(sept(5), "100.00"), line(sept(8), "-40.00")]
        curve = compute([point(sept(1), "1000.00")], lines, []).curve(DateRange())
        self.assertEqual(
            curve,
            [
                (sept(1), euros("1000.00")),
                (sept(4), euros("1000.00")),
                (sept(5), euros("1100.00")),
                (sept(7), euros("1100.00")),
                (sept(8), euros("1060.00")),
            ],
        )

    def test_a_point_mid_span_is_a_day_of_the_curve_with_its_step(self):
        # Review C4: a gap's jump lands on the later point's own day, even
        # with no operation that day - never a diagonal from 05/09 to 24/09
        # through balances nobody had (review money-13).
        points = [point(sept(1), "1000.00", 1), point(sept(15), "1300.00", 2)]
        lines = [line(sept(5), "100.00"), line(sept(25), "10.00")]
        curve = compute(points, lines, []).curve(DateRange())
        self.assertEqual(
            curve,
            [
                (sept(1), euros("1000.00")),
                (sept(4), euros("1000.00")),
                (sept(5), euros("1100.00")),
                (sept(14), euros("1100.00")),
                (sept(15), euros("1300.00")),
                (sept(24), euros("1300.00")),
                (sept(25), euros("1310.00")),
            ],
        )

    def test_consecutive_days_need_no_step(self):
        lines = [line(sept(2), "10.00"), line(sept(3), "10.00")]
        curve = compute([point(sept(1), "0.00")], lines, []).curve(DateRange())
        self.assertEqual([day for day, _ in curve], [sept(1), sept(2), sept(3)])

    def test_a_single_day_is_one_value(self):
        self.assertEqual(compute([point(sept(1), "7.00")], [], []).curve(DateRange()), [(sept(1), euros("7.00"))])

    def test_an_adjustment_is_a_movement_of_the_curve(self):
        points = [point(sept(1), "0.00"), point(sept(9), "25.00")]
        curve = compute(points, [], [adjustment(sept(5), "25.00")]).curve(DateRange())
        self.assertIn((sept(5), euros("25.00")), curve)
        self.assertIn((sept(4), euros("0.00")), curve)


class MonthGapsTests(SimpleTestCase):
    def test_a_gap_to_resolve_has_a_figure_a_pending_one_none(self):
        points = [point(date(2026, 8, 15), "1000.00"), point(sept(20), "1130.00"), point(octo(2), "1000.00")]
        lines = [line(date(2026, 8, 20), "100.00"), line(sept(25), "-10.00")]
        result = compute(points, lines, [adjustment(sept(10), "0.50")])
        august, september, october = result.months(DateRange())
        self.assertEqual((august.gap_to_resolve, august.gaps_to_resolve, august.gap_pending), (0, 0, False))
        self.assertEqual(
            (september.gap_to_resolve, september.gaps_to_resolve, september.gap_pending), (euros("29.50"), 1, False)
        )
        self.assertEqual(september.adjustments, euros("0.50"))
        # « Entrées » and « Sorties » are the lines alone: the adjustment is
        # in « Ajustements » only (review C4).
        self.assertEqual((september.credits, september.debits), (0, euros("-10.00")))
        self.assertEqual((october.gap_to_resolve, october.gaps_to_resolve, october.gap_pending), (0, 0, True))
        self.assertEqual(october.closing_provisional, False)  # its end is a point's day
        self.assertTrue(result.provisional(octo(1)))

    def test_credits_and_debits_are_the_lines_alone_whatever_the_adjustments(self):
        points = [point(sept(1), "1000.00"), point(sept(30), "1000.00")]
        lines = [line(sept(5), "40.00"), line(sept(6), "-25.00"), CLOSING]
        result = compute(points, lines, [adjustment(sept(10), "7.50"), adjustment(sept(20), "-22.50")])
        self.assertTrue(result.gaps[0].agrees)
        september = result.months(DateRange())[0]
        self.assertEqual(
            (september.credits, september.debits, september.adjustments),
            (euros("40.00"), euros("-25.00"), euros("-15.00")),
        )

    def test_two_gaps_that_cancel_out_still_count_two(self):
        # Review C10: one typo between two right balances makes two gaps of
        # opposite sign; their sum is 0, so only their count says the month
        # has gaps to resolve.
        points = [point(sept(1), "1000.00"), point(sept(10), "1300.00"), point(sept(15), "1150.00")]
        lines = [line(sept(5), "100.00"), line(sept(12), "50.00"), line(sept(20), "1.00")]
        result = compute(points, lines, [])
        self.assertEqual([gap.missing for gap in result.to_resolve], [euros("200.00"), euros("-200.00")])
        self.assertEqual(result.gaps[0].suspect, points[1])
        (september,) = result.months(DateRange())
        self.assertEqual((september.gap_to_resolve, september.gaps_to_resolve), (0, 2))


class ReadBalanceTests(SimpleTestCase):
    def test_what_reads(self):
        for typed, read in (
            ("1 234,56", "1234.56"),
            ("1\N{NO-BREAK SPACE}234,56", "1234.56"),
            ("-250", "-250"),
            ("250-", "-250"),
            ("\N{MINUS SIGN}250,5", "-250.50"),
            ("1234.5", "1234.50"),
            ("0", "0"),
            ("12 500", "12500"),
            ("12.500,00", "12500.00"),
            ("1.500.000", "1500000"),
            ("9 999 999 999,99", "9999999999.99"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(read_balance(typed), (euros(read), ""))

    def test_ambiguous_thousands_are_asked_again(self):
        for typed in ("12.500", "-1,500", "1,500-", "\N{MINUS SIGN}1.500", " 12 .500 ", "0,125"):
            with self.subTest(typed=typed):
                self.assertEqual(read_balance(typed), (None, BALANCE_AMBIGUOUS))

    def test_what_does_not_read(self):
        for typed in ("", "   ", "abc", "1e999", "12,5051", "1.234,567", "10000000000", "1 2", "12\x0050", None, 12):
            with self.subTest(typed=typed):
                self.assertEqual(read_balance(typed), (None, BALANCE_UNREADABLE))

    def test_the_words(self):
        self.assertEqual(BALANCE_UNREADABLE, "Solde illisible : tapez un montant comme 1 234,56 ou -250.")
        self.assertEqual(BALANCE_AMBIGUOUS, "Solde ambigu : tapez 12 500 ou 12,50.")

    def test_one_rule_for_both_pages(self):
        from inventory import views as inventory_views

        self.assertIs(inventory_views.AMBIGUOUS_THOUSANDS, common.AMBIGUOUS_THOUSANDS)


class CheckPointDateTests(SimpleTestCase):
    TODAY = octo(2)

    def test_from_2000_to_today(self):
        for value, day in (
            ("2026-10-02", octo(2)),
            (" 2026-10-01 ", octo(1)),
            ("2000-01-01", date(2000, 1, 1)),
            (sept(15), sept(15)),
        ):
            with self.subTest(value=value):
                self.assertEqual(check_point_date(value, today=self.TODAY), (day, ""))

    def test_missing_or_unreadable(self):
        for value in (None, "", "abc", "02/10/2026", "2026-02-30", "2026-10-02\x00", datetime(2026, 10, 1, 9, 0)):
            with self.subTest(value=value):
                self.assertEqual(check_point_date(value, today=self.TODAY), (None, DATE_UNREADABLE))
        self.assertEqual(DATE_UNREADABLE, "Date illisible.")

    def test_impossible(self):
        said = "Date impossible : entre le 01/01/2000 et aujourd'hui (02/10/2026)."
        for value in ("2026-10-03", "1999-12-31", date(2099, 1, 1)):
            with self.subTest(value=value):
                self.assertEqual(check_point_date(value, today=self.TODAY), (None, said))


class LoadQueriesTests(TestCase):
    """`load()` reads the three tables in `QUERIES` queries, whatever the
    history holds."""

    def setUp(self):
        self.built = 0

    def build(self, weeks: int):
        start = date(2026, 1, 5) + timedelta(weeks=self.built)
        for week in range(weeks):
            monday = start + timedelta(weeks=week)
            TreasuryCheckpoint.objects.create(date=monday, balance=euros("1000.00") + week)
            TreasuryAdjustment.objects.create(date=monday + timedelta(days=3), amount=euros("1.50"), reason="Essai")
            for offset in range(5):
                number = (self.built + week) * 10 + offset
                BankTransaction.objects.create(
                    operation_date=monday + timedelta(days=offset),
                    label=f"VIR EXEMPLE {number}",
                    amount=euros("12.30") if offset % 2 else euros("-4.10"),
                    account=ACCOUNT,
                    fingerprint=f"tresorerie-{number}",
                )
        self.built += weeks

    def test_three_times_the_history_costs_no_more_queries(self):
        self.build(1)
        with self.assertNumQueries(treasury.QUERIES):
            treasury.load()
        self.build(3)
        with self.assertNumQueries(treasury.QUERIES):
            result = treasury.load()
        self.assertEqual(treasury.QUERIES, 3)
        self.assertEqual(len(result.points), 4)
        self.assertEqual(len(result.gaps), 3)
        self.assertEqual(len(result.corrections) + len(result.orphans), 4)
        self.assertEqual(result.accounts, 1)

    def test_what_it_reads(self):
        TreasuryCheckpoint.objects.create(date=sept(30), balance=euros("-250.10"))
        first = TreasuryCheckpoint.objects.create(date=sept(1), balance=euros("100.00"))
        made = TreasuryAdjustment.objects.create(date=sept(30), amount=euros("-0.10"), reason="Frais")
        BankTransaction.objects.create(
            operation_date=sept(10), label="PRLV EXEMPLE", amount=euros("-350.00"), fingerprint="tresorerie-a"
        )
        result = treasury.load()
        self.assertEqual(result.points[0], Point(first.pk, sept(1), euros("100.00")))
        self.assertEqual(result.corrections, [Correction(made.pk, sept(30), euros("-0.10"), "Frais")])
        self.assertEqual(result.gaps[0].missing, euros("0.00"))

    def test_an_import_is_dated_by_the_local_day(self):
        line_ = BankTransaction.objects.create(
            operation_date=octo(1), label="CB EXEMPLE", amount=euros("-9.90"), fingerprint="tresorerie-b"
        )
        # 23:30 UTC on 01/10 is 01:30 on 02/10 in Paris: a later day.
        BankTransaction.objects.filter(pk=line_.pk).update(imported_at=datetime(2026, 10, 1, 23, 30, tzinfo=UTC))
        self.assertEqual(treasury.load().complete_through, octo(1))
        BankTransaction.objects.filter(pk=line_.pk).update(imported_at=datetime(2026, 10, 1, 21, 30, tzinfo=UTC))
        self.assertEqual(treasury.load().complete_through, sept(30))

    def test_a_frozen_today_does_not_redate_the_imports(self):
        line_ = BankTransaction.objects.create(
            operation_date=octo(1), label="CB EXEMPLE", amount=euros("-9.90"), fingerprint="tresorerie-c"
        )
        BankTransaction.objects.filter(pk=line_.pk).update(imported_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
        with mock.patch("bank.treasury.timezone.localdate", return_value=date(2030, 1, 1)):
            self.assertEqual(treasury.load().complete_through, sept(30))


class ModelTests(TestCase):
    def refusals(self, record, *, whole: bool = False) -> dict:
        """What `clean` says - in French; `full_clean` adds Django's own
        (English) refusal of a figure wider than its column."""
        with self.assertRaises(ValidationError) as caught:
            record.full_clean() if whole else record.clean()
        return caught.exception.message_dict

    def test_a_point_is_refused_in_french_outside_its_bounds(self):
        self.assertEqual(
            self.refusals(TreasuryCheckpoint(date=date(1999, 12, 31), balance=euros("10000000000.00"))),
            {
                "date": ["Date hors limites : entre le 01/01/2000 et le 31/12/2099."],
                "balance": [f"Solde hors limites : 9{NBSP}999{NBSP}999{NBSP}999.99 € au plus, en plus ou en moins."],
            },
        )
        self.assertIn("date", self.refusals(TreasuryCheckpoint(date=date(2100, 1, 1), balance=euros("1.00"))))
        TreasuryCheckpoint(date=sept(1), balance=euros("-9999999999.99")).full_clean()
        TreasuryCheckpoint(date=sept(1), balance=euros("0.00")).full_clean()
        self.assertIn("balance", self.refusals(TreasuryCheckpoint(date=sept(1), balance=euros("1e10")), whole=True))

    def test_an_adjustment_is_refused_in_french(self):
        self.assertEqual(
            self.refusals(TreasuryAdjustment(date=sept(1), amount=euros("0.00"))), {"amount": [TREASURY_AMOUNT_ZERO]}
        )
        self.assertEqual(TREASURY_AMOUNT_ZERO, "Un ajustement de 0 € ne change rien : tapez un montant.")
        self.assertEqual(
            self.refusals(TreasuryAdjustment(date=date(2100, 1, 1), amount=euros("-10000000000.00"))),
            {
                "date": ["Date hors limites : entre le 01/01/2000 et le 31/12/2099."],
                "amount": [f"Montant hors limites : 9{NBSP}999{NBSP}999{NBSP}999.99 € au plus, en plus ou en moins."],
            },
        )
        TreasuryAdjustment(date=sept(1), amount=euros("-0.01")).full_clean()

    def test_a_value_its_field_refuses_reaches_clean_without_a_crash(self):
        # full_clean calls clean() even when a field failed, with the raw value.
        refused = self.refusals(TreasuryCheckpoint(date="hier", balance="beaucoup"), whole=True)
        self.assertEqual(set(refused), {"date", "balance"})
        refused = self.refusals(TreasuryAdjustment(date=sept(1), amount=Decimal("NaN")), whole=True)
        self.assertEqual(set(refused), {"amount"})

    def test_a_reference_of_its_own_and_one_point_a_day(self):
        first = TreasuryAdjustment.objects.create(date=sept(1), amount=euros("5.00"))
        second = TreasuryAdjustment.objects.create(date=sept(1), amount=euros("5.00"))
        self.assertRegex(first.reference, r"\A[0-9a-f]{16}\Z")
        self.assertNotEqual(first.reference, second.reference)
        self.assertRegex(new_reference(), r"\A[0-9a-f]{16}\Z")
        self.assertEqual(first.reason, "")
        TreasuryCheckpoint.objects.create(date=sept(1), balance=euros("1.00"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TreasuryCheckpoint.objects.create(date=sept(1), balance=euros("2.00"))

    def test_the_words_of_the_fields(self):
        self.assertEqual(
            [TreasuryCheckpoint._meta.get_field(name).verbose_name for name in ("date", "balance")], ["date", "solde"]
        )
        self.assertEqual(
            [
                TreasuryAdjustment._meta.get_field(name).verbose_name
                for name in ("reference", "date", "amount", "reason")
            ],
            ["référence", "date", "montant", "raison"],
        )


class MigrationTests(SimpleTestCase):
    def test_two_empty_tables_and_nothing_else(self):
        from django.db import migrations

        migration = importlib.import_module("bank.migrations.0008_treasury")
        self.assertEqual(migration.Migration.dependencies, [("bank", "0007_statement_formats")])
        operations = migration.Migration.operations
        self.assertEqual([type(one) for one in operations], [migrations.CreateModel, migrations.CreateModel])
        self.assertEqual({one.name for one in operations}, {"TreasuryCheckpoint", "TreasuryAdjustment"})
        self.assertIn("Reversing drops", migration.__doc__)
