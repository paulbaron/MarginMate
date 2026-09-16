"""Parser for SCEA Plou & Fils invoices - a clean layout where pdfplumber's
own table extraction works directly (unlike Cecina's, which needs a
text-regex fallback - see cecina.py).

The product table is found by its header ("Désignation", "Qté", ... "HT")
and its columns by name, never by count: the layout has already changed
twice. The 2026 one added a "Taux" column (20,00%) to every product row -
read by position, a seven-column row was no product at all, and an April
2026 invoice imported with no lines. Its VAT summary lost the "TVA réglée"
column at the same time. A January 2025 invoice prints its number as
"Référence interne" rather than "N° document".

The lines are checked against the printed "Montant total HT": a row the
table extraction drops or garbles would otherwise go unnoticed.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .base import InvoiceParser, ParsedInvoice, ParsedLine, PdfPage
from .registry import register

NUMBER_REGEX = re.compile(r"(?:N°\s*document|Référence\s+interne)\s*:\s*(\S+)")
# The number is printed in the page footer too ("SCEA PLOU ET FILS FA-202604-8169 - ...").
FOOTER_NUMBER_REGEX = re.compile(r"\bFA-\d{6}-\d+\b")
DATE_REGEX = re.compile(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})")
# "Taux 20.00%" in the VAT summary row - printed with a decimal POINT.
SUMMARY_RATE_REGEX = re.compile(r"Taux\s+(\d+[.,]\d+)\s*%")
RATE_CELL_REGEX = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
TOTAL_HT_REGEX = re.compile(r"Montant total HT\s*([\d .,]+?)\s*€")
DEFAULT_RATE = Decimal("0.20")


def _to_decimal(text: str | None, default: str = "0") -> Decimal:
    """"82,80€", "5.00€", "1 234,56€" and "1.234,56€" alike."""
    if not text:
        return Decimal(default)
    text = text.replace("€", "").replace(" ", "").replace(" ", "").replace(" ", "").strip()
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    if not text:
        return Decimal(default)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(default)


def _columns(header) -> dict | None:
    """Where each needed column sits in a product table, from its header -
    or None when the table isn't the product table."""
    names = [(cell or "").strip() for cell in header]
    if not {"Désignation", "Qté", "HT"} <= set(names):
        return None
    return {
        "name": names.index("Désignation"),
        "quantity": names.index("Qté"),
        "total_ht": names.index("HT"),
        "rate": names.index("Taux") if "Taux" in names else None,
        "width": len(names),
    }


def _product_line(row, columns, default_rate: Decimal) -> ParsedLine | None:
    if len(row) != columns["width"]:
        return None
    name = (row[columns["name"]] or "").strip()
    try:
        quantity = int((row[columns["quantity"]] or "").strip())
    except ValueError:
        return None
    if not name:
        return None
    rate = default_rate
    if columns["rate"] is not None:
        printed = RATE_CELL_REGEX.search(row[columns["rate"]] or "")
        if printed:
            rate = _to_decimal(printed.group(1)) / Decimal("100")
    total_ht = _to_decimal(row[columns["total_ht"]])
    return ParsedLine(
        raw_name=name,
        quantity=quantity,
        total_volume=Decimal("0"),
        unit_cost_ht=(total_ht / quantity).quantize(Decimal("0.0001")) if quantity else Decimal("0"),
        total_ht=total_ht,
        vat_rate=rate,
    )


@register
class PlouFilsParser(InvoiceParser):
    supplier_code = "PLOUFILS"
    needs_tables = True

    def parse_pages(
        self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = ""
    ) -> ParsedInvoice:
        full_text = "\n".join(page.text for page in pages)

        summary_rate = SUMMARY_RATE_REGEX.search(full_text)
        # One overall rate when no line prints its own - French wine is 20%,
        # the fallback when the summary row can't be read either.
        default_rate = _to_decimal(summary_rate.group(1)) / Decimal("100") if summary_rate else DEFAULT_RATE

        parsed_lines = []
        columns = None
        for page in pages:
            for table in page.tables:
                if not table:
                    continue
                header = _columns(table[0])
                if header is not None:
                    columns, rows = header, table[1:]
                elif columns is not None and all(len(row) == columns["width"] for row in table):
                    # The product table carried over to the next page without
                    # repeating its header.
                    rows = table
                else:
                    continue
                for row in rows:
                    line = _product_line(row, columns, default_rate)
                    if line is not None:
                        parsed_lines.append(line)

        number = NUMBER_REGEX.search(full_text) or FOOTER_NUMBER_REGEX.search(full_text)
        invoice_number = (number.group(1) if number.re is NUMBER_REGEX else number.group(0)) if number else ""

        invoice_date = date_hint
        date_match = DATE_REGEX.search(full_text)
        if date_match:
            try:
                invoice_date = datetime.strptime(date_match.group(1), "%d/%m/%Y").date()
            except ValueError:
                pass

        warnings = []
        printed_total = TOTAL_HT_REGEX.search(full_text)
        if printed_total and parsed_lines:
            printed_ht = _to_decimal(printed_total.group(1))
            lines_ht = sum((line.total_ht for line in parsed_lines), Decimal("0"))
            if lines_ht != printed_ht:
                warnings.append(
                    f"Les lignes lues font {lines_ht} € HT, la facture imprime {printed_ht} € HT : "
                    "une ligne manque ou a été mal lue."
                )

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=parsed_lines,
            warnings=warnings,
        )
