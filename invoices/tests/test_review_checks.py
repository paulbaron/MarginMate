"""The review screen's checks follow the lines a person types.

The checks shown beside a photo were the parser's verdict at import, and
stayed so: a ticket corrected line by line still read "lignes 3.85 € /
ticket 9.03 €" for ever. The lines are now checked against the printed total
as they are typed (the page's script, from `live_check`), the total itself
can be typed when the OCR missed it, and validating stores the check on the
lines as validated. A parser fix reaches tickets still to be checked through
their stored reading (`reread_receipt`).

Structurally faithful, data invented.
"""

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from invoices.models import Invoice, Supplier
from invoices.receipts import reread_receipt
from invoices.tests.test_parser_franprix_banner import SUFFIXED_MULTIPLIER
from tests.factories import make_invoice_line, make_product

D = Decimal
FIVE_FIVE = D("0.055")
IMPORTED_CHECKS = [
    {"label": "Somme des lignes = total imprimé", "passed": False, "detail": "lignes 0.49 € / ticket 0.98 € (écart +0.49 €)"},
    {"label": "TVA 5.5% cohérente", "passed": True, "detail": "HT 0.93 € x 5.5% = 0.05 € / ticket 0.05 €"},
    {"label": "Somme HT des lignes = base HT du ticket", "passed": False, "detail": "lignes 0.46 € HT / ticket 0.93 € HT"},
    {"label": "Articles = total avant remise", "passed": False, "detail": "un article manque"},
]


def ticket(printed_total="0.98", reviewed=False, **fields):
    shop = Supplier.objects.get(code="FRANPRIX")
    invoice = Invoice.objects.create(
        supplier=shop, invoice_number="004211-02-555", parse_checks=list(IMPORTED_CHECKS),
        printed_total_ttc=None if printed_total is None else D(printed_total), **fields,
    )
    if reviewed:
        from django.utils import timezone

        invoice.reviewed_at = timezone.now()
        invoice.save(update_fields=["reviewed_at"])
    make_invoice_line(
        invoice=invoice, product=make_product(supplier=shop, raw_name="PAIN COMPLET"), raw_name="PAIN COMPLET",
        read_as="PAIN COMPLET", quantity=1, total_ht="0.46", vat_rate=FIVE_FIVE, printed_ttc=D("0.49"),
    )
    return invoice


class ReviewScreenChecksTests(TestCase):
    def setUp(self):
        self.invoice = ticket()
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def post(self, **changes):
        data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "invoice_date": "2026-07-15",
            "form-0-product_name": "PAIN COMPLET",
            "form-0-quantity": "2",
            "form-0-total_ttc": "0.98",
            "form-0-vat_rate": "5.5",
            "form-0-line_id": str(self.invoice.lines.get().pk),
        }
        data.update(changes)
        return self.client.post(self.url, data)

    def test_the_page_checks_the_lines_as_they_stand(self):
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["live_check"],
            {
                "label": "Somme des lignes = total imprimé",
                "passed": False,
                "detail": "lignes 0.49 € / ticket 0.98 € (écart +0.49 €)",
            },
        )
        self.assertContains(response, 'id="live-lines-check"')
        self.assertContains(response, 'data-tolerance="0.05"')
        self.assertContains(response, 'name="printed_total_ttc"')
        self.assertContains(response, 'value="0.98"')
        self.assertContains(response, 'id="document-total"')
        self.assertEqual(response.content.decode().count("Somme des lignes = total imprimé"), 1)
        self.assertContains(response, "Somme HT des lignes = base HT du ticket <span class=\"muted\">(à la lecture du ticket)</span>")
        self.assertNotContains(response, "cohérente <span")

    def test_validating_stores_the_check_on_the_lines_as_validated(self):
        self.post()
        self.invoice.refresh_from_db()
        checks = {check["label"]: check for check in self.invoice.parse_checks}
        self.assertEqual(
            checks["Somme des lignes = total imprimé"],
            {
                "label": "Somme des lignes = total imprimé",
                "passed": True,
                "detail": "vérifié à la main : lignes 0.98 € / ticket 0.98 € (écart +0.00 €)",
            },
        )
        self.assertNotIn("Somme HT des lignes = base HT du ticket", checks)
        self.assertNotIn("Articles = total avant remise", checks)
        self.assertTrue(checks["TVA 5.5% cohérente"]["passed"])
        self.assertTrue(self.invoice.receipt_verified)

    def test_lines_that_still_do_not_add_up_say_so(self):
        self.post(**{"form-0-total_ttc": "0.49", "form-0-quantity": "1"})
        self.invoice.refresh_from_db()
        (check,) = [c for c in self.invoice.parse_checks if c["label"] == "Somme des lignes = total imprimé"]
        self.assertFalse(check["passed"])
        self.assertIn("écart +0.49 €", check["detail"])

    def test_the_total_can_be_typed_when_it_was_not_read(self):
        self.invoice.printed_total_ttc = None
        self.invoice.parse_checks = self.invoice.parse_checks + [
            {"label": "Total imprimé lu", "passed": False, "detail": "Aucun total lisible"}
        ]
        self.invoice.save(update_fields=["printed_total_ttc", "parse_checks"])
        page = self.client.get(self.url)
        self.assertIn("saisissez le total pour les vérifier", page.context["live_check"]["detail"])
        self.post(printed_total_ttc="0.98")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.printed_total_ttc, D("0.98"))
        labels = [check["label"] for check in self.invoice.parse_checks]
        self.assertNotIn("Total imprimé lu", labels)
        self.assertEqual(self.invoice.total_ttc, D("0.98"))

    def test_a_blank_total_keeps_the_one_read(self):
        self.post(printed_total_ttc="")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.printed_total_ttc, D("0.98"))


class RereadTests(TestCase):
    """SUFFIXED_MULTIPLIER was read as no line at all before the parser
    learnt the "Eur" suffix."""

    def stored(self, **fields):
        invoice = ticket(ocr_text=SUFFIXED_MULTIPLIER, **fields)
        invoice.lines.all().delete()
        return invoice

    def test_a_ticket_to_check_gets_the_better_reading(self):
        invoice = self.stored()
        self.assertTrue(reread_receipt(invoice))
        invoice.refresh_from_db()
        (line,) = invoice.lines.all()
        self.assertEqual((line.raw_name, line.quantity, line.printed_ttc, line.read_as), ("PAIN COMPLET", 2, D("0.98"), "PAIN COMPLET"))
        self.assertEqual(invoice.printed_total_ttc, D("0.98"))
        self.assertTrue(all(check["passed"] for check in invoice.parse_checks))

    def test_a_checked_ticket_is_never_reread(self):
        invoice = self.stored(reviewed=True)
        self.assertFalse(reread_receipt(invoice))
        self.assertFalse(invoice.lines.exists())

    def test_a_ticket_that_already_adds_up_is_left_alone(self):
        invoice = self.stored()
        invoice.parse_checks = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]
        invoice.save(update_fields=["parse_checks"])
        self.assertFalse(reread_receipt(invoice))

    def test_a_sum_that_passed_for_the_wrong_reason_is_reread(self):
        invoice = self.stored()
        invoice.parse_checks = [
            {"label": "Somme des lignes = total imprimé", "passed": True, "detail": "lignes 0.20 € / ticket 0.20 €"},
            {"label": "Remise attribuée", "passed": False, "detail": "remise 0.78 €"},
        ]
        invoice.save(update_fields=["parse_checks"])
        self.assertTrue(reread_receipt(invoice))
        self.assertEqual(invoice.lines.get().quantity, 2)

    def test_a_reading_no_better_than_the_old_is_not_kept(self):
        invoice = self.stored()
        invoice.parse_checks = [
            {"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""},
            {"label": "Confiance OCR", "passed": False, "detail": ""},
        ]
        invoice.save(update_fields=["parse_checks"])
        invoice.ocr_text = invoice.ocr_text.replace("5.5%  0.93  0.05  0.98", "")
        invoice.save(update_fields=["ocr_text"])
        self.assertFalse(reread_receipt(invoice))

    def test_a_reading_that_still_does_not_add_up_is_not_kept(self):
        invoice = self.stored()
        invoice.ocr_text = SUFFIXED_MULTIPLIER.replace("PAIN COMPLET  T1 2 X 0.49Eur 0.98Eur" + chr(10), "")
        invoice.save(update_fields=["ocr_text"])
        self.assertFalse(reread_receipt(invoice))
        self.assertEqual(invoice.parse_checks, IMPORTED_CHECKS)

    def test_the_command_changes_nothing_on_a_dry_run(self):
        invoice = self.stored()
        call_command("reread_receipts", "--dry-run", stdout=StringIO())
        self.assertFalse(invoice.lines.exists())
        call_command("reread_receipts", stdout=StringIO())
        self.assertTrue(invoice.lines.exists())
