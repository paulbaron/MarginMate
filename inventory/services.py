from decimal import Decimal

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


def unlink_product(product: Product) -> None:
    """Send a product back to the review queue: drop every stock movement it
    contributed and clear its stock_type. Its invoices/lines are untouched -
    only the stock ledger entries derived from them are removed."""
    StockMovement.objects.filter(invoice_line__product=product).delete()
    product.stock_type = None
    product.save(update_fields=["stock_type"])
    refresh_invoice_statuses_for_product(product)
