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
from decimal import Decimal
from datetime import date

from django.db.models import Q

from .models import MovementKind, Product, StockMovement, StockTake, StockType, UnitChoices
from .services import product_counting_ratio

ZERO = Decimal("0")


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

def stock_units_per_item(product: Product) -> Decimal:
    """How much of the stock type's own unit one physical item of this
    product is - 0.7 for a 70cl bottle of a vodka tracked in litres.

    Mirrors what compute_movement_amounts does when a purchase becomes a
    stock movement, so a bottle counted on a shelf and a bottle bought on an
    invoice are worth the same amount of stock.
    """
    if product.unit == UnitChoices.UNIT:
        return product.stock_equivalent
    ratio = product_counting_ratio(product) or Decimal("1")
    return ratio * product.stock_equivalent


def counted_quantity_in_stock_units(line) -> Decimal:
    """A stock-take line's count, converted to its stock type's own unit.

    A line is either a stock type counted directly (already in that unit) or
    a specific product, counted either in items or in the stock unit - see
    StockTakeLine.
    """
    if line.stock_type_id:
        return line.counted_quantity
    if line.unit == UnitChoices.UNIT:
        return line.counted_quantity * stock_units_per_item(line.product)
    return line.counted_quantity * line.product.stock_equivalent


def counts_by_stock_type(stock_take: StockTake) -> dict[int, Decimal]:
    """{stock_type_id: counted amount in that type's unit} for one count.

    Several lines can land on the same stock type (two brands of vodka
    counted separately), so they're summed.
    """
    counts: dict[int, Decimal] = {}
    lines = stock_take.lines.select_related("product__stock_type", "stock_type")
    for line in lines:
        stock_type_id = line.stock_type_id or line.product.stock_type_id
        if stock_type_id is None:
            continue  # a product still in the review queue: no stock type to credit
        counts[stock_type_id] = counts.get(stock_type_id, ZERO) + counted_quantity_in_stock_units(line)
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
    def is_missing(self) -> bool:
        return self.unexplained_min > 0

    @property
    def is_impossible(self) -> bool:
        """Sales account for more stock than actually left the shelf, which
        cannot happen. Not shrinkage - a data error: a miscount, a delivery
        never imported, or a recipe that doesn't match what's really poured.
        """
        return self.unexplained_max < 0

    @property
    def bottles_missing_min(self) -> Decimal | None:
        """The floor, in whole-ish bottles of the cheapest thing in the pool."""
        if self.cheapest_item_size is None or self.cheapest_item_size <= 0:
            return None
        return self.unexplained_min / self.cheapest_item_size

    @property
    def value_missing_min(self) -> Decimal | None:
        """What that floor is worth at the cheapest member's own cost - so
        the euro figure is a floor too."""
        if self.cheapest_unit_cost is None:
            return None
        return self.unexplained_min * self.cheapest_unit_cost


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


def _cheapest_member(stock_types: list[StockType]) -> tuple[StockType | None, Decimal | None, Decimal | None]:
    """The pool member with the lowest cost per stock unit, plus what one
    physical item of it holds - the basis for stating the shortfall as
    "N bottles of the cheapest", which is the most charitable reading of an
    ambiguous loss."""
    priced = [(st, st.current_unit_cost_ht) for st in stock_types]
    priced = [(st, cost) for st, cost in priced if cost > 0]
    if not priced:
        return None, None, None
    stock_type, unit_cost = min(priced, key=lambda pair: pair[1])
    return stock_type, unit_cost, typical_item_size(stock_type)


def typical_item_size(stock_type: StockType) -> Decimal | None:
    """The size of the container this stock item is usually bought in.

    "How many bottles are missing" is only a useful sentence if "bottle"
    means the one actually on the shelf. Several products can sit under one
    stock item in different formats - a vodka bought mostly in 70cl but once
    in a 3-litre box - so this picks the format bought most often, by number
    of invoice lines, rather than the largest (which would quietly divide
    the answer by four) or the smallest.
    """
    counts: dict[Decimal, int] = {}
    for product in stock_type.products.all():
        size = stock_units_per_item(product)
        if not size or size <= 0:
            continue
        counts[size] = counts.get(size, 0) + product.invoice_lines.count()
    if not counts:
        return None
    # Most-bought format wins; ties go to the larger, which is the more
    # conservative bottle count.
    return max(counts, key=lambda size: (counts[size], size))


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
    stock_types = {st.id: st for st in StockType.objects.filter(id__in=set().union(*pools) if pools else [])}

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
            variance.opening += opening_counts.get(member.id, ZERO)
            variance.closing += closing_counts.get(member.id, ZERO)
            variance.purchases += in_movements.get("purchases", ZERO)
            variance.known_losses += in_movements.get("known_losses", ZERO)
            # Since the beginning there IS no opening count to be missing
            # from - opening stock is zero by definition - so only the
            # closing count matters.
            missing_from_opening = opening_take is not None and member.id not in opening_counts
            if missing_from_opening or member.id not in closing_counts:
                variance.uncounted.append(member)
        variance.expected_usage_min = expected_min.get(pool, ZERO)
        variance.expected_usage_max = expected_max.get(pool, ZERO)
        variance.cheapest, variance.cheapest_unit_cost, variance.cheapest_item_size = _cheapest_member(members)
        report.pools.append(variance)

    return report


@dataclass
class SoldQuantity:
    """How much of one stock item was sold over a period.

    Two numbers, because "how much vodka did I sell" genuinely has two
    answers when recipes offer alternatives. `exact` is what is certainly
    this item: sold as itself, or used by a recipe that names it with no
    "OU" beside it. `shared` is the amount a choice consumed from the pool
    this item belongs to - some of it was this item, and there is no way to
    know how much.

    Reporting exact + shared as one number would be a guess; reporting only
    the exact part would understate a bar whose spirits are all alternatives
    to each other, which is most bars.
    """

    exact: Decimal = ZERO
    shared: Decimal = ZERO
    pool: frozenset = frozenset()

    @property
    def pool_partners(self) -> int:
        return max(0, len(self.pool) - 1)

    @property
    def is_ambiguous(self) -> bool:
        return self.shared > 0 and self.pool_partners > 0

    @property
    def upper_bound(self) -> Decimal:
        """The most this item could possibly have been."""
        return self.exact + self.shared


def quantities_sold(start: date | None = None, end: date | None = None) -> dict[int, SoldQuantity]:
    """{stock_type_id: SoldQuantity} over a window (all time by default).

    Built the same way the variance report builds expected usage, and for the
    same reason: a recipe's choice group consumes from a POOL, not from an
    identifiable item.
    """
    from django.utils import timezone

    from recipes.models import Recipe
    from recipes.sales import sales_between, stock_type_sales_between

    if end is None:
        end = timezone.localdate()

    sold = sales_between(start, end)
    recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type", "ingredients__sub_recipe"))
    pool_of = build_pools(recipes)

    result: dict[int, SoldQuantity] = {}

    def entry(stock_type_id: int) -> SoldQuantity:
        if stock_type_id not in result:
            result[stock_type_id] = SoldQuantity(
                pool=pool_of.get(stock_type_id, frozenset({stock_type_id}))
            )
        return result[stock_type_id]

    # Sold as itself: no recipe, no alternatives, no doubt.
    for stock_type_id, quantity in stock_type_sales_between(start, end).items():
        entry(stock_type_id).exact += quantity

    for recipe in recipes:
        count = sold.get(recipe.pk, 0)
        if not count:
            continue
        for options in recipe_usage_terms(recipe):
            if not options:
                continue
            if len(options) == 1:
                for stock_type_id, amount in options[0].items():
                    entry(stock_type_id).exact += amount * count
                continue
            # A choice: the amount is certain, which item it came from isn't.
            # Credited to every member of the pool as "shared" - deliberately
            # NOT divided between them, since the split is exactly what can't
            # be known and an equal share would look like a measurement.
            totals = [sum(option.values(), start=ZERO) for option in options]
            consumed = max(totals) * count
            for stock_type_id in {st_id for option in options for st_id in option}:
                entry(stock_type_id).shared += consumed

    return result
