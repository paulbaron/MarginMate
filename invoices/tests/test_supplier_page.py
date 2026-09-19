"""A supplier's own page (« fiche fournisseur »): what files its documents
under it, changed from there - and every change seen, and undone.

The Sources tab listed the shops with no way in, and a Charges box saving
on a click. Data invented (the fixtures of test_subscriptions).
"""

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from invoices.models import Supplier, SupplierChange
from invoices.tests.test_subscriptions import SIREN, Subscriptions
from tests.factories import make_invoice, make_supplier

MOBILE_SITE = "web:mobile.operateur-exemple.fr"


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class SourcesTabTests(TestCase):
    def test_rows_lead_to_the_suppliers_page(self):
        shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="", ticket_header="EPICERIE EXEMPLE")
        make_invoice(supplier=shop, ocr_text="EPICERIE EXEMPLE\nTOTAL 3,00")
        page = self.client.get(reverse("invoices:invoice_type_list"))
        fiche = reverse("invoices:supplier_detail", args=[shop.pk])
        self.assertContains(page, f'data-row-href="{fiche}"')
        self.assertContains(page, f'<a href="{fiche}">Epicerie Exemple</a>', html=True)

    def test_a_supplier_with_nothing_yet_is_not_called_a_known_till(self):
        make_supplier(code="NOUVEAU_X", name="Nouveau Exemple", parser_key="")
        page = self.client.get(reverse("invoices:invoice_type_list"))
        self.assertContains(page, "attend son premier document")
        self.assertNotContains(page, "caisse déjà connue")

    def test_charges_is_not_a_box_saving_on_a_click(self):
        make_supplier(code="LOYER_X", name="Loyer Exemple", parser_key="", expenses_only=True)
        page = self.client.get(reverse("invoices:invoice_type_list"))
        self.assertNotContains(page, 'name="expenses_only"')
        self.assertContains(page, "Charges")

    def test_the_documents_of_one_supplier_only(self):
        """A search for "Operateur" also finds "Operateur Mobile"."""
        one = make_supplier(code="OP_X", name="Operateur", parser_key="")
        other = make_supplier(code="OP_MOBILE_X", name="Operateur Mobile", parser_key="")
        mine = make_invoice(supplier=one, invoice_number="F-1")
        make_invoice(supplier=other, invoice_number="F-2")
        page = self.client.get(reverse("invoices:invoice_list") + f"?fournisseur={one.pk}")
        self.assertEqual([invoice.pk for invoice in page.context["invoices"]], [mine.pk])
        self.assertContains(page, "Fournisseur : Operateur")


class FichePageTests(Subscriptions):
    """The fixtures' operator: three box bills, two mobile bills printing
    the mobile company's number - and nothing learned."""

    def setUp(self):
        super().setUp()
        self.operator.ticket_identifiers = []
        self.operator.save()
        self.fiche = reverse("invoices:supplier_detail", args=[self.operator.pk])

    def post(self, action, identifier):
        return self.client.post(
            reverse("invoices:supplier_identifiers", args=[self.operator.pk]),
            {"action": action, "identifier": identifier},
            follow=True,
        )

    def test_the_page_offers_what_to_keep_never_the_customers_phone(self):
        page = self.client.get(self.fiche)
        self.assertNotContains(page, "Séparer")
        report = page.context["report"]
        retainable = {row["identifier"] for row in report["printed"] if row["can_keep"]}
        # The box's own support site is on three of the five: it may be kept too.
        self.assertLessEqual({f"siren:{SIREN}", MOBILE_SITE}, retainable)
        phone = next(row for row in report["printed"] if row["identifier"].startswith("tel:"))
        self.assertFalse(phone["can_keep"])
        self.assertIn("Grossiste Exemple", phone["reason"])

    def test_undoing_a_figure_no_longer_set_aside_is_recorded(self):
        """Only the list of what is set aside changed: the change showed as
        undone, and nothing in the history had undone it."""
        Supplier.objects.filter(pk=self.operator.pk).update(refused_identifiers=["tel:0612345678"])
        self.post("ne_plus_ecarter", "tel:0612345678")
        freed = SupplierChange.objects.get(supplier=self.operator, kind=SupplierChange.Kind.IDENTIFIERS)
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.operator.pk, freed.pk]))
        self.operator.refresh_from_db()
        self.assertEqual(self.operator.refused_identifiers, ["tel:0612345678"])
        undone_by = SupplierChange.objects.exclude(pk=freed.pk).get(supplier=self.operator)
        self.assertEqual(undone_by.data["undoes"], freed.pk)
        self.assertIn("de nouveau écarté", undone_by.summary)

    def test_keep_then_remove_then_stop_setting_aside_each_recorded(self):
        self.post("retenir", f"siren:{SIREN}")
        self.operator.refresh_from_db()
        self.assertEqual(self.operator.ticket_identifiers, [f"siren:{SIREN}"])

        self.post("retirer", f"siren:{SIREN}")
        self.operator.refresh_from_db()
        self.assertEqual((self.operator.ticket_identifiers, self.operator.refused_identifiers), ([], [f"siren:{SIREN}"]))

        self.post("ne_plus_ecarter", f"siren:{SIREN}")
        self.operator.refresh_from_db()
        self.assertEqual(self.operator.refused_identifiers, [])
        kinds = list(SupplierChange.objects.filter(supplier=self.operator).values_list("by_person", flat=True))
        self.assertEqual(kinds, [True, True, True])

    def test_a_change_is_undone_from_the_page(self):
        self.post("retenir", f"siren:{SIREN}")
        change = SupplierChange.objects.get(supplier=self.operator)
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.operator.pk, change.pk]))
        self.operator.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(self.operator.ticket_identifiers, [])
        self.assertIsNotNone(change.undone_at)

    def test_a_lost_figure_is_given_back_only_if_it_still_names_it(self):
        self.operator.ticket_identifiers = [MOBILE_SITE]
        self.operator.save()
        other = make_supplier(code="PARTENAIRE_X", name="Partenaire Exemple", parser_key="")
        make_invoice(supplier=other, ocr_text="PARTENAIRE\nvoir mobile.operateur-exemple.fr\nTOTAL 1,00")
        from invoices.receipts import _recheck

        _recheck(self.operator)
        change = SupplierChange.objects.get(supplier=self.operator)
        self.assertTrue(change.needs_review)
        response = self.client.post(
            reverse("invoices:supplier_change_undo", args=[self.operator.pk, change.pk]), follow=True
        )
        self.operator.refresh_from_db()
        self.assertEqual(self.operator.ticket_identifiers, [])
        self.assertTrue(any("Partenaire Exemple" in message for message in messages_of(response)))

    def test_a_change_to_see_is_said_until_seen(self):
        self.operator.ticket_identifiers = [MOBILE_SITE]
        self.operator.save()
        other = make_supplier(code="PARTENAIRE_X", name="Partenaire Exemple", parser_key="")
        make_invoice(supplier=other, ocr_text="PARTENAIRE\nvoir mobile.operateur-exemple.fr\nTOTAL 1,00")
        from invoices.receipts import _recheck

        _recheck(self.operator)
        change = SupplierChange.objects.get(supplier=self.operator)
        self.assertContains(self.client.get(self.fiche), "À voir")
        self.client.post(reverse("invoices:supplier_change_seen", args=[self.operator.pk, change.pk]))
        self.assertNotContains(self.client.get(self.fiche), "À voir")

    def test_tampered_values_are_a_message_never_an_error_page(self):
        response = self.post("retenir", "tel:0000000000")
        self.assertEqual(response.status_code, 200)
        self.operator.refresh_from_db()
        self.assertEqual(self.operator.ticket_identifiers, [])
        other = make_supplier(code="AUTRE_X", name="Autre Exemple", parser_key="")
        foreign = SupplierChange.objects.create(supplier=other, kind=SupplierChange.Kind.IDENTIFIERS, summary="x")
        response = self.client.post(reverse("invoices:supplier_change_undo", args=[self.operator.pk, foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_no_page_of_it_writes_on_a_get(self):
        before = (list(Supplier.objects.values()), SupplierChange.objects.count())
        for name in ("supplier_detail", "supplier_edit", "supplier_delete", "supplier_expenses"):
            self.assertEqual(self.client.get(reverse(f"invoices:{name}", args=[self.operator.pk])).status_code, 200)
        self.assertEqual((list(Supplier.objects.values()), SupplierChange.objects.count()), before)


class PagesItHasNotTests(TestCase):
    def test_the_ai_pseudo_supplier_has_no_page(self):
        response = self.client.get(reverse("invoices:supplier_detail", args=[Supplier.objects.get(code="OTHER").pk]))
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))

    def test_a_till_has_no_header_to_set(self):
        till = make_supplier(code="FRANPRIX", name="Franprix", parser_key="FRANPRIX")
        page = self.client.get(reverse("invoices:supplier_detail", args=[till.pk]))
        self.assertContains(page, "Reconnue par sa caisse")
        self.assertNotContains(page, "Séparer")
