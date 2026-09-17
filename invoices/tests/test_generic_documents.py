"""The ticket reader on layouts met since the first tables: an electronics
till whose rows print only a product code under the line naming it, a
discount printed twice (TTC, then HT), an invoice and its till ticket on one
photo, a bazaar's till printing HT and tax with no rate, a print shop's rows
already net of the discounts detailed under them, a boutique's invoice whose
HT has four decimals, a count told by the "N ARTICLE(S)" line.

Structure copied from real documents; every name, code and amount invented.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.generic_receipt import GenericReceiptParser, TicketShop, read_line

D = Decimal
READER = GenericReceiptParser(TicketShop("SHOP", (), "Magasin"))


def lines_of(parsed):
    return [(line.raw_name, line.quantity, line.printed_ttc, line.total_ht, line.vat_rate) for line in parsed.lines]


def failed(parsed):
    return [(check.label, check.detail) for check in parsed.checks if not check.passed]


# The row prints the product's code and its figures; the name, and the count,
# are on the line starting with that code above it - with more lines of name,
# or a footnote mark, in between.
LINKED_ROWS = """ELECTRO EXEMPLE  12, RUE INVENTEE
Ticket de caisse 400000001
12/02/2024 / 12H24 Caisse 12 Vendeur 000001
VENTE
Qté Désignation
Code PU TTC  TVA  Total HT  Total
RETRAIT COMPTOIR
1  5550001-ENCEINTE PORTABLE XL
NOIR MAT (1) (2)
5550001  120,00 €  A  100,00 €  120,00 €
Garantie: 24 mois
LIBRE SERVICE
2  5550002-CABLE USB-C 2M
TRESSE BLANC
(1)
5550002  9,99 €  A  16,65 €  19,98 €
Dont éco-part DEEE 0,10€
SOUS TOTAL  139,98 €
TOTAL NET TTC  139,98 €
Carte Bancaire  139,98 €
TAUX  MTHT  MT TVA  MT TTC
A 20,00 %  116,65 € 23,33 €  139,98 €
TOTAL  116,65 € 23,33 €  139,98 €"""

# A discount printed under the item it applies to, TTC, then again in HT.
DISCOUNT_TWICE = """VENTE  PU HT  TVA  PU TTC  TOTAL
Désignation  Qté  Garantie
1112223-GOURDE INOX 1L  1  20,87 €  C  22,02 €  22,02 €
RECHARGE GAZ  1  12,00€  -12,00 €
ECHANGE BOUTEILLE (1)  -11,37 €  C
Remise immédiate
1112223-GOURDE INOX 1L  1  20,87 €  C  22,02 €  22,02 €
1112224-REPRISE BOUTEILLE VIDE  1.  0,00 €  C  0,00 €  0,00 €
SOUS TOTAL  32,04 €
TOTAL NET TTC  32,04 €
DONT TOTAL REMISES  12,00 €
Carte Bancaire  32,04 €
Code  Taux  Mt HT  Mt TVA  Mt TTC
C  5,50 %  30,37 €  1,67 €  32,04 €
Total  30,37 €  1,67 €  32,04 €"""

# The invoice, its rows in HT with a charge included in them between, then
# the till's ticket for the same sale.
INVOICE_AND_TICKET = """FACTURE N° : 100/001/L100001 Date facture : 10/01/2024
Famille Marque Référence  Pv unit HT Qté Montant HT  Date
7000001 CAFETIERE PISTON 1L  50.00  50,00 10/01
Eco-Part DEEE  0,02
7000002 FILTRE PAPIER  4.99  4,99 10/01
Tx TVA  20,00%
Base ht  54,99  Total HT  54,99
Mt TVA  11,00
Total TVA  11,00
TOTAL T.T.C  65,99
Ticket de caisse 400000002
Qté Désignation
Code PU TTC  TVA  Total HT  Total
1  7000001-CAFETIERE PISTON 1L
7000001  60,00 €  A  50,00 €  60,00 €
1  7000002-FILTRE PAPIER
7000002  5,99 €  A  4,99 €  5,99 €
SOUS TOTAL  65,99 €
TOTAL NET TTC  65,99 €
Carte Bancaire  65,99 €
A 20,00 %  54,99 € 11,00 €  65,99 €
TOTAL  54,99 € 11,00 €  65,99 €"""

# The goods' HT and the tax under the item, no rate printed anywhere.
BAZAAR = """VOTRE TICKET
MERCI A BIENTOT
REG  24-01-2024 15:36
CAISSIER01  083205
1 DEPT001  T1  €24.90
HORS TAXE 1  €20.75
TVA1  €4.15
TOTAL  €24.90
ESPECES  €24.90
FACTURE No.  072642"""

# Each row is already net of the discount detailed under it; a group line
# above them is their sum.
PRINT_SHOP = """Ticket n° 93357  01/09/26  17:19
Désignation  Montant HT
8x  Impression simple  30,00
remise commerciale -14,99%
sur 35,29 soit -5,29
12x  Impression couleur A4  10.00
remise commerciale -14,97%
sur 11,76 soit -1,76
8x  Papier blanc 100g inclus  0.00
4x  Plastification  20.00
remise commerciale -15,00%
sur 23,53 soit -3,53
Total HT  30,00
TVA  20,00%  6,00
TOTAL TTC  36,00
Règlement
Carte Bancaire  36,00 €"""

# HT to four decimals: the row says 35,54, the totals 35,55. The payment and
# tax lines after the sub-total are no items.
BOUTIQUE = """LA BOUTIQUE EXEMPLE
FACTURE N°1234
en date du 29/01/2024
Article  P.U.  HT  Rem.  Qté  Mt. HT
Sirop Exemple Velours  Solde
35,54  1  35,54
SOUS TOTAL N°100 du 29/01/2024 (Achat : 37,50 Paiement: 37,50 Solde: 0,00)  35,545
Règlement  Montant €  dont EcoT.  €
Carte  37,50  TOTAL HT  0,00  35,55
Mt HT  €  Taux  Taxe  €  TOTAL TAXE  0,00  1,95
35,55  TVA  5,50%  1,954  TOTAL TTC  0,00  37,50
RESTE A PAYER  0,00
Total  37,50  Total TAXE  1,95  Solde compte client  0,00"""

HERBALIST = """FACTURE
EPICERIE EXEMPLE 10 rue Inventée
Ticket Num. T100001 Caisse N° 01
Date: 11/02/2026 Heure: 14:33
3 Acide citrique 500g  30.00
Total TVA €  1.56
Total TTC €  30.00
2 5.50 Base HT  28.44 TVA  1.56
CARTES  30.00
3 ARTICLE(S)"""


class LinkedRowsTests(SimpleTestCase):
    def test_a_row_takes_the_name_and_count_of_the_line_its_code_starts(self):
        parsed = READER.parse_text(LINKED_ROWS)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc) for line in parsed.lines],
            [("ENCEINTE PORTABLE XL", 1, D("120.00")), ("CABLE USB-C 2M", 2, D("19.98"))],
        )
        self.assertEqual(failed(parsed), [])

    def test_the_other_document_on_the_photo_is_read_when_the_first_adds_up_to_nothing(self):
        parsed = READER.parse_text(INVOICE_AND_TICKET)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc) for line in parsed.lines],
            [("CAFETIERE PISTON 1L", 1, D("60.00")), ("FILTRE PAPIER", 1, D("5.99"))],
        )
        self.assertNotIn("Base ht", [line.raw_name for line in parsed.lines])


class DiscountTests(SimpleTestCase):
    def test_a_discount_printed_again_in_ht_is_taken_once(self):
        parsed = READER.parse_text(DISCOUNT_TWICE)
        self.assertEqual(
            [(line.quantity, line.printed_ttc, line.discount_ttc) for line in parsed.lines],
            [(1, D("22.02"), D("12.00")), (1, D("22.02"), D("0"))],
        )
        self.assertEqual(failed(parsed), [])

    def test_a_row_already_net_of_its_discount(self):
        parsed = READER.parse_text(PRINT_SHOP)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.total_ht, line.vat_rate, line.discount_ttc) for line in parsed.lines],
            [
                ("Impression couleur A4", 12, D("10.00"), D("0.20"), D("0")),
                ("Plastification", 4, D("20.00"), D("0.20"), D("0")),
            ],
        )
        self.assertEqual(failed(parsed), [])

    def test_a_percentage_is_never_an_amount(self):
        self.assertIsNone(read_line(0, "remise commerciale -14,99%").total)


class UnratedTaxUnderTheItemsTests(SimpleTestCase):
    def test_the_items_in_ttc_take_the_rate_their_ht_and_tax_prove(self):
        parsed = READER.parse_text(BAZAAR)
        self.assertEqual(
            [(line.quantity, line.printed_ttc, line.total_ht, line.vat_rate) for line in parsed.lines],
            [(1, D("24.90"), D("20.75"), D("0.20"))],
        )
        self.assertEqual(failed(parsed), [])
        self.assertIn("Taux déduit", [check.label for check in parsed.checks])
        self.assertEqual(parsed.invoice_number, "072642")


class BoutiqueInvoiceTests(SimpleTestCase):
    def test_the_row_is_the_purchase_not_the_payment(self):
        parsed = READER.parse_text(BOUTIQUE)
        (line,) = parsed.lines
        self.assertNotIn("Carte", line.raw_name)
        self.assertIn("Sirop Exemple Velours", line.raw_name)
        self.assertEqual((line.quantity, line.vat_rate), (1, D("0.055")))
        self.assertLessEqual(abs(line.total_ht - D("35.55")), D("0.01"))
        self.assertEqual(failed(parsed), [])


class CountTests(SimpleTestCase):
    def test_a_dimension_is_not_a_count(self):
        reading = read_line(0, "412233  PLATINE 100X35X2.5  3000000001011  i  3,25  0,00  3,25  3,25  20,00")
        self.assertIsNone(reading.count)
        self.assertEqual(read_line(0, "4X BASILIC  2,00").count, 4)
        self.assertEqual(read_line(0, "EAU 6X1L  2,00").count, None)

    def test_the_number_of_articles_printed_tells_a_count_before_a_name(self):
        parsed = READER.parse_text(HERBALIST)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc) for line in parsed.lines],
            [("Acide citrique 500g", 3, D("30.00"))],
        )

    def test_a_number_in_front_of_a_name_stays_when_the_articles_say_otherwise(self):
        parsed = READER.parse_text(HERBALIST.replace("3 ARTICLE(S)", "1 ARTICLE(S)"))
        self.assertEqual(
            [(line.raw_name, line.quantity) for line in parsed.lines], [("3 Acide citrique 500g", 1)]
        )


# A phone bill: its totals first, its detail below them, each amount printed
# in TTC with its HT in brackets - and its date spelled out.
PHONE_BILL = """SiteInternet:mobile.exemple.fr
Forfait Exemple 5G  AU DIPSO
N de ligne:0600000000  JEAN EXEMPLE
Facture no 1000000001 du 19 mai 2026
Total de la facture HT  8.33
TVA [20.00%]  1.66
Somme a payer TTC*  9.99
*Cette somme sera prelevee sur votre compte le 20-05-2026.
Details de votre facture
Services (Total : 9.99  TTC)
Abonnements, forfaits et options du 19-05 au 18-06
Abonnement "Forfait Exemple 5G"  19.99 (16.66)
Remise abonne - Famille  -10.00 (-8.33)
Communications incluses :
Appels depuis la France metropolitaine  1 h 13 min 47 s  0.00 (0.00)"""


class TotalsFirstTests(SimpleTestCase):
    def test_the_detail_under_the_totals_is_the_purchase_when_it_makes_them_both(self):
        parsed = READER.parse_text(PHONE_BILL)
        paid = sum(line.printed_ttc - line.discount_ttc for line in parsed.lines)
        self.assertEqual((paid, parsed.printed_total_ttc, parsed.invoice_number), (D("9.99"), D("9.99"), "1000000001"))
        self.assertEqual({line.vat_rate for line in parsed.lines}, {D("0.20")})
        self.assertEqual(failed(parsed), [])

    def test_the_date_it_spells_out_comes_before_the_day_it_is_debited(self):
        self.assertEqual(READER.parse_text(PHONE_BILL).invoice_date, date(2026, 5, 19))

    def test_an_amount_in_brackets_that_is_the_same_one_in_ht(self):
        self.assertEqual(read_line(0, 'Abonnement "Forfait Exemple 5G"  19.99 (16.66)').total, D("19.99"))
        # Not a rate apart, so two amounts.
        self.assertEqual(read_line(0, "ARTICLE  12,00 (9,00)").total, D("9.00"))

    def test_a_total_restated_under_the_rows_is_no_row(self):
        """"Total de la facture HT 8.33" makes the HT base on its own: it is
        the document's own figure, not a line of it."""
        parsed = READER.parse_text(PHONE_BILL)
        self.assertNotIn("Total de la facture HT", [line.raw_name for line in parsed.lines])
