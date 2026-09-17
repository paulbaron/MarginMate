"""Smoke-GET every page in the app.

Cheap and shallow on purpose. It won't tell you a number is wrong, but it
catches the whole "page X now 500s" class in one go - a template referencing
a property that was renamed, a view reading a field that moved, a division
by a value that can be zero. Those are exactly the failures that otherwise
only surface when the page is next opened by hand.

Every route is built against real (if tiny) objects rather than an empty
database, because an empty database is the one case that accidentally works:
no rows means no loop body, so a broken row template never renders.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from inventory.models import UnitChoices
from invoices.models import ReceiptBatch, ShopItemPrice
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_invoice_type,
    make_priced_stock_type,
    make_product,
    make_purchase_history,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_supplier,
)


def assertNoUnrenderedTemplateSyntax(test, response, label=""):
    """Django's `{# ... #}` comment is SINGLE-LINE ONLY - spread one over
    several lines and the whole thing is printed to the page as text (and any
    tag inside it is executed). It renders fine, returns 200, and looks
    perfectly correct to every other kind of test; the only way to notice is
    to look at the output. So: look at the output.
    """
    content = response.content.decode(response.charset or "utf-8")
    # "}}" is deliberately not checked: inline JSON and JS legitimately end
    # with it ("...}}" closing nested braces), and a check that cries wolf on
    # every page with a script tag is worse than no check.
    for marker in ("{#", "#}", "{%", "%}", "{{"):
        test.assertNotIn(marker, content, f"unrendered template syntax {marker!r} on {label or 'the page'}")


class PageSmokeTests(TestCase):
    """Populated database - every list has rows, every detail page exists."""

    @classmethod
    def setUpTestData(cls):
        cls.supplier = make_supplier(code="METRO", name="Metro", parser_key="METRO")
        cls.vodka = make_priced_stock_type(name="Vodka", unit=UnitChoices.LITRE, unit_cost_ht="14", quantity="5")
        cls.limes = make_priced_stock_type(
            name="Citrons verts", unit=UnitChoices.KILOGRAM, unit_cost_ht="3", quantity="2"
        )
        cls.product = make_product(
            supplier=cls.supplier, raw_name="SOBIESKI VODKA 70CL", stock_type=cls.vodka, stock_equivalent="0.7"
        )
        # A product still in the review queue.
        cls.unassigned = make_product(supplier=cls.supplier, raw_name="RHUM INCONNU 70CL")
        make_purchase_history(cls.product, [("2026-01-10", 6, "60.00"), ("2026-02-10", 6, "90.00")])

        cls.invoice = make_invoice(supplier=cls.supplier, invoice_date=date(2026, 3, 1))
        make_invoice_line(invoice=cls.invoice, product=cls.product, quantity=6, total_ht="90.00")
        make_invoice_line(invoice=cls.invoice, product=cls.unassigned, quantity=1, total_ht="20.00")

        cls.invoice_type = make_invoice_type(supplier=cls.supplier, name="Metro - Factures", parser_key="METRO")

        # A photographed till receipt waiting to be checked. It carries a
        # FAILED check on purpose: the review page renders the failing and
        # passing branches differently, and the empty state hides both.
        cls.receipt_supplier = make_supplier(code="SABBH", name="Sabbh Oriental", parser_key="SABBH")
        cls.receipt = make_invoice(
            supplier=cls.receipt_supplier,
            invoice_date=date(2026, 7, 14),
            parse_checks=[
                {"label": "Somme des lignes = total imprimé", "passed": False, "detail": "écart +0.49 €"},
                {"label": "TVA 5.5% cohérente", "passed": True, "detail": "HT 1.99 € x 5.5%"},
            ],
            ocr_text="Sabbh Oriental\nArticle divers\n3pcs  0,70  2,10A",
            ocr_confidence=Decimal("0.86"),
        )
        cls.receipt_product = make_product(
            supplier=cls.receipt_supplier, raw_name="Article divers (0.70 EUR/u)"
        )
        make_invoice_line(
            invoice=cls.receipt,
            product=cls.receipt_product,
            quantity=3,
            total_ht="1.99",
            unit_cost_ht="0.6633",
            vat_rate=Decimal("0.055"),
            raw_name="Article divers (0.70 EUR/u)",
        )
        ShopItemPrice.objects.create(
            supplier=cls.receipt_supplier, unit_price_ttc=Decimal("0.70"), label="Citron vert"
        )
        # A finished folder import with every outcome the batch page draws.
        cls.batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {"name": "ok.pdf", "status": "ok", "invoice_id": cls.receipt.pk, "shop": "Sabbh Oriental",
                 "total": "2.10", "date": "14/07/2026", "verified": False},
                {"name": "dup.pdf", "status": "duplicate", "message": "Fichier déjà importé"},
                {"name": "x.pdf", "status": "unrecognised", "message": "Enseigne non reconnue", "kept": True},
                {"name": "Thumbs.db", "status": "ignored", "message": "Ni un PDF ni une photo : ignoré."},
            ],
        )

        cls.recipe = make_recipe(name="Moscow Mule", category="Cocktail", selling_price_ttc="8.50")
        make_ingredient(cls.recipe, stock_type=cls.vodka, quantity="0.04", group=0)
        make_ingredient(cls.recipe, stock_type=cls.limes, quantity="0.02", group=1)
        # A second recipe with real alternatives, so variation rendering is
        # exercised rather than just the single-variation path.
        cls.variant_recipe = make_recipe(name="Alcool + Soda", selling_price_ttc="8.50")
        make_ingredient(cls.variant_recipe, stock_type=cls.vodka, quantity="0.04", group=1)
        make_ingredient(cls.variant_recipe, stock_type=cls.limes, quantity="0.05", group=1)
        make_ingredient(cls.variant_recipe, stock_type=cls.vodka, quantity="0.20", group=2)

        cls.stock_take = make_stock_take()
        make_stock_take_line(
            stock_take=cls.stock_take, product=cls.product, counted_quantity="4",
            unit=UnitChoices.UNIT, value_ht="60.00",
        )

    def assertPageOK(self, name, **kwargs):
        url = reverse(name, kwargs=kwargs)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{name} ({url}) returned {response.status_code}")
        assertNoUnrenderedTemplateSyntax(self, response, url)
        return response

    def assertRedirectsOnGet(self, name, **kwargs):
        """POST-only actions redirect rather than 405 - still worth hitting,
        since a broken one raises before it ever gets to the redirect."""
        url = reverse(name, kwargs=kwargs)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302, f"{name} ({url}) returned {response.status_code}")

    # --- inventory -------------------------------------------------------
    def test_stock_list(self):
        response = self.assertPageOK("inventory:stock_list")
        self.assertContains(response, "Vodka")
        # The products to classify are on the same page.
        self.assertContains(response, "RHUM INCONNU")

    def test_stock_catalogue(self):
        self.assertContains(self.assertPageOK("inventory:stock_catalogue"), "Vodka")

    def test_stock_type_update(self):
        self.assertPageOK("inventory:stock_type_update", pk=self.vodka.pk)

    def test_stock_type_create(self):
        self.assertPageOK("inventory:stock_type_create")

    def test_stock_type_merge(self):
        self.assertRedirectsOnGet("inventory:stock_type_merge", pk=self.vodka.pk)

    def test_stock_type_movements(self):
        self.assertPageOK("inventory:stock_type_movements", pk=self.vodka.pk)

    def test_stock_type_price_history(self):
        self.assertPageOK("inventory:stock_type_price_history", pk=self.vodka.pk)

    def test_review_queue(self):
        """Now the Produits page's side panel: the page asks for it alone."""
        self.assertRedirectsOnGet("inventory:review_queue")
        response = self.client.get(reverse("inventory:review_queue"), HTTP_HX_REQUEST="true")
        self.assertContains(response, "RHUM INCONNU")
        assertNoUnrenderedTemplateSyntax(self, response, "the review panel")

    def test_assign_product(self):
        self.assertRedirectsOnGet("inventory:assign_product", product_id=self.unassigned.pk)

    def test_edit_product_conversion(self):
        self.assertRedirectsOnGet("inventory:edit_product_conversion", product_id=self.product.pk)

    def test_search_stock_types(self):
        self.assertEqual(self.client.get(reverse("inventory:search_stock_types"), {"q": "vod"}).status_code, 200)

    def test_export_associations(self):
        self.assertPageOK("inventory:export_associations")

    def test_import_associations(self):
        self.assertPageOK("inventory:import_associations")

    def test_stock_take_list(self):
        self.assertPageOK("inventory:stock_take_list")

    def test_stock_take_create(self):
        self.assertPageOK("inventory:stock_take_create")

    def test_stock_take_detail(self):
        self.assertPageOK("inventory:stock_take_detail", pk=self.stock_take.pk)

    def test_stock_take_update(self):
        self.assertPageOK("inventory:stock_take_update", pk=self.stock_take.pk)

    def test_stock_take_variance(self):
        """The first count has no previous one to compare against - the page
        has to say so rather than fall over."""
        self.assertPageOK("inventory:stock_take_variance", pk=self.stock_take.pk)

    # --- invoices --------------------------------------------------------
    def test_invoice_list(self):
        self.assertPageOK("invoices:invoice_list")

    def test_invoice_detail(self):
        self.assertContains(self.assertPageOK("invoices:invoice_detail", pk=self.invoice.pk), "SOBIESKI")

    def test_invoice_upload(self):
        self.assertPageOK("invoices:invoice_upload")

    def test_invoice_create_manual(self):
        self.assertPageOK("invoices:invoice_create_manual")

    def test_invoice_type_list(self):
        self.assertContains(self.assertPageOK("invoices:invoice_type_list"), "Metro - Factures")

    def test_invoice_type_create(self):
        self.assertPageOK("invoices:invoice_type_create")

    def test_invoice_type_update(self):
        self.assertPageOK("invoices:invoice_type_update", pk=self.invoice_type.pk)

    def test_receipt_upload(self):
        self.assertContains(self.assertPageOK("invoices:receipt_upload"), "Un dossier entier")

    def test_receipt_batch(self):
        response = self.assertPageOK("invoices:receipt_batch", pk=self.batch.pk)
        for name in ("ok.pdf", "dup.pdf", "x.pdf", "Thumbs.db"):
            self.assertContains(response, name)

    def test_receipt_batch_status(self):
        self.assertPageOK("invoices:receipt_batch_status", pk=self.batch.pk)

    def test_receipt_batch_resume_is_post_only(self):
        self.assertRedirectsOnGet("invoices:receipt_batch_resume", pk=self.batch.pk)

    def test_receipt_batch_assign_is_post_only(self):
        self.assertRedirectsOnGet("invoices:receipt_batch_assign", pk=self.batch.pk, index=2)

    def test_receipt_queue(self):
        self.assertContains(self.assertPageOK("invoices:receipt_queue"), "Sabbh Oriental")

    def test_invoice_delete_confirmation(self):
        self.assertContains(self.assertPageOK("invoices:invoice_delete", pk=self.invoice.pk), "Supprimer")

    def test_receipt_review(self):
        response = self.assertPageOK("invoices:receipt_review", pk=self.receipt.pk)
        self.assertContains(response, "Somme des lignes")
        self.assertContains(response, "Citron vert")

    def test_invoice_edit_lines(self):
        """The same page as a ticket's, in its supplier-invoice form."""
        response = self.assertPageOK("invoices:invoice_edit_lines", pk=self.invoice.pk)
        self.assertContains(response, "SOBIESKI")
        self.assertContains(response, "Enregistrer les lignes")

    # --- till (L'Addition) ------------------------------------------------
    def test_pos_product_list(self):
        self.assertPageOK("recipes:pos_product_list")

    def test_sales_import(self):
        self.assertPageOK("recipes:sales_import")

    def test_pos_product_assign_is_post_only(self):
        from recipes.models import PosProduct

        product = PosProduct.objects.create(name="Pinte Blonde", total_quantity=5)
        self.assertRedirectsOnGet("recipes:pos_product_assign", pk=product.pk)

    # --- recipes ---------------------------------------------------------
    def test_recipe_list(self):
        self.assertContains(self.assertPageOK("recipes:recipe_list"), "Moscow Mule")

    def test_recipe_create(self):
        self.assertPageOK("recipes:recipe_create")

    def test_recipe_detail(self):
        self.assertPageOK("recipes:recipe_detail", pk=self.recipe.pk)

    def test_recipe_detail_with_variations(self):
        response = self.assertPageOK("recipes:recipe_detail", pk=self.variant_recipe.pk)
        self.assertContains(response, "variation-select")

    def test_recipe_update(self):
        self.assertPageOK("recipes:recipe_update", pk=self.recipe.pk)

    def test_recipe_delete(self):
        self.assertRedirectsOnGet("recipes:recipe_delete", pk=self.recipe.pk)


class EmptyDatabasePageSmokeTests(TestCase):
    """Every list page on a brand-new install. A "no rows yet" page that
    divides by a total, or unpacks a None range, 500s here and nowhere else.
    """

    def assertPageOK(self, name, **kwargs):
        response = self.client.get(reverse(name, kwargs=kwargs))
        self.assertEqual(response.status_code, 200, f"{name} returned {response.status_code}")

    def test_stock_list(self):
        self.assertPageOK("inventory:stock_list")

    def test_review_queue(self):
        response = self.client.get(reverse("inventory:review_queue"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tout est classé")

    def test_stock_take_list(self):
        self.assertPageOK("inventory:stock_take_list")

    def test_stock_take_create(self):
        self.assertPageOK("inventory:stock_take_create")

    def test_invoice_list(self):
        self.assertPageOK("invoices:invoice_list")

    def test_invoice_type_list(self):
        self.assertPageOK("invoices:invoice_type_list")

    def test_invoice_create_manual(self):
        self.assertPageOK("invoices:invoice_create_manual")

    def test_receipt_upload(self):
        self.assertPageOK("invoices:receipt_upload")

    def test_receipt_queue(self):
        self.assertPageOK("invoices:receipt_queue")

    def test_recipe_list(self):
        self.assertPageOK("recipes:recipe_list")

    def test_recipe_create(self):
        self.assertPageOK("recipes:recipe_create")

    def test_pos_product_list(self):
        self.assertPageOK("recipes:pos_product_list")

    def test_sales_import(self):
        self.assertPageOK("recipes:sales_import")


class RecipeEdgeCaseRenderingTests(TestCase):
    """Recipes whose numbers can't be computed - the pages still have to
    render rather than 500 on a division or a None."""

    def test_a_recipe_with_no_ingredients_at_all(self):
        recipe = make_recipe(name="Vide")
        self.assertEqual(self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)

    def test_a_recipe_whose_ingredients_cost_nothing(self):
        """No stock movements means no cost, so the price factor
        (price / cost) has no value - it must render as "-", not crash."""
        recipe = make_recipe(name="Gratuit")
        make_ingredient(recipe, stock_type=make_priced_stock_type(unit_cost_ht="0", quantity="0"), quantity="1")
        self.assertEqual(self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("recipes:recipe_list")).status_code, 200)

    def test_a_recipe_with_no_happy_hour_price(self):
        recipe = make_recipe(name="Sans happy hour", happy_hour_price_ttc=None)
        make_ingredient(recipe, stock_type=make_priced_stock_type(unit_cost_ht="10", quantity="1"), quantity="1")
        self.assertEqual(self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": recipe.pk})).status_code, 200)

    def test_a_sub_recipe_used_as_an_ingredient(self):
        syrup = make_recipe(name="Sirop maison", yield_quantity="2")
        make_ingredient(syrup, stock_type=make_priced_stock_type(unit_cost_ht="4", quantity="10"), quantity="1")
        cocktail = make_recipe(name="Cocktail au sirop")
        make_ingredient(cocktail, sub_recipe=syrup, quantity="0.05")

        response = self.client.get(reverse("recipes:recipe_detail", kwargs={"pk": cocktail.pk}))
        self.assertEqual(response.status_code, 200)
        # 4/unit, 1 unit per batch, yielding 2 => 2.00 per yield unit.
        self.assertEqual(syrup.unit_cost_ht(), Decimal("2"))
