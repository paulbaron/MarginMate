"""Reading the bar's own electronic invoice as a sale - « Lire la facture ».

Pure: bytes in, a `SaleReading` out. No database, no request, no file:
recipes/sale_files.py reads the file, and decides what becomes of it.

**Achats' reader, not a second one.** `invoices.einvoice.read` does all the
reading - the refusals (a DOCTYPE, an encoding, a currency, a figure past
its own bounds), the signs (a credit note), the profiles that carry no line,
the document's own checks - and states the buyer, the totals and the
references BT-25 / BT-72 / BG-14 in `EInvoiceFacts`, never in `source_text`.
This module turns that into what a « facture de vente » holds, and asks one
more question: does each figure fit the SALE column it goes into? A sale
line's quantity is (10,4) where Achats' is (12,3), its rate stops at 100 %
where Achats' stops at 999,99 %: what does not fit is refused in French and
never cut (CLAUDE.md « A figure wider than the column behind it is refused,
at the door »). A name is cut.

Every refusal is an `einvoice.EInvoiceError` - a ValueError with a French
sentence - so the page says each one as it is (common.error_for_page,
`said=`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from common import fits_column, group_thousands, plain_number, search_key
from invoices import einvoice
from invoices.einvoice import EInvoiceError

from .models import SaleDocument, SaleDocumentLine

#: More lines than this and the page that ties them would be tens of
#: megabytes (a select of every recipe and article per line) and its save
#: past DATA_UPLOAD_MAX_NUMBER_FIELDS (25 000, config/settings.py:324): a 400
#: « before the view exists ». A bar's sales invoice has a few lines.
MAX_SALE_LINES = 500
TOO_MANY_LINES = (
    "Cette facture électronique compte {count} lignes : MarginMate en relie {limit} au plus. "
    "Ajoutez-la avec « + Facture de vente » et saisissez son total."
)
#: A figure the invoice states that a sale column cannot hold - refused, never
#: cut. `what` names it, `value` is as stated.
SALE_FIGURE_REFUSED = (
    "Cette facture électronique déclare {what} que MarginMate ne peut pas enregistrer ({value}) : "
    "elle n'est pas ajoutée."
)
#: What a MINIMUM / BASIC WL invoice's line rebuilt from its VAT table says
#: it is: money at a rate, never what was sold.
REBUILT_LABEL = "Total au taux de {rate} % (facture sans lignes)"
#: BT-3 of a deposit invoice (« facture d'acompte »).
DEPOSIT_TYPE_CODE = "386"
#: A 380 saying this in its number or a line is perhaps a deposit: said, never
#: decided (spec §2.5).
DEPOSIT_WORD = "acompte"

CENTS = Decimal("0.01")
HUNDRED = Decimal("100")
ONE = Decimal("1")
#: SaleDocument.reference.
MAX_NUMBER = SaleDocument._meta.get_field("reference").max_length
#: SaleDocumentLine.vat_rate and SaleDocument.adjustment_vat_rate are
#: validated at most 1 (100 %); their columns hold 9,9999.
MAX_RATE = ONE


@dataclass(frozen=True)
class SaleLineReading:
    label: str  # BT-153, cut to 255 (einvoice._short)
    quantity: Decimal  # signed; fits (10,4)
    unit_price_ht: Decimal | None  # positive; fits (10,4)
    total_ht: Decimal  # signed; fits (12,2)
    vat_rate: Decimal  # a fraction, 0..1; fits (5,4)
    rebuilt: bool = False  # a MINIMUM profile's line, rebuilt from its VAT table


@dataclass(frozen=True)
class SaleReading:
    syntax: str  # CII / UBL
    profile: str
    number: str  # BT-1, at most 100 characters
    issued: date | None  # BT-2 as stated
    delivered: date | None  # BT-72, else BG-14's start, as stated
    type_code: str  # BT-3
    is_credit_note: bool
    is_deposit: bool  # BT-3 == "386"
    preceding_number: str  # BT-25, cut to 100
    mentions_deposit: bool  # « acompte » in BT-1 or a line's BT-153 (search_key), not 386
    seller_name: str
    seller_siren: str  # einvoice.party_siren(seller BT-30, BT-31)
    customer: str  # BT-44, cut to 255
    customer_identifier: str  # BT-47, else BT-48, as stated (40)
    customer_siren: str  # einvoice.party_siren(buyer BT-47, BT-48)
    total_ttc: Decimal | None  # BT-112 (ParsedInvoice.printed_total_ttc)
    total_ht: Decimal | None  # BT-109, to the cent, fits (12,2)
    prepaid: Decimal | None  # BT-113
    payable: Decimal | None  # BT-115
    adjustment_ht: Decimal  # ParsedInvoice.reconciliation_adjustment
    adjustment_vat_rate: Decimal | None
    lines: tuple[SaleLineReading, ...]
    carries_no_lines: bool
    checks: tuple[dict, ...]  # {"label", "passed", "detail"} - einvoice's own


def read_sale(data: bytes) -> SaleReading:
    """Raises einvoice.EInvoiceError (a ValueError, a French sentence) for
    everything einvoice.read refuses - not an EN 16931 invoice, DOCTYPE,
    encoding, non-EUR, a figure past einvoice's own bounds - for more than
    MAX_SALE_LINES lines, and for a figure that does not fit a SALE column,
    never truncated."""
    parsed = einvoice.read(data)
    facts = parsed.einvoice
    assert facts is not None  # einvoice.read always states them
    lines_read = parsed.vat_breakdown if facts.carries_no_lines else parsed.lines
    if len(lines_read) > MAX_SALE_LINES:
        raise EInvoiceError(TOO_MANY_LINES.format(count=len(lines_read), limit=MAX_SALE_LINES))

    number = parsed.invoice_number
    if len(number) > MAX_NUMBER:
        _refuse("un numéro de plus de 100 caractères", number[:20] + "…")

    if facts.carries_no_lines:
        lines = tuple(_rebuilt(rate, base) for rate, base, _tax in parsed.vat_breakdown)
    else:
        lines = tuple(
            SaleLineReading(
                label=line.raw_name,
                quantity=Decimal(line.quantity),
                unit_price_ht=line.unit_cost_ht,
                total_ht=_unsigned_zero(line.total_ht),
                vat_rate=line.vat_rate,
            )
            for line in parsed.lines
        )
    for line in lines:
        _check_line(line)

    total_ttc = _money(parsed.printed_total_ttc, "stated_total_ttc", "le total de la facture")
    total_ht = _money(facts.taxable_total, "stated_total_ht", "le total HT")
    prepaid = _money(facts.prepaid, "prepaid_ttc", "un acompte")
    payable = _money(facts.payable, "payable_ttc", "le reste à payer")
    adjustment_ht = _money(parsed.reconciliation_adjustment, "adjustment_ht", "les frais ou remises")
    _check_rate(facts.adjustment_vat_rate)

    is_deposit = facts.document_type_code == DEPOSIT_TYPE_CODE
    return SaleReading(
        syntax=facts.syntax,
        profile=facts.profile,
        number=number,
        issued=parsed.invoice_date,
        delivered=facts.delivered or facts.period_start,
        type_code=facts.document_type_code,
        is_credit_note=facts.is_credit_note,
        is_deposit=is_deposit,
        preceding_number=facts.preceding_number,
        mentions_deposit=not is_deposit and _says_deposit(number, parsed.lines),
        seller_name=facts.seller_name,
        seller_siren=einvoice.party_siren(facts.seller_siren, facts.seller_vat),
        customer=facts.buyer_name,
        customer_identifier=facts.buyer_siren or facts.buyer_vat,
        customer_siren=einvoice.party_siren(facts.buyer_siren, facts.buyer_vat),
        total_ttc=total_ttc,
        total_ht=total_ht,
        prepaid=prepaid,
        payable=payable,
        adjustment_ht=adjustment_ht if adjustment_ht is not None else Decimal("0.00"),
        adjustment_vat_rate=facts.adjustment_vat_rate,
        lines=lines,
        carries_no_lines=facts.carries_no_lines,
        checks=tuple({"label": check.label, "passed": check.passed, "detail": check.detail} for check in parsed.checks),
    )


def _rebuilt(rate: Decimal, base: Decimal) -> SaleLineReading:
    """One line of a MINIMUM / BASIC WL invoice, from a row of its VAT table:
    Achats' `charge_reading` rule, here as a free line - money at a rate,
    never tied to what was sold. A negative base (a credit note) is one
    returned, its unit price positive."""
    percent = format(rate * HUNDRED, ".2f").replace(".", ",")
    return SaleLineReading(
        label=REBUILT_LABEL.format(rate=percent),
        quantity=-ONE if base < 0 else ONE,
        unit_price_ht=abs(base),
        total_ht=_unsigned_zero(base),
        vat_rate=rate,
        rebuilt=True,
    )


def _check_line(line: SaleLineReading) -> None:
    if not fits_column(SaleDocumentLine, "quantity", line.quantity):
        _refuse("une quantité", plain_number(line.quantity))
    if not fits_column(SaleDocumentLine, "unit_price_ht", line.unit_price_ht):
        _refuse("un prix unitaire", group_thousands(line.unit_price_ht))
    if not fits_column(SaleDocumentLine, "total_ht", line.total_ht):
        _refuse("un montant de ligne", group_thousands(line.total_ht))
    _check_rate(line.vat_rate)


def _check_rate(rate: Decimal | None) -> None:
    """A rate above 100 %, or below 0: the sale columns' validator (0 to 1)
    refuses it, though their (5,4) and einvoice's own bound (± 999,99 %)
    hold it - stored, a line's TTC would be read below its HT."""
    if rate is None:
        return
    if rate > MAX_RATE:
        _refuse("un taux de TVA de plus de 100 %", format(rate * HUNDRED, ".2f").replace(".", ",") + " %")
    if rate < 0:
        _refuse("un taux de TVA négatif", format(rate * HUNDRED, ".2f").replace(".", ",") + " %")


def _money(value: Decimal | None, field_name: str, what: str) -> Decimal | None:
    """`value` to the cent, half away from zero (the rule this codebase
    converts money with), or a refusal when SaleDocument.`field_name` cannot
    hold it - asked before the rounding, which a figure such as 1E+999999999
    would not survive, and after it, which may carry 9 999 999 999,995 past
    the column."""
    if value is None:
        return None
    if not fits_column(SaleDocument, field_name, value):
        _refuse(what, group_thousands(value))
    rounded = value.quantize(CENTS, rounding=ROUND_HALF_UP)
    if not fits_column(SaleDocument, field_name, rounded):
        _refuse(what, group_thousands(value))
    return _unsigned_zero(rounded)


def _unsigned_zero(value: Decimal) -> Decimal:
    """0 as « 0.00 », never the « -0.00 » einvoice's signing makes of a
    credit note's zero (0 × -1): a page would print it so."""
    return abs(value) if value == 0 else value


def _says_deposit(number: str, lines) -> bool:
    """« acompte » in the number or a line's label, case and accents aside."""
    return any(DEPOSIT_WORD in search_key(text) for text in (number, *(line.raw_name for line in lines)))


def _refuse(what: str, value: str):
    raise EInvoiceError(SALE_FIGURE_REFUSED.format(what=what, value=value))
