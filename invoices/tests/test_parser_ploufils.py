"""Plou & Fils parser tests. Structurally faithful, data invented - see
test_parser_metro.py for why.

Three layouts so far, copied from real invoices: the product table is found
by its header and its columns by name, because the 2026 layout added a
"Taux" column (and an April 2026 invoice, read by column count, imported
with no lines at all); the VAT summary lost a column at the same time; and a
January 2025 invoice calls its number "Référence interne".
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
contact@exemple.fr N° document: FA-202601-0001
Libellé Hors taxe TVA TVA réglée TTC Montant total HT 202,80€
Taux 20.00% 202,80€ 40,56€ 0,00€ 243,36€
"""
PAGE = PdfPage(text=TEXT, tables=[PRODUCT_TABLE, VAT_TABLE])

# April 2026: a "Taux" column on every product row, four columns in the summary.
PRODUCT_TABLE_2026 = [
    ["Désignation", "Qté", "Px U. HT", "Px U. TTC", "HT", "TTC", "Taux"],
    ["LES CAILLOUX EXEMPLE - - AC TOURAINE AMBOISE", "18", "5,00€", "6,00€", "90,00€", "108,00€", "20,00%"],
    ["JUS DE RAISIN EXEMPLE - - VAL DE LOIRE", "24", "5,50€", "5,80€", "132,00€", "139,26€", "5,50%"],
]
VAT_TABLE_2026 = [
    ["Libellé", "Hors taxe", "TVA", "TTC"],
    ["Taux 20.00%", "90,00€", "18,00€", "108,00€"],
]
TEXT_2026 = """\
SCEA PLOU ET FILS FACTURE
37530, Chargé, France Date: 30/04/2026
contact@exemple.fr N° document: FA-202604-0001
Désignation Qté Px U. HT Px U. TTC HT TTC Taux
Libellé Hors taxe TVA TTC Montant total HT 222,00€
Taux 20.00% 90,00€ 18,00€ 108,00€
SCEA PLOU ET FILS FA-202604-0001 - EXEMPLE - EXEMPLE Page 1/1
"""
PAGE_2026 = PdfPage(text=TEXT_2026, tables=[PRODUCT_TABLE_2026, VAT_TABLE_2026])

# January 2025: "Référence interne", amounts with a decimal point.
PRODUCT_TABLE_2025 = [
    ["Désignation", "Qté", "Px U. HT", "Px U. TTC", "HT", "TTC"],
    ["AUTHENTICUS EXEMPLE - - IGP VAL DE LOIRE", "6", "5.00€", "6.00€", "30.00€", "36.00€"],
]
TEXT_2025 = """\
FACTURE
Date: 20/01/2025
Référence interne: FA-202501-0001
Libellé Hors taxe TVA TVA réglée TTC Montant total HT 30,00€
"""
PAGE_2025 = PdfPage(text=TEXT_2025, tables=[PRODUCT_TABLE_2025, VAT_TABLE])


def parse(pages=None):
    return PlouFilsParser().parse_pages(pages or [PAGE])


class PlouFilsParserTests(SimpleTestCase):
    def test_reads_the_product_table(self):
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

    def test_the_vat_table_is_not_mistaken_for_products(self):
        self.assertEqual(len(parse().lines), 2)

    def test_the_header_row_is_skipped(self):
        self.assertNotIn("Désignation", [line.raw_name for line in parse().lines])

    def test_vat_rate_comes_from_the_summary_line(self):
        """Printed with a decimal POINT here, unlike every other supplier."""
        self.assertTrue(all(line.vat_rate == Decimal("0.20") for line in parse().lines))

    def test_vat_rate_falls_back_to_twenty_percent(self):
        page = PdfPage(text="N° document: FA-1\n", tables=[PRODUCT_TABLE])
        self.assertEqual(parse([page]).lines[0].vat_rate, Decimal("0.20"))

    def test_invoice_number_and_date(self):
        invoice = parse()
        self.assertEqual(invoice.invoice_number, "FA-202601-0001")
        self.assertEqual(invoice.invoice_date, date(2026, 1, 29))

    def test_no_tables_parses_to_an_empty_invoice(self):
        self.assertEqual(parse([PdfPage(text=TEXT)]).lines, [])

    def test_lines_that_add_up_raise_no_warning(self):
        self.assertEqual(parse().warnings, [])


class Layout2026Tests(SimpleTestCase):
    def test_rows_with_a_rate_column_are_products(self):
        invoice = parse([PAGE_2026])
        self.assertEqual(
            [(line.raw_name, line.quantity, line.total_ht) for line in invoice.lines],
            [
                ("LES CAILLOUX EXEMPLE - - AC TOURAINE AMBOISE", 18, Decimal("90.00")),
                ("JUS DE RAISIN EXEMPLE - - VAL DE LOIRE", 24, Decimal("132.00")),
            ],
        )

    def test_each_line_takes_its_own_printed_rate(self):
        self.assertEqual([line.vat_rate for line in parse([PAGE_2026]).lines], [Decimal("0.20"), Decimal("0.055")])

    def test_the_four_column_summary_is_not_a_product(self):
        self.assertEqual(len(parse([PAGE_2026]).lines), 2)

    def test_number_date_and_totals(self):
        invoice = parse([PAGE_2026])
        self.assertEqual((invoice.invoice_number, invoice.invoice_date), ("FA-202604-0001", date(2026, 4, 30)))
        self.assertEqual(invoice.warnings, [])


class Layout2025Tests(SimpleTestCase):
    def test_the_number_printed_as_internal_reference(self):
        invoice = parse([PAGE_2025])
        self.assertEqual((invoice.invoice_number, invoice.invoice_date), ("FA-202501-0001", date(2025, 1, 20)))

    def test_amounts_with_a_decimal_point(self):
        self.assertEqual(parse([PAGE_2025]).lines[0].total_ht, Decimal("30.00"))


class RobustnessTests(SimpleTestCase):
    def test_the_footer_gives_the_number_when_the_header_does_not(self):
        page = PdfPage(text="FACTURE\nSCEA PLOU ET FILS FA-202607-0042 - EXEMPLE Page 1/1\n", tables=[PRODUCT_TABLE])
        self.assertEqual(parse([page]).invoice_number, "FA-202607-0042")

    def test_lines_that_do_not_add_up_to_the_printed_total_are_flagged(self):
        """A dropped or garbled row must not import as a smaller invoice."""
        page = PdfPage(text=TEXT.replace("Montant total HT 202,80€", "Montant total HT 250,80€"), tables=[PRODUCT_TABLE])
        (warning,) = parse([page]).warnings
        self.assertIn("202.80", warning)
        self.assertIn("250.80", warning)

    def test_a_product_table_carried_over_to_the_next_page(self):
        continued = PdfPage(
            text="Page 2/2\n",
            tables=[[["CREMANT EXEMPLE - - CREMANT DE LOIRE", "6", "5,00€", "6,00€", "30,00€", "36,00€"]]],
        )
        self.assertEqual(len(parse([PAGE, continued]).lines), 3)

    def test_thousands_are_read(self):
        table = [PRODUCT_TABLE[0], ["GRANDE CUVEE EXEMPLE", "300", "4,11€", "4,93€", "1 234,56€", "1 481,47€"]]
        other = [PRODUCT_TABLE[0], ["GRANDE CUVEE EXEMPLE", "300", "4,11€", "4,93€", "1.234,56€", "1.481,47€"]]
        for rows in (table, other):
            with self.subTest(amount=rows[1][4]):
                self.assertEqual(parse([PdfPage(text="", tables=[rows])]).lines[0].total_ht, Decimal("1234.56"))
