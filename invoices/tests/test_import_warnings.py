"""What an import says when the parser read nothing, or read lines that don't
add up - and what an invoice with no lines says about its supplier.

A wine grower's invoice in a new layout imported with no lines, and its page
said "Ce fournisseur n'a pas de parseur" - which was not the problem. The
supplier here has a reader of its own, invented like the rest: what is being
tested is what the page says, not whose invoice it is.
"""

import os
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts import paths
from invoices.importing import parse_and_import
from invoices.models import Invoice
from invoices.parsers.base import ParsedInvoice, ParsedLine
from invoices.receipts import reread_document
from tests.factories import make_invoice, make_product, make_stock_type, make_supplier


class FakeParser:
    # Read back by receipts._reread_invoice_file, which refuses to re-read a
    # document the LLM fallback produced.
    supplier_code = "GROSSISTE"

    def __init__(self, parsed):
        self.parsed = parsed

    def parse(self, pdf_path, date_hint=None):
        return self.parsed


def parsed(lines=(), warnings=()):
    return ParsedInvoice(
        supplier_code="GROSSISTE",
        invoice_number="FA-202604-0001",
        invoice_date=date(2026, 4, 30),
        lines=list(lines),
        warnings=list(warnings),
    )


class ImportWarningTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="GROSSISTE", name="Grossiste Exemple", parser_key="GROSSISTE")
        self.path = os.path.join(paths.media_root(), "grossiste-exemple.pdf")
        with open(self.path, "wb") as handle:
            handle.write(b"%PDF-1.4 exemple")

    def import_with(self, result):
        with mock.patch.dict("invoices.parsers.registry.PARSER_REGISTRY", {"GROSSISTE": FakeParser(result)}):
            return parse_and_import(self.path, self.supplier)

    def test_a_parser_that_finds_nothing_says_so_on_the_invoice(self):
        invoice = self.import_with(parsed())
        self.assertIn("n'a trouvé aucune ligne", invoice.error_message)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)

    def test_it_is_named_by_its_label_not_its_registry_key(self):
        """« Le parseur CECINA » was the code's word and key: the page calls
        it a « lecteur », by its name. A reader without a label is named by
        its key."""
        self.assertIn("Le lecteur GROSSISTE n'a trouvé aucune ligne", self.import_with(parsed()).error_message)
        Invoice.objects.all().delete()
        reader = FakeParser(parsed())
        reader.label = "Grossiste Exemple (Halles)"
        with mock.patch.dict("invoices.parsers.registry.PARSER_REGISTRY", {"GROSSISTE": reader}):
            invoice = parse_and_import(self.path, self.supplier)
        self.assertIn("Le lecteur Grossiste Exemple (Halles) n'a trouvé aucune ligne", invoice.error_message)

    def test_the_ai_reader_is_named_as_the_import_s_select_names_it(self):
        """« Le lecteur LLM » was the registry's key; the PDF import's select
        calls that group « Analyse IA »."""
        from invoices.models import InvoiceType
        from invoices.parsers import LLM_PARSER_KEY

        self.supplier.parser_key = LLM_PARSER_KEY
        self.supplier.save()
        with mock.patch.dict("invoices.parsers.registry.PARSER_REGISTRY", {LLM_PARSER_KEY: FakeParser(parsed())}):
            invoice = parse_and_import(self.path, self.supplier)
        self.assertIn("Le lecteur Analyse IA n'a trouvé aucune ligne", invoice.error_message)
        self.assertNotIn("LLM", invoice.error_message)
        self.assertEqual(InvoiceType(parser_key=LLM_PARSER_KEY).reader_name, "Analyse IA")

    def test_a_parser_warning_is_kept_and_holds_the_invoice_for_review(self):
        """Even when every product is known and the invoice would otherwise
        be complete."""
        stock_type = make_stock_type()
        make_product(supplier=self.supplier, raw_name="VIN EXEMPLE", stock_type=stock_type)
        line = ParsedLine(
            raw_name="VIN EXEMPLE",
            quantity=6,
            total_volume=Decimal("0"),
            unit_cost_ht=Decimal("5"),
            total_ht=Decimal("30"),
            vat_rate=Decimal("0.20"),
        )
        invoice = self.import_with(parsed([line], warnings=["Les lignes lues font 30 € HT, la facture imprime 60 €."]))
        self.assertIn("la facture imprime 60", invoice.error_message)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)

    def test_a_supplier_without_a_parser_is_filed_empty_without_complaint(self):
        """By design: its lines are typed in by hand."""
        supplier = make_supplier(code="NOPARSER", parser_key="")
        invoice = parse_and_import(self.path, supplier, date_hint=date(2026, 5, 1))
        self.assertEqual(invoice.error_message, "")

    def test_an_invoice_with_no_date_says_so(self):
        """Undated, it counts in no stock valuation and matches no payment."""
        supplier = make_supplier(code="NOPARSER", parser_key="")
        invoice = parse_and_import(self.path, supplier)
        self.assertIn("Date introuvable", invoice.error_message)


class EmptyInvoicePageTests(TestCase):
    def test_a_supplier_with_a_parser_is_not_said_to_have_none(self):
        supplier = make_supplier(code="GROSSISTE", name="Grossiste Exemple", parser_key="METRO")
        invoice = make_invoice(supplier=supplier)
        response = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "Aucune ligne lue dans ce document")
        self.assertNotContains(response, "n'a pas de lecteur dédié")
        edit = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk]))
        self.assertContains(edit, "Corrigez ce qui a été mal lu")

    def test_a_supplier_without_one_is(self):
        invoice = make_invoice(supplier=make_supplier(code="NOPARSER", parser_key=""))
        response = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "n'a pas de lecteur dédié")
        self.assertNotContains(response, "parseur")


class WarnedInvoiceIsFoundTests(TestCase):
    """An invoice whose lines do not add up to its own printed total holds
    itself for review - and then has to be somewhere a person will find it.

    Until 24/09/2026 it was in neither queue. The ticket queue keys on
    `parse_checks`, which is what tells a photographed ticket from a digital
    invoice (invoices/workspace.IS_TICKET), so a supplier's PDF must not
    fill it - every Metro invoice would have queued behind the tickets.
    And « Documents à corriger » asked only for an undated or errored
    document. So a Metro invoice saying "une ligne n'a pas été lue" was
    counted nowhere at all, which is the same silence the warning exists to
    break.
    """

    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")

    def invoice(self, **kwargs):
        return make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, 6), **kwargs)

    def test_a_warned_invoice_is_among_the_documents_to_fix(self):
        from invoices.workspace import DOCUMENT_TO_FIX

        warned = self.invoice(
            status=Invoice.Status.NEEDS_REVIEW,
            error_message="Les lignes lues font 696,40 € HT alors que la facture imprime 732,78 € HT.",
        )
        self.assertIn(warned.pk, [found.pk for found in Invoice.objects.filter(DOCUMENT_TO_FIX)])

    def test_its_row_says_what_is_wrong_rather_than_products_to_sort(self):
        warned = self.invoice(status=Invoice.Status.NEEDS_REVIEW, error_message="Une ligne n'a pas été lue.")
        self.assertEqual(warned.review_state["label"], "À corriger")

    def test_it_never_enters_the_ticket_queue(self):
        from invoices.workspace import TICKET_TO_CHECK

        warned = self.invoice(status=Invoice.Status.NEEDS_REVIEW, error_message="Une ligne n'a pas été lue.")
        self.assertNotIn(warned.pk, [found.pk for found in Invoice.objects.filter(TICKET_TO_CHECK)])

    def test_an_invoice_only_waiting_on_its_products_is_still_called_that(self):
        """NEEDS_REVIEW with nothing said is the ordinary "a product needs
        classifying" case, and it is not a document to correct."""
        from invoices.workspace import DOCUMENT_TO_FIX

        ordinary = self.invoice(status=Invoice.Status.NEEDS_REVIEW)
        self.assertEqual(ordinary.review_state["label"], "Produits à classer")
        self.assertNotIn(ordinary.pk, [found.pk for found in Invoice.objects.filter(DOCUMENT_TO_FIX)])

    def test_a_finished_invoice_keeping_an_old_message_is_left_where_it_is(self):
        """A real invoice is COMPLETE - a scan with no text layer whose lines
        were typed in by hand - and still carries the message from the
        import that found none. It is done, and must not be dragged back."""
        from invoices.workspace import DOCUMENT_TO_FIX

        done = self.invoice(
            status=Invoice.Status.COMPLETE,
            error_message="Le parseur METRO n'a trouvé aucune ligne dans ce document.",
        )
        self.assertNotIn(done.pk, [found.pk for found in Invoice.objects.filter(DOCUMENT_TO_FIX)])


class InvoiceVatTableTests(TestCase):
    """A supplier invoice's own VAT table, on import and on a re-read.

    Metro prints one and the parser ignored it, so `vat_breakdown` was empty
    on every Metro invoice: nothing had a second figure to disagree with.
    Now that the import stores it, a re-read has to keep it current, or a
    corrected invoice carries the table of the reading it replaced.
    """

    def setUp(self):
        self.supplier = make_supplier(code="GROSSISTE", name="Grossiste Exemple", parser_key="GROSSISTE")
        self.path = os.path.join(paths.media_root(), "grossiste-table.pdf")
        with open(self.path, "wb") as handle:
            handle.write(b"%PDF-1.4 exemple")
        make_product(supplier=self.supplier, raw_name="VIN EXEMPLE", stock_type=make_stock_type())

    def reading(self, base, tax, total):
        line = ParsedLine(
            raw_name="VIN EXEMPLE",
            quantity=6,
            total_volume=Decimal("0"),
            unit_cost_ht=Decimal("5"),
            total_ht=Decimal(base),
            vat_rate=Decimal("0.20"),
        )
        result = parsed([line])
        result.printed_total_ttc = Decimal(total)
        result.vat_breakdown = [(Decimal("0.20"), Decimal(base), Decimal(tax))]
        return result

    def run_parser(self, result, action):
        with mock.patch.dict("invoices.parsers.registry.PARSER_REGISTRY", {"GROSSISTE": FakeParser(result)}):
            return action()

    def test_the_import_stores_the_table_the_document_prints(self):
        invoice = self.run_parser(
            self.reading("30.00", "6.00", "36.00"), lambda: parse_and_import(self.path, self.supplier)
        )
        self.assertEqual(invoice.vat_breakdown, [["0.20", "30.00", "6.00"]])
        self.assertEqual(invoice.printed_total_ttc, Decimal("36.00"))

    def test_a_re_read_brings_the_table_up_to_date(self):
        invoice = self.run_parser(
            self.reading("30.00", "6.00", "36.00"), lambda: parse_and_import(self.path, self.supplier)
        )
        self.run_parser(self.reading("50.00", "10.00", "60.00"), lambda: reread_document(invoice))
        invoice.refresh_from_db()
        self.assertEqual(invoice.vat_breakdown, [["0.20", "50.00", "10.00"]])

    def test_a_table_a_person_typed_is_never_overwritten(self):
        """A table emptied on purpose came back as the reading's, with
        checks against figures nobody typed."""
        invoice = self.run_parser(
            self.reading("30.00", "6.00", "36.00"), lambda: parse_and_import(self.path, self.supplier)
        )
        Invoice.objects.filter(pk=invoice.pk).update(vat_breakdown=[["0.055", "1.00", "0.06"]], vat_table_typed=True)
        invoice.refresh_from_db()
        self.run_parser(self.reading("50.00", "10.00", "60.00"), lambda: reread_document(invoice))
        invoice.refresh_from_db()
        self.assertEqual(invoice.vat_breakdown, [["0.055", "1.00", "0.06"]])
