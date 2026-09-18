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
import threading
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .identifiers import describe as describe_identifier
from .identifiers import document_identifiers, may_print
from .importing import DuplicateInvoiceError, import_parsed_invoice
from .models import Invoice, InvoiceLine, ShopItemPrice, Supplier, label_for_unit_price
from .ocr import (
    deskew,
    document_text,
    ocr_prepared_image,
    page_images,
    text_layer_pages,
)
from .parsers import (
    LLM_PARSER_KEY,
    PARSER_REGISTRY,
    get_parser,
    is_ticket_shop,
    ticket_parser_for,
)
from .parsers.base import ParseCheck, ParsedInvoice
from .parsers.generic_receipt import GenericReceiptParser, TicketShop
from .parsers.receipt_base import (
    CENTS,
    RECONCILIATION_TOLERANCE,
    ReceiptParser,
    line_amounts,
)

# Wide enough to read a price off on screen, small enough that a batch of
# thirty receipts doesn't add 50MB to the media folder.
PREVIEW_MAX_WIDTH = 1000
PREVIEW_QUALITY = 82
# What an un-named line is called once its price has been appended. The
# review screen uses it to find lines still waiting for a name.
PLACEHOLDER_MARKER = "EUR/u)"
CHOSEN_SHOP_CHECK = "Enseigne choisie à la main"
IDENTIFIED_CHECK = "Enseigne reconnue"
UNREAD_CHECK = "Lecture automatique"
SUM_CHECK = "Somme des lignes = total imprimé"
UNREAD_TOTAL_CHECK = "Total imprimé lu"
DATE_CHECK = "Date du ticket"
# One recognition at a time in a request (a shop chosen by hand, a document
# read again): each is seconds of CPU, and two tabs used to import one file
# twice.
OCR_LOCK = threading.Lock()
OCR_WAIT_SECONDS = 120
# A shop's own header text shorter than this would find itself on any ticket.
MIN_HEADER_LENGTH = 4
# A new shop's header already printed on more tickets filed elsewhere than
# this - or on tickets of two shops - is that shop's, or anybody's.
MAX_TICKETS_ELSEWHERE = 3
# "Label : value" - a field of the document, never its sender's name.
FIELD_RE = re.compile(r"^[^:]{2,40}:\s*\S")
DATE_OR_TIME_RE = re.compile(r"(?<!\d)(?:\d{2}[/.-]\d{2}[/.-]\d{2,4}|\d{1,2}\s?[:Hh]\s?\d{2})(?!\d)")
# How far down a document its shop's own name can be, and how many of its
# lines are offered as its header.
HEADER_LINES_READ = 10
MAX_HEADER_CHOICES = 6
# A shop's name or street, not a sentence: longer than this, a line is cut.
MAX_HEADER_LENGTH = 40
# The share of a shop's documents that print an identifier for it to name the
# shop (learn_identifiers).
MIN_IDENTIFIER_SHARE = Decimal("0.25")
# What the parser said about the lines it read. Once a person has corrected
# the lines, these describe lines that no longer exist: they give way to one
# check on the lines as they are now (lines_check).
READING_CHECKS = {
    SUM_CHECK,
    UNREAD_CHECK,
    "Somme HT des lignes = base HT du ticket",
    "Articles = total avant remise",
    "Montants recalculés",
    "Poids rattachés",
    "Poids x prix au kilo = montant",
    "Remise attribuée",
    "Quantités recalculées",
    "Quantité x prix unitaire = total",
    "Taux par article",
    "Taux applicable",
    "Lignes écartées",
    # The page refuses a document with no valid date.
    DATE_CHECK,
}


class RereadError(Exception):
    """A document could not be read again; nothing was changed. The message
    is for the operator."""


def date_check(invoice_date: date | None) -> ParseCheck | None:
    """A failed check when the ticket gave no usable date - none, or one
    outside 2000-today (a misread year): the ticket goes to review, where the
    date is required."""
    from .forms import EARLIEST_DOCUMENT_DATE

    if invoice_date is None:
        return ParseCheck(label=DATE_CHECK, passed=False, detail="Aucune date lisible : saisissez-la d'après la photo.")
    if not EARLIEST_DOCUMENT_DATE <= invoice_date <= timezone.localdate():
        return ParseCheck(
            label=DATE_CHECK,
            passed=False,
            detail=f"Date lue impossible ({invoice_date:%d/%m/%Y}) : corrigez-la d'après la photo.",
        )
    return None


class UnrecognisedShopError(ValueError):
    """No known shop's header is on the ticket. Reported, never guessed: the
    operator can name the shop (see receipt_batches.import_with_shop).
    `text` is what the ticket was read as, to show while choosing."""

    def __init__(self, message: str, text: str = ""):
        super().__init__(message)
        self.text = text


def receipt_parsers() -> dict[str, ReceiptParser]:
    return {key: parser for key, parser in PARSER_REGISTRY.items() if isinstance(parser, ReceiptParser)}


def parser_for(supplier: Supplier) -> ReceiptParser | None:
    """The reader for `supplier`'s tickets: its till's own settings when it
    has some, and the same reader without them for any other supplier - a
    shop added from a ticket, a Metro paper ticket. None only for the AI
    pseudo-supplier, under which nothing is filed."""
    configured = ticket_parser_for(supplier.code)
    if configured is not None:
        return configured
    if supplier.parser_key == LLM_PARSER_KEY:
        return None
    return GenericReceiptParser(TicketShop(supplier.code, (), supplier.name))


def shop_choices() -> list[tuple[str, list[Supplier]]]:
    """What a ticket can be filed under by hand, grouped: the shops, then the
    suppliers whose invoices are PDFs (a paper ticket of theirs). Every one
    of them has its tickets read."""
    suppliers = list(Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).order_by("name"))
    return [
        ("Magasins", [supplier for supplier in suppliers if is_ticket_shop(supplier)]),
        ("Fournisseurs à factures (ticket papier)", [supplier for supplier in suppliers if not is_ticket_shop(supplier)]),
    ]


def invoice_supplier_choices() -> list[tuple[str, list[Supplier]]]:
    """What a PDF invoice can be imported under, grouped by how it is read."""
    suppliers = list(Supplier.objects.order_by("name"))
    return [
        ("Lecteur dédié", [supplier for supplier in suppliers if has_own_reader(supplier)]),
        (
            "Lue comme un ticket",
            [supplier for supplier in suppliers if supplier.parser_key != LLM_PARSER_KEY and not has_own_reader(supplier)],
        ),
        ("Analyse IA", [supplier for supplier in suppliers if supplier.parser_key == LLM_PARSER_KEY]),
    ]


def plain_text(text: str) -> str:
    """`text` for comparing headers: capitals, no accents, words and figures
    separated by single spaces - "Épicerie  Sabah," is "EPICERIE SABAH"."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()
    return " ".join(re.findall(r"[A-Z0-9]+", folded))


def _has_header(plain: str, header: str) -> bool:
    return len(header) >= MIN_HEADER_LENGTH and f" {header} " in f" {plain} "


def detect_parser(text: str) -> ReceiptParser | None:
    return detect_shop(text)[0]


def detect_shop(text: str) -> tuple[ReceiptParser | None, list[str]]:
    """Which shop this receipt belongs to, from what it prints - and, when
    that was not a header, the identifiers that said so.

    A header a person gave a shop first, the longest first - "EPICERIE SABAH"
    before the "SABAH" a configured till answers to - then the configured
    tills, then the SIREN, phone or web site learned from the shop's tickets
    (`identified_supplier`). Returns None rather than a best guess: an
    unrecognised receipt that is reported as such costs the operator one
    click, while one filed under the wrong shop produces plausible lines under
    the wrong products.
    """
    plain = plain_text(text)
    named = [
        (plain_text(supplier.ticket_header), supplier)
        for supplier in Supplier.objects.exclude(ticket_header="").exclude(parser_key=LLM_PARSER_KEY)
    ]
    for header, supplier in sorted(named, key=lambda pair: -len(pair[0])):
        if _has_header(plain, header):
            return parser_for(supplier), []
    for parser in receipt_parsers().values():
        for pattern in getattr(parser, "header_patterns", ()):
            if re.search(pattern, text, re.IGNORECASE):
                return parser, []
    supplier, identifiers = identified_supplier(text)
    if supplier is None:
        return None, []
    return parser_for(supplier), identifiers


def identified_supplier(text: str) -> tuple[Supplier | None, list[str]]:
    """The one supplier whose learned identifiers `text` prints, and those
    identifiers. One learned by two suppliers names neither; identifiers
    naming two suppliers name no one; a web site alone names no one - the
    one that brands the goods ("fsc.org" on wood) is printed at every shop
    selling them."""
    printed = document_identifiers(text)
    if not printed:
        return None, []
    owners = defaultdict(list)
    for supplier in Supplier.objects.exclude(parser_key=LLM_PARSER_KEY):
        for identifier in printed.intersection(supplier.ticket_identifiers or ()):
            owners[identifier].append(supplier)
    named = {identifier: suppliers[0] for identifier, suppliers in owners.items() if len(suppliers) == 1}
    if len({supplier.pk for supplier in named.values()}) != 1:
        return None, []
    if all(identifier.startswith("web:") for identifier in named):
        return None, []
    # A company number printed that belongs to nobody known: whoever sent this
    # document, it is not the supplier whose phone or web site it also prints.
    # Seven Free invoices went to UBA on a mobile number both print - the
    # customer's own - while naming Free's SIREN, which named nobody.
    unknown_company = {
        identifier for identifier in printed if identifier.startswith("siren:") and identifier not in owners
    }
    if unknown_company and not any(identifier.startswith("siren:") for identifier in named):
        return None, []
    return next(iter(named.values())), sorted(named)


def learn_identifiers(supplier: Supplier, *texts: str) -> list[str]:
    """Bring what names `supplier` up to date with `texts` - documents a
    person filed or checked under it - and return what it learned.

    An identifier names the shop when at least MIN_IDENTIFIER_SHARE of its
    documents print it - a misreading, or a label printed on some goods, is
    on one ticket or two - and no other supplier's documents do: the
    customer's own phone names nobody. Checked again for those it knew.
    """
    if supplier.parser_key == LLM_PARSER_KEY:
        return []
    # As stored now: another ticket of the shop may have taught it meanwhile.
    supplier.refresh_from_db(fields=["ticket_identifiers"])
    known = set(supplier.ticket_identifiers or ())
    candidates = set(known)
    for text in texts:
        candidates |= document_identifiers(text)
    if not candidates:
        return []
    own = _stored_texts(Invoice.objects.filter(supplier=supplier))
    own += [text for text in texts if text and text not in own]
    seen = Counter()
    for document in own:
        if may_print(document, candidates):
            seen.update(document_identifiers(document) & candidates)
    kept = {identifier for identifier in candidates if seen[identifier] >= MIN_IDENTIFIER_SHARE * len(own)}
    for other in _stored_texts(Invoice.objects.exclude(supplier=supplier)):
        if not kept:
            break
        if may_print(other, kept):
            kept -= document_identifiers(other)
    if kept != known:
        supplier.ticket_identifiers = sorted(kept)
        supplier.save(update_fields=["ticket_identifiers"])
    return sorted(kept - known)


def _stored_texts(invoices) -> list[str]:
    """What the documents of `invoices` say: a receipt's reading, a digital
    one's own text."""
    return [
        ocr_text or source_text
        for ocr_text, source_text in invoices.values_list("ocr_text", "source_text")
        if ocr_text or source_text
    ]


def forget_identifiers(supplier: Supplier, text: str) -> None:
    """A document printing them is not `supplier`'s after all: they name it
    no longer."""
    printed = document_identifiers(text)
    supplier.refresh_from_db(fields=["ticket_identifiers"])
    kept = [identifier for identifier in supplier.ticket_identifiers or () if identifier not in printed]
    if len(kept) != len(supplier.ticket_identifiers or ()):
        supplier.ticket_identifiers = kept
        supplier.save(update_fields=["ticket_identifiers"])


def header_guess(text: str) -> str:
    """What a ticket seems to print as its shop's name - the first of its
    top lines made of words rather than figures - to suggest when naming a
    new shop. Blank when nothing looks like one."""
    for line in [line.strip() for line in text.splitlines() if line.strip()][:6]:
        if FIELD_RE.match(line):
            continue  # "Statut : COMPLETE" is a field, not a name
        letters = sum(char.isalpha() for char in line)
        visible = len(line.replace(" ", ""))
        if letters >= MIN_HEADER_LENGTH and not any(char.isdigit() for char in line) and letters >= 0.7 * visible:
            return " ".join(line.split())[:60]
    return ""


def header_choices(text: str) -> list[str]:
    """The lines of a document that could be the text its shop prints at the
    top: its first lines made of words - a name, a street - with no amount,
    date or field among them. Offered on the review screen, where the
    document is on show."""
    choices: list[str] = []
    for line in [line.strip() for line in text.splitlines() if line.strip()][:HEADER_LINES_READ]:
        line = " ".join(line.split())[:MAX_HEADER_LENGTH]
        letters = sum(char.isalpha() for char in line)
        visible = len(line.replace(" ", ""))
        if letters < MIN_HEADER_LENGTH or letters < 0.5 * visible:
            continue
        # "No de Commande :" is the label of a field whose value is elsewhere.
        if FIELD_RE.match(line) or line.endswith(":") or line_amounts(line) or DATE_OR_TIME_RE.search(line):
            continue
        if line not in choices:
            choices.append(line)
    return choices[:MAX_HEADER_CHOICES]


def names_shop(supplier: Supplier) -> list[str]:
    """What files a document under `supplier` on its own, as the operator
    reads it: the text it prints at the top, then what its figures say."""
    found = []
    if supplier.ticket_header:
        found.append(f"en-tête « {supplier.ticket_header} »")
    found += [describe_identifier(identifier) for identifier in supplier.ticket_identifiers or ()]
    return found


def first_reading(text: str) -> dict:
    """The date and total a ticket reads as, before any shop is known -
    shown beside a ticket waiting for its shop, to tell it from the others."""
    try:
        parsed = GenericReceiptParser(TicketShop("", (), "")).parse_text(text)
    except Exception:  # noqa: BLE001 - only a hint
        return {}
    return {
        "read_date": f"{parsed.invoice_date:%d/%m/%Y}" if parsed.invoice_date else "",
        "read_total": f"{parsed.printed_total_ttc:.2f}" if parsed.printed_total_ttc is not None else "",
        "header": header_guess(text),
    }


def tickets_printing(header: str, ignoring=()) -> list[Invoice]:
    """The documents already filed whose text carries `header`, oldest
    first - a ticket as it was read, a digital one as it prints."""
    plain = plain_text(header)
    if len(plain) < MIN_HEADER_LENGTH:
        return []
    ignored = {invoice.pk for invoice in ignoring}
    found = [
        pk
        for pk, ocr_text, source_text in Invoice.objects.values_list("pk", "ocr_text", "source_text")
        if pk not in ignored and (ocr_text or source_text) and _has_header(plain_text(ocr_text or source_text), plain)
    ]
    return list(Invoice.objects.filter(pk__in=found).select_related("supplier").order_by("invoice_date", "pk"))


def describe_tickets(tickets) -> str:
    return ", ".join(
        f"{ticket.supplier.name} du {ticket.invoice_date:%d/%m/%Y}" if ticket.invoice_date else f"{ticket.supplier.name} n° {ticket.pk}"
        for ticket in tickets
    )


def check_header(header: str, ignoring=(), shop: Supplier | None = None) -> str:
    """`header` as it will be stored. Raises ValueError, for the operator: a
    text too short to tell a shop, or one already printed on the tickets of
    other shops, or of many, which it would take from them. A few tickets of
    one shop carrying it are more likely this shop's, filed there before it
    existed (tickets_printing says which; `ignoring` is the ticket being
    moved, `shop` the shop the header is being given to - its own tickets
    print it, of course)."""
    header = " ".join(header.split())
    if header and len(plain_text(header)) < MIN_HEADER_LENGTH:
        raise ValueError(
            f"Le texte d'en-tête « {header} » est trop court pour reconnaître des tickets "
            f"({MIN_HEADER_LENGTH} caractères au moins)."
        )
    elsewhere = [
        ticket for ticket in tickets_printing(header, ignoring) if shop is None or ticket.supplier_id != shop.pk
    ]
    if len(elsewhere) > MAX_TICKETS_ELSEWHERE or len({ticket.supplier_id for ticket in elsewhere}) > 1:
        raise ValueError(
            f"« {header} » est imprimé sur {len(elsewhere)} tickets d'autres enseignes "
            f"({describe_tickets(elsewhere[:5])}{'…' if len(elsewhere) > 5 else ''}) : "
            "choisissez un texte propre à cette enseigne (son nom, sa rue)."
        )
    return header


def set_shop_header(supplier: Supplier, header: str, ignoring=()) -> str:
    """Give `supplier` the text its documents print at the top, so the next
    ones are filed there on their own - or take it back, with a blank. Same
    refusals as `create_shop`; returns the header as stored."""
    header = check_header(header, ignoring, shop=supplier)
    supplier.ticket_header = header
    supplier.save(update_fields=["ticket_header"])
    return header


def create_shop(name: str, header: str = "", ignoring=()) -> Supplier:
    """A new shop, for tickets no known header was on. With `header`, its
    next tickets are recognised by it. Raises ValueError, for the operator:
    no name, a name taken, a header too short - or one printed on the tickets
    of other shops, or of many, which it would take from them. A few tickets
    of one shop carrying it are more likely this shop's, filed there before
    it existed (tickets_printing says which; `ignoring` is the one being
    moved)."""
    name = " ".join(name.split())
    header = " ".join(header.split())
    if not name:
        raise ValueError("Donnez un nom à la nouvelle enseigne.")
    if Supplier.objects.filter(name__iexact=name).exists():
        raise ValueError(f"« {name} » existe déjà : choisissez-la dans la liste.")
    header = check_header(header, ignoring)
    base = re.sub(r"[^A-Z0-9]+", "_", plain_text(name)).strip("_")[:24] or "ENSEIGNE"
    code, suffix = base, 1
    while Supplier.objects.filter(code=code).exists():
        suffix += 1
        code = f"{base}_{suffix}"
    return Supplier.objects.create(code=code, name=name, parser_key="", ticket_header=header)


def move_to_shop(invoice: Invoice, supplier: Supplier) -> None:
    """File a document under another supplier - a ticket or a digital
    invoice: its lines stay as they are and find their products among the new
    supplier's (the old ones nobody else uses go). What named the one it
    leaves named it wrongly, so that supplier forgets it; the new one learns
    what the document prints. Raises ValueError when that supplier has a
    document of the same number, and InvoiceLinesInUseError as a correction
    would."""
    from .importing import corrected_line, replace_invoice_lines

    if supplier.pk == invoice.supplier_id:
        return
    if supplier.parser_key == LLM_PARSER_KEY:
        raise ValueError("Un document ne se range pas sous ce fournisseur.")
    if invoice.invoice_number and (
        Invoice.objects.filter(supplier=supplier, invoice_number=invoice.invoice_number).exclude(pk=invoice.pk).exists()
    ):
        raise ValueError(f"{supplier.name} a déjà un document n° {invoice.invoice_number} : c'est peut-être le même.")
    lines = [
        corrected_line(line, raw_name=line.raw_name, quantity=line.quantity, total_ht=line.total_ht, vat_rate=line.vat_rate)
        for line in invoice.lines.all()
    ]
    text = invoice.document_text
    fields = ["supplier"]
    with transaction.atomic():
        forget_identifiers(invoice.supplier, text)
        invoice.supplier = supplier
        if invoice.is_receipt:
            # A check is what a ticket's review screen reads; adding one to a
            # digital invoice would turn it into a ticket (Invoice.is_receipt).
            invoice.parse_checks = [
                check for check in invoice.parse_checks if check["label"] not in (CHOSEN_SHOP_CHECK, IDENTIFIED_CHECK)
            ] + [{"label": CHOSEN_SHOP_CHECK, "passed": True, "detail": f"Rangé chez {supplier.name} à la main."}]
            fields.append("parse_checks")
        invoice.save(update_fields=fields)
        replace_invoice_lines(invoice, lines)
        learn_identifiers(supplier, text)


@dataclass
class ReceiptRead:
    """What recognising one photo produced, before anything is stored."""

    parser: ReceiptParser | None
    parsed: ParsedInvoice | None
    preview: bytes | None
    text: str
    # Why a reader the operator chose produced nothing (its exception).
    problem: str = ""
    # What named the shop, when no header did (identified_supplier).
    identified_by: list[str] = field(default_factory=list)


def recognise(pdf_path: str):
    """The document's pages as images, and what each says: a PDF page's own
    text when it carries some, what the OCR engine read on the deskewed
    photo otherwise."""
    layers = text_layer_pages(pdf_path)
    images, pages = [], []
    for position, image in enumerate(page_images(pdf_path)):
        layer = layers[position] if position < len(layers) else None
        if layer is not None:
            images.append(image)
            pages.append(layer)
            continue
        image = deskew(image)
        images.append(image)
        pages.append(ocr_prepared_image(image))
    return images, pages


def read_receipt(pdf_path: str, date_hint: date | None = None, supplier: Supplier | None = None) -> ReceiptRead:
    """Recognise a receipt photo and parse it, without touching the database.

    The shop is detected from the ticket's own header, unless `supplier` is
    given: then that shop's reader runs whatever the header says, and a
    reader that fails is reported in `problem` rather than raised - the
    ticket still has to reach the review screen, to be typed in.
    """
    images, ocr_pages = recognise(pdf_path)
    text = "\n".join(page.text for page in ocr_pages)

    parser, identified_by = detect_shop(text) if supplier is None else (parser_for(supplier), [])
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
    return ReceiptRead(
        parser=parser, parsed=parsed, preview=_encode_preview(images), text=text, problem=problem,
        identified_by=identified_by,
    )


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
    enters the queue. Nor does a charge (Supplier.expenses_only): there is
    nothing to type on a rent - what it charges is filed, and a total that
    could not be read holds the document in "À vérifier" with what is wrong
    written on it. Forty-two rents and water bills queued behind the tickets
    is a queue nobody works through.
    """
    return (
        Invoice.objects.filter(reviewed_at__isnull=True)
        .exclude(parse_checks=[])
        .exclude(supplier__expenses_only=True)
    )


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
    if not is_ticket_shop(product.supplier):
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


def lines_check(invoice: Invoice, prefix: str = "") -> dict:
    """The lines as they stand against the total the ticket printed - with the
    parser's own tolerance, the one Invoice.total_ttc trusts. Each line counts
    as the review screen shows it, to the cent."""
    lines = list(invoice.lines.all())
    lines_total = sum((line.total_ttc.quantize(CENTS, rounding=ROUND_HALF_UP) for line in lines), start=Decimal("0"))
    discounts = sum((line.discount_ttc for line in lines if line.printed_ttc is not None), start=Decimal("0"))
    # The promotions apart, as the page shows them: the articles are what the
    # ticket prints as its total before promotions.
    promotions = (
        f" - articles {lines_total + discounts:.2f} € moins {discounts:.2f} € de remises" if discounts else ""
    )
    paid = invoice.printed_total_ttc
    if paid is None:
        return {
            "label": SUM_CHECK,
            "passed": False,
            "detail": f"{prefix}lignes {lines_total:.2f} € : saisissez le total pour les vérifier{promotions}",
        }
    gap = paid - lines_total
    return {
        "label": SUM_CHECK,
        "passed": abs(gap) <= RECONCILIATION_TOLERANCE,
        "detail": f"{prefix}lignes {lines_total:.2f} € / ticket {paid:.2f} € (écart {gap:+.2f} €){promotions}",
    }


def recheck_after_review(invoice: Invoice) -> None:
    """Replace what the parser said about the lines with the check on the
    lines a person validated - or the review screen keeps showing "lignes
    3.85 € / ticket 9.03 €" about lines corrected long ago. What was read of
    the VAT table stays: nobody retyped that. Not saved here."""
    dropped = set(READING_CHECKS)
    if invoice.printed_total_ttc is not None:
        dropped.add(UNREAD_TOTAL_CHECK)
    invoice.parse_checks = [check for check in invoice.parse_checks if check["label"] not in dropped] + [
        lines_check(invoice, prefix="vérifié à la main : ")
    ]


def _sum_check_passed(checks) -> bool:
    return any(check["label"] == SUM_CHECK and check["passed"] for check in checks)


def _failures(checks) -> int:
    return sum(1 for check in checks if not check["passed"])


def reread_receipt(invoice: Invoice) -> bool:
    """Read a ticket still waiting to be checked again, from its stored
    reading, with today's parser - keeping the new lines only if they add up
    to the printed total and fail fewer checks than the old ones (a sum can
    pass for the wrong reason: a ticket whose total was misread passed with
    the rest booked as a "promotion", its other checks failing). Never a
    checked ticket: its lines are what a person confirmed. Returns whether it
    changed.
    """
    from .importing import InvoiceLinesInUseError, refile_as_charge, replace_invoice_lines

    if invoice.reviewed_at is not None or not invoice.ocr_text:
        return False
    charge = invoice.supplier.expenses_only
    if not charge and not _failures(invoice.parse_checks):
        return False
    parser = parser_for(invoice.supplier)
    if parser is None:
        return False
    try:
        parsed = parser.parse_text(invoice.ocr_text)
    except Exception:  # noqa: BLE001 - a reading today's parser can't handle stays as it was
        return False
    if charge:
        # Not the lines a ticket reader makes of it: what it charges.
        return refile_as_charge(invoice, parsed)
    dated = date_check(parsed.invoice_date or invoice.invoice_date)
    checks = [_as_dict(check) for check in parsed.checks + ([dated] if dated else [])]
    if not parsed.lines or not _sum_check_passed(checks) or _failures(checks) >= _failures(invoice.parse_checks):
        return False
    label_placeholder_lines(invoice.supplier, parsed)
    chosen = [check for check in invoice.parse_checks if check["label"] == CHOSEN_SHOP_CHECK]
    try:
        with transaction.atomic():
            replace_invoice_lines(invoice, parsed.lines)
            invoice.reconciliation_adjustment = parsed.reconciliation_adjustment
            invoice.printed_total_ttc = parsed.printed_total_ttc
            invoice.invoice_date = invoice.invoice_date or parsed.invoice_date
            invoice.parse_checks = checks + chosen
            if invoice.failed_checks:
                invoice.status = Invoice.Status.NEEDS_REVIEW
            invoice.save(
                update_fields=[
                    "reconciliation_adjustment", "printed_total_ttc", "invoice_date", "parse_checks", "status",
                ]
            )
    except InvoiceLinesInUseError:
        return False
    return True


def _as_dict(check: ParseCheck) -> dict:
    return {"label": check.label, "passed": check.passed, "detail": check.detail}


def reread_document(invoice: Invoice) -> str:
    """Read a document's file again from scratch and put what it says in
    place of its lines, date and total - a person's corrections included.
    A ticket goes back to the review queue. Returns what to tell the
    operator; raises RereadError, having changed nothing, when there is
    nothing to read or nothing was read, and InvoiceLinesInUseError when a
    stock take was priced from a line the reading drops."""
    if not invoice.source_file:
        raise RereadError("Aucun fichier d'origine n'est enregistré pour ce document : rien à relire.")
    try:
        path = invoice.source_file.path
    except (NotImplementedError, ValueError):
        path = ""
    if not path or not os.path.exists(path):
        raise RereadError("Le fichier d'origine de ce document est introuvable : rien à relire.")
    if invoice.is_receipt or isinstance(get_parser(invoice.supplier.parser_key), ReceiptParser):
        return _reread_receipt_file(invoice, path)
    return _reread_invoice_file(invoice, path)


def _reread_receipt_file(invoice: Invoice, path: str) -> str:
    from .importing import replace_invoice_lines

    supplier = invoice.supplier
    if parser_for(supplier) is None:
        raise RereadError(f"Les tickets {supplier.name} ne sont pas lus automatiquement : rien à relire.")
    if not OCR_LOCK.acquire(timeout=OCR_WAIT_SECONDS):
        raise RereadError("Un autre ticket est en cours de lecture : réessayez dans un instant.")
    try:
        read = read_receipt(path, supplier=supplier)
    finally:
        OCR_LOCK.release()
    parsed = read.parsed
    if parsed is None or not parsed.lines:
        reason = f" ({read.problem})" if read.problem else ""
        raise RereadError(f"La relecture n'a trouvé aucune ligne{reason} : le ticket n'a pas été modifié.")
    if supplier.expenses_only:
        from .importing import refile_as_charge

        invoice.ocr_text = parsed.source_text
        invoice.invoice_date = parsed.invoice_date or invoice.invoice_date
        invoice.save(update_fields=["ocr_text", "invoice_date"])
        refile_as_charge(invoice, parsed)
        return f"Document relu : {invoice.lines.count()} poste(s) de charge."
    label_placeholder_lines(supplier, parsed)
    invoice_date = parsed.invoice_date or invoice.invoice_date
    dated = date_check(invoice_date)
    chosen = [check for check in invoice.parse_checks if check["label"] == CHOSEN_SHOP_CHECK]
    with transaction.atomic():
        replace_invoice_lines(invoice, parsed.lines)
        invoice.invoice_date = invoice_date
        invoice.printed_total_ttc = parsed.printed_total_ttc
        invoice.reconciliation_adjustment = parsed.reconciliation_adjustment
        invoice.ocr_text = parsed.source_text
        invoice.ocr_confidence = parsed.confidence
        invoice.parse_checks = [_as_dict(check) for check in parsed.checks + ([dated] if dated else [])] + chosen
        invoice.reviewed_at = None
        if invoice.failed_checks:
            invoice.status = Invoice.Status.NEEDS_REVIEW
        if read.preview:
            if invoice.preview_image:
                invoice.preview_image.delete(save=False)
            invoice.preview_image.save(
                f"{os.path.splitext(os.path.basename(path))[0]}.jpg", ContentFile(read.preview), save=False
            )
        invoice.save()
    return f"Ticket relu : {len(parsed.lines)} ligne(s), à vérifier de nouveau."


def _reread_invoice_file(invoice: Invoice, path: str) -> str:
    from .importing import replace_invoice_lines

    parser = get_parser(invoice.supplier.parser_key)
    if parser is None or parser.supplier_code == LLM_PARSER_KEY:
        raise RereadError(f"Les factures {invoice.supplier.name} ne sont pas lues automatiquement : rien à relire.")
    try:
        parsed = parser.parse(path, date_hint=invoice.invoice_date)
    except Exception as exc:  # noqa: BLE001 - said to the operator; nothing changed
        raise RereadError(f"La relecture a échoué ({str(exc).strip() or exc.__class__.__name__}) : rien n'a été modifié.")
    if not parsed.lines:
        raise RereadError("La relecture n'a trouvé aucune ligne : la facture n'a pas été modifiée.")
    if invoice.supplier.expenses_only:
        from .importing import refile_as_charge

        refile_as_charge(invoice, parsed)
        return f"Facture relue : {invoice.lines.count()} poste(s) de charge."
    with transaction.atomic():
        replace_invoice_lines(invoice, parsed.lines)
        invoice.invoice_date = parsed.invoice_date or invoice.invoice_date
        invoice.reconciliation_adjustment = parsed.reconciliation_adjustment
        if parsed.printed_total_ttc is not None:
            invoice.printed_total_ttc = parsed.printed_total_ttc
        invoice.error_message = " ".join(parsed.warnings)
        if invoice.error_message:
            invoice.status = Invoice.Status.NEEDS_REVIEW
        invoice.save()
    return f"Facture relue : {len(parsed.lines)} ligne(s)."


def _describe(invoice: Invoice) -> str:
    """Shop, ticket number and date, written the way the rest of the app
    writes them - the batch page shows this beside rows dated 02/06/2026."""
    described = f"{invoice.supplier} n° {invoice.invoice_number or invoice.pk}"
    if invoice.invoice_date:
        described += f" du {invoice.invoice_date:%d/%m/%Y}"
    return described


def has_own_reader(supplier: Supplier) -> bool:
    """Whether `supplier`'s PDF invoices have a reader of their own (Metro,
    UBA...) - any other supplier's are read the way a ticket is."""
    return supplier.parser_key != LLM_PARSER_KEY and not is_ticket_shop(supplier)


def has_text_layer(path: str) -> bool:
    """A digital document, rather than a photo or a scan."""
    return any(page is not None for page in text_layer_pages(path))


def document_supplier(text: str) -> Supplier | None:
    """The supplier whose own reader may be handed this document: named by
    the text it prints at the top, by a configured till, or by its company
    number (detect_shop).

    A reader of its own turns a whole document into lines, so a phone number
    or a web site is not enough to choose one - those two are printed by a
    customer and a supplier alike. Read as a ticket, the document is checked
    against its own totals, which is why that path is not this strict.
    """
    parser, identifiers = detect_shop(text)
    if parser is None:
        return None
    if identifiers and not any(identifier.startswith("siren:") for identifier in identifiers):
        return None
    return Supplier.objects.filter(code=parser.supplier_code).first()


def import_document(
    path: str,
    display_filename: str | None = None,
    supplier: Supplier | None = None,
    date_hint: date | None = None,
    chosen_because: str | None = None,
) -> Invoice:
    """Import one file, whatever it is - the file says which reader it needs.

    A photo or a scan is read as a ticket. A digital document goes through
    its supplier's own reader when that supplier has one (Metro, UBA...) and
    is recognised, by `supplier` or by what the document prints; anything
    else is read by the ticket reader, which reads an invoice's table too.
    """
    # Before the file is opened at all: a folder scanned again is mostly
    # documents already in, and its digest answers for them.
    known = Invoice.objects.filter(source_sha256=file_sha256(path)).select_related("supplier").first()
    if known is not None:
        raise DuplicateInvoiceError(f"Fichier déjà importé : {_describe(known)}.")
    text = document_text(path)
    if text:
        found = supplier if supplier is not None else document_supplier(text)
        if found is not None and has_own_reader(found):
            return import_invoice_pdf(path, found, display_filename=display_filename, text=text)
    said = {} if chosen_because is None else {"chosen_because": chosen_because}
    return import_receipt(path, display_filename=display_filename, supplier=supplier, date_hint=date_hint, **said)


def import_invoice_pdf(
    path: str, supplier: Supplier, display_filename: str | None = None, text: str | None = None
) -> Invoice:
    """A digital invoice through its supplier's own reader - refused, like a
    ticket, when this very file is in already. What it prints is kept
    (`source_text`) and teaches the supplier what names it."""
    from .importing import parse_and_import

    digest = file_sha256(path)
    known = Invoice.objects.filter(source_sha256=digest).select_related("supplier").first()
    if known is not None:
        raise DuplicateInvoiceError(f"Fichier déjà importé : {_describe(known)}.")
    invoice = parse_and_import(path, supplier, display_filename=display_filename)
    invoice.source_sha256 = digest
    invoice.source_text = document_text(path) if text is None else text
    invoice.save(update_fields=["source_sha256", "source_text"])
    learn_identifiers(supplier, invoice.source_text)
    return invoice


def import_receipt(
    pdf_path: str,
    display_filename: str | None = None,
    date_hint: date | None = None,
    supplier: Supplier | None = None,
    chosen_because: str = "L'en-tête du ticket n'a pas été reconnu.",
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
    named_by_hand = supplier is not None
    if supplier is None:
        if read.parser is None or read.parsed is None:
            raise UnrecognisedShopError("Enseigne non reconnue sur ce ticket.", text=read.text)
        supplier = Supplier.objects.get(code=read.parser.supplier_code)
        parsed = read.parsed
        if read.identified_by:
            parsed.checks.append(ParseCheck(
                label=IDENTIFIED_CHECK,
                passed=True,
                detail="Aucun en-tête connu : reconnue par son "
                + ", son ".join(describe_identifier(identifier) for identifier in read.identified_by)
                + " (vu sur ses tickets).",
            ))
    else:
        parsed = _chosen_shop_read(supplier, read, date_hint, chosen_because)
    label_placeholder_lines(supplier, parsed)
    dated = date_check(parsed.invoice_date)
    if dated is not None:
        parsed.checks.append(dated)

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
    if named_by_hand:
        # Its next tickets are recognised by what this one prints.
        learn_identifiers(supplier, invoice.ocr_text)
    return invoice


def _chosen_shop_read(
    supplier: Supplier, read: ReceiptRead, date_hint: date | None, chosen_because: str
) -> ParsedInvoice:
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
    parsed.checks.append(ParseCheck(label=CHOSEN_SHOP_CHECK, passed=True, detail=chosen_because))
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
    "RereadError",
    "UnrecognisedShopError",
    "apply_known_prices",
    "check_header",
    "create_shop",
    "date_check",
    "describe_tickets",
    "detect_parser",
    "detect_shop",
    "document_supplier",
    "document_text",
    "first_reading",
    "forget_identifiers",
    "has_own_reader",
    "has_text_layer",
    "header_choices",
    "header_guess",
    "identified_supplier",
    "import_document",
    "import_invoice_pdf",
    "import_receipt",
    "invoice_supplier_choices",
    "label_placeholder_lines",
    "learn_identifiers",
    "lines_check",
    "move_to_shop",
    "names_shop",
    "parser_for",
    "pending_receipts",
    "plain_text",
    "printed_unit_price",
    "read_receipt",
    "receipt_parsers",
    "recheck_after_review",
    "recognise",
    "rename_product",
    "reread_document",
    "reread_receipt",
    "set_shop_header",
    "shop_choices",
    "tickets_printing",
]
