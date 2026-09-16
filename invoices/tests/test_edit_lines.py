"""Correcting an invoice's lines by hand.

The forms show a line's name, count, total and VAT; a stored line carries
more - Metro's measured volume, duty, a discount, the pack size, a receipt's
reading. Saving used to rebuild every line from the four visible fields:
a Metro invoice saved untouched turned 4.2 L of vodka into 6 L of stock. It
also refused a deposit refund's negative figures (41 real Metro invoices
carry one), and crashed on any line a stock take had been priced from.

Structurally faithful, data invented.
"""

from datetime import datetime
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from inventory.models import StockTakeLineSource, UnitChoices
from inventory.services import create_stock_movement_for_line
from invoices.forms import ManualInvoiceLineFormSet
from invoices.models import Invoice, InvoiceLine, Supplier
from tests.factories import make_invoice_line, make_product, make_stock_take, make_stock_take_line, make_stock_type

D = Decimal


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def as_posted(forms, fields, **changes):
    """What the browser sends back for these forms, with `changes` applied."""
    data = {
        "form-TOTAL_FORMS": str(len(forms)),
        "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
    }
    for index, form in enumerate(forms):
        for field in fields:
            value = form.initial.get(field)
            data[f"form-{index}-{field}"] = "" if value is None else str(value)
    data.update(changes)
    return data


class HtEditorTests(TestCase):
    FIELDS = ("product_name", "quantity", "total_ht", "vat_rate", "line_id")

    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        self.vodka = make_stock_type("Vodka", unit=UnitChoices.LITRE)
        product = make_product(
            supplier=self.metro, raw_name="VODKA EXEMPLE 70CL", stock_type=self.vodka, unit=UnitChoices.LITRE
        )
        self.invoice = Invoice.objects.create(supplier=self.metro, invoice_number="134-056-000001")
        self.line = make_invoice_line(
            invoice=self.invoice, product=product, quantity=6, total_volume="4.2", total_ht="62.40",
            taxes="2.40", discount="1.00", colisage=6, category="Spiritueux", vat_rate=D("0.20"),
        )
        create_stock_movement_for_line(self.line)
        self.url = reverse("invoices:invoice_edit_lines", args=[self.invoice.pk])

    def post(self, **changes):
        forms = [form for form in self.client.get(self.url).context["formset"].forms if form.initial]
        return self.client.post(self.url, as_posted(forms, self.FIELDS, **changes))

    def test_saved_untouched_nothing_changes(self):
        self.post()
        line = self.invoice.lines.get()
        self.assertEqual(line.pk, self.line.pk)
        self.assertEqual(
            (line.total_volume, line.taxes, line.discount, line.colisage, line.category),
            (D("4.2"), D("2.40"), D("1.00"), 6, "Spiritueux"),
        )
        self.assertEqual(self.vodka.current_quantity, D("4.2"))

    def test_a_new_count_scales_the_measured_volume(self):
        self.post(**{"form-0-quantity": "12", "form-0-total_ht": "124.80"})
        line = self.invoice.lines.get()
        self.assertEqual((line.quantity, line.total_volume), (12, D("8.4")))
        self.assertEqual(self.vodka.current_quantity, D("8.4"))

    def test_a_deposit_refund_can_be_saved(self):
        crate = make_product(supplier=self.metro, raw_name="PALETTE EUROPE")
        make_invoice_line(invoice=self.invoice, product=crate, quantity=-1, total_ht="-15.00", vat_rate=D("0"))
        response = self.post()
        self.assertEqual(response.status_code, 302, response.context and response.context["formset"].errors)
        refund = self.invoice.lines.get(product=crate)
        self.assertEqual((refund.quantity, refund.total_ht), (-1, D("-15.00")))

    def test_a_line_a_stock_take_was_priced_from_can_be_corrected(self):
        self._price_a_count_from(self.line)
        response = self.post(**{"form-0-total_ht": "63.00"})
        self.assertEqual(response.status_code, 302)
        line = InvoiceLine.objects.get(pk=self.line.pk)
        self.assertEqual(line.total_ht, D("63.00"))

    def test_removing_such_a_line_is_refused_and_says_why(self):
        self._price_a_count_from(self.line)
        other = make_product(supplier=self.metro, raw_name="GIN EXEMPLE 70CL")
        response = self.post(
            **{
                "form-0-DELETE": "on",
                "form-1-product_name": other.raw_name,
                "form-1-quantity": "1",
                "form-1-total_ht": "10.00",
                "form-1-vat_rate": "20",
                "form-TOTAL_FORMS": "2",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("01/07/2026" in message for message in messages_of(response)), messages_of(response))
        self.assertEqual(list(self.invoice.lines.values_list("pk", flat=True)), [self.line.pk])

    def _price_a_count_from(self, line):
        count = make_stock_take_line(
            stock_take=make_stock_take(taken_at=datetime(2026, 7, 1, 21, 0)), product=line.product,
            unit=UnitChoices.UNIT,
        )
        StockTakeLineSource.objects.create(
            stock_take_line=count, invoice_line=line, quantity_used=D("1"), unit_cost_ht=D("10.40")
        )


class RefundValidationTests(TestCase):
    def build(self, **row):
        data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-product_name": "PALETTE EUROPE",
            "form-0-vat_rate": "20",
        }
        data.update({f"form-0-{key}": value for key, value in row.items()})
        return ManualInvoiceLineFormSet(data)

    def test_a_refund_is_a_negative_count_and_a_negative_amount(self):
        self.assertTrue(self.build(quantity="-1", total_ht="-15.00").is_valid())

    def test_the_signs_must_agree(self):
        """A positive count at a negative price would be stock valued below
        nothing - the FIFO valuation's worst known failure."""
        for quantity, total in (("1", "-15.00"), ("-1", "15.00")):
            with self.subTest(quantity=quantity, total=total):
                self.assertFalse(self.build(quantity=quantity, total_ht=total).is_valid())

    def test_a_free_item_either_way_is_fine(self):
        self.assertTrue(self.build(quantity="-1", total_ht="0").is_valid())
        self.assertTrue(self.build(quantity="2", total_ht="0").is_valid())

    def test_a_zero_count_is_still_refused(self):
        self.assertFalse(self.build(quantity="0", total_ht="0").is_valid())


class ReceiptReviewKeepsWhatItDoesNotShowTests(TestCase):
    """The same trap on the ticket screen: a weighed Wing Seng line lost its
    kilos on every validation."""

    FIELDS = ("product_name", "quantity", "total_ttc", "vat_rate", "read_as", "computed_ttc", "line_id")

    def test_a_weighed_line_keeps_its_weight(self):
        shop = Supplier.objects.get(code="WINGSENG")
        invoice = Invoice.objects.create(
            supplier=shop, invoice_number="000401", parse_checks=[{"label": "x", "passed": True, "detail": ""}]
        )
        line = make_invoice_line(
            invoice=invoice, product=make_product(supplier=shop, raw_name="CITRON VERT"), raw_name="CITRON VERT",
            read_as="CITRON VERT", quantity=1, total_volume="4.184", total_ht="11.86", vat_rate=D("0.055"),
            printed_ttc=D("12.51"),
        )
        url = reverse("invoices:receipt_review", args=[invoice.pk])
        forms = self.client.get(url).context["formset"].forms
        self.client.post(url, as_posted(forms, self.FIELDS))
        stored = invoice.lines.get()
        self.assertEqual((stored.pk, stored.total_volume, stored.printed_ttc), (line.pk, D("4.184"), D("12.51")))
