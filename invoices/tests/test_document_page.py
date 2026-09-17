"""The correction page, one for tickets and supplier invoices.

It takes the document's date (required, between 2000 and today), its total and
each line - the amount HT and TTC side by side, a ticket's promotion beside
its printed price, the weight - and reads the document again on request, puts
a checked ticket back in the queue, and forgets a price the shop's list got
wrong.

OCR never runs here: `receipts.recognise` is replaced. Data invented.
"""

import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import StockTakeLineSource, UnitChoices
from invoices.forms import DOCUMENT_RECEIPT, LineCorrectionForm, ManualInvoiceForm
from invoices.importing import parse_and_import, replace_invoice_lines
from invoices.models import Invoice, ShopItemPrice, Supplier
from invoices.parsers.base import ParsedInvoice, ParsedLine
from invoices.receipts import import_receipt, pending_receipts
from invoices.tests.page_posts import page_post
from invoices.tests.test_parser_sabbh import BASIC
from invoices.tests.test_receipt_shop_choice import recognised
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_supplier,
)

D = Decimal
FIVE_FIVE = D("0.055")
CHECKED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def scratch_file(test, name):
    """A file outside the project, gone after the test."""
    folder = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(b"%PDF-1.4 " + name.encode())
    return path


def typed_line(name, total_ht, printed_ttc):
    return ParsedLine(
        raw_name=name, quantity=1, total_volume=D("0"), unit_cost_ht=D(total_ht), total_ht=D(total_ht),
        vat_rate=FIVE_FIVE, printed_ttc=D(printed_ttc),
    )


class RereadTicketTests(TestCase):
    """"Relire le document": the photo read again, the corrections gone, the
    ticket back in the queue."""

    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        path = scratch_file(self, "ticket-sabbh.pdf")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(BASIC)):
            self.ticket = import_receipt(path)
        # Checked since, and corrected by hand: one line, another date, another total.
        replace_invoice_lines(self.ticket, [typed_line("Pain Pita", "9.48", "10.00")])
        self.ticket.invoice_date = date(2026, 7, 1)
        self.ticket.printed_total_ttc = D("10.00")
        self.ticket.reviewed_at = timezone.now()
        self.ticket.save()
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def reread(self, text=BASIC):
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)):
            return self.client.post(self.url, {"action": "reread"})

    def test_the_page_offers_it(self):
        self.assertContains(self.client.get(self.url), 'name="action" value="reread"')

    def test_reading_again_replaces_the_corrections_and_sends_it_back_to_check(self):
        response = self.reread()
        self.assertRedirects(response, self.url)
        self.ticket.refresh_from_db()
        self.assertEqual(sorted(line.quantity for line in self.ticket.lines.all()), [2, 3])
        self.assertEqual((self.ticket.invoice_date, self.ticket.printed_total_ttc), (date(2026, 7, 14), D("11.40")))
        self.assertIsNone(self.ticket.reviewed_at)
        self.assertIn(self.ticket, pending_receipts())
        self.assertTrue(any("Ticket relu" in message for message in messages_of(response)))

    def test_a_reading_with_nothing_on_it_changes_nothing(self):
        response = self.reread("illisible")
        self.assertTrue(any("aucune ligne" in message for message in messages_of(response)))
        self.ticket.refresh_from_db()
        self.assertEqual([line.raw_name for line in self.ticket.lines.all()], ["Pain Pita"])
        self.assertIsNotNone(self.ticket.reviewed_at)

    def test_a_file_gone_changes_nothing(self):
        os.remove(self.ticket.source_file.path)
        response = self.reread()
        self.assertTrue(any("introuvable" in message for message in messages_of(response)))
        self.assertEqual(self.ticket.lines.count(), 1)

    def test_a_line_a_stock_take_was_priced_from_stops_it(self):
        line = self.ticket.lines.get()
        count = make_stock_take_line(
            stock_take=make_stock_take(taken_at=datetime(2026, 7, 20, 21, 0)), product=line.product,
            unit=UnitChoices.UNIT,
        )
        StockTakeLineSource.objects.create(
            stock_take_line=count, invoice_line=line, quantity_used=D("1"), unit_cost_ht=D("0.95")
        )
        response = self.reread()
        self.assertTrue(any("20/07/2026" in message for message in messages_of(response)), messages_of(response))
        self.assertEqual([saved.pk for saved in self.ticket.lines.all()], [line.pk])

    def test_any_suppliers_paper_ticket_is_read_again(self):
        paper = make_invoice(supplier=Supplier.objects.get(code="METRO"), parse_checks=CHECKED)
        paper.source_file.save("ticket-metro.pdf", ContentFile(b"%PDF-1.4"), save=True)
        url = reverse("invoices:receipt_review", args=[paper.pk])
        self.assertContains(self.client.get(url), 'value="reread"')
        with mock.patch("invoices.receipts.recognise", return_value=recognised(BASIC)):
            self.client.post(url, {"action": "reread"})
        self.assertEqual(sorted(line.quantity for line in paper.lines.all()), [2, 3])

    def test_the_ai_pseudo_supplier_reads_nothing(self):
        paper = make_invoice(supplier=Supplier.objects.get(code="OTHER"), parse_checks=CHECKED)
        paper.source_file.save("ticket-autre.pdf", ContentFile(b"%PDF-1.4"), save=True)
        url = reverse("invoices:receipt_review", args=[paper.pk])
        self.assertNotContains(self.client.get(url), 'value="reread"')
        response = self.client.post(url, {"action": "reread"})
        self.assertTrue(any("pas lus automatiquement" in message for message in messages_of(response)))


class RereadInvoiceTests(TestCase):
    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        self.invoice = make_invoice(supplier=self.metro, invoice_date=date(2026, 5, 2))
        make_invoice_line(invoice=self.invoice, product=make_product(supplier=self.metro), total_ht="1.00")
        self.invoice.source_file.save("facture-metro.pdf", ContentFile(b"%PDF-1.4"), save=True)
        self.url = reverse("invoices:invoice_edit_lines", args=[self.invoice.pk])

    def test_the_pdf_is_parsed_again(self):
        parsed = ParsedInvoice(
            supplier_code="METRO", invoice_number="X", invoice_date=date(2026, 5, 3),
            lines=[ParsedLine(raw_name="VODKA EXEMPLE", quantity=6, total_volume=D("4.2"), unit_cost_ht=D("10"),
                              total_ht=D("60.00"), vat_rate=D("0.20"))],
        )
        with mock.patch("invoices.parsers.metro.MetroParser.parse", return_value=parsed):
            response = self.client.post(self.url, {"action": "reread"})
        self.assertRedirects(response, self.url)
        self.invoice.refresh_from_db()
        self.assertEqual([(line.raw_name, line.total_ht) for line in self.invoice.lines.all()], [("VODKA EXEMPLE", D("60.00"))])
        self.assertEqual(self.invoice.invoice_date, date(2026, 5, 3))

    def test_no_total_typed_is_not_a_failure(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'class="check check-neutral" id="live-lines-check"')
        self.invoice.printed_total_ttc = D("9.99")
        self.invoice.save(update_fields=["printed_total_ttc"])
        self.assertContains(self.client.get(self.url), 'class="check check-fail" id="live-lines-check"')

    def test_a_supplier_with_no_parser_offers_nothing(self):
        invoice = make_invoice(supplier=make_supplier(code="SANS", name="Sans parseur"))
        invoice.source_file.save("facture.pdf", ContentFile(b"%PDF-1.4"), save=True)
        self.assertNotContains(self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk])), 'value="reread"')


class UnverifyTests(TestCase):
    def setUp(self):
        shop = Supplier.objects.get(code="FRANPRIX")
        self.ticket = make_invoice(supplier=shop, parse_checks=CHECKED, reviewed_at=timezone.now())
        make_invoice_line(invoice=self.ticket, product=make_product(supplier=shop), total_ht="0.46",
                          vat_rate=FIVE_FIVE, printed_ttc=D("0.49"))
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def test_a_checked_ticket_goes_back_to_the_queue(self):
        self.assertContains(self.client.get(self.url), 'value="unverify"')
        self.assertNotIn(self.ticket, pending_receipts())
        self.assertRedirects(self.client.post(self.url, {"action": "unverify"}), self.url)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.reviewed_at)
        self.assertIn(self.ticket, pending_receipts())
        self.assertNotContains(self.client.get(self.url), 'value="unverify"')

    def test_a_checked_ticket_corrected_goes_back_to_its_page(self):
        """Not to the next ticket of the queue: it was opened from its page."""
        response = self.client.post(self.url, page_post(self.client.get(self.url)))
        self.assertRedirects(response, reverse("invoices:invoice_detail", args=[self.ticket.pk]))


class ForgetPriceTests(TestCase):
    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.ticket = make_invoice(supplier=self.sabbh, parse_checks=CHECKED)
        make_invoice_line(invoice=self.ticket, product=make_product(supplier=self.sabbh), raw_name="Pain Pita",
                          quantity=10, total_ht="6.64", vat_rate=FIVE_FIVE, printed_ttc=D("7.00"))
        self.price = ShopItemPrice.objects.create(supplier=self.sabbh, unit_price_ttc=D("0.70"), label="Pain Pita")
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def test_each_known_price_can_be_forgotten(self):
        self.assertContains(self.client.get(self.url), f'name="price" value="{self.price.pk}"')

    def test_forgetting_a_price_leaves_the_lines_it_named(self):
        response = self.client.post(self.url, {"action": "forget_price", "price": self.price.pk})
        self.assertRedirects(response, self.url)
        self.assertFalse(ShopItemPrice.objects.exists())
        self.assertEqual(self.ticket.lines.get().raw_name, "Pain Pita")
        self.assertTrue(any("Prix oublié : 0.70 € = Pain Pita" in message for message in messages_of(response)))

    def test_a_price_that_is_not_a_number_is_refused_quietly(self):
        for posted in ("abc", "", "1.5"):
            with self.subTest(posted=posted):
                response = self.client.post(self.url, {"action": "forget_price", "price": posted})
                self.assertRedirects(response, self.url)
        self.assertTrue(ShopItemPrice.objects.filter(pk=self.price.pk).exists())

    def test_another_shops_price_is_not_forgotten_from_here(self):
        other = ShopItemPrice.objects.create(
            supplier=Supplier.objects.get(code="FRANPRIX"), unit_price_ttc=D("0.70"), label="Autre"
        )
        response = self.client.post(self.url, {"action": "forget_price", "price": other.pk})
        self.assertTrue(ShopItemPrice.objects.filter(pk=other.pk).exists())
        self.assertTrue(any("pas (ou plus) connu" in message for message in messages_of(response)))


class PromotionOnThePageTests(TestCase):
    """A loaf printed 0,49 with 0,17 of a "3 pour 2", beside a lemon at 1,80."""

    def setUp(self):
        shop = Supplier.objects.get(code="FRANPRIX")
        self.ticket = make_invoice(supplier=shop, parse_checks=CHECKED, printed_total_ttc=D("2.12"))
        self.loaf = make_invoice_line(
            invoice=self.ticket, product=make_product(supplier=shop, raw_name="PAIN"), raw_name="PAIN",
            total_ht="0.30", discount="0.16", vat_rate=FIVE_FIVE, printed_ttc=D("0.49"), discount_ttc=D("0.17"),
        )
        make_invoice_line(
            invoice=self.ticket, product=make_product(supplier=shop, raw_name="CITRON"), raw_name="CITRON",
            total_ht="1.71", vat_rate=FIVE_FIVE, printed_ttc=D("1.80"),
        )
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def test_the_price_and_the_promotion_are_shown_apart(self):
        response = self.client.get(self.url)
        loaf = response.context["formset"].forms[0].initial
        self.assertEqual((loaf["total_ttc"], loaf["total_ht"], loaf["discount_ttc"]), (D("0.49"), D("0.46"), D("0.17")))
        self.assertIn("articles 2.29 € moins 0.17 € de remises", response.context["live_check"]["detail"])
        self.assertTrue(response.context["live_check"]["passed"])

    def test_saved_untouched_nothing_moves(self):
        self.assertEqual(self.client.post(self.url, page_post(self.client.get(self.url))).status_code, 302)
        self.loaf.refresh_from_db()
        self.assertEqual(
            (self.loaf.total_ht, self.loaf.discount, self.loaf.printed_ttc, self.loaf.discount_ttc),
            (D("0.30"), D("0.16"), D("0.49"), D("0.17")),
        )

    def test_a_promotion_typed_comes_off_the_cost(self):
        self.client.post(self.url, page_post(self.client.get(self.url), **{"form-0-discount_ttc": "0.20"}))
        self.loaf.refresh_from_db()
        self.assertEqual(
            (self.loaf.total_ht, self.loaf.discount, self.loaf.printed_ttc, self.loaf.discount_ttc),
            (D("0.27"), D("0.19"), D("0.49"), D("0.20")),
        )
        self.assertEqual(self.loaf.total_ttc, D("0.29"))

    def test_a_promotion_removed_gives_the_line_its_price_back(self):
        self.client.post(self.url, page_post(self.client.get(self.url), **{"form-0-discount_ttc": ""}))
        self.loaf.refresh_from_db()
        self.assertEqual((self.loaf.total_ht, self.loaf.discount, self.loaf.discount_ttc), (D("0.46"), D("0"), D("0")))


class RemovedLinesTests(TestCase):
    """A line taken out stays on the page, struck through, until saved - and
    a page sent back with an error shows it that way still."""

    def test_a_removed_line_comes_back_removed(self):
        shop = Supplier.objects.get(code="FRANPRIX")
        ticket = make_invoice(supplier=shop, parse_checks=CHECKED)
        make_invoice_line(invoice=ticket, product=make_product(supplier=shop), total_ht="1.00", vat_rate=FIVE_FIVE)
        make_invoice_line(invoice=ticket, product=make_product(supplier=shop), total_ht="2.00", vat_rate=FIVE_FIVE)
        url = reverse("invoices:receipt_review", args=[ticket.pk])
        page = self.client.get(url)
        self.assertNotContains(page, "invoice-line-row is-removed")
        response = self.client.post(url, page_post(page, invoice_date="", **{"form-0-DELETE": "on"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="invoice-line-row is-removed"', count=1)
        self.assertContains(response, 'id="line-count"')


class UnitPriceTests(TestCase):
    """Five loaves printed 2.45 with 0.50 off, beside a lemon at 1.80: a
    wrong count shows once the line is written as a price each."""

    def setUp(self):
        shop = Supplier.objects.get(code="FRANPRIX")
        self.ticket = make_invoice(supplier=shop, parse_checks=CHECKED, printed_total_ttc=D("3.75"))
        make_invoice_line(
            invoice=self.ticket, product=make_product(supplier=shop, raw_name="BAGUETTE"), raw_name="BAGUETTE",
            quantity=5, total_ht="1.85", discount="0.47", vat_rate=FIVE_FIVE, printed_ttc=D("2.45"),
            discount_ttc=D("0.50"),
        )
        make_invoice_line(
            invoice=self.ticket, product=make_product(supplier=shop, raw_name="CITRON"), raw_name="CITRON",
            total_ht="1.71", vat_rate=FIVE_FIVE, printed_ttc=D("1.80"),
        )
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def test_several_of_a_kind_show_their_price_each(self):
        response = self.client.get(self.url)
        loaves, lemon = response.context["formset"].forms
        self.assertEqual(loaves.unit_price_hint, "soit 0.39 € TTC l'unité après remise (0.49 € avant)")
        self.assertEqual(lemon.unit_price_hint, "")
        self.assertContains(response, "soit 0.39 € TTC l&#x27;unité après remise (0.49 € avant)")

    def test_the_hint_follows_what_was_typed(self):
        response = self.client.post(
            self.url, page_post(self.client.get(self.url), invoice_date="", **{"form-0-quantity": "4"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["formset"].forms[0].unit_price_hint,
            "soit 0.49 € TTC l'unité après remise (0.61 € avant)",
        )

    def test_a_row_added_on_the_page_has_room_for_it(self):
        response = self.client.get(self.url)
        template = response.content.decode().split('id="empty-line-row"')[1]
        self.assertIn("data-unit-price", template)

    def test_nothing_to_divide_says_nothing(self):
        form = LineCorrectionForm(
            document=DOCUMENT_RECEIPT,
            data={"product_name": "X", "quantity": "3", "total_ttc": "abc", "vat_rate": "5.5"},
        )
        self.assertEqual(form.unit_price_hint, "")

    def test_a_refund_of_several_is_a_price_each(self):
        form = LineCorrectionForm(
            document=DOCUMENT_RECEIPT,
            data={"product_name": "X", "quantity": "-2", "total_ttc": "-3,00", "vat_rate": "5.5"},
        )
        self.assertEqual(form.unit_price_hint, "soit 1.50 € TTC l'unité")

    def test_an_invoice_line_is_a_cost_each_before_tax(self):
        invoice = make_invoice(supplier=Supplier.objects.get(code="METRO"))
        make_invoice_line(invoice=invoice, quantity=6, total_ht="12.00", vat_rate=D("0.2"))
        response = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertEqual(response.context["formset"].forms[0].unit_price_hint, "soit 2.00 € HT l'unité")


class WeightOnThePageTests(TestCase):
    def test_a_weight_can_be_corrected(self):
        shop = Supplier.objects.get(code="WINGSENG")
        ticket = make_invoice(supplier=shop, parse_checks=CHECKED)
        make_invoice_line(invoice=ticket, product=make_product(supplier=shop), total_volume="4.184",
                          total_ht="11.86", vat_rate=FIVE_FIVE, printed_ttc=D("12.51"))
        url = reverse("invoices:receipt_review", args=[ticket.pk])
        response = self.client.get(url)
        self.assertContains(response, 'value="4.184"')
        self.client.post(url, page_post(response, **{"form-0-total_volume": "4.2"}))
        line = ticket.lines.get()
        self.assertEqual((line.total_volume, line.printed_ttc), (D("4.200"), D("12.51")))

    def test_a_round_weight_is_not_written_in_powers_of_ten(self):
        shop = Supplier.objects.get(code="WINGSENG")
        ticket = make_invoice(supplier=shop, parse_checks=CHECKED)
        make_invoice_line(invoice=ticket, product=make_product(supplier=shop), total_volume="10.000",
                          total_ht="11.86", vat_rate=FIVE_FIVE, printed_ttc=D("12.51"))
        response = self.client.get(reverse("invoices:receipt_review", args=[ticket.pk]))
        self.assertEqual(response.context["formset"].forms[0].initial["total_volume"], D("10"))
        self.assertNotContains(response, "1E+1")


class DetailPageTests(TestCase):
    def test_a_ticket_line_shows_its_price_its_promotion_and_its_ttc(self):
        shop = Supplier.objects.get(code="FRANPRIX")
        ticket = make_invoice(supplier=shop, parse_checks=CHECKED, reconciliation_adjustment=D("0.01"))
        make_invoice_line(invoice=ticket, product=make_product(supplier=shop), total_ht="0.30", discount="0.16",
                          vat_rate=FIVE_FIVE, printed_ttc=D("0.49"), discount_ttc=D("0.17"))
        # Typed again by hand before promotions were kept apart: its HT
        # discount no longer says anything.
        make_invoice_line(invoice=ticket, product=make_product(supplier=shop), total_ht="0.46", discount="0.47",
                          vat_rate=FIVE_FIVE, printed_ttc=D("0.49"))
        response = self.client.get(reverse("invoices:invoice_detail", args=[ticket.pk]))
        self.assertContains(response, "0.49 € TTC imprimé, − 0.17 € de remise")
        self.assertNotContains(response, "0.47 € HT de remise")
        self.assertContains(response, "<th class=\"num\">Total TTC</th>", html=True)
        self.assertContains(response, "Arrondi")
        self.assertNotContains(response, "accise")

    def test_an_invoice_keeps_its_duty_explanation(self):
        invoice = make_invoice(supplier=Supplier.objects.get(code="METRO"), reconciliation_adjustment=D("1.20"))
        make_invoice_line(invoice=invoice, discount="2.00")
        response = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "accise")
        self.assertContains(response, "2.00 € HT de remise")


class DefaultRateTests(TestCase):
    def test_a_line_added_to_a_ticket_is_food_and_to_an_invoice_is_not(self):
        ticket = make_invoice(supplier=Supplier.objects.get(code="SABBH"), parse_checks=CHECKED)
        invoice = make_invoice(supplier=Supplier.objects.get(code="METRO"))
        for document, url, rate in (
            (ticket, reverse("invoices:receipt_review", args=[ticket.pk]), D("5.5")),
            (invoice, reverse("invoices:invoice_edit_lines", args=[invoice.pk]), D("20")),
        ):
            with self.subTest(document=document):
                empty = self.client.get(url).context["formset"].empty_form
                self.assertEqual(empty["vat_rate"].value(), rate)


class DatesTests(TestCase):
    def test_a_ticket_with_no_date_read_is_sent_to_check(self):
        path = scratch_file(self, "ticket-sans-date.pdf")
        for text, detail in (
            (BASIC.replace("14-07-2026 14:49:26", "illisible"), "Aucune date lisible"),
            (BASIC.replace("14-07-2026", f"14-07-{timezone.localdate().year + 1}"), "Date lue impossible"),
        ):
            with self.subTest(detail=detail):
                Invoice.objects.all().delete()
                with mock.patch("invoices.receipts.recognise", return_value=recognised(text)):
                    ticket = import_receipt(path)
                failed = {check["label"]: check["detail"] for check in ticket.parse_checks if not check["passed"]}
                self.assertIn(detail, failed["Date du ticket"])
                self.assertIn(ticket, pending_receipts())

    def test_a_pdf_with_no_date_says_so(self):
        path = scratch_file(self, "facture-sans-date.pdf")
        invoice = parse_and_import(path, make_supplier(code="SANSDATE", parser_key=""))
        self.assertIn("Date introuvable", invoice.error_message)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)

    def test_a_hand_typed_invoice_is_dated_in_the_past(self):
        supplier = make_supplier(code="MAIN")
        for day, valid in (
            (timezone.localdate(), True),
            (timezone.localdate() + timedelta(days=1), False),
            (date(1999, 12, 31), False),
        ):
            with self.subTest(day=day):
                form = ManualInvoiceForm(data={"supplier": supplier.pk, "invoice_number": "", "invoice_date": day})
                self.assertEqual(form.is_valid(), valid, form.errors)

    def test_the_list_says_how_many_have_no_date_and_lists_them(self):
        make_invoice(invoice_number="DATEE")
        undated = make_invoice(invoice_number="SANS-DATE")
        Invoice.objects.filter(pk=undated.pk).update(invoice_date=None)
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertEqual(response.context["undated_count"], 1)
        self.assertContains(response, "?filtre=sans-date")
        # The address the list used to give still filters.
        for query in ("?filtre=sans-date", "?sans_date=1"):
            filtered = self.client.get(reverse("invoices:invoice_list") + query)
            self.assertEqual([invoice.pk for invoice in filtered.context["invoices"]], [undated.pk])
