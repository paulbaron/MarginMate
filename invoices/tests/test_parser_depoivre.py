"""Champagne Depoivre parser tests. Structurally faithful, data invented -
see test_parser_metro.py for why.

Two quirks worth pinning down: only MNT TTC is printed per line, so total_ht
has to be computed (quantity x PU HT) rather than read - the only parser in
the package that does; and the product table and the VAT breakdown table
come out with the SAME column count, so they're told apart by content (a
plain integer quantity in column 0) rather than shape.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.depoivre import DepoivreParser

PRODUCT_TABLE = [
    ["Quantité", None, "Désignation", None, None, None, "PU HT", "PU TTC", "MNT TTC", "Tva"],
    ["12", None, "BOUTEILLE(S) CHAMPAGNE EXEMPLE BRUT 75 CL", None, None, None,
     "13,75 €", "16,50 €", "198,00 €", "A"],
    ["6", None, "BOUTEILLE(S) CHAMPAGNE EXEMPLE ROSE 75 CL", None, None, None,
     "16,00 €", "19,20 €", "115,20 €", "B"],
]
VAT_TABLE = [
    ["Tva", "Libellé", None, "Taux", "Base H.T.", "Montant", None, None, None, None],
    ["A", "TVA à 20 %", None, "20,00", "165,00 €", "33,00 €", None, None, None, None],
    ["B", "TVA à 5,5 %", None, "5,50", "96,00 €", "5,28 €", None, None, None, None],
]
TEXT = """\
EARL DEPOIVRE PERE & FILS
N° ADEME : FR246127801QEKR Commande N° 20250180 du 22/11/2025
Commande N° 20250180 du 22/11/2025 Règlement A réception au 02/12/2025
Tva Libellé Taux Base H.T. Montant
"""

PAGE = PdfPage(text=TEXT, tables=[PRODUCT_TABLE, VAT_TABLE])


def parse(pages=None):
    return DepoivreParser().parse_pages(pages or [PAGE])


class DepoivreParserTests(SimpleTestCase):
    def test_reads_the_product_rows(self):
        self.assertEqual(
            [line.raw_name for line in parse().lines],
            ["BOUTEILLE(S) CHAMPAGNE EXEMPLE BRUT 75 CL", "BOUTEILLE(S) CHAMPAGNE EXEMPLE ROSE 75 CL"],
        )

    def test_total_ht_is_computed_because_only_ttc_is_printed(self):
        line = parse().lines[0]
        self.assertEqual(line.quantity, 12)
        self.assertEqual(line.unit_cost_ht, Decimal("13.75"))
        self.assertEqual(line.total_ht, Decimal("165.00"))  # 12 x 13,75, not the 198,00 TTC

    def test_letter_vat_codes_map_through_the_breakdown_table(self):
        brut, rose = parse().lines
        self.assertEqual(brut.vat_rate, Decimal("0.2"))
        self.assertEqual(rose.vat_rate, Decimal("0.055"))

    def test_vat_breakdown_rows_are_not_mistaken_for_products(self):
        """Same column count as the product table - only the leading integer
        quantity tells them apart."""
        self.assertEqual(len(parse().lines), 2)
        self.assertNotIn("TVA à 20 %", [line.raw_name for line in parse().lines])

    def test_unknown_vat_letter_falls_back_to_zero(self):
        page = PdfPage(text=TEXT, tables=[PRODUCT_TABLE])  # no breakdown table
        self.assertTrue(all(line.vat_rate == Decimal("0") for line in parse([page]).lines))

    def test_order_number_and_date_stand_in_for_the_invoice_number(self):
        """This document has no invoice number of its own."""
        invoice = parse()
        self.assertEqual(invoice.invoice_number, "20250180")
        self.assertEqual(invoice.invoice_date, date(2025, 11, 22))

    def test_date_hint_used_when_no_order_line_is_found(self):
        invoice = DepoivreParser().parse_pages([PdfPage(text="")], date_hint=date(2026, 4, 4))
        self.assertEqual(invoice.invoice_date, date(2026, 4, 4))
        self.assertEqual(invoice.invoice_number, "")
