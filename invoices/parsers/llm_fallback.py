"""Generic, last-resort invoice parser using the Claude API.

Only used when a supplier has no dedicated regex parser (see metro.py /
uba.py) - those are always preferred since they're free, fast, and exact.
This one is for onboarding a brand-new supplier before anyone has written a
proper parser for it: it reads the raw PDF text and asks Claude to return the
same structured shape a hand-written parser would.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber
from django.conf import settings

from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

MODEL = "claude-sonnet-5"

EXTRACTION_TOOL = {
    "name": "record_invoice",
    "description": "Record the structured contents of a purchase invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string", "description": "ISO format YYYY-MM-DD"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Product name as printed on the invoice"},
                        "quantity": {"type": "integer", "description": "Number of units/items purchased"},
                        "total_volume_or_weight": {
                            "type": "number",
                            "description": "Total physical volume in litres or weight in kilograms for this line, 0 if not applicable (e.g. a piece-counted item)",
                        },
                        "total_price_ht": {"type": "number", "description": "Line total excluding tax"},
                        "vat_rate": {"type": "number", "description": "VAT rate as a fraction, e.g. 0.2 for 20%"},
                        "category": {"type": "string"},
                    },
                    "required": ["name", "quantity", "total_price_ht"],
                },
            },
        },
        "required": ["lines"],
    },
}


def _extract_text(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((page.extract_text(y_tolerance=0) or "") for page in pdf.pages)


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


@register
class LLMFallbackParser(InvoiceParser):
    supplier_code = "LLM"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not configured - set it in .env to use the AI-assisted invoice parser."
            )

        import anthropic

        text = _extract_text(pdf_path)
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_invoice"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract every purchased product line from this supplier invoice text. "
                        "Only include actual line items, not subtotals, taxes, or shipping.\n\n" + text
                    ),
                }
            ],
        )

        tool_use = next(block for block in response.content if block.type == "tool_use")
        data = tool_use.input

        invoice_date = date_hint
        raw_date = data.get("invoice_date")
        if raw_date:
            try:
                invoice_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                pass

        lines = [
            ParsedLine(
                raw_name=line["name"],
                quantity=int(line.get("quantity") or 0),
                total_volume=_to_decimal(line.get("total_volume_or_weight")),
                unit_cost_ht=(
                    _to_decimal(line.get("total_price_ht")) / int(line["quantity"])
                    if line.get("quantity")
                    else Decimal("0")
                ),
                total_ht=_to_decimal(line.get("total_price_ht")),
                vat_rate=_to_decimal(line.get("vat_rate")),
                category=line.get("category") or "",
            )
            for line in data.get("lines", [])
        ]

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=data.get("invoice_number") or "",
            invoice_date=invoice_date,
            lines=lines,
        )
