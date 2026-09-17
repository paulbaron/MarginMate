"""Saying what a shop's documents print at the top, from the page where the
document is on screen.

The import card cannot ask for it: nobody has seen the ticket yet when its
shop is chosen there. The review screen shows the photo beside the lines, so
that is where the header is given - offered from the document's own top
lines - and where what already names the shop is listed.

OCR never runs here: `receipts.recognise` is replaced. Data invented.
"""

from datetime import date

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from invoices.models import Invoice, Supplier
from invoices.receipts import set_shop_header
from tests.factories import make_invoice, make_supplier

CHECKED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]
TICKET = """EPICERIE DU COIN
3 RUE INVENTEE 75011 PARIS
TEL 01 00 00 00 00
LE 12/03/2026 A 18:04
TOMATES GRAPPE        2,35
TOTAL                 2,35
"""


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class SetHeaderTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="EPICERIE", name="Épicerie du coin", parser_key="")
        self.ticket = make_invoice(
            supplier=self.shop, invoice_date=date(2026, 3, 12), ocr_text=TICKET, parse_checks=CHECKED
        )
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def test_the_page_offers_the_documents_own_top_lines(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="action" value="shop_header"')
        self.assertContains(page, "EPICERIE DU COIN")
        self.assertContains(page, "3 RUE INVENTEE 75011 PARIS")
        # A date or a total says nothing about the shop.
        self.assertNotContains(page, 'data-header-choice="TOTAL 2,35"')

    def test_giving_the_header_files_the_next_tickets_there(self):
        response = self.client.post(self.url, {"action": "shop_header", "ticket_header": "EPICERIE DU COIN"})
        self.assertRedirects(response, self.url)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_header, "EPICERIE DU COIN")
        self.assertTrue(any("EPICERIE DU COIN" in message for message in messages_of(response)))

    def test_a_header_of_another_shop_is_refused(self):
        other = make_supplier(code="AUTRE", name="Autre magasin", parser_key="")
        for day in (1, 2, 3, 4):
            make_invoice(supplier=other, invoice_date=date(2026, 3, day), ocr_text="AUTRE MAGASIN\n77 RUE AILLEURS\n")
        response = self.client.post(self.url, {"action": "shop_header", "ticket_header": "RUE AILLEURS"})
        self.assertRedirects(response, self.url)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_header, "")
        self.assertTrue(any("tickets d'autres enseignes" in message for message in messages_of(response)))

    def test_its_own_tickets_never_stand_in_the_way(self):
        for day in (1, 2, 3, 4):
            make_invoice(supplier=self.shop, invoice_date=date(2026, 3, day), ocr_text=TICKET)
        self.assertEqual(set_shop_header(self.shop, "EPICERIE DU COIN"), "EPICERIE DU COIN")
        self.assertEqual(Supplier.objects.get(pk=self.shop.pk).ticket_header, "EPICERIE DU COIN")

    def test_a_header_too_short_is_refused(self):
        with self.assertRaisesMessage(ValueError, "trop court"):
            set_shop_header(self.shop, "AB")

    def test_it_can_be_taken_back(self):
        set_shop_header(self.shop, "EPICERIE DU COIN")
        self.assertEqual(set_shop_header(self.shop, ""), "")
        self.assertEqual(Supplier.objects.get(pk=self.shop.pk).ticket_header, "")

    def test_the_page_says_what_already_names_the_shop(self):
        self.shop.ticket_header = "EPICERIE DU COIN"
        self.shop.ticket_identifiers = ["tel:0100000000"]
        self.shop.save()
        page = self.client.get(self.url)
        self.assertContains(page, "téléphone 01 00 00 00 00")

    def test_a_shop_with_a_till_of_its_own_is_recognised_by_it(self):
        """Franprix's tickets are known by their layout: no header to give."""
        ticket = make_invoice(
            supplier=Supplier.objects.get(code="FRANPRIX"), ocr_text=TICKET, parse_checks=CHECKED
        )
        page = self.client.get(reverse("invoices:receipt_review", args=[ticket.pk]))
        self.assertNotContains(page, 'name="action" value="shop_header"')


class ImportCardTests(TestCase):
    """The card asks for a name, not for a header: the ticket is not on
    screen there."""

    def test_the_tickets_import_asks_for_a_name_only(self):
        page = self.client.get(reverse("invoices:receipt_upload"))
        self.assertNotContains(page, 'name="new_header"')

    def test_so_does_the_pdf_import(self):
        page = self.client.get(reverse("invoices:invoice_upload"))
        self.assertContains(page, '<option value="new">+ Nouveau fournisseur…</option>', html=True)
        self.assertNotContains(page, 'name="new_header"')


class AfterANewShopTests(TestCase):
    """A shop named without a header: its ticket says so on the review page,
    and giving it there reads the files no shop was recognised on again."""

    def test_the_ticket_of_a_shop_without_a_header_asks_for_one(self):
        shop = make_supplier(code="EPICERIE", name="Épicerie du coin", parser_key="")
        ticket = make_invoice(supplier=shop, ocr_text=TICKET, parse_checks=CHECKED)
        page = self.client.get(reverse("invoices:receipt_review", args=[ticket.pk]))
        self.assertContains(page, "pour que ses prochains tickets soient reconnus")
        self.assertEqual(page.context["header_choices"][0], "EPICERIE DU COIN")
        self.assertNotIn(Invoice.objects.get(pk=ticket.pk).supplier.ticket_header, ["EPICERIE DU COIN"])
