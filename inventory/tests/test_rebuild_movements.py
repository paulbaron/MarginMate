"""The bulk rebuilds an import ends with (transfer/rebuild.py), each proven
equal to the per-object service it stands for.

An import never copies a PURCHASE movement or an invoice's status: it
writes the lines and the classifications, then rebuilds what follows from
them. That is only safe if the bulk version gives exactly what the app's
own services give, one line or one product at a time - otherwise a restored
database would hold stock quantities nobody could get by using the app,
and nothing on screen would say so. Every test here runs the per-object
service and the bulk helper on the same rows and compares what they wrote.
"""

from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory import services
from inventory.models import MovementKind, StockMovement, UnitChoices
from inventory.services import (
    create_stock_movement_for_line,
    rebuild_purchase_movements,
    refresh_invoice_statuses,
    refresh_invoice_statuses_for_product,
)
from invoices.models import Invoice, InvoiceLine
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_type,
    make_supplier,
)


def movement_rows(line_ids=None):
    """Every movement tied to an invoice line, as the ledger reads it."""
    movements = StockMovement.objects.filter(invoice_line__isnull=False)
    if line_ids is not None:
        movements = movements.filter(invoice_line_id__in=line_ids)
    return sorted(
        movements.values_list("invoice_line_id", "stock_type_id", "kind", "quantity", "unit_cost_ht", "note", "occurred_on")
    )


class RebuildPurchaseMovementsTests(TestCase):
    """rebuild_purchase_movements against create_stock_movement_for_line."""

    def setUp(self):
        self.supplier = make_supplier()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.beef = make_stock_type(name="Boeuf", unit=UnitChoices.KILOGRAM)
        self.limes = make_stock_type(name="Citrons verts", unit=UnitChoices.UNIT)
        invoice = make_invoice(supplier=self.supplier)
        # A bottle counted in units, converted to litres by its factor.
        self.bottle = make_product(supplier=self.supplier, stock_type=self.vodka, stock_equivalent="0.7")
        # A product bought by the litre with a printed volume: the volume counts.
        self.keg = make_product(supplier=self.supplier, stock_type=self.vodka, unit=UnitChoices.LITRE)
        # A weighed product with no printed weight: the quantity is the kilos.
        self.meat = make_product(supplier=self.supplier, stock_type=self.beef, unit=UnitChoices.KILOGRAM)
        # A net of limes: one unit is 12 limes.
        self.net = make_product(supplier=self.supplier, stock_type=self.limes, stock_equivalent="12")
        self.unclassified = make_product(supplier=self.supplier)
        self.lines = [
            make_invoice_line(invoice, self.bottle, quantity=Decimal("6"), total_ht="57.00"),
            make_invoice_line(invoice, self.keg, quantity=Decimal("1"), total_volume="4.200", total_ht="30.00"),
            make_invoice_line(invoice, self.meat, quantity=Decimal("0.350"), total_ht="7.35", unit_cost_ht="21"),
            make_invoice_line(invoice, self.net, quantity=Decimal("3"), total_ht="9.99"),
            # A refund: a negative count at a negative amount (Metro's "1-").
            make_invoice_line(invoice, self.bottle, quantity=Decimal("-1"), total_ht="-9.50"),
            # A line with no quantity at all: priced at 0, as the service does.
            make_invoice_line(invoice, self.net, quantity=Decimal("0"), total_ht="0.00", unit_cost_ht="0"),
            make_invoice_line(invoice, self.unclassified, quantity=Decimal("2"), total_ht="4.00"),
        ]
        self.line_ids = [line.pk for line in self.lines]

    def per_line_reference(self):
        """What the app's own service books, line by line."""
        StockMovement.objects.filter(invoice_line__isnull=False).delete()
        for line in InvoiceLine.objects.filter(pk__in=self.line_ids).select_related("product__stock_type"):
            create_stock_movement_for_line(line)
        rows = movement_rows()
        StockMovement.objects.filter(invoice_line__isnull=False).delete()
        return rows

    def test_same_rows_as_the_per_line_service(self):
        reference = self.per_line_reference()
        self.assertEqual(len(reference), 6)  # every line but the unclassified one

        deleted, created = rebuild_purchase_movements(line_ids=self.line_ids)

        self.assertEqual((deleted, created), (0, 6))
        self.assertEqual(movement_rows(), reference)

    def test_the_amounts_are_the_services_arithmetic(self):
        """Pinned by hand once, so the reference itself cannot drift."""
        rebuild_purchase_movements(line_ids=self.line_ids)
        by_line = {row[0]: row for row in movement_rows()}
        bottle, keg, meat, net, refund, empty, unclassified = self.lines
        self.assertEqual(by_line[bottle.pk][3:5], (Decimal("4.200"), Decimal("13.5714")))
        self.assertEqual(by_line[keg.pk][3:5], (Decimal("4.200"), Decimal("7.1429")))
        self.assertEqual(by_line[meat.pk][3:5], (Decimal("0.350"), Decimal("21.0000")))
        self.assertEqual(by_line[net.pk][3:5], (Decimal("36.000"), Decimal("0.2775")))
        self.assertEqual(by_line[refund.pk][3:5], (Decimal("-0.700"), Decimal("13.5714")))
        self.assertEqual(by_line[empty.pk][3:5], (Decimal("0.000"), Decimal("0.0000")))
        self.assertNotIn(unclassified.pk, by_line)
        self.assertEqual({row[2] for row in by_line.values()}, {MovementKind.PURCHASE})
        self.assertEqual(by_line[bottle.pk][1], self.vodka.pk)
        self.assertEqual(by_line[meat.pk][1], self.beef.pk)

    def test_by_product_rebuilds_every_line_of_it(self):
        reference = self.per_line_reference()

        deleted, created = rebuild_purchase_movements(
            product_ids=[self.bottle.pk, self.keg.pk, self.meat.pk, self.net.pk, self.unclassified.pk]
        )

        self.assertEqual((deleted, created), (0, 6))
        self.assertEqual(movement_rows(), reference)

    def test_lines_and_products_together_count_each_line_once(self):
        reference = self.per_line_reference()

        deleted, created = rebuild_purchase_movements(line_ids=self.line_ids[:2], product_ids=[self.bottle.pk, self.net.pk])

        bottle, keg, _meat, net, refund, empty, _unclassified = self.lines
        wanted = {bottle.pk, keg.pk, net.pk, refund.pk, empty.pk}
        self.assertEqual((deleted, created), (0, 5))
        self.assertEqual(movement_rows(), [row for row in reference if row[0] in wanted])

    def test_running_it_again_replaces_every_row_with_the_same(self):
        rebuild_purchase_movements(line_ids=self.line_ids)
        first = movement_rows()

        deleted, created = rebuild_purchase_movements(line_ids=self.line_ids)

        self.assertEqual((deleted, created), (6, 6))
        self.assertEqual(movement_rows(), first)

    def test_a_stale_movement_is_recomputed_like_a_conversion_change(self):
        """update_product_conversion drops a product's movements and books
        them again from its lines: a movement computed with an old factor
        must not survive a rebuild."""
        bottle_line = self.lines[0]
        make_movement(stock_type=self.vodka, quantity="6", unit_cost_ht="9.5", invoice_line=bottle_line)

        deleted, created = rebuild_purchase_movements(line_ids=[bottle_line.pk])

        self.assertEqual((deleted, created), (1, 1))
        self.assertEqual(movement_rows([bottle_line.pk]), [
            (bottle_line.pk, self.vodka.pk, MovementKind.PURCHASE, Decimal("4.200"), Decimal("13.5714"), "", None)
        ])

    def test_a_product_unclassified_since_loses_its_movements_like_unlink_product(self):
        rebuild_purchase_movements(line_ids=self.line_ids)
        self.bottle.stock_type = None
        self.bottle.save(update_fields=["stock_type"])

        deleted, created = rebuild_purchase_movements(product_ids=[self.bottle.pk])

        self.assertEqual((deleted, created), (2, 0))
        self.assertFalse(StockMovement.objects.filter(invoice_line__product=self.bottle).exists())
        self.assertEqual(StockMovement.objects.filter(invoice_line__isnull=False).count(), 4)

    def test_a_product_moved_to_another_article_books_there(self):
        rebuild_purchase_movements(line_ids=self.line_ids)
        gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        self.bottle.stock_type = gin
        self.bottle.save(update_fields=["stock_type"])

        rebuild_purchase_movements(product_ids=[self.bottle.pk])

        self.assertEqual(
            set(StockMovement.objects.filter(invoice_line__product=self.bottle).values_list("stock_type_id", flat=True)),
            {gin.pk},
        )

    def test_lines_not_asked_for_are_left_alone(self):
        """Even a stale one: the rebuild does what it is told, and a caller
        that forgot a line must show up in a test, not be papered over."""
        keg_line = self.lines[1]
        stale = make_movement(stock_type=self.vodka, quantity="99", unit_cost_ht="1", invoice_line=keg_line)

        rebuild_purchase_movements(line_ids=[self.lines[0].pk])

        stale.refresh_from_db()
        self.assertEqual(stale.quantity, Decimal("99.000"))

    def test_losses_and_corrections_are_never_touched(self):
        loss = make_movement(stock_type=self.vodka, quantity="-0.5", kind=MovementKind.LOSS, note="Casse")
        correction = make_movement(stock_type=self.vodka, quantity="2", kind=MovementKind.CORRECTION)

        rebuild_purchase_movements(line_ids=self.line_ids, product_ids=[self.bottle.pk])

        self.assertEqual(StockMovement.objects.filter(pk__in=[loss.pk, correction.pk]).count(), 2)

    def test_a_line_holding_another_kind_of_movement_keeps_it(self):
        """create_stock_movement_for_line books nothing on a line that already
        has a movement, whatever its kind - and the ledger allows one per line.
        A loss tied to a line in the admin is someone's data: kept, and no
        purchase is added beside it."""
        bottle_line = self.lines[0]
        loss = make_movement(stock_type=self.vodka, quantity="-1", kind=MovementKind.LOSS, invoice_line=bottle_line)
        reference = self.per_line_reference_keeping(loss)

        deleted, created = rebuild_purchase_movements(line_ids=self.line_ids)

        self.assertEqual((deleted, created), (0, 5))
        self.assertEqual(movement_rows(), reference)
        self.assertTrue(StockMovement.objects.filter(pk=loss.pk).exists())

    def per_line_reference_keeping(self, keep):
        for line in InvoiceLine.objects.filter(pk__in=self.line_ids):
            create_stock_movement_for_line(line)
        rows = movement_rows()
        StockMovement.objects.filter(invoice_line__isnull=False).exclude(pk=keep.pk).delete()
        return rows

    def test_nothing_asked_changes_nothing(self):
        rebuild_purchase_movements(line_ids=self.line_ids)
        before = movement_rows()

        self.assertEqual(rebuild_purchase_movements(), (0, 0))
        self.assertEqual(rebuild_purchase_movements(line_ids=set(), product_ids=set()), (0, 0))
        self.assertEqual(movement_rows(), before)

    def test_ids_that_no_longer_exist_are_ignored(self):
        """A replace deletes lines and products before the rebuild runs."""
        self.assertEqual(rebuild_purchase_movements(line_ids=[999_999], product_ids=[999_999]), (0, 0))

    def test_more_ids_than_one_query_takes(self):
        """SQLite caps the parameters of one query; the helper batches."""
        reference = self.per_line_reference()

        with mock.patch.object(services, "_IN_BATCH", 2):
            deleted, created = rebuild_purchase_movements(
                line_ids=self.line_ids, product_ids=[self.bottle.pk, self.keg.pk, self.meat.pk, self.net.pk]
            )

        self.assertEqual((deleted, created), (0, 6))
        self.assertEqual(movement_rows(), reference)

    def test_queries_do_not_grow_with_the_lines(self):
        """The per-line service costs three queries a line; 3 787 real
        movements are rebuilt on a full import."""
        with CaptureQueriesContext(connection) as few:
            rebuild_purchase_movements(product_ids=[self.bottle.pk])
        invoice = make_invoice(supplier=self.supplier)
        for _ in range(30):
            make_invoice_line(invoice, self.bottle, quantity=Decimal("1"), total_ht="9.50")

        with CaptureQueriesContext(connection) as many:
            deleted, created = rebuild_purchase_movements(product_ids=[self.bottle.pk])

        self.assertEqual((deleted, created), (2, 32))
        self.assertEqual(len(many.captured_queries), len(few.captured_queries))


def statuses():
    return dict(Invoice.objects.values_list("pk", "status"))


class RefreshInvoiceStatusesTests(TestCase):
    """refresh_invoice_statuses against refresh_invoice_statuses_for_product."""

    def setUp(self):
        supplier = make_supplier()
        vodka = make_stock_type(name="Vodka")
        self.classified = make_product(supplier=supplier, stock_type=vodka)
        self.unclassified = make_product(supplier=supplier)
        self.charge = make_product(supplier=supplier, is_expense=True)
        self.elsewhere = make_product(supplier=supplier)
        Status = Invoice.Status

        def invoice_with(status, *products):
            invoice = make_invoice(supplier=supplier, status=status)
            for product in products:
                make_invoice_line(invoice, product)
            return invoice

        # Stale either way: what the rule is for.
        self.stale_complete = invoice_with(Status.COMPLETE, self.classified, self.unclassified)
        self.stale_waiting = invoice_with(Status.NEEDS_REVIEW, self.classified)
        # A charge's poste never waits for a stock item.
        self.with_charge = invoice_with(Status.NEEDS_REVIEW, self.classified, self.charge)
        # Right already.
        self.right_waiting = invoice_with(Status.NEEDS_REVIEW, self.unclassified)
        self.right_complete = invoice_with(Status.COMPLETE, self.classified)
        # Never touched: an error, or an import not finished.
        self.error = invoice_with(Status.ERROR, self.classified)
        self.imported = invoice_with(Status.IMPORTED, self.classified)
        # Stale too, but only through a product most tests do not ask about.
        self.other = invoice_with(Status.COMPLETE, self.elsewhere)
        self.asked = [self.unclassified.pk, self.charge.pk]

    def reference(self, product_ids):
        from inventory.models import Product

        before = statuses()
        for product in Product.objects.filter(pk__in=product_ids):
            refresh_invoice_statuses_for_product(product)
        after = statuses()
        for pk, status in before.items():
            Invoice.objects.filter(pk=pk).update(status=status)
        return before, after

    def test_same_statuses_as_the_per_product_rule(self):
        product_ids = [self.classified.pk, self.unclassified.pk, self.charge.pk]
        before, reference = self.reference(product_ids)

        changed = refresh_invoice_statuses(product_ids)

        self.assertEqual(statuses(), reference)
        self.assertEqual(changed, sum(1 for pk in before if before[pk] != reference[pk]))
        self.assertEqual(changed, 3)
        Status = Invoice.Status
        self.assertEqual(reference[self.stale_complete.pk], Status.NEEDS_REVIEW)
        self.assertEqual(reference[self.stale_waiting.pk], Status.COMPLETE)
        self.assertEqual(reference[self.with_charge.pk], Status.COMPLETE)
        self.assertEqual(reference[self.error.pk], Status.ERROR)
        self.assertEqual(reference[self.imported.pk], Status.IMPORTED)
        self.assertEqual(reference[self.other.pk], Status.COMPLETE)  # not asked about

    def test_only_invoices_holding_the_products_asked(self):
        _before, reference = self.reference(self.asked)

        changed = refresh_invoice_statuses(self.asked)

        self.assertEqual(statuses(), reference)
        # stale_complete holds the unclassified product; with_charge the charge.
        self.assertEqual(changed, 2)
        self.assertEqual(statuses()[self.stale_waiting.pk], Invoice.Status.NEEDS_REVIEW)

    def test_a_product_classified_since_completes_its_invoices(self):
        vodka = self.classified.stock_type
        self.unclassified.stock_type = vodka
        self.unclassified.save(update_fields=["stock_type"])
        _before, reference = self.reference([self.unclassified.pk])

        changed = refresh_invoice_statuses([self.unclassified.pk])

        self.assertEqual(statuses(), reference)
        self.assertEqual(changed, 1)  # right_waiting; stale_complete was already complete
        self.assertEqual(statuses()[self.right_waiting.pk], Invoice.Status.COMPLETE)

    def test_never_touches_an_error(self):
        self.error.lines.update(product=self.unclassified)

        refresh_invoice_statuses([self.unclassified.pk])

        self.error.refresh_from_db()
        self.assertEqual(self.error.status, Invoice.Status.ERROR)

    def test_nothing_asked_changes_nothing(self):
        before = statuses()

        self.assertEqual(refresh_invoice_statuses([]), 0)
        self.assertEqual(refresh_invoice_statuses(set()), 0)
        self.assertEqual(refresh_invoice_statuses([999_999]), 0)
        self.assertEqual(statuses(), before)

    def test_more_ids_than_one_query_takes(self):
        product_ids = [self.classified.pk, self.unclassified.pk, self.charge.pk, self.elsewhere.pk]
        _before, reference = self.reference(product_ids)

        with mock.patch.object(services, "_IN_BATCH", 2):
            changed = refresh_invoice_statuses(product_ids)

        self.assertEqual(statuses(), reference)
        self.assertEqual(changed, 4)  # the three above, and `other` through `elsewhere`
        self.assertEqual(statuses()[self.other.pk], Invoice.Status.NEEDS_REVIEW)
