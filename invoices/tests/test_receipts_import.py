"""The parts of receipt handling that need the database.

Shop detection, the price list that names a till's unnamed lines, and the
review screen that a person actually works through. The parsers themselves
are tested from hand-written pages elsewhere in this package.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import Product
from invoices.models import ShopItemPrice, Supplier, label_for_unit_price
from invoices.parsers.base import ParsedInvoice, ParsedLine
from invoices.receipts import PLACEHOLDER_MARKER, detect_parser, label_placeholder_lines
from tests.factories import make_invoice, make_invoice_line, make_product

FIVE_FIVE = Decimal("0.055")


class DetectParserTests(TestCase):
    def test_each_shop_is_recognised_from_its_own_header(self):
        for header, expected in (
            ("FRANPRIX\n12 RUE INVENTEE", "FRANPRIX"),
            ("MONOPRIX\nDimax", "MONOPRIX"),
            ("Sabbh Oriental\n12 Rue Inventee", "SABBH"),
            ("WING SENG\n2 RUE INVENTEE", "WINGSENG"),
        ):
            with self.subTest(shop=expected):
                parser = detect_parser(header)
                self.assertIsNotNone(parser)
                self.assertEqual(parser.supplier_code, expected)

    def test_the_alternate_spelling_of_sabbh_is_recognised(self):
        """The files come in as both "sabah" and "Sabbah"."""
        self.assertIsNotNone(detect_parser("Sabah Oriental"))

    def test_an_unknown_shop_is_not_guessed_at(self):
        """A Franprix ticket run through the Monoprix parser would produce
        lines, and they would be wrong."""
        self.assertIsNone(detect_parser("CARREFOUR CITY\n1 RUE AILLEURS"))
        self.assertIsNone(detect_parser(""))


class LabelForUnitPriceTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.get(code="SABBH")

    def test_an_undated_entry_is_the_standing_answer(self):
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Citron vert")
        self.assertEqual(label_for_unit_price(self.supplier, Decimal("0.70")), "Citron vert")

    def test_a_dated_entry_takes_over_from_its_date(self):
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Ancien")
        ShopItemPrice.objects.create(
            supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Nouveau",
            valid_from=date(2026, 6, 1),
        )
        self.assertEqual(label_for_unit_price(self.supplier, Decimal("0.70"), date(2026, 5, 1)), "Ancien")
        self.assertEqual(label_for_unit_price(self.supplier, Decimal("0.70"), date(2026, 7, 1)), "Nouveau")

    def test_a_nearby_price_never_answers(self):
        """A 0,70 mapping answering for a 0,75 line is how one product's
        costs quietly become another's."""
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Citron vert")
        self.assertEqual(label_for_unit_price(self.supplier, Decimal("0.75")), "")

    def test_another_shops_price_list_is_not_consulted(self):
        other = Supplier.objects.get(code="WINGSENG")
        ShopItemPrice.objects.create(supplier=other, unit_price_ttc=Decimal("0.70"), label="Menthe")
        self.assertEqual(label_for_unit_price(self.supplier, Decimal("0.70")), "")


class LabelPlaceholderLinesTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.get(code="SABBH")

    def _parsed(self, unit_ht, quantity=3):
        line = ParsedLine(
            raw_name="Article divers",
            quantity=quantity,
            total_volume=Decimal("0"),
            unit_cost_ht=unit_ht,
            total_ht=unit_ht * quantity,
            vat_rate=FIVE_FIVE,
            is_placeholder=True,
        )
        return ParsedInvoice(
            supplier_code="SABBH", invoice_number="1", invoice_date=date(2026, 7, 14), lines=[line]
        )

    def test_a_known_price_gets_its_name(self):
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Citron vert")
        parsed = self._parsed(Decimal("0.6633"))
        self.assertEqual(label_placeholder_lines(self.supplier, parsed), 1)
        self.assertEqual(parsed.lines[0].raw_name, "Citron vert")
        self.assertFalse(parsed.lines[0].is_placeholder)

    def test_the_stored_ht_unit_price_round_trips_to_the_printed_ttc_one(self):
        """The parser stores HT; the price list is keyed on the TTC figure
        printed on the ticket. The two have to meet in the middle to the
        cent, whatever the quantity was."""
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Citron vert")
        for unit_ht, quantity in ((Decimal("0.6636"), 11), (Decimal("0.6633"), 3), (Decimal("0.6643"), 7)):
            with self.subTest(quantity=quantity):
                parsed = self._parsed(unit_ht, quantity)
                self.assertEqual(label_placeholder_lines(self.supplier, parsed), 1)

    def test_an_unknown_price_is_named_by_its_price(self):
        """So the operator can record it without opening the photo."""
        parsed = self._parsed(Decimal("4.6450"))
        self.assertEqual(label_placeholder_lines(self.supplier, parsed), 0)
        self.assertIn("4.90", parsed.lines[0].raw_name)
        self.assertIn(PLACEHOLDER_MARKER, parsed.lines[0].raw_name)

    def test_a_named_line_is_left_alone(self):
        parsed = self._parsed(Decimal("0.6633"))
        parsed.lines[0].is_placeholder = False
        parsed.lines[0].raw_name = "PAIN COMPLET"
        self.assertEqual(label_placeholder_lines(self.supplier, parsed), 0)
        self.assertEqual(parsed.lines[0].raw_name, "PAIN COMPLET")


class ReceiptReviewViewTests(TestCase):
    """The screen a person works the queue through."""

    def setUp(self):
        self.supplier = Supplier.objects.get(code="SABBH")
        self.invoice = make_invoice(
            supplier=self.supplier,
            invoice_date=date(2026, 7, 14),
            parse_checks=[
                {"label": "Somme des lignes = total imprimé", "passed": False, "detail": "écart +0.49 €"},
                {"label": "TVA 5.5% cohérente", "passed": True, "detail": ""},
            ],
            ocr_text="Sabbh Oriental\nArticle divers\n3pcs  0,70  2,10A",
        )
        product = make_product(supplier=self.supplier, raw_name="Article divers (0.70 EUR/u)")
        make_invoice_line(
            invoice=self.invoice, product=product, quantity=3, total_ht="1.99",
            unit_cost_ht="0.6633", vat_rate=FIVE_FIVE, raw_name="Article divers (0.70 EUR/u)",
        )

    def test_the_queue_lists_an_unreviewed_receipt(self):
        response = self.client.get(reverse("invoices:receipt_queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sabbh")

    def test_a_reviewed_receipt_leaves_the_queue(self):
        self.invoice.reviewed_at = timezone.now()
        self.invoice.save(update_fields=["reviewed_at"])
        response = self.client.get(reverse("invoices:receipt_queue"))
        self.assertNotContains(response, str(self.invoice.pk) + "/verifier")

    def test_a_digital_invoice_never_enters_the_receipt_queue(self):
        """Only photographed receipts carry parse_checks; an ordinary
        invoice appearing here would be a number nothing accounts for."""
        make_invoice(supplier=Supplier.objects.get(code="METRO"))
        response = self.client.get(reverse("invoices:receipt_queue"))
        self.assertEqual(len(response.context["receipts"]), 1)

    def test_no_spare_blank_row_is_rendered_on_a_saved_receipt(self):
        """An empty row under the real ones reads as an item that went
        missing - on the one screen whose whole job is telling you whether
        an item went missing."""
        response = self.client.get(reverse("invoices:receipt_review", args=[self.invoice.pk]))
        self.assertEqual(len(response.context["formset"].forms), self.invoice.lines.count())

    def test_the_review_page_shows_the_checks(self):
        """The sum check is worked out on the lines as they stand (see
        test_review_checks); the others are what was read."""
        self.invoice.printed_total_ttc = Decimal("2.59")
        self.invoice.save(update_fields=["printed_total_ttc"])
        response = self.client.get(reverse("invoices:receipt_review", args=[self.invoice.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Somme des lignes")
        self.assertContains(response, "écart +0.49")
        self.assertContains(response, "TVA 5.5% cohérente")

    def test_saving_marks_the_receipt_reviewed_and_replaces_its_lines(self):
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "invoice_date": "2026-07-14",
                "form-0-product_name": "Citron vert",
                "form-0-quantity": "3",
                "form-0-total_ttc": "2.10",
                "form-0-vat_rate": "5.5",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.invoice.refresh_from_db()
        self.assertIsNotNone(self.invoice.reviewed_at)
        self.assertEqual([line.raw_name for line in self.invoice.lines.all()], ["Citron vert"])

    def test_the_rate_the_page_renders_is_a_rate_the_page_accepts(self):
        """vat_rate is stored to four decimals, so 5.5% renders as "5.5000"
        - and the form field allows two. Posting the page back exactly as
        it was rendered failed validation on every single receipt, with the
        error under a field the user never touched.
        """
        response = self.client.get(reverse("invoices:receipt_review", args=[self.invoice.pk]))
        rendered = response.context["formset"].forms[0].initial["vat_rate"]

        payload = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "invoice_date": "2026-07-14",
            "form-0-product_name": "Citron vert",
            "form-0-quantity": "3",
            "form-0-total_ttc": "2.10",
            "form-0-vat_rate": str(rendered),
        }
        saved = self.client.post(reverse("invoices:receipt_review", args=[self.invoice.pk]), payload)
        self.assertEqual(saved.status_code, 302, "the rendered VAT rate was rejected on submit")
        self.invoice.refresh_from_db()
        self.assertIsNotNone(self.invoice.reviewed_at)

    def _payload(self, **extra):
        return {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "invoice_date": "2026-07-14",
            "form-0-product_name": "Citron vert",
            "form-0-quantity": "3",
            "form-0-total_ttc": "2.10",
            "form-0-vat_rate": "5.5",
            **extra,
        }

    def test_the_review_screen_shows_the_date_to_correct(self):
        response = self.client.get(reverse("invoices:receipt_review", args=[self.invoice.pk]))
        self.assertContains(response, 'name="invoice_date"')
        self.assertContains(response, 'value="2026-07-14"')

    def test_the_date_is_set_on_the_review_screen(self):
        """A ticket typed in from its photo, or one whose date the OCR
        missed, gets its date where it is checked."""
        self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]), self._payload(invoice_date="2024-08-13")
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.invoice_date, date(2024, 8, 13))
        self.assertIsNotNone(self.invoice.reviewed_at)

    def test_a_ticket_is_not_saved_without_a_date(self):
        """A document with no date sits outside every stock valuation and the
        bank match: the page asks for one rather than keeping none."""
        self.invoice.invoice_date = None
        self.invoice.save(update_fields=["invoice_date"])
        for posted in ({"invoice_date": ""}, {}):
            with self.subTest(posted=posted):
                payload = self._payload()
                payload.pop("invoice_date")
                payload.update(posted)
                response = self.client.post(reverse("invoices:receipt_review", args=[self.invoice.pk]), payload)
                self.assertContains(response, "Saisissez la date du document.")
                self.invoice.refresh_from_db()
                self.assertIsNone(self.invoice.reviewed_at)

    def test_an_impossible_date_is_refused_on_the_page(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        for posted in ("2024-13-45", "1999-12-31", tomorrow.isoformat()):
            with self.subTest(posted=posted):
                response = self.client.post(
                    reverse("invoices:receipt_review", args=[self.invoice.pk]), self._payload(invoice_date=posted)
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "message-error")
                self.invoice.refresh_from_db()
                self.assertIsNone(self.invoice.reviewed_at)
                self.assertEqual(self.invoice.invoice_date, date(2026, 7, 14))

    def test_today_is_a_date(self):
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            self._payload(invoice_date=timezone.localdate().isoformat()),
        )
        self.assertEqual(response.status_code, 302)

    def test_a_row_removed_in_the_browser_leaves_a_gap_in_the_indices(self):
        """Removing a row client-side does not renumber the others, so the
        POST arrives with 0 and 2 and no 1 at all. Django validates the
        missing row anyway unless the formset tolerates it - and it fails
        "required" where nobody can see or fix it."""
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {
                "form-TOTAL_FORMS": "3",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "invoice_date": "2026-07-14",
                "form-0-product_name": "Citron vert",
                "form-0-quantity": "3",
                "form-0-total_ttc": "2.10",
                "form-0-vat_rate": "5.5",
                "form-2-product_name": "Menthe",
                "form-2-quantity": "2",
                "form-2-total_ttc": "1.00",
                "form-2-vat_rate": "5.5",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(line.raw_name for line in self.invoice.lines.all()), ["Citron vert", "Menthe"]
        )

    def test_a_row_left_at_its_prefilled_vat_rate_does_not_block_the_save(self):
        """The VAT field carries an initial, so an otherwise-empty trailing
        row still posts a value and looks filled in."""
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "invoice_date": "2026-07-14",
                "form-0-product_name": "Citron vert",
                "form-0-quantity": "3",
                "form-0-total_ttc": "2.10",
                "form-0-vat_rate": "5.5",
                "form-1-product_name": "",
                "form-1-quantity": "",
                "form-1-total_ttc": "",
                "form-1-vat_rate": "20",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.invoice.lines.count(), 1)

    def test_remembering_a_price_names_this_invoices_matching_lines(self):
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {"action": "remember_price", "unit_price_ttc": "0.70", "label": "Citron vert", "valid_from": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ShopItemPrice.objects.filter(supplier=self.supplier, unit_price_ttc=Decimal("0.70")).exists()
        )
        self.assertEqual([line.raw_name for line in self.invoice.lines.all()], ["Citron vert"])

    def test_a_price_already_known_is_refused_on_the_page(self):
        """The shop is not a form field, so Django never checked the
        uniqueness it is part of: the second 0,70 reached the database and
        came back as a server error."""
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Pain Pita")
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {"action": "remember_price", "unit_price_ttc": "0.70", "label": "Citron vert", "valid_from": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0.70 € est déjà retenu chez Sabbh Oriental : « Pain Pita »")
        self.assertEqual(ShopItemPrice.objects.count(), 1)

    def test_the_same_price_from_a_new_date_is_accepted(self):
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.70"), label="Pain Pita")
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {"action": "remember_price", "unit_price_ttc": "0.70", "label": "Citron vert", "valid_from": "2026-07-01"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ShopItemPrice.objects.count(), 2)
        duplicate = self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {"action": "remember_price", "unit_price_ttc": "0.70", "label": "Menthe", "valid_from": "2026-07-01"},
        )
        self.assertContains(duplicate, "à partir du 01/07/2026")

    def test_a_date_is_not_kept_when_the_lines_fail_to_save(self):
        with mock.patch("invoices.views.replace_invoice_lines", side_effect=RuntimeError("disque plein")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("invoices:receipt_review", args=[self.invoice.pk]),
                    self._payload(invoice_date="2024-08-13"),
                )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.invoice_date, date(2026, 7, 14))
        self.assertIsNone(self.invoice.reviewed_at)

    def test_the_queue_does_not_query_once_per_receipt(self):
        url = reverse("invoices:receipt_queue")
        self.client.get(url)  # warm up the session
        with_one = self._queue_queries(url)
        for _ in range(3):
            receipt = make_invoice(supplier=self.supplier, parse_checks=[{"label": "x", "passed": True}])
            make_invoice_line(invoice=receipt, vat_rate=FIVE_FIVE)
        self.assertEqual(self._queue_queries(url), with_one)

    def _queue_queries(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        return len(captured)

    def test_remembering_a_price_does_not_mark_the_receipt_reviewed(self):
        """Recording a price is not the same as having checked the ticket."""
        self.client.post(
            reverse("invoices:receipt_review", args=[self.invoice.pk]),
            {"action": "remember_price", "unit_price_ttc": "0.70", "label": "Citron vert", "valid_from": ""},
        )
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.reviewed_at)


class RememberPriceAcrossQueueTests(TestCase):
    """A price recorded on one ticket names the same "Article divers" line on
    every ticket of that shop still waiting to be checked - one answer, not
    one per ticket."""

    def setUp(self):
        self.supplier = Supplier.objects.get(code="SABBH")
        self.current = self._receipt(date(2026, 7, 14))

    def _receipt(self, on, supplier=None, reviewed=False, unit_ht="0.6633", name=None):
        supplier = supplier or self.supplier
        invoice = make_invoice(
            supplier=supplier,
            invoice_date=on,
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
            reviewed_at=timezone.now() if reviewed else None,
        )
        unit_ttc = (Decimal(unit_ht) * (1 + FIVE_FIVE)).quantize(Decimal("0.01"))
        name = name or f"Article divers ({unit_ttc} EUR/u)"
        # One product per name and shop, as the import leaves it.
        product = Product.objects.filter(supplier=supplier, raw_name=name).first()
        make_invoice_line(
            invoice=invoice, product=product or make_product(supplier=supplier, raw_name=name), quantity=3,
            total_ht=str((Decimal(unit_ht) * 3).quantize(Decimal("0.01"))), unit_cost_ht=unit_ht,
            vat_rate=FIVE_FIVE, raw_name=name,
        )
        return invoice

    def _remember(self, price="0.70", label="Citron vert", valid_from=""):
        return self.client.post(
            reverse("invoices:receipt_review", args=[self.current.pk]),
            {"action": "remember_price", "unit_price_ttc": price, "label": label, "valid_from": valid_from},
            follow=True,
        )

    @staticmethod
    def _names(invoice):
        return [line.raw_name for line in invoice.lines.all()]

    def test_the_price_names_the_same_line_on_every_ticket_still_to_check(self):
        others = [self._receipt(date(2026, 7, 2)), self._receipt(date(2026, 7, 20))]
        response = self._remember()
        for invoice in [self.current, *others]:
            with self.subTest(invoice=invoice.pk):
                self.assertEqual(self._names(invoice), ["Citron vert"])
        self.assertContains(response, "3 ligne(s) renommée(s) sur 3 ticket(s)")

    def test_a_checked_ticket_keeps_what_was_confirmed(self):
        """A checked ticket says what a person confirmed it bought."""
        checked = self._receipt(date(2026, 7, 2), reviewed=True)
        self._remember()
        self.assertEqual(self._names(checked), ["Article divers (0.70 EUR/u)"])

    def test_the_ticket_the_price_was_recorded_on_is_named_even_once_checked(self):
        """Opened again through "Corriger les lignes": naming its line is
        what the person is doing on that very screen."""
        self.current.reviewed_at = timezone.now()
        self.current.save(update_fields=["reviewed_at"])
        self._remember()
        self.assertEqual(self._names(self.current), ["Citron vert"])

    def test_another_shops_tickets_are_left_alone(self):
        other_shop = self._receipt(date(2026, 7, 2), supplier=Supplier.objects.get(code="WINGSENG"))
        self._remember()
        self.assertEqual(self._names(other_shop), ["Article divers (0.70 EUR/u)"])

    def test_a_line_at_another_price_is_left_alone(self):
        dearer = self._receipt(date(2026, 7, 2), unit_ht="0.7109")  # 0,75 TTC
        self._remember()
        self.assertEqual(self._names(dearer), ["Article divers (0.75 EUR/u)"])

    def test_a_named_line_at_the_same_price_is_left_alone(self):
        named = self._receipt(date(2026, 7, 2), name="MENTHE FRAICHE")
        self._remember()
        self.assertEqual(self._names(named), ["MENTHE FRAICHE"])

    def test_a_dated_price_leaves_older_tickets_alone(self):
        """"À partir du" means the price did not name anything before it."""
        before = self._receipt(date(2026, 6, 20))
        after = self._receipt(date(2026, 7, 20))
        response = self._remember(valid_from="2026-07-01")
        self.assertEqual(self._names(before), ["Article divers (0.70 EUR/u)"])
        self.assertEqual(self._names(after), ["Citron vert"])
        self.assertEqual(self._names(self.current), ["Citron vert"])
        self.assertContains(response, "2 ligne(s) renommée(s) sur 2 ticket(s)")

    def test_a_price_recorded_earlier_names_the_tickets_imported_since(self):
        """A ticket imported before its price was known is caught up too, so
        the queue never holds a line the price list already answers."""
        waiting = self._receipt(date(2026, 7, 2), unit_ht="0.4739")  # 0,50 TTC
        ShopItemPrice.objects.create(supplier=self.supplier, unit_price_ttc=Decimal("0.50"), label="Menthe")
        self._remember()
        self.assertEqual(self._names(waiting), ["Menthe"])

    def test_saying_so_when_nothing_was_renamed(self):
        response = self._remember(price="1.20", label="Coriandre")
        self.assertContains(response, "Prix retenu : 1.20 € = Coriandre")
        self.assertContains(response, "aucune ligne à renommer")
        self.assertEqual(self._names(self.current), ["Article divers (0.70 EUR/u)"])


class InvoiceReceiptPropertyTests(TestCase):
    def test_a_receipt_with_a_failed_check_needs_review(self):
        invoice = make_invoice(parse_checks=[{"label": "x", "passed": False, "detail": ""}])
        self.assertTrue(invoice.is_receipt)
        self.assertFalse(invoice.receipt_verified)
        self.assertTrue(invoice.needs_receipt_review)

    def test_a_receipt_whose_checks_all_passed_is_verified(self):
        invoice = make_invoice(parse_checks=[{"label": "x", "passed": True, "detail": ""}])
        self.assertTrue(invoice.receipt_verified)
        self.assertFalse(invoice.needs_receipt_review)

    def test_confidence_is_shown_as_a_percentage(self):
        """Stored as a fraction; rendered straight through floatformat it
        made every receipt read "1%"."""
        invoice = make_invoice(ocr_confidence=Decimal("0.69"))
        self.assertEqual(invoice.ocr_confidence_percent, Decimal("69.00"))

    def test_confidence_is_none_for_a_digital_invoice(self):
        self.assertIsNone(make_invoice().ocr_confidence_percent)

    def test_a_digital_invoice_is_not_a_receipt(self):
        invoice = make_invoice()
        self.assertFalse(invoice.is_receipt)
        self.assertFalse(invoice.needs_receipt_review)
        self.assertFalse(invoice.receipt_verified)

    def test_a_reviewed_receipt_no_longer_needs_review(self):
        invoice = make_invoice(
            parse_checks=[{"label": "x", "passed": False, "detail": ""}], reviewed_at=timezone.now()
        )
        self.assertFalse(invoice.needs_receipt_review)
