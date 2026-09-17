from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from django.core.files import File
from django.db import transaction
from django.utils import timezone

from inventory.matching import resolve_products
from inventory.models import StockMovement, StockTake, StockTakeLineSource
from inventory.services import create_stock_movement_for_line

from .deletion import remove_orphan_products
from .models import Invoice, InvoiceLine, Supplier
from .parsers import ticket_parser_for
from .parsers.base import ParsedInvoice, ParsedLine


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
            raise DuplicateInvoiceError(f"Déjà dans MarginMate : {supplier} n° {parsed.invoice_number}.")

    invoice = Invoice(
        supplier=supplier,
        invoice_number=parsed.invoice_number,
        invoice_date=parsed.invoice_date,
        reconciliation_adjustment=parsed.reconciliation_adjustment,
        printed_total_ttc=parsed.printed_total_ttc,
    )
    if source_file_path:
        name = display_filename or os.path.basename(source_file_path)
        with open(source_file_path, "rb") as fh:
            invoice.source_file.save(name, File(fh), save=False)
    invoice.save()

    needs_review = False
    resolved = resolve_products(
        supplier, [(line.raw_name, line.ean) for line in parsed.lines], ocr_tolerant=parsed.from_ocr
    )
    for parsed_line, (product, _created) in zip(parsed.lines, resolved):
        line = _create_line(invoice, product, parsed_line)
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


def _line_values(parsed_line: ParsedLine) -> dict:
    return {
        "raw_name": parsed_line.raw_name,
        "read_as": parsed_line.read_as,
        "quantity": parsed_line.quantity,
        "colisage": parsed_line.colisage,
        "total_volume": parsed_line.total_volume,
        "unit_cost_ht": parsed_line.unit_cost_ht,
        "total_ht": parsed_line.total_ht,
        "taxes": parsed_line.taxes,
        "discount": parsed_line.discount,
        "vat_rate": parsed_line.vat_rate,
        "category": parsed_line.category,
        "printed_ttc": parsed_line.printed_ttc,
        "discount_ttc": parsed_line.discount_ttc,
    }


def _create_line(invoice: Invoice, product, parsed_line: ParsedLine) -> InvoiceLine:
    return InvoiceLine.objects.create(invoice=invoice, product=product, **_line_values(parsed_line))


class InvoiceLinesInUseError(Exception):
    """Lines a stock take was priced from can be corrected, not removed."""

    def __init__(self, lines, stock_takes):
        self.lines = lines
        self.stock_takes = stock_takes
        names = ", ".join(sorted({line.raw_name for line in lines}))
        dates = ", ".join(f"{timezone.localtime(take.taken_at):%d/%m/%Y}" for take in stock_takes)
        super().__init__(
            f"{names} a servi à valoriser l'inventaire du {dates} : cette ligne peut être corrigée, "
            "pas retirée - la retirer changerait la valeur de cet inventaire."
        )


_STORED = object()
VOLUME = Decimal("0.001")
UNIT_COST = Decimal("0.0001")


def corrected_line(
    stored: InvoiceLine | None,
    raw_name: str,
    quantity: int,
    total_ht: Decimal,
    vat_rate: Decimal,
    read_as=_STORED,
    printed_ttc=_STORED,
    total_volume=None,
    discount_ttc=_STORED,
    discount=_STORED,
) -> ParsedLine:
    """A row a person corrected, as the ParsedLine replace_invoice_lines takes.

    A row that was a stored line keeps what the forms don't show: Metro's
    measured volume (scaled when the count changed - the size of an item
    didn't), duty, discount, pack size, category, and what OCR read. Rebuilt
    from the four visible fields instead, a Metro invoice saved untouched
    turned 4.2 L of vodka into 6 L of stock. The printed TTC stays while the
    amount does; `read_as`/`printed_ttc`, when given, are the form's own, and
    so are a weight typed (`total_volume`) and a ticket's promotion
    (`discount_ttc`, with `discount` its HT).
    """
    line = ParsedLine(
        raw_name=raw_name,
        quantity=quantity,
        total_volume=Decimal("0"),
        unit_cost_ht=(total_ht / quantity).quantize(UNIT_COST) if quantity else Decimal("0"),
        total_ht=total_ht,
        vat_rate=vat_rate,
        read_as="" if read_as is _STORED else read_as,
        printed_ttc=None if printed_ttc is _STORED else printed_ttc,
        discount_ttc=Decimal("0") if discount_ttc is _STORED else discount_ttc,
    )
    if discount is not _STORED:
        line.discount = discount
    if total_volume is not None:
        line.total_volume = total_volume
    if stored is None:
        return line
    line.line_id = stored.pk
    if total_volume is None:
        if stored.quantity and quantity != stored.quantity:
            line.total_volume = (stored.total_volume * quantity / stored.quantity).quantize(VOLUME)
        else:
            line.total_volume = stored.total_volume
    line.taxes = stored.taxes
    if discount is _STORED:
        line.discount = stored.discount
    line.colisage, line.category = stored.colisage, stored.category
    if read_as is _STORED:
        line.read_as = stored.read_as
    if printed_ttc is _STORED and (total_ht, vat_rate) == (stored.total_ht, stored.vat_rate):
        line.printed_ttc = stored.printed_ttc
        if discount_ttc is _STORED:
            line.discount_ttc = stored.discount_ttc
    return line


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
    invoice = import_parsed_invoice(supplier, parsed, source_file_path=pdf_path, display_filename=display_filename)

    # Said on the invoice itself. A parser that reads nothing has usually met
    # a new layout - and an empty invoice otherwise reads as "this supplier
    # has no parser", which is what a Plou & Fils one said in April 2026.
    problems = list(parsed.warnings)
    if parser is not None and not parsed.lines:
        problems.append(
            f"Le parseur {key} n'a trouvé aucune ligne dans ce document : sa mise en page a peut-être "
            "changé. Saisissez les lignes à la main."
        )
    if invoice.invoice_date is None:
        # Undated, it sits outside every stock valuation and the bank match.
        problems.append("Date introuvable dans le document : saisissez-la dans « Corriger les lignes ».")
    if problems:
        invoice.error_message = " ".join(problems)
        invoice.status = Invoice.Status.NEEDS_REVIEW
        invoice.save(update_fields=["error_message", "status"])
    return invoice


@transaction.atomic
def replace_invoice_lines(invoice: Invoice, parsed_lines) -> Invoice:
    """Replace an invoice's lines with `parsed_lines`, rebuilding the stock
    movements that came from them.

    Used when lines are typed in by hand (an invoice from a supplier with no
    parser) and when correcting a parsed one. The old lines' stock movements
    have to go with them - leaving them behind would double-count the stock,
    and they are the whole reason a line matters.

    A parsed line with a `line_id` corrects that stored line in place: the
    same row, so a stock take priced from it keeps its trail (the line used
    to be deleted and recreated, which the stock take's PROTECT refused with
    a server error). Stored lines no parsed line names are removed - unless a
    stock take was priced from them: InvoiceLinesInUseError, nothing saved.

    So do the products the old lines pointed at that no line uses any more,
    when nobody has classified them: a misreading attached by hand to the
    product it really was leaves behind the product it had created, which
    would otherwise wait in the review queue for ever. Same rule as deleting
    an invoice (deletion.remove_orphan_products).
    """
    parsed_lines = list(parsed_lines)
    stored = {line.pk: line for line in invoice.lines.all()}
    corrected = {parsed.line_id for parsed in parsed_lines if parsed.line_id in stored}
    removed = [line for pk, line in stored.items() if pk not in corrected]
    take_ids = (
        StockTakeLineSource.objects.filter(invoice_line__in=removed)
        .values_list("stock_take_line__stock_take_id", flat=True)
        .distinct()
    )
    if removed and take_ids:
        used = set(StockTakeLineSource.objects.filter(invoice_line__in=removed).values_list("invoice_line_id", flat=True))
        raise InvoiceLinesInUseError(
            [line for line in removed if line.pk in used],
            list(StockTake.objects.filter(id__in=take_ids).order_by("taken_at")),
        )

    previous_products = {line.product_id for line in stored.values()}
    StockMovement.objects.filter(invoice_line__in=list(stored.values())).delete()
    InvoiceLine.objects.filter(pk__in=[line.pk for line in removed]).delete()

    needs_review = False
    # A receipt's lines were read by OCR, and a name corrected on the
    # review screen can still carry the recogniser's mistakes. Not a paper
    # ticket filed by hand under Metro or UBA: its lines are typed, and that
    # supplier's digital catalogue keeps the strict matcher.
    ocr_tolerant = invoice.is_receipt and ticket_parser_for(invoice.supplier.code) is not None
    resolved = resolve_products(
        invoice.supplier, [(line.raw_name, line.ean) for line in parsed_lines], ocr_tolerant=ocr_tolerant
    )
    for parsed_line, (product, _created) in zip(parsed_lines, resolved):
        # pop: a line named twice (a crafted post) is corrected once.
        line = stored.pop(parsed_line.line_id, None) if parsed_line.line_id in corrected else None
        if line is None:
            line = _create_line(invoice, product, parsed_line)
        else:
            for field, value in _line_values(parsed_line).items():
                setattr(line, field, value)
            line.product = product
            line.save()
        if product.needs_review:
            needs_review = True
        else:
            create_stock_movement_for_line(line)
    remove_orphan_products(previous_products)

    if not parsed_lines:
        invoice.status = Invoice.Status.NEEDS_REVIEW
    else:
        invoice.status = Invoice.Status.NEEDS_REVIEW if needs_review else Invoice.Status.COMPLETE
    invoice.save(update_fields=["status"])
    return invoice
