"""Counting, merging and deleting stock items.

Three things found by looking for bugs away from the day's work:

* A bottle counted in a stock take became 0 litres in the écarts and stock
  pages when its product never had a volume printed on an invoice: an
  unmeasured line stores total_volume 0, not NULL, and "no measured volume"
  was tested with isnull. 62 real count lines were affected.
* Merging a stock item that a recipe, a count or a sale used crashed after
  its products had already moved, leaving the merge half done.
* Deleting such an item - on its own or through "supprimer les types vides" -
  crashed on the same protection.
"""

from datetime import date, datetime
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import StockMovement, StockTakeLine, StockTakeLineSource, StockType, UnitChoices
from inventory.services import create_stock_movement_for_line, product_counting_ratio, product_counting_ratios
from inventory.variance import counted_quantity_in_stock_units
from recipes.models import RecipeIngredient, SaleDocument, SaleDocumentLine
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
)

D = Decimal


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class UnmeasuredProductsTests(TestCase):
    def setUp(self):
        self.juice = make_stock_type("Jus de goyave", unit=UnitChoices.LITRE)

    def product(self, stock_equivalent, *volumes):
        product = make_product(stock_type=self.juice, unit=UnitChoices.LITRE, stock_equivalent=stock_equivalent)
        for volume in volumes:
            make_invoice_line(product=product, quantity=6, total_volume=volume, total_ht="12.00")
        return product

    def test_a_line_without_a_printed_volume_is_not_a_measurement(self):
        product = self.product("1", "0", "0")
        self.assertEqual(product_counting_ratios([product.id]), {})
        self.assertIsNone(product_counting_ratio(product))

    def test_counted_bottles_convert_like_the_purchases_did(self):
        """Eleven 1 L bottles are 11 L - as the purchases were booked
        (quantity x stock_equivalent), not 0."""
        product = self.product("1", "0")
        line = make_stock_take_line(product=product, counted_quantity="11", unit=UnitChoices.UNIT)
        self.assertEqual(counted_quantity_in_stock_units(line), D("11"))
        movement = create_stock_movement_for_line(product.invoice_lines.get())
        self.assertEqual(movement.quantity, D("6"))

    def test_a_70cl_bottle_bought_without_a_volume_uses_its_factor(self):
        product = self.product("0.7", "0")
        line = make_stock_take_line(product=product, counted_quantity="2", unit=UnitChoices.UNIT)
        self.assertEqual(counted_quantity_in_stock_units(line), D("1.4"))

    def test_a_measured_product_keeps_its_size(self):
        product = self.product("1", "4.2", "0")
        self.assertEqual(product_counting_ratios([product.id]), {product.id: {D("0.7")}})
        line = make_stock_take_line(product=product, counted_quantity="2", unit=UnitChoices.UNIT)
        self.assertEqual(counted_quantity_in_stock_units(line), D("1.4"))


class MergeStockTypesTests(TestCase):
    def setUp(self):
        self.source = make_stock_type("Vodka A")
        self.target = make_stock_type("Vodka B")
        self.product = make_product(stock_type=self.source, unit=UnitChoices.LITRE)
        create_stock_movement_for_line(make_invoice_line(product=self.product, total_volume="0.7"))

    def merge(self):
        return self.client.post(
            reverse("inventory:stock_type_merge", args=[self.source.pk]), {"target_id": self.target.pk}
        )

    def test_a_target_that_is_not_an_id_is_not_found(self):
        """A tampered form: a 404, not a server error."""
        for posted in ("abc", "", "1.5"):
            with self.subTest(posted=posted):
                response = self.client.post(
                    reverse("inventory:stock_type_merge", args=[self.source.pk]), {"target_id": posted}
                )
                self.assertEqual(response.status_code, 404)

    def test_everything_that_used_it_moves_over(self):
        recipe = make_recipe(name="Moscow Mule")
        ingredient = make_ingredient(recipe, stock_type=self.source, quantity="0.04")
        counted = make_stock_take_line(stock_type=self.source, unit=UnitChoices.LITRE, counted_quantity="0.5")
        document = SaleDocument.objects.create(sold_on=date(2026, 7, 1))
        sold = SaleDocumentLine.objects.create(document=document, stock_type=self.source, quantity=D("0.7"))

        response = self.merge()

        self.assertRedirects(response, reverse("inventory:stock_list"))
        self.assertFalse(StockType.objects.filter(pk=self.source.pk).exists())
        for obj in (self.product, ingredient, counted, sold):
            obj.refresh_from_db()
            self.assertEqual(obj.stock_type, self.target)
        self.assertEqual(StockMovement.objects.get().stock_type, self.target)

    def test_a_count_of_both_adds_up_into_one_line(self):
        take = make_stock_take()
        mine = make_stock_take_line(
            stock_take=take, stock_type=self.source, unit=UnitChoices.LITRE, counted_quantity="0.5", value_ht="6"
        )
        theirs = make_stock_take_line(
            stock_take=take, stock_type=self.target, unit=UnitChoices.LITRE, counted_quantity="1.2", value_ht="15"
        )
        source_row = StockTakeLineSource.objects.create(
            stock_take_line=mine, invoice_line=self.product.invoice_lines.get(), quantity_used=D("0.5"),
            unit_cost_ht=D("12"),
        )
        self.merge()
        (line,) = StockTakeLine.objects.filter(stock_take=take)
        self.assertEqual((line.pk, line.counted_quantity, line.value_ht), (theirs.pk, D("1.7"), D("21")))
        source_row.refresh_from_db()
        self.assertEqual(source_row.stock_take_line, line)

    def test_a_failure_leaves_nothing_half_merged(self):
        """The products used to have moved already when the delete failed."""
        from unittest import mock

        make_ingredient(make_recipe(name="Mule"), stock_type=self.source, quantity="0.04")
        with mock.patch.object(StockType, "delete", side_effect=RuntimeError("disque plein")):
            with self.assertRaises(RuntimeError):
                self.merge()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_type, self.source)
        self.assertEqual(RecipeIngredient.objects.get().stock_type, self.source)


class DeleteStockTypeTests(TestCase):
    def setUp(self):
        self.syrup = make_stock_type("Sirop de basilic")

    def test_an_item_a_recipe_uses_is_kept_and_the_page_says_where(self):
        make_ingredient(make_recipe(name="Smash"), stock_type=self.syrup, quantity="0.02")
        make_stock_take_line(
            stock_take=make_stock_take(taken_at=datetime(2026, 7, 1, 20, 0)), stock_type=self.syrup,
            unit=UnitChoices.LITRE,
        )
        response = self.client.post(reverse("inventory:stock_type_delete", args=[self.syrup.pk]))
        self.assertRedirects(response, reverse("inventory:stock_list"))
        self.assertTrue(StockType.objects.filter(pk=self.syrup.pk).exists())
        (message,) = messages_of(response)
        self.assertIn("Smash", message)
        self.assertIn("01/07/2026", message)

    def test_an_unused_item_is_still_deleted(self):
        response = self.client.post(reverse("inventory:stock_type_delete", args=[self.syrup.pk]))
        self.assertRedirects(response, reverse("inventory:stock_list"))
        self.assertFalse(StockType.objects.filter(pk=self.syrup.pk).exists())

    def test_clearing_empty_items_skips_the_ones_in_use(self):
        """A syrup written into a recipe before its first purchase is empty
        and in use."""
        make_ingredient(make_recipe(name="Smash"), stock_type=self.syrup, quantity="0.02")
        unused = make_stock_type("Vide")
        response = self.client.post(reverse("inventory:clear_empty_stock_types"))
        self.assertRedirects(response, reverse("inventory:stock_list"))
        self.assertTrue(StockType.objects.filter(pk=self.syrup.pk).exists())
        self.assertFalse(StockType.objects.filter(pk=unused.pk).exists())
        self.assertIn("1 gardé(s)", " ".join(messages_of(response)))

    def test_clearing_empty_items_keeps_recorded_losses(self):
        """No product, but a broken bottle written down: deleting the item
        would delete the loss with it."""
        StockMovement.objects.create(
            stock_type=self.syrup, kind="LOSS", quantity=D("-0.7"), unit_cost_ht=D("10"),
            occurred_on=timezone.localdate(),
        )
        self.client.post(reverse("inventory:clear_empty_stock_types"))
        self.assertTrue(StockType.objects.filter(pk=self.syrup.pk).exists())


class EditConversionTests(TestCase):
    def test_a_product_without_a_stock_item_is_refused_not_a_crash(self):
        product = make_product(supplier=make_invoice().supplier)
        response = self.client.post(
            reverse("inventory:edit_product_conversion", args=[product.pk]), {"stock_equivalent": "0.7"}
        )
        self.assertRedirects(response, reverse("inventory:stock_list"))
        self.assertTrue(any("aucun article" in message for message in messages_of(response)))
