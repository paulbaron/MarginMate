"""« Combler les écarts », the list: `GapFillEntry` and the helpers of
`inventory/gaps.py` that read it - `list_counts` (the list's sales, handed to
the planner), `entry_lines` / `entry_rows` (an entry's lines as kept and as
read back) and `list_is_stale`.

An entry keeps its lines as JSON, and the page reads them back whatever was
stored: a line that does not read is left out rather than breaking the page
(`entry_rows`). So a good part of what is below is garbage on purpose.

Invented data throughout: every recipe, till button, article, price and day
is made up for these tests; every price is above 30 € or off the 0,50 €
grid.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from inventory.gap_planner import EXACT, Plan
from inventory.gaps import (
    ArticleGap,
    EntryLine,
    FillResult,
    GapReport,
    Line,
    TillButton,
    entry_lines,
    entry_rows,
    fill_gaps,
    gaps_since,
    list_counts,
    list_is_stale,
    sales_watched_from,
    servings_from,
)
from inventory.models import GapFillEntry, MovementKind, StockTake, StockType
from recipes.models import (
    PosProduct,
    PosProductDailyQuantity,
    Recipe,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
)
from tests.factories import (
    make_ingredient,
    make_movement,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
)

START = date(2026, 3, 1)
END = date(2026, 3, 31)
ZERO = Decimal("0")


def at(day: int, hour: int = 12) -> datetime:
    """A time on a day of March 2026."""
    return timezone.make_aware(datetime(2026, 3, day, hour, 0))


def line(recipe=12, count=1, price="3.30", **more) -> dict:
    """One line as `entry_lines` writes it: « Demi exemple » unless told otherwise."""
    return {
        "recipe": recipe,
        "name": "Demi exemple",
        "till": "DEMI EXEMPLE",
        "till_price": None,
        "count": count,
        "price": price,
        "fills": ["Blonde exemple"],
        **more,
    }


def without(written: dict, key: str) -> dict:
    return {name: value for name, value in written.items() if name != key}


def entry_of(lines) -> GapFillEntry:
    """An entry whose JSON holds `lines`, whatever they are - never saved."""
    return GapFillEntry(amount=Decimal("7.00"), total=Decimal("7.00"), lines=lines)


# Garbage an entry's JSON may hold instead of a list of lines.
NOT_A_LIST = ({"recipe": 12, "count": 2, "price": "3.30"}, "Demi exemple", 7, None, True)
# A list, but of things that are no line.
NOT_LINES = [1, "Demi exemple", None, ["Demi exemple"], 3.3]


# ---------------------------------------------------------------------------
# The list's sales: list_counts
# ---------------------------------------------------------------------------


class ListCountsTests(SimpleTestCase):
    """{recipe_id: servings} every entry of the list proposed, all
    together - what the planner builds the next amount on."""

    def test_the_list_s_sales_added_up_entry_after_entry(self):
        entries = [
            entry_of([line(12, 1), line(13, 1, "3.70")]),
            entry_of([line(14, 1, "2.30"), line(15, 1, "4.70")]),
            entry_of([line(12, 2), line(13, 3, "3.70")]),
        ]
        self.assertEqual(list_counts(entries), {12: 3, 13: 4, 14: 1, 15: 1})

    def test_no_entry_or_no_line_is_no_sale(self):
        self.assertEqual(list_counts([]), {})
        self.assertEqual(list_counts([entry_of([]), entry_of([])]), {})

    def test_a_line_that_does_not_read_is_left_out(self):
        good = line(12, 2)
        malformed = {
            "count as text": line(12, "3"),
            "count as a decimal number": line(12, 3.0),
            "count missing": without(line(12), "count"),
            "count null": line(12, None),
            "count zero": line(12, 0),
            "count negative": line(12, -4),
            "recipe missing": without(line(12, 5), "recipe"),
            "recipe as text": line("12", 5),
            "recipe null": line(None, 5),
            "recipe as a decimal number": line(12.0, 5),
        }
        for why, bad in malformed.items():
            with self.subTest(why):
                self.assertEqual(list_counts([entry_of([bad, good])]), {12: 2})

    def test_a_bool_is_neither_a_count_nor_a_recipe(self):
        # list_counts once checked isinstance(..., int), and a JSON true is a
        # Python True, an int. {"count": true} was read as one serving, and
        # {"recipe": true} as recipe pk 1 (True == 1 as a dict key): the
        # planner then built on sales the list never proposed. It reads
        # through entry_rows now, where a bool is no count and no recipe.
        for bad in (line(12, True), line(True, 2), line(12, False)):
            with self.subTest(bad=bad):
                self.assertEqual(list_counts([entry_of([bad, line(13, 2)])]), {13: 2})

    def test_garbage_json_is_skipped_as_entry_rows_skips_it(self):
        # list_counts once called line.get on whatever the JSON held, so an
        # entry whose lines were not a list of objects raised (AttributeError,
        # TypeError) - while entry_rows read the same JSON as no line. The
        # page calls list_counts on every visit and on every amount added
        # (views.stock_gap_filler and stock_gap_filler_add), so one such
        # entry broke the page until the list was cleared. It reads through
        # entry_rows now.
        for lines in (*NOT_A_LIST, NOT_LINES):
            with self.subTest(lines=lines):
                garbage = entry_of(lines)
                self.assertEqual(entry_rows(garbage), [])
                self.assertEqual(list_counts([garbage, entry_of([line(12, 2)])]), {12: 2})


# ---------------------------------------------------------------------------
# An entry's lines: entry_lines, then entry_rows
# ---------------------------------------------------------------------------


def a_line(recipe_id, name, till, till_price, count, price, fills) -> Line:
    return Line(
        recipe=SimpleNamespace(pk=recipe_id, name=name),
        till=TillButton(till, None if till_price is None else Decimal(till_price)),
        count=count,
        price=Decimal(price),
        fills=[ArticleGap(stock_type=StockType(name=filled)) for filled in fills],
    )


class EntryLinesRoundTripTests(SimpleTestCase):
    """What `entry_lines` writes, `entry_rows` reads back the same - through
    JSON, as the JSONField keeps it: names, till buttons, Decimal prices
    with their digits, the gaps each line fills."""

    def result(self) -> FillResult:
        lines = [
            # The till rings this one at 4,60 €, the recipe says 4,70 €.
            a_line(
                31, "Ti punch exemple", "TI PUNCH CAISSE EXEMPLE", "4.60", 2, "4.70", ["Rhum exemple", "Citron exemple"]
            ),
            a_line(30, "Demi exemple", "DEMI EXEMPLE", "3.30", 3, "3.30", ["Blonde exemple"]),
            # A button never read: no till price.
            a_line(32, "Shot exemple", "Shot exemple", None, 1, "3.70", []),
        ]
        return FillResult(plan=Plan({31: 2, 30: 3, 32: 1}, 2300, 0, EXACT), lines=lines)

    def test_the_json_an_entry_keeps(self):
        written = entry_lines(self.result())
        self.assertEqual(
            written[0],
            {
                "recipe": 31,
                "name": "Ti punch exemple",
                "till": "TI PUNCH CAISSE EXEMPLE",
                "till_price": "4.60",
                "count": 2,
                "price": "4.70",
                "fills": ["Rhum exemple", "Citron exemple"],
            },
        )
        self.assertIsNone(written[2]["till_price"])
        self.assertEqual([one["recipe"] for one in written], [31, 30, 32])
        # Plain JSON: nothing a JSONField cannot store.
        self.assertEqual(json.loads(json.dumps(written)), written)

    def test_what_is_written_is_what_is_read_back(self):
        result = self.result()
        rows = entry_rows(entry_of(json.loads(json.dumps(entry_lines(result)))))
        self.assertEqual(
            rows,
            [
                EntryLine(
                    "Ti punch exemple",
                    "TI PUNCH CAISSE EXEMPLE",
                    Decimal("4.60"),
                    2,
                    Decimal("4.70"),
                    ["Rhum exemple", "Citron exemple"],
                    31,
                ),
                EntryLine("Demi exemple", "DEMI EXEMPLE", Decimal("3.30"), 3, Decimal("3.30"), ["Blonde exemple"], 30),
                EntryLine("Shot exemple", "Shot exemple", None, 1, Decimal("3.70"), [], 32),
            ],
        )
        for row, original in zip(rows, result.lines, strict=True):
            with self.subTest(recipe=row.name):
                self.assertIsInstance(row.price, Decimal)
                self.assertEqual(str(row.price), str(original.price))
                self.assertEqual(row.total, original.total)
                self.assertEqual(row.till_differs, original.till_differs)
        self.assertEqual([row.till_differs for row in rows], [True, False, False])
        self.assertEqual(sum((row.total for row in rows), start=ZERO), result.total)

    def test_an_entry_line_s_total_and_till_price(self):
        row = EntryLine("Demi exemple", "DEMI EXEMPLE", None, 3, Decimal("3.30"), [])
        self.assertEqual(row.total, Decimal("9.90"))
        self.assertFalse(row.till_differs)
        self.assertIsNone(row.recipe_id)
        row.till_price = Decimal("3.3")
        self.assertFalse(row.till_differs)
        row.till_price = Decimal("3.10")
        self.assertTrue(row.till_differs)


class EntryRowsGarbageTests(SimpleTestCase):
    """`entry_rows` reads whatever an entry's JSON holds: a line that does
    not read is left out, never a broken page."""

    def test_lines_that_are_not_a_list_read_as_no_line(self):
        for lines in NOT_A_LIST:
            with self.subTest(lines=lines):
                self.assertEqual(entry_rows(entry_of(lines)), [])

    def test_a_line_that_is_not_an_object_is_left_out(self):
        rows = entry_rows(entry_of([*NOT_LINES, line(12, 2)]))
        self.assertEqual([(row.recipe_id, row.count) for row in rows], [(12, 2)])

    def test_a_price_that_is_no_number_leaves_its_line_out(self):
        for price in ("abc", "", "3,30", "3.30 €", None, [3.3], {"prix": "3.30"}, True):
            with self.subTest(price=price):
                rows = entry_rows(entry_of([line(12, 2, price), line(13, 1, "3.70")]))
                self.assertEqual([row.recipe_id for row in rows], [13])
        rows = entry_rows(entry_of([without(line(12, 2), "price"), line(13, 1, "3.70")]))
        self.assertEqual([row.recipe_id for row in rows], [13])

    def test_a_price_that_is_nan_or_infinite_is_no_price(self):
        # _decimal once read "NaN", "sNaN" and "Infinity" as Decimals - the
        # one kind of « no number » Decimal() does not refuse - and the line
        # was kept. A signalling NaN made EntryLine.total and .till_differs
        # raise InvalidOperation (the template reads both on the last entry:
        # the page broke); a quiet NaN or an infinity printed « NaN € » or
        # « Infinity € ». It refuses them now, as the codebase's own amount
        # reader does (common.read_amount: no NaN, no Infinity).
        for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(price=value):
                rows = entry_rows(entry_of([line(12, 2, value), line(13, 1, "3.70")]))
                self.assertEqual([row.recipe_id for row in rows], [13])
            with self.subTest(till_price=value):
                (row,) = entry_rows(entry_of([line(12, 2, "3.30", till_price=value)]))
                self.assertIsNone(row.till_price)

    def test_a_count_that_is_no_whole_number_leaves_its_line_out(self):
        for count in ("2", 2.0, 2.5, None, [2]):
            with self.subTest(count=count):
                rows = entry_rows(entry_of([line(12, count), line(13, 1, "3.70")]))
                self.assertEqual([row.recipe_id for row in rows], [13])
        rows = entry_rows(entry_of([without(line(12, 2), "count"), line(13, 1, "3.70")]))
        self.assertEqual([row.recipe_id for row in rows], [13])

    def test_what_else_is_missing_or_wrong_reads_as_nothing(self):
        (row,) = entry_rows(entry_of([{"count": 2, "price": "3.30"}]))
        self.assertEqual(row, EntryLine("", "", None, 2, Decimal("3.30"), [], None))
        # A till price that is no number is no till price; a fill that is no
        # name is dropped; a recipe that is no pk links nowhere.
        (row,) = entry_rows(
            entry_of([line("12", 2, till_price="abc", fills=["Blonde exemple", 7, None, ["x"], "Citron exemple"])])
        )
        self.assertIsNone(row.till_price)
        self.assertFalse(row.till_differs)
        self.assertEqual(row.fills, ["Blonde exemple", "Citron exemple"])
        self.assertIsNone(row.recipe_id)
        self.assertEqual(
            (row.name, row.till, row.count, row.total), ("Demi exemple", "DEMI EXEMPLE", 2, Decimal("6.60"))
        )

    def test_fills_that_are_no_list_read_as_no_fills(self):
        # entry_rows once iterated line.get("fills", []) as it came, so a
        # "fills": null - or a number - raised TypeError out of entry_rows
        # itself, the one reader meant to survive whatever was stored.
        for fills in (None, 7, 3.5, True):
            with self.subTest(fills=fills):
                (row,) = entry_rows(entry_of([line(12, 2, fills=fills)]))
                self.assertEqual(row.fills, [])

    def assert_no_price(self, value):
        """A line priced `value` is left out, and `value` as a till price is
        no till price."""
        with self.subTest(price=value):
            entry = entry_of([line(12, 2, value), line(13, 1, "3.70")])
            self.assertEqual([row.recipe_id for row in entry_rows(entry)], [13])
            self.assertEqual(list_counts([entry]), {13: 1})
            self.assertEqual(entry.sales, 1)
        with self.subTest(till_price=value):
            (row,) = entry_rows(entry_of([line(12, 2, "3.30", till_price=value)]))
            self.assertIsNone(row.till_price)
            self.assertFalse(row.till_differs)
            self.assertEqual(row.total, Decimal("6.60"))

    def test_a_price_of_ten_billion_or_more_is_no_price(self):
        # « 12345678901 » is a price no column holds. Bounded like a price
        # column now (10^10, read_amount's): the line is left out, a till
        # price is no till price - as text or as a JSON number.
        for value in ("12345678901", "10000000000", "-10000000000", "1E+10", 12345678901, 1e300):
            self.assert_no_price(value)
        # Just under the bound, a price still.
        (row,) = entry_rows(entry_of([line(12, 1, "9999999999.99", till_price="9999999999.99")]))
        self.assertEqual((row.price, row.till_price), (Decimal("9999999999.99"), Decimal("9999999999.99")))

    def test_a_price_past_the_decimal_exponent_is_no_price(self):
        # A stored « 1E+999999999 » is a finite Decimal, and the first sum
        # past the context's exponent (Emax 999 999) raised Overflow out of
        # EntryLine.total. The bound was meant to leave such a line out.
        # Its first bound, `abs(number) < 10 ** 10`, was context arithmetic:
        # abs() raised decimal.Overflow itself on « 1E+999999999 », out of
        # entry_rows - list_counts, the page and the add with it. It is
        # measured with number.copy_abs() now, which needs no context.
        for value in ("1E+999999999", "-1E+999999999"):
            self.assert_no_price(value)


class EntryLinesFromAPlanTests(TestCase):
    """A real plan kept and read back from the database: three recipes of
    one rum, 31,70 €, 33,90 € and 36,30 € - 101,90 € is one of each and no
    other mix. The punch's button rings 35,10 € at the till, the cocktail's
    its own price, the grog's was never read."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.rum = make_stock_type(name="Rhum exemple")
        cls.syrup = make_stock_type(name="Sirop exemple")
        for stock_type, quantity, cost in ((cls.rum, "2", "20.00"), (cls.syrup, "1", "8.00")):
            make_movement(
                stock_type=stock_type,
                quantity=quantity,
                unit_cost_ht=cost,
                kind=MovementKind.PURCHASE,
                occurred_on=date(2026, 2, 20),
            )
            make_stock_take_line(
                stock_take=cls.take,
                product=None,
                stock_type=stock_type,
                counted_quantity=quantity,
                unit=stock_type.unit,
            )
        cls.punch = make_recipe(name="Punch exemple", selling_price_ttc="36.30")
        make_ingredient(cls.punch, stock_type=cls.rum, quantity="0.04", group=0)
        make_ingredient(cls.punch, stock_type=cls.syrup, quantity="0.02", group=1)
        cls.cocktail = make_recipe(name="Cocktail exemple", selling_price_ttc="33.90")
        make_ingredient(cls.cocktail, stock_type=cls.rum, quantity="0.05", group=0)
        cls.grog = make_recipe(name="Grog exemple", selling_price_ttc="31.70")
        make_ingredient(cls.grog, stock_type=cls.rum, quantity="0.04", group=0)
        for made, sold in ((cls.punch, 3), (cls.cocktail, 2), (cls.grog, 2)):
            RecipeSale.objects.create(recipe=made, sold_on=date(2026, 3, 15), quantity=sold, source="manual")
        punch_button = PosProduct.objects.create(name="PUNCH CAISSE EXEMPLE", recipe=cls.punch, total_quantity=3)
        PosProductDailyQuantity.objects.create(
            product=punch_button,
            sold_on=date(2026, 3, 10),
            quantity=3,
            revenue_ttc=Decimal("105.30"),
            revenue_read=True,
        )
        cocktail_button = PosProduct.objects.create(
            name="COCKTAIL CAISSE EXEMPLE", recipe=cls.cocktail, total_quantity=2
        )
        PosProductDailyQuantity.objects.create(
            product=cocktail_button,
            sold_on=date(2026, 3, 10),
            quantity=2,
            revenue_ttc=Decimal("67.80"),
            revenue_read=True,
        )
        PosProduct.objects.create(name="GROG CAISSE EXEMPLE", recipe=cls.grog, total_quantity=2)

    def kept(self):
        report = gaps_since(self.take, end=END)
        result = fill_gaps(report, Decimal("101.90"))
        entry = GapFillEntry.objects.create(
            stock_take=self.take,
            amount=Decimal("101.90"),
            total=result.total,
            lines=entry_lines(result),
            sales_up_to=report.last_sale_day,
        )
        entry.refresh_from_db()
        return result, entry

    def test_the_plan(self):
        result, entry = self.kept()
        self.assertEqual(result.plan.reason, EXACT)
        self.assertEqual(result.plan.counts, {self.punch.pk: 1, self.cocktail.pk: 1, self.grog.pk: 1})
        self.assertEqual(entry.total, Decimal("101.90"))
        self.assertEqual(entry.remainder, ZERO)
        self.assertEqual(entry.sales, 3)

    def test_read_back_as_proposed(self):
        result, entry = self.kept()
        rows = entry_rows(entry)
        self.assertEqual(len(rows), len(result.lines))
        for row, proposed in zip(rows, result.lines, strict=True):
            with self.subTest(recipe=row.name):
                self.assertEqual(row.recipe_id, proposed.recipe.pk)
                self.assertEqual(row.name, proposed.recipe.name)
                self.assertEqual((row.till, row.till_price), (proposed.till.name, proposed.till.price))
                self.assertEqual((row.count, row.price, row.total), (proposed.count, proposed.price, proposed.total))
                self.assertEqual(row.fills, [article.stock_type.name for article in proposed.fills])
                self.assertEqual(row.till_differs, proposed.till_differs)
        self.assertEqual(
            [(row.name, row.till, row.till_price, row.price, row.till_differs) for row in rows],
            [
                ("Punch exemple", "PUNCH CAISSE EXEMPLE", Decimal("35.10"), Decimal("36.30"), True),
                ("Cocktail exemple", "COCKTAIL CAISSE EXEMPLE", Decimal("33.90"), Decimal("33.90"), False),
                ("Grog exemple", "GROG CAISSE EXEMPLE", None, Decimal("31.70"), False),
            ],
        )
        self.assertEqual(rows[0].fills, ["Rhum exemple", "Sirop exemple"])

    def test_a_recipe_renamed_or_repriced_later_does_not_rewrite_the_entry(self):
        _result, entry = self.kept()
        before = entry_rows(entry)
        Recipe.objects.filter(pk=self.punch.pk).update(name="Punch renommé exemple", selling_price_ttc=Decimal("38.10"))
        PosProduct.objects.filter(recipe=self.punch).update(name="PUNCH NOUVEAU EXEMPLE")
        entry.refresh_from_db()
        self.assertEqual(entry_rows(entry), before)
        self.assertEqual(before[0].name, "Punch exemple")


# ---------------------------------------------------------------------------
# Sales imported since an entry: list_is_stale
# ---------------------------------------------------------------------------


def nothing_consumed(extra: dict[int, int]) -> dict[int, Decimal]:
    return {}


def report_with(last_sale_day: date | None) -> GapReport:
    """A report with nothing in it but the last day of till sales imported."""
    return GapReport(
        take=StockTake(taken_at=at(1)),
        start=START,
        end=END,
        articles={},
        offers=[],
        recipes={},
        till_buttons={},
        blocked=[],
        not_sold_since=[],
        consumption=nothing_consumed,
        last_sale_day=last_sale_day,
    )


def made_with(sales_up_to: date | None) -> GapFillEntry:
    return GapFillEntry(amount=Decimal("7.00"), total=Decimal("7.00"), sales_up_to=sales_up_to)


class ListIsStaleTests(SimpleTestCase):
    """Till sales newer than those known when an entry was made: if that
    entry was rung up, its sales are in them, and the list counts them
    twice."""

    DAY = date(2026, 3, 20)

    def test_no_entry_is_never_stale(self):
        for day in (None, self.DAY):
            with self.subTest(last_sale_day=day):
                self.assertFalse(list_is_stale(report_with(day), []))

    def test_with_no_sale_imported_nothing_counts_twice(self):
        for up_to in (None, self.DAY, self.DAY - timedelta(days=5)):
            with self.subTest(sales_up_to=up_to):
                self.assertFalse(list_is_stale(report_with(None), [made_with(up_to)]))

    def test_the_same_last_sale_day_is_not_stale(self):
        self.assertFalse(list_is_stale(report_with(self.DAY), [made_with(self.DAY)]))

    def test_a_newer_sale_day_is_stale(self):
        self.assertTrue(list_is_stale(report_with(self.DAY + timedelta(days=1)), [made_with(self.DAY)]))

    def test_an_entry_made_before_any_sale_was_imported_is_stale_once_one_is(self):
        self.assertTrue(list_is_stale(report_with(self.DAY), [made_with(None)]))

    def test_one_stale_entry_is_enough(self):
        later = self.DAY + timedelta(days=2)
        self.assertTrue(list_is_stale(report_with(later), [made_with(later), made_with(self.DAY), made_with(later)]))
        self.assertFalse(list_is_stale(report_with(later), [made_with(later), made_with(later)]))

    def test_sales_taken_out_since_an_entry_do_not_make_it_stale(self):
        # The last sale day went back (an import undone): nothing newer.
        self.assertFalse(list_is_stale(report_with(self.DAY), [made_with(self.DAY + timedelta(days=3))]))


def sold(recipe, day: int, quantity, source: str = "caisse") -> RecipeSale:
    """`quantity` of `recipe` sold on `day` of March 2026, as imported."""
    return RecipeSale.objects.create(recipe=recipe, sold_on=date(2026, 3, day), quantity=quantity, source=source)


def sold_on_a_slip(day: int, recipe=None, stock_type=None, quantity="2") -> SaleDocumentLine:
    """One line of a sale document of `day` of March 2026: a recipe, or an
    article sold as it is."""
    document = SaleDocument.objects.create(sold_on=date(2026, 3, day))
    return SaleDocumentLine.objects.create(
        document=document, recipe=recipe, stock_type=stock_type, quantity=Decimal(quantity)
    )


class SalesWatchedFromTests(SimpleTestCase):
    """The first day whose sales may hold a list made on a day: the day
    before - the till files a sale rung after midnight under the day its
    service began."""

    def test_the_day_before(self):
        for made, watched in (
            (date(2026, 3, 15), date(2026, 3, 14)),
            (date(2026, 3, 1), date(2026, 2, 28)),
            (date(2028, 3, 1), date(2028, 2, 29)),
            (date(2027, 1, 1), date(2026, 12, 31)),
        ):
            with self.subTest(made=made):
                self.assertEqual(sales_watched_from(made), watched)


class ListIsStaleOnSalesSeenTests(TestCase):
    """An entry keeps every serving sold from the day BEFORE it was made on
    (`sales_from`, `gaps.sales_watched_from`), as known then (`sales_seen`,
    `gaps.servings_from`): the list is stale once that moves - a newer day
    imported, the entry's own day or the day before imported late (the till
    files a sale rung after midnight under the day its service began), or
    one of those imported again with more sales, which leaves the last day
    of sales where it was - and not for a late import of an OLDER day, which
    holds none of its sales. An entry missing either falls back on the last
    day of sales (`sales_up_to`).

    A pint at 33,70 €, sold on 10 March and imported; each entry made in the
    evening of its day."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = make_stock_type(name="Blonde exemple")
        make_stock_take_line(
            stock_take=cls.take, product=None, stock_type=cls.blonde, counted_quantity="100", unit=cls.blonde.unit
        )
        cls.pint = make_recipe(name="Pinte exemple", selling_price_ttc="33.70")
        make_ingredient(cls.pint, stock_type=cls.blonde, quantity="0.5")
        sold(cls.pint, 10, 4)

    def report(self) -> GapReport:
        return gaps_since(self.take, end=END)

    def made_on(self, day: int, hour: int = 20) -> GapFillEntry:
        """An entry made on `day` of March, kept as the view keeps it - what
        was sold from the day before on, as known now."""
        report = self.report()
        watched_from = sales_watched_from(date(2026, 3, day))
        entry = GapFillEntry.objects.create(
            stock_take=self.take,
            amount=Decimal("33.70"),
            total=Decimal("33.70"),
            lines=[line(self.pint.pk, 1, "33.70")],
            sales_up_to=report.last_sale_day,
            sales_from=watched_from,
            sales_seen=servings_from(watched_from, report.end),
        )
        GapFillEntry.objects.filter(pk=entry.pk).update(created_at=at(day, hour))
        entry.refresh_from_db()
        return entry

    def stale(self, *entries: GapFillEntry) -> bool:
        return list_is_stale(self.report(), list(entries))

    def test_fresh_as_made(self):
        entry = self.made_on(15)
        self.assertEqual(entry.sales_up_to, date(2026, 3, 10))
        self.assertEqual(entry.sales_from, date(2026, 3, 14))
        self.assertEqual(entry.sales_seen, ZERO)
        self.assertFalse(self.stale(entry))

    def test_a_late_import_of_an_older_day_is_not_stale(self):
        entry = self.made_on(15)
        # Newer than the last day known then, older than the day before the
        # entry's.
        sold(self.pint, 12, 3)
        sold_on_a_slip(13, recipe=self.pint)
        # The last day known, again with more sales.
        RecipeSale.objects.filter(recipe=self.pint, sold_on=date(2026, 3, 10)).update(quantity=9)
        report = self.report()
        self.assertEqual(report.last_sale_day, date(2026, 3, 12))
        self.assertFalse(list_is_stale(report, [entry]))

    def test_a_late_import_of_the_day_before_is_stale(self):
        # Reversed on purpose (it used to be « older than the entry, not
        # stale »): the till files a sale rung after midnight under the day
        # its service began, so a list made and rung up at 00:30 on the 15th
        # is in the 14th's sales. Watched from the calendar day, it was never
        # flagged; a list made in the evening now reads a late import of the
        # day before as stale too - a false alarm, never a double count gone
        # silent.
        entry = self.made_on(15)
        sold(self.pint, 14, 5)
        self.assertTrue(self.stale(entry))

    def test_a_list_made_after_midnight_is_in_the_day_before_s_sales(self):
        entry = self.made_on(16, hour=0)
        self.assertEqual(entry.sales_from, date(2026, 3, 15))
        self.assertFalse(self.stale(entry))
        # The service of the 15th, imported the next morning: it holds the
        # list rung up at midnight.
        sold(self.pint, 15, 6)
        self.assertTrue(self.stale(entry))

    def test_the_entry_s_own_day_imported_late_is_stale(self):
        entry = self.made_on(15)
        sold(self.pint, 15, 2)
        self.assertTrue(self.stale(entry))

    def test_a_newer_day_imported_is_stale(self):
        entry = self.made_on(15)
        sold(self.pint, 16, 2)
        self.assertTrue(self.stale(entry))

    def test_the_entry_s_day_imported_again(self):
        sale = sold(self.pint, 15, 4)  # imported in the afternoon
        entry = self.made_on(15)
        self.assertEqual((entry.sales_up_to, entry.sales_seen), (date(2026, 3, 15), Decimal("4")))
        # The same import run again: nothing moved.
        RecipeSale.objects.filter(pk=sale.pk).update(quantity=4)
        self.assertFalse(self.stale(entry))
        # With the list's two pints rung up since: moved, though the last day
        # of sales has not.
        RecipeSale.objects.filter(pk=sale.pk).update(quantity=6)
        report = self.report()
        self.assertEqual(report.last_sale_day, date(2026, 3, 15))
        self.assertTrue(list_is_stale(report, [entry]))

    def test_a_sale_document_counts_its_recipes_not_its_articles(self):
        entry = self.made_on(15)
        # A bottle sold as it is: no recipe the list could have rung up.
        sold_on_a_slip(16, stock_type=self.blonde, quantity="0.7")
        self.assertFalse(self.stale(entry))
        sold_on_a_slip(16, recipe=self.pint)
        self.assertTrue(self.stale(entry))

    def test_a_fraction_of_a_serving_reads_back_the_same(self):
        # `sales_seen` keeps four decimals: what was seen, read back from the
        # database, is what is counted again.
        sold_on_a_slip(15, recipe=self.pint, quantity="1.2500")
        sold(self.pint, 15, 3)
        entry = self.made_on(15)
        self.assertEqual(entry.sales_seen, Decimal("4.25"))
        self.assertFalse(self.stale(entry))

    def test_one_entry_whose_sales_moved_is_enough(self):
        early, late = self.made_on(12), self.made_on(20)
        # Before both, and before the day before either: the 11th, which
        # this used to import here, is the day before the first entry's now
        # (test_a_late_import_of_the_day_before_is_stale).
        sold(self.pint, 9, 2)
        self.assertFalse(self.stale(early, late))
        sold(self.pint, 15, 2)  # after the first entry's day, before the day before the second's
        self.assertFalse(self.stale(late))
        self.assertTrue(self.stale(early, late))
        self.assertTrue(self.stale(late, early))

    def test_two_entries_of_one_day_with_an_import_between_them(self):
        # The day imported after the first entry, before the second: the
        # first one's sales may be in it, the second was planned on them.
        first = self.made_on(15, hour=18)
        sold(self.pint, 15, 2)
        second = self.made_on(15, hour=21)
        self.assertEqual((first.sales_seen, second.sales_seen), (ZERO, Decimal("2")))
        self.assertFalse(self.stale(second))
        self.assertTrue(self.stale(first, second))
        self.assertTrue(self.stale(second, first))

    def assert_the_last_sale_day_decides(self, missing: str):
        """An entry made on the 15th whose `missing` field was never kept is
        judged on the last day of sales alone."""
        entry = self.made_on(15)
        GapFillEntry.objects.filter(pk=entry.pk).update(**{missing: None})
        entry.refresh_from_db()
        self.assertFalse(self.stale(entry))
        # The last day again, with more sales: the last day alone cannot see
        # it.
        sold(self.pint, 10, 3, source="manual")
        self.assertFalse(self.stale(entry))
        # A newer day, even one older than the entry, reads as newer sales.
        sold(self.pint, 12, 1)
        self.assertTrue(self.stale(entry))

    def test_without_sales_seen_the_last_sale_day_decides(self):
        # An entry kept before `sales_seen` existed.
        self.assert_the_last_sale_day_decides("sales_seen")

    def test_without_sales_from_the_last_sale_day_decides(self):
        # What was seen, with no day it was counted from, says nothing of
        # which days to count again.
        self.assert_the_last_sale_day_decides("sales_from")

    def test_an_entry_never_saved_without_a_day_falls_back_too(self):
        # It used to read its day off `created_at`, None until saved; the day
        # is `sales_from` now, and an entry holding only `sales_seen` falls
        # back the same way.
        unsaved = GapFillEntry(
            amount=Decimal("33.70"), total=Decimal("33.70"), sales_up_to=date(2026, 3, 10), sales_seen=ZERO
        )
        self.assertIsNone(unsaved.created_at)
        self.assertIsNone(unsaved.sales_from)
        self.assertFalse(self.stale(unsaved))
        sold(self.pint, 12, 1)
        self.assertTrue(self.stale(unsaved))


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class GapFillEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.other = make_stock_take(taken_at=at(2))

    def add(self, take=None, amount="7.00", total="7.00", lines=None, **fields) -> GapFillEntry:
        return GapFillEntry.objects.create(
            stock_take=take or self.take,
            amount=Decimal(amount),
            total=Decimal(total),
            lines=[] if lines is None else lines,
            **fields,
        )

    def test_a_new_entry_s_defaults(self):
        entry = GapFillEntry.objects.create(stock_take=self.take, amount=Decimal("7.00"), total=Decimal("6.60"))
        entry.refresh_from_db()
        self.assertEqual(entry.lines, [])
        self.assertEqual(entry.reason, "")
        self.assertIsNone(entry.sales_up_to)
        self.assertIsNone(entry.sales_from)
        self.assertIsNone(entry.sales_seen)
        self.assertIsNotNone(entry.created_at)
        self.assertEqual(entry.sales, 0)
        self.assertEqual(list(self.take.gap_fill_entries.all()), [entry])

    def test_the_remainder_is_the_amount_less_what_was_proposed(self):
        cases = [
            ("7.00", "7.00", "0.00"),
            ("7.00", "6.60", "0.40"),
            ("150.50", "0.00", "150.50"),
            ("10000.00", "9999.99", "0.01"),
        ]
        for amount, total, remainder in cases:
            with self.subTest(amount=amount, total=total):
                entry = self.add(amount=amount, total=total)
                entry.refresh_from_db()
                self.assertIsInstance(entry.remainder, Decimal)
                self.assertEqual(entry.remainder, Decimal(remainder))

    def test_sales_is_every_line_s_count(self):
        entry = self.add(lines=[line(12, 2), line(13, 1, "3.70"), line(14, 4, "2.30")])
        entry.refresh_from_db()
        self.assertEqual(entry.sales, 7)
        self.assertEqual(entry.sales, sum(row.count for row in entry_rows(entry)))

    def test_the_lines_come_back_from_the_database_as_written(self):
        written = [
            line(12, 2, "3.30", till_price="3.10"),
            line(13, 1, "33.90", name="Café « maison » exemple", till="CAFÉ EXEMPLE", fills=[]),
        ]
        entry = self.add(lines=written, reason="no_combination", sales_up_to=date(2026, 3, 30))
        entry.refresh_from_db()
        self.assertEqual(entry.lines, written)
        self.assertEqual(entry.reason, "no_combination")
        self.assertEqual(entry.sales_up_to, date(2026, 3, 30))

    def test_ordered_by_creation_then_pk(self):
        first, second, third = self.add(amount="7.00"), self.add(amount="7.10"), self.add(amount="7.20")
        GapFillEntry.objects.filter(pk=first.pk).update(created_at=at(5, 14))
        GapFillEntry.objects.filter(pk=second.pk).update(created_at=at(5, 9))
        GapFillEntry.objects.filter(pk=third.pk).update(created_at=at(5, 14))
        self.assertEqual(list(self.take.gap_fill_entries.all()), [second, first, third])
        self.assertEqual(list(GapFillEntry.objects.all()), [second, first, third])

    def test_the_entries_go_with_their_stock_take(self):
        mine = [self.add(), self.add(amount="14.00")]
        theirs = self.add(take=self.other)
        self.take.delete()
        self.assertFalse(GapFillEntry.objects.filter(pk__in=[entry.pk for entry in mine]).exists())
        self.assertEqual(list(GapFillEntry.objects.all()), [theirs])
        self.assertEqual(list(self.other.gap_fill_entries.all()), [theirs])


class EntrySalesGarbageTests(SimpleTestCase):
    def test_sales_reads_garbage_json_as_entry_rows_does(self):
        # GapFillEntry.sales once summed int(line.get("count", 0)) over
        # whatever the JSON held: lines that were not a list of objects, or a
        # count that was no number, raised (AttributeError, TypeError,
        # ValueError). views.stock_gap_filler sums entry.sales over the list
        # for its « Ventes » figure on every visit, so the page broke where
        # entry_rows read the same entry fine. It goes through entry_rows now.
        for lines in (*NOT_A_LIST, NOT_LINES, [line(12, "deux"), line(13, 2, "3.70")]):
            with self.subTest(lines=lines):
                entry = entry_of(lines)
                self.assertEqual(entry.sales, sum(row.count for row in entry_rows(entry)))
