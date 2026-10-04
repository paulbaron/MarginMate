"""Manual-invoice line formset tests.

Same class of bug as recipes/tests/test_forms.py, same reason for testing it
at the payload level: the browser posts non-contiguous indices and echoes
back pre-filled defaults, neither of which a happy-path test produces.
"""

import os
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from invoices.forms import ManualInvoiceLineFormSet
from invoices.models import Invoice
from tests.factories import make_invoice, make_supplier


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
        formset = self.build(payload({0: {"product_name": "", "quantity": "", "total_ht": "", "vat_rate": "20"}}))
        self.assertFalse(formset.is_valid())
        self.assertIn("Ajoutez au moins un produit.", formset.non_form_errors())

    def test_a_half_filled_row_is_still_an_error(self):
        """The blank-row tolerance must not swallow a genuine mistake."""
        formset = self.build(payload({0: {"product_name": "Vodka", "quantity": "", "total_ht": "", "vat_rate": "20"}}))
        self.assertFalse(formset.is_valid())
        self.assertIn("quantity", formset.errors[0])

    def test_a_negative_total_on_a_purchase_is_rejected(self):
        """A refund is a negative count AND a negative amount (see
        test_edit_lines); a purchase at a negative price is a typo."""
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

    def test_the_form_offers_the_one_reader_not_ai(self):
        """No dedicated parser has not meant typing it in since the one
        reader started reading any document."""
        from invoices.forms import InvoiceTypeForm

        labels = [label for _value, label in InvoiceTypeForm().fields["parser_key"].choices]
        self.assertIn("— Lecteur générique —", labels)
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
            "invoice_date": "2026-01-01",
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
            product,
            StockType.objects.create(name="Vodka", unit="L"),
            unit="L",
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


class EditInvoiceLinesRoundTripTests(TestCase):
    """The page has to accept the values it just rendered.

    `InvoiceLine.vat_rate` is stored to four decimals, so 20% comes out of
    the database as 0.2000 and reaches the form as "20.0000" - which
    `ManualInvoiceLineForm.vat_rate` (two decimal places) then refuses. The
    save fails with an error under a field the user never touched, on a form
    they have just spent time filling in. Both line-entry screens pre-fill
    from saved lines, so both are checked here.
    """

    def setUp(self):
        from tests.factories import make_invoice_line, make_product

        supplier = make_supplier(code="ROUNDTRIP", name="Round Trip")
        self.invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 3, 1))
        product = make_product(supplier=supplier, raw_name="VODKA 70CL")
        make_invoice_line(
            invoice=self.invoice,
            product=product,
            quantity=6,
            total_ht="90.00",
            vat_rate=Decimal("0.2000"),
        )

    def _round_trip(self, url_name, prefix):
        response = self.client.get(reverse(url_name, args=[self.invoice.pk]))
        self.assertEqual(response.status_code, 200)
        rendered = response.context["formset"].forms[0].initial["vat_rate"]

        data = {
            f"{prefix}-TOTAL_FORMS": "1",
            f"{prefix}-INITIAL_FORMS": "1",
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
            "invoice_date": "2026-03-01",
            f"{prefix}-0-product_name": "VODKA 70CL",
            f"{prefix}-0-quantity": "6",
            f"{prefix}-0-total_ht": "90.00",
            f"{prefix}-0-vat_rate": str(rendered),
        }
        saved = self.client.post(reverse(url_name, args=[self.invoice.pk]), data)
        self.assertEqual(saved.status_code, 302, f"{url_name} rejected the VAT rate it rendered ({rendered})")

    def test_edit_invoice_lines_accepts_its_own_rendered_rate(self):
        self._round_trip("invoices:invoice_edit_lines", "form")

    def test_the_rate_keeps_its_value_through_the_round_trip(self):
        response = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        self.assertEqual(response.context["formset"].forms[0].initial["vat_rate"], Decimal("20.00"))


class FiguresWiderThanTheirColumnTests(TestCase):
    """A figure wider than the column behind it is refused, at the door - on
    the hand-typed pages too, not only on the e-invoice import.

    SQLite stored 1500 / 0.001 = 1 500 000 in `unit_cost_ht` (10,4) without a
    word, and every read of that line then raised: the document's own pages,
    « Produits & charges » and « Banque » answered 500 for every login of the
    bar, and nothing short of raw SQL got the line out."""

    def setUp(self):
        self.supplier = make_supplier(code="NOPARSER", name="Sans parseur", parser_key="")

    def create(self, **row):
        # The page's own prefix, « form » - not the formset tests' « lines ».
        data = {key.replace("lines-", "form-", 1): value for key, value in payload({0: line(**row)}).items()}
        data.update({"supplier": self.supplier.pk, "invoice_number": "", "invoice_date": "2026-09-01"})
        return self.client.post(reverse("invoices:invoice_create_manual"), data)

    def test_a_count_of_a_thousandth_is_refused_on_the_hand_typed_invoice(self):
        before = Invoice.objects.count()
        response = self.create(quantity="0.001", total_ht="1500")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prix unitaire impossible")
        self.assertEqual(Invoice.objects.count(), before)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get(reverse("bank:bank_home")).status_code, 200)

    def test_a_forgotten_decimal_comma_is_refused(self):
        before = Invoice.objects.count()
        response = self.create(quantity="1", total_ht="1250000")
        self.assertContains(response, "Prix unitaire impossible")
        self.assertEqual(Invoice.objects.count(), before)

    def test_the_cliff_is_the_column_exactly(self):
        response = self.create(quantity="1", total_ht="999999.99")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Invoice.objects.get(supplier=self.supplier).lines.get().unit_cost_ht, Decimal("999999.99"))

    def test_a_rate_above_a_hundred_percent_is_refused(self):
        formset = ManualInvoiceLineFormSet(payload({0: line(vat_rate="150")}), prefix="lines")
        self.assertFalse(formset.is_valid())
        self.assertEqual(formset.errors[0]["vat_rate"], ["Un taux ne dépasse pas 100 %."])

    def test_the_correction_page_refuses_it_too(self):
        invoice = make_invoice(supplier=self.supplier)
        data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "invoice_date": "2026-01-01",
            "form-0-product_name": "VODKA 70CL",
            "form-0-quantity": "0.001",
            "form-0-total_ht": "1500",
            "form-0-vat_rate": "20",
        }
        response = self.client.post(reverse("invoices:invoice_edit_lines", args=[invoice.pk]), data)
        self.assertContains(response, "Prix unitaire impossible")
        self.assertEqual(invoice.lines.count(), 0)

    def test_a_ticket_whose_amount_with_its_vat_is_too_wide_is_refused(self):
        from invoices.forms import DOCUMENT_RECEIPT, LineCorrectionForm

        form = LineCorrectionForm(
            data={
                "product_name": "PAIN",
                "quantity": "1000000",
                "total_ht": "9999999999.99",
                "amount_source": "ht",
                "vat_rate": "20",
            },
            document=DOCUMENT_RECEIPT,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("total_ht", form.errors)
        form = LineCorrectionForm(
            data={"product_name": "PAIN", "quantity": "1", "total_ttc": "10", "vat_rate": "999"},
            document=DOCUMENT_RECEIPT,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("vat_rate", form.errors)

    def test_nothing_stores_a_line_its_column_cannot_hold(self):
        """The last guard, under every path that writes lines - an OCR
        reading, a move, a re-read: refused before anything is written."""
        from invoices.importing import LineTooWideError, import_parsed_invoice, replace_invoice_lines
        from invoices.parsers.base import ParsedInvoice, ParsedLine

        def wide():
            return ParsedLine(
                raw_name="VODKA 70CL",
                quantity=Decimal("0.001"),
                total_volume=Decimal("0"),
                unit_cost_ht=Decimal("1500000.0000"),
                total_ht=Decimal("1500.00"),
                vat_rate=Decimal("0.2"),
            )

        before = Invoice.objects.count()
        parsed = ParsedInvoice(
            supplier_code=self.supplier.code, invoice_number="", invoice_date=date(2026, 9, 1), lines=[wide()]
        )
        with self.assertRaisesMessage(LineTooWideError, "« VODKA 70CL » : le prix unitaire (1 500 000.0000)"):
            import_parsed_invoice(self.supplier, parsed)
        self.assertEqual(Invoice.objects.count(), before)
        self.assertIsInstance(LineTooWideError("x"), ValueError)

        invoice = make_invoice(supplier=self.supplier)
        with self.assertRaises(LineTooWideError):
            replace_invoice_lines(invoice, [wide()])
        self.assertEqual(invoice.lines.count(), 0)


class ManualInvoicePostedTwiceTests(TestCase):
    """Most paper invoices typed in have no number, and the number was the
    only thing refusing a second copy: two taps on « Créer la facture » on a
    phone filed the purchase twice - its stock, its « Facturé » in Marges,
    and two documents for one bank debit."""

    def setUp(self):
        self.supplier = make_supplier(code="NOPARSER", name="Sans parseur", parser_key="")
        self.url = reverse("invoices:invoice_create_manual")

    def post(self, token):
        data = {key.replace("lines-", "form-", 1): value for key, value in payload({0: line()}).items()}
        data.update({"supplier": self.supplier.pk, "invoice_number": "", "invoice_date": "2026-09-01", "jeton": token})
        return self.client.post(self.url, data)

    def test_the_same_form_posted_twice_makes_one_invoice(self):
        token = "Xq3vB0b1hJ8yQm2dKzP7cA"
        first = self.post(token)
        invoice = Invoice.objects.get(supplier=self.supplier)
        self.assertRedirects(first, reverse("invoices:invoice_detail", args=[invoice.pk]))
        second = self.post(token)
        self.assertRedirects(second, reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertEqual(Invoice.objects.filter(supplier=self.supplier).count(), 1)

    def test_two_forms_make_two_invoices(self):
        """Two identical tickets bought the same day are two purchases."""
        self.post("Xq3vB0b1hJ8yQm2dKzP7cA")
        self.post("Lr5tN9wE2uY6iO1pS4dF8g")
        self.assertEqual(Invoice.objects.filter(supplier=self.supplier).count(), 2)

    def test_each_page_carries_its_own_value_and_a_busy_button(self):
        first, second = self.client.get(self.url), self.client.get(self.url)
        self.assertContains(first, 'data-busy-label="Création…"')
        token = first.context["jeton"]
        self.assertContains(first, f'<input type="hidden" name="jeton" value="{token}">', html=True)
        self.assertNotEqual(token, second.context["jeton"])
