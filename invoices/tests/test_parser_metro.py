"""Metro parser tests.

The page text below is *structurally* a real Metro invoice - every column
position, separator and quirk is copied from actual PDFs - but every EAN,
product name and amount is invented, so this fixture can live in git while
real invoices (which carry IBANs and delivery addresses) stay out of it.

Each test covers a quirk that has already cost real money or real debugging
time: the social-security levy and the bulk discount were both silently
dropped from `total_ht` until this session, understating 602 invoice lines
by 2,644.53 EUR in total.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.metro import MetroParser

# One product per quirk. Columns, in order:
#   EAN  N#  Désignation  [Régie]  [Vol%]  [VAP]  [Poids/Vol]  PrixUnit
#   [Colisage]  Qté  Montant  TVA
# The "②" is really in Metro's own PDFs (a footnote marker), and it matters:
# the store-number regex takes the LAST parenthesised number in the token, so
# a fixture that substitutes a plain "(2)" for it silently parses store "2".
HEADER = """\
METRO FRANCE
5, rue des Grands Pres 40 AVENUE DES TERROIRS DE FRANCE ② Date facture : 28-08-2026 07:03
Nº FACTURE 0/0(134)0056/038190② (056-056683) 134/010
"""

BODY = """\
05010106013120 1933605 WHISKY EXEMPLE 40D 70CL S 40,0 0,280 0,700 12,875 6 1 77,25 D
Plus : COTIS. SECURITE SOCIALE 10,42 D
1933662 GIN EXEMPLE 37.5D 70CL S 37,5 0,263 0,700 7,400 6 3 133,20 D P
Plus : COTIS. SECURITE SOCIALE 3,75 D
Offre Achetez Plus Payez Moins 1,80-
*** Spiritueux Total: 222,87
+ 2604800 CAISSE EXEMPLE 24X33CL PLEIN 5,500 1 2 11,00 A
20297794 0297796 PALETTE EUROPE 15,000 1 1- 15,00- A
*** Articles divers Total: 4,00-
"""

PAGE = PdfPage(text=HEADER + BODY)


def parse(pages=None, source_name="134_56_37795_20260504145552_invoice.pdf"):
    return MetroParser().parse_pages(pages or [PAGE], source_name=source_name)


def line_named(invoice, name):
    return next(line for line in invoice.lines if line.raw_name == name)


class MetroParserTests(SimpleTestCase):
    def test_parses_every_product_row(self):
        invoice = parse()
        self.assertEqual(
            [line.raw_name for line in invoice.lines],
            [
                "WHISKY EXEMPLE 40D 70CL",
                "GIN EXEMPLE 37.5D 70CL",
                "CAISSE EXEMPLE 24X33CL PLEIN",
                "PALETTE EUROPE",
            ],
        )

    def test_social_security_levy_is_added_to_total(self):
        """"Plus : COTIS. SECURITE SOCIALE" is billed on its own line right
        after the product and is a real, mandatory part of what was paid."""
        line = line_named(parse(), "WHISKY EXEMPLE 40D 70CL")
        self.assertEqual(line.taxes, Decimal("10.42"))
        self.assertEqual(line.total_ht, Decimal("77.25") + Decimal("10.42"))

    def test_a_levy_printed_at_the_top_of_the_next_page_still_counts(self):
        """A page break between a bottle and its levy line used to drop the
        levy: the product the page started with was forgotten."""
        levy = "Plus : COTIS. SECURITE SOCIALE 10,42 D" + chr(10)
        bottom, top = BODY.split(levy)
        first = PdfPage(text=HEADER + bottom)
        second = PdfPage(text=HEADER + levy + top)
        invoice = parse([first, second])
        whisky = line_named(invoice, "WHISKY EXEMPLE 40D 70CL")
        self.assertEqual(whisky.taxes, Decimal("10.42"))
        self.assertEqual(whisky.total_ht, line_named(parse(), "WHISKY EXEMPLE 40D 70CL").total_ht)

    def test_bulk_discount_is_subtracted_from_total(self):
        line = line_named(parse(), "GIN EXEMPLE 37.5D 70CL")
        self.assertEqual(line.discount, Decimal("1.80"))
        self.assertEqual(line.taxes, Decimal("3.75"))
        # montant - remise + taxe, the formula the original parser used.
        self.assertEqual(line.total_ht, Decimal("133.20") - Decimal("1.80") + Decimal("3.75"))

    def test_unit_cost_is_derived_from_the_corrected_total(self):
        """The regression that matters: unit_cost_ht must be recomputed from
        the levy/discount-corrected total, not from the printed unit price."""
        line = line_named(parse(), "GIN EXEMPLE 37.5D 70CL")
        self.assertEqual(line.quantity, 18)  # colisage 6 x qty 3
        self.assertEqual(line.unit_cost_ht, (Decimal("135.15") / 18).quantize(Decimal("0.0001")))
        self.assertNotEqual(line.unit_cost_ht, Decimal("7.4000"))  # the printed price

    def test_colisage_multiplies_quantity_and_volume(self):
        line = line_named(parse(), "WHISKY EXEMPLE 40D 70CL")
        self.assertEqual(line.colisage, 6)
        self.assertEqual(line.quantity, 6)  # colisage 6 x qty 1
        self.assertEqual(line.total_volume, Decimal("6") * Decimal("0.700"))

    def test_consigne_charge_line_is_kept(self):
        """A crate deposit charge is prefixed with a literal "+ " and has no
        EAN of its own - it still has to be imported so the deposit can be
        matched to a stock item like any other product."""
        line = line_named(parse(), "CAISSE EXEMPLE 24X33CL PLEIN")
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.total_ht, Decimal("11.00"))

    def test_refund_line_keeps_its_negative_sign(self):
        """Metro prints refunds with a TRAILING minus ("1-", "15,00-"), which
        Decimal() won't accept directly."""
        line = line_named(parse(), "PALETTE EUROPE")
        self.assertEqual(line.quantity, -1)
        self.assertEqual(line.total_ht, Decimal("-15.00"))
        self.assertEqual(line.unit_cost_ht, Decimal("15.0000"))

    def test_category_is_applied_to_the_products_above_it(self):
        invoice = parse()
        self.assertEqual(line_named(invoice, "WHISKY EXEMPLE 40D 70CL").category, "Spiritueux")
        self.assertEqual(line_named(invoice, "PALETTE EUROPE").category, "Articles divers")

    def test_same_product_twice_is_merged_into_one_line(self):
        repeated = PdfPage(
            text=HEADER
            + "1933605 WHISKY EXEMPLE 40D 70CL S 40,0 0,280 0,700 12,875 6 1 77,25 D\n"
            + "1933605 WHISKY EXEMPLE 40D 70CL S 40,0 0,280 0,700 12,875 6 2 154,50 D\n"
        )
        invoice = parse([repeated])
        self.assertEqual(len(invoice.lines), 1)
        self.assertEqual(invoice.lines[0].quantity, 18)  # 6x1 + 6x2
        self.assertEqual(invoice.lines[0].total_ht, Decimal("231.75"))

    def test_vat_letter_maps_to_a_rate(self):
        invoice = parse()
        self.assertEqual(line_named(invoice, "WHISKY EXEMPLE 40D 70CL").vat_rate, Decimal("0.2"))
        self.assertEqual(line_named(invoice, "PALETTE EUROPE").vat_rate, Decimal("0"))

    def test_invoice_number_combines_store_and_reference(self):
        self.assertEqual(parse().invoice_number, "134-056-056683")

    def test_invoice_date_comes_from_the_printed_date(self):
        self.assertEqual(parse().invoice_date, date(2026, 8, 28))

    def test_falls_back_to_the_filename_when_the_page_says_nothing(self):
        """Some Metro PDFs print neither a parseable header nor a date - the
        scraper's own filename carries both, so it's real input, not just
        metadata."""
        invoice = parse([PdfPage(text="")], source_name="134_56_37795_20260504145552.pdf")
        self.assertEqual(invoice.invoice_number, "134_56_37795_20260504145552")
        self.assertEqual(invoice.invoice_date, date(2026, 5, 4))

    def test_date_hint_is_used_only_as_a_last_resort(self):
        hinted = MetroParser().parse_pages([PdfPage(text="")], date_hint=date(2026, 2, 2), source_name="x.pdf")
        self.assertEqual(hinted.invoice_date, date(2026, 2, 2))
        # ...but never overrides a date the invoice itself prints.
        printed = MetroParser().parse_pages([PAGE], date_hint=date(2026, 2, 2), source_name="x.pdf")
        self.assertEqual(printed.invoice_date, date(2026, 8, 28))

    def test_empty_document_parses_to_an_empty_invoice(self):
        invoice = parse([PdfPage(text="")])
        self.assertEqual(invoice.lines, [])
        self.assertEqual(invoice.supplier_code, "METRO")


# A second page, structurally faithful and wholly invented, for the quirks
# found on 24/09/2026: the own-brand marker, the "N pour M" promotion, and
# the totals block the parser used to ignore entirely.
#
# It adds up on purpose, so a test can assert that a faithful reading
# reconciles - and so the fixtures that break it below isolate one fault
# each:
#   JUS      35,58 HT @ 5,5%
#   CAFE     48,69 HT less a 16,23 promotion = 32,46 @ 5,5%   -> B base 68,04
#   WHISKY   77,25 HT + 10,42 levy           = 87,67 @ 20%    -> D base 87,67
#   Total HT 155,71   VAT 3,74 + 17,53 = 21,27   Total TTC 176,98
TOTALS_BODY = """\
M 03000000123450 1900123 JUS EXEMPLE VP 1L 2,965 6 2 35,58 B
8000000012345 2300456 CAFE EXEMPLE 1KG CLASSIC 16,230 1 3 48,69 B P
3 POUR 2 16,23-
*** Epicerie Total: 84,27
05010106013120 1933605 WHISKY EXEMPLE 40D 70CL S 40,0 0,280 0,700 12,875 6 1 77,25 D
Plus : COTIS. SECURITE SOCIALE 10,42 D
*** Spiritueux Total: 87,67
Nombre de colis :12 Poids total :3,000 KG Consigne :0 Total H.T. : 155,71
Dont : COTIS. SECURITE SOCIALE 10,42 D
Montant hors T.V.A.: 155,71
⑩ Total Volume effectif: ⑩ Total Volume d'A.P. : Montant H.T. Taux T.V.A. Montant TVA Montant TTC
M 9,000 68,04 B = 5,50% 3,74 71,78
S 4,200 87,67 D = 20,00% 17,53 105,20
155,71 21,27 176,98
⑬ Total à payer 176,98
"""

TOTALS_PAGE = PdfPage(text=HEADER + TOTALS_BODY)

JUS_ROW = "M 03000000123450 1900123 JUS EXEMPLE VP 1L 2,965 6 2 35,58 B\n"


class MetroOwnBrandColumnTests(SimpleTestCase):
    """Metro's leftmost "MM" column prints a literal "M " before the EAN on
    its own-brand rows. LINE_REGEX tolerated only a leading "+ " (consigne)
    and is applied anchored, so those rows matched nothing, fell through
    every branch and vanished with no error and no warning.

    A few rows across the invoices filed went that way, their money and
    their stock with them, and the bank debits paying those invoices could
    not be matched because of it.
    """

    def test_own_brand_row_is_parsed_like_any_other(self):
        line = line_named(parse([TOTALS_PAGE]), "JUS EXEMPLE VP 1L")
        self.assertEqual(line.total_ht, Decimal("35.58"))
        self.assertEqual(line.vat_rate, Decimal("0.055"))
        self.assertEqual(line.quantity, 12)  # colisage 6 x qty 2
        self.assertEqual(line.colisage, 6)

    def test_the_marker_is_not_taken_for_part_of_the_name(self):
        names = [line.raw_name for line in parse([TOTALS_PAGE]).lines]
        self.assertIn("JUS EXEMPLE VP 1L", names)
        self.assertNotIn("M JUS EXEMPLE VP 1L", names)


class MetroPromotionTests(SimpleTestCase):
    """Metro prints a second promotion format besides "Offre Achetez Plus
    Payez Moins": "3 POUR 2  16,23-", the case varying between invoices.
    Unread, the discount is never subtracted and the invoice claims MORE
    than Metro billed, which overstates every cost and margin computed from
    those lines.
    """

    def test_three_for_two_is_subtracted_from_the_total(self):
        line = line_named(parse([TOTALS_PAGE]), "CAFE EXEMPLE 1KG CLASSIC")
        self.assertEqual(line.discount, Decimal("16.23"))
        self.assertEqual(line.total_ht, Decimal("48.69") - Decimal("16.23"))

    def test_the_format_is_read_whatever_its_case(self):
        lower = PdfPage(text=HEADER + TOTALS_BODY.replace("3 POUR 2", "3 pour 2"))
        self.assertEqual(line_named(parse([lower]), "CAFE EXEMPLE 1KG CLASSIC").discount, Decimal("16.23"))

    def test_a_promotion_line_is_never_taken_for_a_product(self):
        names = [line.raw_name for line in parse([TOTALS_PAGE]).lines]
        self.assertEqual([name for name in names if "POUR" in name.upper()], [])


class MetroPrintedTotalsTests(SimpleTestCase):
    """The parser read no total at all: `printed_total_ttc` was NULL on
    nearly every Metro invoice and `vat_breakdown` empty on all of them, so
    nothing compared the lines with the document. That silence is what hid
    both faults above - the figures were sitting in the text the parser
    already held.
    """

    def test_reads_the_total_to_pay(self):
        self.assertEqual(parse([TOTALS_PAGE]).printed_total_ttc, Decimal("176.98"))

    def test_reads_the_vat_table(self):
        self.assertEqual(
            parse([TOTALS_PAGE]).vat_breakdown,
            [
                (Decimal("0.055"), Decimal("68.04"), Decimal("3.74")),
                (Decimal("0.2"), Decimal("87.67"), Decimal("17.53")),
            ],
        )

    def test_the_volume_column_is_not_mistaken_for_the_vat_base(self):
        """Each VAT row may carry a volume in front of its HT base ("M 9,000
        68,04 B = 5,50%"). A base read greedily swallows the volume and
        files 9 000 68,04 EUR of goods at 5,5 %."""
        base = parse([TOTALS_PAGE]).vat_breakdown[0][1]
        self.assertEqual(base, Decimal("68.04"))
        self.assertLess(base, Decimal("1000"))

    def test_a_faithful_reading_raises_no_warning(self):
        self.assertEqual(parse([TOTALS_PAGE]).warnings, [])

    def test_a_dropped_row_is_said_out_loud(self):
        """The regression that matters: if a row ever stops matching again,
        the invoice must say so instead of quietly filing a smaller number."""
        parsed = parse([PdfPage(text=HEADER + TOTALS_BODY.replace(JUS_ROW, ""))])
        self.assertTrue(parsed.warnings)
        said = " ".join(parsed.warnings).replace("\xa0", " ")
        self.assertIn("155,71", said)
        self.assertIn("120,13", said)

    def test_an_unread_promotion_is_said_out_loud_too(self):
        """The same check catches the opposite fault: lines claiming MORE
        than the document's own total."""
        parsed = parse([PdfPage(text=HEADER + TOTALS_BODY.replace("3 POUR 2 16,23-\n", ""))])
        self.assertTrue(parsed.warnings)

    def test_a_credit_note_keeps_its_sign(self):
        """An avoir prints its totals with a TRAILING minus ("65,50-") and
        the parser stores it negative: read unsigned, every credit note
        filed would disagree with its own total by twice its value."""
        avoir = """\
+ 0290123 CAISSE EXEMPLE 24X33CL PLEIN 65,500 1 1- 65,50- A
Nombre de colis :0 Poids total :0,000 KG Consigne :1- Total H.T. : 65,50-
65,50- A = 0,00% 0,00 65,50-
⑬ Total à payer 65,50-
"""
        parsed = parse([PdfPage(text=HEADER + avoir)])
        self.assertEqual(parsed.printed_total_ttc, Decimal("-65.50"))
        self.assertEqual(parsed.warnings, [])

    def test_a_document_with_no_totals_block_says_nothing(self):
        """The older fixtures print no totals at all - that is not a fault."""
        parsed = parse()
        self.assertIsNone(parsed.printed_total_ttc)
        self.assertEqual(parsed.vat_breakdown, [])
        self.assertEqual(parsed.warnings, [])
