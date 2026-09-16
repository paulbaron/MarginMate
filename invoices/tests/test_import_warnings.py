"""What an import says when the parser read nothing, or read lines that don't
add up - and what an invoice with no lines says about its supplier.

A Plou & Fils invoice in a new layout imported with no lines, and its page
said "Ce fournisseur n'a pas de parseur" - which was not the problem.
"""

import os
from datetime import date
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from invoices.importing import parse_and_import
from invoices.models import Invoice
from invoices.parsers.base import ParsedInvoice, ParsedLine
from tests.factories import make_invoice, make_product, make_stock_type, make_supplier


class FakeParser:
    def __init__(self, parsed):
        self.parsed = parsed

    def parse(self, pdf_path, date_hint=None):
        return self.parsed


def parsed(lines=(), warnings=()):
    return ParsedInvoice(
        supplier_code="PLOUFILS",
        invoice_number="FA-202604-0001",
        invoice_date=date(2026, 4, 30),
        lines=list(lines),
        warnings=list(warnings),
    )


class ImportWarningTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="PLOUFILS", name="SCEA Plou & Fils", parser_key="PLOUFILS")
        self.path = os.path.join(settings.MEDIA_ROOT, "plou-exemple.pdf")
        with open(self.path, "wb") as handle:
            handle.write(b"%PDF-1.4 exemple")

    def import_with(self, result):
        with mock.patch.dict("invoices.parsers.registry.PARSER_REGISTRY", {"PLOUFILS": FakeParser(result)}):
            return parse_and_import(self.path, self.supplier)

    def test_a_parser_that_finds_nothing_says_so_on_the_invoice(self):
        invoice = self.import_with(parsed())
        self.assertIn("n'a trouvé aucune ligne", invoice.error_message)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)

    def test_a_parser_warning_is_kept_and_holds_the_invoice_for_review(self):
        """Even when every product is known and the invoice would otherwise
        be complete."""
        stock_type = make_stock_type()
        make_product(supplier=self.supplier, raw_name="VIN EXEMPLE", stock_type=stock_type)
        line = ParsedLine(
            raw_name="VIN EXEMPLE", quantity=6, total_volume=Decimal("0"), unit_cost_ht=Decimal("5"),
            total_ht=Decimal("30"), vat_rate=Decimal("0.20"),
        )
        invoice = self.import_with(parsed([line], warnings=["Les lignes lues font 30 € HT, la facture imprime 60 €."]))
        self.assertIn("la facture imprime 60", invoice.error_message)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)

    def test_a_supplier_without_a_parser_is_filed_empty_without_complaint(self):
        """By design: its lines are typed in by hand."""
        supplier = make_supplier(code="NOPARSER", parser_key="")
        invoice = parse_and_import(self.path, supplier)
        self.assertEqual(invoice.error_message, "")


class EmptyInvoicePageTests(TestCase):
    def test_a_supplier_with_a_parser_is_not_said_to_have_none(self):
        supplier = make_supplier(code="PLOUFILS", name="SCEA Plou & Fils", parser_key="PLOUFILS")
        invoice = make_invoice(supplier=supplier)
        response = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "n'a rien trouvé dans ce document")
        self.assertNotContains(response, "n'a pas de parseur")
        edit = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(edit, "Corrigez ce que le parseur a mal lu")

    def test_a_supplier_without_one_is(self):
        invoice = make_invoice(supplier=make_supplier(code="NOPARSER", parser_key=""))
        response = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "n'a pas de parseur")
