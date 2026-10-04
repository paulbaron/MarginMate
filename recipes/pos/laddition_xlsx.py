"""Reading L'Addition's "Lignes de ventes" export.

The workbook has several sheets. Two are read:

- `SalesDocumentLines` - one row per item on per ticket, with the day it was
  sold, what it was called on the till, how many and for how much;
- `SalesDocument` - one row per ticket, with how it was PAID (`Paiements`,
  « CB(4,50)/Cash(2,00) »): what the bank is paid from. Optional - see
  parse_sales_export.

Everything else (ProductAnalytics, ExtraLines, StockMovement) is either a
consolidation of those or about something else.

`SalesDocumentLines` is used rather than the ready-made per-product totals in
`ProductAnalytics` for one reason: it carries a DATE per line. That's what
lets sales be sliced by stock-take window afterwards, so a download doesn't
have to be aligned to an inventory period to be usable.

Columns are looked up by their header text, not by position - the export has
40 of them and their order is not a promise anyone made.

The file may come from outside - the export uploaded on « Ventes », by any
bar - so every figure is bounded by the column that stores it (« A figure
wider than the column behind it is refused, at the door », CLAUDE.md): a
(product, day)'s revenue fits `PosProductDailyQuantity`'s (10, 2), a (day,
method)'s payments `PosDailyPayment`'s (12, 2), a quantity a sane count. A
number written with an exponent, « NaN » or « Infinity » - which `Decimal`
reads, and which then made every later read of the row raise - refuses the
file, as does a day outside 2000-2099; a name is cut to 255 (a name is not
money). Every refusal is a `LadditionExportError` in French naming no path;
the owner's downloaded exports never come near any of it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

logger = logging.getLogger(__name__)

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

#: One row per ticket. Its `Jour` is the same business day as the lines'.
PAYMENTS_SHEET = "SalesDocument"
TICKET_COLUMN = "ID Ticket"
#: The ticket's total, dot decimal (« 12.5 »), like the lines' amounts.
TICKET_TOTAL_COLUMN = "Total TTC"
#: The tip or change not given back: a ticket's payments add up to its
#: total PLUS this, on every ticket stored - the check parse_payment_rows
#: makes on each one.
OVERPAID_COLUMN = "Trop perçus"
#: « Method(amount) » joined by « / »: « CB(4,50) », « Cash(5,00)/CB(3,50) »,
#: « Avoir(1 350,00) ». Comma decimal, a thousands separator, and a minus
#: on money handed back (« Cash(-1,50) »).
PAYMENTS_COLUMN = "Paiements"
_ONE_PAYMENT = r"([^()/]+)\(([^()/]*)\)"
#: The whole cell or nothing: every ticket stored reads with it, so a cell
#: it refuses is something new, and is said rather than half-read.
PAYMENTS_GRAMMAR = re.compile(rf"^{_ONE_PAYMENT}(?:/{_ONE_PAYMENT})*$")
ONE_PAYMENT = re.compile(_ONE_PAYMENT)
#: After the thousands separators are taken out: one optional minus, digits,
#: one decimal separator and at most two decimals. « 1.800,00 » (two
#: separators) is refused rather than guessed at. ASCII digits only: `\d`
#: says yes to digits of other scripts, which Decimal reads.
PAYMENT_AMOUNT = re.compile(r"^-?[0-9]+(?:[.,][0-9]{1,2})?$")
#: A space, a no-break space, a narrow no-break space: which one a number
#: formatter puts between thousands depends on its locale and version.
#: Written as escapes on purpose - the three are indistinguishable on screen.
THOUSANDS_SEPARATORS = (" ", " ", " ")

ZERO = Decimal("0")
CENTS = Decimal("0.01")

#: A plain number as the export writes one, once its spaces are out and a
#: comma is a point: an optional sign, ASCII digits, one decimal point.
#: Never an exponent, « NaN » or « Infinity », which `Decimal` reads too.
PLAIN_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
#: What stores the figures: `PosProductDailyQuantity`'s revenue (10, 2) and
#: `PosDailyPayment.amount` (12, 2). Past them the row is unreadable for
#: good - every read raises InvalidOperation - so the file is refused.
MAX_REVENUE = Decimal("99999999.99")
MAX_PAYMENT = Decimal("9999999999.99")
#: One line's quantity, and a (product, day)'s: a bar's sale is a few units,
#: a day of one product some hundreds.
MAX_LINE_QUANTITY = 100_000
MAX_DAY_QUANTITY = 1_000_000
#: What a till product's name, category and typology are cut to - their
#: columns (`PosProduct`).
NAME_LENGTH = 255
#: The days a till export may hold - the bank's bounds (bank/statements.py).
FIRST_DAY = date(2000, 1, 1)
LAST_DAY = date(2099, 12, 31)

#: What a file that is not this export is told, its detail in the log.
NOT_THE_EXPORT = "Ce fichier n'est pas l'export « Lignes de ventes » de L'Addition."
#: The rates France has (invoices/parsers/receipt_base.py keeps the same
#: list for the receipts, plus the exempt 0). Anything else read off a
#: `Taux` cell is a column that moved or a value nobody can act on, and the
#: line's HT is left unknown rather than guessed at the drink rate.
KNOWN_RATES = (ZERO, Decimal("0.021"), Decimal("0.055"), Decimal("0.10"), Decimal("0.20"))


class LadditionExportError(RuntimeError):
    """Why a file is not this export, or not one that can be stored - a
    French sentence naming no path."""


class PaymentsSheetMissing(LadditionExportError):
    """The workbook has no `SalesDocument` sheet - an older or another
    export. Not damage: its lines are still read, its days simply carry no
    payments."""


@dataclass
class DayPayment:
    """What one means of payment brought in on one day."""

    #: Signed, summed as printed: change handed back is a negative payment.
    amount: Decimal = ZERO
    #: Payments of that method that day (tickets, for the UNREAD and UNPAID
    #: pseudo-methods - see recipes.models.PosDailyPayment).
    count: int = 0


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

    # -- the payments (the `SalesDocument` sheet) -------------------------------------
    #: {(day, method): DayPayment}, the method as PosDailyPayment.canonical
    #: files it. A day in `payment_days` with no key here was read and paid
    #: nothing (every ticket comped); a day in neither was never read.
    payments: dict = field(default_factory=dict)
    #: Every day with at least one ticket row, all-zero ones included: the
    #: days a writer replaces whole (recipes.payments.record_payments).
    payment_days: set = field(default_factory=set)
    #: Whether a payments sheet was read at all. False: nothing about the
    #: payments may be replaced from this reading.
    payments_read: bool = False
    #: Tickets read (after the duplicates), and the rows with no day (the
    #: export's own « Total » row).
    tickets: int = 0
    ticket_rows_skipped: int = 0
    #: Tickets whose `Paiements` did not read (filed at their total under
    #: UNREAD), and tickets with no payment but a total that is not 0
    #: (filed at it under UNPAID). Counted: neither has ever happened, which
    #: is exactly why they must be said the day they do.
    unread_payment_tickets: int = 0
    unpaid_tickets: int = 0
    #: The same `ID Ticket` twice in one file: read once.
    duplicate_tickets: int = 0
    #: Tickets whose payments are not their `Total TTC` plus `Trop perçus`.
    #: Kept as paid - the payments are what the bank sees - and said.
    tickets_not_adding_up: int = 0
    #: Files with no payments sheet, and files whose payments sheet could
    #: not be read (« name : why ») - their lines are read all the same.
    payment_sheets_missing: int = 0
    payment_sheet_errors: list = field(default_factory=list)
    #: Days a later file restated - see parse_sales_exports.
    repeated_payment_days: int = 0

    @property
    def payments_total(self) -> Decimal:
        return sum((payment.amount for payment in self.payments.values()), ZERO)

    def payments_by_method(self) -> dict[str, DayPayment]:
        """{method: DayPayment} over every day read."""
        methods: dict[str, DayPayment] = {}
        for (_day, method), payment in self.payments.items():
            total = methods.setdefault(method, DayPayment())
            total.amount += payment.amount
            total.count += payment.count
        return methods

    def payments_by_day(self) -> dict[date, dict[str, DayPayment]]:
        """{day: {method: DayPayment}} for every day read - an all-comped
        day as {} (read, nothing paid)."""
        days: dict[date, dict[str, DayPayment]] = {day: {} for day in self.payment_days}
        for (day, method), payment in self.payments.items():
            days.setdefault(day, {})[method] = payment
        return days

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
    """The day a cell says, or None where it says none (the « Total » row's
    « - »). A day outside 2000-2099 refuses the file: the pages count back
    and forth from it, and a year 1 is no sale."""
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    else:
        text = str(value or "").strip()
        try:
            day = date.fromisoformat(text)
        except ValueError:
            return None
    if not FIRST_DAY <= day <= LAST_DAY:
        raise LadditionExportError(f"Jour hors limites dans le fichier : {day:%d/%m/%Y} (de 2000 à 2099).")
    return day


def _plain(text: str) -> Decimal | None:
    """`text` as a Decimal when it is a plain number (PLAIN_NUMBER), None
    when it is no number at all. One Decimal reads but is no plain number -
    an exponent, NaN, Infinity, digits that are not ASCII - refuses the
    file: summed, it made the stored row unreadable for good."""
    if PLAIN_NUMBER.fullmatch(text):
        return Decimal(text)
    try:
        Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    raise LadditionExportError(f"Nombre refusé dans le fichier : « {text[:40]} » (ni exposant, ni NaN, ni infini).")


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    # Quantities arrive as ints, but a « 2.0 » sneaks through when Excel has
    # decided a column is numeric - read as before, toward zero.
    number = _plain(str(value).strip().replace(",", "."))
    if number is None:
        return None
    quantity = int(number)
    if abs(quantity) > MAX_LINE_QUANTITY:
        raise LadditionExportError(
            f"Quantité refusée dans le fichier : {quantity} sur une ligne (au plus {MAX_LINE_QUANTITY})."
        )
    return quantity


def _to_money(value) -> Decimal | None:
    """An amount as the export writes it ("4.5", "0.00", "-1.5").

    Decimal, never float: this is the only figure in the application that
    says what came in, and a cent lost per line is a euro a day.
    """
    if value is None:
        return None
    text = str(value).strip().replace("\N{NARROW NO-BREAK SPACE}", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    return _plain(text)


def _to_rate(value) -> Decimal | None:
    """ "20%" -> 0.20. None when the cell says nothing that can be trusted.

    The minus sign belongs to the amount, not to the rate: a refund line
    prints "-20%" beside its negative price (7 lines on the stored
    exports), and read as a rate of its own it would be no French rate at
    all - the line's HT dropped and reported as unreadable, for a rate that
    was perfectly legible.
    """
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", ".")
    if not text or not PLAIN_NUMBER.fullmatch(text):
        return None
    rate = abs(Decimal(text)) / 100
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

    Bounded (the module's docstring): a quantity, a (product, day)'s units
    and its revenue, a name cut to 255. An arithmetic error is this file's
    refusal, never a traceback.
    """
    try:
        return _parse_rows(rows)
    except ArithmeticError as exc:
        logger.warning("Lignes de ventes : nombre illisible (%s)", exc)
        raise LadditionExportError("Ce fichier porte un nombre que le calcul ne peut pas lire.") from None


def _missing(sheet: str, missing: list[str]) -> LadditionExportError:
    columns = ", ".join(f"« {name} »" for name in missing)
    return LadditionExportError(f"La feuille « {sheet} » n'a pas la ou les colonnes {columns} : {NOT_THE_EXPORT}")


def _parse_rows(rows) -> ParsedExport:
    rows = iter(rows)
    try:
        header = [str(cell or "").strip() for cell in next(rows)]
    except StopIteration:
        raise LadditionExportError(f"La feuille « {SALES_SHEET} » est vide.") from None

    missing = [c for c in (DAY_COLUMN, NAME_COLUMN, QUANTITY_COLUMN) if c not in header]
    if missing:
        raise _missing(SALES_SHEET, missing)
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

    def _short(text: str) -> str:
        return text[:NAME_LENGTH].strip()

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
        name = _short(str(row[name_at] or "").strip())
        quantity = _to_int(row[quantity_at])
        # The export's own "Total" row has a "-" where the date should be.
        if day is None or not name or quantity is None:
            result.skipped += 1
            continue

        key = (name, day)
        if key not in totals:
            order.append(key)
        totals[key] = totals.get(key, 0) + quantity
        if abs(totals[key]) > MAX_DAY_QUANTITY:
            raise LadditionExportError(
                f"« {name} » le {day:%d/%m/%Y} : plus de {MAX_DAY_QUANTITY} unités en un jour. Fichier refusé."
            )
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
        product["category"] = product["category"] or _short(_cell(row, category_at))
        product["typology"] = product["typology"] or _short(_cell(row, typology_at))

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
        check_revenue(key, money)
        result.money[key] = money
    return result


def check_revenue(key: tuple[str, date], money: DayMoney) -> None:
    """A (product, day)'s money fits the columns that store it (10, 2), or
    the file is refused, naming it: stored wider, every later read of the
    row raises - Marges and « Entrées d'argent » a 500 for good."""
    name, day = key
    for figure in (money.revenue_ttc, money.revenue_ht, money.without_rate_ttc):
        if abs(figure) > MAX_REVENUE:
            raise LadditionExportError(
                f"« {name} » le {day:%d/%m/%Y} : une recette de plus de 99 999 999,99 € en un jour. Fichier refusé."
            )


def check_payment(day: date, method: str, payment: DayPayment) -> None:
    """A (day, method)'s payments fit `PosDailyPayment.amount` (12, 2)."""
    if abs(payment.amount) > MAX_PAYMENT:
        raise LadditionExportError(
            f"Paiements du {day:%d/%m/%Y} ({method or 'sans moyen'}) : plus de 9 999 999 999,99 €. Fichier refusé."
        )


def _payment_amount(text: str) -> Decimal | None:
    """« 4,50 », « 1 350,00 », « -6,00 » -> Decimal. None when the text is
    not one amount (PAYMENT_AMOUNT) - never a best guess."""
    text = str(text or "").strip()
    for separator in THOUSANDS_SEPARATORS:
        text = text.replace(separator, "")
    if not PAYMENT_AMOUNT.match(text):
        return None
    return Decimal(text.replace(",", "."))


def read_payments(text: str) -> list[tuple[str, Decimal]] | None:
    """A `Paiements` cell as [(method as printed, amount)], in order.

    [] for an empty cell. None when the cell is not the till's grammar
    (PAYMENTS_GRAMMAR), a method is blank or an amount does not read: the
    whole ticket is then unread, never half of it - a ticket read as its
    card payment alone would look perfectly ordinary and be short of its
    cash.
    """
    text = str(text or "").strip()
    if not text:
        return []
    if not PAYMENTS_GRAMMAR.match(text):
        return None
    payments = []
    for method, amount_text in ONE_PAYMENT.findall(text):
        method = method.strip()
        amount = _payment_amount(amount_text)
        if not method or amount is None:
            return None
        payments.append((method, amount))
    return payments


def parse_payment_rows(rows) -> ParsedExport:
    """Sum raw `SalesDocument` rows (header first) per (day, method).

    Only the payment fields of the result are filled. One ticket is:

    - **paid**: each payment filed under its method, the method matched
      accent- and case-blind to the till's own spelling
      (PosDailyPayment.canonical), its amount signed as printed. Checked
      against the receipt's own arithmetic - payments = `Total TTC` +
      `Trop perçus` on every ticket stored - and a ticket that does not add
      up is kept as paid (the payments are what the bank sees) and counted;
    - **unreadable** (`read_payments` says None): its `Total TTC` filed under
      UNREAD, so the day still adds up to what it took, and counted;
    - **empty**: a `Total TTC` of 0 is a comped ticket - nothing paid,
      nothing filed, its day still read. Any other total is filed under
      UNPAID, and counted - never dropped.

    A row with no day (the export's « Total » row prints « - ») is skipped
    and counted; a ticket id seen before in the same file is read once.
    Bounded like the lines: each (day, method) fits `PosDailyPayment`.
    """
    try:
        return _parse_payment_rows(rows)
    except ArithmeticError as exc:
        logger.warning("Paiements : nombre illisible (%s)", exc)
        raise LadditionExportError("La feuille des tickets porte un nombre que le calcul ne peut pas lire.") from None


def _parse_payment_rows(rows) -> ParsedExport:
    # The vocabulary has one home, the model; imported here so this module
    # still loads where Django is not set up (a probe script, a shell).
    from recipes.models import PosDailyPayment

    rows = iter(rows)
    try:
        header = [str(cell or "").strip() for cell in next(rows)]
    except StopIteration:
        raise LadditionExportError(f"La feuille « {PAYMENTS_SHEET} » est vide.") from None
    missing = [c for c in (DAY_COLUMN, TICKET_TOTAL_COLUMN, PAYMENTS_COLUMN) if c not in header]
    if missing:
        raise _missing(PAYMENTS_SHEET, missing)
    day_at = header.index(DAY_COLUMN)
    total_at = header.index(TICKET_TOTAL_COLUMN)
    payments_at = header.index(PAYMENTS_COLUMN)
    ticket_at = header.index(TICKET_COLUMN) if TICKET_COLUMN in header else None
    overpaid_at = header.index(OVERPAID_COLUMN) if OVERPAID_COLUMN in header else None

    def _cell(row, at):
        return str(row[at] or "").strip() if at is not None and at < len(row) else ""

    result = ParsedExport(payments_read=True)
    seen: set[str] = set()

    def file(day, method, amount) -> None:
        payment = result.payments.setdefault((day, PosDailyPayment.canonical(method)), DayPayment())
        payment.amount += amount
        payment.count += 1

    for row in rows:
        day = _to_date(_cell(row, day_at))
        if day is None:
            result.ticket_rows_skipped += 1
            continue
        ticket = _cell(row, ticket_at)
        if ticket:
            if ticket in seen:
                result.duplicate_tickets += 1
                continue
            seen.add(ticket)
        result.tickets += 1
        result.payment_days.add(day)
        total = _to_money(_cell(row, total_at))
        payments = read_payments(_cell(row, payments_at))

        if payments is None or (not payments and total is None):
            # Nothing tells what was paid: filed at the ticket's total - or
            # at 0 when even that is illegible, still counted.
            result.unread_payment_tickets += 1
            file(day, PosDailyPayment.UNREAD, total or ZERO)
            continue
        if not payments:
            if total:
                result.unpaid_tickets += 1
                file(day, PosDailyPayment.UNPAID, total)
            continue
        for method, amount in payments:
            file(day, method, amount)
        if overpaid_at is None or total is None:
            continue  # nothing to check against
        overpaid_text = _cell(row, overpaid_at)
        overpaid = _to_money(overpaid_text) if overpaid_text else ZERO
        if overpaid is not None and sum((amount for _m, amount in payments), ZERO) != total + overpaid:
            result.tickets_not_adding_up += 1
    for (day, method), payment in result.payments.items():
        check_payment(day, method, payment)
    return result


def _unreadable():
    """Every way a file can fail to be this export, as the zip and XML
    machinery raise it: none of these is an `XlsxError`, and each one that
    escaped stopped a whole folder being read (see parse_sales_export). A
    zip holding no workbook at all raises KeyError, malformed XML a
    ParseError. A member whose deflate stream no longer inflates - the
    zip's directory intact, the sheet's bytes damaged - raises `zlib.error`,
    and one whose stream stops before its end marker `EOFError`: neither is
    a zip error either (« Données » refuses the same damage in its archive,
    transfer/archive.DAMAGED).

    Deliberately NOT RuntimeError, which zipfile raises for an encrypted
    member: LadditionExportError and PaymentsSheetMissing are RuntimeErrors,
    and caught here parse_payments_export would wrap a missing sheet as a
    broken file."""
    import zipfile
    import zlib
    from xml.etree.ElementTree import ParseError

    from .xlsx_reader import XlsxError

    return (XlsxError, zipfile.BadZipFile, zlib.error, EOFError, OSError, KeyError, ParseError)


def _file_name(path) -> str:
    """The file's own name, never its folder: a log line may reach a page."""
    from pathlib import Path

    return Path(str(getattr(path, "name", path) or "")).name or "fichier"


def _add_payments(result: ParsedExport, path, *, untrusted: bool = False) -> None:
    """The payments sheet of the file just read, onto `result` - or said on
    it. Neither a file with no such sheet (an older export) nor one whose
    sheet does not read may cost that file its lines: the sales are read,
    and the payments are either whole or absent, never half a sheet (the
    sheet is parsed apart, and only a complete reading is taken). Said in
    French: this module's own refusal or the reader's, else one fixed
    sentence for what the zip or the XML machinery raised (its detail - a
    library's English - in the log)."""
    from .xlsx_reader import XlsxError, read_sheet, sheet_names

    try:
        if PAYMENTS_SHEET not in sheet_names(path, untrusted=untrusted):
            result.payment_sheets_missing += 1
            return
        part = parse_payment_rows(read_sheet(path, PAYMENTS_SHEET, untrusted=untrusted))
    except (LadditionExportError, XlsxError) as exc:
        result.payment_sheet_errors.append(f"{_file_name(path)} : {exc}")
        return
    except _unreadable() as exc:
        logger.warning("Feuille des tickets illisible dans %s : %r", _file_name(path), exc)
        result.payment_sheet_errors.append(f"{_file_name(path)} : la feuille ne se lit pas.")
        return
    _take_payments(result, part)


def _take_payments(result: ParsedExport, part: ParsedExport) -> None:
    result.payments = part.payments
    result.payment_days = part.payment_days
    result.payments_read = True
    for counter in _PAYMENT_COUNTERS:
        setattr(result, counter, getattr(result, counter) + getattr(part, counter))


#: The payment counters that are about the READING, summed file after file
#: like `skipped` - a file read twice counts its tickets twice, which is what
#: makes a duplicate file visible.
_PAYMENT_COUNTERS = (
    "tickets",
    "ticket_rows_skipped",
    "unread_payment_tickets",
    "unpaid_tickets",
    "duplicate_tickets",
    "tickets_not_adding_up",
)


def _not_the_export(exc: BaseException, path) -> LadditionExportError:
    """What the zip or the XML machinery raised, as this export's refusal:
    the reader's own French sentence (an XlsxError names no path), else one
    fixed sentence - the detail, which may name the folder, to the log."""
    from .xlsx_reader import XlsxError

    logger.warning("Export L'Addition illisible (%s) : %r", _file_name(path), exc)
    if isinstance(exc, XlsxError):
        return LadditionExportError(f"{exc} {NOT_THE_EXPORT}")
    return LadditionExportError(NOT_THE_EXPORT)


def parse_payments_export(path) -> ParsedExport:
    """Read the payments of one downloaded .xlsx, and nothing else - the
    backfill's reading (laddition_backfill_payments): the lines sheet is the
    big one, and the payments do not need it.

    Raises PaymentsSheetMissing when the file has no payments sheet, and
    LadditionExportError for every other way it is not this export.
    """
    from .xlsx_reader import read_sheet, sheet_names

    try:
        if PAYMENTS_SHEET not in sheet_names(path):
            raise PaymentsSheetMissing(f"Pas de feuille « {PAYMENTS_SHEET} » dans ce fichier.")
        return parse_payment_rows(read_sheet(path, PAYMENTS_SHEET))
    except _unreadable() as exc:
        raise _not_the_export(exc, path) from exc


def parse_sales_export(path, *, untrusted: bool = False) -> ParsedExport:
    """Read one downloaded .xlsx: its lines, then its payments.

    Uses this package's own reader rather than openpyxl, which refuses the
    file outright - see xlsx_reader for the gory details.

    An .xlsx is a zip, and the folder this reads from is scanned whole: a
    half-finished download, a file Excel was holding, anything renamed
    `.xlsx` by hand raises `zipfile.BadZipFile` or an `OSError` from inside
    the zip machinery. Neither is an `XlsxError`, so both escaped this
    function AND the backfill's own « illisible » branch - one such file
    stopped the other sixteen exports being read at all, with a traceback.
    Every way a file can fail to be this export answers in one type.

    The payments sheet is OPTIONAL: missing or unreadable, it is counted on
    the result (`payment_sheets_missing`, `payment_sheet_errors`) and the
    lines import all the same - see _add_payments.

    `untrusted`: a file a person uploaded - the reader's bounds on the zip
    and its string table (xlsx_reader.check_untrusted).
    """
    from .xlsx_reader import read_sheet

    try:
        result = parse_rows(read_sheet(path, SALES_SHEET, untrusted=untrusted))
    except _unreadable() as exc:
        raise _not_the_export(exc, path) from exc
    _add_payments(result, path, untrusted=untrusted)
    return result


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

    The payments follow the same rule by DAY: a day a later file read again
    replaces every method of that day (`repeated_payment_days`), a method
    the later reading lacks included - summed, a folder read twice would
    have shown twice the card takings; merged method by method, a day could
    keep a payment its second reading no longer has. A file with no
    payments sheet, or one that did not read, replaces nothing.
    """
    combined = ParsedExport()
    at: dict[tuple[str, date], int] = {}
    for path in paths:
        part = parse_sales_export(path)
        _combine_payments(combined, part)
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


def _combine_payments(combined: ParsedExport, part: ParsedExport) -> None:
    """One file's payments onto the folder's - see parse_sales_exports."""
    combined.payment_sheets_missing += part.payment_sheets_missing
    combined.payment_sheet_errors.extend(part.payment_sheet_errors)
    if not part.payments_read:
        return
    combined.payments_read = True
    for counter in _PAYMENT_COUNTERS:
        setattr(combined, counter, getattr(combined, counter) + getattr(part, counter))
    restated = combined.payment_days & part.payment_days
    combined.repeated_payment_days += len(restated)
    if restated:
        combined.payments = {key: value for key, value in combined.payments.items() if key[0] not in restated}
    combined.payments.update(part.payments)
    combined.payment_days |= part.payment_days
