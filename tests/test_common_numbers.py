"""common.read_number and common.read_amount: a number as a person types it.

Moved from returnables/patterns.py (which re-exports both: a seller's slip
is read through them, returnables/tests/test_patterns.py) when « Combler les
écarts » needed the same reading for its « Montant à encaisser » field. What
is pinned here is what that field sees - what the owner types, and what a form
can post: a Decimal exact to the cent, or None, never a float,
never rounded. Whether the amount is above zero and under the page's
ceiling is the view's (tests/test_views_smoke.py). Invented data.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from common import read_amount, read_number
from returnables import patterns


class OneReaderTests(SimpleTestCase):
    def test_the_slip_and_the_amount_field_share_it(self):
        self.assertIs(patterns.read_amount, read_amount)
        self.assertIs(patterns.read_number, read_number)


class AnAmountTypedTests(SimpleTestCase):
    def test_what_is_read(self):
        cases = {
            "420": "420.00",
            "420,50": "420.50",
            "420.50": "420.50",
            "420,5": "420.50",
            " 420,50 ": "420.50",
            "1 234,50": "1234.50",
            "1\N{NO-BREAK SPACE}234,50": "1234.50",
            "1\N{NARROW NO-BREAK SPACE}234,50": "1234.50",
            "1.234,50": "1234.50",
            "1,234.50": "1234.50",
            "0,01": "0.01",
            # Zeros past the cent change nothing.
            "420,500": "420.50",
            # Read: the page then says « au plus » / « supérieur à zéro ».
            "10000,01": "10000.01",
            "-5": "-5.00",
            "0": "0.00",
            "-0": "0.00",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                amount = read_amount(text)
                self.assertIsInstance(amount, Decimal)
                self.assertEqual(amount, Decimal(expected))
                # Exactly two places, as the page prints and the planner
                # turns into cents.
                self.assertEqual(str(amount), expected)

    def test_what_is_not_read(self):
        for text in (
            None,
            420,
            "",
            "   ",
            "abc",
            "420 €",
            "+420",
            # What Decimal() itself would read, and the page must not.
            "Infinity",
            "-Infinity",
            "inf",
            "NaN",
            "nan",
            "sNaN",
            "1e999",
            "1E5",
            # More than two decimals: never rounded to an amount nobody typed.
            "420,505",
            "0,001",
            "12,345",
            "1 234,567",
            # Not ASCII digits.
            "\N{SUPERSCRIPT TWO}",
            "\N{ARABIC-INDIC DIGIT THREE}",
            # Wider than an amount's column, and longer than anything typed.
            "10000000000",
            "1" * 41,
        ):
            with self.subTest(text=text):
                self.assertIsNone(read_amount(text))

    def test_a_space_inside_a_number_only_groups_thousands(self):
        # A euro and its cents typed a space apart - « 42 50 » for 42,50 -
        # once read as 4 250,00 € (every space was dropped) and the page
        # planned sales for that. A space separates thousands, groups of
        # exactly three, as « . » and « , » already did; else no amount.
        for text in ("42 50", "4 2 0", "1 23,50", "1234 5"):
            with self.subTest(text=text):
                self.assertIsNone(read_amount(text))
        for text, expected in (("1 234", "1234.00"), ("12 345 678,90", "12345678.90")):
            with self.subTest(text=text):
                self.assertEqual(read_amount(text), Decimal(expected))

    def test_an_apostrophe_groups_thousands_like_a_space(self):
        # The Swiss way, « 1'234,50 »: the same rule as a space - between
        # groups of exactly three, never anywhere else.
        for text, expected in (("1'234,50", "1234.50"), ("12'345'678", "12345678.00"), ("-1'234", "-1234.00")):
            with self.subTest(text=text):
                self.assertEqual(read_amount(text), Decimal(expected))
        for text in ("42'50", "1'23,50", "1234'5", "'1234", "1''234,50x"):
            with self.subTest(text=text):
                self.assertIsNone(read_amount(text))

    def test_one_separator_is_the_decimal_mark_whatever_follows(self):
        # « 10.000 » and « 1,500 » are read as 10 and 1,5 here - the amount
        # field of « Combler les écarts » asks them again rather than plan
        # for either reading (tests/test_views_smoke.py); a slip prints cents.
        for text, expected in (("10.000", "10.00"), ("1,500", "1.50"), ("2.000", "2.00")):
            with self.subTest(text=text):
                self.assertEqual(read_amount(text), Decimal(expected))
        # The same separator twice groups thousands.
        self.assertEqual(read_amount("1.000.000"), Decimal("1000000.00"))
        self.assertIsNone(read_amount("1.00.000"))


class ANumberTests(SimpleTestCase):
    """The unbounded reading under read_amount: every decimal kept."""

    def test_what_is_read(self):
        for text, expected in (
            ("0,125", "0.125"),
            ("420", "420"),
            ("-420,5", "-420.5"),
            ("420,5-", "-420.5"),
            ("1 234,5678", "1234.5678"),
        ):
            with self.subTest(text=text):
                self.assertEqual(read_number(text), Decimal(expected))

    def test_what_is_not_read(self):
        for text in (None, 4.2, "", "-", "Infinity", "NaN", "1e999", "12.", ".5", "--5", "1" * 41):
            with self.subTest(text=text):
                self.assertIsNone(read_number(text))
