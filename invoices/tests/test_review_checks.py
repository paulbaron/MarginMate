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

from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from invoices.models import Invoice, Supplier
from invoices.receipts import reread_receipt
from invoices.tests.page_posts import page_post
from invoices.tests.test_parser_franprix_banner import SUFFIXED_MULTIPLIER
from tests.factories import make_invoice_line, make_product, make_supplier

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
        self.assertNotIn("Articles = total avant remise", checks)
        # The VAT checks are recomputed from the table on the page: this
        # ticket carries none, so the reading's own are gone with the rest.
        self.assertNotIn("TVA 5.5% cohérente", checks)
        self.assertNotIn("Somme HT des lignes = base HT du ticket", checks)
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


class PrintedVatTableTests(TestCase):
    """The third thing a check compares.

    The lines and the printed total were on the page; the table the document
    prints was not, so "TVA 5,5% cohérente" and "Somme HT des lignes = base
    HT du ticket" were warnings nobody could answer - 21 of them still
    standing on tickets checked long ago.
    """

    def setUp(self):
        self.invoice = ticket()
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def post(self, rows=(), **changes):
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
            "tva-TOTAL_FORMS": str(len(rows) or 1),
            "tva-INITIAL_FORMS": "0",
            "tva-MIN_NUM_FORMS": "0",
            "tva-MAX_NUM_FORMS": "1000",
        }
        for index, (rate, base, vat) in enumerate(rows):
            data[f"tva-{index}-rate"] = rate
            data[f"tva-{index}-base"] = base
            data[f"tva-{index}-vat"] = vat
        data.update(changes)
        return self.client.post(self.url, data)

    def checks(self):
        self.invoice.refresh_from_db()
        return {check["label"]: check for check in self.invoice.parse_checks}

    def test_the_page_offers_the_table_read_on_the_document(self):
        self.invoice.vat_breakdown = [["0.055", "0.93", "0.05"]]
        self.invoice.save(update_fields=["vat_breakdown"])
        page = self.client.get(self.url)
        self.assertEqual(
            page.context["vat_form"].initial,
            [{"rate": D("5.500"), "base": D("0.93"), "vat": D("0.05")}],
        )
        self.assertContains(page, "TVA imprimée sur le document")

    def test_typing_the_table_checks_the_lines_against_it(self):
        """0,93 € HT at 5,5% is 0,05 € of tax, and the line typed is 0,93 €
        HT: both checks pass, and both name figures on the page."""
        self.post(rows=[("5.5", "0.93", "0.05")])
        checks = self.checks()
        self.assertTrue(checks["TVA 5.5% cohérente"]["passed"])
        self.assertTrue(checks["Somme HT des lignes = base HT du ticket"]["passed"])
        self.assertEqual(self.invoice.vat_breakdown, [["0.055", "0.93", "0.05"]])

    def test_a_table_that_does_not_add_up_says_which_figure_is_wrong(self):
        self.post(rows=[("5.5", "0.93", "0.19")])
        checks = self.checks()
        self.assertFalse(checks["TVA 5.5% cohérente"]["passed"])
        self.assertIn("0.05 € / document 0.19 €", checks["TVA 5.5% cohérente"]["detail"])

    def test_and_correcting_it_clears_the_warning(self):
        """Which is the whole point: a warning that can be answered."""
        self.post(rows=[("5.5", "0.93", "0.19")])
        self.assertFalse(self.checks()["TVA 5.5% cohérente"]["passed"])
        self.post(rows=[("5.5", "0.93", "0.05")])
        self.assertTrue(self.checks()["TVA 5.5% cohérente"]["passed"])

    def test_a_base_that_is_not_what_the_lines_come_to_says_so(self):
        self.post(rows=[("5.5", "5.00", "0.28")])
        check = self.checks()["Somme HT des lignes = base HT du ticket"]
        self.assertFalse(check["passed"])
        self.assertIn("lignes 0.93 € HT / document 5.00 € HT", check["detail"])

    def test_no_table_asks_nothing(self):
        """A document that prints none is not wrong, and a failure nobody
        asked for is the noise this page exists to avoid."""
        self.post(rows=[("", "", "")])
        labels = self.checks()
        self.assertNotIn("TVA 5.5% cohérente", labels)
        self.assertNotIn("Somme HT des lignes = base HT du ticket", labels)
        self.assertNotIn("Table TVA lue", labels)
        self.assertEqual(self.invoice.vat_breakdown, [])

    def test_half_a_row_is_refused_with_what_is_missing(self):
        response = self.post(rows=[("5.5", "0.93", "")])
        self.assertContains(response, "Un taux se saisit avec sa base HT et son montant de TVA.")
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.reviewed_at)

    def test_a_rate_taken_out_takes_its_check_with_it(self):
        self.post(rows=[("5.5", "0.93", "0.05"), ("20", "1.00", "0.20")])
        self.assertIn("TVA 20% cohérente", self.checks())
        self.post(rows=[("5.5", "0.93", "0.05")])
        self.assertNotIn("TVA 20% cohérente", self.checks())


HT_CHECK = "Somme HT des lignes = base HT du ticket"
AS_READ = HT_CHECK + ' <span class="muted">(à la lecture du ticket)</span>'
# A DIY store's ticket as read: each item is followed by the eco-participation
# included in its price ("Dt Ecopart. unit. EcoMob 0.60"), which the reading
# took for an item of its own. The table prints 35,00 € HT at 20 %, paid
# 42,00 €; the lines as read make 35,70 € HT.
DIY_READING = [
    ("CAISSE A OUTILS 50CM", 1, "25.00", "30.00"),
    ("Dt Ecopart. unit. EcoMob", 1, "0.50", "0.60"),
    ("BOITE DE RANGEMENT 10L", 3, "10.00", "12.00"),
    ("Dt Ecopart. unit. EcoMob", 1, "0.20", "0.24"),
]
DIY_CHECKS = [
    {"label": "Somme des lignes = total imprimé", "passed": False, "detail": "lignes 42.84 € / ticket 42.00 € (écart -0.84 €)"},
    {"label": "TVA 20% cohérente", "passed": True, "detail": "HT 35.00 € x 20% = 7.00 € / ticket 7.00 €"},
    {"label": HT_CHECK, "passed": False, "detail": "lignes 35.70 € HT / ticket 35.00 € HT (écart -0.70 €)"},
]


def diy_ticket(vat_breakdown=(("0.20", "35.00", "7.00"),)):
    shop = make_supplier(name="Brico Exemple")
    invoice = Invoice.objects.create(
        supplier=shop, invoice_number="055-0000001-001", invoice_date=date(2026, 8, 12), parse_checks=list(DIY_CHECKS),
        printed_total_ttc=D("42.00"), vat_breakdown=[list(row) for row in vat_breakdown],
    )
    products = {}
    for name, quantity, total_ht, printed in DIY_READING:
        if name not in products:
            products[name] = make_product(supplier=shop, raw_name=name)
        make_invoice_line(
            invoice=invoice, product=products[name], raw_name=name, read_as=name, quantity=quantity,
            total_ht=total_ht, vat_rate=D("0.20"), printed_ttc=D(printed),
        )
    return invoice


def with_vat_table(response, data: dict) -> dict:
    """The VAT table block the page posts beside its lines, as it drew it:
    `page_post` leaves it out, and a post without it keeps the stored table
    (a page cached before the block existed)."""
    vat_form = response.context["vat_form"]
    data = dict(data)
    data[f"{vat_form.prefix}-TOTAL_FORMS"] = str(vat_form.total_form_count())
    data[f"{vat_form.prefix}-INITIAL_FORMS"] = str(vat_form.initial_form_count())
    data[f"{vat_form.prefix}-MIN_NUM_FORMS"] = "0"
    data[f"{vat_form.prefix}-MAX_NUM_FORMS"] = "1000"
    for form in vat_form.forms:
        for name in form.fields:
            value = form[name].value()
            data[form.add_prefix(name)] = "" if value is None else str(value)
    return data


class CorrectedLinesHtCheckTests(TestCase):
    """"Somme HT des lignes = base HT du ticket" is worked out from the lines
    as they stand, never kept from the reading.

    Two DIY-store tickets had been read with the eco-participation under
    each item as an item of its own - their lines 0,77 € and 7,54 € HT off
    the base - and the owner corrected them by hand. Validating did rebuild
    the check from the lines as saved and the table typed
    (`recheck_after_review`), and it passed; but its label is one the reading
    writes too, and the page marked every check carrying it "(à la lecture du
    ticket)" - all 445 documents holding it on 19/09, the three whose reading
    failed it and whose corrected lines pass among them, and the three whose
    corrected lines still disagree, blamed on a reading nobody could answer.
    While the lines were being typed it did not move at all.
    """

    def setUp(self):
        self.invoice = diy_ticket()
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def correct(self, **changes):
        page = self.client.get(self.url)
        response = self.client.post(self.url, with_vat_table(page, page_post(page, **changes)))
        self.assertEqual(response.status_code, 302)
        self.invoice.refresh_from_db()
        return self.client.get(self.url)

    def stored(self):
        return next(check for check in self.invoice.parse_checks if check["label"] == HT_CHECK)

    def test_lines_corrected_to_the_base_pass_and_are_not_the_reading(self):
        # The two eco-participation lines taken out: 25,00 + 10,00 € HT.
        page = self.correct(**{"form-1-DELETE": "on", "form-3-DELETE": "on"})
        expected = {
            "label": HT_CHECK,
            "passed": True,
            "detail": "lignes 35.00 € HT / document 35.00 € HT (écart +0.00 €)",
        }
        self.assertEqual(self.stored(), expected)
        self.assertEqual(page.context.get("ht_check"), expected)
        self.assertContains(page, '<li class="check check-pass" id="live-ht-check"')
        self.assertNotContains(page, AS_READ)
        self.assertEqual(page.content.decode().count(HT_CHECK), 1)

    def test_lines_still_wrong_fail_on_the_lines_as_saved(self):
        # One eco-participation left in: 0,20 € HT over the base.
        page = self.correct(**{"form-1-DELETE": "on"})
        expected = {
            "label": HT_CHECK,
            "passed": False,
            "detail": "lignes 35.20 € HT / document 35.00 € HT (écart -0.20 €)",
        }
        self.assertEqual(self.stored(), expected)
        self.assertEqual(page.context.get("ht_check"), expected)
        self.assertContains(page, '<li class="check check-fail" id="live-ht-check"')
        self.assertNotContains(page, AS_READ)
        self.assertEqual(page.content.decode().count(HT_CHECK), 1)

    def test_before_any_correction_the_lines_as_they_stand_against_the_table(self):
        """The same figures the reading had, but worked out on the page from
        what it shows - which is what the script follows as they are typed."""
        page = self.client.get(self.url)
        self.assertEqual(
            page.context.get("ht_check"),
            {"label": HT_CHECK, "passed": False, "detail": "lignes 35.70 € HT / document 35.00 € HT (écart -0.70 €)"},
        )
        self.assertContains(page, '<li class="check check-fail" id="live-ht-check" data-tolerance="0.05"')
        self.assertNotContains(page, AS_READ)
        self.assertEqual(page.content.decode().count(HT_CHECK), 1)

    def test_with_no_table_the_readings_own_stays_marked_as_read(self):
        """Validating without a table drops the check, so one stored on a
        page that shows none can only be the reading's: said so, and set
        aside by the script once a table is typed."""
        self.invoice.vat_breakdown = []
        self.invoice.save(update_fields=["vat_breakdown"])
        page = self.client.get(self.url)
        self.assertIsNone(page.context.get("ht_check", "absent"))
        self.assertContains(page, '<li class="check check-fail" id="live-ht-check" data-tolerance="0.05" hidden>')
        self.assertContains(page, AS_READ)
        self.assertContains(page, "data-before-table")


class SupplierInvoiceHtCheckTests(TestCase):
    """The live HT check is a ticket's. A supplier's invoice never showed it
    and never stores it (recheck_after_review is a ticket's too) - and its
    lines leave out what the reconciliation adds (a duty,
    Invoice.reconciliation_adjustment) where the printed base counts it, so a
    table typed exactly as printed showed a failure, under a label saying
    « du ticket »."""

    def test_its_page_stays_as_it_was(self):
        supplier = make_supplier(name="Grossiste Exemple")
        invoice = Invoice.objects.create(
            supplier=supplier, invoice_number="F-2026-0815", invoice_date=date(2026, 8, 15),
            reconciliation_adjustment=D("0.34"), vat_breakdown=[["0.2", "20.34", "4.07"]],
        )
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=supplier, raw_name="SIROP ORGEAT 70CL"),
            quantity=2, total_ht="20.00", vat_rate=D("0.2"),
        )
        self.assertFalse(invoice.is_receipt)
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(page, "TVA imprimée sur le document")
        self.assertIsNone(page.context["ht_check"])
        self.assertNotContains(page, 'id="live-ht-check"')
        self.assertNotContains(page, HT_CHECK)
