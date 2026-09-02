"""Parser for SCEA Plou & Fils invoices - a clean, well-structured layout
where pdfplumber's own table extraction works directly (unlike Cecina's,
which needs a text-regex fallback - see cecina.py). Two tables per page:
the product lines (6 columns: Désignation, Qté, Px U. HT, Px U. TTC, HT,
TTC) and a VAT summary (5 columns: Libellé, Hors taxe, TVA, TVA réglée,
TTC) - told apart by column count alone, which is simpler and more
reliable here than trying to detect either table's header text.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

DOCUMENT_NUMBER_REGEX = re.compile(r"N°\s*document\s*:\s*(\S+)")
DATE_REGEX = re.compile(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})")
# "Taux 20.00%" in the VAT summary row - printed with a decimal POINT here,
# unlike Cecina's comma - _to_decimal handles either.
TAUX_REGEX = re.compile(r"Taux\s+(\d+[.,]\d+)\s*%")

PRODUCT_ROW_COLUMNS = 6


def _to_decimal(text: str | None, default: str = "0") -> Decimal:
    if not text:
        return Decimal(default)
    text = text.strip().replace("€", "").replace(",", ".")
    if not text:
        return Decimal(default)
    try:
        return Decimal(text)
    except Exception:
        return Decimal(default)


@register
class PlouFilsParser(InvoiceParser):
    supplier_code = "PLOUFILS"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        all_rows: list[list] = []
        full_text_parts: list[str] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                full_text_parts.append(page.extract_text() or "")
                for table in page.extract_tables():
                    all_rows.extend(table)

        full_text = "\n".join(full_text_parts)

        rate_match = TAUX_REGEX.search(full_text)
        # A single overall rate is assumed (the only shape seen so far) -
        # French wine invoices are near-universally 20%, used as a sane
        # fallback if the summary row's own text can't be found/parsed.
        vat_rate = (_to_decimal(rate_match.group(1)) / Decimal("100")) if rate_match else Decimal("0.20")

        parsed_lines = []
        for row in all_rows:
            if len(row) != PRODUCT_ROW_COLUMNS:
                continue
            designation, quantity_raw, _unit_ht, _unit_ttc, total_ht_raw, _total_ttc = row
            if not designation or not quantity_raw:
                continue
            try:
                quantity = int(quantity_raw)
            except (TypeError, ValueError):
                continue  # the header row ("Qté" isn't an int) lands here too
            total_ht = _to_decimal(total_ht_raw)
            parsed_lines.append(
                ParsedLine(
                    raw_name=designation.strip(),
                    quantity=quantity,
                    total_volume=Decimal("0"),
                    unit_cost_ht=(total_ht / quantity).quantize(Decimal("0.0001")) if quantity else Decimal("0"),
                    total_ht=total_ht,
                    vat_rate=vat_rate,
                )
            )

        number_match = DOCUMENT_NUMBER_REGEX.search(full_text)
        invoice_number = number_match.group(1) if number_match else ""

        invoice_date = date_hint
        date_match = DATE_REGEX.search(full_text)
        if date_match:
            try:
                invoice_date = datetime.strptime(date_match.group(1), "%d/%m/%Y").date()
            except ValueError:
                pass

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=parsed_lines,
        )
