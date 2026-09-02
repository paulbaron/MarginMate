from __future__ import annotations

import os
from datetime import date

from django.core.files import File
from django.db import transaction

from inventory.matching import resolve_product
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

    invoice.status = Invoice.Status.NEEDS_REVIEW if needs_review else Invoice.Status.COMPLETE
    invoice.save(update_fields=["status"])
    return invoice


def parse_and_import(
    pdf_path: str,
    supplier: Supplier,
    date_hint: date | None = None,
    display_filename: str | None = None,
) -> Invoice:
    from .parsers import get_parser

    parser = get_parser(supplier.parser_key) or get_parser("LLM")
    if parser is None:
        raise RuntimeError(f"No parser available for supplier {supplier}.")
    parsed = parser.parse(pdf_path, date_hint=date_hint)
    return import_parsed_invoice(supplier, parsed, source_file_path=pdf_path, display_filename=display_filename)
