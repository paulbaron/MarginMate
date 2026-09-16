"""Importing photographed till receipts, end to end.

One receipt is one photo: recognise it, work out which shop it came from,
run that shop's parser, and file the result with everything a human needs to
check it - the corrected image, the text the recogniser produced, and the
parser's own verdict on whether its arithmetic held.

**The shop is detected, not chosen.** A batch is a handful of photos taken
over a week at four different shops, and making the operator tag each file
before uploading is the kind of friction that ends with receipts not being
entered at all. Every receipt prints its own name at the top, so that is
what decides. When it doesn't match anything the file is reported as
unrecognised rather than guessed at - a Franprix ticket run through the
Monoprix parser would produce lines, and they would be wrong.

When the header is unreadable the operator names the shop instead
(`import_receipt(..., supplier=...)`): that shop's parser runs on the ticket,
and a supplier with no ticket reader gets the ticket filed empty, to be typed
in from the photo on the same review screen.

Nothing here trusts the parse. `import_receipt` files what it read and marks
the invoice for review unless the receipt reconciled against its own printed
totals; `invoices/views.py::receipt_review` is where a person confirms it.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction

from .importing import DuplicateInvoiceError, import_parsed_invoice
from .models import Invoice, InvoiceLine, ShopItemPrice, Supplier, label_for_unit_price
from .ocr import deskew, ocr_prepared_image, page_images
from .parsers import LLM_PARSER_KEY, PARSER_REGISTRY, ticket_parser_for
from .parsers.base import ParseCheck, ParsedInvoice
from .parsers.receipt_base import CENTS, ReceiptParser

# Wide enough to read a price off on screen, small enough that a batch of
# thirty receipts doesn't add 50MB to the media folder.
PREVIEW_MAX_WIDTH = 1000
PREVIEW_QUALITY = 82
# What an un-named line is called once its price has been appended. The
# review screen uses it to find lines still waiting for a name.
PLACEHOLDER_MARKER = "EUR/u)"
CHOSEN_SHOP_CHECK = "Enseigne choisie à la main"
UNREAD_CHECK = "Lecture automatique"


class UnrecognisedShopError(ValueError):
    """No known shop's header is on the ticket. Reported, never guessed: the
    operator can name the shop (see receipt_batches.import_with_shop)."""


def receipt_parsers() -> dict[str, ReceiptParser]:
    return {key: parser for key, parser in PARSER_REGISTRY.items() if isinstance(parser, ReceiptParser)}


def parser_for(supplier: Supplier) -> ReceiptParser | None:
    """The ticket reader for `supplier`'s tills, if it has one."""
    return ticket_parser_for(supplier.code)


def shop_choices() -> list[tuple[str, list[Supplier]]]:
    """What a ticket can be filed under by hand, grouped: the shops whose
    tickets are read automatically, then every other supplier, whose tickets
    are typed in from the photo."""
    readable = {parser.supplier_code for parser in receipt_parsers().values()}
    suppliers = list(Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).order_by("name"))
    return [
        ("Tickets lus automatiquement", [supplier for supplier in suppliers if supplier.code in readable]),
        ("Autres fournisseurs : lignes à saisir", [supplier for supplier in suppliers if supplier.code not in readable]),
    ]


def detect_parser(text: str) -> ReceiptParser | None:
    """Which shop's parser this receipt belongs to, from its own header.

    Returns None rather than a best guess: an unrecognised receipt that is
    reported as such costs the operator one click, while one silently run
    through the wrong shop's parser produces plausible lines at wrong prices.
    """
    for parser in receipt_parsers().values():
        for pattern in getattr(parser, "header_patterns", ()):
            if re.search(pattern, text, re.IGNORECASE):
                return parser
    return None


@dataclass
class ReceiptRead:
    """What recognising one photo produced, before anything is stored."""

    parser: ReceiptParser | None
    parsed: ParsedInvoice | None
    preview: bytes | None
    text: str
    # Why a reader the operator chose produced nothing (its exception).
    problem: str = ""


def recognise(pdf_path: str):
    """The photo's pages, deskewed, and what the OCR engine read on each."""
    images = [deskew(image) for image in page_images(pdf_path)]
    return images, [ocr_prepared_image(image) for image in images]


def read_receipt(pdf_path: str, date_hint: date | None = None, supplier: Supplier | None = None) -> ReceiptRead:
    """Recognise a receipt photo and parse it, without touching the database.

    The shop is detected from the ticket's own header, unless `supplier` is
    given: then that shop's reader runs whatever the header says, and a
    reader that fails is reported in `problem` rather than raised - the
    ticket still has to reach the review screen, to be typed in.
    """
    images, ocr_pages = recognise(pdf_path)
    text = "\n".join(page.text for page in ocr_pages)

    parser = detect_parser(text) if supplier is None else parser_for(supplier)
    parsed, problem = None, ""
    if parser is not None:
        try:
            parsed = parser.parse_ocr_pages(
                ocr_pages, date_hint=date_hint, source_name=os.path.basename(pdf_path)
            )
        except Exception as exc:  # noqa: BLE001 - only swallowed for a shop chosen by hand
            if supplier is None:
                raise
            problem = str(exc).strip() or exc.__class__.__name__
    return ReceiptRead(parser=parser, parsed=parsed, preview=_encode_preview(images), text=text, problem=problem)


def _encode_preview(images) -> bytes | None:
    """The first page, deskewed, as a JPEG for the review screen.

    The deskewed image rather than the original on purpose: it is what the
    recogniser actually read, so a line that came out wrong can be compared
    against the pixels that produced it.
    """
    if not images:
        return None
    image = images[0]
    if image.width > PREVIEW_MAX_WIDTH:
        height = round(image.height * PREVIEW_MAX_WIDTH / image.width)
        image = image.resize((PREVIEW_MAX_WIDTH, height))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
    return buffer.getvalue()


def pending_receipts():
    """Receipts a person still has to check: the review queue, unordered.

    Only photographed receipts carry `parse_checks`; a digital invoice never
    enters the queue.
    """
    return Invoice.objects.filter(reviewed_at__isnull=True).exclude(parse_checks=[])


def printed_unit_price(line) -> Decimal:
    """The unit price TTC the till printed, for a parsed line or a stored one.

    From the line's printed amount when it kept one, from its HT figures
    otherwise (a line imported before lines kept it). One formula for both:
    the price list is keyed on this figure, and the import and the review
    screen have to reach it identically, to the cent.
    """
    if line.printed_ttc is not None and line.quantity:
        return (line.printed_ttc / line.quantity).quantize(CENTS)
    return (line.unit_cost_ht * (Decimal("1") + line.vat_rate)).quantize(CENTS)


def label_placeholder_lines(supplier: Supplier, parsed: ParsedInvoice) -> int:
    """Give a name to lines the till printed as "Article divers".

    Looks each line's unit price up in the shop's own price list (see
    models.ShopItemPrice) and rewrites `raw_name` when it finds one. Lines
    with no entry keep the placeholder and carry the price in their name, so
    the review screen shows the operator exactly what to record.

    Deliberately not in the parser: parsers must not touch the database, or
    they stop being testable from hand-written pages (see
    tests/test_parser_contract.py).
    """
    labelled = 0
    for line in parsed.lines:
        if not line.is_placeholder:
            continue
        unit_price_ttc = printed_unit_price(line)
        label = label_for_unit_price(supplier, unit_price_ttc, parsed.invoice_date)
        if label:
            line.raw_name = label
            line.is_placeholder = False
            labelled += 1
        else:
            # Naming it by its price is what makes the unknown line
            # actionable: the operator reads "0,70 EUR" off the screen and
            # records what it was, rather than opening the photo to find out.
            line.raw_name = f"{line.raw_name} ({unit_price_ttc:.2f} EUR/u)"
    return labelled


@dataclass
class PricesApplied:
    lines: int = 0
    receipts: int = 0


def apply_known_prices(invoice: Invoice) -> PricesApplied:
    """Name the "Article divers" lines the shop's price list now answers, on
    `invoice` and on every receipt of the same shop still waiting to be
    checked.

    Called when a price is recorded from `invoice`'s review screen: the same
    unnamed line sits on a dozen tickets of the queue, and recording it once
    has to name it on all of them. Every known price is applied, not only the
    new one, so a ticket imported before a price was recorded is caught up
    too. Each line is looked up as its import would have looked it up, as of
    its own ticket's date - a price valid "à partir du" some day leaves older
    tickets alone.

    A receipt already checked is never touched, other than `invoice` itself
    (reopened through "Corriger les lignes", naming its line is what the
    person is doing): it says what someone confirmed it bought. Only
    placeholder lines are renamed, and only their name - the product follows
    when the ticket is validated, through `replace_invoice_lines`.
    """
    receipt_ids = set(pending_receipts().filter(supplier=invoice.supplier).values_list("pk", flat=True))
    receipt_ids.add(invoice.pk)
    lines = InvoiceLine.objects.filter(
        invoice_id__in=receipt_ids, raw_name__contains=PLACEHOLDER_MARKER
    ).select_related("invoice")

    applied = PricesApplied()
    touched = set()
    with transaction.atomic():
        for line in lines:
            label = label_for_unit_price(invoice.supplier, printed_unit_price(line), line.invoice.invoice_date)
            if not label:
                continue
            line.raw_name = label
            line.save(update_fields=["raw_name"])
            applied.lines += 1
            touched.add(line.invoice_id)
    applied.receipts = len(touched)
    return applied


def rename_product(product, name: str) -> int:
    """Rename one of a shop's products on every ticket that shows it, and
    return how many invoice lines took the new name.

    A receipt's product is named after its first reading ("BAGUETTE BLAND"),
    and that name is what every ticket's review form is filled in with - so
    this is where a misreading is corrected for good. The old spelling stays
    recognised: lines keep what OCR read (`read_as`, and `raw_name` where it
    is the reading), which is what the next ticket is matched against. Lines
    named after the product rather than as read - a name typed on the review
    screen, a price-list name - take the new name, and so does the shop's
    price list, or the next ticket would bring the old name back as a new
    product.

    Raises ValueError, for the operator, when the name is empty or another
    product of the shop already has it: two products of one name would split
    one item's purchases between them.
    """
    if parser_for(product.supplier) is None:
        # Metro's and UBA's own invoices find their products by this exact
        # name, with no reading kept to fall back on.
        raise ValueError(
            f"Les produits {product.supplier.name} gardent le nom de leurs factures : "
            "ils ne se renomment pas depuis un ticket."
        )
    name = " ".join(name.split())
    if not name:
        raise ValueError("Donnez un nom au produit.")
    longest = type(product)._meta.get_field("raw_name").max_length
    if len(name) > longest:
        raise ValueError(f"Nom trop long : {longest} caractères au plus.")
    clash = (
        type(product)
        .objects.filter(supplier=product.supplier, raw_name__iexact=name)
        .exclude(pk=product.pk)
        .first()
    )
    if clash is not None:
        raise ValueError(
            f"Un autre produit {product.supplier.name} s'appelle déjà « {clash.raw_name} » : pour rattacher une "
            "ligne à ce produit, tapez ce nom dans la ligne et validez le ticket."
        )
    old = product.raw_name
    with transaction.atomic():
        product.raw_name = name
        product.save(update_fields=["raw_name"])
        ShopItemPrice.objects.filter(supplier=product.supplier, label=old).update(label=name)
        return product.invoice_lines.filter(raw_name=old).exclude(read_as=old).update(raw_name=name)


def _describe(invoice: Invoice) -> str:
    """Shop, ticket number and date, written the way the rest of the app
    writes them - the batch page shows this beside rows dated 02/06/2026."""
    described = f"{invoice.supplier} n° {invoice.invoice_number or invoice.pk}"
    if invoice.invoice_date:
        described += f" du {invoice.invoice_date:%d/%m/%Y}"
    return described


def import_receipt(
    pdf_path: str,
    display_filename: str | None = None,
    date_hint: date | None = None,
    supplier: Supplier | None = None,
) -> Invoice:
    """Recognise, parse and file one receipt photo.

    Raises UnrecognisedShopError when the shop can't be identified and
    DuplicateInvoiceError when this ticket has already been imported - both
    are reported per file by the batch view rather than aborting the batch.

    With `supplier`, the operator has named the shop: nothing is detected,
    and the ticket is always filed - with no lines, and a failed check saying
    why, when its reader read nothing (or it has none).
    """
    # Before any OCR: a folder scanned again is mostly receipts already in.
    digest = file_sha256(pdf_path)
    known = Invoice.objects.filter(source_sha256=digest).select_related("supplier").first()
    if known is not None:
        raise DuplicateInvoiceError(f"Fichier déjà importé : {_describe(known)}.")

    read = read_receipt(pdf_path, date_hint=date_hint, supplier=supplier)
    if supplier is None:
        if read.parser is None or read.parsed is None:
            raise UnrecognisedShopError(
                "Enseigne non reconnue sur ce ticket "
                f"({', '.join(sorted(receipt_parsers())) or 'aucun parseur'})."
            )
        supplier = Supplier.objects.get(code=read.parser.supplier_code)
        parsed = read.parsed
    else:
        parsed = _chosen_shop_read(supplier, read, date_hint)
    label_placeholder_lines(supplier, parsed)

    invoice = import_parsed_invoice(
        supplier,
        parsed,
        source_file_path=pdf_path,
        display_filename=display_filename or os.path.basename(pdf_path),
    )

    invoice.ocr_text = parsed.source_text
    invoice.ocr_confidence = parsed.confidence
    invoice.parse_checks = [
        {"label": check.label, "passed": check.passed, "detail": check.detail}
        for check in parsed.checks
    ]
    if read.preview:
        invoice.preview_image.save(
            f"{os.path.splitext(os.path.basename(pdf_path))[0]}.jpg",
            ContentFile(read.preview),
            save=False,
        )
    # A receipt whose own arithmetic didn't hold is never COMPLETE, however
    # well its products matched: the products can all be known and the
    # amounts still be misread.
    if invoice.failed_checks:
        invoice.status = Invoice.Status.NEEDS_REVIEW
    invoice.source_sha256 = digest
    invoice.save(
        update_fields=["ocr_text", "ocr_confidence", "parse_checks", "preview_image", "status", "source_sha256"]
    )
    return invoice


def _chosen_shop_read(supplier: Supplier, read: ReceiptRead, date_hint: date | None) -> ParsedInvoice:
    """What gets filed for a ticket whose shop the operator named.

    Whatever the reader made of it, plus a check saying the shop was chosen
    by hand - it is also what puts a ticket with no reader in the review
    queue, which only lists receipts with checks.
    """
    parsed = read.parsed
    if parsed is None:
        parsed = ParsedInvoice(
            supplier_code=supplier.code,
            invoice_number="",
            invoice_date=date_hint,
            source_text=read.text,
            from_ocr=True,
        )
    if not parsed.lines:
        if read.parser is None:
            reason = f"Les tickets {supplier.name} ne sont pas lus automatiquement"
        elif read.problem:
            reason = f"La lecture du ticket a échoué ({read.problem})"
        else:
            reason = "Aucune ligne lue sur le ticket"
        parsed.checks.append(
            ParseCheck(label=UNREAD_CHECK, passed=False, detail=f"{reason} : saisissez les lignes d'après la photo.")
        )
    parsed.checks.append(
        ParseCheck(label=CHOSEN_SHOP_CHECK, passed=True, detail="L'en-tête du ticket n'a pas été reconnu.")
    )
    return parsed


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "PLACEHOLDER_MARKER",
    "DuplicateInvoiceError",
    "PricesApplied",
    "ReceiptRead",
    "UnrecognisedShopError",
    "apply_known_prices",
    "detect_parser",
    "import_receipt",
    "label_placeholder_lines",
    "parser_for",
    "pending_receipts",
    "printed_unit_price",
    "read_receipt",
    "receipt_parsers",
    "recognise",
    "rename_product",
    "shop_choices",
]
