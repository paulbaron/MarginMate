"""Parser for E.U.R.L Cecina (Vignerons de Cessenon) invoices.

Line format, one product per line (occasionally two - see below), as plain
text (pdfplumber's own table extraction merges multi-line designation cells
unpredictably on this layout, so a line-based regex over the raw text is
more reliable here):

    CODE  DESIGNATION  UNITE  QUANTITE  [PRIX_UNITAIRE  MONTANT_HT]  CODE_TVA

A product bought as part of a "buy N get M free" offer prints as TWO
consecutive lines sharing the same code/designation: one with a quantity,
unit price and amount (the paid units), one with only a quantity (the free
units - no unit price/amount at all). Both lines are folded into one
ParsedLine per product code so quantity reflects every physical unit
actually received while total_ht only counts what was actually paid - the
free units aren't priceless, they lower the true average cost of every
unit of that product, which is exactly what dividing total_ht by the
combined quantity gives.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

LINE_REGEX = re.compile(
    r"^(\S+)\s+(.+?)\s+(\S+)\s+(\d+)\s+(?:(\d+,\d+)\s+(\d+,\d+)\s+)?(\d)\s*$"
)
REFERENCE_REGEX = re.compile(r"Référence\s*:\s*(\S+)")
DATE_REGEX = re.compile(r"Date\s*:\s*(\d{2}/\d{2}/\d{2})")
# "Code Taux Montant" footer table - the only lines with this exact "digit
# taux,xx montant,xx" shape are the TVA codes actually used on this invoice,
# e.g. "4 20,00 60,20" (code 4 = 20.00%, collecting 60,20 EUR of VAT) - codes
# printed with no taux/montant (unused on this invoice) are simply skipped.
TVA_CODE_REGEX = re.compile(r"^(\d)\s+(\d+,\d+)\s+(\d+,\d+)\s*$", re.MULTILINE)


def _to_decimal(text: str | None, default: str = "0") -> Decimal:
    if not text:
        return Decimal(default)
    text = text.strip().replace(",", ".")
    if not text:
        return Decimal(default)
    try:
        return Decimal(text)
    except Exception:
        return Decimal(default)


@register
class CecinaParser(InvoiceParser):
    supplier_code = "CECINA"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        lines_by_code: dict[str, ParsedLine] = {}
        full_text_parts: list[str] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text_parts.append(text)

                tva_rate_by_code = {
                    match.group(1): _to_decimal(match.group(2)) / Decimal("100")
                    for match in TVA_CODE_REGEX.finditer(text)
                }

                for line in text.split("\n"):
                    match = LINE_REGEX.match(line.strip())
                    if not match:
                        continue
                    code, designation, unit, quantity_raw, unit_price_raw, amount_raw, tva_code = match.groups()
                    if unit.lower() not in ("unité", "carton", "colis"):
                        continue  # a stray numeric-looking line that isn't really a product row
                    quantity = int(quantity_raw)
                    amount = _to_decimal(amount_raw)
                    vat_rate = tva_rate_by_code.get(tva_code, Decimal("0"))

                    parsed_line = lines_by_code.get(code)
                    if parsed_line is None:
                        lines_by_code[code] = ParsedLine(
                            raw_name=designation.strip(),
                            quantity=quantity,
                            total_volume=Decimal("0"),
                            unit_cost_ht=Decimal("0"),
                            total_ht=amount,
                            vat_rate=vat_rate,
                            ean=code,
                        )
                    else:
                        parsed_line.quantity += quantity
                        parsed_line.total_ht += amount

        for parsed_line in lines_by_code.values():
            if parsed_line.quantity:
                parsed_line.unit_cost_ht = (parsed_line.total_ht / parsed_line.quantity).quantize(Decimal("0.0001"))

        full_text = "\n".join(full_text_parts)
        ref_match = REFERENCE_REGEX.search(full_text)
        invoice_number = ref_match.group(1) if ref_match else ""

        invoice_date = date_hint
        date_match = DATE_REGEX.search(full_text)
        if date_match:
            try:
                invoice_date = datetime.strptime(date_match.group(1), "%d/%m/%y").date()
            except ValueError:
                pass

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=list(lines_by_code.values()),
        )
