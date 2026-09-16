from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class ParsedLine:
    raw_name: str
    quantity: int
    total_volume: Decimal
    unit_cost_ht: Decimal
    total_ht: Decimal
    taxes: Decimal = Decimal("0")
    discount: Decimal = Decimal("0")
    vat_rate: Decimal = Decimal("0")
    category: str = ""
    ean: str = ""
    # The supplier's own "packs per line" multiplier (Metro's "Colisage"),
    # already folded into `quantity` above (quantity = colisage * qty
    # bought) - kept separately too so the review queue can show it: whether
    # a "pack of 10" was already multiplied into quantity, or needs to be
    # applied by hand via the stock_equivalent factor, isn't always obvious
    # from quantity alone. 1 when the supplier's format has no such concept.
    colisage: int = 1
    # True when the till printed a generic placeholder ("Article divers")
    # instead of a product name. The line's own unit price is then the only
    # thing identifying what was bought, and the name is filled in at import
    # time from the shop's price list (see inventory ShopItemPrice and
    # importing.label_placeholder_lines) rather than in the parser, which
    # must stay free of database access to remain testable.
    is_placeholder: bool = False
    # The name exactly as OCR read it (see InvoiceLine.read_as). Set by
    # ReceiptParser.parse_ocr_pages; blank for a placeholder and for any
    # document that was not photographed.
    read_as: str = ""


@dataclass
class ParseCheck:
    """One self-consistency check a parser ran against its own output.

    Parsers that read a clean digital PDF don't need these: if the layout
    matched, the numbers are the numbers. OCR-backed parsers (see
    parsers/receipt_base.py) are different - the input is a photograph, and
    a misread digit produces a perfectly well-formed wrong price. So those
    parsers check their own arithmetic against the totals the receipt itself
    prints, and hand the verdict up for the review queue to show.

    `label` is shown to the user as-is, in French, and `detail` carries the
    numbers that made the check pass or fail so a human can see *why*
    without re-reading the ticket.
    """

    label: str
    passed: bool
    detail: str = ""


@dataclass
class ParsedInvoice:
    supplier_code: str
    invoice_number: str
    invoice_date: date | None
    lines: list[ParsedLine] = field(default_factory=list)
    # The text this parse was derived from, kept only when it isn't already
    # in the source file - i.e. OCR output for a photographed receipt.
    # Stored on the Invoice so the review screen can show what the parser
    # actually read next to the photo it read it from.
    source_text: str = ""
    # Lowest per-line OCR confidence on the page, 0..1. None for parsers
    # that read a digital PDF, where the concept doesn't apply.
    confidence: Decimal | None = None
    checks: list[ParseCheck] = field(default_factory=list)
    # True when the lines were read off a photo by OCR rather than out of a
    # digital document. Product names are then matched tolerantly (see
    # inventory.matching.ocr_match): a recogniser misreads "500G" as
    # "5OOG", a supplier's own PDF never does.
    from_ocr: bool = False
    # See Invoice.reconciliation_adjustment - 0 when a parser doesn't have
    # (or doesn't need) a printed grand total to reconcile against.
    reconciliation_adjustment: Decimal = Decimal("0")
    # What the parser noticed but could not settle - lines that don't add up
    # to the printed total - shown on the imported invoice, which then waits
    # in "À vérifier".
    warnings: list[str] = field(default_factory=list)


@dataclass
class PdfPage:
    """One page's worth of raw material, as pdfplumber hands it over: the
    page text, and (only for parsers that ask for them) its extracted
    tables as row lists.

    This exists so the PDF reading and the actual parsing are separable.
    Every parser's real logic lives in `parse_pages`, which takes these and
    never touches pdfplumber - so a test can hand it hand-written pages and
    exercise a supplier's layout quirks without needing a real invoice file.
    """

    text: str
    tables: list[list[list[str | None]]] = field(default_factory=list)


class InvoiceParser:
    """One InvoiceParser subclass per supplier PDF layout.

    Subclasses implement `parse_pages`; `parse` handles the pdfplumber I/O
    for all of them. (The LLM fallback parser overrides `parse` directly,
    since it works from whole-document text rather than a layout.)
    """

    supplier_code: str = ""
    # extract_tables() is comparatively expensive, so only the parsers that
    # actually read tables pay for it.
    needs_tables: bool = False
    # Per-parser tweaks for page.extract_text() - Metro needs y_tolerance=0
    # to stop adjacent columns being merged into one line. Never mutated.
    text_extraction_kwargs: dict = {}

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        import os

        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            pages = [
                PdfPage(
                    text=page.extract_text(**self.text_extraction_kwargs) or "",
                    tables=page.extract_tables() if self.needs_tables else [],
                )
                for page in pdf.pages
            ]
        return self.parse_pages(pages, date_hint=date_hint, source_name=os.path.basename(pdf_path))

    def parse_pages(
        self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = ""
    ) -> ParsedInvoice:
        """`source_name` is the PDF's bare filename - Metro invoices fall back
        to it for the invoice number and date when the page text doesn't
        yield them, so it's part of the raw material, not just metadata."""
        raise NotImplementedError
