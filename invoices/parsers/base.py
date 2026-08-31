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


@dataclass
class ParsedInvoice:
    supplier_code: str
    invoice_number: str
    invoice_date: date | None
    lines: list[ParsedLine] = field(default_factory=list)


class InvoiceParser:
    """One InvoiceParser subclass per supplier PDF layout."""

    supplier_code: str = ""

    def parse(self, pdf_path: str, date_hint: date | None = None) -> ParsedInvoice:
        raise NotImplementedError
