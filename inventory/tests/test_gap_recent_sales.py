"""« Combler les écarts »: only the recipes sold over the months chosen (the
owner, 01/10/2026: « ne proposer que des recettes ayant été vendues il y a
moins de X temps » - some recipes are off the menu).

`GapFillSetting.sold_within_months`, kept for the espace, is set from the
page's « Recettes vendues il y a moins de [3] [mois / ans] » (POST
`stock_gap_filler_recent`, a redirect to the page every time). It is counted
back from the end of the window - today on the page - whatever the count the
gaps run from: right after a count nothing has been sold since it, yet the
menu has not changed. The gaps themselves still run from the take; only
which recipes are proposed and the mix of sales a tie goes to follow the
window - the till's prices follow the more recent of the two windows. None,
the default, is the recipes sold since the take: a sale there, which a
refund on another day no longer takes back (until 01/10/2026 the default
asked for net sales above zero).

What these tests pin:

* the first day a sale counts: the day after the take, or `months_before`
  the end - the day of the month kept where the month has it;
* a refund is no sale and undoes none, a sale document is one, a credit on
  one is not;
* the till's prices never come back from before the take, and each window's
  first day decides alone;
* an amount nothing can be proposed for says why: nothing sold over the
  window, or nothing sold that pours an article;
* a recipe sold before the take comes back with a window reaching it, and
  one sold only before the window leaves, said with its last sale;
* the form posts what a browser posts, reads back what was stored (twelve
  months as a year), refuses in French what is no duration, and a GET
  writes nothing.

Invented data throughout: every article, recipe, till button, price and
quantity is made up for these tests.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import unescape

from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from inventory.gaps import TillButton, UnsoldRecipe, duration_words, fill_gaps, gaps_since, months_before, show_list
from inventory.models import GapFillSetting
from inventory.tests import test_gap_filler_page as page
from inventory.tests.test_gap_filler_page import (
    NO_RECIPE_USES_ONE,
    PLAN_TABLE,
    PageTestCase,
    explainer_of,
    noon,
    said_at_the_top,
    sentences_of,
    summary_of,
    table_of,
)
from inventory.tests.test_gaps import article, bought, counted, recipe, rung_up, sold, till_button
from inventory.views import duration_fields, read_typed_duration
from margins.tests.test_page import cells_of, row_of, text_of
from recipes.models import RecipeSale, SaleDocument, SaleDocumentLine
from staff.tests.page_forms import as_post, form_posting_to
from tests.factories import make_recipe, make_stock_take

RECENT = "inventory:stock_gap_filler_recent"
PAGE = "inventory:stock_gap_filler"

TAKE_DAY = date(2026, 3, 1)
END = date(2026, 6, 30)
#: Three months back from END: the first day a sale counts.
THREE_MONTHS_BACK = date(2026, 3, 30)


def choose(months: int | None) -> None:
    """The espace's setting, as the page's form stores it."""
    GapFillSetting.objects.update_or_create(pk=GapFillSetting.SINGLETON_PK, defaults={"sold_within_months": months})


def offered(report) -> set[int]:
    return {offer.recipe_id for offer in report.offers}


def unsold(report) -> list[tuple[str, date | None]]:
    return [(row.recipe.name, row.last_sold) for row in report.not_sold_since]


# ---------------------------------------------------------------------------
# The arithmetic of the window, and the words for it
# ---------------------------------------------------------------------------


class MonthsBeforeTests(SimpleTestCase):
    def test_the_same_day_of_an_earlier_month(self):
        self.assertEqual(months_before(date(2026, 10, 15), 3), date(2026, 7, 15))

    def test_across_new_year(self):
        self.assertEqual(months_before(date(2026, 1, 31), 1), date(2025, 12, 31))
        self.assertEqual(months_before(date(2026, 2, 10), 14), date(2024, 12, 10))

    def test_a_day_the_month_lacks_is_its_last(self):
        self.assertEqual(months_before(date(2026, 3, 31), 1), date(2026, 2, 28))
        self.assertEqual(months_before(date(2028, 3, 31), 1), date(2028, 2, 29))  # a leap year
        self.assertEqual(months_before(date(2026, 5, 31), 1), date(2026, 4, 30))
        self.assertEqual(months_before(date(2028, 2, 29), 12), date(2027, 2, 28))

    def test_whole_years(self):
        self.assertEqual(months_before(date(2026, 10, 1), 12), date(2025, 10, 1))
        self.assertEqual(months_before(date(2026, 10, 1), GapFillSetting.MAX_MONTHS), date(2016, 10, 1))

    def test_no_month_is_the_day_itself(self):
        self.assertEqual(months_before(date(2026, 10, 1), 0), date(2026, 10, 1))


class DurationWordsTests(SimpleTestCase):
    def test_months_and_years_as_the_page_says_them(self):
        for months, words in (
            (1, "moins d'un mois"),
            (3, "moins de 3 mois"),
            (11, "moins de 11 mois"),
            (12, "moins d'un an"),
            (18, "moins de 18 mois"),
            (24, "moins de 2 ans"),
            (120, "moins de 10 ans"),
        ):
            with self.subTest(months=months):
                self.assertEqual(duration_words(months), words)


class ReadTypedDurationTests(SimpleTestCase):
    """What the form's two fields post, read into months - never raising."""

    def test_a_whole_number_of_months_or_years(self):
        for typed, unit, months in (
            ("3", "mois", 3),
            (" 3 ", "mois", 3),
            ("3\N{NO-BREAK SPACE}", "mois", 3),
            ("003", "mois", 3),
            ("1", "mois", 1),
            ("120", "mois", 120),
            ("2", "ans", 24),
            ("10", "ans", 120),
        ):
            with self.subTest(typed=typed, unit=unit):
                self.assertEqual(read_typed_duration(typed, unit), (months, ""))

    def test_what_is_no_whole_number_or_no_unit_is_unreadable(self):
        for typed, unit in (
            ("", "mois"),
            ("   ", "mois"),
            (None, "mois"),
            ("abc", "mois"),
            ("1,5", "mois"),
            ("1.5", "mois"),
            ("3.0", "mois"),
            ("-1", "mois"),
            ("+3", "mois"),
            ("1e3", "mois"),
            ("3 mois", "mois"),
            ("\N{ARABIC-INDIC DIGIT THREE}", "mois"),  # a digit to Python, not to the form
            ("\N{FULLWIDTH DIGIT THREE}", "mois"),
            ("3", "semaines"),
            ("3", ""),
            ("3", "MOIS"),
        ):
            with self.subTest(typed=typed, unit=unit):
                self.assertEqual(read_typed_duration(typed, unit), (None, "unreadable"))

    def test_none_or_past_ten_years_is_out_of_range(self):
        for typed, unit in (
            ("0", "mois"),
            ("000", "mois"),
            ("0", "ans"),
            ("121", "mois"),
            ("11", "ans"),
            ("1000000", "mois"),
            # Thousands of digits: refused, never handed to int(), which
            # raises past 4 300 of them.
            ("9" * 5000, "mois"),
        ):
            with self.subTest(typed=typed[:12], unit=unit):
                self.assertEqual(read_typed_duration(typed, unit), (None, "out_of_range"))

    def test_the_form_shows_whole_years_in_years(self):
        for months, shown in ((None, ("", "mois")), (3, ("3", "mois")), (12, ("1", "ans")), (18, ("18", "mois"))):
            with self.subTest(months=months):
                self.assertEqual(duration_fields(months), shown)
        self.assertEqual(duration_fields(24), ("2", "ans"))


class GapFillSettingTests(TestCase):
    def test_no_row_is_the_default_and_reading_it_writes_nothing(self):
        setting = GapFillSetting.current()
        self.assertIsNone(setting.sold_within_months)
        self.assertEqual(setting.pk, GapFillSetting.SINGLETON_PK)
        self.assertFalse(GapFillSetting.objects.exists())

    def test_the_stored_row(self):
        choose(6)
        self.assertEqual(GapFillSetting.current().sold_within_months, 6)

    def test_one_row_for_the_espace(self):
        choose(6)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GapFillSetting.objects.create(pk=2, sold_within_months=3)

    def test_the_database_refuses_a_duration_out_of_range(self):
        for months in (0, GapFillSetting.MAX_MONTHS + 1):
            with self.subTest(months=months), self.assertRaises(IntegrityError), transaction.atomic():
                GapFillSetting.objects.create(pk=GapFillSetting.SINGLETON_PK, sold_within_months=months)


# ---------------------------------------------------------------------------
# What gaps_since proposes over the window
# ---------------------------------------------------------------------------


class MenuWindowTests(TestCase):
    """A count on 01/03/2026, read up to 30/06/2026. Blonde: 100 L counted,
    50 L bought on 01/04. Recipes pouring it, each sold on its own days:

    * Pinte (35,00 €): 6 on 05/03, 4 on 20/06 - on every menu;
    * Demi (31,00 €): 8 on 10/03 - since the take, not in three months;
    * Galopin (30,50 €): 5 on 15/02 - before the take only;
    * Bock (32,00 €): 3 on 10/01, then one refunded on 25/06;
    * Panaché (33,00 €): 2 on a sale document of 25/06;
    * Pinte du premier jour (34,00 €): 1 on 30/03, three months back from
      30/06 to the day; Pinte de la veille (36,00 €): 1 on 29/03;
    * Formule (37,00 €): never sold."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "100")
        bought(cls.blonde, "50", date(2026, 4, 1))
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 6, day=date(2026, 3, 5))
        sold(cls.pint, 4, day=date(2026, 6, 20))
        cls.half = recipe("Demi exemple", "31.00", (cls.blonde, "0.25"))
        sold(cls.half, 8, day=date(2026, 3, 10))
        cls.galopin = recipe("Galopin exemple", "30.50", (cls.blonde, "0.125"))
        sold(cls.galopin, 5, day=date(2026, 2, 15))
        cls.bock = recipe("Bock exemple", "32.00", (cls.blonde, "0.25"))
        sold(cls.bock, 3, day=date(2026, 1, 10))
        sold(cls.bock, -1, day=date(2026, 6, 25))
        cls.shandy = recipe("Panaché exemple", "33.00", (cls.blonde, "0.25"))
        document = SaleDocument.objects.create(sold_on=date(2026, 6, 25))
        SaleDocumentLine.objects.create(document=document, recipe=cls.shandy, quantity=Decimal("2"))
        cls.first_day = recipe("Pinte du premier jour exemple", "34.00", (cls.blonde, "0.5"))
        sold(cls.first_day, 1, day=THREE_MONTHS_BACK)
        cls.day_before = recipe("Pinte de la veille exemple", "36.00", (cls.blonde, "0.5"))
        sold(cls.day_before, 1, day=THREE_MONTHS_BACK - timedelta(days=1))
        cls.never = recipe("Formule exemple", "37.00", (cls.blonde, "0.5"))

    def report(self, months: int | None = None, take=None):
        choose(months)
        return gaps_since(take or self.take, end=END)

    def test_by_default_the_recipes_sold_since_the_take(self):
        report = self.report()
        self.assertEqual(
            offered(report), {self.pint.pk, self.half.pk, self.shandy.pk, self.first_day.pk, self.day_before.pk}
        )
        self.assertEqual(
            unsold(report),
            [("Bock exemple", date(2026, 1, 10)), ("Formule exemple", None), ("Galopin exemple", date(2026, 2, 15))],
        )
        self.assertEqual(
            (report.menu_since, report.sold_within_months, report.sold_within_words), (date(2026, 3, 2), None, "")
        )

    def test_no_row_and_a_row_saying_none_read_the_same(self):
        GapFillSetting.objects.all().delete()
        bare = gaps_since(self.take, end=END)
        said = self.report(None)
        self.assertEqual((offered(bare), unsold(bare)), (offered(said), unsold(said)))

    def test_three_months_back_from_the_end(self):
        report = self.report(3)
        self.assertEqual(report.menu_since, THREE_MONTHS_BACK)
        self.assertEqual(report.sold_within_words, "moins de 3 mois")
        self.assertEqual(offered(report), {self.pint.pk, self.shandy.pk, self.first_day.pk})
        self.assertEqual(
            unsold(report),
            [
                ("Bock exemple", date(2026, 1, 10)),
                ("Demi exemple", date(2026, 3, 10)),
                ("Formule exemple", None),
                ("Galopin exemple", date(2026, 2, 15)),
                ("Pinte de la veille exemple", date(2026, 3, 29)),
            ],
        )

    def test_a_sale_on_the_first_day_counts_the_day_before_does_not(self):
        report = self.report(3)
        self.assertIn(self.first_day.pk, offered(report))
        self.assertNotIn(self.day_before.pk, offered(report))

    def test_a_recipe_sold_before_the_take_comes_back_with_a_window_reaching_it(self):
        report = self.report(6)
        self.assertEqual(report.menu_since, date(2025, 12, 30))
        self.assertIn(self.galopin.pk, offered(report))
        self.assertIn(self.bock.pk, offered(report))
        self.assertEqual(unsold(report), [("Formule exemple", None)])

    def test_a_refund_is_no_sale(self):
        # The bock's last row, 25/06, is a refund: its last sale is 10/01.
        report = self.report(3)
        self.assertNotIn(self.bock.pk, offered(report))
        self.assertIn(("Bock exemple", date(2026, 1, 10)), unsold(report))

    def test_a_sale_document_is_a_sale(self):
        report = self.report(3)
        offer = next(offer for offer in report.offers if offer.recipe_id == self.shandy.pk)
        self.assertEqual(offer.sold, 2)

    def test_the_mix_a_tie_goes_to_is_the_window_s(self):
        """What each offer sold: over the window chosen, the take's by
        default - the galopin, sold before the take only, its 5."""

        def mix(report):
            return {offer.recipe_id: offer.sold for offer in report.offers}

        self.assertEqual(mix(self.report())[self.pint.pk], 10)
        # The first day's sale is in the mix as well as on the menu.
        three = mix(self.report(3))
        self.assertEqual((three[self.pint.pk], three[self.first_day.pk]), (4, 1))
        six = mix(self.report(6))
        self.assertEqual((six[self.galopin.pk], six[self.bock.pk], six[self.pint.pk]), (5, 2, 10))

    def test_the_gaps_still_run_from_the_take(self):
        """Only the menu moves: every article's figures are the take's,
        whatever the window."""

        def figures(report):
            row = report.articles[self.blonde.pk]
            return (row.opening, row.purchases, row.known_losses, row.sold, row.allowance, row.room)

        default = figures(self.report())
        self.assertEqual(default[:2], (Decimal("100"), Decimal("50")))
        for months in (1, 3, 6, 120):
            with self.subTest(months=months):
                self.assertEqual(figures(self.report(months)), default)

    def test_the_till_price_is_read_over_the_window(self):
        """The pint's button rang 33,00 € six times in March and 35,00 €
        (the recipe's price) four times in June: since the take it charged
        33,00 € most often; over the last three months, 35,00 €."""
        button = till_button("PINTE CAISSE EXEMPLE", self.pint, rung=10)
        rung_up(button, date(2026, 3, 5), 6, "198.00")
        rung_up(button, date(2026, 6, 20), 4, "140.00")
        self.assertEqual(self.report().till_buttons[self.pint.pk], TillButton("PINTE CAISSE EXEMPLE", Decimal("33.00")))
        self.assertEqual(
            self.report(3).till_buttons[self.pint.pk], TillButton("PINTE CAISSE EXEMPLE", Decimal("35.00"))
        )

    def test_the_till_window_s_first_day_counts_and_the_day_before_does_not(self):
        """Each window's first day decides alone: three months back, 5 rung
        at 35,00 € on that day beat 4 at 33,00 € in June, and 9 at 31,00 €
        the day before stay out; by default, the day after the take is in
        and the take's own day is out."""
        draught = recipe("Pinte pression exemple", "35.00", (self.blonde, "0.5"))
        sold(draught, 1, day=date(2026, 6, 20))
        button = till_button("PRESSION CAISSE EXEMPLE", draught, rung=18)
        rung_up(button, THREE_MONTHS_BACK, 5, "175.00")
        rung_up(button, date(2026, 6, 20), 4, "132.00")
        rung_up(button, THREE_MONTHS_BACK - timedelta(days=1), 9, "279.00")
        self.assertEqual(self.report(3).till_buttons[draught.pk].price, Decimal("35.00"))
        # Since the take, the 9 at 31,00 € are the most rung.
        self.assertEqual(self.report().till_buttons[draught.pk].price, Decimal("31.00"))

        mild = recipe("Pinte douce exemple", "34.00", (self.blonde, "0.5"))
        sold(mild, 1, day=date(2026, 6, 20))
        button = till_button("DOUCE CAISSE EXEMPLE", mild, rung=18)
        rung_up(button, TAKE_DAY + timedelta(days=1), 5, "170.00")
        rung_up(button, date(2026, 3, 20), 4, "132.00")
        rung_up(button, TAKE_DAY, 9, "279.00")
        self.assertEqual(self.report().till_buttons[mild.pk].price, Decimal("34.00"))

    def test_a_long_window_never_brings_back_a_price_charged_before_the_take(self):
        """Regression (review of 01/10/2026): read over the menu's whole year,
        20 pints rung at 33,00 € in January outnumbered the 4 rung at 35,00 €
        since the take, and the page said « en caisse 33,00 € » for a button
        ringing 35,00 €. The till's prices are read over the more recent of
        the two windows."""
        button = till_button("PINTE CAISSE EXEMPLE", self.pint, rung=24)
        rung_up(button, date(2026, 1, 10), 20, "660.00")
        rung_up(button, date(2026, 6, 20), 4, "140.00")
        for months in (None, 6, 12, 120):
            with self.subTest(months=months):
                self.assertEqual(
                    self.report(months).till_buttons[self.pint.pk],
                    TillButton("PINTE CAISSE EXEMPLE", Decimal("35.00")),
                )
        line = fill_gaps(self.report(12), Decimal("35.00")).lines[0]
        self.assertFalse(line.till_differs)

    def test_a_recipe_back_from_before_the_take_is_named_by_its_button_with_no_price(self):
        button = till_button("GALOPIN CAISSE EXEMPLE", self.galopin, rung=5)
        rung_up(button, date(2026, 2, 15), 5, "152.50")
        self.assertEqual(self.report(6).till_buttons[self.galopin.pk], TillButton("GALOPIN CAISSE EXEMPLE", None))

    def test_a_credit_on_a_sale_document_is_no_sale(self):
        monaco = recipe("Monaco exemple", "33.50", (self.blonde, "0.25"))
        sold(monaco, 2, day=date(2026, 1, 15))
        credit = SaleDocument.objects.create(sold_on=date(2026, 6, 25))
        SaleDocumentLine.objects.create(document=credit, recipe=monaco, quantity=Decimal("-1"))
        report = self.report(3)
        self.assertNotIn(monaco.pk, offered(report))
        self.assertIn(("Monaco exemple", date(2026, 1, 15)), unsold(report))

    def test_a_sale_refunded_later_stays_on_the_menu(self):
        """A refund does not undo a sale: rung on 05/04 and refunded on
        06/04, the cider is on the take's menu, its mix 0. Until 01/10/2026
        the default asked for net sales above zero."""
        cider = recipe("Cidre exemple", "32.50", (self.blonde, "0.25"))
        sold(cider, 1, day=date(2026, 4, 5))
        sold(cider, -1, day=date(2026, 4, 6))
        report = self.report()
        self.assertEqual({offer.recipe_id: offer.sold for offer in report.offers}[cider.pk], 0)
        self.assertNotIn("Cidre exemple", [name for name, _last in unsold(report)])

    def test_a_plan_proposes_a_recipe_back_on_the_menu(self):
        """30,50 € is one galopin, and only that: proposed once a year's
        sales reach it, below the cheapest recipe on the take's menu."""
        six = fill_gaps(self.report(6), Decimal("30.50"))
        self.assertEqual([(line.recipe.pk, line.count) for line in six.lines], [(self.galopin.pk, 1)])
        self.assertEqual(fill_gaps(self.report(), Decimal("30.50")).lines, [])

    def test_a_count_taken_yesterday_still_has_a_menu(self):
        """The reason the window is counted from the end and not from the
        count: a count taken the day before the end has no sale since it,
        so by default nothing is proposed; three months back, the menu is
        the same as from an older count."""
        yesterday = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 6, 29, 12, 0)))
        counted(yesterday, self.blonde, "60")
        self.assertEqual(offered(self.report(None, take=yesterday)), set())
        self.assertEqual(offered(self.report(3, take=yesterday)), {self.pint.pk, self.shandy.pk, self.first_day.pk})

    def test_a_list_made_before_the_window_changed_still_counts(self):
        """Two halves proposed, then the window shortened: the half is no
        offer now, and the list's halves still fill the blonde."""
        report = self.report(3)
        self.assertNotIn(self.half.pk, offered(report))
        show_list(report, {self.half.pk: 2})
        self.assertEqual(report.articles[self.blonde.pk].proposed, Decimal("0.5"))
        result = fill_gaps(report, Decimal("35.00"), {self.half.pk: 2})
        self.assertEqual(result.plan.counts, {self.pint.pk: 1})

    def test_the_unsold_rows_are_what_the_report_holds(self):
        report = self.report(3)
        self.assertEqual(report.not_sold_since[0], UnsoldRecipe(self.bock, date(2026, 1, 10)))


class MenuWindowQueryTests(TestCase):
    """The window costs the same whatever the sales: the setting, the last
    sale of every recipe and the window's sales are a few queries each."""

    def setUp(self):
        self.take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        self.made = []
        for index in range(3):
            stock_type = article(f"Article exemple {index}")
            counted(self.take, stock_type, "30")
            made = recipe(f"Recette exemple {index}", "35.00", (stock_type, "0.5"))
            sold(made, 4, day=date(2026, 6, 1))
            self.made.append(made)

    def queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            gaps_since(self.take, end=END)
        return len(captured)

    def test_more_sales_cost_nothing_more(self):
        choose(3)
        few = self.queries()
        for day in range(1, 29):
            for made in self.made:
                sold(made, 1, day=date(2026, 5, day), source="caisse")
                sold(made, 1, day=date(2025, 5, day), source="caisse")
        self.assertEqual(self.queries(), few)

    def test_a_window_costs_two_queries_more_than_none(self):
        """The window's own sales, read beside the take's: the till's and
        the sale documents'."""
        choose(None)
        default = self.queries()
        choose(3)
        self.assertEqual(self.queries(), default + 2)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def menu_sentence(html: str) -> str:
    """« Proposées : … », the line under the form, as it reads."""
    found = re.search(r'<p class="muted">(Proposées : .*?)</p>', html, flags=re.DOTALL)
    return unescape(text_of(found.group(1))) if found else ""


def day(value: date) -> str:
    return f"{value:%d/%m/%Y}"


class RecentSalesPageTests(PageTestCase):
    """A count 200 days ago, the blonde counted at 100 L. Pinte (35,00 €)
    sold yesterday; Demi (31,00 €) 150 days ago - since the count, not in
    three months; Galopin (30,50 €) 250 days ago - before the count, within
    a year."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(cls.today - timedelta(days=200)))
        cls.blonde = page.article("Blonde exemple")
        page.bought(cls.blonde, "100", cls.today - timedelta(days=220))
        page.counted(cls.take, cls.blonde, "100")
        cls.pint = page.recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=cls.today - timedelta(days=1), quantity=10)
        cls.half = page.recipe("Demi exemple", "31.00", cls.blonde, "0.25")
        cls.half_sold = cls.today - timedelta(days=150)
        RecipeSale.objects.create(recipe=cls.half, sold_on=cls.half_sold, quantity=8)
        cls.galopin = page.recipe("Galopin exemple", "30.50", cls.blonde, "0.125")
        cls.galopin_sold = cls.today - timedelta(days=250)
        RecipeSale.objects.create(recipe=cls.galopin, sold_on=cls.galopin_sold, quantity=5)

    def recent_form(self, html: str):
        return form_posting_to(html, reverse(RECENT))

    def choose_on_the_page(self, press=None, **values):
        """The form as drawn on the count's page, sent as a browser sends it:
        one redirect, to that page."""
        form = self.recent_form(self.html(depuis=self.take.pk))
        response = self.client.post(form.action, as_post(form.submission(press=press, values=values)), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(self.page_of(self.take), 302)])
        return response.content.decode()

    def plan_names(self, html: str) -> list[str]:
        table = table_of(html, PLAN_TABLE)
        return [cells_of(row)[0] for row in re.findall(r"<tr>.*?</tr>", table.split("<tbody>")[-1], flags=re.DOTALL)]

    # -- by default -------------------------------------------------------

    def test_by_default_the_recipes_sold_since_the_count(self):
        html = self.html(depuis=self.take.pk)
        form = self.recent_form(html)
        self.assertEqual((form.control("duree").value, form.control("unite").value), ("", "mois"))
        self.assertEqual([option for option, _selected in form.control("unite").options], ["mois", "ans"])
        self.assertEqual(len(form.buttons()), 1, "no « Depuis l'inventaire » while nothing is chosen")
        self.assertEqual(
            menu_sentence(html),
            f"Proposées : les recettes vendues depuis l'inventaire du {day(self.take.taken_at.date())}.",
        )
        self.assertEqual(
            sentences_of(explainer_of(html)),
            [
                (
                    "Pas vendue depuis l'inventaire, donc pas proposée : "
                    f"Galopin exemple (dernière vente {day(self.galopin_sold)})"
                )
            ],
        )
        self.assertEqual(self.plan_names(self.add("31")), ["Demi exemple"])

    def test_the_field_asks_a_browser_for_a_whole_number(self):
        field = self.recent_form(self.html()).control("duree")
        self.assertEqual(
            {name: field.attrs.get(name) for name in ("type", "min", "max", "step", "inputmode")},
            {"type": "number", "min": "1", "max": "120", "step": "1", "inputmode": "numeric"},
        )
        self.assertIn("required", field.attrs)

    def test_a_year_is_read_an_s(self):
        """« il y a moins de [1] [an(s)] »: the unit's words fit one year as
        well as ten; what it posts stays « ans »."""
        html = self.choose_on_the_page(duree="1", unite="ans")
        self.assertRegex(html, r'<option value="ans" selected>an\(s\)</option>')
        self.assertRegex(html, r'<option value="mois">mois</option>')

    def test_drawing_the_page_writes_nothing(self):
        self.html(depuis=self.take.pk)
        self.html()
        self.assertFalse(GapFillSetting.objects.exists())

    # -- choosing ---------------------------------------------------------

    def test_three_months(self):
        html = self.choose_on_the_page(duree="3")
        self.assertEqual(
            said_at_the_top(html), [("success", "Recettes proposées : celles vendues il y a moins de 3 mois.")]
        )
        self.assertEqual(GapFillSetting.current().sold_within_months, 3)
        since = months_before(self.today, 3)
        form = self.recent_form(html)
        self.assertEqual((form.control("duree").value, form.control("unite").value), ("3", "mois"))
        back = [button for button in form.buttons() if button.name == "depuis_inventaire"]
        self.assertEqual(len(back), 1)
        self.assertIn("formnovalidate", back[0].attrs)
        self.assertEqual(form.buttons()[0].name, None, "Enter applies the duration typed")
        self.assertEqual(
            menu_sentence(html), f"Proposées : les recettes vendues il y a moins de 3 mois (depuis le {day(since)})."
        )
        self.assertEqual(summary_of(html), "Ce qui ne peut pas être comblé · 2 recettes pas vendues")
        self.assertEqual(
            sentences_of(explainer_of(html)),
            [
                (
                    f"Pas vendues depuis le {day(since)}, donc pas proposées : "
                    f"Demi exemple (dernière vente {day(self.half_sold)}) · "
                    f"Galopin exemple (dernière vente {day(self.galopin_sold)})"
                )
            ],
        )
        # The demi is off the menu: 31 € is now less than the cheapest.
        self.assertEqual(
            said_at_the_top(self.add("31")), [("warning", "Rien pour 31.00 € : moins que la recette la moins chère.")]
        )
        self.assertEqual(self.plan_names(self.add("35")), ["Pinte exemple"])

    def test_each_recipe_not_proposed_links_to_its_page(self):
        html = self.choose_on_the_page(duree="3")
        fold = explainer_of(html)
        for made in (self.half, self.galopin):
            with self.subTest(recipe=made.name):
                self.assertIn(f'<a href="{reverse("recipes:recipe_detail", args=[made.pk])}">{made.name}</a>', fold)

    def test_a_year_brings_back_a_recipe_sold_before_the_count(self):
        html = self.choose_on_the_page(duree="1", unite="ans")
        self.assertEqual(
            said_at_the_top(html), [("success", "Recettes proposées : celles vendues il y a moins d'un an.")]
        )
        self.assertEqual(GapFillSetting.current().sold_within_months, 12)
        form = self.recent_form(html)
        self.assertEqual((form.control("duree").value, form.control("unite").value), ("1", "ans"))
        self.assertEqual(summary_of(html), "")  # nothing left that cannot be filled
        self.assertEqual(self.plan_names(self.add("30.50")), ["Galopin exemple"])

    def test_twelve_months_read_back_as_a_year(self):
        html = self.choose_on_the_page(duree="12", unite="mois")
        form = self.recent_form(html)
        self.assertEqual((form.control("duree").value, form.control("unite").value), ("1", "ans"))
        self.assertIn("il y a moins d'un an (depuis le", menu_sentence(html))

    def test_back_to_the_count(self):
        choose(3)
        html = self.choose_on_the_page(press=("depuis_inventaire", "1"))
        self.assertEqual(
            said_at_the_top(html), [("success", "Recettes proposées : celles vendues depuis l'inventaire.")]
        )
        self.assertIsNone(GapFillSetting.current().sold_within_months)
        self.assertEqual(len(self.recent_form(html).buttons()), 1)
        self.assertEqual(self.plan_names(self.add("31")), ["Demi exemple"])

    def test_kept_for_every_count_s_page(self):
        older = make_stock_take(taken_at=noon(self.today - timedelta(days=300)))
        page.counted(older, self.blonde, "80")
        self.choose_on_the_page(duree="3")
        html = self.html(depuis=older.pk)
        self.assertEqual(self.recent_form(html).control("duree").value, "3")
        self.assertIn("il y a moins de 3 mois", menu_sentence(html))

    # -- refused ----------------------------------------------------------

    def test_a_refused_duration_changes_nothing(self):
        choose(3)
        for typed, unit, said in (
            ("", "mois", "Durée illisible : tapez un nombre entier, par exemple 3 mois."),
            ("abc", "mois", "Durée illisible : tapez un nombre entier, par exemple 3 mois."),
            ("1,5", "mois", "Durée illisible : tapez un nombre entier, par exemple 3 mois."),
            ("-1", "mois", "Durée illisible : tapez un nombre entier, par exemple 3 mois."),
            ("\N{ARABIC-INDIC DIGIT THREE}", "mois", "Durée illisible : tapez un nombre entier, par exemple 3 mois."),
            ("3", "semaines", "Durée illisible : tapez un nombre entier, par exemple 3 mois."),
            ("0", "mois", "La durée va d'un mois à 10 ans."),
            ("121", "mois", "La durée va d'un mois à 10 ans."),
            ("11", "ans", "La durée va d'un mois à 10 ans."),
            ("9" * 5000, "mois", "La durée va d'un mois à 10 ans."),
        ):
            with self.subTest(typed=typed[:12], unit=unit):
                response = self.client.post(
                    reverse(RECENT), {"depuis": self.take.pk, "duree": typed, "unite": unit}, follow=True
                )
                self.assertEqual(response.redirect_chain, [(self.page_of(self.take), 302)])
                self.assertEqual(said_at_the_top(response.content.decode()), [("error", said)])
                self.assertEqual(GapFillSetting.current().sold_within_months, 3)

    def test_a_post_without_the_unit_is_refused_too(self):
        response = self.client.post(reverse(RECENT), {"depuis": self.take.pk, "duree": "3"}, follow=True)
        self.assertEqual(
            said_at_the_top(response.content.decode()),
            [("error", "Durée illisible : tapez un nombre entier, par exemple 3 mois.")],
        )
        self.assertFalse(GapFillSetting.objects.exists())

    def test_a_get_goes_to_the_page_and_writes_nothing(self):
        response = self.client.get(reverse(RECENT), {"depuis": self.take.pk, "duree": "3", "unite": "mois"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse(PAGE))
        self.assertFalse(GapFillSetting.objects.exists())

    def test_posted_from_a_count_that_is_gone_it_lands_on_the_latest(self):
        response = self.client.post(reverse(RECENT), {"depuis": "999999", "duree": "3", "unite": "mois"}, follow=True)
        self.assertEqual(response.redirect_chain, [(reverse(PAGE), 302)])
        self.assertEqual(GapFillSetting.current().sold_within_months, 3)
        self.assertEqual(
            said_at_the_top(response.content.decode()),
            [("success", "Recettes proposées : celles vendues il y a moins de 3 mois.")],
        )

    # -- a list -----------------------------------------------------------

    def test_a_list_keeps_what_it_proposed_when_the_window_changes(self):
        """A demi proposed, then three months chosen: the entry still says
        the demi (it may have been rung up), and the gaps still count it."""
        self.add("31")
        html = self.choose_on_the_page(duree="3")
        self.assertEqual(self.plan_names(html), ["Demi exemple"])
        self.assertEqual(cells_of(row_of(table_of(html, "écarts"), "Blonde exemple"))[-3], "0.25")


class NothingSoldOverTheWindowTests(PageTestCase):
    """A count 200 days ago; the pint's only sales 100 days ago - since the
    count, not in the last month."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(cls.today - timedelta(days=200)))
        cls.blonde = page.article("Blonde exemple")
        page.bought(cls.blonde, "100", cls.today - timedelta(days=220))
        page.counted(cls.take, cls.blonde, "100")
        cls.pint = page.recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=cls.today - timedelta(days=100), quantity=10)

    def test_an_amount_says_no_recipe_sold_since_the_window_began(self):
        choose(1)
        html = self.add("35")
        since = day(months_before(self.today, 1))
        self.assertEqual(
            said_at_the_top(html), [("warning", f"Rien pour 35.00 € : aucune recette vendue depuis le {since}.")]
        )
        said = unescape(
            text_of(html[html.index('<h2 id="ecarts">') : html.index('<details class="explainer" id="exclusions"')])
        )
        self.assertEqual(said, f"Écarts {NO_RECIPE_USES_ONE}")

    def test_by_default_the_pint_sold_since_the_count_is_proposed(self):
        html = self.add("35")
        self.assertEqual(said_at_the_top(html), [])
        self.assertIn("Pinte exemple", table_of(html, PLAN_TABLE))


class NothingSoldSinceTheCountTests(PageTestCase):
    """No duration chosen. A count 10 days ago; the pint's only sale 20 days
    ago, before it."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(cls.today - timedelta(days=10)))
        cls.blonde = page.article("Blonde exemple")
        page.bought(cls.blonde, "100", cls.today - timedelta(days=30))
        page.counted(cls.take, cls.blonde, "100")
        cls.pint = page.recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=cls.today - timedelta(days=20), quantity=10)

    def add_as_drawn(self, amount: str) -> str:
        """The add form as the count's page draws it, sent as a browser does."""
        form = form_posting_to(self.html(depuis=self.take.pk), reverse(page.ADD))
        response = self.client.post(form.action, as_post(form.submission(values={"montant": amount})), follow=True)
        self.assertEqual(response.redirect_chain, [(self.page_of(self.take), 302)])
        return response.content.decode()

    def test_an_amount_says_no_recipe_sold_since_the_count(self):
        """Not « plus aucune vente ne tient dans les écarts », which the
        page said before: the gaps are not full, nothing sold."""
        self.assertEqual(
            said_at_the_top(self.add_as_drawn("35")),
            [("warning", "Rien pour 35.00 € : aucune recette vendue depuis l'inventaire.")],
        )

    def test_recipes_sold_that_pour_nothing_are_said_as_such(self):
        """An entry ticket sold yesterday is on the menu and pours nothing:
        recipes did sell, none uses an article."""
        ticket = make_recipe(name="Entrée concert exemple", selling_price_ttc="35.00")
        RecipeSale.objects.create(recipe=ticket, sold_on=self.today - timedelta(days=1), quantity=3)
        self.assertEqual(
            said_at_the_top(self.add_as_drawn("35")),
            [("warning", "Rien pour 35.00 € : aucune recette vendue depuis l'inventaire n'utilise un article.")],
        )
