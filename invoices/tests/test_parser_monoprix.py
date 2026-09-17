"""Monoprix receipts: two layouts, one parser.

Structurally faithful, data invented. The second fixture is not a
hypothetical - some tills in this group print the whole ticket in Dutch, and
one recogniser read "TOTAAL EX PROMO" as "TOTAAL EX PROHO" - which, back when
the parser looked for the words, let it run past the totals and read the
payment and VAT blocks as products. Now the items stop where a line repeats
what they add up to.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers import ticket_parser_for
from invoices.parsers.base import PdfPage

FIVE_FIVE = Decimal("0.055")

# Department headings, one plain line and one multiplied line.
# 2.40 + 6.00 = 8.40 TTC; HT 7.96 + VAT 0.44.
FRENCH = """MONOPRIX
Dimax
12 RUE INVENTEE
75000PARIS
0100000000
BONJOUR
EPICERIE/BOISSONS.................
SUCRE ROUX PU  2,40
FRUITS/LEGUMES..................
4X BASILIC FRAIS  1,50  6,00
TOTAL HORS AVANTAGES  8,40
NOMBRE D'ARTICLES  5
RESTE A PAYER  8,40
PAIEMENT
CB EMV  8,40
Rendu (ESPECES)  0,00
Vente a emporter
TVA  H.T.  T.V.A.  T.T.C
5,5%  7,96  0,44  8,40
Total TVA  7,96  0,44  8,40
Version  V1.1.0
Superviseur: 6521
28/01/2026 16:03 245 93 4042 930
024509304042260128160356
Garantie legale de conformite aupres
"""

# The Dutch-language variant. 1.95 + 4.50 = 6.45 TTC. Note the date is
# printed with a two-digit year and so is never read - the barcode is what
# identifies this ticket.
DUTCH = """MONOPRIX
024507308908260421161657
DINAX
12 RUE INVENTEE
75000PARIS
Tel:0100000000
21/04/26-16:17730738908
OPERATION:VERKOOP IN
TBIO CONCOMBRE  1.95EUR
T3XBASILIC FRAIS  1  1.50EUR  4.50EUR
TOTAAL EX PROHO  6.45 EUR
AANTAL ARTIKELEN  4
9532INCONNU  6.45 EUR
CARTE  6.45EUR
SPLIT PER BTW TARIEF
CODE  EXTAX  TARIEF  BTW  INCTAX
6  6.1137  5.50%  0.3363  6.45
TOTAAL  0.3363  6.45
SIRET48000051200016
"""


def parse(text):
    return ticket_parser_for("MONOPRIX").parse_pages([PdfPage(text=text)], source_name="ticket.pdf")


class MonoprixFrenchTests(SimpleTestCase):
    def setUp(self):
        self.invoice = parse(FRENCH)

    def test_only_the_two_products_are_lines(self):
        """"CB EMV 8,40" and "Rendu (ESPECES) 0,00" have exactly the shape of
        a priced item line and are not items."""
        self.assertEqual(len(self.invoice.lines), 2)
        self.assertEqual(
            sorted(line.raw_name for line in self.invoice.lines),
            ["BASILIC FRAIS", "SUCRE ROUX PU"],
        )

    def test_a_multiplied_line_keeps_its_count(self):
        basil = next(line for line in self.invoice.lines if "BASILIC" in line.raw_name)
        self.assertEqual(basil.quantity, 4)
        self.assertEqual(basil.total_ht, Decimal("5.69"))

    def test_department_headings_become_the_category(self):
        sugar = next(line for line in self.invoice.lines if "SUCRE" in line.raw_name)
        self.assertEqual(sugar.category, "EPICERIE/BOISSONS")

    def test_the_rate_comes_from_the_table(self):
        for line in self.invoice.lines:
            self.assertEqual(line.vat_rate, FIVE_FIVE)

    def test_the_lines_reconcile(self):
        total_ht = sum(line.total_ht for line in self.invoice.lines)
        self.assertEqual(total_ht + self.invoice.reconciliation_adjustment, Decimal("7.96"))
        self.assertEqual([c.label for c in self.invoice.checks if not c.passed], [])

    def test_the_barcode_identifies_the_ticket(self):
        self.assertEqual(self.invoice.invoice_number, "024509304042260128160356")
        self.assertEqual(self.invoice.invoice_date.isoformat(), "2026-01-28")


class MonoprixDutchTests(SimpleTestCase):
    def setUp(self):
        self.invoice = parse(DUTCH)

    def test_the_totals_block_stops_the_item_scan(self):
        """Read past it, the parser reported 38.30 EUR of items on a
        7.66 EUR receipt - from the payment lines and the VAT table."""
        self.assertEqual(len(self.invoice.lines), 2)
        for line in self.invoice.lines:
            self.assertNotIn("INCONNU", line.raw_name)
            self.assertNotIn("CARTE", line.raw_name)

    def test_the_vat_code_before_the_quantity_does_not_eat_the_count(self):
        basil = next(line for line in self.invoice.lines if "BASILIC" in line.raw_name)
        self.assertEqual(basil.quantity, 3)
        self.assertEqual(basil.total_ht, Decimal("4.27"))

    def test_the_lines_reconcile_against_the_dutch_vat_block(self):
        total_ht = sum(line.total_ht for line in self.invoice.lines)
        self.assertEqual(total_ht + self.invoice.reconciliation_adjustment, Decimal("6.11"))
        self.assertEqual([c.label for c in self.invoice.checks if not c.passed], [])

    def test_a_two_digit_year_is_not_guessed_at(self):
        self.assertIsNone(self.invoice.invoice_date)
        self.assertEqual(self.invoice.invoice_number, "024507308908260421161657")


class MonoprixFailureTests(SimpleTestCase):
    def _unreadable_totals(self):
        broken = FRENCH.replace("RESTE A PAYER  8,40", "RESIE A PAYEB  ....")
        broken = broken.replace("TOTAL HORS AVANTAGES  8,40", "TOTAL HORS AVANTAGES  ....")
        return broken.replace("CB EMV  8,40", "CB EMV  ....")

    def test_the_vat_tables_own_total_stands_in_for_mangled_totals(self):
        """Every total line unreadable, the VAT table still prints 8,40 twice
        and adds up to it: that is a printed total."""
        invoice = parse(self._unreadable_totals())
        self.assertEqual([c.label for c in invoice.checks if not c.passed], [])

    def test_no_readable_total_is_reported(self):
        """And with the table's total gone too, the HT base 7,96 is the
        largest figure printed twice - which must not become the total."""
        broken = self._unreadable_totals().replace("0,44  8,40", "0,44  ....")
        invoice = parse(broken)
        failed = [check.label for check in invoice.checks if not check.passed]
        self.assertIn("Total imprimé lu", failed)

    def test_a_misread_amount_fails_the_arithmetic(self):
        broken = FRENCH.replace("SUCRE ROUX PU  2,40", "SUCRE ROUX PU  2,10")
        invoice = parse(broken)
        failed = [check.label for check in invoice.checks if not check.passed]
        self.assertIn("Somme des lignes = total imprimé", failed)

    def test_an_empty_page_is_an_empty_invoice(self):
        self.assertEqual(parse("").lines, [])
