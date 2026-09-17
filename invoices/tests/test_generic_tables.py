"""The ticket reader on documents laid out as tables: an invoice printed at a
DIY store's till (a code, the name, the EAN, a quantity that can be a
fraction, prices before and after discount, the HT total and the rate), and a
web shop's invoice in HT whose VAT table prints its rate without "%".

Structure copied from real documents; every name, code and amount invented.
The EANs are made up but carry valid check digits, as printed ones do.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.generic_receipt import (
    GenericReceiptParser,
    TicketShop,
    is_gtin,
    read_line,
)
from invoices.parsers.receipt_base import (
    collect_vat_summaries,
    line_amounts,
    printed_total,
)

D = Decimal
READER = GenericReceiptParser(TicketShop("BRICO", (), "Brico"))

# An invoice with the till's ticket stapled beside it, photographed together:
# the invoice's rows in HT, then the ticket's in TTC.
DIY_INVOICE = """NO AUTO:12345
MONTANT  TICKET CLIENT
16,21 EUR  CONSERVER
FACTURE N° P5200000012345
DEBIT  Date de facturation : 08/01/2024
Brico Exemple  APPLICATION : Unimag
VERSION: 2023.11.9.4 (-bMh)
N° SIREN:123456789
N° TVA : FR00123456789
PRIX  REMISE  PRIX  TAUX
UNITAIRE  TOTAL HT  TVA
CODE  DÉSIGNATION  GENCOD  QUANTITÉ  UNITAIRE  UNITAIRE  HT REMISÉ  REMISÉ(€)
TICKET 2790300012345 DU 08/01/2024 10:26:09  Total Ticket TTC : 16,21 €  Montant soldé TTC : 16,21 €
412233  VIS INOX 40MM BTE50  3000000001011  1  3,50  0,00  3,50  3,50  20,00
800112  DECOUPE BOIS  2000000002026  2  1,25  0,00  1,25  2,50  20,00
3100200  PLANCHE PIN 18MM 1M2  2600000003032  0,35  21,45  0,00  21,45  7,51  20,00
Ventilation par taux de TVA
TVA  Montant HT  Montant TVA
20,00 %  13,51 €  2,70 €  Total HT  13,51 €
Total  13,51 €  2,70 €  Montant TVA  2,70 €
Total TTC  16,21 €
CB  16,21 €
Net à payer  0,00 €
Conformément à larticle L 441-6 du code de commerce (calculées au taux de 0,75 % par mois).
2600000003032 PLANCHE PIN 18MM 1M2
Qté : 0,350 * 25,74  9,01
3000000001011 VIS INOX 40MM BTE50  4,20  FRANCE
800112 DECOUPE BOIS  3,00
TOTAL TTC (€)  16,21
(1)Dont TVA à 20,00%  :(€)(  2,70)
CB  (€)  16,21
08/01/2024 10:26:09"""

# A web shop's invoice, from its text layer: amounts in HT, a delivery fee,
# a VAT table whose rate is a plain "20,00".
WEB_INVOICE = """Numéro Facture :  1234567
Date Facture :  07/11/2024
CUISIPRO FRANCE SARL,  Tel: 01 23 45 67 89  Numéro Client :  765432
VAT No: FR00123456789
FACTURE
PRODUIT  DESCRIPTION  QTÉ  PRIX UNITAIRE  VALEUR
Produits livrés sur bon de livraison:  1234567
WEBF -AB123  Verre à shot Olympia (lot de 12)  8  3,50  28,00
R
WEBF -CD456  Pince à glaçons inox 20cm  2  4,25  8,50
R
Frais divers
Transport Charges  9,95
TOTAL HT  EURO  46,45
EURO (€) Application de la TVA
Total HT  Code  Taux  TVA
TVA  9,29
46,45  STDFR  20,00  9,29
TOTAL TTC  EURO  55,74
RIB : BANQUE EXEMPLE - IBAN : FR76 0000 0000 0000 0000 0000 000
SIRET:  123 456 789 00012"""
# The IBAN a web shop prints under its totals is a long run of digits too.
WEB_INVOICE_WITH_IBAN = WEB_INVOICE.replace(
    "RIB : BANQUE EXEMPLE - IBAN : FR76 0000 0000 0000 0000 0000 000",
    "RIB : BANQUE EXEMPLE - IBAN : FR76 3000 4000 1234 5678 9012 345 - BIC : BANKFRPPXXX",
)


def lines_of(parsed):
    return [(line.raw_name, line.quantity, line.total_ht, line.vat_rate, line.ean) for line in parsed.lines]


class PiecesTests(SimpleTestCase):
    def test_an_ean_is_told_by_its_check_digit(self):
        self.assertTrue(is_gtin("3000000001011"))
        self.assertTrue(is_gtin("96385074"))  # EAN-8
        self.assertFalse(is_gtin("3000000001012"))
        self.assertFalse(is_gtin("2790300012345"))  # a ticket number
        self.assertFalse(is_gtin("12345"))

    def test_a_version_number_is_not_money(self):
        self.assertEqual(line_amounts("VERSION: 2023.11.9.4 (-bMh)"), [])
        self.assertEqual(line_amounts("TOTAL  12,50"), [D("12.50")])
        self.assertEqual(line_amounts("2.80  11.20"), [D("2.80"), D("11.20")])
        self.assertIsNone(read_line(0, "VERS10N: 2023.11.9.4 (Lqux)").total)
        self.assertEqual(read_line(0, "SAC  2.80.11.20").total, D("11.20"))

    def test_a_table_row_gives_its_count_price_amount_rate_and_ean(self):
        reading = read_line(0, "412233  VIS INOX 40MM BTE50  3000000001011  1  3,50  0,00  3,50  3,50  20,00")
        self.assertEqual(
            (reading.name, reading.count, reading.unit, reading.total, reading.row_rate, reading.ean),
            ("VIS INOX 40MM BTE50", 1, D("3.50"), D("3.50"), D("0.20"), "3000000001011"),
        )

    def test_a_fraction_of_a_unit_is_a_quantity(self):
        reading = read_line(0, "3100200  PLANCHE PIN 18MM 1M2  2600000003032  0,35  21,45  0,00  21,45  7,51  20,00")
        self.assertEqual((reading.count, reading.unit, reading.total), (D("0.35"), D("21.45"), D("7.51")))
        detail = read_line(1, "Qté : 0,350 * 25,74  9,01")
        self.assertEqual((detail.name, detail.count, detail.unit, detail.total), (None, D("0.350"), D("25.74"), D("9.01")))

    def test_a_rounded_unit_price_still_explains_its_amount(self):
        reading = read_line(0, "2149483  DECOUPE  2000000002026  2  4,17  0,00  4,17  8,33  20,00")
        self.assertEqual((reading.count, reading.total), (2, D("8.33")))

    def test_a_vat_row_printing_its_rate_as_a_number(self):
        lines = WEB_INVOICE.split("\n")
        (summary,), _rates = collect_vat_summaries(lines, None)
        self.assertEqual((summary.rate, summary.base, summary.vat_amount), (D("0.20"), D("46.45"), D("9.29")))
        self.assertEqual(printed_total(lines), D("55.74"))

    def test_a_rate_number_is_a_vat_row_only_when_the_document_prints_its_total(self):
        lines = ["1  12,00  2,00  10,00  10,00  20,00", "TOTAL  10,00"]
        self.assertEqual(collect_vat_summaries(lines, None), ([], []))


class DiyInvoiceTests(SimpleTestCase):
    def setUp(self):
        self.parsed = READER.parse_text(DIY_INVOICE)

    def test_the_invoice_rows_are_the_purchase_in_ht(self):
        self.assertEqual(
            lines_of(self.parsed),
            [
                ("VIS INOX 40MM BTE50", 1, D("3.50"), D("0.20"), "3000000001011"),
                ("DECOUPE BOIS", 2, D("2.50"), D("0.20"), "2000000002026"),
                ("PLANCHE PIN 18MM 1M2", D("0.35"), D("7.51"), D("0.20"), "2600000003032"),
            ],
        )

    def test_the_documents_own_figures_are_found(self):
        self.assertEqual((self.parsed.printed_total_ttc, self.parsed.invoice_date), (D("16.21"), date(2024, 1, 8)))
        self.assertEqual(self.parsed.invoice_number, "P5200000012345")

    def test_every_check_passes(self):
        failed = [(check.label, check.detail) for check in self.parsed.checks if not check.passed]
        self.assertEqual(failed, [])
        self.assertEqual(sum(line.total_ht for line in self.parsed.lines), D("13.51"))

    def test_a_row_in_ht_prints_no_ttc(self):
        self.assertEqual({line.printed_ttc for line in self.parsed.lines}, {None})


class WebInvoiceTests(SimpleTestCase):
    def setUp(self):
        self.parsed = READER.parse_text(WEB_INVOICE)

    def test_the_rows_and_the_fee_are_the_purchase_in_ht(self):
        self.assertEqual(
            [(line.raw_name, line.quantity, line.total_ht, line.vat_rate) for line in self.parsed.lines],
            [
                ("WEBF -AB123 Verre à shot Olympia (lot de 12)", 8, D("28.00"), D("0.20")),
                ("WEBF -CD456 Pince à glaçons inox 20cm", 2, D("8.50"), D("0.20")),
                ("Transport Charges", 1, D("9.95"), D("0.20")),
            ],
        )

    def test_an_iban_is_not_the_invoice_number(self):
        self.assertEqual(READER.parse_text(WEB_INVOICE_WITH_IBAN).invoice_number, "1234567")

    def test_the_tax_is_not_an_item(self):
        self.assertNotIn("TVA", [line.raw_name for line in self.parsed.lines])

    def test_every_check_passes_and_the_total_is_the_ttc(self):
        failed = [(check.label, check.detail) for check in self.parsed.checks if not check.passed]
        self.assertEqual(failed, [])
        self.assertEqual(
            (self.parsed.printed_total_ttc, self.parsed.invoice_date, self.parsed.invoice_number),
            (D("55.74"), date(2024, 11, 7), "1234567"),
        )


# A web shop's order page, captured from the screen: the rows in HT, then the
# goods' value and the tax, and no rate printed anywhere.
ORDER_PAGE = """Details de la commande FR10000001  Poser une question
Référence de la commande : 1000001
Référence Web : FR10000001
Date de la commande : 14/03/2025
Méthode de commande : Web Order
Valeur de la commande : 61,56 €
Statut : COMPLETE
Produit  Quantité  Prix unitaire  Sous total
Cliquez ici pour suivre votre colis: 1Z000AA00000000000
NAPPE PAPIER 120CM X 25M
1  21,45 €  21,45 €
AB001
Copie De La Commande Au Panier
VERRE A SHOT (LOT DE 12)  2  7,35 €  14,70 €
AB002
Copie De La Commande Au Panier
TAPIS DE BAR
5  3,03 €  15,15 €
AB003
Copie De La Commande Au Panier
valeur des marchandises  51,30 €
Web Transport  GRATUIT
Total TVA  10,26 €
Total de la commande  61,56 €"""


class UnratedTaxTests(SimpleTestCase):
    def test_rows_in_ht_under_a_tax_printed_without_its_rate(self):
        """The goods' value and the tax make what was paid, and the tax is
        that value at 20%: the rows adding up to that value are in HT."""
        parsed = READER.parse_text(ORDER_PAGE)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.total_ht, line.vat_rate) for line in parsed.lines],
            [
                ("NAPPE PAPIER 120CM X 25M", 1, D("21.45"), D("0.20")),
                ("VERRE A SHOT (LOT DE 12)", 2, D("14.70"), D("0.20")),
                ("TAPIS DE BAR", 5, D("15.15"), D("0.20")),
            ],
        )
        self.assertEqual((parsed.printed_total_ttc, parsed.invoice_date), (D("61.56"), date(2025, 3, 14)))
        failed = [(check.label, check.detail) for check in parsed.checks if not check.passed]
        self.assertEqual(failed, [])
        (note,) = [check for check in parsed.checks if check.label == "Taux déduit"]
        self.assertIn("20 %", note.detail)

    def test_two_items_the_second_a_fifth_of_the_first_prove_no_rate(self):
        parsed = READER.parse_text("EPICERIE\nPAIN  10,00\nBEURRE  2,00\nTOTAL  12,00\nCB  12,00")
        self.assertEqual([(line.printed_ttc, line.vat_rate) for line in parsed.lines], [
            (D("10.00"), D("0.055")), (D("2.00"), D("0.055")),
        ])
        self.assertIn("Taux par article", [check.label for check in parsed.checks if not check.passed])
        self.assertNotIn("Taux déduit", [check.label for check in parsed.checks])

    def test_a_ticket_in_ttc_printing_its_ht_and_tax_stays_in_ttc(self):
        parsed = READER.parse_text("EPICERIE\nVIN  7,00\nSAVON  5,00\nTOTAL HT  10,00\nTVA  2,00\nTOTAL  12,00\nCB  12,00")
        self.assertEqual([line.printed_ttc for line in parsed.lines], [D("7.00"), D("5.00")])
