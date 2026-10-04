"""Reading any till's export - a CSV or an .xlsx - with a format a person
describes (`recipes.models.TillFormat`, « Formats des fichiers de caisse »).

No till is written in the code. A format says how ITS till's export is laid
out: what it holds (« Ventes par produit » or « Encaissements » - most tills
export them as two files), which column holds the day, the product, the
quantity, the amount, the rate..., each given by its title as the header
prints it (accents, case and spaces aside) or by its number counted from 1,
how dates and decimals are printed, the hour the service ends, and how the
till spells its means of payment. A day the file does not print (a daily Z
report) is given with the upload (`day`).

Pure: no database is written, nothing is read from it but the payments'
vocabulary (`PosDailyPayment`, imported lazily). It needs Django's settings
all the same: `common.search_key` folds a title or a spelling, and the last
day a file may hold is tomorrow (`timezone.localdate`). `check_format`
compiles a format - any object carrying its fields: a row, a form's data, a
test's namespace - or says what is wrong with it (FormatError: a field and a
French sentence); `read` reads a file with it into the very `ParsedExport`
L'Addition's reader returns, so the one writer (recipes.tasks.store_reading)
stores both.

The rules, each a way a till's file could be silently wrong money:

- **A row is read whole, or the file is refused naming it.** A day cell
  holding digits that are no date of the format, a dated row with a product
  and no readable quantity, a payment with no readable amount, an amount
  wider than its column: `TillFileError`, « ligne 12 : … ». Only what
  carries no sale is passed over - a row whose day cell is blank, or holds
  no digit and no readable sale (a « Total » footer, the header of a format
  given by numbers) - and counted, and said.
- **The business day**: a sale timed before `service_day_end_hour` belongs
  to the day before (a sale at 01:30 is the previous evening's service).
  A date cell may carry its time (« 03/07/2026 23:41 », ISO's « T »,
  fractional seconds), or a time column may be named; an .xlsx date cell is
  Excel's serial number, in the 1904 calendar where the workbook says so.
  Days between 2000 and tomorrow only (`DayReader.latest`): « 99 » in a
  jj/mm/aa column is 2099, and a day and a month the wrong way round can
  land in the months to come - a sale there is a misread column.
- **Money**: the line's amount TTC (remises déduites; a comp is 0), or a
  unit price multiplied by the quantity when the format says so. HT is
  worked out per rate bucket from French rates only (`KNOWN_RATES`, read as
  a percentage or a fraction - « 20 % », « 20 », « 0,2 » are one rate,
  « 19,6 » none), or summed from an HT column, which wins over the rate. No
  rate is ever assumed: the TTC whose rate does not read is
  `without_rate_ttc`. A (product, day) one of whose lines has no readable
  amount is left unread, not filed at what the rest took.
- **Numbers are Decimal**: a text cell read digit for digit with the
  format's decimal mark - a space or « ' » only between groups of three
  digits (« 42 50 » is no 4 250, « 1 0,5 » no 10,5) -, no exponent, no NaN;
  an .xlsx numeric cell as the
  number it is (`Decimal` of its text), its binary noise (10.499999999999998)
  rounded half up to the cent - and refused when the excess is more than
  noise (3.505 is no amount).
- **Quantities are signed** (a refund is negative), summed exactly per
  (product, day), and a day ending on a fraction (0,5 + 0,5 is 1; 1,5
  alone) is rounded half away from zero - and counted, never truncated.
- **Payments**: each row one payment filed under the method the format maps
  its spelling to (left side matched accent- and case-blind), else the
  app's own words (« Carte », « Espèces »…), else the till's known spellings
  (`PosDailyPayment.canonical`) - and anything else kept as printed, and
  listed (`unmapped_methods`). A row with an amount and no method is filed
  under « Illisible » and counted.
- **Bounds**: a (product, day)'s revenue fits (10, 2), a (day, method)'s
  payments (12, 2), a name 255 characters (cut, not refused), at most
  `MAX_ROWS` rows and `MAX_PRODUCTS` distinct products a file, `MAX_COLUMN`
  columns. An .xlsx goes through the reader's bounds for an upload
  (xlsx_reader: no cell past XFD, no DOCTYPE, the zip's sizes).
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from common import search_key

from .laddition_xlsx import (
    CENTS,
    FIRST_DAY,
    KNOWN_RATES,
    LAST_DAY,
    MAX_DAY_QUANTITY,
    MAX_LINE_QUANTITY,
    MAX_PAYMENT,
    MAX_PRODUCTS,
    MAX_REVENUE,
    MAX_ROWS,
    NAME_LENGTH,
    ZERO,
    DayMoney,
    DayPayment,
    ParsedExport,
    _to_ht,
    with_progress,
)

# -- what a format may say -----------------------------------------------------------

SALES, PAYMENTS = "ventes", "paiements"
KINDS = {SALES: "Ventes par produit", PAYMENTS: "Encaissements"}
ENCODINGS = {
    "auto": "Automatique (UTF-8, sinon Windows-1252)",
    "utf-8": "UTF-8",
    "cp1252": "Windows-1252",
    "iso-8859-1": "ISO-8859-1",
    "utf-16": "UTF-16",
}
DELIMITERS = {";": "Point-virgule ( ; )", ",": "Virgule ( , )", "\t": "Tabulation", "|": "Barre verticale ( | )"}
DECIMAL_MARKS = {",": "Virgule (1 234,56)", ".": "Point (1,234.56)"}
#: Each date format: its pattern (a day and a month of one digit or two), and
#: the order of its three numbers (day, month, year).
DATE_FORMATS = {
    "dd/mm/yyyy": ("jj/mm/aaaa", r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})", "dmy"),
    "dd/mm/yy": ("jj/mm/aa", r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{2})", "dmy"),
    "dd-mm-yyyy": ("jj-mm-aaaa", r"([0-9]{1,2})-([0-9]{1,2})-([0-9]{4})", "dmy"),
    "dd.mm.yyyy": ("jj.mm.aaaa", r"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})", "dmy"),
    "yyyy-mm-dd": ("aaaa-mm-jj", r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})", "ymd"),
    "mm/dd/yyyy": ("mm/jj/aaaa", r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})", "mdy"),
}
#: A time after the date, or in a column of its own: « 23:41 », « 23:41:05 »,
#: « 23:41:05.123 ». ISO's « T » joins it to the date.
_TIME = r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:[.,][0-9]+)?)?"
TIME_RE = re.compile(_TIME)

#: The columns, by the field that names each - in the order a page lists them.
DAY, TIME, PRODUCT, QUANTITY, AMOUNT, AMOUNT_HT, RATE, CATEGORY, TYPOLOGY, METHOD, PAID = (
    "day_column",
    "time_column",
    "product_column",
    "quantity_column",
    "amount_column",
    "amount_ht_column",
    "rate_column",
    "category_column",
    "typology_column",
    "method_column",
    "paid_column",
)
COLUMN_FIELDS = (DAY, TIME, PRODUCT, QUANTITY, AMOUNT, AMOUNT_HT, RATE, CATEGORY, TYPOLOGY, METHOD, PAID)
#: What each column is called on a page - a refusal, « Tester »'s mapping.
ROLE_WORDS = {
    DAY: "jour",
    TIME: "heure",
    PRODUCT: "produit",
    QUANTITY: "quantité",
    AMOUNT: "montant TTC",
    AMOUNT_HT: "montant HT",
    RATE: "taux de TVA",
    CATEGORY: "catégorie",
    TYPOLOGY: "typologie",
    METHOD: "moyen de paiement",
    PAID: "montant payé",
}
#: Which columns each kind of file may name.
KIND_COLUMNS = {
    SALES: {DAY, TIME, PRODUCT, QUANTITY, AMOUNT, AMOUNT_HT, RATE, CATEGORY, TYPOLOGY},
    PAYMENTS: {DAY, TIME, METHOD, PAID},
}
REQUIRED = {SALES: (PRODUCT, QUANTITY), PAYMENTS: (METHOD, PAID)}

#: How far a format may name a column, by number or by title: a row is never
#: read past it (the .xlsx reader's `max_columns`, a CSV row cut).
MAX_COLUMN = 100
TITLE_LENGTH = 100
#: Where a header is looked for: a title row or two above it is common.
HEADER_ROWS = 30
#: What one file may hold - `MAX_ROWS` and `MAX_PRODUCTS`, shared with an
#: uploaded L'Addition export (laddition_xlsx): past them it is no till's
#: export of a period, or a period too long for one upload. Refused, in
#: French.
#: A CSV line wider than this is no export (a « ; » repeated millions of
#: times would be read into one row of millions of cells).
MAX_SEPARATORS = 5_000
MAX_SERVICE_HOUR = 11
#: The method map: « texte de la caisse = Carte », one per line.
MAX_MAP_LINES = 100
MAP_SIDE_LENGTH = 100

FILE_SUFFIXES = (".csv", ".txt", ".xlsx")

#: Binary noise on an .xlsx number: below this, the figure is the rounded one.
NOISE = Decimal("1E-9")
#: Decimals a figure may carry: an amount TTC or paid, an amount HT (a line's
#: HT is often printed to the tenth of a cent), a quantity (a weight), a VAT
#: rate stored as a number (0.055, or 5.5 as a percentage).
AMOUNT_PLACES, HT_PLACES, QUANTITY_PLACES, RATE_PLACES = 2, 4, 3, 4
#: The digits a number is written in: ASCII only (`str.isdigit` says yes to
#: digits of other scripts, which Decimal reads).
DIGITS = frozenset("0123456789")
#: Excel's serial 0: 30/12/1899 (its 1900 calendar counts a 29/02/1900 that
#: never was), or 01/01/1904 in the 1904 calendar.
EXCEL_EPOCH = datetime(1899, 12, 30)  # noqa: DTZ001 - a calendar's origin, no moment
EXCEL_EPOCH_1904 = datetime(1904, 1, 1)  # noqa: DTZ001 - a calendar's origin, no moment

XLS_REFUSED = "Les fichiers .xls (Excel 97-2003) ne se lisent pas : enregistrez-le en .xlsx ou en .csv."
SUFFIX_REFUSED = "Seuls les fichiers .csv, .txt et .xlsx sont acceptés."
NOT_A_CSV = "Ce fichier ne se lit pas comme un CSV : vérifiez le séparateur et l'encodage du format."
NOT_A_WORKBOOK = "Ce fichier n'est pas un classeur Excel (.xlsx) lisible."
NO_DAY = "Ce format n'a pas de colonne du jour : indiquez le jour des ventes avec le fichier."
DAY_GIVEN_TWICE = "Ce format lit le jour dans le fichier : laissez « Jour des ventes » vide."
NOTHING_READ = "Aucune ligne lue dans ce fichier : vérifiez les colonnes du format."
DAY_TO_COME = "Le jour des ventes est à venir : vérifiez-le."


class TillFileError(ValueError):
    """Why a till's file is not read - French, naming the row, never a path.
    A ValueError: the upload's page and the job's log say it as it is."""


class FormatError(ValueError):
    """A format that cannot read a file: `field` is the form field the
    sentence belongs to."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Column:
    """A column as a format names it: its number (from 0) or its title."""

    index: int | None = None
    title: str = ""

    @property
    def key(self) -> str:
        return _title_key(self.title)

    @property
    def said(self) -> str:
        return f"colonne {self.index + 1}" if self.index is not None else f"colonne « {self.title} »"


@dataclass(frozen=True)
class Layout:
    """A format, checked: what `read` reads a file with."""

    name: str
    kind: str
    encoding: str
    delimiter: str
    decimal_mark: str
    date_format: str
    end_hour: int
    sheet: str
    columns: dict
    unit_price: bool
    #: {spelling folded: PosDailyPayment method}.
    method_map: dict

    @property
    def titled(self) -> bool:
        return any(column.index is None for column in self.columns.values())

    def has(self, role: str) -> bool:
        return role in self.columns


def _title_key(text: str) -> str:
    """A header cell or a typed title as compared: accents, case and spaces aside."""
    return " ".join(search_key(str(text or "")).split())


def _column(fmt, role: str) -> Column | None:
    value = str(getattr(fmt, role, "") or "").strip()
    if not value:
        return None
    if value.isascii() and value.isdigit():
        if len(value) > 3 or not 1 <= int(value) <= MAX_COLUMN:
            raise FormatError(role, f"Un numéro de colonne de 1 à {MAX_COLUMN}, ou le titre de la colonne.")
        return Column(index=int(value) - 1)
    if len(value) > TITLE_LENGTH:
        raise FormatError(role, f"Un titre de colonne de {TITLE_LENGTH} caractères au plus.")
    if not _title_key(value):
        raise FormatError(role, "Titre de colonne illisible : tapez-le comme l'en-tête l'imprime.")
    return Column(title=value)


def method_words() -> dict[str, str]:
    """{the app's word, folded: the stored method} - « carte » is CB."""
    from recipes.models import PosDailyPayment

    return {search_key(PosDailyPayment.LABELS[method]): method for method in PosDailyPayment.ORDER}


def read_method_map(text) -> dict[str, str]:
    """« texte de la caisse = Carte », one per line, as {spelling folded:
    method}. FormatError naming the line for anything else."""
    from recipes.models import PosDailyPayment

    words = method_words()
    mapped: dict[str, str] = {}
    lines = [line.strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) > MAX_MAP_LINES:
        raise FormatError("method_map", f"{MAX_MAP_LINES} lignes au plus.")
    for line in lines:
        printed, equals, word = line.rpartition("=")
        printed, word = printed.strip(), word.strip()
        if not equals or not printed or not word:
            raise FormatError("method_map", f"« {line[:60]} » : écrivez « texte de la caisse = Carte ».")
        if len(printed) > MAP_SIDE_LENGTH or len(word) > MAP_SIDE_LENGTH:
            raise FormatError("method_map", f"« {line[:60]} » : {MAP_SIDE_LENGTH} caractères au plus de chaque côté.")
        method = words.get(_title_key(word)) or words.get(search_key(word))
        if method is None and PosDailyPayment.canonical(word) in PosDailyPayment.ORDER:
            method = PosDailyPayment.canonical(word)
        if method is None:
            known = ", ".join(PosDailyPayment.LABELS[method] for method in PosDailyPayment.ORDER)
            raise FormatError("method_map", f"« {line[:60]} » : après « = », l'un de {known}.")
        key = _title_key(printed)
        if key in mapped:
            raise FormatError("method_map", f"« {printed[:60]} » est indiqué deux fois.")
        mapped[key] = method
    return mapped


def check_format(fmt) -> Layout:
    """The `Layout` of `fmt`, or FormatError naming the field: a choice it
    does not offer, a column outside 1..MAX_COLUMN or a title too long, a
    column the kind of file does not read or that it needs and lacks, two
    roles on one column, an HT or rate column with no amount, a time with no
    day, an end of service past MAX_SERVICE_HOUR, a method map line that
    reads no method."""
    choices = (
        ("kind", KINDS, "Contenu inconnu."),
        ("encoding", ENCODINGS, "Encodage inconnu."),
        ("delimiter", DELIMITERS, "Séparateur inconnu."),
        ("decimal_mark", DECIMAL_MARKS, "Séparateur décimal inconnu."),
        ("date_format", DATE_FORMATS, "Format de date inconnu."),
    )
    for attribute, known, refusal in choices:
        if getattr(fmt, attribute, None) not in known:
            raise FormatError(attribute, refusal)
    kind = fmt.kind
    columns: dict[str, Column] = {}
    for role in COLUMN_FIELDS:
        column = _column(fmt, role)
        if column is None:
            continue
        if role not in KIND_COLUMNS[kind]:
            raise FormatError(role, f"Un fichier « {KINDS[kind]} » n'a pas cette colonne : laissez ce champ vide.")
        columns[role] = column
    for role in REQUIRED[kind]:
        if role not in columns:
            raise FormatError(role, "Indiquez cette colonne : son titre ou son numéro.")
    if TIME in columns and DAY not in columns:
        raise FormatError(TIME, "Une heure sans colonne du jour ne sert à rien : indiquez aussi la colonne du jour.")
    for role in (AMOUNT_HT, RATE):
        if role in columns and AMOUNT not in columns:
            raise FormatError(role, "Indiquez d'abord la colonne du montant TTC.")
    unit_price = bool(getattr(fmt, "amount_is_unit_price", False))
    if unit_price and AMOUNT not in columns:
        raise FormatError("amount_is_unit_price", "Un prix unitaire se lit dans la colonne du montant TTC.")
    by_place: dict = {}
    for role, column in columns.items():
        place = column.index if column.index is not None else column.key
        if place in by_place:
            raise FormatError(
                role,
                f"La {column.said} sert deux fois : pour {ROLE_WORDS[by_place[place]]} et pour {ROLE_WORDS[role]}.",
            )
        by_place[place] = role
    hour = getattr(fmt, "service_day_end_hour", 0)
    if hour in (None, ""):
        hour = 0
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= MAX_SERVICE_HOUR:
        raise FormatError("service_day_end_hour", f"Une heure de 0 à {MAX_SERVICE_HOUR}.")
    sheet = str(getattr(fmt, "sheet", "") or "").strip()
    if len(sheet) > TITLE_LENGTH:
        raise FormatError("sheet", f"{TITLE_LENGTH} caractères au plus.")
    method_map = read_method_map(getattr(fmt, "method_map", "")) if kind == PAYMENTS else {}
    return Layout(
        name=str(getattr(fmt, "name", "") or ""),
        kind=kind,
        encoding=fmt.encoding,
        delimiter=fmt.delimiter,
        decimal_mark=fmt.decimal_mark,
        date_format=fmt.date_format,
        end_hour=hour,
        sheet=sheet,
        columns=columns,
        unit_price=unit_price,
        method_map=method_map,
    )


# -- the file's rows -----------------------------------------------------------------


@dataclass
class Table:
    """A file's rows, each (its number as a person reads it, its cells)."""

    rows: object
    xlsx: bool = False
    date1904: bool = False


def suffix_of(file_name: str) -> str:
    name = str(file_name or "").lower()
    return name[name.rfind(".") :] if "." in name else ""


def check_suffix(file_name: str) -> None:
    suffix = suffix_of(file_name)
    if suffix == ".xls":
        raise TillFileError(XLS_REFUSED)
    if suffix not in FILE_SUFFIXES:
        raise TillFileError(SUFFIX_REFUSED)


def table(source, layout: Layout, file_name: str) -> Table:
    """The file's rows: a CSV decoded and split by the format, an .xlsx's
    sheet (the format's, else the first) read through the reader's bounds
    for an upload. `source` is a path or a seekable binary file object."""
    check_suffix(file_name)
    if suffix_of(file_name) == ".xlsx":
        return _workbook_table(source, layout)
    if hasattr(source, "read"):
        if hasattr(source, "seek"):
            source.seek(0)
        content = source.read()
    else:
        with open(source, "rb") as handle:
            content = handle.read()
    return Table(_csv_rows(content, layout))


def _workbook_table(source, layout: Layout) -> Table:
    import zipfile
    import zlib
    from xml.etree.ElementTree import ParseError

    from . import xlsx_reader

    unreadable = (zipfile.BadZipFile, zlib.error, EOFError, OSError, KeyError, ParseError)
    try:
        epoch_1904 = xlsx_reader.date1904(source, untrusted=True)
    except xlsx_reader.XlsxError as exc:
        raise TillFileError(str(exc)) from None
    except unreadable:
        raise TillFileError(NOT_A_WORKBOOK) from None

    def rows():
        try:
            yield from xlsx_reader.read_sheet(
                source,
                layout.sheet or None,
                max_columns=MAX_COLUMN,
                typed=True,
                untrusted=True,
                numbered=True,
            )
        except xlsx_reader.XlsxError as exc:
            raise TillFileError(str(exc)) from None
        except unreadable:
            raise TillFileError(NOT_A_WORKBOOK) from None

    return Table(rows(), xlsx=True, date1904=epoch_1904)


_UNICODE_MARKS = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def decode(content: bytes, encoding: str) -> str:
    """The text of the file: « auto » is UTF-16 behind its byte order mark,
    else UTF-8 (with or without one), else Windows-1252. A file opening on
    a Unicode mark is never read as Windows-1252 or ISO-8859-1, which
    decode anything: the mark would be read into the first cell."""
    if encoding == "auto":
        if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        else:
            try:
                return content.decode("utf-8-sig")
            except UnicodeDecodeError:
                if content.startswith(codecs.BOM_UTF8):
                    raise TillFileError(_not_in("utf-8")) from None
                encoding = "cp1252"
    elif encoding in ("cp1252", "iso-8859-1") and content.startswith(_UNICODE_MARKS):
        raise TillFileError(_not_in(encoding))
    try:
        return content.decode("utf-8-sig" if encoding == "utf-8" else encoding)
    except (UnicodeDecodeError, UnicodeError):
        raise TillFileError(_not_in(encoding)) from None


def _not_in(encoding: str) -> str:
    return (
        f"Ce fichier n'est pas en {ENCODINGS.get(encoding, encoding)} : changez l'encodage du format, "
        "ou exportez-le à nouveau."
    )


def _csv_rows(content: bytes, layout: Layout):
    text = decode(content, layout.encoding)
    if "\N{NULL}" in text:
        raise TillFileError(NOT_A_CSV)
    if any(line.count(layout.delimiter) > MAX_SEPARATORS for line in text.splitlines()):
        raise TillFileError(f"Une ligne de ce fichier a plus de {MAX_SEPARATORS} colonnes : ce n'est pas un export.")

    def rows():
        reader = csv.reader(io.StringIO(text), delimiter=layout.delimiter)
        try:
            for row in reader:
                if any(cell.strip() for cell in row):
                    yield reader.line_num, row[:MAX_COLUMN]
        except csv.Error:
            raise TillFileError(NOT_A_CSV) from None

    return rows()


# -- the cells -----------------------------------------------------------------------


def _is_number_cell(cell) -> bool:
    from .xlsx_reader import Number

    return isinstance(cell, Number)


def _is_moment_cell(cell) -> bool:
    from .xlsx_reader import Moment

    return isinstance(cell, Moment)


def _cell(row, index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def read_number(cell, decimal_mark: str, places: int) -> Decimal | None:
    """A cell's number, Decimal, or None when it holds no number at all.
    An .xlsx numeric cell is the number it is - its binary noise rounded
    half up to `places`, anything more refused (ValueError). A text cell is
    read digit for digit: ONE kind of separator - a space of any kind, « ' »
    or the other mark - and only between groups of exactly three digits
    (« 1 234,50 »; « 42 50 », « 1 0,5 » and « 1 2 3 » are no number), a
    sign in front or a « - » behind, a « € » at either end; at most `places`
    decimals (ValueError past them). Never an exponent, NaN or Infinity."""
    if _is_number_cell(cell):
        try:
            number = Decimal(str(cell).strip())
        except InvalidOperation:
            return None
        if not number.is_finite() or abs(number) > Decimal("1E15"):
            raise ValueError(f"nombre hors limites « {str(cell)[:40]} »")
        rounded = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if abs(number - rounded) >= NOISE:
            raise ValueError(f"plus de {places} décimales : « {_shown(number)} »")
        return rounded
    text = str(cell).strip().removesuffix("€").removeprefix("€").strip()
    if not text:
        return None
    sign = ""
    if text[:1] in ("-", "+", "\N{MINUS SIGN}"):
        sign, text = ("" if text[0] == "+" else "-"), text[1:].strip()
    elif text.endswith("-"):
        sign, text = "-", text[:-1].strip()
    if text.count(decimal_mark) > 1:
        return None
    whole, mark, fraction = text.partition(decimal_mark)
    whole = _ungrouped(whole, "." if decimal_mark == "," else ",")
    if whole is None:
        return None
    if (mark and not fraction) or not (whole or fraction):
        return None
    if not all(part.isascii() and part.isdigit() for part in (whole, fraction) if part):
        return None
    if len(fraction) > places:
        raise ValueError(f"plus de {places} décimales : « {str(cell).strip()[:40]} »")
    if len(whole) > 15:
        raise ValueError(f"nombre hors limites « {str(cell).strip()[:40]} »")
    return Decimal(f"{sign}{whole or '0'}.{fraction}" if mark else f"{sign}{whole}")


def _ungrouped(whole: str, thousands: str) -> str | None:
    """The digits of a number's whole part, its group separator taken out -
    None when anything but ONE separator (a space of any kind, « ' », the
    mark that is not the decimal one) stands between groups of exactly three
    digits. Taken out wherever it stood, « 42 50 » read 4 250 € and
    « 1 0,5 » 10,5 (« Combler les écarts » met the same bug)."""
    separators = {char for char in whole if char not in DIGITS}
    if not separators:
        return whole
    if len(separators) > 1:
        return None
    separator = separators.pop()
    if not (separator.isspace() or separator in ("'", thousands)):
        return None
    if re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(separator) + r"[0-9]{3})+", whole) is None:
        return None
    return whole.replace(separator, "")


def _shown(number: Decimal) -> str:
    return format(number.normalize(), "f").replace(".", ",")


def read_rate(cell) -> Decimal | None:
    """A VAT rate as a percentage or a fraction - « 20 % », « 20 », « 0,2 »,
    « 0.2 » are 0.20 - against the French rates; None for anything else
    (« 19,6 »). The sign is the amount's (a refund prints « -20 % »). An
    .xlsx numeric cell is the number it is, its binary noise rounded off
    (Excel stores 5,5 % as 0.055000000000000007)."""
    if _is_number_cell(cell):
        value = _noiseless_rate(cell)
        if value is None:
            return None
        return _known_rate(value, percentage=False)
    text = "".join(str(cell or "").split())
    percentage = "%" in text
    text = text.replace("%", "").replace(",", ".").lstrip("+-\N{MINUS SIGN}")
    if not text or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?|\.[0-9]+", text):
        return None
    return _known_rate(Decimal(text), percentage=percentage)


def _noiseless_rate(cell) -> Decimal | None:
    """An .xlsx numeric rate, its sign dropped, rounded to RATE_PLACES when
    what it loses is binary noise (under NOISE); None for anything else."""
    try:
        value = abs(Decimal(str(cell).strip()))
    except InvalidOperation:
        return None
    if not value.is_finite() or value > 100:
        return None
    rounded = value.quantize(Decimal(1).scaleb(-RATE_PLACES), rounding=ROUND_HALF_UP)
    return rounded if abs(value - rounded) < NOISE else None


def _known_rate(value: Decimal, *, percentage: bool) -> Decimal | None:
    """The French rate `value` is, as a fraction or (unless a « % » said
    which) a percentage."""
    # « 20 » and « 0,2 » never collide: no French rate is both a fraction
    # and a hundredth of another. A « % » printed says which it is.
    candidates = (value / 100,) if percentage else (value, value / 100)
    for candidate in candidates:
        for rate in KNOWN_RATES:
            if rate == candidate:
                return rate
    return None


def read_time(cell) -> tuple[int, int] | None:
    """(hour, minute) of a time cell: « 23:41 », « 23:41:05.123 », or an
    .xlsx time (a fraction of a day). None for a blank cell; ValueError for
    one that is no time."""
    if _is_number_cell(cell):
        try:
            fraction = Decimal(str(cell)) % 1
        except InvalidOperation:
            raise ValueError("heure illisible") from None
        minutes = int((fraction * 24 * 60).to_integral_value(rounding=ROUND_HALF_UP)) % (24 * 60)
        return divmod(minutes, 60)
    text = str(cell or "").strip()
    if not text:
        return None
    match = TIME_RE.fullmatch(text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError(f"heure illisible « {text[:40]} »")
    return int(match.group(1)), int(match.group(2))


def latest_day(today: date | None = None) -> date:
    """The last day a till's file may hold a sale on: tomorrow."""
    if today is None:
        from django.utils import timezone

        today = timezone.localdate()
    return today + timedelta(days=1)


class DayReader:
    """A format's dates, read: the date with its time when it carries one,
    an .xlsx serial in the workbook's calendar, the business day shifted
    back before the end of the service."""

    def __init__(self, layout: Layout, date1904: bool = False, today: date | None = None):
        self.layout = layout
        _label, pattern, self.order = DATE_FORMATS[layout.date_format]
        self.date_re = re.compile(rf"{pattern}(?:(?:[ T]|\s+){_TIME})?")
        self.epoch = EXCEL_EPOCH_1904 if date1904 else EXCEL_EPOCH
        #: The last business day a file may hold: tomorrow (a server's
        #: clock and a till's are not always on one day).
        self.latest = latest_day(today)

    def __call__(self, cell, time_cell="") -> date | None:
        """The business day of a row - None when the cell holds no date at
        all (blank, or no digit); ValueError when it holds digits that are
        no date of the format, or a day outside 2000-2099."""
        moment = self._moment(cell)
        if moment is None:
            return None
        day, time = moment
        if self.layout.has(TIME):
            time = read_time(time_cell) or time
        if not FIRST_DAY <= day <= LAST_DAY:
            raise ValueError(f"jour hors limites « {str(cell).strip()[:40]} » (de 2000 à 2099)")
        if self.layout.end_hour and time is not None and time[0] < self.layout.end_hour:
            day -= timedelta(days=1)
        if day > self.latest:
            raise ValueError(f"jour à venir « {str(cell).strip()[:40]} » (le {day:%d/%m/%Y})")
        return day

    def _moment(self, cell):
        if _is_moment_cell(cell):
            # A date cell of the workbook: ISO whatever the sheet shows.
            text = str(cell).strip()
            try:
                moment = datetime.fromisoformat(text.removesuffix("Z"))
            except ValueError:
                raise ValueError(f"jour illisible « {text[:40]} »") from None
            return moment.date(), (moment.hour, moment.minute)
        if _is_number_cell(cell):
            try:
                serial = Decimal(str(cell))
            except InvalidOperation:
                raise ValueError(f"jour illisible « {str(cell)[:40]} »") from None
            if not serial.is_finite() or not 0 < serial < 80000:
                raise ValueError(f"jour illisible « {str(cell)[:40]} »")
            moment = self.epoch + timedelta(days=float(serial))
            # Rounded to the minute: a serial's fraction is binary too. A
            # whole serial is a date with no time: never shifted.
            moment = (moment + timedelta(seconds=30)).replace(second=0, microsecond=0)
            return moment.date(), (None if serial == serial.to_integral_value() else (moment.hour, moment.minute))
        text = str(cell or "").strip()
        if not text or not any(char.isdigit() for char in text):
            return None
        match = self.date_re.fullmatch(text)
        if match is None:
            raise ValueError(f"jour illisible « {text[:40]} »")
        first, second, third = (int(part) for part in match.groups()[:3])
        if self.order == "dmy":
            day_of, month, year = first, second, third
        elif self.order == "mdy":
            month, day_of, year = first, second, third
        else:
            year, month, day_of = first, second, third
        if year < 100:
            year += 2000
        try:
            day = date(year, month, day_of)
        except ValueError:
            raise ValueError(f"jour illisible « {text[:40]} »") from None
        time = None
        if match.group(4) is not None:
            hour, minute = int(match.group(4)), int(match.group(5))
            if hour > 23 or minute > 59:
                raise ValueError(f"heure illisible « {text[:40]} »")
            time = (hour, minute)
        return day, time


# -- reading a file ------------------------------------------------------------------


@dataclass
class TillReading:
    """What `read` read: the export the writer stores, and how it was read
    - what « Tester » and the job's log say."""

    export: ParsedExport
    #: The row the titles were found on (its number as a person reads it).
    header_row: int | None = None
    #: {role: (column number from 1, its header's text or "")}.
    mapped: dict = field(default_factory=dict)
    #: Rows read as a sale or a payment; rows passed over, by why.
    rows_read: int = 0
    rows_above_header: int = 0
    rows_without_day: int = 0
    rows_without_product: int = 0
    rows_without_payment: int = 0
    #: `limit` rows were read and the rest was not (« Tester »).
    truncated: bool = False


def read(
    source,
    layout: Layout,
    *,
    file_name: str,
    day: date | None = None,
    limit: int | None = None,
    progress=None,
) -> TillReading:
    """Read a till's file with a checked format - `TillFileError` for every
    way it is not one the format reads. `day`: the day of every row, for a
    format with no day column (and refused for one that has it, or one to
    come). `limit`: stop after that many rows read (« Tester »). `progress`:
    called as the rows go by (a job's heartbeat, laddition_xlsx's
    `with_progress`)."""
    if layout.has(DAY) and day is not None:
        raise TillFileError(DAY_GIVEN_TWICE)
    if not layout.has(DAY) and day is None:
        raise TillFileError(NO_DAY)
    if day is not None and not FIRST_DAY <= day <= LAST_DAY:
        raise TillFileError("Jour des ventes hors limites (de 2000 à 2099).")
    if day is not None and day > latest_day():
        raise TillFileError(DAY_TO_COME)
    found = table(source, layout, file_name)
    reading = TillReading(ParsedExport())
    rows = with_progress(iter(found.rows), progress)
    try:
        indexes = _locate(rows, layout, reading)
        day_of = DayReader(layout, found.date1904)
        if layout.kind == SALES:
            _read_sales(rows, layout, indexes, day_of, day, reading, limit)
        else:
            _read_payments(rows, layout, indexes, day_of, day, reading, limit)
    except ArithmeticError:
        raise TillFileError("Ce fichier porte un nombre que le calcul ne peut pas lire.") from None
    finally:
        # A workbook's rows hold its zip open until they are closed - on
        # Windows a file still open cannot be deleted, and a refused upload
        # is deleted at once.
        _close(rows)
    if not reading.rows_read:
        raise TillFileError(NOTHING_READ)
    # What was passed over, as L'Addition's reader counts it: said in the log.
    reading.export.skipped = reading.rows_without_day + reading.rows_without_product + reading.rows_without_payment
    return reading


def _close(rows) -> None:
    close = getattr(rows, "close", None)
    if close is not None:
        close()


def _locate(rows, layout: Layout, reading: TillReading) -> dict[str, int]:
    """{role: column index}. Titles are looked for in the first HEADER_ROWS
    rows: the header is the first holding every one, the rows above it
    passed over (counted). A title found twice in the header, or two roles
    on one column, refuses the file."""
    indexes = {role: column.index for role, column in layout.columns.items() if column.index is not None}
    titled = {role: column for role, column in layout.columns.items() if column.index is None}
    if titled:
        for position, (number, row) in enumerate(rows):
            if position >= HEADER_ROWS:
                break
            keys = [_title_key(cell) for cell in row]
            if all(column.key in keys for column in titled.values()):
                reading.header_row = number
                for role, column in titled.items():
                    if keys.count(column.key) > 1:
                        raise TillFileError(
                            f"Ligne {number} : la colonne « {column.title} » apparaît deux fois dans l'en-tête."
                        )
                    indexes[role] = keys.index(column.key)
                reading.mapped = {role: (index + 1, str(_cell(row, index)).strip()) for role, index in indexes.items()}
                break
            reading.rows_above_header += 1
        if reading.header_row is None:
            missing = ", ".join(f"« {column.title} »" for column in titled.values())
            raise TillFileError(
                f"Les colonnes {missing} n'ont pas été trouvées dans les {HEADER_ROWS} premières lignes."
            )
    else:
        reading.mapped = {role: (index + 1, "") for role, index in indexes.items()}
    by_index: dict[int, str] = {}
    for role, index in indexes.items():
        if index in by_index:
            raise TillFileError(
                f"La colonne {index + 1} sert deux fois : pour {ROLE_WORDS[by_index[index]]} et pour {ROLE_WORDS[role]}."
            )
        by_index[index] = role
    return indexes


def _refused(number: int, said: str) -> TillFileError:
    return TillFileError(f"Ligne {number} : {said}. Fichier refusé.")


def _counted(reading: TillReading, limit: int | None) -> bool:
    """One more row read; True when « Tester » has read enough."""
    reading.rows_read += 1
    if reading.rows_read > MAX_ROWS:
        raise TillFileError(f"Plus de {MAX_ROWS} lignes : exportez une période plus courte.")
    if limit is not None and reading.rows_read >= limit:
        reading.truncated = True
        return True
    return False


def _row_day(row, number, indexes, day_of, given) -> date | None:
    """The row's business day - the day given with the upload, else its day
    cell's; None when that cell holds no date at all. A day cell holding
    digits that are no date refuses the file: the row is never dropped."""
    if given is not None:
        return given
    try:
        return day_of(_cell(row, indexes[DAY]), _cell(row, indexes.get(TIME)))
    except ValueError as said:
        raise _refused(number, str(said)) from None


def _read_sales(rows, layout, indexes, day_of, given, reading: TillReading, limit) -> None:
    export = reading.export
    export.money_columns = layout.has(AMOUNT)
    quantities: dict[tuple[str, date], Decimal] = {}
    order: list[tuple[str, date]] = []
    #: {(name, day): {rate, "ht" or None: amount}} - HT worked out per rate
    #: once the file is read, or summed from the HT column.
    buckets: dict[tuple[str, date], dict] = {}
    ht_sums: dict[tuple[str, date], Decimal] = {}
    incomplete: set[tuple[str, date]] = set()

    for number, row in rows:
        name = str(_cell(row, indexes[PRODUCT])).strip()[:NAME_LENGTH].strip()
        quantity_cell = _cell(row, indexes[QUANTITY])
        day = _row_day(row, number, indexes, day_of, given)
        if day is None:
            # A blank day cell, or one with no digit at all (a « Total »
            # footer, a format by numbers reading its own header): no day's
            # sale - unless the row reads as one, which is never dropped.
            cell = str(_cell(row, indexes[DAY])).strip()
            if cell and name and _sale_like(quantity_cell, layout):
                raise _refused(number, f"jour illisible « {cell[:40]} »")
            reading.rows_without_day += 1
            continue
        if not name:
            reading.rows_without_product += 1
            continue
        try:
            quantity = read_number(quantity_cell, layout.decimal_mark, QUANTITY_PLACES)
        except ValueError as said:
            raise _refused(number, f"quantité refusée, {said}") from None
        if quantity is None:
            raise _refused(number, f"quantité illisible « {str(quantity_cell).strip()[:40]} » pour « {name[:60]} »")
        if abs(quantity) > MAX_LINE_QUANTITY:
            raise _refused(number, f"quantité {_shown(quantity)} sur une ligne (au plus {MAX_LINE_QUANTITY})")
        key = (name, day)
        if key not in quantities:
            order.append(key)
            quantities[key] = ZERO
            if name not in export.products and len(export.products) >= MAX_PRODUCTS:
                raise TillFileError(f"Plus de {MAX_PRODUCTS} produits différents : ce n'est pas l'export d'une caisse.")
        quantities[key] += quantity
        if abs(quantities[key]) > MAX_DAY_QUANTITY:
            raise _refused(number, f"plus de {MAX_DAY_QUANTITY} unités de « {name[:60]} » en un jour")
        if quantity < 0:
            export.refund_lines += 1
        product = export.products.setdefault(
            name, {"quantity": 0, "category": "", "typology": "", "first": day, "last": day}
        )
        product["first"] = min(product["first"], day)
        product["last"] = max(product["last"], day)
        product["category"] = (
            product["category"] or str(_cell(row, indexes.get(CATEGORY))).strip()[:NAME_LENGTH].strip()
        )
        product["typology"] = (
            product["typology"] or str(_cell(row, indexes.get(TYPOLOGY))).strip()[:NAME_LENGTH].strip()
        )

        if layout.has(AMOUNT):
            _read_money(row, number, layout, indexes, key, quantity, buckets, ht_sums, incomplete, export)
        if _counted(reading, limit):
            break

    for key in order:
        total = quantities[key]
        whole = total.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if whole != total:
            export.quantities_rounded += 1
        export.entries.append((key[0], key[1], int(whole)))
    for info in export.products.values():
        info["quantity"] = 0
    for name, _day, quantity in export.entries:
        export.products[name]["quantity"] += quantity
    export.days_without_amount = len(incomplete)
    for key, per_rate in buckets.items():
        if key in incomplete:
            continue
        money = DayMoney()
        for rate, amount in per_rate.items():
            money.revenue_ttc += amount
            if rate == "ht":
                continue
            if rate is None:
                money.without_rate_ttc += amount
            else:
                money.revenue_ht += _to_ht(amount, rate)
        money.revenue_ht += ht_sums.get(key, ZERO).quantize(CENTS, rounding=ROUND_HALF_UP)
        money.revenue_ttc = money.revenue_ttc.quantize(CENTS, rounding=ROUND_HALF_UP)
        money.without_rate_ttc = money.without_rate_ttc.quantize(CENTS, rounding=ROUND_HALF_UP)
        for figure in (money.revenue_ttc, money.revenue_ht, money.without_rate_ttc):
            if abs(figure) > MAX_REVENUE:
                raise TillFileError(
                    f"« {key[0]} » le {key[1]:%d/%m/%Y} : une recette de plus de 99 999 999,99 € en un jour. "
                    "Fichier refusé."
                )
        export.money[key] = money
    export.rows_read = reading.rows_read


def _sale_like(quantity_cell, layout: Layout) -> bool:
    """Whether a row with no day would be a sale: its quantity reads."""
    try:
        return read_number(quantity_cell, layout.decimal_mark, QUANTITY_PLACES) is not None
    except ValueError:
        return True


def _read_money(row, number, layout, indexes, key, quantity, buckets, ht_sums, incomplete, export) -> None:
    cell = _cell(row, indexes[AMOUNT])
    try:
        amount = read_number(cell, layout.decimal_mark, AMOUNT_PLACES)
    except ValueError as said:
        raise _refused(number, f"montant refusé, {said}") from None
    if amount is None:
        # Nothing to record and nothing to invent: the (product, day) is now
        # short of an unknown figure, so it is left unread - said.
        export.lines_without_amount += 1
        incomplete.add(key)
        return
    if abs(amount) > MAX_REVENUE:
        raise _refused(number, f"montant {_shown(amount)} € plus large que sa colonne")
    if layout.unit_price:
        amount = amount * quantity
    per_rate = buckets.setdefault(key, {})
    if layout.has(AMOUNT_HT):
        try:
            ht = read_number(_cell(row, indexes[AMOUNT_HT]), layout.decimal_mark, HT_PLACES)
        except ValueError as said:
            raise _refused(number, f"montant HT refusé, {said}") from None
        if ht is not None:
            if abs(ht) > MAX_REVENUE:
                raise _refused(number, f"montant HT {_shown(ht)} € plus large que sa colonne")
            if layout.unit_price:
                ht = ht * quantity
            per_rate["ht"] = per_rate.get("ht", ZERO) + amount
            ht_sums[key] = ht_sums.get(key, ZERO) + ht
            return
    rate = read_rate(_cell(row, indexes[RATE])) if layout.has(RATE) else None
    if rate is None:
        export.lines_without_rate += 1
    per_rate[rate] = per_rate.get(rate, ZERO) + amount


def map_method(printed: str, layout: Layout) -> tuple[str, bool]:
    """(the method a printed spelling is filed under, whether anything knew
    it): the format's map, else the app's own words, else the till's known
    spellings - else as printed, unknown."""
    from recipes.models import PosDailyPayment

    key = _title_key(printed)
    if key in layout.method_map:
        return layout.method_map[key], True
    words = method_words()
    if key in words:
        return words[key], True
    canonical = PosDailyPayment.canonical(printed)
    return canonical, canonical in PosDailyPayment.ORDER


def _read_payments(rows, layout, indexes, day_of, given, reading: TillReading, limit) -> None:
    from recipes.models import PosDailyPayment

    export = reading.export
    export.payments_read = True
    export.sales_read = False
    unmapped: dict[str, str] = {}
    for number, row in rows:
        printed = str(_cell(row, indexes[METHOD])).strip()
        paid_cell = _cell(row, indexes[PAID])
        day = _row_day(row, number, indexes, day_of, given)
        try:
            amount = read_number(paid_cell, layout.decimal_mark, AMOUNT_PLACES)
        except ValueError as said:
            raise _refused(number, f"montant payé refusé, {said}") from None
        if day is None:
            cell = str(_cell(row, indexes.get(DAY))).strip()
            if cell and printed and amount is not None:
                raise _refused(number, f"jour illisible « {cell[:40]} »")
            reading.rows_without_day += 1
            continue
        if amount is None:
            if not printed and not str(paid_cell).strip():
                reading.rows_without_payment += 1
                continue
            raise _refused(number, f"montant payé illisible « {str(paid_cell).strip()[:40]} »")
        if abs(amount) > MAX_PAYMENT:
            raise _refused(number, f"montant payé {_shown(amount)} € plus large que sa colonne")
        if printed:
            method, known = map_method(printed, layout)
            if not known:
                unmapped.setdefault(_title_key(printed), printed[:40])
        else:
            # Paid, by nobody knows what: filed so the day adds up, and said.
            method = PosDailyPayment.UNREAD
            export.unread_payment_tickets += 1
        export.payment_days.add(day)
        payment = export.payments.setdefault((day, method), DayPayment())
        payment.amount += amount
        payment.count += 1
        if abs(payment.amount) > MAX_PAYMENT:
            raise _refused(number, f"paiements du {day:%d/%m/%Y} plus larges que leur colonne")
        export.tickets += 1
        if _counted(reading, limit):
            break
    export.unmapped_methods = sorted(unmapped.values(), key=search_key)
    export.rows_read = reading.rows_read


def read_file(source, fmt, *, file_name: str, day: date | None = None) -> ParsedExport:
    """A till's file read with a format (a TillFormat, or its `Layout`)."""
    layout = fmt if isinstance(fmt, Layout) else check_format(fmt)
    return read(source, layout, file_name=file_name, day=day).export


def preview(source, layout: Layout, file_name: str, rows: int) -> list[tuple[int, list[str]]]:
    """The file's first `rows` rows as the reader splits them - what
    « Tester » numbers the columns of."""
    found = table(source, layout, file_name)
    shown = []
    try:
        for number, row in found.rows:
            shown.append((number, [str(cell) for cell in row]))
            if len(shown) >= rows:
                break
    finally:
        _close(found.rows)
    return shown
