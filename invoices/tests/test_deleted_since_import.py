"""A document deleted after the import that created it.

The import keeps its own record of every file (`ReceiptBatch.results`), and
that record still names the document it became after the document is gone.
The page drew such a row as « À vérifier » with a « Vérifier » button leading
to a 404 - the page is meant to show each document as it is now
(`workspace.batch_rows`), and « deleted » is how this one is. Data invented.
"""

from django.test import TestCase
from django.urls import reverse

from invoices.models import Invoice, ReceiptBatch
from tests.factories import make_invoice, make_supplier


class DeletedSinceTheImportTests(TestCase):
    def setUp(self):
        shop = make_supplier(code="EPICERIE", name="Épicerie Exemple")
        self.kept = make_invoice(supplier=shop)
        gone = make_invoice(supplier=shop)
        self.gone_pk = gone.pk
        gone.delete()
        self.invoice = make_invoice(supplier=shop)
        self.gone_invoice_pk = self.invoice.pk
        self.invoice.delete()
        self.batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {"name": "ticket-1.jpg", "status": "ok", "invoice_id": self.kept.pk, "receipt": True, "shop": "Épicerie Exemple"},
                {"name": "ticket-2.jpg", "status": "ok", "invoice_id": self.gone_pk, "receipt": True, "shop": "Épicerie Exemple"},
                {"name": "facture.pdf", "status": "ok", "invoice_id": self.gone_invoice_pk, "receipt": False, "shop": "Épicerie Exemple"},
            ],
        )
        self.page = reverse("invoices:receipt_batch", args=[self.batch.pk])

    def test_the_import_says_the_document_was_deleted_and_offers_no_link_to_it(self):
        response = self.client.get(self.page)
        self.assertContains(response, "Supprimé depuis l'import", count=2)
        self.assertNotContains(response, reverse("invoices:receipt_review", args=[self.gone_pk]))
        self.assertNotContains(response, reverse("invoices:invoice_edit_lines", args=[self.gone_invoice_pk]))
        # The document still there keeps its link.
        self.assertContains(response, reverse("invoices:receipt_review", args=[self.kept.pk]) + f"?lot={self.batch.pk}")

    def test_an_old_link_into_the_import_goes_back_to_it_and_says_why(self):
        url = reverse("invoices:receipt_review", args=[self.gone_pk]) + f"?lot={self.batch.pk}"
        response = self.client.get(url, follow=True)
        self.assertRedirects(response, self.page)
        # The message is escaped like any other (« l&#x27;import »).
        self.assertContains(response, "Ce ticket a été supprimé depuis")

    def test_without_an_import_a_deleted_ticket_is_still_not_found(self):
        response = self.client.get(reverse("invoices:receipt_review", args=[self.gone_pk]))
        self.assertEqual(response.status_code, 404)

    def test_an_import_that_does_not_name_the_ticket_does_not_hide_the_404(self):
        other = ReceiptBatch.objects.create(status=ReceiptBatch.Status.SUCCESS, results=[])
        url = reverse("invoices:receipt_review", args=[self.gone_pk]) + f"?lot={other.pk}"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_kept_document_is_untouched(self):
        self.assertTrue(Invoice.objects.filter(pk=self.kept.pk).exists())
