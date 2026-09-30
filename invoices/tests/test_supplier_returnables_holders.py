"""A supplier « Consignes » still names cannot be deleted, and its page says
why (spec §4, « Deletions »).

A slip format and a pickup hold their supplier with PROTECT. Before this,
`delete_refused` looked at documents and sources only: the page offered
« Supprimer… », and the delete then failed with « un de ses produits sert
encore (inventaire, recette) » - a false reason, about products the supplier
may not even have. Every name below is invented.
"""

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from invoices.models import Supplier
from invoices.supplier_views import delete_refused, returnables_refusal
from returnables.tests.support import make_format, make_pickup
from tests.factories import make_supplier


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class ReturnablesHoldersTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="BRASSEUR_X", name="Brasseur Exemple", parser_key="")

    def supplier_page(self):
        return self.client.get(reverse("invoices:supplier_detail", args=[self.supplier.pk]))

    def test_a_slip_format_holds_it_and_the_page_says_so(self):
        make_format(name="Brasseur Exemple — bon", supplier=self.supplier)
        reason = "1 format de bon de consignes est à son nom"
        self.assertEqual(delete_refused(self.supplier), reason)
        self.assertContains(self.supplier_page(), f'title="Ne peut pas être supprimé : {reason}"')

    def test_a_pickup_holds_it_and_the_page_says_so(self):
        make_pickup(supplier=self.supplier)
        make_pickup(supplier=self.supplier)
        reason = "2 reprises de consignes sont à son nom"
        self.assertEqual(delete_refused(self.supplier), reason)
        self.assertContains(self.supplier_page(), reason)

    def test_both_are_named_in_one_sentence(self):
        make_format(name="Brasseur Exemple — bon", supplier=self.supplier)
        make_pickup(supplier=self.supplier)
        self.assertEqual(
            returnables_refusal(self.supplier), "1 format de bon de consignes et 1 reprise de consignes sont à son nom"
        )

    def test_the_delete_is_refused_with_the_true_reason(self):
        make_pickup(supplier=self.supplier)
        url = reverse("invoices:supplier_delete", args=[self.supplier.pk])
        self.assertContains(self.client.get(url), "1 reprise de consignes est à son nom")
        response = self.client.post(url, {"confirme": "1"})
        self.assertRedirects(response, reverse("invoices:supplier_detail", args=[self.supplier.pk]))
        self.assertTrue(Supplier.objects.filter(pk=self.supplier.pk).exists())
        said = messages_of(response)
        self.assertEqual(said, ["Brasseur Exemple n'est pas supprimé : 1 reprise de consignes est à son nom."])
        self.assertNotIn("un de ses produits", " ".join(said))

    def test_a_supplier_returnables_do_not_name_can_still_be_deleted(self):
        make_pickup(supplier=None)  # « fournisseur non précisé »: holds nobody
        self.assertEqual(delete_refused(self.supplier), "")
        response = self.client.post(reverse("invoices:supplier_delete", args=[self.supplier.pk]), {"confirme": "1"})
        self.assertRedirects(response, reverse("invoices:supplier_list"), fetch_redirect_response=False)
        self.assertFalse(Supplier.objects.filter(pk=self.supplier.pk).exists())

    def test_documents_are_still_said_first(self):
        from tests.factories import make_invoice

        make_invoice(supplier=self.supplier, ocr_text="BRASSEUR EXEMPLE\nTOTAL 1,00")
        make_pickup(supplier=self.supplier)
        self.assertEqual(delete_refused(self.supplier), "1 document y est rangé")
