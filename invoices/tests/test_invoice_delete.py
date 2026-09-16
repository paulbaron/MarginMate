"""Deleting invoices.

The two ways this goes wrong are both silent. Stock movements point at their
invoice line with SET_NULL, so a naive delete leaves the purchases in the
ledger - stock that was never bought, still valued, nothing pointing at why.
And a stock take is priced from specific invoice lines and keeps that trail
(PROTECT), so deleting one of those invoices would either crash or rewrite
what a past count was worth.
"""

from datetime import date
from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product, StockMovement, StockTakeLineSource
from inventory.services import create_stock_movement_for_line
from invoices.deletion import InvoiceInUseError, delete_invoice
from invoices.models import Invoice, InvoiceLine
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)


class DeleteInvoiceTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="SHOP", name="Shop")
        self.stock_type = make_stock_type(name="Citrons")
        self.lemon = make_product(supplier=self.supplier, raw_name="CITRON 500G", stock_type=self.stock_type)
        self.invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 5, 1))
        self.line = make_invoice_line(invoice=self.invoice, product=self.lemon, quantity=2, total_ht="4.00")
        create_stock_movement_for_line(self.line)

    def test_the_invoice_and_its_lines_are_gone(self):
        delete_invoice(self.invoice)
        self.assertFalse(Invoice.objects.filter(pk=self.invoice.pk).exists())
        self.assertFalse(InvoiceLine.objects.filter(pk=self.line.pk).exists())

    def test_its_stock_goes_with_it(self):
        """SET_NULL would keep the purchase in the ledger with no invoice
        behind it - stock that was never bought."""
        self.assertEqual(StockMovement.objects.filter(stock_type=self.stock_type).count(), 1)
        summary = delete_invoice(self.invoice)
        self.assertEqual(summary.movements, 1)
        self.assertFalse(StockMovement.objects.filter(stock_type=self.stock_type).exists())

    def test_an_invoice_that_priced_a_stock_take_is_refused(self):
        take = make_stock_take()
        take_line = make_stock_take_line(stock_take=take, product=self.lemon, counted_quantity="2")
        StockTakeLineSource.objects.create(
            stock_take_line=take_line, invoice_line=self.line, quantity_used=Decimal("2"), unit_cost_ht=Decimal("2")
        )
        with self.assertRaises(InvoiceInUseError) as raised:
            delete_invoice(self.invoice)
        self.assertEqual(raised.exception.stock_takes, [take])
        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())
        self.assertEqual(StockMovement.objects.filter(stock_type=self.stock_type).count(), 1)

    def test_unclassified_products_only_it_created_are_removed(self):
        """A misread receipt's garbled names shouldn't wait in the review
        queue for ever after the receipt itself is gone."""
        garbled = make_product(supplier=self.supplier, raw_name="C1TR0N G4RBLED")
        make_invoice_line(invoice=self.invoice, product=garbled, total_ht="1.00")
        summary = delete_invoice(self.invoice)
        self.assertEqual(summary.products_removed, 1)
        self.assertFalse(Product.objects.filter(pk=garbled.pk).exists())

    def test_classified_products_are_kept(self):
        delete_invoice(self.invoice)
        self.assertTrue(Product.objects.filter(pk=self.lemon.pk).exists())

    def test_a_product_another_invoice_also_bought_is_kept(self):
        shared = make_product(supplier=self.supplier, raw_name="MENTHE")
        make_invoice_line(invoice=self.invoice, product=shared, total_ht="1.00")
        make_invoice_line(invoice=make_invoice(supplier=self.supplier), product=shared, total_ht="1.00")
        delete_invoice(self.invoice)
        self.assertTrue(Product.objects.filter(pk=shared.pk).exists())

    def test_a_product_counted_in_a_stock_take_is_kept(self):
        """Unclassified and on no other invoice, but a stock-take line holds
        it (PROTECT) - the invoice still goes, the product stays."""
        counted = make_product(supplier=self.supplier, raw_name="ANETH")
        make_invoice_line(invoice=self.invoice, product=counted, total_ht="1.00")
        make_stock_take_line(product=counted)
        delete_invoice(self.invoice)
        self.assertFalse(Invoice.objects.filter(pk=self.invoice.pk).exists())
        self.assertTrue(Product.objects.filter(pk=counted.pk).exists())

    def test_its_files_are_removed_once_committed(self):
        self.invoice.source_file.save("ticket.pdf", ContentFile(b"%PDF-1.4 test"), save=False)
        self.invoice.preview_image.save("ticket.jpg", ContentFile(b"jpeg"), save=False)
        self.invoice.save()
        storage = self.invoice.source_file.storage
        names = [self.invoice.source_file.name, self.invoice.preview_image.name]
        with self.captureOnCommitCallbacks(execute=True):
            delete_invoice(self.invoice)
        for name in names:
            self.assertFalse(storage.exists(name), name)


class DeleteInvoiceViewTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="SHOP", name="Shop")
        self.invoice = make_invoice(supplier=self.supplier)
        make_invoice_line(invoice=self.invoice, total_ht="3.00")

    def test_opening_the_page_deletes_nothing(self):
        response = self.client.get(reverse("invoices:invoice_delete", args=[self.invoice.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Supprimer")
        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_confirming_deletes_and_returns_to_the_list(self):
        response = self.client.post(reverse("invoices:invoice_delete", args=[self.invoice.pk]))
        self.assertRedirects(response, reverse("invoices:invoice_list"))
        self.assertFalse(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_it_returns_where_it_was_asked_from(self):
        queue = reverse("invoices:receipt_queue")
        response = self.client.post(reverse("invoices:invoice_delete", args=[self.invoice.pk]), {"next": queue})
        self.assertRedirects(response, queue)

    def test_an_outside_address_is_never_followed(self):
        response = self.client.post(
            reverse("invoices:invoice_delete", args=[self.invoice.pk]), {"next": "https://example.com/"}
        )
        self.assertRedirects(response, reverse("invoices:invoice_list"))

    def test_a_refused_deletion_says_why_and_keeps_the_invoice(self):
        line = self.invoice.lines.get()
        StockTakeLineSource.objects.create(
            stock_take_line=make_stock_take_line(product=line.product),
            invoice_line=line,
            quantity_used=Decimal("1"),
            unit_cost_ht=Decimal("3"),
        )
        response = self.client.post(reverse("invoices:invoice_delete", args=[self.invoice.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "inventaire")
        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())


class BulkDeleteViewTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="SHOP", name="Shop")
        self.invoices = [make_invoice(supplier=self.supplier) for _ in range(3)]
        for invoice in self.invoices:
            make_invoice_line(invoice=invoice, total_ht="2.00")

    def post(self, ids, **extra):
        return self.client.post(
            reverse("invoices:invoice_bulk_delete"), {"invoice_ids": [str(pk) for pk in ids], **extra}
        )

    def test_the_first_post_only_asks_for_confirmation(self):
        response = self.post([invoice.pk for invoice in self.invoices])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Invoice.objects.count(), 3)
        for invoice in self.invoices:
            self.assertContains(response, f'value="{invoice.pk}"')

    def test_confirming_deletes_exactly_the_selection(self):
        chosen = [self.invoices[0].pk, self.invoices[2].pk]
        response = self.post(chosen, confirm="1")
        self.assertRedirects(response, reverse("invoices:invoice_list"))
        self.assertEqual(list(Invoice.objects.values_list("pk", flat=True)), [self.invoices[1].pk])

    def test_one_invoice_in_use_does_not_stop_the_others(self):
        held = self.invoices[0]
        line = held.lines.get()
        StockTakeLineSource.objects.create(
            stock_take_line=make_stock_take_line(product=line.product),
            invoice_line=line,
            quantity_used=Decimal("1"),
            unit_cost_ht=Decimal("2"),
        )
        self.post([invoice.pk for invoice in self.invoices], confirm="1")
        self.assertEqual(list(Invoice.objects.values_list("pk", flat=True)), [held.pk])

    def test_nothing_selected_changes_nothing(self):
        response = self.post([], confirm="1")
        self.assertRedirects(response, reverse("invoices:invoice_list"))
        self.assertEqual(Invoice.objects.count(), 3)

    def test_a_get_never_deletes(self):
        response = self.client.get(reverse("invoices:invoice_bulk_delete"))
        self.assertRedirects(response, reverse("invoices:invoice_list"))
        self.assertEqual(Invoice.objects.count(), 3)
