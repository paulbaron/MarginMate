"""UBA parser tests.

Structurally faithful, data-invented fixtures (see test_parser_metro.py for
why). UBA's rows carry more traps than any other supplier's, and every one
below is a bug that was actually hit: duties silently dropped from the cost,
a CO2 cylinder's TUB unit not recognised at all, and an empty crate whose
PRIX HTHD column echoes its deposit and so fabricated a charge that was
never billed.

The 16 table columns, by index:
  0 CODE       1 DESIGNATION  2 Qté livré  3 Qté rupture  4 QUANTITE
  5 PRIX HTHD  6 MNT HTHD     7 % REMISE   8 DROITS       9 CONSIG.
 10 DECONS.   11 10-1 %      12 CONT.UNIT 13 VOL.EFFECTIF 14 ALCOOL PUR
 15 POIDS KG
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.uba import UBAParser

HEADER_ROW = [
    "CODE", "8\nDESIGNATION", "Quantité Livré", "Quantité en Rupture", "QUANTITE",
    "PRIX HTHD", "MNT HTHD", "% REMISE", "DROITS", "CONSIG.\nCONSIG.", "DECONS.",
    "10-1\n%", "CONT.\nUNIT.", "10\nVOLUME\nEFFECTIF", "10-2\nALCOOL\nPUR", "11-12\nPOIDS KG",
]

# A keg: priced per litre, with a deposit on the keg itself.
KEG_ROW = ["7412", "BIERE EXEMPLE FÛT 30L 4,9°", "5 FUT", "", "150 L",
           "1,3000", "195,00", "", "0,20", "150,00", "", "4,90", "1,00", "150,00", "7,35", "195,00"]
# Bottles: priced per bottle, no deposit.
BOTTLE_ROW = ["3325", "LIQUEUR EXEMPLE 70 CL 15° N", "4 BT", "", "4 BT",
              "6,0480", "24,19", "10,00 %", "2,03", "", "", "15,00", "0,70", "2,80", "0,42", "5,20"]
# A CO2 cylinder: flat per-unit price in a "TUB" unit, deposit on top, and
# no volume at all - so the cost comes from MNT HTHD directly.
CYLINDER_ROW = ["8056", "BOUTEILLE CO2 10 KG GRISE - CONS. 85€", "2 TUB", "", "2 TUB",
                "72,0000", "144,00", "", "", "170,00", "", "", "", "", "", "20,00"]
# An empty crate handed back: PRIX HTHD echoes the deposit, but MNT HTHD is
# BLANK - there is no product charge here, only the deposit.
EMPTY_CRATE_ROW = ["EMB29", "CFP VIDE", "1 EMB", "", "1 EMB",
                   "0,2000", "", "", "", "0,20", "", "", "", "", "", ""]
# Returning empty kegs: a refund, its whole value in the DECONS. column.
DECONSIGNE_ROW = ["EMB01", "FÛT 15/16/20/30 L", "-9 EMB", "", "-9 EMB",
                  "30,0000", "", "", "", "", "-270,00", "", "", "", "", ""]
BLANK_ROW = [None] * 9 + [""] + [None] * 6

TEXT = """\
FRANCE Tel: 01.48.81.00.99 - Fax: 01.48.81.22.85 Facture No : VE-2026080299
75010 PARIS 10EME ARRONDISSEMENT N° COMMANDE REF. CLIENT N° CLIENT TELEPHONE DATE COMMANDE DATE FACTURE
VE-2026080299 12/11/2024 15/11/2024
"""

ALL_ROWS = [KEG_ROW, BOTTLE_ROW, CYLINDER_ROW, EMPTY_CRATE_ROW, DECONSIGNE_ROW]


def make_page(rows=None, text=TEXT):
    return PdfPage(text=text, tables=[[HEADER_ROW, BLANK_ROW, *(rows if rows is not None else ALL_ROWS)]])


def parse(rows=None, text=TEXT):
    return UBAParser().parse_pages([make_page(rows, text)])


def names(invoice):
    return [line.raw_name for line in invoice.lines]


def line_named(invoice, name):
    return next(line for line in invoice.lines if line.raw_name == name)


class UBAProductLineTests(SimpleTestCase):
    def test_volume_priced_row_derives_its_total_from_the_volume(self):
        """150 L at 1,30/L, in 1,00 L containers."""
        line = line_named(parse([KEG_ROW]), "BIERE EXEMPLE FÛT 30L 4,9°")
        self.assertEqual(line.quantity, 5)
        self.assertEqual(line.total_volume, Decimal("150.00"))
        # 150/1 x 1,30 = 195,00 product + 150/1 x 0,20 duty
        self.assertEqual(line.total_ht, Decimal("195.00") + Decimal("30.00"))
        self.assertEqual(line.taxes, Decimal("30.00"))

    def test_duties_are_part_of_the_real_cost(self):
        """"DROITS" is a per-unit excise duty, not VAT - dropping it silently
        understated every alcohol line (213 lines, fixed this session)."""
        line = line_named(parse([BOTTLE_ROW]), "LIQUEUR EXEMPLE 70 CL 15° N")
        # 2,80 L / 0,70 L per bottle = 4 bottles; 4 x 6,048 + 4 x 2,03
        self.assertEqual(line.taxes, Decimal("8.12"))
        self.assertEqual(line.total_ht, Decimal("24.192") + Decimal("8.12"))
        self.assertEqual(line.unit_cost_ht, (line.total_ht / 4).quantize(Decimal("0.0001")))

    def test_flat_priced_row_without_volume_uses_the_printed_total(self):
        """A CO2 cylinder has no volume concept; its cost is MNT HTHD as
        printed. The TUB unit also has to be recognised at all - it wasn't,
        so these rows were dropped entirely."""
        invoice = parse([CYLINDER_ROW])
        line = line_named(invoice, "BOUTEILLE CO2 10 KG GRISE - CONS. 85€")
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.total_volume, Decimal("0"))
        self.assertEqual(line.total_ht, Decimal("144.00"))
        self.assertEqual(line.unit_cost_ht, Decimal("72.0000"))

    def test_vat_rate_comes_from_the_text_pass(self):
        """Product VAT lives only in the free text, keyed by code+name."""
        text = TEXT + (
            "7412 BIERE EXEMPLE FÛT 30L 4,9° 5 FUT 150 L 1,3000 195,00 "
            "0,20 150,00 4,90 1,00 150,00 7,35 195,00 2\n"
        )
        line = line_named(parse([KEG_ROW], text=text), "BIERE EXEMPLE FÛT 30L 4,9°")
        self.assertEqual(line.vat_rate, Decimal("0.055"))

    def test_vat_rate_defaults_to_20_percent_when_the_text_says_nothing(self):
        self.assertEqual(line_named(parse([KEG_ROW]), "BIERE EXEMPLE FÛT 30L 4,9°").vat_rate, Decimal("0.2"))


class UBADepositTests(SimpleTestCase):
    def test_consigne_becomes_its_own_line(self):
        """A deposit is billed on the product's own row but is a refundable
        charge on the container, not part of what the product is worth."""
        invoice = parse([KEG_ROW])
        self.assertIn("Consigne BIERE EXEMPLE FÛT 30L 4,9°", names(invoice))
        consigne = line_named(invoice, "Consigne BIERE EXEMPLE FÛT 30L 4,9°")
        self.assertEqual(consigne.total_ht, Decimal("150.00"))
        self.assertEqual(consigne.quantity, 5)
        self.assertEqual(consigne.unit_cost_ht, Decimal("30.0000"))
        self.assertEqual(consigne.vat_rate, Decimal("0"))
        self.assertEqual(consigne.category, "UBA - Consignes")

    def test_deconsigne_row_is_a_negative_line(self):
        invoice = parse([DECONSIGNE_ROW])
        line = line_named(invoice, "FÛT 15/16/20/30 L")
        self.assertEqual(line.quantity, -9)
        self.assertEqual(line.total_ht, Decimal("-270.00"))
        self.assertEqual(line.unit_cost_ht, Decimal("30.0000"))
        self.assertEqual(line.vat_rate, Decimal("0"))

    def test_empty_crate_produces_only_its_deposit_not_a_phantom_charge(self):
        """The one that was actually billed wrong: PRIX HTHD echoes the
        deposit (0,2000) while MNT HTHD is blank, so quantity x unit price
        invented a 0,20 EUR product charge that was never on the invoice."""
        invoice = parse([EMPTY_CRATE_ROW])
        self.assertEqual(names(invoice), ["Consigne CFP VIDE"])
        self.assertEqual(invoice.lines[0].total_ht, Decimal("0.20"))

    def test_zero_cost_informational_rows_are_skipped(self):
        """UBA prints a nominal 0,0001 in PRIX HTHD for equipment loans and
        POS material - not real purchases."""
        freebie = ["9001", "PUB VERRE EXEMPLE", "6 PUB", "", "6 PUB",
                   "0,0001", "", "", "", "", "", "", "", "", "", ""]
        self.assertEqual(parse([freebie]).lines, [])


class UBAInvoiceLevelTests(SimpleTestCase):
    def test_invoice_number_and_date(self):
        invoice = parse()
        self.assertEqual(invoice.invoice_number, "VE-2026080299")
        # DATE COMMANDE then DATE FACTURE on the same line - FACTURE is last.
        self.assertEqual(invoice.invoice_date, date(2024, 11, 15))

    def test_reconciliation_adjustment_closes_the_gap_to_the_printed_total(self):
        """UBA's printed grand total includes duty categories that never
        appear in any per-product column, so the lines alone understate it."""
        text = TEXT + "257,19 39,82 53,69 14 U.B.A. 01.48.81.00.99\n"
        invoice = parse([BOTTLE_ROW], text=text)
        product_total = Decimal("24.192") + Decimal("8.12")
        self.assertEqual(
            invoice.reconciliation_adjustment,
            (Decimal("257.19") + Decimal("39.82")) - product_total,
        )

    def test_no_adjustment_when_no_grand_total_is_printed(self):
        """Better a 0 adjustment than a guessed one."""
        self.assertEqual(parse([BOTTLE_ROW]).reconciliation_adjustment, Decimal("0"))

    def test_all_rows_together(self):
        invoice = parse()
        self.assertEqual(
            names(invoice),
            [
                "BIERE EXEMPLE FÛT 30L 4,9°",
                "Consigne BIERE EXEMPLE FÛT 30L 4,9°",
                "LIQUEUR EXEMPLE 70 CL 15° N",
                "BOUTEILLE CO2 10 KG GRISE - CONS. 85€",
                "Consigne BOUTEILLE CO2 10 KG GRISE - CONS. 85€",
                "Consigne CFP VIDE",
                "FÛT 15/16/20/30 L",
            ],
        )

    def test_non_product_tables_are_ignored(self):
        page = PdfPage(text=TEXT, tables=[[["Libellé", "Montant"], ["Total", "257,19"]]])
        self.assertEqual(UBAParser().parse_pages([page]).lines, [])

    def test_date_hint_used_only_when_the_invoice_prints_no_date(self):
        invoice = UBAParser().parse_pages([make_page(text="Facture No : VE-1\n")], date_hint=date(2026, 3, 3))
        self.assertEqual(invoice.invoice_date, date(2026, 3, 3))
