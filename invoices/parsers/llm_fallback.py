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

from django.conf import settings

from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

MODEL = "claude-sonnet-5"
# An answer cut off at this limit is refused, never filed (AIReadingRefused):
# room for some 250 lines, at 50 to 70 tokens a line.
MAX_TOKENS = 16000

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
    """Every page's text, through `ocr.pdf_pages`: refused (DocumentTooBig)
    past ocr.MAX_PAGES before a page is read - or the API billed for it -
    and each page released once read (security review of HARDEN-01: this
    loop had no cap at all)."""
    from ..ocr import pdf_pages

    return "\n".join((page.extract_text(y_tolerance=0) or "") for page in pdf_pages(pdf_path))


class AIReadingRefused(ValueError):
    """The AI's answer is not an invoice to file: cut off at its length
    limit, or with no invoice in it. Said on the page as it stands
    (receipt_batches.READING_REFUSALS) - and nothing is filed: the lines
    before a cut are a valid answer, and filed, a long invoice silently lost
    its last ones."""


def _to_decimal(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")
    return number if number.is_finite() else Decimal("0")


@register
class LLMFallbackParser(InvoiceParser):
    supplier_code = "LLM"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        # The key and its bill are the owner's: from a tenant that may not
        # use the server's accounts, refused before the document is read or
        # anything is sent (invoices/integrations.py) - whichever path got
        # here (the PDF import, a source's reader, a gathered attachment).
        from accounts.tenancy import integrations_allowed
        from invoices import integrations

        if not integrations_allowed():
            raise RuntimeError(integrations.AI_READING)
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not configured - set it in .env to use the AI-assisted invoice parser."
            )

        import anthropic

        text = _extract_text(pdf_path)
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
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

        if response.stop_reason == "max_tokens":
            raise AIReadingRefused(
                "Facture trop longue pour l'analyse IA : sa réponse a été coupée, rien n'a été importé. "
                "Saisissez-la à la main."
            )
        # Declined (« refusal »), whatever it started to write is no answer.
        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        data = tool_use.input if tool_use is not None and response.stop_reason != "refusal" else None
        if not isinstance(data, dict):
            raise AIReadingRefused("L'analyse IA n'a rendu aucune facture lisible : rien n'a été importé.")

        invoice_date = date_hint
        raw_date = data.get("invoice_date")
        if raw_date and isinstance(raw_date, str):
            try:
                invoice_date = datetime.strptime(raw_date, "%Y-%m-%d").date()  # noqa: DTZ007 - a printed date, read into .date()
            except ValueError:
                pass

        # The tool's schema is asked for, not enforced: a count may come back
        # as 0.5 or "2.5", a line without its name or its price.
        lines: list[ParsedLine] = []
        nameless = 0
        doubtful: list[str] = []
        raw_lines = data.get("lines")
        for line in raw_lines if isinstance(raw_lines, list) else []:
            name = str(line.get("name") or "").strip() if isinstance(line, dict) else ""
            if not name:
                nameless += 1
                continue
            count = _to_decimal(line.get("quantity"))
            total = _to_decimal(line.get("total_price_ht"))
            if count == 0 or line.get("total_price_ht") is None:
                doubtful.append(name)
            lines.append(
                ParsedLine(
                    raw_name=name,
                    quantity=int(count) if count == count.to_integral_value() else count,
                    total_volume=_to_decimal(line.get("total_volume_or_weight")),
                    unit_cost_ht=total / count if count else Decimal("0"),
                    total_ht=total,
                    vat_rate=_to_decimal(line.get("vat_rate")),
                    category=str(line.get("category") or ""),
                )
            )
        warnings = []
        if nameless:
            warnings.append(f"L'analyse IA a rendu {nameless} ligne(s) sans nom, laissée(s) de côté.")
        if doubtful:
            warnings.append(f"Analyse IA : quantité ou prix illisible pour {', '.join(doubtful)}.")

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=str(data.get("invoice_number") or ""),
            invoice_date=invoice_date,
            lines=lines,
            warnings=warnings,
        )
