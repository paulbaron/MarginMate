"""Months, days and public holidays - in French, from this app's own
constants (LANGUAGE_CODE is en-us: Django would say « June »)."""

from datetime import date, timedelta

from django.test import SimpleTestCase

from staff.timesheet import (
    DAY_NAMES,
    MONTH_NAMES,
    day_name,
    easter_sunday,
    first_of_month,
    french_public_holidays,
    month_days,
    month_holidays,
    month_label,
    month_name,
    month_slug,
    month_title,
    next_month,
    parse_month,
    previous_month,
    span_label,
)


class MonthDaysTests(SimpleTestCase):
    def test_every_length_of_month(self):
        for month, length in (
            (date(2026, 1, 1), 31),
            (date(2026, 2, 1), 28),
            (date(2026, 4, 1), 30),
            (date(2026, 6, 1), 30),
            (date(2026, 8, 1), 31),
            (date(2026, 12, 1), 31),
        ):
            with self.subTest(month=month):
                days = month_days(month)
                self.assertEqual(len(days), length)
                self.assertEqual(days[0], month)
                self.assertEqual(days[-1] + timedelta(days=1), next_month(month))

    def test_february_of_leap_and_common_years(self):
        """A year divisible by 100 is no leap year unless divisible by 400."""
        for year, length in ((1900, 28), (2000, 29), (2024, 29), (2026, 28), (2028, 29), (2100, 28)):
            with self.subTest(year=year):
                self.assertEqual(len(month_days(date(year, 2, 1))), length)

    def test_any_day_of_the_month_names_the_month(self):
        self.assertEqual(month_days(date(2026, 6, 17)), month_days(date(2026, 6, 1)))
        self.assertEqual(first_of_month(date(2026, 6, 30)), date(2026, 6, 1))

    def test_the_month_before_and_after_across_the_year(self):
        self.assertEqual(previous_month(date(2026, 1, 1)), date(2025, 12, 1))
        self.assertEqual(next_month(date(2026, 12, 1)), date(2027, 1, 1))
        self.assertEqual(next_month(date(2026, 1, 31)), date(2026, 2, 1))
        self.assertEqual(previous_month(date(2026, 3, 31)), date(2026, 2, 1))
        # From each month of a leap and a common year, one step lands on the
        # 1st of the next - never skips one.
        for year in (2026, 2028):
            for month in range(1, 13):
                with self.subTest(year=year, month=month):
                    step = next_month(date(year, month, 1))
                    self.assertEqual((step.year * 12 + step.month) - (year * 12 + month), 1)
                    self.assertEqual(previous_month(step), date(year, month, 1))


class ParseMonthTests(SimpleTestCase):
    def test_the_address_s_month(self):
        self.assertEqual(parse_month("2026-06"), date(2026, 6, 1))
        self.assertEqual(month_slug(date(2026, 6, 1)), "2026-06")
        self.assertEqual(parse_month(month_slug(date(2026, 12, 1))), date(2026, 12, 1))

    def test_anything_else_is_no_month_never_an_exception(self):
        for text in (
            "2026-6", "2026-13", "2026-00", "26-06", "2026-06-01", "2026/06", "juin", "", " 2026-06",
            None, 202606, "２０２６-06", "0001-01", "1899-12", "3000-01",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_month(text))

    def test_the_first_and_last_years_have_a_month_on_either_side(self):
        """The page links to the month before and after: those must exist."""
        first, last = parse_month("1900-01"), parse_month("2999-12")
        self.assertEqual(previous_month(first), date(1899, 12, 1))
        self.assertEqual(next_month(last), date(3000, 1, 1))


class FrenchNamesTests(SimpleTestCase):
    def test_the_whole_name_of_the_day_then_its_number(self):
        self.assertEqual(day_name(date(2026, 6, 1)), "Lundi 1")
        self.assertEqual(day_name(date(2026, 8, 4)), "Mardi 4")
        self.assertEqual(day_name(date(2026, 6, 28)), "Dimanche 28")
        self.assertEqual([day_name(date(2026, 6, day)).split()[0] for day in range(1, 8)], list(DAY_NAMES))

    def test_month_names_and_the_elision_before_a_vowel(self):
        self.assertEqual(month_name(date(2026, 8, 1)), "août")
        self.assertEqual(month_label(date(2026, 2, 1)), "février 2026")
        self.assertEqual(month_title(date(2026, 6, 1)), "Mois de juin 2026")
        self.assertEqual(month_title(date(2026, 8, 1)), "Mois d'août 2026")
        self.assertEqual(month_title(date(2026, 4, 1)), "Mois d'avril 2026")
        self.assertEqual(month_title(date(2026, 10, 1)), "Mois d'octobre 2026")
        titles = [month_title(date(2026, month, 1)) for month in range(1, 13)]
        elided = [name for name, title in zip(MONTH_NAMES, titles, strict=True) if title.startswith("Mois d'")]
        self.assertEqual(elided, ["avril", "août", "octobre"])


class SpanLabelTests(SimpleTestCase):
    def test_some_days_in_a_sentence(self):
        self.assertEqual(span_label(date(2026, 6, 2), date(2026, 6, 2)), "le 2 juin 2026")
        self.assertEqual(span_label(date(2026, 6, 11), date(2026, 6, 18)), "du 11 au 18 juin 2026")
        self.assertEqual(span_label(date(2026, 6, 1), date(2026, 6, 3)), "du 1er au 3 juin 2026")
        self.assertEqual(span_label(date(2026, 8, 1), date(2026, 8, 1)), "le 1er août 2026")
        self.assertEqual(span_label(date(2026, 6, 29), date(2026, 7, 5)), "du 29 juin au 5 juillet 2026")
        self.assertEqual(
            span_label(date(2026, 12, 28), date(2027, 1, 3)), "du 28 décembre 2026 au 3 janvier 2027"
        )

    def test_the_wrong_way_round_reads_the_right_way(self):
        self.assertEqual(span_label(date(2026, 6, 18), date(2026, 6, 11)), "du 11 au 18 juin 2026")


class EasterTests(SimpleTestCase):
    def test_known_easter_sundays(self):
        """Pinned from the published calendars, including both ends of the
        range: 22 March (1818, 2285) and 25 April (1943, 2038)."""
        known = {
            1818: (3, 22), 1900: (4, 15), 1943: (4, 25), 1975: (3, 30), 2000: (4, 23), 2008: (3, 23),
            2011: (4, 24), 2019: (4, 21), 2024: (3, 31), 2025: (4, 20), 2026: (4, 5), 2027: (3, 28),
            2028: (4, 16), 2029: (4, 1), 2030: (4, 21), 2038: (4, 25), 2285: (3, 22),
        }
        for year, (month, day) in known.items():
            with self.subTest(year=year):
                self.assertEqual(easter_sunday(year), date(year, month, day))

    def test_always_a_sunday_between_22_march_and_25_april(self):
        for year in range(1900, 3000):
            easter = easter_sunday(year)
            self.assertEqual(easter.weekday(), 6, year)
            self.assertTrue(date(year, 3, 22) <= easter <= date(year, 4, 25), year)


class PublicHolidaysTests(SimpleTestCase):
    def test_the_eleven_holidays_of_2026(self):
        self.assertEqual(
            french_public_holidays(2026),
            {
                date(2026, 1, 1): "Jour de l'an",
                date(2026, 4, 6): "Lundi de Pâques",
                date(2026, 5, 1): "Fête du Travail",
                date(2026, 5, 8): "Victoire 1945",
                date(2026, 5, 14): "Ascension",
                date(2026, 5, 25): "Lundi de Pentecôte",
                date(2026, 7, 14): "Fête nationale",
                date(2026, 8, 15): "Assomption",
                date(2026, 11, 1): "Toussaint",
                date(2026, 11, 11): "Armistice",
                date(2026, 12, 25): "Noël",
            },
        )

    def test_the_holidays_that_move_with_easter(self):
        for year, easter_monday, ascension, whit_monday in (
            (2024, date(2024, 4, 1), date(2024, 5, 9), date(2024, 5, 20)),
            (2025, date(2025, 4, 21), date(2025, 5, 29), date(2025, 6, 9)),
            (2027, date(2027, 3, 29), date(2027, 5, 6), date(2027, 5, 17)),
            (2038, date(2038, 4, 26), date(2038, 6, 3), date(2038, 6, 14)),
        ):
            with self.subTest(year=year):
                holidays = french_public_holidays(year)
                self.assertEqual(holidays[easter_monday], "Lundi de Pâques")
                self.assertEqual(holidays[ascension], "Ascension")
                self.assertEqual(holidays[whit_monday], "Lundi de Pentecôte")
                self.assertEqual(easter_monday.weekday(), 0)
                self.assertEqual(ascension.weekday(), 3)
                self.assertEqual(whit_monday.weekday(), 0)

    def test_an_ascension_on_1_or_8_may_keeps_both_names(self):
        """A dict keyed by date would silently drop one of the two."""
        self.assertEqual(french_public_holidays(2008)[date(2008, 5, 1)], "Fête du Travail et Ascension")
        self.assertEqual(french_public_holidays(1975)[date(1975, 5, 8)], "Victoire 1945 et Ascension")
        self.assertEqual(len(french_public_holidays(2008)), 10)

    def test_the_holidays_of_one_month(self):
        self.assertEqual(month_holidays(date(2026, 6, 1)), {})
        self.assertEqual(
            month_holidays(date(2026, 5, 1)),
            {
                date(2026, 5, 1): "Fête du Travail",
                date(2026, 5, 8): "Victoire 1945",
                date(2026, 5, 14): "Ascension",
                date(2026, 5, 25): "Lundi de Pentecôte",
            },
        )
        self.assertEqual(month_holidays(date(2026, 8, 20)), {date(2026, 8, 15): "Assomption"})

    def test_what_a_caller_does_to_the_answer_does_not_change_the_next_one(self):
        french_public_holidays(2026).clear()
        self.assertEqual(len(french_public_holidays(2026)), 11)
