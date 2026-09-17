"""A quantity can be a fraction: plywood sold by the square metre (0,82 m²),
a cable by the metre.

A till or an invoice prints it in its quantity column; the correction page
takes it as typed (0,82 or 0.82), stores it to the thousandth and shows it
as written - never "0.820", never "1E+1". Data invented.
"""

from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from invoices.importing import replace_invoice_lines
from invoices.models import InvoiceLine, Supplier
from invoices.parsers.base import ParsedLine
from invoices.tests.page_posts import page_post
from tests.factories import make_invoice, make_invoice_line, make_product

D = Decimal
CHECKED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]


class QuantityFilterTests(SimpleTestCase):
    def render(self, value):
        return Template("{% load assets %}{{ value|quantity }}").render(Context({"value": value}))

    def test_as_written(self):
        for value, shown in (
            (D("0.820"), "0.82"),
            (D("2.000"), "2"),
            (D("10.000"), "10"),
            (D("-1.000"), "-1"),
            (3, "3"),
            (D("0.125"), "0.125"),
            (None, ""),
            ("", ""),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.render(value), shown)


class DecimalQuantityTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")
        self.ticket = make_invoice(supplier=self.shop, parse_checks=CHECKED)
        self.line = make_invoice_line(
            invoice=self.ticket, product=make_product(supplier=self.shop, raw_name="CP EXT 10MM 1M2"),
            raw_name="CP EXT 10MM 1M2", total_ht="25.22", vat_rate=D("0.20"), printed_ttc=D("30.26"),
        )
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def test_a_fraction_is_stored(self):
        replace_invoice_lines(
            self.ticket,
            [ParsedLine(raw_name="CP EXT 15MM 1M2", quantity=D("0.55"), total_volume=D("0"),
                        unit_cost_ht=D("45.7636"), total_ht=D("25.17"), vat_rate=D("0.20"))],
        )
        line = InvoiceLine.objects.get(invoice=self.ticket)
        self.assertEqual(line.quantity, D("0.55"))
        self.assertEqual(str(line), "CP EXT 15MM 1M2 x0.55")

    def test_the_page_takes_a_fraction_typed_with_a_comma(self):
        page = self.client.get(self.url)
        self.assertEqual(page.context["formset"].forms[0].initial["quantity"], D("1"))
        response = self.client.post(self.url, page_post(page, **{"form-0-quantity": "0,82"}))
        self.assertEqual(response.status_code, 302)
        self.line.refresh_from_db()
        self.assertEqual((self.line.quantity, self.line.total_ht), (D("0.82"), D("25.22")))

    def test_the_page_shows_it_as_written(self):
        InvoiceLine.objects.filter(pk=self.line.pk).update(quantity=D("0.82"))
        page = self.client.get(self.url)
        self.assertContains(page, 'value="0.82"')
        self.assertNotContains(page, 'value="0.820"')
        detail = self.client.get(reverse("invoices:invoice_detail", args=[self.ticket.pk]))
        self.assertContains(detail, ">0.82<")
        preview = self.client.get(reverse("invoices:invoice_preview", args=[self.ticket.pk]))
        self.assertContains(preview, ">0.82<")

    def test_more_than_three_decimals_is_refused(self):
        page = self.client.get(self.url)
        response = self.client.post(self.url, page_post(page, **{"form-0-quantity": "0.8215"}))
        self.assertEqual(response.status_code, 200)
        self.line.refresh_from_db()
        self.assertEqual(self.line.quantity, D("1"))
