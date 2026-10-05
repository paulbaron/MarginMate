"""Reading the bar's own electronic invoice as a sale (recipes/sale_einvoice.py).

Pure: bytes in, a `SaleReading` out - no database, no request. The figures
are the invoice's own data, so these tests assert equality, never tolerance:
a figure read that is not exactly what the XML states is a bug. What a sale
column cannot hold is refused, in French, never cut (CLAUDE.md « A figure
wider than the column behind it is refused, at the door »); a name is cut.

Fixtures in `sale_einvoice_files.py`: the standard's structure, every name,
SIREN and amount invented.
"""

import dataclasses
import os
import tempfile
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices import einvoice
from invoices.tests.einvoice_files import XML_NOT_AN_INVOICE, XML_WITH_ENTITY
from recipes import sale_einvoice
from recipes.sale_einvoice import MAX_SALE_LINES, SaleLineReading, read_sale
from recipes.tests.sale_einvoice_files import (
    BAR_NAME,
    BAR_SIREN,
    CUSTOMER_NAME,
    CUSTOMER_SIREN,
    SALE_CII,
    SALE_CII_ALLOWANCE,
    SALE_CII_BUYER_IS_THE_BAR,
    SALE_CII_CHARGE,
    SALE_CII_CREDIT_NOTE,
    SALE_CII_DELIVERED,
    SALE_CII_DEPOSIT,
    SALE_CII_DEPOSIT_AS_380,
    SALE_CII_DOCTYPE,
    SALE_CII_MINIMUM,
    SALE_CII_MIRROR_LINE,
    SALE_CII_NO_DATE,
    SALE_CII_NUMBER_TOO_LONG,
    SALE_CII_PERIOD,
    SALE_CII_PREPAID,
    SALE_CII_QUANTITY_TOO_WIDE,
    SALE_CII_RATE_OVER_100,
    SALE_CII_SELLER_IS_A_SUPPLIER,
    SALE_CII_UTF16,
    SALE_CII_YEAR_ONE,
    SALE_NUMBER,
    SALE_UBL,
    SUPPLIER_SIREN,
    factur_x,
    sale_cii_with_lines,
)

D = Decimal


def read(xml) -> sale_einvoice.SaleReading:
    return read_sale(xml.encode("utf-8") if isinstance(xml, str) else xml)


class ReadSaleTests(SimpleTestCase):
    """Every field of the reading, from a CII and a UBL invoice of one sale."""

    def test_every_field_of_a_cii_sale(self):
        reading = read(SALE_CII)
        self.assertEqual(reading.syntax, "CII")
        self.assertEqual(reading.profile, "urn:cen.eu:en16931:2017")
        self.assertEqual(reading.number, SALE_NUMBER)
        self.assertEqual(reading.issued, date(2026, 9, 3))
        self.assertIsNone(reading.delivered)
        self.assertEqual(reading.type_code, "380")
        self.assertFalse(reading.is_credit_note)
        self.assertFalse(reading.is_deposit)
        self.assertEqual(reading.preceding_number, "")
        self.assertFalse(reading.mentions_deposit)
        self.assertEqual(reading.seller_name, BAR_NAME)
        self.assertEqual(reading.seller_siren, BAR_SIREN)
        self.assertEqual(reading.customer, CUSTOMER_NAME)
        self.assertEqual(reading.customer_identifier, CUSTOMER_SIREN)
        self.assertEqual(reading.customer_siren, CUSTOMER_SIREN)
        self.assertEqual(reading.total_ttc, D("230.52"))
        self.assertEqual(reading.total_ht, D("194.20"))
        self.assertIsNone(reading.prepaid)
        self.assertEqual(reading.payable, D("230.52"))
        self.assertEqual(reading.adjustment_ht, D("0"))
        self.assertIsNone(reading.adjustment_vat_rate)
        self.assertFalse(reading.carries_no_lines)
        self.assertEqual(
            reading.lines,
            (
                SaleLineReading(
                    label="Formule cocktail",
                    quantity=D("2"),
                    unit_price_ht=D("84.5000"),
                    total_ht=D("169.00"),
                    vat_rate=D("0.2000"),
                ),
                SaleLineReading(
                    label="Planche apéritive",
                    quantity=D("6"),
                    unit_price_ht=D("4.2000"),
                    total_ht=D("25.20"),
                    vat_rate=D("0.1000"),
                ),
            ),
        )
        self.assertTrue(all(not line.rebuilt for line in reading.lines))

    def test_the_figures_are_decimals_with_the_columns_places(self):
        reading = read(SALE_CII)
        for value in (reading.total_ttc, reading.total_ht, reading.payable, reading.adjustment_ht):
            self.assertIsInstance(value, Decimal)
            self.assertEqual(value.as_tuple().exponent, -2)
        for line in reading.lines:
            self.assertIsInstance(line.quantity, Decimal)

    def test_ubl_reads_as_the_same_sale(self):
        cii, ubl = read(SALE_CII), read(SALE_UBL)
        self.assertEqual(ubl.syntax, "UBL")
        self.assertEqual(dataclasses.replace(cii, syntax="UBL"), ubl)

    def test_the_checks_are_the_invoices_own_as_they_are(self):
        reading = read(SALE_CII)
        parsed = einvoice.read(SALE_CII.encode("utf-8"))
        self.assertEqual(
            reading.checks,
            tuple({"label": check.label, "passed": check.passed, "detail": check.detail} for check in parsed.checks),
        )
        self.assertTrue(reading.checks)
        self.assertTrue(all(check["passed"] for check in reading.checks))

    def test_a_failing_check_is_kept_and_the_mirror_line_read_as_stated(self):
        """-1 × 60,00 € on a 380: neither a sale nor a refund. The reader
        repairs nothing: the line as stated, the invoice's own check failing."""
        reading = read(SALE_CII_MIRROR_LINE)
        self.assertEqual(reading.lines[1].quantity, D("-1"))
        self.assertEqual(reading.lines[1].total_ht, D("60.00"))
        failed = [check for check in reading.checks if not check["passed"]]
        self.assertEqual([check["label"] for check in failed], [einvoice.LINE_SIGN_CHECK])
        self.assertIn("60", failed[0]["detail"])

    def test_a_credit_note_is_signed_its_unit_prices_positive(self):
        reading = read(SALE_CII_CREDIT_NOTE)
        self.assertTrue(reading.is_credit_note)
        self.assertEqual(reading.type_code, "381")
        self.assertEqual(reading.preceding_number, SALE_NUMBER)
        self.assertEqual([line.quantity for line in reading.lines], [D("-2"), D("-6")])
        self.assertEqual([line.total_ht for line in reading.lines], [D("-169.00"), D("-25.20")])
        self.assertEqual([line.unit_price_ht for line in reading.lines], [D("84.5000"), D("4.2000")])
        self.assertEqual(reading.total_ttc, D("-230.52"))
        self.assertEqual(reading.total_ht, D("-194.20"))
        self.assertEqual(reading.payable, D("-230.52"))

    def test_no_minus_zero_is_read(self):
        """A credit note with no allowance or charge: 0 × -1 is « -0.00 »,
        which a page would print as such."""
        reading = read(SALE_CII_CREDIT_NOTE)
        self.assertEqual(str(reading.adjustment_ht), "0.00")

    def test_a_credit_notes_zero_line_is_no_minus_zero(self):
        """A line of 0,00 € on a credit note - its own, or one rebuilt from a
        VAT row of 0,00 € - is signed like the rest: « -0.00 » on a page."""
        zero_line = SALE_CII_CREDIT_NOTE.replace(
            "<ram:ChargeAmount>4.20</ram:ChargeAmount>", "<ram:ChargeAmount>0.00</ram:ChargeAmount>"
        ).replace("<ram:LineTotalAmount>25.20</ram:LineTotalAmount>", "<ram:LineTotalAmount>0.00</ram:LineTotalAmount>")
        self.assertEqual(str(read(zero_line).lines[1].total_ht), "0.00")
        zero_row = SALE_CII_MINIMUM.replace(
            "<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>381</ram:TypeCode>"
        ).replace("<ram:BasisAmount>25.20</ram:BasisAmount>", "<ram:BasisAmount>0.00</ram:BasisAmount>")
        rebuilt = read(zero_row).lines[1]
        self.assertEqual(str(rebuilt.total_ht), "0.00")
        self.assertEqual(rebuilt.quantity, D("1"))

    def test_a_386_is_a_deposit_invoice(self):
        reading = read(SALE_CII_DEPOSIT)
        self.assertTrue(reading.is_deposit)
        self.assertEqual(reading.type_code, "386")
        self.assertFalse(reading.mentions_deposit)

    def test_a_380_whose_line_says_acompte_mentions_it(self):
        reading = read(SALE_CII_DEPOSIT_AS_380)
        self.assertFalse(reading.is_deposit)
        self.assertTrue(reading.mentions_deposit)

    def test_acompte_in_the_number_is_mentioned_too_accents_and_case_aside(self):
        reading = read(SALE_CII.replace(f"<ram:ID>{SALE_NUMBER}</ram:ID>", "<ram:ID>ACOMPTE-2026-04</ram:ID>", 1))
        self.assertTrue(reading.mentions_deposit)

    def test_a_386_saying_acompte_is_a_deposit_not_a_mention(self):
        reading = read(
            SALE_CII_DEPOSIT_AS_380.replace("<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>386</ram:TypeCode>")
        )
        self.assertTrue(reading.is_deposit)
        self.assertFalse(reading.mentions_deposit)

    def test_delivered_is_bt_72(self):
        reading = read(SALE_CII_DELIVERED)
        self.assertEqual(reading.delivered, date(2026, 8, 31))
        self.assertEqual(reading.issued, date(2026, 9, 3))

    def test_else_the_start_of_the_billing_period(self):
        reading = read(SALE_CII_PERIOD)
        self.assertEqual(reading.delivered, date(2026, 8, 15))

    def test_bt_72_wins_over_the_billing_period(self):
        both = SALE_CII_DELIVERED.replace(
            "      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>",
            "      <ram:BillingSpecifiedPeriod><ram:StartDateTime>"
            '<udt:DateTimeString format="102">20260801</udt:DateTimeString>'
            "</ram:StartDateTime></ram:BillingSpecifiedPeriod>\n"
            "      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>",
        )
        self.assertEqual(read(both).delivered, date(2026, 8, 31))

    def test_no_date_at_all_is_none_never_a_refusal(self):
        reading = read(SALE_CII_NO_DATE)
        self.assertIsNone(reading.issued)
        self.assertIsNone(reading.delivered)

    def test_a_minimum_invoice_gives_one_rebuilt_free_line_per_rate(self):
        reading = read(SALE_CII_MINIMUM)
        self.assertTrue(reading.carries_no_lines)
        self.assertEqual(
            reading.lines,
            (
                SaleLineReading(
                    label="Total au taux de 20,00 % (facture sans lignes)",
                    quantity=D("1"),
                    unit_price_ht=D("169.00"),
                    total_ht=D("169.00"),
                    vat_rate=D("0.2000"),
                    rebuilt=True,
                ),
                SaleLineReading(
                    label="Total au taux de 10,00 % (facture sans lignes)",
                    quantity=D("1"),
                    unit_price_ht=D("25.20"),
                    total_ht=D("25.20"),
                    vat_rate=D("0.1000"),
                    rebuilt=True,
                ),
            ),
        )
        self.assertEqual(reading.total_ttc, D("230.52"))

    def test_a_minimum_credit_note_rebuilds_negative_lines(self):
        reading = read(SALE_CII_MINIMUM.replace("<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>381</ram:TypeCode>"))
        self.assertEqual([line.quantity for line in reading.lines], [D("-1"), D("-1")])
        self.assertEqual([line.total_ht for line in reading.lines], [D("-169.00"), D("-25.20")])
        self.assertEqual([line.unit_price_ht for line in reading.lines], [D("169.00"), D("25.20")])
        self.assertTrue(all(line.rebuilt for line in reading.lines))

    def test_prepaid_and_payable(self):
        reading = read(SALE_CII_PREPAID)
        self.assertEqual(reading.prepaid, D("100.00"))
        self.assertEqual(reading.payable, D("130.52"))
        self.assertEqual(reading.total_ttc, D("230.52"))

    def test_a_stated_figure_is_rounded_half_up_to_the_cent(self):
        reading = read(
            SALE_CII_PREPAID.replace(
                "<ram:TotalPrepaidAmount>100.00</ram:TotalPrepaidAmount>",
                "<ram:TotalPrepaidAmount>100.005</ram:TotalPrepaidAmount>",
            )
        )
        self.assertEqual(reading.prepaid, D("100.01"))

    def test_a_charge_is_the_adjustment_with_its_rate(self):
        reading = read(SALE_CII_CHARGE)
        self.assertEqual(reading.adjustment_ht, D("12.00"))
        self.assertEqual(reading.adjustment_vat_rate, D("0.2000"))
        self.assertEqual(reading.total_ht, D("206.20"))
        self.assertEqual(reading.total_ttc, D("244.92"))

    def test_an_allowance_is_the_adjustment_negative_with_its_rate(self):
        reading = read(SALE_CII_ALLOWANCE)
        self.assertEqual(reading.adjustment_ht, D("-10.00"))
        self.assertEqual(reading.adjustment_vat_rate, D("0.2000"))
        self.assertEqual(reading.total_ht, D("184.20"))

    def test_a_name_is_cut_never_refused(self):
        long_name = "Exemple " * 60
        reading = read(SALE_CII.replace(f"<ram:Name>{CUSTOMER_NAME}</ram:Name>", f"<ram:Name>{long_name}</ram:Name>"))
        self.assertEqual(len(reading.customer), einvoice.MAX_NAME)
        self.assertTrue(reading.customer.endswith("…"))

    def test_the_customer_identifier_is_bt_48_without_bt_47(self):
        reading = read(
            SALE_CII.replace(
                "        <ram:SpecifiedLegalOrganization>\n"
                f'          <ram:ID schemeID="0002">{CUSTOMER_SIREN}</ram:ID>\n'
                "        </ram:SpecifiedLegalOrganization>\n",
                "",
            )
        )
        self.assertEqual(reading.customer_identifier, "FR73700000003")
        self.assertEqual(reading.customer_siren, CUSTOMER_SIREN)

    def test_the_customer_identifier_and_bt_25_are_cut_to_their_columns(self):
        reading = read(
            SALE_CII_CREDIT_NOTE.replace(
                f"<ram:IssuerAssignedID>{SALE_NUMBER}</ram:IssuerAssignedID>",
                f"<ram:IssuerAssignedID>{'7' * 150}</ram:IssuerAssignedID>",
            ).replace(
                f'<ram:ID schemeID="0002">{CUSTOMER_SIREN}</ram:ID>',
                f'<ram:ID schemeID="0088">{"3" * 60}</ram:ID>',
            )
        )
        self.assertEqual(reading.preceding_number, "7" * 100)
        self.assertEqual(reading.customer_identifier, "3" * 40)

    def test_500_lines_are_read(self):
        reading = read(sale_cii_with_lines(MAX_SALE_LINES))
        self.assertEqual(len(reading.lines), MAX_SALE_LINES)
        self.assertEqual(reading.total_ttc, D("600.00"))


class RefusalTests(SimpleTestCase):
    """A figure no sale column holds, too many lines, and everything einvoice
    refuses - each an `EInvoiceError` (a ValueError) with a French sentence,
    nothing cut."""

    def assertRefused(self, xml, *words) -> str:
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            read(xml)
        message = str(caught.exception)
        for word in words:
            self.assertIn(word, message)
        return message

    def test_a_quantity_wider_than_a_sale_line(self):
        message = self.assertRefused(SALE_CII_QUANTITY_TOO_WIDE, "une quantité", "(1234567.5)")
        self.assertIn("n'est pas ajoutée", message)

    def test_a_rate_over_100_percent(self):
        self.assertRefused(SALE_CII_RATE_OVER_100, "un taux de TVA de plus de 100 %", "150,00 %")

    def test_a_negative_rate(self):
        """einvoice reads a rate down to -999,99 % (its column's own bound);
        a sale line's validator starts at 0 - stored, its TTC would be read
        below its HT."""
        negative = SALE_CII.replace(
            "<ram:RateApplicablePercent>10.00</ram:RateApplicablePercent>",
            "<ram:RateApplicablePercent>-5.00</ram:RateApplicablePercent>",
            1,
        )
        self.assertNotEqual(negative, SALE_CII)
        self.assertRefused(negative, "un taux de TVA négatif", "-5,00 %")

    def test_a_number_longer_than_100_characters(self):
        self.assertRefused(SALE_CII_NUMBER_TOO_LONG, "un numéro de plus de 100 caractères", "(FV-" + "9" * 17 + "…)")

    def test_100_characters_are_a_number(self):
        number = "FV-" + "9" * 97
        self.assertEqual(read(SALE_CII.replace(SALE_NUMBER, number, 1)).number, number)

    def test_a_prepaid_wider_than_its_column(self):
        self.assertRefused(
            SALE_CII_PREPAID.replace(
                "<ram:TotalPrepaidAmount>100.00</ram:TotalPrepaidAmount>",
                "<ram:TotalPrepaidAmount>12345678901234.00</ram:TotalPrepaidAmount>",
            ),
            "un acompte",
            "12\N{NO-BREAK SPACE}345\N{NO-BREAK SPACE}678\N{NO-BREAK SPACE}901\N{NO-BREAK SPACE}234.00",
        )

    def test_a_stated_total_ht_wider_than_its_column(self):
        self.assertRefused(
            SALE_CII.replace(
                "<ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>",
                "<ram:TaxBasisTotalAmount>12345678901234.00</ram:TaxBasisTotalAmount>",
            ),
            "le total HT",
            "(12\N{NO-BREAK SPACE}345\N{NO-BREAK SPACE}678\N{NO-BREAK SPACE}901\N{NO-BREAK SPACE}234.00)",
        )

    def test_a_rebuilt_lines_unit_price_wider_than_a_sale_line(self):
        """A MINIMUM invoice's VAT row fits a line's amount (12,2) and not its
        unit price (10,4), which a rebuilt line repeats."""
        self.assertRefused(
            SALE_CII_MINIMUM.replace(
                "<ram:BasisAmount>169.00</ram:BasisAmount>", "<ram:BasisAmount>1234567.00</ram:BasisAmount>"
            ),
            "un prix unitaire",
            "(1\N{NO-BREAK SPACE}234\N{NO-BREAK SPACE}567.00)",
        )

    def test_a_payable_einvoice_never_reads_is_refused_here(self):
        """Achats never reads BT-115 without BT-113, so einvoice lets an
        absurd one through; a sale stores it, and refuses it."""
        self.assertRefused(
            SALE_CII.replace(
                "<ram:DuePayableAmount>230.52</ram:DuePayableAmount>",
                "<ram:DuePayableAmount>1E+999999999</ram:DuePayableAmount>",
            ),
            "le reste à payer",
        )

    def test_a_figure_rounding_past_its_column(self):
        self.assertRefused(
            SALE_CII.replace(
                "<ram:DuePayableAmount>230.52</ram:DuePayableAmount>",
                "<ram:DuePayableAmount>9999999999.995</ram:DuePayableAmount>",
            ),
            "le reste à payer",
        )

    def test_an_adjustment_rate_over_100_percent(self):
        self.assertRefused(
            SALE_CII_CHARGE.replace(
                "<ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>\n        </ram:CategoryTradeTax>",
                "<ram:RateApplicablePercent>120.00</ram:RateApplicablePercent>\n        </ram:CategoryTradeTax>",
            ),
            "un taux de TVA de plus de 100 %",
            "120,00 %",
        )

    def test_501_lines_are_too_many(self):
        message = self.assertRefused(sale_cii_with_lines(MAX_SALE_LINES + 1))
        self.assertEqual(message, sale_einvoice.TOO_MANY_LINES.format(count=MAX_SALE_LINES + 1, limit=MAX_SALE_LINES))

    def assertRefusedAsEinvoiceDoes(self, data: bytes):
        with self.assertRaises(einvoice.EInvoiceError) as einvoice_said:
            einvoice.read(data)
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            read_sale(data)
        self.assertEqual(str(caught.exception), str(einvoice_said.exception))

    def test_every_refusal_of_einvoice_is_said_as_it_is(self):
        for data in (
            SALE_CII_DOCTYPE.encode("utf-8"),
            XML_WITH_ENTITY.encode("utf-8"),
            SALE_CII_UTF16,
            SALE_CII.replace(">EUR<", ">GBP<").encode("utf-8"),
            XML_NOT_AN_INVOICE.encode("utf-8"),
            b"",
            b"%PDF-1.4 not an xml",
        ):
            with self.subTest(data=data[:40]):
                self.assertRefusedAsEinvoiceDoes(data)

    def test_it_is_a_value_error(self):
        with self.assertRaises(ValueError):
            read(SALE_CII_RATE_OVER_100)


class FixtureTests(SimpleTestCase):
    """The fixtures later slices read are what their names say: a `.replace`
    that matched nothing would leave a copy of SALE_CII testing nothing."""

    def test_each_fixture_is_its_own_invoice(self):
        numbers = [
            read(fixture).number
            for fixture in (
                SALE_CII,
                SALE_CII_CREDIT_NOTE,
                SALE_CII_DEPOSIT,
                SALE_CII_DEPOSIT_AS_380,
                SALE_CII_MINIMUM,
                SALE_CII_PREPAID,
                SALE_CII_CHARGE,
                SALE_CII_ALLOWANCE,
                SALE_CII_DELIVERED,
                SALE_CII_PERIOD,
                SALE_CII_NO_DATE,
                SALE_CII_YEAR_ONE,
                SALE_CII_MIRROR_LINE,
                SALE_CII_SELLER_IS_A_SUPPLIER,
                SALE_CII_BUYER_IS_THE_BAR,
            )
        ]
        self.assertEqual(len(set(numbers)), len(numbers))

    def test_year_one(self):
        self.assertEqual(read(SALE_CII_YEAR_ONE).issued, date(1, 1, 1))

    def test_the_seller_is_a_supplier_the_buyer_a_customer(self):
        reading = read(SALE_CII_SELLER_IS_A_SUPPLIER)
        self.assertEqual(reading.seller_siren, SUPPLIER_SIREN)
        self.assertEqual(reading.customer_siren, CUSTOMER_SIREN)

    def test_the_buyer_is_the_bar(self):
        reading = read(SALE_CII_BUYER_IS_THE_BAR)
        self.assertEqual(reading.seller_siren, SUPPLIER_SIREN)
        self.assertEqual(reading.customer, BAR_NAME)
        self.assertEqual(reading.customer_siren, BAR_SIREN)

    def test_every_checked_figure_adds_up_but_the_mirror_line(self):
        for fixture in (SALE_UBL, SALE_CII_PREPAID, SALE_CII_CHARGE, SALE_CII_ALLOWANCE, sale_cii_with_lines(3)):
            with self.subTest(fixture=read(fixture).number):
                self.assertTrue(all(check["passed"] for check in read(fixture).checks))

    def test_a_factur_x_carries_its_xml(self):
        with tempfile.TemporaryDirectory() as folder:
            path = factur_x(os.path.join(folder, "facture.pdf"), SALE_CII)
            self.assertEqual(read_sale(einvoice.document_xml(path)).number, SALE_NUMBER)
