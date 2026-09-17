"""The "Produits" page: the stock and the products still to classify, side by
side.

A product classified from the side panel lands in the list beside it without
leaving the page: the panel comes back with the next product and a note
saying where the last one went (with an undo), and the list reloads itself,
opened on that stock item. Without JavaScript the same forms post and come
back to the page. Data invented.
"""

import json
import re
from datetime import date
from decimal import Decimal

from django.core import signing
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product, StockMovement, StockType, UnitChoices
from inventory.views import REVIEW_PANEL_SIZE, UNDO_SALT
from invoices.models import Invoice
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_type,
    make_supplier,
)

HTMX = {"HTTP_HX_REQUEST": "true"}


def drop_type_in(response):
    return re.search(r'name="drop_type" value="([^"]*)"', response.content.decode()).group(1)


class ProductsPageTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")
        self.rum = make_stock_type(name="Rhum", unit=UnitChoices.LITRE, category="Spiritueux")
        self.pending = make_product(supplier=self.supplier, raw_name="RHUM AMBRE 70CL")
        invoice = make_invoice(supplier=self.supplier)
        make_invoice_line(invoice=invoice, product=self.pending, quantity=6, total_ht="60.00", total_volume="4.2")
        self.url = reverse("inventory:stock_list")

    def test_the_products_to_classify_sit_beside_the_list(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'id="a-classer"')
        self.assertContains(response, "RHUM AMBRE 70CL")
        self.assertContains(response, reverse("inventory:assign_product", args=[self.pending.pk]))
        # The list and the panel on one page: the stock item is listed too.
        self.assertContains(response, "Rhum")
        self.assertContains(response, 'class="container container-wide"')

    def test_nothing_to_classify_means_no_panel(self):
        Product.objects.filter(pk=self.pending.pk).update(stock_type=self.rum)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'id="a-classer"')
        self.assertNotContains(response, "container-wide")

    def test_the_old_queue_address_opens_the_panel(self):
        response = self.client.get(reverse("inventory:review_queue"))
        self.assertRedirects(response, self.url + "#a-classer", fetch_redirect_response=False)

    def test_the_panel_alone_for_the_page_script(self):
        response = self.client.get(reverse("inventory:review_queue"), **HTMX)
        self.assertContains(response, "RHUM AMBRE 70CL")
        self.assertNotContains(response, "<html")

    def test_classifying_without_javascript_comes_back_to_the_page(self):
        response = self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {"stock_type_name": "rhum", "stock_equivalent": "0,7"},
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.pending.refresh_from_db()
        self.assertEqual((self.pending.stock_type, self.pending.stock_equivalent), (self.rum, Decimal("0.7")))

    def test_classifying_from_the_panel_says_where_it_went_and_reloads_the_list(self):
        other = make_product(supplier=self.supplier, raw_name="GIN SEC 1L")
        response = self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {"stock_type_name": "Rhum", "stock_equivalent": "0.7"},
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<html")
        self.assertContains(response, "« RHUM AMBRE 70CL » classé dans")
        self.assertContains(response, f'data-reveal-stock-type="{self.rum.pk}"')
        self.assertContains(response, "1 produit = 0.7 litre.")
        # The next product is up, the classified one gone from the queue.
        self.assertContains(response, "GIN SEC 1L")
        self.assertNotContains(response, reverse("inventory:assign_product", args=[self.pending.pk]))
        self.assertEqual(json.loads(response["HX-Trigger"]), {"catalogue-changed": {"stock_type": self.rum.pk}})
        # The navigation's count follows.
        self.assertContains(response, 'id="nav-count-produits" hx-swap-oob="true"')
        self.assertEqual(StockMovement.objects.filter(stock_type=self.rum).count(), 1)
        self.assertIsNotNone(other)

    def test_a_new_name_makes_a_new_stock_item_and_says_so(self):
        response = self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {
                "stock_type_name": "Rhum ambré",
                "new_stock_type_unit": UnitChoices.LITRE,
                "new_stock_type_category": "Spiritueux",
                "stock_equivalent": "0.7",
            },
            **HTMX,
        )
        created = StockType.objects.get(name="Rhum ambré")
        self.assertEqual((created.unit, created.category), (UnitChoices.LITRE, "Spiritueux"))
        self.assertContains(response, "nouvel article")
        # The undo takes it away again - and only it: the value is signed.
        self.assertEqual(signing.loads(drop_type_in(response), salt=UNDO_SALT), created.pk)

    def test_a_wrong_factor_is_said_in_the_panel(self):
        response = self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {"stock_type_name": "Rhum", "stock_equivalent": "-1"},
            **HTMX,
        )
        self.assertContains(response, "L&#x27;équivalence en stock doit être un nombre positif.")
        self.assertNotIn("HX-Trigger", response)
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.stock_type)
        # Said once, in the panel - not again on the next full page.
        self.assertNotContains(self.client.get(self.url), "doit être un nombre positif")

    def test_undo_puts_it_back_and_drops_the_item_it_created(self):
        classified = self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {"stock_type_name": "Rhum ambré", "stock_equivalent": "0.7"},
            **HTMX,
        )
        created = StockType.objects.get(name="Rhum ambré")
        response = self.client.post(
            reverse("inventory:remove_product", args=[self.pending.pk]),
            {"drop_type": drop_type_in(classified)},
            **HTMX,
        )
        self.assertContains(response, reverse("inventory:assign_product", args=[self.pending.pk]))
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.stock_type)
        self.assertFalse(StockType.objects.filter(pk=created.pk).exists())
        self.assertEqual(json.loads(response["HX-Trigger"]), {"catalogue-changed": {"stock_type": None}})

    def test_undo_keeps_an_item_in_use_or_not_its_own(self):
        kept = make_stock_type(name="Sirop")
        make_ingredient(make_recipe(name="Mojito"), stock_type=kept, quantity="0.02")
        classified = make_product(supplier=self.supplier, raw_name="SIROP 1L", stock_type=make_stock_type(name="X"))
        in_use = signing.dumps(kept.pk, salt=UNDO_SALT)
        for drop in (in_use, self.rum.pk, "abc", ""):
            with self.subTest(drop=drop):
                self.client.post(reverse("inventory:remove_product", args=[classified.pk]), {"drop_type": drop}, **HTMX)
        self.assertTrue(StockType.objects.filter(pk=kept.pk).exists())
        self.assertTrue(StockType.objects.filter(pk=self.rum.pk).exists())

    def test_the_list_alone_reloads_with_its_figures(self):
        self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {"stock_type_name": "Rhum", "stock_equivalent": "0.7"},
        )
        response = self.client.get(reverse("inventory:stock_catalogue"), **HTMX)
        self.assertNotContains(response, "<html")
        self.assertContains(response, f'data-stock-type-id="{self.rum.pk}"')
        self.assertContains(response, 'id="stock-stats" hx-swap-oob="true"')
        self.assertContains(response, "60.00 €")

    def test_the_list_keeps_the_period_it_was_showing(self):
        take = make_stock_take()
        response = self.client.get(self.url, {"inventaire": take.pk})
        self.assertContains(response, f'{reverse("inventory:stock_catalogue")}?inventaire={take.pk}')

    def test_a_long_queue_shows_the_first_ones_and_says_how_many_more(self):
        for number in range(REVIEW_PANEL_SIZE + 2):
            make_product(supplier=self.supplier, raw_name=f"PRODUIT {number:03d}")
        response = self.client.get(self.url)
        self.assertContains(response, "3 autres produits")
        self.assertEqual(response.context["review_products"].__len__(), REVIEW_PANEL_SIZE)

    def test_a_rows_purchases_are_dated_the_french_way(self):
        Invoice.objects.update(invoice_date=date(2026, 3, 9))
        self.client.post(
            reverse("inventory:assign_product", args=[self.pending.pk]),
            {"stock_type_name": "Rhum", "stock_equivalent": "0.7"},
        )
        response = self.client.get(reverse("inventory:stock_type_movements", args=[self.rum.pk]))
        self.assertContains(response, "09/03/2026")
        # The 4.2 litres bought, as written: not "4.200".
        self.assertContains(response, "4.2 Litre")

    def test_approving_every_suggestion_comes_back_to_the_page(self):
        self.client.get(self.url)  # the suggestions are made as the page is drawn
        response = self.client.post(reverse("inventory:approve_all_suggestions"))
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertFalse(Product.objects.filter(stock_type__isnull=True).exists())
