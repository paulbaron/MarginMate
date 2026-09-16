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

from .importing import DuplicateInvoiceError, import_parsed_invoice
from .models import Invoice, Supplier, label_for_unit_price
from .ocr import deskew, ocr_prepared_image, page_images
from .parsers import PARSER_REGISTRY
from .parsers.base import ParsedInvoice
from .parsers.receipt_base import CENTS, ReceiptParser

# Wide enough to read a price off on screen, small enough that a batch of
# thirty receipts doesn't add 50MB to the media folder.
PREVIEW_MAX_WIDTH = 1000
PREVIEW_QUALITY = 82
# What an un-named line is called once its price has been appended. The
# review screen uses it to find lines still waiting for a name.
PLACEHOLDER_MARKER = "EUR/u)"


def receipt_parsers() -> dict[str, ReceiptParser]:
    return {key: parser for key, parser in PARSER_REGISTRY.items() if isinstance(parser, ReceiptParser)}


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


def read_receipt(pdf_path: str, date_hint: date | None = None) -> ReceiptRead:
    """Recognise a receipt photo and parse it, without touching the database."""
    images = [deskew(image) for image in page_images(pdf_path)]
    ocr_pages = [ocr_prepared_image(image) for image in images]
    text = "\n".join(page.text for page in ocr_pages)

    parser = detect_parser(text)
    parsed = None
    if parser is not None:
        parsed = parser.parse_ocr_pages(
            ocr_pages, date_hint=date_hint, source_name=os.path.basename(pdf_path)
        )
    return ReceiptRead(parser=parser, parsed=parsed, preview=_encode_preview(images), text=text)


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
        unit_price_ttc = (line.unit_cost_ht * (Decimal("1") + line.vat_rate)).quantize(CENTS)
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


def _describe(invoice: Invoice) -> str:
    """Shop, ticket number and date, written the way the rest of the app
    writes them - the batch page shows this beside rows dated 02/06/2026."""
    described = f"{invoice.supplier} n° {invoice.invoice_number or invoice.pk}"
    if invoice.invoice_date:
        described += f" du {invoice.invoice_date:%d/%m/%Y}"
    return described


def import_receipt(
    pdf_path: str, display_filename: str | None = None, date_hint: date | None = None
) -> Invoice:
    """Recognise, parse and file one receipt photo.

    Raises ValueError when the shop can't be identified and
    DuplicateInvoiceError when this ticket has already been imported - both
    are reported per file by the batch view rather than aborting the batch.
    """
    # Before any OCR: a folder scanned again is mostly receipts already in.
    digest = file_sha256(pdf_path)
    known = Invoice.objects.filter(source_sha256=digest).select_related("supplier").first()
    if known is not None:
        raise DuplicateInvoiceError(f"Fichier déjà importé : {_describe(known)}.")

    read = read_receipt(pdf_path, date_hint=date_hint)
    if read.parser is None or read.parsed is None:
        raise ValueError(
            "Enseigne non reconnue sur ce ticket "
            f"({', '.join(sorted(receipt_parsers())) or 'aucun parseur'})."
        )

    supplier = Supplier.objects.get(code=read.parser.supplier_code)
    label_placeholder_lines(supplier, read.parsed)

    invoice = import_parsed_invoice(
        supplier,
        read.parsed,
        source_file_path=pdf_path,
        display_filename=display_filename or os.path.basename(pdf_path),
    )

    invoice.ocr_text = read.parsed.source_text
    invoice.ocr_confidence = read.parsed.confidence
    invoice.parse_checks = [
        {"label": check.label, "passed": check.passed, "detail": check.detail}
        for check in read.parsed.checks
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


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "PLACEHOLDER_MARKER",
    "DuplicateInvoiceError",
    "ReceiptRead",
    "detect_parser",
    "import_receipt",
    "label_placeholder_lines",
    "read_receipt",
    "receipt_parsers",
]
