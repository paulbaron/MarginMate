"""The window « du … au … » every list is read through (common.date_range).

Four pages read it - « Produits & charges », « Achats », « Ventes » and la
Banque - so it is read once, here, and the four cannot disagree about what
"between these two dates" means. The decisions it makes, each of which was a
way to be silently wrong:

* **both ends included**, unlike the half-open window the stock pages slice
  with: a person asking « au 28 » means the 28th, and a document dated that
  day must not vanish;
* **a date that is no date is no window**, never a 500: this arrives from a
  query string, so a stale bookmark and a hand-typed URL both land here;
* **two dates the wrong way round are swapped**, not answered with an empty
  page;
* **an undated row is in no window**: it has no date to be between two
  others, and « Sans date » is where those are looked at.
"""

from datetime import date

from django.test import SimpleTestCase
from django.test.client import RequestFactory

from common import DateRange, date_range, read_date


class ReadDateTests(SimpleTestCase):
    def test_iso_dates_only(self):
        self.assertEqual(read_date("2026-02-28"), date(2026, 2, 28))

    def test_anything_else_is_no_date(self):
        # parse_date returns None on a shape it doesn't know, and RAISES on
        # one it knows that is no date ("2026-02-30", "2026-13-01"). Both
        # are a query parameter somebody typed, so both are simply no date.
        for value in ("", None, "hier", "28/02/2026", "2026-02-30", "2026-13-01", "2026-2", "  "):
            with self.subTest(value=value):
                self.assertIsNone(read_date(value))

    def test_surrounding_spaces(self):
        self.assertEqual(read_date(" 2026-02-28 "), date(2026, 2, 28))


class DateRangeReadingTests(SimpleTestCase):
    def range_for(self, query: str) -> DateRange:
        return date_range(RequestFactory().get(f"/?{query}"))

    def test_both_ends(self):
        window = self.range_for("du=2026-02-01&au=2026-02-28")
        self.assertEqual((window.start, window.end), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertTrue(window)

    def test_one_end_is_still_a_window(self):
        since = self.range_for("du=2026-02-01")
        self.assertEqual((since.start, since.end), (date(2026, 2, 1), None))
        self.assertTrue(since)
        until = self.range_for("au=2026-02-28")
        self.assertEqual((until.start, until.end), (None, date(2026, 2, 28)))
        self.assertTrue(until)

    def test_nothing_asked_is_no_window(self):
        self.assertFalse(self.range_for(""))
        self.assertFalse(self.range_for("du=&au="))

    def test_garbage_falls_back_to_no_window(self):
        self.assertFalse(self.range_for("du=hier&au=demain"))
        # One end readable, the other not: the readable one still counts.
        half = self.range_for("du=2026-02-01&au=pas-une-date")
        self.assertEqual((half.start, half.end), (date(2026, 2, 1), None))

    def test_backwards_dates_are_swapped(self):
        window = self.range_for("du=2026-02-28&au=2026-02-01")
        self.assertEqual((window.start, window.end), (date(2026, 2, 1), date(2026, 2, 28)))

    def test_one_day(self):
        window = self.range_for("du=2026-02-14&au=2026-02-14")
        self.assertTrue(window.holds(date(2026, 2, 14)))

    def test_other_parameter_names(self):
        request = RequestFactory().get("/?depuis=2026-02-01&jusqu=2026-02-28")
        window = date_range(request, "depuis", "jusqu")
        self.assertEqual((window.start, window.end), (date(2026, 2, 1), date(2026, 2, 28)))


class HoldsTests(SimpleTestCase):
    window = DateRange(date(2026, 2, 1), date(2026, 2, 28))

    def test_both_ends_included(self):
        self.assertTrue(self.window.holds(date(2026, 2, 1)))
        self.assertTrue(self.window.holds(date(2026, 2, 28)))

    def test_outside(self):
        self.assertFalse(self.window.holds(date(2026, 1, 31)))
        self.assertFalse(self.window.holds(date(2026, 3, 1)))

    def test_open_ended(self):
        since = DateRange(start=date(2026, 2, 1))
        self.assertFalse(since.holds(date(2026, 1, 31)))
        self.assertTrue(since.holds(date(2030, 1, 1)))
        until = DateRange(end=date(2026, 2, 28))
        self.assertTrue(until.holds(date(2000, 1, 1)))
        self.assertFalse(until.holds(date(2026, 3, 1)))

    def test_undated_is_in_no_window(self):
        self.assertFalse(self.window.holds(None))
        self.assertFalse(DateRange(start=date(2026, 2, 1)).holds(None))
        # No window at all filters nothing, undated rows included - the way
        # `limit` leaves the queryset alone.
        self.assertTrue(DateRange().holds(None))
        self.assertTrue(DateRange().holds(date(2026, 2, 1)))


class FormValuesTests(SimpleTestCase):
    def test_values_for_the_date_inputs(self):
        window = DateRange(date(2026, 2, 1), date(2026, 2, 28))
        self.assertEqual(window.start_value, "2026-02-01")
        self.assertEqual(window.end_value, "2026-02-28")
        self.assertEqual(DateRange().start_value, "")
        self.assertEqual(DateRange().end_value, "")

    def test_parameters_carry_the_window_on_a_link(self):
        self.assertEqual(
            DateRange(date(2026, 2, 1), date(2026, 2, 28)).parameters,
            {"du": "2026-02-01", "au": "2026-02-28"},
        )
        self.assertEqual(DateRange(start=date(2026, 2, 1)).parameters, {"du": "2026-02-01"})
        self.assertEqual(DateRange().parameters, {})
