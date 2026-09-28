"""Hours as a person types them, and as the page and the PDF print them."""

from decimal import Decimal

from django.test import SimpleTestCase

from staff.tests.support import employee
from staff.timesheet import (
    HoursError,
    checked_hours,
    format_hours,
    format_hours_difference,
    parse_hours,
    parse_optional_hours,
    week_summary,
)


class ParseHoursTests(SimpleTestCase):
    def test_every_way_of_writing_seven_and_a_half(self):
        for typed in ("7,5", "7.5", "7h30", "7H30", "7 h 30", "7:30", " 7,50 ", "07h30", "7 h 30"):
            with self.subTest(typed=typed):
                self.assertEqual(parse_hours(typed), Decimal("7.5"))

    def test_whole_hours_and_quarters(self):
        self.assertEqual(parse_hours("7"), Decimal("7"))
        self.assertEqual(parse_hours("7h"), Decimal("7"))
        self.assertEqual(parse_hours("7h15"), Decimal("7.25"))
        self.assertEqual(parse_hours("7h45"), Decimal("7.75"))
        self.assertEqual(parse_hours("0h06"), Decimal("0.1"))
        self.assertEqual(parse_hours(",5"), Decimal("0.5"))
        self.assertEqual(parse_hours("7."), Decimal("7"))

    def test_blank_is_zero(self):
        for typed in ("", "   ", None, "0", "0,00", "0h00"):
            with self.subTest(typed=typed):
                self.assertEqual(parse_hours(typed), Decimal("0"))

    def test_the_limits_of_a_day(self):
        self.assertEqual(parse_hours("24"), Decimal("24"))
        self.assertEqual(parse_hours("24h00"), Decimal("24"))
        self.assertEqual(parse_hours("0,01"), Decimal("0.01"))
        for typed in ("24,01", "24h01", "25", "100"):
            with self.subTest(typed=typed), self.assertRaisesMessage(HoursError, "24 h"):
                parse_hours(typed)

    def test_a_negative_figure_is_refused(self):
        for typed in ("-1", "-0,5", "−7", "- 7"):
            with self.subTest(typed=typed), self.assertRaisesMessage(HoursError, "négatives"):
                parse_hours(typed)

    def test_more_than_two_decimals_is_refused_not_rounded(self):
        with self.assertRaisesMessage(HoursError, "deux décimales"):
            parse_hours("7,555")

    def test_minutes_that_do_not_come_to_a_hundredth_are_refused(self):
        """7h20 is 7,333… h: rounded, the sheet would print what nobody typed."""
        for typed in ("7h20", "7h10", "7:05"):
            with self.subTest(typed=typed), self.assertRaisesMessage(HoursError, "centièmes"):
                parse_hours(typed)

    def test_garbage_is_refused_with_the_formats_that_work(self):
        for typed in (
            "abc", "7h5", "7h75", "7,5,5", "1e1", "NaN", "Infinity", "+7", "7 heures", "٣", "７", "7:3",
            "9" * 400, "h30", ":30",
        ):
            with self.subTest(typed=typed), self.assertRaises(HoursError):
                parse_hours(typed)
        with self.assertRaisesMessage(HoursError, "7h30"):
            parse_hours("sept")

    def test_digits_a_space_apart_are_refused_not_glued_together(self):
        """« 1 5 » - a slip for 1,5 - was read as 15 h, and « 0 5 » as 5 h,
        without a word (review, 28/09): the spaces were taken out before
        the number was read. Any space counts, the typographer's too."""
        for typed in (
            "1 5", "0 5", "2 4", "7 5", "1\t5", "1\N{NO-BREAK SPACE}2", "1\N{NARROW NO-BREAK SPACE}5", "7,5 0",
            "7 h 3 0",
        ):
            with self.subTest(typed=typed), self.assertRaises(HoursError) as caught:
                parse_hours(typed, "Mardi 2")
            self.assertTrue(str(caught.exception).startswith("Mardi 2 : « "), str(caught.exception))
            self.assertIn("un espace entre deux chiffres", str(caught.exception))

    def test_a_space_beside_a_unit_or_a_comma_still_reads(self):
        for typed, hours in (
            ("7 h 30", "7.5"),
            ("7h 30", "7.5"),
            ("7 h", "7"),
            ("7 : 30", "7.5"),
            ("7 , 5", "7.5"),
            (" 7,5 ", "7.5"),
            ("7\N{NARROW NO-BREAK SPACE}h\N{NARROW NO-BREAK SPACE}30", "7.5"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(parse_hours(typed), Decimal(hours))

    def test_minutes_past_59_say_so(self):
        with self.assertRaisesMessage(HoursError, "00 à 59"):
            parse_hours("7h75")

    def test_the_message_names_the_day(self):
        with self.assertRaises(HoursError) as caught:
            parse_hours("abc", "Mardi 4")
        self.assertTrue(str(caught.exception).startswith("Mardi 4 : « abc »"), str(caught.exception))
        with self.assertRaises(HoursError) as caught:
            parse_hours("-2", "Lundi")
        self.assertEqual(str(caught.exception), "Lundi : les heures ne peuvent pas être négatives.")

    def test_without_a_day_the_message_is_a_sentence(self):
        with self.assertRaises(HoursError) as caught:
            parse_hours("-2")
        self.assertEqual(str(caught.exception), "Les heures ne peuvent pas être négatives.")

    def test_a_very_long_entry_is_cut_in_the_message(self):
        with self.assertRaises(HoursError) as caught:
            parse_hours("x" * 500, "Mardi 4")
        self.assertLess(len(str(caught.exception)), 150)

    def test_an_hours_error_is_a_value_error(self):
        """A view catching ValueError catches it: never a 500."""
        self.assertTrue(issubclass(HoursError, ValueError))


class ParseOptionalHoursTests(SimpleTestCase):
    def test_blank_is_not_said_rather_than_zero(self):
        """« Travail du 1 au 14 » with the hours left blank must not put
        0 h on fourteen days."""
        for typed in ("", "  ", None, " "):
            with self.subTest(typed=typed):
                self.assertIsNone(parse_optional_hours(typed))

    def test_anything_else_is_read_like_any_hours(self):
        self.assertEqual(parse_optional_hours("0"), Decimal("0"))
        self.assertEqual(parse_optional_hours("6h30"), Decimal("6.5"))
        with self.assertRaisesMessage(HoursError, "Heures : "):
            parse_optional_hours("abc", "Heures")


class CheckedHoursTests(SimpleTestCase):
    def test_a_decimal_or_an_int(self):
        self.assertEqual(checked_hours(Decimal("7.5")), Decimal("7.5"))
        self.assertEqual(checked_hours(8), Decimal("8"))
        self.assertEqual(checked_hours(0), Decimal("0"))
        self.assertEqual(checked_hours("7h30"), Decimal("7.5"))
        self.assertEqual(checked_hours(None), Decimal("0"))

    def test_out_of_a_day_is_refused(self):
        for value in (Decimal("-0.01"), Decimal("24.01"), Decimal("7.555"), -1, 25, Decimal("NaN"), Decimal("Inf")):
            with self.subTest(value=value), self.assertRaises(HoursError):
                checked_hours(value)

    def test_a_float_is_refused_outright(self):
        with self.assertRaises(TypeError):
            checked_hours(7.5)
        with self.assertRaises(TypeError):
            checked_hours(True)


class FormatHoursTests(SimpleTestCase):
    def test_a_comma_and_no_trailing_zero(self):
        for value, text in (
            (Decimal("7.00"), "7"),
            (Decimal("7.50"), "7,5"),
            (Decimal("7.25"), "7,25"),
            (Decimal("0.05"), "0,05"),
            (Decimal("24"), "24"),
            (Decimal("163"), "163"),
            (Decimal("100.00"), "100"),
            (8, "8"),
        ):
            with self.subTest(value=value):
                self.assertEqual(format_hours(value), text)

    def test_zero_prints_zero_and_nothing_prints_nothing(self):
        self.assertEqual(format_hours(Decimal("0.00")), "0")
        self.assertEqual(format_hours(Decimal("-0")), "0")
        self.assertEqual(format_hours(0), "0")
        self.assertEqual(format_hours(None), "")
        self.assertEqual(format_hours(""), "")

    def test_what_is_typed_comes_back_as_it_prints(self):
        for typed in ("7", "7,5", "7,25", "0", "24"):
            with self.subTest(typed=typed):
                self.assertEqual(format_hours(parse_hours(typed)), typed)

    def test_a_negative_figure_has_a_minus_sign(self):
        self.assertEqual(format_hours(Decimal("-3.5")), "−3,5")

    def test_a_difference_says_its_sign(self):
        self.assertEqual(format_hours_difference(Decimal("3")), "+3 h")
        self.assertEqual(format_hours_difference(Decimal("-8")), "−8 h")
        self.assertEqual(format_hours_difference(Decimal("-0.5")), "−0,5 h")
        self.assertEqual(format_hours_difference(Decimal("0.00")), "0 h")


class WeekSummaryTests(SimpleTestCase):
    def test_neighbouring_days_with_the_same_hours_are_one_run(self):
        self.assertEqual(week_summary(employee(save=False)), "Ma 7,5 · Me 6 · Je–Sa 7,5 · 36 h / semaine")

    def test_a_five_day_week(self):
        hours = {f"{day}_hours": Decimal("7") for day in ("monday", "tuesday", "wednesday", "thursday", "friday")}
        self.assertEqual(week_summary(employee(save=False, **hours)), "Lu–Ve 7 · 35 h / semaine")

    def test_every_day_different_and_a_sunday(self):
        person = employee(
            save=False, monday_hours=Decimal("7"), tuesday_hours=Decimal("7.5"), sunday_hours=Decimal("4.25")
        )
        self.assertEqual(week_summary(person), "Lu 7 · Ma 7,5 · Di 4,25 · 18,75 h / semaine")

    def test_two_runs_of_the_same_hours_apart(self):
        person = employee(save=False, monday_hours=Decimal("6"), tuesday_hours=Decimal("6"), friday_hours=Decimal("6"))
        self.assertEqual(week_summary(person), "Lu–Ma 6 · Ve 6 · 18 h / semaine")

    def test_no_typical_week(self):
        self.assertEqual(week_summary(employee(save=False, monday_hours=Decimal("0"))), "0 h / semaine")
