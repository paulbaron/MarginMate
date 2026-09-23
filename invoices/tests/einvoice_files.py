"""EN 16931 invoices written by hand: CII (what Factur-X embeds) and UBL.

Not one line of this comes off a real document. The project holds no
Factur-X invoice at all - none of the PDFs filed carries an embedded XML - so
there was nothing to copy, and there must never be: a real one carries the
supplier's IBAN, the bar's address and its prices, and this repository is
public and has leaked real data once already.

What IS faithful is the structure: the namespaces, the element names, the
nesting and the attributes (`schemeID="0002"` for a SIREN, `format="102"` for
a date) are the standard's own, because those are what the reader keys on. A
guessed element name would test a document that does not exist.

Every name, SIREN, VAT number, date and amount is invented. The SIREN
900000019 is the one `invoices/identifiers.py` already uses in its own
docstring: it satisfies the Luhn check, which is what makes it usable as a
fixture, and it belongs to nobody.
"""

# A plain commercial invoice (type 380), two lines at two rates, in CII -
# the form Factur-X embeds in its PDF.
CII_TWO_RATES = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:cen.eu:en16931:2017</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
  <rsm:ExchangedDocument>
    <ram:ID>FA-2026-0042</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260903</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument>
        <ram:LineID>1</ram:LineID>
      </ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct>
        <ram:GlobalID schemeID="0160">3560070000012</ram:GlobalID>
        <ram:Name>BIERE BLONDE FUT 30L</ram:Name>
      </ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice>
          <ram:ChargeAmount>84.50</ram:ChargeAmount>
        </ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">2</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>169.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument>
        <ram:LineID>2</ram:LineID>
      </ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct>
        <ram:Name>SIROP CITRON 1L</ram:Name>
      </ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice>
          <ram:ChargeAmount>4.20</ram:ChargeAmount>
        </ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">6</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>5.50</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>25.20</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Brasserie du Canal</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
        <ram:PostalTradeAddress>
          <ram:PostcodeCode>99999</ram:PostcodeCode>
          <ram:LineOne>12 quai Inventé</ram:LineOne>
          <ram:CityName>Paris</ram:CityName>
          <ram:CountryID>FR</ram:CountryID>
        </ram:PostalTradeAddress>
        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">FR25900000019</ram:ID>
        </ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
      <ram:BuyerTradeParty>
        <ram:Name>Le Comptoir Exemple</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">800000002</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>33.80</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>169.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>1.39</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>25.20</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>5.50</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>194.20</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">35.19</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>
        <ram:DuePayableAmount>229.39</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""

# The same invoice, same figures, in UBL - so a test can assert the two
# readings are the same invoice and not two dialects of one reader.
UBL_TWO_RATES = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
    xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
    xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
  <cbc:ID>FA-2026-0042</cbc:ID>
  <cbc:IssueDate>2026-09-03</cbc:IssueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyName>
        <cbc:Name>Brasserie du Canal</cbc:Name>
      </cac:PartyName>
      <cac:PostalAddress>
        <cbc:StreetName>12 quai Invente</cbc:StreetName>
        <cbc:CityName>Paris</cbc:CityName>
        <cbc:PostalZone>99999</cbc:PostalZone>
        <cac:Country><cbc:IdentificationCode>FR</cbc:IdentificationCode></cac:Country>
      </cac:PostalAddress>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>FR25900000019</cbc:CompanyID>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:PartyTaxScheme>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>Brasserie du Canal</cbc:RegistrationName>
        <cbc:CompanyID schemeID="0002">900000019</cbc:CompanyID>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>Le Comptoir Exemple</cbc:RegistrationName>
        <cbc:CompanyID schemeID="0002">800000002</cbc:CompanyID>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">35.19</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">169.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">33.80</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">25.20</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">1.39</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>5.50</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">194.20</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">194.20</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">229.39</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">229.39</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="H87">2</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">169.00</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>BIERE BLONDE FUT 30L</cbc:Name>
      <cac:StandardItemIdentification>
        <cbc:ID schemeID="0160">3560070000012</cbc:ID>
      </cac:StandardItemIdentification>
      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="EUR">84.50</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
  <cac:InvoiceLine>
    <cbc:ID>2</cbc:ID>
    <cbc:InvoicedQuantity unitCode="H87">6</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">25.20</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>SIROP CITRON 1L</cbc:Name>
      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>5.50</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="EUR">4.20</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
</Invoice>
"""

# A credit note in CII: type 381, and every amount stated POSITIVE. The
# document means money going back, which this codebase writes as a negative
# count and a negative amount.
CII_CREDIT_NOTE = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>AV-2026-0007</ram:ID>
    <ram:TypeCode>381</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260910</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument>
        <ram:LineID>1</ram:LineID>
      </ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct>
        <ram:Name>BIERE BLONDE FUT 30L</ram:Name>
      </ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice>
          <ram:ChargeAmount>84.50</ram:ChargeAmount>
        </ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">1</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>84.50</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Brasserie du Canal</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>16.90</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>84.50</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>84.50</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>84.50</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">16.90</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>101.40</ram:GrandTotalAmount>
        <ram:DuePayableAmount>101.40</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""

# The same credit note in UBL, where the document type is the ROOT element
# and the quantity element is named differently (cbc:CreditedQuantity).
UBL_CREDIT_NOTE = """<?xml version="1.0" encoding="UTF-8"?>
<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
    xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
    xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
  <cbc:ID>AV-2026-0007</cbc:ID>
  <cbc:IssueDate>2026-09-10</cbc:IssueDate>
  <cbc:CreditNoteTypeCode>381</cbc:CreditNoteTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>Brasserie du Canal</cbc:RegistrationName>
        <cbc:CompanyID schemeID="0002">900000019</cbc:CompanyID>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">16.90</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">84.50</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">16.90</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">84.50</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">84.50</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">101.40</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">101.40</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:CreditNoteLine>
    <cbc:ID>1</cbc:ID>
    <cbc:CreditedQuantity unitCode="H87">1</cbc:CreditedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">84.50</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>BIERE BLONDE FUT 30L</cbc:Name>
      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="EUR">84.50</cbc:PriceAmount>
    </cac:Price>
  </cac:CreditNoteLine>
</CreditNote>
"""

# The MINIMUM profile: totals and the VAT breakdown, and no line at all.
# That is a VALID Factur-X invoice, not a failed reading - the profile is
# named in ExchangedDocumentContext and there is nothing else in the file.
CII_MINIMUM = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:factur-x.eu:1p0:minimum</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
  <rsm:ExchangedDocument>
    <ram:ID>ABO-2026-09</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260901</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Telecom Exemple</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>24.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>120.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:TaxBasisTotalAmount>120.00</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">24.00</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>144.00</ram:GrandTotalAmount>
        <ram:DuePayableAmount>144.00</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""

# A document-level CHARGE (BG-21): delivery, an eco-participation, a duty -
# money the lines do not carry and the VAT base does.
CII_DOCUMENT_CHARGE = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>FA-2026-0051</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260904</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>1</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>VIN ROUGE 75CL</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>10.00</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">10</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>100.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Cave Exemple</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:SpecifiedTradeAllowanceCharge>
        <ram:ChargeIndicator><udt:Indicator>true</udt:Indicator></ram:ChargeIndicator>
        <ram:ActualAmount>12.00</ram:ActualAmount>
        <ram:Reason>Droits de circulation</ram:Reason>
        <ram:CategoryTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:CategoryTradeTax>
      </ram:SpecifiedTradeAllowanceCharge>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>22.40</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>112.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>100.00</ram:LineTotalAmount>
        <ram:ChargeTotalAmount>12.00</ram:ChargeTotalAmount>
        <ram:AllowanceTotalAmount>0.00</ram:AllowanceTotalAmount>
        <ram:TaxBasisTotalAmount>112.00</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">22.40</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>134.40</ram:GrandTotalAmount>
        <ram:DuePayableAmount>134.40</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""

# A document-level ALLOWANCE (BG-20): the same element, ChargeIndicator
# false - a discount taken off the whole invoice.
CII_DOCUMENT_ALLOWANCE = CII_DOCUMENT_CHARGE.replace(
    "<udt:Indicator>true</udt:Indicator>", "<udt:Indicator>false</udt:Indicator>"
).replace(
    "<ram:ActualAmount>12.00</ram:ActualAmount>", "<ram:ActualAmount>5.00</ram:ActualAmount>"
).replace(
    "<ram:Reason>Droits de circulation</ram:Reason>", "<ram:Reason>Remise de fin d'annee</ram:Reason>"
).replace(
    "<ram:CalculatedAmount>22.40</ram:CalculatedAmount>", "<ram:CalculatedAmount>19.00</ram:CalculatedAmount>"
).replace(
    "<ram:BasisAmount>112.00</ram:BasisAmount>", "<ram:BasisAmount>95.00</ram:BasisAmount>"
).replace(
    "<ram:ChargeTotalAmount>12.00</ram:ChargeTotalAmount>", "<ram:ChargeTotalAmount>0.00</ram:ChargeTotalAmount>"
).replace(
    "<ram:AllowanceTotalAmount>0.00</ram:AllowanceTotalAmount>",
    "<ram:AllowanceTotalAmount>5.00</ram:AllowanceTotalAmount>",
).replace(
    "<ram:TaxBasisTotalAmount>112.00</ram:TaxBasisTotalAmount>",
    "<ram:TaxBasisTotalAmount>95.00</ram:TaxBasisTotalAmount>",
).replace(
    "<ram:TaxTotalAmount currencyID=\"EUR\">22.40</ram:TaxTotalAmount>",
    "<ram:TaxTotalAmount currencyID=\"EUR\">19.00</ram:TaxTotalAmount>",
).replace(
    "<ram:GrandTotalAmount>134.40</ram:GrandTotalAmount>",
    "<ram:GrandTotalAmount>114.00</ram:GrandTotalAmount>",
).replace(
    "<ram:DuePayableAmount>134.40</ram:DuePayableAmount>",
    "<ram:DuePayableAmount>114.00</ram:DuePayableAmount>",
)

# A rate of 0 (category Z) beside an exempt line (category E, which carries
# NO RateApplicablePercent at all - the trap: absent is not 0).
CII_ZERO_AND_EXEMPT = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>FA-2026-0060</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260905</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>1</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>TIMBRES POSTE</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>2.00</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">10</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>Z</ram:CategoryCode>
          <ram:RateApplicablePercent>0.00</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>20.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>2</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>COTISATION ASSURANCE</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>30.00</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">1</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>E</ram:CategoryCode>
          <ram:ExemptionReason>Exoneration article 261 C du CGI</ram:ExemptionReason>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>30.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Bureau Exemple</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>0.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>20.00</ram:BasisAmount>
        <ram:CategoryCode>Z</ram:CategoryCode>
        <ram:RateApplicablePercent>0.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>0.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>30.00</ram:BasisAmount>
        <ram:CategoryCode>E</ram:CategoryCode>
        <ram:ExemptionReason>Exoneration article 261 C du CGI</ram:ExemptionReason>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>50.00</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>50.00</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">0.00</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>50.00</ram:GrandTotalAmount>
        <ram:DuePayableAmount>50.00</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""

# An invoice whose figures carry more decimals than this application stores:
# a line of 12.345 (total_ht is two decimals) at a unit price of 4.1150
# (unit_cost_ht is four). The document's own arithmetic holds exactly.
CII_MORE_DECIMALS = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>FA-2026-0070</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260906</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>1</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>SERVIETTES PAPIER</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>4.1150</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">3</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>12.345</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Papeterie Exemple</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>2.469</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>12.345</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>12.345</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>12.345</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">2.469</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>14.814</ram:GrandTotalAmount>
        <ram:DuePayableAmount>14.814</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""

# A seller stated by name only: no SpecifiedLegalOrganization, no
# SpecifiedTaxRegistration. Legal for a foreign or a very small seller, and
# nothing in it can name a supplier.
CII_NO_SIREN = CII_TWO_RATES.replace(
    """        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
""",
    "",
    1,
).replace(
    """        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">FR25900000019</ram:ID>
        </ram:SpecifiedTaxRegistration>
""",
    "",
    1,
)

# BT-1 is mandatory, and a sender can still leave it empty.
CII_NO_NUMBER = CII_TWO_RATES.replace("<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID></ram:ID>", 1)

# The supplier's own arithmetic does not hold: the lines and the VAT come to
# 229.39 and the document claims 239.39. Never repaired - reported.
CII_TOTALS_DISAGREE = CII_TWO_RATES.replace(
    "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
    "<ram:GrandTotalAmount>239.39</ram:GrandTotalAmount>",
).replace(
    "<ram:DuePayableAmount>229.39</ram:DuePayableAmount>",
    "<ram:DuePayableAmount>239.39</ram:DuePayableAmount>",
)

# Not EUR. Refused rather than converted: a rate is a decision nobody in
# this application is entitled to take.
CII_IN_POUNDS = CII_TWO_RATES.replace(
    "<ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>",
    "<ram:InvoiceCurrencyCode>GBP</ram:InvoiceCurrencyCode>",
)

# A DOCTYPE with an internal subset: the billion-laughs shape. Refused
# before any parser sees it - ElementTree expands internal entities.
XML_WITH_DOCTYPE = """<?xml version="1.0"?>
<!DOCTYPE CrossIndustryInvoice [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100">
  <ram:ID>&b;</ram:ID>
</rsm:CrossIndustryInvoice>
"""

# An ENTITY declaration with no DOCTYPE line above it in the first bytes -
# the same refusal, looked for on its own.
XML_WITH_ENTITY = """<?xml version="1.0"?>
<!DOCTYPE x SYSTEM "http://exemple.invalid/x.dtd">
<!ENTITY fichier SYSTEM "file:///etc/passwd">
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"/>
"""

# Well-formed XML that is simply not an invoice.
XML_NOT_AN_INVOICE = """<?xml version="1.0" encoding="UTF-8"?>
<Commande xmlns="urn:exemple:commande">
  <Numero>CMD-1</Numero>
</Commande>
"""

# The root element has the right NAME and no namespace: a home-made file,
# not UBL. Told apart by the root element, which is more than its name.
XML_BARE_INVOICE_ROOT = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice><ID>1</ID></Invoice>
"""

# The same document-level charge as CII_DOCUMENT_CHARGE, in UBL: the block
# is a direct child of the root and says charge-or-allowance in
# cbc:ChargeIndicator rather than in a nested udt:Indicator.
UBL_DOCUMENT_CHARGE = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
    xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
    xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
  <cbc:ID>FA-2026-0051</cbc:ID>
  <cbc:IssueDate>2026-09-04</cbc:IssueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>Cave Exemple</cbc:RegistrationName>
        <cbc:CompanyID schemeID="0002">900000019</cbc:CompanyID>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AllowanceCharge>
    <cbc:ChargeIndicator>true</cbc:ChargeIndicator>
    <cbc:AllowanceChargeReason>Droits de circulation</cbc:AllowanceChargeReason>
    <cbc:Amount currencyID="EUR">12.00</cbc:Amount>
    <cac:TaxCategory>
      <cbc:ID>S</cbc:ID>
      <cbc:Percent>20.00</cbc:Percent>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
    </cac:TaxCategory>
  </cac:AllowanceCharge>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">22.40</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">112.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">22.40</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">100.00</cbc:LineExtensionAmount>
    <cbc:AllowanceTotalAmount currencyID="EUR">0.00</cbc:AllowanceTotalAmount>
    <cbc:ChargeTotalAmount currencyID="EUR">12.00</cbc:ChargeTotalAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">112.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">134.40</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">134.40</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="H87">10</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">100.00</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>VIN ROUGE 75CL</cbc:Name>
      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="EUR">10.00</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
</Invoice>
"""

# The same charge, stated only as BT-108 in the totals: a sender may give
# the total of its charges without listing them one by one.
CII_CHARGE_TOTAL_ONLY = CII_DOCUMENT_CHARGE.replace(
    """      <ram:SpecifiedTradeAllowanceCharge>
        <ram:ChargeIndicator><udt:Indicator>true</udt:Indicator></ram:ChargeIndicator>
        <ram:ActualAmount>12.00</ram:ActualAmount>
        <ram:Reason>Droits de circulation</ram:Reason>
        <ram:CategoryTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:CategoryTradeTax>
      </ram:SpecifiedTradeAllowanceCharge>
""",
    "",
)

# BT-111: the VAT total repeated in the currency the seller accounts for tax
# in, stated BEFORE the invoice's own. Both elements are TaxTotalAmount and
# only currencyID tells them apart.
CII_TAX_IN_TWO_CURRENCIES = CII_TWO_RATES.replace(
    '<ram:TaxTotalAmount currencyID="EUR">35.19</ram:TaxTotalAmount>',
    '<ram:TaxTotalAmount currencyID="CHF">32.60</ram:TaxTotalAmount>\n'
    '        <ram:TaxTotalAmount currencyID="EUR">35.19</ram:TaxTotalAmount>',
)

# The seller states a GLN rather than a SIREN - legal, and common from a
# foreign seller. Calling it a SIREN on the review screen would be a lie.
CII_SELLER_GLN = CII_TWO_RATES.replace(
    '<ram:ID schemeID="0002">900000019</ram:ID>',
    '<ram:ID schemeID="0088">4012345000009</ram:ID>',
    1,
).replace(
    """        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">FR25900000019</ram:ID>
        </ram:SpecifiedTaxRegistration>
""",
    "",
    1,
)

# A line item that states neither an amount nor a quantity: the document DID
# carry lines and one of them is lost, which must never read like the
# MINIMUM profile's "this invoice has no lines".
CII_LINE_WITHOUT_FIGURES = CII_TWO_RATES.replace(
    "        <ram:BilledQuantity unitCode=\"H87\">6</ram:BilledQuantity>\n", ""
).replace("          <ram:LineTotalAmount>25.20</ram:LineTotalAmount>\n", "")

# --------------------------------------------------------------------------
# A document-level charge at a rate NO line carries
# --------------------------------------------------------------------------
# The case this bar meets first. Duty on alcohol is 20 %; the soft drinks and
# the food on the same invoice are 5,5 % or 10 %. Guessing the charge's rate
# from the lines puts 100,00 € of duty at 5,5 % and files the invoice 14,50 €
# below what the bank will debit - with every check green, because every
# check works on figures the document states and those all balance.
CII_CHARGE_AT_ITS_OWN_RATE = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>FA-2026-0061</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260905</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>1</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>JUS DE POMME 1L</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>10.00</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="H87">100</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>5.50</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>1000.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Brasserie du Canal</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">900000019</ram:ID>
        </ram:SpecifiedLegalOrganization>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:SpecifiedTradeAllowanceCharge>
        <ram:ChargeIndicator><udt:Indicator>true</udt:Indicator></ram:ChargeIndicator>
        <ram:ActualAmount>100.00</ram:ActualAmount>
        <ram:Reason>Droits de circulation</ram:Reason>
        <ram:CategoryTradeTax>
          <ram:TypeCode>VAT</ram:TypeCode>
          <ram:CategoryCode>S</ram:CategoryCode>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:CategoryTradeTax>
      </ram:SpecifiedTradeAllowanceCharge>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>55.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>1000.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>5.50</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>20.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>100.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>1000.00</ram:LineTotalAmount>
        <ram:ChargeTotalAmount>100.00</ram:ChargeTotalAmount>
        <ram:AllowanceTotalAmount>0.00</ram:AllowanceTotalAmount>
        <ram:TaxBasisTotalAmount>1100.00</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">75.00</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>1175.00</ram:GrandTotalAmount>
        <ram:DuePayableAmount>1175.00</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""

# The same invoice the other way: a year-end discount at 20 % taken off lines
# charged at 5,5 %. Guessed, it files the invoice ABOVE what is due.
CII_ALLOWANCE_AT_ITS_OWN_RATE = CII_CHARGE_AT_ITS_OWN_RATE.replace(
    "<ram:ID>FA-2026-0061</ram:ID>", "<ram:ID>FA-2026-0062</ram:ID>"
).replace(
    "<udt:Indicator>true</udt:Indicator>", "<udt:Indicator>false</udt:Indicator>"
).replace(
    "<ram:Reason>Droits de circulation</ram:Reason>", "<ram:Reason>Remise de fin d'annee</ram:Reason>"
).replace(
    """      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>20.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>100.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
""",
    """      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>-20.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>-100.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
""",
).replace(
    "<ram:ChargeTotalAmount>100.00</ram:ChargeTotalAmount>", "<ram:ChargeTotalAmount>0.00</ram:ChargeTotalAmount>"
).replace(
    "<ram:AllowanceTotalAmount>0.00</ram:AllowanceTotalAmount>",
    "<ram:AllowanceTotalAmount>100.00</ram:AllowanceTotalAmount>",
).replace(
    "<ram:TaxBasisTotalAmount>1100.00</ram:TaxBasisTotalAmount>",
    "<ram:TaxBasisTotalAmount>900.00</ram:TaxBasisTotalAmount>",
).replace(
    '<ram:TaxTotalAmount currencyID="EUR">75.00</ram:TaxTotalAmount>',
    '<ram:TaxTotalAmount currencyID="EUR">35.00</ram:TaxTotalAmount>',
).replace(
    "<ram:GrandTotalAmount>1175.00</ram:GrandTotalAmount>",
    "<ram:GrandTotalAmount>935.00</ram:GrandTotalAmount>",
).replace(
    "<ram:DuePayableAmount>1175.00</ram:DuePayableAmount>",
    "<ram:DuePayableAmount>935.00</ram:DuePayableAmount>",
)

# The same trap in UBL: cac:AllowanceCharge/cac:TaxCategory/cbc:Percent is
# where the charge's own rate is stated there.
UBL_CHARGE_AT_ITS_OWN_RATE = UBL_DOCUMENT_CHARGE.replace(
    "<cbc:ID>FA-2026-0051</cbc:ID>", "<cbc:ID>FA-2026-0063</cbc:ID>"
).replace(
    """      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>""",
    """      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>5.50</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>""",
).replace(
    """  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">22.40</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">112.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">22.40</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>""",
    """  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">7.90</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">100.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">5.50</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>5.50</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">12.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">2.40</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>20.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>""",
).replace(
    '<cbc:TaxInclusiveAmount currencyID="EUR">134.40</cbc:TaxInclusiveAmount>',
    '<cbc:TaxInclusiveAmount currencyID="EUR">119.90</cbc:TaxInclusiveAmount>',
).replace(
    '<cbc:PayableAmount currencyID="EUR">134.40</cbc:PayableAmount>',
    '<cbc:PayableAmount currencyID="EUR">119.90</cbc:PayableAmount>',
)

# --------------------------------------------------------------------------
# BT-114: the sender rounds its own total
# --------------------------------------------------------------------------
# EN 16931 says BT-112 = BT-109 + BT-110 + BT-114. Ignored, BT-114 turns an
# invoice that balances into an accusation against a supplier who did
# nothing wrong - and there is nothing anybody can do about it.
CII_ROUNDING = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0064</ram:ID>"
).replace(
    "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
    "<ram:RoundingAmount>0.01</ram:RoundingAmount>\n"
    "        <ram:GrandTotalAmount>229.40</ram:GrandTotalAmount>",
).replace(
    "<ram:DuePayableAmount>229.39</ram:DuePayableAmount>",
    "<ram:DuePayableAmount>229.40</ram:DuePayableAmount>",
)

UBL_ROUNDING = UBL_TWO_RATES.replace(
    '<cbc:TaxInclusiveAmount currencyID="EUR">229.39</cbc:TaxInclusiveAmount>',
    '<cbc:PayableRoundingAmount currencyID="EUR">0.01</cbc:PayableRoundingAmount>\n'
    '    <cbc:TaxInclusiveAmount currencyID="EUR">229.40</cbc:TaxInclusiveAmount>',
).replace(
    '<cbc:PayableAmount currencyID="EUR">229.39</cbc:PayableAmount>',
    '<cbc:PayableAmount currencyID="EUR">229.40</cbc:PayableAmount>',
)

# BT-113 / BT-115: part of the invoice was paid in advance, so the bank will
# only ever show what is left. The purchase is still the whole invoice.
CII_PREPAID = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0065</ram:ID>"
).replace(
    "<ram:DuePayableAmount>229.39</ram:DuePayableAmount>",
    "<ram:TotalPrepaidAmount>100.00</ram:TotalPrepaidAmount>\n"
    "        <ram:DuePayableAmount>129.39</ram:DuePayableAmount>",
)

# --------------------------------------------------------------------------
# Lines whose sign is not this codebase's
# --------------------------------------------------------------------------
# An ORDINARY invoice (380) carrying a discount as a line: a count of 1 at a
# negative amount. Nothing signs it - the document is not a credit note - and
# booked as it stands it puts a unit of stock on the shelf at MINUS 60 €.
CII_DISCOUNT_LINE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0066</ram:ID>"
).replace(
    "<ram:Name>SIROP CITRON 1L</ram:Name>", "<ram:Name>REMISE COMMERCIALE</ram:Name>"
).replace(
    "<ram:ChargeAmount>4.20</ram:ChargeAmount>", "<ram:ChargeAmount>60.00</ram:ChargeAmount>"
).replace(
    '<ram:BilledQuantity unitCode="H87">6</ram:BilledQuantity>',
    '<ram:BilledQuantity unitCode="H87">1</ram:BilledQuantity>',
).replace(
    # Only the LINE's rate: the header's 5,5 % row goes away entirely below,
    # and replacing both here would leave nothing for that to match.
    "<ram:RateApplicablePercent>5.50</ram:RateApplicablePercent>",
    "<ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>", 1
).replace(
    "<ram:LineTotalAmount>25.20</ram:LineTotalAmount>", "<ram:LineTotalAmount>-60.00</ram:LineTotalAmount>", 1
).replace(
    """      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>1.39</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>25.20</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>5.50</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
""",
    "",
).replace(
    "<ram:BasisAmount>169.00</ram:BasisAmount>", "<ram:BasisAmount>109.00</ram:BasisAmount>"
).replace(
    "<ram:CalculatedAmount>33.80</ram:CalculatedAmount>", "<ram:CalculatedAmount>21.80</ram:CalculatedAmount>"
).replace(
    "<ram:LineTotalAmount>194.20</ram:LineTotalAmount>", "<ram:LineTotalAmount>109.00</ram:LineTotalAmount>"
).replace(
    "<ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>",
    "<ram:TaxBasisTotalAmount>109.00</ram:TaxBasisTotalAmount>",
).replace(
    '<ram:TaxTotalAmount currencyID="EUR">35.19</ram:TaxTotalAmount>',
    '<ram:TaxTotalAmount currencyID="EUR">21.80</ram:TaxTotalAmount>',
).replace(
    "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
    "<ram:GrandTotalAmount>130.80</ram:GrandTotalAmount>",
).replace(
    "<ram:DuePayableAmount>229.39</ram:DuePayableAmount>",
    "<ram:DuePayableAmount>130.80</ram:DuePayableAmount>",
)

# The mirror shape, and the one nothing can repair: a NEGATIVE count at a
# POSITIVE amount on a 380. Neither a purchase nor a return, and the header
# totals agree with it, so every check that works on the totals passes.
CII_NEGATIVE_QUANTITY = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0067</ram:ID>"
).replace(
    '<ram:BilledQuantity unitCode="H87">2</ram:BilledQuantity>',
    '<ram:BilledQuantity unitCode="H87">-2</ram:BilledQuantity>',
)

# --------------------------------------------------------------------------
# Figures wider than the columns that would store them
# --------------------------------------------------------------------------
# Each of these is ONE stated number, in an otherwise ordinary invoice that
# balances and passes every check. Written to the database they are read
# back by Django's SQLite decimal converter as decimal.InvalidOperation, and
# from then on the document cannot be opened, corrected, re-read OR deleted
# through the application - only raw SQL gets it out.
CII_AMOUNT_TOO_WIDE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0071</ram:ID>"
).replace(
    "<ram:LineTotalAmount>169.00</ram:LineTotalAmount>",
    "<ram:LineTotalAmount>10000000000.00</ram:LineTotalAmount>", 1
).replace(
    "<ram:BasisAmount>169.00</ram:BasisAmount>", "<ram:BasisAmount>10000000000.00</ram:BasisAmount>"
).replace(
    "<ram:CalculatedAmount>33.80</ram:CalculatedAmount>",
    "<ram:CalculatedAmount>2000000000.00</ram:CalculatedAmount>",
).replace(
    "<ram:LineTotalAmount>194.20</ram:LineTotalAmount>",
    "<ram:LineTotalAmount>10000000025.20</ram:LineTotalAmount>",
).replace(
    "<ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>",
    "<ram:TaxBasisTotalAmount>10000000025.20</ram:TaxBasisTotalAmount>",
).replace(
    '<ram:TaxTotalAmount currencyID="EUR">35.19</ram:TaxTotalAmount>',
    '<ram:TaxTotalAmount currencyID="EUR">2000000001.39</ram:TaxTotalAmount>',
).replace(
    "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
    "<ram:GrandTotalAmount>12000000026.59</ram:GrandTotalAmount>",
).replace(
    "<ram:DuePayableAmount>229.39</ram:DuePayableAmount>",
    "<ram:DuePayableAmount>12000000026.59</ram:DuePayableAmount>",
)

# InvoiceLine.vat_rate holds five digits, four of them decimals: 1000 % needs
# six.
CII_RATE_TOO_WIDE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0072</ram:ID>"
).replace("<ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>",
          "<ram:RateApplicablePercent>1000.00</ram:RateApplicablePercent>")

# InvoiceLine.unit_cost_ht holds ten digits, four of them decimals.
CII_UNIT_PRICE_TOO_WIDE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0073</ram:ID>"
).replace("<ram:ChargeAmount>84.50</ram:ChargeAmount>",
          "<ram:ChargeAmount>1000000.00</ram:ChargeAmount>")

# InvoiceLine.quantity holds twelve digits, three of them decimals.
CII_QUANTITY_TOO_WIDE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0074</ram:ID>"
).replace('<ram:BilledQuantity unitCode="H87">2</ram:BilledQuantity>',
          '<ram:BilledQuantity unitCode="H87">1000000000</ram:BilledQuantity>')

# Invoice.reconciliation_adjustment holds ten digits, two of them decimals.
CII_ADJUSTMENT_TOO_WIDE = CII_DOCUMENT_CHARGE.replace(
    "<ram:ID>FA-2026-0051</ram:ID>", "<ram:ID>FA-2026-0075</ram:ID>"
).replace("<ram:ActualAmount>12.00</ram:ActualAmount>",
          "<ram:ActualAmount>100000000.00</ram:ActualAmount>")

# An exponent rather than a width: decimal.InvalidOperation and
# decimal.Overflow are ArithmeticErrors, not ValueErrors, so they escaped
# every handler the import has and reached the owner as a traceback.
CII_EXPONENT_LINE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0076</ram:ID>"
).replace("<ram:LineTotalAmount>169.00</ram:LineTotalAmount>",
          "<ram:LineTotalAmount>1E+500</ram:LineTotalAmount>", 1)

CII_EXPONENT_TOTAL = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0077</ram:ID>"
).replace("<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
          "<ram:GrandTotalAmount>1E+999999999</ram:GrandTotalAmount>")

# A product name longer than the column that stores it (255). Past about
# 50 000 characters SQLite's own LIKE limit turns the import into an English
# OperationalError; below it the name is simply stored whole, on the line AND
# on the Product it creates.
CII_ENDLESS_NAME = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0078</ram:ID>"
).replace("<ram:Name>BIERE BLONDE FUT 30L</ram:Name>",
          "<ram:Name>" + "BIERE " * 12000 + "</ram:Name>")

# --------------------------------------------------------------------------
# Dates no invoice was ever issued on
# --------------------------------------------------------------------------
# In no window, no valuation, no bank match and no margin - and, before this
# was said on the document, in no queue either: error_message was empty, so
# nothing anywhere pointed at it.
CII_DATE_YEAR_ONE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0081</ram:ID>"
).replace('<udt:DateTimeString format="102">20260903</udt:DateTimeString>',
          '<udt:DateTimeString format="102">00010101</udt:DateTimeString>')

CII_DATE_FAR_FUTURE = CII_TWO_RATES.replace(
    "<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>FA-2026-0082</ram:ID>"
).replace('<udt:DateTimeString format="102">20260903</udt:DateTimeString>',
          '<udt:DateTimeString format="102">99991231</udt:DateTimeString>')

# --------------------------------------------------------------------------
# A billion laughs that walks past a byte grep
# --------------------------------------------------------------------------
# The DOCTYPE guard looks for the bytes b"<!DOCTYPE". In UTF-16 they are
# b"<\x00!\x00D\x00..." and the grep misses, while expat reads the BOM,
# decodes happily and expands every entity. What saved the machine was
# libexpat's own amplification limit, not this code.
XML_UTF16_DOCTYPE = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE rsm:CrossIndustryInvoice [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
  <!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
]>
""" + CII_TWO_RATES.split("?>", 1)[1].replace(
    "<ram:Name>BIERE BLONDE FUT 30L</ram:Name>", "<ram:Name>&e;</ram:Name>"
)
