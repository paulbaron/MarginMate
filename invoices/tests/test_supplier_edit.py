"""A supplier created before any document of its own, modified after a look
at what it changes, deleted while nothing rests on it - each recorded in its
history with its undo.

A new source had to name an existing supplier, and one not yet sent a
document did not exist: it could not be set up before its first invoice.
Renaming or changing a header saved on one click, with nothing saying what
it would move. Data invented.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from bank.models import CounterpartyAlias
from inventory.models import Product
from inventory.services import expense_product
from invoices.models import InvoiceLine, InvoiceType, ShopItemPrice, Supplier, SupplierChange
from invoices.receipts import create_shop, rename_supplier
from tests.factories import make_invoice, make_invoice_line, make_supplier

D = Decimal
CREATE = reverse("invoices:supplier_create")


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class CreateTests(TestCase):
    def create(self, **fields):
        data = {"name": "Cave Exemple", "nature": "produits", "header": "", "arrivee": "import"}
        data.update(fields)
        return self.client.post(CREATE, data)

    def test_a_supplier_is_created_before_any_document(self):
        page = self.client.get(CREATE)
        self.assertContains(page, "Créer le fournisseur")
        response = self.create()
        supplier = Supplier.objects.get(name="Cave Exemple")
        self.assertRedirects(response, reverse("invoices:supplier_detail", args=[supplier.pk]))
        self.assertEqual((supplier.code, supplier.parser_key, supplier.expenses_only), ("CAVE_EXEMPLE", "", False))
        created = SupplierChange.objects.get(supplier=supplier)
        self.assertEqual((created.kind, created.by_person), (SupplierChange.Kind.CREATED, True))
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[supplier.pk]))
        self.assertContains(fiche, "En attente de son premier document")

    def test_charges(self):
        self.create(name="Loyer Exemple", nature="charges")
        self.assertTrue(Supplier.objects.get(name="Loyer Exemple").expenses_only)

    def test_a_taken_name_is_refused_whatever_its_case_and_leads_to_it(self):
        taken = make_supplier(code="CAVE_X", name="Cave Exemple", parser_key="")
        response = self.create(name="  cave   EXEMPLE ")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "existe déjà")
        self.assertContains(response, reverse("invoices:supplier_detail", args=[taken.pk]))
        self.assertEqual(Supplier.objects.filter(name__iexact="cave exemple").count(), 1)

    def test_a_header_given_reads_the_waiting_files_again(self):
        with mock.patch("invoices.receipt_batches.requeue_everywhere", return_value=0) as requeue:
            self.create(header="CAVE EXEMPLE SARL")
        self.assertEqual(Supplier.objects.get(name="Cave Exemple").ticket_header, "CAVE EXEMPLE SARL")
        requeue.assert_called_once()

    def test_a_header_refused_is_said_under_it_and_nothing_is_created(self):
        make_supplier(code="AUTRE_X", name="Autre Exemple", parser_key="", ticket_header="CAVE EXEMPLE SARL")
        response = self.create(header="cave exemple sarl")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "est déjà l&#x27;en-tête de Autre Exemple")
        self.assertFalse(Supplier.objects.filter(name="Cave Exemple").exists())

    def test_its_invoices_arriving_by_email_lead_to_a_type_for_it(self):
        response = self.create(arrivee="EMAIL")
        supplier = Supplier.objects.get(name="Cave Exemple")
        fiche = reverse("invoices:supplier_detail", args=[supplier.pk])
        self.assertRedirects(
            response,
            reverse("invoices:invoice_type_create") + f"?fournisseur={supplier.pk}&source=EMAIL&retour={fiche}",
            fetch_redirect_response=False,
        )

    def test_retour_goes_back_with_the_supplier_named(self):
        back = reverse("invoices:invoice_type_create") + "?source=WEBSITE"
        response = self.client.post(
            CREATE + f"?retour={back.replace('?', '%3F').replace('=', '%3D')}",
            {"name": "Cave Exemple", "nature": "produits", "header": "", "arrivee": "import"},
        )
        supplier = Supplier.objects.get(name="Cave Exemple")
        self.assertRedirects(response, back + f"&fournisseur={supplier.pk}", fetch_redirect_response=False)

    def test_an_outside_retour_is_ignored(self):
        response = self.client.post(
            CREATE + "?retour=https://exemple.invalid/", {"name": "Cave Exemple", "nature": "produits", "arrivee": "import"}
        )
        supplier = Supplier.objects.get(name="Cave Exemple")
        self.assertRedirects(response, reverse("invoices:supplier_detail", args=[supplier.pk]))

    def test_the_suppliers_tab_offers_it(self):
        self.assertContains(self.client.get(reverse("invoices:supplier_list")), CREATE)

    def test_the_creation_is_undone_by_deleting_it_while_nothing_rests_on_it(self):
        self.create()
        supplier = Supplier.objects.get(name="Cave Exemple")
        created = SupplierChange.objects.get(supplier=supplier, kind=SupplierChange.Kind.CREATED)
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[supplier.pk]))
        self.assertContains(fiche, "Annuler la création…")
        undo = self.client.post(reverse("invoices:supplier_change_undo", args=[supplier.pk, created.pk]))
        self.assertRedirects(undo, reverse("invoices:supplier_delete", args=[supplier.pk]))
        make_invoice(supplier=supplier, ocr_text="CAVE EXEMPLE\nTOTAL 12,00")
        page = self.client.get(reverse("invoices:supplier_delete", args=[supplier.pk]))
        self.assertContains(page, "1 document y est rangé")
        self.client.post(reverse("invoices:supplier_delete", args=[supplier.pk]), {"confirme": "1"})
        self.assertTrue(Supplier.objects.filter(pk=supplier.pk).exists())


class EditTests(TestCase):
    """A supplier of charges: its poste and its lines carry its name."""

    def setUp(self):
        self.supplier = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)
        self.poste = expense_product(self.supplier)
        self.bills = []
        for day in (1, 2):
            bill = make_invoice(supplier=self.supplier, ocr_text=f"EAU EXEMPLE SERVICES\nLe {day:02d}/05/2026\nTOTAL 80,00")
            make_invoice_line(invoice=bill, product=self.poste, raw_name="Eau Exemple", total_ht=D("66.67"), vat_rate=D("0.20"))
            self.bills.append(bill)
        self.url = reverse("invoices:supplier_edit", args=[self.supplier.pk])

    def verify(self, **fields):
        page = self.client.get(self.url)
        data = {"name": self.supplier.name, "header": self.supplier.ticket_header, "version": page.context["version"]}
        data.update(fields)
        return self.client.post(self.url, {**data, "action": "verifier"})

    def save(self, checked):
        return self.client.post(self.url, {**checked.context["posted"], "action": "enregistrer"})

    def test_the_check_says_what_changes_and_changes_nothing(self):
        with mock.patch("invoices.receipt_batches.requeue_everywhere") as requeue:
            checked = self.verify(name="Eau Exemple Paris", header="EAU EXEMPLE SERVICES")
        self.assertEqual(checked.status_code, 200)
        self.assertContains(checked, "rien n'est encore enregistré")
        self.assertContains(checked, "2 lignes de charge")
        self.assertContains(checked, "imprimé sur 2 de ses 2 documents")
        self.supplier.refresh_from_db()
        self.assertEqual((self.supplier.name, self.supplier.ticket_header), ("Eau Exemple", ""))
        self.assertFalse(SupplierChange.objects.exists())
        requeue.assert_not_called()

    def test_a_rename_takes_its_charge_lines_and_poste_not_its_code(self):
        self.save(self.verify(name="Eau Exemple Paris"))
        self.supplier.refresh_from_db()
        self.assertEqual((self.supplier.name, self.supplier.code), ("Eau Exemple Paris", "EAU_X"))
        self.assertEqual(
            set(InvoiceLine.objects.filter(invoice__supplier=self.supplier).values_list("raw_name", flat=True)),
            {"Eau Exemple Paris"},
        )
        self.poste.refresh_from_db()
        self.assertEqual(self.poste.raw_name, "Eau Exemple Paris")
        renamed = SupplierChange.objects.get(supplier=self.supplier, kind=SupplierChange.Kind.RENAMED)
        self.assertEqual(renamed.data["before"], "Eau Exemple")

    def test_a_rename_meeting_a_poste_of_that_name_is_refused(self):
        Product.objects.create(supplier=self.supplier, raw_name="Eau Exemple Paris", is_expense=True)
        checked = self.verify(name="Eau Exemple Paris")
        self.assertContains(checked, "a déjà un poste « Eau Exemple Paris »")
        self.assertNotContains(checked, "Enregistrer ces changements")
        response = self.client.post(self.url, {**checked.context["posted"], "action": "enregistrer"})
        self.assertEqual(response.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.name, "Eau Exemple")

    def test_a_rename_to_a_name_taken_is_refused(self):
        make_supplier(code="EAU2_X", name="Eau Exemple Paris", parser_key="")
        checked = self.verify(name="eau exemple paris")
        self.assertContains(checked, "existe déjà")
        self.assertNotContains(checked, "Enregistrer ces changements")

    def test_a_rename_is_undone(self):
        self.save(self.verify(name="Eau Exemple Paris"))
        renamed = SupplierChange.objects.get(supplier=self.supplier, kind=SupplierChange.Kind.RENAMED)
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.supplier.pk, renamed.pk]))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.name, "Eau Exemple")
        self.poste.refresh_from_db()
        self.assertEqual(self.poste.raw_name, "Eau Exemple")
        renamed.refresh_from_db()
        self.assertIsNotNone(renamed.undone_at)

    def test_a_header_is_recorded_reads_the_waiting_files_again_and_is_undone(self):
        with mock.patch("invoices.receipt_batches.requeue_everywhere", return_value=0) as requeue:
            response = self.save(self.verify(header="EAU EXEMPLE SERVICES"))
        self.assertRedirects(response, reverse("invoices:supplier_detail", args=[self.supplier.pk]))
        requeue.assert_called_once()
        header = SupplierChange.objects.get(supplier=self.supplier, kind=SupplierChange.Kind.HEADER)
        self.assertEqual((header.data["before"], header.data["after"]), ("", "EAU EXEMPLE SERVICES"))
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.supplier.pk, header.pk]))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.ticket_header, "")

    def test_a_page_grown_stale_saves_nothing(self):
        checked = self.verify(name="Eau Exemple Paris")
        Supplier.objects.filter(pk=self.supplier.pk).update(ticket_header="EAU EXEMPLE SERVICES")
        response = self.save(checked)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "a changé depuis")
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.name, "Eau Exemple")

    def test_nothing_changed_is_said(self):
        checked = self.verify()
        self.assertContains(checked, "Rien ne change")
        self.assertNotContains(checked, "Enregistrer ces changements")

    def test_a_supplier_without_documents_saves_in_one_step(self):
        empty = make_supplier(code="VIDE_X", name="Vide Exemple", parser_key="")
        url = reverse("invoices:supplier_edit", args=[empty.pk])
        page = self.client.get(url)
        self.assertContains(page, "Enregistrer")
        response = self.client.post(
            url, {"name": "Vide Exemple Deux", "header": "", "version": page.context["version"], "action": "enregistrer"}
        )
        self.assertRedirects(response, reverse("invoices:supplier_detail", args=[empty.pk]))
        empty.refresh_from_db()
        self.assertEqual(empty.name, "Vide Exemple Deux")

    def test_a_configured_till_has_no_header_box(self):
        franprix = Supplier.objects.get(code="FRANPRIX")
        page = self.client.get(reverse("invoices:supplier_edit", args=[franprix.pk]))
        self.assertNotContains(page, 'name="header"')


class DeleteTests(TestCase):
    def test_an_empty_supplier_goes_with_what_only_it_held(self):
        supplier = make_supplier(code="VIDE_X", name="Vide Exemple", parser_key="")
        Product.objects.create(supplier=supplier, raw_name="ARTICLE EXEMPLE")
        ShopItemPrice.objects.create(supplier=supplier, unit_price_ttc=D("0.70"), label="Pain Exemple")
        CounterpartyAlias.objects.create(supplier=supplier, name="VIDE EX")
        url = reverse("invoices:supplier_delete", args=[supplier.pk])
        page = self.client.get(url)
        self.assertContains(page, "Cela ne se défait pas")
        self.assertContains(page, "1 prix connu")
        # Asked, not done.
        self.client.post(url, {})
        self.assertTrue(Supplier.objects.filter(pk=supplier.pk).exists())
        response = self.client.post(url, {"confirme": "1"})
        self.assertRedirects(response, reverse("invoices:supplier_list"), fetch_redirect_response=False)
        self.assertFalse(Supplier.objects.filter(pk=supplier.pk).exists())
        self.assertFalse(Product.objects.filter(raw_name="ARTICLE EXEMPLE").exists())

    def test_one_with_a_document_or_a_type_is_kept(self):
        with_document = make_supplier(code="DOC_X", name="Doc Exemple", parser_key="")
        make_invoice(supplier=with_document, ocr_text="DOC EXEMPLE\nTOTAL 1,00")
        with_type = make_supplier(code="TYPE_X", name="Type Exemple", parser_key="")
        InvoiceType.objects.create(supplier=with_type, name="Type Exemple - e-mail", source_kind=InvoiceType.SourceKind.EMAIL)
        for supplier, reason in ((with_document, "1 document y est rangé"), (with_type, "1 source récupère pour lui")):
            url = reverse("invoices:supplier_delete", args=[supplier.pk])
            self.assertContains(self.client.get(url), reason)
            response = self.client.post(url, {"confirme": "1"})
            self.assertRedirects(response, reverse("invoices:supplier_detail", args=[supplier.pk]))
            self.assertTrue(Supplier.objects.filter(pk=supplier.pk).exists())

    def test_a_till_or_a_reader_of_its_own_is_kept(self):
        for code in ("FRANPRIX", "METRO"):
            supplier = Supplier.objects.get(code=code)
            self.client.post(reverse("invoices:supplier_delete", args=[supplier.pk]), {"confirme": "1"})
            self.assertTrue(Supplier.objects.filter(pk=supplier.pk).exists())

    def test_the_fiche_says_why_it_cannot_be_deleted(self):
        supplier = make_supplier(code="DOC_X", name="Doc Exemple", parser_key="")
        make_invoice(supplier=supplier, ocr_text="DOC EXEMPLE\nTOTAL 1,00")
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[supplier.pk]))
        self.assertContains(fiche, "1 document y est rangé")
        self.assertContains(fiche, reverse("invoices:supplier_edit", args=[supplier.pk]))


class ReviewFindingsTests(TestCase):
    """What an adversarial review of these pages found (EditTests' supplier
    of charges, without running its tests again)."""

    setUp = EditTests.setUp
    verify = EditTests.verify
    save = EditTests.save

    def test_it_is_not_renamed_to_one_of_its_own_postes(self):
        """Its poste not named after it (a document naming its postes), the
        clash went unseen - and the rename's undo renamed that poste too."""
        Product.objects.filter(pk=self.poste.pk).update(raw_name="Assainissement")
        checked = self.verify(name="Assainissement")
        self.assertContains(checked, "a déjà un poste « Assainissement »")
        self.assertNotContains(checked, "Enregistrer ces changements")

    def test_a_postes_name_counts_whatever_its_case(self):
        Product.objects.create(supplier=self.supplier, raw_name="Assainissement", is_expense=True)
        self.assertContains(self.verify(name="ASSAINISSEMENT"), "a déjà un poste « ASSAINISSEMENT »")
        # Its own poste, renamed with it, is no clash.
        self.save(self.verify(name="EAU EXEMPLE"))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.name, "EAU EXEMPLE")

    def test_an_undo_has_no_undo_of_its_own(self):
        """Offered one, it redid the change in one click, nothing shown."""
        self.save(self.verify(name="Eau Exemple Paris"))
        renamed = SupplierChange.objects.get(supplier=self.supplier, kind=SupplierChange.Kind.RENAMED)
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.supplier.pk, renamed.pk]))
        back = SupplierChange.objects.filter(supplier=self.supplier, kind=SupplierChange.Kind.RENAMED).exclude(
            pk=renamed.pk
        ).get()
        self.assertEqual(back.data["undoes"], renamed.pk)
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[self.supplier.pk]))
        self.assertNotContains(fiche, "Rétablir « Eau Exemple Paris »")
        response = self.client.post(reverse("invoices:supplier_change_undo", args=[self.supplier.pk, back.pk]))
        self.assertIn("il ne s'annule pas lui-même", " ".join(messages_of(response)))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.name, "Eau Exemple")


class AccentedNameTests(TestCase):
    def test_a_name_differing_only_by_an_accented_capital_is_taken(self):
        """SQLite compares case-blind for plain letters only: "ÉPICERIE" and
        "Épicerie" made two suppliers."""
        make_supplier(code="EPI_X", name="Épicerie Exemple", parser_key="")
        response = self.client.post(CREATE, {"name": "ÉPICERIE EXEMPLE", "nature": "produits", "arrivee": "import"})
        self.assertContains(response, "existe déjà")
        self.assertEqual(Supplier.objects.filter(name__icontains="picerie").count(), 1)
        other = make_supplier(code="AUTRE_X", name="Autre Exemple", parser_key="")
        with self.assertRaises(ValueError):
            rename_supplier(other, "épicerie exemple")
        with self.assertRaises(ValueError):
            create_shop("ÉPICERIE   exemple")
