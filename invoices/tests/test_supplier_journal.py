"""What changed in what recognises a supplier - when, why, from which
document - kept, and shown on its page (SupplierChange).

On 18/09 the figures a supplier had learned went without a word, as a side
effect of a correction validated on one of its documents: nothing said so,
nothing kept it, and the split link it held vanished with no trace to
follow. Every change is now recorded with its cause, and one that nobody
asked for is flagged to be seen. Data invented.
"""

import pathlib
import re
from datetime import date
from unittest import mock

from django.test import TestCase

from invoices import supplier_changes
from invoices.models import ScrapeJob, SupplierChange
from invoices.receipts import _recheck, learn_identifiers
from tests.factories import make_invoice, make_supplier

SITE = "web:quincaillerie-exemple.fr"
TEXT = "QUINCAILLERIE EXEMPLE\nwww.quincaillerie-exemple.fr\nTicket 1\nTOTAL 3,00"


class JournalTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="QUINCAILLERIE_X", name="Quincaillerie Exemple", parser_key="")

    def test_every_change_is_recorded_with_its_cause_and_document(self):
        ticket = make_invoice(supplier=self.shop, ocr_text=TEXT)
        with supplier_changes.cause("validation du ticket", invoice=ticket, by_person=True):
            learn_identifiers(self.shop, TEXT)
        change = SupplierChange.objects.get(supplier=self.shop)
        self.assertEqual(
            (change.kind, change.cause, change.invoice, change.by_person, change.needs_review),
            (SupplierChange.Kind.IDENTIFIERS, "validation du ticket", ticket, True, False),
        )
        self.assertIn("quincaillerie-exemple.fr", change.summary)
        self.assertEqual(change.data["gained"], [SITE])

    def test_a_drop_nobody_asked_for_names_its_reason_and_is_to_be_seen(self):
        self.shop.ticket_identifiers = [SITE]
        self.shop.save()
        make_invoice(supplier=self.shop, ocr_text=TEXT)
        other = make_supplier(code="AUTRE_X", name="Autre Exemple", parser_key="")
        make_invoice(supplier=other, ocr_text="AUTRE\npartenaire www.quincaillerie-exemple.fr\nTOTAL 9,00")
        _recheck(self.shop)
        change = SupplierChange.objects.get(supplier=self.shop)
        self.assertEqual((change.cause, change.needs_review), ("automatique", True))
        self.assertIn("Autre Exemple", change.summary)
        self.assertEqual(change.data["lost"], [SITE])

    def test_nothing_is_recorded_when_nothing_changed(self):
        self.shop.ticket_identifiers = [SITE]
        self.shop.save()
        make_invoice(supplier=self.shop, ocr_text=TEXT)
        learn_identifiers(self.shop, TEXT)
        self.assertFalse(SupplierChange.objects.exists())

    def test_a_figure_set_aside_by_hand_is_never_learned_again(self):
        self.shop.refused_identifiers = [SITE]
        self.shop.save()
        make_invoice(supplier=self.shop, ocr_text=TEXT)
        self.assertEqual(learn_identifiers(self.shop, TEXT), [])

    def test_the_changes_of_an_act_are_collected_to_be_said(self):
        with supplier_changes.collect() as changes:
            learn_identifiers(self.shop, TEXT)
        self.assertEqual([change.kind for change in changes], [SupplierChange.Kind.IDENTIFIERS])

    def test_the_gather_log_says_what_a_document_taught(self):
        from invoices.models import EmailInvoiceSource, InvoiceType
        from invoices.tasks import gather_invoices_task

        invoice_type = InvoiceType.objects.create(
            supplier=self.shop, name="Quincaillerie - Factures", source_kind=InvoiceType.SourceKind.EMAIL
        )
        EmailInvoiceSource.objects.create(invoice_type=invoice_type, sender_pattern="factures@")

        def imported(path, **kwargs):
            learn_identifiers(kwargs["supplier"], TEXT)

        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.scrape_email_invoices", return_value=[("/tmp/q.pdf", date(2026, 5, 2))]), \
                mock.patch("invoices.receipts.import_document", side_effect=imported):
            gather_invoices_task(job.id, date(2026, 5, 1), date(2026, 5, 31), {f"type-{invoice_type.id}"})
        job.refresh_from_db()
        self.assertIn("quincaillerie-exemple.fr", job.log)
        change = SupplierChange.objects.get(supplier=self.shop)
        self.assertIn("Quincaillerie - Factures", change.cause)


class OneWriterTests(TestCase):
    def test_only_set_identifiers_writes_what_names_a_supplier(self):
        """Every change goes through one function, which records it: a write
        elsewhere would change recognition without a trace."""
        root = pathlib.Path(__file__).resolve().parents[1]
        writers = []
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if re.search(r"\.ticket_identifiers\s*=(?!=)", line):
                    writers.append(f"{path.name}:{number}")
        self.assertEqual(len(writers), 1, writers)
