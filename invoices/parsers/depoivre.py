"""Parser for EARL Depoivre Père & Fils (Champagne Bernard Depoivre)
invoices - a simple, single-table layout. Two tables per page told apart
by content rather than shape (both parse to the same column count):
product rows start with a plain integer quantity, the VAT breakdown rows
start with a one-letter code (e.g. "A TVA à 20 % ... 20,00 ...").

Only MNT TTC (tax-included) is printed per line, not HT - total_ht is
computed as quantity * PU HT instead of read directly, unlike every other
parser in this package so far.

This document has no invoice number of its own - "Commande N° 20250180 du
22/11/2025" (also embedded in the attachment filename, e.g.
"AUDIPSO_EARLD_CM20250180.pdf") is the closest stable identifier, used as
both invoice_number and the date source.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

ORDER_REGEX = re.compile(r"Commande\s*N[°o]\s*(\S+)\s+du\s+(\d{2}/\d{2}/\d{4})")
TVA_RATE_REGEX = re.compile(r"TVA\s+à\s+(\d+(?:,\d+)?)\s*%")


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
class DepoivreParser(InvoiceParser):
    supplier_code = "DEPOIVRE"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        all_rows: list[list] = []
        full_text_parts: list[str] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                full_text_parts.append(page.extract_text() or "")
                for table in page.extract_tables():
                    all_rows.extend(table)

        full_text = "\n".join(full_text_parts)

        # {tva_code: rate_fraction} from lines like "A TVA à 20 % ... 20,00 ..."
        # - a fallback to the row's own printed "20 %" wording, in case the
        # cleaner "Taux" table column doesn't line up the same way on a
        # differently laid out invoice.
        tva_rate_by_label: dict[str, Decimal] = {}
        for row in all_rows:
            if len(row) >= 2 and row[0] and re.fullmatch(r"[A-Z]", row[0] or "") and row[1] and "TVA" in row[1]:
                rate_match = TVA_RATE_REGEX.search(row[1])
                if rate_match:
                    tva_rate_by_label[row[0]] = _to_decimal(rate_match.group(1)) / Decimal("100")

        parsed_lines = []
        for row in all_rows:
            quantity_text = (row[0] or "").strip() if row else ""
            if not quantity_text.isdigit():
                continue  # the header row, VAT breakdown rows, and any blank rows all land here
            designation = next((cell for cell in row[1:-4] if cell and cell.strip()), None) if len(row) > 5 else None
            if not designation:
                continue
            quantity = int(quantity_text)
            unit_price_ht = _to_decimal(row[-4]) if len(row) >= 4 else Decimal("0")
            tva_code = (row[-1] or "").strip() if row else ""
            vat_rate = tva_rate_by_label.get(tva_code, Decimal("0"))
            total_ht = (unit_price_ht * quantity).quantize(Decimal("0.01"))
            parsed_lines.append(
                ParsedLine(
                    raw_name=designation.strip(),
                    quantity=quantity,
                    total_volume=Decimal("0"),
                    unit_cost_ht=unit_price_ht,
                    total_ht=total_ht,
                    vat_rate=vat_rate,
                )
            )

        order_match = ORDER_REGEX.search(full_text)
        invoice_number = order_match.group(1) if order_match else ""

        invoice_date = date_hint
        if order_match:
            try:
                invoice_date = datetime.strptime(order_match.group(2), "%d/%m/%Y").date()
            except ValueError:
                pass

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=parsed_lines,
        )
