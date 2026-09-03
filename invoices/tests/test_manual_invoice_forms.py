"""Manual-invoice line formset tests.

Same class of bug as recipes/tests/test_forms.py, same reason for testing it
at the payload level: the browser posts non-contiguous indices and echoes
back pre-filled defaults, neither of which a happy-path test produces.
"""

from decimal import Decimal

import os
from datetime import date
from django.urls import reverse
from invoices.models import Invoice
from tests.factories import make_invoice, make_supplier
from django.test import TestCase

from invoices.forms import ManualInvoiceLineFormSet


def payload(rows, total_forms=None):
    data = {
        "lines-TOTAL_FORMS": str(total_forms if total_forms is not None else len(rows)),
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
    }
    for index, fields in rows.items():
        for name, value in fields.items():
            data[f"lines-{index}-{name}"] = str(value)
    return data


def line(name="Vodka 70cl", quantity=6, total_ht="90.00", vat_rate="20"):
    return {"product_name": name, "quantity": quantity, "total_ht": total_ht, "vat_rate": vat_rate}


class ManualInvoiceLineFormSetTests(TestCase):
    def build(self, data):
        return ManualInvoiceLineFormSet(data, prefix="lines")

    def test_a_single_line_is_valid(self):
        formset = self.build(payload({0: line()}))
        self.assertTrue(formset.is_valid(), formset.errors)
        self.assertEqual(formset.forms[0].cleaned_data["total_ht"], Decimal("90.00"))

    def test_several_lines(self):
        formset = self.build(payload({0: line(), 1: line(name="Gin 70cl", total_ht="45.00")}))
        self.assertTrue(formset.is_valid(), formset.errors)

    def test_a_trailing_row_left_at_its_default_vat_is_ignored(self):
        """B7's sibling: the extra blank row still submits vat_rate=20."""
        formset = self.build(
            payload({0: line(), 1: {"product_name": "", "quantity": "", "total_ht": "", "vat_rate": "20"}})
        )
        self.assertTrue(formset.is_valid(), formset.errors)

    def test_removing_a_row_leaves_a_gap_that_must_not_block_the_save(self):
        formset = self.build(payload({0: line(), 2: line(name="Gin 70cl")}, total_forms=3))
        self.assertTrue(formset.is_valid(), formset.errors)

    def test_an_invoice_with_no_lines_at_all_is_rejected(self):
        formset = self.build(
            payload({0: {"product_name": "", "quantity": "", "total_ht": "", "vat_rate": "20"}})
        )
        self.assertFalse(formset.is_valid())
        self.assertIn("Ajoutez au moins un produit.", formset.non_form_errors())

    def test_a_half_filled_row_is_still_an_error(self):
        """The blank-row tolerance must not swallow a genuine mistake."""
        formset = self.build(payload({0: {"product_name": "Vodka", "quantity": "", "total_ht": "", "vat_rate": "20"}}))
        self.assertFalse(formset.is_valid())
        self.assertIn("quantity", formset.errors[0])

    def test_a_negative_total_is_rejected(self):
        formset = self.build(payload({0: line(total_ht="-10.00")}))
        self.assertFalse(formset.is_valid())

    def test_a_zero_quantity_is_rejected(self):
        formset = self.build(payload({0: line(quantity=0)}))
        self.assertFalse(formset.is_valid())


class ManualEntryInsteadOfAiTests(TestCase):
    """A supplier with no parser used to have its PDF handed to an LLM, and
    whatever came back was kept. Guessing at prices is the one thing this app
    must not do: every number ends up in a stock valuation or a margin, and a
    plausible wrong figure is worse than none, because nothing downstream can
    tell the difference. The invoice now arrives empty, to be typed in."""

    def setUp(self):
        self.supplier = make_supplier(code="NOPARSER", name="Sans parseur", parser_key="")

    def make_pdf(self):
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".pdf")
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"%PDF-1.4\n% not really a pdf\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_an_unparsed_invoice_is_imported_empty_rather_than_guessed(self):
        from invoices.importing import parse_and_import

        invoice = parse_and_import(self.make_pdf(), self.supplier, date_hint=date(2026, 3, 5))
        self.assertEqual(invoice.lines.count(), 0)
        self.assertEqual(invoice.invoice_date, date(2026, 3, 5))
        self.assertTrue(invoice.source_file, "the PDF still has to be filed")

    def test_it_is_flagged_for_review_not_left_looking_complete(self):
        from invoices.importing import parse_and_import

        invoice = parse_and_import(self.make_pdf(), self.supplier)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)

    def test_no_llm_is_reached_for(self):
        """The old fallback would have called out to the Anthropic API."""
        from unittest import mock

        from invoices.importing import parse_and_import

        with mock.patch("invoices.parsers.llm_fallback.LLMFallbackParser.parse") as llm:
            parse_and_import(self.make_pdf(), self.supplier)
        llm.assert_not_called()

    def test_the_form_offers_manual_entry_not_ai(self):
        from invoices.forms import InvoiceTypeForm

        labels = [label for _value, label in InvoiceTypeForm().fields["parser_key"].choices]
        self.assertIn("— Saisie manuelle —", labels)
        self.assertFalse([label for label in labels if "IA" in label])


class InvoiceLineEditingTests(TestCase):
    """Typing an invoice's lines in by hand, and correcting them later."""

    def setUp(self):
        self.supplier = make_supplier(code="NOPARSER", name="Sans parseur")
        self.invoice = make_invoice(supplier=self.supplier, invoice_number="M-1")

    def url(self):
        return reverse("invoices:invoice_edit_lines", kwargs={"pk": self.invoice.pk})

    def payload(self, rows):
        data = {
            "form-TOTAL_FORMS": str(len(rows)),
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"form-{index}-{key}"] = str(value)
        return data

    def test_lines_can_be_typed_in(self):
        self.client.post(
            self.url(),
            self.payload([{"product_name": "VODKA 70CL", "quantity": 6, "total_ht": "90.00", "vat_rate": "20"}]),
        )
        line = self.invoice.lines.get()
        self.assertEqual(line.raw_name, "VODKA 70CL")
        self.assertEqual(line.total_ht, Decimal("90.00"))
        self.assertEqual(line.vat_rate, Decimal("0.2"))
        self.assertEqual(line.unit_cost_ht, Decimal("15.0000"))

    def test_saving_replaces_the_previous_lines(self):
        self.client.post(
            self.url(),
            self.payload([{"product_name": "VODKA 70CL", "quantity": 6, "total_ht": "90.00", "vat_rate": "20"}]),
        )
        self.client.post(
            self.url(),
            self.payload([{"product_name": "GIN 70CL", "quantity": 3, "total_ht": "60.00", "vat_rate": "20"}]),
        )
        self.assertEqual([line.raw_name for line in self.invoice.lines.all()], ["GIN 70CL"])

    def test_replacing_lines_does_not_leave_orphan_stock_movements(self):
        """The movements are the whole reason a line matters; leaving the old
        ones behind would double-count the stock."""
        from inventory.models import StockMovement, StockType
        from inventory.services import link_product_to_stock_type

        self.client.post(
            self.url(),
            self.payload([{"product_name": "VODKA 70CL", "quantity": 6, "total_ht": "90.00", "vat_rate": "20"}]),
        )
        product = self.invoice.lines.get().product
        link_product_to_stock_type(
            product, StockType.objects.create(name="Vodka", unit="L"), unit="L",
            stock_equivalent=Decimal("1"),
        )
        self.assertEqual(StockMovement.objects.count(), 1)

        self.client.post(
            self.url(),
            self.payload([{"product_name": "VODKA 70CL", "quantity": 12, "total_ht": "180.00", "vat_rate": "20"}]),
        )
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity, Decimal("12"))

    def test_the_form_is_prefilled_with_the_existing_lines(self):
        self.client.post(
            self.url(),
            self.payload([{"product_name": "VODKA 70CL", "quantity": 6, "total_ht": "90.00", "vat_rate": "20"}]),
        )
        html = self.client.get(self.url()).content.decode()
        self.assertIn("VODKA 70CL", html)
        self.assertIn("90.00", html)

    def test_submitting_no_lines_at_all_is_refused(self):
        """An invoice with no lines is what you started with, not an edit -
        the formset says so rather than silently wiping the lines."""
        self.client.post(
            self.url(),
            self.payload([{"product_name": "VODKA 70CL", "quantity": 6, "total_ht": "90.00", "vat_rate": "20"}]),
        )
        response = self.client.post(self.url(), self.payload([]))
        self.assertContains(response, "Ajoutez au moins un produit")
        self.assertEqual(self.invoice.lines.count(), 1)
