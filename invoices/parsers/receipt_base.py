"""Shared machinery for photographed till receipts ("facturettes").

These differ from every other parser in this package in one way that drives
the whole design: **the input is a photograph, so a misread digit produces a
perfectly well-formed wrong price.** `importing.parse_and_import` already
states the rule this codebase runs on - "guessing at prices is the one thing
this app must not do... a plausible-looking wrong figure is worse than no
figure at all, because nothing downstream can tell the difference." OCR is a
guessing machine, so everything here is built to make its guesses checkable.

The lever is that a till receipt is redundant. It prints the item lines AND
their sum AND a VAT table, and those three have to agree. On the 42-receipt
corpus this was measured on, 38 of 41 receipts reconcile to the penny - so
the parser can *prove* it read them correctly, and the three that don't are
exactly the ones a human should look at. `ParseCheck` carries that verdict
up to the review screen; nothing here silently repairs a receipt that
doesn't add up.

Three OCR artefacts show up often enough to be handled centrally:

*Column separators become digits.* Franprix's VAT table is drawn with "|"
rules, and the recogniser reads them as "1" - "| 2,26 | 0,12 | 2,38 |"
arrives as "2.261 0.121 2.381". Every amount is therefore read as a set of
candidate values (see `amount_candidates`) and the arithmetic picks the
reading that makes the receipt balance, rather than trusting one parse.

*The same table means different things at different shops.* Monoprix and
Franprix print the VAT base excluding tax; Sabbh's "Base TVA" column is the
tax-INCLUSIVE total. Guessing per shop would be fragile, so `VatSummary`
works it out from the arithmetic: whichever reading satisfies the identity
is the right one.

*Found by arithmetic, not by spelling.* Where the items stop, what the
total is and whether a promotion applies are all decided by the numbers
(`printed_total`, `ends_items`, `amount_printed`), never by recognising
"TOTAL", "SOUS-TOTAL" or "HORS AVANTAGES". Each recogniser spells those its
own way on every other ticket, and a parser keyed on the words becomes a
parser for one engine's mistakes: switching engines once turned "S TOTAL
EUR" into "TOTAL EOR" and a till total into a purchase.

*Receipts price in TTC, this app stores HT.* `ParsedLine.total_ht` is
excluding tax, so every line is divided by (1 + rate) on the way in - which
means the per-line VAT rate has to be right before the division is. Two of
the 42 receipts are at 20% (cleaning vinegar) rather than the 5.5% food
rate, so "assume 5.5%" is not available.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .base import InvoiceParser, ParseCheck, ParsedInvoice, PdfPage

CENTS = Decimal("0.01")
UNIT = Decimal("0.0001")

# How alike a name in the discount block and a name on an item line have
# to be to count as the same product. They are two separate OCR reads of
# the same printed words, so exact equality never fires; 80 absorbs a
# couple of substituted characters without matching a different product.
DISCOUNT_NAME_MATCH_THRESHOLD = 80

# The French rates a food/drink receipt can legitimately carry. Anything the
# OCR produces outside this set is a misread, not a new tax band, so it is
# rejected rather than propagated into a price.
KNOWN_VAT_RATES = (Decimal("0.055"), Decimal("0.10"), Decimal("0.20"), Decimal("0.021"))

# How far the lines may fall short of the printed total and still be treated
# as rounding rather than a misparse. Per-line HT is rounded to the cent, so
# a long receipt can drift a couple of centimes legitimately; anything
# larger means a line was misread or missed, and must not be absorbed
# silently into the reconciliation adjustment.
RECONCILIATION_TOLERANCE = Decimal("0.05")
# Slack allowed when checking a printed VAT amount against the rate. The
# tills themselves round to the cent, so exact equality is too strict.
VAT_IDENTITY_TOLERANCE = Decimal("0.02")
# Below this, the recogniser was unsure enough about some line that a human
# should look even if the arithmetic happens to balance.
MIN_OCR_CONFIDENCE = Decimal("0.80")

# 1 to 4 digits, a comma or dot, then 2 to 4 decimals. The 3rd and 4th
# decimals are kept rather than rejected: they are sometimes a real
# higher-precision HT figure (Monoprix prints "3.0237") and sometimes a
# column rule misread as a digit ("2.261" for "2,26"). Which one it is gets
# decided by arithmetic, in amount_candidates.
AMOUNT_RE = re.compile(r"(?<![\d.,])(\d{1,4})[.,](\d{2,4})(?!\d)")
# Rates print as "5,5%", "5.50%", "20%", and - when a column rule is read as
# a leading digit - "15.5%". The leading-1 case is undone in read_rate.
RATE_RE = re.compile(r"(?<![\d.,])(\d{1,3})(?:[.,](\d{1,2}))?\s*%")
# The year is anchored to a century rather than followed by a "not a
# digit" guard, because the recogniser routinely closes the space
# between a date and the time beside it: "Heure:14-07-202614:49:26".
# With the guard, every Sabbh receipt came out with no date at all.
DATE_RE = re.compile(r"(?<!\d)(\d{2})[-/.](\d{2})[-/.]((?:19|20)\d{2})")


# Money as a till prints it: 1-4 digits, a decimal comma or point, two
# decimals. A digit straight after the cents is not a third decimal - money is
# printed in cents - but a column rule or the euro sign read as a digit
# ("6.061" in a Franprix VAT table), so it is swallowed rather than allowed to
# make a different amount.
MONEY_RE = re.compile(r"(?<![\d.,])(?P<sign>-\s?)?(?P<units>\d{1,4})[.,](?P<cents>\d{2})\d{0,2}(?!\d)")
# A weight: three decimals, which money never has ("BRUTWEIGHT 0.920 KG",
# "MAN 3.360kg").
WEIGHT_RE = re.compile(r"(?<![\d.,])(?P<weight>\d{1,3}[.,]\d{3})(?!\d)")
# A price per unit: an amount followed by a slash, whatever the unit after it
# reads as ("@ 3.49 / KG", "à 3.49. / <G", "2.99EUR/kg").
PER_UNIT_RE = re.compile(r"(?P<price>\d{1,4}[.,]\d{2})[^\d\s/]{0,3}\s*/")


# "**DUPLICATA**", "-**DUPLICATA**---": a banner the till prints between
# asterisks. Never part of a product's name - but the recogniser once grouped
# one with the first item's price, whose name it never read.
BANNER_RE = re.compile(r"[-\s]*\*{2,}[^*]*\*{2,}[-\s]*")
LETTER_RE = re.compile(r"[A-Za-zÀ-ÿ]")
# What an item whose name was not read is called until a person names it.
UNREAD_NAME = "Article non lu"


def item_name(text: str) -> str | None:
    """The product name in `text`, banners taken out; None when no letter is
    left - the amount is an item, its name was not read."""
    name = " ".join(BANNER_RE.sub(" ", text).split())
    return name if LETTER_RE.search(name) else None


def line_amounts(line: str) -> list[Decimal]:
    """Every money amount on a line, signed, to the cent."""
    return [
        Decimal(f"{'-' if match.group('sign') else ''}{match.group('units')}.{match.group('cents')}")
        for match in MONEY_RE.finditer(line)
    ]


def printed_total(lines: list[str]) -> Decimal | None:
    """The amount paid, found by arithmetic rather than by the word "TOTAL".

    Every till here prints the amount paid several times - sub-total, total,
    the card or cash line, the VAT table's tax-inclusive column - and no other
    figure that large more than once, so it is the largest amount printed at
    least twice. A promotion can print the pre-discount total twice as well
    (Monoprix: on the item line and on "TOTAL HORS AVANTAGES"), and a customer
    can hand over a round sum that is printed twice too - so among the
    repeated amounts, the largest one the ticket's own VAT table adds up to
    wins.

    When no VAT row confirms any of them, a legible table is still trusted
    over repetition: Monoprix prints its HT base twice ("5,5% 7,96 ..." and
    "Total TVA 7,96 ..."), and taking the largest repeated amount there would
    make the HT base the total. The table's own total is used if the ticket
    prints it; otherwise there is no total, and the receipt says so. Only a
    ticket with no readable table at all falls back to the largest repeated
    amount.

    Measured on the 42 real receipts: 41 right on repetition alone, the VAT
    table settling the one discount ticket that isn't.
    """
    counts = Counter(abs(amount) for line in lines for amount in line_amounts(line) if amount)
    repeated = sorted((amount for amount, times in counts.items() if times >= 2), reverse=True)
    for candidate in repeated:
        summaries, _rate_only = collect_vat_summaries(lines, candidate)
        read = [summary for summary in summaries if not summary.derived and summary.total_ttc is not None]
        if any(abs(summary.total_ttc - candidate) <= CENTS for summary in read):
            return candidate
        # Several rates print one row each, and the amount paid is their sum -
        # which no row shows. Taking the one row that repeats an amount made a
        # 0.20 paper bag the total, and the 11.20 of ham a "promotion".
        if len(read) >= 2 and abs(sum((summary.total_ttc for summary in read), start=Decimal("0")) - candidate) <= CENTS:
            return candidate
    table, _rate_only = collect_vat_summaries(lines, None)
    read = [summary for summary in table if not summary.derived and summary.total_ttc is not None]
    if read:
        table_total = sum((summary.total_ttc for summary in read), start=Decimal("0"))
        return table_total if amount_printed(lines, table_total) is not None else None
    return repeated[0] if repeated else None


def ends_items(amount: Decimal, item_count: int, gross: Decimal, net: Decimal, total: Decimal | None) -> bool:
    """Whether a line showing `amount` is where the items stop and the totals
    begin - decided by the numbers, not by how "TOTAL" came out.

    It is the first line to show the amount paid or, once the items read so
    far add up to it, the first to repeat their running sum (a sub-total, a
    pre-discount total). `gross` is that sum before any promotion printed
    under an item, `net` after. With no readable total, a line repeating the
    sum of two or more items still counts: two prices adding up to exactly a
    later figure is a total, not a coincidence.
    """
    if item_count == 0:
        return False
    matches_sum = abs(amount - gross) <= CENTS or abs(amount - net) <= CENTS
    if total is None:
        return item_count >= 2 and matches_sum
    if abs(amount - total) <= CENTS:
        return True
    return max(gross, net) >= total - CENTS and matches_sum


def amount_printed(lines: list[str], amount: Decimal, start: int = 0) -> int | None:
    """Index of the first line from `start` printing `amount`, or None.

    It is how a promotion is confirmed: the items' own sum has to appear on
    the ticket as its pre-discount total. Computing the discount as items
    minus total without that would absorb a misread item price as a
    "promotion" - the receipt would balance, and be wrong.
    """
    for index in range(start, len(lines)):
        if any(abs(abs(value) - amount) <= CENTS for value in line_amounts(lines[index])):
            return index
    return None


def printed_promotion(lines: list[str], start: int, total: Decimal | None) -> tuple[Decimal, Decimal] | None:
    """(pre-discount total, discount) when the ticket prints a promotion after
    its items, recognised by arithmetic: an amount larger than the one paid,
    whose difference from it is itself printed at least twice - once against
    the product and once as the total of promotions, or as a promotion line
    and its reminder. None when there is no such pair.

    It closes one silent case. A "3 pour 2" makes an item free; if that item's
    line is misread, the remaining lines add up to exactly the amount paid
    and every sum balances. The printed pre-discount total is the only thing
    on the ticket saying an item is missing. Cash and change (20,00 handed
    over, 17,10 back) make the same pair, but change is printed once - hence
    "twice".
    """
    if total is None:
        return None
    region = [abs(amount) for line in lines[start:] for amount in line_amounts(line)]
    counts = Counter(region)
    for candidate in sorted({amount for amount in region if amount - total > CENTS}, reverse=True):
        if counts[candidate - total] >= 2:
            return candidate, candidate - total
    return None


def missing_item_check(gross: Decimal, promotion: tuple[Decimal, Decimal] | None) -> ParseCheck | None:
    """A failed check when the ticket prints a pre-discount total the items
    do not reach (see printed_promotion)."""
    if promotion is None or abs(gross - promotion[0]) <= CENTS:
        return None
    before, discount = promotion
    return ParseCheck(
        label="Articles = total avant remise",
        passed=False,
        detail=(
            f"les articles lus font {gross:.2f} €, le ticket imprime {before:.2f} € avant une remise de "
            f"{discount:.2f} € : un article manque ou est mal lu"
        ),
    )


def weight_on(line: str) -> tuple[Decimal, Decimal | None] | None:
    """(weight in kg, price per kg or None) when `line` is a weight detail.

    A line with a three-decimal figure and no money on it but that figure
    and a price per unit. The condition is what keeps a Franprix VAT row -
    "5.5%  12.92  0.711  13.63", a column rule read as a third decimal -
    from passing as 0.711 kg of something.
    """
    match = WEIGHT_RE.search(line)
    if not match:
        return None
    weight = Decimal(match.group("weight").replace(",", "."))
    per_unit = PER_UNIT_RE.search(line)
    price = Decimal(per_unit.group("price").replace(",", ".")) if per_unit else None
    allowed = {weight.quantize(CENTS, rounding=ROUND_DOWN)} | ({price} if price is not None else set())
    if any(abs(amount) not in allowed for amount in line_amounts(line)):
        return None
    return weight, price


def amount_candidates(text: str) -> list[Decimal]:
    """Every plausible reading of every amount in `text`.

    A 2-decimal amount has exactly one reading. A 3-or-4-decimal one has
    two: the value as printed (a genuine high-precision figure) and the
    value with its trailing digit dropped (a column rule misread as a
    digit - see the module docstring). Both are offered; the caller's
    arithmetic decides which is real.
    """
    values: list[Decimal] = []

    def remember(value: Decimal) -> None:
        if value not in values:
            values.append(value)

    for match in AMOUNT_RE.finditer(text):
        integer, decimals = match.group(1), match.group(2)
        printed = Decimal(f"{integer}.{decimals}")
        remember(printed)
        if len(decimals) > 2:
            remember(Decimal(f"{integer}.{decimals[:2]}"))
            remember(printed.quantize(CENTS, rounding=ROUND_HALF_UP))
    return values


def read_rate(text: str) -> Decimal | None:
    """A VAT rate as a fraction (0.055), or None if `text` holds no rate
    that a French receipt could actually carry.

    Rejecting unknown values is the point: OCR happily turns the VAT
    *amount* "0,26" into a rate of 26%, and a receipt at "26%" would divide
    every line by the wrong number without anything downstream noticing.
    """
    for match in RATE_RE.finditer(text):
        whole, decimals = match.group(1), match.group(2)
        readings = [Decimal(f"{whole}.{decimals}") if decimals else Decimal(whole)]
        # "| 5,5%" read as "15.5%" - drop a leading 1 that a column rule
        # could have contributed.
        if len(whole) > 1 and whole.startswith("1"):
            stripped = whole[1:]
            readings.append(Decimal(f"{stripped}.{decimals}") if decimals else Decimal(stripped))
        for reading in readings:
            rate = reading / Decimal("100")
            if any(rate == known for known in KNOWN_VAT_RATES):
                return rate
    return None


def read_date(text: str, date_hint: date | None = None) -> date | None:
    """All four shops print day first (15-07-2026, 28/01/2026)."""
    for match in DATE_RE.finditer(text):
        day, month, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date_hint


def to_ht(total_ttc: Decimal, vat_rate: Decimal) -> Decimal:
    return (total_ttc / (Decimal("1") + vat_rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass
class VatSummary:
    """The receipt's own VAT table for one rate.

    `base` is whatever number the till printed next to the rate - which is
    excluding tax at Franprix and Monoprix but INCLUDING tax at Sabbh. Which
    one it is is settled by `resolve`, from the arithmetic rather than from
    a per-shop assumption.
    """

    rate: Decimal
    base: Decimal | None = None
    vat_amount: Decimal | None = None
    total_ttc: Decimal | None = None
    # True when only the rate was legible and base/VAT were computed from the
    # ticket's grand total rather than read. The rate is still right - it is
    # checked against the rates France actually has - but the split is
    # arithmetic, not evidence, and the checks report the table as unread.
    derived: bool = False

    def resolve(self) -> "VatSummary":
        """Fill in whatever the table did not print, and normalise `base` to
        a tax-exclusive figure."""
        rate, base, vat, ttc = self.rate, self.base, self.vat_amount, self.total_ttc

        if base is not None and vat is not None and ttc is None:
            # Is `base` HT or TTC? Only one of the two readings reproduces
            # the printed VAT amount.
            as_ht = (base * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
            as_ttc = (base * rate / (Decimal("1") + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)
            if abs(as_ttc - vat) < abs(as_ht - vat):
                ttc, base = base, base - vat
            else:
                ttc = base + vat
        elif base is not None and ttc is not None and vat is None:
            vat = ttc - base
        elif ttc is not None and vat is not None and base is None:
            base = ttc - vat
        elif ttc is not None and base is None and vat is None:
            vat = (ttc * rate / (Decimal("1") + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)
            base = ttc - vat
        elif base is not None and vat is None and ttc is None:
            vat = (base * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
            ttc = base + vat

        return VatSummary(rate=rate, base=base, vat_amount=vat, total_ttc=ttc, derived=self.derived)

    @property
    def is_consistent(self) -> bool:
        if self.base is None or self.vat_amount is None:
            return False
        expected = (self.base * self.rate).quantize(CENTS, rounding=ROUND_HALF_UP)
        return abs(expected - self.vat_amount) <= VAT_IDENTITY_TOLERANCE


@dataclass
class ReceiptTotals:
    """What the receipt says about itself, as opposed to what its lines say."""

    printed_total_ttc: Decimal | None = None
    vat_summaries: list[VatSummary] = field(default_factory=list)
    # Rates legible on a VAT row whose amounts were not, and that no bucket
    # covers - see `line_rate`.
    rate_only: list[Decimal] = field(default_factory=list)

    @property
    def default_rate(self) -> Decimal | None:
        """The rate to apply to a line that carries no code of its own.

        Only meaningful when the receipt has exactly one VAT bucket - which
        was true of 41 of the 42 receipts measured, but a mixed-rate receipt
        must not silently inherit one of its two rates.
        """
        if len(self.vat_summaries) == 1:
            return self.vat_summaries[0].rate
        return None

    @property
    def line_rate(self) -> Decimal | None:
        """The rate to price uncoded lines at: `default_rate`, or - when no
        bucket could be read at all - the single rate that was legible.

        Pricing at a legible rate beats pricing at none (a TTC amount booked
        as HT overstates the cost by the whole tax). The checks still report
        the table as unread, so such a ticket always goes to review.
        """
        if self.default_rate is not None:
            return self.default_rate
        if not self.vat_summaries and len(set(self.rate_only)) == 1:
            return self.rate_only[0]
        return None


def _printed_ht_base(totals: ReceiptTotals) -> Decimal | None:
    """The receipt's own tax-exclusive total, summed across its VAT buckets.

    None when any bucket failed to yield a base - a partial sum would be a
    smaller number than the truth and would silently understate the invoice.
    """
    if not totals.vat_summaries:
        return None
    bases = []
    for summary in totals.vat_summaries:
        resolved = summary.resolve()
        if resolved.base is None or resolved.base <= 0:
            # A bucket that resolves to nothing (or less) was misread; a sum
            # including it would understate the invoice rather than fail.
            return None
        bases.append(resolved.base)
    return sum(bases, start=Decimal("0"))


def build_checks(
    totals: ReceiptTotals,
    lines_total_ttc: Decimal,
    lines_total_ht: Decimal | None = None,
    line_count: int = 0,
    confidence: Decimal | None = None,
    extra: list[ParseCheck] | None = None,
) -> tuple[list[ParseCheck], Decimal]:
    """The self-verification every receipt parser runs, and the
    reconciliation adjustment (in HT) that follows from it.

    Two different sums are checked, because they catch different mistakes.
    `lines_total_ttc` is built from the amounts as *printed*, and comparing
    it with the printed grand total proves the recogniser read those amounts
    correctly. `lines_total_ht` is what will actually be stored, after each
    line has been divided by (1 + rate) and rounded to the cent, and
    comparing it with the VAT table's own HT base catches the rounding
    those divisions accumulate.

    That second check is not theoretical. Six baguettes at 0.49 TTC are
    0.4645 HT each, stored as 0.46; the receipt's own HT base says 2.79 and
    the lines say 2.76. Three centimes, on a receipt whose TTC arithmetic
    balanced perfectly - which is exactly the shape of "silently wrong
    money" this app has been bitten by before. The gap goes into
    `reconciliation_adjustment` so the invoice total is the printed one.

    Returns (checks, adjustment). The adjustment is non-zero only when the
    lines agree with the printed total to within rounding - a real
    discrepancy is reported as a failed check and left visible, never
    absorbed.
    """
    checks: list[ParseCheck] = []
    adjustment = Decimal("0")
    # A cent can be lost per line to the HT division, so the slack has to
    # grow with the receipt; a flat tolerance would fail a long one that is
    # perfectly well read.
    rounding_slack = max(RECONCILIATION_TOLERANCE, CENTS * line_count)

    printed = totals.printed_total_ttc
    if printed is None:
        checks.append(
            ParseCheck(
                label="Total imprimé lu",
                passed=False,
                detail="Aucun total lisible sur le ticket : les lignes ne peuvent pas être vérifiées.",
            )
        )
    else:
        drift = printed - lines_total_ttc
        within_rounding = abs(drift) <= RECONCILIATION_TOLERANCE
        checks.append(
            ParseCheck(
                label="Somme des lignes = total imprimé",
                passed=within_rounding,
                detail=f"lignes {lines_total_ttc:.2f} € / ticket {printed:.2f} € (écart {drift:+.2f} €)",
            )
        )
        if within_rounding:
            # Fall back to converting the TTC gap only when the VAT table
            # gave no HT base to reconcile against (Wing Seng prints none).
            rate = totals.line_rate or Decimal("0")
            adjustment = to_ht(drift, rate)

    for summary in totals.vat_summaries:
        resolved = summary.resolve()
        rate_percent = format_rate(resolved.rate)
        if resolved.base is None or resolved.vat_amount is None:
            checks.append(
                ParseCheck(
                    label=f"Table TVA {rate_percent}%",
                    passed=False,
                    detail="Table TVA incomplète : montant ou base illisible.",
                )
            )
            continue
        if resolved.derived:
            checks.append(
                ParseCheck(
                    label=f"Table TVA {rate_percent}%",
                    passed=False,
                    detail=(
                        f"taux lu, montants illisibles : HT {resolved.base:.2f} € et TVA "
                        f"{resolved.vat_amount:.2f} € calculés depuis le total imprimé"
                    ),
                )
            )
            continue
        expected_vat = (resolved.base * resolved.rate).quantize(CENTS, rounding=ROUND_HALF_UP)
        checks.append(
            ParseCheck(
                label=f"TVA {rate_percent}% cohérente",
                passed=resolved.is_consistent,
                detail=(
                    f"HT {resolved.base:.2f} € x {rate_percent}% = {expected_vat:.2f} € "
                    f"/ ticket {resolved.vat_amount:.2f} €"
                ),
            )
        )

    if not totals.vat_summaries:
        if totals.rate_only:
            legible = ", ".join(f"{format_rate(rate)}%" for rate in totals.rate_only)
            detail = f"Seul le taux ({legible}) est lisible : les montants de TVA n'ont pas pu être vérifiés."
        else:
            detail = "Aucun taux de TVA lisible : le taux n'a pas pu être vérifié."
        checks.append(ParseCheck(label="Table TVA lue", passed=False, detail=detail))

    printed_ht = _printed_ht_base(totals)
    if printed_ht is not None and lines_total_ht is not None:
        ht_drift = printed_ht - lines_total_ht
        within_rounding = abs(ht_drift) <= rounding_slack
        checks.append(
            ParseCheck(
                label="Somme HT des lignes = base HT du ticket",
                passed=within_rounding,
                detail=(
                    f"lignes {lines_total_ht:.2f} € HT / ticket {printed_ht:.2f} € HT "
                    f"(écart {ht_drift:+.2f} €)"
                ),
            )
        )
        # The HT base is the figure the invoice total should land on, so it
        # supersedes the TTC-derived adjustment above when both are known.
        adjustment = ht_drift if within_rounding else Decimal("0")

    if confidence is not None:
        checks.append(
            ParseCheck(
                label="Confiance OCR",
                passed=confidence >= MIN_OCR_CONFIDENCE,
                detail=f"ligne la moins sûre : {confidence:.0%}",
            )
        )

    checks.extend(extra or [])
    return checks, adjustment


def compose_invoice_number(ticket: str, invoice_date: date | None, total: Decimal | None) -> str:
    """A stable identifier for a receipt, used to stop the same photo being
    imported twice in a batch.

    A real ticket number is used when the till prints one. When it does not,
    date + total stands in - which is what the source filenames themselves
    use. That is not guaranteed unique (two identical baguette runs on the
    same day would collide), and a collision surfaces as a
    DuplicateInvoiceError the operator can see and act on, rather than as a
    silently merged invoice.
    """
    if ticket:
        return ticket
    if invoice_date is None or total is None:
        return ""
    return f"{invoice_date:%Y%m%d}-{total:.2f}"


class ReceiptParser(InvoiceParser):
    """Base for the photographed-receipt parsers.

    Overrides `parse` because the shared implementation reads the PDF with
    pdfplumber, which returns an empty string for every one of these files -
    they contain no text layer at all, only a photo. The override swaps that
    one I/O step for OCR and changes nothing else: subclasses still
    implement `parse_pages` and only `parse_pages`, so every shop's layout
    is still testable from hand-written text with no PDF, no photo and no
    OCR engine involved. `tests/test_parser_contract.py` enforces that the
    override lives here and not in any individual shop parser.
    """

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        import os

        from invoices.ocr import ocr_pdf

        return self.parse_ocr_pages(
            ocr_pdf(pdf_path), date_hint=date_hint, source_name=os.path.basename(pdf_path)
        )

    def parse_ocr_pages(self, ocr_pages, date_hint: date | None = None, source_name: str = "") -> ParsedInvoice:
        """Run this parser over pages that have already been recognised.

        The batch import needs the recognised text *and* the corrected image
        (to store as the review preview), so it does the OCR itself and
        hands the result here rather than letting `parse` redo it - one
        receipt is several seconds of CPU, and a batch is dozens.
        """
        pages = [PdfPage(text=page.text, tables=[]) for page in ocr_pages]
        parsed = self.parse_pages(pages, date_hint=date_hint, source_name=source_name)
        self._as_read(parsed, "\n".join(page.text for page in ocr_pages))
        confidences = [page.confidence for page in ocr_pages if page.lines]
        if confidences:
            parsed.confidence = Decimal(str(min(confidences))).quantize(CENTS, rounding=ROUND_HALF_UP)
        return parsed

    def parse_text(self, text: str, date_hint: date | None = None) -> ParsedInvoice:
        """Parse a receipt's stored reading (Invoice.ocr_text) again: how a
        parser improvement reaches tickets already imported, with no photo
        and no OCR (see receipts.reread_receipt)."""
        parsed = self.parse_pages([PdfPage(text=text, tables=[])], date_hint=date_hint)
        self._as_read(parsed, text)
        return parsed

    @staticmethod
    def _as_read(parsed: ParsedInvoice, text: str) -> None:
        parsed.source_text = text
        parsed.from_ocr = True
        for line in parsed.lines:
            # Kept before the shop's price list renames a placeholder: a
            # price is not a name, and what was read is what the next ticket
            # gets matched against (InvoiceLine.read_as).
            if not line.is_placeholder:
                line.read_as = line.raw_name


def assign_rates_by_bucket(
    coded_totals: dict[str, Decimal], vat_summaries: list[VatSummary]
) -> tuple[dict[str, Decimal], bool]:
    """Map each per-line VAT code ("T1", "A", ...) to an actual rate.

    Shops print a one-character code against each item and then a table of
    rates at the bottom, but never a legend joining the two. The join is
    recovered from the arithmetic instead: each code's items sum to some
    amount, and so does each rate's bucket in the table, so the code whose
    items total 7.18 belongs to the rate whose bucket totals 7.18.

    Deriving it beats hardcoding "T1 means 5.5%": one of the 42 receipts
    measured is a T2/20% ticket (cleaning vinegar), and a hardcoded map
    would have divided it by the wrong rate and understated its cost by 14%
    with nothing downstream able to notice.

    Returns (code -> rate, confident). `confident` is False when the codes
    could not be matched to buckets, in which case the caller must not
    silently pick one.
    """
    if not vat_summaries:
        return {}, False
    if len(vat_summaries) == 1:
        # Nothing to disambiguate - every code is that rate.
        return {code: vat_summaries[0].rate for code in coded_totals}, True

    assignment: dict[str, Decimal] = {}
    confident = True
    for code, code_total in coded_totals.items():
        best = None
        for summary in vat_summaries:
            resolved = summary.resolve()
            if resolved.total_ttc is None:
                continue
            gap = abs(resolved.total_ttc - code_total)
            if best is None or gap < best[0]:
                best = (gap, resolved.rate)
        if best is None or best[0] > RECONCILIATION_TOLERANCE:
            confident = False
            continue
        assignment[code] = best[1]
    return assignment, confident


def distribute_discount(
    lines, discount_total: Decimal, discounted_names: list[str]
) -> tuple[bool, Decimal]:
    """Spread a receipt-level discount over the lines it belongs to,
    mutating them in place. Returns (attributed, unattributed_amount).

    Franprix prints its promotions as a separate "Detail des remises
    immediates" block naming the products, not as a reduction on the item
    lines themselves - so the items sum to the pre-discount total and only
    "TOTAL A PAYER" reflects the promotion. Something has to bridge that gap
    or every promotional receipt fails its own arithmetic check.

    Spreading pro-rata across the *matching* lines rather than all of them
    is what keeps unit costs honest: a "3 pour 2" on baguettes must make
    baguettes cheaper, not shave a few centimes off the oranges bought at
    full price. The names are fuzzy-matched because the discount block and
    the item line are two separate OCR reads of the same words, and they
    routinely disagree by a character or two ("BAGUETTE" / "BAGUETIE").
    """
    if discount_total <= 0 or not lines:
        return True, Decimal("0")

    targets = []
    if discounted_names:
        from rapidfuzz import fuzz

        for line in lines:
            for name in discounted_names:
                if fuzz.token_sort_ratio(line.raw_name.upper(), name.upper()) >= DISCOUNT_NAME_MATCH_THRESHOLD:
                    targets.append(line)
                    break

    attributed = bool(targets)
    if not targets:
        # No name matched. The total still has to reconcile, so the discount
        # is spread over everything - but the caller is told, so the review
        # screen can say the split is a guess rather than a reading.
        targets = list(lines)

    base = sum((line.total_ht for line in targets), start=Decimal("0"))
    if base <= 0:
        return False, discount_total

    remaining = discount_total
    for index, line in enumerate(targets):
        if index == len(targets) - 1:
            share = remaining
        else:
            share = (discount_total * line.total_ht / base).quantize(CENTS, rounding=ROUND_HALF_UP)
            remaining -= share
        line.discount = (line.discount or Decimal("0")) + share
        line.total_ht = line.total_ht - share
        # What the line finally cost is worked out now, not printed.
        line.printed_ttc = None
        line.unit_cost_ht = (
            (line.total_ht / line.quantity).quantize(UNIT, rounding=ROUND_HALF_UP)
            if line.quantity
            else Decimal("0")
        )
    return attributed, Decimal("0")


def format_rate(rate: Decimal) -> str:
    """"5.5" / "20" - never "2E+1", which is what Decimal.normalize() gives
    for a whole-number percentage and what the review screen showed once."""
    return f"{(rate * Decimal('100')).quantize(CENTS).normalize():f}"


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def parse_vat_line(
    text: str, rate: Decimal | None = None, expected_total: Decimal | None = None
) -> VatSummary | None:
    """Read one row of a receipt's VAT table.

    The four shops print the same three numbers in four different column
    orders, with four different conventions about whether the "base" column
    is tax-inclusive, and Franprix draws rules between them that the
    recogniser turns into extra digits. Rather than encode one layout per
    shop, this searches the row's plausible readings for the pair that
    actually satisfies a VAT identity - either base x rate = vat (a
    tax-exclusive base) or base x rate / (1 + rate) = vat (an inclusive
    one) - and prefers a pair whose implied total also appears on the row.

    That makes the reading self-proving: a row only parses if its numbers
    are arithmetically consistent, so a misread digit yields no summary at
    all rather than a plausible wrong one.

    `expected_total` is the grand total the receipt printed elsewhere, when
    the caller has already read it. Several readings of a mangled row can
    satisfy the identity at once; the one that also reproduces the printed
    total is the real one. Callers should read the total first and pass it.
    """
    resolved_rate = rate if rate is not None else read_rate(text)
    if resolved_rate is None:
        return None

    # The rate's own digits would otherwise be read as an amount ("5.50%").
    values = amount_candidates(RATE_RE.sub(" ", text))
    if not values:
        if expected_total is not None:
            return _derive_from_total(resolved_rate, expected_total, values)
        return VatSummary(rate=resolved_rate)

    best: tuple[tuple[int, int, int], VatSummary] | None = None
    for base in values:
        for vat in values:
            if base == vat:
                continue
            as_exclusive = (base * resolved_rate).quantize(CENTS, rounding=ROUND_HALF_UP)
            as_inclusive = (base * resolved_rate / (Decimal("1") + resolved_rate)).quantize(
                CENTS, rounding=ROUND_HALF_UP
            )
            if abs(as_exclusive - vat) <= VAT_IDENTITY_TOLERANCE:
                total = base + vat
            elif abs(as_inclusive - vat) <= VAT_IDENTITY_TOLERANCE:
                total = base
            else:
                continue
            # A row whose third column confirms the total is a better
            # reading than one where the total is only inferred.
            confirmed = any(abs(value - total) <= CENTS for value in values)
            candidate = VatSummary(rate=resolved_rate, base=base, vat_amount=vat).resolve()
            # Tie-break towards centimes. Both "2.261 / 0.121" (the column
            # rules read as digits) and "2.26 / 0.12" (the real figures)
            # satisfy the identity and both are confirmed by a third
            # column, so the identity alone cannot separate them - but a
            # till prints money in cents, so the shorter reading is the
            # true one. Monoprix's genuinely 4-decimal HT survives this
            # because only its long reading is confirmed at all.
            extra_decimals = _decimal_places(base) + _decimal_places(vat)
            # The grand total the receipt printed elsewhere is the strongest
            # evidence available: a VAT row that adds up to it is the right
            # reading of that row, whatever its column order or decimal
            # count. Monoprix's real 4-decimal HT and Franprix's
            # rule-inflated "2.261" both satisfy the bare identity, and only
            # this separates them.
            matches_total = (
                expected_total is not None
                and candidate.total_ttc is not None
                and abs(candidate.total_ttc - expected_total) <= CENTS
            )
            score = (0 if matches_total else 1, 0 if confirmed else 1, extra_decimals)
            if best is None or score < best[0]:
                best = (score, candidate)

    if best is not None:
        return best[1]

    # Nothing balanced, so the row printed - or the recogniser recovered -
    # only one of its columns. Which column that is has to be decided, not
    # assumed: Wing Seng's whole table is the tax amount ("TVA 5,50%: 0,58
    # EUR"), while a Sabbh ticket whose VAT column was cut off leaves the
    # tax-INCLUSIVE base standing alone. Reading the second as the first
    # gave a base of 0.00 EUR on a 7.70 EUR receipt.
    if len(values) == 1 and expected_total is not None:
        value = values[0]
        if abs(value - expected_total) <= CENTS:
            # Only the total and the rate were read; the split is arithmetic.
            return VatSummary(rate=resolved_rate, total_ttc=value, derived=True)
        implied_vat = (expected_total * resolved_rate / (Decimal("1") + resolved_rate)).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )
        if abs(value - implied_vat) <= VAT_IDENTITY_TOLERANCE:
            return VatSummary(rate=resolved_rate, vat_amount=value, total_ttc=expected_total)
        # Neither - the number is something else on that row entirely, and
        # guessing would put a fabricated base on the invoice.
        return VatSummary(rate=resolved_rate)
    if len(values) == 1:
        return VatSummary(rate=resolved_rate, vat_amount=values[0])
    if expected_total is not None:
        return _derive_from_total(resolved_rate, expected_total, values)
    return VatSummary(rate=resolved_rate)


def _derive_from_total(rate: Decimal, expected_total: Decimal, values: list[Decimal]) -> VatSummary:
    """A VAT row the recogniser mangled past reading ("15.5%1  3.481
    0.192063.671"), on a ticket whose grand total is known from elsewhere.

    The legible rate and that total fix the split by arithmetic. It counts as
    *read* only when one of the row's own numbers confirms the computed HT or
    VAT; otherwise it is marked `derived`, and the checks say the table was
    not read - the lines get the right rate, and a person still looks.
    """
    computed = VatSummary(rate=rate, total_ttc=expected_total).resolve()
    confirmed = any(
        abs(value - computed.base) <= CENTS or abs(value - computed.vat_amount) <= CENTS for value in values
    )
    return VatSummary(
        rate=rate,
        base=computed.base,
        vat_amount=computed.vat_amount,
        total_ttc=expected_total,
        derived=not confirmed,
    )


# Inferring a rate that is not printed legibly at all needs a tighter match
# than checking one that is: at 2 cents, a 0,03 EUR tax on a 0,49 EUR ticket
# fits both 5.5% and 10%.
INFERENCE_TOLERANCE = CENTS


def infer_vat_row(text: str, expected_total: Decimal | None) -> VatSummary | None:
    """A VAT row whose rate is illegible ("XSG  5.74  0.32  6.061"),
    identified by arithmetic alone.

    Accepted only when two of the row's numbers reproduce the ticket's grand
    total at exactly one French rate, to the cent - either base + tax (a
    tax-exclusive base) or the base itself (Sabbh's tax-inclusive one).
    That is three independent constraints, which no other line on a receipt
    meets by accident; and when more than one rate fits, none is chosen.
    """
    if expected_total is None:
        return None
    values = amount_candidates(text)
    found: dict[Decimal, tuple[Decimal, Decimal]] = {}
    for base in values:
        for vat in values:
            if vat >= base:
                continue
            for rate in KNOWN_VAT_RATES:
                exclusive = (base * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
                if abs(exclusive - vat) <= INFERENCE_TOLERANCE and abs(base + vat - expected_total) <= CENTS:
                    found.setdefault(rate, (expected_total - vat, vat))
                inclusive = (base * rate / (Decimal("1") + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)
                if abs(inclusive - vat) <= INFERENCE_TOLERANCE and abs(base - expected_total) <= CENTS:
                    found.setdefault(rate, (expected_total - vat, vat))
    if len(found) != 1:
        return None
    ((rate, (base, vat)),) = found.items()
    return VatSummary(rate=rate, base=base.quantize(CENTS), vat_amount=vat.quantize(CENTS), total_ttc=expected_total)


def collect_vat_summaries(lines: list[str], printed_total: Decimal | None) -> tuple[list[VatSummary], list[Decimal]]:
    """Every VAT bucket on the ticket, one per rate - and the rates that were
    legible on rows whose amounts were not (see ReceiptTotals.rate_only).

    One per rate because a "Total TVA" row repeats the bucket on a
    single-rate ticket, and counting it twice would block `default_rate`. A
    bucket actually read beats one derived from the total. Only when no rate
    is legible anywhere is a row's arithmetic allowed to name one.
    """
    summaries: dict[Decimal, VatSummary] = {}
    legible: list[Decimal] = []
    for line in lines:
        rate = read_rate(line)
        if rate is None:
            continue
        summary = finalise_summary(parse_vat_line(line, rate=rate, expected_total=printed_total), printed_total)
        if summary is not None and summary.base is not None:
            known = summaries.get(summary.rate)
            if known is None or (known.derived and not summary.derived):
                summaries[summary.rate] = summary
        elif rate not in legible:
            legible.append(rate)
    if not summaries and not legible:
        for line in lines:
            inferred = infer_vat_row(line, printed_total)
            if inferred is not None:
                summaries[inferred.rate] = inferred.resolve()
                break
    return list(summaries.values()), [rate for rate in legible if rate not in summaries]


def finalise_summary(summary: VatSummary | None, printed_total: Decimal | None) -> VatSummary | None:
    """Fill a VAT row that printed only its tax amount.

    Wing Seng's whole VAT table is one line - "TVA 5,50%: 0,58 EUR" - with no
    base and no total. The receipt's own grand total supplies the missing
    side, which is what makes the row checkable at all.
    """
    if summary is None:
        return None
    if summary.base is None and summary.total_ttc is None and printed_total is not None:
        summary = VatSummary(
            rate=summary.rate,
            vat_amount=summary.vat_amount,
            total_ttc=printed_total,
            # With neither the tax nor a base read, everything but the rate
            # would come from the grand total: arithmetic, not a reading. It
            # used to pass as "TVA cohérente" - trivially, since resolve()
            # makes any split it computes consistent with itself.
            derived=summary.derived or summary.vat_amount is None,
        )
    return summary.resolve()


def reconcile_quantity(
    name: str, quantity: int, unit_price: Decimal, amount: Decimal, fixes: list[str], problems: list[str]
) -> int:
    """A line printing a count, a unit price and a total checks itself.

    The count is the one number on it nothing else verifies, while the total
    is verified by the receipt's own sums - so when count x unit price
    disagrees with the total, the amounts win and the count is recomputed
    ("8 X 0.49  2.94" is six baguettes). When no whole count fits, the line
    is kept as read and the disagreement reported. Appends one sentence to
    `fixes` or `problems`; see `quantity_checks`.
    """
    if unit_price <= 0 or abs(quantity * unit_price - amount) <= CENTS:
        return quantity
    implied = (amount / unit_price).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if implied >= 1 and abs(implied * unit_price - amount) <= CENTS:
        fixes.append(f"{name} : {quantity} lu, {implied} d'après {amount} € / {unit_price} €")
        return int(implied)
    problems.append(f"{name} : {quantity} x {unit_price} € ≠ {amount} €")
    return quantity


def quantity_checks(fixes: list[str], problems: list[str]) -> list[ParseCheck]:
    checks = []
    if fixes:
        checks.append(ParseCheck(label="Quantités recalculées", passed=True, detail="; ".join(fixes)))
    if problems:
        checks.append(ParseCheck(label="Quantité x prix unitaire = total", passed=False, detail="; ".join(problems)))
    return checks
