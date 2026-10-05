"""What a sale document consumes, for every reader of stock: each line's
consumed quantity - the invoiced one when none is typed - and only for a
document that counts (« Compte dans les marges et le stock »).

A document « Déjà comptée par la caisse » or « Acompte » consumed nothing
here: the till's sales already hold it, or its final invoice will. A line
tied to nothing consumes nothing. And a « Fût 30 L » invoiced once and
followed by the litre consumed 30, not 1. Each reader stays ONE query, so no
page's pinned count moves.

Invented data throughout.
"""

from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.gaps import _last_sale_days, servings_from
from inventory.models import UnitChoices
from inventory.shopping_data import prepare, purchase_rows, read_till
from inventory.tests.test_shopping_data import D1, D2, NOW, REF, TILL_ON, TODAY, article, bought, covered, recipe, sold
from recipes.models import SaleDocument
from recipes.sales import sales_between, stock_type_sales_between
from tests.factories import make_recipe, make_sale_document, make_sale_line, make_stock_type, make_supplier

MARCH_5 = date(2026, 3, 5)
#: A stock-take window around it: (opening count, closing count].
OPENED, CLOSED = date(2026, 3, 1), date(2026, 3, 10)
SET_ASIDE = (SaleDocument.Counting.TILL, SaleDocument.Counting.DEPOSIT)


class ConsumedQuantityTests(TestCase):
    def setUp(self):
        self.keg = make_stock_type(name="Fût exemple 30 L", unit=UnitChoices.LITRE)
        self.cocktail = make_recipe(name="Cocktail exemple")
        self.document = make_sale_document(sold_on=MARCH_5)

    def test_a_recipe_line_consumes_what_it_poured(self):
        """« Forfait 30 cocktails », invoiced as one line of 1."""
        make_sale_line(self.document, recipe=self.cocktail, quantity="1", consumed_quantity="30")

        self.assertEqual(sales_between(OPENED, CLOSED), {self.cocktail.pk: Decimal("30")})

    def test_an_article_line_consumes_its_own_unit(self):
        """A keg invoiced once, followed by the litre: 30 L left the cellar."""
        make_sale_line(
            self.document, stock_type=self.keg, quantity="1", unit_price_ttc="150.00", consumed_quantity="30"
        )

        self.assertEqual(stock_type_sales_between(OPENED, CLOSED), {self.keg.pk: Decimal("30")})

    def test_with_no_consumed_quantity_the_invoiced_one_is_read(self):
        make_sale_line(self.document, recipe=self.cocktail, quantity="3")
        make_sale_line(self.document, stock_type=self.keg, quantity="0.7")

        self.assertEqual(sales_between(OPENED, CLOSED), {self.cocktail.pk: Decimal("3")})
        self.assertEqual(stock_type_sales_between(OPENED, CLOSED), {self.keg.pk: Decimal("0.7")})

    def test_a_consumed_zero_took_nothing(self):
        """0 is typed - nothing left the stock - never read as blank."""
        make_sale_line(self.document, recipe=self.cocktail, quantity="10", consumed_quantity="0")
        make_sale_line(self.document, stock_type=self.keg, quantity="1", consumed_quantity="0")

        self.assertEqual(sales_between(OPENED, CLOSED)[self.cocktail.pk], 0)
        self.assertEqual(stock_type_sales_between(OPENED, CLOSED)[self.keg.pk], 0)

    def test_a_credit_note_tied_gives_stock_back(self):
        make_sale_line(self.document, recipe=self.cocktail, quantity="-2", unit_price_ttc="9.00")

        self.assertEqual(sales_between(OPENED, CLOSED), {self.cocktail.pk: Decimal("-2")})

    def test_the_window_still_bounds_it(self):
        make_sale_line(self.document, recipe=self.cocktail, quantity="1", consumed_quantity="30")

        self.assertEqual(sales_between(MARCH_5, CLOSED), {})
        self.assertEqual(sales_between(OPENED, date(2026, 3, 4)), {})
        self.assertEqual(sales_between(None, MARCH_5), {self.cocktail.pk: Decimal("30")})


class CountingTests(TestCase):
    def setUp(self):
        self.keg = make_stock_type(name="Fût exemple 30 L", unit=UnitChoices.LITRE)
        self.cocktail = make_recipe(name="Cocktail exemple")

    def test_a_document_that_counts_in_nothing_consumed_nothing(self):
        for counting in SET_ASIDE:
            with self.subTest(counting=counting):
                document = make_sale_document(sold_on=MARCH_5, counting=counting)
                make_sale_line(document, recipe=self.cocktail, quantity="10")
                make_sale_line(document, stock_type=self.keg, quantity="1", consumed_quantity="30")

                self.assertEqual(sales_between(OPENED, CLOSED), {})
                self.assertEqual(stock_type_sales_between(OPENED, CLOSED), {})

    def test_a_document_that_counts_beside_them_still_does(self):
        for counting in (*SET_ASIDE, SaleDocument.Counting.COUNTED):
            document = make_sale_document(sold_on=MARCH_5, counting=counting)
            make_sale_line(document, recipe=self.cocktail, quantity="10")

        self.assertEqual(sales_between(OPENED, CLOSED), {self.cocktail.pk: Decimal("10")})

    def test_a_line_tied_to_nothing_consumed_nothing(self):
        document = make_sale_document(sold_on=MARCH_5)
        make_sale_line(document, label="Location de salle", quantity="1", unit_price_ttc="300.00")

        self.assertEqual(sales_between(OPENED, CLOSED), {})
        self.assertEqual(stock_type_sales_between(OPENED, CLOSED), {})


class GapsTests(TestCase):
    """« Combler les écarts »: the last day a recipe sold puts it on the menu
    (`_last_sale_days`), and the servings sold since an entry say whether a
    list is stale (`servings_from`)."""

    def setUp(self):
        self.cocktail = make_recipe(name="Cocktail exemple")

    def test_a_line_nothing_was_poured_for_is_no_sale(self):
        document = make_sale_document(sold_on=MARCH_5)
        make_sale_line(document, recipe=self.cocktail, quantity="1", consumed_quantity="0")

        self.assertNotIn(self.cocktail.pk, _last_sale_days(CLOSED))

    def test_a_forfait_poured_thirty_times_is_a_sale(self):
        document = make_sale_document(sold_on=MARCH_5)
        make_sale_line(document, recipe=self.cocktail, quantity="1", consumed_quantity="30")

        self.assertEqual(_last_sale_days(CLOSED)[self.cocktail.pk], MARCH_5)

    def test_a_refund_is_still_no_sale_and_a_sale_still_is(self):
        make_sale_line(make_sale_document(sold_on=MARCH_5), recipe=self.cocktail, quantity="-1")
        self.assertNotIn(self.cocktail.pk, _last_sale_days(CLOSED))

        make_sale_line(make_sale_document(sold_on=date(2026, 3, 3)), recipe=self.cocktail, quantity="2")
        self.assertEqual(_last_sale_days(CLOSED)[self.cocktail.pk], date(2026, 3, 3))

    def test_a_document_that_counts_in_nothing_is_no_sale_here(self):
        for counting in SET_ASIDE:
            make_sale_line(make_sale_document(sold_on=MARCH_5, counting=counting), recipe=self.cocktail, quantity="4")

        self.assertNotIn(self.cocktail.pk, _last_sale_days(CLOSED))

    def test_the_servings_since_an_entry_are_the_consumed_ones(self):
        make_sale_line(make_sale_document(sold_on=MARCH_5), recipe=self.cocktail, quantity="1", consumed_quantity="30")
        make_sale_line(make_sale_document(sold_on=MARCH_5), recipe=self.cocktail, quantity="2")
        make_sale_line(
            make_sale_document(sold_on=MARCH_5, counting=SaleDocument.Counting.TILL), recipe=self.cocktail, quantity="9"
        )
        make_sale_line(make_sale_document(sold_on=MARCH_5), label="Location de salle", unit_price_ttc="300.00")

        self.assertEqual(servings_from(MARCH_5, CLOSED), Decimal("32"))
        self.assertEqual(servings_from(date(2026, 3, 6), CLOSED), Decimal("0"))


class ShoppingTests(TestCase):
    """« Prévoir les courses » reads the till's year (`read_till`): the
    documents that count, by what they poured."""

    def setUp(self):
        covered(REF)
        shop = make_supplier(name="Grossiste exemple")
        self.syrup = article("Sirop exemple")
        bought(shop, self.syrup, D1, "1", "32.00")
        self.soda = recipe("Soda sirop exemple", "35", (self.syrup, "0.05"))

    def read(self):
        return read_till(purchase_rows(TODAY), NOW)

    def queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            prepare(TODAY, TILL_ON, NOW)
        return len(captured)

    def test_a_document_that_counts_in_nothing_poured_nothing(self):
        sold(self.soda, 2, D2)
        for counting in SET_ASIDE:
            make_sale_line(make_sale_document(sold_on=D1, counting=counting), recipe=self.soda, quantity="4")

        self.assertEqual(list(self.read().data.series[self.syrup.pk]), [(D2, 0.1)])

    def test_alone_it_is_no_sale_and_no_till(self):
        make_sale_line(
            make_sale_document(sold_on=D1, counting=SaleDocument.Counting.TILL), recipe=self.soda, quantity="4"
        )
        make_sale_line(make_sale_document(sold_on=D1), label="Location de salle", unit_price_ttc="300.00")

        self.assertIsNone(self.read())

    def test_a_line_tied_to_nothing_poured_nothing(self):
        sold(self.soda, 2, D2)
        make_sale_line(make_sale_document(sold_on=D1), label="Location de salle", unit_price_ttc="300.00")

        self.assertEqual(list(self.read().data.series[self.syrup.pk]), [(D2, 0.1)])

    def test_the_consumed_quantity_is_what_left_the_shelf(self):
        sold(self.soda, 2, D2)
        make_sale_line(make_sale_document(sold_on=D1), recipe=self.soda, quantity="1", consumed_quantity="4")
        make_sale_line(make_sale_document(sold_on=D1), stock_type=self.syrup, quantity="1", consumed_quantity="0.5")

        self.assertEqual(list(self.read().data.series[self.syrup.pk]), [(D1, 0.7), (D2, 0.1)])

    def test_still_one_query_for_the_documents(self):
        """`TILL_ON_QUERIES` stays: the counting and the consumed quantity
        are read in the very query that read the lines."""
        sold(self.soda, 2, D2)
        make_sale_line(make_sale_document(sold_on=D1), recipe=self.soda, quantity="1")
        few = self.queries()
        make_sale_line(make_sale_document(sold_on=D1), recipe=self.soda, quantity="1", consumed_quantity="4")
        make_sale_line(
            make_sale_document(sold_on=D1, counting=SaleDocument.Counting.DEPOSIT), recipe=self.soda, quantity="4"
        )
        make_sale_line(make_sale_document(sold_on=D1), label="Location de salle", unit_price_ttc="300.00")

        self.assertEqual(self.queries(), few)
