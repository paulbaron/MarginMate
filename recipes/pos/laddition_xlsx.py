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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SALES_SHEET = "SalesDocumentLines"
DAY_COLUMN = "Jour"
NAME_COLUMN = "Nom"
QUANTITY_COLUMN = "Qte"
OFFERED_COLUMN = "TAG_Offered"
CATEGORY_COLUMN = "TAG_Catégorie"
TYPOLOGY_COLUMN = "TAG_Typologie"
#: The line's own amount, tax included. NOT a unit price: see parse_rows.
PRICE_COLUMN = "Prix TTC"
DISCOUNT_COLUMN = "Remises TTC"
RATE_COLUMN = "Taux"
#: Deliberately NOT read: "Prix achat HT" is the till's own idea of what a
#: drink costs, and it is 0 on every row of every export downloaded so far.
#: Costs come from the invoices, through the recipes.

ZERO = Decimal("0")
CENTS = Decimal("0.01")
#: The rates France has (invoices/parsers/receipt_base.py keeps the same
#: list for the receipts, plus the exempt 0). Anything else read off a
#: `Taux` cell is a column that moved or a value nobody can act on, and the
#: line's HT is left unknown rather than guessed at the drink rate.
KNOWN_RATES = (ZERO, Decimal("0.021"), Decimal("0.055"), Decimal("0.10"), Decimal("0.20"))


class LadditionExportError(RuntimeError):
    pass


@dataclass
class DayMoney:
    """What one till product took on one day."""

    #: `Prix TTC` less `Remises TTC`, summed as printed - the amounts carry
    #: their own sign, so a refund subtracts itself.
    revenue_ttc: Decimal = ZERO
    #: Worked out per rate, not per line: a day mixing food at 10 % and
    #: drink at 20 % is right, and the rounding happens once per rate rather
    #: than 50 000 times a year.
    revenue_ht: Decimal = ZERO
    #: The part of revenue_ttc whose `Taux` could not be read, and which is
    #: therefore NOT in revenue_ht. A margin page that showed HT without
    #: this beside it would quietly under-report what came in.
    without_rate_ttc: Decimal = ZERO


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
    #: Their `Prix TTC` is already 0, so they add nothing to the revenue -
    #: which is what a margin wants: the stock left and no money came in.
    offered: int = 0
    #: {till name: {"quantity", "category", "typology", "first", "last"}} -
    #: what PosProduct needs to keep a workable backlog of unmapped items.
    products: dict = field(default_factory=dict)
    #: {(till name, day): DayMoney}, alongside `entries` rather than inside
    #: it: half a dozen callers unpack those triples, and a file with no
    #: money columns must still import. A key ABSENT here means this day's
    #: money was never read - never that it took 0 €.
    money: dict = field(default_factory=dict)
    #: Whether the sheet carried the money columns at all (an older export
    #: does not).
    money_columns: bool = False
    #: Lines whose `Taux` could not be read, and lines whose `Prix TTC`
    #: could not. Reported by every caller: a column that moves shows up
    #: here rather than as a smaller margin nobody questions.
    lines_without_rate: int = 0
    lines_without_amount: int = 0
    #: `Remises TTC` is 0.00 on every line of every export read so far,
    #: exactly why it is read and counted - a column that has never fired is
    #: the one that fires silently.
    discounted_lines: int = 0
    discount_ttc: Decimal = ZERO
    #: Lines whose discount would have made the line BIGGER than its own
    #: gross price - a sign the till has never yet written, so nobody knows
    #: which way it writes one. Left alone and counted rather than taken:
    #: see parse_rows.
    discounts_not_taken: int = 0
    #: (till name, day) left out of `money` because a line of that day had
    #: no readable amount - the day is short of an unknown figure, so it
    #: counts as never read rather than as what the other lines took.
    days_without_amount: int = 0
    #: Lines at a negative quantity (a refund: a handful in all) and
    #: lines at any quantity other than 1 or -1 (none, ever). The second is
    #: reported rather than acted on: whatever `Qte` says, `Prix TTC` is
    #: still the line's own amount, and the day the till prints a 2 the
    #: import says so instead of quietly deciding what it means.
    refund_lines: int = 0
    unusual_quantity_lines: int = 0
    #: (till name, day) pairs a later file restated - see parse_sales_exports.
    repeated_days: int = 0

    @property
    def total_quantity(self) -> int:
        return sum(quantity for _name, _day, quantity in self.entries)

    @property
    def revenue_ttc(self) -> Decimal:
        return sum((money.revenue_ttc for money in self.money.values()), ZERO)

    @property
    def revenue_ht(self) -> Decimal:
        return sum((money.revenue_ht for money in self.money.values()), ZERO)

    @property
    def revenue_without_rate_ttc(self) -> Decimal:
        return sum((money.without_rate_ttc for money in self.money.values()), ZERO)

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


def _to_money(value) -> Decimal | None:
    """An amount as the export writes it ("4.5", "0.00", "-1.5").

    Decimal, never float: this is the only figure in the application that
    says what came in, and a cent lost per line is a euro a day.
    """
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _to_rate(value) -> Decimal | None:
    """"20%" -> 0.20. None when the cell says nothing that can be trusted.

    The minus sign belongs to the amount, not to the rate: a refund line
    prints "-20%" beside its negative price (7 lines on the stored
    exports), and read as a rate of its own it would be no French rate at
    all - the line's HT dropped and reported as unreadable, for a rate that
    was perfectly legible.
    """
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", ".")
    if not text:
        return None
    try:
        rate = abs(Decimal(text)) / 100
    except InvalidOperation:
        return None
    return rate if rate in KNOWN_RATES else None


def _to_ht(amount: Decimal, rate: Decimal) -> Decimal:
    """The same arithmetic, and the same rounding, as everywhere else money
    changes side here (invoices/forms.py::_to_ht): half away from zero, so a
    refund converts like the sale it takes back."""
    return (amount / (Decimal("1") + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


def parse_rows(rows) -> ParsedExport:
    """Aggregate raw `SalesDocumentLines` rows (header first) into totals per
    (till name, day).

    Summing here rather than leaving 2,000 individual lines for the importer
    keeps the shapes small, and matches how the till really behaves: one row
    per item rung up, so five pints on one ticket is five rows.

    The money is summed the same way, into `money`. Two rules it obeys, both
    measured over every line of every export stored:

    - **`Prix TTC` is the line's own amount, and is never multiplied by
      `Qte`.** `Qte` is 1 on all but seven lines, and those seven are
      refunds at -1 whose amount is already negative. A "x Qte" reads
      identically on this data and would double the first line that is not
      a 1 - which is why the rule is pinned by a test rather than by the
      arithmetic agreeing today.
    - **HT is worked out per rate**, from the TTC accumulated for that rate,
      so a day mixing food at 10 % and drink at 20 % is right. Rounding once
      per rate rather than once per line also keeps the day within a cent of
      what the VAT return says: three sodas at 3,50 are 9,55 HT, not 9,54.
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
    price_at = header.index(PRICE_COLUMN) if PRICE_COLUMN in header else None
    discount_at = header.index(DISCOUNT_COLUMN) if DISCOUNT_COLUMN in header else None
    rate_at = header.index(RATE_COLUMN) if RATE_COLUMN in header else None

    def _cell(row, at):
        return str(row[at] or "").strip() if at is not None and at < len(row) else ""

    result = ParsedExport(money_columns=price_at is not None)
    totals: dict[tuple[str, date], int] = {}
    order: list[tuple[str, date]] = []
    #: {(name, day): {rate or None: TTC}} - the rate buckets HT is worked
    #: out from once the whole file has been read.
    buckets: dict[tuple[str, date], dict] = {}
    #: Days one of whose lines had no readable amount. They are dropped at
    #: the end rather than filed at what the rest of the day took.
    incomplete: set[tuple[str, date]] = set()

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
        if quantity < 0:
            result.refund_lines += 1
        if quantity not in (1, -1):
            result.unusual_quantity_lines += 1

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

        if price_at is None:
            continue
        amount = _to_money(_cell(row, price_at))
        if amount is None:
            # Nothing to record and nothing to invent - and the day it
            # belongs to is now short of an unknown figure, so the whole
            # (name, day) is left unread rather than filed at what the other
            # lines took. Said out loud either way.
            result.lines_without_amount += 1
            incomplete.add(key)
            continue
        discount = _to_money(_cell(row, discount_at)) or ZERO
        if discount and abs(amount - discount) > abs(amount):
            # Which sign the till writes a discount with has never been
            # observed: the column is 0,00 € on every line stored. Read
            # the wrong way it INFLATES - 6,00 € less -1,50 € is 7,50 €,
            # more than the line's own gross price, which cannot happen. So
            # the arithmetic decides rather than a guess about the sign, and
            # the first line that carries one is reported instead of quietly
            # making the revenue larger.
            result.discounts_not_taken += 1
            discount = ZERO
        elif discount:
            result.discounted_lines += 1
            result.discount_ttc += discount
        rate = _to_rate(_cell(row, rate_at))
        if rate is None:
            result.lines_without_rate += 1
        per_rate = buckets.setdefault(key, {})
        per_rate[rate] = per_rate.get(rate, ZERO) + amount - discount

    result.entries = [(name, day, totals[(name, day)]) for name, day in order]
    result.days_without_amount = len(incomplete)
    for key, per_rate in buckets.items():
        if key in incomplete:
            continue
        money = DayMoney()
        for rate, amount in per_rate.items():
            money.revenue_ttc += amount
            if rate is None:
                money.without_rate_ttc += amount
            else:
                money.revenue_ht += _to_ht(amount, rate)
        result.money[key] = money
    return result


def parse_sales_export(path: str) -> ParsedExport:
    """Read one downloaded .xlsx.

    Uses this package's own reader rather than openpyxl, which refuses the
    file outright - see xlsx_reader for the gory details.

    An .xlsx is a zip, and the folder this reads from is scanned whole: a
    half-finished download, a file Excel was holding, anything renamed
    `.xlsx` by hand raises `zipfile.BadZipFile` or an `OSError` from inside
    the zip machinery. Neither is an `XlsxError`, so both escaped this
    function AND the backfill's own « illisible » branch - one such file
    stopped the other sixteen exports being read at all, with a traceback.
    Every way a file can fail to be this export answers in one type.
    """
    import zipfile

    from .xlsx_reader import XlsxError, read_sheet

    try:
        return parse_rows(read_sheet(path, SALES_SHEET))
    except (XlsxError, zipfile.BadZipFile, OSError) as exc:
        raise LadditionExportError(
            f"{exc} - is this the 'Lignes de ventes' export?"
        ) from exc


def parse_sales_exports(paths) -> ParsedExport:
    """Read several downloads - one per date window - as one result.

    A downloaded window never overlaps another (see laddition.date_windows),
    but a FOLDER of them does: the stored exports cover the same history
    several times over, under different signatures. So a (till name, day)
    read again REPLACES the first reading rather than adding to it - a day
    is a day, however many files print it - and `repeated_days` counts them.
    Added up instead, a folder scanned twice would have shown twice the
    pints and twice the money, each figure perfectly plausible.

    Measured on the stored exports: a great many days appear in two windows at once
    and the two readings agree on every one of them, to the cent - the
    export slices by service day, so a window's edge is not half a day.

    `skipped`, `offered` and the counters about unreadable cells are about
    the READING, not about the days: a file read twice counts its lines
    twice, which is what makes a duplicate file visible.

    A day one file could not read whole (`days_without_amount`) is simply
    absent from that file's `money`, so a file that COULD read it fills it -
    the day is kept from whichever reading was complete, and left unread
    only when none was.
    """
    combined = ParsedExport()
    at: dict[tuple[str, date], int] = {}
    for path in paths:
        part = parse_sales_export(path)
        combined.money_columns = combined.money_columns or part.money_columns
        combined.skipped += part.skipped
        combined.offered += part.offered
        combined.lines_without_rate += part.lines_without_rate
        combined.lines_without_amount += part.lines_without_amount
        combined.discounted_lines += part.discounted_lines
        combined.discount_ttc += part.discount_ttc
        combined.discounts_not_taken += part.discounts_not_taken
        combined.days_without_amount += part.days_without_amount
        combined.refund_lines += part.refund_lines
        combined.unusual_quantity_lines += part.unusual_quantity_lines
        for name, day, quantity in part.entries:
            key = (name, day)
            if key in at:
                combined.repeated_days += 1
                combined.entries[at[key]] = (name, day, quantity)
            else:
                at[key] = len(combined.entries)
                combined.entries.append((name, day, quantity))
        combined.money.update(part.money)
        for name, info in part.products.items():
            merged = combined.products.setdefault(
                name, {"quantity": 0, "category": "", "typology": "", "first": info["first"], "last": info["last"]}
            )
            merged["first"] = min(merged["first"], info["first"])
            merged["last"] = max(merged["last"], info["last"])
            merged["category"] = merged["category"] or info["category"]
            merged["typology"] = merged["typology"] or info["typology"]
    # Counted from the days that were kept, so a repeated file leaves it
    # equal to what the till really rang up.
    for info in combined.products.values():
        info["quantity"] = 0
    for name, _day, quantity in combined.entries:
        combined.products[name]["quantity"] += quantity
    return combined
