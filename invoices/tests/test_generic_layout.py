"""The ticket reader given WHERE each piece of text sits (`PdfPage.rows`,
filled by `ReceiptParser.parse_ocr_pages` from a PDF's own words or a
photo's boxes), on the layouts the text alone misread: an ice supplier's
invoice whose table header is printed over three lines and whose "Capital"
line read as the purchase; a computer shop keeping its line numbers and
article codes in the names; an English web shop whose descriptions wrap onto
the next line; a photographed invoice with a column of lone "1"s in front of
the names; a grocer's web shop whose delivery prints its price four times.

Every fixture is a list of rows with positions (points for a PDF, pixels for
a photo) copied from the real layouts, and every name, number, date and
amount invented. Each one is also read WITHOUT its positions, and that
reading has to be exactly what `parse_text` - the stored-reading path every
ticket already filed goes through - gives: positions add a reading, they
never change the text one.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.ocr import OcrCell, OcrLine, OcrPage
from invoices.parsers.base import PdfPage
from invoices.parsers.generic_receipt import GenericReceiptParser, TicketShop
from invoices.parsers.layout import Cell, Row, text_of

D = Decimal
READER = GenericReceiptParser(TicketShop("SHOP", (), "Magasin"))
TABLE_CHECK = "Tableau reconnu"


def row(y, *cells):
    return Row(cells=[Cell(text, x0, x1) for text, x0, x1 in cells], y=y)


def photo_row(y, *cells):
    """A line as a recogniser groups it on a photograph: not exact."""
    return Row(cells=[Cell(text, x0, x1) for text, x0, x1 in cells], y=y, exact=False)


def positioned(rows):
    return PdfPage(text=text_of(rows), tables=[], rows=rows)


def text_only(rows):
    return PdfPage(text=text_of(rows), tables=[])


def lines_of(parsed):
    return [(line.raw_name, line.quantity, line.total_ht, line.vat_rate, line.printed_ttc) for line in parsed.lines]


def failed(parsed):
    return {check.label: check.detail for check in parsed.checks if not check.passed}


def table_check(parsed):
    return next((check for check in parsed.checks if check.label == TABLE_CHECK), None)


# An ice supplier's digital invoice. The header is printed over three lines
# ("Taux de / Montant / Montant" above "Référence / Désignation / QuantitéPU
# HT" above "TVA / TVA / HT"), the rows are in HT and each prints its rate
# and its tax, and the document's head - its number, its address, its
# capital - sits above. Read as text, "Capital : 8 000,00 Euros" was the
# purchase and the rows passed for the VAT table.
ICE_INVOICE = [
    row(13.4, ("FACTURE", 12, 76)),
    row(56.7, ("Numéro de facture : 20260115.10042", 12, 147)),
    row(68.0, ("Date de facturation : 15/01/2026", 12, 129)),
    row(90.5, ("SAS GLACE EXEMPLE", 12, 109)),
    row(101.7, ("4 rue des Frimas", 12, 81)),
    row(113.0, ("95000 Givréville", 12, 76)),
    row(124.2, ("Tél : 0100000000", 12, 76)),
    row(135.5, ("Capital : 8 000,00 Euros", 12, 100)),
    row(191.0, ("Client Exemple", 543, 583)),
    row(202.3, ("1 rue de la Soif", 512, 583)),
    row(213.5, ("75000 Paris", 540, 583)),
    row(254.8, ("Taux de", 455, 485), ("Montant", 497, 530), ("Montant", 542, 575)),
    row(260.8, ("Référence", 12, 50), ("Désignation", 77, 122), ("QuantitéPU HT", 323, 384)),
    row(266.1, ("TVA", 455, 473), ("TVA", 497, 515), ("HT", 542, 555)),
    row(279.5, ("AR0142", 12, 53), ("Sac de glace pilée 10 kg", 77, 178), ("2", 323, 329), ("12.40", 357, 384), ("5.5%", 455, 480), ("1.36", 497, 518), ("24.80", 542, 569)),
    row(293.0, ("Livraison", 12, 58), ("Frais de port", 77, 160), ("1", 323, 329), ("6.00", 357, 378), ("20%", 455, 477), ("1.20", 497, 518), ("6.00", 542, 563)),
    row(326.0, ("Total TVA 20%", 6, 82), ("1.20", 104, 125), ("Total HT", 405, 449), ("30.80", 500, 527)),
    row(339.5, ("Net HT", 405, 442), ("30.80", 500, 527)),
    row(353.0, ("Total TVA 5.5%", 6, 85), ("1.36", 104, 125)),
    row(366.5, ("Total TVA", 405, 458), ("2.56", 500, 521)),
    row(380.0, ("Total TTC", 405, 459), ("33.36", 500, 527)),
    row(393.3, ("Montants exprimés en Euros", 6, 92)),
    row(421.0, ("Date de règlement : paiement comptant", 6, 124)),
]

# A computer shop's invoice: a line number and an article code in front of
# each name, a quantity column, two unit prices around an empty discount
# column, the row's total, a status column; then the shop's delivery terms
# printed under the table, and its totals and VAT table far below.
COMPUTER_SHOP = [
    row(25.7, ("Facture", 393, 444)),
    row(45.5, ("Numéro de document", 292, 361), ("Date", 370, 385)),
    row(56.5, ("FP2020000041188", 292, 358), ("12/02/2026", 370, 408)),
    row(77.5, ("INFORMATIQUE EXEMPLE SAS", 24, 138)),
    row(91.0, ("3 RUE DES CIRCUITS", 24, 108), ("Adresse de facturation", 292, 363)),
    row(101.7, ("75000", 24, 53), ("PARIS", 61, 89)),
    row(133.4, ("Tél :", 23, 43), ("01.00.00.00.00", 48, 118)),
    row(217.4, ("N°", 26, 34), ("Article", 152, 174), ("Qté", 338, 350), ("PU TTC", 359, 385), ("Remise", 393, 419), ("PU TTC", 429, 454), ("Total TTC", 468, 501), ("Statut", 517, 537)),
    row(230.8, ("1", 27, 31), ("704411", 37, 63), ("Câble HDMI 2 m tressé", 71, 261), ("1", 342, 346), ("7,99", 365, 380), ("7,99", 434, 449), ("7,99", 477, 492)),
    row(241.6, ("2", 27, 31), ("118830", 37, 63), ("Adaptateur USB-C vers Jack", 71, 228), ("3", 342, 346), ("8,30", 363, 383), ("8,30", 432, 452), ("24,90", 475, 494)),
    row(262.5, ("Retrait en boutique sous 2 heures", 106, 291)),
    row(267.0, ("Expédition sous 24 heures", 352, 487)),
    row(272.7, ("dans tout le centre-ville et OFFERT au", 106, 301)),
    row(277.2, ("partout en métropole et", 352, 453)),
    row(283.4, ("delà de 60EUR d'achats sur présentation du", 106, 277)),
    row(288.0, ("OFFERTE au-delà de 200EUR d'achats*", 352, 496)),
    row(315.0, ("Retour ou avoir possibles sous 15 jours avec le ticket d'origine.", 40, 531)),
    row(580.9, ("Total Brut HT:", 384, 437), ("27,41€", 498, 539)),
    row(596.3, ("Remise:", 384, 415)),
    row(614.0, ("Port HT :", 384, 418), ("€", 532, 539)),
    row(629.6, ("Total Net HT:", 384, 435), ("27,41€", 498, 539)),
    row(645.1, ("Total T.V.A.:", 384, 431), ("5,48€", 498, 539)),
    row(659.9, ("Total T.T.C (euros) :", 384, 461), ("32,89€", 498, 539)),
    row(692.3, ("Nb article(s) :", 365, 415), ("2", 421, 426), ("Poids total :0,12kg", 472, 546)),
    row(709.9, ("Règlement(s) :", 365, 421), ("Carte bancaire 32.89€ le 12/02/2026", 425, 541)),
    row(734.6, ("Taxe", 40, 60), ("Base", 101, 121), ("Taux", 151, 171), ("Montant", 194, 227)),
    row(746.2, ("Taux normal", 31, 78), ("27,41€", 97, 123), ("20%", 152, 170), ("5,48€", 197, 223)),
]

# An English web shop, VAT at 0 %: each description wraps onto the line
# under it, the euro sign is glued to every figure, and the delivery is
# printed among the totals under the table.
WEB_SHOP = [
    row(29.2, ("INVOICE", 24, 123)),
    row(33.0, ("Invoice number", 215, 282), ("Invoice total", 519, 572)),
    row(46.5, ("FAC-000777", 215, 266), ("€46.70", 543, 572)),
    row(91.5, ("Date of issue", 215, 271), ("Additional details", 497, 572)),
    row(105.0, ("March 3, 2026", 215, 281)),
    row(176.2, ("Bill to", 24, 48), ("Ship to", 215, 245), ("Merchant", 531, 572)),
    row(189.7, ("Client Exemple", 24, 70), ("Client Exemple", 215, 260), ("NUISIBLES EXEMPLE", 503, 572)),
    row(207.7, ("1 rue de la Soif", 24, 156), ("1 rue de la Soif", 215, 347), ("3 rue des Exemples", 467, 572)),
    row(234.7, ("75000 Paris", 24, 73), ("75000 Paris", 215, 264), ("69000 Lyon", 513, 572)),
    row(248.2, ("France", 24, 53), ("France", 215, 244), ("France", 554, 572)),
    row(331.5, ("Description", 24, 74), ("Quantity", 276, 314), ("Unit price", 354, 396), ("VAT rate", 441, 478), ("Amount", 538, 572)),
    row(355.5, ("Piège à phéromones PRO-TRAP grand", 24, 200), ("1", 310, 314), ("€24.50", 365, 396), ("0%", 463, 478), ("€24.50", 542, 572)),
    row(366.7, ("modèle  boîte de 5 recharges", 24, 152)),
    row(390.7, ("Gel appât fourmis ANT-STOP tube", 24, 218), ("2", 310, 314), ("€7.65", 365, 396), ("0%", 463, 478), ("€15.30", 542, 572)),
    row(402.0, ("de 25 g", 24, 174)),
    row(450.0, ("Subtotal", 326, 362), ("€39.80", 542, 572)),
    row(474.0, ("VAT 0%", 326, 366), ("€0.00", 547, 572)),
    row(498.0, ("Delivery", 326, 364), ("€6.90", 547, 572)),
    row(522.0, ("Delivery VAT 0%", 326, 407), ("€0.00", 547, 572)),
    row(546.0, ("Total", 326, 347), ("€46.70", 543, 572)),
    row(804.0, ("Provided by: NUISIBLES EXEMPLE", 24, 149), ("Issued on March 3, 2026", 462, 572)),
]

# A photographed paper invoice with no header: a quantity column of lone
# "1"s in front of the names, unit prices, amounts. Positions in pixels.
LINEN_INVOICE = [
    row(120, ("LINGE EXEMPLE", 400, 900)),
    row(180, ("FACTURE N° 2026-0187", 300, 1000)),
    row(240, ("Date : 03/02/2026", 300, 800)),
    row(330, ("1", 150, 175), ("TORCHON COTON 200G", 260, 820), ("14,90", 1100, 1210), ("14,90", 1400, 1510)),
    row(392, ("6", 150, 175), ("SERVIETTE NOIRE", 260, 700), ("2,10", 1120, 1210), ("12,60", 1400, 1510)),
    row(455, ("1", 150, 175), ("TABLIER BAR", 260, 600), ("9,80", 1120, 1210), ("9,80", 1420, 1510)),
    row(560, ("TOTAL TTC", 300, 620), ("37,30", 1400, 1510)),
    row(620, ("CB", 300, 380), ("37,30", 1400, 1510)),
    row(700, ("TVA 20% incluse", 300, 700), ("6,22", 1400, 1510)),
]

# A grocer's web shop: a header over three lines ("Prix / Unitaire / HT",
# "Prix / Unitaire / TTC", "Montant / Total / TTC"), the delivery as a row
# at 0 % whose 8,00 € is printed as its unit price, its unit price TTC, its
# amount and again as « Frais d'expédition » under the table - four times,
# where the total is printed once. A reference wraps onto the next line and
# a brand line stands under the last row.
GROCERY_WEB_SHOP = [
    row(81.9, ("EPICERIE EXEMPLE", 60, 222), ("Facture n°EPI-F-2026-", 352, 500)),
    row(93.9, ("https://www.epicerie-exemple.example", 60, 233)),
    row(97.1, ("000321", 352, 398)),
    row(109.6, ("Du", 352, 365), ("14/03/2026 18:02", 370, 451)),
    row(118.2, ("12 Centre Commercial des Exemples", 60, 220)),
    row(128.7, ("78000 EXEMPLEVILLE", 60, 170), ("Commande n° : 000842", 352, 451)),
    row(139.2, ("Tél : 0100000000", 60, 137), ("Client n° : 12", 352, 411)),
    row(158.5, ("Règlement : carte bancaire sur le site", 352, 525)),
    row(167.9, ("marchand", 352, 387)),
    row(177.2, ("Livraison : REMISE EN POINT RELAIS", 352, 506)),
    row(206.3, ("Adresse de livraison :", 62, 160), ("Adresse de facturation :", 355, 464)),
    row(221.2, ("Client Exemple", 62, 118), ("Client Exemple", 355, 410)),
    row(230.5, ("RELAIS EXEMPLE", 62, 103), ("Le Bar Exemple", 355, 390)),
    row(239.9, ("2 AVENUE DES EXEMPLES", 62, 190), ("1 rue de la Soif", 355, 432)),
    row(249.2, ("75000 PARIS", 62, 113), ("75000 PARIS", 355, 406)),
    row(258.6, ("France", 62, 89), ("France", 355, 381)),
    row(314.3, ("Votre commande :", 57, 149)),
    row(338.5, ("Image", 72, 100), ("Référence", 121, 167), ("Désignation", 229, 283), ("Prix", 354, 372), ("Prix", 401, 419), ("Qté", 437, 453), ("TVA", 467, 484), ("Montant", 498, 536)),
    row(347.8, ("Unitaire", 345, 381), ("Unitaire", 392, 428), ("Total", 506, 528)),
    row(357.2, ("HT", 357, 369), ("TTC", 402, 418), ("TTC", 508, 525)),
    row(372.5, ("Livraison : REMISE EN POINT RELAIS", 119, 273), ("8,00 €", 354, 384), ("8,00 €", 400, 431), ("1", 449, 454), ("0 %", 477, 492), ("8,00 €", 505, 536)),
    row(390.5, ("PureeAman", 119, 164), ("Purée d'amande 2 kg", 175, 260), ("26,00 €", 354, 384), ("27,43 €", 400, 431), ("3", 449, 454), ("5.5 %", 470, 492), ("82,29 €", 505, 536)),
    row(399.9, ("Bio", 119, 126)),
    row(418.6, ("Votre Marque", 175, 229)),
    row(443.8, ("Frais d'expédition", 385, 455), ("8,00 €", 506, 537)),
    row(459.1, ("Total HT", 426, 458), ("86,00 €", 506, 537)),
    row(474.5, ("TVA 5.5 %", 417, 458), ("4,29 €", 511, 537)),
    row(489.8, ("TOTAL TTC", 410, 458), ("90,29 €", 497, 537)),
    row(768.9, ("EPICERIE EXEMPLE | 12 Centre Commercial des Exemples 78000 EXEMPLEVILLE", 64, 442), ("| RCS : 00000000000000 |", 446, 531)),
    row(776.5, ("TVA : FR00000000000", 262, 334)),
    row(784.1, ("Tél : 0100000000 | https://www.epicerie-exemple.example", 157, 345), ("| Capital social : 2 500,00 €", 349, 438)),
    row(791.7, ("Les articles peuvent être retournés sous quinze jours dans leur emballage d'origine.", 144, 451)),
]

# A DIY store's photographed ticket (pixels, the recogniser's lines): a code,
# the name and the amount under a header, an EAN line and a department line
# between the rows, a pack count in the name ("D4 X4") with no quantity
# column, and the VAT table printed as a table of its own.
PHOTO_DIY_TICKET = [
    photo_row(264.4, ("Brico Exemple", 747, 1673)),
    photo_row(436.0, ("12 rue des Outils", 1004, 1399)),
    photo_row(478.4, ("75000 - PARIS", 1078, 1321)),
    photo_row(669.7, ("CODE", 694, 777), ("NOM DU PRODUIT", 942, 1201), ("MONTANT TVA", 1494, 1710)),
    photo_row(767.6, ("** PEINTURE / MENAGE **", 960, 1453)),
    photo_row(814.5, ("20000003", 694, 845), ("SAC CABAS REUTILISABLE", 941, 1469), ("0,30", 1548, 1635), ("1", 1657, 1701)),
    photo_row(861.3, ("EAN : 3000000001011", 939, 1184)),
    photo_row(909.1, ("** QUINCAILLERIE **", 956, 1451)),
    photo_row(956.8, ("20000005", 689, 842), ("CHAINE 200KG D4 X4", 937, 1309), ("14,40", 1548, 1635), ("1", 1657, 1701)),
    photo_row(1004.6, ("EAN : 2000000002026", 937, 1288)),
    photo_row(1050.6, ("** PLOMBERIE **", 1059, 1342)),
    photo_row(1102.2, ("20000006", 685, 837), ("ROBINET LAITON 1/2", 935, 1303), ("8,70", 1548, 1633), ("1", 1657, 1701)),
    photo_row(1148.7, ("EAN : 2600000003032", 932, 1286)),
    photo_row(1509.6, ("TOTAL", 689, 800), ("23,40 €", 1539, 1689)),
    photo_row(1609.5, ("CB", 689, 742), ("23,40 €", 1541, 1686)),
    photo_row(1707.4, ("TVA", 823, 893), ("TAUX", 934, 1022), ("VAL TVA", 1131, 1272), ("MONTANT HT", 1349, 1543)),
    photo_row(1806.0, ("1", 844, 868), ("20,00", 912, 1015), ("3,90", 1168, 1252), ("19,50", 1441, 1539)),
    photo_row(1856.2, ("TOTAUX TVA", 826, 1013), ("3,90", 1166, 1254), ("19,50", 1437, 1543)),
    photo_row(1955.7, ("NOMBRE DE PRODUITS", 671, 1004), ("3", 1666, 1698)),
]

# An electronics till's photographed ticket: the article's family and code
# on the line above the row, its description wrapped under it, and a
# discount printed under the article, with its own row of figures.
PHOTO_DISCOUNT_TICKET = [
    photo_row(321.1, ("Ticket de caisse 300000001", 644, 1110)),
    photo_row(361.9, ("12/02/2026 / 16H40 Caisse 2", 681, 1066)),
    photo_row(584.8, ("Désignation", 33, 221), ("Qté", 870, 933), ("Garantie", 960, 1089), ("PU HT", 1168, 1268), ("TVA", 1272, 1340), ("PU TTC", 1380, 1495), ("TOTAL", 1558, 1664)),
    photo_row(634.3, ("2200001-PETIT ELECTROMENAGER", 64, 667)),
    photo_row(670.7, ("BOUILLOIRE INOX SANS FIL", 65, 741), ("1", 889, 915), ("27,01 €", 1159, 1270), ("C", 1293, 1324), ("28,50 €", 1388, 1498), ("28,50 €", 1559, 1669)),
    photo_row(705.2, ("COLORIS GRIS (1)", 65, 631)),
    photo_row(734.2, ("Remise immédiate", 98, 309), ("-12,32 €", 1169, 1270), ("C", 1296, 1321), ("13,00 €", 1409, 1499), ("-13,00 €", 1571, 1670)),
    photo_row(1540.9, ("SOUS TOTAL", 1297, 1486), ("15,50 €", 1543, 1652)),
    photo_row(1655.3, ("TOTAL NET TTC", 63, 303), ("15,50 €", 1543, 1649)),
    photo_row(1711.8, ("DONT TOTAL REMISES", 65, 339), ("13,00 €", 1558, 1647)),
    photo_row(1756.9, ("(1) Garantie légale : deux ans à compter de la livraison du produit.", 72, 740)),
    photo_row(1893.4, ("Code", 74, 155), ("Taux", 182, 259), ("Mt HT", 332, 420), ("Mt TVA", 479, 579), ("Mt TTC", 639, 739), ("Réglé ce jour", 1007, 1198)),
    photo_row(1916.5, ("Carte Bancaire", 1301, 1498), ("15,50 €", 1548, 1652)),
    photo_row(1951.5, ("C", 95, 125), ("5,50 %", 163, 259), ("14,69 €", 321, 421), ("0,81 €", 495, 583), ("15,50 €", 641, 740)),
    photo_row(2011.1, ("Total", 72, 149), ("14,69 €", 322, 422), ("0,81 €", 496, 583), ("15,50 €", 641, 740)),
]

# A till ticket: names and amounts, no table.
TILL_TICKET = [
    row(100, ("FRANPRIX EXEMPLE", 200, 700)),
    row(160, ("BAGUETTE TRADITION", 100, 500), ("1.20", 800, 950)),
    row(210, ("CITRON X4", 100, 400), ("2.29", 800, 950)),
    row(270, ("TOTAL A PAYER", 100, 450), ("3.49", 850, 950)),
    row(320, ("CB", 100, 200), ("3.49", 850, 950)),
    row(380, ("TVA 5.5%  3.31  0.18  3.49", 100, 950)),
]


class SameTextSameReadingTests(SimpleTestCase):
    """Positions add a reading; the text one is untouched. A page with no
    positions - a stored reading parsed again by `parse_text`, a hand-written
    fixture - reads exactly as it did before this module existed."""

    def assertReadsAsText(self, rows):
        text = text_of(rows)
        without = READER.parse_pages([text_only(rows)])
        stored = READER.parse_text(text)
        self.assertEqual(lines_of(without), lines_of(stored))
        self.assertEqual([(c.label, c.passed, c.detail) for c in without.checks], [(c.label, c.passed, c.detail) for c in stored.checks])
        self.assertIsNone(table_check(without), "a page without positions has no table to recognise")
        return without

    def test_every_layout_read_without_its_positions_is_the_stored_reading(self):
        for name, rows in (
            ("ice", ICE_INVOICE), ("computer", COMPUTER_SHOP), ("web", WEB_SHOP),
            ("linen", LINEN_INVOICE), ("grocery", GROCERY_WEB_SHOP), ("till", TILL_TICKET),
            ("photo diy", PHOTO_DIY_TICKET), ("photo discount", PHOTO_DISCOUNT_TICKET),
        ):
            with self.subTest(layout=name):
                self.assertReadsAsText(rows)

    def test_the_text_reading_of_the_ice_invoice_is_the_misreading_the_positions_fix(self):
        # What the owner saw: the footer's "Capital" line read as an article.
        without = READER.parse_pages([text_only(ICE_INVOICE)])
        self.assertIn("Capital", [line.raw_name for line in without.lines])

    def test_a_page_whose_rows_do_not_match_its_text_is_read_as_text_alone(self):
        rows = ICE_INVOICE
        page = PdfPage(text=text_of(rows), tables=[], rows=rows[:-1])
        parsed = READER.parse_pages([page])
        self.assertEqual(lines_of(parsed), lines_of(READER.parse_text(text_of(rows))))
        self.assertIsNone(table_check(parsed))


class NoTableTests(SimpleTestCase):
    def test_a_till_ticket_with_positions_falls_through_to_the_text_logic(self):
        """No table on the page: the positions change nothing, and the page
        carries no « Tableau reconnu » check - nothing was recognised."""
        with_positions = READER.parse_pages([positioned(TILL_TICKET)])
        without = READER.parse_pages([text_only(TILL_TICKET)])
        self.assertEqual(lines_of(with_positions), lines_of(without))
        self.assertEqual([(c.label, c.passed) for c in with_positions.checks], [(c.label, c.passed) for c in without.checks])
        self.assertIsNone(table_check(with_positions))
        self.assertEqual([line.raw_name for line in with_positions.lines], ["BAGUETTE TRADITION", "CITRON X4"])


class IceInvoiceTests(SimpleTestCase):
    def setUp(self):
        self.parsed = READER.parse_pages([positioned(ICE_INVOICE)])

    def test_the_rows_under_the_three_line_header_are_the_purchase_in_ht(self):
        self.assertEqual(
            lines_of(self.parsed),
            [
                ("Sac de glace pilée 10 kg", 2, D("24.80"), D("0.055"), None),
                ("Frais de port", 1, D("6.00"), D("0.20"), None),
            ],
        )
        self.assertEqual(self.parsed.printed_total_ttc, D("33.36"))

    def test_the_documents_head_and_its_totals_are_never_items(self):
        names = [line.raw_name for line in self.parsed.lines]
        for word in ("Capital", "Total", "Net HT", "Désignation", "Référence"):
            self.assertFalse(any(word in name for name in names), f"{word!r} read as an article: {names}")

    def test_the_vat_table_is_rebuilt_from_the_rows_that_each_print_their_tax(self):
        """No VAT table is printed under this table; every row prints its
        rate and its tax, confirmed by its own arithmetic (24,80 x 5,5 % =
        1,36), and the rows make what was paid. One bucket a rate."""
        self.assertEqual(
            [(r, b, t) for r, b, t in self.parsed.vat_breakdown],
            [(D("0.055"), D("24.80"), D("1.36")), (D("0.20"), D("6.00"), D("1.20"))],
        )
        self.assertEqual(failed(self.parsed), {})

    def test_the_check_says_what_the_layout_recognised_in_french(self):
        check = table_check(self.parsed)
        self.assertIsNotNone(check)
        self.assertTrue(check.passed)
        self.assertIn(
            "colonnes : référence, désignation, quantité, prix unitaire HT, taux de TVA, montant TVA, montant HT",
            check.detail,
        )
        self.assertIn("en-tête sur 3 lignes", check.detail)
        self.assertIn("lignes avant le tableau écartées", check.detail)
        self.assertIn("le nom est la désignation seule", check.detail)
        self.assertIn("table de TVA reconstituée depuis les lignes du tableau", check.detail)
        self.assertIn("2 lignes du tableau retenues parmi les articles", check.detail)

    def test_parse_ocr_pages_carries_the_positions_to_the_reader(self):
        """What `receipts.read_receipt` hands the parser: OCR pages, whose
        cells become the rows. The same reading, the same check."""
        page = OcrPage(lines=[
            OcrLine(cells=[OcrCell(cell.text, cell.x0, cell.x1, 0.99) for cell in r.cells], y=r.y) for r in ICE_INVOICE
        ])
        parsed = READER.parse_ocr_pages([page])
        self.assertEqual(lines_of(parsed), lines_of(self.parsed))
        self.assertIsNotNone(table_check(parsed))
        self.assertTrue(table_check(parsed).passed)


class ComputerShopTests(SimpleTestCase):
    def setUp(self):
        self.parsed = READER.parse_pages([positioned(COMPUTER_SHOP)])

    def test_line_numbers_and_article_codes_stay_out_of_the_names(self):
        self.assertEqual(
            [(line.raw_name, line.quantity) for line in self.parsed.lines],
            [("Câble HDMI 2 m tressé", 1), ("Adaptateur USB-C vers Jack", 3)],
        )

    def test_the_amounts_are_ttc_and_the_printed_table_is_the_vat_table(self):
        self.assertEqual([line.printed_ttc for line in self.parsed.lines], [D("7.99"), D("24.90")])
        self.assertEqual([line.total_ht for line in self.parsed.lines], [D("6.66"), D("20.75")])
        self.assertEqual(self.parsed.printed_total_ttc, D("32.89"))
        self.assertEqual([(r, b, t) for r, b, t in self.parsed.vat_breakdown], [(D("0.2"), D("27.41"), D("5.48"))])
        self.assertEqual(failed(self.parsed), {})
        # The table under the totals was read; nothing was rebuilt from the rows.
        self.assertNotIn("reconstituée", table_check(self.parsed).detail)

    def test_the_delivery_terms_under_the_table_are_not_a_wrapped_name(self):
        """Two rows' worth under the last article, the shop's terms are not
        its description: a wrapped line sits as close under its row as the
        rows are to each other."""
        self.assertNotIn("Livraison", self.parsed.lines[1].raw_name)


class WebShopTests(SimpleTestCase):
    def setUp(self):
        self.parsed = READER.parse_pages([positioned(WEB_SHOP)])

    def test_a_wrapped_description_belongs_to_its_row(self):
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc) for line in self.parsed.lines],
            [
                ("Piège à phéromones PRO-TRAP grand modèle boîte de 5 recharges", 1, D("24.50")),
                ("Gel appât fourmis ANT-STOP tube de 25 g", 2, D("15.30")),
                ("Delivery", 1, D("6.90")),
            ],
        )
        self.assertEqual(self.parsed.printed_total_ttc, D("46.70"))

    def test_the_rows_rate_is_the_documents_only_legible_rate(self):
        """At 0 % the amounts are HT and TTC at once, and the delivery under
        the table takes the one rate the rows print. No VAT table was read,
        and the check says so rather than passing on the rows' say-so."""
        self.assertEqual([line.vat_rate for line in self.parsed.lines], [D("0"), D("0"), D("0")])
        self.assertEqual([line.total_ht for line in self.parsed.lines], [D("24.50"), D("15.30"), D("6.90")])
        self.assertEqual(set(failed(self.parsed)), {"Table TVA lue"})
        self.assertIn("2 désignations sur deux lignes", table_check(self.parsed).detail)


class LinenInvoiceTests(SimpleTestCase):
    def setUp(self):
        self.parsed = READER.parse_pages([positioned(LINEN_INVOICE)])

    def test_a_column_of_lone_ones_is_the_quantity_not_the_name(self):
        """Without a header, the columns are told by alignment and by the
        arithmetic (6 x 2,10 = 12,60); a "1" in that column is a count of one,
        never "1 TORCHON"."""
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc) for line in self.parsed.lines],
            [("TORCHON COTON 200G", 1, D("14.90")), ("SERVIETTE NOIRE", 6, D("12.60")), ("TABLIER BAR", 1, D("9.80"))],
        )
        self.assertEqual(failed(self.parsed), {})
        self.assertIn("sans en-tête", table_check(self.parsed).detail)

    def test_read_as_text_the_lone_one_stays_in_the_name(self):
        without = READER.parse_pages([text_only(LINEN_INVOICE)])
        self.assertIn("1 TORCHON COTON 200G", [line.raw_name for line in without.lines])


class GroceryWebShopTests(SimpleTestCase):
    def setUp(self):
        self.parsed = READER.parse_pages([positioned(GROCERY_WEB_SHOP)])

    def test_a_delivery_printed_four_times_is_not_the_total(self):
        """Repetition would make 8,00 € the amount paid (printed four times,
        where 90,29 € is printed once). The table's rows, each proven TTC,
        add up to the total printed under it."""
        self.assertEqual(self.parsed.printed_total_ttc, D("90.29"))
        self.assertEqual(
            lines_of(self.parsed),
            [
                ("Livraison : REMISE EN POINT RELAIS", 1, D("8.00"), D("0"), D("8.00")),
                ("Purée d'amande 2 kg", 3, D("78.00"), D("0.055"), D("82.29")),
            ],
        )

    def test_the_rows_prove_their_rates_and_every_check_passes(self):
        """Two unit prices one rate apart (26,00 x 1,055 = 27,43) and a 0 %
        row: the breakdown is the rows', and it is what the footer prints."""
        self.assertEqual(
            [(r, b, t) for r, b, t in self.parsed.vat_breakdown],
            [(D("0"), D("8.00"), D("0.00")), (D("0.055"), D("78.00"), D("4.29"))],
        )
        self.assertEqual(failed(self.parsed), {})

    def test_a_reference_wrapped_and_a_brand_line_are_not_the_name(self):
        self.assertEqual(self.parsed.lines[1].raw_name, "Purée d'amande 2 kg")

    def test_the_qualifiers_on_the_third_header_line_name_the_columns(self):
        """"HT | TTC | TTC" alone under "Prix / Prix / Montant" names no column
        and is the header's third line all the same: it is what tells the two
        unit prices apart on the review screen."""
        detail = table_check(self.parsed).detail
        self.assertIn("en-tête sur 3 lignes", detail)
        self.assertIn(
            "colonnes : référence, désignation, prix unitaire HT, prix unitaire TTC, quantité, taux de TVA, montant TTC",
            detail,
        )


class PhotographedTableTests(SimpleTestCase):
    """Photographed tables: the columns place the rows, and everything the
    text reading already did right - the detail lines, the discounts, a
    count printed in the name - it goes on doing."""

    def test_the_header_and_the_ean_and_department_lines_are_never_items_nor_names(self):
        parsed = READER.parse_pages([positioned(PHOTO_DIY_TICKET)])
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc, line.total_ht) for line in parsed.lines],
            [
                ("SAC CABAS REUTILISABLE", 1, D("0.30"), D("0.25")),
                ("CHAINE 200KG D", 4, D("14.40"), D("12.00")),
                ("ROBINET LAITON 1/2", 1, D("8.70"), D("7.25")),
            ],
        )
        self.assertEqual(parsed.printed_total_ttc, D("23.40"))
        self.assertEqual(failed(parsed), {})
        check = table_check(parsed)
        self.assertIn("colonnes : référence, désignation, montant", check.detail)
        self.assertNotIn("sur deux lignes", check.detail)

    def test_a_count_in_the_name_is_the_texts_when_the_table_has_no_quantity_column(self):
        """"CHAINE 200KG D4 X4" is four; the table prints no quantity, so the
        count stays what the text reading makes of the name, as before - and
        so does the name it cut the count out of ("CHAINE 200KG D"), which is
        what the tickets already checked hold. The check says so."""
        parsed = READER.parse_pages([positioned(PHOTO_DIY_TICKET)])
        self.assertEqual(parsed.lines[1].quantity, 4)
        self.assertEqual(parsed.lines[1].unit_cost_ht, D("3.00"))
        detail = table_check(parsed).detail
        self.assertIn("nom et quantité repris de la lecture texte sur 1 ligne (quantité imprimée dans le nom)", detail)
        # No quantity, unit price or rate column on this ticket: the check
        # claims none of them.
        self.assertIn("le montant vient de sa colonne", detail)
        self.assertNotIn("la quantité", detail)

    def test_a_discount_under_the_article_still_reaches_the_text_reading(self):
        """The discount row is inside the table and is no item: left to the
        text reading, it is the promotion on the article above, as before.
        The description under the row is not joined to the name on a
        photograph, and the family line above it is not the name either."""
        parsed = READER.parse_pages([positioned(PHOTO_DISCOUNT_TICKET)])
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc, line.discount_ttc, line.total_ht, line.vat_rate) for line in parsed.lines],
            [("BOUILLOIRE INOX SANS FIL", 1, D("28.50"), D("13.00"), D("14.69"), D("0.055"))],
        )
        self.assertEqual(parsed.printed_total_ttc, D("15.50"))
        self.assertEqual(failed(parsed), {})
        self.assertIn("Remise attribuée", [check.label for check in parsed.checks])

    def test_read_as_text_the_same_photographed_tickets_read_the_same_lines(self):
        """What the positions change on these tickets is what the check says:
        the names, the counts and the amounts are the ones the text reading
        gives and the tickets already checked hold."""
        for rows in (PHOTO_DIY_TICKET, PHOTO_DISCOUNT_TICKET):
            with self.subTest(rows=rows[0].text):
                with_positions = READER.parse_pages([positioned(rows)])
                without = READER.parse_pages([text_only(rows)])
                self.assertEqual(
                    [(line.raw_name, line.quantity, line.total_ht, line.printed_ttc) for line in with_positions.lines],
                    [(line.raw_name, line.quantity, line.total_ht, line.printed_ttc) for line in without.lines],
                )


class CalculationInTheDescriptionTests(SimpleTestCase):
    def test_a_description_that_is_a_calculation_leaves_the_row_to_the_text_reading(self):
        """"12,50 €/m2 x 0,200 m2" in the description column is no name: the
        text reading names the row after the line above, as it always did."""
        rows = [
            photo_row(669.7, ("CODE", 694, 777), ("NOM DU PRODUIT", 942, 1201), ("MONTANT TVA", 1494, 1710)),
            photo_row(1043.4, ("** PANNEAU, VERRE **", 1052, 1432)),
            photo_row(1092.6, ("20000007 PANNEAU MDF 10MM", 712, 1205)),
            photo_row(1139.4, ("12.50 €/m2 x 0,200m2", 763, 1150), ("2,50", 1598, 1687), ("1", 1670, 1760)),
            photo_row(1288.5, ("20000008", 713, 879), ("DECOUPE SUR MESURE", 888, 1300), ("3,00", 1598, 1689), ("1", 1716, 1754)),
            photo_row(1455.3, ("TOTAL", 729, 844), ("5,50 €", 1615, 1754)),
            photo_row(1609.5, ("CB", 689, 742), ("5,50 €", 1541, 1686)),
            photo_row(1707.4, ("TVA", 823, 893), ("TAUX", 934, 1022), ("VAL TVA", 1131, 1272), ("MONTANT HT", 1349, 1543)),
            photo_row(1806.0, ("1", 844, 868), ("20,00", 912, 1015), ("0,92", 1168, 1252), ("4,58", 1441, 1539)),
        ]
        parsed = READER.parse_pages([positioned(rows)])
        self.assertEqual([line.raw_name for line in parsed.lines], ["PANNEAU MDF 10MM", "DECOUPE SUR MESURE"])
        self.assertEqual([line.printed_ttc for line in parsed.lines], [D("2.50"), D("3.00")])
        check = table_check(parsed)
        self.assertTrue(check.passed)
        self.assertIn("1 ligne du tableau retenue parmi les articles", check.detail)
        # The calculation row was handed to the text reading on purpose, and
        # the check says so rather than counting it against the table.
        self.assertIn("1 ligne du tableau dont la désignation n'est pas un nom, lue en texte", check.detail)
        without = READER.parse_pages([text_only(rows)])
        self.assertEqual([line.raw_name for line in without.lines], [line.raw_name for line in parsed.lines])


# -- the findings of the review, each a test that failed first --------------


class HandWrittenPdfTests(SimpleTestCase):
    """The upload flow's own path: a PDF's words through `ocr.text_layer_pages`,
    then `rows_from_ocr`. A hand-written PDF prints each line as ONE string,
    so the figures of a row sit wherever the words before them end and
    nothing aligns from one row to the next: that is no table, and the text
    reading keeps the page - as it did before positions existed."""

    def read(self, text):
        import os
        import tempfile

        from invoices.ocr import text_layer_pages
        from invoices.tests.pdf_files import write_pdf

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "facture.pdf")
            write_pdf(path, text.replace("€", "EUR").split("\n"))
            (page,) = text_layer_pages(path)
        return page

    def test_one_string_lines_whose_figures_do_not_align_are_no_table(self):
        from invoices.parsers.layout import find_table, rows_from_ocr
        from invoices.tests.test_generic_tables import WEB_INVOICE

        page = self.read(WEB_INVOICE)
        self.assertIsNone(find_table(rows_from_ocr(page)))
        parsed = READER.parse_ocr_pages([page])
        self.assertEqual(lines_of(parsed), lines_of(READER.parse_text(page.text)))
        self.assertIsNone(table_check(parsed))
        self.assertEqual(parsed.lines[0].raw_name, "WEBF -AB123 Verre à shot Olympia (lot de 12)")
        self.assertEqual([line.quantity for line in parsed.lines], [8, 2, 1])
        self.assertTrue(all(isinstance(line.quantity, int) for line in parsed.lines))

    def test_the_same_rows_printing_no_count_are_no_table_either(self):
        """Without a count on the rows nothing multiplies out, and the
        rightmost figure is not the amount by default: the name is whole."""
        from invoices.tests.test_generic_tables import WEB_INVOICE

        no_count = WEB_INVOICE.replace("(lot de 12)  8  3,50  28,00", "(lot de 12)  28,00").replace("inox 20cm  2  4,25  8,50", "inox 20cm  8,50")
        page = self.read(no_count)
        parsed = READER.parse_ocr_pages([page])
        self.assertIsNone(table_check(parsed))
        self.assertEqual(parsed.lines[0].raw_name, "WEBF -AB123 Verre à shot Olympia (lot de 12)")


# A till's invoice layout: unit price HT, count, HT, the rate as a bare
# "5,50", the tax, then the row's TTC as printed.
TAXED_ROWS_INVOICE = [
    row(1079, ("Description", 175, 423), ("Prix unitaire H.T", 466, 570), ("Qté", 615, 634), ("Prix H.T", 681, 783), ("TVA", 811, 884), ("Montant", 916, 1003), ("Remise TTC", 1057, 1146), ("Prix TTC", 1271, 1371)),
    row(1150, ("RUBAN DE MASQUAGE", 175, 423), ("3,33 €", 466, 570), ("1", 615, 634), ("3,33 €", 681, 783), ("20,00", 811, 884), ("0,67 €", 916, 1003), ("4,00 €", 1271, 1371)),
    row(1175, ("PITA LIBANAISE", 175, 423), ("6,64 €", 466, 570), ("1", 615, 634), ("6,64 €", 681, 783), ("5,50", 811, 884), ("0,37 €", 916, 1003), ("7,01 €", 1271, 1371)),
    row(1300, ("Total HT", 700, 783), ("9,97 €", 916, 1003)),
    row(1325, ("Total TVA", 700, 783), ("1,04 €", 916, 1003)),
    row(1350, ("Total TTC", 700, 783), ("11,01 €", 1271, 1371)),
    row(1375, ("CB", 700, 740), ("11,01 €", 1271, 1371)),
    row(1400, ("Taux", 175, 250), ("Base HT", 466, 570), ("TVA", 916, 1003), ("TTC", 1271, 1371)),
    row(1425, ("20,00 %", 175, 250), ("3,33 €", 466, 570), ("0,67 €", 916, 1003), ("4,00 €", 1271, 1371)),
    row(1450, ("5,50 %", 175, 250), ("6,64 €", 466, 570), ("0,37 €", 916, 1003), ("7,01 €", 1271, 1371)),
]


class PrintedTtcTests(SimpleTestCase):
    def test_a_row_in_ht_keeps_the_ttc_it_prints(self):
        """"A receipt line keeps its printed TTC": read through its columns the
        row lost it (`printed_ttc` None), and every screen showing the line
        in TTC worked it back out of the HT. On this layout the text reading
        kept it (`_taxed_row`); the two readings have to agree."""
        parsed = READER.parse_pages([positioned(TAXED_ROWS_INVOICE)])
        self.assertEqual(
            [(line.raw_name, line.total_ht, line.vat_rate, line.printed_ttc) for line in parsed.lines],
            [("RUBAN DE MASQUAGE", D("3.33"), D("0.20"), D("4.00")), ("PITA LIBANAISE", D("6.64"), D("0.055"), D("7.01"))],
        )
        self.assertEqual(parsed.printed_total_ttc, D("11.01"))
        self.assertEqual(failed(parsed), {})

    def test_read_as_text_the_same_rows_keep_it_too(self):
        without = READER.parse_pages([text_only(TAXED_ROWS_INVOICE)])
        self.assertEqual([line.printed_ttc for line in without.lines], [D("4.00"), D("7.01")])
        self.assertEqual([line.total_ht for line in without.lines], [D("3.33"), D("6.64")])


# A DIY store's photographed ticket whose recogniser grouped a department
# heading with the amount of the product printed on the next line.
PHOTO_HEADING_TICKET = [
    photo_row(924, ("CODE", 641, 747), ("NOM DU PRODUIT", 852, 1150), ("MONTANT TVA", 1562, 1807)),
    photo_row(1058, ("** DROGUERIE / OUTILLAGE **", 955, 1520), ("16,90", 1606, 1728)),
    photo_row(1101, ("20000009 MANCHE TELESCOPIQUE", 646, 1258)),
    photo_row(1155, ("EAN : 3000000001011", 858, 1256)),
    photo_row(1276, ("TOTAL", 664, 791), ("16,90 €", 1626, 1800)),
    photo_row(1385, ("CB", 668, 812), ("16,90 €", 1633, 1797)),
    photo_row(1499, ("TVA", 826, 907), ("TAUX", 951, 1050), ("VAL TVA", 1178, 1339), ("MONTANT HT", 1418, 1638)),
    photo_row(1611, ("1", 858, 886), ("20,00", 930, 1048), ("2,82", 1221, 1316), ("14,08", 1524, 1643)),
]


class HeadingWithAnAmountTests(SimpleTestCase):
    def test_a_department_heading_is_never_the_name_and_its_amount_goes_to_the_product_under_it(self):
        """The heading row overlaps the name column and carries the amount, so
        the table took it for the item. The text reading's own rule - an
        amount glued to a heading belongs to the name printed under it - has
        the say first, with positions as without."""
        expected = [("MANCHE TELESCOPIQUE", 1, D("14.08"), D("0.20"), D("16.90"))]
        with_positions = READER.parse_pages([positioned(PHOTO_HEADING_TICKET)])
        self.assertEqual(lines_of(with_positions), expected)
        self.assertEqual(failed(with_positions), {})
        self.assertEqual(with_positions.lines[0].category, "DROGUERIE / OUTILLAGE")
        detail = table_check(with_positions).detail
        self.assertIn("1 ligne du tableau dont la désignation n'est pas un nom, lue en texte", detail)
        self.assertIn("1 ligne lue en texte dans le tableau, complétée du montant imprimé sur la ligne au-dessus : « MANCHE TELESCOPIQUE »", detail)
        without = READER.parse_pages([text_only(PHOTO_HEADING_TICKET)])
        self.assertEqual(lines_of(without), expected)

    def test_a_heading_framed_by_stars_or_trailed_by_dots(self):
        from invoices.parsers.generic_receipt import heading_of

        self.assertEqual(heading_of("** DROGUERIE / OUTILLAGE **  16,90"), ("DROGUERIE / OUTILLAGE", D("16.90")))
        self.assertEqual(heading_of("** PLOMBERIE **"), ("PLOMBERIE", None))
        self.assertEqual(heading_of("FRUITS/LEGUMES. 2,51"), ("FRUITS/LEGUMES", D("2.51")))
        self.assertEqual(heading_of("EPICERIE/BOISSONS........"), ("EPICERIE/BOISSONS", None))
        for line in ("ROBINET LAITON 1/2  9,90", "** 2 X BAGUETTE **  2,40", "SAC CABAS  0,30", "BAGUETTE. 1,20"):
            with self.subTest(line=line):
                self.assertIsNone(heading_of(line))


# An electronics retailer's invoice: the speaker row proves the column HT
# (its two unit prices are one rate apart); the eco-participation under it
# prints a unit price that multiplies into nothing.
HT_COLUMN_INVOICE = [
    row(100, ("EAN", 48, 101), ("Référence", 114, 142), ("Quantité", 174, 178), ("Libellé", 183, 393), ("Prix Unitaire Net HT", 428, 456), ("Prix Unitaire Net TTC", 488, 516), ("Montant Net HT", 548, 576)),
    row(113, ("3000000001011", 48, 101), ("I 9000001", 114, 142), ("3", 174, 178), ("Enceinte exemple", 183, 393), ("120,00", 428, 456), ("144,00", 488, 516), ("360,00", 548, 576)),
    row(126, ("Eco-Participation DEEE", 183, 393), ("0,40", 428, 456), ("0,48", 488, 516), ("1,50", 548, 576)),
    row(160, ("Total HT", 405, 449), ("361,50", 548, 576)),
    row(173, ("TVA 20%", 405, 449), ("72,30", 548, 576)),
    row(186, ("TOTAL GENERAL TTC", 405, 459), ("433,80", 548, 576)),
]


class HtColumnReaderTests(SimpleTestCase):
    def test_every_row_of_a_proven_ht_column_is_read_in_ht(self):
        parsed = READER.parse_pages([positioned(HT_COLUMN_INVOICE)])
        self.assertEqual(
            lines_of(parsed),
            [("Enceinte exemple", 3, D("360.00"), D("0.20"), None), ("Eco-Participation DEEE", 1, D("1.50"), D("0.20"), None)],
        )
        self.assertEqual(parsed.printed_total_ttc, D("433.80"))
        self.assertEqual(failed(parsed), {})


class OneRowTableTests(SimpleTestCase):
    def test_a_sentence_under_the_single_row_is_not_its_name(self):
        rows = [
            row(100, ("Désignation", 77, 122), ("Qté", 323, 340), ("PU HT", 357, 384), ("Montant HT", 542, 585)),
            row(113, ("Frais de dossier", 77, 200), ("1", 323, 329), ("250,00", 357, 384), ("250,00", 542, 569)),
            row(126, ("En cas de rendez-vous manqué, toute absence non prévenue entraînera des frais.", 77, 500)),
            row(160, ("Total HT", 405, 449), ("250,00", 542, 569)),
            row(173, ("TVA 20%", 405, 449), ("50,00", 542, 569)),
            row(186, ("Total TTC", 405, 459), ("300,00", 542, 569)),
        ]
        parsed = READER.parse_pages([positioned(rows)])
        self.assertEqual(lines_of(parsed), [("Frais de dossier", 1, D("250.00"), D("0.20"), None)])
        self.assertEqual(failed(parsed), {})


class TableCheckTruthTests(SimpleTestCase):
    """« Tableau reconnu » says what the reader DID with the table, and fails
    when the reading and the columns disagree."""

    def test_a_body_row_the_columns_missed_and_the_text_read_fails_the_check(self):
        rows = [
            row(100, ("Référence", 12, 50), ("Désignation", 77, 122), ("Qté", 323, 340), ("PU HT", 357, 384), ("TVA", 455, 473), ("Montant HT", 542, 585)),
            row(113, ("AR01", 12, 40), ("Sac de glace 10 kg", 77, 170), ("2", 323, 329), ("12.40", 357, 384), ("5.5%", 455, 480), ("24.80", 542, 569)),
            row(126, ("Consigne bac", 77, 140), ("3.00", 420, 445)),
            row(139, ("AR02", 12, 40), ("Livraison", 77, 130), ("1", 323, 329), ("6.00", 357, 384), ("20%", 455, 477), ("6.00", 542, 563)),
            row(160, ("Total HT", 405, 449), ("33.80", 542, 569)),
            row(173, ("TVA 5.5%", 300, 349), ("24.80", 405, 449), ("1.36", 542, 569)),
            row(186, ("TVA 20%", 300, 349), ("9.00", 405, 449), ("1.80", 542, 569)),
            row(199, ("Total TTC", 405, 459), ("36.96", 542, 569)),
            row(212, ("CB", 405, 430), ("36.96", 542, 569)),
        ]
        parsed = READER.parse_pages([positioned(rows)])
        self.assertEqual([line.raw_name for line in parsed.lines], ["Sac de glace 10 kg", "Consigne bac", "Livraison"])
        check = table_check(parsed)
        self.assertFalse(check.passed)
        self.assertIn("1 ligne du corps du tableau lue comme texte : « Consigne bac »", check.detail)

    def test_a_table_row_the_arithmetic_left_out_fails_the_check(self):
        rows = [
            row(100, ("Référence", 12, 50), ("Désignation", 77, 122), ("Qté", 323, 340), ("PU", 357, 384), ("Total", 542, 585)),
            row(113, ("AR01", 12, 40), ("Sac de glace 10 kg", 77, 170), ("2", 323, 329), ("12.40", 357, 384), ("24.80", 542, 569)),
            row(126, ("AR02", 12, 40), ("Livraison", 77, 130), ("1", 323, 329), ("6.00", 357, 384), ("6.00", 542, 563)),
            row(160, ("Total TTC", 405, 459), ("24.80", 542, 569)),
            row(173, ("CB", 405, 430), ("24.80", 542, 569)),
        ]
        parsed = READER.parse_pages([positioned(rows)])
        self.assertEqual([line.raw_name for line in parsed.lines], ["Sac de glace 10 kg"])
        check = table_check(parsed)
        self.assertFalse(check.passed)
        self.assertIn("1 ligne du tableau écartée par l'arithmétique", check.detail)

    def test_the_check_names_the_lines_read_outside_the_table(self):
        parsed = READER.parse_pages([positioned(WEB_SHOP)])
        detail = table_check(parsed).detail
        self.assertTrue(table_check(parsed).passed)
        self.assertIn("1 ligne lue en texte, hors du tableau : « Delivery »", detail)
        self.assertIn("la quantité, le prix unitaire, le montant et le taux viennent de leurs colonnes", detail)

    def test_a_header_line_printed_again_in_the_body_is_the_tables(self):
        """"Prix Unitaire Brut HT" with a figure the recogniser ran into it,
        between two rows: never an article carrying a total."""
        rows = [
            row(100, ("Référence", 12, 50), ("Désignation", 77, 122), ("Qté", 323, 340), ("PU HT", 357, 384), ("TVA", 455, 473), ("Montant HT", 542, 585)),
            row(113, ("AR01", 12, 40), ("Sac de glace 10 kg", 77, 170), ("2", 323, 329), ("12.40", 357, 384), ("5.5%", 455, 480), ("24.80", 542, 569)),
            row(126, ("Prix Unitaire Brut HT", 77, 200), ("30,80", 542, 569)),
            row(139, ("AR02", 12, 40), ("Livraison", 77, 130), ("1", 323, 329), ("6.00", 357, 384), ("20%", 455, 477), ("6.00", 542, 563)),
            row(160, ("Total HT", 405, 449), ("30.80", 542, 569)),
            row(173, ("TVA 5.5%", 300, 349), ("24.80", 405, 449), ("1.36", 542, 569)),
            row(186, ("TVA 20%", 300, 349), ("6.00", 405, 449), ("1.20", 542, 569)),
            row(199, ("Total TTC", 405, 459), ("33.36", 542, 569)),
            row(212, ("CB", 405, 430), ("33.36", 542, 569)),
        ]
        parsed = READER.parse_pages([positioned(rows)])
        self.assertEqual([line.raw_name for line in parsed.lines], ["Sac de glace 10 kg", "Livraison"])
        self.assertTrue(table_check(parsed).passed)
        self.assertEqual(failed(parsed), {})


class TwoPageTests(SimpleTestCase):
    def test_the_second_pages_items_are_numbered_over_the_whole_document(self):
        from invoices.parsers.generic_receipt import _lines_and_layout

        pages = [positioned(ICE_INVOICE), positioned(ICE_INVOICE)]
        lines, view = _lines_and_layout(pages)
        self.assertEqual(len(lines), 2 * len(ICE_INVOICE))
        self.assertEqual(len(view.tables), 2)
        self.assertEqual(sorted(view.items), [14, 15, 37, 38])
        self.assertEqual(sorted(view.headed), [14, 15, 37, 38])
        self.assertEqual([view.items[index].name for index in sorted(view.items)], ["Sac de glace pilée 10 kg", "Frais de port"] * 2)
        self.assertTrue({11, 12, 13, 34, 35, 36} <= view.skip)


class RowsBreakdownTests(SimpleTestCase):
    def test_working_out_the_breakdown_marks_nothing_on_the_view(self):
        from invoices.parsers.generic_receipt import _lines_and_layout, _rows_breakdown

        _lines, view = _lines_and_layout([positioned(ICE_INVOICE)])
        breakdown = _rows_breakdown(view, D("33.36"))
        self.assertEqual(len(breakdown), 2)
        self.assertFalse(view.taxed_rows)
