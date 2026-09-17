"""The receipt review screen works in TTC, like the ticket it is checked
against - converting every price to HT in one's head to compare it with the
photo made correcting a receipt hard. Lines are still stored HT."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from invoices.forms import DOCUMENT_RECEIPT, LineCorrectionForm
from invoices.models import Supplier
from tests.factories import make_invoice, make_invoice_line, make_product

CHECKED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]


def bound_form(total_ttc, rate, quantity="1", **fields):
    form = LineCorrectionForm(
        data={"product_name": "MENTHE", "quantity": quantity, "total_ttc": total_ttc, "vat_rate": rate, **fields},
        document=DOCUMENT_RECEIPT,
    )
    if not form.is_valid():
        raise AssertionError(form.errors)
    return form


def ht(form):
    return form.amounts()["total_ht"]


class TtcToHtTests(SimpleTestCase):
    def test_the_ht_total_comes_from_the_ttc_and_the_line_rate(self):
        self.assertEqual(ht(bound_form("10.55", "5.5")), Decimal("10.00"))
        self.assertEqual(ht(bound_form("12.00", "20")), Decimal("10.00"))
        self.assertEqual(ht(bound_form("7.00", "0")), Decimal("7.00"))

    def test_a_total_saved_untouched_keeps_its_ht_to_the_cent(self):
        """The page shows HT x (1 + rate), rounded. Saved as it is, a line
        must not drift a cent with every review."""
        drifted = []
        for percent in ("5.5", "10", "20", "0"):
            rate = Decimal(percent) / 100
            for cents in range(1, 2001):
                stored = Decimal(cents) / 100
                shown = (stored * (1 + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if ht(bound_form(str(shown), percent)) != stored:
                    drifted.append((percent, stored, shown))
        self.assertEqual(drifted, [])

    def test_the_ht_typed_last_is_the_one_kept(self):
        """The page fills each amount from the other; the one typed last
        says which is the reading."""
        form = bound_form("12.00", "20", total_ht="9.99", amount_source="ht")
        self.assertEqual(form.amounts(), {
            "total_ht": Decimal("9.99"), "printed_ttc": Decimal("11.99"),
            "discount_ttc": Decimal("0"), "discount": Decimal("0"),
        })

    def test_an_amount_given_only_one_way_is_that_way(self):
        form = bound_form("", "20", total_ht="10.00")
        self.assertEqual(form.amounts()["printed_ttc"], Decimal("12.00"))

    def test_a_promotion_comes_off_the_printed_amount(self):
        form = bound_form("0.49", "5.5", discount_ttc="0.17")
        self.assertEqual(form.amounts(), {
            "total_ht": Decimal("0.30"), "printed_ttc": Decimal("0.49"),
            "discount_ttc": Decimal("0.17"), "discount": Decimal("0.16"),
        })

    def test_a_promotion_above_the_price_is_refused(self):
        form = LineCorrectionForm(
            data={"product_name": "PAIN", "quantity": "1", "total_ttc": "0.49", "discount_ttc": "0.50",
                  "vat_rate": "5.5"},
            document=DOCUMENT_RECEIPT,
        )
        self.assertIn("discount_ttc", form.errors)

    def test_no_amount_at_all_is_refused(self):
        form = LineCorrectionForm(
            data={"product_name": "PAIN", "quantity": "1", "vat_rate": "5.5"}, document=DOCUMENT_RECEIPT
        )
        self.assertIn("total_ttc", form.errors)

    def test_a_new_ticket_line_is_food(self):
        self.assertEqual(LineCorrectionForm(document=DOCUMENT_RECEIPT)["vat_rate"].value(), Decimal("5.5"))
        self.assertEqual(LineCorrectionForm()["vat_rate"].value(), Decimal("20"))


class ReviewScreenTtcTests(TestCase):
    def setUp(self):
        supplier = Supplier.objects.get(code="WINGSENG")
        self.invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 7, 1), parse_checks=CHECKED)
        make_invoice_line(
            invoice=self.invoice,
            product=make_product(supplier=supplier, raw_name="MENTHE"),
            quantity=3,
            total_ht="1.42",
            unit_cost_ht="0.4733",
            vat_rate=Decimal("0.055"),
            raw_name="MENTHE",
            read_as="MENTHE",
        )
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def post(self, total_ttc, vat_rate="5.5"):
        return self.client.post(
            self.url,
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "invoice_date": "2026-07-01",
                "form-0-product_name": "MENTHE",
                "form-0-read_as": "MENTHE",
                "form-0-quantity": "3",
                "form-0-total_ttc": total_ttc,
                "form-0-vat_rate": vat_rate,
            },
        )

    def test_the_form_shows_what_the_ticket_prints(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["formset"].forms[0].initial["total_ttc"], Decimal("1.50"))
        self.assertContains(response, 'name="form-0-total_ttc"')
        self.assertContains(response, 'value="1.50"')
        self.assertContains(response, 'name="form-0-total_ht"')
        self.assertContains(response, 'value="1.42"')

    def test_a_corrected_ttc_total_is_stored_ht(self):
        self.assertEqual(self.post("2.11").status_code, 302)
        line = self.invoice.lines.get()
        self.assertEqual((line.total_ht, line.unit_cost_ht), (Decimal("2.00"), Decimal("0.6667")))

    def test_saved_untouched_nothing_moves(self):
        initial = self.client.get(self.url).context["formset"].forms[0].initial
        self.post(str(initial["total_ttc"]), str(initial["vat_rate"]))
        self.assertEqual(self.invoice.lines.get().total_ht, Decimal("1.42"))

    def test_a_receipts_correct_button_opens_the_review_screen(self):
        response = self.client.get(reverse("invoices:invoice_detail", args=[self.invoice.pk]))
        self.assertContains(response, f'href="{self.url}"')

    def test_a_digital_invoice_keeps_the_ht_form(self):
        invoice = make_invoice(supplier=Supplier.objects.get(code="METRO"))
        response = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(response, reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
