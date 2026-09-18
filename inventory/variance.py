"""Where did the alcohol go?

Between two stock takes, one hard physical fact is available per stock item:

    actual_usage = opening count + purchases - closing count

That is what genuinely left the shelf. Sales say what SHOULD have left it.
The gap is shrinkage - over-pouring, breakage nobody logged, unrecorded
drinks, theft:

    unexplained = actual_usage - known_losses - usage_explained_by_sales

The awkward part is that recipes are fuzzy. A Mule is "4cl of vodka OR gin
OR rum", so 200 Mules tell you 8 litres of *something* went, not which
bottle it came out of. Guessing the split would invent a precision that
isn't there.

So don't guess - POOL. Stock items that appear as alternatives to each other
are, for this purpose, interchangeable, and the ambiguity vanishes the
moment you stop trying to tell them apart. The pools build themselves by
union-find over the recipes' own choice groups; on the real data
"Alcool + Soda" (Gin/Vodka/Whisky/Rum) and "Mule" (Vodka/Gin/Rum) overlap,
so those four spirits collapse into one pool, and the mixers into another.
Within a pool substitution is invisible; between pools the accounting is
exact. Ingredients with no alternatives end up in a pool of one, and get an
exact per-item answer.

The result is then stated the most charitable way it can honestly be
stated - "at least N bottles of the CHEAPEST thing in the pool". If the
missing litres were really the good whisky, the loss is bigger; it is never
smaller. A floor is what you want to act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from datetime import date

from django.db.models import Count, Q

from .models import (
    DEFAULT_LOSS_PERCENT,
    MovementKind,
    Product,
    StockMovement,
    StockTake,
    StockType,
    UnitChoices,
    loss_fraction,
)
from .services import product_counting_ratios

ZERO = Decimal("0")
DEFAULT_LOSS_FRACTION = DEFAULT_LOSS_PERCENT / Decimal("100")


# --------------------------------------------------------------------------
# Turning recipes into stock usage
# --------------------------------------------------------------------------

# A sub-recipe can have alternatives of its own ("Vodka OU <sirop>", where
# the syrup is "sucre OU miel"), so one option can stand for many different
# stock draws. Those are enumerated to get the amounts right - but the count
# is a product and can be astronomical, so it is capped. Above the cap the
# amounts come from a sample; the POOLS never do (see reachable_stock_types),
# because getting those wrong would mix unrelated stock items together, which
# is far worse than an approximate quantity.
MAX_SUB_VARIATIONS = 64


def _sub_recipe_usage_per_yield_unit(recipe, seen: frozenset, sub_index: int = 0) -> dict[int, Decimal]:
    """{stock_type_id: amount} consumed per ONE unit of what `recipe`
    produces, following sub-recipes down, for one of its variations.

    `seen` guards against a cycle, which the ingredient form refuses but the
    admin does not; without it an A->B->A loop would recurse until the stack
    ran out.
    """
    if recipe.pk in seen:
        return {}
    seen = seen | {recipe.pk}

    variation = recipe.variation_at(sub_index) or recipe.variation_at(0)
    if not variation:
        return {}

    usage: dict[int, Decimal] = {}
    for entry in variation["breakdown"]:
        options = _ingredient_usage_options(entry["ingredient"], seen, entry.get("sub_index", 0))
        for stock_type_id, amount in (options[0] if options else {}).items():
            usage[stock_type_id] = usage.get(stock_type_id, ZERO) + amount

    yield_quantity = recipe.yield_quantity or Decimal("1")
    return {stock_type_id: amount / yield_quantity for stock_type_id, amount in usage.items()}


def _ingredient_usage_options(ingredient, seen: frozenset = frozenset(), only: int | None = None):
    """Every {stock_type_id: amount} this one ingredient line could consume.

    One entry for a stock item. For a sub-recipe, one per variation of it -
    those are alternatives just as much as a top-level "OU" is, and the
    caller pools them accordingly. `only` pins it to a single variation,
    for when the choice has already been made higher up.
    """
    if ingredient.stock_type_id:
        return [{ingredient.stock_type_id: ingredient.quantity}]
    if not ingredient.sub_recipe_id:
        return []

    sub = ingredient.sub_recipe
    if only is not None:
        indices = [only]
    else:
        indices = range(min(max(1, sub.variation_count), MAX_SUB_VARIATIONS))
    options = []
    for index in indices:
        per_unit = _sub_recipe_usage_per_yield_unit(sub, seen, index)
        options.append({st_id: amount * ingredient.quantity for st_id, amount in per_unit.items()})
    return options


def _ingredient_usage(ingredient, seen: frozenset = frozenset()) -> dict[int, Decimal]:
    """The first way this ingredient line could be realised. Kept for callers
    that genuinely want one answer rather than the alternatives."""
    options = _ingredient_usage_options(ingredient, seen, only=0)
    return options[0] if options else {}


def reachable_stock_types(ingredient, seen: frozenset = frozenset()) -> set[int]:
    """Every stock item this ingredient could ever draw on, however deeply
    its sub-recipes nest.

    Deliberately separate from the amounts above, and never capped: pools are
    built from this, and a pool that misses one of its members would report
    that member's whole consumption as unexplained.
    """
    if ingredient.stock_type_id:
        return {ingredient.stock_type_id}
    if not ingredient.sub_recipe_id or ingredient.sub_recipe_id in seen:
        return set()
    seen = seen | {ingredient.sub_recipe_id}
    found: set[int] = set()
    for nested in ingredient.sub_recipe.ingredients.select_related("stock_type", "sub_recipe"):
        found |= reachable_stock_types(nested, seen)
    return found


def recipe_usage_terms(recipe) -> list[list[dict[int, Decimal]]]:
    """One term per choice group; each term lists what each of that group's
    options would consume, per serving sold.

    A term with a single entry is a fixed ingredient: exactly that. A term
    with several is a choice, and the caller must treat them as a pool rather
    than picking one. A sub-recipe with alternatives of its own contributes
    several entries here, for the same reason.
    """
    yield_quantity = recipe.yield_quantity or Decimal("1")
    terms = []
    for group in recipe.choice_groups():
        options = []
        for ingredient in group:
            for usage in _ingredient_usage_options(ingredient, frozenset({recipe.pk})):
                options.append({st_id: amount / yield_quantity for st_id, amount in usage.items()})
        terms.append(options)
    return terms


def recipe_pool_groups(recipe) -> list[set[int]]:
    """Per choice group, every stock item reachable through ANY of its
    options - what has to end up in one pool when the group is a choice."""
    return [
        set().union(*(reachable_stock_types(ingredient) for ingredient in group)) if group else set()
        for group in recipe.choice_groups()
    ]


# --------------------------------------------------------------------------
# Pools
# --------------------------------------------------------------------------

class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def build_pools(recipes, stock_type_ids=()) -> dict[int, frozenset[int]]:
    """{stock_type_id: the pool it belongs to}.

    Two stock items land in the same pool when some recipe offers them as
    alternatives - directly, or transitively through a third item. Anything
    never offered as an alternative gets a pool of one, and so an exact
    answer.
    """
    union_find = _UnionFind()
    for stock_type_id in stock_type_ids:
        union_find.find(stock_type_id)

    for recipe in recipes:
        groups = recipe.choice_groups()
        for group, involved_set in zip(groups, recipe_pool_groups(recipe)):
            involved = sorted(involved_set)
            for st_id in involved:
                union_find.find(st_id)
            # A group is a choice when it offers more than one way to be
            # satisfied - which includes a SINGLE option that is a sub-recipe
            # with alternatives of its own. Every stock item reachable
            # through such a choice is indistinguishable from the others
            # once sold, so they all belong to one pool.
            if recipe.group_size(group) > 1:
                for st_id in involved[1:]:
                    union_find.union(involved[0], st_id)

    members: dict[int, set] = {}
    for stock_type_id in list(union_find.parent):
        members.setdefault(union_find.find(stock_type_id), set()).add(stock_type_id)
    return {
        stock_type_id: frozenset(group)
        for group in members.values()
        for stock_type_id in group
    }


# --------------------------------------------------------------------------
# Counting what's on the shelf
# --------------------------------------------------------------------------

def stock_units_per_item(product: Product, ratios: dict | None = None) -> Decimal:
    """How much of the stock type's own unit one physical item of this
    product is - 0.7 for a 70cl bottle of a vodka tracked in litres.

    Mirrors what compute_movement_amounts does when a purchase becomes a
    stock movement, so a bottle counted on a shelf and a bottle bought on an
    invoice are worth the same amount of stock.

    `ratios` is {product_id: {ratio, ...}} from services.product_counting_ratios
    for a batch of products at once. Without it this runs that query for this
    ONE product - fine for a single stock-take line, ruinous across a whole
    report (357 of them was most of the écarts page's runtime).
    """
    if product.unit == UnitChoices.UNIT:
        return product.stock_equivalent
    if ratios is None:
        ratios = product_counting_ratios([product.id])
    seen = ratios.get(product.id) or set()
    ratio = next(iter(seen)) if len(seen) == 1 else Decimal("1")
    return ratio * product.stock_equivalent


def counted_quantity_in_stock_units(line, ratios: dict | None = None) -> Decimal:
    """A stock-take line's count, converted to its stock type's own unit.

    A line is either a stock type counted directly (already in that unit) or
    a specific product, counted either in items or in the stock unit - see
    StockTakeLine. `ratios` is passed straight through to
    stock_units_per_item, so a caller with many lines pays one query rather
    than one per line.
    """
    if line.stock_type_id:
        return line.counted_quantity
    if line.unit == UnitChoices.UNIT:
        return line.counted_quantity * stock_units_per_item(line.product, ratios)
    return line.counted_quantity * line.product.stock_equivalent


def counts_by_stock_type(stock_take: StockTake) -> dict[int, Decimal]:
    """{stock_type_id: counted amount in that type's unit} for one count.

    Several lines can land on the same stock type (two brands of vodka
    counted separately), so they're summed.
    """
    counts: dict[int, Decimal] = {}
    lines = list(stock_take.lines.select_related("product__stock_type", "stock_type"))
    # One counting-ratio query for the whole count rather than one per line -
    # an inventory is hundreds of lines, and both the écarts page and the
    # stock page's period mode read two of these per request.
    ratios = product_counting_ratios([line.product_id for line in lines if line.product_id])
    for line in lines:
        stock_type_id = line.stock_type_id or line.product.stock_type_id
        if stock_type_id is None:
            continue  # a product still in the review queue: no stock type to credit
        counts[stock_type_id] = counts.get(stock_type_id, ZERO) + counted_quantity_in_stock_units(line, ratios)
    return counts


def movements_between(start: date | None, end: date) -> dict[int, dict[str, Decimal]]:
    """{stock_type_id: {"purchases": x, "known_losses": y}} in the window.

    Same half-open window as sales (see recipes.sales.sales_between): a
    delivery on the day of the opening count is already in that count.
    Losses are reported as a positive amount of stock lost, which is the
    sign a human expects to read.
    """
    movements = StockMovement.objects.select_related("invoice_line__invoice").filter(
        Q(kind=MovementKind.PURCHASE) | Q(kind=MovementKind.LOSS)
    )
    totals: dict[int, dict[str, Decimal]] = {}
    for movement in movements:
        occurred = movement.effective_date
        if occurred is None or occurred > end:
            continue
        if start is not None and occurred <= start:
            continue
        entry = totals.setdefault(movement.stock_type_id, {"purchases": ZERO, "known_losses": ZERO})
        if movement.kind == MovementKind.PURCHASE:
            entry["purchases"] += movement.quantity
        else:
            entry["known_losses"] += -movement.quantity
    return totals


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

@dataclass
class PoolVariance:
    stock_types: list[StockType]
    unit: str
    opening: Decimal = ZERO
    purchases: Decimal = ZERO
    closing: Decimal = ZERO
    known_losses: Decimal = ZERO
    expected_usage_min: Decimal = ZERO
    expected_usage_max: Decimal = ZERO
    # What each member's own StockType.loss_percent says should never have
    # reached a glass: over-pouring, the last centilitres of a bottle, a
    # keg's foam. Accumulated per member at that member's own rate, so a
    # draught beer at 15% and a syrup at 2% are not averaged into a fiction.
    loss_allowance: Decimal = ZERO
    # Whether any recipe can draw on this pool at all. An item nothing is
    # made from can only ever read as 100% missing - the till sells it (a
    # glass of prosecco, a saucisson board) but no recipe says so, and the
    # report has no way to know. That isn't shrinkage, it's a recipe that
    # hasn't been written, so the page can set those aside.
    in_recipes: bool = False
    # A stock item in this pool that wasn't counted in one of the two takes;
    # the numbers below are then not trustworthy and are flagged, not hidden.
    uncounted: list[StockType] = field(default_factory=list)
    cheapest: StockType | None = None
    cheapest_unit_cost: Decimal | None = None
    cheapest_item_size: Decimal | None = None

    @property
    def label(self) -> str:
        return " / ".join(stock_type.name for stock_type in self.stock_types)

    @property
    def is_reliable(self) -> bool:
        return not self.uncounted

    @property
    def has_activity(self) -> bool:
        """Whether anything at all happened here. A stock item nobody
        counted, bought or sold this period has nothing to say, and there
        are hundreds of them - they'd bury the handful that matter."""
        return any(
            value
            for value in (
                self.opening, self.closing, self.purchases,
                self.known_losses, self.expected_usage_max,
            )
        )

    @property
    def actual_usage(self) -> Decimal:
        """What physically left the shelf."""
        return self.opening + self.purchases - self.closing

    @property
    def unexplained_min(self) -> Decimal:
        """The least that can be missing: assume every ambiguous serving used
        the option that consumes the most."""
        return self.actual_usage - self.known_losses - self.expected_usage_max

    @property
    def unexplained_max(self) -> Decimal:
        return self.actual_usage - self.known_losses - self.expected_usage_min

    @property
    def shortfall(self) -> Decimal:
        """The gap once the expected spillage is allowed for - the number to
        act on.

        `unexplained_min` is the raw arithmetic: everything that left the
        shelf and no sale accounts for. But some of that was always going to
        be lost rather than sold, which is what StockType.loss_percent says,
        so charging all of it as shrinkage overstates the case. Never
        negative: an allowance bigger than the gap means nothing is missing,
        not that stock appeared.
        """
        return max(ZERO, self.unexplained_min - self.loss_allowance)

    @property
    def is_missing(self) -> bool:
        return self.shortfall > 0

    @property
    def is_impossible(self) -> bool:
        """Sales account for more stock than actually left the shelf, which
        cannot happen. Not shrinkage - a data error: a miscount, a delivery
        never imported, or a recipe that doesn't match what's really poured.

        Judged on the raw figures, deliberately: the loss allowance is an
        estimate, and an estimate must never be what turns a merely generous
        allowance into an accusation that the data is wrong.
        """
        return self.unexplained_max < 0

    @property
    def bottles_missing_min(self) -> Decimal | None:
        """The floor, in whole-ish bottles of the cheapest thing in the pool."""
        if self.cheapest_item_size is None or self.cheapest_item_size <= 0:
            return None
        return self.shortfall / self.cheapest_item_size

    @property
    def value_missing_min(self) -> Decimal | None:
        """What that floor is worth at the cheapest member's own cost - so
        the euro figure is a floor too."""
        if self.cheapest_unit_cost is None:
            return None
        return self.shortfall * self.cheapest_unit_cost


@dataclass
class VarianceReport:
    closing_take: StockTake
    opening_take: StockTake | None
    pools: list[PoolVariance] = field(default_factory=list)
    sales_counted: int = 0
    recipes_sold: int = 0

    @property
    def period_start(self):
        return self.opening_take.taken_at.date() if self.opening_take else None

    @property
    def since_beginning(self) -> bool:
        """True when there was no earlier count, so this measures everything
        ever bought against what is on the shelf now - which is only right if
        the invoice history goes back to the day the bar opened."""
        return self.opening_take is None

    @property
    def period_end(self):
        return self.closing_take.taken_at.date()

    @property
    def missing(self) -> list[PoolVariance]:
        """Only pools whose every member was counted in BOTH stock takes.

        Anything else is not a shrinkage figure at all. A stock item that was
        bought this period but never counted reads as "everything bought has
        vanished" - on the real database that put 360 litres of beer, worth
        more than the genuine finding, at the top of the report. An
        uncountable item belongs in `incomplete`, where it reads as the
        instruction it actually is: count this next time.
        """
        return sorted(
            (pool for pool in self.pools if pool.is_reliable and pool.is_missing),
            key=lambda pool: pool.value_missing_min or ZERO,
            reverse=True,
        )

    @property
    def impossible(self) -> list[PoolVariance]:
        return [pool for pool in self.pools if pool.is_reliable and pool.is_impossible]

    @property
    def incomplete(self) -> list[PoolVariance]:
        """Pools that saw activity but weren't fully counted, so no variance
        can honestly be computed for them."""
        return sorted(
            (pool for pool in self.pools if not pool.is_reliable and pool.has_activity),
            key=lambda pool: pool.label,
        )

    @property
    def total_value_missing_min(self) -> Decimal:
        return sum((pool.value_missing_min or ZERO for pool in self.missing), start=ZERO)

    @property
    def unlinked_count(self) -> int:
        """Reliable pools no recipe can reach - how many rows the recipe-only
        view would set aside."""
        return len([pool for pool in self.missing if not pool.in_recipes])

    def only_in_recipes(self) -> "VarianceReport":
        """The same report with the pools no recipe can reach left out.

        A new report rather than a flag read by the properties: every figure
        on the page then comes from one object, and there is no way for a
        total and the table under it to disagree about which pools they are
        talking about.
        """
        return VarianceReport(
            closing_take=self.closing_take,
            opening_take=self.opening_take,
            pools=[pool for pool in self.pools if pool.in_recipes],
            sales_counted=self.sales_counted,
            recipes_sold=self.recipes_sold,
        )


def _movement_totals(stock_type_ids=None) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    """({stock_type_id: quantity on the ledger}, {stock_type_id: average cost
    per stock unit}) - one scan of StockMovement for both.

    The quantity is the same number the stock page's "Quantité" column shows
    (StockType.current_quantity): everything bought, less the losses somebody
    recorded. Nothing deducts sales from the ledger, so it is also "how much
    of this could possibly have been poured" - which is exactly the ceiling
    quantities_sold() has to respect.

    The cost is StockType.current_unit_cost_ht, batched into one query
    instead of one per stock type: quantities_sold() compares prices across
    every stock type that shows up as a recipe alternative, and the property
    version would be an N+1 across all of them on every stock-page load.
    """
    quantity_by_type: dict[int, Decimal] = {}
    value_by_type: dict[int, Decimal] = {}
    movements = StockMovement.objects.all()
    if stock_type_ids is not None:
        movements = movements.filter(stock_type_id__in=stock_type_ids)
    for stock_type_id, quantity, unit_cost_ht in movements.values_list("stock_type_id", "quantity", "unit_cost_ht"):
        quantity_by_type[stock_type_id] = quantity_by_type.get(stock_type_id, ZERO) + quantity
        value_by_type[stock_type_id] = value_by_type.get(stock_type_id, ZERO) + quantity * unit_cost_ht
    unit_costs = {
        stock_type_id: value_by_type[stock_type_id] / quantity
        for stock_type_id, quantity in quantity_by_type.items()
        if quantity
    }
    return quantity_by_type, unit_costs


def _cheapest_member(
    stock_types: list[StockType],
    unit_costs: dict[int, Decimal] | None = None,
    item_sizes: dict[int, Decimal] | None = None,
) -> tuple[StockType | None, Decimal | None, Decimal | None]:
    """The pool member with the lowest cost per stock unit, plus what one
    physical item of it holds - the basis for stating the shortfall as
    "N bottles of the cheapest", which is the most charitable reading of an
    ambiguous loss.

    `unit_costs` and `item_sizes` are the batched forms of the two questions
    this asks, for a caller reporting on every pool at once (compute_variance
    is called with hundreds). Left out, each falls back to the one-at-a-time
    version, which is an N+1 per pool member.
    """
    if unit_costs is None:
        priced = [(st, st.current_unit_cost_ht) for st in stock_types]
    else:
        priced = [(st, unit_costs.get(st.id, ZERO)) for st in stock_types]
    priced = [(st, cost) for st, cost in priced if cost > 0]
    if not priced:
        return None, None, None
    stock_type, unit_cost = min(priced, key=lambda pair: pair[1])
    if item_sizes is None:
        return stock_type, unit_cost, typical_item_size(stock_type)
    return stock_type, unit_cost, item_sizes.get(stock_type.id)


def typical_item_sizes(stock_type_ids=None) -> dict[int, Decimal]:
    """{stock_type_id: the size of the container it is usually bought in}.

    "How many bottles are missing" is only a useful sentence if "bottle"
    means the one actually on the shelf. Several products can sit under one
    stock item in different formats - a vodka bought mostly in 70cl but once
    in a 3-litre box - so this picks the format bought most often, by number
    of invoice lines, rather than the largest (which would quietly divide
    the answer by four) or the smallest.

    Batched deliberately: the per-stock-type version ran three queries per
    stock type (its products, each product's counting ratio, each product's
    invoice-line count), which on 274 stock types was over 1,400 queries and
    the bulk of the écarts page. This is three, whatever the size.
    """
    from invoices.models import InvoiceLine

    products = Product.objects.filter(stock_type__isnull=False)
    if stock_type_ids is not None:
        products = products.filter(stock_type_id__in=list(stock_type_ids))
    products = list(products.only("id", "stock_type_id", "unit", "stock_equivalent"))

    product_ids = [product.id for product in products]
    ratios = product_counting_ratios(product_ids)
    line_counts = dict(
        InvoiceLine.objects.filter(product_id__in=product_ids)
        .values_list("product_id")
        .annotate(total=Count("id"))
    )

    counts: dict[int, dict[Decimal, int]] = {}
    for product in products:
        size = stock_units_per_item(product, ratios)
        if not size or size <= 0:
            continue
        per_type = counts.setdefault(product.stock_type_id, {})
        per_type[size] = per_type.get(size, 0) + line_counts.get(product.id, 0)
    # Most-bought format wins; ties go to the larger, which is the more
    # conservative bottle count.
    return {
        stock_type_id: max(sizes, key=lambda size: (sizes[size], size))
        for stock_type_id, sizes in counts.items()
        if sizes
    }


def typical_item_size(stock_type: StockType) -> Decimal | None:
    """typical_item_sizes for one stock item. Prefer the batched version
    anywhere more than a couple are needed."""
    return typical_item_sizes([stock_type.id]).get(stock_type.id)


def compute_variance(closing_take: StockTake, opening_take: StockTake | None = None) -> VarianceReport:
    """Where the stock went, up to `closing_take`.

    Two ways to bound the period, and the difference is only where the
    opening figure comes from:

    * **Between two counts.** The previous count is the opening stock, and
      only purchases and sales inside the window count. Needs no invoice
      history before the opening count, which is what makes it usable in a
      bar that started tracking invoices last month.

    * **Since the beginning** - used automatically when there is no earlier
      count. Nothing existed before the first invoice, so opening stock is
      zero and everything ever bought, sold and lost is in scope. One
      inventory really is enough: you know what you bought, what you sold,
      and what is on the shelf now, and those three have to reconcile.

    The catch with the second is that it assumes the invoices go back to the
    day the bar opened. If they don't, every bottle bought before the
    records start looks like it vanished. The report says which mode it used
    (see VarianceReport.since_beginning) so the page can say so out loud
    rather than leaving that assumption buried.
    """
    from recipes.models import variation_scope

    # One read of each recipe for the whole report, as on the stock page: its
    # groups are asked for once per pool, once per expected amount and once
    # per name, and each ask was a query (277 to draw the écarts page).
    with variation_scope():
        return _compute_variance(closing_take, opening_take)


def _compute_variance(closing_take: StockTake, opening_take: StockTake | None = None) -> "VarianceReport":
    from recipes.models import Recipe
    from recipes.sales import sales_between, stock_type_sales_between

    if opening_take is None:
        opening_take = (
            StockTake.objects.filter(taken_at__lt=closing_take.taken_at).order_by("-taken_at").first()
        )

    report = VarianceReport(closing_take=closing_take, opening_take=opening_take)

    period_end = closing_take.taken_at.date()
    # None means "no lower bound": everything up to the closing count. Both
    # movements_between and sales_between already treat it that way.
    period_start = opening_take.taken_at.date() if opening_take is not None else None

    opening_counts = counts_by_stock_type(opening_take) if opening_take is not None else {}
    closing_counts = counts_by_stock_type(closing_take)
    movements = movements_between(period_start, period_end)

    recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type", "ingredients__sub_recipe"))
    sold = sales_between(period_start, period_end)
    report.sales_counted = sum(sold.values())
    report.recipes_sold = len([count for count in sold.values() if count])

    involved = set(opening_counts) | set(closing_counts) | set(movements)
    pool_of = build_pools(recipes, involved)

    # Every stock item any recipe can reach, however deeply its sub-recipes
    # nest. Same walk build_pools uses and never capped, for the same reason:
    # missing one member here would wrongly set the whole pool aside.
    in_recipes: set[int] = set()
    for recipe in recipes:
        for ingredient in recipe.ingredients.all():
            in_recipes |= reachable_stock_types(ingredient)

    # Expected usage, accumulated straight onto pools.
    expected_min: dict[frozenset, Decimal] = {}
    expected_max: dict[frozenset, Decimal] = {}
    for recipe in recipes:
        count = sold.get(recipe.pk, 0)
        if not count:
            continue
        for options in recipe_usage_terms(recipe):
            if not options:
                continue
            if len(options) == 1:
                # A fixed ingredient: exactly this much of exactly these items.
                for stock_type_id, amount in options[0].items():
                    pool = pool_of.setdefault(stock_type_id, frozenset({stock_type_id}))
                    used = amount * count
                    expected_min[pool] = expected_min.get(pool, ZERO) + used
                    expected_max[pool] = expected_max.get(pool, ZERO) + used
                continue
            # A choice. Every option is in the same pool by construction (that
            # is what put them there), so only the TOTAL each option consumes
            # matters - not which member it came from, which is exactly the
            # thing that can't be known.
            totals = [sum(option.values(), start=ZERO) for option in options]
            any_member = next((st_id for option in options for st_id in option), None)
            if any_member is None:
                continue
            pool = pool_of.setdefault(any_member, frozenset({any_member}))
            expected_min[pool] = expected_min.get(pool, ZERO) + min(totals) * count
            expected_max[pool] = expected_max.get(pool, ZERO) + max(totals) * count

    # A stock item sold as itself - a bottle over the counter - consumes
    # exactly itself. No recipe, no alternatives, no ambiguity.
    for stock_type_id, quantity in stock_type_sales_between(period_start, period_end).items():
        pool = pool_of.setdefault(stock_type_id, frozenset({stock_type_id}))
        expected_min[pool] = expected_min.get(pool, ZERO) + quantity
        expected_max[pool] = expected_max.get(pool, ZERO) + quantity

    pools = set(pool_of.values()) | set(expected_min)
    involved_ids = set().union(*pools) if pools else set()
    stock_types = {st.id: st for st in StockType.objects.filter(id__in=involved_ids)}
    # Both are asked once per pool below, and there are hundreds of pools -
    # batched here instead, which is what took this page from ~2.5s to well
    # under a second (see _cheapest_member / typical_item_sizes).
    _, unit_costs = _movement_totals(involved_ids)
    item_sizes = typical_item_sizes(involved_ids)

    for pool in sorted(pools, key=lambda p: sorted(p)):
        members = [stock_types[st_id] for st_id in sorted(pool) if st_id in stock_types]
        if not members:
            continue
        units = {member.unit for member in members}
        variance = PoolVariance(
            stock_types=members,
            # A pool whose members are tracked in different units can't be
            # summed at all; say so rather than adding litres to kilos.
            unit=members[0].unit if len(units) == 1 else "",
        )
        for member in members:
            in_movements = movements.get(member.id, {})
            member_opening = opening_counts.get(member.id, ZERO)
            member_closing = closing_counts.get(member.id, ZERO)
            member_purchases = in_movements.get("purchases", ZERO)
            variance.opening += member_opening
            variance.closing += member_closing
            variance.purchases += member_purchases
            variance.known_losses += in_movements.get("known_losses", ZERO)
            # Each member's allowance at its OWN rate, on what actually left
            # the shelf. Floored at zero because a negative usage is a
            # miscount, and letting it subtract would hand the pool a
            # negative allowance - i.e. quietly inflate the shortfall.
            member_usage = max(ZERO, member_opening + member_purchases - member_closing)
            variance.loss_allowance += member_usage * loss_fraction(member.loss_percent)
            # Since the beginning there IS no opening count to be missing
            # from - opening stock is zero by definition - so only the
            # closing count matters.
            missing_from_opening = opening_take is not None and member.id not in opening_counts
            if missing_from_opening or member.id not in closing_counts:
                variance.uncounted.append(member)
        variance.expected_usage_min = expected_min.get(pool, ZERO)
        variance.expected_usage_max = expected_max.get(pool, ZERO)
        variance.in_recipes = any(member.id in in_recipes for member in members)
        variance.cheapest, variance.cheapest_unit_cost, variance.cheapest_item_size = _cheapest_member(
            members, unit_costs, item_sizes
        )
        report.pools.append(variance)

    return report


# --------------------------------------------------------------------------
# One item, between two counts
# --------------------------------------------------------------------------

@dataclass
class PeriodStock:
    """What happened to one stock item between two counts.

    The per-item counterpart of PoolVariance, and deliberately NOT a rival to
    it: PoolVariance answers "how much is missing" the only way that can be
    said without guessing, by pooling the alternatives. This says the same
    thing per item, which needs the guess quantities_sold() makes - useful to
    act on, weaker to argue from. Both are on screen, in different places.
    """

    opening: Decimal = ZERO
    purchases: Decimal = ZERO
    closing: Decimal = ZERO
    known_losses: Decimal = ZERO
    # Counted in BOTH takes (or in the closing one, when there is no earlier
    # count and opening stock is zero by definition). False means the numbers
    # below are arithmetic, not measurement - see StockPeriod.
    counted: bool = False

    @property
    def left_the_shelf(self) -> Decimal:
        """What physically went, counted rather than inferred."""
        return self.opening + self.purchases - self.closing

    @property
    def sellable(self) -> Decimal:
        """The most of this that any sale could have used.

        What left the shelf, less what is already known to have left it
        another way. A bottle you recorded as broken cannot also have been
        poured, and stock still standing there at the closing count obviously
        wasn't - which is what makes this a far tighter ceiling than "what
        was bought" over the same window.

        Floored at zero: a negative usage means a miscount or a missing
        delivery, and a negative ceiling would let an item soak up sales it
        never covered.
        """
        return max(ZERO, self.left_the_shelf - self.known_losses)

    @property
    def has_activity(self) -> bool:
        return any((self.opening, self.purchases, self.closing, self.known_losses))


@dataclass
class StockPeriod:
    """The window between two stock takes, per stock item."""

    closing_take: StockTake
    opening_take: StockTake | None
    items: dict[int, PeriodStock] = field(default_factory=dict)

    @property
    def start(self) -> date | None:
        return self.opening_take.taken_at.date() if self.opening_take is not None else None

    @property
    def end(self) -> date:
        return self.closing_take.taken_at.date()

    @property
    def since_beginning(self) -> bool:
        return self.opening_take is None

    def ceilings(self) -> dict[int, Decimal]:
        """{stock_type_id: the most that could have been sold} - what to hand
        quantities_sold() as `available` for this window."""
        return {stock_type_id: item.sellable for stock_type_id, item in self.items.items()}


def stock_between(closing_take: StockTake, opening_take: StockTake | None = None) -> StockPeriod:
    """Everything that moved, per stock item, up to `closing_take`.

    The same two modes as compute_variance, picked the same way: between two
    counts when there is an earlier one, otherwise since the beginning with
    opening stock zero. Built from the same counts_by_stock_type() and
    movements_between() so the stock page and the écarts page can never
    disagree about what a period contains.
    """
    if opening_take is None:
        opening_take = (
            StockTake.objects.filter(taken_at__lt=closing_take.taken_at).order_by("-taken_at").first()
        )
    period = StockPeriod(closing_take=closing_take, opening_take=opening_take)

    opening_counts = counts_by_stock_type(opening_take) if opening_take is not None else {}
    closing_counts = counts_by_stock_type(closing_take)
    movements = movements_between(period.start, period.end)

    for stock_type_id in set(opening_counts) | set(closing_counts) | set(movements):
        in_movements = movements.get(stock_type_id, {})
        period.items[stock_type_id] = PeriodStock(
            opening=opening_counts.get(stock_type_id, ZERO),
            purchases=in_movements.get("purchases", ZERO),
            closing=closing_counts.get(stock_type_id, ZERO),
            known_losses=in_movements.get("known_losses", ZERO),
            # Since the beginning there IS no opening count to be missing
            # from, so only the closing one matters - same rule as
            # compute_variance, and for the same reason: an item nobody
            # counted has everything it ever bought looking evaporated.
            counted=(
                stock_type_id in closing_counts
                and (opening_take is None or stock_type_id in opening_counts)
            ),
        )
    return period


@dataclass
class SoldQuantity:
    """How much of one stock item was sold over a period.

    Two numbers, because "how much vodka did I sell" genuinely has two
    answers when recipes offer alternatives. `exact` is what is certainly
    this item: sold as itself, or used by a recipe that names it with no
    "OU" beside it. `shared` is what an ambiguous choice most plausibly took
    from it (see allocate_choices) - a defensible attribution, never a
    certainty.

    `available` is the ceiling this item's sales are measured against - what
    the ledger says was bought when looking at all time, or what actually
    left the shelf between two counts (PeriodStock.sellable). `headline`
    above it is stock that was poured and never accounted for, which is the
    whole point of the column.
    """

    exact: Decimal = ZERO
    shared: Decimal = ZERO
    pool: frozenset = frozenset()
    available: Decimal = ZERO

    @property
    def pool_partners(self) -> int:
        return max(0, len(self.pool) - 1)

    @property
    def is_ambiguous(self) -> bool:
        return self.shared > 0 and self.pool_partners > 0

    @property
    def headline(self) -> Decimal:
        """Everything attributed to this item - the number the column leads
        with.

        Certain consumption and attributed consumption ARE added together
        now, which they deliberately weren't before: the attribution respects
        what was actually bought (allocate_choices), so it is a quantity this
        bottle could really have poured rather than a whole pool's total
        parked on whichever member happened to be priciest. Showing `exact`
        alone would read as "zero sold" for a spirit that is never anything
        BUT an alternative, which is most of a bar's spirits."""
        return self.exact + self.shared

    @property
    def headline_is_estimate(self) -> bool:
        """Whether `headline` is entirely attributed rather than certain -
        every drop of it came from a choice that could have used something
        else."""
        return not self.exact and self.shared > 0

    @property
    def missing(self) -> Decimal:
        """How much more was poured than the ceiling allows for - stock that
        was sold and never accounted for."""
        return max(ZERO, self.headline - self.available)

    @property
    def unexplained(self) -> Decimal:
        """The other direction: stock that left and no sale explains.

        All-time this is rarely interesting (nothing deducts sales from the
        ledger, so it's just "what's still on the shelf"). Between two counts
        it is exactly the shrinkage figure - what went, minus the losses
        somebody wrote down, minus what the till accounts for.
        """
        return max(ZERO, self.available - self.headline)

    @property
    def is_over(self) -> bool:
        """Sales account for more of this item than could possibly have left
        the shelf. Not shrinkage - shrinkage is stock that left WITHOUT being
        sold - but a data error: a missing invoice, a recipe that names the
        wrong bottle, or a till product linked to the wrong recipe."""
        return self.headline > self.available


@dataclass
class _Demand:
    """`count` servings, each of which used exactly ONE of `options`.

    An option is {stock_type_id: amount per serving} - several items at once
    when it is a sub-recipe that pours more than one thing (see "OU nests" in
    CLAUDE.md). `options` is kept priciest-first, which is the order they get
    filled in.
    """

    count: int
    options: list[dict[int, Decimal]]


def _option_value(option: dict[int, Decimal], unit_costs: dict[int, Decimal]) -> Decimal:
    return sum((unit_costs.get(st_id, ZERO) * amount for st_id, amount in option.items()), start=ZERO)


def order_options(options, unit_costs: dict[int, Decimal]) -> list[dict[int, Decimal]]:
    """One choice's options, priciest first.

    Priciest by what the option AS A WHOLE costs per serving - its own
    quantities at its own items' prices - not by the dearest item inside it,
    and not by the largest pour among the alternatives: a "double" of the
    well brand next to a single of the good stuff is two options of one
    choice with different quantities, and mixing the identity of one with the
    amount of the other overstates the premium spirit by the cheap one's
    bigger pour.

    Ties go to the option whose lowest stock item id is smallest, so the same
    alternative wins from one page load to the next rather than flickering.

    An option that consumes nothing is dropped: it can absorb any number of
    servings without ever running out, so leaving it in would let a
    half-built recipe silently swallow a whole pool's consumption.
    """
    real = [option for option in options if any(amount > 0 for amount in option.values())]
    return sorted(real, key=lambda option: (_option_value(option, unit_costs), -min(option)), reverse=True)


def _servings_that_fit(option, used, hard_cap, allowance_cap=None) -> int:
    """How many whole servings of `option` the shelf can still cover.

    Whole, because half a Mule was never poured. Against `allowance_cap` the
    count ROUNDS UP: a serving that starts below the allowance is finished
    out of the same bottle, since stopping short would leave a quantity no
    sale could ever have produced - 22 half-litres out of a 6-litre keg with
    a 10% allowance is 11 pints (5.5L), not 10 and a stranded half. It never
    rounds past `hard_cap`, which is a physical fact rather than an
    allowance.
    """
    limit = None
    for stock_type_id, amount in option.items():
        if amount <= 0:
            continue
        room = hard_cap.get(stock_type_id, ZERO) - used.get(stock_type_id, ZERO)
        if room <= 0:
            return 0
        fits = int((room / amount).to_integral_value(rounding=ROUND_FLOOR))
        if allowance_cap is not None:
            allowance = allowance_cap.get(stock_type_id, ZERO) - used.get(stock_type_id, ZERO)
            fits = min(
                fits,
                0 if allowance <= 0 else int((allowance / amount).to_integral_value(rounding=ROUND_CEILING)),
            )
        if fits <= 0:
            return 0
        limit = fits if limit is None else min(limit, fits)
    return limit or 0


def allocate_choices(
    demands,
    available: dict[int, Decimal],
    loss_fractions: dict[int, Decimal],
    unit_costs: dict[int, Decimal],
    already_used: dict[int, Decimal] | None = None,
) -> dict[int, Decimal]:
    """{stock_type_id: amount} that ambiguous sales most plausibly took.

    `demands` is (servings sold, options) per choice; `available` is what the
    ledger says was bought. Nobody can know which bottle a "vodka OU gin"
    went into, but the shelf rules a great many splits out, and what's left is
    a far better answer than "assume the priciest one every time":

    1. **Priciest first, but only while there's stock to justify it.** An
       option is filled until it reaches what was bought less that item's own
       loss allowance (StockType.loss_percent, 10% by default) - the part of
       a purchase that goes down the drain rather than into a glass. Past
       that, the next-priciest option takes over.
    2. **Then round two, up to 100% of what was bought.** Once every
       alternative has taken its allowance-adjusted share and servings are
       still unaccounted for, the allowance is released and they're filled
       again, priciest first. A bar that poured every last drop is unusual,
       not impossible.
    3. **Anything still left is charged to the priciest option**, which
       pushes it over what was bought and lights the row up red. That is the
       honest reading: more was poured than anybody ever delivered, so an
       invoice is missing or a recipe is wrong.

    Certain consumption (`already_used` - fixed ingredients, bottles sold as
    themselves) is counted against the shelf before any of this, because that
    stock is genuinely gone and can't also cover a choice.

    Demands are served in the order given, and an early one can take capacity
    a later one wanted. That's inherent to filling greedily rather than
    solving for a global optimum; the order is stable (recipe, then group)
    so the answer doesn't move between page loads.
    """
    used = dict(already_used or {})
    allocated: dict[int, Decimal] = {}
    pending = [
        demand
        for demand in (_Demand(count, order_options(options, unit_costs)) for count, options in demands)
        if demand.count > 0 and demand.options
    ]
    if not pending:
        return allocated

    hard_cap = dict(available)
    allowance_cap = {
        stock_type_id: quantity * (Decimal("1") - loss_fractions.get(stock_type_id, DEFAULT_LOSS_FRACTION))
        for stock_type_id, quantity in available.items()
    }

    def take(option, servings):
        for stock_type_id, amount in option.items():
            used[stock_type_id] = used.get(stock_type_id, ZERO) + amount * servings
            allocated[stock_type_id] = allocated.get(stock_type_id, ZERO) + amount * servings

    # Round one leaves every item its loss allowance; round two spends it.
    # Both are global rather than per-demand: the allowance is only released
    # once EVERY alternative has had its turn, which is what stops one busy
    # cocktail draining a bottle to the last drop while another still has
    # untouched alternatives standing next to it.
    for cap in (allowance_cap, None):
        for demand in pending:
            for option in demand.options:
                if not demand.count:
                    break
                servings = min(demand.count, _servings_that_fit(option, used, hard_cap, cap))
                if servings:
                    take(option, servings)
                    demand.count -= servings

    for demand in pending:
        if demand.count:
            take(demand.options[0], demand.count)
            demand.count = 0

    return allocated


def quantities_sold(
    start: date | None = None,
    end: date | None = None,
    unit_costs: dict[int, Decimal] | None = None,
    available: dict[int, Decimal] | None = None,
) -> dict[int, SoldQuantity]:
    """{stock_type_id: SoldQuantity} over a window (all time by default).

    Built the same way the variance report builds expected usage - a recipe's
    choice group consumes from a POOL, not from an identifiable item - but
    then it goes one step further and says WHICH member, as far as the shelf
    allows: see allocate_choices for how, and why that is more than guessing.

    `unit_costs` and `available` let a caller that already computed
    {stock_type_id: average cost} and {stock_type_id: quantity} (StockListView
    does, for its own columns) pass them in rather than have this run a second
    identical scan of StockMovement.

    Note the asymmetry: `start`/`end` window the SALES, but the quantities
    fetched here are the whole ledger, because that is what the stock page
    shows beside them and the only caller asks for all time anyway. A caller
    that genuinely wants a window on both has to pass `available` itself -
    all-time purchases against one month's sales would say nothing is ever
    missing.
    """
    from django.utils import timezone

    from recipes.models import Recipe, variation_scope
    from recipes.sales import sales_between, stock_type_sales_between

    # Every recipe is asked for its choice groups three times over (its usage
    # terms, its pools, its allocation), and each ask was a query of its own
    # with another for its stock items' movements: 290 queries to draw the
    # Produits page, 132 of them the same ingredients again. The scope reads
    # each recipe once and keeps it for this computation only - a grouping
    # changes every time someone presses "OU", so nothing is cached longer.
    with variation_scope():
        return _quantities_sold(start, end, unit_costs, available)


def _quantities_sold(start, end, unit_costs, available) -> dict[int, "SoldQuantity"]:
    from django.utils import timezone

    from recipes.models import Recipe
    from recipes.sales import sales_between, stock_type_sales_between

    if end is None:
        end = timezone.localdate()
    if unit_costs is None or available is None:
        ledger_quantities, ledger_costs = _movement_totals()
        unit_costs = ledger_costs if unit_costs is None else unit_costs
        available = ledger_quantities if available is None else available

    sold = sales_between(start, end)
    recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type", "ingredients__sub_recipe"))
    pool_of = build_pools(recipes)

    result: dict[int, SoldQuantity] = {}

    def entry(stock_type_id: int) -> SoldQuantity:
        if stock_type_id not in result:
            result[stock_type_id] = SoldQuantity(
                pool=pool_of.get(stock_type_id, frozenset({stock_type_id})),
                available=available.get(stock_type_id, ZERO),
            )
        return result[stock_type_id]

    # Certain consumption, kept separately as well as on the entry: it is
    # already off the shelf, so it has to be charged against capacity before
    # any ambiguous sale gets to claim the same bottle.
    certain: dict[int, Decimal] = {}

    def consume(stock_type_id: int, amount: Decimal) -> None:
        entry(stock_type_id).exact += amount
        certain[stock_type_id] = certain.get(stock_type_id, ZERO) + amount

    # Sold as itself: no recipe, no alternatives, no doubt.
    for stock_type_id, quantity in stock_type_sales_between(start, end).items():
        consume(stock_type_id, quantity)

    demands: list[tuple[int, list[dict[int, Decimal]]]] = []
    for recipe in recipes:
        count = sold.get(recipe.pk, 0)
        if not count:
            continue
        for options in recipe_usage_terms(recipe):
            if not options:
                continue
            if len(options) == 1:
                # A fixed ingredient. Never clamped: if the recipe says every
                # Caipirinha takes 50g of lime, then 100 of them took 5kg,
                # whether or not 5kg was ever bought - and saying so is
                # exactly what makes the shortfall visible.
                for stock_type_id, amount in options[0].items():
                    consume(stock_type_id, amount * count)
                continue
            demands.append((count, options))
            # Every alternative gets a dict entry, even the ones that end up
            # covering nothing - "never chosen" is not the same as "never
            # offered", and both need their `.pool` set for the tooltip.
            for stock_type_id in {st_id for option in options for st_id in option}:
                entry(stock_type_id)

    loss_fractions = {
        stock_type_id: loss_fraction(percent)
        for stock_type_id, percent in StockType.objects.values_list("id", "loss_percent")
    }
    for stock_type_id, amount in allocate_choices(
        demands, available, loss_fractions, unit_costs, already_used=certain
    ).items():
        entry(stock_type_id).shared += amount

    return result
