"""`manage.py reread_einvoices`: an electronic invoice filed at a total its
XML does not state is read again from that XML. Two bugs did that
(01/10/2026): OVH's FR80644402, 22,62 €, refiled at 20,00 € when OVH was
ticked « charges », and its credit note AFR1176742, -3,92 €, signed twice to
+3,27 €. Data invented."""

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from invoices.models import Invoice, InvoiceLine, Supplier
from invoices.receipts import import_document
from invoices.tests.einvoice_files import CII_TWO_RATES
from invoices.tests.test_einvoice_import import seller, write_xml

D = Decimal


class RereadEInvoicesTests(TestCase):
    def setUp(self):
        self.supplier = seller()
        Supplier.objects.filter(pk=self.supplier.pk).update(expenses_only=True)
        self.invoice = import_document(write_xml(self, "abonnement.xml", CII_TWO_RATES))

    def run_command(self, *args):
        out = StringIO()
        call_command("reread_einvoices", *args, stdout=out)
        return out.getvalue()

    def filed_wrong(self):
        """What the ticket reader made of it: a total it does not state."""
        Invoice.objects.filter(pk=self.invoice.pk).update(printed_total_ttc=D("20.00"))
        InvoiceLine.objects.filter(invoice=self.invoice).update(total_ht=D("20.00"), vat_rate=D("0"))

    def test_one_filed_at_another_total_is_read_again_from_its_xml(self):
        self.filed_wrong()
        said = self.run_command()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.printed_total_ttc, D("229.39"))
        self.assertEqual(self.invoice.total_ttc, D("229.39"))
        self.assertIn("Brasserie du Canal n° FA-2026-0042 : 20.00 € -> 229.39 €", said)

    def test_the_dry_run_changes_nothing(self):
        self.filed_wrong()
        said = self.run_command("--dry-run")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.printed_total_ttc, D("20.00"))
        self.assertIn("essai", said)

    def test_one_at_its_own_total_is_left_alone_even_with_its_lines_corrected(self):
        """A person who disagrees with a supplier corrects the lines and
        leaves the stated total: that is not undone here."""
        InvoiceLine.objects.filter(invoice=self.invoice).update(raw_name="Corrigé à la main")
        said = self.run_command()
        self.assertEqual(
            set(InvoiceLine.objects.filter(invoice=self.invoice).values_list("raw_name", flat=True)),
            {"Corrigé à la main"},
        )
        self.assertIn("Aucune facture électronique à relire.", said)

    def test_one_whose_file_is_gone_is_said_and_left(self):
        self.filed_wrong()
        self.invoice.source_file.storage.delete(self.invoice.source_file.name)
        said = self.run_command()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.printed_total_ttc, D("20.00"))
        self.assertIn("Laissé : Brasserie du Canal n° FA-2026-0042", said)
