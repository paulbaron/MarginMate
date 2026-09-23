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
from django.db.models import Count, Max, Sum
from django.db.models.functions import Length
from django.utils import timezone

from . import einvoice
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
    VAT_IDENTITY_TOLERANCE,
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
HT_CHECK = "Somme HT des lignes = base HT du ticket"
UNREAD_TOTAL_CHECK = "Total imprimé lu"
DATE_CHECK = "Date du ticket"
# What an electronic invoice charges outside its lines (BG-20/BG-21).
ADJUSTMENT_CHECK = "Frais et remises sur la facture"
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
# Six digits or more in a row: a document's own number, a SIRET, a customer
# reference - never a street number or a postcode.
REFERENCE_RE = re.compile(r"\d{6,}")
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
#
# The two sums are here as the reading's own verdict only: the page shows
# both worked out from the lines as they stand - against the total, and
# against the VAT table's HT base whenever it has a table - and follows them
# as they are typed (views._checks_context). Validating rebuilds both under
# the same labels, which is why the page cannot tell them apart by label.
READING_CHECKS = {
    SUM_CHECK,
    UNREAD_CHECK,
    HT_CHECK,
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


WAITING_GROUP = "En attente de leur premier document"


def _waiting_first(suppliers) -> tuple[list[Supplier], list[Supplier]]:
    """(those with no document yet, the others): a supplier created for
    what is about to be imported is looked for first - among eighty, under
    its letter, it was one more to scroll past. The AI pseudo-supplier is
    never waiting: it is a way of reading, not someone."""
    filed = set(Invoice.objects.values_list("supplier_id", flat=True).distinct())
    waiting = [supplier for supplier in suppliers if supplier.pk not in filed and supplier.parser_key != LLM_PARSER_KEY]
    return waiting, [supplier for supplier in suppliers if supplier not in waiting]


def shop_choices() -> list[tuple[str, list[Supplier]]]:
    """What a ticket can be filed under by hand, grouped: those waiting for
    their first document, the shops, then the suppliers whose invoices are
    PDFs (a paper ticket of theirs). Every one of them has its tickets read."""
    waiting, suppliers = _waiting_first(list(Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).order_by("name")))
    return [
        (WAITING_GROUP, waiting),
        ("Magasins", [supplier for supplier in suppliers if is_ticket_shop(supplier)]),
        ("Fournisseurs à factures (ticket papier)", [supplier for supplier in suppliers if not is_ticket_shop(supplier)]),
    ]


def invoice_supplier_choices() -> list[tuple[str, list[Supplier]]]:
    """What a PDF invoice can be imported under, grouped by how it is read -
    those waiting for their first document first."""
    waiting, suppliers = _waiting_first(list(Supplier.objects.order_by("name")))
    return [
        (WAITING_GROUP, waiting),
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


def prints_header(text: str, header: str) -> bool:
    """Whether a document prints `header`: the one definition, as whole
    words, accents, case and punctuation aside."""
    return _has_header(plain_text(text), plain_text(header))


def detect_parser(text: str) -> ReceiptParser | None:
    return detect_shop(text)[0]


def detect_shop(text: str) -> tuple[ReceiptParser | None, list[str]]:
    """Which shop this receipt belongs to, from what it prints - and, when
    that was not a header, the identifiers that said so (recognise_shop)."""
    parser, identifiers, _conflict = recognise_shop(text)
    return parser, identifiers


def recognise_shop(text: str) -> tuple[ReceiptParser | None, list[str], str]:
    """Which shop a document belongs to, the identifiers that said so when
    no header did, and - when nobody is named because two things printed on
    it disagree - what they are, for the person who will choose.

    A header a person gave a shop first - "EPICERIE SABAH" before the
    "SABAH" a configured till answers to, since a header printed inside
    another one gives way to it - then the configured tills, then the SIREN,
    phone or web site learned from the shop's documents
    (`identified_supplier`). Returns None rather than a best guess: an
    unrecognised receipt that is reported as such costs the operator one
    click, while one filed under the wrong shop produces plausible lines under
    the wrong products. Two guards ask rather than choose:

    - **two suppliers' headers** on one document, neither inside the other:
      the longest used to win, and one company's two sources (a box and a
      mobile line) each given the text of its own subscription would take
      each other's documents on an advert;
    - **a header against a company number**: a document that prints the
      header of one supplier and a SIREN another one learned is one of them,
      and nothing printed on it says which.
    """
    plain = plain_text(text)
    printed = [
        (header, supplier)
        for header, supplier in (
            (plain_text(supplier.ticket_header), supplier)
            for supplier in Supplier.objects.exclude(ticket_header="").exclude(parser_key=LLM_PARSER_KEY)
        )
        if _has_header(plain, header)
    ]
    kept = [
        (header, supplier)
        for header, supplier in printed
        if not any(other != header and _has_header(other, header) for other, _ in printed)
    ]
    by_header = {supplier.pk: supplier for _header, supplier in kept}
    if len(by_header) > 1:
        names = " et ".join(sorted(supplier.name for supplier in by_header.values()))
        return None, [], f"Ce document porte les en-têtes de {names} : choisissez l'enseigne."
    named = next(iter(by_header.values()), None)
    parser = parser_for(named) if named is not None else None
    if parser is None:
        parser = next(
            (
                till
                for till in receipt_parsers().values()
                if any(re.search(pattern, text, re.IGNORECASE) for pattern in getattr(till, "header_patterns", ()))
            ),
            None,
        )
    if parser is not None:
        other = _company_of_another(text, parser.supplier_code)
        if other is not None:
            owner = named.name if named is not None else getattr(
                Supplier.objects.filter(code=parser.supplier_code).first(), "name", parser.supplier_code
            )
            return None, [], (
                f"Ce document porte l'en-tête de {owner} mais le n° SIREN de {other.name} : choisissez l'enseigne."
            )
        return parser, [], ""
    supplier, identifiers = identified_supplier(text)
    if supplier is None:
        return None, [], ""
    return parser_for(supplier), identifiers, ""


def _companies_of_others(text: str, supplier_code: str) -> list[Supplier]:
    """Every supplier, other than `supplier_code`'s, that alone learned a
    company number `text` prints - two of them on one document were no
    doubt at all to _company_of_another, which names one or none."""
    sirens = {identifier for identifier in document_identifiers(text) if identifier.startswith("siren:")}
    if not sirens:
        return []
    owners = defaultdict(list)
    for supplier in Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).exclude(ticket_identifiers=[]):
        for identifier in sirens.intersection(supplier.ticket_identifiers or ()):
            owners[identifier].append(supplier)
    found = {}
    for suppliers in owners.values():
        if len(suppliers) == 1 and suppliers[0].code != supplier_code:
            found[suppliers[0].pk] = suppliers[0]
    return sorted(found.values(), key=lambda supplier: supplier.name)


def _company_of_another(text: str, supplier_code: str) -> Supplier | None:
    """The one supplier, other than `supplier_code`'s, that learned a company
    number `text` prints - or None. A number two suppliers learned names
    neither (identified_supplier), so it contradicts nobody either."""
    sirens = {identifier for identifier in document_identifiers(text) if identifier.startswith("siren:")}
    if not sirens:
        return None
    owners = defaultdict(list)
    for supplier in Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).exclude(ticket_identifiers=[]):
        for identifier in sirens.intersection(supplier.ticket_identifiers or ()):
            owners[identifier].append(supplier)
    others = {
        suppliers[0].pk: suppliers[0]
        for suppliers in owners.values()
        if len(suppliers) == 1 and suppliers[0].code != supplier_code
    }
    return next(iter(others.values())) if len(others) == 1 else None


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

    A new identifier names the shop when no other supplier's documents print
    it - the customer's own phone names nobody - and enough of its own do
    (identifiers_naming): a misreading, or a label printed on some goods, is
    on one ticket or two. What it knew stays unless there is a reason about
    that identifier itself (still_naming): measured again against a share
    of ALL its documents, the mobile line's figures of a supplier holding
    two subscriptions were dropped - silently, on a correction validated -
    once thirty box bills had been filed beside them by the header.

    **The others are corrected too**: a supplier that learned something
    these documents print learned it while it was the only one printing it,
    and it no longer is. Left alone, one supplier that had once learned the
    customer's own company number refused every document of another that
    printed it, header or not, for as long as nobody filed one of its own.
    """
    if supplier.parser_key == LLM_PARSER_KEY:
        return []
    # As stored now: another ticket of the shop may have taught it meanwhile.
    supplier.refresh_from_db(fields=["ticket_identifiers", "ticket_header", "refused_identifiers"])
    known = set(supplier.ticket_identifiers or ())
    printed = set()
    for text in texts:
        printed |= document_identifiers(text)
    # A figure a person set aside is never learned again.
    new = printed - known - set(supplier.refused_identifiers or ())
    learned = []
    if known or new:
        own = _stored_texts(Invoice.objects.filter(supplier=supplier))
        own += [text for text in texts if text and text not in own]
        others = _stored_texts(Invoice.objects.exclude(supplier=supplier))
        kept = still_naming(known, own, others)
        if new:
            kept |= identifiers_naming(own, others, new, headerless_texts=_without_header(supplier, own))
        set_identifiers(supplier, kept, reasons=_why_lost(supplier, known - kept))
        learned = sorted(kept - known)
    if printed:
        for other in Supplier.objects.exclude(pk=supplier.pk).exclude(parser_key=LLM_PARSER_KEY).exclude(
            ticket_identifiers=[]
        ):
            if printed.intersection(other.ticket_identifiers or ()):
                _recheck(other)
    return learned


def _recheck(supplier: Supplier) -> None:
    """What `supplier` knows, checked again against everybody's documents as
    they are stored now - it can only forget, and only for a reason about
    the identifier (still_naming)."""
    known = set(supplier.ticket_identifiers or ())
    kept = still_naming(
        known,
        _stored_texts(Invoice.objects.filter(supplier=supplier)),
        _stored_texts(Invoice.objects.exclude(supplier=supplier)),
    )
    set_identifiers(supplier, kept, reasons=_why_lost(supplier, known - kept))


def set_identifiers(supplier: Supplier, identifiers, reasons=None, asked: bool = False) -> None:
    """The one writer of what names `supplier` (ticket_identifiers): every
    change is recorded (supplier_changes), each figure gained or lost with
    its reason, and a loss nobody asked for (`asked`: « Retirer » on its
    page) is to be seen - a validation's too: on 18/09 a supplier's figures
    went as the side effect of one, without a word or a trace."""
    from . import supplier_changes
    from .identifiers import describe
    from .models import SupplierChange

    before = set(supplier.ticket_identifiers or ())
    after = set(identifiers)
    if before == after:
        return None
    supplier.ticket_identifiers = sorted(after)
    supplier.save(update_fields=["ticket_identifiers"])
    reasons = reasons or {}
    gained, lost = sorted(after - before), sorted(before - after)
    said = []
    if gained:
        said.append(f"Reconnaît désormais {supplier.name} : " + ", ".join(describe(identifier) for identifier in gained))
    if lost:
        said.append(
            f"Ne reconnaît plus {supplier.name} : "
            + ", ".join(
                describe(identifier) + (f" ({reasons[identifier]})" if reasons.get(identifier) else "")
                for identifier in lost
            )
        )
    return supplier_changes.record(
        supplier,
        SupplierChange.Kind.IDENTIFIERS,
        " ; ".join(said) + ".",
        data={"before": sorted(before), "after": sorted(after), "gained": gained, "lost": lost},
        needs_review=bool(lost) and not asked,
    )


def _why_lost(supplier: Supplier, lost) -> dict:
    """Why each of `lost` names `supplier` no longer: the other supplier's
    document that prints it too, or none of its own printing it any more."""
    lost = set(lost)
    reasons: dict = {}
    if not lost:
        return reasons
    documents = Invoice.objects.exclude(supplier=supplier).filter(supplier_doubt="")
    for invoice in documents.select_related("supplier").order_by("-pk"):
        text = invoice.document_text
        if not text or not may_print(text, lost - set(reasons)):
            continue
        for identifier in document_identifiers(text) & (lost - set(reasons)):
            reasons[identifier] = f"imprimé aussi sur {_describe(invoice)}"
        if len(reasons) == len(lost):
            break
    for identifier in lost - set(reasons):
        reasons[identifier] = "aucun de ses documents ne l'imprime plus"
    return reasons


def still_naming(known, own_texts, other_texts) -> set[str]:
    """What a supplier knew that still names it: not printed on another
    supplier's documents, and - when it has documents - printed on one of
    its own at least. Documents of another family filed there since (the box
    bills beside the mobile ones) are no reason to forget anything; a
    supplier with no documents keeps what it holds. No database."""
    kept = set(known)
    own_texts = [text for text in own_texts if text]
    if own_texts and kept:
        printed = set()
        for text in own_texts:
            if may_print(text, kept):
                printed |= document_identifiers(text) & kept
        kept &= printed
    for other in other_texts:
        if not kept:
            break
        if other and may_print(other, kept):
            kept -= document_identifiers(other)
    return kept


def _without_header(supplier: Supplier, texts) -> list[str]:
    """Those of `texts` that do not print `supplier`'s header - none when it
    has no header."""
    if not supplier.ticket_header:
        return []
    return [text for text in texts if text and not prints_header(text, supplier.ticket_header)]


def identifiers_naming(own_texts, other_texts, candidates, headerless_texts=()) -> set[str]:
    """Which of `candidates` name a supplier whose documents say `own_texts`,
    when everybody else's say `other_texts`: those none of the others print,
    and that at least MIN_IDENTIFIER_SHARE of its documents print - or, for
    a supplier some of whose documents print its header and some do not
    (`headerless_texts`: those filed there by something other than its
    header), at least that share of those without the header, two of them
    at the least. No documents of its own, nothing names it. No database:
    « Retenir » on a supplier's page is worked out by the same rule."""
    own_texts = [text for text in own_texts if text]
    if not own_texts or not candidates:
        return set()
    candidates = set(candidates)

    def counted(texts):
        seen = Counter()
        for document in texts:
            if may_print(document, candidates):
                seen.update(document_identifiers(document) & candidates)
        return seen

    seen = counted(own_texts)
    kept = {identifier for identifier in candidates if seen[identifier] >= MIN_IDENTIFIER_SHARE * len(own_texts)}
    headerless_texts = [text for text in headerless_texts if text]
    if headerless_texts and len(headerless_texts) < len(own_texts):
        seen_headerless = counted(headerless_texts)
        floor = max(2, MIN_IDENTIFIER_SHARE * len(headerless_texts))
        kept |= {identifier for identifier in candidates if seen_headerless[identifier] >= floor}
    for other in other_texts:
        if not kept:
            break
        if other and may_print(other, kept):
            kept -= document_identifiers(other)
    return kept


def _stored_texts(invoices) -> list[str]:
    """What the documents of `invoices` say: a receipt's reading, a digital
    one's own text - not a document whose supplier is in doubt
    (`Invoice.supplier_doubt`): until a person says whose it is, it is
    nobody's, and counted as another supplier's it made that one forget its
    own company number."""
    return [
        ocr_text or source_text
        for ocr_text, source_text in invoices.filter(supplier_doubt="").values_list("ocr_text", "source_text")
        if ocr_text or source_text
    ]


def supplier_named(name: str, exclude=None) -> Supplier | None:
    """The supplier already called `name`, whatever the case of its letters -
    accented ones included, which SQLite's case-blind comparison leaves out
    ("ÉPICERIE" and "épicerie" were two suppliers)."""
    wanted = " ".join(name.split()).casefold()
    if not wanted:
        return None
    for supplier in Supplier.objects.exclude(pk=getattr(exclude, "pk", exclude)).only("pk", "name"):
        if " ".join(supplier.name.split()).casefold() == wanted:
            return supplier
    return None


def forget_identifiers(supplier: Supplier, *texts: str, reason: str = "imprimé sur des documents rangés ailleurs") -> None:
    """Documents printing them are not `supplier`'s after all: they name it
    no longer."""
    printed = set()
    for text in texts:
        printed |= document_identifiers(text)
    supplier.refresh_from_db(fields=["ticket_identifiers"])
    known = set(supplier.ticket_identifiers or ())
    set_identifiers(supplier, known - printed, reasons={identifier: reason for identifier in known & printed})


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
            # Cut inside a word, the guess matched no document - not even
            # the one it was read from.
            guess = _cut_at_a_word(" ".join(line.split()), 60)
            if guess:
                return guess
    return ""


def header_choices(text: str) -> list[str]:
    """The lines of a document that could be the text its shop prints at the
    top: its first lines made of words - a name, a street - with no amount,
    date or field among them. Offered on the review screen, where the
    document is on show."""
    choices: list[str] = []
    for line in [line.strip() for line in text.splitlines() if line.strip()][:HEADER_LINES_READ]:
        line = _cut_at_a_word(" ".join(line.split()), MAX_HEADER_LENGTH)
        letters = sum(char.isalpha() for char in line)
        visible = len(line.replace(" ", ""))
        if letters < MIN_HEADER_LENGTH or letters < 0.5 * visible:
            continue
        # "No de Commande :" is the label of a field whose value is elsewhere.
        if FIELD_RE.match(line) or line.endswith(":") or line_amounts(line) or DATE_OR_TIME_RE.search(line):
            continue
        # "Facture no 2519210291 du 19 janvier": a reference this document
        # alone prints is no header - the next one prints another.
        if REFERENCE_RE.search(line):
            continue
        if line not in choices:
            choices.append(line)
    return choices[:MAX_HEADER_CHOICES]


def _cut_at_a_word(line: str, length: int) -> str:
    """`line` no longer than `length`, cut after a whole word: cut inside
    one ("...depuisu"), a header offered from a document no longer matched
    that very document - a header is compared as whole words."""
    if len(line) <= length:
        return line
    cut = line[:length]
    # One word longer than a header is no name, and cut it matches nothing.
    return cut[: cut.rfind(" ")].rstrip() if " " in cut else ""


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


def document_corpus() -> list[tuple[int, str]]:
    """Every stored document's id and plain text, read once - for checking
    several headers against them (about 90 ms for 875 documents, where
    reading them again for each header offered cost that much each).

    Kept between requests while no document is added, removed or read
    again (its reading's length changes): the review page offers header
    chips on every ticket, and paid those 90 ms on each step through the
    queue. It only decides which chips are offered - a header saved is
    checked against the documents as they are (check_header, no corpus)."""
    fingerprint = tuple(
        Invoice.objects.aggregate(
            count=Count("pk"),
            last=Max("pk"),
            latest=Max("imported_at"),
            read=Sum(Length("ocr_text") + Length("source_text")),
        ).values()
    )
    with _CORPUS_LOCK:
        if _CORPUS.get("fingerprint") != fingerprint:
            _CORPUS["texts"] = [
                (pk, plain_text(ocr_text or source_text))
                for pk, ocr_text, source_text in Invoice.objects.values_list("pk", "ocr_text", "source_text")
                if ocr_text or source_text
            ]
            _CORPUS["fingerprint"] = fingerprint
        return _CORPUS["texts"]


_CORPUS: dict = {}
_CORPUS_LOCK = threading.Lock()


def tickets_printing(header: str, ignoring=(), corpus=None) -> list[Invoice]:
    """The documents already filed whose text carries `header`, oldest
    first - a ticket as it was read, a digital one as it prints."""
    plain = plain_text(header)
    if len(plain) < MIN_HEADER_LENGTH:
        return []
    ignored = {invoice.pk for invoice in ignoring}
    found = [
        pk for pk, text in (corpus if corpus is not None else document_corpus())
        if pk not in ignored and _has_header(text, plain)
    ]
    return list(Invoice.objects.filter(pk__in=found).select_related("supplier").order_by("invoice_date", "pk"))


def describe_tickets(tickets) -> str:
    return ", ".join(
        f"{ticket.supplier.name} du {ticket.invoice_date:%d/%m/%Y}" if ticket.invoice_date else f"{ticket.supplier.name} n° {ticket.pk}"
        for ticket in tickets
    )


def check_header(header: str, ignoring=(), shop: Supplier | None = None, corpus=None) -> str:
    """`header` as it will be stored. Raises ValueError, for the operator: a
    text too short to tell a shop, one another supplier already has, or one
    already printed on the tickets of other shops, or of many, which it
    would take from them.
    A few tickets of one shop carrying it are more likely this shop's, filed
    there before it existed (tickets_printing says which; `ignoring` is what
    is being moved, `shop` the shop the header is being given to - its own
    tickets print it, of course)."""
    header = " ".join(header.split())
    if header and len(plain_text(header)) < MIN_HEADER_LENGTH:
        raise ValueError(
            f"Le texte d'en-tête « {header} » est trop court pour reconnaître des tickets "
            f"({MIN_HEADER_LENGTH} caractères au moins)."
        )
    if not header:
        return header
    taken = next(
        (
            supplier
            for supplier in Supplier.objects.exclude(ticket_header="").exclude(pk=getattr(shop, "pk", None))
            if plain_text(supplier.ticket_header) == plain_text(header)
        ),
        None,
    )
    if taken is not None:
        raise ValueError(f"« {header} » est déjà l'en-tête de {taken.name} : deux enseignes ne partagent pas un en-tête.")
    elsewhere = [
        ticket
        for ticket in tickets_printing(header, ignoring, corpus)
        if shop is None or ticket.supplier_id != shop.pk
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
    before = supplier.ticket_header
    supplier.ticket_header = header
    supplier.save(update_fields=["ticket_header"])
    if header != before:
        _record_header(supplier, before, header)
    return header


def _record_header(supplier: Supplier, before: str, after: str) -> None:
    from . import supplier_changes
    from .models import SupplierChange

    if not after:
        summary = f"En-tête « {before} » retiré."
    elif before:
        summary = f"En-tête « {after} » (avant : « {before} »)."
    else:
        summary = f"En-tête « {after} »."
    supplier_changes.record(supplier, SupplierChange.Kind.HEADER, summary, data={"before": before, "after": after})


@dataclass
class Renamed:
    before: str
    after: str
    # A supplier of charges: the lines and the poste named after it.
    lines: int = 0
    postes: int = 0


def rename_supplier(supplier: Supplier, name: str, dry_run: bool = False) -> Renamed:
    """Give `supplier` another name - what it is called on every page, not
    its code, which nothing shows and the configured tills are keyed on. A
    supplier of charges files its documents on a poste named after it, and
    their lines carry that name: both follow, or its next bill would open a
    poste of the new name beside the old one. Bank matching keeps the payee
    names it learned (CounterpartyAlias). Raises ValueError, for the
    operator, with nothing changed: no name, a name taken, or a poste of
    the new name already there. `dry_run`: what it would do, nothing done."""
    from inventory.models import Product

    from . import supplier_changes
    from .models import SupplierChange

    name = " ".join(name.split())
    before = supplier.name
    if not name:
        raise ValueError("Donnez-lui un nom.")
    taken = supplier_named(name, exclude=supplier)
    if taken is not None:
        raise ValueError(f"« {name} » existe déjà : c'est le nom de {taken.name}.")
    renamed = Renamed(before, name)
    if name == before:
        return renamed
    lines = InvoiceLine.objects.filter(invoice__supplier=supplier, raw_name=before)
    postes = Product.objects.filter(supplier=supplier, raw_name=before)
    if supplier.expenses_only:
        renamed.lines = lines.count()
        renamed.postes = postes.count()
        # One of its postes is already called that, whatever its case: its
        # next bill read as a total alone would be filed on that poste, and a
        # rename undone would have renamed that poste's lines too. Not the
        # poste being renamed with it.
        if any(
            " ".join(poste.split()).casefold() == name.casefold()
            for poste in Product.objects.filter(supplier=supplier).exclude(raw_name=before).values_list(
                "raw_name", flat=True
            )
        ):
            raise ValueError(
                f"{before} a déjà un poste « {name} » : renommer le sien en ferait deux du même nom. "
                "Choisissez un autre nom."
            )
    if dry_run:
        return renamed
    with transaction.atomic():
        if supplier.expenses_only:
            lines.update(raw_name=name)
            postes.update(raw_name=name)
        supplier.name = name
        supplier.save(update_fields=["name"])
        took = f" ; {renamed.lines} ligne(s) de charge et son poste portent le nouveau nom" if renamed.lines else ""
        supplier_changes.record(
            supplier, SupplierChange.Kind.RENAMED, f"Nom : « {before} » → « {name} »{took}.",
            data={"before": before, "after": name},
        )
    return renamed


def create_shop(name: str, header: str = "", ignoring=(), expenses_only: bool = False) -> Supplier:
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
    if supplier_named(name) is not None:
        raise ValueError(f"« {name} » existe déjà : choisissez-la dans la liste.")
    header = check_header(header, ignoring)
    base = re.sub(r"[^A-Z0-9]+", "_", plain_text(name)).strip("_")[:24] or "ENSEIGNE"
    code, suffix = base, 1
    while Supplier.objects.filter(code=code).exists():
        suffix += 1
        code = f"{base}_{suffix}"
    supplier = Supplier.objects.create(
        code=code, name=name, parser_key="", ticket_header=header, expenses_only=expenses_only
    )
    _record_created(supplier)
    return supplier


def _record_created(supplier: Supplier) -> None:
    from . import supplier_changes
    from .models import SupplierChange

    said = [f"en-tête « {supplier.ticket_header} »"] if supplier.ticket_header else []
    said.append("charges" if supplier.expenses_only else "produits")
    supplier_changes.record(
        supplier, SupplierChange.Kind.CREATED, f"Créé ({', '.join(said)}).",
        data={"name": supplier.name, "header": supplier.ticket_header},
    )


def move_to_shop(invoice: Invoice, supplier: Supplier) -> None:
    """File a document under another supplier - a ticket or a digital
    invoice. See move_documents, which this is for one document."""
    move_documents([invoice], supplier)


@dataclass
class Moved:
    count: int = 0
    # Charge lines named after the supplier left, now after the new one.
    renamed: int = 0
    # Classified lines whose product at the new supplier took the same
    # stock item, and their stock movement back.
    reclassified: int = 0


def move_documents(invoices, supplier: Supplier) -> Moved:
    """File documents under another supplier, together: the one definition
    of a move, for one document or several filed together.

    Their lines stay as they are and find their products among the new
    supplier's (the old ones nobody else uses go), and four things the
    lines alone would lose are kept:

    - **a charge stays a charge.** Read as goods and moved into a supplier
      of charges, a document is read again as one (refile_as_charge, the
      path ticking "Charges" takes): kept as they were, a rent statement's
      previous balance, direct debit and rent became three postes and the
      charge three times what it charges. Moved between two suppliers of
      charges, a line named after the supplier takes the new name. Either
      way its state is its total's (charge_state): an unread total marked
      COMPLETE left the only list where it could be seen;
    - **a classified line stays in stock**: its product at the new supplier,
      new or never classified, takes the same stock item
      (link_product_to_stock_type) - re-resolved as a new product, the
      purchase silently left the stock ledger;
    - **a credit leaving charges becomes a return** (importing.
      credit_as_return, as when the supplier itself leaves them): kept at the
      count of 1 a charge takes it at, a document moved to a supplier of
      goods could not be saved untouched, and classifying the credit's
      product booked stock at a negative unit cost;
    - what the documents print is learned by the new supplier **once every
      one of them has moved**, and forgotten by those they leave.

    Raises ValueError when a number would be there twice, and
    InvoiceLinesInUseError as a correction would. All or nothing.
    """
    from inventory.services import link_product_to_stock_type

    from .importing import (
        _as_parsed,
        charge_state,
        corrected_line,
        credit_as_return,
        is_charge_credit,
        refile_as_charge,
        replace_invoice_lines,
    )

    moving = [invoice for invoice in invoices if invoice.supplier_id != supplier.pk]
    moved = Moved(count=len(moving))
    if not moving:
        return moved
    if supplier.parser_key == LLM_PARSER_KEY:
        raise ValueError("Un document ne se range pas sous ce fournisseur.")
    numbers = Counter(invoice.invoice_number for invoice in moving if invoice.invoice_number)
    twice = [number for number, times in numbers.items() if times > 1]
    if twice:
        raise ValueError(f"Deux des documents portent le n° {twice[0]} : ils ne vont pas chez le même fournisseur.")
    for invoice in moving:
        if Invoice.objects.filter(supplier=supplier, invoice_number=invoice.invoice_number).exclude(
            invoice_number=""
        ).exclude(pk=invoice.pk).exists():
            raise ValueError(
                f"{supplier.name} a déjà un document n° {invoice.invoice_number} : c'est peut-être le même."
            )
    left: dict[int, tuple[Supplier, list[str]]] = {}
    with transaction.atomic():
        for invoice in moving:
            old = invoice.supplier
            stored = list(invoice.lines.select_related("product", "product__stock_type"))
            classified = {
                line.raw_name: line.product
                for line in stored
                if line.product is not None and line.product.stock_type_id is not None
            }
            renamed = old.expenses_only and supplier.expenses_only
            leaving_charges = old.expenses_only and not supplier.expenses_only
            lines = []
            for line in stored:
                name = supplier.name if renamed and line.raw_name == old.name else line.raw_name
                moved.renamed += name != line.raw_name
                parsed = corrected_line(
                    line, raw_name=name, quantity=line.quantity, total_ht=line.total_ht, vat_rate=line.vat_rate
                )
                if leaving_charges and is_charge_credit(parsed):
                    credit_as_return(parsed)
                lines.append(parsed)
            left.setdefault(old.pk, (old, []))[1].append(invoice.document_text)
            fields = ["supplier", "supplier_doubt"]
            invoice.supplier = supplier
            # Filed by a person: whatever a type doubted is answered.
            invoice.supplier_doubt = ""
            if invoice.is_receipt:
                # A check is what a ticket's review screen reads; adding one to
                # a digital invoice would turn it into a ticket (is_receipt).
                invoice.parse_checks = [
                    check
                    for check in invoice.parse_checks
                    if check["label"] not in (CHOSEN_SHOP_CHECK, IDENTIFIED_CHECK)
                ] + [{"label": CHOSEN_SHOP_CHECK, "passed": True, "detail": f"Rangé chez {supplier.name} à la main."}]
                fields.append("parse_checks")
            invoice.save(update_fields=fields)
            if supplier.expenses_only and not old.expenses_only and refile_as_charge(
                invoice, _as_parsed(invoice, stored)
            ):
                continue  # read again as a charge, its state included
            replace_invoice_lines(invoice, lines)
            if supplier.expenses_only:
                charge_state(invoice, invoice.printed_total_ttc)
                continue
            for line in invoice.lines.select_related("product"):
                was = classified.get(line.raw_name)
                if was is not None and line.product.needs_review:
                    link_product_to_stock_type(line.product, was.stock_type, was.unit, was.stock_equivalent)
                    moved.reclassified += 1
        for old, texts in left.values():
            forget_identifiers(old, *texts)
            # What it still knows, checked against what it still has.
            learn_identifiers(old)
        learn_identifiers(supplier, *(text for _old, texts in left.values() for text in texts))
    return moved


def headerless_documents(supplier: Supplier) -> list[Invoice]:
    """`supplier`'s documents that carry text and do not print its header,
    oldest first - none when it has no header. A header only adds: these
    were filed there by something else they print - a SIREN, a phone, a
    web site."""
    if not supplier.ticket_header:
        return []
    return [
        invoice
        for invoice in Invoice.objects.filter(supplier=supplier).order_by("invoice_date", "pk")
        if invoice.document_text and not prints_header(invoice.document_text, supplier.ticket_header)
    ]


def offerable_headers(choices, shop: Supplier | None = None, ignoring=()) -> list[str]:
    """The `choices` a person could give as a header without the save
    refusing it - by check_header itself, over the documents read once. The
    customer's own name and street are on every supplier's documents: offered
    as chips, they were one click from a refusal."""
    corpus = document_corpus() if choices else []
    kept = []
    for choice in choices:
        try:
            check_header(choice, ignoring=ignoring, shop=shop, corpus=corpus)
        except ValueError:
            continue
        kept.append(choice)
    return kept


def identifier_report(supplier: Supplier) -> dict:
    """What names `supplier`, and what else its documents print, for its
    page: each figure with how many of its documents print it and, for one
    it does not hold, whether the rule learning uses would keep it - and why
    not (printed on another supplier's documents, too rare)."""
    from .identifiers import describe

    own = _stored_texts(Invoice.objects.filter(supplier=supplier))
    total = len(own)
    headerless = _without_header(supplier, own)
    counts = Counter()
    for text in own:
        counts.update(document_identifiers(text))
    known = set(supplier.ticket_identifiers or ())
    refused = set(supplier.refused_identifiers or ())
    candidates = (set(counts) | known) - refused
    elsewhere: dict[str, Counter] = {}
    if candidates:
        # Not a document whose supplier is in doubt: learning counts it as
        # nobody's (_stored_texts), and « Retenir » was refused for it.
        for supplier_name, ocr_text, source_text in (
            Invoice.objects.exclude(supplier=supplier)
            .filter(supplier_doubt="")
            .values_list("supplier__name", "ocr_text", "source_text")
        ):
            text = ocr_text or source_text
            if text and may_print(text, candidates):
                for identifier in document_identifiers(text) & candidates:
                    elsewhere.setdefault(identifier, Counter())[supplier_name] += 1

    def where_else(identifier) -> str:
        return ", ".join(
            f"{count} document{'s' if count > 1 else ''} de {name}" for name, count in elsewhere[identifier].most_common(3)
        )

    rows_known = [
        {
            "identifier": identifier,
            "label": describe(identifier),
            "count": counts[identifier],
            "total": total,
            "elsewhere": where_else(identifier) if identifier in elsewhere else "",
        }
        for identifier in sorted(known)
    ]
    rows_printed = []
    for identifier in sorted(set(counts) - known - refused, key=lambda i: (-counts[i], i)):
        if identifier in elsewhere:
            can_keep, reason = False, f"aussi sur {where_else(identifier)} : ne distingue pas {supplier.name}"
        else:
            can_keep = bool(identifiers_naming(own, [], {identifier}, headerless_texts=headerless))
            if can_keep:
                without = sum(1 for text in headerless if identifier in document_identifiers(text))
                reason = f"sur {counts[identifier]} de ses {total} documents" + (
                    f", dont les {without} sans l'en-tête" if without and without == counts[identifier] else ""
                )
            else:
                reason = f"sur {counts[identifier]} de ses {total} documents : trop rare pour le reconnaître"
        rows_printed.append(
            {"identifier": identifier, "label": describe(identifier), "count": counts[identifier],
             "can_keep": can_keep, "reason": reason}
        )
    return {
        "known": rows_known,
        "printed": rows_printed,
        "refused": [{"identifier": identifier, "label": describe(identifier)} for identifier in sorted(refused)],
        "total": total,
        "headerless": len(headerless),
        "with_header": total - len(headerless) if supplier.ticket_header else 0,
    }


def supplier_notices(supplier: Supplier) -> list[dict]:
    """What a supplier's page flags: documents a type fetched that print what
    names another supplier, changes nobody asked for still to be seen,
    nothing that recognises it. The « Enseignes et fournisseurs » tab shows
    "À voir" on its row for the changes, and lists them at its top
    (workspace._suppliers)."""
    notices = []
    doubted = list(
        Invoice.objects.filter(supplier=supplier).exclude(supplier_doubt="").order_by("pk").values_list("pk", flat=True)
    )
    if doubted:
        notices.append({
            "kind": "type_check",
            "text": (
                f"{len(doubted)} document{'s' if len(doubted) > 1 else ''} récupéré{'s' if len(doubted) > 1 else ''} "
                f"par une source de {supplier.name} "
                f"{'portent' if len(doubted) > 1 else 'porte'} ce qui reconnaît un autre fournisseur : rien n'en a "
                "été appris. Validez-le s'il est bien le sien, ou changez-le de fournisseur."
            ),
            "count": len(doubted),
            "invoice": doubted[0],
        })
    # The changes themselves, each with why it asks to be seen and its « Vu »
    # beside it: a count sent the owner to the foot of the page to find out
    # what it counted, and why it asked was said nowhere.
    to_see = list(
        supplier.changes.filter(needs_review=True, reviewed_at__isnull=True, undone_at__isnull=True)
        .select_related("supplier")
        .order_by("created_at", "pk")
    )
    if to_see:
        from .supplier_changes import why_to_see

        for change in to_see:
            change.why = why_to_see(change)
        notices.append({"kind": "review", "text": f"{len(to_see)} changement{'s' if len(to_see) > 1 else ''} à voir :",
                        "count": len(to_see), "changes": to_see})
    if (
        supplier.parser_key != LLM_PARSER_KEY
        and ticket_parser_for(supplier.code) is None
        and not has_own_reader(supplier)
        and not supplier.ticket_header
        and not supplier.ticket_identifiers
    ):
        empty = not Invoice.objects.filter(supplier=supplier).exists()
        notices.append({
            "kind": "unrecognised",
            "text": (
                f"Rien ne reconnaît encore {supplier.name} : "
                + ("son premier document vous sera demandé à l'import, sauf si une source le récupère pour lui."
                   if empty else "ses documents vous sont demandés à l'import, sauf ceux qu'une source récupère pour lui.")
            ),
            "count": 0,
        })
    return notices


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
    # Why nobody was named although something was printed that names a
    # shop: two of them disagree (recognise_shop).
    conflict: str = ""


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

    parser, identified_by, conflict = recognise_shop(text) if supplier is None else (parser_for(supplier), [], "")
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
        identified_by=identified_by, conflict=conflict,
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
    from .workspace import TICKET_TO_CHECK  # here: workspace reads this module

    return Invoice.objects.filter(TICKET_TO_CHECK)


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
    lines_only = sum((line.total_ttc.quantize(CENTS, rounding=ROUND_HALF_UP) for line in lines), start=Decimal("0"))
    adjustment = adjustment_counted(invoice)
    lines_total = lines_only + adjustment
    discounts = sum((line.discount_ttc for line in lines if line.printed_ttc is not None), start=Decimal("0"))
    # The promotions apart, as the page shows them: the articles are what the
    # ticket prints as its total before promotions.
    promotions = (
        f" - articles {lines_total + discounts:.2f} € moins {discounts:.2f} € de remises" if discounts else ""
    )
    # Duty, an eco-participation, a document-level charge: money the lines do
    # not carry, which the total does. "lignes" has to mean the LINES, so the
    # adjustment is added in front of the eye rather than folded into the
    # figure it is then announced beside - written that way the sentence read
    # « lignes 134,40 € + 14,40 € de frais / ticket 134,40 € », which does
    # not add up on any invoice.
    counted = f" + {adjustment:.2f} € de frais facturés globalement = {lines_total:.2f} €" if adjustment else ""
    # « ticket » on a document nobody photographed: an electronic invoice and
    # a supplier's PDF were both being checked against a till receipt that
    # does not exist, on the first line of the page.
    printed = "ticket" if invoice.is_receipt else "total de la facture"
    paid = invoice.printed_total_ttc
    if paid is None:
        return {
            "label": SUM_CHECK,
            "passed": False,
            "detail": f"{prefix}lignes {lines_only:.2f} €{counted} : saisissez le total pour les vérifier{promotions}",
        }
    gap = paid - lines_total
    return {
        "label": SUM_CHECK,
        "passed": abs(gap) <= RECONCILIATION_TOLERANCE,
        "detail": (
            f"{prefix}lignes {lines_only:.2f} €{counted} / {printed} {paid:.2f} € "
            f"(écart {gap:+.2f} €){promotions}"
        ),
    }


def adjustment_counted(invoice: Invoice) -> Decimal:
    """The reconciliation adjustment where the lines are checked against the
    total with it - a supplier's invoice, tax included.

    Never a ticket's: there it is the cents each line lost being divided by
    (1 + taux), which the printed amounts never lost, and added on top six
    baguettes of 0,49 € came to 2,97 €. On a supplier's invoice it is duty
    charged globally (or a document-level charge on an electronic invoice),
    and left out of the sum an invoice that balances to the cent read as
    failing its own check.
    """
    if invoice.is_receipt or not invoice.reconciliation_adjustment:
        return Decimal("0")
    return invoice.adjustment_ttc.quantize(CENTS, rounding=ROUND_HALF_UP)


def vat_table(invoice: Invoice) -> list[dict]:
    """The VAT table the document prints, as rows of {rate, base, vat}.

    What is stored, or - for a document filed before the table was kept -
    what reading it again says, so an old ticket's row comes up filled in
    rather than blank. Never for a table a person saved (`vat_table_typed`),
    even empty: "emptied" and "never stored" were one empty list, and a
    table emptied on the page came back as the reading's, with checks
    against figures nobody typed - invoice 842's failed « Somme HT des
    lignes = base HT du ticket », against a base of 2,61 € it never had.
    """
    rows = invoice.vat_breakdown
    if not rows and not invoice.vat_table_typed and invoice.ocr_text:
        parser = parser_for(invoice.supplier)
        if parser is not None and hasattr(parser, "parse_text"):
            try:
                rows = [[str(rate), str(base), str(tax)] for rate, base, tax in parser.parse_text(invoice.ocr_text).vat_breakdown]
            except Exception:  # noqa: BLE001 - a reading that fails leaves the table to be typed
                rows = []
    table = []
    for row in rows:
        try:
            rate, base, vat = (Decimal(str(value)) for value in row)
        except (ArithmeticError, TypeError, ValueError):
            continue
        table.append({"rate": rate, "base": base, "vat": vat})
    return table


def vat_table_checks(invoice: Invoice, table: list[dict] | None = None) -> list[dict]:
    """What the printed VAT table says about the lines, as checks a person
    can answer: each rate's own arithmetic, and the bases against the lines'
    HT. Both sides are on the review screen, so a failure always points at a
    field rather than at a reading nobody can reach.
    """
    from .parsers.receipt_base import format_rate

    table = vat_table(invoice) if table is None else table
    if not table:
        # No table, no check: a document that prints none is not wrong, and a
        # failure nobody asked for is the noise this page exists to avoid.
        # Typing one brings the check with it.
        return []
    checks = []
    for row in table:
        expected = (row["base"] * row["rate"]).quantize(CENTS, rounding=ROUND_HALF_UP)
        percent = format_rate(row["rate"])
        checks.append(
            {
                "label": f"TVA {percent}% cohérente",
                "passed": abs(expected - row["vat"]) <= VAT_IDENTITY_TOLERANCE,
                "detail": (
                    f"HT {row['base']:.2f} € x {percent}% = {expected:.2f} € / document {row['vat']:.2f} €"
                ),
            }
        )
    base_total = sum((row["base"] for row in table), start=Decimal("0"))
    lines_ht = sum((line.total_ht for line in invoice.lines.all()), start=Decimal("0"))
    drift = base_total - lines_ht
    slack = max(RECONCILIATION_TOLERANCE, CENTS * invoice.lines.count())
    checks.append(
        {
            "label": HT_CHECK,
            "passed": abs(drift) <= slack,
            "detail": f"lignes {lines_ht:.2f} € HT / document {base_total:.2f} € HT (écart {drift:+.2f} €)",
        }
    )
    return checks


def recheck_after_review(invoice: Invoice) -> None:
    """Replace what the parser said with checks on what the page now holds:
    the lines a person validated, the total they typed and the VAT table
    they typed beside it.

    Every check is then a comparison between two things on the screen - the
    reading's own notes ("Lignes écartées", "Montants recalculés") go, since
    they describe lines that no longer exist. Not saved here.
    """
    dropped = set(READING_CHECKS) | {check["label"] for check in vat_table_checks(invoice)}
    dropped |= {label for label in _vat_check_labels(invoice)}
    if invoice.printed_total_ttc is not None:
        dropped.add(UNREAD_TOTAL_CHECK)
    kept = [check for check in invoice.parse_checks if check["label"] not in dropped]
    invoice.parse_checks = (
        kept + [lines_check(invoice, prefix="vérifié à la main : ")] + vat_table_checks(invoice)
    )


def _vat_check_labels(invoice: Invoice) -> set:
    """Every label a VAT check has ever carried on this document - the rates
    read at import included, so a table corrected to another rate does not
    leave the old one's check behind."""
    return {
        check["label"]
        for check in invoice.parse_checks
        if check["label"].startswith(("TVA ", "Table TVA"))
        or check["label"] == HT_CHECK
    }


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
    if invoice.is_einvoice:
        # Its own data again - never its supplier's parser, which would be
        # handed an XML file, and never OCR, which would be handed a
        # document whose figures are stated.
        return _reread_einvoice_file(invoice, path)
    if invoice.is_receipt or isinstance(get_parser(invoice.supplier.parser_key), ReceiptParser):
        return _reread_receipt_file(invoice, path)
    return _reread_invoice_file(invoice, path)


def _reread_einvoice_file(invoice: Invoice, path: str) -> str:
    """Read an electronic invoice's file again: its lines, its date, its
    total, its VAT table and its checks, as the document states them."""
    from .importing import refile_as_charge, replace_invoice_lines

    data = einvoice.document_xml(path)
    if data is None:
        raise RereadError("Ce fichier ne porte plus de facture électronique : rien à relire.")
    try:
        parsed = einvoice.read(data)
    except einvoice.EInvoiceError as exc:
        raise RereadError(f"{exc} Le document n'a pas été modifié.") from exc
    kind = einvoice_format(path, parsed.einvoice)
    checks = einvoice_checks(kind, parsed.einvoice, parsed)
    if invoice.supplier.expenses_only:
        refile_as_charge(invoice, parsed)
        return f"Facture électronique relue : {invoice.lines.count()} poste(s) de charge."
    from .importing import charge_reading, credit_as_return, is_charge_credit

    lines = parsed.lines
    if parsed.einvoice.carries_no_lines:
        lines = charge_reading(parsed, invoice.supplier.name)[1]
    # Every line, whatever produced it - see import_einvoice.
    for line in lines:
        if is_charge_credit(line):
            credit_as_return(line)
    with transaction.atomic():
        replace_invoice_lines(invoice, lines)
        invoice.invoice_date = parsed.invoice_date or invoice.invoice_date
        invoice.printed_total_ttc = parsed.printed_total_ttc
        invoice.reconciliation_adjustment = parsed.reconciliation_adjustment
        invoice.adjustment_vat_rate = parsed.einvoice.adjustment_vat_rate
        invoice.vat_breakdown = [[str(rate), str(base), str(tax)] for rate, base, tax in parsed.vat_breakdown]
        invoice.vat_table_typed = False
        invoice.einvoice_format = kind
        invoice.parse_checks = checks
        invoice.error_message = " ".join(
            parsed.warnings + einvoice_date_problem(invoice.invoice_date)
        )
        if invoice.error_message:
            invoice.status = Invoice.Status.NEEDS_REVIEW
        invoice.save()
    return f"Facture électronique relue : {len(lines)} ligne(s)."


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


def type_supplier_doubt(text: str, supplier: Supplier, type_name: str) -> str:
    """Why a document the invoice type `type_name` fetched for `supplier`
    may be another supplier's - it prints that one's header, till, company
    number or what it learned, or things that disagree - or "".

    The type files it where it says all the same (it is what a person set
    up), but nothing is learned from it: learned, the other supplier's
    figures would become this one's, and the other one would lose them."""
    parser, identifiers, conflict = recognise_shop(text)
    if conflict:
        what = conflict.split(" : ")[0].rstrip(".")
        return (
            f"Récupéré par « {type_name} » pour {supplier.name}, mais {what[0].lower()}{what[1:]}. "
            f"Vérifiez qu'il est bien de {supplier.name} ; rien n'en a été appris."
        )
    if parser is None or parser.supplier_code == supplier.code:
        # Named by nobody, or by the type's own supplier - but a company
        # number another supplier learned alone, printed beside this one's,
        # names nobody either, and learned it would be taken from that one.
        others = _companies_of_others(text, supplier.code)
        if not others:
            return ""
        printed = document_identifiers(text)
        said = " et ".join(
            ", le ".join(
                describe_identifier(identifier)
                for identifier in sorted(printed & set(other.ticket_identifiers or ()))
                if identifier.startswith("siren:")
            )
            + f" de {other.name}"
            for other in others
        )
        whose = " ou de ".join(other.name for other in others)
        return (
            f"Récupéré par « {type_name} » pour {supplier.name}, mais il porte aussi le {said} : s'il est de "
            f"{whose}, rangez-le chez lui (« Changer de fournisseur »). Rien n'en a été appris."
        )
    other = Supplier.objects.filter(code=parser.supplier_code).first()
    if other is None:
        return ""
    printed = (
        "le " + ", le ".join(describe_identifier(identifier) for identifier in identifiers)
        if identifiers
        else "l'en-tête"
    )
    return (
        f"Récupéré par « {type_name} » pour {supplier.name}, mais il porte {printed} de {other.name} : s'il est de "
        f"{other.name}, rangez-le chez lui (« Changer de fournisseur ») et rattachez la source à {other.name}. "
        "Rien n'en a été appris."
    )


def _record_first_document(supplier: Supplier, invoice: Invoice, how: str, learned, text: str) -> None:
    """A supplier's first document: what it taught - or what it prints that
    was not retained - to be seen on its page. Nothing was known of it
    before, so nothing else vouches for what it learned."""
    from . import supplier_changes
    from .models import SupplierChange

    # A document whose supplier is in doubt teaches nothing and vouches for
    # nothing: it is not the first, and the next one, the one that teaches,
    # is recorded as the first instead.
    if invoice.supplier_doubt or Invoice.objects.filter(supplier=supplier, supplier_doubt="").exclude(
        pk=invoice.pk
    ).exists():
        return
    printed = sorted(document_identifiers(text or ""))
    if learned:
        taught = "Il lui a appris : " + ", ".join(describe_identifier(identifier) for identifier in learned) + "."
    elif printed:
        taught = (
            "Il imprime " + ", ".join(describe_identifier(identifier) for identifier in printed)
            + " : rien n'en a été retenu (« Retenir » sur la fiche)."
        )
    else:
        taught = "Il n'imprime ni n° SIREN, ni téléphone, ni site."
    supplier_changes.record(
        supplier, SupplierChange.Kind.FIRST_DOCUMENT,
        f"Premier document : {_describe(invoice)} ({how.rstrip('.')}). {taught}",
        invoice=invoice, needs_review=True, data={"learned": list(learned), "printed": printed},
    )


def import_document(
    path: str,
    display_filename: str | None = None,
    supplier: Supplier | None = None,
    date_hint: date | None = None,
    chosen_because: str | None = None,
    by_type: str | None = None,
) -> Invoice:
    """Import one file, whatever it is - the file says which reader it needs.

    **An electronic invoice is read from its own data, first.** A file
    carrying an EN 16931 XML - a Factur-X PDF, or the XML on its own - goes
    to `einvoice.read` whatever the supplier and whatever the file looks
    like. That order is not negotiable: everything below this line INFERS
    (OCR reads a photograph, a regex finds an amount in a column) and the
    whole parse_checks apparatus exists to catch the inferences that are
    wrong, while the XML STATES the number, the date, the seller's SIREN,
    every line, the VAT breakdown and the totals. A Factur-X PDF handed to
    the ticket reader - which it would be, since it carries a text layer and
    prints a perfectly readable page - is a document whose exact figures
    were sitting inside it, thrown away and replaced by a guess.

    A photo or a scan is read as a ticket. A digital document goes through
    its supplier's own reader when that supplier has one (Metro, UBA...) and
    is recognised, by `supplier` or by what the document prints; anything
    else is read by the ticket reader, which reads an invoice's table too.

    `by_type`: the name of the invoice type that fetched it for `supplier` -
    filed there whatever it prints, but it teaches nothing when it prints
    what names another supplier (type_supplier_doubt).
    """
    # Before the file is opened at all: a folder scanned again is mostly
    # documents already in, and its digest answers for them.
    known = Invoice.objects.filter(source_sha256=file_sha256(path)).select_related("supplier").first()
    if known is not None:
        raise DuplicateInvoiceError(f"Fichier déjà importé : {_describe(known)}.")
    xml = einvoice.document_xml(path)
    if xml is not None:
        return import_einvoice(
            path, xml, display_filename=display_filename, supplier=supplier, date_hint=date_hint,
            chosen_because=chosen_because, by_type=by_type,
        )
    text = document_text(path)
    if text:
        found = supplier if supplier is not None else document_supplier(text)
        if found is not None and has_own_reader(found):
            return import_invoice_pdf(
                path, found, display_filename=display_filename, text=text, date_hint=date_hint,
                by_type=by_type if found == supplier else None, chosen_because=chosen_because,
            )
    said = {} if chosen_because is None else {"chosen_because": chosen_because}
    return import_receipt(
        path, display_filename=display_filename, supplier=supplier, date_hint=date_hint, by_type=by_type, **said
    )


def import_invoice_pdf(
    path: str,
    supplier: Supplier,
    display_filename: str | None = None,
    text: str | None = None,
    date_hint: date | None = None,
    by_type: str | None = None,
    chosen_because: str | None = None,
) -> Invoice:
    """A digital invoice through its supplier's own reader - refused, like a
    ticket, when this very file is in already. What it prints is kept
    (`source_text`) and teaches the supplier what names it - unless the type
    that fetched it (`by_type`) has reason to doubt it is its supplier's,
    said on the invoice. `date_hint` (the email's date) is what a reader
    reading no date falls back on."""
    from .importing import parse_and_import

    digest = file_sha256(path)
    known = Invoice.objects.filter(source_sha256=digest).select_related("supplier").first()
    if known is not None:
        raise DuplicateInvoiceError(f"Fichier déjà importé : {_describe(known)}.")
    invoice = parse_and_import(path, supplier, date_hint=date_hint, display_filename=display_filename)
    invoice.source_sha256 = digest
    invoice.source_text = document_text(path) if text is None else text
    # Kept on the document, waiting in "À corriger" until it is validated or
    # moved (workspace.DOCUMENT_TO_FIX).
    invoice.supplier_doubt = type_supplier_doubt(invoice.source_text, supplier, by_type) if by_type else ""
    doubt = invoice.supplier_doubt
    invoice.save(update_fields=["source_sha256", "source_text", "supplier_doubt"])
    learned = [] if doubt else learn_identifiers(supplier, invoice.source_text)
    _record_first_document(supplier, invoice, chosen_because or "lue par son lecteur", learned, invoice.source_text)
    return invoice


def einvoice_format(path: str, facts) -> str:
    """What to call the document this XML came out of, for the person
    reading the page: **Factur-X** is the PDF around a CII invoice, so a CII
    file arriving on its own is a CII file and calling it Factur-X would be
    a claim about a PDF that never came."""
    if facts.syntax == einvoice.UBL:
        return "UBL"
    return "Factur-X" if path.lower().endswith(".pdf") else einvoice.CII


def einvoice_supplier(text: str) -> tuple[Supplier | None, list[str]]:
    """Whose an electronic invoice is, and what said so.

    What the invoice STATES about its seller comes first - its SIREN and its
    VAT number, through `identified_supplier`, the one matcher for those.
    They are data rather than something read off a page, so they beat a
    header, which is a guess about printed words two companies can share:
    `recognise_shop` weighs a header first because a ticket's header is all
    it usually has.

    The header and the configured tills are still asked when nothing stated
    names anybody - a supplier known only by the words it prints has its
    first electronic invoice to file like any other, and its SIREN is learnt
    from it (learn_identifiers). Nobody named is nobody guessed at: the
    caller raises, and the document waits for a person to say whose it is.
    """
    supplier, identifiers = identified_supplier(text)
    if supplier is not None:
        return supplier, identifiers
    parser, found, _conflict = recognise_shop(text)
    if parser is None:
        return None, []
    return Supplier.objects.filter(code=parser.supplier_code).first(), found


def einvoice_date_problem(invoice_date: date | None) -> list[str]:
    """What is wrong with an electronic invoice's date, in one sentence.

    Absent, or outside 2000-today. A date is the one field of an EN 16931
    document this application cannot simply take as stated: every window,
    every valuation, the bank match and every margin place the document by
    it, so one dated 01/01/0001 is in none of them - and, said nowhere, in
    no queue either. `error_message` is what puts it in « Documents à
    corriger », beside the one with no date at all.

    Not a refusal of the file: the invoice is real and its figures are
    exact, and it is the date that has to be typed in.
    """
    from .forms import EARLIEST_DOCUMENT_DATE

    if invoice_date is None:
        return ["Date absente de la facture électronique : saisissez-la dans « Corriger les lignes »."]
    today = timezone.localdate()
    if not EARLIEST_DOCUMENT_DATE <= invoice_date <= today:
        return [
            f"Date invraisemblable sur la facture électronique ({invoice_date:%d/%m/%Y}) : "
            "corrigez-la dans « Corriger les lignes »."
        ]
    return []


def einvoice_checks(kind: str, facts, parsed: ParsedInvoice) -> list[dict]:
    """What an electronic invoice's page says about itself.

    First what it is - and, under that, the document's own arithmetic as
    `einvoice._checks` worked it out. Nothing else: the OCR confidence, the
    shop checks and every check that exists to catch a guess do not apply
    here, and shown beside exact data they would say that somebody has to go
    and compare figures with a photograph that does not exist.
    """
    said = f"Émise par {facts.seller_name} : s" if facts.seller_name else "S"
    checks = [
        {
            "label": f"Facture électronique ({kind})",
            "passed": True,
            "detail": (
                f"{said}es montants sont les données de la facture (EN 16931), "
                "et non une lecture de la page."
            ),
        }
    ]
    if parsed.reconciliation_adjustment:
        # Duty, an eco-participation, a discount on the whole invoice
        # (BG-20/BG-21): money no line carries, which the VAT base counts.
        # Its reasons are stated in the sender's own words, and an amount
        # with no reason beside it is a figure nobody can check.
        why = ", ".join(facts.adjustment_reasons) or "motif non précisé par le fournisseur"
        checks.append({
            "label": ADJUSTMENT_CHECK,
            "passed": True,
            "detail": (
                f"{parsed.reconciliation_adjustment:+.2f} € HT facturés globalement ({why}) : "
                "comptés dans le total et dans la base de TVA, imputés à aucune ligne."
            ),
        })
    return checks + [_as_dict(check) for check in parsed.checks]


def import_einvoice(
    path: str,
    data: bytes,
    display_filename: str | None = None,
    supplier: Supplier | None = None,
    date_hint: date | None = None,
    chosen_because: str | None = None,
    by_type: str | None = None,
) -> Invoice:
    """File one EN 16931 invoice - a Factur-X PDF, or the XML on its own.

    The file kept is the one received: it is the legal invoice, and a
    rendering of it is not. Raises EInvoiceError (a ValueError, reported per
    file by the folder import) when the XML is not one this can read, and
    UnrecognisedShopError when nothing known answers to the seller it names -
    a document waits for a person exactly as a PDF of nobody known does,
    because filing it under a guess would put a whole invoice's lines under
    the wrong supplier's products.
    """
    from .importing import charge_reading, credit_as_return, is_charge_credit

    digest = file_sha256(path)
    known = Invoice.objects.filter(source_sha256=digest).select_related("supplier").first()
    if known is not None:
        raise DuplicateInvoiceError(f"Fichier déjà importé : {_describe(known)}.")

    parsed = einvoice.read(data)
    facts = parsed.einvoice
    kind = einvoice_format(path, facts)
    named_by_hand = supplier is not None
    if supplier is None:
        supplier, _identifiers = einvoice_supplier(parsed.source_text)
        if supplier is None:
            raise UnrecognisedShopError(
                f"Facture électronique de « {facts.seller_name or 'fournisseur non nommé'} » : "
                "aucun fournisseur connu ne porte ce numéro SIREN.",
                text=parsed.source_text,
            )
    parsed.supplier_code = supplier.code
    # The e-mail's date only where the invoice states none of its own, which
    # for an EN 16931 document means a sender that left BT-2 out.
    parsed.invoice_date = parsed.invoice_date or date_hint
    checks = einvoice_checks(kind, facts, parsed)
    if named_by_hand:
        checks.append({
            "label": CHOSEN_SHOP_CHECK,
            "passed": True,
            "detail": chosen_because or f"Rangée chez {supplier.name} à la main.",
        })
    if not supplier.expenses_only:
        if facts.carries_no_lines:
            # MINIMUM and BASIC WL carry the totals and the VAT breakdown and
            # no line at all - a valid invoice, not a failed reading. Filed as
            # it stands it would be an invoice worth nothing, out of every
            # total and of the bank match, so it takes the total-only path a
            # charge takes: one line per rate, from the table it does state. A
            # supplier of charges is already filed that way by
            # import_parsed_invoice.
            parsed.lines = charge_reading(parsed, supplier.name)[1]
        for line in parsed.lines:
            # A count of 1 at a negative amount is how a charge takes a credit
            # and how goods book stock at a NEGATIVE unit cost - the FIFO
            # valuation's worst known failure, which LineCorrectionForm
            # refuses outright for a supplier of goods. The document produces
            # that shape two ways: rebuilt from a negative VAT table, and
            # stated outright, as an ordinary invoice's « REMISE COMMERCIALE
            # 1 × -60,00 ». Both are a return here, so the guard runs over
            # every line and not only over the ones this rebuilt.
            if is_charge_credit(line):
                credit_as_return(line)
    doubt = type_supplier_doubt(parsed.source_text, supplier, by_type) if by_type and named_by_hand else ""

    invoice = import_parsed_invoice(
        supplier, parsed, source_file_path=path,
        display_filename=display_filename or os.path.basename(path),
    )
    problems = list(parsed.warnings)
    problems.extend(einvoice_date_problem(invoice.invoice_date))
    invoice.source_text = parsed.source_text
    invoice.source_sha256 = digest
    invoice.einvoice_format = kind
    invoice.adjustment_vat_rate = facts.adjustment_vat_rate
    invoice.supplier_doubt = doubt
    # A charge keeps the checks the charge reading has just set
    # (importing.charge_state), as it does for a ticket: every path that
    # touches a charge writes those, and one of them would drop these the
    # next time it ran. That it is an electronic invoice is said by
    # `einvoice_format` instead, which nothing overwrites and which every
    # screen reads.
    if not supplier.expenses_only:
        invoice.parse_checks = checks
    # What the supplier's own figures say when they do not hold, and an
    # absent date: neither is a reading to correct, so neither belongs in the
    # ticket queue - `error_message` holds the document in « Documents à
    # corriger » instead (workspace.DOCUMENT_TO_FIX). A charge with nothing
    # wrong keeps the message the charge reading left, which is about its
    # total and not about this reading.
    if problems or not supplier.expenses_only:
        invoice.error_message = " ".join(problems)
        if invoice.error_message:
            invoice.status = Invoice.Status.NEEDS_REVIEW
    invoice.save(
        update_fields=[
            "source_text", "source_sha256", "einvoice_format", "adjustment_vat_rate",
            "parse_checks", "supplier_doubt", "error_message", "status",
        ]
    )
    # A stated SIREN is the strongest thing a document ever says about its
    # sender - stronger than the text a PDF prints, which teaches at import
    # already - so this one teaches too, unless a type's doubt says the
    # document may not be this supplier's at all.
    learned = [] if doubt else learn_identifiers(supplier, invoice.source_text)
    _record_first_document(
        supplier, invoice,
        chosen_because or f"lue dans sa facture électronique ({kind})",
        learned, invoice.source_text,
    )
    return invoice


def import_receipt(
    pdf_path: str,
    display_filename: str | None = None,
    date_hint: date | None = None,
    supplier: Supplier | None = None,
    chosen_because: str = "L'en-tête du ticket n'a pas été reconnu.",
    by_type: str | None = None,
) -> Invoice:
    """Recognise, parse and file one receipt photo.

    Raises UnrecognisedShopError when the shop can't be identified and
    DuplicateInvoiceError when this ticket has already been imported - both
    are reported per file by the batch view rather than aborting the batch.

    With `supplier`, the operator has named the shop: nothing is detected,
    and the ticket is always filed - with no lines, and a failed check saying
    why, when its reader read nothing (or it has none). With `by_type` too,
    an invoice type named it: a document printing what names another
    supplier is filed all the same, its doubt kept on it, and teaches nothing
    until a person validates or moves it (type_supplier_doubt,
    `Invoice.supplier_doubt`).
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
            raise UnrecognisedShopError(read.conflict or "Enseigne non reconnue sur ce ticket.", text=read.text)
        supplier = Supplier.objects.get(code=read.parser.supplier_code)
        parsed = read.parsed
        if read.identified_by:
            parsed.checks.append(ParseCheck(
                label=IDENTIFIED_CHECK,
                passed=True,
                detail=(
                    f"Sans son en-tête « {supplier.ticket_header} »" if supplier.ticket_header else "Aucun en-tête connu"
                )
                + " : reconnue par son "
                + ", son ".join(describe_identifier(identifier) for identifier in read.identified_by)
                + " (vu sur ses tickets).",
            ))
    else:
        parsed = _chosen_shop_read(supplier, read, date_hint, chosen_because)
    doubt = type_supplier_doubt(read.text, supplier, by_type) if by_type and named_by_hand and read.text else ""
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
    if not supplier.expenses_only:
        invoice.parse_checks = [
            {"label": check.label, "passed": check.passed, "detail": check.detail}
            for check in parsed.checks
        ]
    # A charge keeps its own checks and state, which the charge reading has
    # just set (importing.charge_state): the ticket reader's are about lines
    # that reading replaced. Stored over them, Eau de Paris' water bills -
    # filed at exactly what they charge - waited in « À vérifier » saying
    # « 5.5 % supposé » and « lignes 310,15 € HT / ticket 303,28 € ».
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
    invoice.supplier_doubt = doubt
    invoice.save(
        update_fields=[
            "ocr_text", "ocr_confidence", "parse_checks", "preview_image", "status", "source_sha256", "supplier_doubt",
        ]
    )
    learned = []
    if named_by_hand and not doubt:
        # Its next tickets are recognised by what this one prints.
        learned = learn_identifiers(supplier, invoice.ocr_text)
    if named_by_hand:
        how = chosen_because
    elif read.identified_by:
        how = "reconnu par son " + ", son ".join(describe_identifier(identifier) for identifier in read.identified_by)
    else:
        how = "reconnu par son en-tête"
    _record_first_document(supplier, invoice, how, learned, invoice.ocr_text)
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
    "einvoice_format",
    "einvoice_supplier",
    "import_einvoice",
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
