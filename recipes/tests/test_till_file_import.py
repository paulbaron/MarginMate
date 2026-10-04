"""A till's file uploaded on « Ventes » (till_views.upload_sales_file,
tasks.import_till_file_task) and L'Addition's card where its account is
ready.

The upload is checked on the page (weight, kind, format, day, a job already
running), staged under imports/, read by a job like the fetch's, written by
the one writer (`store_reading`) - payments only beside the days « Ventes »
holds - and kept only when read whole: L'Addition's export in downloads/
(the backfills read it there), any other till's file in downloads/caisse/.
A refused file is deleted. The thread is patched, its target run here.

Every file, name and figure is invented.
"""

from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from recipes import tasks
from recipes.models import PosDailyPayment, PosProduct, PosProductDailyQuantity, RecipeSale, SalesImportJob, TillFormat
from recipes.pos import connectors, till_file
from recipes.sales import MANUAL_SALE_SOURCE, TILL_SOURCE
from recipes.tests.test_pos_payments import TICKET_HEADER, ticket
from recipes.tests.test_pos_revenue import HEADER, line, write_workbook
from recipes.tests.till_support import LADDITION_ACCOUNT
from recipes.till_views import ALREADY_RUNNING, CHOOSE_A_FILE, DAY_NEEDED, DAY_TO_COME
from recipes.views import LADDITION_NOT_READY
from tests.factories import make_recipe
from tests.runner import employee_of_the_test_tenant

UPLOAD = reverse("recipes:upload_sales_file")
SALES = "Date;Article;Qté;Total TTC;TVA"


def sales_format(**fields) -> TillFormat:
    values = {
        "name": "Caisse Exemple",
        "day_column": "Date",
        "product_column": "Article",
        "quantity_column": "Qté",
        "amount_column": "Total TTC",
        "rate_column": "TVA",
    }
    values.update(fields)
    return TillFormat.objects.create(**values)


def payments_format() -> TillFormat:
    return TillFormat.objects.create(
        name="Caisse Exemple — encaissements",
        kind=TillFormat.Kind.PAYMENTS,
        day_column="Date",
        method_column="Moyen",
        paid_column="Montant",
        method_map="Carte bancaire = Carte",
    )


def csv(*lines: str, name: str = "export.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, "\n".join(lines).encode("utf-8"), content_type="text/csv")


def kept(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if path.is_file()) if folder.exists() else []


class UploadCase(TestCase):
    def setUp(self):
        # Folders of their own: the test espace's are the whole run's.
        self.enterContext(override_settings(TENANTS_ROOT=tempfile.mkdtemp()))

    def upload(self, file, choice, **fields):
        """POST the upload, the thread patched and its target run here, as
        the thread would. Returns (response, the job or None)."""
        with mock.patch("recipes.till_views.threading.Thread") as thread:
            response = self.client.post(UPLOAD, {"fichier": file, "format": str(choice), **fields}, follow=True)
        if not thread.called:
            return response, None
        kwargs = thread.call_args.kwargs
        self.target = kwargs["target"]
        self.target(*kwargs["args"])
        return response, SalesImportJob.objects.order_by("-pk").first()

    def messages(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]


class SalesFileTests(UploadCase):
    def setUp(self):
        super().setUp()
        self.fmt = sales_format()
        self.pinte = make_recipe(name="Pinte Exemple")

    def test_a_file_writes_the_till_s_days_its_money_and_the_recipes_sales(self):
        _response, job = self.upload(
            csv(SALES, "03/07/2026;Pinte Exemple;2;13,00;20 %", "03/07/2026;Inconnue Exemple;1;4,00;10"), self.fmt.pk
        )
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
        day = PosProductDailyQuantity.objects.get(product__name="Pinte Exemple")
        self.assertEqual(
            (day.sold_on, day.quantity, day.revenue_ttc, day.revenue_read),
            (date(2026, 7, 3), 2, Decimal("13.00"), True),
        )
        self.assertEqual(
            list(RecipeSale.objects.values_list("recipe", "sold_on", "source", "quantity")),
            [(self.pinte.pk, date(2026, 7, 3), TILL_SOURCE, 2)],
        )
        # A till name that is a recipe's is linked, as by the fetch.
        self.assertEqual(PosProduct.objects.get(name="Pinte Exemple").recipe, self.pinte)
        self.assertIn("Fichier « export.csv ».", job.log)
        self.assertIn("Format « Caisse Exemple ».", job.log)
        self.assertIn("1 produits de caisse sans recette", job.log)
        self.assertEqual((job.range_start, job.range_end), (date(2026, 7, 3), date(2026, 7, 3)))
        self.assertEqual(job.items_sold, 3)
        # Kept under downloads/caisse/, never in downloads/ itself; nothing staged.
        self.assertEqual(len(kept(paths.downloads_dir() / "caisse")), 1)
        self.assertTrue(kept(paths.downloads_dir() / "caisse")[0].endswith("-export.csv"))
        self.assertEqual(kept(paths.downloads_dir()), [])
        self.assertEqual(kept(tasks.staged_uploads_dir()), [])

    def test_the_same_file_twice_changes_nothing(self):
        lines = (SALES, "03/07/2026;Pinte Exemple;2;13,00;20 %")
        self.upload(csv(*lines), self.fmt.pk)
        before = list(
            PosProductDailyQuantity.objects.values_list("product__name", "sold_on", "quantity", "revenue_ttc")
        )
        _response, job = self.upload(csv(*lines), self.fmt.pk)
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
        after = list(PosProductDailyQuantity.objects.values_list("product__name", "sold_on", "quantity", "revenue_ttc"))
        self.assertEqual(after, before)
        self.assertEqual(RecipeSale.objects.get().quantity, 2)

    def test_a_later_file_replaces_each_product_of_each_day_it_holds_and_only_those(self):
        """What the card promises, no more: a product missing from the
        corrected file keeps its day; a sale typed by hand is never touched."""
        RecipeSale.objects.create(recipe=self.pinte, sold_on=date(2026, 7, 3), quantity=5, source=MANUAL_SALE_SOURCE)
        self.upload(
            csv(SALES, "03/07/2026;Pinte Exemple;2;13,00;20 %", "03/07/2026;Soda Exemple;1;3,50;10"), self.fmt.pk
        )
        self.upload(csv(SALES, "03/07/2026;Pinte Exemple;4;26,00;20 %"), self.fmt.pk)
        days = dict(PosProductDailyQuantity.objects.values_list("product__name", "quantity"))
        self.assertEqual(days, {"Pinte Exemple": 4, "Soda Exemple": 1})
        self.assertEqual(
            sorted(RecipeSale.objects.values_list("source", "quantity")), [(TILL_SOURCE, 4), (MANUAL_SALE_SOURCE, 5)]
        )

    def test_a_refused_file_is_said_and_deleted_nothing_written(self):
        _response, job = self.upload(csv(SALES, "03/07/2026;Pinte Exemple;deux;13,00;20 %"), self.fmt.pk)
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertIn("Échec : Ligne 2 : quantité illisible", job.log)
        self.assertNotIn(str(paths.downloads_dir()), job.log)
        self.assertFalse(PosProductDailyQuantity.objects.exists())
        self.assertEqual(kept(paths.downloads_dir() / "caisse"), [])
        self.assertEqual(kept(tasks.staged_uploads_dir()), [])

    def test_a_format_deleted_before_the_job_reads_is_said(self):
        with mock.patch("recipes.till_views.threading.Thread") as thread:
            self.client.post(
                UPLOAD, {"fichier": csv(SALES, "03/07/2026;Pinte Exemple;2;13,00;20"), "format": self.fmt.pk}
            )
        self.fmt.delete()
        thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        job = SalesImportJob.objects.get()
        self.assertIn(connectors.FORMAT_GONE, job.log)
        self.assertEqual(kept(tasks.staged_uploads_dir()), [])

    def test_the_job_says_who_imported(self):
        self.client.force_login(_owner())
        _response, job = self.upload(csv(SALES, "03/07/2026;Pinte Exemple;2;13,00;20"), self.fmt.pk)
        self.assertIn("Importé par Gérant Exemple.", job.log)

    def test_only_the_newest_files_are_kept(self):
        with mock.patch.object(tasks, "KEPT_FILES", 2):
            for quantity in (1, 2, 3):
                self.upload(
                    csv(SALES, f"03/07/2026;Pinte Exemple;{quantity};6,50;20", name=f"jour-{quantity}.csv"), self.fmt.pk
                )
        self.assertEqual(len(kept(paths.downloads_dir() / "caisse")), 2)


def _owner():
    from tests.runner import test_user

    user = test_user()
    user.first_name, user.last_name = "Gérant", "Exemple"
    user.save()
    return user


class DayGivenTests(UploadCase):
    def test_a_format_with_no_day_column_reads_the_day_given(self):
        fmt = sales_format(name="Rapport du jour", day_column="")
        _response, job = self.upload(
            csv("Article;Qté;Total TTC;TVA", "Soda Exemple;4;14,00;10"), fmt.pk, jour="2026-07-03"
        )
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
        self.assertEqual(PosProductDailyQuantity.objects.get().sold_on, date(2026, 7, 3))
        self.assertIn("Jour des ventes : 03/07/2026.", job.log)

    def test_the_day_is_asked_for_or_refused_before_any_job(self):
        no_day = sales_format(name="Rapport du jour", day_column="")
        dated = sales_format(name="Détail")
        response, job = self.upload(csv("Article;Qté", "Soda;1"), no_day.pk)
        self.assertIsNone(job)
        self.assertIn(DAY_NEEDED, self.messages(response))
        response, job = self.upload(csv(SALES, "03/07/2026;Soda;1;3,50;10"), dated.pk, jour="2026-07-03")
        self.assertIsNone(job)
        self.assertIn(till_file.DAY_GIVEN_TWICE, self.messages(response))
        self.assertFalse(SalesImportJob.objects.exists())


class PaymentsFileTests(UploadCase):
    def test_payments_only_beside_the_days_with_sales_the_others_said(self):
        product = PosProduct.objects.create(name="Pinte Exemple")
        PosProductDailyQuantity.objects.create(product=product, sold_on=date(2026, 7, 3), quantity=1)
        _response, job = self.upload(
            csv(
                "Date;Moyen;Montant",
                "03/07/2026;Carte bancaire;12,50",
                "03/07/2026;Lydia;3,00",
                "04/07/2026;Espèces;5,00",
            ),
            payments_format().pk,
        )
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
        self.assertEqual(
            sorted(PosDailyPayment.objects.values_list("sold_on", "method", "amount")),
            [(date(2026, 7, 3), PosDailyPayment.CARD, Decimal("12.50")), (date(2026, 7, 3), "Lydia", Decimal("3.00"))],
        )
        self.assertIn("1 jour(s) laissé(s) de côté : aucune vente enregistrée ces jours-là.", job.log)
        self.assertIn("« Lydia »", job.log)
        self.assertNotIn("produits de caisse vus", job.log)
        self.assertFalse(RecipeSale.objects.exists())


class LadditionExportUploadTests(UploadCase):
    def test_l_addition_s_export_is_read_without_a_format_and_kept_where_the_backfills_look(self):
        path = write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")],
        )
        upload = SimpleUploadedFile("Lignes de ventes.xlsx", Path(path).read_bytes())
        _response, job = self.upload(upload, connectors.LADDITION_CHOICE)
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
        self.assertEqual(PosProductDailyQuantity.objects.get().revenue_ttc, Decimal("7.50"))
        self.assertEqual(list(PosDailyPayment.objects.values_list("method", "amount")), [("CB", Decimal("7.50"))])
        self.assertIn("Paiements lus : 7,50 €", job.log)
        names = kept(paths.downloads_dir())
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".xlsx"))
        self.assertEqual(kept(paths.downloads_dir() / "caisse"), [])

    def test_a_refused_export_never_stays_where_the_backfills_read(self):
        upload = SimpleUploadedFile("faux.xlsx", b"pas un classeur")
        _response, job = self.upload(upload, connectors.LADDITION_CHOICE)
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertIn("n'est pas l'export « Lignes de ventes »", job.log)
        self.assertEqual(kept(paths.downloads_dir()), [])
        self.assertEqual(kept(tasks.staged_uploads_dir()), [])

    def test_a_hostile_figure_is_refused_before_any_row(self):
        path = write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", "1E+500", "20%")])
        _response, job = self.upload(SimpleUploadedFile("x.xlsx", Path(path).read_bytes()), connectors.LADDITION_CHOICE)
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertFalse(PosProductDailyQuantity.objects.exists())

    def test_its_export_is_an_xlsx(self):
        response, job = self.upload(csv(SALES), connectors.LADDITION_CHOICE)
        self.assertIsNone(job)
        self.assertIn(connectors.LADDITION_XLSX, self.messages(response))


class RefusedAtTheDoorTests(UploadCase):
    def test_each_refusal_before_any_job_or_file(self):
        fmt = sales_format()
        cases = (
            ({"format": str(fmt.pk)}, CHOOSE_A_FILE),
            ({"fichier": csv(SALES, name="x.xls"), "format": str(fmt.pk)}, till_file.XLS_REFUSED),
            ({"fichier": csv(SALES, name="x.pdf"), "format": str(fmt.pk)}, till_file.SUFFIX_REFUSED),
            ({"fichier": csv(SALES), "format": "abc"}, connectors.FORMAT_UNKNOWN),
            ({"fichier": csv(SALES), "format": "²"}, connectors.FORMAT_UNKNOWN),
            ({"fichier": csv(SALES), "format": "999999"}, connectors.FORMAT_GONE),
            ({"fichier": csv(SALES), "format": str(fmt.pk), "jour": "hier"}, "Jour des ventes illisible."),
            ({"fichier": csv(SALES), "format": str(fmt.pk), "jour": "2099-01-01"}, DAY_TO_COME),
        )
        for data, said in cases:
            with self.subTest(said=said), mock.patch("recipes.till_views.threading.Thread") as thread:
                response = self.client.post(UPLOAD, data, follow=True)
                thread.assert_not_called()
                self.assertIn(said, self.messages(response))
        self.assertFalse(SalesImportJob.objects.exists())
        self.assertEqual(kept(tasks.staged_uploads_dir()), [])

    def test_a_format_the_check_refuses_now_is_said(self):
        fmt = sales_format()
        TillFormat.objects.filter(pk=fmt.pk).update(quantity_column="")
        response, job = self.upload(csv(SALES), fmt.pk)
        self.assertIsNone(job)
        self.assertTrue(any("est à corriger" in said for said in self.messages(response)))

    def test_a_file_too_heavy_is_refused_by_its_name(self):
        fmt = sales_format()
        with override_settings(), mock.patch("common.UPLOAD_MAX_FILE_BYTES", 10):
            response, job = self.upload(csv(SALES, "03/07/2026;Soda;1;3,50;10"), fmt.pk)
        self.assertIsNone(job)
        self.assertTrue(any("export.csv" in said for said in self.messages(response)))

    def test_a_job_running_holds_the_upload(self):
        fmt = sales_format()
        job = SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        job.beat()
        response, new = self.upload(csv(SALES, "03/07/2026;Soda;1;3,50;10"), fmt.pk)
        self.assertIsNone(new)
        self.assertIn(ALREADY_RUNNING, self.messages(response))

    def test_a_get_starts_nothing(self):
        self.assertRedirects(self.client.get(UPLOAD), reverse("recipes:sales_list"), fetch_redirect_response=False)
        self.assertFalse(SalesImportJob.objects.exists())

    def test_an_employee_may_not_upload(self):
        fmt = sales_format()
        self.client.force_login(employee_of_the_test_tenant("serveur@example.invalid", ["recipes"], name="Serveur"))
        with mock.patch("recipes.till_views.threading.Thread") as thread:
            response = self.client.post(UPLOAD, {"fichier": csv(SALES), "format": fmt.pk})
        thread.assert_not_called()
        self.assertContains(response, "Page non accessible", status_code=403)
        page = self.client.get(reverse("recipes:sales_list")).content.decode()
        self.assertNotIn(UPLOAD, page)
        self.assertNotIn("Identifiants", page)


class SalesTabDoorsTests(TestCase):
    def page(self) -> str:
        return self.client.get(reverse("recipes:sales_list")).content.decode()

    def test_without_its_account_no_l_addition_card_one_line_for_the_owner(self):
        page = self.page()
        self.assertNotIn(reverse("recipes:trigger_sales_import"), page)
        self.assertIn("Votre caisse est L'Addition ?", page)
        self.assertIn(reverse("accounts:credentials"), page)
        # The file card is drawn, with its own job control.
        self.assertIn(UPLOAD, page)
        self.assertIn('enctype="multipart/form-data"', page)
        self.assertIn("Lignes de ventes » (.xlsx)</option>", page)
        self.assertIn("Décrivez d'abord les colonnes de votre fichier", page)

    @LADDITION_ACCOUNT
    def test_with_its_account_the_card_and_no_server_variable(self):
        page = self.page()
        self.assertIn(reverse("recipes:trigger_sales_import"), page)
        self.assertIn("Récupérer les ventes de L'Addition", page)
        self.assertNotIn("Votre caisse est L'Addition ?", page)
        self.assertNotIn("LADDITION_EMAIL", page)
        self.assertEqual(page.count('data-job-control="sales-import-status"'), 2)

    def test_the_status_card_shows_whatever_door_started_the_job(self):
        SalesImportJob.objects.create(status=SalesImportJob.Status.SUCCESS, log="Fichier « export.csv ».\n")
        page = self.page()
        self.assertIn('id="sales-import-status"', page)
        self.assertIn("export.csv", page)

    def test_a_format_is_offered_by_its_name(self):
        sales_format()
        page = self.page()
        self.assertIn("Caisse Exemple (Ventes par produit)", page)
        self.assertNotIn("Décrivez d'abord", page)

    def test_the_fetch_is_refused_before_any_job_without_its_account(self):
        with mock.patch("recipes.views.threading.Thread") as thread:
            response = self.client.post(
                reverse("recipes:trigger_sales_import"),
                {"start_date": "2026-06-01", "end_date": "2026-06-30"},
                follow=True,
            )
        thread.assert_not_called()
        self.assertIn(LADDITION_NOT_READY, [str(message) for message in response.context["messages"]])
        self.assertFalse(SalesImportJob.objects.exists())


class AnotherBarTests(TwoTenantsTestCase):
    """Bar B never signs in with the owner's L'Addition account, even once
    the till may be used in every espace (the connectors' change, simulated
    by patching `till_allowed`): the .env's values are the owner's alone."""

    owner_a = True

    def test_the_owner_s_env_account_is_not_bar_b_s(self):
        with (
            override_settings(LADDITION_EMAIL="caisse@example.invalid", LADDITION_PASSWORD="mot-de-passe-essai"),
            mock.patch("recipes.integration.till_allowed", return_value=True),
        ):
            with bound_tenant(self.bar_b):
                self.assertFalse(connectors.LADDITION.ready())
            with bound_tenant(self.bar_a):
                self.assertTrue(connectors.LADDITION.ready())

    def test_a_file_uploaded_in_bar_b_lands_in_bar_b_alone(self):
        with bound_tenant(self.bar_b):
            fmt = sales_format()
        self.client.force_login(self.user_b)
        with mock.patch("recipes.till_views.threading.Thread") as thread:
            self.client.post(UPLOAD, {"fichier": csv(SALES, "03/07/2026;Pinte Exemple;2;13,00;20"), "format": fmt.pk})
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, tasks.import_till_file_task)
        self.assertEqual(target.tenant.pk, self.bar_b.pk)
        target(*thread.call_args.kwargs["args"])
        with bound_tenant(self.bar_b):
            self.assertEqual(PosProductDailyQuantity.objects.get().quantity, 2)
            self.assertEqual(len(kept(paths.downloads_dir() / "caisse")), 1)
        with bound_tenant(self.bar_a):
            self.assertFalse(PosProductDailyQuantity.objects.exists())
            self.assertEqual(kept(paths.downloads_dir() / "caisse"), [])
