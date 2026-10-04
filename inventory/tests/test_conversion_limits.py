"""A conversion factor is refused at the door when it, or a stock movement
made from it, would not fit its column.

SQLite stores a figure wider than its DecimalField without a word, and
Django's converter then raises on every read: a factor of 1 000 000 put the
product beyond repair from the app, and 0.0001 on a 183 EUR line made a unit
cost of 1 835 000 EUR that took the Stock, « Liste » and Marges pages down for
the whole bar. Data invented.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product, StockMovement, UnitChoices
from inventory.services import create_stock_movement_for_line, update_product_conversion
from tests.factories import make_invoice_line, make_product, make_stock_type

D = Decimal
HTMX = {"HTTP_HX_REQUEST": "true"}

#: What a person, or a hand-made POST, may type that no column holds.
UNSTORABLE = ("1000000", "1e20", "NaN", "sNaN", "Infinity", "-Infinity", "0.00001", "12.123456")


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class ConversionLimitsTests(TestCase):
    def setUp(self):
        self.prosecco = make_stock_type("Prosecco", unit=UnitChoices.UNIT)
        self.classified = make_product(raw_name="PROSECCO 75CL", stock_type=self.prosecco)
        create_stock_movement_for_line(make_invoice_line(product=self.classified, quantity=1, total_ht="183.50"))
        self.pending = make_product(raw_name="PROSECCO ROSE 75CL")
        make_invoice_line(product=self.pending, quantity=1, total_ht="183.50")

    def movements(self):
        return list(StockMovement.objects.order_by("pk").values_list("stock_type_id", "quantity", "unit_cost_ht"))

    def assert_pages_open(self):
        for url in (reverse("inventory:stock_list"), reverse("inventory:stock_catalogue"), "/marges/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def edit(self, factor):
        return self.client.post(
            reverse("inventory:edit_product_conversion", args=[self.classified.pk]), {"stock_equivalent": factor}
        )

    def assign(self, factor, **extra):
        return self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {"stock_type_name": "Prosecco", "stock_equivalent": factor},
            **extra,
        )

    def test_a_factor_no_column_holds_is_refused_on_the_stock_page(self):
        before = self.movements()
        for factor in UNSTORABLE:
            with self.subTest(factor=factor):
                response = self.edit(factor)
                self.assertRedirects(response, reverse("inventory:stock_list"), fetch_redirect_response=False)
                self.assertIn("Facteur invalide", " ".join(messages_of(response)))
                self.classified.refresh_from_db()
                self.assertEqual(self.classified.stock_equivalent, D("1"))
                self.assertEqual(self.movements(), before)
        self.assert_pages_open()

    def test_a_factor_no_column_holds_is_refused_in_the_panel(self):
        for factor in UNSTORABLE:
            with self.subTest(factor=factor):
                response = self.assign(factor, **HTMX)
                self.assertContains(response, "doit être un nombre positif d&#x27;au plus 4 décimales")
                self.pending.refresh_from_db()
                self.assertIsNone(self.pending.stock_type)
                self.assertFalse(StockMovement.objects.filter(invoice_line__product=self.pending).exists())

    def test_a_factor_that_fits_but_makes_an_unstorable_unit_cost_is_refused(self):
        """0.0001 fits the factor's column; a bottle bought 183.50 EUR is then
        1 835 000 EUR a unit, which the movement's unit cost does not hold."""
        before = self.movements()
        response = self.edit("0.0001")
        self.assertRedirects(response, reverse("inventory:stock_list"), fetch_redirect_response=False)
        (message,) = messages_of(response)
        self.assertIn("PROSECCO 75CL", message)
        # The unit cost is what does not fit; the quantity (0.0001) said nothing.
        self.assertIn("1 835 000.00 €", message)
        self.assertNotIn("0.000 unités", message)
        self.classified.refresh_from_db()
        self.assertEqual(self.classified.stock_equivalent, D("1"))
        self.assertEqual(self.movements(), before)
        self.assert_pages_open()

        response = self.assign("0.0001")
        self.assertRedirects(response, reverse("inventory:stock_list"), fetch_redirect_response=False)
        self.assertIn("PROSECCO ROSE 75CL", " ".join(messages_of(response)))
        self.assertIsNone(Product.objects.get(pk=self.pending.pk).stock_type)
        self.assertEqual(self.movements(), before)
        self.assert_pages_open()

    def test_a_factor_that_fits_is_still_taken(self):
        response = self.edit("0,7")
        # As typed, not as the column stores it (0.7000).
        self.assertEqual(messages_of(response), ['"PROSECCO 75CL" mis à jour (facteur 0.7, Prosecco).'])
        self.assertEqual(self.movements(), [(self.prosecco.pk, D("0.7"), D("262.1429"))])
        self.assign("0.75")
        self.pending.refresh_from_db()
        self.assertEqual((self.pending.stock_type, self.pending.stock_equivalent), (self.prosecco, D("0.75")))

    def change_unit(self, unit):
        return self.client.post(
            reverse("inventory:stock_type_update", args=[self.prosecco.pk]),
            {"name": "Prosecco", "unit": unit, "category": "", "loss_percent": "0"},
        )

    def test_an_article_unit_whose_movements_no_column_holds_is_refused(self):
        """Counted by the unit, a line's measured volume says nothing; in
        litres it divides the cost. 1 835 EUR over 0.001 L is 1 835 000 EUR a
        litre, which the movement's unit cost does not hold."""
        create_stock_movement_for_line(
            make_invoice_line(product=self.classified, quantity=1, total_volume="0.001", total_ht="1835")
        )
        before = self.movements()
        response = self.change_unit(UnitChoices.LITRE)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PROSECCO 75CL")
        self.assertContains(response, "1 835 000.00 €")
        self.prosecco.refresh_from_db()
        self.classified.refresh_from_db()
        self.assertEqual((self.prosecco.unit, self.classified.unit), (UnitChoices.UNIT, UnitChoices.UNIT))
        self.assertEqual(self.movements(), before)
        self.assert_pages_open()

    def test_an_article_unit_that_fits_is_still_taken(self):
        response = self.change_unit(UnitChoices.LITRE)
        self.assertRedirects(response, reverse("inventory:stock_list"), fetch_redirect_response=False)
        self.classified.refresh_from_db()
        self.assertEqual(self.classified.unit, UnitChoices.LITRE)
        self.assertEqual(self.movements(), [(self.prosecco.pk, D("1"), D("183.5"))])

    def test_a_conversion_that_fails_halfway_keeps_the_old_movements(self):
        """The movements are deleted before they are made again: a failure in
        between used to leave the product's purchases out of the stock."""
        before = self.movements()
        with mock.patch("inventory.services.create_stock_movement_for_line", side_effect=RuntimeError("disque plein")):
            with self.assertRaises(RuntimeError):
                update_product_conversion(self.classified, unit=UnitChoices.UNIT, stock_equivalent=D("2"))
        self.assertEqual(self.movements(), before)
        self.assertEqual(Product.objects.get(pk=self.classified.pk).stock_equivalent, D("1"))
