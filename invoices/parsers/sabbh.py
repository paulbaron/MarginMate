"""Sabbh Oriental till receipts, photographed.

    Sabbh Oriental
    68 Rue Du Fb Du Temple
    Vendeur: V1 Baba .. Numero de ticket: 6888884
    Heure: 14-07-2026 14:49:26
    PLU            kg(pcs)   EUR/kg(pcs)          EUR
    Article divers
                     3pcs          0,70      2,10 A
    Article divers
                     2pcs          4,90      9,80 A
    Articles: 4                    Total: 16,90
    Information TVA
              Taux        Base TVA         TVA
    TVA A     5,50%          16,90        0,88

**This till prints no product names.** Every line reads "Article divers",
and the only thing distinguishing a lemon from a bunch of mint is its unit
price. That is why `ParsedLine.is_placeholder` exists: the parser records
what it can actually see - quantity, unit price, line total, VAT code - and
the name is filled in afterwards from the shop's own price list
(`invoices.ShopItemPrice`), which the operator builds up from the review
screen one price at a time.

The unit price is the key, not the line total: 0,70 appears as 3pcs/2,10,
6pcs/4,20, 7pcs/4,90 and 11pcs/7,70 across the five receipts, so keying on
the total would need a new mapping for every quantity ever bought while
keying on the unit price needs one per product.

"Base TVA" here is the tax-INCLUSIVE total, unlike Franprix and Monoprix -
`VatSummary.resolve` works that out from the arithmetic rather than being
told. The count is checked against unit price x total, and the total is the
amount the ticket prints most (receipt_base.printed_total), not whatever
follows the word "Total".
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
    assign_rates_by_bucket,
    build_checks,
    collect_vat_summaries,
    compose_invoice_number,
    printed_total,
    quantity_checks,
    read_date,
    reconcile_quantity,
    to_ht,
)
from .registry import register

PLACEHOLDER_NAME = "Article divers"

# "3pcs   0,70   2,10 A" and its weighed form "0.850kg  4,90  4,17 A": the
# quantity and its unit, the unit price, the line total, the VAT code.
DETAIL_RE = re.compile(
    r"(?:^|\s)(?P<quantity>\d{1,3}(?:[.,]\d{1,3})?)\s*(?P<unit>pcs|kg)\s+"
    r"(?P<unit_price>\d{1,4}[.,]\d{2})\s+(?P<total>\d{1,4}[.,]\d{2})\s*(?P<code>[A-C])?\s*$",
    re.IGNORECASE,
)
TICKET_RE = re.compile(r"ticket\s*:?\s*(\d{4,10})", re.IGNORECASE)


@register
class SabbhParser(ReceiptParser):
    supplier_code = "SABBH"
    header_patterns = (r"SAB+H", r"SABAH")

    def parse_pages(
        self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = ""
    ) -> ParsedInvoice:
        lines = [line for page in pages for line in page.text.split("\n") if line.strip()]
        total = printed_total(lines)

        parsed_lines: list[ParsedLine] = []
        coded_totals: dict[str, Decimal] = {}
        quantity_fixes: list[str] = []
        quantity_problems: list[str] = []
        weight_problems: list[str] = []
        for line in lines:
            match = DETAIL_RE.search(line)
            if not match:
                continue
            unit_price = Decimal(match.group("unit_price").replace(",", "."))
            total_ttc = Decimal(match.group("total").replace(",", "."))
            raw_quantity = Decimal(match.group("quantity").replace(",", "."))
            weighed = match.group("unit").lower() == "kg"
            code = (match.group("code") or "A").upper()

            if weighed:
                quantity = 1
                expected = (raw_quantity * unit_price).quantize(CENTS, rounding=ROUND_HALF_UP)
                if abs(expected - total_ttc) > CENTS:
                    weight_problems.append(f"{raw_quantity} kg x {unit_price} € = {expected} € ≠ {total_ttc} €")
            else:
                # The count is the one figure nothing else checks; when it
                # disagrees with unit price x total, the money wins.
                quantity = reconcile_quantity(
                    PLACEHOLDER_NAME, int(raw_quantity), unit_price, total_ttc, quantity_fixes, quantity_problems
                )

            parsed_lines.append(
                ParsedLine(
                    raw_name=PLACEHOLDER_NAME,
                    quantity=max(quantity, 1),
                    total_volume=raw_quantity if weighed else Decimal("0"),
                    unit_cost_ht=unit_price,  # TTC for now; converted below
                    total_ht=total_ttc,  # TTC for now; converted below
                    vat_rate=Decimal("0"),
                    category=code,
                    is_placeholder=True,
                )
            )
            coded_totals[code] = coded_totals.get(code, Decimal("0")) + total_ttc

        extra_checks: list[ParseCheck] = quantity_checks(quantity_fixes, quantity_problems)
        if weight_problems:
            extra_checks.append(
                ParseCheck(label="Poids x prix au kilo = montant", passed=False, detail="; ".join(weight_problems))
            )

        vat_summaries, rate_only = collect_vat_summaries(lines, total)
        totals = ReceiptTotals(printed_total_ttc=total, vat_summaries=vat_summaries, rate_only=rate_only)
        rates, confident = assign_rates_by_bucket(coded_totals, vat_summaries)
        if not rates and len(coded_totals) == 1 and totals.line_rate is not None:
            # One code, one legible rate: nothing to join, nothing guessed.
            rates, confident = {code: totals.line_rate for code in coded_totals}, True
        if parsed_lines and not rates:
            extra_checks.append(
                ParseCheck(
                    label="Taux par article",
                    passed=False,
                    detail="Aucun taux de TVA n'a pu être rattaché aux codes du ticket.",
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
        for parsed_line in parsed_lines:
            rate = rates.get(parsed_line.category, Decimal("0"))
            lines_total_ttc += parsed_line.total_ht
            parsed_line.printed_ttc = parsed_line.total_ht
            parsed_line.vat_rate = rate
            parsed_line.total_ht = to_ht(parsed_line.total_ht, rate)
            parsed_line.unit_cost_ht = (
                (parsed_line.total_ht / parsed_line.quantity).quantize(UNIT, rounding=ROUND_HALF_UP)
                if parsed_line.quantity
                else Decimal("0")
            )
            parsed_line.category = "Sabbh Oriental"

        full_text = "\n".join(lines)
        invoice_date = read_date(full_text, date_hint)
        ticket_match = TICKET_RE.search(full_text)
        ticket = ticket_match.group(1) if ticket_match else ""

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
