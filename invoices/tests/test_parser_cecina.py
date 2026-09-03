"""Cecina parser tests. Structurally faithful, data invented - see
test_parser_metro.py for why.

Cecina's one real quirk is the "buy N get M free" offer, printed as two
consecutive lines sharing a product code: one with a price, one with only a
quantity. Both have to be folded into a single line, so the free bottles
raise the quantity without raising the amount paid - which is exactly what
makes the average unit cost come out right.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.cecina import CecinaParser

# Columns: Référence Désignation Unité Quantité [PrixUnitaire MontantHT] CodeTVA
TEXT = """\
E.U.R.L CECINA
FACTURE
Référence : 12949
Date : 31/03/26
Prix % Montant Code
Référence Désignation Unité Quantité
Unitaire Rem. H.T. TVA
-Bon de livraison N° 17001 du 27/03/2026
LIBV325 Le Vin Exemple Blanc IGP Oc 2025 Unité 11 5,83 64,13 4
LIBV325 Le Vin Exemple Blanc IGP Oc 2025 Unité 1 4
BLAHAU325 Le Vin Exemple Rouge AOP Languedoc 2025 Unité 12 3,68 44,16 4
Sous-Total 108,29
TVA
Code Taux Montant
0
1
2
3
4 20,00 21,66
Total TVA: 21,66
"""

PAGE = PdfPage(text=TEXT)


def parse(pages=None):
    return CecinaParser().parse_pages(pages or [PAGE])


def line_named(invoice, name):
    return next(line for line in invoice.lines if line.raw_name == name)


class CecinaParserTests(SimpleTestCase):
    def test_parses_product_rows(self):
        invoice = parse()
        self.assertEqual(
            sorted(line.raw_name for line in invoice.lines),
            ["Le Vin Exemple Blanc IGP Oc 2025", "Le Vin Exemple Rouge AOP Languedoc 2025"],
        )

    def test_plain_row(self):
        line = line_named(parse(), "Le Vin Exemple Rouge AOP Languedoc 2025")
        self.assertEqual(line.quantity, 12)
        self.assertEqual(line.total_ht, Decimal("44.16"))
        self.assertEqual(line.unit_cost_ht, Decimal("3.6800"))
        self.assertEqual(line.ean, "BLAHAU325")

    def test_free_bonus_bottles_raise_quantity_but_not_the_amount_paid(self):
        line = line_named(parse(), "Le Vin Exemple Blanc IGP Oc 2025")
        self.assertEqual(line.quantity, 12)  # 11 paid + 1 free
        self.assertEqual(line.total_ht, Decimal("64.13"))
        # ...so the real average cost is lower than the printed unit price.
        self.assertEqual(line.unit_cost_ht, (Decimal("64.13") / 12).quantize(Decimal("0.0001")))
        self.assertLess(line.unit_cost_ht, Decimal("5.83"))

    def test_vat_rate_comes_from_the_footer_code_table(self):
        """Only codes actually printed with a taux/montant are used; the bare
        "0"/"1"/"2"/"3" lines in the same table are unused codes."""
        self.assertEqual(line_named(parse(), "Le Vin Exemple Rouge AOP Languedoc 2025").vat_rate, Decimal("0.2"))

    def test_unknown_vat_code_falls_back_to_zero_rather_than_guessing(self):
        page = PdfPage(text="X1 Un Vin Exemple Unité 6 5,00 30,00 9\n")
        self.assertEqual(parse([page]).lines[0].vat_rate, Decimal("0"))

    def test_rows_whose_unit_is_not_a_product_unit_are_skipped(self):
        """The footer's own "4 20,00 21,66" has the same numeric shape as a
        product row; the Unité column is what tells them apart."""
        page = PdfPage(text="AAA Quelque chose Palette 3 1,00 3,00 4\n")
        self.assertEqual(parse([page]).lines, [])

    def test_invoice_number_and_date(self):
        invoice = parse()
        self.assertEqual(invoice.invoice_number, "12949")
        self.assertEqual(invoice.invoice_date, date(2026, 3, 31))  # two-digit year

    def test_date_hint_used_when_the_page_prints_no_date(self):
        invoice = CecinaParser().parse_pages([PdfPage(text="")], date_hint=date(2026, 7, 1))
        self.assertEqual(invoice.invoice_date, date(2026, 7, 1))
