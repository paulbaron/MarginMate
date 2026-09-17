"""The one ticket reader, on the layouts the four shop parsers missed.

Each fixture copies the structure of a real ticket the old parsers misread
(the ticket's number in the class docstring is the real database's); every
name and amount is invented, and each fixture's arithmetic holds, since that
is what the reader checks itself against.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers import ticket_parser_for
from invoices.parsers.base import PdfPage
from invoices.parsers.generic_receipt import (
    Reading,
    _discount_targets,
    _split_by_buckets,
    _spread,
    _taxed_row,
)
from invoices.parsers.receipt_base import VatSummary

D = Decimal
FIVE_FIVE = D("0.055")
TWENTY = D("0.20")

FRANPRIX_HEAD = "franprix\nFRANPRIX\n12 RUE INVENTEE\n75000 PARIS\n004211-01\n**DUPLICATA**-\n"
FRANPRIX_FOOT = "15-07-2026 MARDI  13:40\nLORIAN  R1 004211-01 190\n**DUPLICATA**-\n"


def parse(shop, text):
    return ticket_parser_for(shop).parse_pages([PdfPage(text=text)])


def failed(invoice):
    return {check.label: check.detail for check in invoice.checks if not check.passed}


def check(invoice, label):
    return next(item for item in invoice.checks if item.label == label)


def lines(invoice):
    return [(line.raw_name, line.quantity, line.printed_ttc, line.discount_ttc) for line in invoice.lines]


class CancelledItemTests(SimpleTestCase):
    """510, 566, 604: a Christmas tree scanned twice and cancelled twice
    ("NUL LIGNE", then the item at minus its price) was bought four times."""

    TEXT = FRANPRIX_HEAD + (
        "SAPIN INVENTE 1  T4 30.00Eur\n"
        "SAPIN INVENTE 1  T4 30.00Eur\n"
        "NUL LIGNE\n"
        "SAPIN INVENTE 1  T4 -30.00Eur\n"
        "30.00Eur\n"
        "NUL LIGNE\n"
        "SAPIN INVENTE 1  T4-30.00Eur\n"
        "SOUS-TOTAL  0.00Eur\n"
        "PAIN COMPLET  T1 0.55Eur\n"
        "CITRON VERT 400G  T1 1.80Eur\n"
        "SOUS-TOTAL  2.35Eur\n"
        "TOTAL A PAYER  2.35Eur\n"
        "CB SANS CONTACT  2.35Eur\n"
        "rTaux-Tr-Tot.HT-r-Tot.TVA-r-Tot.TTC-\n"
        "5.5%  2.23  0.12  2.35\n"
    ) + FRANPRIX_FOOT

    def test_a_cancelled_item_is_not_bought(self):
        invoice = parse("FRANPRIX", self.TEXT)
        self.assertEqual(
            lines(invoice),
            [("PAIN COMPLET", 1, D("0.55"), D("0")), ("CITRON VERT 400G", 1, D("1.80"), D("0"))],
        )
        self.assertEqual(failed(invoice), {})

    def test_an_item_cancelled_for_more_than_it_cost_is_a_refund(self):
        """568: a weighed courgette cancelled in two goes, for more than it
        was scanned at. What was paid says the till refunded the difference."""
        text = FRANPRIX_HEAD + (
            "Poids Brut 1.500 kg\n"
            "Eur 2.80Eur / kg\n"
            "COURGETTE INVENTEE  T1 4.20Eur\n"
            "NUL-IMADIS M3\n"
            "COURGETTE INVENTEE  T1-3.60Eur\n"
            "NUL-IMADIS  M3\n"
            "COURGETTE INVENTEE  T1-0.94Eur\n"
            "FRUITS ET LEGUME  T1 0.79Eur\n"
            "FRUITS ET LEGUME  T1 0.79Eur\n"
            "FRUITS ET LEGUME  T1 0.79Eur\n"
            "FRUITS ET LEGUME  T1 0.79Eur\n"
            "SOUS-TOTAL  2.82Eur\n"
            "TOTAL A PAYER  2.82Eur\n"
            "CARTES BLEUES A  2.82Eur\n"
            "5.5%  2.67  0.15  2.82\n"
        ) + FRANPRIX_FOOT
        invoice = parse("FRANPRIX", text)
        refund = invoice.lines[0]
        self.assertEqual((refund.raw_name, refund.quantity, refund.printed_ttc), ("COURGETTE INVENTEE", -1, D("-0.34")))
        self.assertEqual(refund.total_volume, D("0"))
        self.assertEqual(len(invoice.lines), 5)
        self.assertEqual(failed(invoice), {})


class PromotionTests(SimpleTestCase):
    """488: a "3 pour 2" worth 0,50 was charged to the one loaf whose name
    matched the promotion block's, which cost 0,49 - a loaf at -0,01."""

    TEXT = FRANPRIX_HEAD + (
        "CONCOMBRE  T1 1.99Eur\n"
        "PAIN COMPLET  T1 0.49Eur\n"
        "PAIN COMPLFT  T1 0.49Eur\n"
        "PAIN COMPIET  T1 0.49Eur\n"
        "SOUS-TOTAL  2.96Eur\n"
        "TOTAL SANS AVANTAGES  3.46Eur\n"
        "Detai des renises inmediates :\n"
        "3 pour 2\n"
        "PAIN COMPLEI  0.50Eur\n"
        "TOTAL remise  0.50Eur\n"
        "TOTAL A PAYER  2.96Eur\n"
        "CB SANS CONTACT  2.96Eur\n"
        "rTaux-Ir-Tot.HT-r-Tot.TVA-r-Tot.TTC-\n"
        "15.5%  2.81  0.15  2.96\n"
    ) + FRANPRIX_FOOT

    def test_the_promotion_goes_to_every_reading_of_the_product(self):
        invoice = parse("FRANPRIX", self.TEXT)
        self.assertEqual(
            lines(invoice),
            [
                ("CONCOMBRE", 1, D("1.99"), D("0")),
                ("PAIN COMPLET", 1, D("0.49"), D("0.17")),
                ("PAIN COMPLFT", 1, D("0.49"), D("0.17")),
                ("PAIN COMPIET", 1, D("0.49"), D("0.16")),
            ],
        )
        self.assertTrue(check(invoice, "Remise attribuée").passed)
        self.assertEqual(failed(invoice), {})

    def test_no_line_costs_less_than_nothing(self):
        for line in parse("FRANPRIX", self.TEXT).lines:
            self.assertGreater(line.total_ht, 0)

    def test_the_cost_is_the_price_less_the_promotion(self):
        loaf = parse("FRANPRIX", self.TEXT).lines[1]
        self.assertEqual(loaf.total_ht, D("0.30"))  # 0,32 TTC at 5.5%
        self.assertEqual(loaf.discount, D("0.16"))  # 0,46 HT printed


class MisreadAmountTests(SimpleTestCase):
    """500: one loaf's 0,49 read as 0,45. The ticket's pre-discount total
    (1,47) said an item was wrong - and a person looking at three loaves at
    0,33 each saw nothing wrong with them."""

    TEXT = FRANPRIX_HEAD + (
        "PAIN COMPLET  T10.45\n"
        "PAIN COMPLFT  T1 0.49\n"
        "PAIN COMPLET  T1 0.49\n"
        "SOUS-TOTAL  0.98\n"
        "TOTAL SANS AVANTAGES  1.47\n"
        "Detai des remises immediates :\n"
        "3 pour 2\n"
        "PAIN COMPLET  0.49\n"
        "TOTAL renise  0.49\n"
        "TOTAL A PAYER  0.98\n"
        "CB SANS CONTACT  0.98\n"
        "Rate  Taxaole Vat Total\n"
        "15.5%  0.931  0.051  0.981\n"
    ) + FRANPRIX_FOOT

    def test_the_amount_is_its_namesakes_when_that_is_what_the_ticket_adds_up_to(self):
        invoice = parse("FRANPRIX", self.TEXT)
        self.assertEqual([line.printed_ttc for line in invoice.lines], [D("0.49")] * 3)
        self.assertEqual(sum(line.discount_ttc for line in invoice.lines), D("0.49"))
        repaired = check(invoice, "Montants recalculés")
        self.assertTrue(repaired.passed)
        self.assertIn("lu 0.45 €", repaired.detail)
        self.assertEqual(failed(invoice), {})

    def test_two_loaves_that_happen_to_make_what_was_paid_are_not_the_purchase(self):
        """The ticket proves 1,47 of items; 0,49 + 0,49 = 0,98 is a
        coincidence with the amount paid."""
        self.assertEqual(len(parse("FRANPRIX", self.TEXT).lines), 3)

    def test_a_gap_no_namesake_explains_is_still_reported(self):
        """A loaf lost as well: 0,45 + 0,49 is 0,53 short of 1,47, which no
        single amount corrected makes up."""
        invoice = parse("FRANPRIX", self.TEXT.replace("PAIN COMPLET  T1 0.49\n", ""))
        self.assertIn("Articles = total avant remise", failed(invoice))
        self.assertNotIn("Montants recalculés", {item.label for item in invoice.checks})


class VatCodeReadAsDigitTests(SimpleTestCase):
    """534: "T2 0.30" read "120.30", and a sub-total misread "SOUSOTA4.25"
    among the items: 154,75 EUR of lines on a 14,25 EUR ticket."""

    TEXT = FRANPRIX_HEAD + (
        "SAC PAPIER INVENTE  120.30\n"
        "PAIN COMPLET  T1 0.49.\n"
        "CITRON VERT 400G  T1 1.80.\n"
        "SOUSOTA1.29\n"
        "TOTAL A PAYER  2.59.\n"
        "CB SANS CONTACT  2.59.\n"
        "Rate-Taxale-Vat  Total\n"
        "5.5%  2.17  0.12  2.29\n"
        "20%  0.25  0.05  0.30\n"
    ) + FRANPRIX_FOOT

    def test_the_shorter_reading_is_the_one_that_adds_up(self):
        invoice = parse("FRANPRIX", self.TEXT)
        self.assertEqual(
            [(line.raw_name, line.printed_ttc, line.vat_rate) for line in invoice.lines],
            [
                ("SAC PAPIER INVENTE", D("0.30"), TWENTY),
                ("PAIN COMPLET", D("0.49"), FIVE_FIVE),
                ("CITRON VERT 400G", D("1.80"), FIVE_FIVE),
            ],
        )
        self.assertIn("lu 120.30 €, 0.30 €", check(invoice, "Montants recalculés").detail)
        self.assertEqual(failed(invoice), {})

    def test_a_ticket_with_no_coded_line_has_no_item_read(self):
        """A Franprix item always carries its code: with the only one lost,
        "SOUS-TOTAL 0.98" is not a purchase that happens to add up."""
        text = FRANPRIX_HEAD + "SOUS-TOTAL  0.98Eur\nTOTAL A PAYER  0.98Eur\n5.5%  0.93  0.05  0.98\n" + FRANPRIX_FOOT
        invoice = parse("FRANPRIX", text)
        self.assertEqual(invoice.lines, [])
        self.assertIn("Somme des lignes = total imprimé", failed(invoice))

    def test_a_count_times_a_price_is_an_item_whatever_its_code_reads(self):
        """486: the ticket's only line, its "T1" read "11"."""
        text = FRANPRIX_HEAD + (
            "PAIN COMPLET  11  7 X 0.49Eur 3.43Eur\n"
            "TOTAL ANS AVANTAGES  3.43Eur\n"
            "Detai des remises immediates :\n"
            "PAIN COMPLET  0.99Eur\n"
            "TOTEL renise  0.99Eur\n"
            "TOTAL 4 PAYER  2.44Lur\n"
            "CB SANS CONTACT  2.44Eur\n"
            "155%  2.31  0.131  2.441\n"
        ) + FRANPRIX_FOOT
        invoice = parse("FRANPRIX", text)
        self.assertEqual(lines(invoice), [("PAIN COMPLET", 7, D("3.43"), D("0.99"))])
        self.assertEqual(failed(invoice), {})


class MisreadSubtotalTests(SimpleTestCase):
    """525: "SOUS-TOTAL 7.05" read "1.25" ran the items past their end, into
    the promotion block."""

    TEXT = FRANPRIX_HEAD + (
        "CITRON VERT 400G  T1 1.80Eur\n"
        "POMME INVENTEE BQ  T1 2.70Eur\n"
        "SUCRE CASSOVA)  T1 3.40Eur\n"
        "ISOUSHTOTALI  1.25Eur\n"
        "TOTAL SANS AVANTAGES  7.90Eur\n"
        "25 % RISucre  Detai des remises immediates :\n"
        "SUCRE CASSONAD  0.85Eur\n"
        "TOTAL renise  0.85Eur\n"
        "TOTAL A PAYER  7.05Eur\n"
        "CB SANS CONTACT  7.05Eur\n"
        "rTaux-Tr-Tot.HT--r-Tot.TVA--Tot.TTC\n"
        "5.5%  6.68|  0.371  7.051\n"
    ) + FRANPRIX_FOOT

    def test_the_items_stop_at_the_total_they_make(self):
        invoice = parse("FRANPRIX", self.TEXT)
        self.assertEqual(
            lines(invoice),
            [
                ("CITRON VERT 400G", 1, D("1.80"), D("0")),
                ("POMME INVENTEE BQ", 1, D("2.70"), D("0")),
                ("SUCRE CASSOVA)", 1, D("3.40"), D("0.85")),
            ],
        )
        self.assertEqual(failed(invoice), {})


class CashOnADoublePhotoTests(SimpleTestCase):
    """536: the photo held the ticket twice, so "RENDU 0.40" was printed
    twice - and change looked like a promotion: "un article manque"."""

    COPY = (
        "franprix\nFRANPRIX\n12 RUE INVENTEE\n004211-02\n"
        "NETTOYANT INVENTE  T2 4.80Eur\n"
        "NETTOYANT INVENTE  T2 4.80Eur\n"
        "SOUS-TOTAL  9.60Eur\n"
        "TOTAL A PAYER  9.60Eur\n"
        "ESPECES  10.00Eur\n"
        "RENDU  0.40Eur\n"
        "rTaux-T-Tot.HT--Tot.TVA--Tot.TTC\n"
        "20%  8.00|  1.601  9.601\n"
        "03-02-2026 SAMEDI  15:32\n"
        "LORIAN  R1 004211-02 265\n"
    )

    def test_change_is_not_a_promotion(self):
        invoice = parse("FRANPRIX", self.COPY + self.COPY)
        self.assertEqual(len(invoice.lines), 2)
        self.assertEqual(failed(invoice), {})


class ItemPromotionTests(SimpleTestCase):
    """509: a promotion printed under its item was dropped because an item
    elsewhere was unreadable - the gap on screen mixed the two."""

    def test_it_is_kept_and_the_gap_is_the_missing_item(self):
        text = FRANPRIX_HEAD + (
            "FROMAGE INVENTE  T1 2.50Eur\n"
            "30%\n"
            "REMISE 30%  -0.75Eur\n"
            "PAIN COMPLET  T1 0.49Eur\n"
            "T1 0 49Fur\n"
            "TOTAL A PAYER  2.73Eur\n"
            "CB SAN CONTACT  2.73Eur\n"
            "15.5%  2.59  0.14  2.73\n"
        ) + FRANPRIX_FOOT
        invoice = parse("FRANPRIX", text)
        self.assertEqual(invoice.lines[0].discount_ttc, D("0.75"))
        self.assertIn("écart +0.49 €", failed(invoice)["Somme des lignes = total imprimé"])


MONOPRIX_HEAD = "MONOPRIX\nDimax\n12 RUE INVENTEE,\n75000 PARIS\nBONJOUR\n"
MONOPRIX_FOOT = "28/05/2026 16:42 245 77 7999 770\n024507707999260528164254\n"


class SplitRowTests(SimpleTestCase):
    """617, 618, 631: a Monoprix row read in two pieces, its amount on the
    department heading above it - or its name lost altogether."""

    TEXT = MONOPRIX_HEAD + (
        "EPICERIE/BOISSONS.  6,70€\n"
        "2 X SAUCE INVENTEE 3,35€\n"
        "FRUITS/LEGUMES.  2,51€\n"
        "POMME INVENTEE BIO\n"
        "3 X BETTERAVE INVENTEE 1,29€  3,87€\n"
        "SURGELES/PRODUITS FRAIS.\n"
        "1,50€\n"
        "TOTAL HORS AVANTAGES  14,58€\n"
        "NOMBRE D'ARTICLES  7\n"
        "RESTE A PAYER  14,58€\n"
        "PAIEMENT\n"
        "CB EMV  14,58€\n"
        "TVA  H.T.  T.V.A.  T.T.C\n"
        "5,5%  13,82  0,76  14,58\n"
        "Total TVA  13,82  0,76  14,58\n"
    ) + MONOPRIX_FOOT

    def setUp(self):
        self.invoice = parse("MONOPRIX", self.TEXT)

    def test_each_piece_finds_its_row(self):
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc, line.category) for line in self.invoice.lines],
            [
                ("SAUCE INVENTEE", 2, D("6.70"), "EPICERIE/BOISSONS"),
                ("POMME INVENTEE BIO", 1, D("2.51"), "FRUITS/LEGUMES"),
                ("BETTERAVE INVENTEE", 3, D("3.87"), "FRUITS/LEGUMES"),
                ("Article non lu", 1, D("1.50"), "SURGELES/PRODUITS FRAIS"),
            ],
        )
        self.assertEqual(failed(self.invoice), {})

    def test_an_amount_without_its_name_is_to_be_named(self):
        self.assertTrue(self.invoice.lines[3].is_placeholder)


class InvoiceStyleRowTests(SimpleTestCase):
    """625: a Monoprix "facture" prints unit HT, count, HT, rate, VAT and TTC
    on each row. Its rum came out at 5.5% and one bottle, and the onion's
    TTC ("0 63e") could not be read at all."""

    TEXT = (
        "MONOPRIX\nCie URBAINE INVENTEE\n12 RUE INVENTEE\n75000 PARIS\nTEL: 01 00 00 00 00\n"
        "Le  24/01/2026\nFACTURE n°:02450000001\n"
        "Caisse : 8 - Ticket no 4278 - 24/01/2026 (17:26) - (6565)\n"
        "Description  Prix unitaire  Qté  Prix  % TVA  T.V.A.  Remise  Prix\n"
        "H.T  H.T  Montant  TTC  TTC\n"
        "1L RHUM INVENTE  15,00 €  4  60,00 €  20,00  12,00 €  72,00 €\n"
        "FROMAGE BLANC INVENTE  2,00 €  1  2,00 €  5,50  0,11 €  2,11 €\n"
        "OIGNON INVENTE VRAC  0,60 €  1  0,60 €  5,50  0,03 €  0 63e\n"
        "Moyens de paiement, pour information.\n"
        "CB EMV  74,74 €\n"
        "Rendu (ESPECES)  0,0e\n"
        "Total  6  62,60 €  12,14 €  74,74 e\n"
        "Total H.T :  62,60 €\n"
        "Total TVA :  12,14 €\n"
        "Total TTC :  74,74 €\n"
        "Prix  T.V.A.  Prix\n"
        "T.V.A. 5,50% :  2,60 €  0,14 €  2,74 €\n"
        "T.V.A. 20,00% :  60,00 €  12,00 €  72,00 €\n"
    )

    def setUp(self):
        self.invoice = parse("MONOPRIX", self.TEXT)

    def test_each_row_carries_its_own_rate_and_count(self):
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc, line.total_ht, line.vat_rate) for line in self.invoice.lines],
            [
                ("1L RHUM INVENTE", 4, D("72.00"), D("60.00"), TWENTY),
                ("FROMAGE BLANC INVENTE", 1, D("2.11"), D("2.00"), FIVE_FIVE),
                ("OIGNON INVENTE VRAC", 1, D("0.63"), D("0.60"), FIVE_FIVE),
            ],
        )

    def test_an_unreadable_ttc_is_worked_out_and_said(self):
        self.assertIn("OIGNON INVENTE VRAC 0.63 €", check(self.invoice, "Montants recalculés").detail)

    def test_the_ticket_adds_up(self):
        self.assertEqual(failed(self.invoice), {})
        self.assertEqual(self.invoice.invoice_number, "4278")


class OtherFranprixTillTests(SimpleTestCase):
    """541, 567: a Franprix with another till - the code before the count,
    unit price and amount in euros, the VAT table lettered."""

    TEXT = (
        "SUPER INVENTE - PARIS\nPARIS 10 75000\nFRANCE\nTél : 01 00 00 00 00\n"
        "19/01/2026 16:16:46 N° Ticket vente 351194\n"
        "Caissier  102 INVENTE 102\n"
        "B0075A1wH 003095123.0.15-8.40  0 Caisse 003\n"
        "T Qt Description  PU  TTC\n"
        "DESSERTS PRETS A CONSOMMER\n"
        "A 1x VANILLE INVENTEE 3,25 € 3,25 €\n"
        "RHUMS BLANCS\n"
        "B 1x RHUM INVENTE 55° 25,00 € 25,00 €\n"
        "SACS DE CAISSE\n"
        "B 3x CABAS INVENTE 0,80 € 2,40 €\n"
        "3 LIGNES  5 ARTICLES\n"
        "TVA  Taux  Mt.HT  Mt.TVA  Mt.TTC\n"
        "A  5,5%  3,08 €  0,17 €  3,25 €\n"
        "B  20%  22,83 €  4,57 €  27,40 €\n"
        "TOTAL HT  MTXIE  25,91 €\n"
        "NET TTC  30,65 €\n"
        "CB SSC  E2C.C0110e.  30,65 €\n"
        "VOTRE MAGASIN\nFRANPRIX\nVOUS REMERCIE\n"
    )

    def test_the_lines_their_counts_and_rates(self):
        invoice = parse("FRANPRIX", self.TEXT)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc, line.vat_rate) for line in invoice.lines],
            [
                ("VANILLE INVENTEE", 1, D("3.25"), FIVE_FIVE),
                ("RHUM INVENTE 55°", 1, D("25.00"), TWENTY),
                ("CABAS INVENTE", 3, D("2.40"), TWENTY),
            ],
        )
        self.assertEqual(failed(invoice), {})
        self.assertEqual(invoice.invoice_number, "351194")

    def test_two_identical_lines_are_two_items(self):
        text = self.TEXT.replace("B 1x RHUM INVENTE 55° 25,00 € 25,00 €", "A 1x VANILLE INVENTEE 3,25 € 3,25 €")
        invoice = parse("FRANPRIX", text)
        self.assertEqual([line.raw_name for line in invoice.lines].count("VANILLE INVENTEE"), 2)


SABBH_HEAD = "Sabbh Oriental\n12 Rue Inventee\n75000 Paris\nTel 01 00 00 00 00\n"


class SabbhTests(SimpleTestCase):
    def test_a_price_with_two_separators(self):
        """374: "0,.70" - the price per unit unreadable, the 7,00 beside it
        was taken for one and the line came to 70,00."""
        text = SABBH_HEAD + (
            "Heure: 01-07-2026 14:44:25  Balance: 68\nPLU\nkg(pcs) €  €/kg(pcs)\n"
            "Article divers\n10pcs  0,.70  7,00 A\n"
            "Articles: 1  Total: 7,00\nEspèces  7,00€\nInformation TVA\nTaux  Base TVA  TVA\n"
            "TVAA  5.50%  7,00  0,36\n"
        )
        invoice = parse("SABBH", text)
        self.assertEqual(lines(invoice), [("Article divers", 10, D("7.00"), D("0"))])
        self.assertEqual(failed(invoice), {})

    def test_a_single_small_line_has_its_total(self):
        """325 and four more: 2,80 printed four times, and no total found -
        the VAT row read as 2,80 HT."""
        text = SABBH_HEAD + (
            "Heure:26-10-202415:01:43  Balance: 68\nPLU\nkg(pcs) €/kg(pcs)\n"
            "Article divers\n4pcs  0,70  2,80 A\n"
            "Articles: 1  Total: 2,80\nEspèces  2,80€\nInformation TVA\nTaux  Base TVA  TVA\n"
            "TVAA 5.50%  2,80  0,15\n"
        )
        invoice = parse("SABBH", text)
        self.assertEqual(invoice.printed_total_ttc, D("2.80"))
        self.assertEqual(lines(invoice), [("Article divers", 4, D("2.80"), D("0"))])
        self.assertTrue(invoice.lines[0].is_placeholder)
        self.assertEqual(failed(invoice), {})


def reading(name, total, index=0, count=None, code=""):
    return Reading(index=index, name=name, total=D(total), count=count, code=code, read_total=D(total))


class SpreadTests(SimpleTestCase):
    def test_the_cents_left_go_to_the_largest_remainders(self):
        loaves = [reading("PAIN", "0.49", index) for index in range(6)]
        _spread(loaves, D("0.98"))
        self.assertEqual([loaf.discount for loaf in loaves], [D("0.17"), D("0.17")] + [D("0.16")] * 4)

    def test_pro_rata(self):
        items = [reading("A", "3.00"), reading("B", "1.00", 1)]
        _spread(items, D("1.00"))
        self.assertEqual([item.discount for item in items], [D("0.75"), D("0.25")])

    def test_a_discount_larger_than_the_items_is_not_spread(self):
        items = [reading("A", "0.49")]
        _spread(items, D("0.50"))
        self.assertEqual(items[0].discount, D("0"))


class DiscountTargetTests(SimpleTestCase):
    def test_nothing_named_like_the_promotion(self):
        self.assertIsNone(_discount_targets([reading("CITRON", "1.80")], ["TOTAL remise", "pour"], D("0.50")))

    def test_products_that_cost_less_than_the_promotion_are_not_its_products(self):
        self.assertIsNone(_discount_targets([reading("PAIN COMPLET", "0.49")], ["PAIN COMPLET"], D("0.50")))

    def test_the_best_match_and_its_other_readings(self):
        items = [
            reading("PAIN COMPLET", "0.49", 0),
            reading("PAIN CONPLET", "0.49", 1),
            reading("CITRON VERT", "1.80", 2),
        ]
        targets = _discount_targets(items, ["PAIN COMPLEI"], D("0.49"))
        self.assertEqual([item.index for item in targets], [0, 1])


class TaxedRowTests(SimpleTestCase):
    def test_ht_vat_and_ttc(self):
        self.assertEqual(_taxed_row([D("15.00"), D("60.00"), D("20.00"), D("12.00"), D("72.00")]), (D("60.00"), D("72.00"), TWENTY, False))

    def test_the_ttc_worked_out_from_the_printed_rate(self):
        self.assertEqual(_taxed_row([D("0.60"), D("0.60"), D("5.50"), D("0.03")]), (D("0.60"), D("0.63"), FIVE_FIVE, True))

    def test_a_count_and_two_prices_are_not_a_taxed_row(self):
        self.assertIsNone(_taxed_row([D("0.49"), D("2.94"), D("3.43")]))

    def test_a_tax_two_rates_explain_is_not_read(self):
        """0,20 HT and 0,01 of tax: 5.5% or 2.1%, to the cent."""
        self.assertIsNone(_taxed_row([D("0.20"), D("0.01"), D("0.21")]))


class SplitByBucketsTests(SimpleTestCase):
    BUCKETS = (
        VatSummary(rate=FIVE_FIVE, base=D("2.17"), vat_amount=D("0.12"), total_ttc=D("2.29")),
        VatSummary(rate=TWENTY, base=D("0.25"), vat_amount=D("0.05"), total_ttc=D("0.30")),
    )

    def test_the_one_split_that_makes_both_buckets(self):
        items = [reading("SAC", "0.30", 0), reading("PAIN", "0.49", 1), reading("CITRON", "1.80", 2)]
        rates = _split_by_buckets(items, self.BUCKETS)
        self.assertEqual([rates[id(item)] for item in items], [TWENTY, FIVE_FIVE, FIVE_FIVE])

    def test_two_splits_is_no_split(self):
        buckets = [
            VatSummary(rate=FIVE_FIVE, base=D("0.95"), vat_amount=D("0.05"), total_ttc=D("1.00")),
            VatSummary(rate=TWENTY, base=D("0.83"), vat_amount=D("0.17"), total_ttc=D("1.00")),
        ]
        self.assertIsNone(_split_by_buckets([reading("A", "1.00", 0), reading("B", "1.00", 1)], buckets))

    def test_identical_items_are_interchangeable(self):
        buckets = [
            VatSummary(rate=FIVE_FIVE, base=D("0.95"), vat_amount=D("0.05"), total_ttc=D("1.00")),
            VatSummary(rate=TWENTY, base=D("2.50"), vat_amount=D("0.50"), total_ttc=D("3.00")),
        ]
        items = [reading("PAIN", "1.00", 0), reading("VIN", "3.00", 1)]
        rates = _split_by_buckets(items, buckets)
        self.assertEqual([rates[id(item)] for item in items], [FIVE_FIVE, TWENTY])
