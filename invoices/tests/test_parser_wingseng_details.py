"""Wing Seng: a faded amount, the payment lines below the totals, and a count
that disagrees with its line.

Shaped on a real crumpled ticket whose "1.00" and "TOTAL EUR: 13.51" faded:
the recogniser read the mint with no price and the total as "IBALRE 73.57" -
a 73,57 EUR purchase that never happened. Names and amounts are invented.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers import ticket_parser_for
from invoices.parsers.base import PdfPage

HEADER = (
    "WING SENG\n2 RUE INVENTEE\n75000 PARIS\nWING SENG\nSiren:000000000\n"
    "Ticket:000123  02/06/2026 17H06\nCaisse N:03  Vendeur N:03\nEUR\n"
)


def parse(body, footer):
    return ticket_parser_for("WINGSENG").parse_pages([PdfPage(text=HEADER + body + footer)])


def totals(total, vat):
    return f"S TOTAL EUR:  {total}\nRecu CARTE BLEUE:\n{total}\nTOTAL EUR:  {total}\nTVA 5.50 %:  {vat} EUR\n"


def failed(invoice):
    return {check.label for check in invoice.checks if not check.passed}


class FadedAmountTests(SimpleTestCase):
    BODY = "#CITRON VERT  12.51\nMAN4.184kg  2.99EUR/kg\nMENTHE\n2X  0.50EUR\n"

    def test_a_faded_amount_is_computed_from_the_detail_line_under_it(self):
        invoice = parse(self.BODY, totals("13.51", "0.70"))
        self.assertEqual([line.raw_name for line in invoice.lines], ["CITRON VERT", "MENTHE"])
        mint = invoice.lines[1]
        self.assertEqual(mint.quantity, 2)
        self.assertEqual(mint.total_ht, Decimal("0.95"))  # 1,00 TTC at 5.5%
        self.assertEqual(invoice.lines[0].total_volume, Decimal("4.184"))
        computed = next(check for check in invoice.checks if check.label == "Montants recalculés")
        self.assertTrue(computed.passed)
        self.assertEqual(failed(invoice), set())

    def test_the_detail_does_not_attach_itself_to_the_item_above_instead(self):
        """Without the pending name, "2 x 0.50" made the lemons a count of 2."""
        invoice = parse(self.BODY, totals("13.51", "0.70"))
        self.assertEqual(invoice.lines[0].quantity, 1)


class BelowTheTotalsTests(SimpleTestCase):
    def test_nothing_below_the_first_total_is_an_item(self):
        footer = "S TOTAL EUR:  1.00\nIBALRE  73.57\nTOTAL EUR:  1.00\nTVA 5.50 %:  0.05 EUR\n"
        invoice = parse("MENTHE  1.00\n", footer)
        self.assertEqual([line.raw_name for line in invoice.lines], ["MENTHE"])

    def test_a_faded_total_is_not_guessed_but_the_lines_keep_their_rate(self):
        invoice = parse("#CITRON VERT  12.51\nMENTHE  1.00\n", "TOTAL  CEUR  1351\nTVA5.50%:  .'O EUR\n")
        self.assertEqual({line.vat_rate for line in invoice.lines}, {Decimal("0.055")})
        self.assertIn("Total imprimé lu", failed(invoice))


class CountTests(SimpleTestCase):
    def test_a_count_that_disagrees_with_its_line_is_recomputed(self):
        invoice = parse("MENTHE  1.50\n5 x  0.50EUR\n", totals("1.50", "0.08"))
        self.assertEqual(invoice.lines[0].quantity, 3)
        self.assertIn("Quantités recalculées", {check.label for check in invoice.checks})
