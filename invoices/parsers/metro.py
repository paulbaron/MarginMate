"""Parser for Metro France invoices, ported from the original ScrapBarInvoices
regex-based extractor. Line format (columns as printed on the PDF):

    EAN  N#  Désignation  [Régie]  [Vol %]  [VAP]  [Poids/Volume]  Prix unitaire
    [Colisage]  Qté  Montant  TVA

VAP and Poids/Volume are both optional and only one is usually present -
whichever is there ends up in the group we call ``weight_or_volume``.

Two more real formatting quirks, both for crate/pallet deposit ("consigne")
lines: a charge line (buying a full crate, e.g. "CAISSE COCA 24X33CL PLEIN")
is prefixed with a literal "+ " before the EAN with no EAN of its own, and a
refund line (returning the empty crate/pallet, e.g. "... VIDE" or "PALETTE
EUROPE") prints its Qté and Montant with a trailing "-" instead of a leading
one (e.g. "1-", "5,50-"). Both are handled here rather than skipped so a
refund can be linked to a stock item like any other product and have its
(negative) quantity/value actually subtracted.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from decimal import Decimal

from common import format_money

from .base import InvoiceParser, ParsedInvoice, ParsedLine, PdfPage
from .registry import register

VAT_LETTER_TO_RATE = {"A": Decimal("0"), "B": Decimal("0.055"), "C": Decimal("0.2"), "D": Decimal("0.2")}

# The leftmost "MM" column prints a literal "M " before the EAN on Metro's
# own-brand rows, exactly where a deposit line prints "+ ". Anchored, and with
# only "+ " allowed, such rows matched nothing and were dropped in silence -
# their money and their stock with them. Across every product row of the
# invoices filed, "M " and "+ " are the only two prefixes that ever occur, so
# the alternation is closed, not a guess: a wider [A-Z] would start
# swallowing rows nothing prefixes.
LINE_REGEX = re.compile(
    r"(?:[+M]\s+)?(\d+\s+)?(\d+)\s+(.+?)\s+([A-Z]\s+)?(\d?\d,\d\s+)?(\d+,\d+\s+)?(\d+,\d+\s+)?"
    r"(\d+,\d+)\s+(\d+\s+)?(\d+-?)\s+(\d+,\d+-?)\s+([A-D])"
)
SOCIAL_SECURITY_LEVY_REGEX = re.compile(r"Plus : COTIS\. SECURITE SOCIALE\s+(\d+,\d+)\s+([A-D])")
# Two printed forms of the same thing, a bulk promotion taken off the line
# above. "N pour M" was unread until 24/09/2026, so its discount was never
# subtracted and the invoice claimed MORE than Metro billed, overstating
# every cost and margin drawn from those lines. The
# case varies between invoices ("3 POUR 2", "3 pour 2"), hence IGNORECASE.
DISCOUNT_REGEX = re.compile(
    r"(?:Offre\s+Achetez\s+Plus\s+Payez\s+Moins|\d+\s+pour\s+\d+)\s+(\d+,\d+)-",
    re.IGNORECASE,
)
CATEGORY_REGEX = re.compile(r"\*\*\*\s+(.+?)\s+Total:\s+(\d+,\d+)")

# An amount as Metro prints it: French decimal comma, an optional space for
# thousands, and the trailing minus a credit note uses instead of a leading one.
# Exactly two decimals is what tells an amount from the three-decimal volume
# printed beside it in the VAT table ("M 9,000 68,04 B = 5,50%") - read
# greedily, that row files 9 000 68,04 EUR of goods at 5,5 %.
_MONEY = r"(?:\d{1,3}(?:[  ]\d{3})*|\d+),\d{2}-?"
PRINTED_HT_REGEX = re.compile(r"Total\s+H\.T\.\s*:\s*(" + _MONEY + r")")
PRINTED_TTC_REGEX = re.compile(r"Total\s+[àa]\s+payer\s+(" + _MONEY + r")")
VAT_ROW_REGEX = re.compile(
    r"(?:^|\s)(" + _MONEY + r")\s+([A-D])\s*=\s*(\d+,\d+)\s*%\s+(" + _MONEY + r")\s+(" + _MONEY + r")\s*$"
)
# Every figure compared here is one Metro printed, so the two sides agree to
# the cent or something was misread; the slack is for a weight-priced line's
# rounding, not for a missing row.
TOTAL_TOLERANCE = Decimal("0.02")

INVOICE_STORE_REGEX = re.compile(r"N[ºo°]\s*FACTURE\s+\S*\((\d+)\)")
INVOICE_REF_REGEX = re.compile(r"\((\d{3}-\d{6})\)")
INVOICE_DATE_REGEX = re.compile(r"Date facture\s*:\s*(\d{2}-\d{2}-\d{4})")

FILENAME_TIMESTAMP_REGEX = re.compile(r"_(\d{14})$")


def _to_decimal(text: str | None, default: str = "0") -> Decimal:
    if not text:
        return Decimal(default)
    # The space is French thousands grouping ("1 234,56"), which Decimal()
    # will not take; it never separates two amounts here, each capture being
    # a single number.
    text = text.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return Decimal(default)
    # Metro prints negative amounts (deposit refunds) with a trailing "-"
    # rather than a leading one, e.g. "5,50-" - Decimal() doesn't accept that
    # form directly, so move the sign before parsing.
    negative = text.endswith("-")
    if negative:
        text = text[:-1]
    try:
        value = Decimal(text)
    except Exception:  # noqa: BLE001 - an unreadable amount reads as the default
        return Decimal(default)
    return -value if negative else value


def _to_int(text: str | None, default: int = 0) -> int:
    if not text:
        return default
    text = text.strip()
    if not text:
        return default
    negative = text.endswith("-")
    if negative:
        text = text[:-1]
    try:
        value = int(text)
    except ValueError:
        return default
    return -value if negative else value


def _fr(value: Decimal) -> str:
    """An amount the way the invoice prints it, for a message a person reads
    next to the document itself, its thousands grouped like every amount on a
    screen."""
    return format_money(value).replace(".", ",")


def _read_printed_totals(full_text: str):
    """What the invoice says about itself: its HT total, what it asks to be
    paid, and its VAT table.

    Metro prints all three and this parser read none of them, so nothing
    ever compared the lines against the document. That silence is the whole
    reason a dropped own-brand row and an unread promotion could each sit
    there being wrong: `printed_total_ttc` was NULL on nearly every Metro
    invoice and `vat_breakdown` empty on all of them, so no check had a
    second figure to disagree with.

    A credit note prints every one of these with a TRAILING minus and this
    codebase files a credit note negative, so the sign is part of the
    reading, not a detail: taken unsigned, every credit note filed would
    disagree with its own totals by twice its value.
    """
    printed_ht = PRINTED_HT_REGEX.search(full_text)
    printed_ttc = PRINTED_TTC_REGEX.search(full_text)
    breakdown = []
    for line in full_text.split("\n"):
        row = VAT_ROW_REGEX.search(line)
        if row is None:
            continue
        breakdown.append(
            (_to_decimal(row.group(3)) / Decimal("100"), _to_decimal(row.group(1)), _to_decimal(row.group(4)))
        )
    return (
        _to_decimal(printed_ht.group(1)) if printed_ht else None,
        _to_decimal(printed_ttc.group(1)) if printed_ttc else None,
        breakdown,
    )


def _reconciliation_warning(lines_total_ht: Decimal, printed_ht: Decimal | None) -> list[str]:
    """Say it when the lines and the document disagree.

    Both figures are sums of amounts Metro itself printed, so they agree to
    the cent unless a row was not read - too little when one was dropped,
    too much when a discount was not. Said here rather than checked in
    `parse_checks`, because that field is what tells a photographed ticket
    from a digital invoice (invoices/workspace.py): filling it for Metro
    would put every Metro invoice into the receipt review queue. A warning
    reaches `Invoice.error_message` and holds the document in "À vérifier",
    which is where a document that cannot be trusted belongs.
    """
    if printed_ht is None:
        return []
    gap = printed_ht - lines_total_ht
    if abs(gap) <= TOTAL_TOLERANCE:
        return []
    missing = "une ligne n'a pas été lue" if gap > 0 else "une remise n'a pas été déduite"
    said = (
        f"Les lignes lues font {_fr(lines_total_ht)} € HT alors que la facture imprime "
        f"{_fr(printed_ht)} € HT (écart {_fr(gap)} €) : {missing}. Vérifiez le document."
    )
    return [said]


def _guess_invoice_number_and_date(full_text: str, source_name: str) -> tuple[str, date | None]:
    store_match = INVOICE_STORE_REGEX.search(full_text)
    ref_match = INVOICE_REF_REGEX.search(full_text)
    if store_match and ref_match:
        invoice_number = f"{store_match.group(1)}-{ref_match.group(1)}"
    elif ref_match:
        invoice_number = ref_match.group(1)
    else:
        invoice_number = os.path.splitext(source_name)[0]

    invoice_date = None
    date_match = INVOICE_DATE_REGEX.search(full_text)
    if date_match:
        try:
            invoice_date = datetime.strptime(date_match.group(1), "%d-%m-%Y").date()  # noqa: DTZ007 - a printed date, read into .date()
        except ValueError:
            invoice_date = None
    if invoice_date is None:
        filename = os.path.splitext(source_name)[0]
        ts_match = FILENAME_TIMESTAMP_REGEX.search(filename)
        if ts_match:
            try:
                invoice_date = datetime.strptime(ts_match.group(1), "%Y%m%d%H%M%S").date()  # noqa: DTZ007 - a file name's local time, read into .date()
            except ValueError:
                invoice_date = None
    return invoice_number, invoice_date


@register
class MetroParser(InvoiceParser):
    supplier_code = "METRO"
    label = "Metro"
    # Without this, pdfplumber merges vertically-adjacent columns into a
    # single line and the product regex stops matching.
    text_extraction_kwargs = {"y_tolerance": 0}

    def parse_pages(self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = "") -> ParsedInvoice:
        lines_by_name: dict[str, ParsedLine] = {}
        products_in_category: list[str] = []
        full_text_parts: list[str] = []
        # Across pages: a levy or discount line can start the next page.
        current_product = None

        for page in pages:
            text = page.text
            full_text_parts.append(text)
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
                    vat_rate = VAT_LETTER_TO_RATE[match.group(12).strip()]

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

                levy_match = SOCIAL_SECURITY_LEVY_REGEX.match(line)
                if levy_match and current_product:
                    lines_by_name[current_product].taxes += _to_decimal(levy_match.group(1))

                discount_match = DISCOUNT_REGEX.match(line)
                if discount_match and current_product:
                    lines_by_name[current_product].discount += _to_decimal(discount_match.group(1))

                category_match = CATEGORY_REGEX.match(line)
                if category_match:
                    for name in products_in_category:
                        lines_by_name[name].category = category_match.group(1).strip()
                    products_in_category.clear()

        for parsed_line in lines_by_name.values():
            # Metro's printed unit/line price is NOT the real cost: a "Plus :
            # COTIS. SECURITE SOCIALE" surcharge (mandatory on alcohol) is
            # billed as a separate line right after the product and has to be
            # added, and a bulk-buy "Offre Achetez Plus Payez Moins" discount
            # subtracted - both already accumulated into taxes/discount above
            # by product name. total_ht is folded to the real total actually
            # paid (matching the original parser's tried-and-tested
            # `montant_ht - promotions + taxes` formula) so every downstream
            # cost calculation, which just reads total_ht as the truth, sees
            # the right number without having to know about this quirk.
            parsed_line.total_ht = parsed_line.total_ht - parsed_line.discount + parsed_line.taxes
            if parsed_line.quantity:
                parsed_line.unit_cost_ht = (parsed_line.total_ht / parsed_line.quantity).quantize(Decimal("0.0001"))

        full_text = "\n".join(full_text_parts)
        invoice_number, invoice_date = _guess_invoice_number_and_date(full_text, source_name)
        if invoice_date is None:
            invoice_date = date_hint

        parsed_lines = list(lines_by_name.values())
        printed_ht, printed_ttc, vat_breakdown = _read_printed_totals(full_text)
        lines_total_ht = sum((line.total_ht for line in parsed_lines), Decimal("0"))

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=parsed_lines,
            printed_total_ttc=printed_ttc,
            vat_breakdown=vat_breakdown,
            warnings=_reconciliation_warning(lines_total_ht, printed_ht),
        )
