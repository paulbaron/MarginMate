"""What a new espace keeps of the suppliers the seed migrations install
(invoices/seeds.py). The test database holds every seed, as the owner's
espace and the template do; a real new espace is in
accounts/tests/test_provisioning.py."""

from django.db.models import ProtectedError
from django.test import TestCase

from accounts import provisioning
from invoices import seeds
from invoices.models import EmailInvoiceSource, Invoice, InvoiceType, Supplier
from returnables.models import ReturnableType, SlipFormat
from tests.factories import make_invoice


class ForgetOriginalBarSuppliersTests(TestCase):
    def test_the_original_bar_s_suppliers_go_with_their_source_and_slip_format(self):
        seeds.forget_original_bar_suppliers()
        self.assertEqual(set(Supplier.objects.values_list("code", flat=True)), {"METRO", "FRANPRIX", "MONOPRIX"})
        self.assertFalse(InvoiceType.objects.exists())
        self.assertFalse(EmailInvoiceSource.objects.exists())
        self.assertFalse(SlipFormat.objects.exists())
        self.assertEqual(ReturnableType.objects.count(), 3)
        # Twice is once.
        seeds.forget_original_bar_suppliers()
        self.assertEqual(Supplier.objects.count(), 3)

    def test_a_source_or_a_format_of_another_supplier_stays(self):
        franprix = Supplier.objects.get(code="FRANPRIX")
        InvoiceType.objects.create(name="Franprix - tickets", supplier=franprix)
        seeds.forget_original_bar_suppliers()
        self.assertEqual(list(InvoiceType.objects.values_list("name", flat=True)), ["Franprix - tickets"])

    def test_one_in_use_stops_it_and_nothing_goes(self):
        """Never on a fresh copy of the template; if it ever happens, the
        espace's creation fails and removes its folder (prepare_tenant)."""
        make_invoice(supplier=Supplier.objects.get(code="WINGSENG"))
        with self.assertRaises(ProtectedError):
            seeds.forget_original_bar_suppliers()
        self.assertEqual(Supplier.objects.filter(code__in=seeds.ORIGINAL_BAR_SUPPLIERS).count(), 3)
        self.assertTrue(InvoiceType.objects.filter(supplier__code="UBA").exists())
        self.assertTrue(SlipFormat.objects.filter(supplier__code="UBA").exists())
        self.assertEqual(Invoice.objects.count(), 1)

    def test_it_is_a_step_of_every_new_hosted_espace(self):
        self.assertIn("invoices.seeds.forget_original_bar_suppliers", provisioning.HOSTED_ESPACE_STEPS)
