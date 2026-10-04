"""« Combler les écarts »: every article's gap since a stock take, and the
recipes that could fill it.

The gap is the owner's own definition (01/10/2026): what was counted at the
take, plus what was bought since, less the losses written down since, less
what the sales since consumed - the stock page's « Vendu », from the same
engine (`variance.attribute_sales`), « OU » attributions included. There is
no closing count, so a gap also holds whatever is still on the shelf: the
owner chose it that way. His second answer: each article's loss allowance
(`loss_percent` of what it had, the « Écarts » page's rule,
`variance.loss_allowance`) is not a hole to fill, so what is left to fill -
its room - is the gap less that allowance.

The window is the stock pages' half-open one: the take's own day is in the
count, today is in the window (`StockPeriod.start` is `taken_at.date()`, and
so is this).

An article absent from the count is read at 0 there, which can only
UNDERSTATE its gap: it stays fillable up to what was bought since, and the
page flags it « non compté ».

The recipes proposed are those sold as themselves at a price above zero
(`Recipe.is_sold_directly`) and sold at least once since the take - or, when
the owner chose a duration (`GapFillSetting`, « Recettes vendues il y a
moins de … »), over that many months back from today, whatever the take: a
recipe nobody ordered in months is no longer on the menu, and its gap is
said instead, with its last sale. A refund is no sale, and does not undo
one: a recipe sold, then refunded another day, stays on the menu (until
01/10/2026 the take's window asked for net sales above zero, and the page
would have listed it « pas vendue » beside a sale inside it). The same
window gives the mix of the sales that settles a tie. One is blocked when a
single sale of it would take an article past its room, as the engine counts
it (`gap_planner.first_sales`); the page names the article and why. The gaps
themselves still run from the take: only the menu moves.

The till button named on a line is the one that rings the recipe's price:
the till's own price for each button is read off what it took over the more
recent of the take's window and the menu's (the price it charged most
often), so a plain glass linked to the same recipe as a dearer cocktail is
never named for the cocktail's price. A year of menu does not bring back the
price a button charged before the take, and a few months chosen on an older
count name it by what it rings now. A line whose best button still
rings another price says so.

The owner can leave articles out, one by one or a whole category at a time
(`GapExclusion`, kept for the espace): an article left out is ignored whole
- no gap to fill, no limit on a sale (a recipe it held back is proposed
again), out of the table, the lists and the average. « Exclus des écarts »
lists each exclusion with the way back - a category with how many of the
page's articles it covers.

Pure arithmetic is in `gap_planner`; this reads the database once and hands
it a closure over `attribute_sales`, which no longer touches the database.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Max
from django.utils import timezone

from .gap_planner import Consumption, Offer, Plan, first_sales, plan_sales
from .models import GapExclusion, GapFillSetting, StockTake, StockType
from .variance import (
    PeriodStock,
    attribute_sales,
    counts_by_stock_type,
    loss_allowance,
    movements_and_last_purchase,
    order_options,
    read_sales,
    unit_costs_ht,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

#: What « Montant à encaisser » takes at most: the plan grows with it, one
#: sale - and one engine call - at a time, and this keeps the page within a
#: couple of seconds.
MAX_AMOUNT = Decimal("10000")

# An article's state, as the page says it.
FILLABLE = "fillable"
OVER = "over"  # sold more than counted and bought
WITHIN_ALLOWANCE = "within_allowance"  # what is left is the normal loss


@dataclass
class ArticleGap:
    stock_type: StockType
    opening: Decimal = ZERO
    purchases: Decimal = ZERO
    known_losses: Decimal = ZERO
    counted: bool = False
    sold: Decimal = ZERO
    allowance: Decimal = ZERO
    unit_cost: Decimal | None = None
    # Whether a recipe that may be proposed can pour it, and what the plan adds.
    target: bool = False
    proposed: Decimal = ZERO
    # A target no proposed sale adds to yet, as the engine counts one: the
    # cheaper side of an « OU » whose dearer side still has room, or a fixed
    # pour whose sale pushes an « au choix » onto another bottle instead.
    secondary: bool = False
    # Left out by the owner, alone or with its category (GapExclusion).
    excluded: bool = False

    @property
    def gap(self) -> Decimal:
        """Counted + bought - lost - sold: the owner's figure, signed."""
        return self.opening + self.purchases - self.known_losses - self.sold

    @property
    def room(self) -> Decimal:
        """What is left to fill once the normal loss is set aside."""
        return self.gap - self.allowance

    @property
    def status(self) -> str:
        if self.gap < 0:
            return OVER
        if self.room <= 0:
            return WITHIN_ALLOWANCE
        return FILLABLE

    @property
    def room_value(self) -> Decimal | None:
        """The room at the article's average cost (HT) - what makes litres,
        kilos and bottles comparable on one list."""
        if self.room <= 0 or not self.unit_cost or self.unit_cost <= 0:
            return None
        return self.room * self.unit_cost

    @property
    def filled_percent(self) -> Decimal | None:
        if not self.target or self.room <= 0:
            return None
        return self.proposed / self.room * HUNDRED


@dataclass
class BlockedRecipe:
    recipe: object
    articles: list[ArticleGap]


@dataclass(frozen=True)
class UnsoldRecipe:
    """A priced recipe not proposed: not sold since the first day the menu
    counts. `last_sold` is its last sale ever, None if it never sold."""

    recipe: object
    last_sold: date | None


@dataclass(frozen=True)
class TillButton:
    """The till button to press for a recipe, and what it charged for one
    most often over the more recent of the take's window and the menu's
    (None: never rung there, or not read)."""

    name: str
    price: Decimal | None = None


@dataclass
class Line:
    """One recipe of the plan."""

    recipe: object
    till: TillButton
    count: int
    price: Decimal
    fills: list[ArticleGap]

    @property
    def total(self) -> Decimal:
        return self.price * self.count

    @property
    def till_differs(self) -> bool:
        """The button rings another price than the recipe's."""
        return self.till.price is not None and self.till.price != self.price


@dataclass
class GapReport:
    take: StockTake
    start: date
    end: date
    articles: dict[int, ArticleGap]
    offers: list[Offer]
    recipes: dict[int, object]
    till_buttons: dict[int, TillButton]
    blocked: list[BlockedRecipe]
    not_sold_since: list[UnsoldRecipe]
    consumption: Consumption
    # Each article's cost per unit: weighs the gaps one sale fills.
    values: dict[int, Decimal] = field(default_factory=dict)
    # Every article some recipe pours, proposed or not.
    in_recipes: set[int] = field(default_factory=set)
    # The articles left out by the owner, and the exclusions that say so.
    ignored: frozenset = frozenset()
    exclusions: list = field(default_factory=list)
    last_sale_day: date | None = None
    last_purchase_day: date | None = None
    # The recipes proposed are those sold from `menu_since` to `end`: the
    # day after the take, or `sold_within_months` back from `end`; `on_menu`
    # how many priced recipes did - those pouring nothing are no offer.
    menu_since: date | None = None
    sold_within_months: int | None = None
    on_menu: int = 0

    @property
    def sold_within_words(self) -> str:
        """« moins de 3 mois », or "" when the menu runs from the take."""
        return duration_words(self.sold_within_months) if self.sold_within_months else ""

    @property
    def rooms(self) -> dict[int, Decimal]:
        return {article_id: article.room for article_id, article in self.articles.items()}

    @property
    def rows(self) -> list[ArticleGap]:
        """The gaps a recipe of the menu touches: those to fill first, the
        biggest in value first, then the ones holding a recipe back."""
        touched = {article for offer in self.offers for article in offer.reach()}
        touched |= {article.stock_type.pk for blocked in self.blocked for article in blocked.articles}
        rows = [self.articles[article] for article in touched - self.ignored if article in self.articles]
        return sorted(
            rows,
            key=lambda row: (
                not row.target,
                -(row.room_value or ZERO),
                row.stock_type.name.lower(),
                row.stock_type.pk,
            ),
        )

    @property
    def targets(self) -> list[ArticleGap]:
        return [article for article in self.articles.values() if article.target]

    @property
    def unreached(self) -> list[ArticleGap]:
        """Gaps left to fill that a recipe pours, but no recipe proposed:
        blocked, not sold over the menu's window, or not sold as itself."""
        rows = [
            article
            for article in self.articles.values()
            if article.room > 0
            and not article.target
            and not article.excluded
            and article.stock_type.pk in self.in_recipes
        ]
        return sorted(rows, key=lambda row: (-(row.room_value or ZERO), row.stock_type.name.lower()))

    @property
    def unreached_value(self) -> Decimal:
        return sum((row.room_value or ZERO for row in self.unreached), start=ZERO)

    @property
    def outside_recipes(self) -> int:
        """Articles bought or counted that no recipe pours at all - the
        equipment, the paper towels: no sale can ever fill them."""
        return sum(
            1
            for article in self.articles.values()
            if article.room > 0
            and not article.target
            and not article.excluded
            and article.stock_type.pk not in self.in_recipes
        )

    @property
    def rows_left_out(self) -> int:
        """How many of the gaps a recipe of the menu touches are left out -
        so an empty table can say why it is empty."""
        touched = {article for offer in self.offers for article in offer.reach()}
        touched |= {article.stock_type.pk for blocked in self.blocked for article in blocked.articles}
        return len(touched & self.ignored)

    @property
    def excluded_rows(self) -> list[ArticleGap]:
        """The articles left out that moved since the take, by name."""
        rows = [article for article in self.articles.values() if article.excluded]
        return sorted(rows, key=lambda row: (row.stock_type.name.lower(), row.stock_type.pk))

    @property
    def excluded_categories(self) -> list:
        """The category exclusions, each with how many of the report's
        articles it leaves out (`.covers`)."""
        shown = []
        for exclusion in self.exclusions:
            if exclusion.stock_type_id is None:
                exclusion.covers = sum(
                    1 for article in self.articles.values() if article.stock_type.category == exclusion.category
                )
                shown.append(exclusion)
        return sorted(shown, key=lambda exclusion: (exclusion.category == "", exclusion.category.lower()))

    @property
    def excluded_articles(self) -> list:
        """The article exclusions, by name - each saying (`.covered`) whether
        its category is left out as well, so taking it back alone changes
        nothing."""
        left_out = {exclusion.category for exclusion in self.exclusions if exclusion.stock_type_id is None}
        shown = []
        for exclusion in self.exclusions:
            if exclusion.stock_type_id is not None:
                exclusion.covered = exclusion.stock_type.category in left_out
                shown.append(exclusion)
        return sorted(shown, key=lambda exclusion: (exclusion.stock_type.name.lower(), exclusion.stock_type_id))

    @property
    def categories_to_exclude(self) -> list[tuple[str, int]]:
        """(category, how many articles) of the report's articles, those not
        left out yet - the blank one last, as « Catégorie non renseignée »."""
        left_out = {exclusion.category for exclusion in self.exclusions if exclusion.stock_type_id is None}
        counts: dict[str, int] = {}
        for article in self.articles.values():
            if article.stock_type.category not in left_out:
                counts[article.stock_type.category] = counts.get(article.stock_type.category, 0) + 1
        return sorted(counts.items(), key=lambda pair: (pair[0] == "", pair[0].lower()))

    @property
    def purchases_after_sales(self) -> bool:
        """Deliveries more recent than the last sales imported."""
        if self.last_purchase_day is None:
            return False
        return self.last_sale_day is None or self.last_purchase_day > self.last_sale_day

    @property
    def sales_behind(self) -> bool:
        """The till's sales stop before yesterday, or before a delivery: every
        gap still holds sales the till has not handed over, and ringing the
        plan up before importing them would count them twice."""
        if self.last_sale_day is None:
            return True
        return self.last_sale_day < self.end - timedelta(days=1) or self.purchases_after_sales


@dataclass
class FillResult:
    plan: Plan
    lines: list[Line] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return Decimal(self.plan.total_cents) / HUNDRED

    @property
    def remainder(self) -> Decimal:
        return Decimal(self.plan.remainder_cents) / HUNDRED

    @property
    def sales(self) -> int:
        return sum(line.count for line in self.lines)

    @property
    def till_differs(self) -> int:
        """How many lines the till rings at another price than the recipe's."""
        return sum(1 for line in self.lines if line.till_differs)


def months_before(day: date, months: int) -> date:
    """`day` moved `months` calendar months back, its day of the month kept
    where that month has it: 31/03 less one month is February's last day."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def duration_words(months: int) -> str:
    """How long ago a sale may be, as the page says it: « moins d'un mois »,
    « moins de 3 mois », « moins d'un an », « moins de 2 ans »."""
    if months % 12 == 0:
        years = months // 12
        return "moins d'un an" if years == 1 else f"moins de {years} ans"
    return "moins d'un mois" if months == 1 else f"moins de {months} mois"


def _last_sale_days(end: date) -> dict[int, date]:
    """{recipe_id: the last day it sold, up to `end`} - on the till or on a
    sale document, as `sales_between` counts them; a refund is no sale."""
    from recipes.models import RecipeSale, SaleDocumentLine

    days = dict(
        RecipeSale.objects.filter(quantity__gt=0, sold_on__lte=end)
        .values_list("recipe_id")
        .annotate(day=Max("sold_on"))
        .order_by()
    )
    for recipe_id, day in (
        SaleDocumentLine.objects.filter(recipe__isnull=False, quantity__gt=0, document__sold_on__lte=end)
        .values_list("recipe_id")
        .annotate(day=Max("document__sold_on"))
        .order_by()
    ):
        if recipe_id not in days or day > days[recipe_id]:
            days[recipe_id] = day
    return days


def _till_buttons(recipes: dict[int, object], start: date, end: date) -> dict[int, TillButton]:
    """{recipe_id: the till button to press} - among the buttons linked to
    it, never its happy-hour one nor one set aside: first one whose price at
    the till over (start, end] is the recipe's, then the most rung. A button's
    price is the one it charged most often (a day's money over its units,
    weighted by units - a comped drink or a discount makes a day's average
    odd, never the most frequent). No button: the recipe's own name."""
    from recipes.models import PosProduct, PosProductDailyQuantity

    buttons = {}
    for product_id, recipe_id, name, rung in PosProduct.objects.filter(
        recipe_id__in=list(recipes), ignored=False
    ).values_list("pk", "recipe_id", "name", "total_quantity"):
        happy_hour = (recipes[recipe_id].happy_hour_name or "").strip().lower()
        if not (happy_hour and name.strip().lower() == happy_hour):
            buttons[product_id] = (recipe_id, name, rung)

    # Not setdefault(..., Counter()): that made a Counter per day read.
    charged: defaultdict[int, Counter] = defaultdict(Counter)
    for product_id, quantity, money in PosProductDailyQuantity.objects.filter(
        product_id__in=list(buttons), revenue_read=True, quantity__gt=0, sold_on__gt=start, sold_on__lte=end
    ).values_list("product_id", "quantity", "revenue_ttc"):
        charged[product_id][(money / quantity).quantize(CENT)] += quantity

    best: dict[int, tuple[tuple, TillButton]] = {}
    for product_id, (recipe_id, name, rung) in buttons.items():
        prices = charged.get(product_id)
        price = max(prices.items(), key=lambda pair: (pair[1], pair[0]))[0] if prices else None
        rank = (price == recipes[recipe_id].selling_price_ttc, rung, name)
        if recipe_id not in best or rank > best[recipe_id][0]:
            best[recipe_id] = (rank, TillButton(name, price))
    return {
        recipe_id: best[recipe_id][1] if recipe_id in best else TillButton(recipe.name)
        for recipe_id, recipe in recipes.items()
    }


def _offer_terms(terms, unit_costs) -> tuple[tuple[dict[int, Decimal], ...], ...]:
    """Usage terms as the engine fills them: a choice's options priciest
    first, nothing that pours nothing."""
    shaped = []
    for options in terms:
        if len(options) > 1:
            kept = order_options(options, unit_costs)
        else:
            kept = [{article: amount for article, amount in option.items() if amount > 0} for option in options]
            kept = [option for option in kept if option]
        if kept:
            shaped.append(tuple(kept))
    return tuple(shaped)


def gaps_since(take: StockTake, end: date | None = None) -> GapReport:
    """Every article's gap from `take` to `end` (today by default), and the
    recipes that could fill them - the articles the owner left out
    (GapExclusion) ignored, the recipes not sold over the menu's window
    (GapFillSetting) set aside."""
    from recipes.models import RecipeSale, variation_scope
    from recipes.sales import sales_between

    end = end or timezone.localdate()
    start = take.taken_at.date()
    # The first day a sale puts its recipe on the menu: the day after the
    # take, or the months chosen back from `end` - before the take or after.
    sold_within_months = GapFillSetting.current().sold_within_months
    # A take on the calendar's last day (saved before its form refused it)
    # has no day after it: the page opened on an OverflowError, every visit.
    menu_since = (
        months_before(end, sold_within_months)
        if sold_within_months
        else (start + timedelta(days=1) if start < date.max else start)
    )

    opening = counts_by_stock_type(take)
    # The latest delivery of the window comes out of the same scan.
    movements, last_purchase_day = movements_and_last_purchase(start, end)
    unit_costs = unit_costs_ht()
    stock = {
        article: PeriodStock(
            opening=opening.get(article, ZERO),
            purchases=movements.get(article, {}).get("purchases", ZERO),
            closing=ZERO,
            known_losses=movements.get(article, {}).get("known_losses", ZERO),
            counted=article in opening,
        )
        for article in set(opening) | set(movements)
    }
    available = {article: period.sellable for article, period in stock.items()}

    with variation_scope():
        sales = read_sales(start, end)
        priced = {
            recipe.pk: recipe
            for recipe in sales.recipes
            if recipe.selling_price_ttc is not None and recipe.selling_price_ttc > 0
        }
        # Every recipe: those sold and those the plan may add are what the
        # engine is asked about, and the others say which articles some
        # recipe pours at all. Read here, inside the scope, so that every
        # later attribution is arithmetic only.
        for recipe in sales.recipes:
            sales.terms_of(recipe)
    # What each recipe sold over the menu's window: the mix a tie goes to.
    menu_sold = sales.sold if sold_within_months is None else sales_between(menu_since - timedelta(days=1), end)
    last_sold = _last_sale_days(end)
    in_recipes = {
        article
        for terms in sales.terms.values()
        for options in terms
        for option in options
        for article, amount in option.items()
        if amount > 0
    }

    def consumption(extra: dict[int, int]) -> dict[int, Decimal]:
        return {
            article: sold.headline for article, sold in attribute_sales(sales, available, unit_costs, extra).items()
        }

    base = attribute_sales(sales, available, unit_costs)

    # An article no « OU » can reach takes exactly what a fixed ingredient
    # pours: the engine's allocation never looks at it.
    in_choices = {
        article
        for recipe_id, terms in sales.terms.items()
        if sales.sold.get(recipe_id) or recipe_id in priced
        for options in terms
        if len(options) > 1
        for option in options
        for article in option
    }
    offers = []
    not_sold_since = []
    on_menu = 0
    for recipe_id, recipe in priced.items():
        last = last_sold.get(recipe_id)
        if last is None or last < menu_since:
            not_sold_since.append(UnsoldRecipe(recipe, last))
            continue
        on_menu += 1
        terms = _offer_terms(sales.terms[recipe_id], unit_costs)
        if not terms:
            continue
        has_choice = any(len(options) > 1 for options in terms)
        poured = {article for options in terms for option in options for article in option}
        offers.append(
            Offer(
                recipe_id=recipe_id,
                name=recipe.name,
                price_cents=int(recipe.selling_price_ttc * 100),
                sold=menu_sold.get(recipe_id, 0),
                terms=terms,
                independent=not has_choice and not (poured & in_choices),
            )
        )

    involved = set(stock) | set(base) | {article for offer in offers for article in offer.reach()}
    articles = {}
    for stock_type in StockType.objects.filter(pk__in=involved):
        period = stock.get(stock_type.pk, PeriodStock())
        sold_quantity = base.get(stock_type.pk)
        articles[stock_type.pk] = ArticleGap(
            stock_type=stock_type,
            opening=period.opening,
            purchases=period.purchases,
            known_losses=period.known_losses,
            counted=period.counted,
            sold=sold_quantity.headline if sold_quantity else ZERO,
            allowance=loss_allowance(period.left_the_shelf, stock_type.loss_percent),
            unit_cost=unit_costs.get(stock_type.pk),
        )
    rooms = {article_id: article.room for article_id, article in articles.items()}

    exclusions = list(GapExclusion.objects.select_related("stock_type"))
    left_out_articles = {exclusion.stock_type_id for exclusion in exclusions if exclusion.stock_type_id is not None}
    left_out_categories = {exclusion.category for exclusion in exclusions if exclusion.stock_type_id is None}
    for article_id, article in articles.items():
        article.excluded = article_id in left_out_articles or article.stock_type.category in left_out_categories
    ignored = frozenset(article_id for article_id, article in articles.items() if article.excluded)

    first = first_sales(offers, rooms, consumption, ignored)
    blocked_recipes = [
        BlockedRecipe(
            recipe=priced[recipe_id],
            articles=sorted(
                (articles[article] for article in past if article in articles),
                key=lambda row: row.stock_type.name.lower(),
            ),
        )
        for recipe_id, (_effect, past) in first.items()
        if past
    ]
    proposable = [offer for offer in offers if not first[offer.recipe_id][1]]
    # What a first sale of each proposed recipe really adds, as the engine
    # books it - not what its terms say it could pour.
    reached = {article for offer in proposable for article, amount in first[offer.recipe_id][0].items() if amount > 0}
    for offer in proposable:
        for article in offer.reach():
            if article in articles and articles[article].room > 0 and article not in ignored:
                articles[article].target = True
    for article in articles.values():
        article.secondary = article.target and article.stock_type.pk not in reached

    # The last day of till sales imported, whatever the count's day: a count
    # taken yesterday has no sale in its window yet, and its sales are not
    # behind for that.
    last_sale = RecipeSale.objects.filter(sold_on__lte=end).aggregate(day=Max("sold_on"))["day"]
    return GapReport(
        take=take,
        start=start,
        end=end,
        articles=articles,
        offers=proposable,
        recipes=priced,
        # The more recent window: a year read for the menu must not let a
        # price raised since the take lose to the old one.
        till_buttons=_till_buttons(priced, max(start, menu_since - timedelta(days=1)), end),
        blocked=sorted(blocked_recipes, key=lambda row: row.recipe.name.lower()),
        not_sold_since=sorted(not_sold_since, key=lambda unsold: (unsold.recipe.name.lower(), unsold.recipe.pk)),
        consumption=consumption,
        values=unit_costs,
        in_recipes=in_recipes,
        ignored=ignored,
        exclusions=exclusions,
        last_sale_day=last_sale,
        last_purchase_day=last_purchase_day,
        menu_since=menu_since,
        sold_within_months=sold_within_months,
        on_menu=on_menu,
    )


def _show_added(report: GapReport, used: dict[int, Decimal]) -> None:
    """Write on every article what the list's sales add to it."""
    for article_id, article in report.articles.items():
        article.proposed = used.get(article_id, ZERO)


def fill_gaps(report: GapReport, amount: Decimal, already: dict[int, int] | None = None) -> FillResult:
    """The sales to ring up for `amount` (TTC, 0 < amount <= MAX_AMOUNT) on
    top of `already` (the list's earlier sales, {recipe_id: count}), and
    what the old and the new add to every article."""
    plan = plan_sales(
        int(amount * 100), report.offers, report.rooms, report.consumption, report.values, already, report.ignored
    )
    _show_added(report, plan.used)
    lines = []
    for recipe_id, count in plan.counts.items():
        recipe = report.recipes[recipe_id]
        poured = plan.poured.get(recipe_id, {})
        fills = sorted(
            (
                report.articles[article]
                for article, amount_poured in poured.items()
                if article in report.articles and report.articles[article].target and amount_poured > 0
            ),
            key=lambda row: (-(poured[row.stock_type.pk] / row.room), row.stock_type.name.lower()),
        )
        lines.append(
            Line(
                recipe=recipe,
                till=report.till_buttons.get(recipe_id, TillButton(recipe.name)),
                count=count,
                price=recipe.selling_price_ttc,
                fills=fills[:2],
            )
        )
    lines.sort(key=lambda line: (-line.total, line.recipe.name.lower()))
    return FillResult(plan=plan, lines=lines)


def show_list(report: GapReport, already: dict[int, int]) -> None:
    """Write on every article what the list's sales add, with no new amount."""
    if already:
        _show_added(
            report,
            plan_sales(0, report.offers, report.rooms, report.consumption, report.values, already, report.ignored).used,
        )


def list_counts(entries) -> dict[int, int]:
    """{recipe_id: servings} the list's entries proposed, all together - read
    through entry_rows, the one reader of what an entry stored."""
    counts: dict[int, int] = {}
    for entry in entries:
        for row in entry_rows(entry):
            if row.recipe_id is not None:
                counts[row.recipe_id] = counts.get(row.recipe_id, 0) + row.count
    return counts


def entry_lines(result: FillResult) -> list[dict]:
    """A plan's lines as an entry keeps them: what was proposed, as it was
    shown - names and prices included, so a recipe renamed or repriced later
    does not rewrite a list already rung up."""
    return [
        {
            "recipe": line.recipe.pk,
            "name": line.recipe.name,
            "till": line.till.name,
            "till_price": None if line.till.price is None else str(line.till.price),
            "count": line.count,
            "price": str(line.price),
            "fills": [article.stock_type.name for article in line.fills],
        }
        for line in result.lines
    ]


@dataclass
class EntryLine:
    """One line of a saved entry, read back for the page."""

    name: str
    till: str
    till_price: Decimal | None
    count: int
    price: Decimal
    fills: list[str]
    recipe_id: int | None = None

    @property
    def total(self) -> Decimal:
        return self.price * self.count

    @property
    def till_differs(self) -> bool:
        return self.till_price is not None and self.till_price != self.price


def _decimal(value) -> Decimal | None:
    """A stored price, or None - never NaN nor an infinity, which print as
    « NaN € » and make arithmetic raise."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    # Bounded like a price column (10^10, read_amount's), measured with
    # copy_abs - abs() itself overflows on it: a stored
    # « 1E+999999999 » is finite and overflows the moment it is multiplied.
    return number if number.is_finite() and number.copy_abs() < Decimal(10) ** 10 else None


def _whole(value) -> int | None:
    """A stored count or id: a positive int, never a bool (True is an int)."""
    return value if type(value) is int and value > 0 else None


def entry_rows(entry) -> list[EntryLine]:
    """A saved entry's lines, read back - whatever was stored, a line that
    does not read is left out rather than breaking the page. The one reader:
    list_counts and GapFillEntry.sales go through it."""
    rows = []
    for line in entry.lines if isinstance(entry.lines, list) else []:
        if not isinstance(line, dict):
            continue
        count, price = _whole(line.get("count")), _decimal(line.get("price"))
        if count is None or price is None:
            continue
        fills = line.get("fills")
        rows.append(
            EntryLine(
                name=str(line.get("name", "")),
                till=str(line.get("till", "")),
                till_price=_decimal(line.get("till_price")),
                count=count,
                price=price,
                fills=[name for name in fills if isinstance(name, str)] if isinstance(fills, list) else [],
                recipe_id=_whole(line.get("recipe")),
            )
        )
    return rows


def servings_from(day: date, end: date) -> Decimal:
    """Every serving the till (and the sale documents) sold from `day` to
    `end`, all recipes together - what moves when the day an entry was made,
    or a later one, is imported again or for the first time."""
    from recipes.models import RecipeSale, SaleDocumentLine

    total = sum(
        (
            Decimal(quantity)
            for quantity in RecipeSale.objects.filter(sold_on__gte=day, sold_on__lte=end).values_list(
                "quantity", flat=True
            )
        ),
        start=ZERO,
    )
    lines = SaleDocumentLine.objects.filter(
        recipe__isnull=False, document__sold_on__gte=day, document__sold_on__lte=end
    )
    return total + sum((quantity for quantity in lines.values_list("quantity", flat=True)), start=ZERO)


def sales_watched_from(today: date) -> date:
    """The first day whose sales may hold a list made `today`: the day
    before - the till files a sale rung after midnight under the day its
    service began. A late import of that day reads as new sales: a false
    alarm, never a double count gone silent."""
    return today - timedelta(days=1)


def list_is_stale(report: GapReport, entries) -> bool:
    """Till sales have been imported since an entry was made, on the days
    its sales could be filed under (`sales_from` on) - a new day, or one of
    those imported again with more sales: if the entry was rung up, its
    sales are in them, and the list now counts them twice. A late import of
    an OLDER day holds none of them. An entry with no `sales_seen` is judged
    on the last day of sales alone."""
    seen_from: dict[date, Decimal] = {}
    for entry in entries:
        if entry.sales_seen is not None and entry.sales_from is not None:
            if entry.sales_from not in seen_from:
                seen_from[entry.sales_from] = servings_from(entry.sales_from, report.end)
            if seen_from[entry.sales_from] != entry.sales_seen:
                return True
        elif report.last_sale_day is not None and (
            entry.sales_up_to is None or report.last_sale_day > entry.sales_up_to
        ):
            return True
    return False


def share_summary(report: GapReport) -> tuple[Decimal, Decimal, Decimal] | None:
    """(mean, lowest, highest) share of their room the plan fills, over the
    gaps a proposed sale reaches - not those no sale adds to yet
    (`ArticleGap.secondary`). None when there is no such gap."""
    shares = [
        article.filled_percent
        for article in report.targets
        if article.filled_percent is not None and not article.secondary
    ]
    if not shares:
        return None
    return sum(shares, start=ZERO) / len(shares), min(shares), max(shares)
