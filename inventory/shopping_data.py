"""« Prévoir les courses »: the page's one read of the database.

`prepare(today, settings, now)` reads what the page needs and hands it, as
plain rows, to `shopping.Prepared.build` - the pure module's single entry.
Everything after that (the chances, the sections, the rhythm view) is
inventory/shopping.py's, in memory. The ShoppingSetting row is the view's
to read (`ShoppingSetting.current()`, then `settings_from`).

**A constant number of queries, whatever the history** (pinned by
inventory/tests/test_shopping_data.py):

* the exclusions, the suppliers and the articles - one query each;
* ONE streamed scan of the purchase movements (`purchase_rows`);
* the names of the products those movements were bought as (every product
  an article claims, one query: the pure module wants them before planning);
* and only while « Tenir compte des ventes de la caisse » is on - off, not
  one sales table is read and the engine never runs - the till
  (`read_till`): the espace's night (`last_complete_day`), the till's
  coverage (`covered_until`), the window's daily recipe sales and
  sale-document lines, the till's first day (two), and `variance.read_sales`
  inside one `variation_scope` with every sold recipe's terms filled there,
  so that the attribution and the daily spread cost no query.

**Which purchases** (`purchase_rows`): every PURCHASE movement of an
invoice line, dated by its own `occurred_on`, else by its invoice's date -
never `created_at`, which is when the product was classified - on or before
today. A document with no date, or dated after today, is left out. Both
signs: a negative movement (a return, a deposit refunded) is the pure
module's to set apart. At every supplier: a purchase at a supplier the page
does not offer is still a purchase, and a visit.

**One article at one store** (`usual_purchase_at`, the shopping lists'): the
same scan narrowed to them, read into a Prepared of its own - the forecast
line's usual purchase, in two queries at most.

**Which stores are offered** (`offered_stores`): every supplier with a
purchase, but the suppliers of charges (`expenses_only`) and the removed AI
reading's supplier, which invoices/0038 kept where something named it (its
code, `receipts.RETIRED_CODES`): a bucket of documents nobody recognised,
no store anybody goes to.

**The till: one attribution over a year, spread per day** (the design's
« Option A »). The window W is (ref - TILL_WINDOW_DAYS, min(covered, ref)]:
ref is the last complete till day, covered how far the till's import
reaches without a gap - or, never recorded, the last day a recipe sold in
the year. `attribute_sales` runs ONCE over W: its capacity is the window's
own purchases, both signs, floored at 0 (the stock page's precedent for a
window), its costs the purchases' average cost (all positive purchases up to
today), which only orders an « OU »'s options. Then per article and day
(`daily_consumption`):

* exact(t): the recipes sold that day times their fixed pours, plus the
  article sold as itself that day - what the engine counts as certain;
* shared(t): the engine's shared figure times pot(t) / pot(W), pot(t)
  being what that day's « OU » could have poured of the article.

So the days add up to `attribute_sales`' totals over W, exact and shared,
and any stretch of days is two prefix sums in the pure module. No sale over
W, or no coverage to say where W ends: the till is off for the page.

**The till's first day** (`TillData.start`, `_till_first_day`) is the first
day a till import recorded a sale - a till button's day, or a recipe sale
not typed by hand - clamped to W. A sale typed by hand and a sale document
count as consumption and never start the till: older than the import, one
put months the till never read into k. Only a bar with no till import at
all starts from the first of its own sales.

Quantities and money stay Decimal here; the till's daily figures become
floats only as they go into `TillData`, the pure module's score boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Min, Q

from invoices.models import Supplier
from invoices.receipts import RETIRED_CODES

from . import shopping
from .models import MovementKind, Product, ShoppingExclusion, StockMovement, StockType

ZERO = Decimal("0")
DAY = timedelta(days=1)
#: The scan's rows are streamed this many at a time.
SCAN_CHUNK = 2000


def settings_from(setting) -> shopping.Settings:
    """The pure module's settings from a ShoppingSetting row (stored or the
    unsaved defaults of `ShoppingSetting.current()`)."""
    return shopping.Settings(
        threshold_percent=setting.threshold_percent,
        memory_months=setting.memory_months,
        use_till=setting.use_till,
    )


def prepare(today: date, settings, now: datetime) -> shopping.Prepared:
    """Everything the page plans from, read once (the module's docstring).

    `today` is the page's day: every purchase dated on or before it is
    history. `settings` is `shopping.Settings` (anything with `use_till`):
    off, the till is not read. `now` says which till day is the last
    complete one."""
    purchases = purchase_rows(today)
    till = read_till(purchases, now) if settings.use_till else None
    return shopping.Prepared.build(
        today=today,
        purchases=purchases,
        articles=article_infos(),
        stores=offered_stores(purchases),
        product_names=product_names(),
        till=till.data if till is not None else None,
        exclusions=exclusions(),
    )


# --------------------------------------------------------------------- the purchases
def purchase_rows(
    today: date, *, store_id: int | None = None, article_id: int | None = None
) -> list[shopping.PurchaseRow]:
    """Every PURCHASE movement of an invoice line dated on or before
    `today`, both signs, in one streamed query. The day is the movement's
    own `occurred_on`, else its invoice's date: an undated document never
    falls back to `created_at`, and one dated after today waits.

    `store_id` and `article_id` narrow the same scan to one supplier's
    invoices and one article (`usual_purchase_at`); with neither, the query
    is the page's, unchanged."""
    movements = StockMovement.objects.filter(kind=MovementKind.PURCHASE, invoice_line__isnull=False).filter(
        Q(occurred_on__isnull=False, occurred_on__lte=today)
        | Q(
            occurred_on__isnull=True,
            invoice_line__invoice__invoice_date__isnull=False,
            invoice_line__invoice__invoice_date__lte=today,
        )
    )
    if store_id is not None:
        movements = movements.filter(invoice_line__invoice__supplier_id=store_id)
    if article_id is not None:
        movements = movements.filter(stock_type_id=article_id)
    movements = movements.order_by().values_list(
        "stock_type_id",
        "invoice_line__invoice__supplier_id",
        "occurred_on",
        "invoice_line__invoice__invoice_date",
        "quantity",
        "invoice_line__product_id",
        "invoice_line__quantity",
        "invoice_line__colisage",
        "invoice_line__total_ht",
        "invoice_line__spread_ht",
    )
    return [
        shopping.PurchaseRow(
            article_id=article_id,
            store_id=store_id,
            day=occurred_on or invoice_date,
            qty=quantity,
            # What the line cost, delivery shared out included (InvoiceLine.cost_ht).
            value_ht=total_ht + spread_ht,
            product_id=product_id,
            product_units=units,
            colisage=colisage,
        )
        for (
            article_id,
            store_id,
            occurred_on,
            invoice_date,
            quantity,
            product_id,
            units,
            colisage,
            total_ht,
            spread_ht,
        ) in movements.iterator(chunk_size=SCAN_CHUNK)
    ]


def usual_purchase_at(today: date, store_id: int, article) -> shopping.UsualPurchase | None:
    """`article`'s (a StockType) usual purchase at the store `store_id` as
    of `today` - what a shopping list counts an article added without a
    quantity as. None when it was never bought there up to today.

    It is the forecast line's own figure: the same rows (the scan narrowed
    to that article at that store - the other articles and stores never
    enter an article's `buys_at`), the same product names (those of the
    products an article claims, as `product_names`) and the same rule
    (`shopping.usual_purchase`). Two queries at most: the scan, then the
    names of the products bought - none when nothing was."""
    rows = purchase_rows(today, store_id=store_id, article_id=article.pk)
    if not rows:
        return None
    bought_as = {row.product_id for row in rows if row.product_id is not None}
    names = (
        dict(
            Product.objects.filter(pk__in=bought_as, stock_type__isnull=False).order_by().values_list("id", "raw_name")
        )
        if bought_as
        else {}
    )
    prepared = shopping.Prepared.build(
        today=today,
        purchases=rows,
        articles=[shopping.ArticleInfo(article.pk, article.name, article.unit, article.category or "")],
        stores=[shopping.StoreInfo(store_id, "")],
        product_names=names,
    )
    return shopping.usual_purchase(prepared, store_id, article.pk)


def offered_stores(purchases: Iterable[shopping.PurchaseRow]) -> list[shopping.StoreInfo]:
    """The suppliers the page offers: those with a purchase, but the
    suppliers of charges and the removed AI reading's (RETIRED_CODES)."""
    bought_at = {row.store_id for row in purchases if row.qty is not None and row.qty > 0}
    return [
        shopping.StoreInfo(pk, name)
        for pk, name, expenses_only, code in Supplier.objects.order_by().values_list(
            "id", "name", "expenses_only", "code"
        )
        if pk in bought_at and not expenses_only and code not in RETIRED_CODES
    ]


def article_infos() -> list[shopping.ArticleInfo]:
    """Every article, whether bought or not: a candidate is found by its
    purchases, its name, unit and category by this."""
    return [
        shopping.ArticleInfo(pk, name, unit, category or "")
        for pk, name, unit, category in StockType.objects.order_by().values_list("id", "name", "unit", "category")
    ]


def product_names() -> dict[int, str]:
    """{product: its name as the supplier prints it}, for every product an
    article claims - every product a purchase movement can be booked from.
    One query, read whole: the ids of the scan's rows would be a list of
    bound variables as long as the catalogue."""
    return dict(Product.objects.filter(stock_type__isnull=False).order_by().values_list("id", "raw_name"))


def exclusions() -> shopping.Exclusions:
    """What the owner left out (ShoppingExclusion): articles everywhere,
    whole categories ("" = the articles with none), articles at one store."""
    everywhere: set[int] = set()
    categories: set[str] = set()
    per_store: set[tuple[int, int]] = set()
    for stock_type_id, category, supplier_id in ShoppingExclusion.objects.order_by().values_list(
        "stock_type_id", "category", "supplier_id"
    ):
        if stock_type_id is None:
            if category is not None:
                categories.add(category)
        elif supplier_id is None:
            everywhere.add(stock_type_id)
        else:
            per_store.add((stock_type_id, supplier_id))
    return shopping.Exclusions(everywhere_articles=everywhere, categories=categories, per_store=per_store)


# --------------------------------------------------------------------- the till
@dataclass(frozen=True, slots=True)
class DailyUse:
    """What the till took of an article on a day, in its unit: the part the
    engine counts as certain, and its share of the « OU » choices."""

    exact: Decimal = ZERO
    shared: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.exact + self.shared


@dataclass(frozen=True)
class TillRead:
    """The till as `read_till` read it: what the pure module gets (`data`),
    and how it was made - the window (after, until], the attribution over
    it (`base`, {article: variance.SoldQuantity}) and its daily spread."""

    data: shopping.TillData
    after: date
    until: date
    base: Mapping
    daily: Mapping[int, Mapping[date, DailyUse]]


def read_till(purchases: Iterable[shopping.PurchaseRow], now: datetime) -> TillRead | None:
    """The till's daily consumption per article over the window W (the
    module's docstring), or None when no sale is there to read.

    `purchases` are `purchase_rows`: they give the attribution its capacity
    (the window's purchases, floored at 0) and its costs, with no query."""
    from recipes import auto_sales
    from recipes.models import RecipeSale, SaleDocumentLine, variation_scope

    from .variance import attribute_sales, read_sales

    ref = auto_sales.last_complete_day(now)
    # L'Addition's coverage, carried on by the till's files that continue it.
    covered = auto_sales.till_covered_until()
    after = ref - timedelta(days=shopping.TILL_WINDOW_DAYS)
    until = ref if covered is None else min(covered, ref)
    if until <= after:
        return None
    recipe_rows = list(
        RecipeSale.objects.filter(sold_on__gt=after, sold_on__lte=until)
        .order_by()
        .values_list("recipe_id", "sold_on", "quantity")
    )
    if covered is None:
        # Never recorded: the import reaches the last day a recipe sold.
        sold_days = [day for _recipe_id, day, quantity in recipe_rows if quantity]
        if not sold_days:
            return None
        until = max(sold_days)
        recipe_rows = [row for row in recipe_rows if row[1] <= until]
    document_rows = list(
        SaleDocumentLine.objects.filter(document__sold_on__gt=after, document__sold_on__lte=until)
        .order_by()
        .values_list("recipe_id", "stock_type_id", "document__sold_on", "quantity")
    )
    if not any(row[2] for row in recipe_rows) and not any(row[3] for row in document_rows):
        return None

    # The window's sales per recipe and per day, summed as
    # recipes.sales.sales_between and stock_type_sales_between sum them.
    sold: dict[int, int | Decimal] = {}
    as_itself: dict[int, Decimal] = {}
    recipe_days: dict[int, dict[date, int | Decimal]] = {}
    itself_days: dict[int, dict[date, Decimal]] = {}
    for recipe_id, day, quantity in recipe_rows:
        sold[recipe_id] = sold.get(recipe_id, 0) + quantity
        days = recipe_days.setdefault(recipe_id, {})
        days[day] = days.get(day, 0) + quantity
    for recipe_id, stock_type_id, day, quantity in document_rows:
        if recipe_id is not None:
            sold[recipe_id] = sold.get(recipe_id, 0) + quantity
            days = recipe_days.setdefault(recipe_id, {})
            days[day] = days.get(day, 0) + quantity
        elif stock_type_id is not None:
            as_itself[stock_type_id] = as_itself.get(stock_type_id, ZERO) + quantity
            days = itself_days.setdefault(stock_type_id, {})
            days[day] = days.get(day, ZERO) + quantity

    start = max(_till_first_day(document_rows), after + DAY)

    available, unit_costs = _capacity_and_costs(purchases, after, until)
    with variation_scope():
        # The window's sales as read above: the attribution and the daily
        # spread are then of the very same rows.
        sales = replace(read_sales(after, until), sold=sold, as_itself=as_itself)
        for recipe in sales.recipes:
            if recipe.pk in recipe_days:
                sales.terms_of(recipe)
    base = attribute_sales(sales, available, unit_costs)
    daily = daily_consumption(sales, base, recipe_days, itself_days)
    series = {
        article_id: [(day, float(use.total)) for day, use in sorted(days.items()) if use.total]
        for article_id, days in daily.items()
    }
    data = shopping.TillData(
        series={article_id: points for article_id, points in series.items() if points},
        start=start,
        covered_until=until,
        ref_day=ref,
    )
    return TillRead(data=data, after=after, until=until, base=base, daily=daily)


def _till_first_day(document_rows: Iterable[tuple]) -> date:
    """The first day the till's import recorded a sale: a till button's day
    (PosProductDailyQuantity), or a recipe sale not typed by hand - every
    importer (« laddition », « csv », « api:… ») writes the till's.

    A sale typed by hand, or a sale document (« bon de vente »), exists
    because the till never saw it: either counts as consumption, never as
    the till's start. Older than the import, one put the months the till
    never read into k. Only a bar with no till import at all starts from
    the first of those: its sales recorded over all time, the documents of
    the window (`document_rows`, `read_till`'s). Two queries, as before."""
    from recipes.forms import MANUAL_SALE_SOURCE
    from recipes.models import PosProductDailyQuantity, RecipeSale

    recipe_firsts = RecipeSale.objects.exclude(quantity=0).aggregate(
        imported=Min("sold_on", filter=~Q(source=MANUAL_SALE_SOURCE)),
        first=Min("sold_on"),
    )
    imported = [
        day
        for day in (
            recipe_firsts["imported"],
            PosProductDailyQuantity.objects.exclude(quantity=0).aggregate(first=Min("sold_on"))["first"],
        )
        if day is not None
    ]
    if imported:
        return min(imported)
    # No till import at all: read_till returned None before here unless a
    # recipe sale or a document of the window sold something.
    return min(
        day
        for day in (
            recipe_firsts["first"],
            *(day for _recipe_id, _stock_type_id, day, quantity in document_rows if quantity),
        )
        if day is not None
    )


def _capacity_and_costs(
    purchases: Iterable[shopping.PurchaseRow], after: date, until: date
) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    """({article: what the window bought, both signs, floored at 0},
    {article: average cost of every positive purchase up to today})."""
    ledger: dict[int, Decimal] = {}
    bought: dict[int, Decimal] = {}
    paid: dict[int, Decimal] = {}
    for row in purchases:
        if row.day is None or not row.qty:
            continue
        if row.qty > 0:
            bought[row.article_id] = bought.get(row.article_id, ZERO) + row.qty
            paid[row.article_id] = paid.get(row.article_id, ZERO) + (row.value_ht or ZERO)
        if after < row.day <= until:
            ledger[row.article_id] = ledger.get(row.article_id, ZERO) + row.qty
    available = {article_id: max(ZERO, quantity) for article_id, quantity in ledger.items()}
    unit_costs = {article_id: paid[article_id] / quantity for article_id, quantity in bought.items()}
    return available, unit_costs


def daily_consumption(
    sales,
    base: Mapping,
    recipe_days: Mapping[int, Mapping[date, int | Decimal]],
    itself_days: Mapping[int, Mapping[date, Decimal]],
) -> dict[int, dict[date, DailyUse]]:
    """{article: {day: DailyUse}}: `base` (attribute_sales over the window
    of `sales`) spread over the days that sold (the module's docstring).

    `recipe_days` is {recipe: {day: servings}}, `itself_days` {article:
    {day: quantity sold as itself}}, both over the window `sales` was read
    for. Every sold recipe's terms must be in `sales.terms` already."""
    exact: dict[int, dict[date, Decimal]] = {}
    potential: dict[int, dict[date, Decimal]] = {}

    def add(into: dict[int, dict[date, Decimal]], article_id: int, day: date, amount: Decimal) -> None:
        days = into.setdefault(article_id, {})
        days[day] = days.get(day, ZERO) + amount

    for article_id, days in itself_days.items():
        for day, quantity in days.items():
            add(exact, article_id, day, quantity)
    recipes = {recipe.pk: recipe for recipe in sales.recipes}
    for recipe_id, days in recipe_days.items():
        recipe = recipes.get(recipe_id)
        if recipe is None:
            continue
        fixed, choices = _pours(sales.terms_of(recipe), chosen=sales.sold.get(recipe_id, 0) > 0)
        for day, servings in days.items():
            for article_id, amount in fixed.items():
                add(exact, article_id, day, amount * servings)
            for article_id, amount in choices.items():
                add(potential, article_id, day, amount * servings)

    shared: dict[int, dict[date, Decimal]] = {}
    for article_id, quantity in base.items():
        if not quantity.shared:
            continue
        days = potential.get(article_id, {})
        whole = sum(days.values(), ZERO)
        if whole <= 0:
            # Cannot happen while every pour is positive (RecipeIngredient's
            # validator): an « OU » naming it was sold. Kept on its last day
            # rather than lost, so the days still add up.
            if not days:
                continue
            days, whole = {max(days): Decimal("1")}, Decimal("1")
        shared[article_id] = {day: quantity.shared * part / whole for day, part in days.items()}

    daily: dict[int, dict[date, DailyUse]] = {}
    for article_id in set(exact) | set(shared):
        exact_days = exact.get(article_id, {})
        shared_days = shared.get(article_id, {})
        daily[article_id] = {
            day: DailyUse(exact_days.get(day, ZERO), shared_days.get(day, ZERO))
            for day in set(exact_days) | set(shared_days)
        }
    return daily


def _pours(terms, chosen: bool) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    """({article: fixed pour per serving}, {article: what the « OU » options
    naming it could pour per serving}) - the groups as `attribute_sales`
    reads them: one option is fixed, several a choice, an option pouring
    nothing is dropped (`order_options`), and a recipe whose sales over the
    window do not add up above zero has no choice allocated (`chosen`)."""
    fixed: dict[int, Decimal] = {}
    choices: dict[int, Decimal] = {}
    for options in terms:
        if not options:
            continue
        if len(options) == 1:
            for article_id, amount in options[0].items():
                fixed[article_id] = fixed.get(article_id, ZERO) + amount
            continue
        if not chosen:
            continue
        for option in options:
            if not any(amount > 0 for amount in option.values()):
                continue
            for article_id, amount in option.items():
                choices[article_id] = choices.get(article_id, ZERO) + amount
    return fixed, choices
