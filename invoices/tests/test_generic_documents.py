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
from invoices.parsers.receipt_base import amount_candidates, line_amounts

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
        self.assertEqual([(line.raw_name, line.quantity) for line in parsed.lines], [("3 Acide citrique 500g", 1)])


# A phone bill: its totals first, its detail below them, each amount printed
# in TTC with its HT in brackets - and its date spelled out.
PHONE_BILL = """SiteInternet:mobile.exemple.fr
Forfait Exemple 5G  AU COMPTOIR
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
        """ "Total de la facture HT 8.33" makes the HT base on its own: it is
        the document's own figure, not a line of it."""
        parsed = READER.parse_text(PHONE_BILL)
        self.assertNotIn("Total de la facture HT", [line.raw_name for line in parsed.lines])


# A water bill: every row prints its own tax, so the subscription's row is a
# VAT row of its own - and its amount is printed again at the top, beside
# "Votre abonnement". The bill's real table is at the foot, fifty lines
# below, with its base printed after its tax.
WATER_BILL = """EAU EXEMPLE  AU DISPO
Votre reference contrat  1000000
FACTURE TRIMESTRIELLE
N 2026100000001 DU 3 JUILLET 2026
200  Votre abonnement  21,10€ TTC
100  Votre consommation  50 m3  96,75€ TTC
Total  117,85€ TTC
Solde anterieur  0,00€ TTC
Montant net a prelever  117,85€ TTC
Le montant de cette facture sera preleve automatiquement le 17 juillet 2026.
VOTRE FACTURE
Compteur n  Ancien index  Nouvel index  Consommation
I00AA000000  ESTIME LE 04/04/26  ESTIME LE 26/06/26  50m3
EN DETAIL
Quantite  Prix unitaire  Montant  Taux  Montant TVA  Montant
€ HT  € HT  TVA  €  € TTC
PRODUCTION ET DISTRIBUTION DE L'EAU  70,00  3,85  73,85
ABONNEMENT
Abonnement pour compteur - O20  du 01/04/26 au 30/06/26  1  20,0000  20,00  5,5%  1,10  21,10
CONSOMMATION
Fourniture d'eau potable  du 04/04/26 au 26/06/26  50  m3  1,0000  50,00  5,5%  2,75  52,75
COLLECTE ET TRAITEMENT DES EAUX USEES  40,00  4,00  44,00
Collecte des eaux usees  du 04/04/26 au 26/06/26  50  m3  0,6000  30,00  10,0%  3,00  33,00
Transport et epuration  du 04/04/26 au 26/06/26  50  m3  0,2000  10,00  10,0%  1,00  11,00
TOTAL  110,00  7,85  117,85
SOLDE ANTERIEUR  0,00
MONTANT NET A PRELEVER  117,85
Prix au litre TTC (hors abonnement): 0,00200 €  TVA :acquittee sur les debits
* EXEMPLE : SYNDICAT INTERDEPARTEMENTAL  Dont TVA 5,5 % : 3,85 € sur la base de 70,00 €
Dont TVA 10,0 % : 4,00 € sur la base de 40,00 €
2/2"""

# The same bill a quarter later, over a thousand euros, with what was still
# owed from the quarter before printed under its total.
WATER_BILL_WITH_BALANCE = (
    WATER_BILL.replace("Solde anterieur  0,00€ TTC", "Solde anterieur  171,37€ TTC")
    .replace("Montant net a prelever  117,85€ TTC", "Montant net a payer  1 289,22€ TTC")
    .replace("SOLDE ANTERIEUR  0,00", "SOLDE ANTERIEUR  171,37")
    .replace("MONTANT NET A PRELEVER  117,85", "MONTANT NET A PAYER  1 289,22")
    .replace(
        "Collecte des eaux usees  du 04/04/26 au 26/06/26  50  m3  0,6000  30,00  10,0%  3,00  33,00",
        "Collecte des eaux usees  du 04/04/26 au 26/06/26  50  m3  20,6000  1 030,00  10,0%  103,00  1 133,00",
    )
    .replace(
        "COLLECTE ET TRAITEMENT DES EAUX USEES  40,00  4,00  44,00",
        "COLLECTE ET TRAITEMENT DES EAUX USEES  1 040,00  104,00  1 144,00",
    )
    .replace(
        "Dont TVA 10,0 % : 4,00 € sur la base de 40,00 €",
        "Dont TVA 10,0 % : 104,00 € sur la base de 1 040,00 €",
    )
    .replace("TOTAL  110,00  7,85  117,85", "TOTAL  1 110,00  107,85  1 217,85")
    .replace("Total  117,85€ TTC", "Total  1 217,85€ TTC")
    .replace("100  Votre consommation  50 m3  96,75€ TTC", "100  Votre consommation  50 m3  1 196,75€ TTC")
)


class WaterBillTests(SimpleTestCase):
    def test_a_rows_own_tax_does_not_stand_for_the_whole_bill(self):
        """The subscription's row is a bucket at 5,5% of 21,10 and its amount
        is printed twice; the bill's own table says 5,5% of 73,85. A bucket
        covers the document, so the larger reading is the bill's."""
        parsed = READER.parse_text(WATER_BILL)
        self.assertEqual(parsed.printed_total_ttc, D("117.85"))
        self.assertEqual(
            sorted((rate, base, tax) for rate, base, tax in parsed.vat_breakdown),
            [(D("0.055"), D("70.00"), D("3.85")), (D("0.100"), D("40.00"), D("4.00"))],
        )

    def test_a_thousands_separator_is_not_a_second_amount(self):
        """Read as "030,00", the bill's own table no longer added up and the
        1 217,85 € charged was filed as 217,85 €."""
        parsed = READER.parse_text(WATER_BILL_WITH_BALANCE)
        self.assertEqual(parsed.printed_total_ttc, D("1217.85"))

    def test_what_was_owed_before_is_not_part_of_this_bill(self):
        """ "MONTANT NET A PAYER 1 289,22" is this quarter plus the last
        quarter's unpaid balance: the document is worth what it charges."""
        self.assertEqual(READER.parse_text(WATER_BILL_WITH_BALANCE).printed_total_ttc, D("1217.85"))


# A phone bill whose sundry services are subtotalled ("Total : 2.97") and
# printed again on their own line, while the amount charged is printed once.
PHONE_BILL_WITH_EXTRAS = (
    PHONE_BILL.replace("Total de la facture HT  8.33", "Total de la facture HT  10.80")
    .replace("TVA [20.00%]  1.66", "TVA [20.00%]  2.16")
    .replace("Somme a payer TTC*  9.99", "Somme a payer TTC*  12.96")
    + """
Services fournis par des tiers (Total : 2.97  TTC ( 2.48 HT ))
SMS+  6  2.97 (2.48)"""
)


class TaxPrintedBesideItsRateTests(SimpleTestCase):
    def test_the_total_is_the_ht_the_printed_tax_belongs_to(self):
        """Nothing here is printed twice but the 2,97 of texts, which the
        bill charges among the 12,96 it debits."""
        parsed = READER.parse_text(PHONE_BILL_WITH_EXTRAS)
        self.assertEqual(parsed.printed_total_ttc, D("12.96"))

    def test_a_tax_that_could_belong_to_two_amounts_proves_nothing(self):
        """20% of 10,80 is 2,16 and so is 20% of 10,81: with both printed,
        and both sums printed too, the document says nothing."""
        ambiguous = """FACTURE
Total HT  10.80
Autre montant  10.81
TVA [20.00%]  2.16
A payer  12.96
Deja regle  12.97"""
        self.assertIsNone(READER.parse_text(ambiguous).printed_total_ttc)


class ThousandsSeparatorTests(SimpleTestCase):
    def test_a_space_between_three_digits_and_a_decimal_part_groups_thousands(self):
        self.assertEqual(line_amounts("MONTANT NET A PAYER  1 011,00"), [D("1011.00")])
        self.assertEqual(line_amounts("TOTAL  936,59  74,41  1 011,00"), [D("936.59"), D("74.41"), D("1011.00")])
        self.assertEqual(amount_candidates("TOTAL  1 011,00"), [D("1011.00")])

    def test_two_amounts_in_neighbouring_columns_stay_two(self):
        """ "10.49 31.47" is what a price column and the amount beside it look
        like once the space between them is all that separates them."""
        self.assertEqual(line_amounts("PAIN COMPLET  10.49 31.47"), [D("10.49"), D("31.47")])
        self.assertEqual(line_amounts("PAIN COMPLET  10.49 314.47"), [D("10.49"), D("314.47")])

    def test_a_group_of_two_or_four_digits_is_not_a_thousands_group(self):
        self.assertEqual(line_amounts("CAISSE 12  1 04,50"), [D("4.50")])
        self.assertEqual(line_amounts("TICKET  2 0110,00"), [D("110.00")])


# A wine grower's invoice, printing each price and each amount both ways -
# HT and TTC - with no tax column, its rate last on the row. Two suppliers
# had a parser of their own for this until the reader could do it: what they
# knew is in these two fixtures. Structure copied; every name, address,
# code, date, count and amount invented.
WINE_INVOICE = """SCEA EXEMPLE ET FILS  FACTURE
7 Rue Inventee
00000, Exempleville, France  Date: 22/03/2026
contact@exemple.fr  N° document: FA-202603-0001
Date de livraison: 08/03/2026
Adresse de livraison:  Adresse de facturation:
Societe AU COMPTOIR  Societe AU COMPTOIR
140 RUE DES LILAS  140 RUE DES LILAS
Désignation  Qté  Px U. HT  Px U. TTC  HT  TTC  Taux
CUVEE DES EXEMPLES - - AOP EXEMPLE  11  28,35€  34,02€  311,85€  374,22€  20,00%
JUS DE RAISIN EXEMPLE - - REGION EXEMPLE  10  29,18€  30,78€  291,80€  307,85€  5,50%
Nombre de produits: 21  Nombre de colis: 4  Volume total: 23.1L  Poids total: 38.61kg
Libellé  Hors taxe  TVA  TTC  Montant total HT  603,65€
Taux 20.00%  311,85€  62,37€  374,22€
Taux 5.50%  291,80€  16,05€  307,85€
Total net HT  603,65€
Règlement  Total TVA  78,42€
Virement - A 30 jours
Montant total TTC  682,07€
RIB  Net à payer  682,07€
Titulaire du compte: SCEA EXEMPLE ET FILS
IBAN: FR76 1234 5678 9012 3456 7890 123
BIC: EXMPFRPP123
SIREN : 912345675 - SIRET : 91234567500017 - TVA : FR65912345675 - NAF : 00.00Z
SCEA EXEMPLE ET FILS  FA-202603-0001 - LE COMPTOIR - AU COMPTOIR  Page 1/1"""

# The same grower's older layout: no "Taux" column, its rows priced with a
# decimal point, and its reference under another name on some invoices
# ("Référence interne").
WINE_INVOICE_2024 = """FACTURE
Date: 14/10/2024
N° document: FA-202410-0001
Désignation  Qté  Px U. HT (hors droits)  Px U. TTC  HT  TTC
CUVEE DES EXEMPLES - - AOP EXEMPLE  7  44.45€  53.34€  311.15€  373.38€
PETILLANT EXEMPLE - - AOP EXEMPLE  3  33.55€  40.26€  100.65€  120.78€
Nombre de produits: 10
Libellé  Hors taxe  TVA  TVA réglée  TTC  Montant total HT  411,80€
Taux 20.00%  411,80€  82,36€  0,00€  494,16€
Total net HT  411,80€
Montant total TTC  494,16€
IBAN: FR76 1234 5678 9012 3456 7890 123"""


class WineInvoiceTests(SimpleTestCase):
    def test_a_row_printing_both_ways_keeps_its_count(self):
        """Nothing adds up on the row - it prints no tax - so the count is
        proved by one rate turning both prices into both amounts. Read as a
        bare list of amounts, eleven bottles came out as one at 311,85. The
        juice at 5,5 % proves the rule works in HT: ten at 30,78 TTC would
        be 307,80, and the row prints 307,85 - ten times 29,18 HT, taxed."""
        parsed = READER.parse_text(WINE_INVOICE)
        self.assertEqual(
            lines_of(parsed),
            [
                ("CUVEE DES EXEMPLES - - AOP EXEMPLE", 11, D("374.22"), D("311.85"), D("0.20")),
                ("JUS DE RAISIN EXEMPLE - - REGION EXEMPLE", 10, D("307.85"), D("291.80"), D("0.055")),
            ],
        )
        self.assertEqual((parsed.printed_total_ttc, parsed.invoice_date), (D("682.07"), date(2026, 3, 22)))
        self.assertEqual(failed(parsed), [])

    def test_the_unit_price_is_per_bottle(self):
        """What every cost downstream is divided by: 28,35 € the bottle, not
        311,85 € the eleven."""
        self.assertEqual(next(line.unit_cost_ht for line in READER.parse_text(WINE_INVOICE).lines), D("28.35"))

    def test_the_document_number_is_its_own_and_never_the_iban(self):
        """An IBAN is a long digit run once its spaces are gone, and the same
        one every month: read as the number, the next invoice was refused as
        a duplicate of this one."""
        self.assertEqual(READER.parse_text(WINE_INVOICE).invoice_number, "FA-202603-0001")
        self.assertEqual(READER.parse_text(WINE_INVOICE_2024).invoice_number, "FA-202410-0001")
        self.assertEqual(
            READER.parse_text(WINE_INVOICE_2024.replace("N° document:", "Référence interne:")).invoice_number,
            "FA-202410-0001",
        )

    def test_the_older_layout_without_a_rate_column(self):
        parsed = READER.parse_text(WINE_INVOICE_2024)
        self.assertEqual(
            [(line.quantity, line.total_ht) for line in parsed.lines],
            [(7, D("311.15")), (3, D("100.65"))],
        )
        self.assertEqual(parsed.printed_total_ttc, D("494.16"))


# One row making what was paid on its own, then the document's base and tax
# on lines of their own - which make it too - and its VAT table.
ONE_ROW_THEN_BASE_AND_TAX = """FACTURE N° 2025-0042 du 05/02/2025
Carton exemple  5  x  48,00  240,00
Total net HT  200,00
Total TVA  40,00
Taux 20%  200,00  40,00  240,00
Net a payer  240,00"""


# Two rates, one row each, and each rate's base and tax on lines of their
# own: four lines making what was paid, and the second row with the first
# rate's base and tax making it too.
TWO_RATES_ONE_ROW_EACH = """FACTURE N° 2025-0044 du 05/02/2025
Jus de pomme  2  x  5,60  11,20
Verres  2  x  6,10  12,20
Base HT A  10,62
Montant taxe A  0,58
Base HT B  10,17
Montant taxe B  2,03
TVA 5,5%  10,62  0,58  11,20
TVA 20%  10,17  2,03  12,20
Total TTC  23,40"""


class OneRowAboveItsBaseAndTaxTests(SimpleTestCase):
    def test_the_row_is_the_purchase_when_the_base_and_the_tax_make_the_total_too(self):
        """The longest run adding up to what was paid is "Total net HT" and
        "Total TVA": the document's own base and tax, refused. The search
        stopped there, and a repair priced the row at 0,00 € beside the two
        totals filed as articles."""
        parsed = READER.parse_text(ONE_ROW_THEN_BASE_AND_TAX)
        self.assertEqual(lines_of(parsed), [("Carton exemple", 5, D("240.00"), D("200.00"), D("0.20"))])
        self.assertEqual(failed(parsed), [])

    def test_a_line_alone_restating_the_total_is_no_purchase_and_is_said(self):
        """Past the refused base and tax, a line has to read as a row - a
        count, a unit price, what makes its amount: "TOTAL 240,00" read as the
        first article is the total restated. Nothing else making what was
        paid, the document fails its checks - it used to pass them all, the
        "TOTAL" line repaired to 0,00 € beside the base and the tax."""
        parsed = READER.parse_text(
            ONE_ROW_THEN_BASE_AND_TAX.replace("Carton exemple  5  x  48,00  240,00", "TOTAL  240,00")
        )
        self.assertTrue(failed(parsed))
        self.assertNotIn(D("0.00"), [line.printed_ttc for line in parsed.lines])
        self.assertNotEqual([line.raw_name for line in parsed.lines], ["TOTAL"])

    def test_no_amount_is_repaired_down_to_nothing(self):
        """A line alone that does not read as a row: the base and the tax read
        as lines make the total, so zeroing the purchase was the one repair
        that added up - « Location de salle » filed at 0,00 € and two
        articles « Total net HT » and « Total TVA », every check passing. No
        line read ever keeps an amount of 0: that repair is none."""
        parsed = READER.parse_text(
            ONE_ROW_THEN_BASE_AND_TAX.replace("Carton exemple  5  x  48,00  240,00", "Location de salle  240,00")
        )
        self.assertTrue(failed(parsed))
        self.assertNotIn(D("0.00"), [line.printed_ttc for line in parsed.lines])
        self.assertEqual(parsed.lines[0].printed_ttc, D("240.00"))

    def test_with_two_rates_every_line_past_the_base_and_tax_is_a_row(self):
        """The second row and the first rate's base and tax make what was
        paid too, one line longer than the two rows: taken, the apple juice
        was lost and two totals filed as articles, every check passing."""
        parsed = READER.parse_text(TWO_RATES_ONE_ROW_EACH)
        self.assertEqual(
            lines_of(parsed),
            [
                ("Jus de pomme", 2, D("11.20"), D("10.62"), D("0.055")),
                ("Verres", 2, D("12.20"), D("10.17"), D("0.20")),
            ],
        )
        self.assertEqual(failed(parsed), [])

    def test_with_two_rates_rows_that_say_nothing_are_right_or_said(self):
        """Rows printing their amount alone do not read as rows: nothing past
        the base and tax is taken, and the document fails its checks."""
        parsed = READER.parse_text(
            TWO_RATES_ONE_ROW_EACH.replace("Jus de pomme  2  x  5,60  11,20", "Jus de pomme  11,20").replace(
                "Verres  2  x  6,10  12,20", "Verres  12,20"
            )
        )
        names = [line.raw_name for line in parsed.lines]
        self.assertTrue(failed(parsed) or names == ["Jus de pomme", "Verres"], names)

    def test_rows_in_ht_are_not_given_up_for_a_recap_printed_at_the_top(self):
        """A recap printing the HT and the TTC reads as a row (two figures)
        and makes what was paid: offered as the TTC reading beside the carton
        in HT, it took the carton's place - one line « Total HT … » at
        240,00 €, five cartons lost, every check passing. The rows in HT come first,
        as they did."""
        body = ONE_ROW_THEN_BASE_AND_TAX.replace(
            "Carton exemple  5  x  48,00  240,00",
            "Désignation  Qté  PU HT  Montant HT\nCarton exemple  5  40,00  200,00",
        )
        for recap in (
            "Total HT 200,00 EUR - Total TTC 240,00 EUR\n",
            "Montant HT  Montant TTC  Echeance\n200,00  240,00  05/03/2025\n",
        ):
            with self.subTest(recap=recap):
                parsed = READER.parse_text(recap + body)
                self.assertEqual(lines_of(parsed), [("Carton exemple", 5, None, D("200.00"), D("0.20"))])
                self.assertEqual(failed(parsed), [])

    def test_further_down_the_photo_the_explained_rows_still_decide(self):
        """Past a first part adding up to nothing, only rows saying what makes
        their amount are read: the search past the base and tax is the first
        part's alone, or the note "3x Divers" joined the glasses."""
        parsed = READER.parse_text(
            "TICKET CLIENT\nLigne illisible  7,30\nTOTAL  23,00\n"
            "FACTURE N° 2025-0050 du 05/02/2025\n"
            "Jus de pomme  2  x  5,20  10,40\n3x  Divers  10,40\nVerres  2  x  6,30  12,60\n"
            "Base HT A  9,86\nMontant taxe A  0,54\nBase HT B  10,50\nMontant taxe B  2,10\n"
            "TVA 5,5%  9,86  0,54  10,40\nTVA 20%  10,50  2,10  12,60\nTotal TTC  23,00"
        )
        self.assertEqual(
            lines_of(parsed),
            [("Jus de pomme", 2, D("10.40"), D("9.86"), D("0.055")), ("Verres", 2, D("12.60"), D("10.50"), D("0.20"))],
        )
        self.assertEqual(failed(parsed), [])

    def test_a_repair_to_nothing_still_counts_against_another_one(self):
        """Without the voucher line (repaired to 0,00) the items make what was
        paid; with the coffee's leading digit cut (45,60 read for 5,60) they
        do too. Two repairs is none - dropped from the count, the second
        passed for the only one and filed the coffee at 5,60 €, every check
        passing."""
        parsed = READER.parse_text(
            "EPICERIE EXEMPLE\nPAIN DE MIE  4,70\nBON ACHAT  40,00\nCAFE MOULU  45,60\n"
            "TOTAL  50,30\nTVA 5,5%  47,68  2,62  50,30\nCB  50,30"
        )
        self.assertTrue(failed(parsed))
        self.assertEqual([line.printed_ttc for line in parsed.lines], [D("4.70"), D("40.00"), D("45.60")])

    def test_an_amount_printed_at_the_top_is_not_the_purchase(self):
        """The first line read is never checked as an end of the items: past
        the refused base and tax, the amount a till prints at the top of the
        page made what was paid on its own."""
        parsed = READER.parse_text(
            "MONTANT  TICKET CLIENT\n240,00 EUR  CONSERVER\n"
            + ONE_ROW_THEN_BASE_AND_TAX.replace(
                "Carton exemple  5  x  48,00  240,00",
                "Désignation  Qté  PU HT  Montant HT\nCarton exemple  5  40,00  200,00",
            )
        )
        self.assertEqual(lines_of(parsed), [("Carton exemple", 5, None, D("200.00"), D("0.20"))])
        self.assertEqual(failed(parsed), [])


# A champagne grower's invoice: the count in front of the name, no line HT at
# all - only the unit prices and the amount TTC - and the HT in the VAT table.
CHAMPAGNE_INVOICE = """B E R N A R D
EXEMPLE
Champagne
2, rue Inventee -51120 VINDEY , FRANCE
EARL EXEMPLE Père & Fils
IBAN : FR76 9876 5432 1098 7654 3210 987
SAS AU COMPTOIR
140 RUE DES LILAS
75011 PARIS 11
Dépôt :  EARLD
Commande N° 20259917 du 22/11/2025  Règlement  A réception  au  02/12/2025
Quantité  Désignation  PU HT  PU TTC  MNT TTC  Tva
12  BOUTEILLE(S)  CHAMPAGNE EXEMPLE BRUT  75 CL  13,75 €  16,50 €  198,00 €  A
Tva  Libellé  Taux  Base H.T.  Montant
A  TVA à 20 %  20,00  165,00 €  33,00 €
Total Net  198,00 €
Net à payer  198,00 €
N° de Siret :  92345678400019
Montant  198,00 €
N° de TVA :  FR21923456784  Code client  AUCOMPTOIR"""


class ChampagneInvoiceTests(SimpleTestCase):
    def test_the_count_in_front_of_the_name_stays_out_of_it(self):
        """ "12 BOUTEILLE(S) CHAMPAGNE" and "6 BOUTEILLE(S) CHAMPAGNE" are the
        same champagne; left in the name they are two products that never
        meet, and neither is what the shelf holds."""
        parsed = READER.parse_text(CHAMPAGNE_INVOICE)
        self.assertEqual(
            lines_of(parsed),
            [("BOUTEILLE(S) CHAMPAGNE EXEMPLE BRUT 75 CL", 12, D("198.00"), D("165.00"), D("0.2"))],
        )
        self.assertEqual(failed(parsed), [])

    def test_its_order_number_is_its_number(self):
        parsed = READER.parse_text(CHAMPAGNE_INVOICE)
        self.assertEqual((parsed.invoice_number, parsed.invoice_date), ("20259917", date(2025, 11, 22)))


# An alarm subscription: one row, carrying the period it covers, and the
# invoice's own number written with an ordinal indicator ("Nº").
ALARM_SUBSCRIPTION = """Votre facture d'abonnement
Ste AU COMPTOIR
Votre facture Nº : SDCF00000001
M. JEAN EXEMPLE
Date d'émission : 01/08/2026
140 RUE DES LILAS
Numéro Client :  000001
75010 PARIS
TOTAL du montant prélevé le 06/08/2026  77,33 €
1 / 2
Votre facture d'abonnement
Votre facture Nº : SDCF00000001  SIRET :  00000000000001
Date d'émission : 01/08/2026
TOTAL du montant prélevé le 06/08/2026  77,33 €
Quantité  Prix unitaire  Taux de  Montant H.T.
TVA  en €
Installation Nº 000001
Abonnement télésurveillance 24/24  01/08/2026au31/08/2026  1,00  64,44  20,00%  64,44
Total hors TVA  64,44 €
TVA  20,00% sur  64,44  12,89 €
Total T.T.C.  77,33 €
2 / 2"""


class SubscriptionInvoiceTests(SimpleTestCase):
    def test_the_row_carrying_its_period_is_read(self):
        """A line was thrown away for holding a date, and the subscription
        went unread. Only a line that is nothing *but* a date - or one
        stamped with an hour, which no article carries - is not an item."""
        read = read_line(0, "Abonnement 24/24  01/08/2026au31/08/2026  1,00  64,44  20,00%  64,44")
        self.assertEqual((read.name, read.total), ("Abonnement 24/24 01/08/2026au31/08/2026", D("64.44")))
        self.assertIsNone(read_line(0, "Date d'émission : 01/08/2026"))
        self.assertIsNone(read_line(0, "TICKET 2790300012345 DU 08/01/2024 10:26:09  Total TTC : 16,21 €"))

    def test_what_it_charges_and_its_table(self):
        parsed = READER.parse_text(ALARM_SUBSCRIPTION)
        self.assertEqual(parsed.printed_total_ttc, D("77.33"))
        self.assertEqual(parsed.vat_breakdown, [(D("0.20"), D("64.44"), D("12.89"))])
        self.assertEqual(parsed.invoice_date, date(2026, 8, 1))

    def test_its_number_is_written_with_an_ordinal_indicator(self):
        self.assertEqual(READER.parse_text(ALARM_SUBSCRIPTION).invoice_number, "SDCF00000001")


# A booking platform's invoice: thousands separated by a point, a date the
# month-first way, and a number the PDF's own rules break with a dash.
PLATFORM_INVOICE = """FACTURE
Facture # FR-F0000—001
Date de la facture août 03, 2026
Exemple Plateforme SA
Montant 1.162,80 €
88 Avenue Inventée
Identifiant client XkQ7mPzR2vLa9
Paris
75017
Période de facturation août 01 au
France
août 31, 2026
PAIEMENT DÛ
Facturé à  Abonnement
Billing Company - AU COMPTOIR  Payment Schedule - One time
SIREN - 934567892  payment
140 Rue des Lilas  Supplier ID - 10001
Paris  Prochaine date de facturation sept.
75010  01, 2026
Description  Unités  Réduction  Total HT  TVA  Montant (EUR)
Abonnement
1  1  0,00 €  969,00 €  193,80 €  1.162,80 €
Total HT  969,00 €
TVA @ 20 %  193,80 €
Montant de la facture  1.162,80 €
Paiements  0,00 €
Montant à payer (EUR)  1.162,80 €"""


class PlatformInvoiceTests(SimpleTestCase):
    def test_thousands_separated_by_a_point(self):
        """ "1.162,80" was read as no amount at all, and the invoice was filed
        at the 969,00 € of its goods."""
        parsed = READER.parse_text(PLATFORM_INVOICE)
        self.assertEqual(parsed.printed_total_ttc, D("1162.80"))
        self.assertEqual(parsed.vat_breakdown, [(D("0.2"), D("969.00"), D("193.80"))])

    def test_a_date_written_month_first(self):
        """ "août 03, 2026" - and the next billing date printed below it is
        not the document's own."""
        self.assertEqual(READER.parse_text(PLATFORM_INVOICE).invoice_date, date(2026, 8, 3))

    def test_a_month_abbreviated_or_in_english(self):
        for written, expected in (
            ("Date de la facture Dec 02, 2024", date(2024, 12, 2)),
            ("Date de la facture déc. 02, 2025", date(2025, 12, 2)),
            ("Date de la facture avr—. 26, 2024", date(2024, 4, 26)),
            ("Date de la facture juin— 03, 2024", date(2024, 6, 3)),
        ):
            self.assertEqual(READER.parse_text(f"FACTURE\n{written}\nMontant 10,00 €").invoice_date, expected)

    def test_a_number_the_rules_broke_in_two(self):
        self.assertEqual(READER.parse_text(PLATFORM_INVOICE).invoice_number, "FR-F0000001")


class NumberPrintedWithItsDateTests(SimpleTestCase):
    """« N° X du <date> » is the document's own number, printed with its
    date, the word « facture » a line or two above it (the water bill's
    « FACTURE TRIMESTRIELLE », the box's « Facture Freebox »). Looked for
    beside « facture » only, it was not found: a payment reference - a long
    digit run in the bill's detail - was taken instead, or a number was
    made up from the date and the total, and the portal's list, printing
    the real one, never recognised an invoice already imported."""

    def test_a_water_bills_number(self):
        bill = WATER_BILL.replace("N 2026100000001", "N° 2026100000001").replace(
            "TOTAL  110,00  7,85  117,85", "TOTAL  110,00  7,85  117,85\n5300000000000000000000001  117,85"
        )
        self.assertEqual(READER.parse_text(bill).invoice_number, "2026100000001")

    def test_a_box_subscriptions_number(self):
        bill = """Facture Freebox
n°1400000001 du 19 Janvier 2024
Abonnement Freebox  29,99
Total de la facture HT  24,99
TVA [20.00%]  5,00
Total TTC  29,99"""
        self.assertEqual(READER.parse_text(bill).invoice_number, "1400000001")

    def test_a_number_printed_beside_the_word_invoice_still_comes_first(self):
        self.assertEqual(READER.parse_text(WINE_INVOICE).invoice_number, "FA-202603-0001")
