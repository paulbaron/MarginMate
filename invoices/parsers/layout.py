"""The columns of a document's table, from where its text sits on the page.

The generic reader (`generic_receipt.py`) reads a line for what its numbers
do, and it reads every line as one string of text. That is right for a till
receipt, which prints a name and an amount and not much more - and wrong for
an invoice, which prints a TABLE: a header naming its columns, then rows
whose cells sit under those names. Read as text, a row "1  704411  Câble
HDMI 2 m  1  7,99  7,99  7,99" is a product called "1 704411 Câble HDMI 2 m";
a row "AR0142  Sac de glace  2  12.40  5.5%  1.36  24.80" satisfies the
VAT identity (24,80 x 5,5 % = 1,36) and passes for the VAT table; and the
footer's "Capital : 8 000,00 Euros" is a product at 8 000 €.

Pure geometry and arithmetic, no Django, no database: `find_table` takes
the page's rows of cells (each cell a text with its x0 and x1, as
`ocr.OcrCell` carries them for a photo's boxes and for a PDF's words) and
says which rows are the table's header, which are its items, which lines
are a description wrapped onto the next line, and which columns hold what.

Three things decide, in this order, and the last one wins:

- **alignment**: cells that overlap horizontally across rows are one column
  (`SAME_COLUMN_OVERLAP`, the rule `ocr.group_boxes_into_lines` already
  applies to the boxes of one photo). Right-aligned amounts, left-aligned
  ones and centred ones all overlap their own column; nothing here reads an
  edge position;
- **vocabulary**: a row printing the generic words an invoice heads its
  columns with - désignation, référence, quantité, prix unitaire, montant,
  TVA, remise… in French or in English - is the header, and it may be
  printed over two or three lines ("Taux de / TVA" above and below each
  other). The words PROPOSE a role for each column;
- **arithmetic** CONFIRMS it: the quantity column is the one whose value
  times the unit price makes the amount, on the rows that print all three;
  an amount is tax-exclusive when its rate makes the row's own VAT amount,
  or when the row's two unit prices are one rate apart - and once one row
  proves the amount column HT, its other rows are HT too. A header is not
  needed: two rows whose columns multiply out the same way are a table too.
  Under a header, the amount column has to be one the header calls an
  amount, or the one a quantity times a unit price makes, or the only column
  of money - and it has to HOLD the amount of the rows it is for: a column
  that misses more priced rows than it holds is not the column, and the page
  is no table. A PDF printing each line as one string of text puts its
  figures wherever the words before them end, and nothing aligns.

What it never does: it never names a column from its position (the third
column is not the quantity), never takes the rightmost figures for the
amount because nothing else was found, never keys on one supplier's words,
and never decides what the purchase is - the reader's arithmetic anchors
(the printed total, the VAT table) go on doing that over the readings this
hands it. A page where no table is found returns None, and the reader reads
its text exactly as before.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import pairwise

from .receipt_base import CENTS, GROUPING_RE, HALF_CENT, KNOWN_VAT_RATES, MONEY_RE, UNITS

ZERO = Decimal("0")
# Two cells share a column when their spans overlap by this share of the
# narrower one - the same rule the OCR grouping applies to boxes on one line.
SAME_COLUMN_OVERLAP = 0.3
# A description wrapped onto the next line sits this close under its row,
# in median gaps between the page's consecutive lines; a brand line or a
# footnote printed further down is not part of the name.
CONTINUATION_GAP = 1.5
# A header may be printed over this many consecutive lines.
MAX_HEADER_LINES = 3
# How far the quantity times the unit price may miss the amount: a unit price
# is printed rounded to the cent, so the miss grows with the count.
ROW_TOLERANCE = CENTS

# Column roles. The values are the French words the review screen prints.
DESCRIPTION = "désignation"
REFERENCE = "référence"
EAN = "EAN"
LINE_NUMBER = "n° de ligne"
QUANTITY = "quantité"
UNIT_PRICE = "prix unitaire"
PRICE = "prix"  # "Prix" alone: a unit price or the amount, arithmetic says which
AMOUNT = "montant"
VAT_AMOUNT = "montant TVA"
RATE = "taux de TVA"
DISCOUNT = "remise"
CODE = "code TVA"
OTHER = "autre"

# The generic words a French or English invoice heads its columns with, once
# folded (no accents, no case, punctuation as spaces). Longest phrases first,
# so "prix unitaire" is one role and not "prix" then "unitaire".
_ROLE_PHRASES = [
    ("prix unitaire net", UNIT_PRICE),
    ("prix unitaire", UNIT_PRICE),
    ("prix unit", UNIT_PRICE),
    ("unit price", UNIT_PRICE),
    ("prix u", UNIT_PRICE),
    ("p u", UNIT_PRICE),
    ("pu", UNIT_PRICE),
    ("tarif", UNIT_PRICE),
    ("unit cost", UNIT_PRICE),
    # "Px U. HT", "Px unit.": « prix » as an invoice shortens it. Unknown,
    # a header naming its prices so named no price at all, and headed no
    # table - a table of one row, which alignment alone cannot find, was
    # then read as text.
    ("px unit", UNIT_PRICE),
    ("px u", UNIT_PRICE),
    # Not "pu ht" / "pu ttc" as phrases: "HT" and "TTC" are flavours, and
    # a phrase swallowing one lost the qualifier the review screen prints.
    ("sous total", AMOUNT),
    ("subtotal", AMOUNT),
    ("montant", AMOUNT),
    ("total", AMOUNT),
    ("amount", AMOUNT),
    ("mt", AMOUNT),
    ("valeur", AMOUNT),
    ("value", AMOUNT),
    ("prix", PRICE),
    ("px", PRICE),
    ("price", PRICE),
    ("quantite", QUANTITY),
    ("quantity", QUANTITY),
    ("qte", QUANTITY),
    ("qty", QUANTITY),
    ("quant", QUANTITY),
    ("nombre", QUANTITY),
    ("nb", QUANTITY),
    ("colis", QUANTITY),
    ("designation", DESCRIPTION),
    ("description", DESCRIPTION),
    ("libelle", DESCRIPTION),
    ("articles", DESCRIPTION),
    ("article", DESCRIPTION),
    ("produits", DESCRIPTION),
    ("produit", DESCRIPTION),
    ("items", DESCRIPTION),
    ("item", DESCRIPTION),
    ("denomination", DESCRIPTION),
    ("intitule", DESCRIPTION),
    ("prestation", DESCRIPTION),
    ("detail", DESCRIPTION),
    ("details", DESCRIPTION),
    ("nom", DESCRIPTION),
    ("code barre", EAN),
    ("codebarre", EAN),
    ("ean", EAN),
    ("ean13", EAN),
    ("gencod", EAN),
    ("barcode", EAN),
    ("gtin", EAN),
    ("code tva", CODE),
    ("reference", REFERENCE),
    ("ref", REFERENCE),
    ("refce", REFERENCE),
    ("code", REFERENCE),
    ("sku", REFERENCE),
    ("numero", REFERENCE),
    ("num", REFERENCE),
    ("no", REFERENCE),
    ("n", REFERENCE),
    ("art", REFERENCE),
    ("taux", RATE),
    ("tva", RATE),
    ("vat", RATE),
    ("tax", RATE),
    ("taxe", RATE),
    ("tx", RATE),
    ("remise", DISCOUNT),
    ("discount", DISCOUNT),
    ("rabais", DISCOUNT),
    ("reduction", DISCOUNT),
    ("ristourne", DISCOUNT),
    ("promo", DISCOUNT),
    # Columns an invoice has and this reader has no use for.
    ("statut", OTHER),
    ("status", OTHER),
    ("image", OTHER),
    ("photo", OTHER),
    ("date", OTHER),
    ("dispo", OTHER),
    ("disponibilite", OTHER),
    ("conditionnement", OTHER),
    ("cond", OTHER),
    ("colisage", OTHER),
    ("poids", OTHER),
    ("volume", OTHER),
    ("unite", OTHER),
    ("unit", OTHER),
    ("u", OTHER),
    ("un", OTHER),
    ("livre", OTHER),
    ("commande", OTHER),
]
_ROLE_PHRASES.sort(key=lambda entry: -len(entry[0].split()))
_ROLE_BY_PHRASE = dict(_ROLE_PHRASES)
_LONGEST_PHRASE = max(len(phrase.split()) for phrase, _role in _ROLE_PHRASES)
# The words that name a NAME. "Produit | Désignation" heads a code and a
# name: where several header cells name the description, the one made of
# these words is it, and the others head the reference.
_NAME_WORDS = {"designation", "description", "libelle", "intitule", "denomination"}
# Words that qualify a role without naming one: "Montant HT", "Prix unitaire
# net", "Taux de TVA". "unitaire" alone turns a "Prix" into a unit price.
_FLAVOURS = {"ht": "HT", "h t": "HT", "hors taxe": "HT", "hors taxes": "HT", "ttc": "TTC", "t t c": "TTC"}
_MODIFIERS = {
    "ht",
    "ttc",
    "h",
    "t",
    "c",
    "hors",
    "taxe",
    "taxes",
    "net",
    "nets",
    "brut",
    "bruts",
    "de",
    "du",
    "des",
    "en",
    "le",
    "la",
    "les",
    "d",
    "l",
    "eur",
    "euro",
    "euros",
    "unitaire",
    "unitaires",
    "par",
    "a",
    "excl",
    "incl",
    "of",
    "per",
    "the",
    "and",
    "et",
    "ou",
    "or",
    "s",
    "es",
    "ligne",
    "line",
}
# What the totals under a table are called: a cell made of these words, one
# of them a marker ("total", "à payer"...), beside an amount, ends the table.
# Not "TVA" alone: an item row may print its rate as "TVA 20 %".
_TOTAL_WORDS = {
    "total",
    "totaux",
    "sous",
    "sous-total",
    "subtotal",
    "net",
    "a",
    "payer",
    "montant",
    "tva",
    "vat",
    "ht",
    "ttc",
    "taxe",
    "taxes",
    "tax",
    "base",
    "brut",
    "remise",
    "general",
    "generale",
    "du",
    "de",
    "la",
    "le",
    "amount",
    "due",
    "balance",
    "reste",
    "regler",
    "facture",
    "commande",
    "hors",
    "solde",
}
_TOTAL_MARKERS = {
    "total",
    "totaux",
    "sous",
    "sous-total",
    "subtotal",
    "payer",
    "due",
    "balance",
    "reste",
    "net",
    "solde",
}
# Glyphs of an icon font (private use), and control characters: never text.
_UNPRINTABLE_RE = re.compile(r"[-\x00-\x1f\x7f-\x9f]")

_NUMBER_RE = re.compile(rf"^\s*(?P<sign>-\s?)?(?P<units>{UNITS})(?:[.,](?P<decimals>\d{{1,4}}))?\s*$")
_RATE_CELL_RE = re.compile(r"^\s*(?P<pct>\d{1,3}(?:[.,]\d{1,2})?)\s*%\s*$")
_DIGITS_RE = re.compile(r"^\s*\d{5,14}\s*$")
# A short code beside an amount ("T1 0.49", "0,80 A", "36,00€ 1" - a VAT
# code's digit run into the total's cell): the amount is the cell. A digit
# IN FRONT of an amount is a count, never a code.
_SIDE_CODE_RE = re.compile(r"^(?:[A-Za-z]{1,2}\d?|\(\d\))\s+|\s+(?:[A-Za-z]{1,2}\d?|\(\d\)|\d)$")
_CURRENCY_RE = re.compile(r"(?i)€|\beur(?:os?)?\b|\$|£")
_LETTERS_RE = re.compile(r"[A-Za-zÀ-ÿ]")
# The whole numbers of three digits at most that end a name, as the text
# reading cuts them ("BASILIC FRAIS 1", "DADDY 71").
_TRAILING_INTEGER_RE = re.compile(r"(?:\s+\d{1,3})+\s*$")
# "QuantitéPU HT", "Net HTPrix Unitaire": two header words glued by the
# extraction, told apart where the case changes.
_GLUE_RE = re.compile(r"(?<=[a-zà-ÿ])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-zà-ÿ])")
_NUMBER_SIGN_RE = re.compile(r"(?i)\bn\s?[°º]")
# A description wrapped onto the next line sits closer under its row than
# the next row does: at most this many times the widest gap between two
# consecutive item rows (a unit price printed to the cent, a line's height
# rounded by the extraction).
ROW_PITCH_SLACK = 1.25


@dataclass(frozen=True)
class Cell:
    """One piece of text and where it sits, left to right."""

    text: str
    x0: float
    x1: float


@dataclass
class Row:
    """One printed line: its cells left to right, its height on the page
    when known (None for a hand-written fixture that does not care), and
    whether the line is exact - a PDF's own words - rather than a
    recogniser's grouping of the boxes it found on a photograph."""

    cells: list[Cell] = field(default_factory=list)
    y: float | None = None
    exact: bool = True

    @property
    def text(self) -> str:
        # Two spaces between cells, as OcrLine.text prints them.
        return "  ".join(cell.text for cell in self.cells)


def rows_from_ocr(page) -> list[Row]:
    """The rows of an `ocr.OcrPage` (or anything shaped like one). A row is
    exact when none of its cells carries a recogniser's doubt: a PDF's own
    words come with a confidence of 1 (`ocr.text_layer_pages`), and an OCR
    engine never vouches for a whole line to the last digit."""
    return [
        Row(
            cells=[Cell(text=cell.text, x0=float(cell.x0), x1=float(cell.x1)) for cell in line.cells],
            y=line.y,
            exact=all(float(cell.confidence) >= 1.0 for cell in line.cells),
        )
        for line in page.lines
    ]


def text_of(rows: list[Row]) -> str:
    """The page text the reader sees for these rows, one line a row."""
    return "\n".join(row.text for row in rows)


@dataclass
class Column:
    x0: float
    x1: float
    role: str = OTHER
    flavour: str = ""  # "HT", "TTC" or ""
    header: str = ""  # the header's own words, for the review screen
    confirmed: bool = False  # the role was confirmed by arithmetic
    # Whether a row read as an item fills it: a footer's figures aligned
    # with nothing form columns too, and the screen must not list them.
    used: bool = True

    def overlaps(self, x0: float, x1: float) -> bool:
        return _overlap(self.x0, self.x1, x0, x1)

    @property
    def label(self) -> str:
        if self.role in (UNIT_PRICE, AMOUNT, PRICE) and self.flavour:
            return f"{self.role} {self.flavour}"
        return self.role


@dataclass
class ItemRow:
    """One row of the table read through its columns."""

    index: int
    name: str
    quantity: int | Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    rate: Decimal | None = None
    # Set when the row itself proves which of HT and TTC its amount is.
    ht: Decimal | None = None
    ttc: Decimal | None = None
    ean: str = ""
    # Whether `ttc` was read off the row (a TTC column, an amount proven
    # TTC) rather than worked out from its HT and rate.
    ttc_printed: bool = False
    # Whether the table has a quantity column at all: without one, a count
    # printed inside the name ("CHAINE D4 X4") is still the text reading's.
    counted: bool = False
    continuation: list[int] = field(default_factory=list)


@dataclass
class Table:
    columns: list[Column]
    header_rows: list[int]
    items: dict[int, ItemRow]
    # Rows inside the table that are not items: the header, a wrapped
    # description, a detail line between two rows.
    inside: set[int]
    # Rows above the header: the document's own head, never an item. Empty
    # for a table found without a header.
    before: set[int]
    # Where the rows under the header stop (the first total): the table's
    # span runs from its header to there. None without a header.
    body_end: int | None = None

    @property
    def has_header(self) -> bool:
        return bool(self.header_rows)

    def describe(self) -> str:
        """French, for the review screen: the columns recognised and how. A
        column no item row fills (a footer's figures aligned with nothing)
        is left out; a role two columns share is counted, not repeated."""
        labels = [
            column.label
            for column in sorted(self.columns, key=lambda column: column.x0)
            if column.role != OTHER and column.used
        ]
        named = []
        for label in labels:
            if label not in named:
                named.append(label)
        parts = [
            "colonnes : "
            + ", ".join(
                f"{label} ({labels.count(label)} colonnes)" if labels.count(label) > 1 else label for label in named
            )
        ]
        if self.header_rows:
            parts.append(f"en-tête sur {len(self.header_rows)} ligne{'s' if len(self.header_rows) > 1 else ''}")
        else:
            parts.append("sans en-tête, colonnes reconnues à leur alignement et à leur arithmétique")
        parts.append(f"{len(self.items)} ligne{'s' if len(self.items) != 1 else ''} d'article")
        wrapped = sum(len(item.continuation) for item in self.items.values())
        if wrapped:
            parts.append(f"{wrapped} désignation{'s' if wrapped > 1 else ''} sur deux lignes")
        if self.before:
            parts.append(
                f"{len(self.before)} ligne{'s' if len(self.before) > 1 else ''} avant le tableau écartée{'s' if len(self.before) > 1 else ''}"
            )
        return " ; ".join(parts)


# -- cells -----------------------------------------------------------------


@dataclass
class _Value:
    kind: str  # "money", "integer", "rate", "digits", "code", "text"
    value: Decimal | int | str | None = None
    code: str = ""


def cell_value(text: str) -> _Value:
    """What a cell holds: an amount (with an optional code beside it), a small
    integer, a rate, a long digit run, an alphanumeric code, or text."""
    stripped = text.strip()
    if not stripped:
        return _Value("text")
    rate = _RATE_CELL_RE.match(stripped)
    if rate:
        return _Value("rate", Decimal(rate.group("pct").replace(",", ".")) / Decimal("100"))
    if _DIGITS_RE.match(stripped):
        return _Value("digits", stripped)
    currency = _CURRENCY_RE.search(stripped) is not None
    plain = _CURRENCY_RE.sub(" ", stripped).strip()
    code = ""
    side = _SIDE_CODE_RE.search(plain)
    if side:
        code = side.group().strip()
        plain = (plain[: side.start()] + plain[side.end() :]).strip()
    number = _NUMBER_RE.match(plain)
    if number:
        units = GROUPING_RE.sub("", number.group("units"))
        decimals = number.group("decimals")
        if decimals is None:
            if len(units) > 4:
                return _Value("digits", units)
            if currency:
                return _Value("money", Decimal(units), code)
            # "2025 (1)", "12 A", "1 2": a whole number beside a code is a
            # year with its footnote, a count, a size - money prints its
            # cents or its sign. Read as 2 025 EUR, a footnoted year under a
            # row made a column that cut the name above it short.
            return _Value("integer", int(units), code)
        try:
            value = Decimal(f"{'-' if number.group('sign') else ''}{units}.{decimals}")
        except InvalidOperation:
            return _Value("text")
        return _Value("money", value, code)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-/._]{2,19}", stripped) and sum(c.isdigit() for c in stripped) >= 3:
        return _Value("code", stripped)
    return _Value("text")


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    folded = folded.replace("°", "o").replace("º", "o")
    return re.sub(r"[^a-z0-9%]+", " ", folded).strip()


def _header_words(text: str) -> list[str]:
    """The folded words of a header cell, glued words split ("QuantitéPU HT")."""
    unglued = " ".join(_GLUE_RE.split(text))
    # "N°", "Nº", "#": the number, one word ("no"). A degree sign anywhere
    # else is dropped by the fold.
    unglued = _NUMBER_SIGN_RE.sub(" no ", unglued).replace("#", " no ")
    return _fold(unglued).split()


def _roles_of(words: list[str]) -> list[tuple[str, str]]:
    """The (role, flavour) segments a header's words name, in order: "montant
    ht" is one AMOUNT in HT; "quantite pu ht" is a QUANTITY then a UNIT_PRICE;
    "montant tva" is a VAT_AMOUNT; "taux de tva" one RATE. Nothing when more
    words name no column than segments were found."""
    result, unknown = _roles_and_unknown(words)
    if unknown > len(result):
        return []
    return result


def _roles_and_unknown(words: list[str]) -> tuple[list[tuple[str, str]], int]:
    """The segments `_roles_of` finds, and how many words named nothing."""
    segments: list[list[str]] = []  # roles per segment
    flavours: list[str] = []
    position = 0
    unknown = 0
    while position < len(words):
        matched = None
        for length in range(min(_LONGEST_PHRASE, len(words) - position), 0, -1):
            phrase = " ".join(words[position : position + length])
            if phrase in _ROLE_BY_PHRASE:
                matched = (length, _ROLE_BY_PHRASE[phrase])
                break
            if phrase in _FLAVOURS:
                matched = (length, "flavour:" + _FLAVOURS[phrase])
                break
        if matched is None:
            word = words[position]
            if word not in _MODIFIERS and word != "%":
                unknown += 1
            elif word in ("unitaire", "unitaires") and segments and segments[-1][-1] == PRICE:
                segments[-1][-1] = UNIT_PRICE
            position += 1
            continue
        length, role = matched
        position += length
        if role.startswith("flavour:"):
            if segments:
                flavours[-1] = role.split(":", 1)[1]
            continue
        if role == OTHER:
            if not segments or segments[-1] != [OTHER]:
                segments.append([OTHER])
                flavours.append("")
            continue
        # "Prix unitaire net HT" then "Prix unitaire net TTC", glued into one
        # cell: the same role again, once a flavour has been given, is a
        # second column.
        repeated_after_flavour = (
            bool(segments) and role in segments[-1] and bool(flavours[-1]) and role in (UNIT_PRICE, AMOUNT, PRICE)
        )
        if segments and _combines(segments[-1][-1], role) and not repeated_after_flavour:
            segments[-1].append(role)
        else:
            segments.append([role])
            flavours.append("")
    result = []
    for roles, flavour in zip(segments, flavours):
        result.append((_settle(roles), flavour))
    return result, unknown


def _combines(previous: str, role: str) -> bool:
    if previous == role:
        return True
    pair = {previous, role}
    return pair in (
        {AMOUNT, RATE},
        {PRICE, AMOUNT},
        {RATE, CODE},
        {REFERENCE, DESCRIPTION},
        {PRICE, UNIT_PRICE},
        {AMOUNT, UNIT_PRICE},
        {QUANTITY, OTHER},
        {REFERENCE, EAN},
    )


def _settle(roles: list[str]) -> str:
    kinds = set(roles)
    if kinds == {AMOUNT, RATE} or kinds == {AMOUNT, RATE, PRICE}:
        return VAT_AMOUNT
    if RATE in kinds and CODE in kinds:
        return CODE
    if REFERENCE in kinds and DESCRIPTION in kinds:
        return DESCRIPTION
    if REFERENCE in kinds and EAN in kinds:
        return EAN
    if UNIT_PRICE in kinds:
        return UNIT_PRICE
    if AMOUNT in kinds:
        return AMOUNT
    if QUANTITY in kinds:
        return QUANTITY
    return roles[0]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    shared = min(a1, b1) - max(a0, b0)
    narrower = min(a1 - a0, b1 - b0)
    if narrower <= 0:
        return shared >= 0 and (a0 <= b0 <= a1 or b0 <= a0 <= b1)
    return shared > SAME_COLUMN_OVERLAP * narrower


def _cluster(spans: list[tuple[int, float, float]]) -> list[list[int]]:
    """Groups of the given (key, x0, x1) spans, transitively, by overlap -
    the connected components of the overlap graph, swept from left to right.
    A span can only overlap a group still open at its left edge, and a group
    whose right edge it has passed is closed for good (every later span
    starts further right still). The same groups as testing every pair
    (`_cluster_pairwise`, the reference the tests compare against) in time
    proportional to the spans times the columns rather than the spans
    squared: two hundred rows of seven figures made a million pair tests."""
    order = sorted(range(len(spans)), key=lambda i: (min(spans[i][1], spans[i][2]), i))
    open_groups: list[dict] = []  # {"members": [span positions], "x1": right edge}
    closed: list[list[int]] = []
    for i in order:
        _key, a0, a1 = spans[i]
        kept: list[dict] = []
        merged: dict | None = None
        for group in open_groups:
            if group["x1"] < min(a0, a1):
                closed.append(group["members"])
            elif any(_overlap(spans[j][1], spans[j][2], a0, a1) for j in group["members"]):
                if merged is None:
                    merged = group
                    kept.append(group)
                else:
                    merged["members"].extend(group["members"])
                    merged["x1"] = max(merged["x1"], group["x1"])
            else:
                kept.append(group)
        if merged is None:
            merged = {"members": [], "x1": max(a0, a1)}
            kept.append(merged)
        merged["members"].append(i)
        merged["x1"] = max(merged["x1"], a0, a1)
        open_groups = kept
    closed += [group["members"] for group in open_groups]
    return [[spans[i][0] for i in members] for members in closed]


def _cluster_pairwise(spans: list[tuple[int, float, float]]) -> list[list[int]]:
    """The groups by every pair tested: the reference `_cluster` is held to."""
    parent = list(range(len(spans)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if _overlap(spans[i][1], spans[i][2], spans[j][1], spans[j][2]):
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(spans)):
        groups.setdefault(find(i), []).append(spans[i][0])
    return list(groups.values())


# -- the header --------------------------------------------------------------


def _header_cells(row: Row, bare: bool = False) -> list[tuple[Cell, list[tuple[str, str]]]] | None:
    """Each cell with the roles it names, or None when the row is no header:
    an amount on it, or a cell that is not made of column words. With `bare`,
    a line made of qualifiers alone ("HT | TTC | TTC" under "Prix / Prix /
    Montant") is a header line too - the last of a header printed over
    several, naming no column but flavouring the ones above it."""
    found = []
    unknown = 0
    for cell in row.cells:
        value = cell_value(cell.text)
        if value.kind in ("money", "rate", "digits"):
            return None
        words = _header_words(cell.text)
        if not words:
            continue
        roles = _roles_of(words)
        if roles:
            found.append((cell, roles))
        elif all(word in _MODIFIERS or word in _FLAVOURS or word == "%" for word in words):
            found.append((cell, [("flavour", _FLAVOURS.get(" ".join(words), ""))]))
        else:
            unknown += 1
    named = [roles for _cell, roles in found if any(role not in ("flavour", OTHER) for role, _f in roles)]
    if not named and not (bare and found and not unknown):
        return None
    if unknown > max(1, len(found) // 2):
        return None
    return found


def _is_header_row(row: Row) -> bool:
    """A row naming at least two columns, one of them what is bought or what
    it costs, and printing no amount."""
    found = _header_cells(row)
    if found is None:
        return False
    roles = {role for _cell, roles in found for role, _f in roles}
    roles -= {"flavour", OTHER}
    if len(roles) < 2 and not (len(roles) == 1 and sum(len(r) for _c, r in found) >= 2):
        return False
    return bool(roles & {DESCRIPTION, AMOUNT, UNIT_PRICE, PRICE, QUANTITY, RATE, VAT_AMOUNT})


def _median_gap(rows: list[Row]) -> float | None:
    ys = [row.y for row in rows if row.y is not None]
    gaps = [b - a for a, b in pairwise(ys) if b - a > 0]
    return statistics.median(gaps) if gaps else None


def _close(rows: list[Row], a: int, b: int, gap: float | None) -> bool:
    """Whether rows a and b (a < b) are consecutive lines close enough to be
    one block - within CONTINUATION_GAP median gaps, or unknown."""
    if gap is None or rows[a].y is None or rows[b].y is None:
        return True
    return rows[b].y - rows[a].y <= CONTINUATION_GAP * gap


def _header_block(rows: list[Row], gap: float | None) -> list[int] | None:
    """The first block of consecutive header lines naming a description and a
    price or amount, or None."""
    for index, row in enumerate(rows):
        if not _is_header_row(row):
            continue
        block = [index]
        while len(block) < MAX_HEADER_LINES and block[0] > 0:
            above = block[0] - 1
            if (
                _header_cells(rows[above]) is not None
                and _close(rows, above, block[0], gap)
                and not _is_body_row(rows[above])
            ):
                block.insert(0, above)
            else:
                break
        while len(block) < MAX_HEADER_LINES and block[-1] + 1 < len(rows):
            below = block[-1] + 1
            if _header_cells(rows[below], bare=True) is not None and _close(rows, block[-1], below, gap):
                block.append(below)
            else:
                break
        roles = set()
        for position in block:
            for _cell, cell_roles in _header_cells(rows[position], bare=True) or []:
                roles.update(role for role, _f in cell_roles)
        # "Montant TVA" heads the tax on one invoice and the amount, tax
        # included, on a till's ticket: the words propose, the arithmetic
        # over the rows decides (_assemble).
        if DESCRIPTION in roles and roles & {AMOUNT, UNIT_PRICE, PRICE, VAT_AMOUNT}:
            return block
    return None


def _is_body_row(row: Row) -> bool:
    return any(cell_value(cell.text).kind == "money" for cell in row.cells)


# -- the table -------------------------------------------------------------


def find_table(rows: list[Row]) -> Table | None:
    """The table on a page of rows, or None when there is none to be found.

    With a header (see the module docstring), its words name the columns and
    the cells of the rows under it fill them; without one, the numeric cells
    aligned across rows are the columns, kept only when two rows multiply out
    the same way. Either way the roles are confirmed by arithmetic before a
    row is read through them.
    """
    if not rows or not any(row.cells for row in rows):
        return None
    gap = _median_gap(rows)
    block = _header_block(rows, gap)
    if block is not None:
        table = _table_under_header(rows, block, gap)
        if table is not None and table.items:
            return table
    return _table_without_header(rows)


def _table_under_header(rows: list[Row], block: list[int], gap: float | None) -> Table | None:
    # 1. The header's columns: its cells merged across the block's lines by
    #    overlap, each with the roles its words name.
    header_cells: list[tuple[int, Cell, list[tuple[str, str]]]] = []
    for position in block:
        for cell, roles in _header_cells(rows[position], bare=True) or []:
            header_cells.append((position, cell, roles))
    header_columns: list[dict] = []
    for group in _cluster([(i, cell.x0, cell.x1) for i, (_p, cell, _r) in enumerate(header_cells)]):
        members = sorted((header_cells[i] for i in group), key=lambda entry: (entry[0], entry[1].x0))
        words = []
        for _position, cell, _roles in members:
            words += _header_words(cell.text)
        header_columns.append(
            {
                "x0": min(cell.x0 for _p, cell, _r in members),
                "x1": max(cell.x1 for _p, cell, _r in members),
                "segments": _roles_of(words) or [(OTHER, "")],
                "text": " ".join(cell.text.strip() for _p, cell, _r in members),
            }
        )
    header_columns.sort(key=lambda column: column["x0"])
    descriptions = [column for column in header_columns if column["segments"][0][0] == DESCRIPTION]
    if not descriptions:
        return None
    # "Produit | Désignation", "Code article | Libellé": an invoice has one
    # description column. Where several header cells name one, the cell made
    # of the words that name a NAME (`_NAME_WORDS`) heads it, and a cell
    # naming the THING beside it (produit, article) heads the code. The
    # leftmost taken as the description, the code column was the name and
    # the name itself, lying under the next header's word, was thrown away.
    naming = [column for column in descriptions if _NAME_WORDS & set(_header_words(column["text"]))]
    description = (naming or descriptions)[0]
    for column in descriptions:
        if column is not description:
            column["segments"] = [(REFERENCE, "")]

    # 2. The body: the rows under the header, down to the totals.
    start = block[-1] + 1
    end = _body_end(rows, start)
    body = list(range(start, end))
    numeric: list[tuple[int, Cell, _Value]] = []  # (row index, cell, value)
    for index in body:
        for cell in rows[index].cells:
            value = cell_value(cell.text)
            if value.kind != "text":
                numeric.append((index, cell, value))
    columns: list[Column] = []
    column_cells: list[dict[int, tuple[Cell, _Value]]] = []  # per column: row -> (cell, value)
    for group in _cluster([(i, cell.x0, cell.x1) for i, (_index, cell, _value) in enumerate(numeric)]):
        members = [numeric[i] for i in group]
        column = Column(x0=min(cell.x0 for _i, cell, _v in members), x1=max(cell.x1 for _i, cell, _v in members))
        cells: dict[int, tuple[Cell, _Value]] = {}
        for index, cell, value in members:
            if index not in cells:  # two numeric cells of one row in one column: the first
                cells[index] = (cell, value)
        columns.append(column)
        column_cells.append(cells)
    order = sorted(range(len(columns)), key=lambda i: columns[i].x0)
    columns = [columns[i] for i in order]
    column_cells = [column_cells[i] for i in order]

    # 3. The header names the numeric columns it covers; a glued header cell
    #    ("QuantitéPU HT") names as many columns as it has segments, left to
    #    right, and no more: a cluster beyond its words - a footnoted year
    #    under the description, a VAT code's column of lone "1"s beside the
    #    quantity - is named by no word, and only the arithmetic may still
    #    make it the quantity (`_best_triple`). Given the header's last role
    #    it made that column of "1"s the quantity of every row.
    for header in header_columns:
        under = [i for i, column in enumerate(columns) if column.overlaps(header["x0"], header["x1"])]
        segments = [segment for segment in header["segments"] if segment[0] != DESCRIPTION]
        for position, i in enumerate(under):
            if columns[i].role != OTHER and columns[i].header:
                continue  # already named by a header column overlapping it more
            if position < len(segments):
                columns[i].role, columns[i].flavour = segments[position]
            columns[i].header = header["text"]
    # Columns no header covers are named by what they hold.
    for i, column in enumerate(columns):
        if column.role == OTHER and not column.header:
            column.role = _role_from_values(column_cells[i], i == 0)
    description_column = Column(
        x0=description["x0"], x1=description["x1"], role=DESCRIPTION, header=description["text"]
    )
    text_columns = [
        Column(x0=header["x0"], x1=header["x1"], role=header["segments"][0][0], header=header["text"])
        for header in header_columns
        if header is not description
    ]

    table = _assemble(rows, body, columns, column_cells, description_column, text_columns, gap)
    if table is None:
        return None
    table.header_rows = list(block)
    table.inside.update(block)
    table.before = set(range(block[0]))
    table.body_end = end
    table.columns.append(description_column)
    for column in text_columns:
        if column.role in (REFERENCE, EAN, CODE, OTHER, DISCOUNT) and not any(
            existing.overlaps(column.x0, column.x1) for existing in table.columns
        ):
            table.columns.append(column)
    return table


def _body_end(rows: list[Row], start: int) -> int:
    """Where the rows under the header stop: the first line that is a second
    header, or a total by its label - a cell made of the words totals are
    called ("Total HT", "Net à payer"), beside an amount, wherever on the
    line that cell sits."""
    for index in range(start, len(rows)):
        row = rows[index]
        if _is_header_row(row):
            return index
        values = [cell_value(cell.text) for cell in row.cells]
        if not any(value.kind == "money" for value in values):
            continue
        if any(value.kind == "text" and _is_total_label(cell.text) for cell, value in zip(row.cells, values)):
            return index
    return len(rows)


def _is_total_label(text: str) -> bool:
    """ "Total HT", "Sous-total", "Net à payer", "TOTAL GENERAL TTC" - and not
    "Frais de port", which is a row."""
    words = [word for word in _fold(text).split() if not re.fullmatch(r"[\d%]+", word)]
    return (
        bool(words)
        and all(word in _TOTAL_WORDS or word in _MODIFIERS for word in words)
        and any(word in _TOTAL_MARKERS for word in words)
    )


def clean_text(text: str) -> str:
    """A name without an icon font's glyphs or control characters, its spaces
    folded."""
    return re.sub(r"\s+", " ", _UNPRINTABLE_RE.sub(" ", text)).strip()


def name_cell_text(text: str, coded: bool = False) -> str:
    """What a description cell says, as the text reading would cut it: never
    an amount the recogniser ran into the cell ("1L SIROP GRENADINE 3,45€" is
    the syrup), and - for a cell run into the reference column beside it
    (`coded`) - never the code it starts with ("20000001 CROCHET ACIER").
    A size ("18,50X20") and a percentage ("20% MG") stay in the name."""
    cleaned = clean_text(text)
    amount = MONEY_RE.search(cleaned)
    if amount is not None and not _LETTERS_RE.search(_CURRENCY_RE.sub(" ", cleaned[amount.end() :])):
        cleaned = cleaned[: amount.start()].rstrip(" :-")
    if coded:
        words = cleaned.split()
        while words and cell_value(words[0]).kind in ("digits", "code") and len(words) > 1:
            words.pop(0)
        cleaned = " ".join(words)
    # A small whole number ending the name ("CARNET SPIRALE 2", "DADDY 71")
    # is a count or a code the recogniser ran into it, and the text reading
    # cuts it (`_read_line`): the same article read two ways is two products.
    trimmed = _TRAILING_INTEGER_RE.sub("", cleaned)
    return trimmed if _LETTERS_RE.search(trimmed) else cleaned


def _role_from_values(cells: dict[int, tuple[Cell, _Value]], leftmost: bool) -> str:
    values = [value for _cell, value in cells.values()]
    kinds = {value.kind for value in values}
    if kinds <= {"digits", "code"}:
        return EAN if all(value.kind == "digits" and is_gtin(value.value) for value in values) else REFERENCE
    if kinds == {"rate"}:
        return RATE
    if kinds == {"integer"}:
        integers = [value.value for _index, (_cell, value) in sorted(cells.items())]
        if leftmost and len(integers) >= 2 and integers == list(range(1, len(integers) + 1)):
            return LINE_NUMBER
        return QUANTITY
    if "money" in kinds:
        return PRICE
    return OTHER


def is_gtin(code: str) -> bool:
    if len(code) not in (8, 12, 13, 14) or not code.isdigit():
        return False
    digits = [int(char) for char in code]
    weighted = sum(digit * (3 if position % 2 == 0 else 1) for position, digit in enumerate(reversed(digits[:-1])))
    return (10 - weighted % 10) % 10 == digits[-1]


def _assemble(rows, body, columns, column_cells, description, text_columns, gap) -> Table | None:
    """Read the body rows through the columns: which are items, what each
    says, which lines wrap a description - the arithmetic confirming the
    roles first, the amount column's included."""
    if not columns:
        return None
    money_columns = [i for i, cells in enumerate(column_cells) if any(v.kind == "money" for _c, v in cells.values())]
    if not money_columns:
        return None

    # The header's word is narrower than the names under it, so the column
    # grows to the cells of the rows read as items (`widen`) - never into a
    # text column beside it (the reference's), nor past the figures.
    left_edge = max((column.x1 for column in text_columns if column.x1 <= description.x0 + 1), default=float("-inf"))
    # The text columns a name never runs into: the code, the EAN, a status.
    # Never the header WORD of a numeric role: the name under "Désignation"
    # may well reach under "Qté", and the figures bound it, not the word.
    excluding = [
        column for column in text_columns if column.role in (REFERENCE, EAN, CODE, OTHER, DISCOUNT, LINE_NUMBER)
    ]

    def figures_from() -> float:
        """Where the figures start, right of the description: the columns of
        money, the columns a header word names, and the columns of whole
        numbers filled on two rows at least. A lone "2025 (1)" under a name
        is inside the description, not a column that cuts it short."""
        return min(
            (
                columns[i].x0
                for i in range(len(columns))
                if columns[i].x0 > description.x0 + 1
                and (
                    i in money_columns
                    or (columns[i].role != OTHER and columns[i].header)
                    or sum(1 for _c, v in column_cells[i].values() if v.kind == "integer") >= 2
                )
            ),
            default=float("inf"),
        )

    def description_text(index: int) -> tuple[str, bool, list[Cell]]:
        """The description column's text on the row, whether the row has
        one, and the cells it came from: a text cell overlapping the column -
        whatever else it touches, a cell run into the reference beside it
        included - or lying between it and the figures."""
        bound = figures_from()
        found = []
        for cell in rows[index].cells:
            value = cell_value(cell.text)
            if value.kind != "text" or not _LETTERS_RE.search(cell.text):
                continue
            if description.overlaps(cell.x0, cell.x1):
                found.append(cell)
            elif any(column.overlaps(cell.x0, cell.x1) for column in excluding):
                continue
            elif cell.x0 >= description.x0 - 1 and cell.x1 <= bound + 1:
                found.append(cell)
        references = [column for column in text_columns if column.role in (REFERENCE, EAN)]
        parts = [
            name_cell_text(cell.text, coded=any(column.overlaps(cell.x0, cell.x1) for column in references))
            for cell in found
        ]
        return " ".join(part for part in parts if part), bool(found), found

    def widen(cells: list[Cell]) -> None:
        for cell in cells:
            description.x0 = max(min(description.x0, cell.x0), left_edge)
            description.x1 = max(description.x1, cell.x1)

    # 4. The amount column, by arithmetic first: the column that a quantity
    #    column times a unit-price column makes, on the most rows printing a
    #    description (`_best_triple`). The header's words only break a tie.
    #    Failing that, the rightmost column the header calls an amount, else
    #    the only column of money. A header naming the total's column
    #    "Prix H.T" beside a "Montant" that is the tax made every row's tax
    #    the amount; a header whose total column read as "T" made the unit
    #    price the amount.
    named = [index for index in body if description_text(index)[1]]
    quantity_at = unit_at = None
    triple = _best_triple(columns, column_cells, named)
    if triple is not None:
        quantity_at, unit_at, amount_at = triple
        for i, column in enumerate(columns):
            if column.role == QUANTITY and i != quantity_at and not column.header:
                column.role = OTHER
        columns[quantity_at].role, columns[quantity_at].confirmed = QUANTITY, True
        columns[unit_at].role, columns[unit_at].confirmed = UNIT_PRICE, True
        columns[amount_at].role, columns[amount_at].confirmed = AMOUNT, True
    else:
        amount_at = next(
            (i for i in reversed(range(len(columns))) if columns[i].role == AMOUNT and i in money_columns), None
        )
        if amount_at is None and len(money_columns) == 1:
            # The one column of money on the rows is their amount, whatever
            # the header calls it: "MONTANT TVA" on a DIY store's ticket is
            # the amount, tax included, not the tax.
            amount_at = money_columns[0]
            columns[amount_at].role = AMOUNT
        elif amount_at is None:
            # Several columns of money, none the header calls an amount, no
            # row that multiplies out: nothing confirmed any of them, and the
            # rightmost is not the amount by default. Taken so, a PDF whose
            # lines are one string each - its figures wherever the words
            # before them end - was a table of one row with a truncated
            # name, and every check passed. The text reading keeps the page.
            return None
    # A "Prix" that is not the unit price is another amount.
    for i, column in enumerate(columns):
        if column.role == PRICE:
            column.role = UNIT_PRICE if i < amount_at else AMOUNT

    # 5. Item rows: a description and an amount, up to the first total. An
    #    EAN printed in the description (told by its check digit) goes on the
    #    line, out of the name, as the text reader keeps it. A body line made
    #    of the header's own words - "Prix Unitaire Brut HT" printed again,
    #    a figure the recogniser ran into it or not - is the table's, never a
    #    row (`_is_vocabulary_row`).
    items: dict[int, ItemRow] = {}
    vocabulary: set[int] = set()
    running = ZERO
    for index in body:
        if _is_vocabulary_row(rows[index]):
            vocabulary.add(index)
            continue
        name, has_name, cells_found = description_text(index)
        amount = _amount_of(column_cells[amount_at].get(index))
        if not has_name or amount is None or amount <= 0:
            continue
        if _is_total_label(name) or (len(items) >= 2 and abs(amount - running) <= CENTS):
            break
        name, ean = _without_gtin(name)
        items[index] = ItemRow(index=index, name=name, amount=amount, ean=ean)
        widen(cells_found)
        running += amount
    if not items:
        return None

    # The column has to hold the amount of the rows it is for. A named row
    # printing money that the amount column has no cell of contradicts it;
    # a column contradicted by as many rows as it holds is not the amount
    # column, and the page is no table: its figures do not align.
    def prints_money(index: int) -> bool:
        return any(
            index in cells
            and cells[index][1].kind == "money"
            and cells[index][1].value is not None
            and cells[index][1].value > 0
            for cells in column_cells
        )

    contradicting = [
        index
        for index in body
        if index not in items
        and index not in vocabulary
        and column_cells[amount_at].get(index) is None
        and prints_money(index)
        and description_text(index)[1]
    ]
    if len(contradicting) >= len(items):
        return None

    # 6. The other roles: the quantity and unit price when no triple settled
    #    them (the header's, a lone candidate), the rate, the EAN column, and
    #    the tax columns the arithmetic proves (`_confirm_tax_columns`).
    if quantity_at is None:
        quantity_at = next((i for i, column in enumerate(columns) if column.role == QUANTITY), None)
    if unit_at is None:
        unit_candidates = [i for i, column in enumerate(columns) if column.role == UNIT_PRICE]
        unit_at = unit_candidates[0] if len(unit_candidates) == 1 else None
    rate_at = next((i for i, column in enumerate(columns) if column.role == RATE), None)
    ean_at = next((i for i, column in enumerate(columns) if column.role == EAN), None)
    for index, item in items.items():
        cells = {i: column_cells[i].get(index) for i in range(len(columns))}
        if quantity_at is not None and cells[quantity_at] is not None:
            item.quantity = _as_quantity(cells[quantity_at][1])
        if unit_at is not None and cells[unit_at] is not None and cells[unit_at][1].kind == "money":
            item.unit_price = cells[unit_at][1].value
        if ean_at is not None and cells[ean_at] is not None and is_gtin(str(cells[ean_at][1].value)):
            item.ean = str(cells[ean_at][1].value)
        if rate_at is not None and cells[rate_at] is not None:
            item.rate = _as_rate(cells[rate_at][1])
    _confirm_tax_columns(columns, column_cells, items, amount_at)
    unit_columns = [i for i, column in enumerate(columns) if column.role == UNIT_PRICE]
    vat_at = next((i for i, column in enumerate(columns) if column.role == VAT_AMOUNT), None)
    ttc_at = next(
        (
            i
            for i, column in enumerate(columns)
            if column.role == AMOUNT and column.flavour == "TTC" and i != amount_at and column.confirmed
        ),
        None,
    )
    for index, item in items.items():
        cells = {i: column_cells[i].get(index) for i in range(len(columns))}
        _settle_taxes(item, cells, unit_columns, vat_at, ttc_at)
        _unit_in_ht(item, cells, unit_columns)
        item.counted = quantity_at is not None
    _column_in_ht(items)

    # 7. Wrapped descriptions: text alone in the description column, right
    #    under an item, close to it - on an exact layer only (see
    #    `_is_continuation`). Those lines are the table's; every other line
    #    inside it - a discount under its article, a detail, a sub-total - is
    #    left to the text reading, which knows what to do with them.
    inside: set[int] = set(vocabulary)
    pitch = _item_pitch(rows, items)
    bound = figures_from()
    ordered = sorted(items)
    for position, index in enumerate(ordered):
        following = ordered[position + 1] if position + 1 < len(ordered) else (body[-1] + 1 if body else index + 1)
        previous = index
        for candidate in range(index + 1, following):
            if candidate in items:
                break
            if _is_continuation(rows, candidate, description, text_columns, bound, gap, previous, pitch):
                text, _has, _cells = description_text(candidate)
                text, ean = _without_gtin(text)
                if text:
                    items[index].name = f"{items[index].name} {text}".strip()
                if ean and not items[index].ean:
                    items[index].ean = ean
                items[index].continuation.append(candidate)
                inside.add(candidate)
                previous = candidate
            else:
                break

    for i, column in enumerate(columns):
        column.used = any(index in items for index in column_cells[i])
    return Table(columns=columns, header_rows=[], items=items, inside=inside, before=set())


def _best_triple(columns, column_cells, named) -> tuple[int, int, int] | None:
    """(quantity, unit price, amount) columns: the three whose cells multiply
    out on the most rows printing a description - a coincidence on none of
    them. Ties go to the columns the header names so, then to the rightmost
    amount and the leftmost quantity. A column of line numbers, references
    or rates is never the quantity."""
    money = [i for i, cells in enumerate(column_cells) if any(v.kind == "money" for _c, v in cells.values())]
    counts = [
        i
        for i, column in enumerate(columns)
        if column.role in (QUANTITY, PRICE, OTHER)
        and column_cells[i]
        and all(v.kind in ("integer", "money") for _c, v in column_cells[i].values())
    ]
    best = None
    for a in money:
        for q in counts:
            if q == a:
                continue
            for u in money:
                if u in (q, a):
                    continue
                hits = 0
                for index in named:
                    quantity_cell, unit_cell, amount_cell = (
                        column_cells[q].get(index),
                        column_cells[u].get(index),
                        column_cells[a].get(index),
                    )
                    if quantity_cell is None or unit_cell is None or amount_cell is None:
                        continue
                    if unit_cell[1].kind != "money" or unit_cell[1].value <= 0:
                        continue
                    amount = _amount_of(amount_cell)
                    quantity = _as_quantity(quantity_cell[1])
                    if amount is None or amount <= 0 or quantity is None or quantity <= 0:
                        continue
                    if abs(Decimal(quantity) * unit_cell[1].value - amount) <= max(
                        ROW_TOLERANCE, Decimal(quantity) * HALF_CENT
                    ):
                        hits += 1
                if not hits:
                    continue
                score = (
                    hits,
                    columns[a].role == AMOUNT,
                    columns[q].role == QUANTITY,
                    columns[u].role == UNIT_PRICE,
                    a,
                    -q,
                )
                if best is None or score > best[0]:
                    best = (score, q, u, a)
    if best is None:
        return None
    _score, q, u, a = best
    return q, u, a


def _amount_of(cell) -> Decimal | None:
    """A cell's value as an amount of money: in cents. A third decimal on an
    amount is a currency sign the recogniser read as a digit ("11,901"), as
    the text reading already holds (`line_amounts`); a unit price keeps its
    decimals, since 9,397 the copy is a real price."""
    if cell is None or cell[1].kind != "money" or cell[1].value is None:
        return None
    value = cell[1].value
    if value == value.quantize(CENTS):
        return value
    exponent = -value.as_tuple().exponent
    if exponent == 3:
        return value.quantize(CENTS, rounding=ROUND_DOWN)
    return value


def _without_gtin(name: str) -> tuple[str, str]:
    """The name without an EAN printed in it (a digit run its check digit
    proves), and that EAN - "" when there is none."""
    ean = ""
    kept = []
    for word in name.split():
        if not ean and is_gtin(word):
            ean = word
            continue
        kept.append(word)
    cleaned = " ".join(kept).strip(" :-")
    return (cleaned if ean else name), ean


def _unit_in_ht(item: ItemRow, cells, unit_columns) -> None:
    """Once a row is known in HT, its unit price is the HT one - the reader
    checks count x unit against the HT - or none, never a TTC price beside
    an HT amount."""
    if item.ht is None or not item.quantity or item.unit_price is None:
        return
    quantity = Decimal(item.quantity)
    slack = max(CENTS, quantity * HALF_CENT)
    if abs(quantity * item.unit_price - item.ht) <= slack:
        return
    for i in unit_columns:
        cell = cells.get(i)
        if cell is not None and cell[1].kind == "money" and abs(quantity * cell[1].value - item.ht) <= slack:
            item.unit_price = cell[1].value
            return
    item.unit_price = None


def _item_pitch(rows, items) -> float | None:
    """How far apart the table's item rows are, at the widest: a wrapped
    description sits closer under its row than the next row does. None with
    fewer than two items, or no heights."""
    ys = [rows[index].y for index in sorted(items) if rows[index].y is not None]
    gaps = [below - above for above, below in pairwise(ys) if below > above]
    return max(gaps) if gaps else None


# A line of this many words ending on a full stop is a sentence - a shop's
# terms, a footnote - and no product's description.
SENTENCE_WORDS = 6


def _is_sentence(text: str) -> bool:
    words = text.split()
    return len(words) >= SENTENCE_WORDS and words[-1].endswith(".")


def _is_continuation(rows, index, description, text_columns, bound, gap, previous, pitch=None) -> bool:
    """A row printing text only, in the description column (or the reference
    column beside it), close under the row before it - within the page's
    line pitch (`_close`) and no further than the table's own rows are from
    each other (`pitch`): a shop's delivery terms printed two rows' worth
    under the last article are not its name. Nor is a line reaching under
    the figures (`bound`: where they start), nor a sentence
    (`_is_sentence`): a one-row table has no pitch, and the terms printed
    right under its row went onto the name with every check passing.

    Only on an exact layer (`Row.exact`: a PDF's own words). On a photograph
    the lines are the recogniser's grouping of its boxes, and the text under
    a row may be the next row's head whose figures were grouped with this
    one; the text reading keeps such a line as it is, and the names the
    tickets already checked are read that way."""
    row = rows[index]
    if not row.cells or not row.exact or not _close(rows, previous, index, gap):
        return False
    if (
        pitch is not None
        and rows[previous].y is not None
        and row.y is not None
        and row.y - rows[previous].y > ROW_PITCH_SLACK * pitch
    ):
        return False
    if _is_sentence(row.text):
        return False
    for cell in row.cells:
        value = cell_value(cell.text)
        if value.kind != "text":
            return False
        if MONEY_RE.search(cell.text) or any(is_gtin(word) for word in cell.text.split()):
            return False  # a detail line ("EAN : 3000000001011"), not a name
        if cell.x1 > bound + 1:
            return False  # reaches under the figures: not a name's overflow
        in_description = description.overlaps(cell.x0, cell.x1)
        in_text_column = any(column.overlaps(cell.x0, cell.x1) for column in text_columns)
        if not in_description and not in_text_column and cell.x0 < description.x0 - 1:
            return False
    return True


def _is_vocabulary_row(row: Row) -> bool:
    """A body line made of the header's own words - "Prix Unitaire Brut HT"
    printed again under the header, a figure the recogniser ran into it or
    not: a header line, never an article. Every text cell has to be column
    words or their qualifiers with not one word naming nothing, and there
    have to be three words at least or two roles: "Total" alone beside an
    amount is a total, "TVA 20 %" a rate, and both are the text reading's."""
    texts = [cell.text for cell in row.cells if cell_value(cell.text).kind == "text" and _LETTERS_RE.search(cell.text)]
    if not texts:
        return False
    words = roles = 0
    for text in texts:
        # The figure run into the words ("Prix Unitaire Brut HT: 123.45€")
        # is not a word naming nothing: it is what the recogniser glued on.
        cell_words = [word for word in _header_words(text) if not word.isdigit()]
        found, unknown = _roles_and_unknown(cell_words)
        if unknown or not (found or all(word in _MODIFIERS or word in _FLAVOURS or word == "%" for word in cell_words)):
            return False
        words += len(cell_words)
        roles += sum(1 for role, _flavour in found if role != OTHER)
    return words >= 3 or roles >= 2


def _as_quantity(value: _Value):
    if value.kind == "integer":
        return value.value
    if value.kind == "money" and value.value is not None and 0 < value.value < 10000:
        quantity = value.value
        return int(quantity) if quantity == quantity.to_integral_value() else quantity
    return None


def _as_rate(value: _Value) -> Decimal | None:
    """A rate cell: "5,5 %", "20%", "0 %", or a bare "20,00" under a header
    naming the rate. Only a rate France has, or none."""
    if value.kind == "rate":
        rate = value.value
    elif value.kind == "money" and value.value is not None:
        rate = value.value / Decimal("100")
    elif value.kind == "integer":
        rate = Decimal(value.value) / Decimal("100")
    else:
        return None
    if rate == 0:
        return ZERO
    return next((known for known in KNOWN_VAT_RATES if known == rate), None)


def _confirm_tax_columns(columns, column_cells, items, amount_at) -> None:
    """The tax columns, by what their cells make of the amount: a column
    whose value is the amount times the row's rate (a French one when the row
    prints none) on half the items at least is the VAT amount, whatever the
    header called it ("Montant" beside "% TVA"); one whose value is the
    amount plus that tax is the amount TTC. Nothing is relabelled that the
    arithmetic does not confirm."""
    for i, column in enumerate(columns):
        if (
            i == amount_at
            or column.confirmed
            or column.role in (QUANTITY, UNIT_PRICE, LINE_NUMBER, REFERENCE, EAN, CODE, DISCOUNT)
        ):
            continue
        # A column the header calls "TVA" holds the rate or the tax: its
        # cells say which. Rates printed with their sign are rates; a
        # column of money under that word is looked at like any other.
        if column.role == RATE and any(v.kind == "rate" for _c, v in column_cells[i].values()):
            continue
        if not any(v.kind == "money" for _c, v in column_cells[i].values()):
            continue
        vat_hits = ttc_hits = 0
        for index, item in items.items():
            cell = column_cells[i].get(index)
            if cell is None or cell[1].kind != "money" or item.amount is None:
                continue
            value = cell[1].value
            rates = [item.rate] if item.rate is not None else list(KNOWN_VAT_RATES)
            # The tax on an amount HT, or the tax inside an amount TTC.
            if any(
                rate > 0
                and (
                    abs((item.amount * rate).quantize(CENTS) - value) <= CENTS
                    or abs((item.amount * rate / (1 + rate)).quantize(CENTS) - value) <= CENTS
                )
                for rate in rates
            ):
                vat_hits += 1
            elif any(rate > 0 and abs((item.amount * (1 + rate)).quantize(CENTS) - value) <= CENTS for rate in rates):
                ttc_hits += 1
        if vat_hits and vat_hits >= ttc_hits and 2 * vat_hits >= len(items):
            column.role, column.flavour, column.confirmed = VAT_AMOUNT, "", True
        elif ttc_hits and 2 * ttc_hits >= len(items) and column.role in (AMOUNT, PRICE, OTHER):
            column.role, column.flavour, column.confirmed = AMOUNT, "TTC", True


def _settle_taxes(item: ItemRow, cells, unit_columns, vat_at, ttc_at=None) -> None:
    """What the row itself proves about HT and TTC: its rate turning its
    amount into its VAT amount, its TTC printed as the amount plus that tax,
    its two unit prices one rate apart, or a rate of zero. Nothing proven,
    nothing set: the document's own totals decide further on
    (generic_receipt._ht_choice)."""
    amount, rate = item.amount, item.rate
    if amount is None:
        return
    if rate is not None and rate == 0:
        item.ht, item.ttc = amount, amount
        return
    rates = [rate] if rate is not None else list(KNOWN_VAT_RATES)
    printed_ttc = (
        cells[ttc_at][1].value
        if ttc_at is not None and cells.get(ttc_at) is not None and cells[ttc_at][1].kind == "money"
        else None
    )
    if vat_at is not None and cells.get(vat_at) is not None and cells[vat_at][1].kind == "money":
        vat = cells[vat_at][1].value
        for candidate in rates:
            if abs((amount * candidate).quantize(CENTS) - vat) <= CENTS:
                item.rate, item.ht, item.ttc = candidate, amount, amount + vat
                if printed_ttc is not None and abs(printed_ttc - item.ttc) <= CENTS:
                    # "A receipt line keeps its printed TTC": 6,64 HT and
                    # 0,37 of tax is 7,01, and the row prints 7,00 - which is
                    # what was paid.
                    item.ttc, item.ttc_printed = printed_ttc, True
                return
            if abs((amount * candidate / (1 + candidate)).quantize(CENTS) - vat) <= CENTS:
                item.rate, item.ht, item.ttc = candidate, amount - vat, amount
                item.ttc_printed = True
                return
    if printed_ttc is not None:
        for candidate in rates:
            if candidate > 0 and abs((amount * (1 + candidate)).quantize(CENTS) - printed_ttc) <= CENTS:
                item.rate, item.ht, item.ttc, item.ttc_printed = candidate, amount, printed_ttc, True
                return
    units = [cells[i][1].value for i in unit_columns if cells.get(i) is not None and cells[i][1].kind == "money"]
    if len(units) == 2 and item.quantity:
        first, second = sorted(units)
        quantity = Decimal(item.quantity)
        slack = max(CENTS, quantity * HALF_CENT)
        for candidate in rates:
            if first > 0 and abs((first * (1 + candidate)).quantize(CENTS) - second) <= CENTS:
                if abs(quantity * second - amount) <= slack:
                    item.rate, item.ttc, item.ht = candidate, amount, (quantity * first).quantize(CENTS)
                    item.ttc_printed = True
                    return
                if abs(quantity * first - amount) <= slack:
                    item.rate, item.ht, item.ttc = candidate, amount, (quantity * second).quantize(CENTS)
                    return


def _column_in_ht(items: dict[int, ItemRow]) -> None:
    """A column is HT or TTC as a whole. Once a row proves the amount column
    HT (`_settle_taxes`: its tax, its two unit prices) and no row proves it
    TTC, the rows that prove nothing - an eco-participation whose unit price
    multiplies into nothing - are in HT too, at their own rate or at the one
    rate every row of the column carries. Left to the document's totals,
    1,50 in a column of HT figures was read as TTC, 1,25 HT beside 360,00
    HT. A row at 0 % proves nothing either way, and a column with a row
    proven TTC is left as it is."""
    proven_ht = [item for item in items.values() if item.ht is not None and item.rate and item.ht == item.amount]
    proven_ttc = [
        item
        for item in items.values()
        if item.ttc is not None and item.rate and item.ttc == item.amount and item.ht != item.amount
    ]
    if not proven_ht or proven_ttc:
        return
    rates = {item.rate for item in items.values() if item.rate is not None}
    for item in items.values():
        if item.ht is not None or item.amount is None:
            continue
        rate = item.rate if item.rate is not None else (next(iter(rates)) if len(rates) == 1 else None)
        if rate is None:
            continue
        item.rate, item.ht = rate, item.amount
        item.ttc = (item.amount * (1 + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


# -- without a header --------------------------------------------------------

# Without a header, this many rows have to multiply out the same way.
MIN_ALIGNED_ROWS = 2


def _table_without_header(rows: list[Row]) -> Table | None:
    """A table nobody headed: rows of at least three numeric cells, aligned,
    in which one column times another makes a third - a quantity, a unit
    price and an amount - on at least MIN_ALIGNED_ROWS rows. The name is the
    text left of the figures."""
    numeric: list[tuple[int, Cell, _Value]] = []
    for index, row in enumerate(rows):
        values = [(cell, cell_value(cell.text)) for cell in row.cells]
        if sum(1 for _c, v in values if v.kind != "text") < 3:
            continue
        for cell, value in values:
            if value.kind != "text":
                numeric.append((index, cell, value))
    if not numeric:
        return None
    columns: list[Column] = []
    column_cells: list[dict[int, tuple[Cell, _Value]]] = []
    for group in _cluster([(i, cell.x0, cell.x1) for i, (_index, cell, _value) in enumerate(numeric)]):
        members = [numeric[i] for i in group]
        cells: dict[int, tuple[Cell, _Value]] = {}
        for index, cell, value in members:
            cells.setdefault(index, (cell, value))
        columns.append(Column(x0=min(c.x0 for _i, c, _v in members), x1=max(c.x1 for _i, c, _v in members)))
        column_cells.append(cells)
    order = sorted(range(len(columns)), key=lambda i: columns[i].x0)
    columns = [columns[i] for i in order]
    column_cells = [column_cells[i] for i in order]
    # The triple (quantity, unit, amount) holding on the most rows.
    best = None
    for a in range(len(columns)):
        if not any(v.kind == "money" for _c, v in column_cells[a].values()):
            continue
        for q in range(len(columns)):
            if q == a or not all(v.kind in ("integer", "money") for _c, v in column_cells[q].values()):
                continue
            for u in range(len(columns)):
                if u in (q, a) or not any(v.kind == "money" for _c, v in column_cells[u].values()):
                    continue
                hits = []
                for index in column_cells[a]:
                    quantity_cell, unit_cell = column_cells[q].get(index), column_cells[u].get(index)
                    amount_cell = column_cells[a][index]
                    if (
                        quantity_cell is None
                        or unit_cell is None
                        or unit_cell[1].kind != "money"
                        or amount_cell[1].kind != "money"
                    ):
                        continue
                    quantity = _as_quantity(quantity_cell[1])
                    amount = _amount_of(amount_cell)
                    if quantity is None or quantity <= 0 or amount is None or amount <= 0:
                        continue
                    if abs(Decimal(quantity) * unit_cell[1].value - amount) <= max(
                        ROW_TOLERANCE, Decimal(quantity) * HALF_CENT
                    ):
                        hits.append(index)
                if len(hits) >= MIN_ALIGNED_ROWS:
                    score = (len(hits), a, -q)
                    if best is None or score > best[0]:
                        best = (score, q, u, a, hits)
    if best is None:
        return None
    _score, q, u, a, hits = best
    columns[q].role, columns[q].confirmed = QUANTITY, True
    columns[u].role, columns[u].confirmed = UNIT_PRICE, True
    columns[a].role, columns[a].confirmed = AMOUNT, True
    for i, column in enumerate(columns):
        if column.role == OTHER:
            column.role = _role_from_values(column_cells[i], i == 0)
            if column.role == QUANTITY:
                column.role = OTHER
            if column.role == PRICE and i > a:
                column.role = OTHER
    rate_at = next((i for i, column in enumerate(columns) if column.role == RATE and i > a), None)
    if rate_at is None:
        # A bare "20,00" beside the amounts: a rate when every row prints one.
        for i, column in enumerate(columns):
            if (
                i > a
                and column.role == OTHER
                and column_cells[i]
                and all(_as_rate(v) is not None and _as_rate(v) != 0 for _c, v in column_cells[i].values())
            ):
                columns[i].role, rate_at = RATE, i
                break
    # The name: the row's text left of its prices, wherever the figures leave
    # it - the quantity column may stand before it ("1  TORCHON  14,90
    # 14,90"). A letter or two between the prices is a code (a VAT code, a
    # unit), never the name.
    figures = [columns[i] for i in (q, u, a) + ((rate_at,) if rate_at is not None else ())]
    prices_from = min(
        columns[i].x0
        for i in range(len(columns))
        if any(index in hits and v.kind == "money" for index, (_c, v) in column_cells[i].items())
    )
    items: dict[int, ItemRow] = {}
    for index in sorted(hits):
        row = rows[index]
        parts = []
        for cell in row.cells:
            value = cell_value(cell.text)
            if value.kind != "text" or not _LETTERS_RE.search(cell.text):
                continue
            if any(column.overlaps(cell.x0, cell.x1) for column in figures) or cell.x0 >= prices_from:
                continue
            parts.append(clean_text(cell.text))
        if not any(parts):
            continue
        name, ean = _without_gtin(" ".join(part for part in parts if part))
        item = ItemRow(index=index, name=name, amount=_amount_of(column_cells[a][index]), ean=ean)
        item.quantity = _as_quantity(column_cells[q][index][1])
        item.unit_price = column_cells[u][index][1].value
        item.counted = True
        cells = {i: column_cells[i].get(index) for i in range(len(columns))}
        if rate_at is not None and cells[rate_at] is not None:
            item.rate = _as_rate(cells[rate_at][1])
        # A row printing its own tax and its TTC beside the amount.
        _settle_headerless_taxes(item, cells, columns, a)
        items[index] = item
    if len(items) < MIN_ALIGNED_ROWS:
        return None
    for i, column in enumerate(columns):
        column.used = any(index in items for index in column_cells[i])
    return Table(columns=columns, header_rows=[], items=items, inside=set(), before=set())


def _settle_headerless_taxes(item: ItemRow, cells, columns, amount_at) -> None:
    """Right of the amount, a VAT amount the amount's rate makes and a TTC
    that is their sum prove the amount HT."""
    amount = item.amount
    money_right = [
        cells[i][1].value
        for i in range(amount_at + 1, len(columns))
        if cells.get(i) is not None and cells[i][1].kind == "money" and columns[i].role != RATE
    ]
    rates = [item.rate] if item.rate else list(KNOWN_VAT_RATES)
    for vat in money_right:
        for candidate in rates:
            if abs((amount * candidate).quantize(CENTS) - vat) <= CENTS and (amount + vat) in money_right:
                item.rate, item.ht, item.ttc = candidate, amount, amount + vat
                return
    if item.rate == 0:
        item.ht, item.ttc = amount, amount
