"""The bank page and the actions on each of its lines."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bank import recognition, reconcile, statements
from bank.models import BankTransaction, InvoicePayment, OperationRule, StatementFormat
from bank.tests import ofx_files
from bank.tests.support import FORMAT_SEED, make_format
from bank.tests.test_recognition_views import text_of
from bank.tests.test_reconcile import FIVE_FIVE, Fixtures, card_row, debit_row, statement
from bank.tests.test_statement_format_views import OTHER_BANK, OTHER_FIELDS, bnp_file
from staff.tests.page_forms import as_post, forms_of
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
        # The invoices its row shows, as « Délier » posts them.
        self.act(metro_line, "unlink", shown=[self.metro.pk])
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

    def test_only_statement_files_are_accepted(self):
        response = self.upload("releve.pdf", b"%PDF-1.4")
        self.assertContains(response, "releve.pdf : seuls les relevés CSV, OFX ou CAMT.053 (XML) sont acceptés.")
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


class ImportFormatTests(TestCase):
    """The import card says which format reads the files and, with several,
    lets the person choose (bank/views.py `_import_format`) - read off the
    page and posted as a browser posts it, its CSRF token checked. Every
    statement here is invented."""

    def setUp(self):
        super().setUp()
        self.client = self.client_class(enforce_csrf_checks=True)
        self.url = reverse("bank:bank_home")
        self.formats_url = reverse("bank:statement_formats")

    def html(self) -> str:
        response = self.client.get(self.url)
        assertNoUnrenderedTemplateSyntax(self, response, self.url)
        return response.content.decode()

    def card(self, html=None):
        (form,) = [form for form in forms_of(html or self.html()) if "files" in form.names]
        return form

    def card_text(self, html) -> str:
        at = html.index('name="files"')
        start = html.rindex('<div class="card">', 0, at)
        return text_of(html[start : html.index("</div>", at)])

    def data(self, *files, form=None, **values) -> dict:
        data = as_post((form or self.card()).submission(values=values))
        data["files"] = [SimpleUploadedFile(name, content) for name, content in files]
        return data

    def upload(self, *files, **values):
        return self.client.post(self.url, self.data(*files, **values), follow=True)

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def test_one_format_there_is_nothing_to_choose_and_the_card_names_it(self):
        html = self.html()
        self.assertNotIn("format", self.card(html).names)
        self.assertIn(f"Format : {FORMAT_SEED.NAME} · modifier", self.card_text(html))
        self.assertIn(f'<a href="{self.formats_url}">modifier</a>', html)

    def test_several_formats_the_card_offers_them_the_first_chosen(self):
        other = make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        html = self.html()
        seeded = StatementFormat.objects.get(name=FORMAT_SEED.NAME)
        self.assertEqual(self.card(html).control("format").options, [(str(seeded.pk), True), (str(other.pk), False)])
        self.assertIn("Formats : 2 · modifier", self.card_text(html))
        # In their order: moved first, the other bank's is chosen.
        StatementFormat.objects.filter(pk=other.pk).update(position=0)
        self.assertEqual(self.card().control("format").value, str(other.pk))

    def test_the_card_as_drawn_reads_with_the_first_format(self):
        make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        response = self.upload(("Juillet.csv", bnp_file()))
        self.assertIn("2 opération(s) importée(s)", " ".join(self.messages_of(response)))
        self.assertEqual(set(BankTransaction.objects.values_list("account", flat=True)), {"****0042"})

    def test_the_format_chosen_reads_the_files(self):
        other = make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        response = self.upload(("export.csv", OTHER_BANK), format=str(other.pk))
        self.assertIn("2 opération(s) importée(s)", " ".join(self.messages_of(response)))
        self.assertEqual(
            list(BankTransaction.objects.order_by("operation_date").values_list("account", "label", "amount")),
            [
                ("000123456789", "CB EPICERIE EXEMPLE ref 0001", Decimal("-12.30")),
                ("000123456789", "VIR CLIENT EXEMPLE ref 0002", Decimal("1250.00")),
            ],
        )

    def test_the_card_offers_every_kind_of_statement_file(self):
        (control,) = [control for control in self.card().controls if control.name == "files"]
        self.assertEqual(control.attrs["accept"], statements.ACCEPT_ATTRIBUTE)
        self.assertEqual(statements.ACCEPT_ATTRIBUTE, ".csv,.ofx,.qfx,.xml,text/csv")
        self.assertIn("L'export du compte - CSV, OFX ou CAMT.053 -", self.card_text(self.html()))

    def test_an_ofx_statement_is_imported_with_an_ofx_format(self):
        ofx_format = make_format("Relevé OFX", file_type="ofx", date_column=None, label_columns="", account_pattern="")
        for name, content in (("releve.ofx", ofx_files.sgml()), ("releve.qfx", ofx_files.xml())):
            with self.subTest(name=name):
                response = self.upload((name, content), format=str(ofx_format.pk))
                self.assertEqual(BankTransaction.objects.count(), len(ofx_files.OPERATIONS))
                self.assertNotIn("Aucune", " ".join(self.messages_of(response)))

    def test_a_file_of_another_kind_than_its_format_is_refused_never_read_otherwise(self):
        """An OFX statement named « .csv », chosen with the CSV format: said
        with both kinds, nothing written - never read with the OFX format in
        silence, nor half read as a CSV."""
        response = self.upload(("releve.csv", ofx_files.sgml()))
        self.assertEqual(
            self.messages_of(response),
            [
                (
                    "releve.csv : Ce fichier est un relevé OFX / QFX (Money), et le format « BNP Paribas (CSV) » lit "
                    "les fichiers CSV (colonnes) : choisissez un format OFX / QFX (Money) à l'import, ou ajoutez-en "
                    "un sur « Format du relevé »."
                )
            ],
        )
        invoice = b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
        response = self.upload(("facture.xml", invoice))
        self.assertEqual(
            self.messages_of(response),
            [
                (
                    "facture.xml : Ce fichier XML n'est pas un relevé de compte : le format « BNP Paribas (CSV) » "
                    "lit les fichiers CSV (colonnes)."
                )
            ],
        )
        self.assertFalse(BankTransaction.objects.exists())

    def test_a_format_that_is_not_there_imports_nothing(self):
        other = make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        form = self.card()
        refused = ["Format de relevé inconnu. Aucun relevé n'a été importé."]
        for chosen in ("999999", "abc", "", "²", "-1", "1" * 19):
            with self.subTest(chosen=chosen):
                data = self.data(("Juillet.csv", bnp_file()), form=form)
                data["format"] = [chosen]
                response = self.client.post(self.url, data, follow=True)
                self.assertEqual(self.messages_of(response), refused)
        # Deleted since the card was drawn.
        data = self.data(("export.csv", OTHER_BANK), form=form, format=str(other.pk))
        other.delete()
        response = self.client.post(self.url, data, follow=True)
        self.assertEqual(self.messages_of(response), refused)
        self.assertFalse(BankTransaction.objects.exists())

    def test_with_no_format_the_card_says_so_and_no_statement_is_imported(self):
        StatementFormat.objects.all().delete()
        html = self.html()
        self.assertIn("Aucun format de relevé — en ajouter un", self.card_text(html))
        self.assertIn(f'<a href="{self.formats_url}">en ajouter un</a>', html)
        response = self.upload(("Juillet.csv", bnp_file()))
        self.assertEqual(self.messages_of(response), [f"{reconcile.NO_FORMAT} Aucun relevé n'a été importé."])
        self.assertFalse(BankTransaction.objects.exists())

    def test_the_format_is_read_once_for_every_file_of_an_upload(self):
        files = [
            (f"Compte {month}.csv", statement(debit_row(date(2026, month, 9), "METRO FRANCE", "1,00")))
            for month in (7, 8, 9)
        ]
        for chosen in (False, True):
            with self.subTest(chosen=chosen):
                BankTransaction.objects.all().delete()
                if chosen:
                    make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
                values = {"format": str(StatementFormat.objects.get(name=FORMAT_SEED.NAME).pk)} if chosen else {}
                data = self.data(*files, **values)
                with CaptureQueriesContext(connection) as queries:
                    self.client.post(self.url, data)
                read = [query["sql"] for query in queries.captured_queries if "bank_statementformat" in query["sql"]]
                self.assertEqual(len(read), 1)
                self.assertEqual(BankTransaction.objects.count(), 3)

    def test_a_stored_format_the_check_refuses_stops_the_import_and_says_which(self):
        StatementFormat.objects.filter(name=FORMAT_SEED.NAME).update(amount_column=None)
        response = self.upload(("Juillet.csv", bnp_file()))
        (said,) = self.messages_of(response)
        self.assertTrue(
            said.startswith(
                f"Juillet.csv : Import annulé : le format « {FORMAT_SEED.NAME} » est à corriger sur « Format du relevé »"
            ),
            said,
        )
        self.assertFalse(BankTransaction.objects.exists())
