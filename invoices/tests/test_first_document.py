"""A supplier created before any document of its own, and its first one.

What a first document teaches is said on the supplier's page, to be seen:
nothing else vouches for it yet. And an invoice type files what it fetches
under its supplier whatever it prints - but a document printing what names
another supplier (its company number, its header) teaches nothing until a
person has said it is this one's: learned, the other supplier's number would
have become this one's, and the other would have lost it.

OCR never runs here: `receipts.recognise` is replaced. Data invented.
"""

from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from invoices.importing import charge_state
from invoices.models import Invoice, ScrapeJob, Supplier, SupplierChange
from invoices.receipts import (
    WAITING_GROUP,
    identifier_report,
    import_document,
    import_invoice_pdf,
    import_receipt,
    invoice_supplier_choices,
    learn_identifiers,
    move_to_shop,
    reread_document,
    shop_choices,
    supplier_notices,
)
from invoices.tasks import _import_document_file
from invoices.tests.page_posts import page_post
from invoices.tests.test_receipt_shop_choice import recognised
from invoices.tests.test_unknown_shops import staged_file
from invoices.workspace import DOCUMENT_TO_FIX
from tests.factories import make_invoice, make_invoice_line, make_supplier

D = Decimal

SIREN = "900000019"
OTHER_SIREN = "800000002"
THIRD_SIREN = "700000003"
PHONE = "06 12 34 56 78"  # the customer's own, printed on everyone's documents
TYPE_NAME = "Traiteur - Factures"


def bill(day=9, siren=SIREN, top="TRAITEUR EXEMPLE SARL"):
    return f"""{top}
3 RUE DES FOURS 75011 PARIS
Client : AU COMPTOIR - {PHONE}
Facture du {day:02d}/05/2026
PLATEAU APERITIF        40,00
TOTAL TTC               40,00
TVA 10%  36,36  3,64
SIREN {siren[:3]} {siren[3:6]} {siren[6:]}
www.traiteur-exemple.fr"""


class FirstDocumentTests(TestCase):
    def setUp(self):
        self.caterer = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        grossiste = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple", parser_key="")
        make_invoice(supplier=grossiste, ocr_text=f"GROSSISTE EXEMPLE\nClient : {PHONE}\nTOTAL 12,00")
        self.grossiste = grossiste

    def fetch(self, text, name="traiteur.pdf", by_type=TYPE_NAME, **kwargs):
        """As a type's gather files it: read as a ticket (no text layer), its
        supplier named by the type."""
        path = staged_file(self, name)
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)), \
                mock.patch("invoices.receipts.document_text", return_value=""):
            return import_document(
                path, supplier=self.caterer, chosen_because=f"Reçue par e-mail (« {TYPE_NAME} »).", by_type=by_type,
                **kwargs,
            )

    def test_a_type_naming_an_empty_supplier_teaches_it_and_asks_for_a_look(self):
        invoice = self.fetch(bill())
        self.assertEqual(invoice.supplier, self.caterer)
        self.caterer.refresh_from_db()
        self.assertEqual(sorted(self.caterer.ticket_identifiers), [f"siren:{SIREN}", "web:traiteur-exemple.fr"])
        first = SupplierChange.objects.get(supplier=self.caterer, kind=SupplierChange.Kind.FIRST_DOCUMENT)
        self.assertTrue(first.needs_review)
        self.assertEqual(first.invoice, invoice)
        self.assertIn("Il lui a appris : n° SIREN 900 000 019", first.summary)
        self.assertIn(TYPE_NAME, first.summary)
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[self.caterer.pk]))
        self.assertContains(fiche, "À voir")

    def test_only_the_first_is_recorded_as_such(self):
        self.fetch(bill())
        self.fetch(bill(day=10), name="second.pdf")
        self.assertEqual(SupplierChange.objects.filter(kind=SupplierChange.Kind.FIRST_DOCUMENT).count(), 1)

    def test_the_customers_phone_is_not_learned_from_a_first_document(self):
        self.fetch(bill())
        self.caterer.refresh_from_db()
        self.assertNotIn("tel:0612345678", self.caterer.ticket_identifiers)

    def test_through_a_gather_the_log_says_what_it_taught(self):
        job = ScrapeJob.objects.create()
        path = staged_file(self, "traiteur-gather.pdf")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(bill())), \
                mock.patch("invoices.receipts.document_text", return_value=""):
            self.assertTrue(_import_document_file(
                job, self.caterer, path, chosen_because=f"Téléchargée par « {TYPE_NAME} ».", by_type=TYPE_NAME
            ))
        job.refresh_from_db()
        self.assertIn("Premier document", job.log)

    def test_an_upload_naming_it_teaches_it_too(self):
        path = staged_file(self, "upload.pdf")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(bill())), \
                mock.patch("invoices.receipts.document_text", return_value=""):
            import_document(path, supplier=self.caterer)
        self.caterer.refresh_from_db()
        self.assertIn(f"siren:{SIREN}", self.caterer.ticket_identifiers)
        self.assertTrue(SupplierChange.objects.filter(supplier=self.caterer, kind="FIRST_DOCUMENT").exists())

    def test_one_recognised_by_its_header_teaches_nothing_and_says_what_it_prints(self):
        Supplier.objects.filter(pk=self.caterer.pk).update(ticket_header="TRAITEUR EXEMPLE SARL")
        path = staged_file(self, "entete.pdf")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(bill())):
            invoice = import_receipt(path)
        self.assertEqual(invoice.supplier_id, self.caterer.pk)
        self.caterer.refresh_from_db()
        self.assertEqual(self.caterer.ticket_identifiers, [])
        first = SupplierChange.objects.get(supplier=self.caterer, kind=SupplierChange.Kind.FIRST_DOCUMENT)
        self.assertIn("reconnu par son en-tête", first.summary)
        self.assertIn("rien n'en a été retenu", first.summary)

    def test_waiting_suppliers_come_first_in_the_import_choices(self):
        for groups in (shop_choices(), invoice_supplier_choices()):
            label, waiting = groups[0]
            self.assertEqual(label, WAITING_GROUP)
            self.assertIn(self.caterer, waiting)
            self.assertNotIn(self.grossiste, waiting)
            self.assertFalse(any(self.caterer in suppliers for _label, suppliers in groups[1:]))
            self.assertFalse(any(supplier.code == "OTHER" for supplier in waiting))


class TypeGuardTests(TestCase):
    """The caterer's type fetches a document printing the company number
    another supplier learned. The doubt is kept on the document
    (`Invoice.supplier_doubt`): as a check, reading it again or as a charge
    rewrote the checks and the doubt went with them."""

    def setUp(self):
        self.caterer = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        self.grossiste = make_supplier(
            code="GROSSISTE_X", name="Grossiste Exemple", parser_key="", ticket_identifiers=[f"siren:{OTHER_SIREN}"]
        )
        make_invoice(supplier=self.grossiste, ocr_text=bill(siren=OTHER_SIREN, top="GROSSISTE EXEMPLE"))

    def fetch(self, text, name="douteux.pdf"):
        path = staged_file(self, name)
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)), \
                mock.patch("invoices.receipts.document_text", return_value=""):
            return import_document(
                path, supplier=self.caterer, chosen_because=f"Reçue par e-mail (« {TYPE_NAME} »).", by_type=TYPE_NAME
            )

    def doubted(self):
        return self.fetch(bill(siren=OTHER_SIREN, top="PLATEAUX DU MARCHE") + "\nwww.plateaux-marche.fr")

    def test_it_is_filed_as_the_type_says_flagged_and_teaches_nothing(self):
        invoice = self.doubted()
        self.assertEqual(invoice.supplier, self.caterer)
        self.assertIn("n° SIREN 800 000 002 de Grossiste Exemple", invoice.supplier_doubt)
        self.assertEqual(invoice.review_state["label"], "Fournisseur à confirmer")
        self.caterer.refresh_from_db()
        self.grossiste.refresh_from_db()
        self.assertEqual(self.caterer.ticket_identifiers, [])
        # Learned, it would have stripped the wholesaler of its own number.
        self.assertEqual(self.grossiste.ticket_identifiers, [f"siren:{OTHER_SIREN}"])
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[self.caterer.pk]))
        self.assertContains(fiche, "ce qui reconnaît un autre fournisseur")
        page = self.client.get(reverse("invoices:receipt_review", args=[invoice.pk]))
        self.assertContains(page, "Valider ce document confirme qu'il est de Traiteur Exemple")

    def test_one_printing_nothing_of_another_is_not_flagged(self):
        self.assertEqual(self.fetch(bill()).supplier_doubt, "")

    def test_one_printing_its_own_number_beside_anothers_is_flagged_too(self):
        """Named by neither, it was not doubted - and learned from, it took
        the wholesaler's number."""
        Supplier.objects.filter(pk=self.caterer.pk).update(ticket_identifiers=[f"siren:{SIREN}"])
        make_invoice(supplier=self.caterer, ocr_text=bill())
        invoice = self.fetch(bill() + f"\nSIREN {OTHER_SIREN[:3]} {OTHER_SIREN[3:6]} {OTHER_SIREN[6:]}")
        self.assertIn("aussi le n° SIREN 800 000 002 de Grossiste Exemple", invoice.supplier_doubt)
        self.grossiste.refresh_from_db()
        self.assertEqual(self.grossiste.ticket_identifiers, [f"siren:{OTHER_SIREN}"])

    def test_one_printing_two_other_companies_numbers_beside_its_own_is_flagged(self):
        """Two others' numbers were no doubt at all (one or none was
        looked for): learned from, it stripped both of them."""
        third = make_supplier(
            code="TIERS_X", name="Tiers Exemple", parser_key="", ticket_identifiers=[f"siren:{THIRD_SIREN}"]
        )
        make_invoice(supplier=third, ocr_text=bill(siren=THIRD_SIREN, top="TIERS EXEMPLE"))
        Supplier.objects.filter(pk=self.caterer.pk).update(ticket_identifiers=[f"siren:{SIREN}"])
        make_invoice(supplier=self.caterer, ocr_text=bill())
        both = "".join(f"\nSIREN {s[:3]} {s[3:6]} {s[6:]}" for s in (OTHER_SIREN, THIRD_SIREN))
        invoice = self.fetch(bill() + both)
        self.assertIn("Grossiste Exemple", invoice.supplier_doubt)
        self.assertIn("Tiers Exemple", invoice.supplier_doubt)
        self.grossiste.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual((self.grossiste.ticket_identifiers, third.ticket_identifiers),
                         ([f"siren:{OTHER_SIREN}"], [f"siren:{THIRD_SIREN}"]))

    def test_a_doubted_document_is_not_the_first_the_next_one_is(self):
        """It vouches for nothing and taught nothing: recorded as the first,
        the one that did teach went unrecorded."""
        self.doubted()
        self.assertFalse(SupplierChange.objects.filter(supplier=self.caterer, kind="FIRST_DOCUMENT").exists())
        self.fetch(bill(day=12), name="vrai.pdf")
        first = SupplierChange.objects.get(supplier=self.caterer, kind="FIRST_DOCUMENT")
        self.assertIn("Il lui a appris", first.summary)

    def test_the_other_suppliers_page_offers_what_only_a_doubted_document_prints(self):
        """Learning counts a doubted document as nobody's; the page refused
        « Retenir » for what it prints."""
        self.doubted()
        Supplier.objects.filter(pk=self.grossiste.pk).update(ticket_identifiers=[])
        self.grossiste.refresh_from_db()
        report = identifier_report(self.grossiste)
        row = next(row for row in report["printed"] if row["identifier"] == f"siren:{OTHER_SIREN}")
        self.assertTrue(row["can_keep"], row["reason"])

    def test_it_takes_nothing_from_the_other_supplier_later_either(self):
        """Counted among everybody else's documents, it made the wholesaler
        forget its number the next time the wholesaler learned."""
        self.doubted()
        make_invoice(supplier=self.grossiste, ocr_text=bill(day=12, siren=OTHER_SIREN, top="GROSSISTE EXEMPLE"))
        learn_identifiers(self.grossiste, bill(day=12, siren=OTHER_SIREN, top="GROSSISTE EXEMPLE"))
        self.grossiste.refresh_from_db()
        self.assertIn(f"siren:{OTHER_SIREN}", self.grossiste.ticket_identifiers)

    def test_nor_through_the_command_learning_from_every_document(self):
        self.doubted()
        call_command("learn_shop_identifiers", stdout=StringIO())
        self.caterer.refresh_from_db()
        self.grossiste.refresh_from_db()
        self.assertEqual(self.caterer.ticket_identifiers, [])
        self.assertIn(f"siren:{OTHER_SIREN}", self.grossiste.ticket_identifiers)

    def test_reading_it_again_keeps_the_doubt(self):
        invoice = self.doubted()
        text = bill(siren=OTHER_SIREN, top="PLATEAUX DU MARCHE")
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)):
            reread_document(invoice)
        invoice.refresh_from_db()
        self.assertIn("Grossiste Exemple", invoice.supplier_doubt)

    def test_a_charge_keeps_it_and_waits_to_be_fixed_saying_why(self):
        Supplier.objects.filter(pk=self.caterer.pk).update(expenses_only=True)
        self.caterer.refresh_from_db()
        invoice = self.doubted()
        charge_state(invoice, invoice.printed_total_ttc)
        invoice.refresh_from_db()
        self.assertTrue(invoice.supplier_doubt)
        self.assertEqual(invoice.review_state["label"], "Fournisseur à confirmer")
        self.assertTrue(Invoice.objects.filter(DOCUMENT_TO_FIX, pk=invoice.pk).exists())
        page = self.client.get(reverse("invoices:receipt_queue"))
        self.assertContains(page, "fournisseur à confirmer")

    def test_moving_it_answers_the_doubt(self):
        """Moved by a person, it is the wholesaler's: the doubt answered, not
        left saying on the wholesaler's page that it may be someone else's."""
        invoice = self.doubted()
        move_to_shop(invoice, self.grossiste)
        invoice.refresh_from_db()
        self.assertEqual(invoice.supplier_doubt, "")
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[self.grossiste.pk]))
        self.assertNotContains(fiche, "ce qui reconnaît un autre fournisseur")

    def test_validating_it_answers_the_doubt_and_teaches(self):
        invoice = self.doubted()
        url = reverse("invoices:receipt_review", args=[invoice.pk])
        self.client.post(url, page_post(self.client.get(url)))
        invoice.refresh_from_db()
        self.assertEqual(invoice.supplier_doubt, "")
        self.caterer.refresh_from_db()
        self.assertIn("web:plateaux-marche.fr", self.caterer.ticket_identifiers)


class DigitalInvoiceGuardTests(TestCase):
    """A supplier with a reader of its own, fetched by a portal type: the
    doubt was free text in its message - never waiting anywhere, never on
    its supplier's page, and nothing on the page answered it."""

    def setUp(self):
        self.wholesaler = make_supplier(code="LECTEUR_X", name="Lecteur Exemple", parser_key="UBA")
        self.other = make_supplier(
            code="GROSSISTE_X", name="Grossiste Exemple", parser_key="", ticket_identifiers=[f"siren:{OTHER_SIREN}"]
        )
        make_invoice(supplier=self.other, ocr_text=bill(siren=OTHER_SIREN, top="GROSSISTE EXEMPLE"))
        self.filed = make_invoice(supplier=self.wholesaler, invoice_number="F-1")
        make_invoice_line(invoice=self.filed, raw_name="CAISSE EXEMPLE", total_ht=D("10.00"), vat_rate=D("0.20"))

    def test_it_waits_is_said_and_validating_it_answers(self):
        path = staged_file(self, "lecteur.pdf")
        text = bill(siren=OTHER_SIREN, top="LECTEUR EXEMPLE") + "\nwww.lecteur-exemple.fr"
        with mock.patch("invoices.importing.parse_and_import", return_value=self.filed):
            invoice = import_invoice_pdf(path, self.wholesaler, text=text, by_type=TYPE_NAME)
        self.assertIn("Grossiste Exemple", invoice.supplier_doubt)
        self.assertTrue(Invoice.objects.filter(DOCUMENT_TO_FIX, pk=invoice.pk).exists())
        self.assertEqual(invoice.error_message, "")
        self.assertTrue(any(notice["kind"] == "type_check" for notice in supplier_notices(self.wholesaler)))
        url = reverse("invoices:invoice_edit_lines", args=[invoice.pk])
        page = self.client.get(url)
        self.assertContains(page, "Valider ce document confirme")
        self.client.post(url, page_post(page))
        invoice.refresh_from_db()
        self.assertEqual(invoice.supplier_doubt, "")
        self.wholesaler.refresh_from_db()
        self.assertIn("web:lecteur-exemple.fr", self.wholesaler.ticket_identifiers)
