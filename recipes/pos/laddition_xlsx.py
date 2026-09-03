"""Reading L'Addition's "Lignes de ventes" export.

The workbook has several sheets; the one that matters is
`SalesDocumentLines` - one row per item on per ticket, with the day it was
sold, what it was called on the till, and how many. Everything else
(ProductAnalytics, SalesDocument, ExtraLines, StockMovement) is either a
consolidation of that or about something else.

`SalesDocumentLines` is used rather than the ready-made per-product totals in
`ProductAnalytics` for one reason: it carries a DATE per line. That's what
lets sales be sliced by stock-take window afterwards, so a download doesn't
have to be aligned to an inventory period to be usable.

Columns are looked up by their header text, not by position - the export has
40 of them and their order is not a promise anyone made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

SALES_SHEET = "SalesDocumentLines"
DAY_COLUMN = "Jour"
NAME_COLUMN = "Nom"
QUANTITY_COLUMN = "Qte"
OFFERED_COLUMN = "TAG_Offered"
CATEGORY_COLUMN = "TAG_Catégorie"
TYPOLOGY_COLUMN = "TAG_Typologie"


class LadditionExportError(RuntimeError):
    pass


@dataclass
class ParsedExport:
    """(till name, day, quantity) triples, ready for recipes.sales."""

    entries: list[tuple[str, date, int]] = field(default_factory=list)
    #: Rows the export listed but that carry no usable day/name/quantity -
    #: the "Total" line, mostly. Counted so a silent drop is visible.
    skipped: int = 0
    #: How many of the counted items were comped ("offerts"). Informational:
    #: they ARE included in the quantities, because a free drink is poured
    #: from the same bottle as a paid one and consumes exactly the same
    #: stock. Recording them again as a known loss would subtract them twice.
    offered: int = 0
    #: {till name: {"quantity", "category", "typology", "first", "last"}} -
    #: what PosProduct needs to keep a workable backlog of unmapped items.
    products: dict = field(default_factory=dict)

    @property
    def total_quantity(self) -> int:
        return sum(quantity for _name, _day, quantity in self.entries)

    @property
    def days(self) -> tuple[date, date] | None:
        if not self.entries:
            return None
        days = [day for _name, day, _quantity in self.entries]
        return min(days), max(days)


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        # Quantities arrive as ints, but a float sneaks through when Excel
        # has decided a column is numeric.
        return int(float(str(value).replace(",", ".")))
    except ValueError:
        return None


def parse_rows(rows) -> ParsedExport:
    """Aggregate raw `SalesDocumentLines` rows (header first) into totals per
    (till name, day).

    Summing here rather than leaving 2,000 individual lines for the importer
    keeps the shapes small, and matches how the till really behaves: one row
    per item rung up, so five pints on one ticket is five rows.
    """
    rows = iter(rows)
    try:
        header = [str(cell or "").strip() for cell in next(rows)]
    except StopIteration:
        raise LadditionExportError(f"The {SALES_SHEET} sheet is empty.") from None

    missing = [c for c in (DAY_COLUMN, NAME_COLUMN, QUANTITY_COLUMN) if c not in header]
    if missing:
        raise LadditionExportError(
            f"{SALES_SHEET} is missing the {', '.join(missing)} column(s) - found: {header}"
        )
    day_at = header.index(DAY_COLUMN)
    name_at = header.index(NAME_COLUMN)
    quantity_at = header.index(QUANTITY_COLUMN)
    offered_at = header.index(OFFERED_COLUMN) if OFFERED_COLUMN in header else None
    category_at = header.index(CATEGORY_COLUMN) if CATEGORY_COLUMN in header else None
    typology_at = header.index(TYPOLOGY_COLUMN) if TYPOLOGY_COLUMN in header else None

    def _cell(row, at):
        return str(row[at] or "").strip() if at is not None and at < len(row) else ""

    result = ParsedExport()
    totals: dict[tuple[str, date], int] = {}
    order: list[tuple[str, date]] = []

    for row in rows:
        if len(row) <= max(day_at, name_at, quantity_at):
            result.skipped += 1
            continue
        day = _to_date(row[day_at])
        name = str(row[name_at] or "").strip()
        quantity = _to_int(row[quantity_at])
        # The export's own "Total" row has a "-" where the date should be.
        if day is None or not name or quantity is None:
            result.skipped += 1
            continue

        key = (name, day)
        if key not in totals:
            order.append(key)
        totals[key] = totals.get(key, 0) + quantity

        if offered_at is not None and str(row[offered_at] or "").strip().upper() == "OUI":
            result.offered += quantity

        product = result.products.setdefault(
            name,
            {"quantity": 0, "category": "", "typology": "", "first": day, "last": day},
        )
        product["quantity"] += quantity
        product["first"] = min(product["first"], day)
        product["last"] = max(product["last"], day)
        # Kept from the first row that states one; the till doesn't change a
        # product's category mid-period.
        product["category"] = product["category"] or _cell(row, category_at)
        product["typology"] = product["typology"] or _cell(row, typology_at)

    result.entries = [(name, day, totals[(name, day)]) for name, day in order]
    return result


def parse_sales_export(path: str) -> ParsedExport:
    """Read one downloaded .xlsx.

    Uses this package's own reader rather than openpyxl, which refuses the
    file outright - see xlsx_reader for the gory details.
    """
    from .xlsx_reader import XlsxError, read_sheet

    try:
        return parse_rows(read_sheet(path, SALES_SHEET))
    except XlsxError as exc:
        raise LadditionExportError(
            f"{exc} - is this the 'Lignes de ventes' export?"
        ) from exc


def parse_sales_exports(paths) -> ParsedExport:
    """Read several downloads - one per date window - as one result.

    The windows never overlap (see laddition.date_windows), so entries are
    concatenated rather than merged; a (name, day) pair cannot appear twice.
    """
    combined = ParsedExport()
    for path in paths:
        part = parse_sales_export(path)
        combined.entries.extend(part.entries)
        combined.skipped += part.skipped
        combined.offered += part.offered
        for name, info in part.products.items():
            merged = combined.products.setdefault(
                name, {"quantity": 0, "category": "", "typology": "", "first": info["first"], "last": info["last"]}
            )
            merged["quantity"] += info["quantity"]
            merged["first"] = min(merged["first"], info["first"])
            merged["last"] = max(merged["last"], info["last"])
            merged["category"] = merged["category"] or info["category"]
            merged["typology"] = merged["typology"] or info["typology"]
    return combined
