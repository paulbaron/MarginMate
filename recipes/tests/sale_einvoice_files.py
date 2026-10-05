"""The bar's own electronic invoices - « factures de vente » - written by hand.

Built from `invoices/tests/einvoice_files.py`'s purchase invoices by
`.replace`, that module's own habit: the same namespaces, elements and
attributes (what the reader keys on), the bar now the SELLER and a customer
the buyer. Every name, SIREN, VAT number, number, date and amount is invented.

The three SIRENs are the house's invented ones (pages critique 19): any other
nine digits passing the Luhn check may be a real company, and the repository
is public. The bar is the buyer of every purchase fixture, the supplier their
seller; import them from here, never retype them.
"""

import re

from invoices.tests.einvoice_files import CII_TWO_RATES, UBL_TWO_RATES
from invoices.tests.pdf_files import write_pdf_with_attachments

#: « Bar des tests », the bar - the SELLER of every sales invoice here.
BAR_SIREN = "800000002"
BAR_VAT = "FR22800000002"
#: « Exemple Événements SARL », a customer.
CUSTOMER_SIREN = "700000003"
CUSTOMER_VAT = "FR73700000003"
#: A supplier of the bar - the seller of the purchase fixtures.
SUPPLIER_SIREN = "900000019"
SUPPLIER_VAT = "FR25900000019"

BAR_NAME = "Bar des tests"
CUSTOMER_NAME = "Exemple Événements SARL"
SALE_NUMBER = "FV-2026-0101"


def _swap(text: str, old: str, new: str, count: int = -1) -> str:
    """`text.replace(old, new, count)`, refusing an `old` that is not there:
    a fixture whose replacement silently matched nothing is a test of the
    invoice it was copied from."""
    if old not in text:
        raise ValueError(f"fixture: {old[:60]!r} is not in the invoice it is built from")
    return text.replace(old, new, count)


def _with_number(text: str, number: str) -> str:
    return _swap(text, f"<ram:ID>{SALE_NUMBER}</ram:ID>", f"<ram:ID>{number}</ram:ID>", 1)


_CII_PURCHASE_BUYER = """      <ram:BuyerTradeParty>
        <ram:Name>Le Comptoir Exemple</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">800000002</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:BuyerTradeParty>"""


def _cii_buyer(name: str, siren: str, vat: str) -> str:
    return f"""      <ram:BuyerTradeParty>
        <ram:Name>{name}</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">{siren}</ram:ID>
        </ram:SpecifiedLegalOrganization>
        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">{vat}</ram:ID>
        </ram:SpecifiedTaxRegistration>
      </ram:BuyerTradeParty>"""


# A commercial invoice (380) the bar issued: two lines, at 20 % and 10 %,
# 230,52 € TTC, dated 03/09/2026, no delivery date.
SALE_CII = (
    _swap(
        _swap(CII_TWO_RATES, "<ram:ID>FA-2026-0042</ram:ID>", f"<ram:ID>{SALE_NUMBER}</ram:ID>", 1),
        _CII_PURCHASE_BUYER,
        _cii_buyer(CUSTOMER_NAME, CUSTOMER_SIREN, CUSTOMER_VAT),
    )
    .replace("<ram:Name>Brasserie du Canal</ram:Name>", f"<ram:Name>{BAR_NAME}</ram:Name>")
    .replace(f'<ram:ID schemeID="0002">{SUPPLIER_SIREN}</ram:ID>', f'<ram:ID schemeID="0002">{BAR_SIREN}</ram:ID>')
    .replace(f'<ram:ID schemeID="VA">{SUPPLIER_VAT}</ram:ID>', f'<ram:ID schemeID="VA">{BAR_VAT}</ram:ID>')
    .replace("<ram:Name>BIERE BLONDE FUT 30L</ram:Name>", "<ram:Name>Formule cocktail</ram:Name>")
    .replace("<ram:Name>SIROP CITRON 1L</ram:Name>", "<ram:Name>Planche apéritive</ram:Name>")
    .replace(
        "<ram:RateApplicablePercent>5.50</ram:RateApplicablePercent>",
        "<ram:RateApplicablePercent>10.00</ram:RateApplicablePercent>",
    )
    .replace("<ram:CalculatedAmount>1.39</ram:CalculatedAmount>", "<ram:CalculatedAmount>2.52</ram:CalculatedAmount>")
    .replace(
        '<ram:TaxTotalAmount currencyID="EUR">35.19</ram:TaxTotalAmount>',
        '<ram:TaxTotalAmount currencyID="EUR">36.32</ram:TaxTotalAmount>',
    )
    .replace(
        "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>", "<ram:GrandTotalAmount>230.52</ram:GrandTotalAmount>"
    )
    .replace(
        "<ram:DuePayableAmount>229.39</ram:DuePayableAmount>", "<ram:DuePayableAmount>230.52</ram:DuePayableAmount>"
    )
)

# The same invoice in UBL: the same figures, read as the same sale.
SALE_UBL = (
    _swap(UBL_TWO_RATES, "<cbc:ID>FA-2026-0042</cbc:ID>", f"<cbc:ID>{SALE_NUMBER}</cbc:ID>", 1)
    .replace(
        "<cbc:RegistrationName>Le Comptoir Exemple</cbc:RegistrationName>",
        f"<cbc:RegistrationName>{CUSTOMER_NAME}</cbc:RegistrationName>",
    )
    .replace(
        '<cbc:CompanyID schemeID="0002">800000002</cbc:CompanyID>',
        f'<cbc:CompanyID schemeID="0002">{CUSTOMER_SIREN}</cbc:CompanyID>',
    )
    .replace(
        "  <cac:AccountingCustomerParty>\n    <cac:Party>\n",
        "  <cac:AccountingCustomerParty>\n    <cac:Party>\n"
        f"      <cac:PartyTaxScheme>\n        <cbc:CompanyID>{CUSTOMER_VAT}</cbc:CompanyID>\n"
        "        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>\n      </cac:PartyTaxScheme>\n",
    )
    .replace("Brasserie du Canal", BAR_NAME)
    .replace(SUPPLIER_VAT, BAR_VAT)
    .replace(SUPPLIER_SIREN, BAR_SIREN)
    .replace("<cbc:Name>BIERE BLONDE FUT 30L</cbc:Name>", "<cbc:Name>Formule cocktail</cbc:Name>")
    .replace("<cbc:Name>SIROP CITRON 1L</cbc:Name>", "<cbc:Name>Planche apéritive</cbc:Name>")
    .replace("<cbc:Percent>5.50</cbc:Percent>", "<cbc:Percent>10.00</cbc:Percent>")
    .replace(
        '<cbc:TaxAmount currencyID="EUR">1.39</cbc:TaxAmount>', '<cbc:TaxAmount currencyID="EUR">2.52</cbc:TaxAmount>'
    )
    .replace(
        '<cbc:TaxAmount currencyID="EUR">35.19</cbc:TaxAmount>', '<cbc:TaxAmount currencyID="EUR">36.32</cbc:TaxAmount>'
    )
    .replace(
        '<cbc:TaxInclusiveAmount currencyID="EUR">229.39</cbc:TaxInclusiveAmount>',
        '<cbc:TaxInclusiveAmount currencyID="EUR">230.52</cbc:TaxInclusiveAmount>',
    )
    .replace(
        '<cbc:PayableAmount currencyID="EUR">229.39</cbc:PayableAmount>',
        '<cbc:PayableAmount currencyID="EUR">230.52</cbc:PayableAmount>',
    )
)

_SETTLEMENT_END = "    </ram:ApplicableHeaderTradeSettlement>"
_SUMMATION = "      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>"

# A credit note (381) correcting SALE_CII: BT-25 names its number. Its
# amounts are stated positive, as every credit note states them.
SALE_CII_CREDIT_NOTE = _swap(
    _swap(
        _with_number(SALE_CII, "AV-2026-0011"), "<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>381</ram:TypeCode>"
    ),
    _SETTLEMENT_END,
    "      <ram:InvoiceReferencedDocument>\n"
    f"        <ram:IssuerAssignedID>{SALE_NUMBER}</ram:IssuerAssignedID>\n"
    "      </ram:InvoiceReferencedDocument>\n" + _SETTLEMENT_END,
)

# A deposit invoice (386): the final invoice will count the sale.
SALE_CII_DEPOSIT = _swap(
    _with_number(SALE_CII, "AC-2026-0003"), "<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>386</ram:TypeCode>"
)

# A deposit typed as an ordinary invoice (380): only its line says so.
SALE_CII_DEPOSIT_AS_380 = _swap(
    _with_number(SALE_CII, "FV-2026-0102"),
    "<ram:Name>Formule cocktail</ram:Name>",
    "<ram:Name>Acompte 30 % - mariage du 12/09</ram:Name>",
)

# The MINIMUM profile: the totals and the VAT table, no line at all.
SALE_CII_MINIMUM = _swap(
    _swap(
        re.sub(
            r"\n    <ram:IncludedSupplyChainTradeLineItem>.*?</ram:IncludedSupplyChainTradeLineItem>",
            "",
            _with_number(SALE_CII, "FV-2026-0103"),
            flags=re.DOTALL,
        ),
        "<ram:ID>urn:cen.eu:en16931:2017</ram:ID>",
        "<ram:ID>urn:factur-x.eu:1p0:minimum</ram:ID>",
    ),
    "        <ram:LineTotalAmount>194.20</ram:LineTotalAmount>\n",
    "",
)

# 100,00 € paid before the invoice (BT-113): 130,52 € left to pay (BT-115).
SALE_CII_PREPAID = _swap(
    _with_number(SALE_CII, "FV-2026-0104"),
    "<ram:DuePayableAmount>230.52</ram:DuePayableAmount>",
    "<ram:TotalPrepaidAmount>100.00</ram:TotalPrepaidAmount>\n        "
    "<ram:DuePayableAmount>130.52</ram:DuePayableAmount>",
)

_CURRENCY = "<ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>"


def _allowance_charge(is_charge: bool, amount: str, reason: str, percent: str) -> str:
    return (
        f"{_CURRENCY}\n"
        "      <ram:SpecifiedTradeAllowanceCharge>\n"
        "        <ram:ChargeIndicator><udt:Indicator>"
        f"{'true' if is_charge else 'false'}</udt:Indicator></ram:ChargeIndicator>\n"
        f"        <ram:ActualAmount>{amount}</ram:ActualAmount>\n"
        f"        <ram:Reason>{reason}</ram:Reason>\n"
        "        <ram:CategoryTradeTax>\n"
        "          <ram:TypeCode>VAT</ram:TypeCode>\n"
        "          <ram:CategoryCode>S</ram:CategoryCode>\n"
        f"          <ram:RateApplicablePercent>{percent}</ram:RateApplicablePercent>\n"
        "        </ram:CategoryTradeTax>\n"
        "      </ram:SpecifiedTradeAllowanceCharge>"
    )


def _restated(text: str, base_20: str, tax_20: str, basis: str, tax: str, grand: str, extra_total: str) -> str:
    """SALE_CII's 20 % row and totals stated again around a document-level
    allowance or charge (`extra_total`, BT-107 or BT-108)."""
    for old, new in (
        ("<ram:BasisAmount>169.00</ram:BasisAmount>", f"<ram:BasisAmount>{base_20}</ram:BasisAmount>"),
        (
            "<ram:CalculatedAmount>33.80</ram:CalculatedAmount>",
            f"<ram:CalculatedAmount>{tax_20}</ram:CalculatedAmount>",
        ),
        (
            "<ram:LineTotalAmount>194.20</ram:LineTotalAmount>",
            f"<ram:LineTotalAmount>194.20</ram:LineTotalAmount>\n        {extra_total}",
        ),
        (
            "<ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>",
            f"<ram:TaxBasisTotalAmount>{basis}</ram:TaxBasisTotalAmount>",
        ),
        (
            '<ram:TaxTotalAmount currencyID="EUR">36.32</ram:TaxTotalAmount>',
            f'<ram:TaxTotalAmount currencyID="EUR">{tax}</ram:TaxTotalAmount>',
        ),
        (
            "<ram:GrandTotalAmount>230.52</ram:GrandTotalAmount>",
            f"<ram:GrandTotalAmount>{grand}</ram:GrandTotalAmount>",
        ),
        (
            "<ram:DuePayableAmount>230.52</ram:DuePayableAmount>",
            f"<ram:DuePayableAmount>{grand}</ram:DuePayableAmount>",
        ),
    ):
        text = _swap(text, old, new, 1)
    return text


# A document-level CHARGE (BG-21): 12,00 € of service charge at 20 %.
SALE_CII_CHARGE = _restated(
    _swap(
        _with_number(SALE_CII, "FV-2026-0105"), _CURRENCY, _allowance_charge(True, "12.00", "Frais de service", "20.00")
    ),
    base_20="181.00",
    tax_20="36.20",
    basis="206.20",
    tax="38.72",
    grand="244.92",
    extra_total="<ram:ChargeTotalAmount>12.00</ram:ChargeTotalAmount>",
)

# A document-level ALLOWANCE (BG-20) at its own stated rate: 10,00 € off at
# 20 %.
SALE_CII_ALLOWANCE = _restated(
    _swap(
        _with_number(SALE_CII, "FV-2026-0106"), _CURRENCY, _allowance_charge(False, "10.00", "Remise fidélité", "20.00")
    ),
    base_20="159.00",
    tax_20="31.80",
    basis="184.20",
    tax="34.32",
    grand="218.52",
    extra_total="<ram:AllowanceTotalAmount>10.00</ram:AllowanceTotalAmount>",
)

# The charge above from a sender that states no grand total (BT-112) nor
# amount due (BT-115): its total is its lines' plus its charge at its own
# rate - 230,52 + 14,40 = 244,92 €.
SALE_CII_CHARGE_NO_GRAND_TOTAL = _swap(
    _swap(
        _swap(SALE_CII_CHARGE, "<ram:ID>FV-2026-0105</ram:ID>", "<ram:ID>FV-2026-0115</ram:ID>", 1),
        "        <ram:GrandTotalAmount>244.92</ram:GrandTotalAmount>\n",
        "",
        1,
    ),
    "        <ram:DuePayableAmount>244.92</ram:DuePayableAmount>\n",
    "",
    1,
)

# BT-72: delivered on 31/08/2026, three days before the invoice (03/09).
SALE_CII_DELIVERED = _swap(
    _with_number(SALE_CII, "FV-2026-0107"),
    "<ram:ApplicableHeaderTradeDelivery/>",
    "<ram:ApplicableHeaderTradeDelivery>\n"
    "      <ram:ActualDeliverySupplyChainEvent>\n"
    "        <ram:OccurrenceDateTime>\n"
    '          <udt:DateTimeString format="102">20260831</udt:DateTimeString>\n'
    "        </ram:OccurrenceDateTime>\n"
    "      </ram:ActualDeliverySupplyChainEvent>\n"
    "    </ram:ApplicableHeaderTradeDelivery>",
)

# BG-14 only: the billing period 15/08/2026 - 31/08/2026, no delivery date.
SALE_CII_PERIOD = _swap(
    _with_number(SALE_CII, "FV-2026-0108"),
    _SUMMATION,
    "      <ram:BillingSpecifiedPeriod>\n"
    "        <ram:StartDateTime>\n"
    '          <udt:DateTimeString format="102">20260815</udt:DateTimeString>\n'
    "        </ram:StartDateTime>\n"
    "        <ram:EndDateTime>\n"
    '          <udt:DateTimeString format="102">20260831</udt:DateTimeString>\n'
    "        </ram:EndDateTime>\n"
    "      </ram:BillingSpecifiedPeriod>\n" + _SUMMATION,
)

# No BT-2 at all.
SALE_CII_NO_DATE = _swap(
    _with_number(SALE_CII, "FV-2026-0109"),
    "    <ram:IssueDateTime>\n"
    '      <udt:DateTimeString format="102">20260903</udt:DateTimeString>\n'
    "    </ram:IssueDateTime>\n",
    "",
)

# BT-2 on 01/01/0001: a date no sale happened on.
SALE_CII_YEAR_ONE = _swap(
    _with_number(SALE_CII, "FV-2026-0110"),
    '<udt:DateTimeString format="102">20260903</udt:DateTimeString>',
    '<udt:DateTimeString format="102">00010101</udt:DateTimeString>',
)

# A line at 150 %: einvoice reads it (its column holds 999,99 %), a sale
# line's validator stops at 100 %.
SALE_CII_RATE_OVER_100 = _swap(
    _with_number(SALE_CII, "FV-2026-0111"),
    "<ram:RateApplicablePercent>10.00</ram:RateApplicablePercent>",
    "<ram:RateApplicablePercent>150.00</ram:RateApplicablePercent>",
    1,
)

# 1 234 567,5 : fits einvoice's quantity (12,3), not a sale line's (10,4).
SALE_CII_QUANTITY_TOO_WIDE = _swap(
    _with_number(SALE_CII, "FV-2026-0112"),
    '<ram:BilledQuantity unitCode="H87">2</ram:BilledQuantity>',
    '<ram:BilledQuantity unitCode="H87">1234567.5</ram:BilledQuantity>',
)

# A number of 101 characters: SaleDocument.reference holds 100.
SALE_CII_NUMBER_TOO_LONG = _with_number(SALE_CII, "FV-" + "9" * 98)

# The mirror shape on a 380: a NEGATIVE count at a POSITIVE amount (-1 ×
# 60,00), neither a sale nor a refund. Read as stated, its check failing.
SALE_CII_MIRROR_LINE = (
    _swap(
        _with_number(SALE_CII, "FV-2026-0113"),
        "<ram:ChargeAmount>4.20</ram:ChargeAmount>",
        "<ram:ChargeAmount>60.00</ram:ChargeAmount>",
    )
    .replace(
        '<ram:BilledQuantity unitCode="H87">6</ram:BilledQuantity>',
        '<ram:BilledQuantity unitCode="H87">-1</ram:BilledQuantity>',
    )
    .replace("<ram:LineTotalAmount>25.20</ram:LineTotalAmount>", "<ram:LineTotalAmount>60.00</ram:LineTotalAmount>")
    .replace("<ram:BasisAmount>25.20</ram:BasisAmount>", "<ram:BasisAmount>60.00</ram:BasisAmount>")
    .replace("<ram:CalculatedAmount>2.52</ram:CalculatedAmount>", "<ram:CalculatedAmount>6.00</ram:CalculatedAmount>")
    .replace("<ram:LineTotalAmount>194.20</ram:LineTotalAmount>", "<ram:LineTotalAmount>229.00</ram:LineTotalAmount>")
    .replace(
        "<ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>",
        "<ram:TaxBasisTotalAmount>229.00</ram:TaxBasisTotalAmount>",
    )
    .replace(
        '<ram:TaxTotalAmount currencyID="EUR">36.32</ram:TaxTotalAmount>',
        '<ram:TaxTotalAmount currencyID="EUR">39.80</ram:TaxTotalAmount>',
    )
    .replace(
        "<ram:GrandTotalAmount>230.52</ram:GrandTotalAmount>", "<ram:GrandTotalAmount>268.80</ram:GrandTotalAmount>"
    )
    .replace(
        "<ram:DuePayableAmount>230.52</ram:DuePayableAmount>", "<ram:DuePayableAmount>268.80</ram:DuePayableAmount>"
    )
)

# Its seller is one of the bar's suppliers: a purchase dropped on the sales side.
SALE_CII_SELLER_IS_A_SUPPLIER = (
    _with_number(SALE_CII, "FV-2026-0114")
    .replace(f"<ram:Name>{BAR_NAME}</ram:Name>", "<ram:Name>Brasserie du Canal</ram:Name>")
    .replace(f'<ram:ID schemeID="0002">{BAR_SIREN}</ram:ID>', f'<ram:ID schemeID="0002">{SUPPLIER_SIREN}</ram:ID>')
    .replace(f'<ram:ID schemeID="VA">{BAR_VAT}</ram:ID>', f'<ram:ID schemeID="VA">{SUPPLIER_VAT}</ram:ID>')
)

# Its buyer is the bar: a supplier's invoice, not a sale.
SALE_CII_BUYER_IS_THE_BAR = _swap(
    SALE_CII_SELLER_IS_A_SUPPLIER.replace("<ram:ID>FV-2026-0114</ram:ID>", "<ram:ID>FA-2026-0115</ram:ID>"),
    _cii_buyer(CUSTOMER_NAME, CUSTOMER_SIREN, CUSTOMER_VAT),
    _cii_buyer(BAR_NAME, BAR_SIREN, BAR_VAT),
)

_LINE = """    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>{index}</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>Verre {index}</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>1.00</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="H87">1</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>1.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
"""


def sale_cii_with_lines(count: int) -> str:
    """A sale of `count` lines « Verre n », 1 × 1,00 € HT at 20 % each, whose
    totals add up - for MAX_SALE_LINES."""
    base = f"{count}.00"
    tax = f"{count * 20 // 100}.{count * 20 % 100:02d}"
    grand = f"{count * 120 // 100}.{count * 120 % 100:02d}"
    lines = "".join(_LINE.format(index=index) for index in range(1, count + 1))
    body = re.sub(
        r"    <ram:IncludedSupplyChainTradeLineItem>.*</ram:IncludedSupplyChainTradeLineItem>\n",
        lambda _match: lines,
        _with_number(SALE_CII, f"FV-2026-L{count}"),
        flags=re.DOTALL,
    )
    body = re.sub(
        r"      <ram:ApplicableTradeTax>\n        <ram:CalculatedAmount>2\.52</ram:CalculatedAmount>.*?"
        r"</ram:ApplicableTradeTax>\n",
        "",
        body,
        flags=re.DOTALL,
    )
    for old, new in (
        ("<ram:CalculatedAmount>33.80</ram:CalculatedAmount>", f"<ram:CalculatedAmount>{tax}</ram:CalculatedAmount>"),
        ("<ram:BasisAmount>169.00</ram:BasisAmount>", f"<ram:BasisAmount>{base}</ram:BasisAmount>"),
        ("<ram:LineTotalAmount>194.20</ram:LineTotalAmount>", f"<ram:LineTotalAmount>{base}</ram:LineTotalAmount>"),
        (
            "<ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>",
            f"<ram:TaxBasisTotalAmount>{base}</ram:TaxBasisTotalAmount>",
        ),
        (
            '<ram:TaxTotalAmount currencyID="EUR">36.32</ram:TaxTotalAmount>',
            f'<ram:TaxTotalAmount currencyID="EUR">{tax}</ram:TaxTotalAmount>',
        ),
        (
            "<ram:GrandTotalAmount>230.52</ram:GrandTotalAmount>",
            f"<ram:GrandTotalAmount>{grand}</ram:GrandTotalAmount>",
        ),
        (
            "<ram:DuePayableAmount>230.52</ram:DuePayableAmount>",
            f"<ram:DuePayableAmount>{grand}</ram:DuePayableAmount>",
        ),
    ):
        body = _swap(body, old, new, 1)
    return body


# The hostile shapes einvoice refuses, as a sales invoice: UTF-16 (bytes,
# behind its byte order mark - read as such, the DOCTYPE grep is blind) and
# a DOCTYPE.
SALE_CII_UTF16 = _swap(SALE_CII, 'encoding="UTF-8"', 'encoding="UTF-16"', 1).encode("utf-16")
SALE_CII_DOCTYPE = _swap(
    SALE_CII,
    "?>\n",
    '?>\n<!DOCTYPE rsm:CrossIndustryInvoice [\n  <!ENTITY client "Exemple">\n]>\n',
    1,
)


def factur_x(path: str, xml: str | bytes) -> str:
    """A Factur-X PDF at `path`: a page of text carrying `xml` as its
    « factur-x.xml » attachment (invoices/tests/pdf_files.py builds it)."""
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    return write_pdf_with_attachments(path, ["Facture de vente", "Total 230,52 EUR"], [("factur-x.xml", data, "Data")])
