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
