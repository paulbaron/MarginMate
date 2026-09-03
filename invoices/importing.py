from __future__ import annotations

import os
from datetime import date

from django.core.files import File
from django.db import transaction

from inventory.matching import resolve_product
from inventory.models import StockMovement
from inventory.services import create_stock_movement_for_line

from .models import Invoice, InvoiceLine, Supplier
from .parsers.base import ParsedInvoice


class DuplicateInvoiceError(Exception):
    """Raised when the (supplier, invoice_number) pair was already imported."""


@transaction.atomic
def import_parsed_invoice(
    supplier: Supplier,
    parsed: ParsedInvoice,
    source_file_path: str | None = None,
    display_filename: str | None = None,
) -> Invoice:
    if parsed.invoice_number:
        if Invoice.objects.filter(supplier=supplier, invoice_number=parsed.invoice_number).exists():
            raise DuplicateInvoiceError(f"Invoice {parsed.invoice_number} from {supplier} was already imported.")

    invoice = Invoice(
        supplier=supplier,
        invoice_number=parsed.invoice_number,
        invoice_date=parsed.invoice_date,
        reconciliation_adjustment=parsed.reconciliation_adjustment,
    )
    if source_file_path:
        name = display_filename or os.path.basename(source_file_path)
        with open(source_file_path, "rb") as fh:
            invoice.source_file.save(name, File(fh), save=False)
    invoice.save()

    needs_review = False
    for parsed_line in parsed.lines:
        product, _created = resolve_product(supplier, parsed_line.raw_name, parsed_line.ean)
        line = InvoiceLine.objects.create(
            invoice=invoice,
            product=product,
            raw_name=parsed_line.raw_name,
            quantity=parsed_line.quantity,
            colisage=parsed_line.colisage,
            total_volume=parsed_line.total_volume,
            unit_cost_ht=parsed_line.unit_cost_ht,
            total_ht=parsed_line.total_ht,
            taxes=parsed_line.taxes,
            discount=parsed_line.discount,
            vat_rate=parsed_line.vat_rate,
            category=parsed_line.category,
        )
        if product.needs_review:
            needs_review = True
        else:
            create_stock_movement_for_line(line)

    if not parsed.lines:
        # Nothing was parsed, by design: the supplier has no parser, so the
        # PDF is filed and its lines get typed in by hand. Marked for review
        # so it doesn't sit in the list looking like a complete, zero-euro
        # invoice - which is what an empty COMPLETE would read as.
        invoice.status = Invoice.Status.NEEDS_REVIEW
    else:
        invoice.status = Invoice.Status.NEEDS_REVIEW if needs_review else Invoice.Status.COMPLETE
    invoice.save(update_fields=["status"])
    return invoice


def parse_and_import(
    pdf_path: str,
    supplier: Supplier,
    date_hint: date | None = None,
    display_filename: str | None = None,
    parser_key_override: str | None = None,
) -> Invoice:
    """`parser_key_override` lets a caller (see InvoiceType.parser_key) pick
    the parser independently of the supplier's own default - None (the
    default) means "use supplier.parser_key" as before; an explicit value
    (including "") takes precedence over it.

    With no parser for the supplier, the invoice is imported EMPTY - the PDF
    is filed, and its lines are typed in by hand afterwards (see
    invoices/views.py::edit_invoice_lines). This used to hand the document to
    an LLM instead and keep whatever it returned. Guessing at prices is the
    one thing this app must not do: every number here ends up in a stock
    valuation or a margin, and a plausible-looking wrong figure is worse than
    no figure at all, because nothing downstream can tell the difference.
    """
    from .parsers import get_parser

    key = supplier.parser_key if parser_key_override is None else parser_key_override
    parser = get_parser(key)
    if parser is None:
        parsed = ParsedInvoice(
            supplier_code=supplier.code,
            invoice_number="",
            invoice_date=date_hint,
            lines=[],
        )
    else:
        parsed = parser.parse(pdf_path, date_hint=date_hint)
    return import_parsed_invoice(supplier, parsed, source_file_path=pdf_path, display_filename=display_filename)


@transaction.atomic
def replace_invoice_lines(invoice: Invoice, parsed_lines) -> Invoice:
    """Replace an invoice's lines with `parsed_lines`, rebuilding the stock
    movements that came from them.

    Used when lines are typed in by hand (an invoice from a supplier with no
    parser) and when correcting a parsed one. The old lines' stock movements
    have to go with them - leaving them behind would double-count the stock,
    and they are the whole reason a line matters.
    """
    for line in invoice.lines.all():
        StockMovement.objects.filter(invoice_line=line).delete()
    invoice.lines.all().delete()

    needs_review = False
    for parsed_line in parsed_lines:
        product, _created = resolve_product(invoice.supplier, parsed_line.raw_name, parsed_line.ean)
        line = InvoiceLine.objects.create(
            invoice=invoice,
            product=product,
            raw_name=parsed_line.raw_name,
            quantity=parsed_line.quantity,
            colisage=parsed_line.colisage,
            total_volume=parsed_line.total_volume,
            unit_cost_ht=parsed_line.unit_cost_ht,
            total_ht=parsed_line.total_ht,
            taxes=parsed_line.taxes,
            discount=parsed_line.discount,
            vat_rate=parsed_line.vat_rate,
            category=parsed_line.category,
        )
        if product.needs_review:
            needs_review = True
        else:
            create_stock_movement_for_line(line)

    if not parsed_lines:
        invoice.status = Invoice.Status.NEEDS_REVIEW
    else:
        invoice.status = Invoice.Status.NEEDS_REVIEW if needs_review else Invoice.Status.COMPLETE
    invoice.save(update_fields=["status"])
    return invoice
