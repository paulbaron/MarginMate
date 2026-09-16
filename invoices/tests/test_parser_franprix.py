"""Franprix receipts, one fixture per layout quirk.

Structurally faithful, data invented: the column positions, the glued VAT
code, the mangled discount heading and the column rules read as digits are
copied from what the current recogniser (PP-OCRv6) makes of real tickets;
every product name and amount is made up. Each fixture's arithmetic is
internally consistent, because that is what the parser checks itself
against - a fixture whose numbers don't add up would test the failure path
while looking like it tested the happy one.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.franprix import FranprixParser

FIVE_FIVE = Decimal("0.055")
TWENTY = Decimal("0.20")

# Three loaves at 0.55 and a lemon at 1.80 = 3.45; a "3 pour 2" takes 0.55
# off, so 2.90 is paid. HT 2.75 + VAT 0.15 = 2.90.
#
# Quirks in here: "T10.55" (the VAT code glued to the price - still six times
# on the 42 real receipts), the DUPLICATA banner, the discount heading read
# as "Detai des renises innediates", and "2.751 0.151 2.901" - the VAT
# table's column rules read as trailing 1s.
DISCOUNTED = """franprix
FRANPRIX
12 RUE INVENTEE
75000 PARIS
BIENVENUE!
004211-01
**DUPLICATA**-
PAIN COMPLET  T10.55
PAIN COMPLET  T10.55
PAIN COMPLET  T1 0.55
CITRON VERT 400G  T11.80
SOUS-TOTAL  2.90
TOTAL SANS AVANTAGES  3.45
3pour2  Detai des renises innediates:
PAIN COMPLET  0.55
TOTAL renise  0.55-
TOTAL A PAYER  2.90
CB SANS CONTACT  2.90
-Rate--Taxaole  Vat-  Total-
5.5%|  2.751  0.151  2.901
CUMULEZ DES EUROS
15-07-2026 WEDNESDAY  17:39
LORIAN  R1 004211-01 385
4PCS
MERCI ET A BIENTOT
"""

# A 20% ticket: household vinegar, not food. Two at 3.60 = 7.20 TTC,
# HT 6.00 + VAT 1.20.
TWENTY_PERCENT = """franprix
FRANPRIX
12 RUE INVENTEE
75000 PARIS
004211-01
**DUPLICATA**-
NETTOYANT VINAIGR  T23.60
NETTOYANT VINAIGR  T23.60
SOUS-TOTAL  7.20
TOTAL A PAYER  7.20
CB SANS CONTACT  7.20
Rate--axa  e-.-Vat...-ota.-  20%  6.001  1.201  7.201
06-05-2026 WEDNESDAY  18:32
UDAYAN  R1 004211-01 389
2PCS
"""

# The multiplier form, printed when the same product is scanned repeatedly:
# "T1 6 X 0.55  3.30". 3.30 TTC, HT 3.13 + VAT 0.17.
MULTIPLIER = """franprix
FRANPRIX
004211-01
**DUPLICATA**-
PAIN COMPLET  T16X0.55.3.30
SOUS-TOTAL  3.30
TOTAL A PAYER  3.30
Rate--Taxaole  Vat-  Total-
5.5%  3.13  0.17  3.30
09-07-2026 THURSDAY  16:44
FERROUIJA  R1 004211-01 304
"""

# The words around the totals as the current engine really reads them, on
# another ticket's layout. Nothing recognises the words any more, so none of
# this may matter: 2 x 0.49 + 2.29 = 3.27.
GARBLED_WORDS = """franprix
FRANPRIX
004211-01
BAGUETTE BLANC  T1 0.49.
BAGUETIE BLANC  T1 0.49.
CITRON SHT 50DG  T1 2.29.
ISQUS-OTAL I  3.27-
OTALA PAYER  3.27.
B SAN CDNTACT  3.27.
RateTaxaoleVat--Total
5.5%  3.10  0.17  3.271
07-07-2026 TUESDAY  17:22
ORIAN  R1 004211-01 316
"""


def parse(text):
    return FranprixParser().parse_pages([PdfPage(text=text)], source_name="ticket.pdf")


def failed(invoice):
    return [check.label for check in invoice.checks if not check.passed]


class FranprixDiscountedTests(SimpleTestCase):
    def setUp(self):
        self.invoice = parse(DISCOUNTED)

    def test_every_item_line_is_read(self):
        self.assertEqual(len(self.invoice.lines), 4)

    def test_the_duplicata_banner_is_not_a_product(self):
        for line in self.invoice.lines:
            self.assertNotIn("DUPLICATA", line.raw_name.upper())

    def test_a_vat_code_glued_to_the_price_is_still_the_code_and_the_price(self):
        """"T10.55" is T1 and 0.55, not T and 10.55."""
        loaves = [line for line in self.invoice.lines if "PAIN" in line.raw_name]
        self.assertEqual(len(loaves), 3)

    def test_amounts_are_converted_from_ttc_to_ht(self):
        lemon = next(line for line in self.invoice.lines if "CITRON" in line.raw_name)
        self.assertEqual(lemon.total_ht, Decimal("1.71"))
        self.assertEqual(lemon.vat_rate, FIVE_FIVE)

    def test_the_promotion_lands_on_the_bread_not_the_lemon(self):
        lemon = next(line for line in self.invoice.lines if "CITRON" in line.raw_name)
        self.assertEqual(lemon.discount, Decimal("0"))
        loaves = [line for line in self.invoice.lines if "PAIN" in line.raw_name]
        self.assertEqual(sum(line.discount for line in loaves), Decimal("0.52"))

    def test_the_lines_reconcile_with_the_ticket(self):
        total_ht = sum(line.total_ht for line in self.invoice.lines)
        self.assertEqual(total_ht + self.invoice.reconciliation_adjustment, Decimal("2.75"))

    def test_every_check_passes(self):
        self.assertEqual(failed(self.invoice), [])

    def test_ticket_number_and_date(self):
        self.assertEqual(self.invoice.invoice_number, "004211-01-385")
        self.assertEqual(self.invoice.invoice_date.isoformat(), "2026-07-15")


class FranprixWordsDoNotMatterTests(SimpleTestCase):
    def test_totals_are_found_however_the_words_came_out(self):
        """"ISQUS-OTAL I", "OTALA PAYER": a parser keyed on the words found no
        total on this ticket and failed it."""
        invoice = parse(GARBLED_WORDS)
        self.assertEqual(len(invoice.lines), 3)
        self.assertEqual(failed(invoice), [])


class FranprixRateTests(SimpleTestCase):
    def test_a_twenty_percent_ticket_is_not_assumed_to_be_food(self):
        """Two of the 42 real receipts are at 20%. Reading them at 5.5%
        understates the cost by 14% with nothing downstream able to tell."""
        invoice = parse(TWENTY_PERCENT)
        self.assertEqual(len(invoice.lines), 2)
        for line in invoice.lines:
            self.assertEqual(line.vat_rate, TWENTY)
            self.assertEqual(line.total_ht, Decimal("3.00"))
        self.assertEqual(failed(invoice), [])


class FranprixMultiplierTests(SimpleTestCase):
    def test_a_multiplied_line_keeps_its_quantity_and_total(self):
        """"T1 6 X 0.55  3.30" on one line. Unmatched, all six loaves vanish
        while the receipt still balances against its own printed total."""
        invoice = parse(MULTIPLIER)
        self.assertEqual(len(invoice.lines), 1)
        line = invoice.lines[0]
        self.assertEqual(line.quantity, 6)
        self.assertEqual(line.total_ht, Decimal("3.13"))
        self.assertEqual(line.unit_cost_ht, Decimal("0.5217"))
        self.assertEqual(failed(invoice), [])


class FranprixFailureTests(SimpleTestCase):
    def test_a_missing_item_line_fails_the_arithmetic_rather_than_passing(self):
        """The whole point of the checks: a receipt the parser read
        incompletely must say so, not quietly book a smaller invoice."""
        invoice = parse(DISCOUNTED.replace("CITRON VERT 400G  T11.80\n", ""))
        self.assertEqual(len(invoice.lines), 3)
        self.assertIn("Somme des lignes = total imprimé", failed(invoice))
        self.assertEqual(invoice.reconciliation_adjustment, Decimal("0"))

    def test_losing_the_free_item_of_a_promotion_is_still_caught(self):
        """The silent case. Misread the free loaf of a "3 pour 2" and the two
        loaves and the lemon left add up to exactly the 2.90 paid - every sum
        balances. Only the pre-discount total the ticket prints (3.45) says
        something is missing. Here the loss comes from a letter read inside
        the price, which is no longer repaired: reported instead."""
        invoice = parse(DISCOUNTED.replace("PAIN COMPLET  T1 0.55\n", "PAIN COMPLET  T1Q.55\n"))
        self.assertEqual(len(invoice.lines), 3)
        self.assertIn("Articles = total avant remise", failed(invoice))

    def test_an_unreadable_vat_table_is_reported(self):
        invoice = parse(DISCOUNTED.replace("5.5%|  2.751  0.151  2.901", "Sxx%  ....  ...."))
        self.assertIn("Table TVA lue", failed(invoice))

    def test_an_empty_page_is_an_empty_invoice(self):
        self.assertEqual(parse("").lines, [])
