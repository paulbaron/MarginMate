"""Tests for the L'Addition Z digital plumbing.

The back office refuses a date range longer than two years, so pulling three
years of history means several consecutive reports. Getting that split wrong
is silently expensive: an overlap double-counts every product in the shared
days, and a gap loses them - and either way the totals still look plausible.
"""

from datetime import date, timedelta

from django.test import SimpleTestCase

from recipes.pos.laddition import MAX_RANGE_DAYS, date_windows


def windows(start, end, max_days=MAX_RANGE_DAYS):
    return list(date_windows(date.fromisoformat(start), date.fromisoformat(end), max_days))


class DateWindowTests(SimpleTestCase):
    def test_a_short_range_is_one_window(self):
        self.assertEqual(
            windows("2026-01-01", "2026-03-31"),
            [(date(2026, 1, 1), date(2026, 3, 31))],
        )

    def test_a_single_day(self):
        self.assertEqual(windows("2026-01-01", "2026-01-01"), [(date(2026, 1, 1), date(2026, 1, 1))])

    def test_an_inverted_range_yields_nothing(self):
        self.assertEqual(windows("2026-03-31", "2026-01-01"), [])

    def test_exactly_the_limit_stays_one_window(self):
        start = date(2024, 1, 1)
        end = start + timedelta(days=MAX_RANGE_DAYS - 1)
        self.assertEqual(list(date_windows(start, end)), [(start, end)])

    def test_one_day_past_the_limit_splits(self):
        start = date(2024, 1, 1)
        end = start + timedelta(days=MAX_RANGE_DAYS)
        result = list(date_windows(start, end))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], (start, start + timedelta(days=MAX_RANGE_DAYS - 1)))
        self.assertEqual(result[1], (end, end))

    def test_no_window_exceeds_the_limit(self):
        for first, last in windows("2019-01-01", "2026-09-02"):
            with self.subTest(window=(first, last)):
                self.assertLessEqual((last - first).days + 1, MAX_RANGE_DAYS)

    def test_windows_are_contiguous_with_no_gap_and_no_overlap(self):
        """Every day in the range belongs to exactly one window - anything
        else quietly changes the totals."""
        result = windows("2019-01-01", "2026-09-02")
        for (_, previous_end), (next_start, _) in zip(result, result[1:]):
            self.assertEqual(next_start, previous_end + timedelta(days=1))

    def test_the_windows_cover_the_whole_range_exactly(self):
        start, end = date(2019, 1, 1), date(2026, 9, 2)
        result = list(date_windows(start, end))
        self.assertEqual(result[0][0], start)
        self.assertEqual(result[-1][1], end)
        covered = sum((last - first).days + 1 for first, last in result)
        self.assertEqual(covered, (end - start).days + 1)

    def test_the_users_own_three_year_history_splits_into_two_reports(self):
        """The period the two exports already cover."""
        result = windows("2023-12-01", "2026-09-02")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], date(2023, 12, 1))
        self.assertEqual(result[-1][1], date(2026, 9, 2))

    def test_a_leap_day_does_not_push_a_window_over_the_limit(self):
        for first, last in windows("2024-02-01", "2028-02-01"):
            with self.subTest(window=(first, last)):
                self.assertLessEqual((last - first).days + 1, MAX_RANGE_DAYS)

    def test_a_smaller_window_size_can_be_asked_for(self):
        result = windows("2026-01-01", "2026-01-10", max_days=4)
        self.assertEqual(
            result,
            [
                (date(2026, 1, 1), date(2026, 1, 4)),
                (date(2026, 1, 5), date(2026, 1, 8)),
                (date(2026, 1, 9), date(2026, 1, 10)),
            ],
        )
