"""Sabbh Oriental receipts - the till that prints no product names.

Structurally faithful, data invented. Every item line really does read
"Article divers": what makes these receipts usable at all is the unit price,
so most of what is tested here is that the price, the count and the total
survive intact and stay consistent with one another.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.sabbh import SabbhParser

FIVE_FIVE = Decimal("0.055")

# 3 x 0.80 = 2.40 and 2 x 4.50 = 9.00, total 11.40 TTC. The "Base TVA" column
# is the TAX-INCLUSIVE total, unlike every other shop's - 11.40, not 10.81.
# "Artice divers" is a real reading of the placeholder: nothing depends on
# the word being spelled right.
BASIC = """Sabbh Oriental
12 Rue Inventee
75000 Paris
Tel 01 00 00 00 00
Vendeur:V1  Numero de ticket:6800001
Heure: 14-07-2026 14:49:26  Balance: 68
PLU
kg(pcs) €/kg(pcs)
Artice divers
3pcs  0,80  2,40 A
Article divers
2pcs  4,50  9,00 A
Articles: 2  Total: 11,40
Espèces  11,40€
Information TVA
Taux  Base TVA  TVA
TVAA 5.50%  11,40  0,59
"""

# The count is misread as 3 where the money says 2. Total 9.00 TTC.
MISREAD_COUNT = """Sabbh Oriental
12 Rue Inventee
Vendeur:V1  Numero de ticket:6800002
Heure: 19-06-2026 14:20:28  Balance: 68
PLU
kg(pcs) €/kg(pcs)
Article divers
3pcs  4,50  9,00 A
Articles: 1  Total: 9,00
Espèces  9,00€
Information TVA
Taux  Base TVA  TVA
TVAA 5.50%  9,00  0,47
"""

# A weighed line: 0.850 kg at 4.90/kg = 4.17.
WEIGHED = """Sabbh Oriental
12 Rue Inventee
Vendeur:V2  Numero de ticket:6800003
Heure: 03-04-2026 14:20:13  Balance: 68
PLU
kg(pcs) €/kg(pcs)
Article divers
0.850kg  4,90  4,17 A
Articles: 1  Total: 4,17
Espèces  4,17€
Information TVA
Taux  Base TVA  TVA
TVAA 5.50%  4,17  0,22
"""


def parse(text):
    return SabbhParser().parse_pages([PdfPage(text=text)], source_name="ticket.pdf")


def failed(invoice):
    return [check.label for check in invoice.checks if not check.passed]


class SabbhBasicTests(SimpleTestCase):
    def setUp(self):
        self.invoice = parse(BASIC)

    def test_both_lines_are_read(self):
        self.assertEqual(sorted(line.quantity for line in self.invoice.lines), [2, 3])
        expensive = next(line for line in self.invoice.lines if line.quantity == 2)
        self.assertEqual(expensive.total_ht, Decimal("8.53"))

    def test_lines_are_marked_as_unnamed(self):
        """The name is filled in at import time from the shop's price list -
        the parser itself must not touch the database."""
        for line in self.invoice.lines:
            self.assertTrue(line.is_placeholder)
            self.assertEqual(line.raw_name, "Article divers")

    def test_the_tax_inclusive_base_column_is_converted(self):
        """11.40 is the TTC total in that column, not the HT base. Read as HT
        it would put the invoice 0.59 EUR over what was paid."""
        total_ht = sum(line.total_ht for line in self.invoice.lines)
        self.assertEqual(total_ht + self.invoice.reconciliation_adjustment, Decimal("10.81"))

    def test_every_check_passes(self):
        self.assertEqual(failed(self.invoice), [])

    def test_ticket_number_and_date(self):
        self.assertEqual(self.invoice.invoice_number, "6800001")
        self.assertEqual(self.invoice.invoice_date.isoformat(), "2026-07-14")

    def test_the_rate_comes_from_the_letter_code(self):
        for line in self.invoice.lines:
            self.assertEqual(line.vat_rate, FIVE_FIVE)


class SabbhMisreadCountTests(SimpleTestCase):
    def test_the_count_is_recomputed_from_the_money(self):
        """Unit price and total are both two-decimal and check each other;
        the count is a single digit with nothing to check it against. When
        they disagree, the money wins - and the receipt says so."""
        invoice = parse(MISREAD_COUNT)
        self.assertEqual(invoice.lines[0].quantity, 2)
        self.assertIn("Quantités recalculées", [check.label for check in invoice.checks])
        self.assertEqual(failed(invoice), [])


class SabbhWeighedTests(SimpleTestCase):
    def test_a_weighed_line_keeps_its_weight_not_a_count(self):
        invoice = parse(WEIGHED)
        line = invoice.lines[0]
        self.assertEqual(line.total_volume, Decimal("0.850"))
        self.assertEqual(line.quantity, 1)
        self.assertEqual(line.total_ht, Decimal("3.95"))
        self.assertEqual(failed(invoice), [])


class SabbhFailureTests(SimpleTestCase):
    def test_a_dropped_line_fails_the_arithmetic(self):
        invoice = parse(BASIC.replace("3pcs  0,80  2,40 A\n", ""))
        self.assertEqual(len(invoice.lines), 1)
        self.assertIn("Somme des lignes = total imprimé", failed(invoice))

    def test_a_vat_amount_is_never_read_as_a_rate(self):
        """With its rate cut off, "0,59" in the VAT column has been read as a
        59% rate. Now the rate is named by arithmetic alone - 11,40 and 0,59
        only fit 5.5% - and never by reading an amount as a percentage."""
        invoice = parse(BASIC.replace("TVAA 5.50%  11,40  0,59", "TVAA  11,40  0,59"))
        for line in invoice.lines:
            self.assertEqual(line.vat_rate, FIVE_FIVE)
        self.assertEqual(failed(invoice), [])

    def test_no_rate_at_all_is_reported_and_not_guessed(self):
        invoice = parse(BASIC.replace("TVAA 5.50%  11,40  0,59", "TVAA  ....  ...."))
        self.assertIn("Table TVA lue", failed(invoice))
        for line in invoice.lines:
            self.assertEqual(line.vat_rate, Decimal("0"))

    def test_an_empty_page_is_an_empty_invoice(self):
        self.assertEqual(parse("").lines, [])
