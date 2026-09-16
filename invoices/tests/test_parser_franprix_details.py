"""Franprix: which item a weight belongs to, a banner glued to an item, and a
misread count.

Layouts copied from real recognitions of the corpus; every name and amount is
invented.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.franprix import FranprixParser

HEADER = "franprix\nFRANPRIX\n12 RUE INVENTEE\n75000 PARIS\n005333-01\n"
FOOTER = "01-06-2026 MONDAY  17:39\nUDAYAN  R1 001234-01 385\n"


def parse(body, total, vat_row):
    text = (
        HEADER
        + body
        + f"SOUS-TOTAL  {total}\nTOTAL SANS AVANTAGES  {total}\nTOTAL A PAYER  {total}\n"
        + f"CB SANS CONTACT  {total}\n-Rate--Taxable--Vat--Total-\n{vat_row}\n"
        + FOOTER
    )
    return FranprixParser().parse_pages([PdfPage(text=text)])


def line_named(invoice, name):
    return next(line for line in invoice.lines if line.raw_name == name)


def failed(invoice):
    return {check.label for check in invoice.checks if not check.passed}


WEIGHED = "POMME JULIET X4  T13.89\nBRUTWEIGHT 0.920 KG\n@3.49./KG\nORANGE  T13.21\n"
WEIGHED_VAT = "15.5%1  6.731  0.371  7.101"


class WeightTests(SimpleTestCase):
    def test_a_weight_belongs_to_the_item_printed_below_it(self):
        """0.920 kg x 3.49 EUR/kg = 3.21: the orange, not the apples above.
        The parser used to put the kilos on the apples."""
        invoice = parse(WEIGHED, "7.10", WEIGHED_VAT)
        self.assertEqual(line_named(invoice, "ORANGE").total_volume, Decimal("0.920"))
        self.assertEqual(line_named(invoice, "POMME JULIET X4").total_volume, Decimal("0"))
        self.assertEqual(failed(invoice), set())

    def test_the_arithmetic_decides_when_a_weight_follows_its_item(self):
        body = "ORANGE  T13.21\nBRUTWEIGHT 0.920 KG\n@3.49./KG\nPOMME JULIET X4  T13.89\n"
        invoice = parse(body, "7.10", WEIGHED_VAT)
        self.assertEqual(line_named(invoice, "ORANGE").total_volume, Decimal("0.920"))
        self.assertEqual(line_named(invoice, "POMME JULIET X4").total_volume, Decimal("0"))

    def test_the_unit_after_the_price_per_kilo_can_read_as_anything(self):
        """"à 3.49. / <G" is how the current engine reads "@ 3.49 / KG": a
        price followed by a slash is a price per unit, whatever the unit."""
        body = WEIGHED.replace("@3.49./KG", "à 3.49. / <G")
        invoice = parse(body, "7.10", WEIGHED_VAT)
        self.assertEqual(line_named(invoice, "ORANGE").total_volume, Decimal("0.920"))
        self.assertEqual(failed(invoice), set())

    def test_a_weight_that_matches_no_item_is_reported(self):
        body = WEIGHED.replace("@3.49./KG", "@9.99./KG")
        invoice = parse(body, "7.10", WEIGHED_VAT)
        self.assertIn("Poids rattachés", failed(invoice))


class BannerTests(SimpleTestCase):
    def test_an_item_glued_to_the_duplicata_banner_keeps_its_price(self):
        """Skipping the whole line once took the lemons - and 1,89 EUR - with
        it. The banner stays in the name, where the review screen shows it;
        the money is what must not go."""
        invoice = parse("CITRON SHT 500G  **DUFLICATA**--  T11.89\n", "1.89", "15.5%1  1.791  0.101  1.891")
        self.assertEqual(len(invoice.lines), 1)
        self.assertTrue(invoice.lines[0].raw_name.startswith("CITRON SHT 500G"))
        self.assertEqual(invoice.lines[0].total_ht, Decimal("1.79"))
        self.assertEqual(failed(invoice), set())

    def test_the_banner_alone_is_still_not_a_product(self):
        invoice = parse("**OUFLICATA**-\nCITRON SHT 500G  T11.89\n", "1.89", "15.5%1  1.791  0.101  1.891")
        self.assertEqual([line.raw_name for line in invoice.lines], ["CITRON SHT 500G"])


class QuantityTests(SimpleTestCase):
    def test_a_misread_count_is_recomputed_from_the_amounts(self):
        """8 x 0.49 is not 2.94; 6 x 0.49 is. The count is the digit nothing
        else checks, the two amounts are checked by the receipt's sums."""
        invoice = parse("BAGUETTE BLANC  T18X0.49  2.94\n", "2.94", "15.5%1  2.791  0.151  2.941")
        self.assertEqual(invoice.lines[0].quantity, 6)
        recomputed = next(check for check in invoice.checks if check.label == "Quantités recalculées")
        self.assertTrue(recomputed.passed)

    def test_a_count_no_whole_number_explains_is_reported(self):
        invoice = parse("BAGUETTE BLANC  T13X0.49  2.99\n", "2.99", "15.5%1  2.831  0.161  2.991")
        self.assertIn("Quantité x prix unitaire = total", failed(invoice))


class VatRecoveryTests(SimpleTestCase):
    BODY = "CITRON SHT 500G  T11.89\nORANGE A JUS  T14.49\n"

    def test_a_legible_rate_prices_the_lines_even_when_the_table_is_not(self):
        invoice = parse(self.BODY, "6.38", "15.5%  44  0.471  6.38")
        self.assertEqual({line.vat_rate for line in invoice.lines}, {Decimal("0.055")})
        self.assertIn("Table TVA 5.5%", failed(invoice))

    def test_an_illegible_rate_named_by_the_arithmetic(self):
        invoice = parse(self.BODY, "6.38", "XSG  6.05  0.33  6.38")
        self.assertEqual({line.vat_rate for line in invoice.lines}, {Decimal("0.055")})
        self.assertEqual(failed(invoice), set())
