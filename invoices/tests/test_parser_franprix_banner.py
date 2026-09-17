"""Franprix: the "**DUPLICATA**" banner, and the currency-suffixed layout.

Structurally faithful, data invented.

* The banner is printed above the items on every duplicate ticket. Once, the
  recogniser grouped it with the first item's price - whose name it never
  read - and "**DUPLICATA**" became a product. A banner is never a name; the
  amount still is an item, one whose name has to be typed in.
* Some tickets print "Eur" after every amount. The multiplier line then reads
  "T1 2 X 0.49Eur 0.98Eur", which the multiplier pattern refused (at most two
  characters between the unit price and the total): the whole line vanished,
  and 2 baguettes were never bought.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.base import PdfPage
from invoices.parsers.franprix import FranprixParser

# The first loaf's name was never read: its price sits on the banner's line.
BANNER_WITH_A_PRICE = """franprix
FRANPRIX
12 RUE INVENTEE
004211-01
**DUPLICATA**  T1 0.49Eur
PAIN COMPLET  T1 0.49Eur
TOTAL A PAYER  0.98Eur
CB SANS CONTACT  0.98Eur
rTaux-Tr-Tot.HT-r-Tot.TVA-r-Tot.TTC-
5.5%  0.93  0.05  0.98
14-07-2026 MARDI  13:40
LORIAN  R1 004211-01 189
**DUPLICATA**-
"""

# Two loaves on one line, every amount followed by "Eur". 0.98 TTC.
SUFFIXED_MULTIPLIER = """franprix
FRANPRIX
004211-02
-**DUPLICATA**-
PAIN COMPLET  T1 2 X 0.49Eur 0.98Eur
SOUS-TOTAL  0.98Eur
TOTAL A PAYER  0.98Eur
CARTES BLEUES A  0.98Eur
rTaux-TT-Tot.HT-T-Tot.TVA--Tot.TTC
5.5%  0.93  0.05  0.98
15-07-2026 MERCREDI  21:23
LORIAN  R1 004211-02 555
2Article(s)
----**DUPLICATA**---
"""


def parse(text):
    return FranprixParser().parse_pages([PdfPage(text=text)], source_name="ticket.pdf")


class BannerTests(SimpleTestCase):
    def setUp(self):
        self.invoice = parse(BANNER_WITH_A_PRICE)

    def test_the_banner_is_never_a_product(self):
        self.assertFalse([line for line in self.invoice.lines if "DUPLICATA" in line.raw_name.upper()])

    def test_its_amount_is_still_an_item_whose_name_is_to_type(self):
        self.assertEqual(len(self.invoice.lines), 2)
        unread = self.invoice.lines[0]
        self.assertTrue(unread.is_placeholder)
        self.assertEqual((unread.raw_name, unread.printed_ttc), ("Article non lu", Decimal("0.49")))

    def test_the_ticket_still_adds_up(self):
        self.assertEqual([check.label for check in self.invoice.checks if not check.passed], [])


class SuffixedMultiplierTests(SimpleTestCase):
    def setUp(self):
        self.invoice = parse(SUFFIXED_MULTIPLIER)

    def test_the_line_is_read_with_its_count(self):
        (line,) = self.invoice.lines
        self.assertEqual(
            (line.raw_name, line.quantity, line.printed_ttc), ("PAIN COMPLET", 2, Decimal("0.98"))
        )

    def test_the_ticket_adds_up(self):
        self.assertEqual([check.label for check in self.invoice.checks if not check.passed], [])


# Two rates, one row each: the amount paid is their sum, which no single row
# shows. 4 x 2.80 = 11.20 at 5.5% and a 0.20 paper bag at 20%; 11.40 paid.
TWO_RATES = """franprix
FRANPRIX
004211-01
**DUPLICATA**
JAMBON EXEMPLE  T14×2.80.11.20.
SAC PAPIER EXEMPLE  T2 0.20.
ISOUS-TOTALI  11.40-
TOTAL A PAYER  11.40.
CB SANS CONTACT  11.40
-Rate-Taxaole-Vat--Total
5.5%  10.62  0.581  11.201
20%  0.171  0.031  0.201
17-04-2026-FRIDAY  20:15
LORIAN  R1 004211-01 516
"""


class TwoRatesTests(SimpleTestCase):
    """The total used to be taken from the one row that repeated an amount -
    the bag's 0.20 - and the jambon's 11.20 became a "promotion", with the
    sum check passing on 0.20 of lines."""

    def setUp(self):
        self.invoice = parse(TWO_RATES)

    def test_the_total_is_the_sum_of_the_rows(self):
        self.assertEqual(self.invoice.printed_total_ttc, Decimal("11.40"))

    def test_both_lines_at_their_own_rate(self):
        self.assertEqual(
            [(line.raw_name, line.quantity, line.printed_ttc, line.vat_rate) for line in self.invoice.lines],
            [
                ("JAMBON EXEMPLE", 4, Decimal("11.20"), Decimal("0.055")),
                ("SAC PAPIER EXEMPLE", 1, Decimal("0.20"), Decimal("0.20")),
            ],
        )

    def test_the_ticket_adds_up_without_a_promotion(self):
        self.assertEqual([check.label for check in self.invoice.checks if not check.passed], [])
        self.assertNotIn("Remise attribuée", [check.label for check in self.invoice.checks])
