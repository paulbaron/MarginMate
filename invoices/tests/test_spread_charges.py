"""Frais à répartir: a delivery charge shared over the goods it delivered.

A supplier prints « LIVRAISON » once, for the whole order. It is not a
product, and what it costs belongs on the bottles it brought - that is the
only figure a margin can be taken against. Ticking the box on the correction
page leaves the line exactly as the document prints it (so every total, every
check and the bank match are the document's own figures) and moves what it
costs onto the other lines, pro rata of what they were priced at.

The rule that must never break: the shares add back up to the charge, to the
centime, so the lines' costs still come to the invoice's own total. Nothing
is created; it only moves.

Structurally faithful, data invented.
"""

from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from inventory.models import StockMovement, UnitChoices
from inventory.services import compute_movement_amounts, create_stock_movement_for_line
from invoices.forms import NOTHING_TO_SPREAD, LineCorrectionFormSet
from invoices.importing import spread_charges
from invoices.models import Invoice, InvoiceLine, Supplier
from invoices.parsers.base import ParsedLine
from invoices.tests.page_posts import page_post
from tests.factories import make_invoice_line, make_product, make_stock_type

D = Decimal


def parsed(total_ht: str, *, charge: bool = False, name: str = "Article") -> ParsedLine:
    return ParsedLine(
        raw_name=name,
        quantity=1,
        total_volume=D("0"),
        unit_cost_ht=D(total_ht),
        total_ht=D(total_ht),
        vat_rate=D("0.20"),
        is_spread_charge=charge,
    )


class SpreadChargesTests(SimpleTestCase):
    """The arithmetic, on its own - no database, like the FIFO valuation's."""

    def test_the_shares_are_pro_rata_of_what_each_line_was_priced_at(self):
        lines = [parsed("75.00"), parsed("25.00"), parsed("10.00", charge=True)]
        self.assertEqual(spread_charges(lines), D("10.00"))
        self.assertEqual([line.spread_ht for line in lines], [D("7.50"), D("2.50"), D("0")])

    def test_the_shares_add_up_to_the_charge_to_the_centime(self):
        # Three equal lines sharing 1,00 EUR: 0,34 + 0,33 + 0,33, never
        # 0,33 x 3 with a centime lost out of every cost on the invoice.
        lines = [parsed("10.00"), parsed("10.00"), parsed("10.00"), parsed("1.00", charge=True)]
        spread_charges(lines)
        shares = [line.spread_ht for line in lines[:3]]
        self.assertEqual(sum(shares), D("1.00"))
        self.assertEqual(sorted(shares, reverse=True), [D("0.34"), D("0.33"), D("0.33")])

    def test_the_leftover_centimes_go_to_the_largest_remainders(self):
        # Exact shares 0,8181... / 0,0909... / 0,0909...: the big line's
        # remainder is the largest, so it takes the centime.
        lines = [parsed("90.00"), parsed("10.00"), parsed("10.00"), parsed("1.00", charge=True)]
        spread_charges(lines)
        self.assertEqual([line.spread_ht for line in lines[:3]], [D("0.82"), D("0.09"), D("0.09")])

    def test_several_charge_lines_are_shared_together(self):
        lines = [parsed("100.00"), parsed("6.00", charge=True), parsed("4.00", charge=True)]
        self.assertEqual(spread_charges(lines), D("10.00"))
        self.assertEqual(lines[0].spread_ht, D("10.00"))

    def test_a_returned_deposit_carries_no_share(self):
        # Signed, a negative line would take a negative share of the
        # delivery and the beer beside it more than the whole of it.
        lines = [parsed("100.00"), parsed("-20.00", name="Consigne rendue"), parsed("10.00", charge=True)]
        spread_charges(lines)
        self.assertEqual([line.spread_ht for line in lines], [D("10.00"), D("0"), D("0")])

    def test_a_credit_on_the_delivery_is_shared_the_same_way(self):
        lines = [parsed("60.00"), parsed("40.00"), parsed("-1.00", charge=True)]
        self.assertEqual(spread_charges(lines), D("-1.00"))
        self.assertEqual([line.spread_ht for line in lines[:2]], [D("-0.60"), D("-0.40")])
        self.assertEqual(sum(line.spread_ht for line in lines), D("-1.00"))

    def test_nothing_positive_to_carry_it_shares_nothing(self):
        lines = [parsed("-20.00", name="Consigne rendue"), parsed("10.00", charge=True)]
        self.assertEqual(spread_charges(lines), D("0"))
        self.assertEqual([line.spread_ht for line in lines], [D("0"), D("0")])

    def test_a_charge_of_zero_shares_nothing(self):
        lines = [parsed("100.00"), parsed("0.00", charge=True)]
        self.assertEqual(spread_charges(lines), D("0"))
        self.assertEqual(lines[0].spread_ht, D("0"))

    def test_no_charge_at_all_leaves_every_share_at_zero(self):
        lines = [parsed("100.00"), parsed("20.00")]
        self.assertEqual(spread_charges(lines), D("0"))
        self.assertEqual([line.spread_ht for line in lines], [D("0"), D("0")])

    def test_no_line_at_all(self):
        self.assertEqual(spread_charges([]), D("0"))

    def test_a_share_already_on_a_line_is_worked_out_again(self):
        # A charge that changed, or a box unticked: the old share is a share
        # of something that no longer exists.
        lines = [parsed("100.00"), parsed("10.00", charge=True)]
        lines[0].spread_ht = D("99.00")
        spread_charges(lines)
        self.assertEqual(lines[0].spread_ht, D("10.00"))
        lines[1].is_spread_charge = False
        spread_charges(lines)
        self.assertEqual(lines[0].spread_ht, D("0"))


class CostHtTests(SimpleTestCase):
    def test_a_line_costs_what_it_was_billed_plus_its_share(self):
        line = InvoiceLine(total_ht=D("75.00"), spread_ht=D("7.50"))
        self.assertEqual(line.cost_ht, D("82.50"))

    def test_the_charge_line_itself_costs_nothing(self):
        # Its money is already on the goods; counted here it is paid twice.
        line = InvoiceLine(total_ht=D("10.00"), is_spread_charge=True)
        self.assertEqual(line.cost_ht, D("0"))

    def test_a_line_with_no_share_costs_what_it_was_billed(self):
        self.assertEqual(InvoiceLine(total_ht=D("12.34")).cost_ht, D("12.34"))


class SpreadChargeFormsetTests(SimpleTestCase):
    """The box on the correction page - and the blank-row trap a field with
    an `initial` sets, which this codebase has shipped three times."""

    @staticmethod
    def payload(rows, total_forms=None):
        """Keyed by index on purpose, so a test can post the non-contiguous
        indices ("0, 1, 3") that removing a row in the browser really sends."""
        data = {
            "form-TOTAL_FORMS": str(total_forms if total_forms is not None else len(rows)),
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for index, fields in rows.items():
            for name, value in fields.items():
                data[f"form-{index}-{name}"] = value
        return data

    @staticmethod
    def goods(name="Gin 70cl", total_ht="100.00"):
        return {"product_name": name, "quantity": "1", "total_ht": total_ht, "vat_rate": "20"}

    @staticmethod
    def delivery(total_ht="10.00"):
        return {"product_name": "Livraison", "total_ht": total_ht, "vat_rate": "20", "is_spread_charge": "on"}

    def formset(self, data):
        return LineCorrectionFormSet(data, form_kwargs={"document": "invoice"})

    def test_a_delivery_row_needs_no_quantity(self):
        formset = self.formset(self.payload({0: self.goods(), 1: self.delivery()}))
        self.assertTrue(formset.is_valid(), formset.errors)
        self.assertEqual(formset.forms[1].cleaned_data["quantity"], D("1"))

    def test_an_ordinary_row_still_needs_one(self):
        row = self.goods()
        del row["quantity"]
        formset = self.formset(self.payload({0: row}))
        self.assertFalse(formset.is_valid())
        self.assertIn("quantity", formset.forms[0].errors)

    def test_removing_a_row_leaves_a_gap_that_must_not_block_the_save(self):
        # Index 1 is absent from the POST entirely, as the browser sends it.
        formset = self.formset(self.payload({0: self.goods(), 2: self.delivery()}, total_forms=3))
        self.assertTrue(formset.is_valid(), formset.errors)

    def test_a_trailing_row_left_at_its_default_is_ignored(self):
        # Every field the page draws, with the box untouched.
        blank = {"product_name": "", "quantity": "", "total_ht": "", "total_ttc": "", "vat_rate": "20"}
        formset = self.formset(self.payload({0: self.goods(), 1: blank}))
        self.assertTrue(formset.is_valid(), formset.errors)
        self.assertEqual(len([form for form in formset if form.cleaned_data]), 1)

    def test_a_row_where_only_the_box_was_ticked_says_what_is_missing(self):
        # It is real user input, so the row must not vanish in silence.
        formset = self.formset(self.payload({0: self.goods(), 1: {"is_spread_charge": "on"}}))
        self.assertFalse(formset.is_valid())
        self.assertIn("product_name", formset.forms[1].errors)

    def test_a_delivery_with_no_goods_under_it_is_refused(self):
        formset = self.formset(self.payload({0: self.delivery()}))
        self.assertFalse(formset.is_valid())
        self.assertEqual(formset.non_form_errors(), [NOTHING_TO_SPREAD])

    def test_a_delivery_whose_only_goods_were_given_back_is_refused(self):
        given_back = {"product_name": "Consigne", "quantity": "-1", "total_ht": "-20.00", "vat_rate": "20"}
        formset = self.formset(self.payload({0: given_back, 1: self.delivery()}))
        self.assertFalse(formset.is_valid())
        self.assertEqual(formset.non_form_errors(), [NOTHING_TO_SPREAD])


class SpreadChargeOnTheCorrectionPageTests(TestCase):
    """End to end: the box ticked on a real invoice."""

    def setUp(self):
        self.supplier = Supplier.objects.get(code="METRO")
        self.gin = make_stock_type("Gin", unit=UnitChoices.LITRE)
        self.product = make_product(
            supplier=self.supplier, raw_name="GIN EXEMPLE 70CL", stock_type=self.gin, unit=UnitChoices.LITRE,
            stock_equivalent="0.7",
        )
        self.invoice = Invoice.objects.create(
            supplier=self.supplier, invoice_number="134-052-000001", printed_total_ttc=D("132.00")
        )
        self.goods = make_invoice_line(
            invoice=self.invoice, product=self.product, quantity=10, total_ht="100.00", vat_rate=D("0.20")
        )
        create_stock_movement_for_line(self.goods)
        self.delivery = make_invoice_line(
            invoice=self.invoice, raw_name="LIVRAISON", quantity=1, total_ht="10.00", vat_rate=D("0.20"),
            product=make_product(supplier=self.supplier, raw_name="LIVRAISON"),
        )
        self.url = reverse("invoices:invoice_edit_lines", args=[self.invoice.pk])

    def post(self, **changes):
        page = self.client.get(self.url)
        return self.client.post(self.url, page_post(page, invoice_date="2026-06-30", **changes))

    def tick(self, **changes):
        return self.post(**{"form-1-is_spread_charge": "on", **changes})

    def test_the_share_lands_on_the_goods_and_the_charge_keeps_its_amount(self):
        self.tick()
        goods, delivery = self.invoice.lines.all()
        self.assertEqual((goods.total_ht, goods.spread_ht, goods.cost_ht), (D("100.00"), D("10.00"), D("110.00")))
        self.assertEqual((delivery.total_ht, delivery.spread_ht), (D("10.00"), D("0")))
        self.assertTrue(delivery.is_spread_charge)

    def test_the_invoice_totals_do_not_move(self):
        # The whole reason the charge stays a line: its 20 % VAT must not be
        # taxed at the goods' rate, and the bank matches on total_ttc to the
        # centime.
        before = (self.invoice.total_ht, self.invoice.total_ttc)
        self.tick()
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual((invoice.total_ht, invoice.total_ttc), before)
        self.assertEqual(invoice.total_ttc, D("132.00"))

    def test_the_lines_costs_still_add_up_to_the_invoice(self):
        self.tick()
        lines = list(self.invoice.lines.all())
        self.assertEqual(sum(line.cost_ht for line in lines), self.invoice.lines_total_ht)

    def test_the_stock_movement_is_priced_delivery_included(self):
        self.tick()
        goods = self.invoice.lines.first()
        movement = StockMovement.objects.get(invoice_line=goods)
        # 10 bottles of 0,7 L = 7 L, 110,00 EUR delivered - not the 14,2857
        # a litre the invoice's own line alone would have priced.
        self.assertEqual(movement.quantity, D("7"))
        self.assertEqual(movement.unit_cost_ht, D("15.7143"))
        self.assertEqual(compute_movement_amounts(goods)[1], D("110.00") / D("7"))

    def test_the_delivery_books_no_stock_and_waits_in_no_queue(self):
        self.tick()
        delivery = self.invoice.lines.last()
        self.assertFalse(StockMovement.objects.filter(invoice_line=delivery).exists())
        self.assertTrue(delivery.product.is_expense)
        self.assertFalse(delivery.product.needs_review)
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(invoice.needs_review_count, 0)
        self.assertEqual(invoice.status, Invoice.Status.COMPLETE)

    def test_a_delivery_line_never_books_stock_even_once_classified(self):
        # Hiding the row in the review panel is not a guard: a product is
        # classified from three screens, and the ledger must not depend on
        # nobody having clicked.
        self.tick()
        delivery = self.invoice.lines.last()
        delivery.product.stock_type = self.gin
        delivery.product.save(update_fields=["stock_type"])
        self.assertIsNone(create_stock_movement_for_line(delivery))

    def test_unticking_the_box_gives_the_product_back(self):
        self.tick()
        self.post(**{"form-1-is_spread_charge": ""})
        goods, delivery = self.invoice.lines.all()
        self.assertFalse(delivery.is_spread_charge)
        self.assertEqual(goods.spread_ht, D("0"))
        self.assertFalse(delivery.product.is_expense)
        self.assertTrue(delivery.product.needs_review)

    def test_saving_again_does_not_share_it_twice(self):
        self.tick()
        self.tick()
        goods = self.invoice.lines.first()
        self.assertEqual((goods.total_ht, goods.spread_ht), (D("100.00"), D("10.00")))

    def test_the_page_shows_the_share_under_the_line(self):
        self.tick()
        page = self.client.get(self.url)
        self.assertContains(page, "10.00 € HT de frais répartis")
        self.assertContains(page, "110.00 € HT")

    def test_the_document_page_names_the_line_rather_than_asking_to_classify_it(self):
        self.tick()
        page = self.client.get(reverse("invoices:invoice_detail", args=[self.invoice.pk]))
        self.assertContains(page, "frais répartis")
        self.assertNotContains(page, "À classer")


class MovedToAnotherSupplierTests(TestCase):
    """A document moved keeps what its lines are.

    Nobody is editing them, so an unticked box here is not a person having
    unticked it. `corrected_line` defaults it to False for the correction
    page, and taken as it stands, moving a document turned its delivery back
    into a product and erased every share with it.
    """

    def test_a_moved_document_keeps_its_delivery_and_its_shares(self):
        from invoices.receipts import move_documents

        old = Supplier.objects.get(code="METRO")
        invoice = Invoice.objects.create(supplier=old, invoice_number="134-052-000002")
        goods = make_invoice_line(
            invoice=invoice, product=make_product(supplier=old, raw_name="GIN EXEMPLE 70CL"),
            quantity=1, total_ht="100.00", vat_rate=D("0.20"),
        )
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=old, raw_name="LIVRAISON", is_expense=True),
            raw_name="LIVRAISON", quantity=1, total_ht="10.00", vat_rate=D("0.20"), is_spread_charge=True,
        )
        goods.spread_ht = D("10.00")
        goods.save(update_fields=["spread_ht"])

        move_documents([invoice], Supplier.objects.get(code="UBA"))

        moved_goods, moved_delivery = invoice.lines.all()
        self.assertTrue(moved_delivery.is_spread_charge)
        self.assertEqual(moved_goods.spread_ht, D("10.00"))
        self.assertEqual(moved_goods.cost_ht, D("110.00"))
        self.assertTrue(moved_delivery.product.is_expense)
