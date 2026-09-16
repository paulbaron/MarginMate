"""Franprix till receipts, photographed.

The most common of the four layouts (19 of the 42 receipts this was built
against): thermal paper, crumpled, dot-matrix print, and thin enough that the
reverse side shows through.

    FRANPRIX
    206 RUE ST MAUR
    **DUPLICATA**
    BAGUETTE BLANC        T1 0.49
    CITRON SHT 500G       T1 2.29
    POMME JULIET X4       T1 3.89
      BRUTWEIGHT 0.920 KG
      @ 3.49 / KG
    ORANGE                T1 3.21
    SOUS-TOTAL                13.06
    TOTAL SANS AVANTAGES      13.55
    Detail des remises immediates :
    3 pour 2
      BAGUETTE BLANC           0.49
    TOTAL remise               0.49
    TOTAL A PAYER             13.06
    | Rate | Taxable |  Vat | Total |
    | 5.5% |   12.38 | 0.68 | 13.06 |
    15-07-2026 WEDNESDAY  17:39
    UDAYAN   R1 005333-01 385

Everything is located by the numbers, never by the words printed beside
them (see receipt_base's module docstring). Three things drive the shape:

*An item is a line with a VAT code.* "T1 0.49" - a T, one digit, an amount -
is printed against every purchase and nothing else, so it is what separates
an item from a sub-total, the DUPLICATA banner or the discount block. The
recogniser often closes the space ("T10.49"); the code is one digit, so that
is still T1 and 0.49.

*Items sum to the pre-discount total, not the total paid.* Promotions print
in their own block rather than reducing the item lines (eleven of the
nineteen receipts carry one). The discount is the gap between the items' sum
and the amount paid - but only once the ticket itself prints that sum, as
its pre-discount total: taken on trust, a misread item price would pass as a
promotion and the receipt would balance while being wrong. It is then
charged to the products the block names (receipt_base.distribute_discount).

*A weight belongs to the item BELOW it.* "BRUTWEIGHT 0.920 KG @ 3.49 / KG"
is the orange's weight - 0.920 x 3.49 = 3.21, the orange's price - not the
apples' above it. When the price per kilo is legible the arithmetic decides;
see `_attach_weight`.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .base import ParseCheck, ParsedInvoice, ParsedLine, PdfPage
from .receipt_base import (
    CENTS,
    UNIT,
    ReceiptParser,
    ReceiptTotals,
    amount_printed,
    assign_rates_by_bucket,
    build_checks,
    collect_vat_summaries,
    compose_invoice_number,
    distribute_discount,
    ends_items,
    line_amounts,
    missing_item_check,
    printed_promotion,
    printed_total,
    quantity_checks,
    read_date,
    reconcile_quantity,
    to_ht,
    weight_on,
)
from .registry import register

# "BAGUETTE BLANC  T1 0.49" - a name with a letter in it, the VAT code, the
# amount.
ITEM_RE = re.compile(r"^(?P<name>.*[A-Za-z].*?)\s+T(?P<code>[0-9])\s*(?P<amount>\d{1,3}[.,]\d{2})")
# "BAGUETTE BLANC  T1 6 X 0.49  2.94" - the multiplier form, printed when the
# same product is scanned several times. Without it the whole line is
# skipped: every one of those items vanishes while the receipt still
# balances against its own printed total.
QUANTITY_ITEM_RE = re.compile(
    r"^(?P<name>.*[A-Za-z].*?)\s+T(?P<code>[0-9])\s*(?P<qty>\d{1,3})\s*[xX×]\s*"
    r"(?P<unit>\d{1,3}[.,]\d{2})\D{0,2}(?P<total>\d{1,4}[.,]\d{2})"
)
# "R1 005333-01 385" - store/till, then the ticket sequence.
TICKET_RE = re.compile(r"R\d\s*(\d{5,6}-\d{2})\s*(\d{2,4})")


@register
class FranprixParser(ReceiptParser):
    supplier_code = "FRANPRIX"
    # Matched against the OCR text to route a batch of photos to the right
    # parser without the operator picking a shop per file.
    header_patterns = (r"FRANPRIX",)

    def parse_pages(
        self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = ""
    ) -> ParsedInvoice:
        lines = [line for page in pages for line in page.text.split("\n") if line.strip()]
        total = printed_total(lines)

        parsed_lines: list[ParsedLine] = []
        coded_totals: dict[str, Decimal] = {}
        gross = Decimal("0")
        items_end = len(lines)
        # A weight waits for the item it belongs to - the next one on this
        # till (see _attach_weight).
        pending_weight: Decimal | None = None
        pending_price_per_kg: Decimal | None = None
        weight_problems: list[str] = []
        quantity_fixes: list[str] = []
        quantity_problems: list[str] = []
        for index, line in enumerate(lines):
            item = _read_item(line)
            if item is None:
                weighed = weight_on(line)
                if weighed is not None:
                    pending_weight, pending_price_per_kg = weighed
                    continue
                amounts = line_amounts(line)
                if pending_weight is not None and len(amounts) == 1 and "/" in line:
                    # "@ 3.49 / KG" on a line of its own, under its weight.
                    pending_price_per_kg = abs(amounts[0])
                    continue
                if amounts and ends_items(abs(amounts[-1]), len(parsed_lines), gross, gross, total):
                    items_end = index
                    break
                continue

            name, quantity, unit_price, amount, code = item
            if unit_price is not None:
                quantity = reconcile_quantity(name, quantity, unit_price, amount, quantity_fixes, quantity_problems)
            parsed_lines.append(
                ParsedLine(
                    raw_name=name,
                    quantity=quantity,
                    total_volume=Decimal("0"),
                    unit_cost_ht=Decimal("0"),  # filled in once the rate is known
                    total_ht=amount,  # still TTC here, converted below
                    vat_rate=Decimal("0"),
                    category=code,
                )
            )
            coded_totals[code] = coded_totals.get(code, Decimal("0")) + amount
            gross += amount
            if pending_weight is not None:
                _attach_weight(parsed_lines[-1:-3:-1], pending_weight, pending_price_per_kg, weight_problems)
                pending_weight = pending_price_per_kg = None
        if pending_weight is not None and parsed_lines:
            # Nothing followed it, so the last item is the only candidate.
            _attach_weight(parsed_lines[-1:], pending_weight, pending_price_per_kg, weight_problems)

        vat_summaries, rate_only = collect_vat_summaries(lines, total)
        totals = ReceiptTotals(printed_total_ttc=total, vat_summaries=vat_summaries, rate_only=rate_only)
        rates, confident = assign_rates_by_bucket(coded_totals, vat_summaries)
        if not rates and len(coded_totals) == 1 and totals.line_rate is not None:
            # One code on the ticket, one legible rate: nothing to join, so
            # nothing is guessed. The unread table still fails its own check.
            rates, confident = {code: totals.line_rate for code in coded_totals}, True
        extra_checks: list[ParseCheck] = quantity_checks(quantity_fixes, quantity_problems)
        if weight_problems:
            extra_checks.append(ParseCheck(label="Poids rattachés", passed=False, detail="; ".join(weight_problems)))
        if parsed_lines and not rates:
            extra_checks.append(
                ParseCheck(
                    label="Taux par article",
                    passed=False,
                    detail="Aucun taux de TVA n'a pu être rattaché aux codes T1/T2 du ticket.",
                )
            )
        elif not confident:
            extra_checks.append(
                ParseCheck(
                    label="Taux par article",
                    passed=False,
                    detail="Ticket à plusieurs taux : le rattachement code/taux n'est pas certain.",
                )
            )

        lines_total_ttc = Decimal("0")
        line_codes = [line.category for line in parsed_lines]
        for parsed_line in parsed_lines:
            rate = rates.get(parsed_line.category)
            if rate is None:
                # No rate could be proven for this code. Leaving it at 0
                # would quietly book the TTC price as an HT cost, so the line
                # keeps its printed amount and the failed check above sends
                # the whole receipt to review.
                rate = Decimal("0")
            lines_total_ttc += parsed_line.total_ht
            parsed_line.printed_ttc = parsed_line.total_ht
            parsed_line.vat_rate = rate
            parsed_line.total_ht = to_ht(parsed_line.total_ht, rate)
            parsed_line.unit_cost_ht = (
                (parsed_line.total_ht / parsed_line.quantity).quantize(UNIT, rounding=ROUND_HALF_UP)
                if parsed_line.quantity
                else Decimal("0")
            )
            parsed_line.category = "Franprix"

        # The promotion: the gap between what the items add up to and what
        # was paid - once the ticket confirms the items' sum by printing it.
        discount_ttc = Decimal("0")
        gross_line = None
        if total is not None and gross - total > CENTS:
            gross_line = amount_printed(lines, gross, items_end)
            if gross_line is not None:
                discount_ttc = gross - total

        missing = missing_item_check(gross, printed_promotion(lines, items_end, total))
        if missing is not None:
            extra_checks.append(missing)

        if discount_ttc > 0 and parsed_lines:
            paid_line = amount_printed(lines, total, gross_line + 1)
            names = _discounted_names(lines, gross_line, paid_line)
            # The lines' own `category` has already been rewritten to the shop
            # name by now, so the rate comes from the codes captured before
            # that - reading it back off the line would silently yield 0 and
            # book a TTC discount as an HT one.
            rate = rates.get(line_codes[0], Decimal("0")) if rates and line_codes else Decimal("0")
            attributed, _unattributed = distribute_discount(parsed_lines, to_ht(discount_ttc, rate), names)
            lines_total_ttc -= discount_ttc
            extra_checks.append(
                ParseCheck(
                    label="Remise attribuée",
                    passed=attributed,
                    detail=(
                        f"remise {discount_ttc:.2f} € répartie sur les articles concernés"
                        if attributed
                        else f"remise {discount_ttc:.2f} € répartie au prorata : "
                        "le produit concerné n'a pas été identifié"
                    ),
                )
            )

        full_text = "\n".join(lines)
        invoice_date = read_date(full_text, date_hint)
        ticket_match = TICKET_RE.search(full_text.replace(" ", ""))
        ticket = "-".join(ticket_match.groups()) if ticket_match else ""

        lines_total_ht = sum((line.total_ht for line in parsed_lines), start=Decimal("0"))
        checks, adjustment = build_checks(
            totals,
            lines_total_ttc,
            lines_total_ht=lines_total_ht,
            line_count=len(parsed_lines),
            extra=extra_checks,
        )

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=compose_invoice_number(ticket, invoice_date, total),
            invoice_date=invoice_date,
            lines=parsed_lines,
            reconciliation_adjustment=adjustment,
            checks=checks,
            printed_total_ttc=totals.printed_total_ttc,
        )


def _discounted_names(lines: list[str], start: int, end: int | None) -> list[str]:
    """Product names mentioned in the discount block.

    The block sits between the line printing the pre-discount total and the
    next line printing the amount paid - located by those two numbers, since
    its own heading is read differently on every ticket ("Detai des renises
    innediates"). Every alphabetic run in it is offered as a candidate name;
    one that isn't a product ("TOTAL remise", the heading) simply fails to
    match any item and costs nothing.
    """
    names: list[str] = []
    for line in lines[start + 1 : end if end is not None else len(lines)]:
        without_amounts = re.sub(r"\d+[.,]\d{2}", " ", line)
        for run in re.findall(r"[A-Za-zÀ-ÿ]{4,}(?:\s+[A-Za-zÀ-ÿ]{2,})*", without_amounts):
            if run.strip():
                names.append(run.strip())
    return names


def _decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", "."))


def _read_item(line: str):
    """(name, quantity, unit price or None, line total TTC, VAT code), or None.

    The multiplier form is tried first: its "6 X 0.49" also contains
    something the single-item pattern would read."""
    match = QUANTITY_ITEM_RE.match(line)
    if match:
        return (
            match.group("name").strip(),
            int(match.group("qty")),
            _decimal(match.group("unit")),
            _decimal(match.group("total")),
            f"T{match.group('code')}",
        )
    match = ITEM_RE.match(line)
    if match:
        return match.group("name").strip(), 1, None, _decimal(match.group("amount")), f"T{match.group('code')}"
    return None


def _attach_weight(candidates, weight: Decimal, price_per_kg: Decimal | None, problems: list[str]) -> None:
    """Give a weighed item its weight.

    On this till the weight is printed BEFORE its item: "BRUTWEIGHT 0.920 KG
    @ 3.49 / KG" then "ORANGE T1 3.21" - 0.920 x 3.49 is 3.21, the orange,
    not the item above. `candidates` is the item below first, then the one
    above. With a legible price per kilo the arithmetic picks between them;
    a weight matching neither goes below, and is reported.
    """
    if price_per_kg is not None:
        expected = (weight * price_per_kg).quantize(CENTS, rounding=ROUND_HALF_UP)
        for candidate in candidates:
            if abs(candidate.total_ht - expected) <= CENTS:  # still the printed TTC amount here
                candidate.total_volume = weight
                return
        problems.append(f"{weight} kg x {price_per_kg} €/kg = {expected} € : aucun article voisin à ce prix")
    candidates[0].total_volume = weight
