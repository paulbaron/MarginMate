"""« Formats des fichiers de caisse » (recipes/till_views.py): the pages
describing how a bar's own till export is read, with « Tester ».

A GET writes nothing; « Tester » reads the file with the format as typed and
saves nothing, keeps no file; a refusal is said on its field; two formats
never share a name whatever its case and accents; and the pages are the
owner's - a format decides what an upload writes into the till's sales and
payments. Every file is invented.
"""

from __future__ import annotations

import io
from datetime import date
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts import paths
from recipes.models import PosProduct, TillFormat
from recipes.pos import till_file
from recipes.till_views import FORMAT_EXAMPLE, TEST_FILE
from tests.runner import employee_of_the_test_tenant

FIELDS = {
    "name": "Caisse Exemple",
    "kind": "ventes",
    "encoding": "auto",
    "delimiter": ";",
    "decimal_mark": ",",
    "date_format": "dd/mm/yyyy",
    "sheet": "",
    "day_column": "Date",
    "time_column": "",
    "service_day_end_hour": "0",
    "product_column": "Article",
    "quantity_column": "Qté",
    "amount_column": "Total TTC",
    "amount_ht_column": "",
    "rate_column": "TVA",
    "category_column": "",
    "typology_column": "",
    "method_column": "",
    "paid_column": "",
    "method_map": "",
}
EXPORT = (
    "Date;Article;Qté;Total TTC;TVA\n03/07/2026;Pinte Exemple;2;13,00;20 %\n03/07/2026;Soda Exemple;1;3,50;10"
).encode()


def upload(content: bytes = EXPORT, name: str = "export.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/csv")


def files_in_the_espace() -> list[str]:
    return sorted(str(path) for path in paths.downloads_dir().rglob("*") if path.is_file())


class NewFormatTests(TestCase):
    URL = reverse("recipes:till_formats")

    def post(self, action, **changes):
        data = {**FIELDS, **changes, "action": action}
        return self.client.post(self.URL, data)

    def test_a_get_writes_nothing_and_pre_fills_the_payment_spellings(self):
        page = self.client.get(self.URL).content.decode()
        self.assertFalse(TillFormat.objects.exists())
        self.assertIn("Carte bancaire = Carte", page)
        self.assertIn("Formats des fichiers de caisse", page)

    def test_tester_reads_the_file_and_saves_nothing_keeps_nothing(self):
        PosProduct.objects.create(name="Soda Exemple")
        before = files_in_the_espace()
        response = self.client.post(self.URL, {**FIELDS, "action": "tester", TEST_FILE: upload()})
        page = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TillFormat.objects.exists())
        self.assertEqual(files_in_the_espace(), before)
        test = response.context["test"]
        self.assertEqual(test.refusal, "")
        self.assertEqual(test.export.entries[0], ("Pinte Exemple", date(2026, 7, 3), 2))
        self.assertEqual((test.known_products, test.new_products), (1, ["Pinte Exemple"]))
        self.assertIn("rien n'est enregistré", page)
        self.assertIn("produit → colonne 2 « Article »", page)
        self.assertIn("16,50", page.replace("16.50", "16,50"))
        self.assertIn("1 nouveau (à lier) : Pinte Exemple", page)

    def test_tester_says_the_import_s_own_refusal(self):
        bad = "Date;Article;Qté;Total TTC;TVA\n03/07/2026;Soda;deux;3,50;10".encode()
        test = self.client.post(self.URL, {**FIELDS, "action": "tester", TEST_FILE: upload(bad)}).context["test"]
        self.assertIn("Ligne 2", test.refusal)
        test = self.client.post(self.URL, {**FIELDS, "action": "tester", TEST_FILE: upload(name="x.xls")}).context[
            "test"
        ]
        self.assertIn(till_file.XLS_REFUSED, test.problem)
        test = self.client.post(self.URL, {**FIELDS, "action": "tester"}).context["test"]
        self.assertIn("Choisissez un fichier", test.problem)

    def test_tester_a_format_with_no_day_column_at_the_day_given(self):
        content = "Article;Qté;Total TTC;TVA\nSoda Exemple;4;14,00;10".encode()
        response = self.client.post(
            self.URL,
            {**FIELDS, "day_column": "", "action": "tester", TEST_FILE: upload(content), "jour_essai": "2026-07-03"},
        )
        self.assertEqual(response.context["test"].export.entries, [("Soda Exemple", date(2026, 7, 3), 4)])

    def test_a_refusal_is_said_on_its_field_and_nothing_is_saved(self):
        response = self.post("enregistrer", product_column="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("product_column", response.context["form"].errors)
        response = self.post("enregistrer", category_column="article")
        self.assertIn("sert deux fois", " ".join(response.context["form"].errors["category_column"]))
        response = self.post("enregistrer", service_day_end_hour="12")
        self.assertIn("Une heure de 0 à 11.", response.context["form"].errors["service_day_end_hour"])
        response = self.post("enregistrer", name="A\x00B")
        self.assertIn("name", response.context["form"].errors)
        self.assertFalse(TillFormat.objects.exists())

    def test_saved_and_a_name_taken_whatever_its_accents_and_case(self):
        response = self.post("enregistrer", name="Caisse  Été")
        fmt = TillFormat.objects.get()
        self.assertEqual(fmt.name, "Caisse Été")
        self.assertRedirects(response, reverse("recipes:till_formats") + f"#format-{fmt.pk}")
        response = self.post("enregistrer", name="CAISSE ETE")
        self.assertIn("porte déjà ce nom", " ".join(response.context["form"].errors["name"]))
        self.assertEqual(TillFormat.objects.count(), 1)

    def test_an_unknown_action_changes_nothing(self):
        self.assertRedirects(self.post("effacer-tout"), self.URL)
        self.assertFalse(TillFormat.objects.exists())


class OneFormatTests(TestCase):
    def setUp(self):
        self.fmt = TillFormat.objects.create(
            name="Caisse Exemple",
            day_column="Date",
            product_column="Article",
            quantity_column="Qté",
            amount_column="Total TTC",
            rate_column="TVA",
        )
        self.url = reverse("recipes:till_format", kwargs={"pk": self.fmt.pk})

    def test_a_get_writes_nothing(self):
        page = self.client.get(self.url).content.decode()
        self.fmt.refresh_from_db()
        self.assertEqual(self.fmt.product_column, "Article")
        self.assertIn("Format « Caisse Exemple »", page)

    def test_tester_on_its_page_and_saving_an_edit(self):
        response = self.client.post(
            self.url, {**FIELDS, "quantity_column": "3", "action": "tester", TEST_FILE: upload()}
        )
        self.assertEqual(response.context["test"].export.total_quantity, 3)
        self.fmt.refresh_from_db()
        self.assertEqual(self.fmt.quantity_column, "Qté")
        self.client.post(self.url, {**FIELDS, "quantity_column": "3", "action": "enregistrer"})
        self.fmt.refresh_from_db()
        self.assertEqual(self.fmt.quantity_column, "3")

    def test_a_refused_edit_leaves_the_title_as_stored(self):
        response = self.client.post(self.url, {**FIELDS, "name": "", "action": "enregistrer"})
        self.assertContains(response, "Format « Caisse Exemple »")

    def test_deleted(self):
        self.client.post(self.url, {"action": "supprimer"})
        self.assertFalse(TillFormat.objects.exists())

    def test_a_stored_format_the_check_refuses_says_so(self):
        TillFormat.objects.filter(pk=self.fmt.pk).update(quantity_column="")
        page = self.client.get(self.url).content.decode()
        self.assertIn("aucun fichier ne se lit avec ce format", page)
        self.assertIn("à corriger", self.client.get(reverse("recipes:till_formats")).content.decode())

    def test_an_unknown_format_is_a_404(self):
        self.assertEqual(self.client.get(reverse("recipes:till_format", kwargs={"pk": 999999})).status_code, 404)


class OwnerOnlyTests(TestCase):
    """An employee given « Recettes & ventes » opens the tab, not the formats:
    a format decides what an upload writes into the till's payments."""

    def test_an_employee_opens_no_format_page(self):
        fmt = TillFormat.objects.create(
            name="Caisse Exemple", day_column="Date", product_column="A", quantity_column="B"
        )
        self.client.force_login(employee_of_the_test_tenant("serveur@example.invalid", ["recipes"], name="Serveur"))
        self.assertEqual(self.client.get(reverse("recipes:sales_list")).status_code, 200)
        for url in (reverse("recipes:till_formats"), reverse("recipes:till_format", kwargs={"pk": fmt.pk})):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "Page non accessible", status_code=403)
                self.assertContains(
                    self.client.post(url, {"action": "supprimer"}), "Page non accessible", status_code=403
                )
        self.assertTrue(TillFormat.objects.exists())


class ExampleTests(TestCase):
    def test_the_worked_example_reads_what_it_says(self):
        fmt = SimpleNamespace(**{**{key: "" for key in FIELDS}, "service_day_end_hour": 0, **FORMAT_EXAMPLE.settings})
        content = "\n".join(FORMAT_EXAMPLE.rows).encode("utf-8")
        export = till_file.read(io.BytesIO(content), till_file.check_format(fmt), file_name="exemple.csv").export
        self.assertEqual(export.entries, [("Pinte Exemple", date(2026, 7, 3), 3)])
        money = export.money[("Pinte Exemple", date(2026, 7, 3))]
        self.assertEqual((str(money.revenue_ttc), str(money.revenue_ht)), ("19.50", "16.25"))
        self.assertIn("19,50 € TTC, 16,25 € HT", FORMAT_EXAMPLE.reads)
