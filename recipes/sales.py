"""The one door sales data comes in through.

Nothing populates this yet - the till isn't connected. When it is, whatever
does the talking (a CSV upload, a REST poll, a webhook) should end up calling
`record_sales` with plain tuples and nothing else. Keeping the parsing out of
here is the point: a new source is then a new function that produces
(recipe name, date, count), and none of the accounting below has to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction

from .models import Recipe, RecipeSale


@dataclass
class SalesImportResult:
    """What an import did, in enough detail to act on.

    `unmatched` is the interesting one: a till's item names won't line up
    with recipe names by themselves, and silently dropping the ones that
    don't match would understate every variance report downstream. They're
    returned so a caller can show them for review rather than swallow them.
    """

    created: int = 0
    updated: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def recorded(self) -> int:
        return self.created + self.updated


def recipe_lookup() -> dict[str, Recipe]:
    """{till name (lowercased): recipe}.

    A recipe answers to its own name and, when set, to whatever the till
    calls its happy-hour version - the same drink out of the same bottles,
    so its sales belong to the same recipe. Recipe.clean() stops two recipes
    claiming one name; should the database already hold such a pair, the
    real `name` wins over another recipe's happy_hour_name rather than the
    outcome depending on row order.
    """
    from .models import PosProduct

    lookup: dict[str, Recipe] = {}
    for recipe in Recipe.objects.exclude(happy_hour_name=""):
        lookup[recipe.happy_hour_name.strip().lower()] = recipe
    for recipe in Recipe.objects.all():
        lookup[recipe.name.strip().lower()] = recipe
    # Last, so it wins: an explicit mapping made on the "Produits caisse"
    # screen is a deliberate decision about this exact till product, and
    # should override a name that merely happens to coincide.
    for pos_product in PosProduct.objects.exclude(recipe__isnull=True).select_related("recipe"):
        lookup[pos_product.name.strip().lower()] = pos_product.recipe
    return lookup


def record_sales(entries, source: str = "manual") -> SalesImportResult:
    """Record `(till_name, sold_on, quantity)` triples.

    Idempotent per (recipe, day, source): re-running an import corrects the
    rows IT wrote rather than doubling them, and leaves every other source
    alone - a sale typed in by hand because the till never saw it must
    survive the next import, not be quietly replaced by it.

    Quantities are summed AFTER the name is resolved to a recipe, not
    before, because several till names can be one recipe - "Alcool + Soda"
    and "Alcool + soda HH" are the same pour. Summing by raw name instead
    would write one of them and then overwrite it with the other, silently
    losing every happy-hour sale.

    Names are matched exactly (case-insensitively). No fuzzy matching: a
    mis-linked sale moves stock consumption from one drink to another, and
    the variance report is only as trustworthy as its inputs. Anything
    unmatched comes back in the result rather than being dropped.
    """
    lookup = recipe_lookup()

    totals: dict[tuple[int, date], int] = {}
    order: list[tuple[int, date]] = []
    recipes: dict[int, Recipe] = {}
    result = SalesImportResult()

    for name, sold_on, quantity in entries:
        name = str(name).strip()
        recipe = lookup.get(name.lower())
        if recipe is None:
            if name not in result.unmatched:
                result.unmatched.append(name)
            continue
        key = (recipe.pk, sold_on)
        if key not in totals:
            order.append(key)
            recipes[recipe.pk] = recipe
        totals[key] = totals.get(key, 0) + int(quantity)

    # One transaction, not one per row. Three years of sales is a few
    # thousand recipe/day totals, and taking and releasing the write lock
    # that many times is both slow and what actually broke the import: the
    # status page polls the same SQLite file every second, and one of those
    # thousands of tiny writes eventually loses the race.
    with transaction.atomic():
        _write_sales(order, totals, recipes, source, result)
    return result


def _write_sales(order, totals, recipes, source, result) -> None:
    for key in order:
        recipe_id, sold_on = key
        _sale, created = RecipeSale.objects.update_or_create(
            recipe=recipes[recipe_id],
            sold_on=sold_on,
            source=source,
            defaults={"quantity": totals[key]},
        )
        if created:
            result.created += 1
        else:
            result.updated += 1


def resync_recipe_from_daily_quantities(recipe: Recipe) -> None:
    """Rebuild this recipe's "laddition" RecipeSale rows from the till
    products already linked to it, using quantities already on file
    (PosProductDailyQuantity) rather than going back to L'Addition.

    That data has been kept locally, per day, since the moment each till
    product was first seen in ANY import - whether or not it had a recipe
    yet (see sync_pos_products) - so linking one is a pure local rebuild:
    sum what's already known, day by day, across every PosProduct that
    resolves to this recipe. No download, no wait; the numbers on both
    "Produits caisse" and "Dernières ventes" are correct the moment the
    link is saved.

    Call this whenever a PosProduct's link to a recipe changes - both
    directions: a NEWLY linked product needs its days added in, and a
    DETACHED one needs them taken back out, or the recipe's total would
    still include a till name that no longer belongs to it.
    """
    from collections import defaultdict

    from .models import PosProductDailyQuantity

    totals: dict[date, int] = defaultdict(int)
    for sold_on, quantity in PosProductDailyQuantity.objects.filter(
        product__recipe=recipe
    ).values_list("sold_on", "quantity"):
        totals[sold_on] += quantity

    with transaction.atomic():
        if totals:
            RecipeSale.objects.bulk_create(
                [
                    RecipeSale(recipe=recipe, sold_on=sold_on, source="laddition", quantity=quantity)
                    for sold_on, quantity in totals.items()
                ],
                update_conflicts=True,
                unique_fields=["recipe", "sold_on", "source"],
                update_fields=["quantity"],
            )
        # A day no PosProduct accounts for anymore (the last one linked to
        # it was just detached) must not leave a stale "laddition" row
        # behind - only days no CURRENTLY linked product covers are safe to
        # drop this way, which is exactly what's left once `totals` (this
        # recipe's full day-by-day picture, just rebuilt) is excluded.
        RecipeSale.objects.filter(recipe=recipe, source="laddition").exclude(sold_on__in=totals.keys()).delete()


def sales_between(start: date | None, end: date) -> dict[int, int]:
    """{recipe_id: units sold} over a stock-take window.

    The window is half-open at the start and closed at the end: a sale on
    the day of the opening count is NOT in the window (it happened after
    that count was taken, but the count is the window's starting point, so
    counting it would double it against the next period), while a sale on
    the day of the closing count is. `start` of None means "everything up to
    the closing count", for the very first period.
    """
    from .models import SaleDocumentLine

    totals: dict[int, int] = {}

    queryset = RecipeSale.objects.filter(sold_on__lte=end)
    if start is not None:
        queryset = queryset.filter(sold_on__gt=start)
    for recipe_id, quantity in queryset.values_list("recipe_id", "quantity"):
        totals[recipe_id] = totals.get(recipe_id, 0) + quantity

    # Hand-written sale documents count too: a tab settled off the books
    # consumed exactly as much stock as one rung up on the till.
    document_lines = SaleDocumentLine.objects.filter(
        recipe__isnull=False, document__sold_on__lte=end
    )
    if start is not None:
        document_lines = document_lines.filter(document__sold_on__gt=start)
    for recipe_id, quantity in document_lines.values_list("recipe_id", "quantity"):
        totals[recipe_id] = totals.get(recipe_id, 0) + quantity

    return totals


def stock_type_sales_between(start: date | None, end: date) -> dict[int, "Decimal"]:
    """{stock_type_id: quantity} sold directly, as itself, over the window.

    A bottle sold over the counter is not a recipe and has no ingredients to
    expand - it consumes exactly itself, in its own unit.
    """
    from .models import SaleDocumentLine

    lines = SaleDocumentLine.objects.filter(stock_type__isnull=False, document__sold_on__lte=end)
    if start is not None:
        lines = lines.filter(document__sold_on__gt=start)
    totals: dict[int, Decimal] = {}
    for stock_type_id, quantity in lines.values_list("stock_type_id", "quantity"):
        totals[stock_type_id] = totals.get(stock_type_id, Decimal("0")) + quantity
    return totals
