"""Removing an invoice, and everything that only existed because of it.

Deleting the Invoice row on its own would be wrong in ways nothing shows:

* **Its stock would stay.** `StockMovement.invoice_line` is SET_NULL, so the
  purchase movements outlive their invoice: stock that was never bought,
  valued at a price nobody paid, with nothing left pointing at where it came
  from. `replace_invoice_lines` deletes them explicitly for the same reason,
  and so does this.
* **A past stock take would lose its paper trail.** `StockTakeLineSource`
  records which invoice lines a count was priced from (PROTECT) - it is the
  audit trail of a count someone already relied on. Such an invoice is
  refused, naming the counts that depend on it, rather than failing with a
  database error or quietly rewriting what the count was worth.

What does go with it: its lines, their stock movements, its files (the
original PDF or photo and the review preview), and the products that only
this invoice had created and nobody has classified yet - otherwise deleting a
misread receipt leaves its garbled names waiting in the review queue for
ever. A classified product stays: that is the user's own work, and the next
invoice will want it. `replace_invoice_lines` applies the same rule to the
products a corrected invoice's old lines leave behind.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import ProtectedError

from inventory.models import Product, StockMovement, StockTake, StockTakeLineSource

from .models import Invoice


class InvoiceInUseError(Exception):
    """The invoice priced a stock take and can't be removed without breaking it."""

    def __init__(self, invoice: Invoice, stock_takes: list[StockTake]):
        self.invoice = invoice
        self.stock_takes = stock_takes
        dates = ", ".join(f"{take.taken_at:%d/%m/%Y}" for take in stock_takes)
        super().__init__(
            f"{invoice} a servi à valoriser l'inventaire du {dates} : "
            "la supprimer changerait la valeur de cet inventaire."
        )


@dataclass
class DeletionSummary:
    label: str
    lines: int
    movements: int
    products_removed: int


def blocking_stock_takes(invoice: Invoice) -> list[StockTake]:
    """The stock takes whose frozen valuation was drawn from this invoice."""
    take_ids = (
        StockTakeLineSource.objects.filter(invoice_line__invoice=invoice)
        .values_list("stock_take_line__stock_take_id", flat=True)
        .distinct()
    )
    return list(StockTake.objects.filter(id__in=take_ids).order_by("taken_at"))


@transaction.atomic
def delete_invoice(invoice: Invoice) -> DeletionSummary:
    blockers = blocking_stock_takes(invoice)
    if blockers:
        raise InvoiceInUseError(invoice, blockers)

    line_ids = list(invoice.lines.values_list("id", flat=True))
    product_ids = set(invoice.lines.values_list("product_id", flat=True))
    movements, _by_model = StockMovement.objects.filter(invoice_line_id__in=line_ids).delete()

    files = [(field.storage, field.name) for field in (invoice.source_file, invoice.preview_image) if field]
    label = str(invoice)
    invoice.delete()  # cascades to its lines

    products_removed = remove_orphan_products(product_ids)

    # Only once the rows are really gone - a rolled-back deletion must not
    # have already thrown the photo away.
    transaction.on_commit(lambda: _delete_files(files))
    return DeletionSummary(label=label, lines=len(line_ids), movements=movements, products_removed=products_removed)


def remove_orphan_products(product_ids) -> int:
    """Delete those of `product_ids` that no invoice line uses any more and
    nobody has classified. Returns how many went."""
    removed = 0
    for product in Product.objects.filter(id__in=product_ids, stock_type__isnull=True):
        if product.invoice_lines.exists():
            continue  # another invoice bought it too
        try:
            # Its own savepoint: a product still held by something else (a
            # stock-take line counts it) must not undo the whole operation.
            with transaction.atomic():
                product.delete()
        except ProtectedError:
            continue
        removed += 1
    return removed


def _delete_files(files) -> None:
    for storage, name in files:
        try:
            storage.delete(name)
        except OSError:
            # Already gone, or locked by another program on Windows. Either
            # way the invoice is deleted; a stray file costs nothing.
            continue
