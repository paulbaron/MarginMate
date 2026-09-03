from datetime import date
from decimal import Decimal

from django.db.models import F, Q

from .models import Product, StockMovement, StockType, UnitChoices


def product_base_amount(invoice_line) -> Decimal:
    """How much of the product was bought on this line, in the product's own
    unit: a plain count for UNIT (bottles, packs, ...), or the measured
    total_volume for L/KG (a variable weight/volume, e.g. a cut of meat) -
    falling back to invoice_line.quantity (colisage * quantity bought) when
    the invoice doesn't print a separate weight/volume column at all. That
    quantity is often already the amount in litres/kg (e.g. a line billed
    "10 x 1.143 EUR" for a product with no listed volume is 10 litres for
    that price, not "1 something" needing a further per-item conversion).
    """
    product: Product = invoice_line.product
    if product.unit == UnitChoices.UNIT:
        return Decimal(invoice_line.quantity)
    return invoice_line.total_volume or Decimal(invoice_line.quantity)


def product_counting_ratios(product_ids) -> dict:
    """{product_id: {ratio, ratio, ...}} - the distinct total_volume/quantity
    ratios seen across each product's own invoice lines (lines with no
    measured total_volume are skipped, not counted as a 0 ratio).

    A product that's always sold in the same fixed size (a 70cl vodka
    bottle) has invoices where total_volume is simply quantity * that
    fixed size - an exact, computed figure - so this ratio is always the
    exact same number no matter how many bottles were on the line. A
    genuinely variable-weight/volume item (a cut of meat, a wheel of
    cheese) instead has a real MEASURED total_volume that's never quite
    the same twice, even when quantity is always "1" (one weigh-in per
    purchase). So a single distinct ratio (or none at all) means "counted
    as discrete items"; more than one means "genuinely measured" - see
    product_is_discrete_count/bulk_product_counting_units.
    """
    from invoices.models import InvoiceLine  # local import: inventory avoids a hard dependency on invoices otherwise

    ratios: dict[int, set] = {}
    lines = InvoiceLine.objects.filter(
        product_id__in=list(product_ids), total_volume__isnull=False, quantity__gt=0
    ).values_list("product_id", "total_volume", "quantity")
    for product_id, total_volume, quantity in lines:
        ratios.setdefault(product_id, set()).add(total_volume / quantity)
    return ratios


def product_is_discrete_count(product: Product) -> bool:
    """Whether a human should count this product in discrete items
    (bottles/packs) rather than the stock type's own measured unit - see
    product_counting_ratios for the reasoning. Single-product convenience
    around bulk_product_counting_units/product_counting_ratios, for
    valuing one stock-take line at a time."""
    ratios = product_counting_ratios([product.id]).get(product.id, set())
    return len(ratios) <= 1


def product_counting_ratio(product: Product) -> Decimal | None:
    """The fixed per-item size (e.g. 0.7 for a 70cl bottle) recovered from
    a discrete product's own invoice history - None if it can't be
    determined (no measured total_volume ever recorded for this product),
    in which case product.stock_equivalent is the only available fallback.
    """
    ratios = product_counting_ratios([product.id]).get(product.id)
    if ratios and len(ratios) == 1:
        return next(iter(ratios))
    return None


def bulk_product_counting_units(products) -> dict:
    """{product_id: "Unité" | "Litre" | "Kilogramme", ...} - what a human
    should physically count each product in: "Unité" (bottles/packs) for a
    discrete item, or the stock type's own measured unit (Litre/
    Kilogramme) for one that's genuinely weighed/measured - see
    product_counting_ratios.

    Deliberately NOT product.unit: that field always mirrors the stock
    type's own unit now (see assign_product in views.py), so it would show
    "Litre" even for a product you obviously count bottle by bottle - the
    two questions ("what unit does the STOCK TYPE track in" and "what do I
    physically count THIS PRODUCT in") aren't the same one.
    """
    products = list(products)
    ratios = product_counting_ratios([p.id for p in products])
    return {
        product.id: (
            "Unité"
            if len(ratios.get(product.id, set())) <= 1
            else (product.stock_type.get_unit_display() if product.stock_type_id else "Unité")
        )
        for product in products
    }


def compute_movement_amounts(invoice_line) -> tuple[Decimal, Decimal]:
    """Returns (quantity, unit_cost_ht) for the StockMovement this line
    should produce, in the stock type's own unit: the product's own amount
    (see product_base_amount) times its manual stock_equivalent conversion.
    unit_cost_ht is derived from the line's actual total, not from dividing
    twice, so it always reconciles exactly with what was paid.
    """
    product: Product = invoice_line.product
    quantity = product_base_amount(invoice_line) * product.stock_equivalent
    unit_cost_ht = (invoice_line.total_ht / quantity) if quantity else Decimal("0")
    return quantity, unit_cost_ht


def _fifo_value(lines_with_amounts, counted_quantity: Decimal) -> dict:
    """Shared FIFO-style ending-inventory valuation: assumes whatever's
    still on the shelf is whatever was bought most recently (the rest
    having already been used/sold), so this walks `lines_with_amounts`
    (already ordered newest-first, each a (line, line_qty) pair where
    line_qty is however much of the counted unit that line represents),
    pricing exactly `counted_quantity` units at the real price they were
    actually bought at - splitting the oldest line touched if the count
    doesn't land exactly on a line boundary.

    If the count exceeds everything the given lines can account for, the
    shortfall is still valued - at the oldest known per-unit price - and
    flagged via has_shortfall/shortfall_quantity, rather than silently
    understating the total or refusing to produce one.

    "sources" records exactly which lines (and how much of each) the
    non-shortfall portion of the value came from - see
    StockTakeLineSource - so the count stays traceable back to the real
    purchases it was priced from.
    """
    remaining = counted_quantity
    total_value = Decimal("0")
    oldest_unit_cost = None
    sources = []
    for line, line_qty in lines_with_amounts:
        if remaining <= 0:
            break
        # Only positive lines are stock that could still be on the shelf. A
        # NEGATIVE line_qty is a return/refund (a déconsigne of empty kegs,
        # a Metro pallet given back) - not inventory, and poison in this
        # loop: min() would pick the negative, so `remaining` would GROW
        # instead of shrinking, and its negative unit cost would then price
        # any shortfall at a discount. Measured a count worth 40 EUR coming
        # out at -140 EUR before this guard existed.
        if line_qty <= 0:
            continue
        line_unit_cost = line.total_ht / line_qty
        oldest_unit_cost = line_unit_cost
        used = min(line_qty, remaining)
        total_value += used * line_unit_cost
        sources.append({"invoice_line": line, "quantity_used": used, "unit_cost_ht": line_unit_cost})
        remaining -= used

    has_shortfall = remaining > 0
    shortfall_quantity = max(remaining, Decimal("0"))
    if has_shortfall and oldest_unit_cost is not None:
        total_value += shortfall_quantity * oldest_unit_cost

    return {
        "value_ht": total_value,
        "has_shortfall": has_shortfall,
        "shortfall_quantity": shortfall_quantity,
        "sources": sources,
    }


def _purchase_ladder(queryset, as_of: date | None):
    """Invoice lines newest-purchase-first, optionally as of a date - the
    order _fifo_value walks.

    Two things the database won't get right on its own. Ordering: invoice_date
    is nullable and NULL ordering is backend-specific (SQLite sorts them last
    on a DESC, PostgreSQL first), so it's pinned explicitly - an undated
    invoice is the one we know least about and belongs at the OLDEST end,
    never at the newest where it would price the entire count.

    And `as_of`: a back-dated stock take must not be priced from deliveries
    that hadn't arrived yet. Undated invoices survive that filter rather than
    being dropped - we can't prove they're too new, and dropping them would
    understate the count; they sort last anyway, so they're only ever reached
    as a fallback.
    """
    if as_of is not None:
        queryset = queryset.filter(Q(invoice__invoice_date__lte=as_of) | Q(invoice__invoice_date__isnull=True))
    return queryset.order_by(F("invoice__invoice_date").desc(nulls_last=True), "-id")


def value_counted_quantity(
    product: Product, counted_quantity: Decimal, unit: str, as_of: date | None = None
) -> dict:
    """FIFO valuation (see _fifo_value) for a count of one specific
    product. `unit` says what counted_quantity is expressed in - UNIT for
    a bottle/pack count, matched against invoice_line.quantity (not
    product_base_amount, which would read the measured total_volume
    instead - the right basis for stock movements, but litres when this
    count is in bottles); the product's stock type's own measured unit
    otherwise. Callers (StockTakeLineForm) restrict which unit can be
    chosen for a given product - see product_is_discrete_count for the
    default suggested. `as_of` prices the count as of a date - see
    _purchase_ladder.
    """
    is_unit_count = unit == UnitChoices.UNIT
    lines = _purchase_ladder(product.invoice_lines.select_related("invoice"), as_of)
    pairs = (
        (
            line,
            # A count in the stock type's own unit has to be matched against
            # a ladder in that same unit - so product_base_amount (which is
            # in the PRODUCT's unit) still needs the stock_equivalent
            # conversion, exactly as value_counted_stock_type_quantity below
            # applies it. Without it, counting "2.1 litres" of a product
            # measured in bottles priced 2.1 BOTTLES instead.
            Decimal(line.quantity) if is_unit_count else product_base_amount(line) * product.stock_equivalent,
        )
        for line in lines
    )
    return _fifo_value(pairs, counted_quantity)


def value_counted_stock_type_quantity(
    stock_type: StockType, counted_quantity: Decimal, as_of: date | None = None
) -> dict:
    """FIFO valuation (see _fifo_value) for a stock type counted directly
    (see StockTakeLine.stock_type) rather than through one specific
    product - for when it's easier to say "how much Vodka" than to pin
    down which brand. counted_quantity is already in the stock type's own
    unit, so every product's invoice lines are converted to that same unit
    via product_base_amount(line) * stock_equivalent - the same conversion
    StockMovement creation already relies on (see compute_movement_amounts)
    - and merged into one newest-first price ladder across every product
    under this stock type.
    """
    from invoices.models import InvoiceLine  # local import: inventory avoids a hard dependency on invoices otherwise

    lines = _purchase_ladder(
        InvoiceLine.objects.filter(product__stock_type=stock_type).select_related("invoice", "product"),
        as_of,
    )
    pairs = ((line, product_base_amount(line) * line.product.stock_equivalent) for line in lines)
    return _fifo_value(pairs, counted_quantity)


def create_stock_movement_for_line(invoice_line) -> StockMovement | None:
    """Create the StockMovement for an already-matched invoice line.

    No-op if the line's product still needs review, or already has a
    movement (idempotent, safe to call again after linking a product).
    """
    product: Product = invoice_line.product
    stock_type = product.stock_type
    if stock_type is None:
        return None
    if StockMovement.objects.filter(invoice_line=invoice_line).exists():
        return invoice_line.stock_movement

    quantity, unit_cost_ht = compute_movement_amounts(invoice_line)

    return StockMovement.objects.create(
        stock_type=stock_type,
        quantity=quantity,
        unit_cost_ht=unit_cost_ht,
        invoice_line=invoice_line,
    )


def refresh_invoice_statuses_for_product(product: Product) -> None:
    """Keeps Invoice.status in sync after a product's review state changes.

    status is set once at import time (NEEDS_REVIEW if any line's product
    still needed review, else COMPLETE) and nothing else ever revisits it -
    so without this, an invoice stays stuck on "À vérifier" forever even
    after every one of its products gets linked to a stock type later, since
    only Invoice.needs_review_count (computed live) actually reflects that.
    Only touches invoices currently in one of those two states, so it can't
    stomp on ERROR.
    """
    from invoices.models import Invoice  # local import: inventory avoids a hard dependency on invoices otherwise

    invoice_ids = product.invoice_lines.values_list("invoice_id", flat=True).distinct()
    for invoice in Invoice.objects.filter(
        id__in=invoice_ids, status__in=[Invoice.Status.NEEDS_REVIEW, Invoice.Status.COMPLETE]
    ):
        new_status = Invoice.Status.NEEDS_REVIEW if invoice.needs_review_count else Invoice.Status.COMPLETE
        if invoice.status != new_status:
            invoice.status = new_status
            invoice.save(update_fields=["status"])


def link_product_to_stock_type(
    product: Product,
    stock_type: StockType,
    unit: str,
    stock_equivalent: Decimal,
) -> None:
    """Link a reviewed product to a stock type and backfill stock movements
    for every invoice line already recorded against that product."""
    product.stock_type = stock_type
    product.unit = unit
    product.stock_equivalent = stock_equivalent
    product.ai_suggestion = None
    product.save(update_fields=["stock_type", "unit", "stock_equivalent", "ai_suggestion"])
    for line in product.invoice_lines.all():
        create_stock_movement_for_line(line)
    refresh_invoice_statuses_for_product(product)


def update_product_conversion(product: Product, unit: str, stock_equivalent: Decimal) -> None:
    """Fixes a product's unit/factor in place, without touching which stock
    type it's linked to - for correcting a wrong conversion directly from
    the Stock page instead of having to remove and re-add the product via
    the review queue. Every existing movement was computed with the old
    values, so they're dropped and recreated from scratch rather than
    patched - there's no way to "adjust" a past movement's quantity/cost
    without just recomputing it from the underlying invoice line.
    """
    StockMovement.objects.filter(invoice_line__product=product).delete()
    product.unit = unit
    product.stock_equivalent = stock_equivalent
    product.save(update_fields=["unit", "stock_equivalent"])
    for line in product.invoice_lines.all():
        create_stock_movement_for_line(line)


def merge_stock_types(source: StockType, target: StockType) -> None:
    """Merges `source` into `target`: every product (and the stock movements
    they've already produced) currently pointing at `source` gets re-pointed
    to `target`, then `source` is deleted.

    Only ever call this when source.unit == target.unit (the view enforces
    it) - a movement's quantity is meaningless without knowing which unit
    it's measured in, and a straight re-point like this never recomputes
    one. Two stock types tracked in different units (e.g. a spirit in
    litres and a food item in kilos) can't be merged this way; the caller
    is expected to reject that case with a clear error instead of silently
    producing numbers that don't mean anything.
    """
    StockMovement.objects.filter(stock_type=source).update(stock_type=target)
    Product.objects.filter(stock_type=source).update(stock_type=target)
    source.delete()


def unlink_product(product: Product) -> None:
    """Send a product back to the review queue: drop every stock movement it
    contributed and clear its stock_type. Its invoices/lines are untouched -
    only the stock ledger entries derived from them are removed."""
    StockMovement.objects.filter(invoice_line__product=product).delete()
    product.stock_type = None
    product.save(update_fields=["stock_type"])
    refresh_invoice_statuses_for_product(product)
