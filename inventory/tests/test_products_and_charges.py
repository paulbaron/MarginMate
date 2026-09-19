"""« Produits & charges »: the words of the page, and the figures they name.

The owner, 19/09: this page is not a stock, it is the list of everything
spent - the products bought, ranged by article, and the charges. So the
workspace is « Produits & charges », a StockType is an « article » wherever
it is read, and the all-time figures say what was BOUGHT (« Total acheté »,
« Acheté »), counted from the purchases alone: a loss written down later
must not make « acheté » a lie. What really is stock - the window between
two inventaires, what left the shelf, what is missing, the losses - keeps
the word and its own arithmetic.

Data invented.
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import MovementKind, UnitChoices
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)

D = Decimal


class WhatWasBoughtTests(TestCase):
    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        supplier = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple")
        product = make_product(
            supplier=supplier, raw_name="VODKA EXEMPLE 1L", stock_type=self.vodka, unit=UnitChoices.LITRE
        )
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 2, 1))
        line = make_invoice_line(invoice=invoice, product=product, quantity=12, total_ht="240", vat_rate=D("0.20"))
        make_movement(stock_type=self.vodka, quantity="12", unit_cost_ht="20", invoice_line=line)
        # Two litres broken, written down afterwards: they left the shelf,
        # they were bought all the same.
        make_movement(
            stock_type=self.vodka, kind=MovementKind.LOSS, quantity="-2", unit_cost_ht="20",
            occurred_on=date(2026, 3, 1),
        )
        self.url = reverse("inventory:stock_list")

    def row(self, response):
        return next(
            row for category in response.context["categories"] for row in category["rows"]
            if row["stock_type"] == self.vodka
        )

    def test_the_headline_is_what_was_bought_and_a_loss_does_not_lower_it(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Total acheté (HT)")
        self.assertContains(response, "Total acheté (TTC)")
        self.assertNotContains(response, "Valeur du stock")
        self.assertEqual(response.context["total_value_ht"], D("240"))
        self.assertEqual(response.context["total_value_ttc"], D("288"))
        self.assertContains(response, "240.00 €")
        row = self.row(response)
        self.assertEqual((row["quantity"], row["value_ht"], row["value_ttc"]), (D("12"), D("240"), D("288")))
        self.assertEqual(response.context["categories"][0]["total_value_ht"], D("240"))

    def test_the_list_says_acheté_not_quantité(self):
        response = self.client.get(self.url)
        self.assertContains(response, ">Acheté</th>")
        self.assertContains(response, ">Total HT</th>")
        self.assertContains(response, ">Total TTC</th>")
        self.assertContains(response, "<th>Article</th>")
        self.assertContains(response, "Depuis le début (tous les achats)")

    def test_what_could_have_been_sold_is_still_the_shelf(self):
        """The ceiling of « Vendu » is a true-stock notion: two broken litres
        cannot have been poured, so it stays the ledger - bought less the
        losses - and does not follow « Acheté »."""
        recipe = make_recipe(name="Vodka tonic")
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.05")
        record_sales([("Vodka tonic", date(2026, 3, 5), 20)])
        row = self.row(self.client.get(self.url))
        self.assertEqual(row["quantity"], D("12"))
        self.assertEqual(row["sold"].available, D("10"))
        self.assertEqual(self.vodka.current_quantity, D("10"))

    def test_vendu_over_the_shelf_says_the_losses_came_off(self):
        """Eleven litres sold of twelve bought, two of them broken: « Vendu »
        stands under « Acheté » and the row is red all the same, since the
        ceiling is the shelf. Its tooltip and the headline said « plus qu'il
        n'en a été acheté », which the row beside them contradicts."""
        recipe = make_recipe(name="Vodka sec")
        make_ingredient(recipe, stock_type=self.vodka, quantity="1")
        record_sales([("Vodka sec", date(2026, 3, 5), 11)])
        response = self.client.get(self.url)
        row = self.row(response)
        self.assertEqual((row["quantity"], row["sold"].headline), (D("12"), D("11")))
        self.assertTrue(row["flag_over"])
        self.assertEqual(response.context["over_stock_count"], 1)
        self.assertContains(
            response, "Vendu plus qu'il n'en a été acheté, pertes déclarées déduites : il manque 1.00 Litre"
        )
        self.assertContains(response, "dont les ventes dépassent les achats (pertes déclarées déduites)")

    def test_a_shared_sale_says_the_losses_came_off_too(self):
        """Sales shared between alternatives are spread up to the same
        ceiling, and the « ? » beside them says so."""
        gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE, category="Spiritueux")
        make_movement(stock_type=gin, quantity="5", unit_cost_ht="15")
        recipe = make_recipe(name="Vodka ou gin tonic")
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.05", group=0)
        make_ingredient(recipe, stock_type=gin, quantity="0.05", group=0)
        record_sales([("Vodka ou gin tonic", date(2026, 3, 5), 40)])
        response = self.client.get(self.url)
        self.assertTrue(self.row(response)["sold"].is_ambiguous)
        self.assertContains(
            response, "dans la limite de ce qui a été acheté, pertes déclarées déduites (moins 10 % de perte)"
        )

    def test_the_charges_stand_beside_what_was_bought(self):
        water = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)
        bill = make_invoice(supplier=water, invoice_date=timezone.localdate() - timedelta(days=30))
        make_invoice_line(
            invoice=bill, product=make_product(supplier=water, raw_name="EAU EXEMPLE", is_expense=True),
            total_ht="50", vat_rate=D("0.055"),
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Charges (TTC)")
        self.assertContains(response, "52.75 €")
        self.assertContains(response, "Charges et abonnements")
        self.assertNotContains(response, "hors stock")

    def test_no_charge_no_charges_figure(self):
        self.assertNotContains(self.client.get(self.url), "Charges (TTC)")

    def test_between_two_inventaires_the_page_keeps_its_stock_words(self):
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(
            stock_take=take, product=None, stock_type=self.vodka, counted_quantity="4", unit=UnitChoices.LITRE
        )
        response = self.client.get(self.url, {"inventaire": take.pk})
        self.assertContains(response, "sorti du stock")
        self.assertContains(response, "Manquant (HT)")
        self.assertNotContains(response, "Total acheté")
        self.assertNotContains(response, "Charges (TTC)")


class ArticleWordingTests(TestCase):
    """A StockType is an « article » wherever it is read."""

    def test_no_page_calls_it_a_type_de_stock(self):
        supplier = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple")
        gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        make_product(supplier=supplier, raw_name="RHUM A CLASSER")  # the side panel is drawn
        take = make_stock_take()
        make_stock_take_line(stock_take=take, product=None, stock_type=gin, counted_quantity="2", unit=UnitChoices.LITRE)
        pages = [
            reverse("inventory:stock_list"),
            reverse("inventory:stock_type_create"),
            reverse("inventory:stock_type_update", args=[gin.pk]),
            reverse("transfer:data_home"),
            reverse("inventory:stock_take_create"),
            reverse("inventory:stock_take_detail", args=[take.pk]),
            reverse("recipes:recipe_create"),
            reverse("recipes:sale_document_create"),
        ]
        for url in pages:
            with self.subTest(url=url):
                # What is read, not the scripts: the inventory form's knows
                # the old suffix to rename a draft typed before 19/09.
                html = re.sub(r"<script.*?</script>", "", self.client.get(url).content.decode(), flags=re.S).lower()
                for old in ("type de stock", "types de stock", "type(s) de stock", "article de stock", "types vides"):
                    self.assertNotIn(old, html)

    def test_one_unit_bought_is_no_article(self):
        """A bottle or a keg bought is « une unité achetée »: a card read
        « taille 30L par article » just above its « Article » field."""
        from inventory.quantity_extraction import extract_quantity

        keg = extract_quantity("FUT BIERE EXEMPLE 30L", colisage=1, qty=1, total_volume=D("0"))
        self.assertEqual(keg.note, "taille 30L par unité achetée")
        crate = extract_quantity("SODA EXEMPLE 24X33CL", colisage=1, qty=24, total_volume=D("7.92"))
        self.assertEqual(crate.note, "vendu à l'unité (0.33L par unité achetée ≤ 33cl)")

    def test_the_écarts_count_units_bought(self):
        """« Valorisé en Rhum (…, 0.700 L par article) »: Rhum is the
        article, 0,7 L one bottle of it."""
        rum = make_stock_type(name="Rhum", unit=UnitChoices.LITRE)
        make_movement(stock_type=rum, quantity="100", unit_cost_ht="15")
        make_product(
            supplier=make_supplier(code="GROSSISTE_X", name="Grossiste Exemple"), raw_name="RHUM EXEMPLE 70CL",
            stock_type=rum, unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        opening = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        make_stock_take_line(stock_take=opening, product=None, stock_type=rum, counted_quantity="20", unit=UnitChoices.LITRE)
        closing = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(stock_take=closing, product=None, stock_type=rum, counted_quantity="15", unit=UnitChoices.LITRE)
        response = self.client.get(reverse("inventory:stock_take_variance", args=[closing.pk]))
        self.assertContains(response, "Valorisé en Rhum")
        self.assertContains(response, "0.700 L par unité achetée")
        self.assertNotContains(response, "L par article")

    def test_an_inventaire_counts_lines(self):
        """Its lines are products mostly, an article now and then: « Articles
        comptés » named both with the word for one of them, where the list
        of inventaires said « Produits comptés » for the same figure."""
        supplier = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple")
        gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        take = make_stock_take()
        make_stock_take_line(
            stock_take=take, product=None, stock_type=gin, counted_quantity="9", unit=UnitChoices.LITRE,
            value_ht="90", has_shortfall=True, shortfall_quantity=D("3"),
        )
        make_stock_take_line(
            stock_take=take, product=make_product(supplier=supplier, raw_name="GIN EXEMPLE 70CL", stock_type=gin),
            counted_quantity="2",
        )
        detail = self.client.get(reverse("inventory:stock_take_detail", args=[take.pk]))
        self.assertContains(detail, "Lignes comptées")
        self.assertContains(detail, "1 ligne comptée au-delà")
        self.assertNotContains(detail, "rticles comptés")
        self.assertNotContains(detail, "article compté")
        listing = self.client.get(reverse("inventory:stock_take_list"))
        self.assertContains(listing, ">Lignes comptées</th>")
        self.assertNotContains(listing, "Produits comptés")

    def test_the_title_is_the_workspace(self):
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "<title>Produits & charges - MarginMate</title>")
        self.assertContains(response, "<h1>Produits &amp; charges</h1>")

    def test_export_and_import_lead_to_the_données_page(self):
        """The two associations buttons became « Données »'s: the export tab
        with the associations ticked, and the import tab."""
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, '<a class="btn btn-secondary" href="/donnees/?cocher=associations">⬇️ Exporter…</a>')
        self.assertContains(response, '<a class="btn btn-secondary" href="/donnees/importer/">⬆️ Importer…</a>')
        self.assertNotContains(response, "Exporter les associations")

    def test_a_name_already_taken_is_refused_in_french(self):
        """Django's own « Stock type with this Name already exists. » was
        what a rename collision printed, in English."""
        make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        for url in (reverse("inventory:stock_type_update", args=[vodka.pk]), reverse("inventory:stock_type_create")):
            with self.subTest(url=url):
                response = self.client.post(
                    url, {"name": "Gin", "unit": UnitChoices.LITRE, "category": "", "loss_percent": "0"}
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Un article porte déjà ce nom.")
                self.assertNotContains(response, "already exists")
        # The rename is still offered as a merge, in the same words.
        response = self.client.post(
            reverse("inventory:stock_type_update", args=[vodka.pk]),
            {"name": "Gin", "unit": UnitChoices.LITRE, "category": "", "loss_percent": "0"},
        )
        self.assertContains(response, "Un article nommé « <strong>Gin</strong> » existe déjà")
