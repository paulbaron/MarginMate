"""A small, forgiving .xlsx row reader.

Written rather than using openpyxl because openpyxl - correctly - refuses
L'Addition's export outright:

    ValueError: Unable to read workbook: ... contains some invalid XML

The export declares its "Total" row's cells as numeric and then writes "-"
into them, which is not valid SpreadsheetML. openpyxl's read-only parser
raises `invalid literal for int() with base 10: '-'`, and its normal parser
rejects the whole workbook. Neither can be talked out of it, and the file
comes from a third party who is not going to fix it.

So: read every cell as text and let the caller decide what a value means.
An .xlsx is a zip of XML, and the parts needed here are few. This does NOT
try to be a general-purpose reader - no formulas, no formatting. A date is
whatever the cell holds: the text L'Addition writes, or the serial number
Excel stores (`date1904` says which calendar it counts from), which the
caller reads as a day (recipes/pos/till_file.py).

The file may come from outside - a bar's own till export, uploaded on
« Ventes » - so the reader refuses, for EVERY caller (the owner's
downloaded exports read exactly as before; none of these ever fires on
them):

- **a cell past column XFD** (16 384, SpreadsheetML's own last column): a
  row is padded to its widest cell, and one reference « ZZZZZZZZZZZZ1 » in
  a tiny file asked for a list of about 10^17 cells - the whole server, one
  process for every espace, out of memory. A caller that only needs the
  first columns says so (`max_columns`) and the rest are never read;
- **a DOCTYPE or an ENTITY declaration** anywhere in a member, before the
  XML parser sees it: ElementTree expands internal entities - the
  billion-laughs door (« The XML comes from outside », invoices/einvoice.py).
  The encoding is decided first, as einvoice does: a member in a wide
  encoding (a UTF-16/32 mark, a NUL in its first bytes) or declaring an
  encoding outside a few ASCII-compatible ones is refused, since a byte
  grep would miss « <!DOCTYPE » written two bytes a letter. Then every
  byte the parser is handed is scanned, chunk by chunk with an overlap -
  a DOCTYPE after kilobytes of comments is still found.

And for a file uploaded by a person (`untrusted=True`, `check_untrusted`):
the zip's own bounds - each member's uncompressed size, the total, the
compression ratio past a few megabytes, the number of members - read from
its directory before anything inflates (zipfile never inflates a member
past the size its directory declares), and the shared strings' count.

Every refusal is an `XlsxError` in French that names no path: the message
may reach a page.
"""

from __future__ import annotations

import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from xml.etree import ElementTree

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_COLUMN_REF = re.compile(r"^([A-Z]+)")

#: SpreadsheetML's last column, XFD: a reference past it is no cell.
MAX_COLUMNS = 16384
#: The sheets a workbook may name - far more than any export holds.
MAX_SHEETS = 1000

#: Read as the parser asks, each chunk scanned before it is handed over.
_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_OVERLAP = max(len(marker) for marker in _FORBIDDEN) - 1
#: How much of a member's start is looked at for its declaration.
_HEAD_BYTES = 512
_BOM_UTF8 = b"\xef\xbb\xbf"
_WIDE_MARKS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")
_DECLARED = re.compile(rb"""^<\?xml[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
#: Encodings in which « <!DOCTYPE » is those very bytes, so the scan sees it.
_ASCII_COMPATIBLE = frozenset(
    {"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "iso8859-1", "latin-1", "latin1", "windows-1252", "cp1252"}
)

MEGABYTE = 1024 * 1024

NOT_A_WORKBOOK = "Ce fichier n'est pas un classeur Excel (.xlsx) lisible."
DOCTYPE_REFUSED = "Classeur refusé : il contient une déclaration XML (DOCTYPE ou ENTITY) qu'aucun export ne porte."
ENCODING_REFUSED = "Classeur refusé : une de ses parties est dans un encodage qu'aucun export n'utilise."
COLUMN_REFUSED = "Classeur refusé : une cellule est au-delà de la dernière colonne d'Excel (XFD)."
TOO_MANY_SHEETS = f"Classeur refusé : plus de {MAX_SHEETS} feuilles."
NO_SHEET = "Ce classeur n'a aucune feuille."
ENCRYPTED = "Classeur refusé : il est protégé par un mot de passe ou compressé d'une façon inconnue."


class XlsxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    """What a workbook uploaded by a person may weigh (`check_untrusted`)."""

    #: Uncompressed, per member and in all: a 25 MB upload of XML inflates
    #: tenfold or so; past these it is not an export.
    member_bytes: int = 100 * MEGABYTE
    total_bytes: int = 200 * MEGABYTE
    #: Past `ratio_min_bytes`, a member inflating more than `ratio` times its
    #: compressed size is a bomb, not a spreadsheet (transfer/archive.py's
    #: own figures).
    ratio: int = 200
    ratio_min_bytes: int = 10 * MEGABYTE
    members: int = 1000
    #: The shared strings held in memory while a sheet is read.
    shared_strings: int = 1_000_000


#: Module-level so a test can patch the bounds small.
UPLOAD_LIMITS = Limits()


class Number(str):
    """A cell the workbook stores as a number (`typed=True`): its text is
    the number as written in the XML - a dot decimal, an Excel date as its
    serial - whatever the locale of whoever typed it. A text cell is a
    plain str, as printed."""


def _column_index(cell_ref: str) -> int | None:
    """ "C7" -> 2, or None when the reference names no column. Cells are
    addressed, not ordered, so an empty cell is simply absent from the row
    and everything after it would shift left if positions were inferred from
    order. A column past XFD is refused, never counted: it would be the
    width of the row."""
    match = _COLUMN_REF.match(cell_ref or "")
    if not match:
        return None
    letters = match.group(1)
    if len(letters) > 3:
        raise XlsxError(COLUMN_REFUSED)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    if index > MAX_COLUMNS:
        raise XlsxError(COLUMN_REFUSED)
    return index - 1


class _Guarded:
    """A member of the zip as the XML parser reads it, every chunk scanned
    first: the encoding at the start, then « <!DOCTYPE » and « <!ENTITY »
    anywhere - across two chunks too (the overlap)."""

    def __init__(self, stream):
        self._stream = stream
        self._tail = b""
        self._started = False

    def close(self) -> None:
        self._stream.close()

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if not self._started:
            self._started = True
            # The start is read whole before it is judged: a first chunk of
            # three bytes would hide a wide encoding's NUL.
            while chunk and len(chunk) < _HEAD_BYTES:
                more = self._stream.read(_HEAD_BYTES - len(chunk))
                if not more:
                    break
                chunk += more
            _check_head(chunk)
        scanned = self._tail + chunk
        if any(marker in scanned for marker in _FORBIDDEN):
            raise XlsxError(DOCTYPE_REFUSED)
        self._tail = scanned[-_OVERLAP:] if _OVERLAP else b""
        return chunk


def _check_head(head: bytes) -> None:
    """A member in a wide encoding, or declaring one the scan cannot read
    « <!DOCTYPE » in, is refused before anything else is read of it."""
    if head.startswith(_WIDE_MARKS) or b"\x00" in head[:4]:
        raise XlsxError(ENCODING_REFUSED)
    declared = _DECLARED.match(head.removeprefix(_BOM_UTF8))
    if declared and declared.group(1).decode("ascii").lower() not in _ASCII_COMPATIBLE:
        raise XlsxError(ENCODING_REFUSED)


def _open_member(archive: zipfile.ZipFile, name: str) -> _Guarded:
    """One member, guarded. An encrypted member, or one compressed in a way
    zipfile cannot inflate, is this file's refusal - zipfile raises
    RuntimeError and NotImplementedError for them."""
    try:
        return _Guarded(archive.open(name))
    except (RuntimeError, NotImplementedError):
        raise XlsxError(ENCRYPTED) from None


@contextmanager
def _iterparse(archive: zipfile.ZipFile, name: str, events=("end",)):
    """The member's events, its stream closed when done - whether the
    reading ended or was given up: on Windows a file still held open by a
    member cannot be deleted (an upload refused is deleted at once)."""
    stream = _open_member(archive, name)
    try:
        yield ElementTree.iterparse(stream, events=events)
    finally:
        stream.close()


def _zip(source) -> zipfile.ZipFile:
    """The workbook's zip - a path or a seekable file object. Anything that
    is not a zip is refused in French: zipfile's own sentence may carry the
    path."""
    if hasattr(source, "seek"):
        source.seek(0)
    try:
        return zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise XlsxError(NOT_A_WORKBOOK) from None


def check_untrusted(source, limits: Limits | None = None) -> None:
    """Refuse an uploaded workbook whose zip is no export: a member, or the
    whole, too big once inflated, a member inflating far past its
    compressed size, too many members. Read off the zip's directory, before
    anything inflates: zipfile never inflates a member past the size the
    directory declares for it (it stops there, and the CRC then fails)."""
    with _zip(source) as archive:
        _check_bounds(archive, limits or UPLOAD_LIMITS)


def _check_bounds(archive: zipfile.ZipFile, limits: Limits) -> None:
    members = archive.infolist()
    if len(members) > limits.members:
        raise XlsxError(f"Classeur refusé : plus de {limits.members} parties.")
    total = 0
    for member in members:
        if member.file_size > limits.member_bytes:
            raise XlsxError(f"Classeur refusé : une partie dépasse {limits.member_bytes // MEGABYTE} Mo une fois lue.")
        if member.file_size > limits.ratio_min_bytes and member.file_size > limits.ratio * max(member.compress_size, 1):
            raise XlsxError("Classeur refusé : une partie est anormalement compressée.")
        total += member.file_size
    if total > limits.total_bytes:
        raise XlsxError(f"Classeur refusé : il dépasse {limits.total_bytes // MEGABYTE} Mo une fois lu.")


def _shared_strings(archive: zipfile.ZipFile, cap: int | None = None) -> list[str]:
    """The workbook's string table.

    Streamed rather than parsed into a DOM: on a multi-year export this table
    holds every distinct product name, ticket reference and date in the file,
    and building a tree of it costs far more than the list it becomes. Each
    string is dropped from the tree once read (the root too, or the emptied
    elements pile up there). `cap`: how many an uploaded workbook may hold.
    """
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings: list[str] = []
    root = None
    with _iterparse(archive, "xl/sharedStrings.xml", events=("start", "end")) as context:
        for event, element in context:
            if root is None:
                root = element
                continue
            if event != "end" or element.tag != f"{MAIN_NS}si":
                continue
            strings.append("".join(t.text or "" for t in element.iter(f"{MAIN_NS}t")))
            if cap is not None and len(strings) > cap:
                raise XlsxError(f"Classeur refusé : plus de {cap} textes différents.")
            element.clear()
            root.clear()
    return strings


@dataclass(frozen=True)
class _Workbook:
    #: {sheet name: path in the zip}, in the workbook's order.
    paths: dict
    #: The 1904 date system: Excel for an old Mac counts its serials from
    #: 1904-01-01, four years and a day later than everyone else.
    date1904: bool


def _workbook(archive: zipfile.ZipFile) -> _Workbook:
    """The sheets, resolved through the relationship ids rather than by
    pairing workbook order with a sorted list of sheet files - those two
    agree often enough to look correct and then silently hand back the
    wrong sheet. Streamed, and at most MAX_SHEETS of them."""
    target_by_id: dict[str, str] = {}
    with _iterparse(archive, "xl/_rels/workbook.xml.rels") as context:
        for _event, element in context:
            if element.tag == f"{REL_NS}Relationship":
                target_by_id[element.get("Id")] = element.get("Target")
                if len(target_by_id) > 4 * MAX_SHEETS:
                    raise XlsxError(TOO_MANY_SHEETS)
                element.clear()
    paths: dict[str, str] = {}
    date1904 = False
    with _iterparse(archive, "xl/workbook.xml") as context:
        for _event, element in context:
            if element.tag == f"{MAIN_NS}workbookPr":
                date1904 = (element.get("date1904") or "").strip().lower() in ("1", "true")
            elif element.tag == f"{MAIN_NS}sheet":
                target = target_by_id.get(element.get(f"{DOC_REL_NS}id"))
                if target:
                    target = target.lstrip("/")
                    paths[element.get("name")] = target if target.startswith("xl/") else f"xl/{target}"
                if len(paths) > MAX_SHEETS:
                    raise XlsxError(TOO_MANY_SHEETS)
                element.clear()
    return _Workbook(paths, date1904)


def sheet_names(source, *, untrusted: bool = False) -> list[str]:
    with _zip(source) as archive:
        if untrusted:
            _check_bounds(archive, UPLOAD_LIMITS)
        return list(_workbook(archive).paths)


def date1904(source, *, untrusted: bool = False) -> bool:
    """Whether the workbook counts its dates from 1904 (`workbookPr`)."""
    with _zip(source) as archive:
        if untrusted:
            _check_bounds(archive, UPLOAD_LIMITS)
        return _workbook(archive).date1904


def read_sheet(
    source,
    sheet_name: str | None = None,
    *,
    max_columns: int | None = None,
    typed: bool = False,
    untrusted: bool = False,
):
    """Yield each row of one sheet as a list of strings - the first sheet
    when no name is given.

    Rows are padded to the width of their own last populated cell; a caller
    reading by column index must cope with a short row (see parse_rows).
    `max_columns`: the cells past it are never read. `typed`: a cell the
    workbook stores as a number comes back as a `Number`. `untrusted`: an
    uploaded workbook - the zip's bounds checked first, the string table
    capped.
    """
    limit = MAX_COLUMNS if max_columns is None else min(max_columns, MAX_COLUMNS)
    with _zip(source) as archive:
        if untrusted:
            _check_bounds(archive, UPLOAD_LIMITS)
        paths = _workbook(archive).paths
        if sheet_name is None:
            if not paths:
                raise XlsxError(NO_SHEET)
            sheet_name = next(iter(paths))
        if sheet_name not in paths:
            found = ", ".join(str(name) for name in paths) or "aucune"
            raise XlsxError(f"Ce classeur n'a pas de feuille « {sheet_name} » (feuilles : {found}).")
        if paths[sheet_name] not in archive.namelist():
            raise XlsxError(f"La feuille « {sheet_name} » manque dans ce classeur.")
        strings = _shared_strings(archive, UPLOAD_LIMITS.shared_strings if untrusted else None)
        # Streamed, and each row dropped as soon as it has been yielded.
        # Reading the sheet with fromstring() built a DOM of the whole
        # thing: three years of line-by-line ticket data peaked at 1.3 GB,
        # which on a smaller machine is not slow but fatal.
        with _iterparse(archive, paths[sheet_name], events=("start", "end")) as context:
            _event, root = next(context)
            for event, element in context:
                if event != "end" or element.tag != f"{MAIN_NS}row":
                    continue
                cells: dict[int, str] = {}
                index = -1
                for cell in element.findall(f"{MAIN_NS}c"):
                    ref = cell.get("r")
                    # A cell without its reference follows the one before
                    # it (SpreadsheetML lets a writer leave `r` out).
                    index = index + 1 if ref is None else _column_index(ref)
                    if index is None:
                        index = 0
                    if index >= limit:
                        continue
                    cells[index] = _cell_text(cell, strings, typed)
                width = max(cells) + 1 if cells else 0
                row = [cells.get(i, "") for i in range(width)]
                # clear() empties the element; the parent still holds it, so
                # the root has to be cleared too or the rows simply pile up
                # there instead and nothing has been saved.
                element.clear()
                root.clear()
                yield row


def _cell_text(cell, strings: list[str], typed: bool = False) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(f"{MAIN_NS}is")
        return "".join(t.text or "" for t in inline.iter(f"{MAIN_NS}t")) if inline is not None else ""
    value = cell.find(f"{MAIN_NS}v")
    if value is None:
        return ""
    if kind == "s":
        try:
            return strings[int(value.text)]
        except (TypeError, ValueError, IndexError):
            return ""
    text = value.text or ""
    if typed and kind in (None, "n"):
        return Number(text)
    return text
