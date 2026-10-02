"""Every amount a person reads has its thousands grouped by a no-break space
(the owner, 01/10/2026: « 10000€ -> 10 000€ »): common.group_thousands,
common.format_money and the `money` template filter."""

import random
import re
from decimal import Decimal, localcontext
from pathlib import Path

from django.conf import settings
from django.template import Context, Template
from django.template.defaultfilters import floatformat
from django.template.loader import get_template
from django.test import SimpleTestCase, override_settings
from django.utils import translation

from common import THOUSANDS_SEPARATOR, format_money, group_thousands
from inventory.templatetags.assets import _floatformat, money

SP = "\N{NO-BREAK SPACE}"


class GroupThousandsTests(SimpleTestCase):
    def test_the_separator_is_a_no_break_space(self):
        self.assertEqual(THOUSANDS_SEPARATOR, SP)

    def test_the_owners_examples(self):
        self.assertEqual(group_thousands("10000"), f"10{SP}000")
        self.assertEqual(group_thousands("16568684"), f"16{SP}568{SP}684")

    def test_three_digits_or_fewer_are_left_alone(self):
        for text in ("0", "7", "12", "999", "0.00", "999.99", "-999.99"):
            with self.subTest(text=text):
                self.assertEqual(group_thousands(text), text)

    def test_every_length_from_four_to_ten(self):
        self.assertEqual(group_thousands("1000"), f"1{SP}000")
        self.assertEqual(group_thousands("12345"), f"12{SP}345")
        self.assertEqual(group_thousands("123456"), f"123{SP}456")
        self.assertEqual(group_thousands("1234567"), f"1{SP}234{SP}567")
        self.assertEqual(group_thousands("1234567890"), f"1{SP}234{SP}567{SP}890")

    def test_only_the_whole_part_is_grouped(self):
        self.assertEqual(group_thousands("1234.5678"), f"1{SP}234.5678")
        self.assertEqual(group_thousands("0.123456"), "0.123456")

    def test_either_decimal_separator(self):
        self.assertEqual(group_thousands("1234,56"), f"1{SP}234,56")
        self.assertEqual(group_thousands("1234.56"), f"1{SP}234.56")

    def test_a_sign_stays_in_front(self):
        self.assertEqual(group_thousands("-1234.56"), f"-1{SP}234.56")
        self.assertEqual(group_thousands("+1234.56"), f"+1{SP}234.56")
        self.assertEqual(group_thousands("\N{MINUS SIGN}1234.56"), f"\N{MINUS SIGN}1{SP}234.56")

    def test_text_around_the_figure_is_kept(self):
        self.assertEqual(group_thousands("1234.56 €"), f"1{SP}234.56 €")

    def test_no_digit_comes_back_as_it_was(self):
        for text in ("", "—", "inf", "nan"):
            with self.subTest(text=text):
                self.assertEqual(group_thousands(text), text)

    def test_not_a_string(self):
        self.assertEqual(group_thousands(Decimal("12345")), f"12{SP}345")


class FormatMoneyTests(SimpleTestCase):
    def test_two_decimals_by_default(self):
        self.assertEqual(format_money(Decimal("16568684.5")), f"16{SP}568{SP}684.50")
        self.assertEqual(format_money(Decimal("0")), "0.00")

    def test_the_spec_is_formats(self):
        self.assertEqual(format_money(Decimal("1234"), "+.2f"), f"+1{SP}234.00")
        self.assertEqual(format_money(Decimal("-1234"), "+.2f"), f"-1{SP}234.00")
        self.assertEqual(format_money(Decimal("1234.56789"), ".4f"), f"1{SP}234.5679")
        self.assertEqual(format_money(1234.5, ".1f"), f"1{SP}234.5")

    def test_a_comma_decimal_is_still_one_replace_away(self):
        self.assertEqual(format_money(Decimal("1234.5")).replace(".", ","), f"1{SP}234,50")


class MoneyFilterTests(SimpleTestCase):
    def render(self, source, **context):
        return Template("{% load assets %}" + source).render(Context(context))

    def test_floatformat_with_its_thousands_grouped(self):
        self.assertEqual(self.render("{{ x|money }} €", x=Decimal("16568684.5")), f"16{SP}568{SP}684.50 €")
        self.assertEqual(self.render("{{ x|money }}", x=Decimal("10000")), f"10{SP}000.00")
        self.assertEqual(self.render("{{ x|money }}", x=Decimal("-1234.565")), f"-1{SP}234.57")
        self.assertEqual(self.render("{{ x|money }}", x=Decimal("999.99")), "999.99")

    def test_places_are_floatformats(self):
        self.assertEqual(self.render("{{ x|money:4 }}", x=Decimal("1234.5")), f"1{SP}234.5000")
        self.assertEqual(self.render("{{ x|money:0 }}", x=Decimal("1234.5")), f"1{SP}235")
        self.assertEqual(self.render('{{ x|money:"-2" }}', x=Decimal("12345")), f"12{SP}345")

    def test_nothing_is_nothing(self):
        self.assertEqual(self.render("{{ x|money }}", x=None), "")
        self.assertEqual(self.render("{{ x|money }}", x=""), "")

    def test_a_float_and_an_int(self):
        self.assertEqual(self.render("{{ x|money }}", x=1234.5), f"1{SP}234.50")
        self.assertEqual(self.render("{{ x|money }}", x=12345), f"12{SP}345.00")

    def test_never_escaped_into_an_entity(self):
        # The no-break space is a character, not « &nbsp; »: datatable.js and
        # the tests read text.
        self.assertNotIn("&", self.render("{{ x|money }}", x=Decimal("12345")))

    def test_in_french_too(self):
        with translation.override("fr"):
            self.assertEqual(self.render("{{ x|money }}", x=Decimal("1234.5")), f"1{SP}234,50")


def _values():
    """Every kind of figure `money` is given, and the corners of floatformat:
    signs, minus zero, half-up ties, carries, exponents both ways, 28-digit
    context limits, the 200-digit cut-off, non-finite values, and what is
    not a number at all."""
    decimals = [
        "0",
        "-0",
        "0.00",
        "-0.00",
        "0E-7",
        "-0E+3",
        "0.004",
        "-0.004",
        "0.005",
        "-0.005",
        "0.015",
        "-0.015",
        "0.045",
        "0.125",
        "-0.125",
        "0.5",
        "-0.5",
        "1.005",
        "2.675",
        "-2.675",
        "9.995",
        "-9.995",
        "99.5",
        "999.995",
        "-999.995",
        "999.4999",
        "1000",
        "1234.5",
        "-1234.565",
        "16568684.5",
        "10000.00",
        "0.00001",
        "1E+2",
        "-1E+2",
        "1E-10",
        "-1E-10",
        "123E+5",
        "5E-3",
        "1.5E+1",
        "12345678901234567890.125",
        "-99999999999999999999999999999.995",
        "1.23456789012345678901234567890123",
        "0.1234567890123456789012345678901234567890",
        "1E+150",
        "-1E+190",
        "1E-190",
        "1E+250",
        "1E-250",
        "NaN",
        "-NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
    ]
    yield from (Decimal(text) for text in decimals)
    yield from (0, 1, -1, 7, 999, 1000, -1000, 12345, 10**18, -(10**25), 10**199, 10**250)
    yield from (0.0, -0.0, 0.5, -0.5, 1.005, 2.675, -2.675, 1234.5, 0.1 + 0.2, 1e16, 1e22, -1e22, 1e-5, 1.23e-7)
    yield from (123456789.125, 5e-324, 1.7976931348623157e308, float("inf"), float("-inf"), float("nan"))
    yield from (None, "", "abc", "12.5", "-0", True, False, [], object())
    # A seeded spread of ordinary amounts, with every number of decimals.
    rng = random.Random(20261001)
    for _ in range(500):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 16)))
        number = Decimal(digits).scaleb(-rng.randint(0, 8))
        yield -number if rng.random() < 0.4 else number
        yield float(number) if rng.random() < 0.5 else int(number)


PLACES = (2, 0, 1, 3, 4, -1, -2, -3, 6, "2", "-2", "2g", "2u", "-2gu", "abc")


class MoneyFastPathTests(SimpleTestCase):
    """`money` skips floatformat's general case for a finite Decimal, int or
    float (assets._floatformat): every figure must come out exactly as
    floatformat-then-grouped prints it, in either language, whatever the
    decimal context."""

    def assert_same_as_floatformat(self):
        for value in _values():
            for places in PLACES:
                expected = group_thousands(floatformat(value, places))
                with self.subTest(value=value, places=places, language=translation.get_language()):
                    self.assertEqual(money(value, places), expected)
                    fast = _floatformat(value, places)
                    if fast is not None:
                        self.assertEqual(fast, str(floatformat(value, places)))

    def test_the_same_characters_as_floatformat(self):
        self.assert_same_as_floatformat()

    def test_in_french(self):
        with translation.override("fr"):
            self.assert_same_as_floatformat()

    def test_whatever_the_decimal_context(self):
        for precision in (10, 50):
            with localcontext(prec=precision):
                self.assert_same_as_floatformat()

    @override_settings(USE_THOUSAND_SEPARATOR=True)
    def test_a_grouping_locale_is_floatformat_s(self):
        self.assertIsNone(_floatformat(Decimal("12345.5"), 2))
        self.assertEqual(money(Decimal("12345.5")), group_thousands(floatformat(Decimal("12345.5"), 2)))

    def test_the_shortcut_is_taken(self):
        """What the pages give it: Decimals, ints and floats, places as an int."""
        self.assertEqual(_floatformat(Decimal("16568684.5"), 2), "16568684.50")
        self.assertEqual(_floatformat(Decimal("-0.004"), 2), "0.00")
        self.assertEqual(_floatformat(12345, 0), "12345")
        self.assertEqual(_floatformat(1234.5, 4), "1234.5000")
        for unknown in (None, "", "12.5", True, Decimal("NaN"), Decimal("1E+250")):
            with self.subTest(value=unknown):
                self.assertIsNone(_floatformat(unknown, 2))
        self.assertIsNone(_floatformat(Decimal("1.5"), "2"))


def app_templates():
    """Every template of the project's own: (its name for the loader, its text)."""
    base = Path(settings.BASE_DIR)
    for path in sorted(base.glob("**/templates/**/*.html")):
        relative = path.relative_to(base)
        if relative.parts[0] in {".venv", "staticfiles", "node_modules"}:
            continue
        name = "/".join(relative.parts[relative.parts.index("templates") + 1 :])
        yield name, path.read_text(encoding="utf-8")


class TemplatesPrintMoneyGroupedTests(SimpleTestCase):
    """A sweep, so the next amount added to a page is grouped like the rest."""

    def test_no_amount_is_printed_through_floatformat(self):
        printed = re.compile(r"\|floatformat(?::[^\s}]+)?\s*}}\s*(?:€|&nbsp;€)")
        offenders = [name for name, text in app_templates() if printed.search(text)]
        self.assertEqual(offenders, [], "an amount in € goes through |money, not |floatformat")

    def test_every_template_using_money_loads_it(self):
        using = [name for name, text in app_templates() if "|money" in text]
        self.assertGreater(len(using), 20)
        for name in using:
            with self.subTest(template=name):
                # An unknown filter is a TemplateSyntaxError when compiled.
                get_template(name)
