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

from bank.models import BankTransaction
from inventory.models import StockMovement, StockType, UnitChoices
from invoices.models import Invoice, ReceiptBatch, ShopItemPrice
from recipes.models import PosProduct, PosProductDailyQuantity, Recipe, RecipeSale
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

        # The till, as the L'Addition import writes it: one product with a
        # recipe behind it and one without. The margins page reads both -
        # a costed category and an uncosted one - and an empty till would
        # render neither branch of its tables.
        cls.pos_linked = PosProduct.objects.create(
            name="Moscow Mule", recipe=cls.recipe, category="Cocktails", typology="Liquide (Alcool)"
        )
        cls.pos_unlinked = PosProduct.objects.create(
            name="Planche apéro", category="Planches", typology="Solide"
        )
        for product, ttc, ht in ((cls.pos_linked, "85.00", "70.83"), (cls.pos_unlinked, "36.00", "34.12")):
            PosProductDailyQuantity.objects.create(
                product=product,
                sold_on=date(2026, 3, 15),
                quantity=10,
                revenue_ttc=Decimal(ttc),
                revenue_ht=Decimal(ht),
                revenue_read=True,
            )

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
        # Moved to « Données »: the old address opens its Exporter tab.
        self.assertRedirectsOnGet("inventory:export_associations")

    def test_import_associations(self):
        self.assertRedirectsOnGet("inventory:import_associations")

    # --- transfer (« Données ») ---------------------------------------------
    def test_data_pages(self):
        for name in ("transfer:data_home", "transfer:data_import", "transfer:data_clear"):
            with self.subTest(page=name):
                self.assertPageOK(name)

    def test_data_post_only_actions(self):
        self.assertRedirectsOnGet("transfer:data_export")
        self.assertRedirectsOnGet("transfer:data_import_backup")

    def test_data_unknown_stage_redirects(self):
        self.assertRedirectsOnGet("transfer:data_import_stage", token="A" * 22)

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

    def test_invoice_preview(self):
        """A document's lines, opened under its row on the Achats page."""
        self.assertContains(self.assertPageOK("invoices:invoice_preview", pk=self.invoice.pk), "SOBIESKI")
        self.assertContains(self.assertPageOK("invoices:invoice_preview", pk=self.receipt.pk), "écart +0.49 €")

    def test_invoice_upload(self):
        self.assertPageOK("invoices:invoice_upload")

    def test_invoice_create_manual(self):
        self.assertPageOK("invoices:invoice_create_manual")

    def test_supplier_pages(self):
        """A supplier's page and what is done from it - created, modified,
        its nature switched, deleted."""
        shop = make_supplier(code="EPICERIE_SMOKE", name="Epicerie", parser_key="", ticket_header="EPICERIE EXEMPLE")
        make_invoice(supplier=shop, ocr_text="EPICERIE EXEMPLE\nTOTAL 3,00")
        self.assertContains(self.assertPageOK("invoices:supplier_detail", pk=shop.pk), "Reconnaissance")
        self.assertPageOK("invoices:supplier_create")
        self.assertPageOK("invoices:supplier_edit", pk=shop.pk)
        self.assertPageOK("invoices:supplier_delete", pk=shop.pk)
        self.assertPageOK("invoices:supplier_expenses", pk=shop.pk)
        # Achats' « Enseignes et fournisseurs » tab (it redirected to the foot
        # of the Sources tab).
        self.assertContains(self.assertPageOK("invoices:supplier_list"), "Epicerie")
        checked = self.client.post(
            reverse("invoices:supplier_edit", args=[shop.pk]), {"name": "Epicerie Deux", "header": "", "action": "verifier"}
        )
        assertNoUnrenderedTemplateSyntax(self, checked, "la vérification d'une modification")

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

    # --- marges ----------------------------------------------------------
    def test_margins(self):
        """Over the dates the fixture sells on, so both branches of the
        tables render: a category with a recipe behind it and one without."""
        response = self.client.get(reverse("margins:margins_home"), {"du": "2026-03-01", "au": "2026-03-31"})
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "les marges sur une période")
        self.assertContains(response, "Marge réelle")
        self.assertContains(response, "Cocktails")
        self.assertContains(response, "Planches")

    def test_margins_on_its_other_periods(self):
        """The default twelve months, all of the history, and a date that is
        no date - a stale bookmark is a window, never a 500."""
        for params in ({}, {"tout": "1"}, {"du": "hier", "au": "2026-02-30"}):
            with self.subTest(params=params):
                response = self.client.get(reverse("margins:margins_home"), params)
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, f"les marges {params}")

    def test_count_articles_is_post_only(self):
        self.assertRedirectsOnGet("margins:count_articles")

    def test_count_articles_ticks_a_category_and_comes_back_to_the_page(self):
        """The fixture's articles have no category, and both are in a
        recipe: ticked, the page draws its « compté deux fois » branches."""
        back = f"{reverse('margins:margins_home')}?du=2026-03-01&au=2026-03-31#articles-comptes"
        response = self.client.post(
            reverse("margins:count_articles"), {"categorie": "", "action": "cocher", "next": back}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(back, 302)])
        assertNoUnrenderedTemplateSyntax(self, response, "les marges après « Tout cocher »")
        self.assertContains(response, "compté deux fois")

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

    def test_supplier_list(self):
        self.assertPageOK("invoices:supplier_list")

    def test_invoice_create_manual(self):
        self.assertPageOK("invoices:invoice_create_manual")

    def test_receipt_upload(self):
        self.assertPageOK("invoices:receipt_upload")

    def test_receipt_queue(self):
        self.assertPageOK("invoices:receipt_queue")

    def test_data_pages(self):
        for name in ("transfer:data_home", "transfer:data_import", "transfer:data_clear"):
            with self.subTest(page=name):
                self.assertPageOK(name)

    def test_recipe_list(self):
        self.assertPageOK("recipes:recipe_list")

    def test_recipe_create(self):
        self.assertPageOK("recipes:recipe_create")

    def test_pos_product_list(self):
        self.assertPageOK("recipes:pos_product_list")

    def test_sales_import(self):
        self.assertPageOK("recipes:sales_import")

    def test_margins(self):
        """Every percentage on that page divides by a revenue, and on a new
        install there is none."""
        self.assertPageOK("margins:margins_home")
        # No article at all: a post names a category nobody carries.
        response = self.client.post(reverse("margins:count_articles"), {"categorie": "", "action": "cocher"}, follow=True)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "les marges sans article")
        self.assertEqual(
            self.client.get(reverse("margins:margins_home"), {"du": "2026-03-01"}).status_code, 200
        )


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


class DateWindowSmokeTests(TestCase):
    """The four pages read through « du … au … » (common.date_range), under
    every window a query string can carry.

    These dates arrive from a URL, so the page has to survive whatever is in
    it: a stale bookmark, a hand-typed address, a browser with no date input,
    two dates the wrong way round, a window with nothing in it. Every one of
    those used to be a 500 waiting to happen, and the shallow GET here is
    what catches "page X now 500s under a window" - what each window actually
    FILTERS is each app's own test (inventory, invoices, recipes, bank).
    """

    WINDOWS = {
        "a real window": "du=2026-02-01&au=2026-02-28",
        "backwards": "du=2026-02-28&au=2026-02-01",
        "one end only": "au=2026-02-28",
        "the other end only": "du=2026-02-01",
        "empty parameters": "du=&au=",
        "not dates at all": "du=hier&au=demain",
        # Well-formed and no date: parse_date RAISES on this one.
        "an impossible day": "du=2026-02-30&au=2026-13-01",
        "a window holding nothing": "du=2030-01-01&au=2030-12-31",
        # A single day, and a day before the bar had anything at all.
        "one day": "du=2026-02-14&au=2026-02-14",
        "before everything": "du=1990-01-01&au=1990-12-31",
    }
    PAGES = (
        "inventory:stock_list",
        "inventory:stock_catalogue",
        "invoices:invoice_list",
        "recipes:sales_list",
        "bank:bank_home",
    )

    @classmethod
    def setUpTestData(cls):
        # Populated, never empty: an empty database is the one case that
        # accidentally works, since no rows means no loop body.
        supplier = make_supplier(code="GROSSISTE_W", name="Grossiste Exemple")
        stock_type = make_priced_stock_type(name="Vodka", unit=UnitChoices.LITRE, unit_cost_ht="14", quantity="5")
        product = make_product(
            supplier=supplier, raw_name="VODKA EXEMPLE 1L", stock_type=stock_type, stock_equivalent="1"
        )
        make_purchase_history(product, [("2026-01-10", 6, "60.00"), ("2026-02-10", 6, "90.00")])
        # One document inside a February window, one outside it, and one with
        # no date at all - which is in no window, and must not take the page
        # down when the window drops it.
        make_invoice_line(
            invoice=make_invoice(supplier=supplier, invoice_date=date(2026, 2, 14)), product=product, total_ht="30.00"
        )
        make_invoice_line(
            invoice=make_invoice(supplier=supplier, invoice_date=date(2026, 5, 2)), product=product, total_ht="30.00"
        )
        make_invoice_line(
            invoice=make_invoice(supplier=supplier, invoice_date=None), product=product, total_ht="30.00"
        )

        recipe = make_recipe(name="Moscow Mule", selling_price_ttc="8.50")
        make_ingredient(recipe, stock_type=stock_type, quantity="0.04")
        for day, quantity in ((date(2026, 2, 14), 3), (date(2026, 5, 2), 4)):
            RecipeSale.objects.create(recipe=recipe, sold_on=day, quantity=quantity, source="caisse")

        for number, (day, amount) in enumerate(
            ((date(2026, 2, 14), "-30.00"), (date(2026, 5, 2), "-30.00"), (date(2026, 2, 20), "410.00"))
        ):
            BankTransaction.objects.create(
                operation_date=day,
                label=f"PAIEMENT EXEMPLE {number}",
                counterparty="Grossiste Exemple",
                amount=Decimal(amount),
                fingerprint=f"fenetre-{number}",
            )

    def test_every_page_under_every_window(self):
        for page in self.PAGES:
            for name, query in self.WINDOWS.items():
                with self.subTest(page=page, window=name):
                    url = f"{reverse(page)}?{query}"
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
                    assertNoUnrenderedTemplateSyntax(self, response, url)

    def test_a_window_beside_what_each_page_already_filters_by(self):
        """The window composes: it never replaces the view, tab, search or
        filter the reader was already on."""
        beside = {
            "inventory:stock_list": "inventaire=999999",
            "invoices:invoice_list": "filtre=tickets&q=exemple",
            "recipes:sales_list": "vente=moscow&ventes=toutes",
            "bank:bank_home": "vue=toutes&mois=2026-02",
        }
        for page, query in beside.items():
            with self.subTest(page=page):
                url = f"{reverse(page)}?{query}&du=2026-02-01&au=2026-02-28"
                self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_pages_still_answer_with_no_window_at_all(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                self.assertEqual(self.client.get(reverse(page)).status_code, 200)

    def test_an_empty_database_under_a_window(self):
        """Nothing bought, nothing sold, no statement imported: the windowed
        pages are empty, not broken - the first thing a new install sees."""
        BankTransaction.objects.all().delete()
        Invoice.objects.all().delete()
        StockMovement.objects.all().delete()
        # Recipes first: an ingredient protects its stock type, and the sales
        # go with the recipe that has them.
        Recipe.objects.all().delete()
        StockType.objects.all().delete()
        for page in self.PAGES:
            with self.subTest(page=page):
                url = f"{reverse(page)}?du=2026-02-01&au=2026-02-28"
                self.assertEqual(self.client.get(url).status_code, 200, url)
