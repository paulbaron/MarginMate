from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class ParsedLine:
    raw_name: str
    # A count, or a measure the document sells by (0.82 m²): a Decimal then.
    quantity: int | Decimal
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
    # The line's amount tax included, as the ticket printed it (see
    # InvoiceLine.printed_ttc). Set by the receipt parsers; None for a digital
    # invoice.
    printed_ttc: Decimal | None = None
    # The line's share of a promotion, TTC, off `printed_ttc` (see
    # InvoiceLine.discount_ttc); `total_ht` is already net of it.
    discount_ttc: Decimal = Decimal("0")
    # The stored InvoiceLine this one corrects, when a person edits an
    # invoice (see importing.replace_invoice_lines). Parsers never set it.
    line_id: int | None = None


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
class EInvoiceFacts:
    """What an EN 16931 invoice states about itself that a ParsedInvoice has
    nowhere else to put - see `invoices/einvoice.py`.

    Set on a ParsedInvoice by that reader and by nothing else, so `parsed.
    einvoice is not None` is the one question worth asking downstream: it
    means the number, the date, the seller, the lines and the totals are the
    document's own data rather than a reading of a printed page. Everything
    else in this application infers, and `parse_checks` exists to catch the
    inferences that are wrong; here there is nothing to catch.

    `carries_no_lines` is the trap. Factur-X's MINIMUM and BASIC WL profiles
    state the totals and the VAT breakdown and no line at all, which is a
    VALID invoice, not a failed reading - it has to go down the total-only
    path (importing.charge_reading) and be SAID, or it looks exactly like a
    document whose lines could not be read.
    """

    # CII or UBL - the two EN 16931 syntaxes, told apart by root element.
    syntax: str = ""
    # BT-24, the guideline the sender claims to follow ("urn:factur-x.eu:
    # 1p0:minimum"). Kept for the review screen: it is what explains a
    # document with no lines.
    profile: str = ""
    # BT-27, the seller's registered name. Its SIREN and VAT number go into
    # ParsedInvoice.source_text instead, where invoices/identifiers.py finds
    # them - there is one matcher for that and this is not a second.
    seller_name: str = ""
    # BT-3 is a credit note (381): money going the other way. Its amounts
    # are stated positive in the file and come out of the reader with this
    # codebase's own sign for a return.
    is_credit_note: bool = False
    document_type_code: str = ""
    # The profile stated no line - not "no line could be read".
    carries_no_lines: bool = False
    # BT-5. Never anything but EUR: the reader refuses the rest rather than
    # converting, since a rate is a decision nobody here is entitled to take.
    currency: str = ""
    # What the document-level allowances and charges (BG-20/BG-21) were for,
    # in the sender's own words - the reasons behind
    # ParsedInvoice.reconciliation_adjustment. An adjustment with no reason
    # beside it is a figure nobody on the review screen can check.
    adjustment_reasons: list[str] = field(default_factory=list)
    # BT-96 / BT-103: the VAT rate those allowances and charges carry, as
    # the fraction this database stores. None when the document does not
    # state one, and then Invoice.adjustment_ttc goes on guessing it from
    # the lines - which is what a supplier's PDF and a till receipt need.
    # Stated and ignored, it files duty at 20 % as if it were at 5,5 %: on a
    # 1 175,00 € invoice that is 14,50 € the bank will debit and the invoice
    # will not show, with every check green.
    adjustment_vat_rate: Decimal | None = None


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
    # The total the ticket printed (what was paid), for a photographed
    # receipt; see Invoice.printed_total_ttc.
    printed_total_ttc: Decimal | None = None
    # The document's own VAT table, as (rate, HT base, tax) - what a
    # supplier of charges is filed on, one line a rate, since there is no
    # product behind a rent (invoices.importing.expense_lines).
    vat_breakdown: list[tuple[Decimal, Decimal, Decimal]] = field(default_factory=list)
    # Set by invoices/einvoice.py only, and None for every other reader:
    # what a Factur-X / UBL / CII document states about itself, and the flag
    # that its figures are data rather than a reading. See EInvoiceFacts.
    einvoice: EInvoiceFacts | None = None


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
