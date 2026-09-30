"""The writer: every slip enters the database through `store_slip` - the page's
upload (`store_uploads`), the mailbox gather (returnables/mail.py) and
Achats' guard (a slip dropped among the invoices) alike - and is read again
only through `reread` / `reread_format`.

`store_slip(content, filename=…, fmt=None, origin=…)`, in this order:

1. At most 5 MB (reading.MAX_PDF_BYTES), then the SHA-256: the same bytes
   twice are one slip - « déjà reçu ».
2. The PDF's text, OUTSIDE any transaction (pdfminer takes seconds on a bad
   file, and SQLite's write lock would be held all along). A document that
   is not a PDF, is too long, or has no text layer is refused with the
   reading's own sentence.
3. The format: the one given, else the one format that recognises the text
   (reading.detect_format - none or several is a refusal). Then the reading,
   with its own 2 s budget, still outside the transaction.
4. The re-send: a slip of the same format with the same number, delivery
   date, references and lines (the driver e-mails a ticket twice, printed
   again the next morning) is « bon n° X déjà reçu ». Only a reading that
   says WHICH slip it is (a number or a reference) is ever one: two
   deliveries of the same three kegs on one day are two slips.
5. A reading that failed (a pattern that no longer compiles, or ran out of
   time) is STORED - its file, its text, no line, the failed check - so that
   « Relire » can fix it once the format is corrected. A mailed slip refused
   here would be lost for good once its mail leaves the gather's overlap.
6. The file: `bon-<slug>.pdf`, the slug being the number kept to
   [0-9A-Za-z-] and 20 characters (a captured number can hold « / » or
   « .. », and the storage would take them for folders), else the delivery
   date, else « sans-numero ». Saved INSIDE the atomic block with the row and
   its lines; the handlers are OUTSIDE it: an IntegrityError on the sha (the
   same bytes stored at the same moment by an upload and a gather) deletes
   the file just saved and answers « déjà reçu », anything else deletes it
   and is raised. Django has no « on rollback » hook: that is this code.

Nothing here ever calls receipts.import_document, importing.parse_and_import
or import_parsed_invoice: a slip is not an invoice (its empties would be filed
as purchases - silently wrong money), and nothing about a slip is written on
an invoice.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from returnables import patterns, reading
from returnables.comparison import slip_label
from returnables.models import Slip, SlipFormat, SlipLine, delete_files
from returnables.reading import SlipError, SlipReading, clean_text

logger = logging.getLogger(__name__)

#: What `store_slip` did with a document (`StoreResult.kind`).
CREATED = "created"
DUPLICATE = "duplicate"
RESEND = "resend"
REFUSED = "refused"
#: Only with `skip_non_slips=True` (the gather): read with its format, the
#: document has no returnables part and no line - a mail's other attachment.
IGNORED = "ignored"

#: The columns a mail's or a file's texts are cut to (Slip).
MAX_NAME_CHARS = 200
MAX_MAIL_CHARS = 300
MAX_ERROR_CHARS = 300
#: A file name's slug: the number, cut to this.
SLUG_CHARS = 20
#: How many refused files an upload's summary names before « … et N autres ».
LISTED_REFUSALS = 10

UNEXPECTED = "erreur inattendue : le fichier n'a pas été enregistré, réessayez."


@dataclass
class StoreResult:
    """What became of one document. `slip` is the slip created, or the one
    already there (duplicate, re-send); None when refused or ignored.
    `message` is a French sentence WITHOUT the file's name - the caller puts
    « nom : » in front. `reading` is what the format read (None when the
    document was refused before being read)."""

    slip: Slip | None
    created: bool
    kind: str
    message: str
    reading: SlipReading | None = None


# -- Small helpers --------------------------------------------------------------------------------------------------


def document_text(text) -> str:
    """The text a slip is read from and stored with: line ends as "\\n",
    control characters dropped (a NUL is no character a page can show),
    "\\n" and "\\t" kept."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc")


def file_name(number, delivery_date) -> str:
    """The stored file's name: flat and safe whatever the pattern captured -
    « 12/34 » is bon-1234.pdf, « ../x » is bon-x.pdf."""
    slug = re.sub(r"[^0-9A-Za-z-]", "", number or "").strip("-")[:SLUG_CHARS].strip("-")
    if not slug and delivery_date is not None:
        slug = f"{delivery_date:%Y%m%d}"
    return f"bon-{slug or 'sans-numero'}.pdf"


def not_a_slip(result: SlipReading) -> bool:
    """A document the format does not recognise as one of its slips: it has a
    start pattern, no line matched it, and nothing was read. A reading that
    FAILED is not this (its patterns, not the document, are the problem)."""
    return result.error is None and result.section_found is False and not result.lines


def _title(slip) -> str:
    """« bon n° 12 du 14/05/2025 », « bon sans numéro » - mid-sentence."""
    return slip_label(slip.number, slip.delivery_date, with_date=True)


def _references(value) -> list:
    return [reference for reference in value if isinstance(reference, str)] if isinstance(value, list) else []


def _mail_day(value):
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value if isinstance(value, date) else None


def _reading_fields(result: SlipReading) -> dict:
    """The Slip columns a reading rewrites."""
    return {
        "delivery_date": result.delivery_date,
        "printed_at": result.printed_at,
        "number": result.number or "",
        "references": list(result.references),
        "replaces": bool(result.replaces),
        "printed_total": result.printed_total,
        "remarks": result.remarks or "",
        "checks": [check.as_dict() for check in result.checks],
        "read_error": (result.error or "")[:MAX_ERROR_CHARS],
        "read_at": timezone.now(),
    }


def _write_lines(slip: Slip, result: SlipReading) -> None:
    if result.lines:
        SlipLine.objects.bulk_create(
            SlipLine(
                slip=slip,
                position=position,
                designation=line.designation,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                amount=line.amount,
            )
            for position, line in enumerate(result.lines, start=1)
        )


def _stored(sha: str) -> Slip | None:
    return Slip.objects.filter(sha256=sha).first()


def _resend_of(fmt, result: SlipReading) -> Slip | None:
    """The slip this reading is a re-send of: same format, number, delivery
    date, references and lines (designation, quantity, unit price, amount,
    in order). Never for a failed reading, nor one that names no slip (no
    number and no reference)."""
    if result.error is not None or not (result.number or result.references):
        return None
    references = sorted(result.references)
    lines = [line.as_tuple() for line in result.lines]
    candidates = (
        Slip.objects.filter(format=fmt, number=result.number or "", delivery_date=result.delivery_date)
        .prefetch_related("lines")
        .order_by("pk")
    )
    for candidate in candidates:
        if sorted(_references(candidate.references)) != references:
            continue
        stored = [(line.designation, line.quantity, line.unit_amount, line.amount) for line in candidate.lines.all()]
        if stored == lines:
            return candidate
    return None


def _refused(message: str, result: SlipReading | None = None) -> StoreResult:
    return StoreResult(None, False, REFUSED, message, result)


def _duplicate(slip: Slip, result: SlipReading | None = None) -> StoreResult:
    return StoreResult(slip, False, DUPLICATE, f"déjà reçu ({_title(slip)}).", result)


def _created_message(slip: Slip, result: SlipReading) -> str:
    title = _title(slip)
    if result.error:
        return f"{title} ajouté, mais il n'a pas pu être lu : {result.error}"
    failed = [check.label for check in result.failed_checks]
    if failed:
        return f"{title} ajouté ; à vérifier : {', '.join(failed)}."
    return f"{title} ajouté."


def _forget(slip: Slip, saved: str | None) -> None:
    """The file saved for a row that was not written."""
    if saved:
        delete_files([(slip.file.storage, saved)])


# -- The writer -------------------------------------------------------------------------------------------------------


def store_slip(
    content,
    *,
    filename,
    fmt=None,
    origin,
    mail_sender="",
    mail_subject="",
    mail_date=None,
    text: str | None = None,
    skip_non_slips: bool = False,
) -> StoreResult:
    """Store one document as a slip (see the module docstring). Never inside a
    transaction of the caller's: it reads the PDF before writing anything.

    `fmt`: the format to read it with; None = the one that recognises it.
    `text`: the PDF's text when the caller already has it from
    reading.pdf_text (Achats' guard), so it is not extracted twice.
    `skip_non_slips` (the gather): a document its format does not recognise
    as a slip (`not_a_slip`) is IGNORED, not stored."""
    if origin not in Slip.Origin.values:
        raise ValueError(f"origin must be one of {Slip.Origin.values}, not {origin!r}")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        return _refused(reading.NOT_A_PDF)
    content = bytes(content)
    if len(content) > reading.MAX_PDF_BYTES:
        return _refused(reading.TOO_HEAVY)
    sha = hashlib.sha256(content).hexdigest()
    existing = _stored(sha)
    if existing is not None:
        return _duplicate(existing)

    if text is None:
        try:
            text = reading.pdf_text(content)
        except SlipError as error:
            return _refused(error.message)
    text = document_text(text)

    if fmt is None:
        formats = SlipFormat.objects.filter(is_active=True).exclude(section_start="").order_by("name", "pk")
        try:
            fmt = reading.detect_format(text, formats)
        except SlipError as error:
            return _refused(error.message)
        if fmt is None:
            return _refused(reading.NO_FORMAT)

    result = reading.read_slip_text(text, fmt)
    if skip_non_slips and not_a_slip(result):
        return StoreResult(None, False, IGNORED, f"pas un bon « {fmt.name} » — ignoré.", result)
    resent = _resend_of(fmt, result)
    if resent is not None:
        return StoreResult(resent, False, RESEND, f"{_title(resent)} déjà reçu : ce document en est un renvoi.", result)

    slip = Slip(
        format=fmt,
        origin=origin,
        sha256=sha,
        original_name=clean_text(filename, MAX_NAME_CHARS),
        mail_sender=clean_text(mail_sender, MAX_MAIL_CHARS),
        mail_subject=clean_text(mail_subject, MAX_MAIL_CHARS),
        mail_date=_mail_day(mail_date),
        text=text,
        **_reading_fields(result),
    )
    saved = None
    try:
        with transaction.atomic():
            slip.file.save(file_name(result.number, result.delivery_date), ContentFile(content), save=False)
            saved = slip.file.name
            slip.save()
            _write_lines(slip, result)
    except IntegrityError:
        _forget(slip, saved)
        existing = _stored(sha)
        if existing is None:
            raise
        return _duplicate(existing, result)
    except BaseException:
        _forget(slip, saved)
        raise
    return StoreResult(slip, True, CREATED, _created_message(slip, result), result)


# -- The page's upload ------------------------------------------------------------------------------------------------


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {plural if count > 1 else singular}"


@dataclass
class UploadSummary:
    """What an upload of several documents became, said in ONE message:
    « 7 documents : 5 bons ajoutés, 1 déjà reçu, 1 renvoi d'un bon déjà
    reçu, 0 refusé. » then at most LISTED_REFUSALS « nom : raison »."""

    results: list = field(default_factory=list)  # [(file name, StoreResult)]

    def _count(self, kind: str) -> int:
        return sum(1 for _name, result in self.results if result.kind == kind)

    @property
    def created(self) -> int:
        return self._count(CREATED)

    @property
    def duplicates(self) -> int:
        return self._count(DUPLICATE)

    @property
    def resends(self) -> int:
        return self._count(RESEND)

    @property
    def refused(self) -> int:
        return self._count(REFUSED)

    @property
    def refused_results(self) -> list:
        return [(name, result) for name, result in self.results if result.kind == REFUSED]

    @property
    def unrecognised(self) -> list:
        """The names refused because no format recognises them - the page
        offers « Nouveau format » beside them."""
        return [name for name, result in self.refused_results if result.message == reading.NO_FORMAT]

    @property
    def refusals(self) -> list:
        """« nom : raison », the first LISTED_REFUSALS."""
        return [f"{name} : {result.message}" for name, result in self.refused_results[:LISTED_REFUSALS]]

    @property
    def more_refused(self) -> int:
        return max(0, self.refused - LISTED_REFUSALS)

    @property
    def sentence(self) -> str:
        parts = [
            _plural(self.created, "bon ajouté", "bons ajoutés"),
            _plural(self.duplicates, "déjà reçu", "déjà reçus"),
            _plural(self.resends, "renvoi d'un bon déjà reçu", "renvois d'un bon déjà reçu"),
            _plural(self.refused, "refusé", "refusés"),
        ]
        return f"{_plural(len(self.results), 'document', 'documents')} : {', '.join(parts)}."

    @property
    def message(self) -> str:
        parts = [self.sentence, *self.refusals]
        if self.more_refused:
            parts.append(f"… et {self.more_refused} autres.")
        return " ".join(parts)

    @property
    def has_refusals(self) -> bool:
        return self.refused > 0


def store_uploads(files, fmt=None) -> UploadSummary:
    """Every PDF of one upload, through `store_slip` (origin « Déposé à la
    main »), one after the other - one file's failure is that file's
    refusal, never the others'. A file over 5 MB is refused before it is
    read. `fmt` None: each document's format is recognised."""
    summary = UploadSummary()
    for uploaded in files:
        name = clean_text(getattr(uploaded, "name", "") or "", MAX_NAME_CHARS) or "document"
        size = getattr(uploaded, "size", None)
        if isinstance(size, int) and size > reading.MAX_PDF_BYTES:
            summary.results.append((name, _refused(reading.TOO_HEAVY)))
            continue
        try:
            content = uploaded.read()
            result = store_slip(content, filename=name, fmt=fmt, origin=Slip.Origin.UPLOAD)
        except Exception:
            logger.exception("Bon « %s » : erreur inattendue à l'enregistrement", name)
            result = _refused(UNEXPECTED)
        summary.results.append((name, result))
    return summary


# -- Reading again ------------------------------------------------------------------------------------------------------


def reread(slip: Slip, *, budget=None) -> SlipReading:
    """Read `slip` again from the text it was stored with (never the PDF,
    never an invoice reader), with its format as it is NOW, and rewrite its
    reading and its lines in one atomic block - the patterns run before it.
    Returns the reading; `slip`'s attributes are updated too. A slip deleted
    meanwhile is left alone."""
    result = reading.read_slip_text(slip.text or "", slip.format, budget=budget)
    fields = _reading_fields(result)
    with transaction.atomic():
        if Slip.objects.filter(pk=slip.pk).update(**fields):
            SlipLine.objects.filter(slip_id=slip.pk).delete()
            _write_lines(slip, result)
    for name, value in fields.items():
        setattr(slip, name, value)
    return result


def reread_format(fmt: SlipFormat, *, budget=None) -> tuple[int, int]:
    """« Relire les N bons de ce format »: every slip of `fmt`, newest first,
    within REREAD_SECONDS in all. A slip is started only while a whole
    reading's time is left - one cut short by the page's budget would be
    stored as « trop lent » though its patterns are fine. Returns (re-read,
    left): the page says how many are left to « Relire » again."""
    budget = budget or patterns.Budget(patterns.REREAD_SECONDS)
    slips = list(Slip.objects.filter(format=fmt).only("pk", "text", "format_id").order_by("-received_at", "-pk"))
    done = 0
    for index, slip in enumerate(slips):
        if budget.remaining() < patterns.READING_SECONDS:
            return done, len(slips) - index
        slip.format = fmt
        reread(slip)
        done += 1
    return done, 0
