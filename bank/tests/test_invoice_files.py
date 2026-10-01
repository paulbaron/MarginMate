"""« Télécharger les factures de la période » on Banque (bank/invoice_files.py):
every document the period's spending paid, one zip, each file named
« Darty 11€55 01_10_2026.pdf »."""

import io
import zipfile
from datetime import date

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from bank import reconcile
from bank.models import BankTransaction, InvoicePayment
from bank.tests.test_reconcile import Fixtures, debit_row
from invoices.models import Supplier

#: What separates an amount's thousands in the list (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"


def credit_row(day, label, amount):
    return f"{day:%d/%m/%Y};VIREMENT RECU;VIR SEPA RECU;VIR SEPA RECU DE {label};{day:%d/%m/%Y};{amount}"


class InvoiceFilesTests(Fixtures, TestCase):
    def setUp(self):
        # METRO 100 € HT at 20 %: 120,00 € TTC, debited on 9 July.
        self.metro = self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.metro.source_file.save("metro-juin.pdf", ContentFile(b"%PDF metro"))
        # A second METRO of the same amount and day, debited in July too.
        self.metro_again = self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.metro_again.source_file.save("metro-juin-bis.pdf", ContentFile(b"%PDF metro bis"))
        # Paid in August: outside July.
        self.august = self.invoice("METRO", date(2026, 7, 30), "50.00")
        self.august.source_file.save("metro-aout.pdf", ContentFile(b"%PDF august"))
        # Typed by hand: no file.
        self.typed = self.invoice("UBA", date(2026, 7, 1), "10.00", invoice_number="UBA-7")
        self.load(
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 10), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 11), "U.B.A.", "12,00"),
            debit_row(date(2026, 8, 3), "METRO FRANCE", "60,00"),
            credit_row(date(2026, 7, 15), "CLIENT EXEMPLE", "50,00"),
        )
        lines = {line.operation_date: line for line in BankTransaction.objects.all()}
        reconcile.link(lines[date(2026, 7, 9)], [self.metro])
        reconcile.link(lines[date(2026, 7, 10)], [self.metro_again])
        reconcile.link(lines[date(2026, 7, 11)], [self.typed])
        reconcile.link(lines[date(2026, 8, 3)], [self.august])
        self.url = reverse("bank:invoice_zip")

    def download(self, **parameters):
        response = self.client.get(self.url, parameters)
        self.assertEqual(response.status_code, 200)
        return response, zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))

    def test_a_month_holds_the_invoices_its_debits_paid_named_the_owner_s_way(self):
        response, archive = self.download(mois="2026-07")
        self.assertEqual(
            sorted(archive.namelist()),
            [
                "Factures sans fichier.txt",
                "Metro 120€00 29_06_2026 (2).pdf",
                "Metro 120€00 29_06_2026.pdf",
            ],
        )
        self.assertEqual(
            {archive.read("Metro 120€00 29_06_2026.pdf"), archive.read("Metro 120€00 29_06_2026 (2).pdf")},
            {b"%PDF metro", b"%PDF metro bis"},
        )
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn('filename="Factures juillet 2026.zip"', response["Content-Disposition"])

    def test_a_document_with_no_file_is_listed_not_dropped(self):
        _, archive = self.download(mois="2026-07")
        missing = archive.read("Factures sans fichier.txt").decode()
        self.assertIn(f"{Supplier.objects.get(code='UBA').name} n° UBA-7, 01/07/2026, 12.00 € TTC", missing)

    def test_the_list_groups_a_total_s_thousands_and_the_file_names_do_not(self):
        """« Factures sans fichier.txt » is read by the accountant: its
        totals group their thousands, as every page does. A file's name is
        the owner's « Metro 1800€00 … », never grouped."""
        typed = self.invoice("METRO", date(2026, 7, 2), "1000.00", invoice_number="M-1200")
        kept = self.invoice("METRO", date(2026, 7, 3), "1500.00")
        kept.source_file.save("metro-gros.pdf", ContentFile(b"%PDF big"))
        self.load(debit_row(date(2026, 7, 21), "METRO FRANCE", "3 000,00"))
        reconcile.link(BankTransaction.objects.get(operation_date=date(2026, 7, 21)), [typed, kept])
        _, archive = self.download(mois="2026-07")
        self.assertIn("Metro 1800€00 03_07_2026.pdf", archive.namelist())
        missing = archive.read("Factures sans fichier.txt").decode()
        self.assertIn(f"{typed.supplier.name} n° M-1200, 02/07/2026, 1{NBSP}200.00 € TTC", missing)

    def test_the_free_dates_are_the_window(self):
        response, archive = self.download(du="2026-08-01", au="2026-08-31")
        self.assertEqual(archive.namelist(), ["Metro 60€00 30_07_2026.pdf"])
        self.assertIn("Factures du 01_08_2026 au 31_08_2026.zip", response["Content-Disposition"])

    def test_no_period_is_everything(self):
        response, archive = self.download()
        self.assertEqual(len([name for name in archive.namelist() if name.endswith(".pdf")]), 3)
        self.assertIn("Factures tout l'historique.zip", response["Content-Disposition"])

    def test_one_invoice_paid_by_two_lines_is_in_once(self):
        second = BankTransaction.objects.get(operation_date=date(2026, 8, 3))
        reconcile.link(second, [self.metro])
        _, archive = self.download()
        self.assertEqual(sum(1 for name in archive.namelist() if name.startswith("Metro 120€00")), 2)

    def test_nothing_paid_goes_back_to_the_tab_it_came_from_and_says_so(self):
        response = self.client.get(
            self.url, {"vue": "rapprochees", "du": "2030-01-01", "au": "2030-12-31"}, follow=True
        )
        self.assertContains(response, "Aucune facture à télécharger sur cette période.")
        self.assertEqual(
            response.redirect_chain[-1][0],
            f"{reverse('bank:bank_home')}?vue=rapprochees&du=2030-01-01&au=2030-12-31",
        )

    def test_invoices_with_no_file_at_all_are_no_zip(self):
        """A zip holding the list of what it could not hold, and nothing
        else, is no download."""
        response = self.client.get(self.url, {"du": "2026-07-11", "au": "2026-07-11"}, follow=True)
        self.assertContains(response, "Aucune facture à télécharger sur cette période.")

    def test_a_credit_pays_nothing_that_goes_in(self):
        """An income line never settles an invoice; one linked all the same
        (a stale POST, an old archive) does not put it in the zip."""
        credit = BankTransaction.objects.get(operation_date=date(2026, 7, 15))
        stray = self.invoice("METRO", date(2026, 7, 14), "5.00")
        stray.source_file.save("metro-avoir.pdf", ContentFile(b"%PDF stray"))
        InvoicePayment.objects.create(transaction=credit, invoice=stray, method=InvoicePayment.Method.MANUAL)
        _, archive = self.download(mois="2026-07")
        self.assertNotIn("Metro 6€00 14_07_2026.pdf", archive.namelist())

    def test_a_file_gone_from_the_disk_is_listed_with_the_others(self):
        self.metro_again.source_file.storage.delete(self.metro_again.source_file.name)
        _, archive = self.download(mois="2026-07")
        self.assertEqual(sorted(archive.namelist()), ["Factures sans fichier.txt", "Metro 120€00 29_06_2026.pdf"])
        self.assertEqual(archive.read("Factures sans fichier.txt").decode().count("\n- "), 2)

    def test_a_stored_name_climbing_out_of_media_is_not_read(self):
        self.metro_again.source_file.name = "../../accounts.sqlite3"
        self.metro_again.save(update_fields=["source_file"])
        _, archive = self.download(mois="2026-07")
        self.assertNotIn("Metro 120€00 29_06_2026 (2).pdf", archive.namelist())

    def test_a_chosen_month_wins_over_the_dates(self):
        response, archive = self.download(mois="2026-07", du="2026-08-01", au="2026-08-31")
        self.assertNotIn("Metro 60€00 30_07_2026.pdf", archive.namelist())
        self.assertIn("Factures juillet 2026.zip", response["Content-Disposition"])

    def test_an_open_ended_period_is_named_so(self):
        response, _ = self.download(du="2026-08-01")
        self.assertIn("Factures depuis le 01_08_2026.zip", response["Content-Disposition"])
        response, _ = self.download(au="2026-07-31")
        self.assertIn("Factures jusqu'au 31_07_2026.zip", response["Content-Disposition"])

    def test_the_zip_is_served_like_every_stored_file(self):
        response, _ = self.download(mois="2026-07")
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])

    def test_only_a_get_builds_it(self):
        self.assertEqual(self.client.head(self.url).status_code, 405)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_the_page_offers_it_over_the_period_and_tab_it_shows(self):
        response = self.client.get(reverse("bank:bank_home"), {"mois": "2026-07"})
        self.assertContains(
            response,
            f'<a class="btn btn-small" href="{self.url}?vue=a-traiter&amp;mois=2026-07">⬇️ Factures de la période (2)</a>',
            html=True,
        )
        self.assertContains(response, "+ 1 sans fichier (listée dans le zip)")

    def test_the_page_offers_nothing_over_a_period_nothing_paid(self):
        response = self.client.get(reverse("bank:bank_home"), {"du": "2030-01-01", "au": "2030-12-31"})
        self.assertNotContains(response, "Factures de la période")


class BanqueTabsTests(Fixtures, TestCase):
    """Banque's three pages, one tab each (bank/_tabs.html): every page draws
    all three, lights its own, and hands the others its period."""

    def setUp(self):
        self.load(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"))

    def tabs_of(self, response):
        html = response.content.decode()
        return html[html.index('<nav class="tabs" aria-label="Banque">') :].split("</nav>")[0]

    def test_each_page_draws_the_three_tabs_and_lights_its_own(self):
        for name, lit in (
            ("bank:bank_home", "Opérations"),
            ("bank:spending_home", "Dépenses par catégorie"),
            ("bank:income_home", "Entrées d'argent"),
        ):
            with self.subTest(page=name):
                tabs = self.tabs_of(self.client.get(reverse(name), {"du": "2026-07-01", "au": "2026-07-31"}))
                self.assertEqual(tabs.count("<a "), 3)
                self.assertEqual(tabs.count('aria-current="page"'), 1)
                self.assertIn(f'aria-current="page">{lit}</a>', tabs)
                self.assertNotIn('href=""', tabs)

    def test_the_operations_tab_carries_the_period_from_both_other_pages(self):
        for name in ("bank:spending_home", "bank:income_home"):
            with self.subTest(page=name):
                tabs = self.tabs_of(self.client.get(reverse(name), {"du": "2026-07-01", "au": "2026-07-31"}))
                self.assertIn(
                    f'href="{reverse("bank:bank_home")}?vue=a-traiter&amp;du=2026-07-01&amp;au=2026-07-31"', tabs
                )
