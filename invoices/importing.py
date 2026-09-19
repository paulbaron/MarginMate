from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from django.core.files import File
from django.db import transaction
from django.utils import timezone

from inventory.matching import resolve_products
from inventory.models import Product, StockMovement, StockTake, StockTakeLineSource
from inventory.services import create_stock_movement_for_line, expense_product

from .charges import read_charge
from .deletion import remove_orphan_products
from .models import Invoice, InvoiceLine, Supplier
from .ocr import document_text
from .parsers import is_ticket_shop
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
    if supplier.expenses_only:
        parsed.printed_total_ttc = charge_reading(parsed)[0]

    invoice = Invoice(
        supplier=supplier,
        invoice_number=parsed.invoice_number,
        invoice_date=parsed.invoice_date,
        reconciliation_adjustment=parsed.reconciliation_adjustment,
        printed_total_ttc=parsed.printed_total_ttc,
        # The table the document prints, for the checks to compare against
        # and for a person to correct (receipts.vat_table).
        vat_breakdown=[[str(rate), str(base), str(tax)] for rate, base, tax in parsed.vat_breakdown],
    )
    if source_file_path:
        name = display_filename or os.path.basename(source_file_path)
        with open(source_file_path, "rb") as fh:
            invoice.source_file.save(name, File(fh), save=False)
    invoice.save()

    # A supplier of charges has no products: its document is filed as the
    # postes it names, or as one line per VAT rate, on products that carry
    # its charges and reach no stock page.
    lines = expense_lines(supplier, parsed) if supplier.expenses_only else parsed.lines
    needs_review = False
    if supplier.expenses_only:
        for parsed_line in lines:
            _create_line(invoice, expense_product(supplier, parsed_line.raw_name), parsed_line)
    else:
        resolved = resolve_products(
            supplier, [(line.raw_name, line.ean) for line in lines], ocr_tolerant=parsed.from_ocr
        )
        for parsed_line, (product, _created) in zip(lines, resolved):
            line = _create_line(invoice, product, parsed_line)
            if product.needs_review:
                needs_review = True
            else:
                create_stock_movement_for_line(line)

    if not lines:
        # Nothing was parsed, by design: the supplier has no parser, so the
        # PDF is filed and its lines get typed in by hand. Marked for review
        # so it doesn't sit in the list looking like a complete, zero-euro
        # invoice - which is what an empty COMPLETE would read as.
        invoice.status = Invoice.Status.NEEDS_REVIEW
    else:
        invoice.status = Invoice.Status.NEEDS_REVIEW if needs_review else Invoice.Status.COMPLETE
    invoice.save(update_fields=["status"])
    if supplier.expenses_only:
        charge_needs_a_look(invoice, parsed.printed_total_ttc)
    return invoice


UNREAD_CHARGE = (
    "Le total de ce document n'a pas été lu : le montant de la charge vient de ce qui a pu être lu, "
    "vérifiez-le sur le document."
)


def charge_needs_a_look(invoice: Invoice, printed_total) -> None:
    """A charge is its document's own total. Where that total could not be
    read, the figure filed is whatever was read instead - said out loud, and
    the document waits in "À vérifier" rather than passing for settled."""
    charge_state(invoice, printed_total)


def charge_state(invoice: Invoice, printed_total) -> bool:
    """Set a charge document's status, message and checks from its total, and
    say whether that changed anything: settled when the document's own total
    was read, waiting with what is wrong written on it when it was not.

    Its checks are about the charge. Kept from the ticket reading it is no
    longer, they said "lignes 946,11 € / ticket 304,74 €" over two lines
    adding up to exactly what the document charges - a warning about nothing,
    under every row a person opens.
    """
    if not invoice.lines.exists():
        return False
    unread = printed_total is None
    checks = charge_checks(invoice, printed_total)
    wanted = (
        Invoice.Status.NEEDS_REVIEW if unread else Invoice.Status.COMPLETE,
        UNREAD_CHARGE if unread else "",
    )
    if (invoice.status, invoice.error_message, invoice.parse_checks) == (*wanted, checks):
        return False
    invoice.status, invoice.error_message = wanted
    invoice.parse_checks = checks
    invoice.save(update_fields=["status", "error_message", "parse_checks"])
    return True


def charge_checks(invoice: Invoice, printed_total) -> list[dict]:
    """What there is to check on a charge: that the document's own total was
    read, and that it is dated (out of every valuation and of the bank match
    without one)."""
    from .receipts import date_check

    total = (
        f"{printed_total} € : le total imprimé sur le document."
        if printed_total is not None
        else UNREAD_CHARGE
    )
    checks = [{"label": "Total de la charge", "passed": printed_total is not None, "detail": total}]
    dated = date_check(invoice.invoice_date)
    if dated is not None:
        checks.append({"label": dated.label, "passed": dated.passed, "detail": dated.detail})
    return checks


def expense_lines(supplier: Supplier, parsed: ParsedInvoice) -> list[ParsedLine]:
    """What a supplier of charges is filed as (see charge_reading)."""
    return charge_reading(parsed, supplier.name)[1]


def charge_reading(parsed: ParsedInvoice, name: str = "") -> tuple[Decimal | None, list[ParsedLine]]:
    """What a charge document is worth, and the lines it is filed as.

    A subscription, a rent, a water bill have no product behind them - what
    matters is what was paid, on what, and at which rate, for the accounts
    and for the bank match. Three readings, in this order:

    - **its VAT table**, when it accounts for the total to the cent: one
      line per rate. The strongest, and a rate that does not add up is a
      rate nobody should book;
    - **the postes it names** (invoices.charges): one line each, which is
      how a rent is kept apart from the building provision beside it. They
      settle the total too - a rent statement prints last month's échéance
      and the direct debit that paid it, and the largest amount printed
      twice is that, not what is being charged now;
    - **its total alone**, on one line named after the supplier.

    What was paid is never the sum of whatever was read as lines: a rent
    statement lists the previous balance, the direct debit, the tax and the
    rent, and adding those up gives a figure nobody ever paid. With no total
    at all, the lines are all there is; with neither, nothing is filed and
    the document waits.
    """
    total = parsed.printed_total_ttc
    breakdown = [(rate, base, tax) for rate, base, tax in parsed.vat_breakdown if base]
    accounted = sum((base + tax for _rate, base, tax in breakdown), start=Decimal("0"))
    if breakdown and (total is None or abs(accounted - total) <= Decimal("0.01")):
        return total, [_expense_line(name, base, rate, base + tax) for rate, base, tax in breakdown]
    settled, postes = read_charge(parsed.source_text, total)
    if postes:
        return settled, [
            _expense_line(poste.name, poste.total_ht, poste.rate, poste.amount) for poste in postes
        ]
    if total is None:
        # Not what was paid, and it must not pass for it (charge_needs_a_look).
        read = _lines_total(parsed)
        return None, ([_expense_line(name, read, Decimal("0"))] if read else [])
    return total, [_expense_line(name, total, Decimal("0"))]


def _lines_total(parsed: ParsedInvoice) -> Decimal:
    gross = sum(
        ((line.total_ht * (Decimal("1") + line.vat_rate)) for line in parsed.lines), start=Decimal("0")
    )
    return gross.quantize(Decimal("0.01"))


def _expense_line(name: str, total_ht: Decimal, rate: Decimal, printed_ttc: Decimal | None = None) -> ParsedLine:
    """A charge line keeps **the amount the document charges**, tax included
    (InvoiceLine.printed_ttc), and not only its HT.

    What a subscription costs is a TTC figure - it is what leaves the bank -
    and 33,33 € HT at 20% works back out to 40,00 € where the invoice says
    39,99 €. The HT stays what the document prints too, so the rounding lands
    where the document put it rather than on what was paid.
    """
    return ParsedLine(
        raw_name=name,
        quantity=1,
        total_volume=Decimal("0"),
        unit_cost_ht=total_ht,
        total_ht=total_ht,
        vat_rate=rate,
        printed_ttc=printed_ttc if printed_ttc is not None else (total_ht * (1 + rate)).quantize(Decimal("0.01")),
    )


def refile_as_charge(invoice: Invoice, parsed: ParsedInvoice) -> bool:
    """Put a charge document's own reading in place of its lines - what
    importing it does, for the paths that read it again.

    Every one of them has to come through here: read as a ticket, a rent
    statement's lines are the previous balance and the direct debit beside
    the rent, and a document relu that way went back to being worth what it
    was before the charge reading settled it.
    """
    total, lines = charge_reading(parsed, invoice.supplier.name)
    if not lines:
        return False
    changed = False
    stored = list(invoice.lines.all())
    if not (_already_charges(invoice.supplier, stored, lines) and total == invoice.printed_total_ttc):
        try:
            with transaction.atomic():
                # The postes first, for replace_invoice_lines to find them by
                # name - and in its savepoint: a replacement refused
                # (InvoiceLinesInUseError) takes them back. Made before it,
                # one was left named after the supplier, on no line.
                for line in lines:
                    expense_product(invoice.supplier, line.raw_name)
                replace_invoice_lines(invoice, lines)
                invoice.printed_total_ttc = total
                invoice.invoice_date = invoice.invoice_date or parsed.invoice_date
                invoice.save(update_fields=["printed_total_ttc", "invoice_date"])
        except InvoiceLinesInUseError:
            return False
        changed = True
    # Its state follows its total even when its lines did not move: a
    # document held in "À vérifier" before its supplier was known to be one
    # of charges stayed flagged, with nothing to say about what was wrong.
    return charge_state(invoice, total) or changed


def redo_as_expenses(supplier: Supplier) -> int:
    """File the documents already in as charges: their postes, or the one
    line their total makes, and the products their old lines named go with
    them (remove_orphan_products). Returns how many documents changed - one
    whose lines a stock take was priced from is left alone, since it was
    stock after all.

    Each document is **read again from its own text** where it kept some,
    rather than from the lines it is filed as: a rent statement filed at
    last month's échéance says so nowhere in those lines, and a correction
    typed on one of them is lost - which is the price of changing what a
    supplier is.

    Through `refile_as_charge` like every other path, so a document held in
    "À vérifier" before the supplier was known to be one of charges is
    settled by the move rather than left flagged with nothing to say.
    """
    done = 0
    for invoice in Invoice.objects.filter(supplier=supplier).prefetch_related("lines"):
        done += refile_as_charge(invoice, _as_parsed(invoice, list(invoice.lines.all())))
    # Whatever the documents needed, what this supplier sends is a charge:
    # unticked and ticked again, not one line changes, so nothing else
    # would put the flag back on its postes. Not a product a stock item
    # claimed - it is stock after all, which is the same reason a document
    # a stock take was priced from is left alone above.
    Product.objects.filter(supplier=supplier, is_expense=False, stock_type__isnull=True).update(is_expense=True)
    return done


def charge_credits(supplier: Supplier):
    """A supplier's lines holding a credit the way a charge takes one: a
    count above zero at a negative amount (LineCorrectionForm, `charge=`) -
    what goods refuse."""
    return InvoiceLine.objects.filter(invoice__supplier=supplier, quantity__gt=0, total_ht__lt=0)


def is_charge_credit(line) -> bool:
    """`line` (an InvoiceLine or a ParsedLine) is what charge_credits finds."""
    return line.quantity > 0 and line.total_ht < 0


def credit_as_return(line) -> None:
    """A credit filed the way a charge takes one, made a return as goods keep
    one: the count negative, the amount as it was, so the unit price is what
    was given back. For a line leaving charges - its supplier leaving them
    (stop_expenses) or the document moved to a supplier of goods
    (receipts.move_documents); `line` is an InvoiceLine or a ParsedLine, and
    the caller saves it."""
    line.quantity = -line.quantity
    # A weight goes back with its count (a charge's is 0, and stays so).
    line.total_volume = -line.total_volume if line.total_volume else line.total_volume
    line.unit_cost_ht = (line.total_ht / line.quantity).quantize(UNIT_COST)


@transaction.atomic
def stop_expenses(supplier: Supplier) -> int:
    """A supplier that no longer sends charges sells goods again, so its
    postes are products like any others - waiting to be classified.

    The documents already filed keep their lines (see views.supplier_expenses:
    they are a person's to correct), but their products must not stay
    flagged: unticked, a supplier's products stayed out of the review queue
    and out of the stock pages with nothing on any screen able to free them
    - correcting a document by hand resolved the same flagged product.

    A credit filed the way a charge takes one (charge_credits) becomes a
    return (credit_as_return). Left at a count of 1, the goods guard refused
    the row on a document saved untouched, and classifying its poste booked
    stock at a negative unit cost. A movement already booked from one (a
    stock item the supplier kept) is booked again.
    Returns how many products went back.
    """
    for line in charge_credits(supplier):
        credit_as_return(line)
        line.save(update_fields=["quantity", "total_volume", "unit_cost_ht"])
        if StockMovement.objects.filter(invoice_line=line).delete()[0]:
            create_stock_movement_for_line(line)
    return Product.objects.filter(supplier=supplier, is_expense=True).update(is_expense=False)


def _as_parsed(invoice: Invoice, stored) -> ParsedInvoice:
    read = _read_again(invoice)
    if read is not None:
        return read
    # Nothing was kept of the document itself (a paper invoice typed in):
    # the rates its lines carry are all the VAT table there is, and redoing
    # it as charges must not lose them.
    by_rate: dict[Decimal, Decimal] = {}
    for line in stored:
        by_rate[line.vat_rate] = by_rate.get(line.vat_rate, Decimal("0")) + line.total_ht
    breakdown = [
        (rate, total, (total * rate).quantize(Decimal("0.01")))
        for rate, total in sorted(by_rate.items())
        if total
    ]
    return ParsedInvoice(
        vat_breakdown=breakdown,
        source_text=invoice.document_text,
        supplier_code=invoice.supplier.code,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        lines=[
            ParsedLine(
                raw_name=line.raw_name, quantity=line.quantity, total_volume=line.total_volume,
                unit_cost_ht=line.unit_cost_ht, total_ht=line.total_ht, vat_rate=line.vat_rate,
            )
            for line in stored
        ],
        printed_total_ttc=invoice.printed_total_ttc,
    )


def _read_again(invoice: Invoice) -> ParsedInvoice | None:
    """The document read from the text it kept, or None - not a PDF any
    more, or a supplier whose reader needs the file itself."""
    from .receipts import parser_for  # here: receipts imports this module

    text = invoice.document_text
    reader = parser_for(invoice.supplier) if text else None
    if reader is None or not hasattr(reader, "parse_text"):
        return None
    try:
        parsed = reader.parse_text(text, date_hint=invoice.invoice_date)
    except Exception:  # noqa: BLE001 - a reading that fails is one less reading
        return None
    parsed.invoice_number = invoice.invoice_number
    parsed.invoice_date = invoice.invoice_date
    return parsed


def _already_charges(supplier: Supplier, stored, wanted) -> bool:
    """Whether the document is already filed as exactly this charge - down
    to the amount each line prints: filed before a charge kept its TTC, the
    lines look the same and show 40,00 € for the 39,99 € of the bill."""
    return len(stored) == len(wanted) and all(
        (line.raw_name, line.total_ht, line.vat_rate, line.printed_ttc)
        == (parsed_line.raw_name, parsed_line.total_ht, parsed_line.vat_rate, parsed_line.printed_ttc)
        for line, parsed_line in zip(stored, wanted)
    )


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
    # What the document itself says, kept for what it tells about its sender
    # (invoices/identifiers.py) - and for the header a shop is given.
    invoice.source_text = document_text(pdf_path)
    if invoice.source_text:
        invoice.save(update_fields=["source_text"])

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
    ocr_tolerant = invoice.is_receipt and is_ticket_shop(invoice.supplier)
    resolved = resolve_products(
        invoice.supplier, [(line.raw_name, line.ean) for line in parsed_lines], ocr_tolerant=ocr_tolerant
    )
    # A product is a poste of charge exactly when its supplier's documents
    # are charges: nothing this supplier sends is a product, so a poste
    # renamed by hand must not land in the queue of products to classify -
    # and the flag has to come **off** again when the supplier goes back to
    # selling goods, or a box ticked by mistake leaves its products out of
    # the queue, out of every stock page and out of every stock movement
    # for ever, with no screen able to put them back.
    for product, _created in resolved:
        if product.is_expense != invoice.supplier.expenses_only:
            product.is_expense = invoice.supplier.expenses_only
            product.save(update_fields=["is_expense"])
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
