"""What the bank's statements upload may weigh (security audit UPLOAD-1):
25 Mo a file (`common.UPLOAD_MAX_FILE_BYTES`), refused by its name while
the others are read, and 100 Mo for the whole selection
(`common.UPLOAD_MAX_TOTAL_BYTES`), refused whole. A bank's CSV export is a
few Ko a month. The caps are patched down: every file here is a few
bytes."""

from datetime import date
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from bank.models import BankTransaction
from bank.tests.test_reconcile import debit_row, statement


class StatementUploadLimitsTests(TestCase):
    def post(self, *files):
        return self.client.post(
            reverse("bank:bank_home"),
            {"files": [SimpleUploadedFile(name, content) for name, content in files]},
            follow=True,
        )

    def test_a_file_over_the_cap_is_refused_by_name_and_the_others_imported(self):
        small = statement(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"))
        big = statement(debit_row(date(2026, 7, 10), "BAILLEUR EXEMPLE", "900,00"), debit_row(date(2026, 7, 11), "EAU", "30,00"))
        with mock.patch("common.UPLOAD_MAX_FILE_BYTES", len(small)):
            response = self.post(("Juillet.csv", small), ("Enorme.csv", big))
        self.assertContains(response, "« Enorme.csv » pèse")
        self.assertContains(response, "au plus par fichier")
        self.assertContains(response, "1 opération(s) importée(s)")
        (line,) = BankTransaction.objects.all()
        self.assertIn("METRO", line.label)

    def test_a_selection_over_its_total_imports_nothing(self):
        rows = statement(debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"))
        with mock.patch("common.UPLOAD_MAX_TOTAL_BYTES", len(rows) + 1):
            response = self.post(("Juillet.csv", rows), ("Aout.csv", rows))
        self.assertContains(response, "La sélection pèse")
        self.assertContains(response, "en une fois")
        self.assertFalse(BankTransaction.objects.exists())
