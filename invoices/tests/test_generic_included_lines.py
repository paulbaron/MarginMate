"""The ticket reader on a DIY store's till: under an item, a line saying
what part of its price is a charge included in it ("Dt Ecopart. unit.
EcoMob 0.72", "Dt" for « dont »); a size at the end of a name ("76X46");
and a sale printed in blocks, each ending on its own sub-total. Beside
them, what those rules must leave alone: an item priced like the items
above it, a count glued to a price in whole euros ("2x4 8,00"), and an
article number that begins with the count's digit.

Structure copied from the tickets of a DIY chain in the real database
(documents 899, 901, 902 and 904, read wrong until their lines were typed
again by hand); every name, code, number, date and amount invented, and
each fixture's arithmetic holds, since that is what the reader checks
itself against.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.generic_receipt import GenericReceiptParser, TicketShop, read_line

D = Decimal
READER = GenericReceiptParser(TicketShop("SHOP", (), "Magasin"))

HEAD = """MAGASIN  EXEMPLE  000
Ville-Exemple
Tel.  :  01  00  00  00  00
www.magasin-exemple.fr
Vente
EUR
"""


def foot(total, vat, ht, included=""):
    """What the till prints under the items; `included`, the charges the
    items include, added up again under the total."""
    return f"""__________
TOTAL  (EUR)  {total}
{included}____________________________________
Carte  bancaire  {total}  EUR
____________________
Ancien  solde  de  points  :  10
Points  de  fidélité  acquis  :  20
Nouveau  solde  de  points  :  30
____________________
***  TVA  EUR  ***
Tva  H  20.00%  :  {vat}  HT  :  {ht}
Total  TVA  :  {vat}  HT  :  {ht}
____________________________________
000-10000000-000  0001  14/07/2026  10:12
CARTE  BANCAIRE
le  14/07/26  a  10:12:40
MONTANT
{total.replace(".", ",")}  EUR
DEBIT
TICKET  CLIENT
A  CONSERVER"""


# Two items, each with the eco-participation it includes printed under it -
# and their sum again under the total (document 901).
TWO_ECOPARTS = HEAD + """OUTILLAGE
CAISSE  A  OUTILS  PLASTIQUE  20"  EXEMPLE
H  3000001000013  19.90
Dt  Ecopart.  unit.  EcoMob  0.60
MALLETTE  RANGEMENT  NOIR  40X30X6  EXEMPLE
H  3000001000020  10.90
Dt  Ecopart.  unit.  EcoMob  0.15
""" + foot("30.80", "5.13", "25.67", "Dt  Ecopart.  recycl  EcoMob  0.75\n")

# One item and an included cent: with it, the lines are one cent off what
# was paid - within the tolerance a total is found with (document 904).
ONE_CENT = HEAD + """ELECTRICITE-PLOMBERIE
C  BOITE  60  JOINTS  ASSORTIS  +  GRAISSE
H  3000001000037  4.99
Dt  Ecopart.  unit.  PMCB  0.01
""" + foot("4.99", "0.83", "4.16", "Dt  Ecopart.  recycl  PMCB  0.01\n")

# A name ending on the article's size, then the item's code and price on
# the next line (document 902).
SIZED_NAME = HEAD + """SOL  ET  CARRELAGE  MURAL
TAPIS  ABSORBANT  GRIS  60X40
H  3000001000068  8.45
Dt  Ecopart.  unit.  EcoMob  0.30
QUINCAILLERIE
BOITE  CLIP  2,5L  TRANSPARENT
H  3000001000075  3  X  4.99  14.97
""" + foot("23.42", "3.90", "19.52", "Dt  Ecopart.  recycl  EcoMob  0.30\n")

# Two sales on one ticket, each ending on its sub-total; an item's store
# reference is no EAN (document 899).
TWO_SALES = HEAD.replace("Vente\n", "Vente\nBVI  NO  :  100001\n") + """MENUISERIE
PLAN  DE  TRAVAIL  CHENE  200X60X2.8CM
PLAN  DE  TRAVAIL  CHENE  200X60X2.8CM  6
H  10000001  49.90
Dt  Ecopart.  unit.  EcoMob  0.25
DECOUPE  DROITE  DE  PLAN
H  10000002  3  X  1.00  3.00
SOUS  TOTAL  52.90
Vente
EUR
ELECTRICITE-PLOMBERIE
1M  GAINE  THERMO  6-3MM  BLEU  EXEMPLE
H  3000001000044  1.49
CONFORT  &  ENERGIE  RENOUVELABLE
LOT  DE  2  BRIQUETS  EXEMPLE
H  3000001000051  2.39
SOUS  TOTAL  3.88
""" + foot("56.78", "9.46", "47.32", "Dt  Ecopart.  recycl  EcoMob  0.25\n")

# The same till, three sales on one ticket and no charge included anywhere.
THREE_SALES = HEAD + """QUINCAILLERIE
CROCHET  ADHESIF  BLANC  X4
H  3000001000013  3.50
CHEVILLE  UNIVERSELLE  6MM  X50
H  3000001000020  4.20
SOUS  TOTAL  7.70
Vente
EUR
PEINTURE
ROULEAU  MOUSSE  110MM
H  3000001000037  2.90
BACHE  DE  PROTECTION  PLASTIQUE
H  3000001000044  1.60
SOUS  TOTAL  4.50
Vente
EUR
OUTILLAGE
CUTTER  18MM  EXEMPLE
H  3000001000051  5.00
LAMES  DE  CUTTER  18MM  X10
H  3000001000068  3.30
SOUS  TOTAL  8.30
""" + foot("20.50", "3.42", "17.08")

# Two packs of two, then a pack of four at their price, in the second sale:
# the sum of the items since the last sub-total, but printed with its own
# article code - an item.
PACK_AFTER_TWO = HEAD + """OUTILLAGE
CUTTER  18MM  EXEMPLE
H  3000001000013  6.50
LAMES  DE  CUTTER  X10
H  3000001000020  3.40
SOUS  TOTAL  9.90
Vente
EUR
QUINCAILLERIE
CROCHET  ADHESIF  X2
H  3000001000037  1.20
CROCHET  ADHESIF  X2
H  3000001000037  1.20
CROCHET  ADHESIF  X4
H  3000001000044  2.40
SOUS  TOTAL  4.80
""" + foot("14.70", "2.45", "12.25")

# The same packs in the ticket's only sale: the pack of four is the sum of
# every item read before it.
ONE_SALE_PACK = HEAD + """QUINCAILLERIE
CROCHET  ADHESIF  X2
H  3000001000037  1.20
CROCHET  ADHESIF  X2
H  3000001000037  1.20
CROCHET  ADHESIF  X4
H  3000001000044  2.40
SOUS  TOTAL  4.80
""" + foot("4.80", "0.80", "4.00")

# A cut of a worktop on the store's own reference (no EAN), in the second
# sale, at what the two items above it make: the sum of the items since the
# last sub-total, but printed under its own name - an item.
CUT_AFTER_TWO = HEAD + """OUTILLAGE
SCIE  EGOINE  EXEMPLE
H  3000001000013  5.00
METRE  RUBAN  EXEMPLE
H  3000001000020  4.00
SOUS  TOTAL  9.00
Vente
EUR
QUINCAILLERIE
VIS  BOIS  BOITE
H  3000001000037  2.00
CHEVILLES  NYLON
H  3000001000044  3.00
DECOUPE  DROITE  DE  PLAN
H  10000002  5.00
SOUS  TOTAL  10.00
""" + foot("19.00", "3.17", "15.83")

# Each sale's sub-total with its label on one line and its amount alone on
# the next: under a name, like an item's amount - but printing nothing else.
SUBTOTAL_UNDER_ITS_LABEL = HEAD + """OUTILLAGE
SCIE  EGOINE  EXEMPLE
H  3000001000013  5.00
METRE  RUBAN  EXEMPLE
H  3000001000020  4.00
SOUS  TOTAL
9.00
Vente
EUR
QUINCAILLERIE
VIS  BOIS  BOITE
H  3000001000037  2.00
CHEVILLES  NYLON
H  3000001000044  3.00
SOUS  TOTAL
5.00
""" + foot("14.00", "2.33", "11.67")

# Name, count, unit price and amount on one line, at what the two items
# above it make: no sub-total prints a count and its price.
COUNTED_AT_THE_ITEMS_SUM = """BOULANGERIE  EXEMPLE
1  RUE  EXEMPLE
CROISSANT  0,60 €
PAIN  AU  CHOCOLAT  0,90 €
CHOUQUETTE  3X0.50  1,50 €
TOTAL  3,00 €
CB  3,00 €
TVA  5,5 %  0,16 €
"""

# A count glued to a unit price in whole euros: 2 x 4 is the 8,00 printed.
COUNT_BY_A_WHOLE_PRICE = """BOUTIQUE  EXEMPLE
1  RUE  EXEMPLE
TASSE  CERAMIQUE  2x4  8,00 €
SOUS-VERRE  LIEGE  3,50 €
TOTAL  11,50 €
CB  11,50 €
TVA  20 %  1,92 €
"""


def lines_of(parsed):
    return [(" ".join(line.raw_name.split()), line.quantity, line.printed_ttc) for line in parsed.lines]


def failed(parsed):
    return [(check.label, check.detail) for check in parsed.checks if not check.passed]


def set_aside(parsed):
    return [check.detail for check in parsed.checks if check.label == "Lignes écartées"]


class IncludedChargeTests(SimpleTestCase):
    def test_a_charge_the_item_above_includes_is_never_an_item(self):
        parsed = READER.parse_text(TWO_ECOPARTS)
        self.assertEqual(
            lines_of(parsed),
            [
                ('CAISSE A OUTILS PLASTIQUE 20" EXEMPLE', 1, D("19.90")),
                ("MALLETTE RANGEMENT NOIR 40X30X6 EXEMPLE", 1, D("10.90")),
            ],
        )
        self.assertEqual(failed(parsed), [])

    def test_an_included_cent_is_not_taken_for_an_item_within_the_tolerance(self):
        parsed = READER.parse_text(ONE_CENT)
        self.assertEqual(lines_of(parsed), [("C BOITE 60 JOINTS ASSORTIS + GRAISSE", 1, D("4.99"))])
        self.assertEqual(failed(parsed), [])

    def test_dont_spelled_out_or_after_a_dash_is_the_same_line(self):
        # "- Dont DDS 0.20" under a bottle of acid, at another DIY till.
        text = ONE_CENT.replace("Dt  Ecopart.  unit.  PMCB  0.01", "- Dont PMC  0.01")
        self.assertEqual(lines_of(READER.parse_text(text)), [("C BOITE 60 JOINTS ASSORTIS + GRAISSE", 1, D("4.99"))])


class SizeTests(SimpleTestCase):
    def test_a_size_ending_a_name_is_not_a_count(self):
        reading = read_line(0, "TAPIS  ABSORBANT  GRIS  60X40")
        self.assertIsNone(reading.count)
        self.assertEqual(reading.name, "TAPIS ABSORBANT GRIS 60X40")
        self.assertIsNone(read_line(0, "TASSEAU  SAPIN20X30  5,90").count)
        self.assertIsNone(read_line(0, "COLLIER  ACIER  12*20  2,90").count)
        # A count is still one before a price, glued or not.
        self.assertEqual(read_line(0, "H  3000001000075  3  X  4.99  14.97").count, 3)
        self.assertEqual(read_line(0, "PAIN  3X0.49  1.47").count, 3)

    def test_the_item_named_with_its_size_keeps_its_price(self):
        parsed = READER.parse_text(SIZED_NAME)
        self.assertEqual(
            lines_of(parsed),
            [("TAPIS ABSORBANT GRIS 60X40", 1, D("8.45")), ("BOITE CLIP 2,5L TRANSPARENT", 3, D("14.97"))],
        )
        self.assertEqual(failed(parsed), [])

    def test_two_whole_numbers_making_the_amount_are_a_count_and_its_price(self):
        # The money decides: 2 x 4 is the 8,00 the line prints, where 60 x 40
        # makes nothing it prints.
        reading = read_line(0, "TASSE  CERAMIQUE  2x4  8,00 €")
        self.assertEqual((reading.name, reading.count, reading.total), ("TASSE CERAMIQUE", 2, D("8.00")))
        self.assertEqual(read_line(0, "GALETTE  3*2  6,00").count, 3)
        self.assertIsNone(read_line(0, "CARREAU  MURAL  20X20  4,00").count)
        parsed = READER.parse_text(COUNT_BY_A_WHOLE_PRICE)
        self.assertEqual(lines_of(parsed), [("TASSE CERAMIQUE", 2, D("8.00")), ("SOUS-VERRE LIEGE", 1, D("3.50"))])
        self.assertEqual(failed(parsed), [])


class SubTotalTests(SimpleTestCase):
    def test_each_sale_s_sub_total_is_stepped_over(self):
        parsed = READER.parse_text(TWO_SALES)
        self.assertEqual(
            lines_of(parsed),
            [
                ("PLAN DE TRAVAIL CHENE 200X60X2.8CM", 1, D("49.90")),
                ("DECOUPE DROITE DE PLAN", 3, D("3.00")),
                ("1M GAINE THERMO 6-3MM BLEU EXEMPLE", 1, D("1.49")),
                ("LOT DE 2 BRIQUETS EXEMPLE", 1, D("2.39")),
            ],
        )
        self.assertEqual(failed(parsed), [])
        self.assertEqual(set_aside(parsed), [])

    def test_three_sales_make_one_run_of_items(self):
        parsed = READER.parse_text(THREE_SALES)
        self.assertEqual(
            [amount for _name, _count, amount in lines_of(parsed)],
            [D("3.50"), D("4.20"), D("2.90"), D("1.60"), D("5.00"), D("3.30")],
        )
        self.assertEqual(failed(parsed), [])

    def test_an_item_priced_like_the_items_of_its_sale_is_no_sub_total(self):
        parsed = READER.parse_text(PACK_AFTER_TWO)
        self.assertEqual(
            lines_of(parsed),
            [
                ("CUTTER 18MM EXEMPLE", 1, D("6.50")),
                ("LAMES DE CUTTER X10", 1, D("3.40")),
                ("CROCHET ADHESIF X2", 1, D("1.20")),
                ("CROCHET ADHESIF X2", 1, D("1.20")),
                ("CROCHET ADHESIF X4", 1, D("2.40")),
            ],
        )
        self.assertEqual(failed(parsed), [])
        self.assertEqual(set_aside(parsed), [])

    def test_an_item_priced_like_every_item_above_it_is_no_sub_total(self):
        parsed = READER.parse_text(ONE_SALE_PACK)
        self.assertEqual(
            lines_of(parsed),
            [
                ("CROCHET ADHESIF X2", 1, D("1.20")),
                ("CROCHET ADHESIF X2", 1, D("1.20")),
                ("CROCHET ADHESIF X4", 1, D("2.40")),
            ],
        )
        self.assertEqual(failed(parsed), [])

    def test_an_item_on_a_store_reference_priced_like_its_sale_is_no_sub_total(self):
        # No EAN to tell it from a sub-total: its name, printed on the line
        # above, does - and so does a count printed with its unit price.
        # Stepped over, it left the sale's own sub-total to be filed as the
        # item, and every check passed.
        for row in ("H  10000002  5.00", "H  10000002  5  X  1.00  5.00"):
            with self.subTest(row=row):
                parsed = READER.parse_text(CUT_AFTER_TWO.replace("H  10000002  5.00", row))
                self.assertEqual(
                    [(name, amount) for name, _count, amount in lines_of(parsed)],
                    [
                        ("SCIE EGOINE EXEMPLE", D("5.00")),
                        ("METRE RUBAN EXEMPLE", D("4.00")),
                        ("VIS BOIS BOITE", D("2.00")),
                        ("CHEVILLES NYLON", D("3.00")),
                        ("DECOUPE DROITE DE PLAN", D("5.00")),
                    ],
                )
                self.assertEqual(failed(parsed), [])
                self.assertEqual(set_aside(parsed), [])

    def test_a_sub_total_printed_under_its_label_is_still_one(self):
        # Its amount stands alone under a name, as an item's does under its
        # name - but an item's row prints its reference too. Kept as the
        # item « SOUS TOTAL », the first sale's went into a run with the
        # second sale's items that made what was paid: its own items were
        # set aside, and every check passed.
        parsed = READER.parse_text(SUBTOTAL_UNDER_ITS_LABEL)
        self.assertEqual(
            [(name, amount) for name, _count, amount in lines_of(parsed)],
            [
                ("SCIE EGOINE EXEMPLE", D("5.00")),
                ("METRE RUBAN EXEMPLE", D("4.00")),
                ("VIS BOIS BOITE", D("2.00")),
                ("CHEVILLES NYLON", D("3.00")),
            ],
        )
        self.assertEqual(failed(parsed), [])

    def test_an_item_printing_its_count_and_price_is_no_sub_total(self):
        parsed = READER.parse_text(COUNTED_AT_THE_ITEMS_SUM)
        self.assertEqual(
            lines_of(parsed),
            [("CROISSANT", 1, D("0.60")), ("PAIN AU CHOCOLAT", 1, D("0.90")), ("CHOUQUETTE", 3, D("1.50"))],
        )
        self.assertEqual(failed(parsed), [])


class LeadingCountTests(SimpleTestCase):
    def test_an_article_number_beginning_with_the_count_keeps_its_digits(self):
        # EAN, the store's article number, the name, the unit price, a one,
        # the count, the amount and the VAT code: the count 2 is its own
        # column, not the first digit of 2000123.
        reading = read_line(0, "3000001000013  2000123  50  GOBELETS  CARTON  20CL  1,650  1  2  3,30  D")
        self.assertEqual((reading.name, reading.count), ("2000123 50 GOBELETS CARTON 20CL", 2))
        # A count in front of the name still leaves it.
        self.assertEqual(read_line(0, "2  GOBELETS  CARTON  20CL  1,65  3,30").name, "GOBELETS CARTON 20CL")
