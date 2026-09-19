"""The VAT table of the correction page: what refused a row, and a table
emptied that came back.

The owner, 19/09: "Accept negative values for TVA table also". The sign was
never what refused it. The stored 5,5 % ("0.055") was drawn as "5.500",
which its own field (two decimals) refused - on 361 documents, invisibly,
since the page printed only a row's own errors, and the row then read « Un
taux se saisit avec sa base HT et son montant de TVA. » beside three values
typed. An electricity bill's -2,76 € at 5,5 % could not be saved, and was
saved with its table emptied - which the reading then filled back in, with
a failed check nobody could answer.

The posts are the page's own (`page_post(..., with_vat=True)`): a table
written by hand as "5.5" is not what the page sends back. Data invented.
"""

import importlib
import re
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.apps import apps
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from invoices.forms import VatRowForm
from invoices.models import Invoice
from invoices.receipts import HT_CHECK, vat_table, vat_table_checks
from invoices.tests.page_posts import page_post
from invoices.tests.test_parser_franprix_banner import SUFFIXED_MULTIPLIER
from invoices.tests.test_review_checks import ticket as franprix_ticket
from tests.factories import make_invoice_line, make_product, make_supplier

D = Decimal
ROW_REFUSED = "Un taux se saisit avec sa base HT et son montant de TVA."
# An electricity bill's credit and charge, as it prints them: the month's
# subscription taken back at 5,5 %, fuel billed at 20 %.
CREDIT_AND_CHARGE = (
    ("AVOIR ABONNEMENT", -1, "-2.76", "0.055", "-2.91"),
    ("PLEIN GAZOLE", 1, "100.00", "0.2", "120.00"),
)


def shop_ticket(table, lines=(("PAIN DE CAMPAGNE", 1, "14.23", "0.055", "15.01"),)):
    """A checked-out ticket of an invented grocer, with `table` stored as the
    VAT table it prints."""
    shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
    invoice = Invoice.objects.create(
        supplier=shop, invoice_number="T-0001", invoice_date=date(2026, 8, 12),
        parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        printed_total_ttc=sum((D(printed) for *_rest, printed in lines), start=D("0")),
        vat_breakdown=[list(row) for row in table],
    )
    for name, quantity, total_ht, rate, printed in lines:
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=shop, raw_name=name), raw_name=name, read_as=name,
            quantity=quantity, total_ht=total_ht, vat_rate=D(rate), printed_ttc=D(printed),
        )
    return invoice


def input_tag(page, name) -> str:
    """The <input> the page draws for the field posted as `name`."""
    match = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', page.content.decode())
    assert match, f"no input named {name}"
    return match.group(0)


class TableSavedFromThePageTests(TestCase):
    def open(self, invoice):
        """The page of `invoice`; `self.url` is where it posts."""
        self.url = reverse("invoices:receipt_review", args=[invoice.pk])
        return self.client.get(self.url)

    def test_a_table_posted_back_untouched_is_saved_as_it_was(self):
        """Every 5,5 % row: drawn as "5.500", posted back, refused."""
        invoice = shop_ticket([("0.055", "14.23", "0.78")])
        page = self.open(invoice)
        response = self.client.post(self.url, page_post(page, with_vat=True))
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.reviewed_at)
        # The same text it held - not "0.0550".
        self.assertEqual(invoice.vat_breakdown, [["0.055", "14.23", "0.78"]])

    def test_the_rate_is_drawn_with_two_decimals(self):
        page = self.open(shop_ticket([("0.055", "14.23", "0.78")]))
        self.assertIn('value="5.50"', input_tag(page, "tva-0-rate"))
        self.assertEqual(page.context["vat_form"].initial[0]["rate"].as_tuple().exponent, -2)

    def test_a_negative_row_typed_over_the_reading_is_saved(self):
        """Invoice 842's case: the reading's 5,5 % row (2,61 / 0,15, the sign
        lost) corrected to the -2,76 / -0,15 printed, its rate left as drawn,
        and the 20 % row typed in the spare one."""
        invoice = shop_ticket([("0.055", "2.61", "0.15")], lines=CREDIT_AND_CHARGE)
        typed = {"tva-0-base": "-2.76", "tva-0-vat": "-0.15", "tva-1-rate": "20", "tva-1-base": "100.00",
                 "tva-1-vat": "20.00"}
        page = self.open(invoice)
        response = self.client.post(self.url, page_post(page, with_vat=True, **typed))
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.vat_breakdown, [["0.055", "-2.76", "-0.15"], ["0.2", "100.00", "20.00"]])
        checks = {check["label"]: check for check in invoice.parse_checks}
        self.assertTrue(checks["TVA 5.5% cohérente"]["passed"], checks["TVA 5.5% cohérente"])
        self.assertTrue(checks["TVA 20% cohérente"]["passed"])
        self.assertEqual(
            checks[HT_CHECK],
            {"label": HT_CHECK, "passed": True, "detail": "lignes 97.24 € HT / document 97.24 € HT (écart +0.00 €)"},
        )

    def test_a_base_with_four_decimals_posted_back_untouched_is_saved(self):
        """Monoprix prints its HT to four decimals: 7 rows on 4 documents."""
        invoice = shop_ticket([("0.2", "3.0237", "0.6047")], lines=(("SAVON LIQUIDE", 1, "3.02", "0.2", "3.63"),))
        page = self.open(invoice)
        response = self.client.post(self.url, page_post(page, with_vat=True))
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.vat_breakdown, [["0.2", "3.0237", "0.6047"]])

    def test_a_refused_rate_says_what_is_wrong_with_it(self):
        """Its own error, where the row is: not « Un taux se saisit… »
        beside three typed values."""
        invoice = shop_ticket([("0.055", "14.23", "0.78")])
        page = self.open(invoice)
        response = self.client.post(self.url, page_post(page, with_vat=True, **{"tva-0-rate": "150"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Taux : Un taux ne dépasse pas 100 %.")
        self.assertNotContains(response, ROW_REFUSED)
        invoice.refresh_from_db()
        self.assertIsNone(invoice.reviewed_at)

    def test_a_refusal_in_the_spare_row_is_not_folded_away(self):
        """With no table stored the block is folded: a row refused there
        was an error nobody could see."""
        invoice = shop_ticket([])
        page = self.open(invoice)
        self.assertContains(page, '<details class="explainer vat-table">')
        typed = {"tva-0-rate": "5.5", "tva-0-base": "14.23", "tva-0-vat": "0.7812345"}
        response = self.client.post(self.url, page_post(page, with_vat=True, **typed))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<details class="explainer vat-table" open>')
        self.assertContains(response, "TVA : Quatre décimales au plus.")

    def test_an_empty_table_takes_two_rates_in_one_save(self):
        """A row is only added by saving: with one spare, invoice 842's two
        rates - its table emptied - took two saves, the page leaving between
        them."""
        invoice = shop_ticket([], lines=CREDIT_AND_CHARGE)
        page = self.open(invoice)
        self.assertEqual(len(page.context["vat_form"].forms), 2)
        self.assertContains(page, "TVA imprimée sur le document (0 taux)")
        typed = {"tva-0-rate": "5.5", "tva-0-base": "-2.76", "tva-0-vat": "-0.15",
                 "tva-1-rate": "20", "tva-1-base": "100.00", "tva-1-vat": "20.00"}
        self.assertEqual(self.client.post(self.url, page_post(page, with_vat=True, **typed)).status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.vat_breakdown, [["0.055", "-2.76", "-0.15"], ["0.2", "100.00", "20.00"]])
        page = self.open(invoice)
        # Two rows kept, one spare.
        self.assertEqual(len(page.context["vat_form"].forms), 3)
        self.assertContains(page, '<details class="explainer vat-table" open>')
        self.assertContains(page, "TVA imprimée sur le document (2 taux)")

    def test_both_spares_left_blank_store_nothing(self):
        invoice = shop_ticket([])
        page = self.open(invoice)
        self.assertEqual(self.client.post(self.url, page_post(page, with_vat=True)).status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.vat_breakdown, [])

    def test_the_amounts_take_a_sign_and_any_decimals(self):
        """With step="0.01" the browser refuses 3.0237 before it is posted,
        and a min would refuse a credit."""
        page = self.open(shop_ticket([("0.055", "14.23", "0.78")]))
        for name in ("tva-0-base", "tva-0-vat", "tva-1-base", "tva-1-vat"):
            tag = input_tag(page, name)
            self.assertIn('step="any"', tag)
            self.assertNotIn("min=", tag)

    def test_the_page_says_how_a_credit_is_typed(self):
        page = self.open(shop_ticket([("0.055", "14.23", "0.78")]))
        self.assertContains(page, "Une remise ou un avoir s'écrit en négatif (base et TVA)")


class VatRowFormTests(SimpleTestCase):
    def row(self, rate, base, vat):
        form = VatRowForm(data={"rate": rate, "base": base, "vat": vat})
        return form, (form.row if form.is_valid() else None)

    def test_the_rate_is_stored_as_the_fraction_it_is(self):
        for typed, stored in (("5.5", "0.055"), ("5.50", "0.055"), ("20", "0.2"), ("20.00", "0.2"), ("2.1", "0.021"),
                              ("100", "1"), ("0", "0"), ("0.00", "0")):
            with self.subTest(typed=typed):
                self.assertEqual(self.row(typed, "10.00", "1.00")[1][0], stored)

    def test_negative_amounts_and_four_decimals_are_kept_as_typed(self):
        self.assertEqual(self.row("5.5", "-2.76", "-0.15")[1], ["0.055", "-2.76", "-0.15"])
        self.assertEqual(self.row("20", "3.0237", "0.6047")[1], ["0.2", "3.0237", "0.6047"])
        self.assertEqual(self.row("20", "0", "0")[1], ["0.2", "0", "0"])

    def test_a_blank_row_is_none_and_no_error(self):
        form, row = self.row("", "", "")
        self.assertTrue(form.is_valid())
        self.assertIsNone(row)

    def test_half_a_row_is_still_refused_as_such(self):
        form, _row = self.row("5.5", "0.93", "")
        self.assertEqual(form.non_field_errors(), [ROW_REFUSED])

    def test_a_field_refused_says_so_and_only_so(self):
        for data, field in (
            (("-5.5", "10.00", "1.00"), "rate"),
            (("150", "10.00", "1.00"), "rate"),
            (("5.555", "10.00", "1.00"), "rate"),
            (("5.5", "10.00001", "1.00"), "base"),
            (("5.5", "10.00", "abc"), "vat"),
        ):
            with self.subTest(data=data):
                form, row = self.row(*data)
                self.assertIsNone(row)
                self.assertTrue(form.has_error(field))
                self.assertEqual(form.non_field_errors(), [])
                # In French, like every word on the page.
                self.assertNotIn("Ensure", " ".join(form.errors[field]))
                self.assertNotIn("Enter", " ".join(form.errors[field]))


class NegativeRowChecksTests(TestCase):
    """What checks a table with a credit row: signed all the way, already -
    locked here so it stays so."""

    def setUp(self):
        self.invoice = shop_ticket(
            [("0.055", "-2.76", "-0.15"), ("0.2", "100.00", "20.00")], lines=CREDIT_AND_CHARGE
        )

    def test_every_check_passes_on_a_credit_row(self):
        checks = vat_table_checks(self.invoice)
        self.assertEqual([check["label"] for check in checks], ["TVA 5.5% cohérente", "TVA 20% cohérente", HT_CHECK])
        self.assertTrue(all(check["passed"] for check in checks), checks)
        self.assertEqual(checks[0]["detail"], "HT -2.76 € x 5.5% = -0.15 € / document -0.15 €")

    def test_the_live_ht_check_follows_a_credit_row(self):
        page = self.client.get(reverse("invoices:receipt_review", args=[self.invoice.pk]))
        self.assertEqual(
            page.context["ht_check"],
            {"label": HT_CHECK, "passed": True, "detail": "lignes 97.24 € HT / document 97.24 € HT (écart +0.00 €)"},
        )


class EmptiedTableTests(TestCase):
    """(b2) A table a person emptied is theirs: it was read again from the
    photo whenever it was empty, and invoice 842 - saved with its table
    blanked, since that was the only way to save it - got back the reading's
    row and a failed « Somme HT des lignes = base HT du ticket »."""

    def setUp(self):
        # The grocer's till reads "5.5%  0.93  0.05  0.98" as its table.
        self.invoice = franprix_ticket(ocr_text=SUFFIXED_MULTIPLIER)
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def test_a_document_never_validated_shows_the_readings_table(self):
        page = self.client.get(self.url)
        self.assertEqual(page.context["vat_form"].initial, [{"rate": D("5.50"), "base": D("0.93"), "vat": D("0.05")}])
        self.assertFalse(self.invoice.vat_table_typed)

    def test_a_table_blanked_on_validation_stays_blank(self):
        page = self.client.get(self.url)
        blank = {"tva-0-rate": "", "tva-0-base": "", "tva-0-vat": ""}
        response = self.client.post(self.url, page_post(page, with_vat=True, invoice_date="2026-07-15", **blank))
        self.assertEqual(response.status_code, 302)
        self.invoice.refresh_from_db()
        labels = [check["label"] for check in self.invoice.parse_checks]
        self.assertFalse([label for label in labels if label.startswith("TVA ")], labels)
        self.assertNotIn(HT_CHECK, labels)
        self.assertEqual(self.invoice.vat_breakdown, [])
        self.assertEqual(vat_table(self.invoice), [])
        self.assertTrue(self.invoice.vat_table_typed)
        self.assertEqual(self.client.get(self.url).context["vat_form"].initial, [])

    def test_a_post_without_the_table_leaves_it_to_the_reading(self):
        """A page cached before the table existed posts none: nothing was
        typed, so nothing is the person's."""
        page = self.client.get(self.url)
        self.client.post(self.url, page_post(page, invoice_date="2026-07-15"))
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.vat_table_typed)
        self.assertEqual(len(vat_table(self.invoice)), 1)


class TypedTableMigrationTests(TestCase):
    """invoices/0031: every document validated since the table was on the
    page (the backup « …pre_vat_table », 18/09 at 03:22 in Paris) was
    validated through a page that posted its table - even empty."""

    def test_documents_validated_since_the_table_shipped_are_marked(self):
        migration = importlib.import_module("invoices.migrations.0031_invoice_vat_table_typed")
        shop = make_supplier(name="Epicerie Exemple")
        shipped = datetime(2026, 9, 18, 1, 22, tzinfo=dt_timezone.utc)
        self.assertEqual(migration.TABLE_SHIPPED, shipped)
        for number, reviewed_at in (
            ("A", datetime(2026, 9, 17, 23, 38, tzinfo=dt_timezone.utc)),  # the last one before it
            ("B", shipped),
            ("C", timezone.now()),
            ("D", None),
        ):
            Invoice.objects.create(supplier=shop, invoice_number=number, reviewed_at=reviewed_at)
        # On the model state the migration runs against: the field as added.
        migration.mark_typed_tables(apps, None)
        marked = dict(Invoice.objects.values_list("invoice_number", "vat_table_typed"))
        self.assertEqual(marked, {"A": False, "B": True, "C": True, "D": False})
