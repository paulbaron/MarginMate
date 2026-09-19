"""What the till rang up is counted in « unités vendues », not « articles ».

Since 19/09 an « article » is what the products bought are ranged under (a
StockType), and the Ventes tab of Recettes & ventes already offers « recettes
ou articles vendus tels quels » in that sense. Its import card and the À lier
tab counted the till's units as « articles vendus » too - one page, two
meanings. The sales table's own header already said « Unités vendues ».

Data invented; nothing is downloaded.
"""

from datetime import date
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from recipes.models import PosProduct, SalesImportJob
from recipes.pos.laddition_xlsx import ParsedExport
from recipes.tasks import import_laddition_sales_task


class TillUnitsTests(TestCase):
    def test_an_import_counts_units_sold(self):
        sold_on = date(2026, 6, 1)
        export = ParsedExport(
            products={"Pinte Exemple": {"quantity": 42, "category": "", "typology": "", "first": sold_on, "last": sold_on}},
            entries=[("Pinte Exemple", sold_on, 42)],
        )
        job = SalesImportJob.objects.create(status=SalesImportJob.Status.PENDING)
        with (
            mock.patch("recipes.tasks.download_sales_lines", return_value=["ventes.xlsx"]),
            mock.patch("recipes.tasks.parse_sales_exports", return_value=export),
        ):
            import_laddition_sales_task(job.pk, sold_on, date(2026, 6, 30), download_dir="non-utilise")
        job.refresh_from_db()
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS)
        self.assertIn("(42 unités vendues)", job.log)
        card = self.client.get(reverse("recipes:sales_import_status", args=[job.pk]))
        self.assertContains(card, "<strong>42</strong> unités vendues")
        for text in (job.log, card.content.decode()):
            self.assertNotIn("articles vendus", text)

    def test_the_backlog_counts_units_sold(self):
        PosProduct.objects.create(name="Pinte Exemple", total_quantity=512)
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertContains(response, "512 unités vendues ne sont rattachées à aucune recette.")
        self.assertNotContains(response, "articles vendus ne sont")
