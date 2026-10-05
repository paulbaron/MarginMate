"""What an electronic sales invoice's page proposes for its lines
(recipes/sale_lines.py): a recipe or an article each line may have sold, and
the consumed quantity with it - drawn, never written until « Enregistrer ».

A wrong tie moves stock in silence, so a proposal is made only where it is
safe: an untied line with a label, a POSITIVE quantity and amount, on a
document that counts. A credit note repeats its invoice's labels: proposed
there, the earlier tie and its consumed ratio would put phantom stock back on
one « Enregistrer » over a price correction.

And the doubt said under a tied line whose money per consumed unit is ten
times its reference, or a tenth of it - a recipe's menu price HT, an
article's unit cost: « Quantité consommée ? », never enforced.

Data invented.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from inventory.models import UnitChoices
from recipes.models import SaleDocument
from recipes.sale_lines import (
    WHY_ARTICLE,
    WHY_EARLIER,
    WHY_RECIPE,
    consumption_doubt,
    proposals,
)
from tests.factories import make_recipe, make_sale_document, make_sale_line, make_stock_type

D = Decimal


def einvoice(**fields) -> SaleDocument:
    """An electronic sales invoice of 03/09/2026, as « Lire la facture » saves it."""
    fields.setdefault("sold_on", date(2026, 9, 3))
    fields.setdefault("einvoice_format", "CII")
    fields.setdefault("einvoice_type_code", "380")
    return make_sale_document(**fields)


def stated(document, label, quantity="2", total_ht="100.00", **fields):
    """A line as an electronic invoice states it: its HT at 20 %."""
    return make_sale_line(document, label=label, quantity=quantity, total_ht=total_ht, vat_rate="0.20", **fields)


class ProposalTests(TestCase):
    def setUp(self):
        self.cocktail = make_recipe(name="Formule cocktail", selling_price_ttc="9.00")
        self.earlier = make_sale_document(sold_on=date(2026, 1, 10), reference="FV-2026-0001")
        self.keg = make_stock_type(name="Fût blonde exemple 30 L", unit=UnitChoices.LITRE)
        self.document = einvoice()

    def test_an_earlier_line_of_the_same_label_wins_over_a_name(self):
        """Case, accents and spaces aside - and over a recipe of that very name."""
        other = make_recipe(name="Cocktail maison exemple", selling_price_ttc="8.00")
        make_sale_line(self.earlier, label="Formule Cocktail", recipe=other, quantity="3")
        line = stated(self.document, "formule  COCKTAIL")

        found = proposals(self.document, [line])

        self.assertEqual(found[line.pk].value, f"recipe:{other.pk}")
        self.assertEqual(found[line.pk].why, WHY_EARLIER.format(day="10/01/2026"))
        self.assertIsNone(found[line.pk].consumed)

    def test_the_latest_earlier_tie_is_the_one(self):
        older = make_sale_document(sold_on=date(2025, 6, 1))
        make_sale_line(older, label="Fût exemple", stock_type=self.keg, quantity="1")
        newer_keg = make_stock_type(name="Fût exemple neuf", unit=UnitChoices.LITRE)
        make_sale_line(self.earlier, label="Fût exemple", stock_type=newer_keg, quantity="1")
        line = stated(self.document, "Fût exemple", quantity="1")

        self.assertEqual(proposals(self.document, [line])[line.pk].value, f"stock:{newer_keg.pk}")

    def test_its_consumed_ratio_is_carried(self):
        """« Fût 30 L » sold 1 and consumed 30 before: sold 2 here, 60."""
        make_sale_line(self.earlier, label="Fût blonde", stock_type=self.keg, quantity="1", consumed_quantity="30")
        line = stated(self.document, "Fût blonde")

        found = proposals(self.document, [line])[line.pk]

        self.assertEqual(found.value, f"stock:{self.keg.pk}")
        self.assertEqual(found.consumed, D("60"))

    def test_no_consumed_ratio_past_the_column(self):
        make_sale_line(self.earlier, label="Fût blonde", stock_type=self.keg, quantity="0.0001", consumed_quantity="99")
        line = stated(self.document, "Fût blonde")

        found = proposals(self.document, [line])[line.pk]

        self.assertEqual(found.value, f"stock:{self.keg.pk}")
        self.assertIsNone(found.consumed)

    def test_a_recipe_turned_preparation_is_not_proposed(self):
        syrup = make_recipe(name="Sirop maison exemple", selling_price_ttc="5.00")
        make_sale_line(self.earlier, label="Sirop maison", recipe=syrup, quantity="1")
        syrup.selling_price_ttc = None
        syrup.save()
        line = stated(self.document, "Sirop maison")

        self.assertEqual(proposals(self.document, [line]), {})

    def test_one_recipe_of_that_name(self):
        line = stated(self.document, "FORMULE COCKTAIL")

        found = proposals(self.document, [line])[line.pk]

        self.assertEqual((found.value, found.why, found.consumed), (f"recipe:{self.cocktail.pk}", WHY_RECIPE, None))

    def test_two_recipes_of_one_name_propose_nothing(self):
        make_recipe(name="Formule  cocktail", selling_price_ttc="12.00")
        line = stated(self.document, "Formule cocktail")

        self.assertEqual(proposals(self.document, [line]), {})

    def test_a_preparation_of_that_name_is_no_recipe_to_propose(self):
        make_recipe(name="Planche apéritive", selling_price_ttc=None)
        line = stated(self.document, "Planche apéritive")

        self.assertEqual(proposals(self.document, [line]), {})

    def test_an_article_of_that_name(self):
        line = stated(self.document, "fût blonde exemple 30 l")

        found = proposals(self.document, [line])[line.pk]

        self.assertEqual((found.value, found.why), (f"stock:{self.keg.pk}", WHY_ARTICLE))

    def test_what_gets_no_proposal(self):
        """A line tied already, a credit note's NEGATIVE line (it repeats its
        invoice's label: the earlier tie and its ratio would give stock
        back), a line rebuilt from a VAT table, the mirror shape, a line with
        no label - and every line of a document that counts in nothing."""
        make_sale_line(self.earlier, label="Formule cocktail", recipe=self.cocktail, quantity="1")
        tied = stated(self.document, "Formule cocktail", recipe=self.cocktail)
        negative = stated(self.document, "Formule cocktail", quantity="-1", total_ht="-50.00")
        rebuilt = stated(self.document, "Formule cocktail", quantity="1", rebuilt=True)
        mirror = stated(self.document, "Formule cocktail", quantity="-1", total_ht="60.00")
        self.assertEqual(proposals(self.document, [tied, negative, rebuilt, mirror]), {})

        for counting in (SaleDocument.Counting.TILL, SaleDocument.Counting.DEPOSIT):
            with self.subTest(counting=counting):
                document = einvoice(counting=counting)
                line = stated(document, "Formule cocktail")
                self.assertEqual(proposals(document, [line]), {})

    def test_three_queries_for_ten_lines(self):
        make_sale_line(self.earlier, label="Fût blonde", stock_type=self.keg, quantity="1", consumed_quantity="30")
        lines = [stated(self.document, f"Ligne exemple {number}") for number in range(8)]
        lines += [stated(self.document, "Fût blonde"), stated(self.document, "Formule cocktail")]

        with self.assertNumQueries(3):
            found = proposals(self.document, lines)

        self.assertEqual(sorted(found), sorted(line.pk for line in lines[8:]))


class ConsumptionDoubtTests(TestCase):
    """Said under the cell, never enforced: a 30 typed in servings on a keg
    tracked by the litre, or a keg's 150 € « consumed » 1."""

    def setUp(self):
        # 9,00 € TTC at 20 %: 7,50 € HT on the menu.
        self.cocktail = make_recipe(name="Formule cocktail", selling_price_ttc="9.00", vat_rate="0.20")
        self.document = einvoice()

    def test_three_hundred_euros_for_one_serving_is_said(self):
        line = stated(self.document, "Formule cocktail", quantity="1", total_ht="300.00", recipe=self.cocktail)

        self.assertEqual(
            consumption_doubt(line), "Quantité consommée ? 300,00 € HT pour 1 portion (carte : 7,50 € HT)."
        )

    def test_thirty_servings_say_nothing(self):
        line = stated(
            self.document,
            "Formule cocktail",
            quantity="1",
            total_ht="300.00",
            recipe=self.cocktail,
            consumed_quantity="30",
        )

        self.assertEqual(consumption_doubt(line), "")

    def test_a_tenth_of_the_reference_is_said_too(self):
        line = stated(self.document, "Formule cocktail", quantity="100", total_ht="70.00", recipe=self.cocktail)

        self.assertIn("0,70 € HT pour 1 portion", consumption_doubt(line))

    def test_an_article_at_fifty_times_its_cost(self):
        keg = make_stock_type(name="Fût exemple", unit=UnitChoices.LITRE)
        line = stated(self.document, "Fût exemple", quantity="1", total_ht="155.00", stock_type=keg)

        self.assertEqual(
            consumption_doubt(line, D("3.10")), "Quantité consommée ? 155,00 € HT pour 1 L (coût : 3,10 € le L)."
        )
        self.assertEqual(consumption_doubt(line, D("0")), "")
        self.assertEqual(consumption_doubt(line, None), "")

    def test_nothing_to_compare_says_nothing(self):
        untied = stated(self.document, "Location de salle", quantity="1", total_ht="300.00")
        consumed_zero = stated(
            self.document,
            "Formule cocktail",
            quantity="1",
            total_ht="300.00",
            recipe=self.cocktail,
            consumed_quantity="0",
        )
        preparation = make_recipe(name="Sirop exemple", selling_price_ttc=None)
        unpriced = stated(self.document, "Sirop", quantity="1", total_ht="300.00", recipe=preparation)
        for line in (untied, consumed_zero, unpriced):
            with self.subTest(line=line.label):
                self.assertEqual(consumption_doubt(line), "")
