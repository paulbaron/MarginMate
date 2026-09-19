"""One reader for every photographed till receipt.

Four shops, five layouts, and the same structure underneath: product lines
(a name, sometimes a count or a weight, a unit price, an amount), then
totals, payment and a VAT table. Their differences are in where things sit,
not in what they are - so this reads a line for what its numbers do rather
than for where a given till prints them:

    BAGUETTE BLANC  T1 0.49              name, VAT code, amount
    PAIN COMPLET  T1 2 X 0.49Eur 0.98Eur count, unit price, amount
    4X BASILIC FRAIS  1,50  6,00         count first
    Article divers / 3pcs  0,80  2,40 A  name above, count, unit, amount, code
    MENTHE  1.00 / 2x  0.50EUR           the detail under its item
    BRUTWEIGHT 0.920 KG / @3.49 / KG     a weight, the item below it
    1L RHUM  18,25 €  4  73,00 €  20,00  14,60 €  87,60 €
                                         a row printing its own HT, rate and VAT

**Everything is decided by arithmetic** (see receipt_base): a count is the
integer that multiplies a unit price into the amount; a detail line belongs to
the neighbouring item whose amount it explains; the items stop where a line
repeats what was paid; header junk is whatever the run of items that adds up
to the total leaves out; a VAT code means the rate whose bucket its items add
up to. Words are never keys: "NUL LIGNE" is recognised because a negative
amount cancels an earlier item of that name, not by its spelling.

**Repairs are arithmetic too, and always said.** A price read 0.45 among
identical items at 0.49, when the ticket's pre-discount total needs exactly
0.49; a VAT code read as a leading digit ("120.30" for "T2 0.30"), when only
the shorter reading adds up. Each is listed under "Montants recalculés".

**A promotion stays visible.** A line keeps the amount the ticket printed
(`printed_ttc`) and carries its share of the promotion (`discount_ttc`),
spread over the products the promotion block names - never beyond what those
products cost.

What differs per shop is data (`TicketShop`): how its header reads, and the
name its till prints instead of a product name ("Article divers" at Sabbh,
where the unit price names the product - receipts.label_placeholder_lines).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from itertools import permutations, product

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from .base import ParseCheck, ParsedInvoice, ParsedLine, PdfPage
from .receipt_base import (
    BANNER_RE,
    CENTS,
    HALF_CENT,
    KNOWN_VAT_RATES,
    MONEY_RE,
    PER_UNIT_RE,
    RATE_RE,
    UNIT,
    UNREAD_NAME,
    ReceiptParser,
    ReceiptTotals,
    VatSummary,
    amount_printed,
    assign_rates_by_bucket,
    build_checks,
    collect_vat_summaries,
    compose_invoice_number,
    ends_items,
    format_rate,
    line_amounts,
    missing_item_check,
    money_value,
    parse_vat_line,
    printed_ht_base,
    printed_promotion,
    printed_total,
    quantity_checks,
    read_date,
    read_rate,
    reconcile_quantity,
    to_ht,
    unmarked_vat_row,
)
from .registry import PARSER_REGISTRY

ZERO = Decimal("0")
# What a ticket's line is priced at when nothing on it proves a rate: the
# shops these tickets come from sell food. The ticket still fails
# "Taux par article", so a person confirms it.
DEFAULT_TICKET_RATE = Decimal("0.055")
TAXED_ROW_TOLERANCE = CENTS
# How close a discount block's name has to be to an item's to be its product,
# and how far below the best match a line may be and still be the same one
# ("BAGUETTE BLAND" and "BAGUETTE BLAVC" are the "BAGUETTE BLANC" promoted).
DISCOUNT_NAME_FLOOR = 70
DISCOUNT_NAME_SPREAD = 12
VOID_NAME_MATCH = 80
PLACEHOLDER_NAME_MATCH = 80
# Beyond this many ways of splitting uncoded items between two rates, the
# split is not searched: the ticket goes to review.
MAX_RATE_SPLITS = 200_000


@dataclass(frozen=True)
class TicketShop:
    supplier_code: str
    # Matched against the recognised text to route a photo to its shop.
    header_patterns: tuple[str, ...]
    # What the line's category defaults to (Monoprix's department headings
    # override it).
    label: str
    # The name a till prints instead of a product's; such a line is named
    # from the shop's price list at import.
    placeholder_names: tuple[str, ...] = ()
    # The till prints a VAT code against every item ("T1"): on a ticket where
    # no line carries one, "SOUS-TOTAL 0.98" is not an item. (A code misread,
    # "71 3.45", is still an item among coded ones.)
    coded_items: bool = False


# "2 X 0.49", "4X BASILIC", "3pcs", "1x" - a count, with the sign that makes
# it one. "6X1L" and "24X33CL" are pack sizes, "100X35X2.5" a size: part of
# the name.
COUNT_RE = re.compile(
    r"(?<![\d.,])(?<!\d[xX×])(?P<count>\d{1,3})(?:[.,]0{3})?\s*(?:[xX×*](?![A-Za-z]{2})|pcs?\b|pce?\b)"
    r"(?!\s*\d+(?:[.,]\d+)?\s*(?:L|CL|ML|G|KG)\b)(?!\s*\d+(?:[.,]\d+)?\s*[xX×*]\s*\d)",
    re.IGNORECASE,
)
# "76X46", a rug's, or "21X35" and "16*25": one word of two whole numbers is
# a size too - unless the one multiplies the other into an amount the line
# prints ("TASSE 2x4 8,00": two at 4,00), since a count is what multiplies a
# unit price into the amount (`_is_size`). Read as 76, the count made the
# rug's name a detail line, and its price went with it.
GLUED_SIZE_RE = re.compile(r"(?P<count>\d{1,3})[xX×*](?P<by>\d+)(?![\d.,])")
# "T11.15": the VAT code T1 glued to 1.15; "T18X0.49": to a count.
GLUED_CODE_RE = re.compile(r"(?<![A-Za-z0-9])T(\d)(?=-?\d)")
# "T3XBASILIC": a code, a count and a name with no space between them.
GLUED_COUNT_RE = re.compile(r"^\s*([A-Z])(\d{1,2})[xX×](?=[A-Za-z]{2})")
# "2.80.11.20": two amounts, a stray dot between them - not a version number
# ("2023.11.9.4"), whose second part is no amount.
GLUED_AMOUNTS_RE = re.compile(r"(\d[.,]\d{2})[.,](?=\d{1,4}[.,]\d{2}(?!\d|[.,]\d))")
# A weight has three decimals and says it is one: "kg" after it, or a price
# per unit on its line. A VAT table's column rule reads as a third decimal
# too ("0.711").
WEIGHT_RE = re.compile(r"(?<![\d.,])(?P<weight>\d{1,3}[.,]\d{3})(?!\d)(?P<kg>\s*kg\b)?", re.IGNORECASE)
CODE_BEFORE_RE = re.compile(r"(?<![A-Za-z])T(?P<code>\d)(?=\s*-?\s*€?\s*\d)")
CODE_AFTER_RE = re.compile(r"(?:\((?P<paren>\d)\)|\s(?P<letter>[A-D]))\s*$")
# "A 1x BQTE 30G": the code in front of the count.
CODE_LEAD_RE = re.compile(r"^\s*(?P<code>[A-Z])\s+(?=\d{1,3}\s*[xX×])")
INTEGER_RE = re.compile(r"(?<![\d.,])\d{1,3}(?![\d.,]?\d)")
# A French phone number: 0 and nine digits, in pairs or not. "10.49 31.47"
# has the shape of pairs too, and was taken for one.
PHONE_RE = re.compile(r"(?<![\d.,])0\d(?:[. ]?\d{2}){4}(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(\d{2})[/.-](\d{2})[/.-](\d{2,4})(?!\d)")
TIME_RE = re.compile(r"(?<!\d)\d{2}\s?[:H]\s?\d{2}(?!\d)")
LETTERS_RE = re.compile(r"[A-Za-zÀ-ÿ]")
# "EPICERIE/BOISSONS........", "FRUITS/LEGUMES.": a department heading -
# sometimes with the amount of the item under it glued on.
HEADING_RE = re.compile(
    r"^\s*(?P<name>[A-ZÀ-Ÿ][A-ZÀ-Ÿ/ '-]{3,}?)(?P<dots>\.+)\s*(?:(?P<amount>\d{1,4}[.,]\d{2})\s*(?:€|E|EUR)?)?\s*$"
)
# « Dont » - "of which" - opens a line printing a part of an amount already
# counted: "Dt Ecopart. unit. EcoMob 0.72" under a tool box at 24,90 (a DIY
# till abbreviates it), "- Dont DDS 0.20" under a bottle of acid, "Dont
# éco-part DEEE 0,10€". The charge is inside the item's price.
INCLUDED_RE = re.compile(r"(?i)^\W*(?:dont|dt)\b")
# Words a weight or count line prints around its figures - and the label of
# a quantity column ("Qté : 0,350 * 25,74  9,01" is a detail line, not an item).
NOISE_WORDS = {
    "eur", "kg", "pcs", "pc", "x", "man", "brutweight", "weight", "net", "@", "à", "poids", "brut",
    "qte", "qté", "quantite", "quantité", "qty",
}
# "0,350 * 25,74": a quantity that is a fraction - a measure sold by the
# unit, m² of plywood - with the sign that multiplies it. (With "kg", a weight.)
DECIMAL_COUNT_RE = re.compile(r"(?<![\d.,])(?P<count>\d{1,4}[.,]\d{1,3})\s*[xX×*]\s*(?=\d)")
# An EAN/GTIN: 8, 12, 13 or 14 digits, told from any other long number by its
# check digit (a ticket number fails it).
GTIN_RE = re.compile(r"(?<![\d.,])(\d{8}|\d{12,14})(?![\d.,]?\d)")
# The store's own article code in front of a name: "412233  VIS INOX".
LEAD_REFERENCE_RE = re.compile(r"^\s*\d{5,8}\s+(?=[^\d\s])")
# "1  5550001-ENCEINTE PORTABLE": a line naming the product whose code starts
# a row of figures further down ("5550001  120,00 €  A  100,00 €  120,00 €"),
# with the count in front.
LINKED_NAME_RE = re.compile(r"^\s*(?:(?P<count>\d{1,3})\s+)?(?P<code>\d{5,13})\s*[-–]\s*(?P<name>\S.*?)\s*$")
ROW_CODE_RE = re.compile(r"^\s*(?P<code>\d{5,13})\s+(?=\S)")
FOOTNOTE_MARKS_RE = re.compile(r"(?:\s*\(\d\))+\s*$")
# "3 ARTICLE(S)", "Nb articles : 3": how many articles the ticket counts.
ARTICLE_COUNT_RES = (
    re.compile(r"(?i)^\s*(\d{1,3})\s*articles?(?:\s*\(s\))?\s*$"),
    re.compile(r"(?i)\b(?:nb|nombre)\s*(?:d['’]\s*)?articles?\s*:?\s*(\d{1,3})\s*$"),
)
LEADING_NUMBER_RE = re.compile(r"^(?P<count>\d{1,3})\s+(?=[A-Za-zÀ-ÿ]{2})")
# "FACTURE N° P5200000012345", "Numéro Facture : 1234567".
# Ticket numbers, in the forms the tills print them.
TICKET_WORD_RE = re.compile(r"(?i)ticket\D{0,15}?(\d{4,10})(?!\d)")
# A ticket number this short is the till's count of the day: it comes round.
DAILY_COUNT_DIGITS = 4
STORE_TILL_RE = re.compile(r"R\d\s*(\d{5,6}-\d{2})\s*(\d{2,4})")
BARCODE_RE = re.compile(r"(?<!\d)(\d{18,26})(?!\d)")
# An IBAN is a long digit run too once its spaces are taken out, and it is
# the same one on every invoice a supplier sends: read as the document's
# number, the second invoice of the year was refused as a duplicate of the
# first. Taken out before anything else is looked for.
IBAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{2,4}){3,9}(?![A-Z0-9])")
# What a document calls its own reference, whatever word it uses for itself.
# Never "N° client" or "Référence contrat": those name the customer, and are
# the same on every document they send.
DOCUMENT_WORDS = r"(?:facture|document|commande|pi[eè]ce|avoir|bon\s+de\s+livraison)"
# What a document's own reference is made of: its characters, and a dash a
# PDF's rules left inside it ("FR-F033—763"), taken back out in _ticket_number.
REFERENCE = r"[A-Z0-9][A-Z0-9\-/\u2013\u2014]{3,}"
# "N°", "No", "Nº" with an ordinal indicator, "#".
NUMBER_MARK = r"(?:n\s*[°\u00bao]\.?|num[ée]ro|#)"
DOCUMENT_NUMBER_RES = (
    re.compile(rf"(?i)\b{DOCUMENT_WORDS}\s*{NUMBER_MARK}\s*:?\s*({REFERENCE})"),
    re.compile(rf"(?i)\bnum[ée]ro\s+(?:de\s+)?{DOCUMENT_WORDS}\s*:?\s*({REFERENCE})"),
    re.compile(rf"(?i)\b{NUMBER_MARK}\s*(?:de\s+|du\s+)?{DOCUMENT_WORDS}\s*:?\s*({REFERENCE})"),
    # "Référence interne: FA-202501-3025", the sender's own reference for the
    # document - a wine merchant's invoices changed that label for "N°
    # document" from one January to the next. Never "Référence unique de
    # mandat", the bank's and the same every month, nor "Votre référence
    # contrat", which names the customer.
    re.compile(rf"(?i)\br[ée]f[ée]rence\s+(?:interne|document|facture)\s*:?\s*({REFERENCE})"),
    # « N° 2026100000001 DU 10 AVRIL 2026 », « n°1400000001 du 19 Janvier
    # 2024 »: a number printed with its date is the document's own, whatever
    # stands between it and the word « facture » (« FACTURE TRIMESTRIELLE »
    # lines above it on a water bill, « Facture Freebox » on a box's). Not
    # found, a payment reference - a long digit run - was taken, or a number
    # made up from the date and the total. Last: a number beside « facture »
    # says more.
    re.compile(
        rf"(?i)\b{NUMBER_MARK}\s*:?\s*({REFERENCE})\s+du\s+\d{{1,2}}(?:er)?"
        r"(?:\s+[a-zéèêûôà]+\.?\s+|[/.-]\d{1,2}[/.-])\d{2,4}"
    ),
)


@dataclass
class Reading:
    """What one printed line says, before it is known to be an item."""

    index: int
    name: str | None
    total: Decimal | None = None
    count: int | None = None
    count_printed: bool = False
    weight: Decimal | None = None
    unit: Decimal | None = None
    code: str = ""
    # From a row printing its own tax: the rate its numbers prove, its HT.
    rate: Decimal | None = None
    ht: Decimal | None = None
    computed: bool = False
    vat_row: bool = False
    # A promotion printed under the item, TTC; the last one taken, which the
    # till may print again in HT.
    discount: Decimal = ZERO
    last_discount: Decimal = ZERO
    category: str = ""
    # The amount as first read, before a partial cancellation.
    read_total: Decimal | None = None
    ean: str = ""
    # The VAT rate a table row prints as its last column ("... 3,50  20,00"),
    # whether its amount turns out to be HT or TTC.
    row_rate: Decimal | None = None
    # An amount read in HT (a document whose rows add up to its HT base):
    # `total` is then worked out, not printed.
    ht_row: bool = False

    @property
    def explained(self) -> Decimal | None:
        """What count/weight x unit comes to."""
        if self.unit is None:
            return None
        if self.weight is not None:
            return (self.weight * self.unit).quantize(CENTS, rounding=ROUND_HALF_UP)
        if self.count is not None:
            return self.count * self.unit
        return None

    @property
    def net(self) -> Decimal:
        return (self.total or ZERO) - self.discount


def _money(match) -> Decimal:
    return money_value(match)


def _is_date_or_time(line: str) -> bool:
    """Whether the line is a date or a time, and so never an item.

    Only when that is *all* it is: an invoice's rows carry the period they
    cover ("Abonnement 01/08/2026au31/08/2026 1,00 64,44 20,00% 64,44"), and
    a line thrown away for holding a date left the subscription unread and
    its own "Total hors TVA" standing in for it.
    """
    if TIME_RE.search(line):
        # A till stamps the hour, and nothing it sells carries one: a
        # "TICKET ... DU 08/01/2024 10:26:09  Total TTC : 16,21 €" line is
        # the ticket saying what it came to.
        return True
    if not any(1 <= int(day) <= 31 and 1 <= int(month) <= 12 for day, month, _year in DATE_RE.findall(line)):
        return False
    return not line_amounts(line)


def _inside(position: int, spans) -> bool:
    return any(start <= position < end for start, end in spans)


def is_gtin(code: str) -> bool:
    """Whether `code` is an EAN-8/UPC/EAN-13/GTIN-14 with a valid check digit."""
    if len(code) not in (8, 12, 13, 14) or not code.isdigit():
        return False
    digits = [int(char) for char in code]
    weighted = sum(digit * (3 if position % 2 == 0 else 1) for position, digit in enumerate(reversed(digits[:-1])))
    return (10 - weighted % 10) % 10 == digits[-1]


def read_line(index: int, line: str, total: Decimal | None = None) -> Reading | None:
    """A line's name, figures and code - or None for a line that is never
    an item (a date, a phone number)."""
    reading = _read_line(index, line, total)
    if reading is not None:
        reading.name = _without_leading_count(reading.name, reading.count)
    return reading


def _without_leading_count(name: str | None, count: int | None) -> str | None:
    """"12 BOUTEILLE(S) CHAMPAGNE 75 CL" is twelve of "BOUTEILLE(S) CHAMPAGNE
    75 CL", not a product of its own: a count in front of the name is the
    quantity column, and left in, the same champagne bought by six and by
    twelve makes two products that never meet. The count as a whole number
    only: "2000123 GOBELET" begins with an article number, not with a count
    of 2, and cut there the product lost the first digit of its code."""
    if not name or not count or count < 2:
        return name
    rest = name[len(str(count)):].lstrip(" .:-xX\u00d7")
    leads = re.match(rf"{re.escape(str(count))}(?![\d.,])", name)
    return rest if leads and LETTERS_RE.search(rest) else name


def _is_size(line: str, count_match, rates) -> bool:
    """Whether the count `COUNT_RE` found opens one word of two whole numbers
    ("60X40") that is a size: the money decides, and a size multiplies into
    no amount the line prints ("2x4 8,00" is two at 4,00)."""
    size = GLUED_SIZE_RE.match(line, count_match.start())
    if size is None:
        return False
    product = int(size.group("count")) * int(size.group("by"))
    return not any(
        _money(match) == product for match in MONEY_RE.finditer(line) if not _inside(match.start("units"), rates)
    )


def _read_line(index: int, line: str, total: Decimal | None = None) -> Reading | None:
    ean = ""
    for match in GTIN_RE.finditer(line):
        if is_gtin(match.group(1)):
            ean = ean or match.group(1)
            line = line[: match.start()] + " " * len(match.group(1)) + line[match.end():]
    line = LEAD_REFERENCE_RE.sub("", line)
    line = GLUED_COUNT_RE.sub(r"\1 \2X ", line)
    line = GLUED_CODE_RE.sub(r"T\1 ", line)
    line = GLUED_AMOUNTS_RE.sub(r"\1 ", line)
    if PHONE_RE.search(line) or _is_date_or_time(line):
        return None
    rate = read_rate(line)
    if rate is not None:
        summary = parse_vat_line(line, rate=rate, expected_total=total)
        if summary is not None and summary.vat_amount is not None and not summary.derived and (
            summary.base is not None or summary.total_ttc is not None
        ):
            return Reading(index=index, name=None, vat_row=True)

    # A percentage is not money - but it can be part of a name ("20% MG").
    rates = [match.span() for match in RATE_RE.finditer(line)]
    taken = []  # spans of figures that are not money: they end the name
    lead = CODE_LEAD_RE.match(line)
    count = None
    count_match = next((match for match in COUNT_RE.finditer(line) if not _is_size(line, match, rates)), None)
    if count_match and not _inside(count_match.start(), rates):
        count = int(count_match.group("count"))
        taken.append(count_match.span())
    else:
        count_match = None
    per_unit = PER_UNIT_RE.search(line)
    weight = None
    fraction = DECIMAL_COUNT_RE.search(line)
    # "3.850kg x 2.99/kg" is a weight, "kg" glued to it or not.
    if fraction and count is None and not re.search(r"(?i)kg\b", line) and not _inside(fraction.start(), rates):
        count = Decimal(fraction.group("count").replace(",", "."))
        taken.append(fraction.span("count"))
        if count == count.to_integral_value():
            count = int(count)
    for match in WEIGHT_RE.finditer(line):
        if _inside(match.start(), taken + rates):
            continue
        if match.group("kg") or per_unit:
            weight = Decimal(match.group("weight").replace(",", "."))
            taken.append(match.span())
            break
    amounts = []  # (position, value, is the price per unit)
    for match in MONEY_RE.finditer(line):
        # The digits, not the sign: "-14,99%" is a percentage too.
        if _inside(match.start("units"), taken + rates):
            continue
        is_per_unit = bool(per_unit and per_unit.start() <= match.start("units") < per_unit.end())
        amounts.append((match.start(), _money(match), is_per_unit))
    amounts = _without_restated_ht(amounts, line)
    integers = [
        (match.start(), int(match.group()))
        for match in INTEGER_RE.finditer(line)
        if not _inside(match.start(), taken + rates)
    ]
    code_before = CODE_BEFORE_RE.search(line)

    # The name runs up to the first figure; a count at the very start (after
    # a code) is a prefix, "4X BASILIC".
    prefix_end = 0
    if count_match and count_match.start() <= (lead.end() if lead else 1) and LETTERS_RE.search(line[count_match.end():]):
        prefix_end = count_match.end()
    tail_starts = [span[0] for span in taken if span[0] >= prefix_end]
    tail_starts += [start for start, _v, _p in amounts if start >= prefix_end]
    if code_before and code_before.start() >= prefix_end:
        tail_starts.append(code_before.start())
    name_end = min(tail_starts) if tail_starts else len(line)
    name_text = BANNER_RE.sub(" ", line[prefix_end:name_end])
    name_text = re.sub(r"(?:\s+\d{1,3})+\s*$", "", name_text)  # "BASILIC FRAIS 1", "DADDY 71"
    words = [word for word in re.split(r"\s+", name_text.strip(" .:-#*+|")) if word]
    meaningful = [word for word in words if LETTERS_RE.search(word) and word.lower().strip(".:@,") not in NOISE_WORDS]
    name = " ".join(words) if sum(len(LETTERS_RE.findall(word)) for word in meaningful) >= 2 else None

    code = ""
    if code_before:
        code = "T" + code_before.group("code")
    elif lead and prefix_end:
        code = lead.group("code")
    else:
        after = CODE_AFTER_RE.search(line)
        if after:
            code = after.group("paren") or after.group("letter")

    reading = Reading(
        index=index, name=name, count=count, count_printed=count is not None, weight=weight, code=code, ean=ean
    )
    plain = [value for _s, value, is_per_unit in amounts if not is_per_unit]
    unit_values = [value for _s, value, is_per_unit in amounts if is_per_unit]
    if unit_values:
        reading.unit = unit_values[0]
    if len(plain) >= 4:
        both_ways = _ht_ttc_row(plain)
        if both_ways is not None:
            read_count, unit, reading.ht, reading.total, reading.rate = both_ways
            reading.unit, reading.count = unit, read_count
            reading.count_printed = reading.count_printed or any(
                value == read_count for _start, value in integers
            )
            return reading
    if len(plain) >= 3 or (len(plain) >= 2 and _rate_tokens(plain)):
        taxed = _taxed_row(plain)
        if taxed is not None:
            ht, ttc, rate, computed = taxed
            reading.total, reading.ht, reading.rate, reading.computed = ttc, ht, rate, computed
            rest = [value for value in plain if value not in (ht, ttc) and value != rate * 100]
            reading.unit, reading.count = _unit_and_count(ht, rest, [value for _s, value in integers], count)
            reading.count_printed = reading.count_printed or reading.count is not None
            return reading
        rated = _rated_row([(start, value) for start, value, is_per_unit in amounts if not is_per_unit], integers)
        if rated is not None:
            row_count, row_unit, reading.total, reading.row_rate = rated
            if row_count is not None:
                reading.count, reading.unit, reading.count_printed = row_count, row_unit, True
            return reading
    if name is None and count is not None and len(plain) == 1 and reading.unit is None and weight is None and not code:
        # "2 x 1.00EUR" under an item: the unit price explaining the total.
        reading.unit = plain[0]
        return reading
    if plain:
        reading.total = plain[-1]
        if len(plain) >= 2 and reading.unit is None:
            reading.unit = plain[-2]
    if reading.count is None and reading.unit and reading.total and integers:
        # "BQTE 1 1,89 1,89": an integer the unit price multiplies into the amount.
        for _start, value in integers:
            if value >= 2 and value * reading.unit == reading.total:
                reading.count = value
                break
        else:
            # "35,54  1  35,54": a quantity column of one, between the price
            # and the amount it makes. A 1 anywhere else says nothing, and two
            # equal amounts with words between them are a total restated.
            plain_starts = [start for start, _value, is_per_unit in amounts if not is_per_unit]
            if (
                len(plain_starts) >= 2
                and reading.unit == reading.total
                and any(value == 1 and plain_starts[-2] < start < plain_starts[-1] for start, value in integers)
            ):
                reading.count = 1
    return reading


def _without_restated_ht(amounts: list[tuple], line: str) -> list[tuple]:
    """Drop an amount in brackets that is the one before it excluding tax:
    "Abonnement 19.99 (16.66)" is one price, printed both ways, not two.
    Only the brackets and the rate say so - 16,66 is no coincidence at 20%."""
    kept = []
    for position, entry in enumerate(amounts):
        start, value, _is_per_unit = entry
        bracketed = line[:start].rstrip().endswith("(") and line[start:].lstrip("-0123456789.,").lstrip().startswith(")")
        before = amounts[position - 1][1] if position else None
        if (
            bracketed
            and before is not None
            and any(abs(to_ht(before, rate) - value) <= CENTS for rate in KNOWN_VAT_RATES)
        ):
            continue
        kept.append(entry)
    return kept


def _rate_tokens(values: list[Decimal]) -> list[Decimal]:
    return [rate for rate in KNOWN_VAT_RATES if rate * 100 in values]


def _taxed_row(values: list[Decimal]) -> tuple[Decimal, Decimal, Decimal, bool] | None:
    """(HT, TTC, rate, TTC worked out) when the row prints its own tax: an HT
    amount, its VAT at one French rate, and their sum - or, the sum illegible,
    the rate printed on the row and a pair that fits it."""
    positive = [value for value in values if value > 0]
    found = set()
    for ht, vat, ttc in permutations(positive, 3):
        if vat >= ht or abs(ht + vat - ttc) > CENTS:
            continue
        rates = [rate for rate in KNOWN_VAT_RATES if abs((ht * rate).quantize(CENTS, ROUND_HALF_UP) - vat) <= TAXED_ROW_TOLERANCE]
        if len(rates) == 1:
            found.add((ht, ttc, rates[0], False))
    if len(found) == 1:
        return found.pop()
    if found:
        return None
    for rate in _rate_tokens(positive):
        pairs = {
            (ht, vat)
            for ht, vat in permutations([value for value in positive if value != rate * 100], 2)
            if vat < ht and abs((ht * rate).quantize(CENTS, ROUND_HALF_UP) - vat) <= TAXED_ROW_TOLERANCE
        }
        if len(pairs) == 1:
            ht, vat = pairs.pop()
            return ht, ht + vat, rate, True
    return None


def _ht_ttc_row(values: list[Decimal]) -> tuple[int, Decimal, Decimal, Decimal, Decimal] | None:
    """(count, unit price HT, HT, TTC, rate) of a row printing its price and
    its amount each both ways - "18  4,50  5,40  81,00  97,20" - or None.

    There is no tax column, so nothing on the row adds up; what proves the
    reading is that one French rate turns both prices into both amounts and
    a whole number of them makes the amount. Only in HT: a unit price is
    rounded before it is multiplied, so 24 bottles at 5,80 TTC are printed
    139,26 and not 139,20. Read as a bare list of amounts, a wine
    merchant's eighteen bottles came out as a single one at 81,00 - the
    quantity is what every unit cost downstream is divided by.
    """
    positive = sorted({value for value in values if value > 0})
    found = set()
    for rate in KNOWN_VAT_RATES:
        pairs = [
            (ht, ttc)
            for ht in positive
            for ttc in positive
            if ht < ttc and (ht * (1 + rate)).quantize(CENTS, ROUND_HALF_UP) == ttc
        ]
        for unit_ht, unit_ttc in pairs:
            for ht, ttc in pairs:
                if ht <= unit_ht:
                    continue
                count = ht / unit_ht
                if count != count.to_integral_value() or count < 2:
                    continue
                found.add((int(count), unit_ht, ht, ttc, rate))
    return found.pop() if len(found) == 1 else None


def _rated_row(amounts: list[tuple[int, Decimal]], integers: list[tuple[int, int]]):
    """(count, unit price, amount, rate) of a table row whose last column is
    its VAT rate as a plain number - "1  3,50  0,00  3,50  3,50  20,00",
    "0,35  21,45  0,00  21,45  7,51  20,00" - or None. The amount is the last
    figure before the rate; the count and unit price, the first pair printed
    before it that multiplies into it (a quantity can be a fraction). Count
    and price are None when no pair does."""
    numbers = sorted(amounts + [(start, Decimal(value)) for start, value in integers])
    if len(numbers) < 3:
        return None
    rate_at, last = numbers[-1]
    rate = next((known for known in KNOWN_VAT_RATES if known * 100 == last), None)
    money = [(start, value) for start, value in amounts if start < rate_at]
    if rate is None or len(money) < 2:
        return None
    amount_at, amount = money[-1]
    if amount <= 0:
        return None
    before = [(start, value) for start, value in numbers if start < amount_at]
    for position, (_start, count) in enumerate(before):
        if not 0 < count < 10000:
            continue
        for _unit_start, unit in before[position + 1 :]:
            if unit > 0 and abs(count * unit - amount) <= max(CENTS, count * HALF_CENT):
                if count == count.to_integral_value():
                    count = int(count)
                return count, unit, amount, rate
    return None, None, amount, rate


def _unit_and_count(ht: Decimal, rest: list[Decimal], integers: list[int], count: int | None):
    """The unit price and count of a taxed row: the integer that multiplies
    one of its other amounts into its HT."""
    for value in integers:
        for unit in rest:
            if value >= 1 and value * unit == ht:
                return unit, value
    if count is not None:
        return (rest[0] if rest else None), count
    return (ht if ht in rest else None), None


def _similar(a: str | None, b: str | None) -> float:
    """How alike two readings of a name are, 0-100: letter by letter, or word
    by word in any order."""
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b, processor=default_process), fuzz.token_sort_ratio(a, b, processor=default_process))


class GenericReceiptParser(ReceiptParser):
    """Reads any shop's ticket; `shop` says which shop it is filed under."""

    def __init__(self, shop: TicketShop):
        self.shop = shop
        self.supplier_code = shop.supplier_code
        self.header_patterns = shop.header_patterns

    def parse_pages(self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = "") -> ParsedInvoice:
        lines = [line for page in pages for line in page.text.split("\n") if line.strip()]
        total = printed_total(lines)
        vat_rows = {index for index in range(len(lines)) if unmarked_vat_row(lines, index) is not None}
        vat_summaries, rate_only = collect_vat_summaries(lines, total)
        totals = ReceiptTotals(printed_total_ttc=total, vat_summaries=vat_summaries, rate_only=rate_only)
        # No VAT table: maybe an HT and a tax printed with no rate beside them.
        untabled = untabled_vat(lines, total) if not vat_summaries and not rate_only else []

        first = segment = self._segment(lines, total, totals, vat_rows, 0, untabled=untabled)
        # Items read up to a total that none of them makes: the document may
        # print its lines again further down - the invoice under the till's
        # ticket, both on one photo, the ticket's half misread. What is found
        # there has to be a table, though: after the total, "CB 0,98" alone
        # also makes what was paid.
        while segment.chosen is None and segment.end + 1 < len(lines):
            segment = self._segment(
                lines, total, totals, vat_rows, segment.end + 1, structured_only=True, untabled=untabled
            )
        if segment.chosen is None:
            segment = first
            segment.chosen = self._repaired(first.items, lines, total, first.notes) if first.items else []
        items, read, notes = segment.chosen, segment.items, segment.notes
        if segment.totals is not None:
            totals = segment.totals
        weight_problems, quantity_fixes, quantity_problems = (
            segment.weight_problems, segment.quantity_fixes, segment.quantity_problems
        )
        items_end = 0
        set_aside = [item for item in read if item not in items]
        if items:
            items_end = max(items_end, items[-1].index + 1)
        promotion = printed_promotion(lines, items_end, total)
        _count_from_articles(items, lines)
        for item in items:
            if item.count_printed and item.unit is not None and item.weight is None and item.total is not None:
                reference = item.ht if item.ht is not None else item.total
                item.count = reconcile_quantity(
                    item.name or UNREAD_NAME, item.count, item.unit, reference, quantity_fixes, quantity_problems
                )
        gross = sum((item.total for item in items), start=ZERO)

        extra: list[ParseCheck] = quantity_checks(quantity_fixes, quantity_problems)
        if segment.totals is not None:
            (summary,) = totals.vat_summaries
            extra.append(ParseCheck(
                label="Taux déduit",
                passed=True,
                detail=f"aucun taux imprimé : {format_rate(summary.rate)} %, la TVA {summary.vat_amount:.2f} € "
                f"étant ce taux de {summary.base:.2f} € HT, et les deux faisant le total",
            ))
        if weight_problems:
            extra.append(ParseCheck(label="Poids rattachés", passed=False, detail="; ".join(weight_problems)))

        discount_check = self._promotions(items, lines, total, gross)
        rates, rate_check = self._rates(items, totals)
        if rate_check is not None:
            extra.append(rate_check)

        parsed_lines = []
        for item in items:
            parsed_lines.append(self._line(item, rates[id(item)]))
        if discount_check is not None:
            extra.append(discount_check)
        if notes:
            extra.append(
                ParseCheck(
                    label="Montants recalculés",
                    passed=True,
                    detail="montant illisible ou mal lu, déduit du ticket : " + "; ".join(notes),
                )
            )
        missing = missing_item_check(gross, promotion)
        if missing is not None:
            extra.append(missing)
        if set_aside:
            extra.append(
                ParseCheck(
                    label="Lignes écartées",
                    passed=True,
                    detail="lues mais hors des articles (en-tête, total mal lu) : "
                    + "; ".join(f"{item.name or UNREAD_NAME} {item.total:.2f} €" for item in set_aside),
                )
            )

        text = "\n".join(lines)
        invoice_date = read_date(text, date_hint)
        lines_total_ttc = sum((item.net for item in items), start=ZERO)
        lines_total_ht = sum((line.total_ht for line in parsed_lines), start=ZERO)
        checks, adjustment = build_checks(
            totals, lines_total_ttc, lines_total_ht=lines_total_ht, line_count=len(parsed_lines), extra=extra
        )
        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=compose_invoice_number(_ticket_number(text, invoice_date), invoice_date, total),
            invoice_date=invoice_date,
            lines=parsed_lines,
            reconciliation_adjustment=adjustment,
            checks=checks,
            printed_total_ttc=total,
            vat_breakdown=[
                (summary.rate, summary.base, summary.vat_amount)
                for summary in (summary.resolve() for summary in totals.vat_summaries)
                if not summary.derived and summary.base is not None and summary.vat_amount is not None
            ],
        )

    def _segment(self, lines, total, totals, vat_rows, start, structured_only=False, untabled=()) -> Segment:
        """The items read from `start` up to the next total, and the run of
        them that is the purchase (None when none adds up).

        `untabled` are the readings of an HT and a tax printed with no rate
        (untabled_vat): one is taken only for rows in HT printed above both
        lines - it then stands for the VAT table (`segment.totals`)."""
        segment = Segment()
        items, details, voids, segment.end = self._read_items(lines, total, segment.notes, vat_rows, start)
        self._attach_details(
            items, details, voids, segment.weight_problems, segment.quantity_fixes, segment.quantity_problems
        )
        if self.shop.coded_items and not any(item.code for item in items):
            # Only a line whose count and unit price make its amount is an
            # item then: "7 X 0.49 3.43" is one, its "T1" read "11".
            items = [item for item in items if item.explained is not None and abs(item.explained - item.total) <= CENTS]
        segment.items = items
        ttc_run = self._fitting_run(items, lines, total)
        if ttc_run is not None and _is_vat_table(ttc_run, totals):
            ttc_run = None  # the document's own base and tax, read as lines
        if ttc_run is None and structured_only:
            # Further down the photo, only rows that say what makes their
            # amount are read - the rest is a charge included in one of them
            # ("Dont éco-part DEEE 0,02"), or a total.
            ttc_run = self._fitting_run([item for item in items if _explained(item)], lines, total)
        if structured_only:
            ttc_run = self._telling_run(items, lines, total, totals, ttc_run)
        choice = _ht_choice(items, ttc_run, totals)
        for candidate, printed_at in untabled if choice is None else ():
            choice = _ht_choice([item for item in items if item.index < min(printed_at)], ttc_run, candidate)
            if choice is not None:
                segment.totals = candidate
                break
        if choice is not None and structured_only and not all(_explained(item) for item in choice[0]):
            choice, segment.totals = None, None  # further down, a table or nothing
        if choice is None and untabled:
            # The HT and the tax printed under the items, with no rate: the
            # items are in TTC, and their rate is the one those two prove.
            for candidate, printed_at in untabled:
                run = self._fitting_run([item for item in items if item.index < min(printed_at)], lines, total)
                if run is not None and _proves_rate(run, candidate, lines, printed_at):
                    ttc_run, segment.totals = run, candidate
                    break
        if choice is not None:
            ht_run, ht_rates = choice
            _as_ht(ht_run, ht_rates)
            segment.chosen = ht_run
        elif ttc_run is not None:
            segment.chosen = ttc_run
        return segment

    def _telling_run(self, items, lines, total, totals, found):
        """Past a total, the run has to say something the total does not: be a
        table (`_structured`), or a price and the discount under it
        (`_priced_detail`). A line repeating the amount paid - a payment, the
        heading of a section - is passed over, and what follows it tried."""
        rest = items
        while found is not None:
            if _structured(found) or _priced_detail(found, totals):
                return found
            rest = rest[rest.index(found[0]) + 1 :]
            found = self._fitting_run(rest, lines, total)
        return None

    # -- reading -----------------------------------------------------------

    def _read_items(self, lines, total, notes, vat_rows=frozenset(), start=0):
        items: list[Reading] = []
        details: list[Reading] = []
        voids: list[Decimal] = []
        pending: Reading | None = None
        category = ""
        items_end = len(lines)

        def sums():
            gross = sum((item.total for item in items), start=ZERO)
            return gross, gross - sum((item.discount for item in items), start=ZERO)

        orphan: tuple[int, Decimal] | None = None  # an amount glued to a heading
        heading_at = -2
        subtotal_at = -1  # the last sub-total stepped over
        named_codes: dict[str, tuple[str, str | None]] = {}  # code -> (name, count)
        for index, line in enumerate(lines):
            if index < start:
                continue
            if INCLUDED_RE.match(line):
                # A part of the item above, never an item: read as one, the
                # eco-participation of each tool box made the lines 0,92 more
                # than what was paid; a cent of it made the one item 6,00 for
                # 5,99, close enough to pass; and 0,29 of it under a shelf
                # kept the sub-total under it from restating the items, so
                # the sub-total came out as an item and the shelf did not.
                pending = None
                continue
            linked = LINKED_NAME_RE.match(line)
            if linked and not line_amounts(line):
                # "1  5550001-ENCEINTE PORTABLE XL": the row of figures further
                # down starts with that code, and has no name of its own.
                named_codes[linked.group("code")] = (
                    FOOTNOTE_MARKS_RE.sub("", linked.group("name")), linked.group("count")
                )
            heading = HEADING_RE.match(line)
            if heading and (len(heading.group("dots")) >= 3 or "/" in heading.group("name")):
                category, pending, heading_at = heading.group("name").strip(" ."), None, index
                amount = heading.group("amount")
                orphan = (index, Decimal(amount.replace(",", "."))) if amount else None
                continue
            reading = read_line(index, line, total) if index not in vat_rows else None
            if reading is None or reading.vat_row:
                pending = None
                continue
            reading.category = category
            if reading.name is None and reading.total is not None:
                _take_linked_name(reading, line, named_codes)
            amount = reading.total
            if amount is not None and amount == 0:
                pending = None
                continue
            if amount is not None and amount < 0:
                pending = None
                self._negative(reading, items, voids)
                continue
            gross, net = sums()
            if amount is not None and items and not reading.code:
                # A coded line is an item. An amount standing alone ends the
                # items only as the amount paid: after a cancelled item, the
                # running sum is repeated by the till's own reminder.
                if reading.name is None and total is not None:
                    is_end = abs(amount - total) <= CENTS
                else:
                    is_end = ends_items(amount, len(items), gross, net, total)
                if is_end:
                    items_end = index
                    break
            block = [item for item in items if item.index > subtotal_at]
            # The amount of the name above - on a row printing more than it
            # (the item's reference): an amount alone under its label is a
            # sub-total printed on two lines, and kept as the item « SOUS
            # TOTAL » it made what was paid with the next sale's items.
            names_above = (
                reading.name is None
                and pending is not None
                and pending.index == index - 1
                and re.search(r"\w", re.sub(r"(?i)€|\bEUR\b", " ", MONEY_RE.sub(" ", line))) is not None
            )
            if (
                amount is not None
                and not reading.code
                and not reading.ean
                and reading.unit is None
                and not names_above
                and (
                    (len(items) >= 2 and abs(amount - gross) <= CENTS)
                    or (len(block) >= 2 and abs(amount - sum((item.total for item in block), start=ZERO)) <= CENTS)
                )
            ):
                # A sub-total between the items: of all of them, or of those
                # since the last one - a DIY till prints a sale in blocks,
                # each ending on its own. Taken for an item, the sub-total
                # of a block in the middle cuts the items into runs none of
                # which makes what was paid. Never a row printing what no
                # sub-total prints - a VAT code, an EAN, a count and its unit
                # price - nor the amount of the name printed on the line
                # above it (a row with no name of its own under one): a
                # pack of four after two packs of two costs what they do, and
                # so can the cut of a worktop on the store's own reference;
                # stepped over, either left the block's own sub-total to be
                # read as the item, and every check passed.
                subtotal_at = index
                pending = None
                continue
            if reading.name and amount is None and reading.count is None and reading.weight is None and reading.unit is None:
                if orphan is not None and orphan[0] == index - 1:
                    # "FRUITS/LEGUMES. 2,51" over "POMME ZELI": the item's
                    # amount landed on its heading.
                    reading.total = reading.read_total = orphan[1]
                    items.append(reading)
                    orphan, pending = None, None
                    continue
                if reading.unit is not None and details and details[-1].index == index - 1 and details[-1].unit is None:
                    details[-1].unit = reading.unit
                pending = reading
                continue
            if amount is None:
                if reading.unit is not None and reading.count is None and reading.weight is None:
                    # "@ 3.49 / KG" on its own line, under its weight.
                    if details and details[-1].index == index - 1 and details[-1].unit is None:
                        details[-1].unit = reading.unit
                    pending = None
                    continue
                if pending is not None and pending.index == index - 1 and reading.explained is not None:
                    # "MENTHE" then "2x 0.50EUR", its amount faded: worked out.
                    reading.name, reading.total, reading.computed = pending.name, reading.explained, True
                    reading.category = pending.category
                    notes.append(f"{reading.name} {reading.total:.2f} € ({reading.count or reading.weight} x {reading.unit} €)")
                    items.append(reading)
                    pending = None
                    continue
                if reading.count is not None or reading.weight is not None:
                    details.append(reading)  # "2 x 1.00", "3.850kg x 2.99/kg", "BRUTWEIGHT 0.920 KG"
                pending = None
                continue
            if (
                orphan is not None
                and orphan[0] == index - 1
                and reading.count
                and reading.unit is None
                and reading.count * amount == orphan[1]
            ):
                # "EPICERIE/BOISSONS. 6,70" over "2 X WORCESTER 3,35": the row
                # was split, its total landed on the heading.
                reading.unit, reading.total, amount = amount, orphan[1], orphan[1]
                reading.count_printed = True
            if reading.name is None and heading_at == index - 1 and not reading.code and not reading.count:
                reading.name = ""  # the item under a heading, its name not read
            if reading.name is None:
                if pending is not None and pending.index == index - 1:
                    reading.name = pending.name  # "Article divers" above "3pcs 0,80 2,40 A"
                    reading.ean = reading.ean or pending.ean
                elif (reading.count is not None or reading.weight is not None) and (
                    reading.explained is not None and items and abs(reading.explained - items[-1].total) <= CENTS
                ):
                    _apply(items[-1], reading)  # a detail repeating its item's amount
                    pending = None
                    continue
                elif not reading.code:
                    pending = None
                    continue  # an amount alone: a sub-total, a payment
            reading.read_total = amount
            if reading.computed:
                notes.append(f"{reading.name or UNREAD_NAME} {amount:.2f} € (HT {reading.ht:.2f} € + TVA {format_rate(reading.rate)} %)")
            items.append(reading)
            pending = None
        return items, details, voids, items_end

    def _negative(self, reading: Reading, items: list[Reading], voids: list[Decimal]) -> None:
        """A negative amount: an item cancelled ("NUL LIGNE" then the item at
        minus its price), part of one, or a promotion under the item above."""
        amount = -reading.total
        voids.append(amount)
        if reading.name:
            for item in reversed(items):
                if item.total == amount and _similar(item.name, reading.name) >= VOID_NAME_MATCH:
                    items.remove(item)
                    return
            for item in reversed(items):
                if _similar(item.name, reading.name) >= VOID_NAME_MATCH:
                    item.total -= amount
                    if item.total == 0:
                        items.remove(item)
                    return
        voids.pop()
        if not items:
            return
        item = items[-1]
        if reading.unit is not None and abs(reading.unit - amount - item.total) <= CENTS:
            # "sur 11,76 soit -1,76" under a row of 10,00: the row is already
            # net of the discount it details.
            return
        if item.last_discount and any(
            abs(to_ht(item.last_discount, rate) - amount) <= CENTS for rate in KNOWN_VAT_RATES
        ):
            return  # the discount just taken, printed again in HT
        # Never beyond what the item costs: the rest, misread or not, is left
        # for the sums to report.
        taken = max(min(amount, item.net), ZERO)
        item.discount += taken
        item.last_discount = taken

    def _attach_details(self, items, details, voids, weight_problems, fixes, problems):
        """A detail line explains a neighbouring item: the one whose amount
        its count or weight times its unit price makes."""
        for detail in details:
            expected = detail.explained
            near = sorted(items, key=lambda item: (abs(item.index - detail.index), -item.index))
            if expected is not None:
                match = next((item for item in near[:2] if abs((item.read_total or item.total) - expected) <= CENTS), None)
                if match is not None:
                    if match.read_total == match.total:
                        _apply(match, detail)
                    continue
                if any(abs(amount - expected) <= CENTS for amount in voids):
                    continue  # the detail of a cancelled item
            below = next((item for item in items if item.index > detail.index), None)
            above = next((item for item in reversed(items) if item.index < detail.index), None)
            if detail.weight is not None:
                target = below if below is not None and below.index <= detail.index + 2 else above
                if target is None:
                    continue
                if expected is not None:
                    weight_problems.append(
                        f"{detail.weight} kg x {detail.unit} €/kg = {expected} € : aucun article voisin à ce prix"
                    )
                target.weight = detail.weight
            elif detail.count is not None and above is not None and above.index == detail.index - 1:
                # "5 x 0.50" under "MENTHE 1.50": the money says 3.
                above.count = reconcile_quantity(
                    above.name or UNREAD_NAME, detail.count, detail.unit, above.total, fixes, problems
                ) if detail.unit is not None else detail.count

    def _select(self, items, lines, total, notes):
        """The items that are the purchase: the longest run adding up to what
        was paid, or to a pre-discount total the ticket prints after them -
        dropping header lines and misread totals. Failing that, one
        arithmetic repair; failing that, everything read."""
        if not items:
            return items
        run = self._fitting_run(items, lines, total)
        if run is not None:
            return run
        return self._repaired(items, lines, total, notes)

    @staticmethod
    def _anchors(items, lines, total):
        anchors = []  # (amount the run must reach, line printing it or None, firm)
        if total is not None:
            anchors.append((total, None, True))
            anchors += _pre_discount_totals(lines, items[0].index, total)
        # A pre-discount total whose discount is printed twice is proof: the
        # items reach it, and two loaves happening to make what was paid on a
        # "3 pour 2" ticket are not the purchase.
        proven = [anchor for anchor in anchors[1:] if anchor[2]]
        return proven or anchors

    def _fitting_run(self, items, lines, total):
        """The longest run of items adding up to what was paid (or a total
        before promotions), or None. Changes nothing."""
        if not items:
            return None
        gross_targets = self._anchors(items, lines, total)

        def fits(run):
            gross = sum((item.total for item in run), start=ZERO)
            net = gross - sum((item.discount for item in run), start=ZERO)
            if net != gross and total is not None and abs(net - total) <= CENTS:
                return True  # promotions printed under their items
            return any(
                abs(gross - target) <= CENTS and (line is None or line > run[-1].index)
                for target, line, _firm in gross_targets
            )

        candidates = [items]
        coded = [item for item in items if item.code]
        if 2 <= len(coded) < len(items):
            candidates.append(coded)
        for candidate in candidates:
            size = len(candidate)
            for length in range(size, 0, -1):
                for start in range(size - length + 1):
                    run = candidate[start : start + length]
                    if fits(run):
                        return run
        return None

    def _repaired(self, items, lines, total, notes):
        gross_targets = self._anchors(items, lines, total)
        # A repair needs a firm target: what was paid, or a pre-discount total
        # whose discount the ticket prints twice.
        for target, line, firm in sorted(gross_targets, key=lambda anchor: anchor[1] is None):
            if not firm:
                continue
            before = [item for item in items if line is None or item.index < line]
            # Uncoded lines at either end of the run may be totals read in
            # among the items ("SOUSOTA 4.25").
            head = next((position for position, item in enumerate(before) if item.code), 0)
            tail = next((len(before) - position for position, item in enumerate(reversed(before)) if item.code), len(before))
            for run in (before, before[:tail], before[head:], before[head:tail]):
                if not run:
                    continue
                repaired = _repair(run, target)
                if repaired is not None:
                    notes.append(repaired)
                    return run
        return items

    # -- money -------------------------------------------------------------

    def _promotions(self, items, lines, total, gross) -> ParseCheck | None:
        """Charge promotions to their products. One printed under its item is
        that item's, always. The rest - what the items add up to beyond what
        was paid - only once the ticket prints the items' sum, and it goes to
        the products its promotion block names."""
        if not items:
            return None
        under = sum((item.discount for item in items), start=ZERO)
        if total is None or gross - under - total <= CENTS:
            if not under:
                return None
            return ParseCheck(
                label="Remise attribuée",
                passed=True,
                detail=f"remise {under:.2f} € imputée à l'article sous lequel elle est imprimée",
            )
        discount = gross - under - total
        gross_line = amount_printed(lines, gross, items[-1].index + 1)
        if gross_line is None:
            return None
        paid_line = amount_printed(lines, total, gross_line + 1)
        names = discounted_names(lines, gross_line, paid_line)
        targets = _discount_targets(items, names, discount)
        attributed = targets is not None
        _spread(targets or [item for item in items if item.net > 0], discount)
        if attributed:
            products = ", ".join(sorted({item.name or UNREAD_NAME for item in targets}))
            detail = f"remise {discount:.2f} € répartie sur : {products}"
        else:
            detail = f"remise {discount:.2f} € répartie au prorata : le produit concerné n'a pas été identifié"
        return ParseCheck(label="Remise attribuée", passed=attributed, detail=detail)

    def _rates(self, items, totals: ReceiptTotals):
        """Each item's rate (keyed by id), and a failed check when some had to
        be assumed."""
        rates: dict[int, Decimal] = {}
        loose = []
        for item in items:
            if item.rate is not None:
                rates[id(item)] = item.rate
            else:
                loose.append(item)
        if not loose:
            return rates, None
        summaries = totals.vat_summaries
        coded_totals: dict[str, Decimal] = {}
        for item in loose:
            coded_totals[item.code] = coded_totals.get(item.code, ZERO) + item.net
        by_code, confident = ({}, False)
        if any(coded_totals) and "" not in coded_totals:
            by_code, confident = assign_rates_by_bucket(coded_totals, summaries)
            if not by_code and len(coded_totals) == 1 and totals.line_rate is not None:
                by_code, confident = {code: totals.line_rate for code in coded_totals}, True
        elif totals.line_rate is not None:
            by_code, confident = {code: totals.line_rate for code in coded_totals}, True
        elif len(summaries) >= 2:
            split = _split_by_buckets(loose, summaries)
            if split is not None:
                rates.update(split)
                return rates, None
        assumed = False
        for item in loose:
            rate = by_code.get(item.code)
            if rate is None:
                rate, assumed = DEFAULT_TICKET_RATE, True
            rates[id(item)] = rate
        if assumed:
            return rates, ParseCheck(
                label="Taux par article",
                passed=False,
                detail="Aucun taux de TVA n'a pu être rattaché aux articles : 5.5 % supposé, à vérifier.",
            )
        if not confident:
            return rates, ParseCheck(
                label="Taux par article",
                passed=False,
                detail="Ticket à plusieurs taux : le rattachement des articles aux taux n'est pas certain.",
            )
        return rates, None

    def _line(self, item: Reading, rate: Decimal) -> ParsedLine:
        name = item.name or None
        placeholder = name is None
        for known in self.shop.placeholder_names:
            if name and _similar(name, known) >= PLACEHOLDER_NAME_MATCH:
                name, placeholder = known, True
        quantity = item.count if item.count and item.weight is None else 1
        if item.total < 0:
            quantity = -abs(quantity)
        net = item.net
        if item.ht is not None and not item.discount:
            total_ht = item.ht
        else:
            total_ht = to_ht(net, rate)
        line = ParsedLine(
            raw_name=name or UNREAD_NAME,
            quantity=quantity,
            total_volume=item.weight if item.weight is not None and item.total > 0 else ZERO,
            unit_cost_ht=(total_ht / quantity).quantize(UNIT, rounding=ROUND_HALF_UP),
            total_ht=total_ht,
            vat_rate=rate,
            category=item.category or self.shop.label,
            is_placeholder=placeholder,
            printed_ttc=None if item.ht_row else item.total,
            discount_ttc=item.discount,
            ean=item.ean,
        )
        if item.discount:
            line.discount = to_ht(item.total, rate) - total_ht
        return line


@dataclass
class Segment:
    """One block of items, read up to a total: what was read, the run that is
    the purchase, and what reading them had to say."""

    items: list = field(default_factory=list)
    chosen: list | None = None
    end: int = 0
    notes: list = field(default_factory=list)
    weight_problems: list = field(default_factory=list)
    quantity_fixes: list = field(default_factory=list)
    quantity_problems: list = field(default_factory=list)
    # The VAT table an HT and a tax printed without a rate stand for, when the
    # chosen rows are read in HT through it.
    totals: ReceiptTotals | None = None


def _take_linked_name(reading: Reading, line: str, named_codes: dict[str, tuple[str, str | None]]) -> None:
    """A row of figures starting with a product code takes the name - and the
    count - of the line that named that code above it."""
    row = ROW_CODE_RE.match(line)
    if row is None:
        return
    found = named_codes.get(row.group("code"))
    if found is None:
        return
    name, count = found
    reading.name = name
    if count is not None and reading.count is None:
        reading.count, reading.count_printed = int(count), True
        # The unit price is then the amount its count makes the row's total of.
        amounts = line_amounts(line)
        reading.unit = next(
            (value for value in amounts[:-1] if value > 0 and reading.count * value == reading.total), reading.unit
        )


def _explained(item: Reading) -> bool:
    """Whether the row says what makes its amount: a count or a weight times
    a price, an EAN, its own rate."""
    return bool(
        item.ean
        or item.row_rate is not None
        or (item.explained is not None and item.total is not None and abs(item.explained - item.total) <= CENTS)
    )


def _structured(run: list[Reading]) -> bool:
    """Lines that are a table, not a payment: at least two, each explained by
    its count or weight times its price, or carrying an EAN or its rate."""
    return len(run) >= 2 and all(_explained(item) for item in run)


def _apply(item: Reading, detail: Reading) -> None:
    if detail.weight is not None:
        item.weight = detail.weight
    elif detail.count is not None:
        item.count = detail.count
        item.count_printed = True
    if detail.unit is not None:
        item.unit = detail.unit


def _repair(items: list[Reading], target: Decimal) -> str | None:
    """One amount corrected so the items reach `target`, when exactly one
    correction does: an item priced like its namesakes, or an amount whose
    leading digit was a VAT code."""
    gap = target - sum((item.total for item in items), start=ZERO)
    fixes = []
    for item in items:
        for other in items:
            if other is item or other.total == item.total or (other.count or 1) != (item.count or 1):
                continue
            if other.total - item.total == gap and _similar(item.name, other.name) >= VOID_NAME_MATCH:
                fixes.append((item, other.total, f"{item.name} lu {item.total:.2f} €, {other.total:.2f} € comme {other.name}"))
                break
        text = f"{item.total:.2f}"
        for cut in (1, 2):
            if len(text) - cut >= 4:
                shorter = Decimal(text[cut:])
                if shorter - item.total == gap:
                    fixes.append((item, shorter, f"{item.name or UNREAD_NAME} lu {item.total:.2f} €, {shorter:.2f} € (code TVA lu comme un chiffre)"))
    candidates = {(id(item), value) for item, value, _note in fixes}
    if len(candidates) != 1:
        return None
    item, value, note = fixes[0]
    item.total = item.read_total = value
    return note


def _pre_discount_totals(lines: list[str], start: int, total: Decimal):
    """(amount, line, proven) for every amount above the one paid, printed
    from `start` on, whose difference from it is printed on a later line: a
    total before promotions. Proven when that discount is printed twice before
    the amount paid comes round again (see receipt_base.printed_promotion)."""
    amounts = [[abs(value) for value in line_amounts(line)] for line in lines]
    found = []
    seen = set()
    for index in range(start, len(lines)):
        for amount in amounts[index]:
            if amount - total <= CENTS or amount in seen:
                continue
            discount = amount - total
            later = [value for values in amounts[index + 1 :] for value in values]
            if discount not in later:
                continue
            between = []
            for values in amounts[index + 1 :]:
                if any(abs(value - total) <= CENTS for value in values):
                    break
                between += values
            seen.add(amount)
            found.append((amount, index, between.count(discount) >= 2))
    return found


def discounted_names(lines: list[str], start: int, end: int | None) -> list[str]:
    """Product names in the promotion block, between the line printing the
    items' sum and the next one printing the amount paid."""
    names = []
    for line in lines[start + 1 : end if end is not None else len(lines)]:
        without_amounts = re.sub(r"-?\d+[.,]\d{2}\S*", " ", line)  # "0.49Eur" with its currency
        for run in re.findall(r"[A-Za-zÀ-ÿ]{4,}(?:\s+[A-Za-zÀ-ÿ)(]{2,})*", without_amounts):
            if run.strip():
                names.append(run.strip())
    return names


def _discount_targets(items: list[Reading], names: list[str], discount: Decimal) -> list[Reading] | None:
    """The items a promotion names: the best match and those read as the same
    product - or None when nothing matches, or they cost less than the
    promotion takes off."""
    scores = {id(item): max((_similar(item.name, name) for name in names), default=0.0) for item in items}
    best = max(scores.values(), default=0.0)
    if best < DISCOUNT_NAME_FLOOR:
        return None
    floor = max(best - DISCOUNT_NAME_SPREAD, DISCOUNT_NAME_FLOOR)
    targets = [item for item in items if scores[id(item)] >= floor and item.net > 0]
    # The same product read another way on another line ("BAGLETTE BLAVD").
    targets += [
        item
        for item in items
        if item not in targets
        and item.net > 0
        and any(_similar(item.name, target.name) >= VOID_NAME_MATCH for target in targets)
    ]
    targets.sort(key=lambda item: item.index)
    if sum((item.net for item in targets), start=ZERO) < discount:
        return None
    return targets


def _spread(targets: list[Reading], discount: Decimal) -> None:
    """Share `discount` over `targets` pro rata, to the cent: each gets its
    share rounded down, and the cents left go to the largest remainders - six
    loaves share 0,98 as 0,17 0,17 0,16 0,16 0,16 0,16, not 0,16 x 5 and 0,18."""
    base = sum((item.net for item in targets), start=ZERO)
    if base <= 0 or discount > base:
        return
    exact = [discount * item.net / base for item in targets]
    shares = [value.quantize(CENTS, rounding=ROUND_DOWN) for value in exact]
    left = int((discount - sum(shares, start=ZERO)) / CENTS)
    by_remainder = sorted(range(len(targets)), key=lambda position: (shares[position] - exact[position], position))
    for position in by_remainder[:left]:
        shares[position] += CENTS
    for item, share in zip(targets, shares):
        item.discount += share


def _split_by_buckets(items: list[Reading], summaries, by_base: bool = False) -> dict[int, Decimal] | None:
    """Uncoded items on a two-rate ticket: the one way of splitting them
    whose sums are the two buckets (their TTC, or their HT base when the
    amounts are HT). Identical items are interchangeable."""
    resolved = [summary.resolve() for summary in summaries]
    if by_base:
        resolved = [
            VatSummary(rate=summary.rate, total_ttc=summary.base) if summary.base is not None else summary
            for summary in resolved
        ]
    if len(resolved) != 2 or any(summary.total_ttc is None for summary in resolved):
        return None
    groups: dict[tuple, list[Reading]] = {}
    for item in items:
        groups.setdefault((item.name, item.net), []).append(item)
    keys = list(groups)
    ways = 1
    for key in keys:
        ways *= len(groups[key]) + 1
    if ways > MAX_RATE_SPLITS:
        return None
    first, second = resolved
    found = []
    for choice in product(*(range(len(groups[key]) + 1) for key in keys)):
        in_first = sum((key[1] * taken for key, taken in zip(keys, choice)), start=ZERO)
        in_second = sum((key[1] * (len(groups[key]) - taken) for key, taken in zip(keys, choice)), start=ZERO)
        if abs(in_first - first.total_ttc) <= CENTS and abs(in_second - second.total_ttc) <= CENTS:
            found.append(choice)
            if len(found) > 1:
                return None
    if len(found) != 1:
        return None
    rates = {}
    for key, taken in zip(keys, found[0]):
        for position, item in enumerate(groups[key]):
            rates[id(item)] = first.rate if position < taken else second.rate
    return rates


def untabled_vat(lines: list[str], total: Decimal | None) -> list[tuple[ReceiptTotals, tuple[int, int]]]:
    """A document that prints its HT and its tax but no rate - a web shop's
    order page: two lines of one amount each that make what was paid, the
    second the first at exactly one French rate. Each such reading, as the
    VAT table it stands for, with the two lines. Only a hypothesis: two items
    of 10,00 and 2,00 fit it too (see GenericReceiptParser._segment)."""
    if total is None:
        return []
    singles = []
    for index, line in enumerate(lines):
        # One amount - or the same one twice, a label and its value printed
        # side by side ("Base ht  332,50  Total HT  332,50").
        amounts = set(line_amounts(line))
        if len(amounts) == 1 and (value := amounts.pop()) > 0:
            singles.append((index, value))
    found = []
    for base_at, base in singles:
        for vat_at, vat in singles:
            if vat_at == base_at or vat >= base or base + vat != total:
                continue
            rates = [
                rate for rate in KNOWN_VAT_RATES
                if abs((base * rate).quantize(CENTS, rounding=ROUND_HALF_UP) - vat) <= CENTS
            ]
            if len(rates) == 1:
                summary = VatSummary(rate=rates[0], base=base, vat_amount=vat)
                found.append((ReceiptTotals(printed_total_ttc=total, vat_summaries=[summary]), (base_at, vat_at)))
    return found


def _count_from_articles(items: list[Reading], lines: list[str]) -> None:
    """A number in front of a name is a count when the ticket says how many
    articles it sold and only that reading makes it up ("3 ARTICLE(S)" over
    "3 Acide citrique 500g  30.00"). A count printed as a count already, or
    figures that do not add up to what the ticket counted, change nothing -
    "3 MOUSQUETAIRES" is a name."""
    printed = None
    for line in lines:
        for pattern in ARTICLE_COUNT_RES:
            match = pattern.search(line)
            if match:
                printed = int(match.group(1))
    if printed is None or not items:
        return
    counted = sum(int(item.count) if item.count and item.weight is None else 1 for item in items)
    if counted == printed:
        return
    candidates = [
        (item, LEADING_NUMBER_RE.match(item.name))
        for item in items
        if item.name and not item.count_printed and item.count is None and item.weight is None
    ]
    candidates = [(item, match) for item, match in candidates if match]
    if len(candidates) != 1:
        return
    item, match = candidates[0]
    count = int(match.group("count"))
    if counted - 1 + count != printed or count < 2:
        return
    item.count, item.count_printed = count, True
    item.name = item.name[match.end():]


def _looks_like_a_row(item: Reading) -> bool:
    """Whether a line on its own reads as a row of a table rather than as one
    of the document's totals restated.

    A row has more figures than its amount: what makes it ("35,54  1  35,54"),
    a quantity ("8x Impression simple  20,08"), or a unit price beside it
    ("Impression simple  5  4,015  20,08"). A total restated prints its
    amount alone, or twice ("Base ht  607,49  Total HT  607,49"), and a
    payment line's other figure is a zero ("Carte  37,50  TOTAL HT  0,00").
    """
    return (
        _explained(item)
        or item.count_printed
        or (item.unit is not None and item.unit > 0 and item.unit != item.total)
    )


def _priced_detail(run: list[Reading], totals: ReceiptTotals) -> bool:
    """Whether a single row read past a total is the purchase itemised: its
    own price, less a discount printed under it, making what was paid - an
    invoice that prints its totals first and its lines below them ("Abonnement
    19.99", "Remise -10.00", for 9.99 paid).

    A line that simply repeats the amount paid proves nothing ("Espèces
    11,40"), and neither does its HT: at one rate, the tax-exclusive total is
    the same fact as the total, not a second one.
    """
    paid = totals.printed_total_ttc
    if paid is None or len(run) != 1 or not run[0].discount:
        return False
    return abs(run[0].net - paid) <= CENTS and abs(run[0].total - paid) > CENTS


def _is_vat_table(run: list[Reading], totals: ReceiptTotals) -> bool:
    """Whether a run adding up to what was paid is the document's own VAT
    table read as lines - its HT base on one, its tax on another ("Base ht
    54,99", "Mt TVA 11,00"). Nothing above them was the purchase, so it is
    read further down (a ticket under the invoice) or not at all."""
    summaries = [summary.resolve() for summary in totals.vat_summaries]
    bases = {summary.base for summary in summaries if summary.base is not None}
    taxes = {summary.vat_amount for summary in summaries if summary.vat_amount is not None}
    if len(run) < 2 or not bases or not taxes:
        return False
    amounts = [item.total for item in run]
    return (
        all(any(abs(amount - figure) <= CENTS for figure in bases | taxes) for amount in amounts)
        and any(any(abs(amount - base) <= CENTS for base in bases) for amount in amounts)
        and any(any(abs(amount - tax) <= CENTS for tax in taxes) for amount in amounts)
    )


def _proves_rate(run: list[Reading], candidate: ReceiptTotals, lines: list[str], printed_at: tuple[int, int]) -> bool:
    """Whether an HT and a tax printed with no rate (untabled_vat) are the
    VAT table of a run of items read in TTC: the run makes what they add up
    to, its own HT comes to theirs, and they are printed before the totals -
    a payment split in two ("CB 5,00", "ESPECES 1,00") fits the arithmetic
    too, but comes after the amount paid."""
    summary = candidate.vat_summaries[0]
    gross = sum((item.total for item in run), start=ZERO)
    if summary.base is None or abs(gross - (summary.base + summary.vat_amount)) > CENTS:
        return False
    worked_out = sum(
        (to_ht(item.total, summary.rate) for item in run), start=ZERO
    )
    if abs(worked_out - summary.base) > max(CENTS, len(run) * HALF_CENT):
        return False
    after_items = range(run[-1].index + 1, len(lines))
    paid_at = next(
        (index for index in after_items if any(abs(value - gross) <= CENTS for value in line_amounts(lines[index]))),
        None,
    )
    return paid_at is None or max(printed_at) < paid_at


def _ht_choice(items, ttc_run, totals: ReceiptTotals):
    """The rows in HT making the document's HT base, with their rates, when
    they are the purchase (_prefer_ht) - or None."""
    ht_run = _ht_run(items, printed_ht_base(totals))
    ht_rates = _ht_rates(ht_run, totals) if ht_run else None
    if ht_rates is not None and _prefer_ht(ht_run, ttc_run, totals, ht_rates):
        return ht_run, ht_rates
    return None


def _ht_run(items: list[Reading], ht_base: Decimal | None) -> list[Reading] | None:
    """The longest run of items whose amounts, as printed, make the
    document's own HT base to the cent: the rows of a document priced in HT.
    Exactly: an HT base is the sum of HT amounts, and TTC lines coming within
    a cent of it are a coincidence."""
    if ht_base is None or not items:
        return None
    # A line restating the base under the rows is the document's own total,
    # not a row ("Base ht  54,99  Total HT  54,99"): only the first line read,
    # or a line saying what makes its amount, is a run on its own.
    runs = []
    size = len(items)
    for length in range(size, 0, -1):
        for start in range(size - length + 1):
            run = items[start : start + length]
            if any(item.total is None for item in run):
                continue
            if length == 1 and run[0].total == ht_base and not _looks_like_a_row(run[0]):
                continue
            runs.append(run)
    for run in runs:
        if sum((item.total for item in run), start=ZERO) == ht_base:
            return run
    # An HT printed to four decimals is a cent away from its rows' sum
    # ("35,545" printed as "35,55"): only for rows that say what makes them.
    for run in runs:
        if all(_explained(item) for item in run) and abs(sum((item.total for item in run), start=ZERO) - ht_base) <= CENTS:
            return run
    return None


def _ht_rates(run: list[Reading], totals: ReceiptTotals) -> dict[int, Decimal] | None:
    """Each HT row's rate, by id: its own, the document's single one, or the
    one split of its buckets' HT bases - or None when a row gets none."""
    split = {}
    if any(item.row_rate is None for item in run) and totals.line_rate is None:
        split = _split_by_buckets(run, totals.vat_summaries, by_base=True) or {}
    rates = {}
    for item in run:
        rate = item.row_rate or totals.line_rate or split.get(id(item))
        if rate is None:
            return None
        rates[id(item)] = rate
    return rates


def _prefer_ht(ht_run, ttc_run, totals: ReceiptTotals, rates: dict[int, Decimal]) -> bool:
    """Whether the rows making the HT base are the purchase, rather than a run
    making the amount paid. Their TTC, worked out, has to come to what was
    paid; then they are when nothing else adds up; when they are more lines;
    when what the other run has on top of them is the tax itself ("TVA 9,29"
    read as a line); and, as many lines each way, when every one of them
    prints its rate - an invoice's rows beside the till's ticket, both on one
    photo."""
    paid = totals.printed_total_ttc
    worked_out = sum(
        ((item.total * (1 + rates[id(item)])).quantize(CENTS, rounding=ROUND_HALF_UP) for item in ht_run), start=ZERO
    )
    if paid is None or abs(worked_out - paid) > max(CENTS, len(ht_run) * HALF_CENT):
        return False
    if ttc_run is None or len(ht_run) > len(ttc_run):
        return True
    ht_ids = {id(item) for item in ht_run}
    extra = [item for item in ttc_run if id(item) not in ht_ids]
    tax = sum((summary.resolve().vat_amount or ZERO for summary in totals.vat_summaries), start=ZERO)
    if extra and len(extra) == len(ttc_run) - len(ht_run) and abs(sum((item.total for item in extra), start=ZERO) - tax) <= CENTS:
        return True
    # The other run is the VAT table itself: its base read as one line, its
    # tax as another.
    base = sum((item.total for item in ht_run), start=ZERO)
    for position, item in enumerate(ttc_run):
        rest = ttc_run[:position] + ttc_run[position + 1 :]
        if item.total == base and rest and abs(sum((other.total for other in rest), start=ZERO) - tax) <= CENTS:
            return True
    if len(ht_run) == len(ttc_run) and all(item.row_rate is not None for item in ht_run):
        return not all(item.row_rate is not None for item in ttc_run)
    return False


def _as_ht(items: list[Reading], rates: dict[int, Decimal]) -> None:
    """Read the run's amounts as HT, at the rates found for them; each TTC is
    worked out."""
    for item in items:
        rate = rates[id(item)]
        item.ht, item.rate, item.ht_row = item.total, rate, True
        item.total = item.read_total = (item.ht * (1 + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)
        if item.discount:
            item.discount = (item.discount * (1 + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


def _ticket_number(text: str, invoice_date: date | None) -> str:
    text = IBAN_RE.sub(" ", text)
    match = TICKET_WORD_RE.search(text)
    if match:
        number = match.group(1)
        if len(number) <= DAILY_COUNT_DIGITS and invoice_date is not None:
            return f"{number}-{invoice_date:%Y%m%d}"
        return number
    # Before the long digit runs below: a payment reference is one too.
    for pattern in DOCUMENT_NUMBER_RES:
        for match in pattern.finditer(text):
            number = re.sub(r"[\u2013\u2014]", "", match.group(1)).strip(".:-/")
            if any(character.isdigit() for character in number):
                return number
    match = STORE_TILL_RE.search(text.replace(" ", ""))
    if match:
        return "-".join(match.groups())
    match = BARCODE_RE.search(text.replace(" ", ""))
    return match.group(1) if match else ""


SHOPS = (
    TicketShop("FRANPRIX", (r"FRANPRIX",), "Franprix", coded_items=True),
    TicketShop("MONOPRIX", (r"MONOPRIX",), "Monoprix"),
    TicketShop("SABBH", (r"SAB+H", r"SABAH"), "Sabbh Oriental", placeholder_names=("Article divers",)),
    TicketShop("WINGSENG", (r"WING\s*SENG",), "Wing Seng"),
)
for _shop in SHOPS:
    PARSER_REGISTRY[_shop.supplier_code] = GenericReceiptParser(_shop)
