"""Parser for Metro France invoices, ported from the original ScrapBarInvoices
regex-based extractor. Line format (columns as printed on the PDF):

    EAN  N#  Désignation  [Régie]  [Vol %]  [VAP]  [Poids/Volume]  Prix unitaire
    [Colisage]  Qté  Montant  TVA

VAP and Poids/Volume are both optional and only one is usually present -
whichever is there ends up in the group we call ``weight_or_volume``.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

TVA_LETTER_TO_RATE = {"A": Decimal("0"), "B": Decimal("0.055"), "C": Decimal("0.2"), "D": Decimal("0.2")}

LINE_REGEX = re.compile(
    r"(\d+\s+)?(\d+)\s+(.+?)\s+([A-Z]\s+)?(\d?\d,\d\s+)?(\d+,\d+\s+)?(\d+,\d+\s+)?"
    r"(\d+,\d+)\s+(\d+\s+)?(\d+)\s+(\d+,\d+)\s+([A-D])"
)
COTIS_SOCIALE_REGEX = re.compile(r"Plus : COTIS\. SECURITE SOCIALE\s+(\d+,\d+)\s+([A-D])")
DISCOUNT_REGEX = re.compile(r"Offre Achetez Plus Payez Moins\s+(\d+,\d+)-")
CATEGORY_REGEX = re.compile(r"\*\*\*\s+(.+?)\s+Total:\s+(\d+,\d+)")

INVOICE_STORE_REGEX = re.compile(r"N[ºo°]\s*FACTURE\s+\S*\((\d+)\)")
INVOICE_REF_REGEX = re.compile(r"\((\d{3}-\d{6})\)")
INVOICE_DATE_REGEX = re.compile(r"Date facture\s*:\s*(\d{2}-\d{2}-\d{4})")

FILENAME_TIMESTAMP_REGEX = re.compile(r"_(\d{14})$")


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


def _to_int(text: str | None, default: int = 0) -> int:
    if not text:
        return default
    text = text.strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def _guess_invoice_number_and_date(full_text: str, pdf_path: str) -> tuple[str, date | None]:
    store_match = INVOICE_STORE_REGEX.search(full_text)
    ref_match = INVOICE_REF_REGEX.search(full_text)
    if store_match and ref_match:
        invoice_number = f"{store_match.group(1)}-{ref_match.group(1)}"
    elif ref_match:
        invoice_number = ref_match.group(1)
    else:
        invoice_number = os.path.splitext(os.path.basename(pdf_path))[0]

    invoice_date = None
    date_match = INVOICE_DATE_REGEX.search(full_text)
    if date_match:
        try:
            invoice_date = datetime.strptime(date_match.group(1), "%d-%m-%Y").date()
        except ValueError:
            invoice_date = None
    if invoice_date is None:
        filename = os.path.splitext(os.path.basename(pdf_path))[0]
        ts_match = FILENAME_TIMESTAMP_REGEX.search(filename)
        if ts_match:
            try:
                invoice_date = datetime.strptime(ts_match.group(1), "%Y%m%d%H%M%S").date()
            except ValueError:
                invoice_date = None
    return invoice_number, invoice_date


@register
class MetroParser(InvoiceParser):
    supplier_code = "METRO"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        lines_by_name: dict[str, ParsedLine] = {}
        products_in_category: list[str] = []
        full_text_parts: list[str] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text(y_tolerance=0) or ""
                full_text_parts.append(text)
                current_product = None
                for line in text.split("\n"):
                    match = LINE_REGEX.match(line)
                    if match:
                        product_name = match.group(3).strip()
                        weight_or_volume_raw = match.group(7) or match.group(6)
                        weight_or_volume = _to_decimal(weight_or_volume_raw)
                        colisage = _to_int(match.group(9), 1) or 1
                        quantity = _to_int(match.group(10))
                        total_units = colisage * quantity
                        total_ht = _to_decimal(match.group(11))
                        vat_rate = TVA_LETTER_TO_RATE[match.group(12).strip()]

                        parsed_line = lines_by_name.get(product_name)
                        if parsed_line is None:
                            parsed_line = ParsedLine(
                                raw_name=product_name,
                                quantity=total_units,
                                total_volume=total_units * weight_or_volume,
                                unit_cost_ht=Decimal("0"),
                                total_ht=total_ht,
                                vat_rate=vat_rate,
                                colisage=colisage,
                            )
                            lines_by_name[product_name] = parsed_line
                        else:
                            parsed_line.quantity += total_units
                            parsed_line.total_volume += total_units * weight_or_volume
                            parsed_line.total_ht += total_ht
                        current_product = product_name
                        products_in_category.append(product_name)
                        continue

                    cotis_match = COTIS_SOCIALE_REGEX.match(line)
                    if cotis_match and current_product:
                        lines_by_name[current_product].taxes += _to_decimal(cotis_match.group(1))

                    discount_match = DISCOUNT_REGEX.match(line)
                    if discount_match and current_product:
                        lines_by_name[current_product].discount += _to_decimal(discount_match.group(1))

                    category_match = CATEGORY_REGEX.match(line)
                    if category_match:
                        for name in products_in_category:
                            lines_by_name[name].category = category_match.group(1).strip()
                        products_in_category.clear()

        for parsed_line in lines_by_name.values():
            if parsed_line.quantity:
                parsed_line.unit_cost_ht = (parsed_line.total_ht / parsed_line.quantity).quantize(Decimal("0.0001"))

        full_text = "\n".join(full_text_parts)
        invoice_number, invoice_date = _guess_invoice_number_and_date(full_text, pdf_path)
        if invoice_date is None:
            invoice_date = date_hint

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=list(lines_by_name.values()),
        )
