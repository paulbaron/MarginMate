"""Plou & Fils parser tests. Structurally faithful, data invented - see
test_parser_metro.py for why.

Two tables per page, told apart by column count alone: 6 columns is the
product table, 5 is the VAT summary. The tests below pin that down, since
"column count" is the only thing keeping a VAT row out of the products.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.ploufils import PlouFilsParser

PRODUCT_TABLE = [
    ["Désignation", "Qté", "Px U. HT", "Px U. TTC", "HT", "TTC"],
    ["LES CAILLOUX EXEMPLE - - AC TOURAINE AMBOISE", "18", "4,60€", "5,52€", "82,80€", "99,36€"],
    ["AUTHENTICUS EXEMPLE - - IGP VAL DE LOIRE", "24", "5,00€", "6,00€", "120,00€", "144,00€"],
]
VAT_TABLE = [
    ["Libellé", "Hors taxe", "TVA", "TVA réglée", "TTC"],
    ["Taux 20.00%", "202,80€", "40,56€", "0,00€", "243,36€"],
]
TEXT = """\
SCEA PLOU & FILS
37530, Chargé, France (FR) Date: 29/01/2026
contact@plouetfils.com N° document: FA-202601-7187
Taux 20.00% 202,80€ 40,56€ 0,00€ 243,36€
"""

PAGE = PdfPage(text=TEXT, tables=[PRODUCT_TABLE, VAT_TABLE])


def parse(pages=None):
    return PlouFilsParser().parse_pages(pages or [PAGE])


class PlouFilsParserTests(SimpleTestCase):
    def test_reads_the_six_column_product_table(self):
        invoice = parse()
        self.assertEqual(
            [line.raw_name for line in invoice.lines],
            ["LES CAILLOUX EXEMPLE - - AC TOURAINE AMBOISE", "AUTHENTICUS EXEMPLE - - IGP VAL DE LOIRE"],
        )

    def test_amounts_strip_the_euro_sign(self):
        line = parse().lines[0]
        self.assertEqual(line.quantity, 18)
        self.assertEqual(line.total_ht, Decimal("82.80"))
        self.assertEqual(line.unit_cost_ht, Decimal("4.6000"))

    def test_the_five_column_vat_table_is_not_mistaken_for_products(self):
        self.assertEqual(len(parse().lines), 2)

    def test_the_header_row_is_skipped(self):
        """"Qté" isn't an int, which is what excludes the header."""
        self.assertNotIn("Désignation", [line.raw_name for line in parse().lines])

    def test_vat_rate_comes_from_the_summary_line(self):
        """Printed with a decimal POINT here, unlike every other supplier."""
        self.assertTrue(all(line.vat_rate == Decimal("0.20") for line in parse().lines))

    def test_vat_rate_falls_back_to_twenty_percent(self):
        page = PdfPage(text="N° document: FA-1\n", tables=[PRODUCT_TABLE])
        self.assertEqual(parse([page]).lines[0].vat_rate, Decimal("0.20"))

    def test_invoice_number_and_date(self):
        invoice = parse()
        self.assertEqual(invoice.invoice_number, "FA-202601-7187")
        self.assertEqual(invoice.invoice_date, date(2026, 1, 29))

    def test_no_tables_parses_to_an_empty_invoice(self):
        self.assertEqual(parse([PdfPage(text=TEXT)]).lines, [])
