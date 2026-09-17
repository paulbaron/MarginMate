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
WATER_BILL_WITH_BALANCE = WATER_BILL.replace(
    "Solde anterieur  0,00€ TTC", "Solde anterieur  171,37€ TTC"
).replace(
    "Montant net a prelever  117,85€ TTC", "Montant net a payer  1 289,22€ TTC"
).replace(
    "SOLDE ANTERIEUR  0,00", "SOLDE ANTERIEUR  171,37"
).replace(
    "MONTANT NET A PRELEVER  117,85", "MONTANT NET A PAYER  1 289,22"
).replace(
    "Collecte des eaux usees  du 04/04/26 au 26/06/26  50  m3  0,6000  30,00  10,0%  3,00  33,00",
    "Collecte des eaux usees  du 04/04/26 au 26/06/26  50  m3  20,6000  1 030,00  10,0%  103,00  1 133,00",
).replace(
    "COLLECTE ET TRAITEMENT DES EAUX USEES  40,00  4,00  44,00",
    "COLLECTE ET TRAITEMENT DES EAUX USEES  1 040,00  104,00  1 144,00",
).replace(
    "Dont TVA 10,0 % : 4,00 € sur la base de 40,00 €",
    "Dont TVA 10,0 % : 104,00 € sur la base de 1 040,00 €",
).replace("TOTAL  110,00  7,85  117,85", "TOTAL  1 110,00  107,85  1 217,85").replace(
    "Total  117,85€ TTC", "Total  1 217,85€ TTC"
).replace(
    "100  Votre consommation  50 m3  96,75€ TTC", "100  Votre consommation  50 m3  1 196,75€ TTC"
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
        """"MONTANT NET A PAYER 1 289,22" is this quarter plus the last
        quarter's unpaid balance: the document is worth what it charges."""
        self.assertEqual(READER.parse_text(WATER_BILL_WITH_BALANCE).printed_total_ttc, D("1217.85"))


# A phone bill whose sundry services are subtotalled ("Total : 2.97") and
# printed again on their own line, while the amount charged is printed once.
PHONE_BILL_WITH_EXTRAS = PHONE_BILL.replace(
    "Total de la facture HT  8.33", "Total de la facture HT  10.80"
).replace("TVA [20.00%]  1.66", "TVA [20.00%]  2.16").replace(
    "Somme a payer TTC*  9.99", "Somme a payer TTC*  12.96"
) + """
Services fournis par des tiers (Total : 2.97  TTC ( 2.48 HT ))
SMS+  6  2.97 (2.48)"""


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
        self.assertEqual(
            line_amounts("TOTAL  936,59  74,41  1 011,00"), [D("936.59"), D("74.41"), D("1011.00")]
        )
        self.assertEqual(amount_candidates("TOTAL  1 011,00"), [D("1011.00")])

    def test_two_amounts_in_neighbouring_columns_stay_two(self):
        """"10.49 31.47" is what a price column and the amount beside it look
        like once the space between them is all that separates them."""
        self.assertEqual(line_amounts("PAIN COMPLET  10.49 31.47"), [D("10.49"), D("31.47")])
        self.assertEqual(line_amounts("PAIN COMPLET  10.49 314.47"), [D("10.49"), D("314.47")])

    def test_a_group_of_two_or_four_digits_is_not_a_thousands_group(self):
        self.assertEqual(line_amounts("CAISSE 12  1 04,50"), [D("4.50")])
        self.assertEqual(line_amounts("TICKET  2 0110,00"), [D("110.00")])


# A wine grower's invoice, printing each price and each amount both ways -
# HT and TTC - with no tax column, its rate last on the row. Two suppliers
# had a parser of their own for this until the reader could do it: what they
# knew is in these two fixtures. Structure copied, data invented.
WINE_INVOICE = """SCEA EXEMPLE ET FILS  FACTURE
26 Rue Inventee
37530, Charge, France  Date: 30/04/2026
contact@exemple.fr  N° document: FA-202604-0001
Date de livraison: 02/04/2026
Adresse de livraison:  Adresse de facturation:
Societe AU DIPSO  Societe AU DIPSO
140 RUE DES LILAS  140 RUE DES LILAS
Désignation  Qté  Px U. HT  Px U. TTC  HT  TTC  Taux
LES CAILLOUX EXEMPLE - - AC TOURAINE  18  5,00€  6,00€  90,00€  108,00€  20,00%
JUS DE RAISIN EXEMPLE - - VAL DE LOIRE  24  5,50€  5,80€  132,00€  139,26€  5,50%
Nombre de produits: 42  Nombre de colis: 7  Volume total: 31.5L  Poids total: 63.35kg
Libellé  Hors taxe  TVA  TTC  Montant total HT  222,00€
Taux 20.00%  90,00€  18,00€  108,00€
Taux 5.50%  132,00€  7,26€  139,26€
Total net HT  222,00€
Règlement  Total TVA  25,26€
Virement - A 45 jours
Montant total TTC  247,26€
RIB  Net à payer  247,26€
Titulaire du compte: SCEA EXEMPLE ET FILS
IBAN: FR76 1234 5678 9012 3456 7890 123
BIC: AGRIFRPP894
SIREN : 383317872 - SIRET : 38331787200018 - TVA : FR04383317872 - NAF : 01.21Z
SCEA EXEMPLE ET FILS  FA-202604-0001 - LE DIPSOMANIAC - AU DIPSO  Page 1/1"""

# The same grower two years earlier: no "Taux" column, and its reference
# under another name again ("Référence interne" in January 2025).
WINE_INVOICE_2024 = """FACTURE
Date: 29/11/2024
N° document: FA-202411-0001
Désignation  Qté  Px U. HT (hors droits)  Px U. TTC  HT  TTC
LES CAILLOUX EXEMPLE - - AC TOURAINE  18  4.50€  5.40€  81.00€  97.20€
CREMANT EXEMPLE - - CREMANT DE LOIRE  6  5.00€  6.00€  30.00€  36.00€
Nombre de produits: 24
Libellé  Hors taxe  TVA  TVA réglée  TTC  Montant total HT  111,00€
Taux 20.00%  111,00€  22,20€  0,00€  133,20€
Total net HT  111,00€
Montant total TTC  133,20€
IBAN: FR76 1234 5678 9012 3456 7890 123"""


class WineInvoiceTests(SimpleTestCase):
    def test_a_row_printing_both_ways_keeps_its_count(self):
        """Nothing adds up on the row - it prints no tax - so the count is
        proved by one rate turning both prices into both amounts. Read as a
        bare list of amounts, eighteen bottles came out as one at 90,00."""
        parsed = READER.parse_text(WINE_INVOICE)
        self.assertEqual(
            lines_of(parsed),
            [
                ("LES CAILLOUX EXEMPLE - - AC TOURAINE", 18, D("108.00"), D("90.00"), D("0.20")),
                ("JUS DE RAISIN EXEMPLE - - VAL DE LOIRE", 24, D("139.26"), D("132.00"), D("0.055")),
            ],
        )
        self.assertEqual((parsed.printed_total_ttc, parsed.invoice_date), (D("247.26"), date(2026, 4, 30)))
        self.assertEqual(failed(parsed), [])

    def test_the_unit_price_is_per_bottle(self):
        """What every cost downstream is divided by: 5,00 € the bottle, not
        90,00 € the case."""
        self.assertEqual([line.unit_cost_ht for line in READER.parse_text(WINE_INVOICE).lines][0], D("5.00"))

    def test_the_document_number_is_its_own_and_never_the_iban(self):
        """An IBAN is a long digit run once its spaces are gone, and the same
        one every month: read as the number, the next invoice was refused as
        a duplicate of this one."""
        self.assertEqual(READER.parse_text(WINE_INVOICE).invoice_number, "FA-202604-0001")
        self.assertEqual(READER.parse_text(WINE_INVOICE_2024).invoice_number, "FA-202411-0001")
        self.assertEqual(
            READER.parse_text(WINE_INVOICE_2024.replace("N° document:", "Référence interne:")).invoice_number,
            "FA-202411-0001",
        )

    def test_the_older_layout_without_a_rate_column(self):
        parsed = READER.parse_text(WINE_INVOICE_2024)
        self.assertEqual(
            [(line.quantity, line.total_ht) for line in parsed.lines],
            [(18, D("81.00")), (6, D("30.00"))],
        )
        self.assertEqual(parsed.printed_total_ttc, D("133.20"))


# A champagne grower's invoice: the count in front of the name, no line HT at
# all - only the unit prices and the amount TTC - and the HT in the VAT table.
CHAMPAGNE_INVOICE = """B E R N A R D
EXEMPLE
Champagne
2, rue Inventee -51120 VINDEY , FRANCE
EARL EXEMPLE Père & Fils
IBAN : FR76 9876 5432 1098 7654 3210 987
SAS AU DIPSO
140 RUE DES LILAS
75011 PARIS 11
Dépôt :  EARLD
Commande N° 20250180 du 22/11/2025  Règlement  A réception  au  02/12/2025
Quantité  Désignation  PU HT  PU TTC  MNT TTC  Tva
12  BOUTEILLE(S)  CHAMPAGNE EXEMPLE BRUT  75 CL  13,75 €  16,50 €  198,00 €  A
Tva  Libellé  Taux  Base H.T.  Montant
A  TVA à 20 %  20,00  165,00 €  33,00 €
Total Net  198,00 €
Net à payer  198,00 €
N° de Siret :  43122667900014
Montant  198,00 €
N° de TVA :  FR70431226679  Code client  AUDIPSO"""


class ChampagneInvoiceTests(SimpleTestCase):
    def test_the_count_in_front_of_the_name_stays_out_of_it(self):
        """"12 BOUTEILLE(S) CHAMPAGNE" and "6 BOUTEILLE(S) CHAMPAGNE" are the
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
        self.assertEqual((parsed.invoice_number, parsed.invoice_date), ("20250180", date(2025, 11, 22)))
