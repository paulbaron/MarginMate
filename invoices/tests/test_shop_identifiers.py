"""A shop named by hand is recognised on its next documents by what they
print that names it - its SIREN, phone, web site - even with no header, or a
torn one.

What a document prints is learned only when a person said whose it is: a
shop chosen for it, a ticket moved, a ticket checked. What other suppliers'
documents print too names nobody (the customer's own phone, a label printed
on everybody's goods): never learned, and forgotten by the shop a ticket
carrying it is moved away from.

OCR never runs here: `receipts.recognise` is replaced. Data invented.
"""

import os
from datetime import date
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices.models import Invoice, Supplier
from invoices.receipts import (
    IDENTIFIED_CHECK,
    UnrecognisedShopError,
    detect_parser,
    import_receipt,
    learn_identifiers,
    move_to_shop,
)
from invoices.tests.test_receipt_shop_choice import recognised
from invoices.tests.test_unknown_shops import staged_file
from tests.factories import make_invoice, make_invoice_line, make_supplier

SIREN = "900000019"
# A hardware shop's ticket, whose name is printed as a logo the OCR can't
# read: nothing but its figures names it.
TICKET = f"""12 RUE DES PLANCHES
75011 PARIS
TEL 01 23 45 67 89
LE 08/01/2026 A 10:26
VIS INOX 40MM        3,50
DECOUPE BOIS         2,50
TOTAL                6,00
CB                   6,00
TVA 20%  5,00  1,00
SIRET {SIREN}10000
www.brico-exemple.fr"""
LEARNED = [f"siren:{SIREN}", "tel:0123456789", "web:brico-exemple.fr"]


def next_ticket(day):
    return TICKET.replace("08/01/2026", f"{day:02d}/01/2026")


class LearningTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="BRICO", name="Brico Exemple", parser_key="")

    def test_what_names_the_shop_is_learned(self):
        self.assertEqual(learn_identifiers(self.shop, TICKET), LEARNED)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_identifiers, LEARNED)
        self.assertEqual(learn_identifiers(self.shop, TICKET), [])

    def test_what_another_suppliers_documents_print_is_not(self):
        other = make_supplier(code="AUTRE", name="Autre magasin", parser_key="")
        make_invoice(supplier=other, ocr_text="AUTRE MAGASIN\nTEL 01 23 45 67 89\nTOTAL 3,00")
        self.assertEqual(learn_identifiers(self.shop, TICKET), [f"siren:{SIREN}", "web:brico-exemple.fr"])

    def test_what_few_of_its_tickets_print_is_not(self):
        """A misreading, or a label on some goods, is on a ticket or two."""
        for day in range(10, 14):
            make_invoice(supplier=self.shop, ocr_text=next_ticket(day).replace("www.brico-exemple.fr", ""))
        learned = learn_identifiers(self.shop, TICKET + "\nBOIS CERTIFIE www.fsc.org")
        self.assertEqual(learned, [f"siren:{SIREN}", "tel:0123456789"])
        # Printed on enough of them later, it is.
        for day in range(14, 17):
            make_invoice(supplier=self.shop, ocr_text=next_ticket(day))
        self.assertEqual(learn_identifiers(self.shop, TICKET), ["web:brico-exemple.fr"])

    def test_what_it_knew_is_forgotten_once_rare(self):
        self.shop.ticket_identifiers = ["tel:0199999999"]
        self.shop.save()
        for day in range(10, 15):
            make_invoice(supplier=self.shop, ocr_text=next_ticket(day))
        learn_identifiers(self.shop, TICKET)
        self.assertNotIn("tel:0199999999", self.shop.ticket_identifiers)

    def test_nothing_is_learned_for_the_ai_pseudo_supplier(self):
        self.assertEqual(learn_identifiers(Supplier.objects.get(code="OTHER"), TICKET), [])


class RecognitionTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="BRICO", name="Brico Exemple", parser_key="", ticket_identifiers=LEARNED)

    def import_ticket(self, text, name="brico.pdf", **kwargs):
        path = staged_file(self, name)
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)):
            return import_receipt(path, **kwargs)

    def test_a_ticket_printing_them_goes_to_the_shop(self):
        self.assertEqual(detect_parser(next_ticket(9)).supplier_code, "BRICO")
        ticket = self.import_ticket(next_ticket(9))
        self.assertEqual((ticket.supplier, ticket.invoice_date), (self.shop, date(2026, 1, 9)))
        check = next(check for check in ticket.parse_checks if check["label"] == IDENTIFIED_CHECK)
        self.assertTrue(check["passed"])
        self.assertIn("n° SIREN 900 000 019", check["detail"])

    def test_one_of_them_is_enough(self):
        self.assertEqual(detect_parser("LE 09/01/2026\nTOTAL 3,00\nTEL 01 23 45 67 89").supplier_code, "BRICO")

    def test_a_web_site_alone_names_no_one(self):
        """The one branding the goods is printed at every shop selling them."""
        self.assertIsNone(detect_parser("LE 09/01/2026\nTOTAL 3,00\nwww.brico-exemple.fr"))

    def test_a_header_given_by_a_person_comes_before_a_phone_or_a_web_site(self):
        make_supplier(code="COIN", name="Coin bricolage", parser_key="", ticket_header="RUE DES PLANCHES")
        without_the_company = next_ticket(9).replace(f"SIRET {SIREN}10000", "")
        self.assertEqual(detect_parser(without_the_company).supplier_code, "COIN")

    def test_a_header_and_another_suppliers_company_number_name_nobody(self):
        """One company's two sources: a mobile bill printing the box's
        subscription as an advert, and the mobile line's own company number.
        The header used to win, silently."""
        make_supplier(code="COIN", name="Coin bricolage", parser_key="", ticket_header="RUE DES PLANCHES")
        self.assertIsNone(detect_parser(next_ticket(9)))
        with self.assertRaises(UnrecognisedShopError) as raised:
            self.import_ticket(next_ticket(9))
        self.assertIn("Coin bricolage", str(raised.exception))
        self.assertIn("Brico Exemple", str(raised.exception))

    def test_the_check_says_when_the_suppliers_header_was_not_printed(self):
        self.shop.ticket_header = "BRICO EXEMPLE CENTRE"
        self.shop.save()
        ticket = self.import_ticket(next_ticket(9))
        check = next(check for check in ticket.parse_checks if check["label"] == IDENTIFIED_CHECK)
        self.assertIn("Sans son en-tête « BRICO EXEMPLE CENTRE »", check["detail"])

    def test_what_two_suppliers_learned_names_neither(self):
        make_supplier(code="AUTRE", name="Autre magasin", parser_key="", ticket_identifiers=["tel:0123456789"])
        self.assertIsNone(detect_parser("LE 09/01/2026\nTEL 01 23 45 67 89\nTOTAL 3,00"))
        # The shop's own SIREN on the same ticket still names it.
        self.assertEqual(detect_parser(next_ticket(9)).supplier_code, "BRICO")

    def test_figures_naming_two_suppliers_name_neither(self):
        make_supplier(code="AUTRE", name="Autre magasin", parser_key="", ticket_identifiers=["web:autre-magasin.fr"])
        self.assertIsNone(detect_parser(next_ticket(9) + "\nwww.autre-magasin.fr"))

    def test_nothing_learned_nothing_recognised(self):
        self.shop.ticket_identifiers = []
        self.shop.save()
        with self.assertRaises(UnrecognisedShopError):
            self.import_ticket(next_ticket(9))


class WhenLearnedTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="BRICO", name="Brico Exemple", parser_key="")

    def import_ticket(self, text, name, **kwargs):
        path = staged_file(self, name)
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)):
            return import_receipt(path, **kwargs)

    def test_a_shop_chosen_by_hand_learns_them_and_its_next_ticket_is_recognised(self):
        with self.assertRaises(UnrecognisedShopError):
            self.import_ticket(TICKET, "premier.pdf")
        self.import_ticket(TICKET, "premier.pdf", supplier=self.shop)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_identifiers, LEARNED)
        self.assertEqual(self.import_ticket(next_ticket(9), "second.pdf").supplier, self.shop)

    def test_a_ticket_moved_teaches_its_new_shop_and_untaught_its_old_one(self):
        wrong = make_supplier(code="AUTRE", name="Autre magasin", parser_key="", ticket_identifiers=["tel:0123456789"])
        ticket = make_invoice(supplier=wrong, ocr_text=TICKET)
        move_to_shop(ticket, self.shop)
        self.shop.refresh_from_db()
        wrong.refresh_from_db()
        self.assertEqual(self.shop.ticket_identifiers, LEARNED)
        self.assertEqual(wrong.ticket_identifiers, [])

    def test_a_ticket_checked_on_the_review_page_teaches_its_shop(self):
        """A configured till's tickets too: the next one with a torn header
        is still recognised."""
        franprix = Supplier.objects.get(code="FRANPRIX")
        ticket = make_invoice(
            supplier=franprix, ocr_text=TICKET, printed_total_ttc="6.00",
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": False, "detail": ""}],
        )
        line = make_invoice_line(invoice=ticket, raw_name="VIS INOX", total_ht="5.00", vat_rate="0.20")
        response = self.client.post(reverse("invoices:receipt_review", args=[ticket.pk]), {
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0", "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
            "invoice_date": "2026-01-08", "printed_total_ttc": "6.00",
            "form-0-product_name": "VIS INOX", "form-0-quantity": "1", "form-0-total_ttc": "6.00",
            "form-0-vat_rate": "20", "form-0-line_id": str(line.pk),
        })
        self.assertEqual(response.status_code, 302)
        franprix.refresh_from_db()
        self.assertEqual(franprix.ticket_identifiers, LEARNED)
        self.assertEqual(detect_parser(next_ticket(9)).supplier_code, "FRANPRIX")

    def test_a_ticket_detected_on_its_own_teaches_nothing(self):
        """Nobody has said whose it is yet."""
        make_supplier(code="COIN", name="Coin bricolage", parser_key="", ticket_header="RUE DES PLANCHES")
        self.import_ticket(TICKET, "coin.pdf")
        self.assertEqual(Supplier.objects.get(code="COIN").ticket_identifiers, [])
        self.assertTrue(Invoice.objects.filter(supplier__code="COIN").exists())


class LearnFromCheckedTicketsCommandTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="BRICO", name="Brico Exemple", parser_key="")
        checked = timezone.now()
        make_invoice(supplier=self.shop, ocr_text=TICKET.replace("www.brico-exemple.fr", ""), reviewed_at=checked)
        make_invoice(supplier=self.shop, ocr_text="LE 09/01/2026\nwww.brico-exemple.fr", reviewed_at=checked)
        # Not checked yet: nobody has said whose it is.
        make_invoice(supplier=self.shop, ocr_text="LE 10/01/2026\nTEL 01 11 11 11 11")

    def run_command(self, *args):
        out = StringIO()
        call_command("learn_shop_identifiers", *args, stdout=out)
        self.shop.refresh_from_db()
        return out.getvalue()

    def test_a_dry_run_says_what_would_be_learned(self):
        out = self.run_command("--dry-run")
        self.assertIn("Brico Exemple apprendrait : n° SIREN 900 000 019, téléphone 01 23 45 67 89, site brico-exemple.fr", out)
        self.assertEqual(self.shop.ticket_identifiers, [])

    def test_every_checked_ticket_teaches_its_shop(self):
        self.run_command()
        self.assertEqual(self.shop.ticket_identifiers, LEARNED)
        self.assertIn("Rien de nouveau", self.run_command())

    def test_the_digital_invoices_already_filed_are_read_for_it(self):
        """A supplier's PDF invoices say who they are too - and the
        customer's own SIREN, on every one of them, then names nobody."""
        from django.conf import settings

        from invoices.tests.pdf_files import write_pdf

        supplier = make_supplier(code="CUISIPRO", name="Cuisipro", parser_key="")
        path = os.path.join(settings.MEDIA_ROOT, "cuisipro-facture.pdf")
        write_pdf(path, [
            "CUISIPRO FRANCE SARL  Tel: 01 98 76 54 32",
            "FACTURE N 7654321 du 07/11/2024",
            "Client : AU COMPTOIR  SIRET 900 000 019 10000",
            "Verre a shot (lot de 12)  8  3,50  28,00",
            "TOTAL TTC  EURO  33,60",
        ])
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        invoice = make_invoice(supplier=supplier)
        invoice.source_file.name = "cuisipro-facture.pdf"
        invoice.save(update_fields=["source_file"])
        # The customer's own SIRET is on this shop's invoices and on the
        # other's: it names neither.
        make_invoice(supplier=self.shop, ocr_text="AUTRE\nSIRET 900 000 019 10000\n", reviewed_at=timezone.now())

        out = self.run_command()
        supplier.refresh_from_db()
        self.assertIn("1 facture(s) numérique(s) lue(s)", out)
        self.assertEqual(supplier.ticket_identifiers, ["tel:0198765432"])
        self.assertIn("CUISIPRO FRANCE", Invoice.objects.get(pk=invoice.pk).source_text)
