"""Parser for UBA (Le Dipsomaniac) invoices, ported from the original
ScrapBarInvoices regex/table extractor. UBA's PDF puts VAT info only in the
free-flowing text (one letter code per line) but the actual product rows only
show up cleanly via pdfplumber's table extraction, so we cross-reference the
two: first pass over the text to build a {product -> vat rate} map, second
pass over the extracted table for quantities/prices.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

TVA_INDEX_TO_RATE = {1: Decimal("0.2"), 2: Decimal("0.055"), 3: Decimal("0")}

LINE_REGEX = re.compile(
    r"([A-Z0-9]+)\s+(.+?)\s+(-?\d+)\s+(FUT|CAR|CAI|BT|EMB)\s(-?\d+)\s+(L|BT|BOI|EMB)\s+(\d+,\d+)\s+(-?\d+,\d+)\s+"
    r"(\d+,\d+\s*\%\s+)?(\d+,\d+\s+)?(-?\d+,\d+\s+)?(-?\d+,\d+\s+)?(\d+,\d\d\s*\s+)?(\d+,\d\d)\s+(-?\d+,\d\d\s+)"
    r"(-?\d+,\d\d\s+)?(-?\d+,\d\d\s+)([1-3])"
)
QUANTITY_REGEX = re.compile(r"(-?\d+)\s+(FUT|CAR|CAI|BT|EMB)")
INVOICE_NUMBER_REGEX = re.compile(r"Facture No\s*:\s*(\S+)")
DATE_SECTION_MARKER = "DATE FACTURE"
DATE_REGEX = re.compile(r"(\d{2}/\d{2}/\d{4})")


def _to_decimal(text: str | None, default: str = "0") -> Decimal:
    if not text:
        return Decimal(default)
    text = text.strip().replace(",", ".").replace("%", "").strip()
    if not text:
        return Decimal(default)
    try:
        return Decimal(text)
    except Exception:
        return Decimal(default)


def _to_int(text: str | None, default: int = 0) -> int:
    if not text:
        return default
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        return default


def _row_is_valid(row) -> bool:
    return bool(row[1]) and bool(row[2]) and bool(row[5]) and bool(row[12]) and bool(row[13])


def _guess_invoice_number_and_date(full_text: str) -> tuple[str, date | None]:
    number_match = INVOICE_NUMBER_REGEX.search(full_text)
    invoice_number = number_match.group(1) if number_match else ""

    invoice_date = None
    marker_idx = full_text.find(DATE_SECTION_MARKER)
    if marker_idx != -1:
        # "DATE COMMANDE" and "DATE FACTURE" values land on the same line, in
        # that column order, e.g. "... 12/11/2024 15/11/2024" - FACTURE is last.
        window = full_text[marker_idx : marker_idx + 200]
        date_matches = DATE_REGEX.findall(window)
        if date_matches:
            try:
                invoice_date = datetime.strptime(date_matches[-1], "%d/%m/%Y").date()
            except ValueError:
                invoice_date = None
    return invoice_number, invoice_date


@register
class UBAParser(InvoiceParser):
    supplier_code = "UBA"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        products_vat: dict[str, Decimal] = {}
        full_text_parts: list[str] = []
        tables = []

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text_parts.append(text)
                for line in text.split("\n"):
                    match = LINE_REGEX.match(line)
                    if match:
                        product_key = match.group(1) + match.group(2)
                        tva_idx = _to_int(match.group(18))
                        products_vat[product_key] = TVA_INDEX_TO_RATE.get(tva_idx, Decimal("0.2"))
                tables += page.extract_tables()

        parsed_lines: list[ParsedLine] = []
        for table in tables:
            if len(table[0]) != 16 or table[0][0] != "CODE":
                continue
            for row in table[1:]:
                if not _row_is_valid(row):
                    continue
                product_code = row[0]
                product_name = row[1]
                product_key = product_code + product_name
                vat_rate = products_vat.get(product_key, Decimal("0.2"))

                quantity_match = QUANTITY_REGEX.match(row[2])
                if not quantity_match:
                    continue
                quantity = _to_int(quantity_match.group(1))
                unit_price = _to_decimal(row[5])
                unit_tax = _to_decimal(row[8])
                cont_unit = _to_decimal(row[12])
                total_volume = _to_decimal(row[13])
                if cont_unit == 0:
                    continue
                total_ht = total_volume / cont_unit * unit_price
                total_taxes = total_volume / cont_unit * unit_tax

                parsed_lines.append(
                    ParsedLine(
                        raw_name=product_name,
                        quantity=quantity,
                        total_volume=total_volume,
                        unit_cost_ht=(total_ht / quantity).quantize(Decimal("0.0001")) if quantity else Decimal("0"),
                        total_ht=total_ht,
                        taxes=total_taxes,
                        vat_rate=vat_rate,
                        category="UBA",
                        ean=product_code,
                    )
                )

        full_text = "\n".join(full_text_parts)
        invoice_number, invoice_date = _guess_invoice_number_and_date(full_text)
        if invoice_date is None:
            invoice_date = date_hint

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=parsed_lines,
        )
