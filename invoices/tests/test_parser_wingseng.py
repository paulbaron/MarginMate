"""Wing Seng receipts - clean print, dangerous structure.

Structurally faithful, data invented. The optical reading of these is the
easiest of the four; the risk is entirely in how the lines are *understood*.
Each item is a name and a total, followed by an indented detail line saying
how that total was reached. Those detail lines carry two real numbers, and
treating one as an item invents a purchase that never happened - which is
exactly what a local vision model did to this layout during evaluation.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers import ticket_parser_for
from invoices.parsers.base import PdfPage

FIVE_FIVE = Decimal("0.055")

# 2 x 0.50 = 1.00 and 3.000 kg at 3.00/kg = 9.00, total 10.00 TTC.
# "MAN" is the till's manual-entry marker, not a product.
BASIC = """WING SENG
2 RUE INVENTEE
75000 PARIS
WING SENG
Siren:000000000
Ticket:000197  02/06/2026 17H06
Caisse N:03  Vendeur N:03
EUR
MENTHE  1.00
2x  0.50EUR
#CITRON VERT  9.00
MAN  3.000kg × 3.00EUR/kg
S TOTAL EUR:  10.00
Recu CARTE BLEUE:  10.00
TOTAL EUR:  10.00
TVA5.50%:  0.52 EUR
UDF 1.2 Certificat LNE 34796r1
signature:/fLySt8DNm8Qm5Lwr2va
Merci de votre visite
Tel:0100000000
Fax:0100000001
"""

# The decimal point dropped out of the grand total: "1351" for "13.51".
DROPPED_DECIMAL = """WING SENG
2 RUE INVENTEE
75000 PARIS
Siren:000000000
Ticket:000401  28/03/2026 17H56
EUR
#CITRON VERT  12.51
MAN 4.184kg X  2.99EUR/kg
MENTHE  1.00
2X  0.50EUR
TOTAL  EUR  1351
Recu CARTE BLEUE  13.51
TVA5.50%:  0.70 EUR
"""


def parse(text):
    return ticket_parser_for("WINGSENG").parse_pages([PdfPage(text=text)], source_name="ticket.pdf")


def failed(invoice):
    return [check.label for check in invoice.checks if not check.passed]


class WingSengBasicTests(SimpleTestCase):
    def setUp(self):
        self.invoice = parse(BASIC)

    def test_a_detail_line_is_not_a_product(self):
        """"MAN 3.000kg × 3.00EUR/kg" explains the line above it. Read as an
        item it adds a third purchase at a fabricated price, and the receipt
        no longer matches its own total."""
        self.assertEqual(sorted(line.raw_name for line in self.invoice.lines), ["CITRON VERT", "MENTHE"])

    def test_the_count_comes_from_the_detail_line(self):
        mint = next(line for line in self.invoice.lines if line.raw_name == "MENTHE")
        self.assertEqual(mint.quantity, 2)
        self.assertEqual(mint.total_ht, Decimal("0.95"))
        self.assertEqual(mint.unit_cost_ht, Decimal("0.4750"))

    def test_the_weight_comes_from_the_detail_line(self):
        lemon = next(line for line in self.invoice.lines if line.raw_name == "CITRON VERT")
        self.assertEqual(lemon.total_volume, Decimal("3.000"))

    def test_a_vat_table_with_no_base_still_reconciles(self):
        """The whole table is "TVA 5,50%: 0,52 EUR" - no base, no total. The
        receipt's own grand total supplies the missing side."""
        total_ht = sum(line.total_ht for line in self.invoice.lines)
        self.assertEqual(total_ht + self.invoice.reconciliation_adjustment, Decimal("9.48"))
        self.assertEqual(failed(self.invoice), [])

    def test_payment_lines_are_not_products(self):
        for line in self.invoice.lines:
            self.assertNotIn("CARTE", line.raw_name.upper())
            self.assertNotIn("TOTAL", line.raw_name.upper())

    def test_ticket_number_and_date(self):
        self.assertEqual(self.invoice.invoice_number, "000197")
        self.assertEqual(self.invoice.invoice_date.isoformat(), "2026-06-02")

    def test_the_rate_is_applied_to_every_line(self):
        for line in self.invoice.lines:
            self.assertEqual(line.vat_rate, FIVE_FIVE)


class WingSengDroppedDecimalTests(SimpleTestCase):
    def test_a_total_with_no_decimal_point_is_flagged_not_guessed(self):
        """"1351" could be 13.51 or 1351.00. The items do sum to 13.51, but
        inferring the total from the lines would defeat the point of having a
        printed total to check them against - so it is reported instead."""
        invoice = parse(DROPPED_DECIMAL)
        self.assertIn("Total imprimé lu", failed(invoice))
        self.assertEqual(invoice.reconciliation_adjustment, Decimal("0"))

    def test_the_items_themselves_are_still_read(self):
        """With no total to stop at, the items end where a later line repeats
        what two of them add up to - the card payment here."""
        invoice = parse(DROPPED_DECIMAL)
        self.assertEqual(len(invoice.lines), 2)


class WingSengFailureTests(SimpleTestCase):
    def test_an_empty_page_is_an_empty_invoice(self):
        self.assertEqual(parse("").lines, [])

    def test_a_dropped_item_fails_the_arithmetic(self):
        invoice = parse(BASIC.replace("MENTHE  1.00\n2x  0.50EUR\n", ""))
        self.assertEqual(len(invoice.lines), 1)
        self.assertIn("Somme des lignes = total imprimé", failed(invoice))
