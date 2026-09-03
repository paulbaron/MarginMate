"""Manual-invoice line formset tests.

Same class of bug as recipes/tests/test_forms.py, same reason for testing it
at the payload level: the browser posts non-contiguous indices and echoes
back pre-filled defaults, neither of which a happy-path test produces.
"""

from decimal import Decimal

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
