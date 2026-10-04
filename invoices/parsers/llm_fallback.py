"""Generic, last-resort invoice parser using the Claude API.

Only used when a supplier has no dedicated regex parser (see metro.py /
uba.py) - those are always preferred since they're free, fast, and exact.
This one is for onboarding a brand-new supplier before anyone has written a
proper parser for it: it reads the raw PDF text and asks Claude to return the
same structured shape a hand-written parser would.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..integrations import AI_KEY_NAME
from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .registry import register

MODEL = "claude-sonnet-5"
#: One call's limit, in seconds, and no retry: the call runs inside the
#: upload's request (one of the server's eight threads), and the tunnel
#: answers the browser by itself past 100 s.
TIMEOUT_SECONDS = 60.0
#: How many AI readings run at once on the server, every espace together; a
#: third is refused at once, never queued (`integrations.AI_BUSY`).
AI_SLOTS = 2
_SLOTS = threading.BoundedSemaphore(AI_SLOTS)
#: How many of them one espace that is not the platform owner's may hold:
#: one bar's two uploads must not refuse every other bar
#: (`integrations.AI_BUSY_HERE`). The owner's readings are not counted.
AI_PER_ESPACE = 1
_RUNNING_LOCK = threading.Lock()
#: tenant_key() -> readings running.
_RUNNING: dict[str, int] = {}


@contextmanager
def _ai_slot():
    """One of the server's AI slots for the bound espace while the block
    runs, never waited for: AiReadingRefused(AI_BUSY_HERE) when this espace
    - another bar's - already reads one, AiReadingRefused(AI_BUSY) when the
    server reads AI_SLOTS."""
    from accounts.tenancy import server_accounts_allowed, tenant_key
    from invoices import integrations

    key = None if server_accounts_allowed() else tenant_key()
    if key is not None:
        with _RUNNING_LOCK:
            if _RUNNING.get(key, 0) >= AI_PER_ESPACE:
                raise integrations.AiReadingRefused(integrations.AI_BUSY_HERE)
            _RUNNING[key] = _RUNNING.get(key, 0) + 1
    try:
        if not _SLOTS.acquire(blocking=False):
            raise integrations.AiReadingRefused(integrations.AI_BUSY)
        try:
            yield
        finally:
            _SLOTS.release()
    finally:
        if key is not None:
            with _RUNNING_LOCK:
                left = _RUNNING.get(key, 1) - 1
                if left > 0:
                    _RUNNING[key] = left
                else:
                    _RUNNING.pop(key, None)


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


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _said(anthropic, exc) -> str | None:
    """The fixed sentence for one of the SDK's errors, or None for anything
    else. Read off the SDK's own classes by name - a test's stand-in module
    holds the same names."""
    from invoices import integrations

    for names, sentence in (
        (("AuthenticationError", "PermissionDeniedError"), integrations.AI_KEY_REFUSED),
        (("RateLimitError",), integrations.AI_RATE_LIMITED),
        (("BadRequestError",), integrations.AI_BAD_REQUEST),
        (("NotFoundError",), integrations.AI_MODEL_GONE),
        (("InternalServerError", "OverloadedError", "ServiceUnavailableError"), integrations.AI_UNAVAILABLE),
        # APITimeoutError is one of these.
        (("APIConnectionError",), integrations.AI_NO_ANSWER),
    ):
        for name in names:
            kind = getattr(anthropic, name, None)
            if isinstance(kind, type) and isinstance(exc, kind):
                return sentence
    status_error = getattr(anthropic, "APIStatusError", None)
    if isinstance(status_error, type) and isinstance(exc, status_error):
        return integrations.AI_UNAVAILABLE if getattr(exc, "status_code", 0) >= 500 else integrations.AI_BAD_REQUEST
    return None


@register
class LLMFallbackParser(InvoiceParser):
    supplier_code = "LLM"

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        # The key and its bill are the tenant's own: the one typed on its
        # « Identifiants » page, else - in the owner's tenant only - the
        # server's (accounts.vault.setting). Refused before the document is
        # read or anything is sent, whichever path got here (the PDF import,
        # a source's reader, a gathered attachment). Every refusal is an
        # AiReadingRefused: a sentence the page says as it is.
        from accounts import vault
        from accounts.tenancy import integrations_allowed
        from invoices import integrations

        if not integrations_allowed():
            raise integrations.AiReadingRefused(integrations.AI_READING)
        api_key = vault.setting(AI_KEY_NAME)
        if not api_key:
            raise integrations.AiReadingRefused(integrations.AI_KEY_MISSING)

        import anthropic

        text = _extract_text(pdf_path)
        # Two at a time on the server, one for another bar, never waited
        # for: a request thread waiting on another bar's reading is a thread
        # nobody else gets.
        with _ai_slot():
            client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)
            try:
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
            except Exception as exc:
                sentence = _said(anthropic, exc)
                if sentence is None:
                    raise
                raise integrations.AiReadingRefused(sentence) from exc

        tool_use = next(block for block in response.content if block.type == "tool_use")
        data = tool_use.input

        invoice_date = date_hint
        raw_date = data.get("invoice_date")
        if raw_date:
            try:
                invoice_date = datetime.strptime(raw_date, "%Y-%m-%d").date()  # noqa: DTZ007 - a printed date, read into .date()
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
