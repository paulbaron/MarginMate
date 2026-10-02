"""« Achats », the Documents tab: what each row prints, worked out once.

« Tout afficher » draws every document - a thousand rows on a real
database, and it took seconds: each row asked the template for four links
({% url %}), three dates, two totals walking its lines through a related
manager, and Django built a queryset of its own for every row it
prefetched. The view now hands each row its links, its dates and its totals
(workspace._documents), its lines and payments prefetched into plain lists.

None of it may change a character. So every precomputed value is held here
to what the old way gives - the model's own properties on a fresh instance,
Django's own filters, reverse() - over documents taking every branch of
`Invoice.total_ttc`: printed amounts paid or not, a mix, an electronic
invoice, a stated or deduced adjustment rate, a credit, no line at all, no
date. And the page still costs the same queries however many it lists.

Data invented.
"""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.template import Context, Template
from django.template.defaultfilters import date as date_filter
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bank.models import BankTransaction, InvoicePayment
from invoices import workspace
from invoices.models import Invoice, ReceiptBatch
from invoices.workspace import bank_state, link_maker, shown_day, sorted_day
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

CHECKS = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]


class Documents:
    """One document down each branch a row's totals can take."""

    def setUp(self):
        self.url = reverse("invoices:invoice_list")
        self.supplier = make_supplier(name="Grossiste Exemple")
        self.shop = make_supplier(name="Épicerie Exemple")
        self.charges = make_supplier(name="Bailleur Exemple", expenses_only=True)
        self.day = 0

    def invoice(self, supplier=None, **kwargs):
        self.day += 1
        kwargs.setdefault("invoice_date", date(2026, 1, 1 + self.day % 28))
        undated = kwargs["invoice_date"] is None
        invoice = make_invoice(supplier=supplier or self.supplier, **kwargs)
        if undated:
            # The factory dates a document given none.
            Invoice.objects.filter(pk=invoice.pk).update(invoice_date=None)
            invoice.refresh_from_db()
        return invoice

    def line(self, invoice, total_ht, rate="0.20", **kwargs):
        return make_invoice_line(
            invoice=invoice,
            product=make_product(supplier=invoice.supplier),
            total_ht=Decimal(total_ht),
            vat_rate=Decimal(rate),
            **kwargs,
        )

    def statement(self, day, amount):
        self.day += 1
        return BankTransaction.objects.create(
            operation_date=day, amount=Decimal(amount), label=f"PRLV {day} {amount}", fingerprint=f"fp-{self.day}"
        )

    def every_kind(self):
        # Goods at two rates, duty on one line: the adjustment takes its rate.
        duty = self.invoice(reconciliation_adjustment=Decimal("0.37"))
        self.line(duty, "100.00", taxes=Decimal("3.50"))
        self.line(duty, "33.33", rate="0.055")
        # The adjustment's rate stated by the document.
        stated = self.invoice(reconciliation_adjustment=Decimal("12.00"), adjustment_vat_rate=Decimal("0.2000"))
        self.line(stated, "40.00", rate="0.055")
        # A ticket whose every line printed its amount, paid as printed.
        paid = self.invoice(self.shop, parse_checks=CHECKS, ocr_text="TICKET", printed_total_ttc=Decimal("7.00"))
        self.line(paid, "4.64", rate="0.055", printed_ttc=Decimal("4.90"), discount_ttc=Decimal("0.00"))
        self.line(paid, "1.99", rate="0.055", printed_ttc=Decimal("2.10"))
        # Printed amounts far from the printed total: the lines stand.
        far = self.invoice(self.shop, parse_checks=CHECKS, printed_total_ttc=Decimal("50.00"))
        self.line(far, "9.48", rate="0.055", printed_ttc=Decimal("10.00"), discount_ttc=Decimal("0.50"))
        # Some lines printed, some not: all from HT, the total within a cent.
        mixed = self.invoice(self.shop, ocr_text="TICKET", printed_total_ttc=Decimal("14.51"))
        self.line(mixed, "6.64", rate="0.055", printed_ttc=Decimal("7.00"))
        self.line(mixed, "6.25", rate="0.20")
        # An electronic invoice states its total: within a cent a line.
        einvoice = self.invoice(einvoice_format="CII", printed_total_ttc=Decimal("229.39"))
        self.line(einvoice, "169.00")
        self.line(einvoice, "25.20", rate="0.055")
        # A credit, a charge, a document with no line, one with no date.
        credit = self.invoice()
        self.line(credit, "-15.00", quantity=-1)
        rent = self.invoice(self.charges, status=Invoice.Status.NEEDS_REVIEW)
        self.line(rent, "833.33")
        self.invoice(reconciliation_adjustment=Decimal("2.50"))
        undated = self.invoice(invoice_date=None)
        self.line(undated, "1.00")
        # Settled twice: the earliest payment is the one said.
        self.pay(duty, self.statement(date(2026, 3, 9), "-80.00"))
        self.pay(duty, self.statement(date(2026, 2, 27), "-80.00"))
        return duty

    def pay(self, invoice, line):
        InvoicePayment.objects.create(transaction=line, invoice=invoice, method=InvoicePayment.Method.MANUAL)

    def listed(self, query="tout=1"):
        return self.client.get(f"{self.url}?{query}").context["invoices"]


class RowsAreWhatTheyWereTests(Documents, TestCase):
    def test_every_total_is_the_models_own(self):
        self.every_kind()
        rows = self.listed()
        self.assertEqual(len(rows), Invoice.objects.count())
        for row in rows:
            fresh = Invoice.objects.get(pk=row.pk)
            with self.subTest(invoice=fresh.invoice_number):
                # Equal AND written the same: the page prints them through
                # `money`, which keeps what a Decimal says about its places.
                self.assertEqual(str(row.listed_total_ht), str(fresh.total_ht))
                self.assertEqual(str(row.listed_total_ttc), str(fresh.total_ttc))

    def test_every_settlement_is_what_the_payments_say(self):
        self.statement(date(2025, 12, 1), "-1.00")
        self.every_kind()
        start = date(2025, 12, 1)
        for row in self.listed():
            with self.subTest(invoice=row.invoice_number):
                self.assertEqual(row.bank_state, bank_state(Invoice.objects.get(pk=row.pk), start))
        settled = [row for row in self.listed() if row.bank_state and row.bank_state["css"] == "COMPLETE"]
        self.assertEqual([row.bank_state["label"] for row in settled], ["Réglée le 27/02/2026"])

    def test_every_date_and_link_is_what_the_template_wrote(self):
        """The old template's own expressions, rendered for each row."""
        self.every_kind()
        old = Template(
            "{% url 'invoices:invoice_preview' invoice.pk %}|{% url 'invoices:invoice_detail' invoice.pk %}|"
            "{% url 'invoices:receipt_review' invoice.pk %}|{% url 'invoices:invoice_edit_lines' invoice.pk %}|"
            "{{ invoice.invoice_date|date:'d/m/Y' }}|{{ invoice.invoice_date|date:'Y-m-d' }}"
        )
        new = Template(
            "{{ invoice.preview_url }}|{{ invoice.detail_url }}|{{ invoice.review_url }}|{{ invoice.lines_url }}|"
            "{{ invoice.listed_day }}|{{ invoice.listed_day_sort }}"
        )
        for row in self.listed():
            with self.subTest(invoice=row.invoice_number):
                self.assertEqual(new.render(Context({"invoice": row})), old.render(Context({"invoice": row})))

    def test_the_highlighted_document_gets_its_row_too(self):
        """Folded in after the others, it goes through the same hands - and
        the row it draws says « importée »."""
        self.every_kind()
        old = self.invoice(invoice_date=date(2020, 5, 6))
        self.line(old, "10.00")
        with mock.patch.object(workspace, "PAGE_SIZE", 2):
            response = self.client.get(f"{self.url}?surligner={old.pk}")
        rows = response.context["invoices"]
        self.assertEqual(rows[0].pk, old.pk)
        self.assertEqual(str(rows[0].listed_total_ttc), str(Invoice.objects.get(pk=old.pk).total_ttc))
        self.assertEqual(rows[0].detail_url, reverse("invoices:invoice_detail", args=[old.pk]))
        self.assertEqual([row.is_new for row in rows], [True, False, False])
        self.assertContains(response, f'<tr data-document="{old.pk}" data-highlight', count=1)
        self.assertContains(response, 'class="document-row is-new"', count=1)
        self.assertContains(response, '<span class="new-pill">importée</span>', count=1)

    def test_the_list_costs_the_same_queries_with_three_times_the_documents(self):
        """Every kind of row, then three times as many: a row reading a
        deferred column or a line's unloaded field would be a query a row."""
        self.every_kind()
        with CaptureQueriesContext(connection) as few:
            self.client.get(f"{self.url}?tout=1")
        self.every_kind()
        self.every_kind()
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(f"{self.url}?tout=1")
        self.assertEqual(len(response.context["invoices"]), Invoice.objects.count())
        self.assertEqual(len(many), len(few))


class PrecomputedStringsTests(SimpleTestCase):
    def test_a_day_is_written_as_djangos_date_filter_writes_it(self):
        for day in (date(1, 1, 1), date(999, 12, 31), date(2026, 3, 5), date(2026, 11, 30), date(9999, 12, 31)):
            with self.subTest(day=day):
                self.assertEqual(shown_day(day), date_filter(day, "d/m/Y"))
                self.assertEqual(sorted_day(day), date_filter(day, "Y-m-d"))
        self.assertEqual(shown_day(None), date_filter(None, "d/m/Y"))
        self.assertEqual(sorted_day(None), date_filter(None, "Y-m-d"))

    def test_a_link_is_the_one_reverse_gives(self):
        for name in ("invoices:invoice_preview", "invoices:receipt_review", "invoices:supplier_detail"):
            link = link_maker(name)
            for pk in (1, 9, 10, 123456789, 9081726354):
                with self.subTest(name=name, pk=pk):
                    self.assertEqual(link(pk), reverse(name, args=[pk]))

    def test_an_address_the_placeholder_cannot_fill_is_reversed_every_time(self):
        def twice(name, args):
            return f"/{args[0]}/{args[0]}/"

        with mock.patch.object(workspace, "reverse", side_effect=twice) as reverse_:
            link = link_maker("invoices:invoice_preview")
            self.assertEqual(link(7), "/7/7/")
        self.assertEqual(reverse_.call_count, 2)


class OneCountForTheBadgeTests(Documents, TestCase):
    """The navigation's « Factures » badge is the « À vérifier » tab's
    count: on a page of Achats, which has just counted it, it is not asked
    of the database a second time."""

    def setUp(self):
        super().setUp()
        self.invoice(self.shop, parse_checks=CHECKS, ocr_text="TICKET")
        self.invoice(invoice_date=None)

    def test_an_achats_page_counts_once_and_says_the_same(self):
        for name in ("invoice_list", "receipt_queue", "invoice_type_list", "supplier_list"):
            with self.subTest(page=name):
                with mock.patch.object(workspace, "waiting_counts", wraps=workspace.waiting_counts) as counted:
                    response = self.client.get(reverse(f"invoices:{name}"))
                counted.assert_not_called()
                self.assertEqual(response.context["receipt_review_count_nav"], 2)
                self.assertContains(response, '<span class="badge">2</span>')

    def test_another_page_still_counts(self):
        invoice = Invoice.objects.first()
        with mock.patch.object(workspace, "waiting_counts", wraps=workspace.waiting_counts) as counted:
            response = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        counted.assert_called_once()
        self.assertEqual(response.context["receipt_review_count_nav"], 2)


class UndatedCountTests(Documents, TestCase):
    def test_the_undated_documents_are_counted_with_or_without_dates_asked(self):
        """Without a window the chips' own count is every document's;
        under one it is counted apart, over every document all the same."""
        self.invoice(invoice_date=None)
        self.invoice(invoice_date=None)
        self.invoice(invoice_date=date(2026, 2, 3))
        for query in ("", "tout=1", "du=2026-02-01&au=2026-02-28", "du=2030-01-01", "q=Grossiste"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"{self.url}?{query}").context["undated_count"], 2)


class WaitingGroupTests(TestCase):
    """Who still waits for a first document - asked of every supplier, once
    a supplier: the model's ordering joined to the DISTINCT read back one row
    per DOCUMENT, on every page with an import card."""

    def test_one_row_per_supplier_and_the_same_answer(self):
        from invoices.receipts import _waiting_first

        filed, new = make_supplier(name="Grossiste Exemple"), make_supplier(name="Primeur Exemple")
        for day in (1, 2, 3):
            make_invoice(supplier=filed, invoice_date=date(2026, 1, day))
        with CaptureQueriesContext(connection) as queries:
            waiting, others = _waiting_first([filed, new])
        self.assertEqual((waiting, others), ([new], [filed]))
        (distinct,) = [query["sql"] for query in queries if "DISTINCT" in query["sql"]]
        self.assertNotIn("invoice_date", distinct)


class ImportRowsTests(Documents, TestCase):
    """An import's files, each with the document it became and its total
    (workspace.batch_rows): the live part is polled every second while a
    folder is read, and each row's total read its lines on its own."""

    def batch(self, documents):
        return ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {
                    "name": f"ticket-{n}.jpg",
                    "status": "ok",
                    "invoice_id": invoice.pk,
                    "receipt": True,
                    "shop": "Épicerie",
                }
                for n, invoice in enumerate(documents)
            ],
        )

    def tickets(self, count):
        documents = []
        for _ in range(count):
            ticket = self.invoice(self.shop, parse_checks=CHECKS, ocr_text="TICKET", printed_total_ttc=Decimal("7.00"))
            self.line(ticket, "4.64", rate="0.055", printed_ttc=Decimal("4.90"))
            self.line(ticket, "1.99", rate="0.055", printed_ttc=Decimal("2.10"))
            documents.append(ticket)
        return documents

    def queries(self, batch):
        with CaptureQueriesContext(connection) as seen:
            response = self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk]))
        self.assertEqual(response.status_code, 200)
        return len(seen), response

    def test_a_bigger_import_costs_no_more_queries(self):
        few, _ = self.queries(self.batch(self.tickets(1)))
        many, response = self.queries(self.batch(self.tickets(6)))
        self.assertEqual(many, few)
        self.assertContains(response, "7.00 €", count=6)

    def test_each_row_prints_its_documents_own_total(self):
        documents = self.tickets(2)
        self.every_kind()
        documents += list(Invoice.objects.exclude(pk__in=[document.pk for document in documents]))
        rows = workspace.batch_rows(self.batch(documents))
        for row in rows:
            with self.subTest(invoice=row["invoice"].invoice_number):
                fresh = Invoice.objects.get(pk=row["invoice"].pk)
                self.assertEqual(str(row["invoice"].total_ttc), str(fresh.total_ttc))
