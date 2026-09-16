"""Monoprix till receipts, photographed.

The cleanest of the four layouts, and the only one that groups its items
under department headings:

    MONOPRIX
    70/72 RUE DU FG DU TEMPLE
    EPICERIE/BOISSONS.................
    DADDY CASSONADE PU            2,65€
    FRUITS/LEGUMES...................
    4 X AROM.BASILIC     1,89€   7,56€
    TOTAL HORS AVANTAGES         10,21€
    NOMBRE D'ARTICLES                   5
    RESTE A PAYER                10,21€
    TVA        H.T.    T.V.A.     T.T.C
    5,5%       9,68     0,53      10,21

Monoprix prints no per-item VAT code - the rate comes from the table at the
bottom, which is why `ReceiptTotals.default_rate` refuses to guess when a
receipt carries more than one bucket.

Every line here has the shape "<words> <amount>", payment and totals
included, so nothing about a line's look says it is not a product. Where the
items stop is therefore found by arithmetic (receipt_base.ends_items): the
first line repeating what the items add up to is a total, however the
recogniser spelled it. Keying on the words instead broke once per engine -
"TOTAAL EX PROHO" with one, "ITOTNAGE AHGS AGES" and "IOTAL HURS PROHOTION"
with the next.

Two variants show up across the eleven receipts - a French and a
Dutch-language till - and the VAT table is either the four-column one above
or a block printing HT to four decimals ("3.0237"); `parse_vat_line` reads
both without being told which.

A promotion prints as its own negative line directly under the item it
reduces ("Le 2eme a moins 50% -3,58€") and is charged to that item. The
department headings are kept as the line's `category`: they are the shop's
own classification, and the only thing on the receipt telling a food item
from a household one when both sit at the same rate.
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
)
from .registry import register

# "4 X AROM.BASILIC   1,89€   7,56€" - quantity, name, unit price, total.
# The optional leading letter is the VAT code the Dutch-language variant
# prints in front of the quantity ("T3XBASILIC"); without it those lines fell
# through to the plain pattern, which read the count as part of the name.
QUANTITY_ITEM_RE = re.compile(
    r"^(?P<code>[A-Z])?\s*(?P<qty>\d{1,3})\s*[X×]\s*(?P<name>.*[A-Za-z].*?)\s+(?P<unit>\d{1,4}[.,]\d{2})\s*"
    r"(?:E|EUR|€)?\s+(?P<total>\d{1,4}[.,]\d{2})\s*(?:E|EUR|€)?\s*$",
    re.IGNORECASE,
)
# "DADDY CASSONADE PU   2,65€" - a plain priced line.
SIMPLE_ITEM_RE = re.compile(
    r"^(?P<name>.*[A-Za-z].*?)\s+(?P<total>-?\d{1,4}[.,]\d{2})\s*(?:E|EUR|€)?\s*$",
    re.IGNORECASE,
)
# "EPICERIE/BOISSONS........." - a department heading: capitals, a dot
# leader, no amount.
HEADING_RE = re.compile(r"^(?P<name>[A-ZÀ-Ÿ][A-ZÀ-Ÿ/ .'-]{3,}?)\.+\s*$")
# The barcode along the bottom is the only genuinely unique number on the
# ticket; the "245 93 4042 930" above it repeats across visits.
BARCODE_RE = re.compile(r"(?<!\d)(\d{18,26})(?!\d)")


@register
class MonoprixParser(ReceiptParser):
    supplier_code = "MONOPRIX"
    header_patterns = (r"MONOPRIX",)

    def parse_pages(
        self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = ""
    ) -> ParsedInvoice:
        lines = [line for page in pages for line in page.text.split("\n") if line.strip()]
        total = printed_total(lines)

        vat_summaries, rate_only = collect_vat_summaries(lines, total)
        totals = ReceiptTotals(printed_total_ttc=total, vat_summaries=vat_summaries, rate_only=rate_only)
        rate = totals.line_rate
        effective_rate = rate if rate is not None else Decimal("0")

        parsed_lines: list[ParsedLine] = []
        # (index into parsed_lines, TTC amount): a promotion printed as its
        # own negative line, directly under the item it reduces.
        item_discounts: list[tuple[int, Decimal]] = []
        quantity_fixes: list[str] = []
        quantity_problems: list[str] = []
        category = ""
        gross = net = Decimal("0")
        items_end = len(lines)
        for index, line in enumerate(lines):
            amounts = line_amounts(line)
            if amounts and ends_items(abs(amounts[-1]), len(parsed_lines), gross, net, total):
                items_end = index
                break
            heading = HEADING_RE.match(line)
            if heading:
                category = heading.group("name").strip(" .")
                continue
            discount = _read_discount(line)
            if discount is not None:
                if parsed_lines:
                    item_discounts.append((len(parsed_lines) - 1, discount))
                    net -= discount
                continue
            parsed = _read_item(line)
            if parsed is None:
                continue
            name, quantity, total_ttc, unit_price = parsed
            if unit_price is not None:
                quantity = reconcile_quantity(name, quantity, unit_price, total_ttc, quantity_fixes, quantity_problems)
            gross += total_ttc
            net += total_ttc
            total_ht = to_ht(total_ttc, effective_rate)
            parsed_lines.append(
                ParsedLine(
                    raw_name=name,
                    quantity=quantity,
                    total_volume=Decimal("0"),
                    unit_cost_ht=(total_ht / quantity).quantize(UNIT, rounding=ROUND_HALF_UP)
                    if quantity
                    else Decimal("0"),
                    total_ht=total_ht,
                    vat_rate=effective_rate,
                    category=category or "Monoprix",
                )
            )

        extra_checks: list[ParseCheck] = quantity_checks(quantity_fixes, quantity_problems)
        if parsed_lines and rate is None:
            extra_checks.append(
                ParseCheck(
                    label="Taux applicable",
                    passed=False,
                    detail=(
                        "Ticket à plusieurs taux de TVA : Monoprix n'imprime pas de code par "
                        "article, le taux de chaque ligne doit être choisi à la main."
                    ),
                )
            )

        # A promotion is the gap between what the items add up to and what
        # was paid, confirmed either by the ticket printing the items' sum as
        # its pre-discount total, or by promotion lines that bring the items
        # exactly to the amount paid. Unconfirmed, it would absorb a misread
        # price.
        lines_total_ttc = gross
        named_discount = sum((amount for _index, amount in item_discounts), start=Decimal("0"))
        discount_ttc = Decimal("0")
        if total is not None and gross - total > CENTS:
            printed_sum = amount_printed(lines, gross, items_end) is not None
            if printed_sum or (item_discounts and abs(net - total) <= CENTS):
                discount_ttc = gross - total
        missing = missing_item_check(gross, printed_promotion(lines, items_end, total))
        if missing is not None:
            extra_checks.append(missing)
        if discount_ttc > 0 and parsed_lines:
            if item_discounts and abs(named_discount - discount_ttc) <= CENTS:
                for line_index, amount in item_discounts:
                    target = parsed_lines[line_index]
                    _apply_discount(target, to_ht(amount, target.vat_rate))
                extra_checks.append(
                    ParseCheck(
                        label="Remise attribuée",
                        passed=True,
                        detail=f"remise {discount_ttc:.2f} € imputée à l'article sous lequel elle est imprimée",
                    )
                )
            else:
                distribute_discount(parsed_lines, to_ht(discount_ttc, effective_rate), [])
                extra_checks.append(
                    ParseCheck(
                        label="Remise attribuée",
                        passed=False,
                        detail=(
                            f"remise {discount_ttc:.2f} € répartie au prorata : le ticket ne dit pas "
                            "à quel produit elle revient"
                        ),
                    )
                )
            lines_total_ttc -= discount_ttc

        full_text = "\n".join(lines)
        invoice_date = read_date(full_text, date_hint)
        barcode_match = BARCODE_RE.search(full_text.replace(" ", ""))
        ticket = barcode_match.group(1) if barcode_match else ""

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
        )


def _read_discount(line: str) -> Decimal | None:
    """A promotion printed as its own line - "Le 2eme a moins 50% -3,58€" -
    as a positive TTC amount. The only negative amount among the items."""
    match = SIMPLE_ITEM_RE.match(line)
    if not match:
        return None
    amount = Decimal(match.group("total").replace(",", "."))
    return -amount if amount < 0 else None


def _apply_discount(line: ParsedLine, share_ht: Decimal) -> None:
    line.discount = (line.discount or Decimal("0")) + share_ht
    line.total_ht -= share_ht
    line.unit_cost_ht = (
        (line.total_ht / line.quantity).quantize(UNIT, rounding=ROUND_HALF_UP) if line.quantity else Decimal("0")
    )


def _read_item(line: str) -> tuple[str, int, Decimal, Decimal | None] | None:
    """(name, quantity, total TTC, unit price or None)."""
    match = QUANTITY_ITEM_RE.match(line)
    if match:
        return (
            match.group("name").strip(),
            int(match.group("qty")),
            Decimal(match.group("total").replace(",", ".")),
            Decimal(match.group("unit").replace(",", ".")),
        )
    match = SIMPLE_ITEM_RE.match(line)
    if match:
        name = match.group("name").strip()
        # A line that is only digits and punctuation ("0143571480  0,00") is
        # a phone number or a till code, not a product.
        if not re.search(r"[A-Za-zÀ-ÿ]{3,}", name):
            return None
        total = Decimal(match.group("total").replace(",", "."))
        if total <= 0:
            return None
        return name, 1, total, None
    return None
