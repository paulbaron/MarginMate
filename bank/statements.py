"""Reading a bank statement exported as CSV - laid out as a `StatementFormat`
says, the operations described by the recognition rules.

    "Compte de chèques";"Compte de chèques";****0042;14/09/2026;;1 234,56
    03/08/2026;PAIEMENT CB;FACTURE CARTE;FACTURE CARTE DU 010826 FRANPRIX 5333   PARIS   CARTE   4974XXXXXXXX1111;03/08/2026;-4,10

No bank's layout is written here. A format (`models.StatementFormat`, edited
on « Format du relevé ») says which encoding the file is in, which separator
splits it, which column holds the date, the label (one or several), the
amount (one signed column, or a column of debits and one of credits), the
value date and the operation type, how dates and decimals are printed, and
what the account number looks like in the lines above the operations. The
owner's bank is seeded as a format (migration 0007), exactly as this module
read it before - so the same file gives the same fingerprints, and a
statement imported again is known again. What each operation IS - its kind,
its payee, the day a card was used - is the recognition rules'
(`bank.recognition.describe`, handed in as `rules`).

A row is an operation when its date column holds a date of the format, whole;
every other row (a header, a balance, a blank) is passed over - the account
number is looked for in those above the first operation. An operation that
cannot be read whole refuses the FILE, in French, naming the row: one line
dropped in silence is a payment nobody will ever look for.

Pure: `check_format` compiles a format (any object carrying its fields - a
StatementFormat, a form's data, a test's namespace) or says what is wrong
with it (FormatError, a field and a sentence); `parse_statement` reads bytes
with it. Nothing here touches the database.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from time import thread_time

from returnables import patterns
from returnables.patterns import PatternError

from . import recognition
from .models import BankTransaction, StatementFormat

#: How a date of each format is told apart from any other cell (the whole
#: cell, spaces aside), and read. A day and a month of one digit or two:
#: a US export or a spreadsheet re-saving the file drops the zero, and a
#: row whose date was passed over for it was a payment lost in silence -
#: `strptime` reads both, and the fingerprint spells the day the same.
DATE_FORMATS = {
    StatementFormat.DateFormat.DAY_MONTH_YEAR: (r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}", "%d/%m/%Y"),
    StatementFormat.DateFormat.DAY_MONTH_SHORT_YEAR: (r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{2}", "%d/%m/%y"),
    StatementFormat.DateFormat.DAY_MONTH_YEAR_DASHES: (r"[0-9]{1,2}-[0-9]{1,2}-[0-9]{4}", "%d-%m-%Y"),
    StatementFormat.DateFormat.DAY_MONTH_YEAR_DOTS: (r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4}", "%d.%m.%Y"),
    StatementFormat.DateFormat.ISO: (r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}", "%Y-%m-%d"),
    StatementFormat.DateFormat.MONTH_DAY_YEAR: (r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}", "%m/%d/%Y"),
}
#: The days a statement may hold: anything else is a misread column, and a
#: year 1 put every matching window out of the calendar.
FIRST_DAY, LAST_DAY = date(2000, 1, 1), date(2099, 12, 31)
#: The widest amount `BankTransaction.amount` (12, 2) holds - the column
#: exactly, as `invoices.einvoice.MAX_AMOUNT`. Bounded before rounding: SQLite
#: keeps a REAL, and 9 999 999 999,995 read back rounds to ten billion,
#: which raises on every read of the line - delete included (CLAUDE.md,
#: « A figure wider than the column »).
MAX_AMOUNT = Decimal("9999999999.99")
#: The widths of `BankTransaction.account` and `.bank_type`: a type read
#: longer is cut (it is in no fingerprint); an account longer refuses the
#: file - it is in every fingerprint. Stored whole, either was skipped by a
#: « Données » restore, the line's links and decisions with it.
ACCOUNT_MAX = BankTransaction._meta.get_field("account").max_length
BANK_TYPE_MAX = BankTransaction._meta.get_field("bank_type").max_length
#: The highest column a format may name, and how many label columns.
MAX_COLUMN = 50
MAX_LABEL_COLUMNS = 5
#: The named group of the account pattern.
ACCOUNT = "compte"
#: Why a file is refused: what the csv module cannot split, an account
#: pattern that takes too long or reads too long an account, and a row
#: wider than its header where the separator can be printed in an amount.
NOT_A_CSV = "Ce fichier ne se lit pas comme un CSV : ce n'est pas un relevé bancaire exporté."
ACCOUNT_TOO_SLOW = "Le motif du numéro de compte est trop lent : simplifiez-le dans le format du relevé."
ACCOUNT_TOO_LONG = (
    f"Le numéro de compte lu fait plus de {ACCOUNT_MAX} caractères : "
    f"resserrez le motif du format du relevé avec (?P<{ACCOUNT}>…)."
)
WIDER_THAN_HEADER = (
    "Ligne plus longue que l'en-tête : un montant non entre guillemets ? "
    "Exportez avec un autre séparateur et changez celui du format du relevé"
)
#: The byte order marks a single-byte encoding never begins with.
UNICODE_MARKS = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)

#: The fields a refusal of `check_format` names - the form's own.
DATE_COLUMN, LABEL_COLUMNS = "date_column", "label_columns"
AMOUNT_COLUMN, DEBIT_COLUMN, CREDIT_COLUMN = "amount_column", "debit_column", "credit_column"
VALUE_DATE_COLUMN, BANK_TYPE_COLUMN = "value_date_column", "bank_type_column"
ACCOUNT_PATTERN = "account_pattern"
#: What each column is called when two of them clash.
COLUMN_ROLES = {
    DATE_COLUMN: "la date",
    LABEL_COLUMNS: "le libellé",
    AMOUNT_COLUMN: "le montant",
    DEBIT_COLUMN: "les débits",
    CREDIT_COLUMN: "les crédits",
    VALUE_DATE_COLUMN: "la date de valeur",
    BANK_TYPE_COLUMN: "le type d'opération",
}
_LIST_SEPARATOR = re.compile(r"[\s,;]+")


class FormatError(ValueError):
    """A format that cannot read a statement: `field` is the form field the
    sentence belongs to."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Layout:
    """A format, checked and compiled: columns counted from 0."""

    name: str
    encoding: str
    delimiter: str
    date_format: str
    decimal_mark: str
    date: int
    labels: tuple
    amount: int | None
    debit: int | None
    credit: int | None
    value_date: int | None
    bank_type: int | None
    account: object | None

    @property
    def width(self) -> int:
        """How many cells an operation's row needs."""
        used = [self.date, *self.labels, self.amount, self.debit, self.credit, self.value_date, self.bank_type]
        return max(column for column in used if column is not None) + 1

    def joined(self, row) -> str:
        """A row as the file printed it, for a refusal and for the account."""
        return (self.delimiter if self.delimiter.isprintable() else " ").join(row)


def label_columns(text) -> list[int]:
    """« 4 » or « 3, 4 » as numbers - FormatError when it is not that."""
    parts = [part for part in _LIST_SEPARATOR.split(str(text or "").strip()) if part]
    if not parts:
        raise FormatError(LABEL_COLUMNS, "Indiquez au moins une colonne pour le libellé.")
    if len(parts) > MAX_LABEL_COLUMNS:
        raise FormatError(LABEL_COLUMNS, f"{MAX_LABEL_COLUMNS} colonnes au plus pour le libellé.")
    numbers = []
    for part in parts:
        # Its length first: `int` refuses a string past 4 300 digits with a
        # ValueError of its own, which was a 500 here.
        if not (part.isascii() and part.isdigit()) or len(part) > 3 or not 1 <= int(part) <= MAX_COLUMN:
            raise FormatError(LABEL_COLUMNS, f"« {part} » n'est pas un numéro de colonne (de 1 à {MAX_COLUMN}).")
        if int(part) in numbers:
            raise FormatError(LABEL_COLUMNS, f"La colonne {part} est indiquée deux fois.")
        numbers.append(int(part))
    return numbers


def check_format(fmt) -> Layout:
    """The `Layout` of `fmt` (a StatementFormat, or any object carrying its
    fields), or FormatError naming the field: a column outside 1..MAX_COLUMN,
    a column given two roles, no amount or an amount said twice (one signed
    column OR debits and credits), an unknown choice, an account pattern the
    guard of `returnables.patterns` refuses."""
    choices = (
        ("encoding", StatementFormat.Encoding, "Encodage inconnu."),
        ("delimiter", StatementFormat.Delimiter, "Séparateur inconnu."),
        ("date_format", StatementFormat.DateFormat, "Format de date inconnu."),
        ("decimal_mark", StatementFormat.DecimalMark, "Séparateur décimal inconnu."),
    )
    for attribute, choice, refusal in choices:
        if getattr(fmt, attribute, None) not in choice.values:
            raise FormatError(attribute, refusal)

    def column(attribute, *, required=False) -> int | None:
        value = getattr(fmt, attribute, None)
        if value in (None, ""):
            if required:
                raise FormatError(attribute, "Indiquez une colonne.")
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_COLUMN:
            raise FormatError(attribute, f"Un numéro de colonne de 1 à {MAX_COLUMN}.")
        return value

    date_column = column(DATE_COLUMN, required=True)
    labels = label_columns(getattr(fmt, LABEL_COLUMNS, ""))
    amount, debit, credit = column(AMOUNT_COLUMN), column(DEBIT_COLUMN), column(CREDIT_COLUMN)
    if amount is None and debit is None and credit is None:
        raise FormatError(AMOUNT_COLUMN, "Indiquez la colonne du montant, ou celles des débits et des crédits.")
    if amount is not None and (debit is not None or credit is not None):
        raise FormatError(
            AMOUNT_COLUMN, "Un montant signé OU des débits et des crédits : pas les deux (laissez l'un vide)."
        )
    value_date, bank_type = column(VALUE_DATE_COLUMN), column(BANK_TYPE_COLUMN)

    roles: dict[int, str] = {}
    for attribute, numbers in (
        (DATE_COLUMN, [date_column]),
        (LABEL_COLUMNS, labels),
        (AMOUNT_COLUMN, [amount]),
        (DEBIT_COLUMN, [debit]),
        (CREDIT_COLUMN, [credit]),
        (VALUE_DATE_COLUMN, [value_date]),
        (BANK_TYPE_COLUMN, [bank_type]),
    ):
        for number in numbers:
            if number is None:
                continue
            if number in roles:
                raise FormatError(
                    attribute,
                    f"La colonne {number} sert deux fois : pour {roles[number]} et pour {COLUMN_ROLES[attribute]}.",
                )
            roles[number] = COLUMN_ROLES[attribute]

    account = None
    pattern = str(getattr(fmt, ACCOUNT_PATTERN, "") or "").strip()
    if pattern:
        try:
            account = patterns.compile_pattern(pattern, field_label="Motif du numéro de compte")
        except PatternError as error:
            raise FormatError(ACCOUNT_PATTERN, error.message) from None
        stray = sorted(set(account.groupindex) - {ACCOUNT})
        if stray:
            raise FormatError(
                ACCOUNT_PATTERN,
                f"Motif du numéro de compte : le groupe (?P<{stray[0]}>…) ne sert à rien ; seul (?P<{ACCOUNT}>…) est lu.",
            )

    def zero_based(number):
        return None if number is None else number - 1

    return Layout(
        name=str(getattr(fmt, "name", "") or ""),
        encoding=fmt.encoding,
        delimiter=fmt.delimiter,
        date_format=fmt.date_format,
        decimal_mark=fmt.decimal_mark,
        date=date_column - 1,
        labels=tuple(number - 1 for number in labels),
        amount=zero_based(amount),
        debit=zero_based(debit),
        credit=zero_based(credit),
        value_date=zero_based(value_date),
        bank_type=zero_based(bank_type),
        account=account,
    )


@dataclass
class StatementLine:
    operation_date: date
    value_date: date | None
    bank_type: str
    label: str
    amount: Decimal
    kind: str
    counterparty: str
    card_date: date | None
    fingerprint: str = ""


@dataclass
class Statement:
    account: str
    lines: list[StatementLine] = field(default_factory=list)


def parse_statement(content: bytes, rules: recognition.Rules, fmt) -> Statement:
    """Every operation of the file, laid out as `fmt` says (a
    StatementFormat, or a `Layout` already checked) and described by `rules`
    (one `recognition.load()`, read by the caller). Refused - ValueError, a
    French sentence - for a format that cannot read a statement, a file that
    is no statement of that format, a row cut short (or wider than its
    header, where the separator can be printed in an amount), a date or an
    amount that cannot be read, an account wider than its column, and, once
    every row was read, any rule that could not be applied
    (`Rules.refusal`): a kind stored wrong is never read again."""
    layout = fmt if isinstance(fmt, Layout) else check_format(fmt)
    date_cell, date_format = DATE_FORMATS[layout.date_format]
    date_re = re.compile(date_cell)
    search_account = _AccountSearch(layout.account)
    splits_amounts = _splits_amounts(layout)
    account = ""
    # The header of columns above the first operation: the widest row there
    # holding at least the cells the format reads (a title or an account
    # line is none) - 0 while there is none.
    header = 0
    lines: list[StatementLine] = []
    for row in rows(content, layout):
        operation_date = _date(row, layout.date, date_re, date_format)
        if operation_date is None:
            if not lines:
                if len(row) >= layout.width:
                    header = max(header, len(row))
                if layout.account is not None:
                    found = search_account(layout.joined(row))
                    if found is not None:
                        account = patterns.captured(found, ACCOUNT) or found.group()
            continue
        if len(row) < layout.width:
            raise ValueError(f"Ligne incomplète dans le relevé : {layout.joined(row)[:80]}")
        if splits_amounts and header and len(row) > header:
            # « -4,10 » unquoted under a « , » separator is two cells, « -4 »
            # and « 10 »: read, the cents were gone, nothing said. Every such
            # row grows by the same cell, so only the header gives it away.
            raise ValueError(f"{WIDER_THAN_HEADER} - {layout.joined(row)[:80]}")
        label = " ".join(" ".join(row[column].split()) for column in layout.labels if row[column].strip())
        # Cut BEFORE the rules read it: the type stored is the type they read.
        bank_type = row[layout.bank_type].strip()[:BANK_TYPE_MAX].rstrip() if layout.bank_type is not None else ""
        value_date = _date(row, layout.value_date, date_re, date_format) if layout.value_date is not None else None
        described = recognition.describe(rules, label, bank_type, operation_date)
        lines.append(
            StatementLine(
                operation_date=operation_date,
                value_date=value_date,
                bank_type=bank_type,
                label=label,
                amount=_amount(row, layout),
                kind=described.kind,
                counterparty=described.counterparty,
                card_date=described.card_date,
            )
        )
    if not lines:
        said = f" (format « {layout.name} »)" if layout.name else ""
        raise ValueError(
            f"Aucune opération trouvée : ce fichier ne ressemble pas à un relevé bancaire exporté en CSV{said}."
        )
    if len(account) > ACCOUNT_MAX:
        # Never cut: it is in every fingerprint, and a cut one is no account
        # - the pattern tightened later reads another, and every operation
        # of an export imported again would be new.
        raise ValueError(ACCOUNT_TOO_LONG)
    # After every row: a rule found too slow on the last one counts too.
    if rules.refusal:
        raise ValueError(rules.refusal)
    _fingerprint(account, lines)
    return Statement(account=account, lines=lines)


def parse_amount(text: str, decimal_mark: str = ",") -> Decimal:
    """An amount as a statement prints it: spaces of any kind (and « ' »)
    between its thousands, the other mark than `decimal_mark` between groups
    of three digits, a sign in front (or a « - » behind). Read digit for
    digit - « 120,00 » is Decimal("120.00"), which a fingerprint spells - and
    refused (ValueError) rather than guessed: two decimal marks, a group that
    is not three digits, a letter, nothing, an amount wider than the column."""
    refused = ValueError(f"Montant illisible dans le relevé : {text!r}")
    digits = "".join(char for char in str(text).strip() if not char.isspace() and char != "'")
    sign = ""
    if digits[:1] in ("-", "+", "\N{MINUS SIGN}"):
        sign, digits = ("" if digits[0] == "+" else "-"), digits[1:]
    elif digits.endswith("-"):
        sign, digits = "-", digits[:-1]
    if digits.count(decimal_mark) > 1:
        raise refused
    whole, mark, fraction = digits.partition(decimal_mark)
    thousands = _thousands_mark(decimal_mark)
    if thousands in whole:
        if re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(thousands) + r"[0-9]{3})+", whole) is None:
            raise refused
        whole = whole.replace(thousands, "")
    if (mark and not fraction) or not (whole or fraction):
        raise refused
    if not all(part.isascii() and part.isdigit() for part in (whole, fraction) if part):
        raise refused
    # Digit for digit, as `Decimal` reads them: « ,50 » is 0.50 as before.
    amount = Decimal(f"{sign}{whole or '0'}.{fraction}" if mark else f"{sign}{whole}")
    if abs(amount) > MAX_AMOUNT:
        raise refused
    return amount


def _thousands_mark(decimal_mark: str) -> str:
    """The mark grouping thousands beside `decimal_mark`: the other one."""
    return "." if decimal_mark == "," else ","


def _splits_amounts(layout: Layout) -> bool:
    """Whether the separator may be printed inside an amount - the decimal
    mark, or the mark grouping its thousands: an amount left unquoted is
    then split over two cells."""
    return layout.delimiter in (layout.decimal_mark, _thousands_mark(layout.decimal_mark))


def _amount(row, layout: Layout) -> Decimal:
    """The row's amount, negative when money went out: its signed column, or
    its credit less its debit - whichever of the two is printed."""
    if layout.amount is not None:
        return parse_amount(row[layout.amount], layout.decimal_mark)
    debit = row[layout.debit] if layout.debit is not None else ""
    credit = row[layout.credit] if layout.credit is not None else ""
    if not debit.strip() and not credit.strip():
        raise ValueError(f"Montant illisible dans le relevé : ni débit ni crédit sur {layout.joined(row)[:80]!r}")
    paid = abs(parse_amount(debit, layout.decimal_mark)) if debit.strip() else None
    received = abs(parse_amount(credit, layout.decimal_mark)) if credit.strip() else None
    if received is None:
        # Not both blank (refused above): a debit alone.
        return -abs(parse_amount(debit, layout.decimal_mark))
    if paid is None:
        return received
    return received - paid


def rows(content: bytes, layout: Layout) -> list[list[str]]:
    """The file's rows that hold anything. A file the csv module cannot
    split (a cell past its limit, a line ended by a lone carriage return)
    is refused in French, never a 500 - and so is a NUL byte, which no text
    export holds and which the csv module reads into the cell since Python
    3.11: the label went into the database with it."""
    text = _decode(content, layout.encoding)
    if "\N{NULL}" in text:
        raise ValueError(NOT_A_CSV)
    try:
        return [
            row
            for row in csv.reader(io.StringIO(text), delimiter=layout.delimiter)
            if any(cell.strip() for cell in row)
        ]
    except csv.Error:
        raise ValueError(NOT_A_CSV) from None


def _decode(content: bytes, encoding: str) -> str:
    """The text of the file: « auto » is UTF-16 behind its byte order mark,
    else UTF-8 (with or without one), else Windows-1252 - the owner's bank.

    A file opening on a Unicode byte order mark is never read as
    Windows-1252 or ISO-8859-1, though either decodes anything: the mark
    became « ï»¿ » in the first cell, and a first operation with no header
    above it was passed over, nothing said."""
    Encoding = StatementFormat.Encoding
    if encoding == Encoding.AUTO:
        if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = Encoding.UTF16
        else:
            try:
                return content.decode("utf-8-sig")
            except UnicodeDecodeError:
                if content.startswith(codecs.BOM_UTF8):
                    raise ValueError(_not_in(Encoding.UTF8)) from None
                encoding = Encoding.CP1252
    elif encoding in (Encoding.CP1252, Encoding.LATIN1) and content.startswith(UNICODE_MARKS):
        raise ValueError(_not_in(encoding))
    try:
        return content.decode("utf-8-sig" if encoding == Encoding.UTF8 else encoding)
    except UnicodeDecodeError:
        raise ValueError(_not_in(encoding)) from None


def _not_in(encoding: str) -> str:
    label = StatementFormat.Encoding(encoding).label
    return f"Ce fichier n'est pas en {label} : changez l'encodage du format du relevé, ou exportez-le à nouveau."


def _date(row, column: int, date_re, date_format: str) -> date | None:
    """The date in that cell of the row, or None where the cell holds no
    date of the format (a header, a balance). A date of the format that is
    no day, or no day a statement can hold, refuses the file."""
    if column >= len(row):
        return None
    printed = row[column].strip()
    if not date_re.fullmatch(printed):
        return None
    try:
        day = datetime.strptime(printed, date_format).date()  # noqa: DTZ007 - the statement's printed day
    except ValueError:
        raise ValueError(f"Date illisible dans le relevé : {printed!r}") from None
    if not FIRST_DAY <= day <= LAST_DAY:
        raise ValueError(f"Date illisible dans le relevé : {printed!r}")
    return day


class _AccountSearch:
    """The account pattern over the rows above the first operation: each
    search within the per-match limit - asked twice, as a rule's is
    (`recognition.search`): one search can lose the clock's limit to a busy
    server, and the sentence would blame a pattern that is fine - and all
    of them within `recognition.RULE_SECONDS` of the thread's own time (as
    a rule is billed): a pattern just short of the limit on row after row -
    a file whose date column never holds a date of the format - is too
    slow all the same, and no single search gives it away."""

    def __init__(self, pattern):
        self.pattern = pattern
        self.spent = 0.0

    def __call__(self, text: str):
        started = thread_time()
        try:
            found = recognition.search(self.pattern, text)
        except PatternError:
            raise ValueError(ACCOUNT_TOO_SLOW) from None
        self.spent += thread_time() - started
        if self.spent > recognition.RULE_SECONDS:
            raise ValueError(ACCOUNT_TOO_SLOW)
        return found


def _fingerprint(account: str, lines: list[StatementLine]) -> None:
    """The same operation in two exports gets the same fingerprint; two
    identical operations in one export (two baguettes, same morning, same
    card) get different ones - by their order among the identical rows."""
    seen: Counter = Counter()
    for line in lines:
        value_date = line.value_date.isoformat() if line.value_date else ""
        key = f"{account}|{line.operation_date.isoformat()}|{value_date}|{line.label}|{line.amount}"
        occurrence = seen[key]
        seen[key] += 1
        line.fingerprint = hashlib.sha256(f"{key}|{occurrence}".encode()).hexdigest()
