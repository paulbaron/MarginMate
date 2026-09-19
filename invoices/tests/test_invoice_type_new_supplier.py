"""An invoice type for a supplier that does not exist yet - created with it,
in one save - and a type moved from one supplier to another, recorded on
both and undone.

A new source had to name an existing supplier: one that had never sent a
document could not be given a type fetching its first. Data invented.
"""

from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from invoices.models import InvoiceType, Supplier, SupplierChange
from tests.factories import make_supplier

CREATE = reverse("invoices:invoice_type_create")


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def email_type(**fields):
    data = {
        "name": "Traiteur Exemple - Factures", "supplier": "new", "new_name": "Traiteur Exemple",
        "source_kind": "EMAIL", "parser_key": "", "is_active": "on", "action": "save",
        "sender_pattern": r"factures@traiteur\.exemple", "subject_pattern": "", "body_pattern": "",
        "attachment_pattern": r"\.pdf$",
    }
    data.update(fields)
    return data


class NewSupplierTests(TestCase):
    def test_the_supplier_and_its_type_are_saved_together(self):
        response = self.client.post(CREATE, email_type(new_expenses="on"))
        self.assertRedirects(response, reverse("invoices:invoice_type_list"), fetch_redirect_response=False)
        supplier = Supplier.objects.get(name="Traiteur Exemple")
        self.assertTrue(supplier.expenses_only)
        self.assertEqual(InvoiceType.objects.get(name="Traiteur Exemple - Factures").supplier, supplier)
        created = SupplierChange.objects.get(supplier=supplier, kind=SupplierChange.Kind.CREATED)
        self.assertIn("Traiteur Exemple - Factures", created.cause)

    def test_an_invalid_type_creates_no_supplier(self):
        response = self.client.post(CREATE, email_type(name="", attachment_pattern="("))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Supplier.objects.filter(name="Traiteur Exemple").exists())

    def test_a_new_supplier_needs_a_name_and_one_not_taken(self):
        response = self.client.post(CREATE, email_type(new_name="  "))
        self.assertContains(response, "Donnez un nom au nouveau fournisseur")
        make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        response = self.client.post(CREATE, email_type(new_name="traiteur exemple"))
        self.assertContains(response, "existe déjà")
        self.assertFalse(InvoiceType.objects.filter(name="Traiteur Exemple - Factures").exists())

    def test_tester_creates_nothing(self):
        with mock.patch("invoices.views.threading.Thread"):
            response = self.client.post(CREATE, email_type(action="test"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Supplier.objects.filter(name="Traiteur Exemple").exists())

    def test_the_fiche_fills_it_in_and_is_where_it_goes_back(self):
        supplier = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        fiche = reverse("invoices:supplier_detail", args=[supplier.pk])
        page = self.client.get(CREATE + f"?fournisseur={supplier.pk}&source=WEBSITE&retour={fiche}")
        self.assertEqual(page.context["supplier_selected"], str(supplier.pk))
        self.assertTrue(page.context["is_website"])
        self.assertContains(page, f'name="retour" value="{fiche}"')
        response = self.client.post(
            CREATE, email_type(supplier=str(supplier.pk), new_name="", retour=fiche)
        )
        self.assertRedirects(response, fiche)

    def test_an_outside_retour_is_ignored(self):
        supplier = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        response = self.client.post(CREATE, email_type(supplier=str(supplier.pk), retour="https://exemple.invalid/"))
        self.assertRedirects(response, reverse("invoices:invoice_type_list"), fetch_redirect_response=False)

    def test_a_tampered_supplier_is_a_message(self):
        response = self.client.post(CREATE, email_type(supplier="abc"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fournisseur inconnu")


class MovedTypeTests(TestCase):
    def setUp(self):
        self.box = make_supplier(code="BOX_X", name="Box Exemple", parser_key="", expenses_only=True)
        self.mobile = make_supplier(code="MOBILE_X", name="Mobile Exemple", parser_key="", expenses_only=True)
        self.invoice_type = InvoiceType.objects.create(
            supplier=self.box, name="Mobile - Espace abonné", source_kind=InvoiceType.SourceKind.EMAIL
        )
        self.url = reverse("invoices:invoice_type_update", args=[self.invoice_type.pk])

    def test_moving_it_is_said_recorded_on_both_and_undone(self):
        response = self.client.post(
            self.url, email_type(name="Mobile - Espace abonné", supplier=str(self.mobile.pk), new_name="")
        )
        said = " ".join(messages_of(response))
        self.assertIn("récupère désormais pour Mobile Exemple (avant : Box Exemple)", said)
        self.assertIn(
            "Les documents déjà récupérés restent chez Box Exemple : s'ils sont de Mobile Exemple, changez-les de "
            "fournisseur depuis leur page.",
            said,
        )
        self.assertNotIn("sépar", said.lower())
        left = SupplierChange.objects.get(supplier=self.box, kind=SupplierChange.Kind.TYPES)
        came = SupplierChange.objects.get(supplier=self.mobile, kind=SupplierChange.Kind.TYPES)
        self.assertEqual(left.operation, came.operation)
        fiche = self.client.get(reverse("invoices:supplier_detail", args=[self.box.pk]))
        self.assertContains(fiche, "Rendre ce type à Box Exemple")
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.box.pk, left.pk]))
        self.invoice_type.refresh_from_db()
        self.assertEqual(self.invoice_type.supplier, self.box)
        self.assertFalse(SupplierChange.objects.filter(operation=left.operation, undone_at__isnull=True).exists())

    def test_the_undo_is_refused_once_it_moved_again(self):
        self.client.post(self.url, email_type(name="Mobile - Espace abonné", supplier=str(self.mobile.pk), new_name=""))
        left = SupplierChange.objects.get(supplier=self.box, kind=SupplierChange.Kind.TYPES)
        other = make_supplier(code="AUTRE_X", name="Autre Exemple", parser_key="", expenses_only=True)
        InvoiceType.objects.filter(pk=self.invoice_type.pk).update(supplier=other)
        response = self.client.post(reverse("invoices:supplier_change_undo", args=[self.box.pk, left.pk]))
        self.assertIn("n'est plus chez Mobile Exemple", " ".join(messages_of(response)))
        self.invoice_type.refresh_from_db()
        self.assertEqual(self.invoice_type.supplier, other)

    def test_saving_it_unchanged_records_nothing(self):
        self.client.post(self.url, email_type(name="Mobile - Espace abonné", supplier=str(self.box.pk), new_name=""))
        self.assertFalse(SupplierChange.objects.filter(kind=SupplierChange.Kind.TYPES).exists())

    def test_the_edit_page_opens_the_suppliers_page(self):
        page = self.client.get(self.url)
        self.assertContains(page, reverse("invoices:supplier_detail", args=[self.box.pk]))
        self.assertEqual(page.context["supplier_selected"], str(self.box.pk))

    def test_a_page_drawn_before_the_type_moved_does_not_move_it_back(self):
        """A « Rendre ce type » in another tab: saved as it was drawn, the
        page moved it back, unseen."""
        page = self.client.get(self.url)
        self.assertContains(page, f'name="supplier_was" value="{self.box.pk}"')
        InvoiceType.objects.filter(pk=self.invoice_type.pk).update(supplier=self.mobile)
        response = self.client.post(
            self.url,
            email_type(name="Mobile - Espace abonné", supplier=str(self.box.pk), new_name="", supplier_was=str(self.box.pk)),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "depuis l&#x27;ouverture de cette page")
        self.invoice_type.refresh_from_db()
        self.assertEqual(self.invoice_type.supplier, self.mobile)

    def test_the_type_given_back_is_not_taken_again_from_its_record(self):
        self.client.post(self.url, email_type(name="Mobile - Espace abonné", supplier=str(self.mobile.pk), new_name=""))
        left = SupplierChange.objects.get(supplier=self.box, kind=SupplierChange.Kind.TYPES)
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.box.pk, left.pk]))
        given = next(
            row for row in SupplierChange.objects.filter(supplier=self.box, kind=SupplierChange.Kind.TYPES)
            if (row.data or {}).get("undoes")
        )
        response = self.client.post(reverse("invoices:supplier_change_undo", args=[self.box.pk, given.pk]))
        self.assertIn("il ne s'annule pas lui-même", " ".join(messages_of(response)))
        self.invoice_type.refresh_from_db()
        self.assertEqual(self.invoice_type.supplier, self.box)


class TamperedTypeFormTests(TestCase):
    def test_unicode_digits_are_a_message_never_an_error_page(self):
        response = self.client.post(CREATE, email_type(supplier="²"))
        self.assertContains(response, "Fournisseur inconnu")
        site = {
            "source_kind": "WEBSITE", "site-login_url": "https://portail.exemple.invalid/login",
            "site-username_env": "X_LOGIN", "site-password_env": "X_PASSWORD",
        }
        with mock.patch("invoices.views.threading.Thread"):
            response = self.client.post(CREATE, {**email_type(supplier="²", action="test"), **site})
        self.assertEqual(response.status_code, 200)

class RedrawnTypePageTests(TestCase):
    setUp = MovedTypeTests.setUp

    def test_a_page_redrawn_by_tester_keeps_what_it_was_drawn_with(self):
        """Redrawn after « Tester », it took the type's supplier from the
        database again, and the next save moved the type back unrefused."""
        InvoiceType.objects.filter(pk=self.invoice_type.pk).update(supplier=self.mobile)
        with mock.patch("invoices.views.threading.Thread"):
            redrawn = self.client.post(
                self.url,
                email_type(name="Mobile - Espace abonné", supplier=str(self.box.pk), new_name="",
                           supplier_was=str(self.box.pk), action="test"),
            )
        self.assertContains(redrawn, f'name="supplier_was" value="{self.box.pk}"')
        response = self.client.post(
            self.url,
            email_type(name="Mobile - Espace abonné", supplier=str(self.box.pk), new_name="", supplier_was=str(self.box.pk)),
        )
        self.assertContains(response, "depuis l&#x27;ouverture de cette page")
        # Refused, the page now carries where the type is: saving again is a choice.
        self.assertContains(response, f'name="supplier_was" value="{self.mobile.pk}"')
        self.invoice_type.refresh_from_db()
        self.assertEqual(self.invoice_type.supplier, self.mobile)
