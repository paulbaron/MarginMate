"""The bank page and the actions on each of its lines."""

from datetime import date
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from bank import recognition, reconcile
from bank.models import BankTransaction, InvoicePayment, OperationRule
from bank.tests.test_reconcile import FIVE_FIVE, Fixtures, card_row, debit_row, statement
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax


class EmptyBankPageTests(TestCase):
    def test_before_any_statement_the_page_says_what_to_do(self):
        response = self.client.get(reverse("bank:bank_home"))
        self.assertContains(response, "page-subtitle")
        self.assertContains(response, "empty-state")

    def test_the_nav_links_to_the_page_and_lights_it_up(self):
        html = self.client.get(reverse("bank:bank_home")).content.decode()
        nav = html[html.index("<nav") : html.index("</nav>")]
        self.assertIn(f'<a href="{reverse("bank:bank_home")}" class="active">Banque</a>', nav)


class BankPageTests(Fixtures, TestCase):
    def setUp(self):
        self.metro = self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.receipt = self.invoice("MONOPRIX", date(2026, 7, 15), "12.38", rate=FIVE_FIVE)
        self.load(
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"),
            card_row(date(2026, 7, 15), "SUMUP *BK PREM", "13,06"),
            debit_row(date(2026, 7, 20), "BAILLEUR EXEMPLE", "900,00"),
        )
        reconcile.reconcile()
        self.url = reverse("bank:bank_home")

    def line(self, counterparty):
        return BankTransaction.objects.get(counterparty=counterparty)

    def act(self, line, action, **data):
        return self.client.post(reverse("bank:bank_line_action", args=[line.pk]), {"action": action, **data})

    def test_every_view_renders(self):
        for view in ("a-traiter", "rapprochees", "sans-facture", "toutes", "entrees", "inconnue"):
            with self.subTest(view=view):
                response = self.client.get(self.url, {"vue": view})
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, view)

    def test_the_table_searches_and_sorts_like_every_other(self):
        response = self.client.get(self.url, {"vue": "toutes"})
        self.assertContains(response, "data-table")
        self.assertContains(response, 'data-sort="2026-07-15"')

    def test_a_linked_payment_shows_its_invoice(self):
        response = self.client.get(self.url, {"vue": "rapprochees"})
        self.assertContains(response, reverse("invoices:invoice_detail", args=[self.metro.pk]))

    def test_a_suggestion_is_accepted_in_one_click(self):
        self.assertContains(self.client.get(self.url), "ne nomme pas ce fournisseur")
        self.act(self.line("SUMUP *BK PREM"), "link", invoice=[self.receipt.pk])
        self.assertEqual(InvoicePayment.objects.get(invoice=self.receipt).method, InvoicePayment.Method.MANUAL)

    def test_an_invoice_can_be_picked_by_hand(self):
        self.assertContains(self.client.get(self.url), "Choisir une facture")
        rent = self.line("BAILLEUR EXEMPLE")
        self.act(rent, "link", invoice=[self.receipt.pk])
        self.assertEqual(InvoicePayment.objects.get(invoice=self.receipt).transaction, rent)

    def test_an_invoice_already_paid_is_linked_again_and_both_rows_say_so(self):
        # Rare, and real: one document settled by two operations. What used
        # to happen here is that the link was refused outright.
        self.act(self.line("BAILLEUR EXEMPLE"), "link", invoice=[self.metro.pk])
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.metro).count(), 2)
        self.assertContains(self.client.get(self.url, {"vue": "rapprochees"}), "Aussi réglée par", count=2)

    def test_no_invoice_then_back_to_the_automatic_pass(self):
        rent = self.line("BAILLEUR EXEMPLE")
        self.act(rent, "no_invoice")
        self.assertContains(self.client.get(self.url, {"vue": "sans-facture"}), "BAILLEUR EXEMPLE")
        self.act(rent, "reopen")
        rent.refresh_from_db()
        self.assertEqual((rent.no_invoice, rent.settled_by_hand), (False, False))

    def test_an_unlinked_line_stays_unlinked(self):
        metro_line = self.line("METRO FRANCE")
        self.act(metro_line, "unlink")
        self.client.post(reverse("bank:bank_reconcile"))
        self.assertFalse(InvoicePayment.objects.filter(transaction=metro_line).exists())

    def test_actions_only_answer_a_post(self):
        rent = self.line("BAILLEUR EXEMPLE")
        response = self.client.get(reverse("bank:bank_line_action", args=[rent.pk]), {"action": "no_invoice"})
        self.assertEqual(response.status_code, 302)
        rent.refresh_from_db()
        self.assertFalse(rent.no_invoice)

    def test_an_invoice_shows_when_it_was_paid(self):
        response = self.client.get(reverse("invoices:invoice_detail", args=[self.metro.pk]))
        self.assertContains(response, "Payée")
        self.assertContains(response, "09/07/2026")


class UploadTests(TestCase):
    def upload(self, name, content):
        return self.client.post(reverse("bank:bank_home"), {"files": [SimpleUploadedFile(name, content)]}, follow=True)

    def test_statements_are_imported_then_matched(self):
        invoice = Fixtures().invoice("METRO", date(2026, 6, 29), "100.00")
        response = self.upload("Compte Juillet.csv", statement(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00")))
        self.assertContains(response, "1 opération(s) importée(s)")
        self.assertEqual(InvoicePayment.objects.get().invoice, invoice)

    def test_an_import_no_rule_of_nature_reads_says_so(self):
        OperationRule.objects.filter(meaning__in=recognition.KIND_MEANINGS).update(is_active=False)
        response = self.upload("Compte Juillet.csv", statement(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00")))
        self.assertContains(response, "1 opération(s) importée(s)")
        self.assertContains(response, "Aucune règle de nature d")
        line = BankTransaction.objects.get()
        self.assertEqual((line.kind, line.counterparty), (BankTransaction.Kind.OTHER, ""))

    def test_an_import_read_by_the_rules_says_nothing_of_them(self):
        response = self.upload("Compte Juillet.csv", statement(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00")))
        self.assertNotContains(response, "Aucune règle de nature d")

    def test_the_rules_are_read_once_for_every_file_of_an_upload(self):
        files = [
            SimpleUploadedFile(
                f"Compte {month}.csv", statement(debit_row(date(2026, month, 9), "METRO FRANCE", "1,00"))
            )
            for month in (7, 8, 9)
        ]
        with mock.patch("bank.views.recognition.load", wraps=recognition.load) as load:
            self.client.post(reverse("bank:bank_home"), {"files": files})
        self.assertEqual(load.call_count, 1)
        self.assertEqual(BankTransaction.objects.count(), 3)

    def test_a_file_that_is_not_a_statement_is_refused(self):
        response = self.upload("notes.csv", b"nom;prenom\nDupont;Jean\n")
        self.assertContains(response, "ne ressemble pas")
        self.assertFalse(BankTransaction.objects.exists())

    def test_only_csv_is_accepted(self):
        response = self.upload("releve.pdf", b"%PDF-1.4")
        self.assertContains(response, "CSV")
        self.assertFalse(BankTransaction.objects.exists())

    def test_nothing_chosen(self):
        self.assertContains(self.client.post(reverse("bank:bank_home"), {}, follow=True), "Choisissez")


class UndatedReceiptPageTests(Fixtures, TestCase):
    def test_an_undated_receipt_is_offered_on_the_page(self):
        receipt = self.invoice("MONOPRIX", date(2026, 8, 27), "7.36", rate=FIVE_FIVE)
        type(receipt).objects.filter(pk=receipt.pk).update(invoice_date=None)
        self.load(card_row(date(2026, 8, 27), "MONOPRIX PARIS", "7,76"))
        response = self.client.get(reverse("bank:bank_home"))
        self.assertContains(response, "sa date n")
        self.assertContains(response, "sans date")
