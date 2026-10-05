"""What « Marges » counts of a sale document since the sales invoices
(margins/computation.py, `_read_the_documents`): every rule with its figures.

**One money rule**: a document counts at the total it STATES - an electronic
invoice's BT-112, or a total typed on the page - else at the sum of its
lines, to the cent; and in HT at its stated HT when it has one. The part no
line carries is booked too: positive, as revenue with no recipe or article
(« free »); negative - a discount, a package price - spread over its lines,
so the HT drops with it. The tab, the bank and this page never disagree on
one document's money.

**The article rule**: an article line puts both sides in - revenue HT and
purchase cost - only with a VAT rate AND a price of its own; without either,
both stay out (its money TTC, declared). An article ticked « compter dans
la marge produits » is revenue with no cost, uncosted.

**Consumption**: the cost reads the consumed quantity; 0 is uncosted; a
consumption against the money's sign is never costed. **Counting**: a
document « Déjà comptée par la caisse » or « Acompte » counts in nothing.

Invented data throughout: every name, number and amount is made up.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from common import DateRange
from inventory.models import UnitChoices
from margins.computation import Money, margins_for
from margins.tests.test_page import rang_up, stat_of, text_of, till_product, value_of
from recipes.models import SaleDocument
from tests.factories import (
    make_ingredient,
    make_movement,
    make_recipe,
    make_sale_document,
    make_sale_line,
    make_stock_type,
)

MARCH = DateRange(date(2026, 3, 1), date(2026, 3, 31))
DAY = date(2026, 3, 20)
PAGE = "margins:margins_home"


def priced_article(name: str, unit_cost: str, **fields):
    """An article whose purchases give it `unit_cost` HT per unit."""
    article = make_stock_type(name=name, **fields)
    make_movement(stock_type=article, quantity="100", unit_cost_ht=unit_cost)
    return article


def costed_recipe(name: str = "Coupe exemple", price: str = "9.00", vat_rate: str = "0.20", cost: str = "4.00"):
    """A recipe one serving of which costs `cost` HT."""
    made = make_recipe(name=name, selling_price_ttc=price, vat_rate=vat_rate)
    make_ingredient(made, stock_type=priced_article(f"Ingrédient de {name}", unit_cost=cost), quantity="1")
    return made


def einvoice(**fields) -> SaleDocument:
    """An electronic invoice the bar issued, as « Lire la facture » stores
    it: its format, its type, the totals it states."""
    fields = {"einvoice_format": "CII", "einvoice_type_code": "380", "sold_on": DAY, **fields}
    return make_sale_document(**fields)


def stated_line(document, total_ht: str, vat_rate: str, **fields):
    """One line of an electronic invoice: its HT (BT-131) and its rate."""
    return make_sale_line(document, total_ht=total_ht, vat_rate=vat_rate, **fields)


def typed(**fields) -> SaleDocument:
    return make_sale_document(sold_on=DAY, **fields)


class StatedDocumentTests(TestCase):
    """An electronic invoice counts at the totals it states (BT-109, BT-112):
    its lines are its own data, and a check that fails is the invoice's own
    arithmetic, said on its page, never repaired here."""

    def lines(self, document):
        stated_line(document, "1000.00", "0.20", label="Cocktails")
        stated_line(document, "250.00", "0.10", label="Planches")

    def test_it_counts_at_its_stated_totals(self):
        self.lines(einvoice(stated_total_ht="1250.00", stated_total_ttc="1475.00"))

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("1250.00"), Decimal("1475.00")))
        self.assertEqual(report.revenue, Money(Decimal("1250.00"), Decimal("1475.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("0"))
        # Tied to nothing: revenue with no cost behind it, said in the foot.
        self.assertEqual(report.revenue_uncosted, Money(Decimal("1250.00"), Decimal("1475.00")))
        self.assertEqual(report.document_free_ttc, Decimal("1475.00"))
        self.assertEqual(report.document_free_without_rate_ttc, Decimal("0"))

    def test_the_stated_total_wins_over_its_lines(self):
        """1 475,01 € printed where its lines make 1 475,00 €: what it states
        is what was invoiced, here as on the tab and at the bank."""
        document = einvoice(stated_total_ht="1250.00", stated_total_ttc="1475.01")
        self.lines(document)

        self.assertEqual(margins_for(MARCH).revenue_documents.ttc, Decimal("1475.01"))
        self.assertEqual(document.total_ttc, Decimal("1475.01"))

    def test_with_no_bt_112_its_lines_rounded_to_the_cent_half_up(self):
        """100,15 € HT at 10 % is 110,165 €: 110,17 €, never 110,16 €."""
        stated_line(einvoice(stated_total_ht="100.15"), "100.15", "0.10", label="Planches")

        self.assertEqual(margins_for(MARCH).revenue_documents, Money(Decimal("100.15"), Decimal("110.17")))

    def test_with_no_bt_109_its_lines_and_its_adjustment(self):
        document = einvoice(stated_total_ttc="138.00", adjustment_ht="-5.00", adjustment_vat_rate="0.20")
        stated_line(document, "120.00", "0.20", label="Cocktails")

        self.assertEqual(margins_for(MARCH).revenue_documents, Money(Decimal("115.00"), Decimal("138.00")))

    def test_two_of_them_add_up_as_stated(self):
        stated_line(einvoice(stated_total_ht="33.33", stated_total_ttc="40.00"), "33.33", "0.20", label="Un")
        stated_line(einvoice(stated_total_ht="33.33", stated_total_ttc="40.00"), "33.33", "0.20", label="Deux")

        self.assertEqual(margins_for(MARCH).revenue_documents, Money(Decimal("66.66"), Decimal("80.00")))


class TypedHtTests(TestCase):
    """A plain PDF typed with its totals (money critique 3): « Total HT »
    typed beside « Total TTC » makes its amount count in HT. Typed alone, the
    TTC has no HT behind it: declared, never converted at an assumed rate."""

    def test_a_typed_total_with_its_ht_counts_in_ht(self):
        typed(stated_total_ttc="1200.00", stated_total_ht="1000.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("1000.00"), Decimal("1200.00")))
        self.assertEqual(report.real_margin_ht, Decimal("1000.00"))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("0"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("1000.00"), Decimal("1200.00")))
        self.assertEqual(report.document_free_ttc, Decimal("1200.00"))
        self.assertEqual(report.document_free_without_rate_ttc, Decimal("0"))

    def test_a_typed_total_alone_counts_in_ttc_and_is_declared(self):
        typed(stated_total_ttc="1200.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("0"), Decimal("1200.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("1200.00"))
        self.assertEqual(report.document_free_ttc, Decimal("1200.00"))
        self.assertEqual(report.document_free_without_rate_ttc, Decimal("1200.00"))

    def test_its_tied_lines_are_costed_inside_its_ht(self):
        document = typed(stated_total_ttc="600.00", stated_total_ht="500.00")
        make_sale_line(document, recipe=costed_recipe(), quantity="10", unit_price_ttc="9.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("500.00"), Decimal("600.00")))
        self.assertEqual(report.cogs_low, Decimal("40.00"))
        # The coupes are 75,00 € HT and 90,00 € TTC of it, costed.
        self.assertEqual(report.revenue_uncosted, Money(Decimal("425.00"), Decimal("510.00")))
        self.assertEqual(report.document_free_ttc, Decimal("510.00"))


class TypedRemainderTests(TestCase):
    """A typed total beside typed lines: the part no line carries is booked,
    whatever its size - free money when positive, a discount spread over the
    lines when negative (money critique 2), never a negative amount « sans
    taux de TVA »."""

    def setUp(self):
        self.cocktail = costed_recipe(name="Cocktail exemple", cost="2.00")

    def test_a_package_price_is_a_discount_spread_over_its_lines(self):
        """« Forfait 30 cocktails 200 € » on a cocktail priced 9,00 €: 200,00 €
        at 20 % is 166,67 € HT - not the 225,00 € its lines alone would make -
        and nothing negative is left « sans taux de TVA »."""
        document = typed(stated_total_ttc="200.00")
        make_sale_line(document, recipe=self.cocktail, quantity="30", unit_price_ttc="9.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("166.67"), Decimal("200.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("0"))
        self.assertEqual(report.document_free_ttc, Decimal("0"))
        self.assertEqual(report.revenue_uncosted, Money())
        self.assertEqual(report.cogs_low, Decimal("60.00"))
        self.assertEqual(report.products_margin_ht_low, Decimal("106.67"))

    def test_a_discount_is_shared_to_the_cent_each_share_in_its_own_line_s_bucket(self):
        """1,00 € off 10 + 20 + 30 €: 0,17 + 0,33 + 0,50 - the leftover cent
        to the largest remainder (Achats' rule) - each at its line's rate."""
        planche = make_recipe(name="Planche exemple", selling_price_ttc="10.00", vat_rate="0.10")
        plat = make_recipe(name="Plat exemple", selling_price_ttc="10.00", vat_rate="0.055")
        document = typed(stated_total_ttc="59.00")
        make_sale_line(document, recipe=self.cocktail, quantity="1", unit_price_ttc="10.00")
        make_sale_line(document, recipe=planche, quantity="2", unit_price_ttc="10.00")
        make_sale_line(document, recipe=plat, quantity="3", unit_price_ttc="10.00")

        report = margins_for(MARCH)

        # 9,83 / 1,20 + 19,67 / 1,10 + 29,50 / 1,055 = 8,19 + 17,88 + 27,96.
        self.assertEqual(report.revenue_documents, Money(Decimal("54.03"), Decimal("59.00")))
        # The planche and the plat have no cost: 19,67 + 29,50 € of it.
        self.assertEqual(report.revenue_uncosted, Money(Decimal("45.84"), Decimal("49.17")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("0"))

    def test_money_no_line_carries_is_free_money(self):
        document = typed(stated_total_ttc="150.00")
        make_sale_line(document, recipe=self.cocktail, quantity="10", unit_price_ttc="9.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("75.00"), Decimal("150.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("60.00"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("0"), Decimal("60.00")))
        self.assertEqual(report.document_free_ttc, Decimal("60.00"))
        self.assertEqual(report.document_free_without_rate_ttc, Decimal("60.00"))

    def test_within_a_cent_a_line_it_is_booked_too(self):
        """Two lines at 10,00 € under a typed 20,01 €: within the slack, so
        the page says nothing - and the cent is still booked: this page
        counts the document at the total the tab and the bank read."""
        document = typed(stated_total_ttc="20.01")
        lines = [
            make_sale_line(document, recipe=self.cocktail, quantity="1", unit_price_ttc="10.00"),
            make_sale_line(document, recipe=self.cocktail, quantity="1", unit_price_ttc="10.00"),
        ]

        report = margins_for(MARCH)

        self.assertFalse(document.lines_differ_of(lines))
        self.assertEqual(report.revenue_documents.ttc, Decimal("20.01"))
        self.assertEqual(report.revenue_documents.ttc, document.total_ttc)

    def test_with_no_total_typed_the_lines_are_the_money(self):
        document = typed()
        make_sale_line(document, recipe=self.cocktail, quantity="3", unit_price_ttc="3.50")

        report = margins_for(MARCH)

        # 10,50 € at 20 %, its HT worked out per rate over the window.
        self.assertEqual(report.revenue_documents, Money(Decimal("8.75"), Decimal("10.50")))
        self.assertEqual(report.document_free_ttc, Decimal("0"))


class FreeDiscountLineTests(TestCase):
    """« Remise 1 x -20,00 € » typed as a line tied to nothing is the same
    discount as « Total TTC 80 » typed over the lines: spread over the lines
    with a positive TTC, so the HT drops with it - never « free » money below
    zero, never a costed part above all of it."""

    def setUp(self):
        self.mojito = costed_recipe(name="Mojito exemple", price="10.00", vat_rate="0.10", cost="2.00")

    def as_a_total(self):
        document = typed(stated_total_ttc="80.00")
        make_sale_line(document, recipe=self.mojito, quantity="10", unit_price_ttc="10.00")
        return margins_for(MARCH)

    def with_a_discount_line(self, **rate):
        document = typed()
        make_sale_line(document, recipe=self.mojito, quantity="10", unit_price_ttc="10.00")
        make_sale_line(document, label="Remise", quantity="1", unit_price_ttc="-20.00", **rate)
        return margins_for(MARCH)

    def assert_like_the_total(self, report):
        self.assertEqual(report.revenue_documents, Money(Decimal("72.73"), Decimal("80.00")))
        self.assertEqual(report.products_margin_ht_low, Decimal("52.73"))
        self.assertEqual(report.document_free_ttc, Decimal("0"))
        self.assertEqual(report.document_free_without_rate_ttc, Decimal("0"))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("0"))
        self.assertLessEqual(report.revenue_coverage, Decimal("1"))

    def test_the_reference_a_typed_total(self):
        self.assert_like_the_total(self.as_a_total())

    def test_a_discount_line_without_a_rate(self):
        self.assert_like_the_total(self.with_a_discount_line())

    def test_a_discount_line_with_a_rate(self):
        self.assert_like_the_total(self.with_a_discount_line(vat_rate="0.10"))

    def test_with_no_positive_line_it_is_booked_as_it_stands(self):
        """A credit note made of free negative lines alone: nothing to spread
        it over, so it is booked whole rather than lost."""
        document = typed()
        make_sale_line(document, label="Remboursement", quantity="1", unit_price_ttc="-15.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents.ttc, Decimal("-15.00"))
        self.assertEqual(report.document_free_ttc, Decimal("-15.00"))


class TypedToTheCentTests(TestCase):
    """A typed document is booked at its own total to the cent - the one
    the « Ventes » tab and the bank read - never at its lines' unrounded
    sum: 3,50 € x 0,3333 is 1,17 € there, so three of them are 3,51 €."""

    def setUp(self):
        self.cocktail = costed_recipe(name="Cocktail exemple", cost="1.00")

    def test_each_document_counts_at_its_total_to_the_cent(self):
        documents = [typed() for _ in range(3)]
        for document in documents:
            make_sale_line(document, recipe=self.cocktail, quantity="0.3333", unit_price_ttc="3.50")
        stated = typed(stated_total_ttc="20.00")
        make_sale_line(stated, recipe=self.cocktail, quantity="1.5", unit_price_ttc="3.55")
        documents.append(stated)

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents.ttc, sum((d.total_ttc for d in documents), start=Decimal("0")))
        self.assertEqual(report.revenue_documents.ttc, Decimal("23.51"))


class ArticleWithARateTests(TestCase):
    """An article sold as itself is both sides in - its HT, and its
    purchase price times what was consumed - only with a rate AND a price of
    its own. Without either, both sides stay out, as before."""

    def setUp(self):
        self.bottle = priced_article("Bouteille exemple", unit_cost="3.00")

    def test_a_rate_and_a_price_put_both_sides_in(self):
        make_sale_line(typed(), stock_type=self.bottle, quantity="2", unit_price_ttc="12.00", vat_rate="0.20")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("20.00"), Decimal("24.00")))
        self.assertEqual((report.cogs_low, report.cogs_high), (Decimal("6.00"), Decimal("6.00")))
        self.assertEqual(report.revenue_uncosted, Money())
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("0"))
        self.assertEqual(report.products_margin_ht_low, Decimal("14.00"))
        self.assertEqual(report.document_costed_articles, 1)
        # Servings are the recipes': an article's quantity is in its own unit.
        self.assertEqual(report.document_costed_units, Decimal("0"))

    def test_an_einvoice_s_article_line_is_both_sides_in(self):
        document = einvoice(stated_total_ht="20.00", stated_total_ttc="24.00")
        stated_line(document, "20.00", "0.20", stock_type=self.bottle, quantity="2", label="Bouteilles")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("20.00"), Decimal("24.00")))
        self.assertEqual(report.cogs_low, Decimal("6.00"))
        self.assertEqual(report.revenue_uncosted, Money())

    def test_a_rate_without_a_price_keeps_both_sides_out(self):
        """The plain PDF typed with its total, and a keg's line typed only to
        say what left the stock, « TVA % » and no price: its 90,00 € of
        purchase would come off 0,00 € of revenue - a profitable sale printed
        as a loss (CLAUDE.md, « both sides out or neither »)."""
        keg = priced_article("Fût exemple", unit_cost="90.00")
        make_sale_line(typed(stated_total_ttc="150.00"), stock_type=keg, quantity="1", vat_rate="0.20")

        report = margins_for(MARCH)

        self.assertEqual((report.cogs_low, report.cogs_high), (Decimal("0"), Decimal("0")))
        self.assertIsNone(report.products_margin_ht_low)
        self.assertEqual(report.document_costed_articles, 0)
        self.assertEqual(report.revenue_documents, Money(Decimal("0"), Decimal("150.00")))

    def test_no_rate_keeps_both_sides_out(self):
        make_sale_line(typed(), stock_type=self.bottle, quantity="2", unit_price_ttc="12.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("0"), Decimal("24.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("24.00"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("0"), Decimal("24.00")))
        self.assertEqual(report.cogs_low, Decimal("0"))
        # Tied to an article: not « free » money.
        self.assertEqual(report.document_free_ttc, Decimal("0"))

    def test_an_article_never_bought_is_uncosted_revenue_in_ht(self):
        never_bought = make_stock_type(name="Article jamais acheté")
        make_sale_line(typed(), stock_type=never_bought, quantity="2", unit_price_ttc="12.00", vat_rate="0.20")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("20.00"), Decimal("24.00")))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("20.00"), Decimal("24.00")))
        self.assertEqual(report.cogs_low, Decimal("0"))
        self.assertEqual(report.document_costed_articles, 0)

    def test_the_articles_are_counted_not_their_lines(self):
        glass = priced_article("Verre exemple", unit_cost="1.00")
        for document in (typed(), typed()):
            make_sale_line(document, stock_type=self.bottle, quantity="1", unit_price_ttc="12.00", vat_rate="0.20")
        make_sale_line(typed(), stock_type=glass, quantity="6", unit_price_ttc="2.40", vat_rate="0.20")

        self.assertEqual(margins_for(MARCH).document_costed_articles, 2)


class FlaggedArticleTests(TestCase):
    """An article ticked « compter dans la marge produits » is counted by
    what was BOUGHT of it (`extra_products_ht`, by invoice date). Sold on an
    invoice with a rate and a price, its revenue is in, uncosted and with no
    cost: costed at 0 it printed a 100 % margin and inflated « Part
    chiffrée » in any month that sold it without buying it."""

    def test_its_revenue_is_in_uncosted_and_it_adds_no_cost(self):
        towels = priced_article("Essuie-tout exemple", unit_cost="3.00", count_in_products_margin=True)
        document = typed()
        make_sale_line(document, recipe=costed_recipe(), quantity="10", unit_price_ttc="9.00")
        make_sale_line(document, stock_type=towels, quantity="2", unit_price_ttc="12.00", vat_rate="0.20")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("95.00"), Decimal("114.00")))
        self.assertEqual(report.cogs_low, Decimal("40.00"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("20.00"), Decimal("24.00")))
        self.assertEqual(report.document_costed_articles, 0)
        self.assertEqual(report.revenue_coverage, Decimal("75.00") / Decimal("95.00"))


class ConsumptionCostTests(TestCase):
    """The cost reads what was consumed, never the money's quantity."""

    def test_a_keg_invoiced_once_costs_the_litres_it_poured(self):
        keg = priced_article("Fût exemple 30 L", unit_cost="2.00", unit=UnitChoices.LITRE)
        document = einvoice(stated_total_ht="150.00", stated_total_ttc="180.00")
        stated_line(document, "150.00", "0.20", stock_type=keg, quantity="1", consumed_quantity="30", label="Fût 30 L")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("60.00"))
        self.assertEqual(report.revenue_uncosted, Money())
        self.assertEqual(report.document_costed_articles, 1)

    def test_a_forfait_costs_the_servings_it_poured(self):
        """« Forfait 30 coupes », one line of 1 at 240,00 €."""
        make_sale_line(typed(), recipe=costed_recipe(), quantity="1", unit_price_ttc="240.00", consumed_quantity="30")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("200.00"), Decimal("240.00")))
        self.assertEqual(report.cogs_low, Decimal("120.00"))
        self.assertEqual(report.document_costed_units, Decimal("30"))

    def test_consumed_zero_is_revenue_without_a_cost(self):
        """A « 0 » typed on a line - nothing left the stock - must not make
        its revenue « chiffrée » at 0 €."""
        make_sale_line(typed(), recipe=costed_recipe(), quantity="10", unit_price_ttc="9.00", consumed_quantity="0")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("0"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("75.00"), Decimal("90.00")))
        self.assertEqual(report.document_costed_units, Decimal("0"))

    def test_a_consumption_against_the_money_is_never_costed(self):
        keg = priced_article("Fût exemple 30 L", unit_cost="2.00", unit=UnitChoices.LITRE)
        document = einvoice(stated_total_ht="210.00", stated_total_ttc="252.00")
        # Stock back on a sale...
        stated_line(document, "150.00", "0.20", stock_type=keg, quantity="1", consumed_quantity="-30", label="Fût")
        # ... and the mirror shape: a count of -1 at a positive amount.
        stated_line(document, "60.00", "0.20", recipe=costed_recipe(), quantity="-1", label="Coupe")

        report = margins_for(MARCH)

        self.assertEqual((report.cogs_low, report.cogs_high), (Decimal("0"), Decimal("0")))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("210.00"), Decimal("252.00")))


class AdjustmentTests(TestCase):
    """An electronic invoice's document-level allowance (BG-20) is spread
    over its lines with a positive HT, pro rata, to the cent, at ITS OWN
    rate; a charge (BG-21) is revenue no line carries - free, never costed."""

    def setUp(self):
        self.cocktail = costed_recipe(name="Cocktail exemple", price="11.00", vat_rate="0.10", cost="2.00")

    def test_an_allowance_is_spread_at_its_own_rate(self):
        """15,00 € off at 20 % over cocktails at 10 % and a room hire: the
        cocktails' share is -10,00 € HT and -12,00 € TTC - at their own 10 %
        it would be -11,00 €."""
        document = einvoice(
            stated_total_ht="135.00", stated_total_ttc="152.00", adjustment_ht="-15.00", adjustment_vat_rate="0.20"
        )
        stated_line(document, "100.00", "0.10", recipe=self.cocktail, quantity="10", label="Cocktails")
        stated_line(document, "50.00", "0.20", label="Location de salle")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("135.00"), Decimal("152.00")))
        self.assertEqual(report.cogs_low, Decimal("20.00"))
        # Costed: the cocktails after their share, 90,00 € HT and 98,00 € TTC.
        self.assertEqual(report.revenue_uncosted, Money(Decimal("45.00"), Decimal("54.00")))
        self.assertEqual(report.document_free_ttc, Decimal("54.00"))

    def test_an_allowance_is_shared_to_the_cent(self):
        """1,00 € over three lines of 10,00 €: 0,34 + 0,33 + 0,33, the cent
        left to the first of the equal remainders."""
        coupe = costed_recipe(name="Coupe exemple", price="12.00", vat_rate="0.20")
        document = einvoice(
            stated_total_ht="29.00", stated_total_ttc="34.80", adjustment_ht="-1.00", adjustment_vat_rate="0.20"
        )
        stated_line(document, "10.00", "0.20", recipe=coupe, quantity="1", label="Coupe")
        stated_line(document, "10.00", "0.20", label="Service")
        stated_line(document, "10.00", "0.20", label="Vestiaire")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("29.00"), Decimal("34.80")))
        # Costed: 10,00 - 0,34 = 9,66 € HT; 12,00 - 0,408 = 11,59 € TTC.
        self.assertEqual(report.revenue_uncosted, Money(Decimal("19.34"), Decimal("23.21")))

    def test_with_no_rate_of_its_own_an_allowance_takes_the_line_s(self):
        document = einvoice(stated_total_ht="90.00", stated_total_ttc="99.00", adjustment_ht="-10.00")
        stated_line(document, "100.00", "0.10", recipe=self.cocktail, quantity="10", label="Cocktails")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("90.00"), Decimal("99.00")))
        self.assertEqual(report.revenue_uncosted, Money())

    def test_a_charge_is_free_money_never_costed(self):
        document = einvoice(
            stated_total_ht="120.00", stated_total_ttc="134.00", adjustment_ht="20.00", adjustment_vat_rate="0.20"
        )
        stated_line(document, "100.00", "0.10", recipe=self.cocktail, quantity="10", label="Cocktails")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("120.00"), Decimal("134.00")))
        self.assertEqual(report.cogs_low, Decimal("20.00"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("20.00"), Decimal("24.00")))
        self.assertEqual(report.document_free_ttc, Decimal("24.00"))


class ClampTests(TestCase):
    """An electronic invoice whose own check fails (BT-109 is not its lines'
    sum) is booked at what it states; what its lines claim is held inside
    that, on its side of zero - never a coverage above 100 %, never « free »
    money of the wrong sign. The failure is said on the document's page."""

    def setUp(self):
        self.coupe = costed_recipe(price="18.00", cost="2.00")

    def test_a_failing_check_never_prints_a_coverage_above_all(self):
        document = einvoice(stated_total_ht="100.00", stated_total_ttc="120.00")
        stated_line(document, "150.00", "0.20", recipe=self.coupe, quantity="10", label="Coupes")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("100.00"), Decimal("120.00")))
        self.assertEqual(report.revenue_uncosted, Money())
        self.assertEqual(report.revenue_coverage, Decimal("1"))
        self.assertEqual(report.document_free_ttc, Decimal("0"))

    def test_a_credit_note_is_held_on_its_own_side(self):
        document = einvoice(einvoice_type_code="381", stated_total_ht="-100.00", stated_total_ttc="-120.00")
        stated_line(document, "-150.00", "0.20", recipe=self.coupe, quantity="-10", label="Coupes")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("-100.00"), Decimal("-120.00")))
        self.assertEqual(report.revenue_uncosted, Money())
        self.assertEqual(report.document_free_ttc, Decimal("0"))


class CountingTests(TestCase):
    """« Déjà comptée par la caisse » and « Acompte » count in nothing here:
    listed on « Ventes », named in the foot."""

    def setUp(self):
        self.coupe = costed_recipe()

    def test_only_a_document_that_counts_is_in_the_margins(self):
        for counting in SaleDocument.Counting.values:
            make_sale_line(typed(counting=counting), recipe=self.coupe, quantity="10", unit_price_ttc="9.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("75.00"), Decimal("90.00")))
        self.assertEqual(report.cogs_low, Decimal("40.00"))
        self.assertEqual(report.document_costed_units, Decimal("10"))
        self.assertEqual(report.documents_set_aside, 2)

    def test_a_typed_total_set_aside_is_out_too(self):
        typed(counting=SaleDocument.Counting.TILL, stated_total_ttc="500.00", stated_total_ht="416.67")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue, Money())
        self.assertEqual(report.document_free_ttc, Decimal("0"))
        self.assertEqual(report.documents_set_aside, 1)

    def test_set_aside_outside_the_window_is_not_counted(self):
        make_sale_document(sold_on=date(2026, 4, 2), counting=SaleDocument.Counting.DEPOSIT)

        self.assertEqual(margins_for(MARCH).documents_set_aside, 0)


class CreditNoteTests(TestCase):
    """A credit note takes revenue back - and, tied, cost and stock too."""

    def setUp(self):
        self.coupe = costed_recipe(cost="4.00")

    def test_an_electronic_credit_note_takes_revenue_and_cost_back(self):
        """Signed by the reader: -5 coupes at -37,50 € HT."""
        document = einvoice(einvoice_type_code="381", stated_total_ht="-37.50", stated_total_ttc="-45.00")
        stated_line(document, "-37.50", "0.20", recipe=self.coupe, quantity="-5", label="Coupes")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("-37.50"), Decimal("-45.00")))
        self.assertEqual(report.cogs_low, Decimal("-20.00"))
        self.assertEqual(report.document_costed_units, Decimal("-5"))
        self.assertEqual(report.revenue_uncosted, Money())

    def test_a_typed_credit_note_is_a_negative_quantity_at_a_positive_price(self):
        make_sale_line(typed(), recipe=self.coupe, quantity="-2", unit_price_ttc="9.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("-15.00"), Decimal("-18.00")))
        self.assertEqual(report.cogs_low, Decimal("-8.00"))

    def test_a_price_correction_untied_takes_revenue_only(self):
        document = einvoice(einvoice_type_code="381", stated_total_ht="-10.00", stated_total_ttc="-12.00")
        stated_line(document, "-10.00", "0.20", quantity="-1", label="Geste commercial")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("-10.00"), Decimal("-12.00")))
        self.assertEqual(report.cogs_low, Decimal("0"))
        self.assertEqual(report.document_free_ttc, Decimal("-12.00"))

    def test_a_credit_note_typed_as_its_total_alone_counts_it(self):
        """No line to spread it over: still booked at its total, as the tab
        and the bank read it - never dropped."""
        typed(stated_total_ttc="-120.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("0"), Decimal("-120.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("-120.00"))
        self.assertEqual(report.document_free_ttc, Decimal("-120.00"))

    def test_a_typed_credit_note_below_its_lines_counts_its_total(self):
        """Its lines refund 18,00 €, its total 20,00 €: the 2,00 € more has no
        line with money to be spread over, and is booked as it is."""
        document = typed(stated_total_ttc="-20.00")
        make_sale_line(document, recipe=self.coupe, quantity="-2", unit_price_ttc="9.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("-15.00"), Decimal("-20.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("-2.00"))


class DepositTwiceTests(TestCase):
    """A deposit invoice counted, and its final invoice deducting it (BT-113,
    « déjà réglé »): the same sale counted twice, in two months. Said on the
    page, never repaired - the person marks the deposit « Acompte »."""

    def deposit(self, **fields) -> SaleDocument:
        fields = {
            "reference": "A-1",
            "sold_on": date(2026, 2, 10),
            "customer": "Mariage Exemple",
            "stated_total_ttc": "500.00",
            **fields,
        }
        return make_sale_document(**fields)

    def final(self, **fields) -> SaleDocument:
        fields = {
            "reference": "F-7",
            "sold_on": DAY,
            "customer": "MARIAGE EXEMPLE",
            "stated_total_ttc": "2000.00",
            "prepaid_ttc": "500.00",
            **fields,
        }
        return make_sale_document(**fields)

    def test_a_counted_deposit_its_final_invoice_deducts_is_named(self):
        self.deposit()
        self.final()

        self.assertEqual(margins_for(MARCH).deposits_counted_twice, [("n° A-1 du 10/02/2026", "n° F-7 du 20/03/2026")])

    def test_the_customer_is_compared_case_accents_and_spaces_aside(self):
        self.deposit(customer="Événements  Exemple")
        self.final(customer="EVENEMENTS EXEMPLE")

        self.assertEqual(len(margins_for(MARCH).deposits_counted_twice), 1)

    def test_a_deposit_read_from_its_lines(self):
        deposit = self.deposit(stated_total_ttc=None, reference="")
        make_sale_line(deposit, label="Acompte 30 %", quantity="1", unit_price_ttc="500.00")
        self.final()

        self.assertEqual(margins_for(MARCH).deposits_counted_twice, [("du 10/02/2026", "n° F-7 du 20/03/2026")])

    def test_within_a_cent_it_is_the_same_amount(self):
        self.deposit(stated_total_ttc="500.01")
        self.final()

        self.assertEqual(len(margins_for(MARCH).deposits_counted_twice), 1)

    def test_what_is_not_counted_twice(self):
        for deposit, final in (
            ({"counting": SaleDocument.Counting.DEPOSIT}, {}),
            ({"stated_total_ttc": "400.00"}, {}),
            ({"sold_on": date(2026, 3, 25)}, {}),
            ({"customer": "Exemple Événements SARL"}, {}),
            ({"customer": ""}, {"customer": ""}),
            ({}, {"prepaid_ttc": None}),
            ({}, {"counting": SaleDocument.Counting.TILL}),
        ):
            with self.subTest(deposit=deposit, final=final):
                self.deposit(**deposit)
                self.final(**final)
                self.assertEqual(margins_for(MARCH).deposits_counted_twice, [])
                SaleDocument.objects.all().delete()

    def test_two_queries_and_only_then(self):
        deposit = self.deposit(stated_total_ttc=None)
        make_sale_line(deposit, label="Acompte 30 %", quantity="1", unit_price_ttc="500.00")
        final = self.final(prepaid_ttc=None)
        with CaptureQueriesContext(connection) as nothing_prepaid:
            margins_for(MARCH)

        SaleDocument.objects.filter(pk=final.pk).update(prepaid_ttc=Decimal("500.00"))
        with CaptureQueriesContext(connection) as prepaid:
            report = margins_for(MARCH)

        self.assertEqual(len(report.deposits_counted_twice), 1)
        self.assertEqual(len(prepaid), len(nothing_prepaid) + 2)


class PageTests(TestCase):
    """The « Marges » page says what the figures now hold."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=4)

    def html(self) -> str:
        return self.client.get(reverse(PAGE)).content.decode()

    def sold_off_the_till(self, **fields) -> SaleDocument:
        return make_sale_document(sold_on=self.day, **fields)

    def test_the_cost_is_of_what_was_sold_articles_included(self):
        bottle = priced_article("Bouteille exemple", unit_cost="3.00")
        make_sale_line(
            self.sold_off_the_till(), stock_type=bottle, quantity="2", unit_price_ttc="12.00", vat_rate="0.20"
        )

        stat = stat_of(self.html(), "Coût de ce qui a été vendu (HT)")

        self.assertEqual(value_of(stat), "6.00 €")
        self.assertIn("+ 1 article vendu hors caisse", text_of(stat))

    def test_the_articles_note_counts_articles(self):
        bottle = priced_article("Bouteille exemple", unit_cost="3.00")
        glass = priced_article("Verre exemple", unit_cost="1.00")
        document = self.sold_off_the_till()
        for article in (bottle, bottle, glass):
            make_sale_line(document, stock_type=article, quantity="1", unit_price_ttc="12.00", vat_rate="0.20")

        self.assertIn(
            "+ 2 articles vendus hors caisse", text_of(stat_of(self.html(), "Coût de ce qui a été vendu (HT)"))
        )

    def test_the_lead_names_the_articles(self):
        self.assertIn("ce que les recettes et les articles vendus ont consommé", text_of(self.html()))

    def test_the_coverage_is_of_a_known_cost(self):
        coupe = costed_recipe()
        make_sale_line(self.sold_off_the_till(), recipe=coupe, quantity="10", unit_price_ttc="9.00")
        rang_up(till_product("Planche exemple", category="Planches"), self.day, 5, ttc="60.00", ht="50.00")

        text = text_of(self.html())

        self.assertIn("du chiffre d'affaires a un coût connu : la marge produits est surestimée", text)

    def test_with_no_known_cost_it_says_so(self):
        rang_up(till_product("Planche exemple", category="Planches"), self.day, 5, ttc="60.00", ht="50.00")

        self.assertIn("aucun produit vendu sur cette période n'a de coût connu", text_of(self.html()))

    def test_the_foot_names_the_money_no_recipe_or_article_carries(self):
        self.sold_off_the_till(stated_total_ttc="150.00")
        make_sale_line(
            self.sold_off_the_till(), label="Location de salle", quantity="1", unit_price_ttc="60.00", vat_rate="0.20"
        )

        text = text_of(self.html())

        self.assertIn("— 210.00 € de ventes hors caisse sans recette ni article", text)
        self.assertIn(
            "210.00 € TTC de factures de vente ne correspondent à aucune recette ni à aucun article "
            "(dont 150.00 € sans taux de TVA, comptés aussi ci-dessus) : dans l'encaissé, sans coût — "
            "la « Part chiffrée » en tient compte.",
            text,
        )

    def test_with_a_rate_every_time_the_foot_says_no_dont(self):
        make_sale_line(
            self.sold_off_the_till(), label="Location de salle", quantity="1", unit_price_ttc="60.00", vat_rate="0.20"
        )

        text = text_of(self.html())

        self.assertIn("60.00 € TTC de factures de vente ne correspondent à aucune recette ni à aucun article :", text)
        self.assertNotIn("(dont", text)

    def test_the_foot_names_the_documents_set_aside(self):
        self.sold_off_the_till(counting=SaleDocument.Counting.TILL, stated_total_ttc="500.00")
        self.sold_off_the_till(counting=SaleDocument.Counting.DEPOSIT, stated_total_ttc="300.00")

        text = text_of(self.html())

        self.assertIn("— 2 factures de vente hors des marges", text)
        self.assertIn(
            "2 factures de vente ne comptent pas ici : déjà comptées par la caisse, ou acomptes repris par la "
            "facture finale.",
            text,
        )

    def test_one_document_set_aside_reads_in_the_singular(self):
        self.sold_off_the_till(counting=SaleDocument.Counting.TILL, stated_total_ttc="500.00")

        text = text_of(self.html())

        self.assertIn("— 1 facture de vente hors des marges", text)
        self.assertIn(
            "1 facture de vente ne compte pas ici : déjà comptée par la caisse, ou acompte repris par la facture "
            "finale.",
            text,
        )

    def test_the_foot_names_a_deposit_counted_twice(self):
        deposit_day = self.day - timedelta(days=30)
        make_sale_document(sold_on=deposit_day, reference="A-1", customer="Mariage Exemple", stated_total_ttc="500.00")
        self.sold_off_the_till(
            reference="F-7", customer="Mariage Exemple", stated_total_ttc="2000.00", prepaid_ttc="500.00"
        )

        text = text_of(self.html())

        self.assertIn("— 1 acompte compté deux fois", text)
        self.assertIn(
            f"L'acompte n° A-1 du {deposit_day:%d/%m/%Y} est compté ici, et la facture finale n° F-7 du "
            f"{self.day:%d/%m/%Y} le déduit de son total : la même vente compte deux fois. Marquez l'acompte "
            "« Acompte ».",
            text,
        )

    def test_none_of_it_without_a_sale_document(self):
        text = text_of(self.html())

        self.assertNotIn("sans recette ni article", text)
        self.assertNotIn("hors des marges", text)
        self.assertNotIn("compté deux fois", text)


class QueryCountTests(TestCase):
    """The documents and their lines in two queries, whatever they hold; the
    articles' costs in one, and only when an article is sold with a rate and
    a price; the deposits in two at most, only when a document deducts one
    (`DepositTwiceTests`)."""

    def setUp(self):
        self.coupe = costed_recipe()
        self.bottle = priced_article("Bouteille exemple", unit_cost="3.00")
        self.made = 0

    def documents(self, count: int) -> None:
        for _ in range(count):
            self.made += 1
            document = typed(reference=f"Q-{self.made}", stated_total_ttc="200.00")
            make_sale_line(document, recipe=self.coupe, quantity="2", unit_price_ttc="9.00")
            make_sale_line(document, stock_type=self.bottle, quantity="1", unit_price_ttc="12.00", vat_rate="0.20")
            make_sale_line(document, label="Location de salle", unit_price_ttc="50.00", vat_rate="0.20")
            stated = einvoice(reference=f"E-{self.made}", stated_total_ht="30.00", stated_total_ttc="36.00")
            stated_line(stated, "30.00", "0.20", recipe=self.coupe, quantity="4", consumed_quantity="5", label="Coupes")

    def queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            margins_for(MARCH)
        return len(captured)

    def test_ten_documents_cost_what_three_do(self):
        self.documents(3)
        few = self.queries()
        self.documents(7)

        self.assertEqual(self.queries(), few)

    def test_the_articles_costs_are_one_query_and_none_without_such_a_line(self):
        document = typed()
        make_sale_line(document, recipe=self.coupe, quantity="2", unit_price_ttc="9.00")
        make_sale_line(document, stock_type=self.bottle, quantity="1", unit_price_ttc="12.00")
        without = self.queries()

        make_sale_line(document, stock_type=self.bottle, quantity="1", unit_price_ttc="12.00", vat_rate="0.20")

        self.assertEqual(self.queries(), without + 1)
