"""The pure column logic (`parsers/layout.py`): what a cell holds, which
words head a column, where a table is and where it is not. Rows are built by
hand with invented text and positions; the shapes are those of real invoices
(a header over three lines, glued header words, a line-number column, a
wrapped description) without one figure of theirs.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.ocr import OcrCell, OcrLine, OcrPage
from invoices.parsers import layout
from invoices.parsers.layout import (
    AMOUNT,
    DESCRIPTION,
    LINE_NUMBER,
    QUANTITY,
    RATE,
    REFERENCE,
    UNIT_PRICE,
    VAT_AMOUNT,
    Cell,
    Row,
    cell_value,
    find_table,
    rows_from_ocr,
    text_of,
)

D = Decimal


def row(y, *cells):
    return Row(cells=[Cell(text, x0, x1) for text, x0, x1 in cells], y=y)


class CellValueTests(SimpleTestCase):
    def test_money_with_its_currency_glued_or_beside(self):
        for text, value in (("€41.90", "41.90"), ("13,00 €", "13.00"), ("24.80", "24.80"), ("-3,00", "-3.00"), ("0,290 €", "0.290")):
            with self.subTest(text=text):
                found = cell_value(text)
                self.assertEqual((found.kind, found.value), ("money", D(value)))

    def test_thousands_grouped_three_ways(self):
        for text, value in (("1 234,00", "1234.00"), ("2.345,60", "2345.60"), ("2,345.60", "2345.60")):
            with self.subTest(text=text):
                self.assertEqual(cell_value(text).value, D(value))

    def test_a_code_beside_an_amount_is_kept_apart(self):
        found = cell_value("T1 0.49")
        self.assertEqual((found.kind, found.value, found.code), ("money", D("0.49"), "T1"))
        found = cell_value("0,80 A")
        self.assertEqual((found.kind, found.value, found.code), ("money", D("0.80"), "A"))

    def test_a_vat_code_digit_run_into_the_amounts_cell_is_a_code(self):
        """"36,00€ 1": the total, then the VAT code's digit from the column
        beside it. Read as text, the row lost its amount and the unit price
        stood in for it. A digit IN FRONT of an amount is a count."""
        found = cell_value("36,00€ 1")
        self.assertEqual((found.kind, found.value, found.code), ("money", D("36.00"), "1"))
        self.assertEqual(cell_value("1 36,00").kind, "text")

    def test_an_amount_is_in_cents_a_unit_price_keeps_its_decimals(self):
        """A third decimal on an amount is a currency sign the recogniser
        read as a digit ("11,901"), as the text reading already holds; four
        decimals are a real unit price ("3.0237"), and so is "9,397" a copy."""
        as_cell = lambda text: (Cell(text, 0, 10), cell_value(text))  # noqa: E731
        self.assertEqual(layout._amount_of(as_cell("11,901")), D("11.90"))
        self.assertEqual(layout._amount_of(as_cell("12,40")), D("12.40"))
        self.assertEqual(layout._amount_of(as_cell("3.0237")), D("3.0237"))
        self.assertIsNone(layout._amount_of(as_cell("Câble")))
        self.assertIsNone(layout._amount_of(None))

    def test_a_description_cell_is_cut_as_the_text_reading_cuts_a_name(self):
        """An amount the recogniser ran into the name's cell is not the name;
        a code the cell starts with is not either, when the cell also lies
        under the reference column (`coded`). A size in one word and a
        percentage stay; bare numbers ending the name are cut, as the text
        reading cuts them ("CROCHET ACIER 12 25" is the hook)."""
        self.assertEqual(layout.name_cell_text("1L SIROP GRENADINE 3,45€"), "1L SIROP GRENADINE")
        self.assertEqual(layout.name_cell_text("SAC PAPIER 0,12"), "SAC PAPIER")
        self.assertEqual(layout.name_cell_text("20000001 CROCHET ACIER 12 25", coded=True), "CROCHET ACIER")
        self.assertEqual(layout.name_cell_text("20000001 CROCHET ACIER 12 25"), "20000001 CROCHET ACIER")
        self.assertEqual(layout.name_cell_text("PLANCHE 18,50X20"), "PLANCHE 18,50X20")
        self.assertEqual(layout.name_cell_text("BOISSON AVOINE 20% MG"), "BOISSON AVOINE 20% MG")
        self.assertEqual(layout.name_cell_text("20000001", coded=True), "20000001")

    def test_a_small_whole_number_ending_a_name_is_cut_as_the_text_reading_cuts_it(self):
        """"CARNET SPIRALE 2" is the notebook: the text reading drops a
        trailing count or code, and the checked tickets hold that name. A
        size, a unit and a fraction stay."""
        self.assertEqual(layout.name_cell_text("CARNET SPIRALE 2"), "CARNET SPIRALE")
        self.assertEqual(layout.name_cell_text("BASILIC FRAIS 1"), "BASILIC FRAIS")
        self.assertEqual(layout.name_cell_text("Sac de glace pilée 10 kg"), "Sac de glace pilée 10 kg")
        self.assertEqual(layout.name_cell_text("ROBINET LAITON 1/2"), "ROBINET LAITON 1/2")
        self.assertEqual(layout.name_cell_text("CHAINE 200KG D4 X4"), "CHAINE 200KG D4 X4")
        self.assertEqual(layout.name_cell_text("12"), "12")

    def test_an_ean_in_a_name_goes_on_the_line(self):
        self.assertEqual(layout._without_gtin("VIS INOX 40MM BTE50 3000000001011"), ("VIS INOX 40MM BTE50", "3000000001011"))
        self.assertEqual(layout._without_gtin("EAN : 3000000001011"), ("EAN", "3000000001011"))
        self.assertEqual(layout._without_gtin("VIS INOX 40MM 704411"), ("VIS INOX 40MM 704411", ""))

    def test_rates_integers_digit_runs_codes_and_text(self):
        self.assertEqual((cell_value("5.5%").kind, cell_value("5.5%").value), ("rate", D("0.055")))
        self.assertEqual((cell_value("0 %").kind, cell_value("0 %").value), ("rate", D("0")))
        self.assertEqual((cell_value("1").kind, cell_value("1").value), ("integer", 1))
        self.assertEqual((cell_value("704411").kind, cell_value("704411").value), ("digits", "704411"))
        self.assertEqual((cell_value("AR0098").kind, cell_value("AR0098").value), ("code", "AR0098"))
        self.assertEqual(cell_value("Câble réseau 2 m").kind, "text")
        self.assertEqual(cell_value("Prix Unitaire Brut HT: 123.45€").kind, "text")
        self.assertEqual(cell_value("").kind, "text")
        self.assertEqual(cell_value("   ").kind, "text")


class HeaderWordsTests(SimpleTestCase):
    def test_glued_header_words_are_split_where_the_case_changes(self):
        self.assertEqual(layout._header_words("QuantitéPU HT"), ["quantite", "pu", "ht"])
        self.assertEqual(layout._header_words("Net HTPrix Unitaire"), ["net", "ht", "prix", "unitaire"])
        self.assertEqual(layout._header_words("N°"), ["no"])

    def test_roles_named_in_french_and_english(self):
        self.assertEqual(layout._roles_of(["taux", "de", "tva"]), [(RATE, "")])
        self.assertEqual(layout._roles_of(["montant", "tva"]), [(VAT_AMOUNT, "")])
        self.assertEqual(layout._roles_of(["montant", "ht"]), [(AMOUNT, "HT")])
        self.assertEqual(layout._roles_of(["quantite", "pu", "ht"]), [(QUANTITY, ""), (UNIT_PRICE, "HT")])
        self.assertEqual(layout._roles_of(["unit", "price"]), [(UNIT_PRICE, "")])
        self.assertEqual(layout._roles_of(["vat", "rate"]), [(RATE, "")])
        self.assertEqual(layout._roles_of(["description"]), [(DESCRIPTION, "")])
        self.assertEqual(layout._roles_of(["libelle"]), [(DESCRIPTION, "")])

    def test_two_unit_prices_glued_in_one_header_cell_are_two_columns(self):
        roles = layout._roles_of(["prix", "unitaire", "net", "ht", "prix", "unitaire", "net", "ttc"])
        self.assertEqual(roles, [(UNIT_PRICE, "HT"), (UNIT_PRICE, "TTC")])

    def test_a_word_that_names_no_column_is_no_header(self):
        self.assertEqual(layout._roles_of(["capital", "social"]), [])
        self.assertFalse(layout._is_header_row(row(10, ("Capital : 8 000,00 Euros", 0, 100))))
        self.assertFalse(layout._is_header_row(row(10, ("Désignation", 0, 60), ("24,80", 300, 340))))

    def test_totals_are_told_by_their_words(self):
        for text in ("Total HT", "Sous-total", "Net à payer", "TOTAL GENERAL TTC", "Total T.T.C (euros) :"):
            with self.subTest(text=text):
                self.assertTrue(layout._is_total_label(text))
        for text in ("Frais de port", "Transport", "TVA 5.5 %", "Sac de glace", "Livraison"):
            with self.subTest(text=text):
                self.assertFalse(layout._is_total_label(text))


# A header printed over three lines - "Taux de / Montant / Montant" above
# "Référence / Désignation / QuantitéPU HT" above "TVA / TVA / HT" - and two
# rows in HT under it, each printing its rate and its tax. The document's
# head (its number, its address, its capital) sits above the header.
THREE_LINE_HEADER = [
    row(13.4, ("FACTURE", 12, 76)),
    row(56.7, ("Numéro de facture : 20260115.10042", 12, 147)),
    row(68.0, ("Date de facturation : 15/01/2026", 12, 129)),
    row(90.5, ("SAS GLACE EXEMPLE", 12, 109)),
    row(101.7, ("4 rue des Frimas", 12, 81)),
    row(135.5, ("Capital : 8 000,00 Euros", 12, 100)),
    row(191.0, ("Client Exemple", 543, 583)),
    row(254.8, ("Taux de", 455, 485), ("Montant", 497, 530), ("Montant", 542, 575)),
    row(260.8, ("Référence", 12, 50), ("Désignation", 77, 122), ("QuantitéPU HT", 323, 384)),
    row(266.1, ("TVA", 455, 473), ("TVA", 497, 515), ("HT", 542, 555)),
    row(279.5, ("AR0142", 12, 53), ("Sac de glace pilée 10 kg", 77, 178), ("2", 323, 329), ("12.40", 357, 384), ("5.5%", 455, 480), ("1.36", 497, 518), ("24.80", 542, 569)),
    row(293.0, ("Livraison", 12, 58), ("Frais de port", 77, 160), ("1", 323, 329), ("6.00", 357, 378), ("5.5%", 455, 480), ("0.33", 497, 518), ("6.00", 542, 563)),
    row(326.0, ("Total TVA 20%", 6, 82), ("0.00", 104, 125), ("Total HT", 405, 449), ("30.80", 500, 527)),
    row(339.5, ("Net HT", 405, 442), ("30.80", 500, 527)),
    row(353.0, ("Total TVA 5.5%", 6, 85), ("1.69", 104, 125)),
    row(366.5, ("Total TVA", 405, 458), ("1.69", 500, 521)),
    row(380.0, ("Total TTC", 405, 459), ("32.49", 500, 527)),
    row(393.3, ("Montants exprimés en Euros", 6, 92)),
]

# A line number, an article code, the description, a quantity, two unit
# prices around an empty discount column, the total and a status column.
LINE_NUMBER_TABLE = [
    row(25.7, ("Facture", 393, 444)),
    row(56.5, ("FP2020000041188", 292, 358), ("12/02/2026", 370, 408)),
    row(77.5, ("INFORMATIQUE EXEMPLE SAS", 24, 138)),
    row(217.4, ("N°", 26, 34), ("Article", 152, 174), ("Qté", 338, 350), ("PU TTC", 359, 385), ("Remise", 393, 419), ("PU TTC", 429, 454), ("Total TTC", 468, 501), ("Statut", 517, 537)),
    row(230.8, ("1", 27, 31), ("704411", 37, 63), ("Câble HDMI 2 m tressé", 71, 261), ("1", 342, 346), ("7,99", 365, 380), ("7,99", 434, 449), ("7,99", 477, 492)),
    row(241.6, ("2", 27, 31), ("118830", 37, 63), ("Adaptateur USB-C vers Jack", 71, 228), ("3", 342, 346), ("8,30", 363, 383), ("8,30", 432, 452), ("24,90", 475, 494)),
    row(262.5, ("Livraison express par coursier en 3 heures", 106, 291)),
    row(580.9, ("Total Brut HT:", 384, 437), ("27,41€", 498, 539)),
    row(659.9, ("Total T.T.C (euros) :", 384, 461), ("32,89€", 498, 539)),
]

# An English web shop: a description wrapped onto the next line, the amount
# on the first, the euro sign glued to every figure, VAT at 0 %.
WRAPPED_DESCRIPTIONS = [
    row(29.2, ("INVOICE", 24, 123)),
    row(33.0, ("Invoice number", 215, 282), ("Invoice total", 519, 572)),
    row(46.5, ("FAC-000777", 215, 266), ("€46.70", 543, 572)),
    row(105.0, ("March 3, 2026", 215, 281)),
    row(189.7, ("Client Exemple", 24, 70), ("NUISIBLES EXEMPLE", 503, 572)),
    row(331.5, ("Description", 24, 74), ("Quantity", 276, 314), ("Unit price", 354, 396), ("VAT rate", 441, 478), ("Amount", 538, 572)),
    row(355.5, ("Piège à phéromones PRO-TRAP grand", 24, 200), ("1", 310, 314), ("€24.50", 365, 396), ("0%", 463, 478), ("€24.50", 542, 572)),
    row(366.7, ("modèle  boîte de 5 recharges", 24, 152)),
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
# "1"s in front of the names, unit prices and amounts. Positions in pixels.
HEADERLESS_QUANTITIES = [
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


class HeaderTableTests(SimpleTestCase):
    def test_a_header_over_three_lines_names_seven_columns(self):
        table = find_table(THREE_LINE_HEADER)
        self.assertIsNotNone(table)
        self.assertEqual(table.header_rows, [7, 8, 9])
        roles = [column.label for column in sorted(table.columns, key=lambda column: column.x0)]
        self.assertEqual(
            roles,
            [REFERENCE, DESCRIPTION, QUANTITY, f"{UNIT_PRICE} HT", RATE, VAT_AMOUNT, f"{AMOUNT} HT"],
        )
        # The glued "QuantitéPU HT" named two columns, left to right, and
        # the arithmetic confirmed them: 2 x 12,40 = 24,80.
        quantity = next(column for column in table.columns if column.role == QUANTITY)
        self.assertTrue(quantity.confirmed)

    def test_the_rows_under_the_header_are_the_items_and_prove_their_own_tax(self):
        table = find_table(THREE_LINE_HEADER)
        self.assertEqual(sorted(table.items), [10, 11])
        first, second = table.items[10], table.items[11]
        self.assertEqual((first.name, first.quantity, first.unit_price, first.amount), ("Sac de glace pilée 10 kg", 2, D("12.40"), D("24.80")))
        self.assertEqual((first.rate, first.ht, first.ttc), (D("0.055"), D("24.80"), D("26.16")))
        self.assertEqual((second.name, second.quantity, second.ht, second.ttc), ("Frais de port", 1, D("6.00"), D("6.33")))

    def test_the_documents_head_and_the_header_are_never_items(self):
        table = find_table(THREE_LINE_HEADER)
        self.assertEqual(table.before, set(range(7)))
        self.assertTrue({7, 8, 9} <= table.inside)
        self.assertNotIn(5, table.items)  # "Capital : 8 000,00 Euros"
        self.assertNotIn(12, table.items)  # "Total HT" ends the table

    def test_a_line_number_and_a_code_column_stay_out_of_the_name(self):
        table = find_table(LINE_NUMBER_TABLE)
        self.assertEqual([item.name for item in table.items.values()], ["Câble HDMI 2 m tressé", "Adaptateur USB-C vers Jack"])
        self.assertEqual([item.quantity for item in table.items.values()], [1, 3])
        self.assertEqual([item.unit_price for item in table.items.values()], [D("7.99"), D("8.30")])
        # The line numbers 1, 2 multiply into the first row's amount too;
        # the column that holds on both rows is the quantity.
        by_x = sorted(table.columns, key=lambda column: column.x0)
        self.assertEqual(by_x[0].role, REFERENCE)
        self.assertEqual(by_x[1].role, REFERENCE)
        self.assertIn(layout.DISCOUNT, [column.role for column in table.columns])

    def test_a_description_wrapped_onto_the_next_line_belongs_to_its_row(self):
        table = find_table(WRAPPED_DESCRIPTIONS)
        names = [item.name for item in table.items.values()]
        self.assertEqual(names, ["Piège à phéromones PRO-TRAP grand modèle boîte de 5 recharges", "Gel appât fourmis ANT-STOP tube de 25 g"])
        self.assertEqual([item.continuation for item in table.items.values()], [[7], [9]])
        self.assertTrue({7, 9} <= table.inside)
        # At 0 % the amount is HT and TTC at once.
        self.assertEqual([(item.rate, item.ht, item.ttc) for item in table.items.values()], [(D("0"), D("24.50"), D("24.50")), (D("0"), D("15.30"), D("15.30"))])
        # The totals under the table are not rows: "Subtotal" ends it.
        self.assertNotIn(10, table.items)
        self.assertNotIn(12, table.items)

    def test_a_line_printed_far_below_a_row_is_not_its_wrapped_description(self):
        rows = list(WRAPPED_DESCRIPTIONS)
        rows[9] = row(440.0, ("de 25 g", 24, 174))  # 50 points down, three lines' worth
        table = find_table(rows)
        self.assertEqual(table.items[8].name, "Gel appât fourmis ANT-STOP tube")
        self.assertEqual(table.items[8].continuation, [])

    def test_describe_says_what_was_recognised_in_french(self):
        text = find_table(THREE_LINE_HEADER).describe()
        self.assertIn("colonnes : référence, désignation, quantité, prix unitaire HT, taux de TVA, montant TVA, montant HT", text)
        self.assertIn("en-tête sur 3 lignes", text)
        self.assertIn("2 lignes d'article", text)
        self.assertIn("7 lignes avant le tableau écartées", text)
        self.assertIn("désignations sur deux lignes", find_table(WRAPPED_DESCRIPTIONS).describe())


# A till's invoice layout whose header calls the row's HT total "Prix H.T"
# and the tax "Montant" (beside "% TVA"): the words say one thing, the
# arithmetic another.
TAXED_ROWS = [
    row(1079.4, ("Description", 178, 332), ("Prix unitaire", 392, 573), ("Qté", 588, 636), ("Prix", 721, 780), ("% TVA", 812, 884), ("T.V.A.", 914, 998), ("Remise", 1057, 1145), ("Prix", 1305, 1367)),
    row(1103.6, ("H.T", 522, 570), ("H.T", 732, 780), ("Montant", 901, 999), ("TTC", 1098, 1146), ("TTC", 1320, 1367)),
    row(1150.4, ("RUBAN ADHESIF 25M", 177, 411), ("2,50 €", 484, 570), ("2", 617, 634), ("5,00 €", 694, 783), ("20,00", 811, 883), ("1,00 €", 917, 1002), ("6,00 €", 1284, 1368)),
    row(1175.1, ("AGRAFEUSE 21CM", 175, 423), ("4,10 €", 484, 570), ("1", 615, 633), ("4,10 €", 694, 783), ("20,00", 811, 884), ("0,82 €", 917, 1002), ("4,92 €", 1285, 1369)),
    row(1418.1, ("Total", 169, 245), ("3", 615, 633), ("9,10 €", 680, 779), ("1,82 €", 918, 1005), ("10,92 €", 1274, 1376)),
]


class ArithmeticOverWordsTests(SimpleTestCase):
    def test_the_amount_column_is_the_one_the_quantity_times_the_unit_price_makes(self):
        """The header calls the row's total "Prix H.T": the vocabulary reads a
        unit price, the arithmetic (2 x 2,50 = 5,00) an amount - and the
        arithmetic decides. Read by the words, every row's TAX was its amount."""
        table = find_table(TAXED_ROWS)
        self.assertIsNotNone(table)
        by_x = sorted((column for column in table.columns if column.role != DESCRIPTION), key=lambda column: column.x0)
        self.assertEqual(
            [(column.label, column.confirmed) for column in by_x],
            [
                (f"{UNIT_PRICE} HT", True), (QUANTITY, True), (f"{AMOUNT} HT", True), (RATE, False),
                (VAT_AMOUNT, True), (layout.DISCOUNT, False), (f"{AMOUNT} TTC", True),
            ],
        )
        self.assertEqual(
            [(item.quantity, item.amount, item.rate, item.ht, item.ttc, item.ttc_printed) for item in table.items.values()],
            [(2, D("5.00"), D("0.20"), D("5.00"), D("6.00"), True), (1, D("4.10"), D("0.20"), D("4.10"), D("4.92"), True)],
        )
        self.assertNotIn(4, table.items)  # "Total" ends the table

    def test_a_tax_column_is_told_by_the_amount_times_the_rate_whatever_its_header(self):
        rows = [r for r in TAXED_ROWS]
        # The same table with its second header line missing: "T.V.A." alone
        # heads the tax, and "Prix" alone the TTC.
        rows[1] = row(1103.6, ("H.T", 522, 570), ("H.T", 732, 780))
        table = find_table(rows)
        labels = [column.label for column in sorted(table.columns, key=lambda column: column.x0)]
        self.assertIn(VAT_AMOUNT, labels)
        self.assertEqual(labels[-1], f"{AMOUNT} TTC")

    def test_no_table_when_no_arithmetic_and_no_header_names_the_amount(self):
        """A header whose total column read as "T" and rows whose figures do
        not multiply out: nothing names an amount, and the text reading
        keeps the page."""
        rows = [
            row(723.7, ("CODE", 182, 247), ("DESIGNATION", 552, 693), ("PRIX UNIT. TTC", 937, 1083), ("QUANTITE", 1114, 1219), ("T", 1557, 1576)),
            row(775.9, ("20000002", 109, 217), ("DECOUPE > 23MM", 328, 645), ("4,50€", 1030, 1091), ("8", 1219, 1239)),
        ]
        table = find_table(rows)
        # Only one money column is left to be the amount: the unit price.
        # The row then says 8 x 4,50 = 4,50, which the reader's own checks
        # refuse; a header that names the total's column puts it right.
        self.assertIsNotNone(table)
        rows[0] = row(723.7, ("CODE", 182, 247), ("DESIGNATION", 552, 693), ("PRIX UNIT. TTC", 937, 1083), ("QUANTITE", 1114, 1219), ("REMISE", 1279, 1362), ("TOTAL TTC", 1424, 1529), ("T", 1557, 1576))
        rows[1] = row(775.9, ("20000002", 109, 217), ("DECOUPE > 23MM", 328, 645), ("4,50€", 1030, 1091), ("8", 1219, 1239), ("36,00€ 1", 1478, 1579))
        table = find_table(rows)
        (item,) = table.items.values()
        self.assertEqual((item.name, item.quantity, item.unit_price, item.amount), ("DECOUPE > 23MM", 8, D("4.50"), D("36.00")))


def inexact(rows):
    """The same rows as a recogniser groups them on a photograph."""
    return [Row(cells=r.cells, y=r.y, exact=False) for r in rows]


class PhotographedRowsTests(SimpleTestCase):
    def test_rows_from_a_text_layer_are_exact_and_a_recognisers_are_not(self):
        layer = OcrPage(lines=[OcrLine(cells=[OcrCell("BAGUETTE", 10, 90, 1.0), OcrCell("0.49", 300, 340, 1.0)], y=12.0)])
        photo = OcrPage(lines=[OcrLine(cells=[OcrCell("BAGUETTE", 10, 90, 0.98), OcrCell("0.49", 300, 340, 1.0)], y=12.0)])
        self.assertTrue(rows_from_ocr(layer)[0].exact)
        self.assertFalse(rows_from_ocr(photo)[0].exact)
        self.assertTrue(Row(cells=[]).exact)

    def test_a_description_is_never_joined_across_lines_on_a_photograph(self):
        """On a photograph the lines are the recogniser's grouping, and the
        text under a row may be the next row's head whose figures were
        grouped with this one. The tickets already checked carry the first
        line as the name; the rows are read the same way."""
        table = find_table(inexact(WRAPPED_DESCRIPTIONS))
        self.assertEqual([item.name for item in table.items.values()], ["Piège à phéromones PRO-TRAP grand", "Gel appât fourmis ANT-STOP tube"])
        self.assertEqual([item.continuation for item in table.items.values()], [[], []])
        self.assertFalse({7, 9} & table.inside)

    def test_a_detail_line_under_a_row_is_left_to_the_text_reading(self):
        """An EAN line, a department line, a discount under its article: none
        of them is a wrapped description and none is hidden from the text
        reading, which knows what each of them is."""
        rows = [
            row(669.7, ("CODE", 694, 777), ("NOM DU PRODUIT", 942, 1201), ("MONTANT TVA", 1494, 1710)),
            row(767.6, ("** PEINTURE / MENAGE **", 960, 1453)),
            row(814.5, ("20000003", 694, 845), ("SAC CABAS REUTILISABLE", 941, 1469), ("0,30", 1548, 1635), ("1", 1657, 1701)),
            row(861.3, ("EAN : 3000000001011", 939, 1184)),
            row(909.1, ("** DROGUERIE **", 956, 1451)),
            row(956.8, ("20000004", 689, 842), ("NETTOYANT MULTI-USAGE", 937, 1309), ("8,90", 1548, 1635), ("1", 1657, 1701)),
            row(1004.6, ("EAN : 2000000002026", 937, 1288)),
            row(1509.6, ("TOTAL", 689, 800), ("9,20 €", 1539, 1689)),
        ]
        table = find_table(inexact(rows))
        self.assertEqual([item.name for item in table.items.values()], ["SAC CABAS REUTILISABLE", "NETTOYANT MULTI-USAGE"])
        self.assertEqual(table.inside, {0})
        self.assertEqual(table.before, set())
        # On an exact layer the EAN line is a detail too, never a name.
        table = find_table(rows)
        self.assertEqual([item.name for item in table.items.values()], ["SAC CABAS REUTILISABLE", "NETTOYANT MULTI-USAGE"])
        self.assertNotIn(3, table.inside)

    def test_a_discount_row_between_the_items_is_not_hidden(self):
        rows = list(THREE_LINE_HEADER)
        rows.insert(11, row(286.0, ("Remise", 12, 53), ("Remise fidélité", 77, 178), ("1", 323, 329), ("-2.00", 357, 384), ("5.5%", 455, 480), ("-0.11", 497, 518), ("-2.00", 542, 569)))
        table = find_table(rows)
        self.assertEqual(sorted(table.items), [10, 12])
        self.assertNotIn(11, table.inside)

    def test_an_ean_printed_in_the_description_is_the_lines_not_the_names(self):
        rows = list(LINE_NUMBER_TABLE)
        rows[6] = row(230.8, ("1", 27, 31), ("704411", 37, 63), ("Câble HDMI 2 m tressé 3000000001011", 71, 261), ("1", 342, 346), ("7,99", 365, 380), ("7,99", 434, 449), ("7,99", 477, 492))
        table = find_table(rows)
        first = table.items[6]
        self.assertEqual((first.name, first.ean), ("Câble HDMI 2 m tressé", "3000000001011"))


class HeaderlessTableTests(SimpleTestCase):
    def test_columns_are_found_from_alignment_and_arithmetic_alone(self):
        table = find_table(HEADERLESS_QUANTITIES)
        self.assertIsNotNone(table)
        self.assertFalse(table.has_header)
        self.assertEqual(table.before, set())
        self.assertEqual([item.name for item in table.items.values()], ["TORCHON COTON 200G", "SERVIETTE NOIRE", "TABLIER BAR"])
        self.assertEqual([item.quantity for item in table.items.values()], [1, 6, 1])
        self.assertEqual([item.unit_price for item in table.items.values()], [D("14.90"), D("2.10"), D("9.80")])
        self.assertEqual([item.amount for item in table.items.values()], [D("14.90"), D("12.60"), D("9.80")])
        self.assertIn("sans en-tête", table.describe())

    def test_one_aligned_row_is_no_table(self):
        self.assertIsNone(find_table(HEADERLESS_QUANTITIES[:4]))

    def test_a_code_between_the_prices_is_not_the_name(self):
        """A VAT code's letter printed between the unit prices ("C") is not
        part of the name: the name is what stands left of the row's prices."""
        rows = [
            row(670, ("GAZ CYLINDRE ECHANGE", 65, 741), ("1", 889, 915), ("27,49 €", 1159, 1270), ("C", 1293, 1324), ("29,00 €", 1388, 1498), ("29,00 €", 1559, 1669)),
            row(821, ("GAZ CYLINDRE ECHANGE", 76, 741), ("1", 890, 912), ("27,49 €", 1155, 1266), ("C", 1290, 1323), ("29,00 €", 1386, 1496), ("29,00 €", 1559, 1666)),
            row(970, ("BOUCHON SILICONE X3", 87, 743), ("2", 890, 911), ("2,84 €", 1151, 1262), ("C", 1284, 1317), ("3,00 €", 1381, 1487), ("6,00 €", 1550, 1656)),
        ]
        table = find_table(rows)
        self.assertEqual([item.name for item in table.items.values()], ["GAZ CYLINDRE ECHANGE", "GAZ CYLINDRE ECHANGE", "BOUCHON SILICONE X3"])
        self.assertEqual([item.quantity for item in table.items.values()], [1, 1, 2])

    def test_rows_whose_figures_do_not_multiply_are_no_table(self):
        rows = [
            row(100, ("BAGUETTE", 100, 400), ("T1", 700, 740), ("0.49", 800, 880), ("0.49", 900, 980)),
            row(150, ("CITRON", 100, 400), ("T1", 700, 740), ("2.29", 800, 880), ("4.58", 900, 980)),
        ]
        # Two amounts and a code: nothing here is a count times a price.
        self.assertIsNone(find_table(rows))


class NoTableTests(SimpleTestCase):
    def test_nothing_is_no_table(self):
        self.assertIsNone(find_table([]))
        self.assertIsNone(find_table([Row(cells=[]), Row(cells=[], y=10.0)]))

    def test_a_till_ticket_is_no_table(self):
        rows = [
            row(100, ("FRANPRIX EXEMPLE", 200, 700)),
            row(160, ("BAGUETTE TRADITION", 100, 500), ("T1 1.20", 800, 950)),
            row(210, ("PAIN COMPLET", 100, 400), ("T1 2 X 0.90Eur 1.80Eur", 500, 950)),
            row(270, ("SOUS-TOTAL", 100, 400), ("3.00", 850, 950)),
            row(320, ("TOTAL A PAYER", 100, 450), ("3.00", 850, 950)),
        ]
        self.assertIsNone(find_table(rows))

    def test_a_header_with_nothing_under_it_is_no_table(self):
        self.assertIsNone(find_table(THREE_LINE_HEADER[:10]))

    def test_a_negative_or_zero_amount_is_never_an_item(self):
        rows = list(THREE_LINE_HEADER[:10]) + [
            row(279.5, ("AR0142", 12, 53), ("Remise fidélité", 77, 178), ("1", 323, 329), ("-2.00", 357, 384), ("5.5%", 455, 480), ("0.00", 497, 518), ("-2.00", 542, 569)),
            row(293.0, ("AR0143", 12, 53), ("Offert", 77, 160), ("1", 323, 329), ("0.00", 357, 378), ("5.5%", 455, 480), ("0.00", 497, 518), ("0.00", 542, 563)),
        ]
        self.assertIsNone(find_table(rows))

    def test_a_line_of_qualifiers_alone_is_a_header_line_only_under_a_header(self):
        """"HT | TTC | TTC" names no column: on its own it is no header, and
        no table starts from it; under "Prix / Prix / Montant" it is the
        header's last line and flavours the columns above it."""
        qualifiers = row(357.2, ("HT", 357, 369), ("TTC", 402, 418), ("TTC", 508, 525))
        self.assertFalse(layout._is_header_row(qualifiers))
        self.assertIsNone(layout._header_cells(qualifiers))
        self.assertEqual(len(layout._header_cells(qualifiers, bare=True)), 3)
        rows = [
            row(338.5, ("Référence", 121, 167), ("Désignation", 229, 283), ("Prix", 354, 372), ("Prix", 401, 419), ("Qté", 437, 453), ("Montant", 498, 536)),
            row(347.8, ("Unitaire", 345, 381), ("Unitaire", 392, 428), ("Total", 506, 528)),
            qualifiers,
            row(372.5, ("REF01", 119, 164), ("Sirop de pissenlit 1 L", 175, 260), ("4,00 €", 354, 384), ("4,80 €", 400, 431), ("2", 449, 454), ("9,60 €", 505, 536)),
            row(390.5, ("REF02", 119, 164), ("Sirop de coquelicot 1 L", 175, 260), ("5,00 €", 354, 384), ("6,00 €", 400, 431), ("3", 449, 454), ("18,00 €", 505, 536)),
            row(459.1, ("Total HT", 426, 458), ("23,00 €", 506, 537)),
        ]
        table = find_table(rows)
        self.assertEqual(table.header_rows, [0, 1, 2])
        labels = [column.label for column in sorted(table.columns, key=lambda column: column.x0)]
        self.assertEqual(labels, [REFERENCE, DESCRIPTION, f"{UNIT_PRICE} HT", f"{UNIT_PRICE} TTC", QUANTITY, f"{AMOUNT} TTC"])
        self.assertEqual([(item.quantity, item.ht, item.ttc, item.rate) for item in table.items.values()], [(2, D("8.00"), D("9.60"), D("0.20")), (3, D("15.00"), D("18.00"), D("0.20"))])

    def test_a_header_row_alone_is_a_header_but_a_vat_summary_header_is_not_the_table(self):
        # "Taxe | Base | Taux | Montant" names columns, none of them a description.
        self.assertIsNone(find_table([row(10, ("Taxe", 40, 60), ("Base", 101, 121), ("Taux", 151, 171), ("Montant", 194, 227)), row(20, ("Taux normal", 31, 78), ("27,41€", 97, 123), ("20%", 152, 170), ("5,48€", 197, 223))]))


class RowsFromOcrTests(SimpleTestCase):
    def test_rows_follow_the_ocr_lines_and_text_of_prints_them_as_the_reader_sees_them(self):
        page = OcrPage(lines=[
            OcrLine(cells=[OcrCell("BAGUETTE", 10, 90, 0.9), OcrCell("0.49", 300, 340, 0.8)], y=12.0),
            OcrLine(cells=[OcrCell("TOTAL", 10, 60, 0.9), OcrCell("0.49", 300, 340, 0.95)], y=30.0),
        ])
        rows = rows_from_ocr(page)
        self.assertEqual([r.y for r in rows], [12.0, 30.0])
        self.assertEqual(rows[0].cells[1], Cell("0.49", 300.0, 340.0))
        self.assertEqual(text_of(rows), page.text)
        self.assertEqual(text_of(rows), "BAGUETTE  0.49\nTOTAL  0.49")

    def test_clean_text_drops_icon_glyphs_and_folds_spaces(self):
        self.assertEqual(layout.clean_text("GRAINES  sachet  de 50 g"), "GRAINES sachet de 50 g")
        self.assertEqual(layout.clean_text("\x00"), "")


class LineNumberRoleTests(SimpleTestCase):
    def test_consecutive_integers_down_the_first_column_are_line_numbers(self):
        cells = {i: (Cell(str(i + 1), 0, 5), cell_value(str(i + 1))) for i in range(3)}
        self.assertEqual(layout._role_from_values(cells, leftmost=True), LINE_NUMBER)
        self.assertEqual(layout._role_from_values(cells, leftmost=False), QUANTITY)
        ones = {i: (Cell("1", 0, 5), cell_value("1")) for i in range(3)}
        self.assertEqual(layout._role_from_values(ones, leftmost=True), QUANTITY)


# -- the findings of the review, each a test that failed first --------------


def sweep_groups(groups):
    """Unordered view of `_cluster`'s result, to compare two algorithms."""
    return sorted(sorted(group) for group in groups)


class CellValueEdgeTests(SimpleTestCase):
    def test_a_units_only_number_with_a_footnote_is_no_amount(self):
        """"2025 (1)" under a row is a year with a footnote, never 2 025 EUR;
        "1 2" is two digits, not one euro and a code. Only a figure printing
        its decimals (or a currency sign) is money beside a code."""
        for text in ("2025 (1)", "1 2", "12 A"):
            with self.subTest(text=text):
                self.assertNotEqual(cell_value(text).kind, "money")
        found = cell_value("36,00€ 1")
        self.assertEqual((found.kind, found.value, found.code), ("money", D("36.00"), "1"))

    def test_the_zero_none_and_negative_edges(self):
        self.assertEqual(cell_value("").kind, "text")
        self.assertIsNone(layout._as_quantity(cell_value("-2,00")))
        self.assertIsNone(layout._as_quantity(cell_value("0,00")))
        self.assertIsNone(layout._as_quantity(cell_value("Câble")))
        self.assertEqual(layout._as_quantity(cell_value("2,000")), 2)
        self.assertEqual(layout._as_quantity(cell_value("0,35")), D("0.35"))
        self.assertIsNone(layout._as_rate(cell_value("19%")))
        self.assertEqual(layout._as_rate(cell_value("0 %")), D("0"))
        self.assertIsNone(layout._as_rate(cell_value("Câble")))
        self.assertTrue(layout._overlap(10, 10, 5, 20))  # a zero-width span inside another
        self.assertFalse(layout._overlap(10, 10, 12, 20))
        self.assertFalse(layout._overlap(0, 10, 10, 20))  # touching is not overlapping


class ClusterTests(SimpleTestCase):
    def test_the_sweep_groups_exactly_as_the_pairwise_test_did(self):
        import random

        rng = random.Random(7)
        for _round in range(40):
            spans = []
            for key in range(rng.randint(0, 60)):
                x0 = rng.uniform(0, 600)
                spans.append((key, x0, x0 + rng.choice([0, 3, 6, 20, 40, 90])))
            self.assertEqual(sweep_groups(layout._cluster(spans)), sweep_groups(layout._cluster_pairwise(spans)))

    def test_a_long_table_is_read_in_a_fraction_of_a_second(self):
        """Two hundred rows of seven figures each: a quadratic grouping took
        half a second here, and four times that on four hundred rows."""
        import time

        rows = [
            row(254.8, ("Taux de", 455, 485), ("Montant", 497, 530), ("Montant", 542, 575)),
            row(260.8, ("N°", 2, 9), ("Référence", 12, 50), ("Désignation", 77, 122), ("QuantitéPU HT", 323, 384)),
            row(266.1, ("TVA", 455, 473), ("TVA", 497, 515), ("HT", 542, 555)),
        ]
        y = 279.5
        for i in range(200):
            qty = (i % 7) + 1
            unit = D("1.10") + D(i % 13)
            amount = unit * qty
            rate = D("0.055") if i % 2 else D("0.20")
            rows.append(row(
                y, (str(i + 1), 2, 8), (f"AR{i:04d}", 12, 53), (f"Article exemple numéro {i}", 77, 178),
                (str(qty), 323, 329), (f"{unit:.2f}", 357, 384), ("5.5%" if i % 2 else "20%", 455, 480),
                (f"{(amount * rate).quantize(D('0.01'))}", 497, 518), (f"{amount:.2f}", 542, 569),
            ))
            y += 13.5
        rows.append(row(y + 20, ("Total HT", 405, 449), ("99999.00", 500, 527)))
        started = time.perf_counter()
        table = find_table(rows)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(table.items), 200)
        self.assertLess(elapsed, 0.3)


# A hand-written PDF whose every line is one string of text: the figures of
# each row sit wherever the words before them end, so nothing aligns from
# one row to the next. A header names five columns all the same.
MISALIGNED_ROWS = [
    row(100, ("Produit", 40, 85), ("Description", 90, 159), ("Qté", 164, 185), ("Prix unitaire", 190, 263), ("Valeur", 269, 309)),
    row(114, ("WEBF -AB123", 40, 105), ("Verre à shot (lot de 12)", 110, 252), ("8", 258, 263), ("3,50", 269, 288), ("28,00", 294, 319)),
    row(128, ("R", 40, 47)),
    row(142, ("WEBF -CD456", 40, 106), ("Pince à cocktail inox 20cm", 112, 231), ("2", 236, 242), ("4,25", 247, 267), ("8,50", 272, 292)),
    row(156, ("R", 40, 47)),
    row(170, ("Frais divers", 40, 92)),
    row(184, ("Transport Charges", 40, 123), ("9,95", 129, 148)),
    row(198, ("TOTAL HT", 40, 88), ("EURO", 94, 123), ("46,45", 128, 153)),
]
# The same invoice with its columns aligned, as its supplier's real template
# would print them.
ALIGNED_ROWS = [
    row(100, ("Produit", 40, 85), ("Description", 110, 178), ("Qté", 258, 275), ("Prix unitaire", 290, 363), ("Valeur", 380, 420)),
    row(114, ("WEBF -AB123", 40, 105), ("Verre à shot (lot de 12)", 110, 252), ("8", 262, 268), ("3,50", 340, 360), ("28,00", 395, 420)),
    row(142, ("WEBF -CD456", 40, 106), ("Pince à cocktail inox 20cm", 110, 231), ("2", 262, 268), ("4,25", 340, 360), ("8,50", 400, 420)),
    row(184, ("Transport Charges", 110, 195), ("9,95", 400, 420)),
    row(198, ("TOTAL HT", 40, 88), ("EURO", 94, 123), ("46,45", 395, 420)),
]


class HeaderConfirmationTests(SimpleTestCase):
    """Under a header, vocabulary proposes and the rows have to confirm: a
    column that misses the amount of most priced rows is not the amount
    column, and a page whose figures do not align is no table."""

    def test_rows_whose_figures_do_not_align_are_no_table(self):
        self.assertIsNone(find_table(MISALIGNED_ROWS))

    def test_the_same_rows_aligned_are_a_table_with_the_code_out_of_the_name(self):
        table = find_table(ALIGNED_ROWS)
        self.assertIsNotNone(table)
        self.assertEqual(
            [(item.name, item.quantity, item.unit_price, item.amount) for item in table.items.values()],
            [("Verre à shot (lot de 12)", 8, D("3.50"), D("28.00")), ("Pince à cocktail inox 20cm", 2, D("4.25"), D("8.50")), ("Transport Charges", None, None, D("9.95"))],
        )
        self.assertEqual(table.describe().split(" ; ")[0], f"colonnes : {REFERENCE}, {DESCRIPTION}, {QUANTITY}, {UNIT_PRICE}, {AMOUNT}")

    def test_produit_beside_designation_is_the_code_column(self):
        """"Produit | Désignation" is the usual code-then-name pair: one
        description column, and the words naming a thing rather than a name
        head the reference."""
        rows = [
            row(100, ("Produit", 12, 45), ("Désignation", 77, 122), ("Qté", 323, 340), ("PU", 357, 370), ("Total", 542, 565)),
            row(113, ("AB123", 12, 40), ("Verre à pied 25 cl", 77, 170), ("6", 323, 329), ("2.50", 357, 378), ("15.00", 542, 569)),
            row(126, ("CD456", 12, 40), ("Carafe 1 L", 77, 130), ("2", 323, 329), ("4.00", 357, 378), ("8.00", 542, 563)),
            row(160, ("Total HT", 405, 449), ("23.00", 542, 569)),
        ]
        table = find_table(rows)
        self.assertEqual([column.role for column in sorted(table.columns, key=lambda column: column.x0)], [REFERENCE, DESCRIPTION, QUANTITY, UNIT_PRICE, AMOUNT])
        self.assertEqual([item.name for item in table.items.values()], ["Verre à pied 25 cl", "Carafe 1 L"])

    def test_no_table_when_nothing_names_or_makes_the_amount(self):
        """Several money columns, none the header calls an amount, no row
        that multiplies out: the rightmost one is not the amount by default."""
        rows = [
            row(100, ("Désignation", 77, 122), ("Tarif", 357, 384), ("Statut", 542, 585)),
            row(113, ("Sac de glace 10 kg", 77, 170), ("12.40", 357, 384), ("24.80", 542, 569)),
            row(126, ("Livraison", 77, 130), ("6.00", 357, 384), ("7.10", 542, 563)),
            row(160, ("Total HT", 405, 449), ("31.90", 542, 569)),
        ]
        self.assertIsNone(find_table(rows))

    def test_a_header_word_names_as_many_columns_as_it_has_roles(self):
        """"Qté" over two numeric clusters names one quantity; the other is
        an unnamed column the arithmetic may still make sense of. Naming both
        made a VAT code's column of lone "1"s the quantity."""
        rows = [
            row(100, ("Désignation", 77, 122), ("Qté", 318, 350), ("PU HT", 357, 384), ("Montant HT", 542, 585)),
            row(113, ("Sac de glace 10 kg", 77, 170), ("2", 323, 329), ("1", 340, 346), ("12.40", 357, 384), ("24.80", 542, 569)),
            row(126, ("Livraison", 77, 130), ("1", 323, 329), ("1", 340, 346), ("6.00", 357, 384), ("6.00", 542, 563)),
            row(160, ("Total HT", 405, 449), ("30.80", 542, 569)),
        ]
        table = find_table(rows)
        labels = [column.label for column in table.columns]
        self.assertEqual(labels.count(QUANTITY), 1)
        self.assertEqual([item.quantity for item in table.items.values()], [2, 1])
        self.assertNotIn(f"{QUANTITY}, {QUANTITY}", table.describe())

    def test_a_number_under_the_description_header_is_no_column(self):
        """A footnoted year ("2025 (1)") printed under a row's name is inside
        the description, and never a column that cuts the name short: the same
        article named the same way whether a year or a word is printed there."""
        def rows(under):
            return [
                Row(cells=[Cell("Désignation", 77, 254), Cell("Qté", 831, 887), Cell("Garantie", 910, 1032), Cell("PU HT", 1105, 1200), Cell("TVA", 1202, 1266), Cell("PU TTC", 1305, 1412), Cell("TOTAL", 1475, 1580)], y=665, exact=False),
                Row(cells=[Cell("8000001-TV LED EXEMPLE 146 CM", 102, 724), Cell("EMR", 760, 824), Cell("1", 848, 867), Cell("24 mois", 915, 1024), Cell("333,33 €", 1077, 1196), Cell("A", 1216, 1248), Cell("399,99 €", 1294, 1410), Cell("399,99 €", 1458, 1578)], y=733, exact=False),
                Row(cells=[Cell(*under)], y=744, exact=False),
                Row(cells=[Cell("Dont éco-part DEEE 0,50€", 132, 421)], y=778, exact=False),
                Row(cells=[Cell("TOTAL NET TTC", 80, 310), Cell("399,99 €", 1448, 1569)], y=944, exact=False),
            ]
        with_year = find_table(rows(("2025 (1)", 101, 224)))
        with_words = find_table(rows(("MODELE 2025 (1)", 101, 300)))
        names = {table.items[1].name for table in (with_year, with_words)}
        self.assertEqual(names, {"8000001-TV LED EXEMPLE 146 CM EMR"})
        self.assertEqual([column.role for column in with_year.columns].count(DESCRIPTION), 1)


class HtColumnTests(SimpleTestCase):
    def test_a_row_of_a_proven_ht_column_is_in_ht_too(self):
        """One row proves the column HT (its two unit prices one rate apart);
        the eco-participation under it multiplies out to nothing, and read as
        TTC it put 1,25 HT beside 360,00 HT in one column. The column's rate is
        the one every row of it printed or proved."""
        rows = [
            row(100, ("EAN", 48, 101), ("Référence", 114, 142), ("Quantité", 174, 178), ("Libellé", 183, 393), ("Prix Unitaire Net HT", 428, 456), ("Prix Unitaire Net TTC", 488, 516), ("Montant Net HT", 548, 576)),
            row(113, ("3000000001011", 48, 101), ("I 9000001", 114, 142), ("3", 174, 178), ("Enceinte exemple", 183, 393), ("120,00", 428, 456), ("144,00", 488, 516), ("360,00", 548, 576)),
            row(126, ("Eco-Participation DEEE", 183, 393), ("0,40", 428, 456), ("0,48", 488, 516), ("1,50", 548, 576)),
            row(160, ("Total HT", 405, 449), ("361,50", 548, 576)),
        ]
        table = find_table(rows)
        first, second = table.items[1], table.items[2]
        self.assertEqual((first.ht, first.ttc, first.rate), (D("360.00"), D("432.00"), D("0.20")))
        self.assertEqual((second.ht, second.ttc, second.rate, second.ttc_printed), (D("1.50"), D("1.80"), D("0.20"), False))

    def test_a_column_with_a_row_proven_ttc_leaves_the_others_alone(self):
        rows = [
            row(100, ("Désignation", 77, 122), ("Qté", 323, 340), ("PU", 357, 384), ("TVA", 455, 473), ("Montant", 542, 585)),
            # 20 % proven TTC: 4,00 - 0,67 = 3,33 = 4,00 / 1,2.
            row(113, ("Ruban de masquage", 77, 170), ("1", 323, 329), ("4.00", 357, 384), ("0.67", 455, 480), ("4.00", 542, 569)),
            row(126, ("Livraison", 77, 130), ("1", 323, 329), ("6.00", 357, 384), ("6.00", 542, 563)),
            row(160, ("Total", 405, 449), ("10.00", 542, 569)),
        ]
        table = find_table(rows)
        self.assertEqual(table.items[1].ttc, D("4.00"))
        self.assertIsNone(table.items[2].ht)

    def test_a_printed_ttc_a_cent_off_the_arithmetic_is_the_one_kept(self):
        """6,64 HT at 5,5 % plus its tax 0,37 is 7,01; the row prints 7,00 and
        7,00 is what was paid. The printed figure wins over the sum."""
        rows = [
            row(100, ("Description", 175, 423), ("Prix unitaire H.T", 466, 570), ("Qté", 615, 634), ("Prix H.T", 681, 783), ("TVA", 811, 884), ("Montant", 916, 1003), ("Prix TTC", 1271, 1371)),
            row(113, ("PITA X10", 175, 423), ("6,64 €", 466, 570), ("1", 615, 634), ("6,64 €", 681, 783), ("5,50 %", 811, 884), ("0,37 €", 916, 1003), ("7,00 €", 1271, 1371)),
            row(126, ("PIED DE LAMPE", 175, 423), ("5,83 €", 466, 570), ("1", 615, 634), ("5,83 €", 681, 783), ("20,00 %", 811, 884), ("1,17 €", 916, 1003), ("7,00 €", 1271, 1371)),
            row(160, ("Total TTC", 700, 783), ("14,00 €", 1271, 1371)),
        ]
        table = find_table(rows)
        self.assertEqual([(item.ht, item.ttc, item.ttc_printed) for item in table.items.values()], [(D("6.64"), D("7.00"), True), (D("5.83"), D("7.00"), True)])


class ContinuationTests(SimpleTestCase):
    ONE_ROW = [
        row(100, ("Désignation", 77, 122), ("Qté", 323, 340), ("PU HT", 357, 384), ("Montant HT", 542, 585)),
        row(113, ("Frais de dossier", 77, 200), ("1", 323, 329), ("250,00", 357, 384), ("250,00", 542, 569)),
    ]
    TOTALS = [
        row(160, ("Total HT", 405, 449), ("250,00", 542, 569)),
        row(173, ("TVA 20%", 405, 449), ("50,00", 542, 569)),
        row(186, ("Total TTC", 405, 459), ("300,00", 542, 569)),
    ]

    def test_a_sentence_under_a_one_row_table_is_not_the_name(self):
        """The shop's terms printed right under the single row: a sentence
        that reaches under the figures. Read as a wrapped description it went
        onto the product's name, and every check passed."""
        rows = self.ONE_ROW + [row(126, ("En cas de rendez-vous manqué, toute absence non prévenue entraînera des frais.", 77, 500))] + self.TOTALS
        table = find_table(rows)
        (item,) = table.items.values()
        self.assertEqual((item.name, item.continuation), ("Frais de dossier", []))

    def test_a_sentence_narrower_than_the_figures_is_not_the_name_either(self):
        rows = self.ONE_ROW + [row(126, ("Toute absence non signalée sera facturée au tarif en vigueur.", 77, 300))] + self.TOTALS
        (item,) = find_table(rows).items.values()
        self.assertEqual(item.name, "Frais de dossier")

    def test_a_short_wrapped_line_under_the_one_row_is_its_name(self):
        rows = self.ONE_ROW + [row(126, ("(catalogue printemps)", 77, 160))] + self.TOTALS
        (item,) = find_table(rows).items.values()
        self.assertEqual((item.name, item.continuation), ("Frais de dossier (catalogue printemps)", [2]))

    def test_a_footer_line_with_a_figure_under_the_last_row_is_neither_item_nor_name(self):
        rows = list(THREE_LINE_HEADER)
        rows.insert(12, row(306.0, ("Poids total : 12,40 kg", 77, 200)))
        table = find_table(rows)
        self.assertEqual(sorted(table.items), [10, 11])
        self.assertEqual(table.items[11].continuation, [])
        self.assertNotIn(12, table.inside)


class VocabularyRowTests(SimpleTestCase):
    def test_header_words_with_a_figure_run_into_them_are_a_header_line(self):
        self.assertTrue(layout._is_vocabulary_row(row(10, ("Prix Unitaire Brut HT: 123.45€", 183, 256), ("-", 259, 261), ("Remise Unitaire HT: 12.34€", 264, 331))))
        self.assertTrue(layout._is_vocabulary_row(row(10, ("Prix Unitaire Brut HT", 77, 200), ("69,44", 542, 569))))
        for cells in (
            (("TVA 20 %", 77, 120), ("9,29", 542, 569)),
            (("Total", 77, 100), ("12,40", 542, 569)),
            (("Frais de port", 77, 140), ("6,00", 542, 569)),
            (("Prix unitaire", 77, 140), ("3,49", 542, 569)),
            (("12,40", 542, 569),),
        ):
            with self.subTest(cells=cells):
                self.assertFalse(layout._is_vocabulary_row(row(10, *cells)))

    def test_a_body_row_made_of_header_words_is_the_tables_not_an_item(self):
        """"Prix Unitaire Brut HT" with a figure the recogniser ran into it:
        a header line printed again inside the body, never a row - handed to
        the text reading, it was an article carrying a total."""
        rows = [
            row(100, ("Référence", 12, 50), ("Désignation", 77, 122), ("Qté", 323, 340), ("PU HT", 357, 384), ("Montant HT", 542, 585)),
            row(113, ("AR01", 12, 40), ("Sac de glace 10 kg", 77, 170), ("2", 323, 329), ("12.40", 357, 384), ("24.80", 542, 569)),
            row(126, ("Prix Unitaire Brut HT", 77, 200), ("69,44", 542, 569)),
            row(139, ("AR02", 12, 40), ("Livraison", 77, 130), ("1", 323, 329), ("6.00", 357, 384), ("6.00", 542, 563)),
            row(160, ("Total HT", 405, 449), ("30.80", 542, 569)),
        ]
        table = find_table(rows)
        self.assertEqual(sorted(table.items), [1, 3])
        self.assertIn(2, table.inside)


class DescribeTests(SimpleTestCase):
    def test_a_footers_figures_are_not_listed_as_columns(self):
        """A headerless table (its header names no description): the footer's
        three amounts fall into columns of their own that hold no cell of any
        row, and describe() listed them as three "prix" columns of the table."""
        rows = [
            row(471, ("Votre sélection", 100, 178), ("Qté", 300, 320), ("PU brut", 400, 440), ("Prix HT net", 520, 580)),
            row(505, ("Affiche A4 couleur", 100, 200), ("5", 300, 306), ("8,113", 400, 430), ("40,57", 540, 570)),
            row(592, ("Flyer A5 recto verso", 100, 220), ("56", 300, 312), ("0,463", 400, 430), ("25,93", 540, 570)),
            row(700, ("Total HT", 60, 120), ("66,50", 131, 165), ("TVA 20.0%", 180, 226), ("13,30", 228, 257), ("Total TTC", 340, 400), ("79,80", 430, 461)),
        ]
        table = find_table(rows)
        self.assertEqual([(item.quantity, item.unit_price, item.amount) for item in table.items.values()], [(5, D("8.113"), D("40.57")), (56, D("0.463"), D("25.93"))])
        self.assertEqual(table.describe().split(" ; ")[0], f"colonnes : {QUANTITY}, {UNIT_PRICE}, {AMOUNT}")

    def test_a_role_doubled_is_counted_not_repeated(self):
        table = find_table(THREE_LINE_HEADER)
        table.columns.append(layout.Column(x0=600, x1=620, role=UNIT_PRICE, flavour="HT", confirmed=True))
        self.assertIn(f"{UNIT_PRICE} HT (2 colonnes)", table.describe())
        self.assertNotIn(f"{UNIT_PRICE} HT, {UNIT_PRICE} HT", table.describe())


# Eight columns on one row: a line number, a code, the description, the
# quantity, the unit price HT, the rate, the tax and the total HT.
EIGHT_COLUMNS = [
    row(100, ("N°", 10, 18), ("Code", 30, 52), ("Désignation", 80, 130), ("Qté", 300, 316), ("PU HT", 340, 366), ("Taux", 400, 420), ("TVA", 450, 468), ("Total HT", 510, 550)),
    row(114, ("1", 10, 15), ("AR0142", 30, 60), ("Sac de glace pilée 10 kg", 80, 190), ("2", 305, 311), ("12,40", 342, 366), ("5,5 %", 398, 422), ("1,36", 450, 470), ("24,80", 520, 548)),
    row(128, ("2", 10, 15), ("AR0150", 30, 60), ("Sac de glace concassée 2 kg", 80, 170), ("3", 305, 311), ("3,00", 346, 366), ("5,5 %", 398, 422), ("0,50", 450, 470), ("9,00", 525, 548)),
    row(142, ("3", 10, 15), ("SV0001", 30, 60), ("Livraison", 80, 125), ("1", 305, 311), ("6,00", 346, 366), ("20 %", 400, 422), ("1,20", 450, 470), ("6,00", 525, 548)),
    row(180, ("Total HT", 400, 449), ("39,80", 520, 548)),
]


class EightColumnTests(SimpleTestCase):
    def test_every_column_has_its_role_and_the_rows_prove_their_tax(self):
        table = find_table(EIGHT_COLUMNS)
        by_x = sorted(table.columns, key=lambda column: column.x0)
        self.assertEqual(
            [column.label for column in by_x],
            [REFERENCE, REFERENCE, DESCRIPTION, QUANTITY, f"{UNIT_PRICE} HT", RATE, VAT_AMOUNT, f"{AMOUNT} HT"],
        )
        self.assertEqual(
            [(item.name, item.quantity, item.rate, item.ht, item.ttc) for item in table.items.values()],
            [
                ("Sac de glace pilée 10 kg", 2, D("0.055"), D("24.80"), D("26.16")),
                ("Sac de glace concassée 2 kg", 3, D("0.055"), D("9.00"), D("9.50")),
                ("Livraison", 1, D("0.20"), D("6.00"), D("7.20")),
            ],
        )

    def test_a_quantity_column_with_no_header_word_is_named_by_its_values(self):
        rows = [
            row(100, ("Désignation", 77, 122), ("PU HT", 357, 384), ("Montant HT", 542, 585)),
            row(113, ("Sac de glace 10 kg", 77, 170), ("2", 323, 329), ("12.40", 357, 384), ("24.80", 542, 569)),
            row(126, ("Livraison", 77, 130), ("1", 323, 329), ("6.00", 357, 384), ("6.00", 542, 563)),
            row(160, ("Total HT", 405, 449), ("30.80", 542, 569)),
        ]
        table = find_table(rows)
        quantity = next(column for column in table.columns if column.role == QUANTITY)
        self.assertTrue(quantity.confirmed)
        self.assertEqual([item.quantity for item in table.items.values()], [2, 1])
