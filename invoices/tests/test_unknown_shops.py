"""Tickets from shops nothing was configured for.

The reader knows no shop by its layout, so any supplier's ticket is read.
What a shop no header matched needs is a name, and the text its tickets
print at the top, so the next ones find it on their own - created from the
batch page or the review page. A ticket filed under the wrong shop is moved,
its lines finding their products among the new shop's.

OCR never runs here: `receipts.recognise` is replaced. Data invented.
"""

import os
import shutil
from datetime import date
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product
from invoices.forms import ReceiptShopForm
from invoices.models import Invoice, ShopItemPrice, Supplier
from invoices.parsers import is_ticket_shop
from invoices.receipt_batches import run_receipt_batch, stage_batch
from invoices.receipts import (
    UnrecognisedShopError,
    create_shop,
    describe_tickets,
    detect_parser,
    first_reading,
    header_guess,
    import_receipt,
    move_to_shop,
    parser_for,
    pending_receipts,
    plain_text,
    tickets_printing,
)
from invoices.tests.test_generic_receipt import UNKNOWN_SHOP
from invoices.tests.test_receipt_shop_choice import recognised
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

D = Decimal
FIVE_FIVE = D("0.055")
CHECKED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def staged_file(test, name):
    path = os.path.join(settings.MEDIA_ROOT, name)
    with open(path, "wb") as handle:
        handle.write(b"%PDF-1.4 " + name.encode())
    test.addCleanup(lambda: os.path.exists(path) and os.remove(path))
    return path


class WhoseTicketsAreReadTests(TestCase):
    def test_every_supplier_but_the_ai_one_has_its_tickets_read(self):
        for code in ("FRANPRIX", "METRO", "UBA"):
            with self.subTest(code=code):
                self.assertIsNotNone(parser_for(Supplier.objects.get(code=code)))
        self.assertIsNone(parser_for(Supplier.objects.get(code="OTHER")))
        reader = parser_for(make_supplier(code="EPICERIE", name="Épicerie du coin"))
        self.assertEqual(reader.parse_text(UNKNOWN_SHOP).supplier_code, "EPICERIE")

    def test_a_shop_is_a_till_configured_or_a_supplier_without_invoices(self):
        self.assertTrue(is_ticket_shop(Supplier.objects.get(code="SABBH")))
        self.assertTrue(is_ticket_shop(make_supplier(code="EPICERIE", parser_key="")))
        for code in ("METRO", "UBA", "OTHER"):
            with self.subTest(code=code):
                self.assertFalse(is_ticket_shop(Supplier.objects.get(code=code)))


class HeaderTests(TestCase):
    def test_a_header_given_to_a_shop_finds_its_tickets(self):
        shop = make_supplier(code="EPICERIE", name="Épicerie du coin", ticket_header="Épicerie  du Coin")
        self.assertEqual(detect_parser(UNKNOWN_SHOP).supplier_code, shop.code)

    def test_it_comes_before_the_configured_tills(self):
        """Épicerie Sabah prints "SABAH", which Sabbh's till answers to: the
        header a person gave is the more particular."""
        make_supplier(code="SABAH", name="Épicerie Sabah", ticket_header="EPICERIE SABAH")
        text = "EPICERIE SABAH\n12 RUE INVENTEE\nPAIN  1,00\nTOTAL  1,00\n"
        self.assertEqual(detect_parser(text).supplier_code, "SABAH")
        self.assertEqual(detect_parser("SABAH\nautre\n").supplier_code, "SABBH")

    def test_the_longest_header_wins(self):
        make_supplier(code="COIN", name="Le Coin", ticket_header="COIN")
        make_supplier(code="EPICERIE", name="Épicerie du coin", ticket_header="EPICERIE DU COIN")
        self.assertEqual(detect_parser(UNKNOWN_SHOP).supplier_code, "EPICERIE")

    def test_a_header_is_whole_words(self):
        make_supplier(code="COIN", name="Coin", ticket_header="COIN")
        self.assertIsNone(detect_parser("RECOINS\nPAIN 1,00\n"))

    def test_the_ai_pseudo_supplier_is_never_found(self):
        Supplier.objects.filter(code="OTHER").update(ticket_header="EPICERIE DU COIN")
        self.assertIsNone(detect_parser(UNKNOWN_SHOP))

    def test_comparing_ignores_accents_case_and_punctuation(self):
        self.assertEqual(plain_text("Épicerie  d'Été, 12-bis"), "EPICERIE D ETE 12 BIS")

    def test_the_name_a_ticket_seems_to_print(self):
        self.assertEqual(header_guess(UNKNOWN_SHOP), "EPICERIE DU COIN")
        self.assertEqual(header_guess("12 RUE X\n0,50\n"), "")
        self.assertEqual(header_guess("  EPICERIE   DU  COIN \n"), "EPICERIE DU COIN")
        # "Label : value" is a field of the document, not its sender's name.
        self.assertEqual(header_guess("Statut : COMPLETE\nMéthode de commande : Web Order\nCUISINE PRO"), "CUISINE PRO")

    def test_what_a_ticket_reads_as_before_its_shop_is_known(self):
        self.assertEqual(
            first_reading(UNKNOWN_SHOP),
            {"read_date": "12/03/2026", "read_total": "10.50", "header": "EPICERIE DU COIN"},
        )


class CreateShopTests(TestCase):
    def test_a_new_shop(self):
        shop = create_shop("  Épicerie du  coin ", "EPICERIE DU COIN")
        self.assertEqual(
            (shop.code, shop.name, shop.parser_key, shop.ticket_header),
            ("EPICERIE_DU_COIN", "Épicerie du coin", "", "EPICERIE DU COIN"),
        )
        self.assertTrue(is_ticket_shop(shop))

    def test_a_code_taken_gets_a_number(self):
        make_supplier(code="EPICERIE_DU_COIN", name="Autre")
        self.assertEqual(create_shop("Épicerie du coin").code, "EPICERIE_DU_COIN_2")

    def test_what_is_refused(self):
        make_invoice(supplier=Supplier.objects.get(code="SABBH"), ocr_text="Sabbh Oriental\nRUE DU TEMPLE\n")
        make_invoice(supplier=Supplier.objects.get(code="FRANPRIX"), ocr_text="FRANPRIX\n2 RUE DU TEMPLE\n")
        for name, header, said in (
            ("", "", "Donnez un nom"),
            ("franprix", "", "existe déjà"),
            ("Nouvelle", "AB", "trop court"),
            ("Nouvelle", "rue du Temple", "imprimé sur 2 tickets d'autres enseignes"),
        ):
            with self.subTest(name=name, header=header):
                with self.assertRaisesMessage(ValueError, said):
                    create_shop(name, header)

    def test_a_header_on_many_tickets_of_one_shop_is_that_shops(self):
        sabbh = Supplier.objects.get(code="SABBH")
        for _ in range(4):
            make_invoice(supplier=sabbh, ocr_text="Sabbh Oriental\nRUE DU TEMPLE\n")
        with self.assertRaisesMessage(ValueError, "imprimé sur 4 tickets"):
            create_shop("Nouvelle", "RUE DU TEMPLE")

    def test_a_few_tickets_filed_before_the_shop_existed_are_named(self):
        """Épicerie Sabah's tickets went to Sabbh while it had no shop of its
        own: they carry its header, and are said, to be moved."""
        misfiled = make_invoice(
            supplier=Supplier.objects.get(code="SABBH"), invoice_date=date(2024, 1, 30),
            ocr_text="EPICERIE SABAH\n77 RUE CROZATIER\n",
        )
        shop = create_shop("Épicerie Sabah", "RUE CROZATIER")
        self.assertEqual(shop.ticket_header, "RUE CROZATIER")
        self.assertEqual(describe_tickets(tickets_printing("RUE CROZATIER")), "Sabbh Oriental du 30/01/2024")
        self.assertEqual(tickets_printing("RUE CROZATIER", ignoring=[misfiled]), [])


class MoveToShopTests(TestCase):
    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.shop = make_supplier(code="EPICERIE", name="Épicerie du coin")
        self.ticket = make_invoice(supplier=self.sabbh, invoice_number="42", parse_checks=CHECKED)
        self.misread = make_product(supplier=self.sabbh, raw_name="TAHINA INVENTEE")
        make_invoice_line(
            invoice=self.ticket, product=self.misread, raw_name="TAHINA INVENTEE", read_as="TAHINA INVENTEE",
            total_ht="8.06", vat_rate=FIVE_FIVE, printed_ttc=D("8.50"),
        )

    def test_the_lines_find_their_products_at_the_new_shop(self):
        known = make_product(supplier=self.shop, raw_name="TAHINA INVENTEE")
        move_to_shop(self.ticket, self.shop)
        self.ticket.refresh_from_db()
        line = self.ticket.lines.get()
        self.assertEqual((self.ticket.supplier, line.product), (self.shop, known))
        self.assertEqual((line.printed_ttc, line.read_as), (D("8.50"), "TAHINA INVENTEE"))
        self.assertFalse(Product.objects.filter(pk=self.misread.pk).exists())
        self.assertIn("Enseigne choisie à la main", [check["label"] for check in self.ticket.parse_checks])

    def test_a_number_the_new_shop_has_is_refused(self):
        make_invoice(supplier=self.shop, invoice_number="42")
        with self.assertRaisesMessage(ValueError, "a déjà un ticket n° 42"):
            move_to_shop(self.ticket, self.shop)
        self.assertEqual(Invoice.objects.get(pk=self.ticket.pk).supplier, self.sabbh)

    def test_the_review_page_moves_it(self):
        url = reverse("invoices:receipt_review", args=[self.ticket.pk])
        page = self.client.get(url)
        self.assertContains(page, 'name="action" value="move_shop"')
        self.assertContains(page, f'<option value="{self.sabbh.pk}" disabled>Sabbh Oriental</option>', html=True)
        response = self.client.post(url, {"action": "move_shop", "supplier": self.shop.pk})
        self.assertRedirects(response, url)
        self.assertEqual(Invoice.objects.get(pk=self.ticket.pk).supplier, self.shop)
        self.assertIn("Ticket rangé chez Épicerie du coin.", messages_of(response))

    def test_the_review_page_moves_it_to_a_new_shop(self):
        """The ticket moved prints the header itself: that is no reason to
        refuse it. Another one filed under Sabbh is named, to move too."""
        Invoice.objects.filter(pk=self.ticket.pk).update(ocr_text="EPICERIE SABAH\nRUE CROZATIER\n")
        # Three others: with the one moved, one too many to be misfiled ones.
        other = make_invoice(
            supplier=self.sabbh, invoice_date=date(2024, 2, 2), ocr_text="EPICERIE SABAH\nRUE CROZATIER\n"
        )
        for day in (3, 4):
            make_invoice(supplier=self.sabbh, invoice_date=date(2024, 2, day), ocr_text="RUE CROZATIER\n")
        url = reverse("invoices:receipt_review", args=[self.ticket.pk])
        response = self.client.post(
            url, {"action": "move_shop", "supplier": "new", "new_name": "Épicerie Sabah", "new_header": "CROZATIER"}
        )
        shop = Supplier.objects.get(name="Épicerie Sabah")
        self.assertEqual((Invoice.objects.get(pk=self.ticket.pk).supplier, shop.ticket_header), (shop, "CROZATIER"))
        said = messages_of(response)
        self.assertTrue(any("les documents qui portent « CROZATIER »" in message for message in said), said)
        self.assertTrue(any("Sabbh Oriental du 02/02/2024" in message for message in said), said)
        self.assertEqual(Invoice.objects.get(pk=other.pk).supplier, self.sabbh)

    def test_its_own_shop_changes_nothing_and_says_so(self):
        url = reverse("invoices:receipt_review", args=[self.ticket.pk])
        response = self.client.post(url, {"action": "move_shop", "supplier": self.sabbh.pk})
        self.assertIn("Ce ticket est déjà rangé chez Sabbh Oriental.", messages_of(response))
        self.assertNotIn("Enseigne choisie à la main", [check["label"] for check in Invoice.objects.get(pk=self.ticket.pk).parse_checks])

    def test_a_new_shop_needs_a_name(self):
        url = reverse("invoices:receipt_review", args=[self.ticket.pk])
        response = self.client.post(url, {"action": "move_shop", "supplier": "new", "new_name": " "})
        self.assertIn("Donnez un nom à la nouvelle enseigne.", messages_of(response))
        self.assertEqual(Invoice.objects.get(pk=self.ticket.pk).supplier, self.sabbh)


class ShopFormTests(TestCase):
    def test_a_supplier_or_a_new_shop(self):
        sabbh = Supplier.objects.get(code="SABBH")
        form = ReceiptShopForm({"supplier": str(sabbh.pk)})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.shop(), (sabbh, False))
        form = ReceiptShopForm({"supplier": "new", "new_name": "Épicerie du coin"})
        self.assertTrue(form.is_valid())
        shop, created = form.shop()
        self.assertTrue(created)
        self.assertEqual(shop.name, "Épicerie du coin")

    def test_what_is_refused(self):
        for data in ({"supplier": ""}, {"supplier": "abc"}, {"supplier": "new"},
                     {"supplier": str(Supplier.objects.get(code="OTHER").pk)}):
            with self.subTest(data=data):
                self.assertFalse(ReceiptShopForm(data).is_valid())


class NewShopFromBatchTests(TestCase):
    """Two tickets of the same unknown shop in one import: naming the shop on
    the first sends the second to it."""

    def setUp(self):
        batch = stage_batch([
            SimpleUploadedFile("coin-1.pdf", b"%PDF-1.4 un"),
            SimpleUploadedFile("coin-2.pdf", b"%PDF-1.4 deux"),
        ])
        self.addCleanup(shutil.rmtree, os.path.join(settings.MEDIA_ROOT, "receipt_batches", str(batch.pk)), True)
        second = UNKNOWN_SHOP.replace("12/03/2026", "13/03/2026")
        with mock.patch("invoices.receipts.recognise", side_effect=[recognised(UNKNOWN_SHOP), recognised(second)]):
            self.batch = run_receipt_batch(batch.pk)
        self.page = reverse("invoices:receipt_batch", args=[self.batch.pk])
        self.url = reverse("invoices:receipt_batch_assign", args=[self.batch.pk, 0])
        self.second = second

    def test_waiting_for_a_shop_is_counted_apart(self):
        page = self.client.get(self.page)
        self.assertContains(page, "À ranger")
        self.assertEqual((self.batch.failed_count, self.batch.awaiting_shop_count), (0, 2))
        upload_page = self.client.get(reverse("invoices:receipt_upload"))
        self.assertContains(upload_page, "<th class=\"num\">À ranger</th>", html=True)

    def test_a_ticket_waiting_for_its_shop_shows_what_it_reads_as(self):
        entry = self.batch.results[0]
        self.assertEqual(
            (entry["status"], entry["header"], entry["read_date"], entry["read_total"]),
            ("unrecognised", "EPICERIE DU COIN", "12/03/2026", "10.50"),
        )
        page = self.client.get(self.page)
        self.assertContains(page, "« EPICERIE DU COIN »")
        self.assertContains(page, "10.50 €")
        # The header is given on the review page, the ticket on screen - not
        # here, where nobody has seen it.
        self.assertNotContains(page, 'name="new_header"')
        self.assertContains(page, 'value="Epicerie Du Coin"')

    def test_naming_the_shop_imports_it_and_the_header_reads_the_others_again(self):
        """The shop is named here, its header given on the ticket's own page:
        the other files no shop was recognised on are read again then."""
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)), \
                mock.patch("invoices.receipt_batches.start_batch") as start:
            response = self.client.post(self.url, {"supplier": "new", "new_name": "Épicerie du coin"})
        shop = Supplier.objects.get(name="Épicerie du coin")
        ticket = Invoice.objects.get(supplier=shop)
        self.assertRedirects(response, reverse("invoices:receipt_review", args=[ticket.pk]) + f"?lot={self.batch.pk}")
        self.assertEqual((ticket.lines.count(), shop.ticket_header), (3, ""))
        self.assertIn(ticket, pending_receipts())
        self.assertTrue(any("indiquez le texte" in message for message in messages_of(response)))
        start.assert_not_called()

        with mock.patch("invoices.receipt_batches.start_batch") as start:
            given = self.client.post(
                reverse("invoices:receipt_review", args=[ticket.pk]),
                {"action": "shop_header", "ticket_header": "EPICERIE DU COIN"},
            )
        self.assertTrue(any("1 fichier(s) sans enseigne sont relus" in message for message in messages_of(given)))
        start.assert_called_once()
        self.batch.refresh_from_db()
        self.assertEqual([entry["status"] for entry in self.batch.results], ["ok", "pending"])

        # The batch runs again: the second ticket now finds its shop.
        with mock.patch("invoices.receipts.recognise", return_value=recognised(self.second)):
            batch = run_receipt_batch(self.batch.pk)
        self.assertEqual(batch.results[1]["status"], "ok")
        self.assertEqual(batch.results[1]["shop"], "Épicerie du coin")
        self.assertEqual(Invoice.objects.filter(supplier=shop).count(), 2)

    def test_a_shop_name_already_taken_is_refused(self):
        with mock.patch("invoices.receipt_batches.import_document") as importer:
            response = self.client.post(self.url, {"supplier": "new", "new_name": "Franprix"})
        importer.assert_not_called()
        self.assertRedirects(response, self.page)
        self.assertTrue(any("existe déjà" in message for message in messages_of(response)))

    def test_a_shop_named_here_is_sent_to_the_ticket_for_its_header(self):
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)), \
                mock.patch("invoices.receipt_batches.start_batch") as start:
            response = self.client.post(self.url, {"supplier": "new", "new_name": "Épicerie du coin"})
        start.assert_not_called()
        said = messages_of(response)
        self.assertTrue(any("Sur la page du ticket" in message for message in said), said)


class ImportTests(TestCase):
    def test_a_ticket_of_a_shop_named_before_is_recognised(self):
        make_supplier(code="EPICERIE", name="Épicerie du coin", ticket_header="EPICERIE DU COIN")
        path = staged_file(self, "coin.pdf")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)):
            ticket = import_receipt(path)
        self.assertEqual(ticket.supplier.code, "EPICERIE")
        self.assertEqual(ticket.invoice_date, date(2026, 3, 12))
        self.assertEqual([line.raw_name for line in ticket.lines.all()], ["TOMATES GRAPPE", "SIROP MENTHE", "PAIN DE MIE"])

    def test_an_unknown_ticket_says_what_it_read(self):
        path = staged_file(self, "coin.pdf")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(UNKNOWN_SHOP)):
            with self.assertRaises(UnrecognisedShopError) as raised:
                import_receipt(path)
        self.assertIn("EPICERIE DU COIN", raised.exception.text)


class PriceListPlacementTests(TestCase):
    def page(self, supplier, **line):
        ticket = make_invoice(supplier=supplier, parse_checks=CHECKED)
        make_invoice_line(invoice=ticket, product=make_product(supplier=supplier), vat_rate=FIVE_FIVE, **line)
        return self.client.get(reverse("invoices:receipt_review", args=[ticket.pk]))

    def test_open_where_the_till_prints_no_names(self):
        self.assertContains(self.page(Supplier.objects.get(code="SABBH")), '<details class="known-prices" open>')

    def test_folded_where_it_does(self):
        franprix = Supplier.objects.get(code="FRANPRIX")
        self.assertContains(self.page(franprix), '<details class="known-prices">')
        ShopItemPrice.objects.create(supplier=franprix, unit_price_ttc=D("0.49"), label="Pain")
        self.assertContains(self.page(franprix), '<details class="known-prices" open>')
