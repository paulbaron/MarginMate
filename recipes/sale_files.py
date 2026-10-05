"""The one writer of a « facture de vente » and its file.

Every sale document with a file enters the database here - « Lire la
facture » on the « Ventes » tab (an electronic invoice, read and created at
once: `read_einvoice_upload`) and the typed form (any plain file:
`save_typed`) - and nowhere else (« Données » writes through its own
context). The pattern is returnables/slips.store_slip: every refusal BEFORE
the transaction; the file saved INSIDE it, with its rows; and whatever goes
wrong inside, the name just saved deleted by the handler OUTSIDE it, for ANY
exception - Django has no « on rollback ». A file replaced, removed or
deleted with its document goes once the transaction commits, never before:
rolled back, the old file stays with its row.

**What is stored is the file as received** (D2), under `ventes/AAAA/MM/`
(models.SALE_FILES_FOLDER), its own name cleaned (`storage_name`), its sha256
beside it - one document per file (`saledocument_one_per_file`, which also
makes two posts of one file in flight one document). Nothing reads a plain
file: no OCR, no AI, no PDFium. The only reading is an electronic invoice's
XML (`einvoice.document_xml`), and the typed form refuses one
(`einvoice_on_typed_form`): its figures typed again by hand would be the
loss receipts.py forbids.

**An electronic invoice's sale date** (`_sold_on`, spec §3.4) is its delivery
date (BT-72, else the start of its billing period) when plausible, else its
issue date: an event invoiced days later consumes in the stock-take window
of the event, not of the paperwork. Neither plausible, the card's « Date »
serves; nothing then, nothing stored.

**How it counts** (`document_from_reading`, §3.3): a deposit invoice (386)
is « Acompte » whatever the card said; a credit note naming an invoice held
here counts like it; else the card's choice - each said.

**Once committed, the bank's automatic pass** (bank.sale_reconcile, §4.4
step 10): a credit already on the statement that surely paid the document
is linked at once - and should the pass fail (a locked database), the
document stays saved, the failure is logged and said: never a 500 after a
commit. Run whoever saves - a « Recettes & ventes » employee included, told
without the bank's detail (recipes.views).

Every refusal is a `SaleFileRefused` (or einvoice's own `EInvoiceError`), a
French sentence the page says as it is.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from typing import NamedTuple

from django import forms
from django.core.exceptions import SuspiciousFileOperation
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction

from common import delete_stored_files, file_too_big, read_date
from invoices import einvoice, receipts
from invoices.einvoice import EInvoiceError
from invoices.forms import check_document_date
from invoices.models import Invoice

from .models import SaleDocument, SaleDocumentLine
from .sale_einvoice import SaleReading, read_sale
from .sale_lines import proposals

logger = logging.getLogger(__name__)

#: Any of these may be a sale document's file (D2), stored as received.
SALE_FILE_EXTENSIONS = (
    ".pdf",
    ".xml",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".tif",
    ".tiff",
    ".bmp",
    ".doc",
    ".docx",
    ".odt",
    ".xls",
    ".xlsx",
    ".ods",
    ".csv",
    ".txt",
)
#: What the typed form's file input offers (`accept`): the same, but HEIC -
#: listed, an iPhone may send its original HEIC (never previewed here) rather
#: than the JPEG it sends otherwise; the server still accepts a .heic sent
#: anyway.
SALE_FILE_ACCEPT = ",".join(extension for extension in SALE_FILE_EXTENSIONS if extension != ".heic")
#: What « Lire la facture » reads: a Factur-X PDF, or a CII / UBL XML.
EINVOICE_EXTENSIONS = (".pdf", ".xml")
#: The name a file is stored under when nothing of its own is left once
#: cleaned (« €.pdf »).
DEFAULT_STEM = "facture"

EXTENSION_REFUSED = (
    "Fichiers acceptés : PDF, photo (JPG, PNG, WebP, HEIC, TIFF, BMP), XML, Word, Excel, OpenDocument, CSV ou texte."
)
EINVOICE_EXTENSION_REFUSED = "Seuls les fichiers PDF (Factur-X) et XML (CII, UBL) se lisent ici."
NO_FILE = "Choisissez le fichier de la facture électronique."
NO_COUNTING = "Choisissez comment elle compte."
NO_EINVOICE_IN_PDF = (
    "« {name} » ne contient pas de facture électronique : ajoutez-le avec « + Facture de vente » et saisissez "
    "la facture."
)
EINVOICE_ON_TYPED_FORM = (
    "« {name} » est une facture électronique : ajoutez-la avec « Lire la facture » sur l'onglet Ventes, ses lignes "
    "y sont reprises telles quelles."
)
#: The same file twice (spec §3.5), on the read card - and on the typed form,
#: where the file is attached to a document.
ALREADY_ADDED = "Déjà ajoutée : facture de vente {document}."
FILE_TAKEN = "Ce fichier est déjà celui de la facture de vente {document}."
ALREADY_A_PURCHASE = (
    "Ce fichier est déjà dans les factures d'achat ({invoice}) : supprimez-le d'Achats s'il s'agit d'une vente."
)
NUMBER_HELD = (
    "Une facture de vente porte déjà le n° {number} (du {day}) : ouvrez-la, ou supprimez-la avant d'ajouter celle-ci."
)
#: A purchase invoice dropped on the sales side (spec §3.6) - which would also
#: teach Achats' guard a supplier's SIREN as the bar's own.
SELLER_IS_A_SUPPLIER = (
    "Le vendeur de cette facture (SIREN {siren}) est votre fournisseur « {supplier} » : c'est une facture d'achat, "
    "importez-la dans « Factures ». Si c'est bien votre établissement, retirez ce numéro de la fiche de « {supplier} »."
)
BUYER_IS_THE_BAR = (
    "L'acheteur de cette facture (SIREN {siren}) est votre établissement : c'est une facture d'achat, importez-la "
    "dans « Factures »."
)
#: The date of the sale (spec §3.4).
DATE_UNREADABLE = "Date illisible : JJ/MM/AAAA."
SALE_NO_DATE = (
    "Date absente de la facture électronique : donnez la date de la vente dans « Date » et envoyez le fichier à "
    "nouveau."
)
SALE_BAD_DATE = (
    "Date invraisemblable sur la facture électronique ({day}) : donnez la date de la vente dans « Date » et envoyez "
    "le fichier à nouveau."
)
DATE_FROM_DELIVERY = "Date de vente : la date de livraison de la facture ({day})."
TYPED_DATE_IGNORED = "La facture électronique donne sa date ({day}) : la date saisie n'a pas servi."
#: How it counts when the card did not decide it (spec §2.5, §3.7).
DEPOSIT_SAID = (
    "Facture d'acompte (type 386) : elle ne compte ni dans les marges ni dans le stock, la facture finale comptera "
    "la vente."
)
CREDIT_NOTE_OF = "Avoir de la facture n° {number} : compté comme elle (« {counting} »)."
CREDIT_NOTE_CHECK = "Avoir : vérifiez « Compte » - il compte comme la facture qu'il corrige."
DEPOSIT_WORD_SAID = (
    "Le mot « acompte » figure sur cette facture (type 380) : si c'est une facture d'acompte, choisissez « Acompte » "
    "dans « Compte »."
)
#: A line stating no label (BT-153 is mandatory, an empty one is read ""):
#: tied to nothing, a line says what it is by its label.
NO_LABEL = "Ligne {number} sans libellé"

INFO, WARNING = "info", "warning"


class DocumentGone(Exception):
    """The document a typed save was asked for is gone - deleted in another
    tab between the page's load and this save. Nothing is written: saved
    anyway, the row and its lines came back under the same pk, naming a
    file the deletion removed."""


class SaleFileRefused(ValueError):
    """A file or a document this module will not store, with the French
    sentence to show. `typed_date`: the date the person typed, when the
    refusal is that date's own - the card's box is filled with it again."""

    def __init__(self, message: str, typed_date: date | None = None):
        super().__init__(message)
        self.typed_date = typed_date


class Said(NamedTuple):
    """A sentence to say beside a document just read, and its level (a
    message's: « info », « warning »)."""

    level: str
    text: str


@dataclass
class ReadOutcome:
    """What « Lire la facture » made: the document, how many of its lines
    the page proposes to tie, whether one of its own checks failed, what to
    say of how it counts, and of its date - and the bank's automatic pass:
    the credits it linked to the document, or that it failed."""

    document: SaleDocument
    proposals: int
    failed_checks: bool
    said: list[Said] = field(default_factory=list)
    date_said: str = ""
    linked: list = field(default_factory=list)
    bank_failed: bool = False


@dataclass
class SavedOutcome:
    """What a typed document's save did: the document, how many recipe lines
    it gave the menu's price, and the bank's automatic pass (`ReadOutcome`'s
    two fields)."""

    document: SaleDocument
    prices_written: int
    linked: list = field(default_factory=list)
    bank_failed: bool = False


@dataclass(frozen=True)
class DeletedDocument:
    """What a deletion took: the document's name, whether it had a file, how
    many bank links went with it."""

    label: str
    had_file: bool
    links: int


# -- the file -----------------------------------------------------------------------------------------------------------


def file_extension(name: str) -> str:
    """The name's extension, lower case - what decides what a file is here
    (the house never sniffs content on upload)."""
    return PurePosixPath(str(name or "").replace("\\", "/")).suffix.lower()


def storage_name(upload_name: str) -> str:
    """The name an upload is stored under (its folder is the field's): its
    own stem cleaned the way the storage cleans it, and its extension lower
    case; « facture » when nothing of the stem is left (« €.pdf » cleans to
    « .pdf », which has no extension - a PDF stored as « .pdf » is served as
    a file of no type)."""
    base = PurePosixPath(str(upload_name or "").replace("\\", "/")).name
    extension = file_extension(base)
    stem = base[: len(base) - len(extension)] if extension else base
    try:
        cleaned = default_storage.get_valid_name(stem).strip("._-")
    except SuspiciousFileOperation:
        cleaned = ""
    return f"{cleaned or DEFAULT_STEM}{extension}"


@contextmanager
def staged(upload):
    """The upload written to a temporary file of the system's (never under
    media, nor an espace's folders), its extension kept - einvoice decides
    by it -, the path given to the block and the file removed after it,
    whatever happens. The sha and the bytes stored come from it, never from
    an upload read twice."""
    handle, path = tempfile.mkstemp(suffix=file_extension(upload.name))
    try:
        with os.fdopen(handle, "wb") as written:
            for chunk in upload.chunks():
                written.write(chunk)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            logger.warning("Fichier temporaire non supprimé : %s", path, exc_info=True)


def einvoice_on_typed_form(path: str, name: str) -> str:
    """Why the typed form refuses this staged file - an electronic invoice,
    or an XML einvoice refuses to read (oversized, a DOCTYPE, an encoding,
    unparseable: its own sentence) - "" when it is a plain file to keep.
    A PDF holding an attachment too big to read, which could have been the
    invoice, is refused too."""
    extension = file_extension(name)
    try:
        if extension == ".xml":
            data = einvoice.document_xml(path)
            if data is None:
                return ""
            found = einvoice.root_syntax(data) is not None
        elif extension == ".pdf":
            found = einvoice.embedded_xml(path) is not None
        else:
            return ""
    except EInvoiceError as refused:
        return str(refused)
    return EINVOICE_ON_TYPED_FORM.format(name=name) if found else ""


def typed_file_problem(path: str, name: str, document: SaleDocument) -> str:
    """Why the typed form refuses this staged file for `document`, "" when
    it does not: an electronic invoice (`einvoice_on_typed_form`), the file
    of another sale document, a purchase's file (spec §3.5)."""
    refusal = einvoice_on_typed_form(path, name)
    if refusal:
        return refusal
    digest = receipts.file_sha256(path)
    holder = SaleDocument.objects.filter(source_sha256=digest).exclude(pk=document.pk).first()
    if holder is not None:
        return FILE_TAKEN.format(document=titled(holder))
    return _purchase_refusal(digest)


def _purchase_refusal(digest: str) -> str:
    purchase = Invoice.objects.filter(source_sha256=digest).select_related("supplier").first()
    if purchase is None:
        return ""
    named = (
        f"{purchase.supplier.name} n° {purchase.invoice_number}"
        if purchase.invoice_number
        else f"{purchase.supplier.name}, sans numéro"
    )
    return ALREADY_A_PURCHASE.format(invoice=named)


def titled(document: SaleDocument) -> str:
    """« n° FV-12 du 05/03/2026 », « sans numéro du 05/03/2026 »."""
    number = f"n° {document.reference}" if document.reference else "sans numéro"
    return f"{number} du {day_text(document.sold_on)}"


def day_text(day: date) -> str:
    """A day as the sentences write it, JJ/MM/AAAA - its four digits even
    for the year 1 an invoice may state (strftime writes « 1 » there on some
    systems)."""
    return f"{day.day:02d}/{day.month:02d}/{day.year:04d}"


def siren_text(siren: str) -> str:
    """« 123 456 789 », as invoices.identifiers.describe writes it."""
    return f"{siren[:3]} {siren[3:6]} {siren[6:]}"


def _forget(saved: str | None) -> None:
    """The file saved for rows that were not written."""
    if saved:
        delete_stored_files([(default_storage, saved)])


def _bank_pass(document: SaleDocument) -> tuple[list, bool]:
    """(the credits the bank's automatic pass linked to `document`, whether
    it failed) - run once the document is committed (spec §4.4 step 10). A
    failure - a locked database - is logged and said by the page, the
    document kept: « Rapprocher automatiquement » on Banque runs it again."""
    # here: bank reads this module
    from bank.sale_reconcile import reconcile_sales

    try:
        made = reconcile_sales()
    except Exception:
        logger.exception("Rapprochement bancaire impossible après la facture de vente %s", document.pk)
        return [], True
    return [line for line, documents in made if any(one.pk == document.pk for one in documents)], False


# -- « Lire la facture » ------------------------------------------------------------------------------------------------


def document_from_reading(
    reading: SaleReading, *, kind: str, sold_on: date, counting: str
) -> tuple[SaleDocument, list[SaleDocumentLine], list[Said]]:
    """The document an electronic invoice is, and its lines - unsaved - with
    what to say of how it counts. Every figure as stated (spec §3.3); a line
    tied to nothing, its consumed quantity the invoiced one.

    How it counts, in this order: a deposit invoice (386) is « Acompte »
    whatever the card said; a credit note naming an invoice held here
    (BT-25, the most recent of that number) counts like it - a credit note
    of a « Déjà comptée par la caisse » invoice, counted, would take off
    revenue never added; else the card's `counting`, a credit note naming
    none said to be checked, and the word « acompte » on an ordinary invoice
    said, never decided."""
    said: list[Said] = []
    if reading.is_deposit:
        counting = SaleDocument.Counting.DEPOSIT
        said.append(Said(INFO, DEPOSIT_SAID))
    elif reading.is_credit_note:
        corrected = (
            SaleDocument.objects.filter(reference__iexact=reading.preceding_number).order_by("-sold_on", "-pk").first()
            if reading.preceding_number
            else None
        )
        if corrected is not None:
            counting = corrected.counting
            said.append(
                Said(INFO, CREDIT_NOTE_OF.format(number=corrected.reference, counting=corrected.get_counting_display()))
            )
        else:
            said.append(Said(WARNING, CREDIT_NOTE_CHECK))
    if reading.mentions_deposit:
        said.append(Said(WARNING, DEPOSIT_WORD_SAID))
    document = SaleDocument(
        reference=reading.number,
        sold_on=sold_on,
        customer=reading.customer,
        customer_identifier=reading.customer_identifier,
        counting=counting,
        stated_total_ttc=reading.total_ttc,
        stated_total_ht=reading.total_ht,
        prepaid_ttc=reading.prepaid,
        payable_ttc=reading.payable,
        adjustment_ht=reading.adjustment_ht,
        adjustment_vat_rate=reading.adjustment_vat_rate,
        einvoice_format=kind,
        # Cut to its column (the kind was read off the whole code already):
        # stored whole - SQLite says nothing - « Données » refused the
        # document for good.
        einvoice_type_code=reading.type_code[: SaleDocument._meta.get_field("einvoice_type_code").max_length],
        einvoice_issued_on=reading.issued,
        einvoice_delivered_on=reading.delivered,
        einvoice_preceding_number=reading.preceding_number,
        einvoice_checks=[dict(check) for check in reading.checks],
        seller_name=reading.seller_name,
        seller_siren=reading.seller_siren,
    )
    lines = [
        SaleDocumentLine(
            label=line.label.strip() or NO_LABEL.format(number=number),
            quantity=line.quantity,
            unit_price_ht=line.unit_price_ht,
            total_ht=line.total_ht,
            vat_rate=line.vat_rate,
            rebuilt=line.rebuilt,
        )
        for number, line in enumerate(reading.lines, start=1)
    ]
    return document, lines, said


def _sold_on(reading: SaleReading, typed_date_text: str) -> tuple[date, str]:
    """(the day the sale counts on, what to say of it) - spec §3.4. The
    delivery date (BT-72, else the billing period's start) when plausible,
    else the issue date (BT-2); plausible is 2000 to today
    (receipts.einvoice_date_problem). Neither: the date typed on the card -
    unreadable, « Date illisible » rather than « nothing typed »; outside
    2000-today, refused in check_document_date's words and given back to the
    card - and with none typed, refused. A date typed beside the invoice's
    own is ignored, and said."""
    typed_text = (typed_date_text or "").strip()
    for day, from_delivery in ((reading.delivered, True), (reading.issued, False)):
        if day is not None and not receipts.einvoice_date_problem(day):
            if typed_text:
                return day, TYPED_DATE_IGNORED.format(day=day_text(day))
            return day, DATE_FROM_DELIVERY.format(day=day_text(day)) if from_delivery else ""
    if typed_text:
        typed = read_date(typed_text)
        if typed is None:
            raise SaleFileRefused(DATE_UNREADABLE)
        try:
            check_document_date(typed)
        except forms.ValidationError as refused:
            raise SaleFileRefused(" ".join(refused.messages), typed_date=typed) from None
        return typed, ""
    stated = reading.issued or reading.delivered
    if stated is None:
        raise SaleFileRefused(SALE_NO_DATE)
    raise SaleFileRefused(SALE_BAD_DATE.format(day=day_text(stated)))


def _check_direction(reading: SaleReading) -> None:
    """A purchase invoice is no sale (spec §3.6): its seller one of the
    bar's suppliers (by a SIREN a supplier's documents taught,
    receipts.identified_supplier), or its buyer the bar (the SIREN the bar's
    own sales invoices state as their seller)."""
    if reading.seller_siren:
        supplier, _found = receipts.identified_supplier(f"SIREN {reading.seller_siren}")
        if supplier is not None:
            raise SaleFileRefused(
                SELLER_IS_A_SUPPLIER.format(siren=siren_text(reading.seller_siren), supplier=supplier.name)
            )
    if reading.customer_siren and SaleDocument.objects.filter(seller_siren=reading.customer_siren).exists():
        raise SaleFileRefused(BUYER_IS_THE_BAR.format(siren=siren_text(reading.customer_siren)))


def read_einvoice_upload(upload, *, typed_date_text: str, counting: str) -> ReadOutcome:
    """« Lire la facture »: the bar's own electronic invoice read, checked,
    dated and stored, with its lines and its file - or refused, nothing
    written (spec §4.4, steps 1-9):

    1. a file, of an electronic invoice's extension, under the upload cap,
       and how it counts;
    2. not a file already held - by a sale document, or by Achats;
    3-4. its XML (a PDF's attachment, or the file), read by einvoice - an
       XML always goes to the reader, which says what is wrong with it;
    5. not a purchase invoice, and not a number another sale document holds;
    6. its date of sale;
    7-8. the document and its lines;
    9. stored in one transaction - and any failure there deletes the file
       just saved; two posts of one file in flight are one document;
    10. committed, the bank's automatic pass (`_bank_pass`)."""
    if not upload:
        raise SaleFileRefused(NO_FILE)
    if file_extension(upload.name) not in EINVOICE_EXTENSIONS:
        raise SaleFileRefused(EINVOICE_EXTENSION_REFUSED)
    too_big = file_too_big(upload)
    if too_big:
        raise SaleFileRefused(too_big)
    if counting not in SaleDocument.Counting.values:
        raise SaleFileRefused(NO_COUNTING)
    with staged(upload) as path:
        digest = receipts.file_sha256(path)
        holder = SaleDocument.objects.filter(source_sha256=digest).first()
        if holder is not None:
            raise SaleFileRefused(ALREADY_ADDED.format(document=titled(holder)))
        refusal = _purchase_refusal(digest)
        if refusal:
            raise SaleFileRefused(refusal)
        xml = einvoice.document_xml(path)
        if xml is None:
            raise SaleFileRefused(NO_EINVOICE_IN_PDF.format(name=upload.name))
        reading = read_sale(xml)
        _check_direction(reading)
        if reading.number:
            same = SaleDocument.objects.filter(reference__iexact=reading.number).order_by("-sold_on", "-pk").first()
            if same is not None:
                raise SaleFileRefused(NUMBER_HELD.format(number=same.reference, day=day_text(same.sold_on)))
        sold_on, date_said = _sold_on(reading, typed_date_text)
        kind = receipts.einvoice_format(path, reading)
        document, lines, said = document_from_reading(reading, kind=kind, sold_on=sold_on, counting=counting)
        _store(document, lines, path=path, name=upload.name, digest=digest)
    # 10. Committed: the credit that already paid it, linked when sure.
    linked, bank_failed = _bank_pass(document)
    return ReadOutcome(
        document=document,
        proposals=len(proposals(document, lines)),
        failed_checks=any(not check["passed"] for check in document.einvoice_checks),
        said=said,
        date_said=date_said,
        linked=linked,
        bank_failed=bank_failed,
    )


def _store(document: SaleDocument, lines: list[SaleDocumentLine], *, path: str, name: str, digest: str) -> None:
    """Step 9: the file saved with the document and its lines, in one
    transaction; whatever fails inside, the file just saved goes."""
    saved = None
    try:
        with transaction.atomic():
            with open(path, "rb") as handle:
                document.source_file.save(storage_name(name), File(handle), save=False)
            saved = document.source_file.name
            document.source_sha256 = digest
            document.save()
            for line in lines:
                line.document = document
            SaleDocumentLine.objects.bulk_create(lines)
    except IntegrityError:
        _forget(saved)
        holder = SaleDocument.objects.filter(source_sha256=digest).first()
        if holder is None:
            raise
        raise SaleFileRefused(ALREADY_ADDED.format(document=titled(holder))) from None
    except BaseException:
        _forget(saved)
        raise


# -- the typed form ---------------------------------------------------------------------------------------------------


def save_typed(form, formset, *, staged_path: str | None = None, upload_name: str = "", remove_file: bool = False):
    """A typed document saved - `form` and `formset` already valid, the file
    (`staged_path`, staged from `upload_name`) already checked
    (`typed_file_problem`). In one transaction: the header; a new file saved
    and its sha the staged file's - a new file WINS over « Retirer le
    fichier » ticked with it -, else « Retirer » empties the field; the
    lines; then every recipe line with no price of its own given the menu's
    price of the day (spec §2.2: no document's money moves after it is saved
    - every line, not only the forms that changed). A file replaced or
    removed goes once the transaction commits. Any failure inside deletes
    the file just saved; two posts of one file in flight are refused
    (FILE_TAKEN). Committed, with something to receive, the bank's automatic
    pass (`_bank_pass`). Returns a SavedOutcome."""
    document = form.instance
    was_new = document._state.adding
    storage = document.source_file.storage
    # What the instance names before the block: a failure gives it back, the
    # page being drawn again from this very instance.
    before = (document.source_file.name, document.source_sha256)
    saved = None
    digest = receipts.file_sha256(staged_path) if staged_path else ""
    try:
        with transaction.atomic():
            # The row read again inside the transaction (IMMEDIATE: the write
            # lock is held): the instance was loaded when the request began,
            # and another tab may have deleted it, or given it another file,
            # since.
            row = None
            if not was_new:
                row = SaleDocument.objects.filter(pk=document.pk).values("source_file", "source_sha256").first()
                if row is None:
                    raise DocumentGone
            old = (storage, row["source_file"]) if row and row["source_file"] else None
            document = form.save(commit=False)
            gone = None
            if staged_path:
                with open(staged_path, "rb") as handle:
                    document.source_file.save(storage_name(upload_name), File(handle), save=False)
                saved = document.source_file.name
                document.source_sha256 = digest
                gone = old
            elif remove_file and old is not None:
                document.source_file = None
                document.source_sha256 = ""
                gone = old
            elif row is not None:
                # A header-only save writes back the file the row names now,
                # never the one a stale instance carried.
                document.source_file = row["source_file"] or None
                document.source_sha256 = row["source_sha256"]
            document.save(force_update=not was_new)
            formset.instance = document
            formset.save()
            prices_written = _write_menu_prices(document)
            if gone is not None and not _same_name(gone[1], saved):
                transaction.on_commit(lambda: _delete_unreferenced([gone]))
    except IntegrityError:
        _forget(saved)
        _unsaved(document, was_new, before)
        holder = SaleDocument.objects.filter(source_sha256=digest).exclude(pk=document.pk).first() if digest else None
        if holder is None:
            raise
        raise SaleFileRefused(FILE_TAKEN.format(document=titled(holder))) from None
    except BaseException:
        _forget(saved)
        _unsaved(document, was_new, before)
        raise
    linked, bank_failed = _bank_pass(document) if document.to_pay > 0 else ([], False)
    return SavedOutcome(document=document, prices_written=prices_written, linked=linked, bank_failed=bank_failed)


def _same_name(one: str | None, other: str | None) -> bool:
    """Whether two stored names are one file on this disk: case aside on
    Windows (`os.path.normcase`), either separator. A file attached again
    under the very name its row still named - gone from the disk, so the
    storage kept it - is the file just written, never one to delete."""
    if not one or not other:
        return False
    return os.path.normcase(one.replace("\\", "/")) == os.path.normcase(other.replace("\\", "/"))


def _delete_unreferenced(files) -> None:
    """`delete_stored_files`, once a commit has dropped what named them -
    each only while no sale document names it any more (compared case
    aside, as Windows finds a file): a file another row took meanwhile
    stays."""
    delete_stored_files(
        [
            (storage, name)
            for storage, name in files
            if not SaleDocument.objects.filter(source_file__iexact=name).exists()
        ]
    )


def _unsaved(document: SaleDocument, was_new: bool, before: tuple[str | None, str]) -> None:
    """A document whose save was rolled back is as it was - its page is
    drawn again for it: new again, not the row that never was, and naming
    the file it still has, not the one just deleted (nor none, when
    « Retirer » was ticked)."""
    if was_new:
        document.pk = None
        document._state.adding = True
    document.source_file, document.source_sha256 = before


def _write_menu_prices(document: SaleDocument) -> int:
    """The menu's price of the day written into every recipe line of
    `document` with no price of its own - one bulk update. How many."""
    lines = [
        line
        for line in document.lines.filter(recipe__isnull=False, unit_price_ttc__isnull=True).select_related("recipe")
        if line.recipe.selling_price_ttc is not None
    ]
    for line in lines:
        line.unit_price_ttc = line.recipe.selling_price_ttc
    SaleDocumentLine.objects.bulk_update(lines, ["unit_price_ttc"])
    return len(lines)


# -- the deletion -----------------------------------------------------------------------------------------------------


def delete_document(document: SaleDocument) -> DeletedDocument | None:
    """`document` deleted, with its lines and its bank links (CASCADE - the
    credits stay, as when an invoice and its payment are deleted), its file
    once that commits. None when it was gone already (a double tap, a second
    tab). The file deleted is the one the row names INSIDE the transaction,
    never the one an instance loaded earlier named: another tab may have
    replaced it since."""
    with transaction.atomic():
        row = SaleDocument.objects.filter(pk=document.pk).values_list("source_file", flat=True).first()
        if row is None:
            return None
        files = [(document.source_file.storage, row)] if row else []
        links = document.bank_payments.count()
        label = str(document)
        deleted, _by_model = document.delete()
        if not deleted:
            return None
        if files:
            transaction.on_commit(lambda: _delete_unreferenced(files))
    return DeletedDocument(label=label, had_file=bool(files), links=links)
