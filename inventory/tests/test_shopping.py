"""« Prévoir les courses »: the pure module on its own (`inventory/shopping.py`).

No database, no request: every Prepared is built through `Prepared.build`
from rows written here, and the expected figures are worked out in this file
with plain arithmetic (`ew_rate`, `weight`, `formula`), never through the
module's own helpers.

Invented data: every article, store, quantity and price below is made up
(« Sirop de cassis exemple », « Grossiste exemple »…), prices are above
30 €, and the dates are counted back from a fixed invented day, TODAY.
"""

from __future__ import annotations

import dataclasses
import math
import re
import time
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from inventory import shopping
from inventory.shopping import (
    BOUGHT_ELSEWHERE,
    CALENDAR_CLOCK,
    DEPOSIT,
    DEPOSIT_LIKE,
    DEPOSIT_SENTENCE,
    DROPPED_LABEL,
    ELSEWHERE,
    MAYBE,
    NEW,
    OK,
    PAUSED_LABEL,
    QUIET,
    SHORT_HISTORY_LABEL,
    SPAN_ADOPTION,
    SPAN_LAST,
    SPAN_MEMORY,
    TILL_CLOCK,
    TO_BUY,
    ArticleInfo,
    Exclusions,
    Prepared,
    PurchaseRow,
    ReturnRow,
    Settings,
    StoreInfo,
    TillData,
    chance_of,
    confidence_of,
    logit,
    plan_store,
    rhythms,
    score_article,
    sigmoid,
    store_choices,
)

TODAY = date(2031, 3, 12)

WHOLESALER, GROCER, CORNER = 7, 8, 9  # CORNER is a store the page does not offer
STORES = (StoreInfo(WHOLESALER, "Grossiste exemple"), StoreInfo(GROCER, "Épicerie exemple"))

CUPS, SYRUP, KEG, LEMONS, DEPOSIT_KEG, VODKA, TONIC, NAPKINS = range(1, 9)
ARTICLES = (
    ArticleInfo(CUPS, "Gobelets exemple", "UNIT", "Consommables exemple"),
    ArticleInfo(SYRUP, "Sirop de cassis exemple", "L", "Sirops exemple"),
    ArticleInfo(KEG, "Fût de blonde exemple", "L", "Bières exemple"),
    ArticleInfo(LEMONS, "Citrons exemple", "KG", ""),
    ArticleInfo(DEPOSIT_KEG, "Consigne fût exemple", "UNIT", "Consignes exemple"),
    ArticleInfo(VODKA, "Vodka exemple", "L", "Spiritueux exemple"),
    ArticleInfo(TONIC, "Tonic exemple", "UNIT", "Softs exemple"),
    ArticleInfo(NAPKINS, "Serviettes exemple", "UNIT", "Consommables exemple"),
)

DEFAULT = Settings()
R = 0.5 ** (1 / 90)


def ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def bought(article, store, days_ago, qty="3", value="45.00", product=None, units=None, colisage=1) -> PurchaseRow:
    return PurchaseRow(
        article_id=article,
        store_id=store,
        day=ago(days_ago),
        qty=Decimal(qty),
        value_ht=Decimal(value),
        product_id=product,
        product_units=Decimal(units if units is not None else qty),
        colisage=colisage,
    )


def visits(days_ago, store=WHOLESALER):
    """A visit on each day: a box of cups bought there."""
    return [bought(CUPS, store, days, "1", "36.00") for days in days_ago]


def prepare(rows, today=TODAY, **kw) -> Prepared:
    kw.setdefault("articles", ARTICLES)
    kw.setdefault("stores", STORES)
    return Prepared.build(today=today, purchases=rows, **kw)


def every_day_till(qty, first, last, article):
    """The till sold `qty` of the article on each day from `first` to `last` days ago."""
    return {article: [(ago(days), qty) for days in range(first, last - 1, -1)]}


def till(series, covered=1, start=400, ref=1):
    return TillData(series=series, start=ago(start), covered_until=ago(covered), ref_day=ago(ref))


def ew_rate(points, first, end):
    """The study's rate, written out: a recency-weighted mean per day over [first, end)."""
    n_days = (end - first).days
    last = end - timedelta(days=1)
    num = sum(qty * R ** (last - day).days for day, qty in points if first <= day < end)
    return num / ((1 - R**n_days) / (1 - R))


def weight(days, memory_days=180):
    return 0.5 ** (days / memory_days)


def formula(habit, need, few):
    p = min(max(habit, 1e-4), 1 - 1e-4)
    ln_need = math.log(min(max(need, 0.1), 2.0)) if need is not None else 0.0
    z = math.log(p / (1 - p)) + 0.39 + 0.82 * ln_need - (2.0 if few else 0.0)
    return 1 / (1 + math.exp(-z))


def extra_articles(count, first=100):
    """`count` more invented articles, ids from `first`."""
    return [ArticleInfo(first + i, f"Article exemple {i}", "UNIT", "Exemple") for i in range(count)]


def lines_of(plan):
    return (*plan.to_buy, *plan.maybe, *plan.new, *plan.quiet, *plan.elsewhere, *plan.deposits)


def section_of(plan, article):
    found = [line.section for line in lines_of(plan) if line.article_id == article]
    return found[0] if found else None


# --------------------------------------------------------------------- the nag rule's fixtures
def lifted_line_rows():
    """Store visited every 14 days; the syrup bought there 3 times, the last one small: the
    habit alone would not list it, its need does - at each of the last 3 visits, unbought."""
    return [
        *visits(range(14, 211, 14)),
        bought(SYRUP, WHOLESALER, 210, "100"),
        bought(SYRUP, WHOLESALER, 154, "100"),
        bought(SYRUP, WHOLESALER, 56, "5"),
    ]


def habit_line_rows(stopped):
    """Tonic bought at every visit (every 14 days) until `stopped` days ago."""
    return [
        *visits(range(14, 211, 14)),
        *(bought(TONIC, WHOLESALER, d, "3") for d in range(14, 211, 14) if d >= stopped),
    ]


class BuilderTests(SimpleTestCase):
    def test_rows_after_today_undated_or_without_a_quantity_are_left_out(self):
        rows = [
            bought(SYRUP, WHOLESALER, 10),
            PurchaseRow(SYRUP, WHOLESALER, TODAY + timedelta(days=1), Decimal("2")),
            PurchaseRow(SYRUP, WHOLESALER, None, Decimal("2")),
            PurchaseRow(SYRUP, WHOLESALER, ago(5), None),
            PurchaseRow(SYRUP, WHOLESALER, ago(4), Decimal("0")),
        ]
        prepared = prepare(rows)
        self.assertEqual(prepared.buys[SYRUP].days, (ago(10),))
        self.assertEqual(prepared.visits[WHOLESALER], (ago(10),))

    def test_a_row_dated_today_is_history(self):
        prepared = prepare([bought(SYRUP, WHOLESALER, 0)])
        self.assertEqual(prepared.buys[SYRUP].days, (TODAY,))
        self.assertEqual(prepared.visits[WHOLESALER], (TODAY,))

    def test_a_negative_row_is_a_return_never_a_purchase_nor_a_visit(self):
        prepared = prepare([bought(DEPOSIT_KEG, WHOLESALER, 20, "4"), bought(DEPOSIT_KEG, GROCER, 10, "-3")])
        self.assertEqual(prepared.buys[DEPOSIT_KEG].days, (ago(20),))
        self.assertEqual(prepared.returned[DEPOSIT_KEG].cum, (Decimal("0"), Decimal("3")))
        self.assertNotIn(GROCER, prepared.visits)

    def test_returns_read_apart_count_in_absolute_value(self):
        prepared = prepare(
            [bought(DEPOSIT_KEG, WHOLESALER, 20, "4")],
            returns=[
                ReturnRow(DEPOSIT_KEG, WHOLESALER, ago(15), Decimal("-2")),
                ReturnRow(DEPOSIT_KEG, WHOLESALER, ago(12), Decimal("1.5")),
                ReturnRow(DEPOSIT_KEG, WHOLESALER, ago(11), Decimal("0")),
                ReturnRow(DEPOSIT_KEG, WHOLESALER, TODAY + timedelta(days=2), Decimal("-9")),
            ],
        )
        self.assertEqual(prepared.returned[DEPOSIT_KEG].cum[-1], Decimal("3.5"))

    def test_a_float_quantity_is_refused(self):
        with self.assertRaises(TypeError):
            prepare([PurchaseRow(SYRUP, WHOLESALER, ago(3), 2.5)])  # ty: ignore[invalid-argument-type]

    def test_one_article_bought_twice_on_a_day_at_a_store_is_one_purchase(self):
        prepared = prepare(
            [
                bought(SYRUP, WHOLESALER, 5, "2", "45.00", product=101, units="6", colisage=6),
                bought(SYRUP, WHOLESALER, 5, "1.5", "38.50", product=101, units="12", colisage=6),
                bought(SYRUP, WHOLESALER, 5, "1", "31.00", product=102, units="1"),
            ]
        )
        (buy,) = prepared.buys_at[(SYRUP, WHOLESALER)].buys
        self.assertEqual(buy.qty, Decimal("4.5"))
        self.assertEqual(buy.value_ht, Decimal("114.50"))
        self.assertEqual(
            [(p.product_id, p.units, p.colisage) for p in buy.products],
            [(101, Decimal("18"), Decimal("6")), (102, Decimal("1"), Decimal("1"))],
        )

    def test_one_day_at_two_stores_is_one_purchase_day_with_both(self):
        prepared = prepare([bought(SYRUP, WHOLESALER, 5, "2"), bought(SYRUP, GROCER, 5, "1")])
        (day,) = prepared.buys[SYRUP].buys
        self.assertEqual(day.stores, frozenset({WHOLESALER, GROCER}))
        self.assertEqual(day.qty, Decimal("3"))
        self.assertEqual(prepared.buys_at[(SYRUP, GROCER)].buys[0].qty, Decimal("1"))

    def test_a_visit_counts_any_article_but_an_unknown_one_is_no_candidate(self):
        prepared = prepare([bought(99, GROCER, 5)])
        self.assertEqual(prepared.visits[GROCER], (ago(5),))
        self.assertNotIn(99, prepared.buys)
        self.assertEqual(prepared.articles_at.get(GROCER, ()), ())

    def test_the_till_drops_days_after_today_and_sums_a_day_given_twice(self):
        prepared = prepare(
            [bought(KEG, WHOLESALER, 5)],
            till=TillData(
                series={KEG: [(ago(3), 1.5), (ago(3), 0.5), (TODAY + timedelta(days=1), 9.0)]},
                start=ago(10),
                covered_until=ago(1),
            ),
        )
        self.assertEqual(prepared.till[KEG].days, (ago(3),))
        self.assertEqual(prepared.till[KEG].cum, (0.0, 2.0))
        self.assertEqual(prepared.ref_day, ago(1))

    def test_exclusions_take_any_set(self):
        exclusions = Exclusions({SYRUP}, {""}, {(KEG, WHOLESALER)})
        self.assertIsInstance(exclusions.everywhere_articles, frozenset)
        self.assertTrue(exclusions.everywhere(ARTICLES[LEMONS - 1]))
        self.assertTrue(exclusions.at(ARTICLES[KEG - 1], WHOLESALER))
        self.assertFalse(exclusions.at(ARTICLES[KEG - 1], GROCER))


class HabitTests(SimpleTestCase):
    def test_one_visit_with_the_article_is_the_prior_and_that_visit(self):
        score = score_article(prepare([bought(SYRUP, WHOLESALER, 0)]), WHOLESALER, SYRUP, DEFAULT)
        self.assertAlmostEqual(score.habit, (1 + 0.1) / (1 + 1), places=12)

    def test_visits_before_the_first_purchase_anywhere_do_not_dilute_it(self):
        rows = [*visits(range(20, 101, 10)), bought(SYRUP, WHOLESALER, 10)]
        score = score_article(prepare(rows), WHOLESALER, SYRUP, DEFAULT)
        self.assertAlmostEqual(score.habit, (weight(10) + 0.1) / (weight(10) + 1), places=12)

    def test_the_window_opens_at_the_first_purchase_at_any_store(self):
        rows = [*visits(range(20, 101, 10)), bought(SYRUP, WHOLESALER, 10), bought(SYRUP, GROCER, 45)]
        score = score_article(prepare(rows), WHOLESALER, SYRUP, DEFAULT)
        seen = sum(weight(d) for d in (40, 30, 20, 10))
        self.assertAlmostEqual(score.habit, (weight(10) + 0.1) / (seen + 1), places=12)

    def test_it_fades_with_each_visit_without_it(self):
        habits = []
        for later in range(5):
            rows = [bought(SYRUP, WHOLESALER, 60), *visits(range(50, 50 - 10 * later, -10))]
            habits.append(score_article(prepare(rows), WHOLESALER, SYRUP, DEFAULT).habit)
        self.assertEqual(habits, sorted(habits, reverse=True))
        self.assertEqual(len(set(habits)), 5)

    def test_the_memory_setting_is_honoured(self):
        rows = [*visits((90, 60, 30)), bought(SYRUP, WHOLESALER, 90), bought(SYRUP, WHOLESALER, 30)]
        for months in (2, 24):
            memory = 30 * months
            score = score_article(prepare(rows), WHOLESALER, SYRUP, Settings(memory_months=months))
            expected = (weight(90, memory) + weight(30, memory) + 0.1) / (
                sum(weight(d, memory) for d in (90, 60, 30)) + 1
            )
            self.assertAlmostEqual(score.habit, expected, places=12)


class CalendarNeedTests(SimpleTestCase):
    rows = [*visits(range(10, 41, 10)), *(bought(SYRUP, WHOLESALER, d, "10") for d in (40, 30, 20, 10))]

    def test_the_cover_and_the_need_by_hand(self):
        score = score_article(prepare(self.rows), WHOLESALER, SYRUP, DEFAULT, horizon_days=7)
        rate = ew_rate([(ago(d), 10.0) for d in (40, 30, 20, 10)], ago(40), TODAY)
        self.assertEqual(score.clock, CALENDAR_CLOCK)
        self.assertAlmostEqual(score.cover, 10 / rate, places=9)
        self.assertAlmostEqual(score.need, (10 + 7) / (10 / rate), places=9)
        self.assertAlmostEqual(score.rate, rate, places=12)

    def test_a_purchase_at_another_store_resets_it(self):
        here = score_article(prepare(self.rows), WHOLESALER, SYRUP, DEFAULT, horizon_days=7)
        elsewhere = score_article(
            prepare([*self.rows, bought(SYRUP, GROCER, 2, "10")]), WHOLESALER, SYRUP, DEFAULT, horizon_days=7
        )
        self.assertEqual(elsewhere.since, 2)
        self.assertEqual(elsewhere.elsewhere_since, GROCER)
        self.assertLess(elsewhere.need, here.need)

    def test_a_bulk_buy_lowers_it(self):
        bulk = [*self.rows[:-1], bought(SYRUP, WHOLESALER, 10, "40")]
        normal = score_article(prepare(self.rows), WHOLESALER, SYRUP, DEFAULT, horizon_days=7)
        big = score_article(prepare(bulk), WHOLESALER, SYRUP, DEFAULT, horizon_days=7)
        self.assertLess(big.need, normal.need)

    def test_no_need_below_three_purchase_days(self):
        score = score_article(
            prepare(self.rows[-2:] + [bought(SYRUP, WHOLESALER, 20, "10")]), WHOLESALER, SYRUP, DEFAULT
        )
        self.assertEqual(score.days_bought, 2)
        self.assertIsNone(score.need)
        self.assertIsNone(score.clock)


class TillNeedTests(SimpleTestCase):
    """The keg: 6 L bought every 15 days, the till pouring 0,4 L a day: k = 1."""

    purchases = [*visits((57, 42, 27, 12)), *(bought(KEG, WHOLESALER, d, "6") for d in (57, 42, 27, 12))]

    def score(self, rows=None, series=None, settings=DEFAULT, **kw):
        series = series if series is not None else every_day_till(0.4, 400, 1, KEG)
        prepared = prepare(rows or self.purchases, till=till(series, **kw))
        return score_article(prepared, WHOLESALER, KEG, settings, horizon_days=7)

    def test_k_inside_its_bounds_uses_the_till(self):
        score = self.score()
        self.assertEqual(score.clock, TILL_CLOCK)
        self.assertAlmostEqual(score.till_share, 12 * 0.4 / 6, places=9)
        self.assertAlmostEqual(score.need, (12 * 0.4 + 0.4 * 7) / 6, places=9)
        self.assertAlmostEqual(score.rate, 0.4, places=9)

    def test_k_outside_its_bounds_falls_back_to_the_calendar(self):
        self.assertEqual(self.score(series=every_day_till(0.01, 400, 1, KEG)).clock, CALENDAR_CLOCK)

    def test_k_under_its_lower_bound_falls_back_to_the_calendar(self):
        # k = 18 L bought / 45 days x 2 L poured = 0.2, under 0.25; at 1 L a day it is 0.4
        self.assertEqual(self.score(series=every_day_till(2.0, 400, 1, KEG)).clock, CALENDAR_CLOCK)
        self.assertEqual(self.score(series=every_day_till(1.0, 400, 1, KEG)).clock, TILL_CLOCK)

    def test_a_net_refund_since_the_last_purchase_reads_nothing_sold(self):
        # Bought yesterday, and yesterday's only movement on the keg was a refund: nothing of
        # this purchase was sold, never a negative share.
        rows = [*self.purchases, *visits((1,)), bought(KEG, WHOLESALER, 1, "6")]
        series = every_day_till(0.4, 400, 2, KEG)
        series[KEG].append((ago(1), -0.5))
        prepared = prepare(rows, till=till(series))
        score = score_article(prepared, WHOLESALER, KEG, DEFAULT, horizon_days=7)
        k = 24 / (56 * 0.4)  # the four purchases before the last, over the 56 days they cover
        rate = ew_rate(series[KEG], ago(365), TODAY)
        self.assertEqual(score.clock, TILL_CLOCK)
        self.assertEqual(score.till_share, 0.0)
        self.assertAlmostEqual(score.need, k * (0 + rate * 7) / 6, places=9)
        text = shopping._sentence(prepared, score, DEFAULT)
        self.assertIn(" ; la caisse a vendu 0 % du dernier achat (hier).", text)
        self.assertNotIn("-", text)

    def test_fewer_than_three_purchases_inside_the_window_fall_back(self):
        rows = [*visits((500, 450, 420, 12)), *(bought(KEG, WHOLESALER, d, "6") for d in (500, 450, 420, 12))]
        self.assertEqual(self.score(rows=rows).clock, CALENDAR_CLOCK)

    def test_the_till_left_out_by_the_setting_or_absent(self):
        self.assertEqual(self.score(settings=Settings(use_till=False)).clock, CALENDAR_CLOCK)
        prepared = prepare(self.purchases)
        self.assertEqual(score_article(prepared, WHOLESALER, KEG, DEFAULT).clock, CALENDAR_CLOCK)

    closed = [*visits((75, 60, 45, 30)), *(bought(KEG, WHOLESALER, d, "6") for d in (75, 60, 45, 30))]

    def test_a_closure_stops_the_till_clock(self):
        series = every_day_till(0.4, 400, 21, KEG)  # nothing sold the last 20 days, the import up to date
        score = self.score(rows=self.closed, series=series)
        rate = ew_rate(series[KEG], ago(365), TODAY)
        self.assertEqual(score.clock, TILL_CLOCK)
        self.assertAlmostEqual(score.need, (10 * 0.4 + rate * 7) / 6, places=9)

    def test_a_stale_import_extrapolates_at_its_recent_rate(self):
        series = every_day_till(0.4, 400, 21, KEG)  # the same sales, but the import stopped 21 days ago
        closure = self.score(rows=self.closed, series=series)
        stale = self.score(rows=self.closed, series=series, covered=21)
        self.assertEqual(stale.clock, TILL_CLOCK)
        self.assertAlmostEqual(stale.need, (10 * 0.4 + 0.4 * 20 + 0.4 * 7) / 6, places=9)
        self.assertGreater(stale.need, closure.need)

    def test_a_stale_import_reads_k_on_the_purchases_it_covers_only(self):
        rows = [*self.closed, *visits((10,)), bought(KEG, WHOLESALER, 10, "6")]
        score = self.score(rows=rows, series=every_day_till(0.4, 400, 21, KEG), covered=21)
        # k = 18 L / 45 days x 0,4 L = 1 over the four purchases the import covers; the fifth
        # one, after it, is the last purchase: 10 days of the recent rate since
        self.assertAlmostEqual(score.need, (0.4 * 10 + 0.4 * 7) / 6, places=9)

    def test_three_days_of_lag_are_tolerated(self):
        series = every_day_till(0.4, 400, 4, KEG)
        fresh = self.score(series=series, covered=4)
        # 3 days behind: not extrapolated, the 9 days sold since the last purchase (12 to 4 days ago)
        self.assertAlmostEqual(fresh.need, (9 * 0.4 + ew_rate(series[KEG], ago(365), TODAY) * 7) / 6, places=9)
        stale = self.score(series=every_day_till(0.4, 400, 5, KEG), covered=5)
        self.assertAlmostEqual(stale.need, (8 * 0.4 + 0.4 * 4 + 0.4 * 7) / 6, places=9)


class ChanceTests(SimpleTestCase):
    def test_every_chance_is_the_formula(self):
        rows = [
            *visits(range(10, 101, 10)),
            *(bought(SYRUP, WHOLESALER, d, "10") for d in (90, 60, 30)),
            *(bought(LEMONS, WHOLESALER, d, "2") for d in (50, 20)),
            *(bought(TONIC, WHOLESALER, d, "6") for d in range(10, 101, 10)),
        ]
        prepared = prepare(rows)
        for article in (SYRUP, LEMONS, TONIC):
            score = score_article(prepared, WHOLESALER, article, DEFAULT)
            expected = formula(score.habit, score.need, score.days_bought < 3)
            self.assertLess(abs(score.chance - expected), 1e-9, article)

    def test_delta_holds_back_an_article_bought_on_fewer_than_three_days(self):
        self.assertAlmostEqual(chance_of(0.5, None, True), formula(0.5, None, True), places=12)
        self.assertLess(chance_of(0.5, None, True), chance_of(0.5, None, False))

    def test_the_need_is_clipped(self):
        self.assertEqual(chance_of(0.3, 50.0, False), chance_of(0.3, 2.0, False))
        self.assertEqual(chance_of(0.3, 0.001, False), chance_of(0.3, 0.1, False))
        self.assertNotEqual(chance_of(0.3, 1.5, False), chance_of(0.3, 2.0, False))

    def test_logit_clamps_zero_and_one(self):
        self.assertEqual(logit(0.0), logit(1e-4))
        self.assertEqual(logit(1.0), logit(1 - 1e-4))
        self.assertEqual(sigmoid(-50), 0.0)

    def test_the_confidence_words(self):
        self.assertEqual(
            [confidence_of(c) for c in (0.25, 0.39, 0.40, 0.59, 0.60, 0.99)],
            ["possible", "possible", "probable", "probable", "presque sûr", "presque sûr"],
        )


class GuardTests(SimpleTestCase):
    def deposit_rows(self, returned):
        rows = [*visits((40, 30, 20, 10)), *(bought(DEPOSIT_KEG, WHOLESALER, d, "10") for d in (40, 30, 20, 10))]
        return [*rows, bought(DEPOSIT_KEG, WHOLESALER, 5, f"-{returned}")]

    def test_returns_of_half_or_more_keep_a_line_off_the_list(self):
        plan = plan_store(prepare(self.deposit_rows(20)), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(plan, DEPOSIT_KEG), DEPOSIT)
        (line,) = plan.deposits
        self.assertEqual(line.sentence, DEPOSIT_SENTENCE)
        self.assertEqual(plan.deposit_hint_categories, ("Consignes exemple",))
        self.assertEqual(
            score_article(prepare(self.deposit_rows(20)), WHOLESALER, DEPOSIT_KEG, DEFAULT).status, DEPOSIT_LIKE
        )

    def test_returns_under_half_are_no_deposit(self):
        plan = plan_store(prepare(self.deposit_rows(19)), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(plan, DEPOSIT_KEG), TO_BUY)
        self.assertEqual(plan.deposit_hint_categories, ())

    def test_the_last_three_purchases_elsewhere_keep_it_off_this_list(self):
        rows = [
            *visits((90, 80, 70, 60)),
            *(bought(VODKA, WHOLESALER, d, "2") for d in (90, 80, 70)),
            *(bought(VODKA, GROCER, d, "2") for d in (30, 20, 10)),
        ]
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(plan, VODKA), ELSEWHERE)
        self.assertEqual(plan.elsewhere[0].sentence, "Vos 3 derniers achats étaient chez Épicerie exemple.")
        self.assertEqual(plan.elsewhere_count, 1)
        self.assertEqual(score_article(prepare(rows), WHOLESALER, VODKA, DEFAULT).status, BOUGHT_ELSEWHERE)
        self.assertEqual(section_of(plan_store(prepare(rows), GROCER, DEFAULT), VODKA), TO_BUY)

    def test_a_store_the_page_does_not_offer_is_not_named(self):
        rows = [
            *visits((90, 80, 70, 60)),
            *(bought(VODKA, WHOLESALER, d, "2") for d in (90, 80, 70)),
            *(bought(VODKA, CORNER, d, "2") for d in (30, 20, 10)),
        ]
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertEqual(plan.elsewhere[0].sentence, "Vos 3 derniers achats étaient ailleurs.")

    silent = [*visits(range(30, 301, 30)), *(bought(SYRUP, WHOLESALER, d, "3") for d in range(120, 301, 30))]

    def test_plus_achete_is_a_label_the_line_stays_listed(self):
        plan = plan_store(prepare(self.silent), WHOLESALER, DEFAULT)
        (line,) = [line for line in plan.to_buy if line.article_id == SYRUP]
        self.assertIn(DROPPED_LABEL, line.badges)
        self.assertEqual(plan.quiet, ())
        # The chance is calibrated on lines with a live rhythm only: this one reads « à vérifier ».
        self.assertEqual(shopping.UNSURE_WORD, "à vérifier")
        self.assertEqual(line.confidence, shopping.UNSURE_WORD)

    def test_en_pause_when_a_recipe_still_sells_it(self):
        series = every_day_till(0.1, 400, 1, SYRUP)
        plan = plan_store(prepare(self.silent, till=till(series)), WHOLESALER, DEFAULT)
        (line,) = [line for line in plan.to_buy if line.article_id == SYRUP]
        self.assertIn(PAUSED_LABEL, line.badges)
        self.assertEqual(line.confidence, confidence_of(line.chance))

    long_silent = [*visits(range(7, 361, 7)), *(bought(SYRUP, WHOLESALER, d, "3") for d in (357, 329, 301))]

    def test_a_silent_line_off_the_list_is_in_plus_achete(self):
        plan = plan_store(prepare(self.long_silent), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(plan, SYRUP), QUIET)
        self.assertEqual(plan.quiet[0].sentence, "Plus acheté depuis 301 jours ; d'habitude tous les 28 jours environ.")

    def test_a_daily_rhythm_reads_every_day(self):
        rows = [*visits(range(1, 361, 2)), *(bought(SYRUP, WHOLESALER, d) for d in (200, 199, 198, 197))]
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(plan, SYRUP), QUIET)
        self.assertEqual(plan.quiet[0].sentence, "Plus acheté depuis 197 jours ; d'habitude tous les jours environ.")

    def test_a_half_day_rhythm_rounds_up_as_the_rhythm_page_does(self):
        rows = [*visits(range(1, 361, 2)), *(bought(SYRUP, WHOLESALER, d) for d in (200, 198, 195))]  # 2.5 days
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertEqual(plan.quiet[0].sentence, "Plus acheté depuis 195 jours ; d'habitude tous les 3 jours environ.")

    def test_a_silent_article_last_bought_here_over_a_year_ago_is_in_no_fold(self):
        for days, section in ((365, QUIET), (366, None)):
            rows = [
                *visits(range(7, 501, 7)),
                *(bought(SYRUP, WHOLESALER, d, "3") for d in (days + 56, days + 28, days)),
            ]
            self.assertEqual(section_of(plan_store(prepare(rows), WHOLESALER, DEFAULT), SYRUP), section, days)

    def test_an_article_bought_elsewhere_last_bought_here_over_a_year_ago_is_in_no_fold(self):
        for days, section, count in ((365, ELSEWHERE, 1), (366, None, 0)):
            rows = [
                *visits(range(10, 501, 10)),
                *(bought(VODKA, WHOLESALER, d, "2") for d in (days + 20, days + 10, days)),
                *(bought(VODKA, GROCER, d, "2") for d in (30, 20, 10)),
            ]
            plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
            self.assertEqual((section_of(plan, VODKA), plan.elsewhere_count), (section, count), days)

    def test_eleven_articles_bought_elsewhere_show_ten_and_count_eleven(self):
        articles = extra_articles(11)
        rows = []
        for article in articles:
            rows += [bought(article.id, WHOLESALER, d, "2") for d in (90, 80, 70)]
            rows += [bought(article.id, GROCER, d, "2") for d in (30, 20, 10)]
        plan = plan_store(prepare(rows, articles=[*ARTICLES, *articles]), WHOLESALER, DEFAULT)
        self.assertEqual((len(plan.elsewhere), plan.elsewhere_count), (10, 11))

    def test_a_deposit_bought_on_two_days_is_in_the_deposits_only(self):
        rows = [
            *visits((40, 30, 20, 10)),
            *(bought(DEPOSIT_KEG, WHOLESALER, d, "10") for d in (40, 20)),
            bought(DEPOSIT_KEG, WHOLESALER, 5, "-15"),
        ]
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertEqual([line.section for line in lines_of(plan) if line.article_id == DEPOSIT_KEG], [DEPOSIT])

    def test_the_short_history_badge(self):
        rows = [*visits((40, 30, 20, 10)), *(bought(SYRUP, WHOLESALER, d) for d in (40, 30, 20, 10))]
        (line,) = [line for line in plan_store(prepare(rows), WHOLESALER, DEFAULT).to_buy if line.article_id == SYRUP]
        self.assertEqual(line.badges, (SHORT_HISTORY_LABEL,))
        rows += [*visits((50,)), bought(SYRUP, WHOLESALER, 50)]
        (line,) = [line for line in plan_store(prepare(rows), WHOLESALER, DEFAULT).to_buy if line.article_id == SYRUP]
        self.assertEqual(line.badges, ())

    def test_a_paused_line_off_the_list_says_so(self):
        series = every_day_till(0.1, 400, 1, SYRUP)
        plan = plan_store(prepare(self.long_silent, till=till(series)), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(plan, SYRUP), QUIET)
        self.assertEqual(
            plan.quiet[0].sentence,
            "Plus acheté depuis 301 jours, mais la caisse en vend encore : vérifiez le stock.",
        )


class NagTests(SimpleTestCase):
    def listed_on_the_morning_of(self, rows, days_ago, article):
        day = ago(days_ago)
        prepared = prepare([row for row in rows if row.day < day], today=day)
        return section_of(plan_store(prepared, WHOLESALER, DEFAULT), article) == TO_BUY

    def test_a_lifted_line_proposed_three_visits_unbought_moves_to_peut_etre(self):
        rows = lifted_line_rows()
        for days in (14, 28, 42):
            self.assertTrue(self.listed_on_the_morning_of(rows, days, SYRUP), days)
        score = score_article(prepare(rows), WHOLESALER, SYRUP, DEFAULT)
        self.assertGreaterEqual(score.chance, 0.25)
        self.assertLess(sigmoid(logit(score.habit) + shopping.MODEL_C), 0.25)  # the need lifts it
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        (line,) = [line for line in plan.maybe if line.article_id == SYRUP]
        self.assertTrue(line.nagged)
        self.assertEqual(line.sentence, "Aurait été proposé à vos 3 derniers passages ici, sans être pris.")
        self.assertNotIn(SYRUP, [line.article_id for line in plan.to_buy])
        self.assertIn(score.chance, [line.chance for line in plan.maybe])
        # The page itself doubts that chance: no calibrated word on it.
        self.assertEqual(line.confidence, shopping.UNSURE_WORD)

    def test_a_typed_horizon_is_never_replayed_into_past_visits(self):
        # Weekly visits; the syrup bought there every 21 days, last 42 days ago, and at the
        # grocer 8 days ago. The page drawn 7 days ago (nothing typed) did not list it: a
        # « Prochain passage dans » typed today is today's page's, never a past one's.
        rows = [
            *visits(range(7, 301, 7)),
            *(bought(SYRUP, WHOLESALER, d, "3") for d in range(42, 301, 21)),
            bought(SYRUP, GROCER, 8, "3"),
        ]
        self.assertFalse(self.listed_on_the_morning_of(rows, 7, SYRUP))
        for horizon in (None, 21, 30, 45):
            plan = plan_store(prepare(rows), WHOLESALER, DEFAULT, horizon)
            self.assertEqual(section_of(plan, SYRUP), TO_BUY, horizon)
            self.assertFalse(any(line.nagged for line in lines_of(plan)), horizon)

    def test_a_purchase_here_resets_it(self):
        rows = [*lifted_line_rows(), bought(SYRUP, WHOLESALER, 28, "5")]
        self.assertEqual(section_of(plan_store(prepare(rows), WHOLESALER, DEFAULT), SYRUP), TO_BUY)

    def test_a_visit_below_the_threshold_resets_it(self):
        rows = [*lifted_line_rows(), bought(SYRUP, GROCER, 43, "30")]
        self.assertFalse(self.listed_on_the_morning_of(rows, 42, SYRUP))
        self.assertTrue(self.listed_on_the_morning_of(rows, 28, SYRUP))
        self.assertTrue(self.listed_on_the_morning_of(rows, 14, SYRUP))
        self.assertEqual(section_of(plan_store(prepare(rows), WHOLESALER, DEFAULT), SYRUP), TO_BUY)

    def test_a_habit_line_gets_five_visits(self):
        five = plan_store(prepare(habit_line_rows(stopped=84)), WHOLESALER, DEFAULT)
        (line,) = [line for line in five.maybe if line.article_id == TONIC]
        self.assertEqual(line.sentence, "Aurait été proposé à vos 5 derniers passages ici, sans être pris.")
        self.assertEqual(line.confidence, shopping.UNSURE_WORD)
        four = plan_store(prepare(habit_line_rows(stopped=70)), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(four, TONIC), TO_BUY)

    def test_a_store_with_fewer_visits_than_k_never_nags(self):
        rows = [
            *visits((28, 14)),
            *(bought(SYRUP, GROCER, d, "1") for d in (90, 60, 45)),
            bought(SYRUP, WHOLESALER, 40, "1"),
        ]
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertFalse(any(line.nagged for line in lines_of(plan)))

    def test_a_visit_made_today_is_replayed_as_the_page_drawn_that_morning(self):
        # Below the threshold 42 days ago only: two listings in a row - until today's visit,
        # where the page drawn that morning listed it too and it was not bought.
        rows = [*lifted_line_rows(), bought(SYRUP, GROCER, 43, "30")]
        self.assertEqual(section_of(plan_store(prepare(rows), WHOLESALER, DEFAULT), SYRUP), TO_BUY)
        self.assertTrue(self.listed_on_the_morning_of(rows, 0, SYRUP))
        plan = plan_store(prepare([*rows, *visits((0,))]), WHOLESALER, DEFAULT)
        self.assertEqual(plan.last_visit, TODAY)
        (line,) = [line for line in plan.maybe if line.article_id == SYRUP]
        self.assertEqual((line.section, line.nagged), (MAYBE, True))


class CutTests(SimpleTestCase):
    def mixed(self):
        return prepare(
            [
                *lifted_line_rows(),
                *(bought(TONIC, WHOLESALER, d, "6") for d in range(14, 211, 28)),
                *(bought(LEMONS, WHOLESALER, d, "2") for d in (140, 98)),
                *(bought(NAPKINS, WHOLESALER, d, "1") for d in (210, 182, 154)),
                *(bought(VODKA, WHOLESALER, d, "2") for d in (196, 98, 42)),
            ]
        )

    def test_the_cut_and_peut_etre(self):
        prepared = self.mixed()
        plan = plan_store(prepared, WHOLESALER, DEFAULT)
        scores = {a: score_article(prepared, WHOLESALER, a, DEFAULT) for a in prepared.articles_at[WHOLESALER]}
        nagged = {line.article_id for line in plan.maybe if line.nagged}
        self.assertEqual(
            {line.article_id for line in plan.to_buy},
            {a for a, s in scores.items() if s.status == OK and s.chance >= 0.25 and a not in nagged},
        )
        self.assertEqual(
            {line.article_id for line in plan.maybe},
            {a for a, s in scores.items() if s.status == OK and 0.125 <= s.chance < 0.25} | nagged,
        )
        self.assertTrue(all(line.chance >= 0.25 for line in plan.to_buy))
        self.assertEqual(
            [line.chance for line in plan.maybe], sorted((line.chance for line in plan.maybe), reverse=True)
        )
        self.assertAlmostEqual(
            plan.expected_basket, sum(s.chance for s in scores.values() if s.status == OK), places=12
        )

    def test_the_threshold_setting_moves_the_cut(self):
        low = plan_store(self.mixed(), WHOLESALER, Settings(threshold_percent=10))
        high = plan_store(self.mixed(), WHOLESALER, Settings(threshold_percent=60))
        self.assertGreater(len(low.to_buy), len(high.to_buy))
        self.assertTrue(all(line.chance >= 0.6 for line in high.to_buy))

    def test_equal_chances_are_listed_by_article(self):
        rows = [
            *visits((30, 20, 10)),
            bought(NAPKINS, WHOLESALER, 0),
            bought(TONIC, WHOLESALER, 0),
            bought(SYRUP, WHOLESALER, 0),
        ]
        plan = plan_store(prepare(rows), WHOLESALER, Settings(threshold_percent=10))
        tied = [line for line in plan.to_buy if line.article_id in (SYRUP, NAPKINS, TONIC)]
        self.assertEqual(len({line.chance for line in tied}), 1)
        self.assertEqual([line.article_id for line in tied], [SYRUP, TONIC, NAPKINS])

    def test_an_empty_list(self):
        rows = [*visits(range(7, 361, 7)), *(bought(SYRUP, WHOLESALER, d, "3") for d in (357, 329, 301))]
        plan = plan_store(prepare(rows, exclusions=Exclusions({CUPS})), WHOLESALER, DEFAULT)
        self.assertEqual(plan.to_buy, ())
        self.assertFalse(plan.few_visits)
        self.assertEqual(plan.visits, 51)  # an article left out still makes the visits
        self.assertEqual([top.article_id for top in plan.top_here], [SYRUP])

    def test_a_store_with_few_visits(self):
        self.assertTrue(plan_store(prepare(visits((40, 30, 20, 10))), WHOLESALER, DEFAULT).few_visits)
        self.assertFalse(plan_store(prepare(visits((50, 40, 30, 20, 10))), WHOLESALER, DEFAULT).few_visits)

    def test_a_store_never_visited_has_nothing_to_plan(self):
        plan = plan_store(prepare(visits((40, 30))), GROCER, DEFAULT)
        self.assertEqual(plan.candidates, 0)
        self.assertEqual((plan.to_buy, plan.maybe, plan.new, plan.top_here), ((), (), (), ()))
        self.assertIsNone(plan.last_visit)
        self.assertEqual(plan.usual_gap, 14.0)
        self.assertEqual(plan.store_name, "Épicerie exemple")

    def test_nouveaux_ici_holds_a_first_purchase_up_to_ninety_days_old(self):
        for days, section in ((90, NEW), (91, None)):
            rows = [*visits(range(10, 361, 10)), bought(SYRUP, WHOLESALER, days, "2")]
            self.assertEqual(section_of(plan_store(prepare(rows), WHOLESALER, DEFAULT), SYRUP), section, days)

    def test_a_line_shows_in_one_section_only(self):
        rows = [*visits(range(10, 101, 10)), *(bought(SYRUP, WHOLESALER, d, "2") for d in (20, 10))]
        plan = plan_store(prepare(rows), WHOLESALER, Settings(threshold_percent=10))
        self.assertEqual(section_of(plan, SYRUP), TO_BUY)
        self.assertEqual(plan.new, ())
        rows = [*visits(range(10, 361, 10)), bought(SYRUP, WHOLESALER, 30, "2")]
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertEqual(section_of(plan, SYRUP), NEW)
        self.assertEqual(
            plan.new[0].sentence, "Acheté 1 fois seulement (dernier il y a 30 jours) : pas encore une habitude."
        )


class QuantityTests(SimpleTestCase):
    def line(self, rows, settings=DEFAULT, horizon_days=None, names=None, article=SYRUP):
        prepared = prepare(
            rows, product_names=names or {101: "Sirop cassis 1L exemple", 102: "Sirop cassis 70cl exemple"}
        )
        plan = plan_store(prepared, WHOLESALER, settings, horizon_days)
        return next(line for line in lines_of(plan) if line.article_id == article)

    def test_the_median_of_the_last_three_purchases_here_in_decimal(self):
        rows = [*visits((40, 30, 20, 10))] + [
            bought(SYRUP, WHOLESALER, d, q) for d, q in ((40, "9"), (30, "1.5"), (20, "3"), (10, "2.25"))
        ]
        self.assertEqual(self.line(rows).qty, Decimal("2.25"))
        two = [*visits((20, 10)), bought(SYRUP, WHOLESALER, 20, "1.5"), bought(SYRUP, WHOLESALER, 10, "2.5")]
        qty = self.line(two).qty
        self.assertIsInstance(qty, Decimal)
        self.assertEqual(qty, Decimal("2"))

    def test_the_usual_product_among_the_last_five_days_ties_to_the_most_recent(self):
        rows = [*visits((50, 40, 30, 20, 10))]
        products = ((50, 101), (40, 101), (30, 102), (20, 102), (10, 101))
        mostly_101 = [*rows, *(bought(SYRUP, WHOLESALER, d, "6", product=p, units="6") for d, p in products)]
        self.assertEqual(self.line(mostly_101).product_id, 101)
        tied = [*rows, *(bought(SYRUP, WHOLESALER, d, "6", product=p, units="6") for d, p in products[:4])]
        self.assertEqual(self.line(tied).product_id, 102)

    def test_the_count_is_the_median_high_of_its_units_and_a_real_amount(self):
        rows = [*visits((30, 20, 10))]
        line = self.line(
            [*rows, *(bought(SYRUP, WHOLESALER, d, "6", product=101, units=u) for d, u in ((20, "6"), (10, "12")))]
        )
        self.assertEqual(line.product_units, Decimal("12"))
        line = self.line(
            [
                *rows,
                *(
                    bought(SYRUP, WHOLESALER, d, "6", product=101, units=u)
                    for d, u in ((30, "12"), (20, "6"), (10, "6"))
                ),
            ]
        )
        self.assertEqual(line.product_units, Decimal("6"))
        self.assertEqual(line.product_name, "Sirop cassis 1L exemple")

    def test_packs_only_when_the_colisage_divides_the_count(self):
        def line(colisage, units="24"):
            rows = [*visits((10,)), bought(SYRUP, WHOLESALER, 10, "24", product=101, units=units, colisage=colisage)]
            return self.line(rows)

        self.assertEqual(line(6).packs, (4, Decimal("6")))
        self.assertIsNone(line(5).packs)
        self.assertIsNone(line(1).packs)

    def test_a_measured_product_shows_the_article_units_only(self):
        rows = [*visits((10,)), bought(LEMONS, WHOLESALER, 10, "2.5", product=101, units="2.5")]
        line = self.line(rows, article=LEMONS)
        self.assertIsNone(line.product_units)
        self.assertEqual(line.qty, Decimal("2.5"))

    def test_a_product_with_no_name_shows_the_article_units_only(self):
        rows = [*visits((10,)), bought(SYRUP, WHOLESALER, 10, "6", product=103, units="6")]
        self.assertIsNone(self.line(rows).product_units)

    weekly = [*visits(range(7, 85, 7)), *(bought(SYRUP, WHOLESALER, d, "10") for d in range(7, 85, 7))]

    def test_the_horizon_multiplier(self):
        # The usual purchase covers one usual gap (7 days here): a typed horizon longer than the
        # gap takes as many usual quantities as it holds gaps, rounded half up.
        self.assertEqual(self.line(self.weekly).multiplier, 1)
        for horizon, multiplier in ((5, 1), (7, 1), (8, 1), (10, 1), (11, 2), (14, 2), (30, 4)):
            self.assertEqual(self.line(self.weekly, horizon_days=horizon).multiplier, multiplier, horizon)
        self.assertEqual(self.line(self.weekly, horizon_days=30).total_qty, Decimal("40"))

    def test_an_article_also_bought_elsewhere_does_not_jump_one_day_past_the_gap(self):
        # Lemons: 5 kg at the wholesaler every 7 days, 3 kg at the grocer mid-week. The grocer
        # keeps supplying its share: one day over the gap is still one usual quantity.
        rows = [
            *visits(range(7, 85, 7)),
            *(bought(LEMONS, WHOLESALER, d, "5") for d in range(7, 85, 7)),
            *(bought(LEMONS, GROCER, d, "3") for d in range(10, 85, 7)),
        ]
        for horizon, multiplier in ((None, 1), (7, 1), (8, 1), (14, 2)):
            line = self.line(rows, horizon_days=horizon, article=LEMONS)
            self.assertEqual(line.multiplier, multiplier, horizon)


class UsualPurchaseTests(SimpleTestCase):
    """`usual_purchase`: what one purchase of an article at a store usually
    is, today - the figures plan_store's lines carry with no typed horizon
    (the shopping list's « quantité habituelle »), for one article alone.

    One weekly store, an article in each section: cups at every visit and a
    syrup in packs of 6 (« À acheter »), lemons by measure, a tonic bought
    once (« Peut-être » or « Nouveaux ici »), a vodka silent for ten months
    (« Plus acheté ? »), a keg bought at the grocer's since (« Acheté
    ailleurs maintenant ») and a deposit mostly given back (« consignes ? »)."""

    NAMES = {101: "Sirop cassis 1L exemple", 102: "Citrons vrac exemple", 103: "Fût blonde 30L exemple"}

    def rows(self):
        weekly = range(7, 85, 7)
        return [
            *visits(weekly),
            *(
                bought(SYRUP, WHOLESALER, d, q, product=101, units=q, colisage=6)
                for d, q in ((d, "12" if d == 14 else "6") for d in weekly)
            ),
            *(bought(LEMONS, WHOLESALER, d, "2.5", product=102, units="2.5") for d in range(7, 85, 14)),
            bought(TONIC, WHOLESALER, 7, "6"),
            *(bought(VODKA, WHOLESALER, d, "1.5") for d in (300, 293, 286)),
            bought(KEG, WHOLESALER, 70, "30", product=103, units="1"),
            *(bought(KEG, GROCER, d, "30") for d in (50, 35, 21)),
            *(bought(DEPOSIT_KEG, WHOLESALER, d, "2") for d in (63, 42, 21)),
            *(bought(DEPOSIT_KEG, WHOLESALER, d, "-2") for d in (56, 35)),
        ]

    def prepared(self):
        return prepare(self.rows(), product_names=self.NAMES)

    def test_every_line_of_every_section_is_its_usual_purchase(self):
        prepared = self.prepared()
        plan = plan_store(prepared, WHOLESALER, DEFAULT)
        lines = lines_of(plan)
        # The fixture reaches the sections it is written for.
        self.assertTrue({TO_BUY, QUIET, ELSEWHERE, DEPOSIT} <= {line.section for line in lines})
        self.assertEqual(len({line.article_id for line in lines}), 7)
        for line in lines:
            with self.subTest(article=line.name, section=line.section):
                usual = shopping.usual_purchase(prepared, WHOLESALER, line.article_id)
                self.assertIsInstance(usual, shopping.UsualPurchase)
                self.assertEqual(
                    (usual.qty, usual.product_id, usual.product_name, usual.product_units, usual.packs),
                    (line.qty, line.product_id, line.product_name, line.product_units, line.packs),
                )
                self.assertIsInstance(usual.qty, Decimal)
                # One rule for the line's chance and its quantity.
                self.assertEqual(usual.qty, score_article(prepared, WHOLESALER, line.article_id, DEFAULT).qty)

    def test_the_syrup_s_pack_and_the_lemons_by_measure(self):
        prepared = self.prepared()
        syrup = shopping.usual_purchase(prepared, WHOLESALER, SYRUP)
        # The last three days bought 6, 12 and 6: the median, and the
        # product's count among them, 6 - one pack of 6.
        self.assertEqual(syrup.qty, Decimal("6"))
        self.assertEqual((syrup.product_id, syrup.product_name), (101, "Sirop cassis 1L exemple"))
        self.assertEqual((syrup.product_units, syrup.packs), (Decimal("6"), (1, Decimal("6"))))
        lemons = shopping.usual_purchase(prepared, WHOLESALER, LEMONS)
        self.assertEqual(lemons.qty, Decimal("2.5"))
        self.assertEqual((lemons.product_id, lemons.product_name), (102, "Citrons vrac exemple"))
        # Bought by measure: no count of the product, no pack.
        self.assertIsNone(lemons.product_units)
        self.assertIsNone(lemons.packs)

    def test_a_typed_horizon_multiplies_the_line_never_the_usual_purchase(self):
        prepared = self.prepared()
        plan = plan_store(prepared, WHOLESALER, DEFAULT, horizon_days=30)
        [line] = [line for line in lines_of(plan) if line.article_id == SYRUP]
        self.assertGreater(line.multiplier, 1)
        usual = shopping.usual_purchase(prepared, WHOLESALER, SYRUP)
        self.assertEqual(usual.qty, line.qty)
        self.assertEqual(line.total_qty, usual.qty * line.multiplier)
        self.assertEqual(line.total_product_units, usual.product_units * line.multiplier)

    def test_none_for_an_article_never_bought_at_the_store(self):
        prepared = self.prepared()
        self.assertIsNone(shopping.usual_purchase(prepared, GROCER, SYRUP))
        self.assertIsNone(shopping.usual_purchase(prepared, WHOLESALER, NAPKINS))
        self.assertIsNone(shopping.usual_purchase(prepared, CORNER, CUPS))
        # Returns alone are no purchase.
        returned = prepare([bought(SYRUP, WHOLESALER, 5, "-2")])
        self.assertIsNone(shopping.usual_purchase(returned, WHOLESALER, SYRUP))

    def test_none_before_its_first_purchase(self):
        prepared = self.prepared()
        # The tonic, first bought 7 days ago, as of 10 days ago: not yet.
        earlier = dataclasses.replace(prepared, today=ago(10))
        self.assertIsNone(shopping.usual_purchase(earlier, WHOLESALER, TONIC))
        # And a purchase made today is today's history.
        today = prepare([bought(TONIC, WHOLESALER, 0, "6")])
        self.assertEqual(shopping.usual_purchase(today, WHOLESALER, TONIC).qty, Decimal("6"))

    def test_the_median_of_two_days_can_carry_more_places(self):
        # Two days, 1.25 and 2.5: 1.875 - the quantity as the line has it,
        # rounded by whoever shows or stores it.
        rows = [*visits((20, 10)), bought(SYRUP, WHOLESALER, 20, "1.25"), bought(SYRUP, WHOLESALER, 10, "2.5")]
        self.assertEqual(shopping.usual_purchase(prepare(rows), WHOLESALER, SYRUP).qty, Decimal("1.875"))


class EdgeTests(SimpleTestCase):
    def test_zero_negative_and_missing_quantities_never_make_a_purchase(self):
        rows = [
            PurchaseRow(SYRUP, WHOLESALER, ago(5), Decimal("0")),
            PurchaseRow(SYRUP, WHOLESALER, ago(4), Decimal("-2")),
            PurchaseRow(SYRUP, WHOLESALER, ago(3), None),
        ]
        prepared = prepare(rows)
        self.assertIsNone(score_article(prepared, WHOLESALER, SYRUP, DEFAULT))
        self.assertEqual(plan_store(prepared, WHOLESALER, DEFAULT).candidates, 0)

    def test_no_purchase_at_all(self):
        prepared = prepare([])
        self.assertEqual(store_choices(prepared), [])
        self.assertEqual(rhythms(prepared, DEFAULT), [])
        self.assertEqual(plan_store(prepared, WHOLESALER, DEFAULT).candidates, 0)

    def test_a_single_visit(self):
        plan = plan_store(prepare([bought(SYRUP, WHOLESALER, 3)]), WHOLESALER, DEFAULT)
        self.assertEqual((plan.visits, plan.usual_gap, plan.horizon), (1, 14.0, 14.0))
        self.assertEqual(plan.candidates, 1)

    def test_bought_at_two_stores_on_one_day(self):
        rows = [*(bought(SYRUP, WHOLESALER, d) for d in (30, 20, 10)), bought(SYRUP, GROCER, 10)]
        score = score_article(prepare(rows), WHOLESALER, SYRUP, DEFAULT)
        self.assertEqual(score.status, OK)
        self.assertIsNone(score.elsewhere_since)
        self.assertEqual(score.days_bought, 3)

    def test_an_excluded_category_with_no_name(self):
        rows = [*(bought(LEMONS, WHOLESALER, d) for d in (20, 10)), *(bought(SYRUP, WHOLESALER, d) for d in (20, 10))]
        plan = plan_store(prepare(rows, exclusions=Exclusions(categories={""})), WHOLESALER, DEFAULT)
        self.assertIsNone(section_of(plan, LEMONS))
        self.assertEqual(plan.candidates, 1)

    def test_an_article_left_out_at_one_store_only(self):
        rows = [bought(SYRUP, WHOLESALER, 10), bought(SYRUP, GROCER, 5)]
        exclusions = Exclusions(per_store={(SYRUP, WHOLESALER)})
        self.assertEqual(plan_store(prepare(rows, exclusions=exclusions), WHOLESALER, DEFAULT).candidates, 0)
        self.assertEqual(plan_store(prepare(rows, exclusions=exclusions), GROCER, DEFAULT).candidates, 1)


class DueElsewhereTests(SimpleTestCase):
    rows = [*visits((90, 60, 30)), *(bought(TONIC, GROCER, d, "6") for d in (90, 75, 60, 45))]

    def test_an_article_never_bought_here_whose_need_has_come(self):
        plan = plan_store(prepare(self.rows), WHOLESALER, DEFAULT)
        (due,) = plan.due_elsewhere
        self.assertEqual((due.article_id, due.store_id, due.store_name), (TONIC, GROCER, "Épicerie exemple"))
        self.assertGreaterEqual(due.need, 1.0)
        self.assertNotIn(TONIC, [line.article_id for line in lines_of(plan)])

    def test_never_an_article_left_out_everywhere(self):
        plan = plan_store(prepare(self.rows, exclusions=Exclusions({TONIC})), WHOLESALER, DEFAULT)
        self.assertEqual(plan.due_elsewhere, ())

    def test_its_sentence_says_its_rhythm(self):
        (due,) = plan_store(prepare(self.rows), WHOLESALER, DEFAULT).due_elsewhere
        self.assertEqual(due.sentence, "Dernier achat il y a 45 jours, d'habitude tous les 15 jours.")
        self.assertEqual(due.badges, ())

    @staticmethod
    def abandoned_and_due(abandoned=(TONIC,)):
        """The grocer visited weekly; at the wholesaler, the `abandoned` articles bought weekly
        until 70 days ago, and the vodka weekly until 8 days ago."""
        rows = [*visits(range(7, 211, 7), store=GROCER), *(bought(VODKA, WHOLESALER, d, "2") for d in range(8, 101, 7))]
        for article in abandoned:
            rows += [bought(article, WHOLESALER, d, "6") for d in range(70, 211, 7)]
        return rows

    def test_an_article_no_longer_bought_is_not_due_elsewhere(self):
        due = plan_store(prepare(self.abandoned_and_due()), GROCER, DEFAULT).due_elsewhere
        self.assertEqual([row.article_id for row in due], [VODKA])

    def test_abandoned_articles_do_not_push_a_due_one_out(self):
        articles = extra_articles(5)
        rows = self.abandoned_and_due([article.id for article in articles])
        due = plan_store(prepare(rows, articles=[*ARTICLES, *articles]), GROCER, DEFAULT).due_elsewhere
        self.assertEqual([row.article_id for row in due], [VODKA])

    def test_an_article_a_recipe_still_sells_is_due_and_said_en_pause(self):
        prepared = prepare(self.abandoned_and_due(), till=till(every_day_till(1.0, 400, 1, TONIC)))
        due = {row.article_id: row for row in plan_store(prepared, GROCER, DEFAULT).due_elsewhere}
        self.assertEqual(due[TONIC].badges, (PAUSED_LABEL,))
        self.assertEqual(due[VODKA].badges, ())

    def test_never_sent_to_a_store_it_is_left_out_at(self):
        exclusions = Exclusions(per_store={(TONIC, GROCER)})
        self.assertEqual(plan_store(prepare(self.rows, exclusions=exclusions), WHOLESALER, DEFAULT).due_elsewhere, ())

    def test_never_an_article_last_bought_over_a_year_ago(self):
        # Bought every 134 days: silent for a year is no « plus acheté ? », and its need has come.
        for days, found in ((365, [SYRUP]), (366, [])):
            rows = [
                *visits(range(7, 701, 7)),
                bought(SYRUP, GROCER, days, "1"),
                bought(SYRUP, GROCER, days + 134, "100"),
                bought(SYRUP, GROCER, days + 268, "100"),
            ]
            due = plan_store(prepare(rows), WHOLESALER, DEFAULT).due_elsewhere
            self.assertEqual([row.article_id for row in due], found, days)

    def test_five_at_most(self):
        articles = extra_articles(6)
        rows = [*visits(range(7, 71, 7))]
        for article in articles:
            rows += [bought(article.id, GROCER, d, "3") for d in (45, 30, 15)]
        due = plan_store(prepare(rows, articles=[*ARTICLES, *articles]), WHOLESALER, DEFAULT).due_elsewhere
        self.assertEqual([row.article_id for row in due], [article.id for article in articles[:5]])


class TopHereTests(SimpleTestCase):
    def test_the_most_bought_here_over_a_year_in_decimal(self):
        rows = [
            bought(SYRUP, WHOLESALER, 300, "1", "41.30"),
            bought(SYRUP, WHOLESALER, 30, "1", "41.30"),
            bought(KEG, WHOLESALER, 20, "30", "95.40"),
            bought(TONIC, WHOLESALER, 400, "6", "500.00"),
        ]
        plan = plan_store(prepare(rows), WHOLESALER, DEFAULT)
        self.assertEqual(
            [(top.article_id, top.purchases_12m, top.total_ht) for top in plan.top_here],
            [
                (KEG, 1, Decimal("95.40")),
                (SYRUP, 2, Decimal("82.60")),
            ],
        )

    def test_ten_at_most(self):
        articles = extra_articles(11)
        rows = [bought(article.id, WHOLESALER, 30, "1", f"{40 + 3 * i}.00") for i, article in enumerate(articles)]
        plan = plan_store(prepare(rows, articles=[*ARTICLES, *articles]), WHOLESALER, DEFAULT)
        self.assertEqual([top.article_id for top in plan.top_here], [article.id for article in articles[:0:-1]])


class SentenceTests(SimpleTestCase):
    def sentence(self, rows, article=SYRUP, settings=DEFAULT, store=WHOLESALER, **kw):
        prepared = prepare(rows, **kw)
        score = score_article(prepared, store, article, settings)
        return score, shopping._sentence(prepared, score, settings)

    def test_the_memory_span(self):
        rows = [*visits(range(10, 301, 10)), *(bought(SYRUP, WHOLESALER, d) for d in (300, 200, 150, 100, 50, 20))]
        score, text = self.sentence(rows)
        self.assertEqual((score.span, score.hits, score.seen), (SPAN_MEMORY, 4, 18))
        self.assertTrue(
            text.startswith(
                "Pris 4 fois sur 18 passages en 6 mois ; dernier achat il y a 20 jours, d'habitude tous les"
            )
        )

    def test_the_adoption_span(self):
        rows = [*visits(range(10, 301, 10)), *(bought(SYRUP, WHOLESALER, d) for d in (100, 50, 20))]
        score, text = self.sentence(rows)
        self.assertEqual((score.span, score.hits, score.seen), (SPAN_ADOPTION, 3, 10))
        self.assertTrue(text.startswith("Pris 3 fois sur 10 passages depuis que vous l'achetez ;"))

    def test_the_last_three_visits(self):
        rows = [*visits((300, 200, 100, 50)), *(bought(SYRUP, WHOLESALER, d) for d in (300, 200, 50))]
        score, text = self.sentence(rows, settings=Settings(memory_months=2))
        self.assertEqual((score.span, score.hits, score.seen), (SPAN_LAST, 2, 3))
        self.assertTrue(text.startswith("Pris 2 fois sur vos 3 derniers passages ;"))

    def test_the_till_sentences(self):
        series = till(every_day_till(0.4, 400, 1, KEG))
        purchases = [*visits((57, 42, 27, 12)), *(bought(KEG, WHOLESALER, d, "6") for d in (57, 42, 27, 12))]
        _score, text = self.sentence(purchases, KEG, till=series)
        self.assertIn(" ; la caisse a vendu 80 % du dernier achat (il y a 12 jours).", text)
        purchases = [*visits((75, 60, 45, 30)), *(bought(KEG, WHOLESALER, d, "6") for d in (75, 60, 45, 30))]
        _score, text = self.sentence(purchases, KEG, till=series)
        self.assertIn(" ; la caisse a écoulé tout le dernier achat (il y a 30 jours).", text)

    def test_a_share_read_as_100_percent_has_sold_it_all(self):
        series = till(every_day_till(0.4, 400, 1, KEG))
        purchases = [*visits((57, 42, 27, 12)), *(bought(KEG, WHOLESALER, d, "6") for d in (57, 42, 27, 12))]
        prepared = prepare(purchases, till=series)
        score = score_article(prepared, WHOLESALER, KEG, DEFAULT)
        for share in (0.995, 0.996, 0.9999, 1 - 1e-15, 1.0, 1.4):
            text = shopping._sentence(prepared, dataclasses.replace(score, till_share=share), DEFAULT)
            self.assertIn(" ; la caisse a écoulé tout le dernier achat (il y a 12 jours).", text, share)
        text = shopping._sentence(prepared, dataclasses.replace(score, till_share=0.994), DEFAULT)
        self.assertIn(" ; la caisse a vendu 99 % du dernier achat (il y a 12 jours).", text)

    def test_a_purchase_the_till_explains_exactly_is_sold_out(self):
        # 4,9 L every 7 days, the till pouring 0,7 L a day: float sums leave the share a hair under 1.
        days = (7, 14, 21, 28)
        prepared = prepare(
            [*visits(days), *(bought(KEG, WHOLESALER, d, "4.9") for d in days)],
            till=till(every_day_till(0.7, 400, 1, KEG)),
        )
        score = score_article(prepared, WHOLESALER, KEG, DEFAULT)
        self.assertLess(score.till_share, 1.0)
        text = shopping._sentence(prepared, score, DEFAULT)
        self.assertIn(" ; la caisse a écoulé tout le dernier achat (il y a 7 jours).", text)

    def test_a_share_extrapolated_past_the_import_reads_as_an_estimate(self):
        # The import covers up to 21 days ago: from the 20th day back on, the till is estimated.
        closed = [*visits((75, 60, 45, 30)), *(bought(KEG, WHOLESALER, d, "6") for d in (75, 60, 45, 30))]
        score, text = self.sentence(closed, KEG, till=till(every_day_till(0.4, 400, 21, KEG), covered=21))
        self.assertEqual(score.till_estimated_from, ago(20))
        self.assertIn(
            " ; la caisse aurait écoulé tout le dernier achat (il y a 30 jours, estimé depuis le 20/02).", text
        )
        purchases = [*visits((57, 42, 27, 12)), *(bought(KEG, WHOLESALER, d, "6") for d in (57, 42, 27, 12))]
        score, text = self.sentence(purchases, KEG, till=till(every_day_till(0.4, 400, 5, KEG), covered=5))
        self.assertEqual(score.till_estimated_from, ago(4))
        self.assertIn(
            " ; la caisse aurait vendu environ 80 % du dernier achat (il y a 12 jours, estimé depuis le 08/03).", text
        )
        # Three days behind is no estimate: what the import holds is said as it is.
        score, text = self.sentence(purchases, KEG, till=till(every_day_till(0.4, 400, 4, KEG), covered=4))
        self.assertIsNone(score.till_estimated_from)
        self.assertIn(" ; la caisse a vendu 60 % du dernier achat (il y a 12 jours).", text)

    def test_an_overdue_article_says_its_rhythm_not_a_cover_its_silence_stretched(self):
        rows = [*visits(range(7, 101, 7)), *(bought(SYRUP, WHOLESALER, d, "2") for d in range(14, 101, 7))]
        _score, text = self.sentence(rows)
        self.assertIn(" ; dernier achat il y a 14 jours, d'habitude tous les 7 jours.", text)
        self.assertNotIn("dure", text)

    def test_bought_again_elsewhere_since(self):
        rows = [*visits((40, 30, 20)), *(bought(SYRUP, WHOLESALER, d) for d in (40, 30, 20))]
        _score, text = self.sentence([*rows, bought(SYRUP, GROCER, 5)])
        self.assertTrue(text.endswith(" ; racheté depuis chez Épicerie exemple."))
        _score, text = self.sentence([*rows, bought(SYRUP, CORNER, 5)])
        self.assertTrue(text.endswith(" ; racheté ailleurs depuis."))

    def test_today_and_yesterday_read_as_words(self):
        rows = [*visits((40, 30, 20)), *(bought(SYRUP, WHOLESALER, d) for d in (40, 30, 0))]
        _score, text = self.sentence(rows)
        self.assertIn("dernier achat aujourd'hui", text)
        _score, text = self.sentence([bought(SYRUP, WHOLESALER, 1)])
        self.assertEqual(text, "Acheté 1 fois seulement (dernier hier) : pas encore une habitude.")

    def test_every_sentence_is_short_and_names_an_offered_store_only(self):
        fixtures = [
            prepare(
                [
                    *lifted_line_rows(),
                    *(bought(VODKA, GROCER, d, "2") for d in (30, 20, 10)),
                    bought(VODKA, WHOLESALER, 98, "1"),
                ]
            ),
            prepare(habit_line_rows(stopped=84)),
            prepare(
                [
                    *visits((57, 42, 27, 12)),
                    *(bought(KEG, WHOLESALER, d, "6") for d in (57, 42, 27, 12)),
                    bought(KEG, CORNER, 3, "6"),
                ],
                till=till(every_day_till(0.4, 400, 1, KEG)),
            ),
        ]
        offered = {store.name for store in STORES}
        for prepared in fixtures:
            for store in (WHOLESALER, GROCER):
                plan = plan_store(prepared, store, DEFAULT)
                sentences = [line.sentence for line in lines_of(plan)] + [due.sentence for due in plan.due_elsewhere]
                for text in sentences:
                    self.assertLessEqual(len(text), 140, text)
                    for name in re.findall(r"chez (.+?)[.;]", text):
                        self.assertIn(name, offered)

    def test_the_figures_of_a_sentence_are_the_habit_s_counts(self):
        prepared = prepare([*lifted_line_rows(), *(bought(TONIC, WHOLESALER, d, "6") for d in range(14, 211, 28))])
        plan = plan_store(prepared, WHOLESALER, Settings(threshold_percent=10))
        checked = 0
        for line in plan.to_buy + plan.maybe:
            found = re.match(r"Pris (\d+) fois sur (?:vos )?(\d+)", line.sentence)
            if found:
                score = score_article(prepared, WHOLESALER, line.article_id, DEFAULT)
                self.assertEqual((int(found[1]), int(found[2])), (score.hits, score.seen))
                checked += 1
        self.assertGreater(checked, 0)


class UsualGapTests(SimpleTestCase):
    """The median gap between the store's last 31 visits - with fewer than 31 too. (The measured
    scorer paired each visit with itself below 31 visits, a horizon of 0 days.)"""

    def test_a_store_with_a_few_visits(self):
        plan = plan_store(prepare(visits(range(10, 101, 10))), WHOLESALER, DEFAULT)
        self.assertEqual((plan.usual_gap, plan.horizon, plan.horizon_typed), (10.0, 10.0, False))

    def test_only_the_last_thirty_gaps_count(self):
        rows = visits([*range(7, 7 * 31 + 1, 7), *range(7 * 31 + 30, 7 * 31 + 30 * 20, 30)])
        self.assertEqual(plan_store(prepare(rows), WHOLESALER, DEFAULT).usual_gap, 7.0)

    def test_fewer_than_three_visits_and_a_typed_horizon(self):
        self.assertEqual(plan_store(prepare(visits((20, 10))), WHOLESALER, DEFAULT).usual_gap, 14.0)
        plan = plan_store(prepare(visits(range(10, 101, 10))), WHOLESALER, DEFAULT, horizon_days=30)
        self.assertEqual((plan.usual_gap, plan.horizon, plan.horizon_typed), (10.0, 30.0, True))

    def test_the_horizon_is_in_the_need(self):
        rows = [*visits(range(10, 101, 10)), *(bought(SYRUP, WHOLESALER, d, "10") for d in (40, 30, 20, 10))]
        score = score_article(prepare(rows), WHOLESALER, SYRUP, DEFAULT)
        self.assertAlmostEqual(score.need, (10 + 10) / score.cover, places=12)


class StoreChoiceTests(SimpleTestCase):
    def test_most_visited_over_a_year_first_and_the_rare_ones_said(self):
        rows = [*visits(range(10, 101, 10)), *visits((400, 300, 50), store=GROCER), *visits((5,), store=CORNER)]
        choices = store_choices(prepare(rows))
        self.assertEqual([choice.store_id for choice in choices], [WHOLESALER, GROCER])
        self.assertEqual([choice.visits_12m for choice in choices], [10, 2])
        self.assertEqual([choice.rare for choice in choices], [False, True])
        self.assertEqual(choices[0].usual_gap, 10.0)
        self.assertEqual(choices[0].last_visit, ago(10))


class RhythmTests(SimpleTestCase):
    def rhythm(self, rows, article=SYRUP, store_id=None, **kw):
        found = [row for row in rhythms(prepare(rows, **kw), DEFAULT, store_id) if row.article_id == article]
        return found[0] if found else None

    def test_the_states(self):
        cases = {
            "régulier": [bought(SYRUP, WHOLESALER, d) for d in range(14, 301, 14)],
            "assez régulier": [bought(SYRUP, WHOLESALER, d) for d in (10, 17, 45, 52, 80, 87, 115)],
            "irrégulier": [bought(SYRUP, WHOLESALER, d) for d in (100, 99, 98, 97, 7)],
            "occasionnel": [bought(SYRUP, WHOLESALER, d) for d in (200, 150, 100)],
            "ponctuel": [bought(SYRUP, WHOLESALER, 200)],
            "nouveau": [bought(SYRUP, WHOLESALER, d) for d in (60, 30)],
            "plus acheté ?": [bought(SYRUP, WHOLESALER, d) for d in range(200, 401, 14)],
            "consigne ?": [bought(SYRUP, WHOLESALER, d, "4") for d in (30, 20)] + [bought(SYRUP, WHOLESALER, 5, "-4")],
        }
        for state, rows in cases.items():
            self.assertEqual(self.rhythm(rows).state, state, state)
        paused = self.rhythm(cases["plus acheté ?"], till=till(every_day_till(0.1, 400, 1, SYRUP)))
        self.assertEqual(paused.state, "en pause")
        self.assertIsNotNone(paused.till_per_week)
        self.assertAlmostEqual(paused.till_per_week, 0.7, places=9)

    def test_the_figures_of_a_regular_article(self):
        rows = [*(bought(SYRUP, WHOLESALER, d, "2") for d in range(14, 301, 14)), bought(SYRUP, GROCER, 7, "2")]
        rhythm = self.rhythm(rows)
        self.assertEqual(rhythm.median_gap, 14.0)
        self.assertEqual(rhythm.last_day, ago(7))
        self.assertEqual(rhythm.usual_qty, Decimal("2"))
        self.assertEqual(
            [(s.store_id, round(s.share, 3)) for s in rhythm.stores], [(WHOLESALER, 0.955), (GROCER, 0.045)]
        )
        self.assertEqual(rhythm.trend, "stable")
        self.assertIsNotNone(rhythm.next_day)

    def test_a_rising_trend_and_a_purchase_due(self):
        rows = [bought(SYRUP, WHOLESALER, d, "2") for d in range(98, 350, 14)]
        rows += [bought(SYRUP, WHOLESALER, d, "6") for d in range(42, 92, 14)]
        rhythm = self.rhythm(rows)
        self.assertEqual(rhythm.trend, "en hausse")
        self.assertTrue(rhythm.due)

    def test_a_store_filter_keeps_its_articles_with_their_habit_there(self):
        rows = [
            *visits(range(10, 101, 10)),
            *(bought(SYRUP, WHOLESALER, d) for d in (90, 50, 10)),
            bought(TONIC, GROCER, 5),
        ]
        prepared = prepare(rows)
        found = rhythms(prepared, DEFAULT, WHOLESALER)
        self.assertNotIn(TONIC, [row.article_id for row in found])
        (syrup,) = [row for row in found if row.article_id == SYRUP]
        score = score_article(prepared, WHOLESALER, SYRUP, DEFAULT)
        self.assertEqual(syrup.habit_here, (score.hits, score.seen))

    def test_an_article_left_out_is_still_listed_and_said(self):
        rhythm = self.rhythm([bought(SYRUP, WHOLESALER, 5)], exclusions=Exclusions({SYRUP}))
        self.assertTrue(rhythm.excluded)

    def test_a_short_burst_then_silence_is_plus_achete_on_both_pages(self):
        rows = [*visits(range(5, 116, 5)), *(bought(SYRUP, WHOLESALER, d) for d in (85, 84, 80))]
        self.assertEqual(score_article(prepare(rows), WHOLESALER, SYRUP, DEFAULT).silent, DROPPED_LABEL)
        self.assertEqual(self.rhythm(rows).state, DROPPED_LABEL)
        paused = self.rhythm(rows, till=till(every_day_till(0.1, 400, 1, SYRUP)))
        self.assertEqual(paused.state, PAUSED_LABEL)
        lively = [*visits(range(5, 116, 5)), *(bought(SYRUP, WHOLESALER, d) for d in (60, 45, 30))]
        self.assertEqual(self.rhythm(lively).state, "nouveau")

    def test_the_next_purchase_is_the_last_one_plus_the_rhythm(self):
        # Every 60 days, the last one 100 days ago: 40 days overdue by its own rhythm.
        rhythm = self.rhythm([bought(SYRUP, WHOLESALER, d) for d in (280, 220, 160, 100)])
        self.assertEqual((rhythm.state, rhythm.median_gap), ("régulier", 60.0))
        self.assertEqual(rhythm.next_day, ago(40))
        self.assertTrue(rhythm.due)

    def test_no_next_purchase_for_an_article_no_longer_bought_paused_or_a_deposit(self):
        dropped = [bought(SYRUP, WHOLESALER, d) for d in (254, 247, 240)]
        rhythm = self.rhythm(dropped)
        self.assertEqual((rhythm.state, rhythm.next_day, rhythm.due), (DROPPED_LABEL, None, False))
        rhythm = self.rhythm(dropped, till=till(every_day_till(0.1, 400, 1, SYRUP)))
        self.assertEqual((rhythm.state, rhythm.next_day), (PAUSED_LABEL, None))
        deposit = [*(bought(SYRUP, WHOLESALER, d, "4") for d in (30, 20, 10)), bought(SYRUP, WHOLESALER, 5, "-6")]
        rhythm = self.rhythm(deposit)
        self.assertEqual((rhythm.state, rhythm.next_day), ("consigne ?", None))

    def test_the_trend_counts_the_earlier_rate_from_the_first_purchase(self):
        steady = self.rhythm([bought(SYRUP, WHOLESALER, d) for d in range(4, 271, 7)])  # weekly for 270 days
        self.assertEqual(steady.trend, "stable")
        young = self.rhythm([bought(SYRUP, WHOLESALER, d) for d in range(3, 179, 7)])  # 88 days before the last 90
        self.assertEqual(shopping.TREND_MIN_SPAN_DAYS, 90)
        self.assertIsNone(young.trend)
        stopped = self.rhythm([bought(SYRUP, WHOLESALER, d) for d in range(42, 183, 7)])
        self.assertEqual(stopped.trend, "stable")

    def test_the_till_per_week_counts_the_days_the_import_covers_only(self):
        rows = [bought(SYRUP, WHOLESALER, d) for d in (60, 30)]
        lagging = self.rhythm(rows, till=till(every_day_till(0.1, 400, 21, SYRUP), covered=21))
        self.assertAlmostEqual(lagging.till_per_week, 0.7, places=9)
        young = self.rhythm(rows, till=till(every_day_till(0.1, 30, 1, SYRUP), start=30))
        self.assertAlmostEqual(young.till_per_week, 0.7, places=9)


class CalibrationTests(SimpleTestCase):
    """Pins MODEL_C, MODEL_GAMMA and MODEL_DELTA. They were fitted once by maximum likelihood,
    logit(habit) as an offset, on the tuning half of the owner's purchase history; refit them
    only with a recorded procedure of the same kind, then update these figures in the same
    change (the invented dataset below stands still)."""

    def test_the_constants(self):
        self.assertEqual((shopping.MODEL_C, shopping.MODEL_GAMMA, shopping.MODEL_DELTA), (0.39, 0.82, 2.00))
        self.assertEqual((shopping.NEED_MIN, shopping.NEED_CAP), (0.1, 2.0))
        self.assertEqual((shopping.PRIOR_VISITS, shopping.PRIOR_SHARE), (1.0, 0.10))

    def test_a_fixed_dataset_gives_fixed_chances(self):
        prepared = prepare(
            [
                *lifted_line_rows(),
                *(bought(TONIC, WHOLESALER, d, "6") for d in range(14, 211, 28)),
                *(bought(LEMONS, WHOLESALER, d, "2") for d in (140, 98)),
                *(bought(KEG, WHOLESALER, d, "6") for d in (154, 112, 70, 28)),
            ],
            till=till(every_day_till(0.15, 400, 1, KEG)),
        )
        pinned = {SYRUP: CHANCE_SYRUP, TONIC: CHANCE_TONIC, LEMONS: CHANCE_LEMONS, KEG: CHANCE_KEG}
        for article, chance in pinned.items():
            score = score_article(prepared, WHOLESALER, article, DEFAULT)
            self.assertAlmostEqual(score.chance, chance, places=9, msg=article)
        self.assertEqual(score_article(prepared, WHOLESALER, KEG, DEFAULT).clock, TILL_CLOCK)


# Worked out once with MODEL_C = 0.39, MODEL_GAMMA = 0.82, MODEL_DELTA = 2.00: the syrup's need
# is clipped (the calendar clock), the tonic's is not, the lemons are bought on 2 days (DELTA, no
# need), the keg's need is on the till clock.
CHANCE_SYRUP = 0.35281525625134075
CHANCE_TONIC = 0.5973626027726978
CHANCE_LEMONS = 0.03683659568977611
CHANCE_KEG = 0.4173097175423668


class SpeedTests(SimpleTestCase):
    BIG_STORE = 300  # an invented size, no real store's count

    def test_a_big_store_plans_well_under_a_second(self):
        articles = extra_articles(self.BIG_STORE)
        rows = [bought(CUPS, WHOLESALER, d, "1") for d in range(7, 731, 7)]
        for i, article in enumerate(articles):
            step = 7 * (1 + i % 9)
            rows += [bought(article.id, WHOLESALER, d, "2") for d in range(7 + 7 * (i % 4), 731, step)]
        prepared = prepare(rows, articles=[*ARTICLES, *articles])
        started = time.perf_counter()
        plan = plan_store(prepared, WHOLESALER, DEFAULT)
        spent = time.perf_counter() - started
        self.assertEqual(plan.candidates, self.BIG_STORE + 1)
        self.assertLess(spent, 1.0)
