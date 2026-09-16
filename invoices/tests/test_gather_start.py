"""Where a search for new invoices starts by default.

It used to be the earliest of each source's latest invoice, minus three days.
Champagne Depoivre bills twice a year, so every gather went ten months back:
four Metro date windows and 3,848 emails, for invoices already imported.
"""

from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices.models import Supplier
from invoices.tasks import DEFAULT_LOOKBACK_DAYS, default_gather_start
from tests.factories import make_invoice, make_invoice_type, make_supplier


class DefaultGatherStartTests(TestCase):
    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        self.uba = Supplier.objects.get(code="UBA")
        self.depoivre = make_supplier(code="DEPOIVRE", name="Champagne", parser_key="DEPOIVRE")
        self.gathered = {self.metro.pk, self.uba.pk, self.depoivre.pk}
        make_invoice(supplier=self.metro, invoice_date=date(2026, 8, 28))
        make_invoice(supplier=self.uba, invoice_date=date(2026, 8, 5))
        make_invoice(supplier=self.depoivre, invoice_date=date(2025, 11, 22))

    def test_the_search_starts_at_the_newest_invoice_already_in(self):
        self.assertEqual(default_gather_start(self.gathered), date(2026, 8, 28))

    def test_a_receipt_is_not_where_a_gather_left_off(self):
        make_invoice(supplier=self.metro, invoice_date=date(2026, 9, 10), ocr_text="METRO ...")
        self.assertEqual(default_gather_start(self.gathered), date(2026, 8, 28))

    def test_a_source_that_is_not_gathered_does_not_count(self):
        make_invoice(supplier=make_supplier(code="SHOP"), invoice_date=date(2026, 9, 10))
        self.assertEqual(default_gather_start(self.gathered), date(2026, 8, 28))

    def test_a_date_in_the_future_is_a_misreading(self):
        make_invoice(supplier=self.metro, invoice_date=timezone.localdate() + timedelta(days=30))
        self.assertEqual(default_gather_start(self.gathered), date(2026, 8, 28))

    def test_with_nothing_imported_yet_it_looks_back_a_while(self):
        self.assertEqual(
            default_gather_start({make_supplier(code="NEW").pk}),
            timezone.localdate() - timedelta(days=DEFAULT_LOOKBACK_DAYS),
        )

    def test_the_invoices_page_offers_it(self):
        make_invoice_type(supplier=self.uba, parser_key="UBA")
        make_invoice_type(supplier=self.depoivre, parser_key="DEPOIVRE")
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertEqual(response.context["default_start_date"], date(2026, 8, 28))
        self.assertContains(response, 'value="2026-08-28"')
