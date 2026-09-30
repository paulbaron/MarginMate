"""Reading a slip: its PDF's text, then what a format's patterns find in it.

- `pdf_text(content)` - the text layer of a slip's PDF, bounded before and
  while it is extracted (5 MB, 5 pages, 200 000 characters): a 40-page
  invoice dropped by mistake is refused, not extracted whole. Never call it
  inside a transaction: pdfminer takes seconds on a bad file, and SQLite's
  write lock would be held all along.
- **What pdfminer inflates is bounded too** (`bound_pdf_decoding`, done when
  this module is imported, process-wide): the 5 MB are COMPRESSED bytes,
  deflate reaches about 1000:1 and two Flate filters chained far more, so a
  spoofed mail's slip inflated to gigabytes before any page or character cap
  applied. pdfminer's Flate (and its retry of a damaged stream, which it
  runs on any zlib.error), LZW and RunLength decoders are replaced by
  bounded ones: MAX_INFLATE_STAGE bytes a stream and a filter, and a slip's
  streams share MAX_INFLATE_TOTAL (`inflate_budget`). Past it,
  `InflateLimit` - never a zlib.error, which pdfminer would retry without a
  bound - and the slip is « trop long ». Achats' own pdfplumber pass gets
  the per-stream bound as well.
- `read_slip_text(text, fmt)` - a `SlipReading`: the returnables part's
  lines, what could not be read, the delivery date, the number, the
  delivery-note references, « annule et remplace », the printed total, the
  remarks, and the checks the page shows. `fmt` is any object carrying a
  format's pattern attributes (a SlipFormat, the format form's unsaved
  values, a test's SimpleNamespace): the module imports no model.
- `detect_format(text, formats)` - which format recognises a document.
- `trace(text, fmt)` - « Tester »: the text line by line, each line with
  what the reading made of it.

The rules, and why (spec §5; the traps are the owner's real tickets'):

- A line longer than 500 characters is never matched (it goes to `unread`,
  shown cut to 120): cut for matching, « = 12345 » would read out of
  « = 12345.67 ».
- Header patterns (date, printed, number, reference, total, replaces) are
  searched LINE BY LINE, pattern order first, then line order: `\\s` never
  crosses a line (invoices/parsers/uba.py has been bitten by a `\\s`
  swallowing a newline), and the first date pattern - the delivery note's
  date - wins over the print date: a slip re-sent the next day keeps its
  delivery day.
- The returnables part starts after the FIRST line matching section_start and
  ends before the first LATER line matching section_end. A part starting
  again after its end is read once, and a check says so.
- A captured value counts only when its group took part and is not blank. A
  line whose designation has fewer than 2 letters or digits, or whose
  quantity or amount does not read (or is wider than its column), is
  UNREAD: listed, never guessed.
- A pattern that does not compile any more, or runs out of time, gives a
  reading with `error` set, no line, one failed check - never a 500.

Budgets: a reading has READING_SECONDS in all (patterns.Budget).
"""

from __future__ import annotations

import io
import re
import unicodedata
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

from returnables import patterns
from returnables.patterns import (
    Budget,
    PatternError,
    captured,
    read_amount,
    read_date,
    read_quantity,
    read_time,
)

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 5
MAX_TEXT_CHARS = 200_000
#: What pdfminer may inflate: one stream through one filter (everywhere in
#: this process), and all the streams of one slip together (pdf_text). A
#: real slip's page is a few KB; an invoice's embedded font a few hundred.
MAX_INFLATE_STAGE = 64 * 1024 * 1024
MAX_INFLATE_TOTAL = 64 * 1024 * 1024
#: A longer line is never matched, and shown cut to SHOWN_LINE_CHARS.
MAX_LINE_CHARS = 500
SHOWN_LINE_CHARS = 120
#: A returnables part longer than this is refused: the patterns are wrong.
MAX_SECTION_LINES = 300
MAX_REFERENCES = 20
MAX_REFERENCE_CHARS = 40
MIN_REFERENCE_ALNUMS = 3
#: The columns a reading's texts are cut to (Slip.number, SlipLine.designation).
MAX_NUMBER_CHARS = 40
MAX_DESIGNATION_CHARS = 200
#: The unread lines a check lists before « … et N autres ».
LISTED = 5

#: A line of dashes, equals, stars, pluses, dots - or blank.
SEPARATOR = re.compile(r"^[\s\-=_*+.]*$")

NOT_A_PDF = "Ce fichier n'est pas un PDF lisible."
TOO_HEAVY = "Ce document est trop lourd pour un bon (5 Mo au plus)."
TOO_LONG = "Ce document est trop long pour un bon."
NO_TEXT = "Ce PDF n'a pas de texte lisible (scan ou photo) : il ne peut pas être lu comme un bon."
SECTION_TOO_LONG = (
    f"Partie des consignes trop longue (plus de {MAX_SECTION_LINES} lignes) : "
    "vérifiez les motifs de début et de fin de la partie."
)
#: What detect_format's None means, for the callers to say.
NO_FORMAT = "Aucun format de bon ne reconnaît ce document."
TEXT_TOO_LONG = f"Ce texte est trop long pour un bon ({MAX_TEXT_CHARS} caractères au plus)."


class SlipError(Exception):
    """A document that cannot be read as a slip: `message` is the French
    sentence."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass
class ReadLine:
    """A line of the returnables part, as printed: quantity signed, the unit
    price (4 decimals) and the amount (2) when the pattern has them."""

    designation: str
    quantity: int
    unit_amount: Decimal | None = None
    amount: Decimal | None = None

    def as_tuple(self) -> tuple:
        return (self.designation, self.quantity, self.unit_amount, self.amount)


@dataclass
class Check:
    """One thing the reading verified - stored as {label, passed, detail}."""

    label: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"label": self.label, "passed": self.passed, "detail": self.detail}


@dataclass
class SlipReading:
    """What a format's patterns found in a slip's text. `section_found` is
    None when the format has no section_start (the whole text is read).
    `error` set means nothing was read: `lines` is empty and `checks` is the
    one failed « Lecture impossible »."""

    section_found: bool | None = None
    lines: list = field(default_factory=list)
    unread: list = field(default_factory=list)
    delivery_date: date | None = None
    printed_at: datetime | None = None
    number: str = ""
    references: list = field(default_factory=list)
    replaces: bool = False
    printed_total: Decimal | None = None
    remarks: str = ""
    checks: list = field(default_factory=list)
    error: str | None = None

    @property
    def all_passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list:
        return [check for check in self.checks if not check.passed]


@dataclass
class TraceTag:
    """What « Tester » says of a line: « ligne lue », « date »…, with what
    was read (`value`) - for « non lue », why."""

    label: str
    value: str = ""


@dataclass
class TraceLine:
    number: int
    text: str
    tags: list = field(default_factory=list)
    read: ReadLine | None = None


@dataclass
class Trace:
    """« Tester »'s result: every line of the text with its tags, and the
    reading itself (its checks, date, number, references…). Iterating it
    gives the lines."""

    lines: list
    reading: SlipReading

    def __iter__(self):
        return iter(self.lines)

    def __len__(self):
        return len(self.lines)


class _ReadingFailed(Exception):
    pass


# -- What pdfminer inflates -------------------------------------------------------------------------------------------


class InflateLimit(Exception):
    """A PDF stream decoding past its bound. Deliberately NOT a zlib.error:
    pdfminer answers one with decompress_corrupted, a byte-by-byte retry."""


#: [bytes left] of the reading under way in this thread (pdf_text), or None.
_BUDGET: ContextVar[list | None] = ContextVar("returnables_inflate_budget", default=None)


@contextmanager
def inflate_budget(total: int):
    """Every pdfminer decode inside - in this thread - shares `total` bytes:
    the streams of one document together (a page may list hundreds). Yields
    the budget, [bytes left]: -1 once a decode was refused."""
    budget = [total]
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def inflate_refused(error: BaseException | None) -> bool:
    """Whether InflateLimit is behind `error`: pdfplumber re-raises what
    pdfminer raises as its own PdfminerException."""
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, InflateLimit):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def _allowance() -> int:
    """What the next decode may give: the per-stream bound, within what the
    reading under way has left (-1 once it has been refused)."""
    budget = _BUDGET.get()
    return MAX_INFLATE_STAGE if budget is None else min(MAX_INFLATE_STAGE, budget[0])


def _charge(size: int) -> None:
    budget = _BUDGET.get()
    if budget is not None:
        budget[0] -= size


def _refuse():
    """Past the bound: this decode, and every later one of the same
    reading (pdfminer may swallow an error and try another way)."""
    budget = _BUDGET.get()
    if budget is not None:
        budget[0] = -1
    raise InflateLimit("flux PDF trop gros une fois décompressé")


def bounded_decompress(data) -> bytes:
    """zlib.decompress(data), never past _allowance(): zlib inflates at most
    that + 1 byte, and a stream giving more is InflateLimit. What
    zlib.decompress refuses (damaged, truncated, not zlib) is the same
    zlib.error."""
    allowance = _allowance()
    if allowance < 0:
        _refuse()
    inflater = zlib.decompressobj()
    output = inflater.decompress(data, allowance + 1)
    if len(output) > allowance:
        _refuse()
    if not inflater.eof:
        raise zlib.error("Error -5 while decompressing data: incomplete or truncated stream")
    _charge(len(output))
    return output


class _BoundedInflater:
    """zlib.decompressobj() for pdfminer's retry of a damaged stream
    (decompress_corrupted feeds it one byte at a time): the same bound, over
    everything it gives. Only `decompress` - pdfminer calls nothing else,
    and flush() would inflate what the bound left unconsumed."""

    def __init__(self, *args, **kwargs):
        self._inflater = zlib.decompressobj(*args, **kwargs)
        self._room = _allowance()

    def decompress(self, data, max_length=0) -> bytes:
        room = min(self._room, _allowance())
        if room < 0:
            _refuse()
        output = self._inflater.decompress(data, room + 1)
        if len(output) > room:
            _refuse()
        self._room -= len(output)
        _charge(len(output))
        return output

    @property
    def eof(self) -> bool:
        return self._inflater.eof


def bounded_lzwdecode(data) -> bytes:
    """pdfminer's lzwdecode, stopped past _allowance() (LZW reaches about
    3 500:1)."""
    from pdfminer.lzw import LZWDecoder

    allowance = _allowance()
    if allowance < 0:
        _refuse()
    chunks, size = [], 0
    for chunk in LZWDecoder(io.BytesIO(data)).run():
        size += len(chunk)
        if size > allowance:
            _refuse()
        chunks.append(chunk)
    _charge(size)
    return b"".join(chunks)


def _run_length_size(data) -> int:
    """What RunLengthDecode gives `data`, counted without decoding it."""
    size, index, end = 0, 0, len(data)
    while index < end:
        length = data[index]
        if length == 128:
            break
        if length < 128:
            size += length + 1
            index += length + 2
        else:
            size += 257 - length
            index += 2
    return size


def bounded_rldecode(data) -> bytes:
    """pdfminer's rldecode (two bytes give up to 128), refused before it
    runs when what it would give is past _allowance()."""
    from pdfminer.runlength import rldecode

    allowance = _allowance()
    if allowance < 0 or (len(data) * 64 > allowance and _run_length_size(data) > allowance):
        _refuse()
    output = rldecode(data)
    _charge(len(output))
    return output


#: What pdfminer.pdftypes finds under the name `zlib` once bounded.
_BOUNDED_ZLIB = SimpleNamespace(decompress=bounded_decompress, decompressobj=_BoundedInflater, error=zlib.error)


def bound_pdf_decoding() -> None:
    """Put the bounded decoders in pdfminer.pdftypes, for the whole process
    (idempotent): the slips' reading and Achats' text layer alike. Left as
    pdfminer has them: ASCIIHex (shrinks), ASCII85 (4:1 at most, after a
    bounded stage), images (never decoded to read text) and CCITTFax (its
    rows grow with Columns, but pdfminer's pure-Python decoder crawls)."""
    from pdfminer import pdftypes

    if pdftypes.zlib is _BOUNDED_ZLIB:
        return
    pdftypes.zlib = _BOUNDED_ZLIB
    pdftypes.lzwdecode = bounded_lzwdecode
    pdftypes.rldecode = bounded_rldecode


bound_pdf_decoding()


# -- The PDF --------------------------------------------------------------------------------------------------------


def pdf_text(content: bytes) -> str:
    """The text layer of a slip's PDF, pages joined by a newline - or
    SlipError: over 5 MB, over 5 pages or 200 000 characters (checked while
    extracting, page by page), streams inflating past MAX_INFLATE_STAGE or,
    together, MAX_INFLATE_TOTAL (« trop long »), no text at all (a scan),
    or not a PDF pdfplumber can read (pdfminer raises a zoo of exceptions:
    every one is « pas un PDF lisible »)."""
    if not isinstance(content, (bytes, bytearray, memoryview)) or not len(content):
        raise SlipError(NOT_A_PDF)
    if len(content) > MAX_PDF_BYTES:
        raise SlipError(TOO_HEAVY)
    import pdfplumber

    bound_pdf_decoding()
    texts, budget = [], None
    try:
        with inflate_budget(MAX_INFLATE_TOTAL) as budget:
            # Counted before pdfplumber opens it: `len(pdf.pages)` makes
            # every page, and so does closing the document.
            pages = _page_count(bytes(content), MAX_PDF_PAGES + 1)
            if not pages:
                # No page at all is a broken file, not a scan.
                raise SlipError(NOT_A_PDF)
            if pages > MAX_PDF_PAGES:
                raise SlipError(TOO_LONG)
            with pdfplumber.open(io.BytesIO(bytes(content))) as pdf:
                texts = _page_texts(pdf)
    except SlipError:
        raise
    except Exception as error:  # noqa: BLE001 - pdfminer's zoo of errors is a French refusal, never a 500
        # However pdfplumber wrapped it (PdfminerException), a decode past
        # its bound left the budget refused.
        if (budget is not None and budget[0] < 0) or inflate_refused(error):
            raise SlipError(TOO_LONG) from None
        raise SlipError(NOT_A_PDF) from None
    text = "\n".join(texts)
    if not text.strip():
        raise SlipError(NO_TEXT)
    return text


def _page_count(content: bytes, limit: int) -> int:
    """How many pages pdfplumber would iterate, counted up to `limit`:
    pdfminer's own walk of the page tree, which makes no page and stops
    there, whatever the file declares (security review of the HARDEN-01
    fix: counted by pdfplumber, a 5 MB slip of some 33 000 light pages took
    about 30 s and 100 MB to refuse)."""
    import itertools

    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    document = PDFDocument(PDFParser(io.BytesIO(content)))
    return sum(1 for _page in itertools.islice(PDFPage.create_pages(document), limit))


def _page_texts(pdf) -> list:
    """Each page's text, in order, stopped past MAX_TEXT_CHARS (TOO_LONG)."""
    texts, size = [], 0
    for page in pdf.pages:
        text = page.extract_text() or ""
        size += len(text) + 1
        if size > MAX_TEXT_CHARS:
            raise SlipError(TOO_LONG)
        texts.append(text)
    return texts


# -- Small helpers --------------------------------------------------------------------------------------------------


def split_lines(text) -> list:
    """The text's lines, "\\r\\n" and "\\r" taken as "\\n" (a pasted text)."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def shown_line(line: str) -> str:
    """A line as a page shows it: cut to 120 characters when too long to be
    matched."""
    if len(line) > MAX_LINE_CHARS:
        return line[:SHOWN_LINE_CHARS] + "…"
    return line


def clean_text(value, limit: int) -> str:
    """A text read from a document, for its column: control characters
    dropped, spaces collapsed, cut to `limit`."""
    if not value:
        return ""
    kept = "".join(" " if unicodedata.category(char) == "Cc" else char for char in value)
    return " ".join(kept.split())[:limit]


def _alnums(value: str) -> int:
    return sum(1 for char in value if char.isalnum())


def french_number(value: Decimal) -> str:
    """30,00 - or 0,3333 when it has more decimals than cents."""
    if value == value.quantize(Decimal("0.01")):
        text = f"{value:.2f}"
    else:
        text = f"{value.normalize():f}"
    return text.replace(".", ",")


def line_amount(line: ReadLine) -> Decimal | None:
    """The line's amount: printed, else quantity × unit price (to the cent)."""
    if line.amount is not None:
        return line.amount
    if line.unit_amount is not None:
        return (line.quantity * line.unit_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return None


def _listed(values: list) -> str:
    shown = " ; ".join(
        f"« {value if len(value) <= SHOWN_LINE_CHARS else value[:SHOWN_LINE_CHARS] + '…'} »"
        for value in values[:LISTED]
    )
    if len(values) > LISTED:
        shown += f" … et {len(values) - LISTED} autres"
    return shown


def _lines_label(count: int) -> str:
    return f"{count} ligne lue" if count <= 1 else f"{count} lignes lues"


# -- The reader -----------------------------------------------------------------------------------------------------


class _Reader:
    """One reading of one text with one format's compiled patterns, keeping
    what it made of each line for « Tester »."""

    def __init__(self, text, regexes: dict, budget: Budget):
        self.lines = split_lines(text)
        self.regexes = regexes
        self.budget = budget
        self.tags = [[] for _ in self.lines]
        self.read_lines = {}
        self.reading = SlipReading()

    # -- matching --

    def _usable(self, index: int) -> bool:
        return len(self.lines[index]) <= MAX_LINE_CHARS

    def _first_line(self, pattern, begin: int = 0) -> int | None:
        for index in range(begin, len(self.lines)):
            if self._usable(index) and patterns.search(pattern, self.lines[index], self.budget):
                return index
        return None

    def _first_value(self, attr: str, value_of) -> tuple:
        """The first value `value_of(match)` reads, pattern order first, then
        line order: (value, line index), or (None, None)."""
        for pattern in self.regexes.get(attr, []):
            for index, line in enumerate(self.lines):
                if not self._usable(index):
                    continue
                match = patterns.search(pattern, line, self.budget)
                if match is None:
                    continue
                value = value_of(match)
                if value is not None:
                    return value, index
        return None, None

    def _tag(self, index: int, label: str, value: str = "") -> None:
        self.tags[index].append(TraceTag(label, value))

    # -- the parts --

    def _section(self) -> tuple:
        """(first line index, stop index, start index, end index, times the
        start was found) of the returnables part; first is None when the
        format has a start pattern and no line matches it."""
        start_pattern = next(iter(self.regexes.get("section_start", [])), None)
        end_pattern = next(iter(self.regexes.get("section_end", [])), None)
        start = None
        if start_pattern is not None:
            start = self._first_line(start_pattern)
            if start is None:
                return None, None, None, None, 0
            first = start + 1
        else:
            first = 0
        end = self._first_line(end_pattern, first) if end_pattern is not None else None
        times = 1
        if start_pattern is not None and end is not None:
            index = end + 1
            while True:
                again = self._first_line(start_pattern, index)
                if again is None:
                    break
                times += 1
                index = again + 1
        return first, (end if end is not None else len(self.lines)), start, end, times

    def _read_line(self, match) -> tuple:
        """(ReadLine, "") or (None, why it is not read)."""
        designation = clean_text(captured(match, "designation"), MAX_DESIGNATION_CHARS)
        if _alnums(designation) < 2:
            return None, "désignation trop courte"
        quantity = read_quantity(captured(match, "quantite"))
        if quantity is None:
            return None, "quantité illisible ou hors limites"
        unit_text = captured(match, "prix")
        unit = None
        if unit_text is not None:
            unit = read_amount(unit_text, places=4)
            if unit is None:
                return None, "prix illisible ou hors limites"
        amount_text = captured(match, "montant")
        amount = None
        if amount_text is not None:
            amount = read_amount(amount_text, places=2)
            if amount is None:
                return None, "montant illisible ou hors limites"
        return ReadLine(designation, quantity, unit, amount), ""

    def _lines(self, first: int, stop: int) -> None:
        line_pattern = self.regexes["line_pattern"][0]
        for index in range(first, stop):
            line = self.lines[index]
            if not self._usable(index):
                self.reading.unread.append(shown_line(line))
                self._tag(index, "non lue", f"ligne de plus de {MAX_LINE_CHARS} caractères")
                continue
            if SEPARATOR.match(line):
                self._tag(index, "séparateur")
                continue
            match = patterns.search(line_pattern, line, self.budget)
            if match is None:
                read, why = None, "ne correspond pas au motif de ligne"
            else:
                read, why = self._read_line(match)
            if read is None:
                self.reading.unread.append(clean_text(line, MAX_LINE_CHARS))
                self._tag(index, "non lue", why)
                continue
            self.reading.lines.append(read)
            self.read_lines[index] = read
            parts = [read.designation, f"quantité {read.quantity}"]
            if read.unit_amount is not None:
                parts.append(f"prix {french_number(read.unit_amount)}")
            if read.amount is not None:
                parts.append(f"montant {french_number(read.amount)}")
            self._tag(index, "ligne lue", " · ".join(parts))

    def _headers(self) -> None:
        reading = self.reading

        reading.delivery_date, index = self._first_value("date_patterns", lambda m: read_date(captured(m, "date")))
        if index is not None:
            self._tag(index, "date", f"{reading.delivery_date:%d/%m/%Y}")

        def printed(match):
            day = read_date(captured(match, "date"))
            if day is None:
                return None
            return patterns.aware_datetime(day, read_time(captured(match, "heure")))

        reading.printed_at, index = self._first_value("printed_patterns", printed)
        if index is not None:
            self._tag(index, "impression", f"{reading.printed_at:%d/%m/%Y %H:%M}")

        number, index = self._first_value(
            "number_patterns", lambda m: clean_text(captured(m, "numero"), MAX_NUMBER_CHARS) or None
        )
        reading.number = number or ""
        if index is not None:
            self._tag(index, "n°", reading.number)

        for pattern in self.regexes.get("reference_patterns", []):
            for index, line in enumerate(self.lines):
                if len(reading.references) >= MAX_REFERENCES:
                    break
                if not self._usable(index):
                    continue
                for match in patterns.find_all(pattern, line, self.budget):
                    reference = clean_text(captured(match, "reference"), MAX_REFERENCE_CHARS)
                    if _alnums(reference) < MIN_REFERENCE_ALNUMS or reference in reading.references:
                        continue
                    if len(reading.references) >= MAX_REFERENCES:
                        break
                    reading.references.append(reference)
                    self._tag(index, "réf.", reference)

        for pattern in self.regexes.get("replaces_pattern", []):
            for index, line in enumerate(self.lines):
                if self._usable(index) and patterns.search(pattern, line, self.budget):
                    reading.replaces = True
                    self._tag(index, "remplace")

        reading.printed_total, index = self._first_value(
            "total_patterns", lambda m: read_amount(captured(m, "total"), places=2)
        )
        if index is not None:
            self._tag(index, "total", french_number(reading.printed_total))

    def _remarks(self) -> None:
        start_pattern = next(iter(self.regexes.get("remarks_start", [])), None)
        if start_pattern is None:
            return
        start = self._first_line(start_pattern)
        if start is None:
            return
        end_pattern = next(iter(self.regexes.get("remarks_end", [])), None)
        end = self._first_line(end_pattern, start + 1) if end_pattern is not None else None
        kept = []
        for index in range(start + 1, end if end is not None else len(self.lines)):
            line = self.lines[index]
            if SEPARATOR.match(line):
                continue
            kept.append(clean_text(shown_line(line), MAX_LINE_CHARS))
            self._tag(index, "remarque")
        self.reading.remarks = "\n".join(kept)

    def _checks(self, start, end, times) -> list:
        reading = self.reading
        checks = []
        if self.regexes.get("section_start"):
            if start is None:
                checks.append(
                    Check("Partie des consignes trouvée", False, "aucune ligne ne correspond au motif de début")
                )
            elif times > 1:
                checks.append(
                    Check("Partie des consignes trouvée", False, f"trouvée {times} fois : seule la première est lue")
                )
            else:
                checks.append(Check("Partie des consignes trouvée", True, f"ligne {start + 1}"))
        if self.regexes.get("section_end"):
            if reading.section_found is False:
                checks.append(Check("Fin de la partie trouvée", False, "la partie des consignes n'a pas été trouvée"))
            elif end is None:
                checks.append(Check("Fin de la partie trouvée", False, "la partie est lue jusqu'à la fin du document"))
            else:
                checks.append(Check("Fin de la partie trouvée", True, f"ligne {end + 1}"))
        checks.append(Check(_lines_label(len(reading.lines)), True))
        if reading.unread:
            count = len(reading.unread)
            checks.append(
                Check(
                    "Aucune ligne ignorée",
                    False,
                    f"{count} ligne{'s' if count > 1 else ''} non lue{'s' if count > 1 else ''} : "
                    + _listed(reading.unread),
                )
            )
        else:
            checks.append(Check("Aucune ligne ignorée", True))
        priced = [line for line in reading.lines if line.unit_amount is not None and line.amount is not None]
        if priced:
            wrong = [
                line
                for line in priced
                if (line.quantity * line.unit_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != line.amount
            ]
            detail = " ; ".join(
                f"« {line.designation} » : {line.quantity} × {french_number(line.unit_amount)} = "
                f"{french_number(line.quantity * line.unit_amount)}, le bon imprime {french_number(line.amount)}"
                for line in wrong[:LISTED]
            )
            if len(wrong) > LISTED:
                detail += f" … et {len(wrong) - LISTED} autres"
            checks.append(Check("quantité × prix = montant", not wrong, detail))
        if reading.printed_total is not None:
            amounts = [line_amount(line) for line in reading.lines]
            missing = sum(1 for amount in amounts if amount is None)
            if missing:
                checks.append(
                    Check(
                        "Total des lignes = total imprimé",
                        False,
                        f"{missing} ligne{'s' if missing > 1 else ''} sans montant : total non vérifiable",
                    )
                )
            else:
                total = sum(amounts, Decimal("0.00"))
                checks.append(
                    Check(
                        "Total des lignes = total imprimé",
                        abs(total) == abs(reading.printed_total),
                        f"lignes : {french_number(total)} · total imprimé : {french_number(reading.printed_total)}",
                    )
                )
        if reading.delivery_date is not None:
            checks.append(Check("Date de livraison lue", True, f"{reading.delivery_date:%d/%m/%Y}"))
        elif self.regexes.get("date_patterns"):
            checks.append(Check("Date de livraison lue", False, "aucun motif de date n'a trouvé de date"))
        else:
            checks.append(Check("Date de livraison lue", False, "le format n'a pas de motif de date"))
        if self.regexes.get("number_patterns"):
            checks.append(
                Check("Numéro lu", bool(reading.number), reading.number or "aucun motif de numéro n'a trouvé de numéro")
            )
        return checks

    def read(self) -> SlipReading:
        if not self.regexes.get("line_pattern"):
            raise PatternError(f"{patterns.FIELD_BY_ATTR['line_pattern'].label} : le motif est vide.")
        first, stop, start, end, times = self._section()
        if self.regexes.get("section_start"):
            self.reading.section_found = first is not None
        if start is not None:
            self._tag(start, "début")
        if end is not None:
            self._tag(end, "fin")
        if first is not None:
            if stop - first > MAX_SECTION_LINES:
                raise _ReadingFailed(SECTION_TOO_LONG)
            self._lines(first, stop)
        self._headers()
        self._remarks()
        self.reading.checks = self._checks(start, end, times)
        return self.reading


def _failed(message: str) -> SlipReading:
    return SlipReading(error=message, checks=[Check("Lecture impossible", False, message)])


def _read(text, fmt, budget: Budget | None) -> tuple:
    """(SlipReading, _Reader or None)."""
    budget = budget or Budget(patterns.READING_SECONDS)
    text = text if isinstance(text, str) else ""
    if len(text) > MAX_TEXT_CHARS:
        return _failed(TEXT_TOO_LONG), None
    try:
        regexes = patterns.compile_format(fmt)
    except PatternError as error:
        return _failed(error.message), _Reader(text, {}, budget)
    reader = _Reader(text, regexes, budget)
    try:
        return reader.read(), reader
    except (PatternError, _ReadingFailed) as error:
        reader.tags = [[] for _ in reader.lines]
        reader.read_lines = {}
        return _failed(str(error)), reader


def read_slip_text(text, fmt, *, budget: Budget | None = None) -> SlipReading:
    """What `fmt`'s patterns read in `text` (see the module docstring). Never
    raises for a bad or slow pattern: the reading's `error` says it."""
    reading, _reader = _read(text, fmt, budget)
    return reading


def trace(text, fmt, *, budget: Budget | None = None) -> Trace:
    """« Tester »: every line of `text` with what the reading made of it -
    « début », « ligne lue » (designation · quantity · price · amount),
    « non lue » (and why), « séparateur », « fin », « date »,
    « impression », « n° », « réf. », « total », « remplace », « remarque » -
    and the reading itself."""
    reading, reader = _read(text, fmt, budget)
    if reader is None:
        # A text too long to be read is not drawn line by line either.
        return Trace(lines=[], reading=reading)
    return Trace(
        lines=[
            TraceLine(index + 1, shown_line(line), reader.tags[index], reader.read_lines.get(index))
            for index, line in enumerate(reader.lines)
        ],
        reading=reading,
    )


# -- Which format -----------------------------------------------------------------------------------------------------


def detect_format(text, formats):
    """The one format that recognises `text`: only ACTIVE formats WITH a
    section_start take part (a format without one would « recognise » any
    document its line pattern reads a line of). None when none does - « Aucun
    format de bon ne reconnaît ce document » is the caller's to say; SlipError
    when several do. A format whose start pattern no longer compiles
    recognises nothing (its page says « motif invalide »); one that runs out
    of time stops the detection with a SlipError naming it."""
    field_ = patterns.FIELD_BY_ATTR["section_start"]
    lines = [line for line in split_lines(text) if len(line) <= MAX_LINE_CHARS]
    budget = Budget(patterns.READING_SECONDS)
    found = []
    for fmt in formats:
        if not getattr(fmt, "is_active", True) or not (getattr(fmt, "section_start", "") or "").strip():
            continue
        try:
            (pattern,) = patterns.compile_field(field_, fmt.section_start)
        except PatternError:
            continue
        try:
            if any(patterns.search(pattern, line, budget) for line in lines):
                found.append(fmt)
        except PatternError as error:
            raise SlipError(
                f"Le format « {fmt.name} » n'a pas pu être essayé ({error}) : choisissez le format dans la liste."
            ) from None
    if not found:
        return None
    if len(found) > 1:
        names = ", ".join(str(getattr(fmt, "name", fmt)) for fmt in found)
        raise SlipError(f"Plusieurs formats reconnaissent ce document ({names}) : choisissez-le dans la liste.")
    return found[0]
