"""Parser for UBA (Le Dipsomaniac) invoices, ported from the original
ScrapBarInvoices regex/table extractor. UBA's PDF puts VAT info only in the
free-flowing text (one letter code per line) but the actual product rows only
show up cleanly via pdfplumber's table extraction, so we cross-reference the
two: first pass over the text to build a {product -> vat rate} map, second
pass over the extracted table for quantities/prices.

Packaging deposits ("consignes"): buying a keg/case with its own container
charges a refundable deposit (column CONSIG.), and returning an empty one
refunds it (column DECONS.) - the same "consigne"/"déconsigne" concept
Metro's own parser already separates out (see metro.py's docstring). A
consigne is billed on the SAME row as the product it came with (so it's
split out into its own "Consigne <product>" line here); a déconsigne
refund is its own row, with a generic packaging code/name (e.g. "EMB01
FÛT 10/15/20/25/30/50 L") rather than tied to any specific product - kept
as its own line with a negative quantity/total, the same way Metro's
crate/pallet refunds are, so it can be matched to a stock item like any
other product and have its value actually subtracted.

Not every row has a volume/weight (CONT. UNIT./VOLUME EFFECTIF, used to
derive most products' totals) - a CO2 gas cylinder, for one, is billed as
a flat "1 TUB" at a per-cylinder price with its own consigne on top (e.g.
"BOUTEILLE CO2 10 KG GRISE"), so that case falls back to quantity * unit
price directly.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from .base import InvoiceParser, ParsedInvoice, ParsedLine, PdfPage
from .registry import register

TVA_INDEX_TO_RATE = {1: Decimal("0.2"), 2: Decimal("0.055"), 3: Decimal("0")}

LINE_REGEX = re.compile(
    r"([A-Z0-9]+)\s+(.+?)\s+(-?\d+)\s+(FUT|CAR|CAI|BT|EMB|TUB|UNI|PUB)\s(-?\d+)\s+(L|BT|BOI|EMB|TUB|UNI|PUB)\s+(\d+,\d+)\s+(-?\d+,\d+)\s+"
    r"(\d+,\d+\s*\%\s+)?(\d+,\d+\s+)?(-?\d+,\d+\s+)?(-?\d+,\d+\s+)?(\d+,\d\d\s*\s+)?(\d+,\d\d)\s+(-?\d+,\d\d\s+)"
    r"(-?\d+,\d\d\s+)?(-?\d+,\d\d\s+)([1-3])"
)
# FUT/CAR/CAI/BT/EMB are container types (keg, case, box, bottle, empty
# return); TUB/UNI/PUB show up on non-alcohol rows this parser otherwise has
# no reason to single out (gas cylinders, equipment loans, POS material) -
# TUB in particular carries a real deposit (see the CO2 cylinder example in
# the module docstring), so all three need to be recognized here even
# though UNI/PUB rows end up filtered out later for having no real price.
QUANTITY_REGEX = re.compile(r"(-?\d+)\s+(FUT|CAR|CAI|BT|EMB|TUB|UNI|PUB)")
INVOICE_NUMBER_REGEX = re.compile(r"Facture No\s*:\s*(\S+)")
DATE_SECTION_MARKER = "DATE FACTURE"
DATE_REGEX = re.compile(r"(\d{2}/\d{2}/\d{4})")
# The invoice's own printed grand total row, e.g. "257,19 39,82 53,69 14
# U.B.A." - Montant HT, Droits, then TVA, right before the supplier's own
# name/phone. Used only to reconcile Invoice.total_ht (see
# Invoice.reconciliation_adjustment) - the "14" and "U.B.A." anchor the
# match so it can't be confused with one of the per-VAT-bucket subtotal
# rows earlier on the page, which have the same "three amounts" shape.
# Spaces only, never \s, inside and between the three amounts: UBA uses a
# space as its thousands separator ("1 234,56"), but \s also matches a
# NEWLINE, which lets a match start on the previous line and swallow its
# trailing digits - e.g. "...15/11/2024\n257,19" parses Montant HT as
# "2024\n257,19", which Decimal rejects, silently making it 0 and the
# reconciliation adjustment wrong by the whole HT total. None of the 84
# real invoices trip this today; the fixture in tests/test_parser_uba.py
# does, which is the point.
GRAND_TOTAL_REGEX = re.compile(r"(\d[\d ]*,\d{2})[ \t]+(\d[\d ]*,\d{2})[ \t]+(\d[\d ]*,\d{2})[ \t]+\d+[ \t]+U\.B\.A\.")


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


_NEGLIGIBLE_PRICE = Decimal("0.001")


def _row_is_valid(row) -> bool:
    if not (row[1] and row[2]):
        return False
    # A packaging-deposit refund row (e.g. "EMB01 FÛT 10/15/20/25/30/50 L")
    # has no price of its own - column 5 (PRIX HTHD) is blank - but does
    # have a DECONS. (column 10) amount. See the module docstring.
    is_deconsigne = bool(row[10])
    # UBA prints a nominal "0,0001" (or the literal text "GRA"/gratuit) in
    # PRIX HTHD for informational, zero-cost rows (equipment loans, POS
    # material, return tickets) - real prices are never that small, so this
    # tells those apart from an actually-priced row without hardcoding
    # which product codes are which.
    has_price = _to_decimal(row[5]) >= _NEGLIGIBLE_PRICE
    return has_price or is_deconsigne


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
    needs_tables = True

    def parse_pages(
        self, pages: list[PdfPage], date_hint: date | None = None, source_name: str = ""
    ) -> ParsedInvoice:
        products_vat: dict[str, Decimal] = {}
        full_text_parts: list[str] = []
        tables = []

        for page in pages:
            text = page.text
            full_text_parts.append(text)
            for line in text.split("\n"):
                match = LINE_REGEX.match(line)
                if match:
                    product_key = match.group(1) + match.group(2)
                    tva_idx = _to_int(match.group(18))
                    products_vat[product_key] = TVA_INDEX_TO_RATE.get(tva_idx, Decimal("0.2"))
            tables += page.tables

        parsed_lines: list[ParsedLine] = []
        product_lines_total = Decimal("0")
        for table in tables:
            if len(table[0]) != 16 or table[0][0] != "CODE":
                continue
            for row in table[1:]:
                if not _row_is_valid(row):
                    continue
                product_code = row[0]
                product_name = row[1]

                quantity_match = QUANTITY_REGEX.match(row[2])
                if not quantity_match:
                    continue
                quantity = _to_int(quantity_match.group(1))

                deconsigne_total = _to_decimal(row[10])
                if deconsigne_total:
                    # A déconsigne (packaging deposit refund) row - see the
                    # module docstring. Its whole "price" is the DECONS.
                    # column; there's no product price/volume to derive.
                    if not quantity:
                        continue
                    parsed_lines.append(
                        ParsedLine(
                            raw_name=product_name,
                            quantity=quantity,
                            total_volume=Decimal("0"),
                            unit_cost_ht=(deconsigne_total / quantity).quantize(Decimal("0.0001")),
                            total_ht=deconsigne_total,
                            vat_rate=Decimal("0"),  # a deposit refund, not a taxable sale
                            category="UBA - Consignes",
                            ean=product_code,
                        )
                    )
                    continue

                product_key = product_code + product_name
                vat_rate = products_vat.get(product_key, Decimal("0.2"))
                cont_unit = _to_decimal(row[12])
                if cont_unit:
                    unit_price = _to_decimal(row[5])
                    unit_tax = _to_decimal(row[8])
                    total_volume = _to_decimal(row[13])
                    base_total_ht = total_volume / cont_unit * unit_price
                    total_taxes = total_volume / cont_unit * unit_tax
                else:
                    # No volume/weight concept at all - a CO2 cylinder is
                    # billed as a flat "1 TUB" with a real MNT HTHD (row[6])
                    # printed, but some rows (e.g. "EMB29 CFP VIDE", an
                    # empty crate/box) have NOTHING in that column - PRIX
                    # HTHD there just echoes the consigne value rather than
                    # being a real per-unit price, so deriving quantity *
                    # unit_price would fabricate a charge that isn't really
                    # there. Reading MNT HTHD directly (0 when blank) avoids
                    # that: no printed total means no real product charge,
                    # only whatever consigne (below) applies.
                    total_volume = Decimal("0")
                    total_taxes = Decimal("0")
                    base_total_ht = _to_decimal(row[6])
                # "Droit Unitaire" (row[8]) is a real per-unit duty (alcohol
                # excise), not VAT - it has to be added to the real cost, the
                # same way Metro's "COTIS. SECURITE SOCIALE" does (see
                # metro.py) - the original parser's shared
                # `montant_ht - promotions + taxes` formula (ProductsUtils.py)
                # applied to this data too.
                total_ht = base_total_ht + total_taxes

                if total_ht:
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
                    product_lines_total += total_ht

                # A consigne (packaging deposit charge) is billed on the
                # SAME row as the product it came with - split out into its
                # own line (see module docstring) rather than folded into
                # the product's own cost, since it's a refundable deposit
                # on the container, not part of what the product is worth.
                consigne_total = _to_decimal(row[9])
                if consigne_total and quantity:
                    parsed_lines.append(
                        ParsedLine(
                            raw_name=f"Consigne {product_name}",
                            quantity=quantity,
                            total_volume=Decimal("0"),
                            unit_cost_ht=(consigne_total / quantity).quantize(Decimal("0.0001")),
                            total_ht=consigne_total,
                            vat_rate=Decimal("0"),  # a refundable deposit, not a taxable sale
                            category="UBA - Consignes",
                            ean="",
                        )
                    )

        full_text = "\n".join(full_text_parts)
        invoice_number, invoice_date = _guess_invoice_number_and_date(full_text)
        if invoice_date is None:
            invoice_date = date_hint

        # UBA's own printed grand total (Montant HT + Droits) sometimes
        # includes duty categories (e.g. "VIG. SECU") that never show up in
        # any per-product column, so summing the lines above alone slightly
        # understates it - see Invoice.reconciliation_adjustment. Left at 0
        # if the summary line isn't found rather than guessing.
        reconciliation_adjustment = Decimal("0")
        grand_total_match = GRAND_TOTAL_REGEX.search(full_text)
        if grand_total_match:
            printed_ht = _to_decimal(grand_total_match.group(1).replace(" ", ""))
            printed_droits = _to_decimal(grand_total_match.group(2).replace(" ", ""))
            reconciliation_adjustment = (printed_ht + printed_droits) - product_lines_total

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            lines=parsed_lines,
            reconciliation_adjustment=reconciliation_adjustment,
        )
