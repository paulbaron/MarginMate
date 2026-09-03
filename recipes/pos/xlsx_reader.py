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
try to be a general-purpose reader - no formulas, no formatting, no dates
(the columns that matter arrive as ISO strings anyway).
"""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_COLUMN_REF = re.compile(r"^([A-Z]+)")


class XlsxError(RuntimeError):
    pass


def _column_index(cell_ref: str) -> int:
    """"C7" -> 2. Cells are addressed, not ordered, so an empty cell is
    simply absent from the row and everything after it would shift left if
    positions were inferred from order."""
    match = _COLUMN_REF.match(cell_ref or "")
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """The workbook's string table.

    Streamed rather than parsed into a DOM: on a multi-year export this table
    holds every distinct product name, ticket reference and date in the file,
    and building a tree of it costs far more than the list it becomes.
    """
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings: list[str] = []
    with archive.open("xl/sharedStrings.xml") as stream:
        context = ElementTree.iterparse(stream, events=("end",))
        for _event, element in context:
            if element.tag != f"{MAIN_NS}si":
                continue
            strings.append("".join(t.text or "" for t in element.iter(f"{MAIN_NS}t")))
            element.clear()
    return strings


def _sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    """{sheet name: path in the zip}.

    Resolved through the relationship ids rather than by pairing workbook
    order with a sorted list of sheet files - those two agree often enough
    to look correct and then silently hand back the wrong sheet.
    """
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target_by_id = {
        rel.get("Id"): rel.get("Target") for rel in rels.findall(f"{REL_NS}Relationship")
    }
    paths = {}
    for sheet in workbook.iter(f"{MAIN_NS}sheet"):
        target = target_by_id.get(sheet.get(f"{DOC_REL_NS}id"))
        if not target:
            continue
        target = target.lstrip("/")
        paths[sheet.get("name")] = target if target.startswith("xl/") else f"xl/{target}"
    return paths


def sheet_names(path: str) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return list(_sheet_paths(archive))


def read_sheet(path: str, sheet_name: str):
    """Yield each row of one sheet as a list of strings.

    Rows are padded to the width of their own last populated cell; a caller
    reading by column index must cope with a short row (see parse_rows).
    """
    with zipfile.ZipFile(path) as archive:
        paths = _sheet_paths(archive)
        if sheet_name not in paths:
            raise XlsxError(f"{path} has no sheet named {sheet_name!r} (found: {list(paths)})")
        strings = _shared_strings(archive)
        with archive.open(paths[sheet_name]) as stream:
            # Streamed, and each row dropped as soon as it has been yielded.
            # Reading the sheet with fromstring() built a DOM of the whole
            # thing: three years of line-by-line ticket data peaked at 1.3 GB,
            # which on a smaller machine is not slow but fatal.
            context = ElementTree.iterparse(stream, events=("start", "end"))
            _event, root = next(context)
            for event, element in context:
                if event != "end" or element.tag != f"{MAIN_NS}row":
                    continue
                cells: dict[int, str] = {}
                for cell in element.findall(f"{MAIN_NS}c"):
                    cells[_column_index(cell.get("r"))] = _cell_text(cell, strings)
                width = max(cells) + 1 if cells else 0
                row = [cells.get(i, "") for i in range(width)]
                # clear() empties the element; the parent still holds it, so
                # the root has to be cleared too or the rows simply pile up
                # there instead and nothing has been saved.
                element.clear()
                root.clear()
                yield row


def _cell_text(cell, strings: list[str]) -> str:
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
    return value.text or ""
