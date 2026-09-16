"""Wing Seng till receipts, photographed.

    WING SENG
    2 RUE REBEVAL
    75019 PARIS
    Ticket:000197              02/06/2026 17H06
    Caisse N:03                Vendeur N:03
                                          EUR
    MENTHE                               1.00
      2 x        0.50EUR
    #CITRON VERT                        10.05
      MAN  3.360kg x   2.99EUR/kg
    S TOTAL EUR:                        11.05
    Recu CARTE BLEUE:                   11.05
    TOTAL EUR:                          11.05
    TVA 5.50 %:      0.58 EUR

Large, clean print - the easiest of the four to read, and the one whose
structure is most likely to be misread *semantically* rather than optically.
Each item is a name and a line total, followed by a detail line saying how
that total was arrived at: a count and a unit price ("2 x 0.50EUR") or a
weight and a price per kilo ("MAN 3.360kg x 2.99EUR/kg").

Those detail lines carry two real numbers, and a reader that treats one as
an item invents a purchase - a vision model did exactly that to this
receipt during evaluation. Here they are recognised by their arithmetic (a
count or a three-decimal weight, times a unit price) and folded into the
item above. The same arithmetic recovers an item whose own amount faded:
a bare "MENTHE" followed by "2 x 0.50EUR" cost 1.00.

Where the items stop is found by arithmetic too (receipt_base.ends_items),
never by the word "TOTAL": the recogniser once read "S TOTAL EUR" as
"TOTAL EOR", and the total became a purchase.

The VAT table is a single line with no base and no total, so the receipt's
own grand total supplies the missing side (receipt_base.finalise_summary).
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
    build_checks,
    collect_vat_summaries,
    compose_invoice_number,
    ends_items,
    line_amounts,
    printed_total,
    quantity_checks,
    read_date,
    reconcile_quantity,
    to_ht,
    weight_on,
)
from .registry import register

# "MENTHE   1.00" / "#CITRON VERT   10.05". The leading "#" marks a weighed
# item on this till and is dropped from the name.
ITEM_RE = re.compile(r"^#?\s*(?P<name>.*[A-Za-z].*?)\s+(?P<total>\d{1,4}[.,]\d{2})\s*(?:EUR|€)?\s*$")
# "2 x   0.50EUR" - a count and a unit price.
COUNT_DETAIL_RE = re.compile(
    r"^\s*(?P<quantity>\d{1,3})\s*[xX*×]\s*(?P<unit_price>\d{1,4}[.,]\d{2})\s*(?:EUR|€)?\s*$"
)
TICKET_RE = re.compile(r"Ticket\s*:?\s*(\d{3,10})", re.IGNORECASE)


@register
class WingSengParser(ReceiptParser):
    supplier_code = "WINGSENG"
    header_patterns = (r"WING\s*SENG",)

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
        printed_amounts: list[Decimal] = []  # each line's TTC amount, as printed or computed
        quantity_fixes: list[str] = []
        quantity_problems: list[str] = []
        weight_problems: list[str] = []
        computed: list[str] = []
        pending_name: str | None = None
        gross = Decimal("0")

        def add(name: str, quantity: int, total_ttc: Decimal, volume: Decimal = Decimal("0")) -> None:
            parsed_lines.append(
                ParsedLine(
                    raw_name=name,
                    quantity=quantity,
                    total_volume=volume,
                    unit_cost_ht=Decimal("0"),
                    total_ht=to_ht(total_ttc, effective_rate),
                    vat_rate=effective_rate,
                    category="Wing Seng",
                    printed_ttc=total_ttc,
                )
            )
            printed_amounts.append(total_ttc)

        for line in lines:
            # Detail lines first: they also look like "<words> <amount>", and
            # treating one as an item fabricates a purchase.
            weighed = weight_on(line)
            if weighed is not None and weighed[1] is not None:
                weight, per_kg = weighed
                expected = (weight * per_kg).quantize(CENTS, rounding=ROUND_HALF_UP)
                if pending_name is not None:
                    add(pending_name, 1, expected, volume=weight)
                    gross += expected
                    computed.append(f"{pending_name} : {weight} kg x {per_kg} €/kg")
                elif parsed_lines:
                    parsed_lines[-1].total_volume = weight
                    if abs(printed_amounts[-1] - expected) > CENTS:
                        weight_problems.append(
                            f"{parsed_lines[-1].raw_name} : {weight} kg x {per_kg} €/kg = {expected} € "
                            f"≠ {printed_amounts[-1]} €"
                        )
                pending_name = None
                continue
            count_detail = COUNT_DETAIL_RE.match(line)
            if count_detail:
                quantity = int(count_detail.group("quantity"))
                unit_price = Decimal(count_detail.group("unit_price").replace(",", "."))
                if pending_name is not None:
                    amount = (quantity * unit_price).quantize(CENTS)
                    add(pending_name, quantity, amount)
                    gross += amount
                    computed.append(f"{pending_name} : {quantity} x {unit_price} €")
                elif parsed_lines:
                    parsed_lines[-1].quantity = reconcile_quantity(
                        parsed_lines[-1].raw_name,
                        quantity,
                        unit_price,
                        printed_amounts[-1],
                        quantity_fixes,
                        quantity_problems,
                    )
                pending_name = None
                continue

            amounts = line_amounts(line)
            if amounts and ends_items(abs(amounts[-1]), len(parsed_lines), gross, gross, total):
                break
            match = ITEM_RE.match(line)
            name = match.group("name").strip() if match else ""
            if match and re.search(r"[A-Za-zÀ-ÿ]{3,}", name):
                total_ttc = Decimal(match.group("total").replace(",", "."))
                if total_ttc > 0:
                    add(name, 1, total_ttc)
                    gross += total_ttc
                    pending_name = None
                    continue
            # A product name whose amount did not come out ("MENTHE", its
            # "1.00" faded to nothing): the detail line under it, if there is
            # one, still says what it cost. Anything else resets it.
            pending_name = _bare_name(line)

        extra_checks: list[ParseCheck] = quantity_checks(quantity_fixes, quantity_problems)
        if computed:
            extra_checks.append(
                ParseCheck(
                    label="Montants recalculés",
                    passed=True,
                    detail="montant illisible, calculé depuis le détail imprimé : " + "; ".join(computed),
                )
            )
        if weight_problems:
            extra_checks.append(
                ParseCheck(label="Poids x prix au kilo = montant", passed=False, detail="; ".join(weight_problems))
            )

        for parsed_line in parsed_lines:
            parsed_line.unit_cost_ht = (
                (parsed_line.total_ht / parsed_line.quantity).quantize(UNIT, rounding=ROUND_HALF_UP)
                if parsed_line.quantity
                else Decimal("0")
            )

        full_text = "\n".join(lines)
        invoice_date = read_date(full_text, date_hint)
        ticket_match = TICKET_RE.search(full_text)
        ticket = ticket_match.group(1) if ticket_match else ""

        lines_total_ht = sum((line.total_ht for line in parsed_lines), start=Decimal("0"))
        checks, adjustment = build_checks(
            totals,
            sum(printed_amounts, start=Decimal("0")),
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


def _bare_name(line: str) -> str | None:
    """A line that is only a product name - letters, and no figure at all."""
    if re.search(r"\d", line) or not re.search(r"[A-Za-zÀ-ÿ]{3,}", line):
        return None
    return line.strip().lstrip("#").strip()
