"""« Prévoir les courses »: what to buy at one store on the next visit, from
the purchase history - and the till's sales, when the owner keeps them in.

Pure: no ORM, no request, no Django. `shopping_data.prepare` reads the
database once and hands plain rows to `Prepared.build`, the single entry;
`plan_store`, `store_choices`, `rhythms`, `score_article` and
`usual_purchase` (one article's usual purchase at one store, the shopping
lists') then work on that in memory, with bisect and running sums.

**One chance per article.** Every article already bought at the store gets
the chance that the owner takes it on this visit (`chance_of`):

    chance = sigmoid( logit(habit) + MODEL_C + MODEL_GAMMA * ln(clip(need, NEED_MIN, NEED_CAP))
                      - MODEL_DELTA * [bought on fewer than FEW_PURCHASES days] )

* habit - the weighted share of the store's visits, since the article was
  first bought ANYWHERE, that included it: a visit `memory_months` old
  counts half, and a prior worth PRIOR_VISITS visit at PRIOR_SHARE keeps one
  purchase from reading as a certainty. Visits made before the article
  existed do not dilute it; a dropped article fades visit after visit.
* need - how used up the last purchase, made at ANY store, will be by the
  next visit (the horizon: the typed « Prochain passage dans », else the
  store's usual gap). The till clock for an article the recipes pour: what
  the till sold since, times k (what is bought per unit poured, kept only
  when the recipes explain the purchases), extrapolated at its recent rate
  past the import's coverage when that lags more than STALE_DAYS. The
  calendar clock for every other article: the days since, over how long one
  purchase usually lasts. No need below FEW_PURCHASES purchase days.

**Off the list, and labels.** Two guards keep a line off « À acheter » and
put it in a section of its own: a deposit-like article (returns at least
DEPOSIT_SHARE of what was bought) and an article whose last ELSEWHERE_LAST
purchases were all at other stores. « plus acheté ? » (silent far beyond its
rhythm) and « en pause » (the same, but a recipe still sells it) only label
a line: as gates they cost the summer's returning articles. A line already
proposed at the store's last NAG_VISITS_LIFTED visits (NAG_VISITS_HABIT when
the habit alone lists it) and not bought there moves to « Peut-être »: those
visits are replayed on the same data cut at each, at the store's usual gap
then (a typed « Prochain passage dans » is today's page's), nag rule left
out. A nagged or a « plus acheté ? » line reads « à vérifier », never a
calibrated word.

**Today.** Everything dated today or earlier is history: a visit made today
is one of the store's visits, a purchase made today is the last purchase.
Every age, weight and window counts from today, as the measurement counted
from the visit's day, so today's purchases stay out of the rate windows
([first purchase, today)) and today's sales are not read (the day is not
over). A replayed visit v sees what was dated before v - the page as drawn
that morning.

Quantities and money stay Decimal; the scores are floats, converted at the
score's boundary only.
"""

from __future__ import annotations

import bisect
import itertools
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")
DAY = timedelta(days=1)

# --------------------------------------------------------------------- constants
# Each was chosen on the tuning half of the owner's purchase history only (or
# taken from an earlier design's grid, as said), then checked on the halves
# never tuned on. Change one only with a recorded measurement.

# The model. Fitted once by maximum likelihood, logit(habit) as an offset, on
# the tuning half's candidates with a live rhythm (unguarded, not silent).
# Refit only with a recorded procedure of the same kind, and update
# CalibrationTests (inventory/tests/test_shopping.py) in the same change.
MODEL_C = 0.39  # what a visit adds to the habit's odds
MODEL_GAMMA = 0.82  # weight of ln(need)
MODEL_DELTA = 2.00  # not yet a habit: bought on fewer than FEW_PURCHASES days
# The need's effect is clipped: higher caps measured the same on the grid.
NEED_MIN, NEED_CAP = 0.1, 2.0
# The habit's prior: one visit at this share (a grid that measured flat).
PRIOR_VISITS, PRIOR_SHARE = 1.0, 0.10
# A probability is clamped this far from 0 and 1 before its logit.
LOGIT_CLAMP = 1e-4
# Half-life of every rate (calendar purchases and till sales), in days: the
# study's rate.
RATE_HALF_LIFE_DAYS = 90
RATE_DECAY = 0.5 ** (1 / RATE_HALF_LIFE_DAYS)
# Below this many purchase days: the DELTA penalty, and no need computed.
FEW_PURCHASES = 3
# Below this many purchase days: the « historique court » badge (a label only).
SHORT_HISTORY = 5
# The confidence words' thresholds on the chance.
SURE, LIKELY = 0.60, 0.40
# « Peut-être » holds [threshold * MAYBE_RATIO, threshold).
MAYBE_RATIO = 0.5
# The till's window, in days back from the visit (the sales read, and k).
TILL_WINDOW_DAYS = 365
# k (bought per unit poured) outside these bounds: the recipes do not explain
# the purchases, and the calendar clock is used.
K_MIN, K_MAX = 0.25, 5.0
# How many days the till import may lag before its consumption is
# extrapolated at its recent rate.
STALE_DAYS = 3
# Returns at least this share of what was bought: deposit-like.
DEPOSIT_SHARE = Decimal("0.5")
# « plus acheté ? »: silent for more than max(DROP_FACTOR x its median
# interval, DROP_MIN_DAYS) - unless a recipe sold it in the last PAUSE_DAYS
# the till covers, which is « en pause ».
DROP_FACTOR, DROP_MIN_DAYS = 3.0, 60
PAUSE_DAYS = 30
# « Acheté ailleurs maintenant »: the last this-many purchases all elsewhere.
ELSEWHERE_LAST = 3
# The nag rule: listed and not bought at this many of the store's last
# visits moves a line to « Peut-être » - one the need lifted, one the habit
# alone lists.
NAG_VISITS_LIFTED, NAG_VISITS_HABIT = 3, 5
# Quantity: the median of the last QTY_LAST purchase days here; the usual
# product is the one bought most among the last PRODUCT_LAST.
QTY_LAST, PRODUCT_LAST = 3, 5
# « Nouveaux ici »: first bought within this many days.
NEW_DAYS = 90
# The usual gap: median of the gaps between the store's last GAP_VISITS + 1
# visits; DEFAULT_GAP_DAYS with fewer than 3 visits.
GAP_VISITS, DEFAULT_GAP_DAYS = 30, 14.0
# Under this many visits the list is indicative (« Peu de passages ici »).
FEW_VISITS = 5
# How many lines the folds show.
TOP_HERE, ELSEWHERE_SHOWN, DUE_ELSEWHERE_SHOWN = 10, 10, 5
# « Within a year »: the folds' and the rhythm view's window.
YEAR_DAYS = 365
# A store with fewer visits than this in the last year is « rare ».
RARE_VISITS = 3

# « Rythme d'achat »: the state of the gaps (coefficient of variation) and the trend.
REGULAR_CV, FAIRLY_REGULAR_CV = 0.5, 1.0
MIN_GAPS_FOR_CV = 3
TREND_RECENT_DAYS = 90
TREND_UP, TREND_DOWN = Decimal("1.5"), Decimal("0.5")
TREND_MIN_PURCHASES = 4
# The earlier rate is counted from the first purchase; over fewer days than
# this it says nothing, and there is no trend.
TREND_MIN_SPAN_DAYS = 90
TILL_WEEK_DAYS = 91  # 13 weeks
# A rhythm longer than this is no date to buy again on.
MAX_RHYTHM_DAYS = 3650

# The owner's settings' defaults (ShoppingSetting).
DEFAULT_THRESHOLD_PERCENT = 25
DEFAULT_MEMORY_MONTHS = 6
DAYS_PER_MONTH = 30

# A line's section on the page.
TO_BUY = "to_buy"
MAYBE = "maybe"
NEW = "new"
QUIET = "quiet"
ELSEWHERE = "elsewhere"
DEPOSIT = "deposit"

# A candidate's status (`Score.status`): proposable, or held off the list.
OK = "ok"
DEPOSIT_LIKE = "deposit"
BOUGHT_ELSEWHERE = "elsewhere"

# The clock that gave the need.
TILL_CLOCK = "till"
CALENDAR_CLOCK = "calendar"

# The habit's window, as the sentence says it.
SPAN_MEMORY = "memory"  # the memory span
SPAN_ADOPTION = "adoption"  # since the first purchase, inside the memory span
SPAN_LAST = "last"  # widened to the store's last 3 visits

# Displayed words (French).
DROPPED_LABEL = "plus acheté ?"
PAUSED_LABEL = "en pause"
SHORT_HISTORY_LABEL = "historique court"
SURE_WORD, LIKELY_WORD, POSSIBLE_WORD = "presque sûr", "probable", "possible"
# The words are calibrated on lines with a live rhythm only: a nagged or a
# « plus acheté ? » line reads this instead.
UNSURE_WORD = "à vérifier"
DEPOSIT_STATE = "consigne ?"
NEW_STATE = "nouveau"
ONE_OFF_STATE = "ponctuel"
OCCASIONAL_STATE = "occasionnel"
REGULAR_STATE = "régulier"
FAIRLY_REGULAR_STATE = "assez régulier"
IRREGULAR_STATE = "irrégulier"
TREND_UP_WORD, TREND_DOWN_WORD, TREND_FLAT_WORD = "en hausse", "en baisse", "stable"
OTHER_STORE = "autre enseigne"


# --------------------------------------------------------------------- inputs
@dataclass(frozen=True, slots=True)
class PurchaseRow:
    """One purchase movement as the data layer reads it: an invoice line's
    PURCHASE movement. `qty` is in the article's unit and signed - a negative
    one is a return or a deposit refunded; `value_ht` is the line's total_ht
    plus its spread_ht; `product_units` is the line's own quantity
    (InvoiceLine.quantity) and `colisage` its pack size."""

    article_id: int
    store_id: int
    day: date | None
    qty: Decimal | None
    value_ht: Decimal | None = ZERO
    product_id: int | None = None
    product_units: Decimal | None = ZERO
    colisage: Decimal | int | None = 1


@dataclass(frozen=True, slots=True)
class ReturnRow:
    """A return read apart (optional: a negative PurchaseRow is one too).
    Its quantity's sign is ignored: returns are summed in absolute value."""

    article_id: int
    store_id: int
    day: date | None
    qty: Decimal | None


@dataclass(frozen=True, slots=True)
class ArticleInfo:
    """An article (StockType). `unit` is its stored code (L, KG, UNIT);
    `category` "" means none."""

    id: int
    name: str
    unit: str = ""
    category: str | None = ""


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """A store the page offers (a supplier with purchases, neither
    expenses_only nor the removed AI reading's)."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Exclusions:
    """What the owner left out (ShoppingExclusion): articles everywhere,
    whole categories ("" = the articles with no category), and (article,
    store) pairs (« Pas ici »)."""

    everywhere_articles: frozenset[int] = frozenset()
    categories: frozenset[str] = frozenset()
    per_store: frozenset[tuple[int, int]] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "everywhere_articles", frozenset(self.everywhere_articles))
        object.__setattr__(self, "categories", frozenset(self.categories))
        object.__setattr__(self, "per_store", frozenset(self.per_store))

    def everywhere(self, article: ArticleInfo) -> bool:
        """Never proposed anywhere: excluded alone, or with its category."""
        return article.id in self.everywhere_articles or (article.category or "") in self.categories

    def at(self, article: ArticleInfo, store_id: int) -> bool:
        """Never proposed at this store."""
        return self.everywhere(article) or (article.id, store_id) in self.per_store


@dataclass(frozen=True, slots=True)
class TillData:
    """What the till sold of each article, per day (consumption.md, Option
    A): {article: [(day, quantity)]} in the article's unit, as floats.
    `start` is the till's first day of sales, `covered_until` the last day
    its import covers, `ref_day` the last complete day of sales
    (`last_complete_day(now)`; the day before today by default)."""

    series: Mapping[int, Iterable[tuple[date, float]]]
    start: date
    covered_until: date
    ref_day: date | None = None


# --------------------------------------------------------------------- prepared data
@dataclass(frozen=True, slots=True)
class ProductBuy:
    """One product of an article bought at a store on a day: its units
    (InvoiceLine.quantity summed) and its pack size."""

    product_id: int
    units: Decimal
    colisage: Decimal


@dataclass(frozen=True, slots=True)
class DayBuy:
    """An article bought on a day: per store, or summed over every store
    (`stores` then holds each store of that day)."""

    day: date
    qty: Decimal
    value_ht: Decimal
    stores: frozenset[int]
    products: tuple[ProductBuy, ...] = ()


@dataclass(frozen=True, slots=True)
class History:
    """An article's purchase days (at every store, or at one), ascending,
    with running sums: qty_cum[i] is what the first i days bought, and
    ew_cum[i] the same, each day weighted RATE_DECAY ** (days before
    `anchor`, the last day) - a rate over any window is two lookups."""

    buys: tuple[DayBuy, ...]
    days: tuple[date, ...]
    qty_cum: tuple[Decimal, ...]
    ew_cum: tuple[float, ...]
    anchor: date


@dataclass(frozen=True, slots=True)
class Returned:
    """An article's returns, per day ascending, with their running sum."""

    days: tuple[date, ...]
    cum: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class TillSeries:
    """An article's daily till consumption: days ascending, cum[i] the sum
    of the first i days, ew_cum the same weighted as History.ew_cum."""

    days: tuple[date, ...]
    cum: tuple[float, ...]
    ew_cum: tuple[float, ...]
    anchor: date


@dataclass(frozen=True, slots=True)
class Settings:
    """The owner's settings (ShoppingSetting). The typed « Prochain passage
    dans » is not one: it is plan_store's `horizon_days`."""

    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT
    memory_months: int = DEFAULT_MEMORY_MONTHS
    use_till: bool = True

    @property
    def threshold(self) -> float:
        """The chance a line needs to be on the list."""
        return self.threshold_percent / 100

    @property
    def memory_days(self) -> int:
        """The age at which a visit counts half in the habit."""
        return DAYS_PER_MONTH * self.memory_months


DEFAULT_SETTINGS = Settings()


@dataclass(frozen=True, slots=True)
class Prepared:
    """Everything the page reads, in memory. Build it with `Prepared.build`."""

    today: date
    ref_day: date
    stores: Mapping[int, StoreInfo]
    articles: Mapping[int, ArticleInfo]
    product_names: Mapping[int, str]
    buys: Mapping[int, History]  # per article, every store
    buys_at: Mapping[tuple[int, int], History]  # per (article, store)
    visits: Mapping[int, tuple[date, ...]]  # per store, ascending
    articles_at: Mapping[int, tuple[int, ...]]  # per store: the articles ever bought there
    returned: Mapping[int, Returned]
    till: Mapping[int, TillSeries] | None
    till_start: date | None
    covered_until: date | None
    excluded: Exclusions

    @classmethod
    def build(
        cls,
        *,
        today: date,
        purchases: Iterable[PurchaseRow],
        articles: Iterable[ArticleInfo],
        stores: Iterable[StoreInfo],
        returns: Iterable[ReturnRow] = (),
        product_names: Mapping[int, str] | None = None,
        till: TillData | None = None,
        exclusions: Exclusions | None = None,
    ) -> Prepared:
        """The single entry from plain rows.

        * A row dated after today, undated, or with no or a zero quantity is
          left out; a negative one is a return, summed per article in
          absolute value, and never a purchase.
        * Purchases are summed per (article, store, day) - quantity, value
          and each product's units, the product's pack size being the
          largest seen that day - and per (article, day) over every store.
        * A store's visit days are the days with a positive row there, on any
          article, excluded or unknown ones included; an article missing
          from `articles` is no candidate.
        * `till`: None when the till is off or has no sales; its days after
          today are left out.
        """
        articles_by_id = {article.id: article for article in articles}
        stores_by_id = {store.id: store for store in stores}
        at_day: dict[tuple[int, int, date], list] = {}  # [qty, value, {product: [units, colisage]}]
        visit_days: dict[int, set[date]] = defaultdict(set)
        returned_rows: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
        for row in purchases:
            qty = _decimal(row.qty)
            if row.day is None or row.day > today or qty is None or not qty.is_finite() or qty == 0:
                continue
            if qty < 0:
                returned_rows[row.article_id].append((row.day, -qty))
                continue
            visit_days[row.store_id].add(row.day)
            if row.article_id not in articles_by_id:
                continue
            held = at_day.get((row.article_id, row.store_id, row.day))
            if held is None:
                held = at_day[(row.article_id, row.store_id, row.day)] = [ZERO, ZERO, {}]
            held[0] += qty
            value = _decimal(row.value_ht)
            if value is not None and value.is_finite():
                held[1] += value
            if row.product_id is not None:
                units = _decimal(row.product_units)
                units = units if units is not None and units.is_finite() else ZERO
                colisage = _decimal(row.colisage)
                colisage = colisage if colisage is not None and colisage.is_finite() and colisage > 0 else ONE
                product = held[2].get(row.product_id)
                if product is None:
                    held[2][row.product_id] = [units, colisage]
                else:
                    product[0] += units
                    product[1] = max(product[1], colisage)
        for row in returns:
            qty = _decimal(row.qty)
            if row.day is None or row.day > today or qty is None or not qty.is_finite() or qty == 0:
                continue
            returned_rows[row.article_id].append((row.day, abs(qty)))

        per_store: dict[tuple[int, int], list[DayBuy]] = defaultdict(list)
        per_day: dict[tuple[int, date], list] = {}  # [qty, value, stores, products]
        for (article_id, store_id, day), (qty, value, products) in at_day.items():
            bought = tuple(
                ProductBuy(product_id, units, colisage) for product_id, (units, colisage) in sorted(products.items())
            )
            per_store[(article_id, store_id)].append(DayBuy(day, qty, value, frozenset((store_id,)), bought))
            held = per_day.get((article_id, day))
            if held is None:
                per_day[(article_id, day)] = [qty, value, {store_id}, list(bought)]
            else:
                held[0] += qty
                held[1] += value
                held[2].add(store_id)
                held[3].extend(bought)
        per_article: dict[int, list[DayBuy]] = defaultdict(list)
        for (article_id, day), (qty, value, day_stores, products) in per_day.items():
            products.sort(key=lambda product: product.product_id)
            per_article[article_id].append(DayBuy(day, qty, value, frozenset(day_stores), tuple(products)))

        articles_at: dict[int, list[int]] = defaultdict(list)
        for article_id, store_id in per_store:
            articles_at[store_id].append(article_id)

        returned = {}
        for article_id, rows in returned_rows.items():
            rows.sort()
            cum = [ZERO]
            for _day, qty in rows:
                cum.append(cum[-1] + qty)
            returned[article_id] = Returned(tuple(day for day, _qty in rows), tuple(cum))

        series = None
        till_start = covered_until = None
        ref_day = today - DAY
        if till is not None:
            series = {}
            for article_id, points in till.series.items():
                daily: dict[date, float] = defaultdict(float)
                for day, qty in points:
                    if day is not None and day <= today:
                        daily[day] += float(qty)
                if daily:
                    series[article_id] = _till_series(sorted(daily.items()))
            till_start, covered_until = till.start, till.covered_until
            if till.ref_day is not None:
                ref_day = till.ref_day

        return cls(
            today=today,
            ref_day=ref_day,
            stores=stores_by_id,
            articles=articles_by_id,
            product_names=dict(product_names or {}),
            buys={article_id: _history(rows) for article_id, rows in per_article.items()},
            buys_at={key: _history(rows) for key, rows in per_store.items()},
            visits={store_id: tuple(sorted(days)) for store_id, days in visit_days.items()},
            articles_at={store_id: tuple(sorted(ids)) for store_id, ids in articles_at.items()},
            returned=returned,
            till=series,
            till_start=till_start,
            covered_until=covered_until,
            excluded=exclusions or Exclusions(),
        )


def _decimal(value) -> Decimal | None:
    """A quantity or an amount as a Decimal. A float is refused: money and
    quantities never go through one (CLAUDE.md, « Money is always Decimal »)."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, (bool, float)):
        raise TypeError("quantities and amounts are Decimal, never float")
    return Decimal(value)


def _history(buys: list[DayBuy]) -> History:
    buys.sort(key=lambda buy: buy.day)
    anchor = buys[-1].day
    qty_cum = [ZERO]
    ew_cum = [0.0]
    for buy in buys:
        qty_cum.append(qty_cum[-1] + buy.qty)
        ew_cum.append(ew_cum[-1] + float(buy.qty) * RATE_DECAY ** (anchor - buy.day).days)
    return History(tuple(buys), tuple(buy.day for buy in buys), tuple(qty_cum), tuple(ew_cum), anchor)


def _till_series(points: list[tuple[date, float]]) -> TillSeries:
    anchor = points[-1][0]
    cum = [0.0]
    ew_cum = [0.0]
    for day, qty in points:
        cum.append(cum[-1] + qty)
        ew_cum.append(ew_cum[-1] + qty * RATE_DECAY ** (anchor - day).days)
    return TillSeries(tuple(day for day, _qty in points), tuple(cum), tuple(ew_cum), anchor)


# --------------------------------------------------------------------- outputs
@dataclass(frozen=True, slots=True)
class Score:
    """One candidate's figures at a store as of a day, before the nag rule
    (`score_article`; plan_store builds its lines from these)."""

    article_id: int
    habit: float
    hits: int  # purchase days here over the habit's window...
    seen: int  # ...and the store's visits in it
    span: str  # SPAN_MEMORY | SPAN_ADOPTION | SPAN_LAST
    days_bought: int  # purchase days anywhere
    days_bought_here: int
    first_bought: date  # anywhere
    last_bought: date  # anywhere
    last_bought_here: date
    since: int  # days since the last purchase anywhere
    need: float | None
    clock: str | None  # TILL_CLOCK | CALENDAR_CLOCK
    cover: float | None  # calendar clock: the days one purchase lasts, at the rate up to today
    till_share: float | None  # till clock: share of the last purchase already sold, 0 at least
    # Till clock: the first day the import does not cover, when the share counts
    # days past it at the recent rate (an estimate); None when it holds none.
    till_estimated_from: date | None
    rate: float | None  # article units a day, by the clock that gave the need
    median_interval: float | None  # days between purchase days, anywhere
    chance: float
    status: str  # OK | DEPOSIT_LIKE | BOUGHT_ELSEWHERE
    silent: str | None  # DROPPED_LABEL | PAUSED_LABEL
    elsewhere_since: int | None  # store of the last purchase, if after the last one here
    qty: Decimal  # median of the last QTY_LAST purchase days here, article units


@dataclass(frozen=True, slots=True)
class Line:
    """One article on the page. `qty` and `product_units` are the usual
    amounts (median of the last purchases here); with a typed horizon longer
    than the usual gap the page shows `multiplier` x them (`total_qty`,
    `total_product_units`). `product_units` is None when the usual product
    is unknown or bought by measure (m², loose kg): the article units alone
    are shown. `packs` is (packs, colisage) when the colisage divides the
    count. `confidence` is the chance's calibrated word, or UNSURE_WORD on a
    nagged or a « plus acheté ? » line."""

    article_id: int
    name: str
    unit: str
    category: str
    section: str
    chance: float
    confidence: str
    sentence: str
    badges: tuple[str, ...]
    qty: Decimal
    product_id: int | None
    product_name: str
    product_units: Decimal | None
    packs: tuple[int, Decimal] | None
    multiplier: int
    last_bought_here: date
    habit: float
    need: float | None
    clock: str | None
    nagged: bool = False

    @property
    def total_qty(self) -> Decimal:
        return self.qty * self.multiplier

    @property
    def total_product_units(self) -> Decimal | None:
        return None if self.product_units is None else self.product_units * self.multiplier


@dataclass(frozen=True, slots=True)
class DueElsewhere:
    """An article never bought at this store whose calendar need at its
    horizon has come (« À acheter ailleurs »), with its usual store - never
    one the article is left out at. `badges` holds « en pause » when a recipe
    still sells an article silent far past its rhythm."""

    article_id: int
    name: str
    unit: str
    need: float
    store_id: int
    store_name: str
    sentence: str
    badges: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TopLine:
    """« Les plus achetés ici »: purchase days and HT here over the last year."""

    article_id: int
    name: str
    unit: str
    purchases_12m: int
    total_ht: Decimal


@dataclass(frozen=True, slots=True)
class TillNote:
    """How fresh the till's sales are: `stale` when the import lags more
    than STALE_DAYS behind the last complete day (the page warns that the
    rest is estimated)."""

    covered_until: date
    lag_days: int
    stale: bool


@dataclass(frozen=True, slots=True)
class StorePlan:
    """The page for one store. Each article shows in one section at most:
    « À acheter » (`to_buy`), « Peut-être » (`maybe`, the nag-moved lines
    included), then « Nouveaux ici », « Plus acheté ? », « Acheté ailleurs
    maintenant » (the first ELSEWHERE_SHOWN of `elsewhere_count`) and the
    deposit-like ones. `candidates` 0 is a first visit."""

    store_id: int
    store_name: str
    today: date
    usual_gap: float
    horizon: float
    horizon_typed: bool
    visits: int
    last_visit: date | None
    few_visits: bool
    candidates: int
    expected_basket: float
    till_note: TillNote | None
    to_buy: tuple[Line, ...]
    maybe: tuple[Line, ...]
    new: tuple[Line, ...]
    quiet: tuple[Line, ...]
    elsewhere: tuple[Line, ...]
    elsewhere_count: int
    due_elsewhere: tuple[DueElsewhere, ...]
    top_here: tuple[TopLine, ...]
    deposits: tuple[Line, ...]
    deposit_hint_categories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoreChoice:
    """A store of the « Enseigne » menu, most visited first."""

    store_id: int
    name: str
    visits_12m: int
    usual_gap: float
    last_visit: date
    rare: bool  # fewer than RARE_VISITS visits in the last year: « Autres enseignes »


@dataclass(frozen=True, slots=True)
class StoreShare:
    """A store's share of an article's purchase days."""

    store_id: int
    name: str
    share: float


@dataclass(frozen=True, slots=True)
class ArticleRhythm:
    """One row of « Rythme d'achat »."""

    article_id: int
    name: str
    unit: str
    category: str
    state: str
    median_gap: float | None  # days, over the gaps ending in the last year (else all)
    last_day: date
    # Last purchase + median_gap rounded half up, as « Rythme » says it - from 3
    # purchase days on; None for « plus acheté ? », « en pause » and « consigne ? ».
    next_day: date | None
    due: bool  # next_day is today or past: « à racheter »
    stores: tuple[StoreShare, ...]
    habit_here: tuple[int, int] | None  # (hits, seen) at the filtered store
    usual_qty: Decimal
    trend: str | None
    till_per_week: float | None  # over the last TILL_WEEK_DAYS the import covers, and since the till started
    purchases_12m: int
    excluded: bool  # never proposed anywhere (alone or with its category)


# --------------------------------------------------------------------- the model
def logit(p: float) -> float:
    p = min(max(p, LOGIT_CLAMP), 1 - LOGIT_CLAMP)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z < -40:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def chance_of(habit: float, need: float | None, few: bool) -> float:
    """The calibrated chance: see the module's docstring."""
    ln_need = math.log(min(max(need, NEED_MIN), NEED_CAP)) if need is not None else 0.0
    return sigmoid(logit(habit) + MODEL_C + MODEL_GAMMA * ln_need - (MODEL_DELTA if few else 0.0))


def confidence_of(chance: float) -> str:
    if chance >= SURE:
        return SURE_WORD
    if chance >= LIKELY:
        return LIKELY_WORD
    return POSSIBLE_WORD


@dataclass(frozen=True, slots=True)
class _AsOf:
    """The data as seen on a day: `day` is D, every age and window counts
    from it; history is what is dated before `stop` (today + 1 for the page,
    v itself for a replayed visit)."""

    day: date
    stop: date
    covered_until: date | None
    ref_day: date
    till_start: date | None

    @property
    def fresh(self) -> bool:
        return self.covered_until is not None and (self.ref_day - self.covered_until).days <= STALE_DAYS


def _today(prepared: Prepared) -> _AsOf:
    today = prepared.today
    start = prepared.till_start
    return _AsOf(
        day=today,
        stop=today + DAY,
        covered_until=prepared.covered_until,
        ref_day=prepared.ref_day,
        till_start=start if start is not None and start < today else None,
    )


def _before(prepared: Prepared, visit: date) -> _AsOf:
    """The page as drawn on the morning of `visit`: what was dated before
    it, and a till import at most complete up to the day before."""
    start = prepared.till_start
    covered = prepared.covered_until
    return _AsOf(
        day=visit,
        stop=visit,
        covered_until=None if covered is None else min(covered, visit - DAY),
        ref_day=min(prepared.ref_day, visit - DAY),
        till_start=start if start is not None and start < visit else None,
    )


@dataclass(frozen=True, slots=True)
class _StoreState:
    visits: tuple[date, ...]  # before as_of.stop
    suffix: tuple[float, ...]  # suffix[i] = the weights of visits[i:]
    usual_gap: float
    horizon: float


def _usual_gap(visits: tuple[date, ...]) -> float:
    """The median gap between the store's last GAP_VISITS + 1 visits."""
    last = visits[-(GAP_VISITS + 1) :]
    gaps = [(later - earlier).days for earlier, later in itertools.pairwise(last)]
    return float(statistics.median(gaps)) if len(gaps) >= 2 else DEFAULT_GAP_DAYS


def _store_state(
    prepared: Prepared, store_id: int, settings: Settings, as_of: _AsOf, horizon_days: float | None
) -> _StoreState:
    every = prepared.visits.get(store_id, ())
    visits = every[: bisect.bisect_left(every, as_of.stop)]
    memory = settings.memory_days
    suffix = [0.0] * (len(visits) + 1)
    for i in range(len(visits) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + 0.5 ** ((as_of.day - visits[i]).days / memory)
    usual_gap = _usual_gap(visits)
    horizon = float(horizon_days) if horizon_days is not None else usual_gap
    return _StoreState(visits, tuple(suffix), usual_gap, horizon)


def _ew_rate(days, ew_cum, anchor: date, first: date, end: date, cap: date) -> float | None:
    """Recency-weighted mean per day (half-life RATE_HALF_LIFE_DAYS) of what
    is dated in [first, min(end, cap)), over the days [first, end)."""
    n_days = (end - first).days
    if n_days <= 0:
        return None
    i = bisect.bisect_left(days, first)
    j = bisect.bisect_left(days, min(end, cap))
    num = (ew_cum[j] - ew_cum[i]) * RATE_DECAY ** ((end - DAY - anchor).days) if j > i else 0.0
    return num / ((1 - RATE_DECAY**n_days) / (1 - RATE_DECAY))


def _consumed(series: TillSeries, start: date, end: date, cap: date) -> float:
    """What the till sold in [start, min(end, cap))."""
    i = bisect.bisect_left(series.days, start)
    j = bisect.bisect_left(series.days, min(end, cap))
    return series.cum[j] - series.cum[i] if j > i else 0.0


def _covered(series: TillSeries, cap: date) -> bool:
    """A recipe sold it before `cap`."""
    return series.cum[bisect.bisect_left(series.days, cap)] > 0


def _series(prepared: Prepared, settings: Settings, article_id: int) -> TillSeries | None:
    if not settings.use_till or prepared.till is None:
        return None
    return prepared.till.get(article_id)


def _habit(
    history: History, here: History, n_here: int, state: _StoreState, settings: Settings, as_of: _AsOf
) -> tuple[float, int, int, str]:
    """(habit, hits, seen, span): the weighted share of the store's visits
    since the article's first purchase anywhere, and the plain counts over
    the window it weighs (the memory span, or since that purchase, widened
    to the store's last 3 visits)."""
    visits = state.visits
    memory = settings.memory_days
    day = as_of.day
    first = bisect.bisect_left(visits, history.days[0])
    weighed = sum(0.5 ** ((day - bought).days / memory) for bought in here.days[:n_here])
    habit = (weighed + PRIOR_VISITS * PRIOR_SHARE) / (state.suffix[first] + PRIOR_VISITS)
    memory_start = day - timedelta(days=memory)
    low = max(first, bisect.bisect_left(visits, memory_start))
    span = SPAN_ADOPTION if first < len(visits) and visits[first] > memory_start else SPAN_MEMORY
    if len(visits) - low < 3:
        low = max(first, len(visits) - 3)
        span = SPAN_LAST
    seen = len(visits) - low
    since = visits[low] if seen else day
    hits = n_here - bisect.bisect_left(here.days, since, 0, n_here)
    return habit, hits, seen, span


def _till_need(
    series: TillSeries | None, history: History, n_all: int, as_of: _AsOf, horizon: float
) -> tuple[float, float, float, date | None] | None:
    """(need, share of the last purchase already sold, k x the daily rate,
    the first day estimated) on the till clock, or None when it does not
    apply. The first day estimated is the first day the import does not
    cover when the share counts days past it at the recent rate, else None.
    What was sold since the last purchase is 0 at least: a net refund undoes
    a sale counted before that purchase, and none of it came back."""
    day = as_of.day
    if series is None or as_of.till_start is None or as_of.covered_until is None or not _covered(series, day):
        return None
    start = max(as_of.till_start, day - timedelta(days=TILL_WINDOW_DAYS))
    end = min(day, as_of.covered_until + DAY)  # the first day the import does not cover
    days = history.days
    low = bisect.bisect_left(days, start, 0, n_all)
    high = bisect.bisect_left(days, end, low, n_all)  # k only where the till is known
    if n_all - low < FEW_PURCHASES or high - low < FEW_PURCHASES:
        return None
    poured = _consumed(series, days[low], days[high - 1], day)
    if poured <= 0:
        return None
    k = float(history.qty_cum[high - 1] - history.qty_cum[low]) / poured
    if not K_MIN <= k <= K_MAX:
        return None
    last_qty = float(history.buys[n_all - 1].qty)
    if last_qty <= 0:
        return None
    last = days[n_all - 1]
    estimated_from = None
    if as_of.fresh:
        rate = _ew_rate(series.days, series.ew_cum, series.anchor, start, day, day) or 0.0
        sold = _consumed(series, last, day, day)
    else:
        end = as_of.covered_until + DAY
        rate = _ew_rate(series.days, series.ew_cum, series.anchor, start, end, day) or 0.0
        extrapolated = (day - max(end, last)).days
        sold = _consumed(series, last, end, day) + rate * max(0, extrapolated)
        if extrapolated > 0:
            estimated_from = end
    sold = max(0.0, sold)
    return k * (sold + rate * horizon) / last_qty, k * sold / last_qty, k * rate, estimated_from


def _calendar_need(history: History, n_all: int, day: date, horizon: float) -> tuple[float, float, float] | None:
    """(need, the days one purchase lasts, the daily rate) on the calendar
    clock, or None."""
    rate = _ew_rate(history.days, history.ew_cum, history.anchor, history.days[0], day, day)
    last_qty = float(history.buys[n_all - 1].qty)
    if not rate or last_qty <= 0:
        return None
    cover = last_qty / rate
    return ((day - history.days[n_all - 1]).days + horizon) / cover, cover, rate


def _still_sold(series: TillSeries | None, as_of: _AsOf) -> bool:
    """A recipe sold it in the last PAUSE_DAYS the till import covers."""
    if series is None or as_of.covered_until is None or not _covered(series, as_of.day):
        return False
    end = min(as_of.day, as_of.covered_until + DAY)
    return _consumed(series, end - timedelta(days=PAUSE_DAYS), end, as_of.day) > 0


def _deposit_like(prepared: Prepared, article_id: int, history: History, n_all: int, stop: date) -> bool:
    returned = prepared.returned.get(article_id)
    if returned is None:
        return False
    back = returned.cum[bisect.bisect_left(returned.days, stop)]
    bought = history.qty_cum[n_all]
    return back > 0 and bought > 0 and back >= bought * DEPOSIT_SHARE


def _median_interval(days: tuple[date, ...], n_all: int) -> float | None:
    if n_all < 2:
        return None
    return statistics.median((days[i + 1] - days[i]).days for i in range(n_all - 1))


def _silence(
    series: TillSeries | None, since: int, median_interval: float | None, n_all: int, as_of: _AsOf
) -> str | None:
    """« plus acheté ? » or « en pause » - a label, never a gate."""
    if n_all < FEW_PURCHASES or not median_interval:
        return None
    if since <= max(DROP_FACTOR * median_interval, DROP_MIN_DAYS):
        return None
    return PAUSED_LABEL if _still_sold(series, as_of) else DROPPED_LABEL


def _score(
    prepared: Prepared, article_id: int, store_id: int, settings: Settings, as_of: _AsOf, state: _StoreState
) -> Score | None:
    history = prepared.buys.get(article_id)
    here = prepared.buys_at.get((article_id, store_id))
    if history is None or here is None:
        return None
    n_all = bisect.bisect_left(history.days, as_of.stop)
    n_here = bisect.bisect_left(here.days, as_of.stop)
    if not n_all or not n_here:
        return None
    day = as_of.day
    habit, hits, seen, span = _habit(history, here, n_here, state, settings, as_of)
    last = history.days[n_all - 1]
    since = (day - last).days
    series = _series(prepared, settings, article_id)
    need = cover = share = rate = estimated_from = None
    clock = None
    if n_all >= FEW_PURCHASES:
        got = _till_need(series, history, n_all, as_of, state.horizon)
        if got is not None:
            need, share, rate, estimated_from = got
            clock = TILL_CLOCK
        else:
            got = _calendar_need(history, n_all, day, state.horizon)
            if got is not None:
                need, cover, rate = got
                clock = CALENDAR_CLOCK
    median_interval = _median_interval(history.days, n_all)
    status = OK
    silent = None
    if _deposit_like(prepared, article_id, history, n_all, as_of.stop):
        status = DEPOSIT_LIKE
    else:
        silent = _silence(series, since, median_interval, n_all, as_of)
    chance = chance_of(habit, need, n_all < FEW_PURCHASES)
    if (
        status == OK
        and n_all >= ELSEWHERE_LAST
        and all(store_id not in buy.stores for buy in history.buys[n_all - ELSEWHERE_LAST : n_all])
    ):
        status = BOUGHT_ELSEWHERE
    last_here = here.days[n_here - 1]
    elsewhere_since = min(history.buys[n_all - 1].stores) if last > last_here else None
    qty = _usual_qty(here, n_here)
    return Score(
        article_id=article_id,
        habit=habit,
        hits=hits,
        seen=seen,
        span=span,
        days_bought=n_all,
        days_bought_here=n_here,
        first_bought=history.days[0],
        last_bought=last,
        last_bought_here=last_here,
        since=since,
        need=need,
        clock=clock,
        cover=cover,
        till_share=share,
        till_estimated_from=estimated_from,
        rate=rate,
        median_interval=median_interval,
        chance=chance,
        status=status,
        silent=silent,
        elsewhere_since=elsewhere_since,
        qty=qty,
    )


def score_article(
    prepared: Prepared, store_id: int, article_id: int, settings: Settings, horizon_days: float | None = None
) -> Score | None:
    """One article's figures at a store today, before the nag rule and
    whatever the exclusions say; None when it was never bought there."""
    as_of = _today(prepared)
    state = _store_state(prepared, store_id, settings, as_of, horizon_days)
    return _score(prepared, article_id, store_id, settings, as_of, state)


def _replay(
    prepared: Prepared,
    store_id: int,
    settings: Settings,
    state: _StoreState,
    scores: list[Score],
    as_of: _AsOf,
) -> dict[int, int]:
    """The nag rule: {article: K} for each listed line that was also listed
    (unguarded, at the threshold or above) at each of the store's last K
    visits and bought at none of them. K is NAG_VISITS_LIFTED for a line
    the habit alone would not list, NAG_VISITS_HABIT for the others. Each
    visit is replayed on the data cut at it, with the same settings, at the
    store's usual gap as of that visit: a typed « Prochain passage dans » is
    today's page's, never a past one's."""
    threshold = settings.threshold
    recent = state.visits[-max(NAG_VISITS_LIFTED, NAG_VISITS_HABIT) :][::-1]  # the latest first
    past: list[tuple[date, _AsOf, _StoreState]] = []

    def visit(i: int) -> tuple[date, _AsOf, _StoreState]:
        while len(past) <= i:
            day = recent[len(past)]
            then = _before(prepared, day)
            past.append((day, then, _store_state(prepared, store_id, settings, then, None)))
        return past[i]

    nagged = {}
    for score in scores:
        if score.status != OK or score.chance < threshold:
            continue
        lifted = sigmoid(logit(score.habit) + MODEL_C) < threshold
        k = NAG_VISITS_LIFTED if lifted else NAG_VISITS_HABIT
        if len(recent) < k:
            continue
        here = prepared.buys_at[(score.article_id, store_id)]
        n_here = bisect.bisect_left(here.days, as_of.stop)
        bought_here = set(here.days[max(0, n_here - k) : n_here])
        run = 0
        for i in range(k):
            day, then, then_state = visit(i)
            if day in bought_here:
                break
            was = _score(prepared, score.article_id, store_id, settings, then, then_state)
            if was is None or was.status != OK or was.chance < threshold:
                break
            run += 1
        if run >= k:
            nagged[score.article_id] = k
    return nagged


# --------------------------------------------------------------------- the quantity
def _is_whole(value: Decimal) -> bool:
    return value == value.to_integral_value()


def _usual_qty(here: History, n_here: int) -> Decimal:
    """The usual quantity here, in article units: the median of the last
    QTY_LAST purchase days among the first `n_here`. A median of two can
    carry one place more than either (1,25 and 2,5 make 1,875)."""
    return statistics.median(buy.qty for buy in here.buys[max(0, n_here - QTY_LAST) : n_here])


def _usual_product(
    here: History, n_here: int, product_names: Mapping[int, str]
) -> tuple[int | None, str, Decimal | None, tuple[int, Decimal] | None]:
    """(product, its name, its usual count, packs): the product bought on
    the most of the last PRODUCT_LAST days here (ties: the most recent),
    counted as the median_high of its units over its last QTY_LAST days
    here - always an amount really bought. No count for a product bought by
    measure or with no name."""
    buys = here.buys[:n_here]
    seen: dict[int, list[int]] = {}  # product -> [days, latest index]
    for index, buy in enumerate(buys[-PRODUCT_LAST:]):
        for product in buy.products:
            held = seen.get(product.product_id)
            if held is None:
                seen[product.product_id] = [1, index]
            else:
                held[0] += 1
                held[1] = index
    if not seen:
        return None, "", None, None
    usual = max(seen, key=lambda product_id: (seen[product_id][0], seen[product_id][1], -product_id))
    name = product_names.get(usual, "")
    units: list[Decimal] = []
    colisage = ONE
    for buy in reversed(buys):
        for product in buy.products:
            if product.product_id == usual:
                if not units:
                    colisage = product.colisage
                units.append(product.units)
        if len(units) == QTY_LAST:
            break
    if not name or not units or not all(unit > 0 and _is_whole(unit) for unit in units):
        return usual, name, None, None
    count = statistics.median_high(units)
    packs = None
    if colisage > 1 and count % colisage == 0:
        packs = (int(count / colisage), colisage)
    return usual, name, count, packs


def _multiplier(state: _StoreState, horizon_typed: bool) -> int:
    """How many usual quantities a typed horizon takes. The usual purchase
    covers one usual gap, so a horizon longer than the gap takes as many as
    it holds gaps, rounded half up - never fewer than 1; nothing typed, or a
    horizon within the gap, is 1. Not measured: no ground truth exists for
    it. Not the article's rate: one also bought at another store would count
    what that store supplies as this one's, one day past the gap doubling."""
    if not horizon_typed or state.usual_gap <= 0 or state.horizon <= state.usual_gap:
        return 1
    return max(1, math.floor(state.horizon / state.usual_gap + 0.5))


@dataclass(frozen=True, slots=True)
class UsualPurchase:
    """What one purchase of an article at a store usually is, as of today:
    the figures plan_store's lines carry before a typed horizon multiplies
    them (Line.qty, product_id, product_name, product_units, packs)."""

    qty: Decimal  # article units
    product_id: int | None
    product_name: str
    product_units: Decimal | None  # None: unknown, or bought by measure
    packs: tuple[int, Decimal] | None


def usual_purchase(prepared: Prepared, store_id: int, article_id: int) -> UsualPurchase | None:
    """One article's usual purchase at one store today, by the very rules
    of the page's line (`_usual_qty`, `_usual_product`) - whatever its
    section, its chance or the exclusions. None when it was never bought
    there up to today."""
    here = prepared.buys_at.get((article_id, store_id))
    if here is None:
        return None
    n_here = bisect.bisect_left(here.days, _today(prepared).stop)
    if not n_here:
        return None
    return UsualPurchase(_usual_qty(here, n_here), *_usual_product(here, n_here, prepared.product_names))


# --------------------------------------------------------------------- the sentences
def _days(count: int) -> str:
    return "1 jour" if count == 1 else f"{count} jours"


def _every(interval: float | None) -> str:
    """A rhythm in days as the page says it: « tous les jours », « tous les
    7 jours » - rounded half up, as « Rythme d'achat » rounds it."""
    days = max(1, math.floor((interval or 0) + 0.5))
    return "tous les jours" if days <= 1 else f"tous les {days} jours"


def _ago(count: int) -> str:
    if count <= 0:
        return "aujourd'hui"
    if count == 1:
        return "hier"
    return f"il y a {count} jours"


def _store_name(prepared: Prepared, store_id: int | None) -> str | None:
    store = prepared.stores.get(store_id) if store_id is not None else None
    return store.name if store is not None else None


def _sentence(prepared: Prepared, score: Score, settings: Settings) -> str:
    """One short French sentence built from the figures the chance uses."""
    if score.days_bought < FEW_PURCHASES:
        return f"Acheté {score.days_bought} fois seulement (dernier {_ago(score.since)}) : pas encore une habitude."
    visits = "1 passage" if score.seen == 1 else f"{score.seen} passages"
    if score.span == SPAN_ADOPTION:
        text = f"Pris {score.hits} fois sur {visits} depuis que vous l'achetez"
    elif score.span == SPAN_MEMORY:
        text = f"Pris {score.hits} fois sur {visits} en {settings.memory_months} mois"
    elif score.seen == 1:
        text = "Pris à votre dernier passage"
    else:
        text = f"Pris {score.hits} fois sur vos {score.seen} derniers passages"
    if score.clock == TILL_CLOCK and score.till_share is not None:
        text += _till_words(score)
    elif score.clock == CALENDAR_CLOCK and score.median_interval:
        # The rhythm, never the cover: a cover is worked out at the rate up
        # to today, which the silence since the last purchase stretches.
        text += f" ; dernier achat {_ago(score.since)}, d'habitude {_every(score.median_interval)}"
    if score.elsewhere_since is not None:
        name = _store_name(prepared, score.elsewhere_since)
        text += f" ; racheté depuis chez {name}" if name else " ; racheté ailleurs depuis"
    return text + "."


def _till_words(score: Score) -> str:
    """What the till sold of the last purchase. The figure shown decides the
    words (99,6 % reads « tout »); a share counting days past the import is
    said as an estimate, from the first day it estimates."""
    percent = round(min(score.till_share, 1.0) * 100)  # a share of 1 or more is all of it
    when = _ago(score.since)
    if score.till_estimated_from is not None:
        when += f", estimé depuis le {score.till_estimated_from:%d/%m}"
        if percent >= 100:
            return f" ; la caisse aurait écoulé tout le dernier achat ({when})"
        return f" ; la caisse aurait vendu environ {percent} % du dernier achat ({when})"
    if percent >= 100:
        return f" ; la caisse a écoulé tout le dernier achat ({when})"
    return f" ; la caisse a vendu {percent} % du dernier achat ({when})"


def _quiet_sentence(score: Score) -> str:
    if score.silent == PAUSED_LABEL:
        return f"Plus acheté depuis {_days(score.since)}, mais la caisse en vend encore : vérifiez le stock."
    return f"Plus acheté depuis {_days(score.since)} ; d'habitude {_every(score.median_interval)} environ."


def _elsewhere_sentence(prepared: Prepared, history: History, n_all: int) -> str:
    stores = set().union(*(buy.stores for buy in history.buys[n_all - ELSEWHERE_LAST : n_all]))
    name = _store_name(prepared, next(iter(stores))) if len(stores) == 1 else None
    return f"Vos {ELSEWHERE_LAST} derniers achats étaient " + (f"chez {name}." if name else "ailleurs.")


NAGGED_SENTENCE = "Aurait été proposé à vos {} derniers passages ici, sans être pris."
DEPOSIT_SENTENCE = "Plus de la moitié est rendue : sans doute une consigne."


# --------------------------------------------------------------------- the page
def _badges(score: Score) -> tuple[str, ...]:
    badges = []
    if score.days_bought < SHORT_HISTORY:
        badges.append(SHORT_HISTORY_LABEL)
    if score.silent:
        badges.append(score.silent)
    return tuple(badges)


def _line(
    prepared: Prepared,
    store_id: int,
    score: Score,
    state: _StoreState,
    horizon_typed: bool,
    as_of: _AsOf,
    section: str,
    sentence: str,
    nagged: bool = False,
) -> Line:
    article = prepared.articles[score.article_id]
    here = prepared.buys_at[(score.article_id, store_id)]
    n_here = bisect.bisect_left(here.days, as_of.stop)
    product_id, product_name, units, packs = _usual_product(here, n_here, prepared.product_names)
    return Line(
        article_id=score.article_id,
        name=article.name,
        unit=article.unit,
        category=article.category or "",
        section=section,
        chance=score.chance,
        # The words were calibrated on live rhythms: where the page itself
        # doubts the chance, it says so rather than « presque sûr ».
        confidence=UNSURE_WORD if nagged or score.silent == DROPPED_LABEL else confidence_of(score.chance),
        sentence=sentence,
        badges=_badges(score),
        qty=score.qty,
        product_id=product_id,
        product_name=product_name,
        product_units=units,
        packs=packs,
        multiplier=_multiplier(state, horizon_typed),
        last_bought_here=score.last_bought_here,
        habit=score.habit,
        need=score.need,
        clock=score.clock,
        nagged=nagged,
    )


def till_note(prepared: Prepared, settings: Settings) -> TillNote | None:
    """How fresh the till's sales are, for both pages: None when the till
    is off or has no sales."""
    if not settings.use_till or prepared.till is None or prepared.covered_until is None:
        return None
    lag = (prepared.ref_day - prepared.covered_until).days
    return TillNote(prepared.covered_until, max(0, lag), lag > STALE_DAYS)


def _usual_store(prepared: Prepared, history: History, besides: int, article: ArticleInfo) -> int | None:
    """The offered store an article was bought at on the most days (ties:
    the most recent, then the lowest id), never one it is left out at
    (« Pas ici »)."""
    days: dict[int, list] = {}
    for buy in history.buys:
        for store_id in buy.stores:
            if store_id == besides or store_id not in prepared.stores or prepared.excluded.at(article, store_id):
                continue
            held = days.setdefault(store_id, [0, buy.day])
            held[0] += 1
            held[1] = buy.day
    if not days:
        return None
    return max(days, key=lambda store_id: (days[store_id][0], days[store_id][1], -store_id))


def _due_elsewhere(
    prepared: Prepared, store_id: int, settings: Settings, state: _StoreState, as_of: _AsOf
) -> tuple[DueElsewhere, ...]:
    """At most DUE_ELSEWHERE_SHOWN articles never bought here, bought on
    FEW_PURCHASES days or more and within a year, whose calendar need at
    this store's horizon has come, the highest need first. They are never
    proposed here. Left out: an article « plus acheté ? » (the list's own
    rule - an abandoned one's need runs highest), and one with no offered
    store it is not left out at to buy it from."""
    day = as_of.day
    year = day - timedelta(days=YEAR_DAYS)
    found = []
    for article_id, history in prepared.buys.items():
        article = prepared.articles.get(article_id)
        if article is None or prepared.excluded.everywhere(article) or (article_id, store_id) in prepared.buys_at:
            continue
        n_all = len(history.days)
        if n_all < FEW_PURCHASES or history.days[-1] < year:
            continue
        if _deposit_like(prepared, article_id, history, n_all, as_of.stop):
            continue
        got = _calendar_need(history, n_all, day, state.horizon)
        if got is None or got[0] < 1.0:
            continue
        since = (day - history.days[-1]).days
        interval = _median_interval(history.days, n_all)
        silent = _silence(_series(prepared, settings, article_id), since, interval, n_all, as_of)
        if silent == DROPPED_LABEL:
            continue
        usual = _usual_store(prepared, history, store_id, article)
        if usual is None:
            continue
        found.append((got[0], article_id, usual, since, interval, silent))
    found.sort(key=lambda row: (-row[0], row[1]))
    due = []
    for need, article_id, usual, since, interval, silent in found[:DUE_ELSEWHERE_SHOWN]:
        article = prepared.articles[article_id]
        due.append(
            DueElsewhere(
                article_id=article_id,
                name=article.name,
                unit=article.unit,
                need=need,
                store_id=usual,
                store_name=_store_name(prepared, usual) or "",
                sentence=f"Dernier achat {_ago(since)}, d'habitude {_every(interval)}.",
                badges=(silent,) if silent else (),
            )
        )
    return tuple(due)


def _top_here(prepared: Prepared, store_id: int, candidates: list[int], as_of: _AsOf) -> tuple[TopLine, ...]:
    year = as_of.day - timedelta(days=YEAR_DAYS)
    rows = []
    for article_id in candidates:
        here = prepared.buys_at[(article_id, store_id)]
        low = bisect.bisect_left(here.days, year)
        high = bisect.bisect_left(here.days, as_of.stop)
        if high <= low:
            continue
        total = sum((buy.value_ht for buy in here.buys[low:high]), ZERO)
        rows.append((total, article_id, high - low))
    rows.sort(key=lambda row: (-row[0], row[1]))
    out = []
    for total, article_id, count in rows[:TOP_HERE]:
        article = prepared.articles[article_id]
        out.append(TopLine(article_id, article.name, article.unit, count, total))
    return tuple(out)


def plan_store(prepared: Prepared, store_id: int, settings: Settings, horizon_days: float | None = None) -> StorePlan:
    """The page for one store today. `horizon_days` is the typed « Prochain
    passage dans » (the view checks it is 1 to 90); None is the store's usual
    gap. A store with no visit gives an empty plan."""
    as_of = _today(prepared)
    day = as_of.day
    state = _store_state(prepared, store_id, settings, as_of, horizon_days)
    candidates = [
        article_id
        for article_id in prepared.articles_at.get(store_id, ())
        if article_id in prepared.articles and not prepared.excluded.at(prepared.articles[article_id], store_id)
    ]
    scores = [
        score
        for score in (_score(prepared, article_id, store_id, settings, as_of, state) for article_id in candidates)
        if score is not None
    ]
    nagged = _replay(prepared, store_id, settings, state, scores, as_of)
    scores.sort(
        key=lambda score: (
            score.status != OK or score.article_id in nagged,
            -score.chance,
            -score.last_bought_here.toordinal(),
            score.article_id,
        )
    )
    threshold = settings.threshold
    typed = horizon_days is not None
    unguarded = [score for score in scores if score.status == OK]

    def make_line(score: Score, section: str, sentence: str, nag: bool = False) -> Line:
        return _line(prepared, store_id, score, state, typed, as_of, section, sentence, nag)

    to_buy = [
        make_line(score, TO_BUY, _sentence(prepared, score, settings))
        for score in unguarded
        if score.article_id not in nagged and score.chance >= threshold
    ]
    maybe_scores = [
        score
        for score in unguarded
        if score.article_id in nagged or threshold * MAYBE_RATIO <= score.chance < threshold
    ]
    maybe_scores.sort(key=lambda score: (-score.chance, score.article_id))
    maybe = [
        make_line(score, MAYBE, NAGGED_SENTENCE.format(nagged[score.article_id]), nag=True)
        if score.article_id in nagged
        else make_line(score, MAYBE, _sentence(prepared, score, settings))
        for score in maybe_scores
    ]
    listed = {line.article_id for line in to_buy} | {line.article_id for line in maybe}
    year = day - timedelta(days=YEAR_DAYS)
    new = [
        make_line(score, NEW, _sentence(prepared, score, settings))
        for score in unguarded
        if score.article_id not in listed
        and score.days_bought < FEW_PURCHASES
        and (day - score.first_bought).days <= NEW_DAYS
    ]
    quiet = [
        make_line(score, QUIET, _quiet_sentence(score))
        for score in unguarded
        if score.article_id not in listed and score.silent and score.last_bought_here >= year
    ]
    elsewhere_scores = [
        score for score in scores if score.status == BOUGHT_ELSEWHERE and score.last_bought_here >= year
    ]
    elsewhere = []
    for score in elsewhere_scores[:ELSEWHERE_SHOWN]:
        history = prepared.buys[score.article_id]
        n_all = bisect.bisect_left(history.days, as_of.stop)
        elsewhere.append(make_line(score, ELSEWHERE, _elsewhere_sentence(prepared, history, n_all)))
    deposits = [make_line(score, DEPOSIT, DEPOSIT_SENTENCE) for score in scores if score.status == DEPOSIT_LIKE]
    store = prepared.stores.get(store_id)
    return StorePlan(
        store_id=store_id,
        store_name=store.name if store is not None else "",
        today=day,
        usual_gap=state.usual_gap,
        horizon=state.horizon,
        horizon_typed=typed,
        visits=len(state.visits),
        last_visit=state.visits[-1] if state.visits else None,
        few_visits=len(state.visits) < FEW_VISITS,
        candidates=len(scores),
        expected_basket=sum(score.chance for score in unguarded),
        till_note=till_note(prepared, settings),
        to_buy=tuple(to_buy),
        maybe=tuple(maybe),
        new=tuple(new),
        quiet=tuple(quiet),
        elsewhere=tuple(elsewhere),
        elsewhere_count=len(elsewhere_scores),
        due_elsewhere=_due_elsewhere(prepared, store_id, settings, state, as_of),
        top_here=_top_here(prepared, store_id, candidates, as_of),
        deposits=tuple(deposits),
        deposit_hint_categories=tuple(sorted({line.category for line in deposits})),
    )


def store_choices(prepared: Prepared) -> list[StoreChoice]:
    """The offered stores with a visit, most visited over the last year
    first (then the latest visit, the name)."""
    year = prepared.today - timedelta(days=YEAR_DAYS)
    choices = []
    for store_id, store in prepared.stores.items():
        visits = prepared.visits.get(store_id, ())
        if not visits:
            continue
        in_year = len(visits) - bisect.bisect_left(visits, year)
        choices.append(
            StoreChoice(
                store_id=store_id,
                name=store.name,
                visits_12m=in_year,
                usual_gap=_usual_gap(visits),
                last_visit=visits[-1],
                rare=in_year < RARE_VISITS,
            )
        )
    choices.sort(
        key=lambda choice: (-choice.visits_12m, -choice.last_visit.toordinal(), choice.name.casefold(), choice.store_id)
    )
    return choices


# --------------------------------------------------------------------- « Rythme d'achat »
def _trend(history: History, day: date) -> str | None:
    """« en hausse » / « en baisse » / « stable »: what the last
    TREND_RECENT_DAYS bought per day against the rest of the year's, that
    earlier rate counted from the first purchase when it is within the year
    (the days before the article existed bought nothing of it). None with
    fewer than TREND_MIN_PURCHASES purchase days in the year, or fewer than
    TREND_MIN_SPAN_DAYS days to count the earlier rate over."""
    year = day - timedelta(days=YEAR_DAYS)
    recent_start = day - timedelta(days=TREND_RECENT_DAYS)
    since = max(year, history.days[0])
    span = (recent_start - since).days
    in_year = len(history.days) - bisect.bisect_left(history.days, year)
    if span < TREND_MIN_SPAN_DAYS or in_year < TREND_MIN_PURCHASES:
        return None
    recent = sum((buy.qty for buy in history.buys if buy.day >= recent_start), ZERO)
    before = sum((buy.qty for buy in history.buys if since <= buy.day < recent_start), ZERO)
    if before <= 0:
        return None
    ratio = (recent * span) / (before * TREND_RECENT_DAYS)
    if ratio >= TREND_UP:
        return TREND_UP_WORD
    if ratio <= TREND_DOWN:
        return TREND_DOWN_WORD
    return TREND_FLAT_WORD


def _shares(prepared: Prepared, history: History) -> tuple[StoreShare, ...]:
    days: dict[int, int] = defaultdict(int)
    for buy in history.buys:
        for store_id in buy.stores:
            days[store_id] += 1
    total = sum(days.values())
    shares = [
        StoreShare(store_id, _store_name(prepared, store_id) or OTHER_STORE, count / total)
        for store_id, count in days.items()
    ]
    shares.sort(key=lambda share: (-share.share, share.name.casefold(), share.store_id))
    return tuple(shares)


def rhythms(prepared: Prepared, settings: Settings, store_id: int | None = None) -> list[ArticleRhythm]:
    """Every article bought (only those bought at `store_id` when given,
    with their habit there): its state, rhythm, last and next purchase,
    stores, usual quantity, trend and till sales. « plus acheté ? » and
    « en pause » follow the store page's own rule, asked before « nouveau »
    as the store page asks it, so the pages agree. The next purchase is the
    last one plus the rhythm the row shows; the till's weekly sales are
    counted over the days its import covers."""
    as_of = _today(prepared)
    day = as_of.day
    year = day - timedelta(days=YEAR_DAYS)
    state = _store_state(prepared, store_id, settings, as_of, None) if store_id is not None else None
    out = []
    for article_id, history in prepared.buys.items():
        article = prepared.articles.get(article_id)
        here = prepared.buys_at.get((article_id, store_id)) if store_id is not None else None
        if article is None or (store_id is not None and here is None):
            continue
        days = history.days
        n_all = len(days)
        gaps = [(days[i + 1] - days[i]).days for i in range(n_all - 1)]
        recent = [gap for gap, later in zip(gaps, days[1:]) if later >= year]
        median_gap = float(statistics.median(recent or gaps)) if gaps else None
        cv = None
        if len(recent) >= MIN_GAPS_FOR_CV and statistics.mean(recent):
            cv = statistics.pstdev(recent) / statistics.mean(recent)
        since = (day - days[-1]).days
        series = _series(prepared, settings, article_id)
        if _deposit_like(prepared, article_id, history, n_all, as_of.stop):
            rhythm = DEPOSIT_STATE
        elif n_all == 1:
            rhythm = NEW_STATE if since <= NEW_DAYS else ONE_OFF_STATE
        elif silent := _silence(series, since, _median_interval(days, n_all), n_all, as_of):
            rhythm = silent
        elif (day - days[0]).days <= NEW_DAYS:
            rhythm = NEW_STATE
        elif cv is None:
            rhythm = OCCASIONAL_STATE
        elif cv < REGULAR_CV:
            rhythm = REGULAR_STATE
        elif cv <= FAIRLY_REGULAR_CV:
            rhythm = FAIRLY_REGULAR_STATE
        else:
            rhythm = IRREGULAR_STATE
        next_day = None
        if (
            n_all >= FEW_PURCHASES
            and median_gap
            and median_gap <= MAX_RHYTHM_DAYS
            and rhythm not in (DROPPED_LABEL, PAUSED_LABEL, DEPOSIT_STATE)
        ):
            next_day = days[-1] + timedelta(days=math.floor(median_gap + 0.5))
        till_per_week = None
        if series is not None and as_of.covered_until is not None and _covered(series, day):
            end = min(day, as_of.covered_until + DAY)  # the first day the import does not cover
            first = end - timedelta(days=TILL_WEEK_DAYS)
            if as_of.till_start is not None:
                first = max(first, as_of.till_start)
            if end > first:
                till_per_week = _consumed(series, first, end, day) / ((end - first).days / 7)
        habit_here = None
        if state is not None and here is not None:
            _habit_value, hits, seen, _span = _habit(history, here, len(here.days), state, settings, as_of)
            habit_here = (hits, seen)
        out.append(
            ArticleRhythm(
                article_id=article_id,
                name=article.name,
                unit=article.unit,
                category=article.category or "",
                state=rhythm,
                median_gap=median_gap,
                last_day=days[-1],
                next_day=next_day,
                due=next_day is not None and next_day <= day,
                stores=_shares(prepared, history),
                habit_here=habit_here,
                usual_qty=statistics.median(buy.qty for buy in history.buys[-QTY_LAST:]),
                trend=_trend(history, day),
                till_per_week=till_per_week,
                purchases_12m=n_all - bisect.bisect_left(days, year),
                excluded=prepared.excluded.everywhere(article),
            )
        )
    out.sort(key=lambda row: (row.name.casefold(), row.article_id))
    return out
