"""Monoprix: the euro sign in the shapes the recogniser gives it, a promotion
printed under its item, and a misread count.

Layouts copied from real recognitions of the corpus; every name and amount is
invented.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.monoprix import MonoprixParser

HEADER = "MONOPRIX\nDimax\n12 RUE INVENTEE,\n75000 PARIS\n0100000000\nBONJOUR\n"
FOOTER = "12/03/2026 15:36 245 77 1598 770\n024507701598260311153600\n"


def parse(items, total, vat_row, pre_discount_line=None):
    text = (
        HEADER
        + items
        + (pre_discount_line or f"TOTAL HORS AVANTAGES  {total}")
        + f"\nNOMBRE D'ARTICLES  3\nRESTE A PAYER  {total}\nPAIEMENT\nCB EMV  {total}\nRendu (ESPECES)  0,00\n"
        + f"TVA  H.T.  T.V.A.  T.T.C\n{vat_row}\n"
        + FOOTER
    )
    return MonoprixParser().parse_pages([PdfPage(text=text)])


def failed(invoice):
    return {check.label for check in invoice.checks if not check.passed}


class CurrencySignTests(SimpleTestCase):
    def test_a_euro_sign_read_as_a_digit_is_not_a_discount(self):
        """"8,44€" as "8,448" once made a 0,008 EUR promotion out of nothing."""
        invoice = parse(
            "TOMATE INVENTEE  0,79\nCONCOMBRE  1,33\n",
            "2,12",
            "5,5%  2,01  0,11  2,12",
            pre_discount_line="TOTAL HORS AVANTAGES  2,128",
        )
        self.assertNotIn("Remise attribuée", {check.label for check in invoice.checks})
        self.assertEqual(failed(invoice), set())


class PromotionTests(SimpleTestCase):
    ITEMS = "2X BONBONS INVENTES 7,15  14,30e\nLe 2eme a moins 50% -3,58e\n"

    def test_a_promotion_printed_under_its_item_is_charged_to_that_item(self):
        invoice = parse(
            self.ITEMS, "10,72", "20%  8,93  1,79  10,72", pre_discount_line="IOTAI HOR: AVANTAGES  14,302"
        )
        (line,) = invoice.lines
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.total_ht, Decimal("8.94"))
        self.assertEqual(line.discount, Decimal("2.98"))
        attributed = next(check for check in invoice.checks if check.label == "Remise attribuée")
        self.assertTrue(attributed.passed)
        self.assertEqual(failed(invoice), set())

    def test_a_misread_total_heading_is_neither_an_item_nor_ignored(self):
        """"IOTAI HOR: AVANTAGES 14,302" is the pre-discount total. Missed, it
        either became a 14,30 EUR product or the discount went unread."""
        invoice = parse(
            self.ITEMS, "10,72", "20%  8,93  1,79  10,72", pre_discount_line="IOTAI HOR: AVANTAGES  14,302"
        )
        self.assertEqual(len(invoice.lines), 1)


class QuantityTests(SimpleTestCase):
    def test_a_misread_count_is_recomputed_from_the_amounts(self):
        invoice = parse("8 X AROM.INVENTE  1,89  5,67e\n", "5,67", "5,5%  5,37  0,30  5,67")
        self.assertEqual(invoice.lines[0].quantity, 3)
        recomputed = next(check for check in invoice.checks if check.label == "Quantités recalculées")
        self.assertTrue(recomputed.passed)
        self.assertEqual(failed(invoice), set())
