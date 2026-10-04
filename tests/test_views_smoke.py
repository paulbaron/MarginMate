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

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import members
from accounts.access import DEFAULT_AREAS
from accounts.models import Membership, Tenant
from bank.models import BankTransaction, OperationRule, StatementFormat, TreasuryAdjustment, TreasuryCheckpoint
from inventory.models import GapExclusion, GapFillEntry, GapFillSetting, StockMovement, StockType, UnitChoices
from invoices.models import Invoice, ReceiptBatch, ShopItemPrice
from recipes.models import PosProduct, PosProductDailyQuantity, Recipe, RecipeSale
from staff.tests.signing_support import SigningTestMixin
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_invoice_type,
    make_movement,
    make_priced_stock_type,
    make_product,
    make_purchase_history,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from tests.runner import TEST_EMAIL, TEST_TENANT_NAME, TEST_TENANT_PK, employee_of_the_test_tenant
from tests.support import NoNetworkTestCase

#: What « Combler les écarts » displays the till button of the red wine as.
GAP_TILL_NAME = "Rouge bouteille exemple"
#: What that button charged since the count - not the recipe's 36,00 €.
GAP_TILL_PRICE = "en caisse 35.00 €"


def make_gaps_to_fill():
    """« Combler les écarts » with something to propose, every kind of row
    its gaps table draws: a count ten days ago, deliveries since and recipes
    sold since - a gap to fill (red), an article bought since and never
    counted (rosé), and one sold past everything it had (amber), whose
    recipe is held back - and a tablecloth bought, which no recipe pours.
    The red's till button rings 35,00 €, not its recipe's 36,00 €. Dated
    from today, the page's own end of window. Invented data, prices above
    30 €. Returns the count.

    Red: 6 L counted + 12 bought - 1,5 sold = 16,5 L, 1,8 L allowed as loss:
    14,7 L to fill, a bottle at 36 €. Rosé: 6 bought - 0,75 sold, 0,6
    allowed: 4,65 L, a bottle at 34 €. Amber: 1 L counted, 3 sold: -2 L.
    70 € is exactly one bottle of each."""
    today = timezone.localdate()
    take = make_stock_take(
        taken_at=timezone.make_aware(datetime.combine(today - timedelta(days=10), time(12, 0))),
        note="Comptage exemple",
    )
    red = make_stock_type(name="Rouge exemple", unit=UnitChoices.LITRE)
    rose = make_stock_type(name="Rosé exemple", unit=UnitChoices.LITRE)
    amber = make_stock_type(name="Ambrée exemple", unit=UnitChoices.LITRE)
    make_stock_take_line(stock_take=take, stock_type=red, counted_quantity="6", unit=UnitChoices.LITRE)
    make_stock_take_line(stock_take=take, stock_type=amber, counted_quantity="1", unit=UnitChoices.LITRE)
    for article, litres, cost in ((red, "12", "9"), (rose, "6", "8")):
        make_movement(stock_type=article, quantity=litres, unit_cost_ht=cost, occurred_on=today - timedelta(days=5))
    recipes = {}
    for name, article, litres, price, sold in (
        ("Bouteille de rouge exemple", red, "0.75", "36.00", 2),
        ("Bouteille de rosé exemple", rose, "0.75", "34.00", 1),
        ("Pichet ambrée exemple", amber, "1", "32.00", 3),
    ):
        recipe = make_recipe(name=name, selling_price_ttc=price)
        make_ingredient(recipe, stock_type=article, quantity=litres)
        RecipeSale.objects.create(recipe=recipe, sold_on=today - timedelta(days=3), quantity=sold, source="caisse")
        recipes[name] = recipe
    button = PosProduct.objects.create(
        name=GAP_TILL_NAME, recipe=recipes["Bouteille de rouge exemple"], total_quantity=2
    )
    PosProductDailyQuantity.objects.create(
        product=button,
        sold_on=today - timedelta(days=3),
        quantity=2,
        revenue_ttc=Decimal("70.00"),
        revenue_read=True,
    )
    cloth = make_stock_type(name="Nappe exemple", unit=UnitChoices.UNIT)
    make_movement(stock_type=cloth, quantity="10", unit_cost_ht="2", occurred_on=today - timedelta(days=5))
    return take


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
        cls.receipt_product = make_product(supplier=cls.receipt_supplier, raw_name="Article divers (0.70 EUR/u)")
        make_invoice_line(
            invoice=cls.receipt,
            product=cls.receipt_product,
            quantity=3,
            total_ht="1.99",
            unit_cost_ht="0.6633",
            vat_rate=Decimal("0.055"),
            raw_name="Article divers (0.70 EUR/u)",
        )
        ShopItemPrice.objects.create(supplier=cls.receipt_supplier, unit_price_ttc=Decimal("0.70"), label="Citron vert")
        # A finished folder import with every outcome the batch page draws.
        cls.batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {
                    "name": "ok.pdf",
                    "status": "ok",
                    "invoice_id": cls.receipt.pk,
                    "shop": "Sabbh Oriental",
                    "total": "2.10",
                    "date": "14/07/2026",
                    "verified": False,
                },
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
        cls.pos_unlinked = PosProduct.objects.create(name="Planche apéro", category="Planches", typology="Solide")
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
            stock_take=cls.stock_take,
            product=cls.product,
            counted_quantity="4",
            unit=UnitChoices.UNIT,
            value_ht="60.00",
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

    def test_credentials_pages(self):
        import time

        from accounts import sudo
        from tests.runner import test_user

        self.assertPageOK("accounts:confirm_password")
        self.client.force_login(test_user())
        session = self.client.session
        session[sudo.SESSION_KEY] = {"user": test_user().pk, "until": time.time() + 600}
        session.save()
        self.assertPageOK("accounts:credentials")

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

    def test_stock_gap_filler(self):
        """« Combler les écarts »: from the latest count (today's, nothing
        since: no row), then from an older one, without a list and with one
        - two amounts POSTed, each answered with a redirect: the last
        entry's rows, the earlier one folded, the gaps' rows with the list's
        columns, the fixture's own recipes listed as not sold since, its
        unlinked till product warned about, the sales imported three days
        ago said to be behind the purchases, the tablecloth no recipe pours
        counted, the till's other price said under its button. Then the last
        entry taken back and the list cleared, each a redirect too."""
        url = reverse("inventory:stock_gap_filler")
        self.assertPageOK("inventory:stock_gap_filler")
        take = make_gaps_to_fill()
        page = f"{url}?depuis={take.pk}"

        def drawn(label):
            response = self.client.get(url, {"depuis": take.pk})
            self.assertEqual(response.status_code, 200)
            assertNoUnrenderedTemplateSyntax(self, response, f"{page} {label}")
            self.assertContains(response, 'data-table-label="écarts"')
            self.assertContains(response, "Ambrée exemple")
            self.assertContains(response, "les ventes pas encore importées comptent comme écart")
            self.assertContains(response, "1 autre article acheté n'est dans aucune recette")
            return response

        response = drawn("without a list")
        self.assertNotContains(response, 'data-table-label="recettes à encaisser"')
        # 70 € is a bottle of each wine; 36 € on top of it, the red again.
        for amount in ("70", "36"):
            response = self.client.post(
                reverse("inventory:stock_gap_filler_add"), {"depuis": take.pk, "montant": amount}
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], f"{page}#a-encaisser")
        response = drawn("with a list")
        self.assertContains(response, 'data-table-label="recettes à encaisser"')
        self.assertContains(response, 'data-table-label="montants déjà saisis"')
        self.assertContains(response, '<th class="num">Comblé</th>')
        self.assertContains(response, GAP_TILL_NAME)
        self.assertContains(response, GAP_TILL_PRICE)
        self.assertEqual(GapFillEntry.objects.filter(stock_take=take).count(), 2)
        for name in ("inventory:stock_gap_filler_undo", "inventory:stock_gap_filler_clear"):
            with self.subTest(action=name):
                response = self.client.post(reverse(name), {"depuis": take.pk})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], page)
        self.assertFalse(GapFillEntry.objects.exists())
        for name in (
            "inventory:stock_gap_filler_add",
            "inventory:stock_gap_filler_undo",
            "inventory:stock_gap_filler_clear",
        ):
            self.assertRedirectsOnGet(name)
        # The count list and each count lead there.
        self.assertContains(self.assertPageOK("inventory:stock_take_list"), url)
        self.assertContains(self.assertPageOK("inventory:stock_take_detail", pk=take.pk), f"{url}?depuis={take.pk}")

    def test_stock_gap_filler_recent_sales(self):
        """« Recettes vendues il y a moins de … »: a duration chosen, then
        the recipes sold since the count again - each POST a redirect to the
        count's page, drawn whole, saying which recipes it proposes. A GET to the route goes to the
        page and writes nothing; a duration refused is a message."""
        url = reverse("inventory:stock_gap_filler")
        recent = reverse("inventory:stock_gap_filler_recent")
        take = make_gaps_to_fill()
        page = f"{url}?depuis={take.pk}"
        self.assertRedirectsOnGet("inventory:stock_gap_filler_recent")
        self.assertFalse(GapFillSetting.objects.exists())
        for data, months in (
            ({"duree": "3", "unite": "mois"}, 3),
            ({"duree": "2", "unite": "ans"}, 24),
            ({"duree": "abc", "unite": "mois"}, 24),
            ({"depuis_inventaire": "1"}, None),
        ):
            with self.subTest(data=data):
                response = self.client.post(recent, {"depuis": take.pk, **data})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], page)
                self.assertEqual(GapFillSetting.current().sold_within_months, months)
                response = self.client.get(url, {"depuis": take.pk})
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, f"{page} after {data}")
                self.assertContains(response, 'id="recettes-vendues"')
                self.assertContains(response, "Proposées : les recettes vendues")

    def test_stock_gap_filler_exclusions(self):
        """« Exclus des écarts »: an article left out from its row, a
        category and the articles with none from the fold - each POST a
        redirect to where it was asked -, the page drawn whole with all three
        listed, then each taken back, a redirect too. A GET to either route
        goes to the page and changes nothing."""
        url = reverse("inventory:stock_gap_filler")
        exclude = reverse("inventory:stock_gap_filler_exclude")
        include = reverse("inventory:stock_gap_filler_include")
        take = make_gaps_to_fill()
        page = f"{url}?depuis={take.pk}"
        StockType.objects.filter(name="Ambrée exemple").update(category="Bières exemple")
        red = StockType.objects.get(name="Rouge exemple")
        for data, landing in (
            ({"article": red.pk}, f"{page}#ecarts"),
            ({"categorie": "Bières exemple"}, f"{page}#exclusions"),
            ({"categorie": ""}, f"{page}#exclusions"),
        ):
            with self.subTest(data=data):
                response = self.client.post(exclude, {"depuis": take.pk, **data})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], landing)
        self.assertEqual(GapExclusion.objects.count(), 3)
        response = self.client.get(url, {"depuis": take.pk})
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"{page} with exclusions")
        self.assertContains(response, '<details class="explainer" id="exclusions" open>')
        self.assertContains(response, '<ul class="exclusion-list">')
        self.assertContains(response, "Rouge exemple")
        self.assertContains(response, "Bières exemple")
        self.assertContains(response, "Catégorie non renseignée")
        # Every gap left out: no row, the page whole all the same.
        self.assertNotContains(response, 'data-table-label="écarts"')
        for exclusion in GapExclusion.objects.all():
            with self.subTest(exclusion=str(exclusion)):
                for name in ("inventory:stock_gap_filler_exclude", "inventory:stock_gap_filler_include"):
                    self.assertRedirectsOnGet(name)
                response = self.client.post(include, {"depuis": take.pk, "exclusion": exclusion.pk})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"{page}#exclusions")
        self.assertFalse(GapExclusion.objects.exists())
        # The page the « Réinclure » land on: nothing left out, the fold
        # drawn open all the same for the three messages it holds.
        response = self.client.get(url, {"depuis": take.pk})
        assertNoUnrenderedTemplateSyntax(self, response, f"{page} with nothing excluded and its messages")
        self.assertContains(response, '<details class="explainer" id="exclusions" open>')
        self.assertContains(response, "Rien d'exclu : tous les articles comptent.")
        self.assertContains(response, '<li class="message message-', count=3)
        self.assertContains(response, 'data-table-label="écarts"')
        # Said once: the next visit draws it shut, with no message.
        response = self.client.get(url, {"depuis": take.pk})
        assertNoUnrenderedTemplateSyntax(self, response, f"{page} with nothing excluded")
        self.assertContains(response, '<details class="explainer" id="exclusions">')
        self.assertNotContains(response, '<li class="message')
        self.assertContains(response, 'data-table-label="écarts"')

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
            reverse("invoices:supplier_edit", args=[shop.pk]),
            {"name": "Epicerie Deux", "header": "", "action": "verifier"},
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

    def test_invoice_add(self):
        """« Ajouter des factures »: its form, then the import this login sent
        drawn above it (`?lot=`) and listed under « Vos derniers envois ». The
        fixture's import was sent by no login (made before `sent_by`): it is
        shown to nobody there, asked for or not."""
        url = reverse("invoices:invoice_add")
        response = self.assertPageOK("invoices:invoice_add")
        self.assertContains(response, "Prendre une photo")
        self.assertNotContains(response, "Vos derniers envois")
        sent = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS, results=self.batch.results, sent_by=TEST_EMAIL
        )
        for lot, shown in ((sent.pk, True), (self.batch.pk, False), ("abc", False)):
            with self.subTest(lot=lot):
                response = self.client.get(url, {"lot": lot})
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, f"{url}?lot={lot}")
                self.assertContains(response, "Vos derniers envois")
                if shown:
                    self.assertContains(response, "dup.pdf")
                else:
                    self.assertNotContains(response, "dup.pdf")
        # Asked for none, the page shows this login's latest import while it
        # still runs: the one a phone just sent.
        ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.RUNNING,
            results=[{"name": "photo-en-cours.jpg", "status": "pending"}],
            sent_by=TEST_EMAIL,
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"{url} with an import running")
        self.assertContains(response, "photo-en-cours.jpg")

    def test_invoice_add_for_an_employee_who_may_only_add(self):
        """The same page for an employee given « Ajouter des factures » alone
        (accounts/access.py): his import is drawn without the ways to check
        its documents or file them under a shop - the owner's - and says who
        checks them."""
        employee = employee_of_the_test_tenant("serveur-smoke@example.invalid", ("invoices_add",), name="Léo Exemple")
        sent = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS, results=self.batch.results, sent_by=employee.get_username()
        )
        self.client.force_login(employee)
        url = reverse("invoices:invoice_add")
        response = self.client.get(url, {"lot": sent.pk})
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"{url}?lot={sent.pk} for an employee")
        self.assertContains(response, "dup.pdf")
        self.assertContains(response, "Déjà envoyé")
        self.assertContains(response, "Terminé : votre employeur vérifiera ces documents.")
        self.assertNotContains(response, reverse("invoices:receipt_review", args=[self.receipt.pk]))
        self.assertNotContains(response, "Toutes les factures")

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

    def test_till_formats(self):
        from recipes.models import TillFormat

        empty = self.assertPageOK("recipes:till_formats")
        self.assertContains(empty, "Formats des fichiers de caisse")
        fmt = TillFormat.objects.create(
            name="Caisse Exemple", day_column="Date", product_column="Article", quantity_column="Qté"
        )
        listed = self.assertPageOK("recipes:till_formats")
        self.assertContains(listed, "Caisse Exemple")
        self.assertContains(listed, "non lue")
        self.assertContains(self.assertPageOK("recipes:till_format", pk=fmt.pk), "Format « Caisse Exemple »")

    # --- margins ---------------------------------------------------------
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

    def test_stock_gap_filler(self):
        """No count yet: the gaps run from one, so the page says so and
        offers to make it - whatever the address carries."""
        url = reverse("inventory:stock_gap_filler")
        for query in ({}, {"depuis": "1", "montant": "50"}, {"montant": "abc"}):
            with self.subTest(query=query):
                response = self.client.get(url, query)
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, f"{url} {query}")
                self.assertContains(response, "empty-state")
                self.assertContains(response, "Aucun inventaire")
                self.assertContains(response, reverse("inventory:stock_take_create"))
        # The list's actions, with no count to keep a list, and the
        # exclusions' with no article nor category to leave out: a redirect
        # to the page, POSTed or not - never a 500.
        for name in (
            "inventory:stock_gap_filler_add",
            "inventory:stock_gap_filler_undo",
            "inventory:stock_gap_filler_clear",
            "inventory:stock_gap_filler_exclude",
            "inventory:stock_gap_filler_include",
            "inventory:stock_gap_filler_recent",
        ):
            with self.subTest(action=name):
                for response in (
                    self.client.post(reverse(name), {"depuis": "1", "montant": "50", "article": "1", "exclusion": "1"}),
                    self.client.post(reverse(name), {"depuis": "1", "categorie": ""}),
                    self.client.get(reverse(name)),
                ):
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"], url)
        self.assertFalse(GapExclusion.objects.exists())
        for data, said in (
            ({"article": "1"}, "Article introuvable : rien n&#x27;a été exclu."),
            ({"categorie": ""}, "Catégorie introuvable : rien n&#x27;a été exclu."),
        ):
            with self.subTest(data=data):
                response = self.client.post(
                    reverse("inventory:stock_gap_filler_exclude"), {"depuis": "1", **data}, follow=True
                )
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, f"an exclusion with no count {data}")
                self.assertContains(response, said)
                self.assertContains(response, "empty-state")
        response = self.client.post(
            reverse("inventory:stock_gap_filler_include"), {"depuis": "1", "exclusion": "1"}, follow=True
        )
        self.assertContains(response, "Cette exclusion n&#x27;existe plus : rien n&#x27;a changé.")
        response = self.client.post(
            reverse("inventory:stock_gap_filler_add"), {"depuis": "1", "montant": "50"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "an amount with no count")
        self.assertContains(response, "Inventaire introuvable.")
        self.assertContains(response, "empty-state")
        self.assertFalse(GapFillEntry.objects.exists())

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

    def test_invoice_add(self):
        self.assertPageOK("invoices:invoice_add")

    def test_receipt_queue(self):
        self.assertPageOK("invoices:receipt_queue")

    def test_employee_access(self):
        """No employee yet: the page says so and offers the first invitation."""
        response = self.client.get(reverse("accounts:members"))
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "« Accès des employés » with nobody")
        self.assertContains(response, "Aucun employé n'a encore d'accès")
        self.assertContains(response, "Inviter un employé")

    def test_returnables(self):
        """With the seeds migration 0002 puts in every database, then
        without them (« Données » cleared them)."""
        from returnables.tests.support import no_defaults

        self.assertPageOK("returnables:home")
        no_defaults()
        self.assertPageOK("returnables:home")
        self.assertPageOK("returnables:format_list")
        self.assertPageOK("returnables:type_list")

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
        response = self.client.post(
            reverse("margins:count_articles"), {"categorie": "", "action": "cocher"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "les marges sans article")
        self.assertEqual(self.client.get(reverse("margins:margins_home"), {"du": "2026-03-01"}).status_code, 200)

    def test_bank_pages(self):
        """« Dépenses » divides every share by what left the account, and on
        a new install nothing has."""
        for name in (
            "bank:bank_home",
            "bank:spending_home",
            "bank:income_home",
            "bank:treasury",
            "bank:rule_list",
            "bank:proposals",
            "bank:recognition",
            "bank:recognition_reapply",
            "bank:statement_formats",
        ):
            with self.subTest(page=name):
                self.assertPageOK(name)

    def test_the_recognition_pages(self):
        """A seeded rule's own page, and the list with no rule at all - an
        espace whose « Données » were cleared. A rule that is not there is a
        404."""
        self.assertPageOK("bank:recognition_rule", pk=OperationRule.objects.first().pk)
        OperationRule.objects.all().delete()
        for name in ("bank:recognition", "bank:recognition_reapply", "bank:income_home"):
            with self.subTest(page=name):
                self.assertPageOK(name)
        self.assertEqual(self.client.get(reverse("bank:recognition_rule", args=[999999])).status_code, 404)

    def test_the_statement_format_pages(self):
        """The seeded format's own page, and the list and Banque with no
        format at all - an espace whose « Données » were cleared, where no
        statement imports. A format that is not there is a 404."""
        url = reverse("bank:statement_format", args=[StatementFormat.objects.first().pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, url)
        StatementFormat.objects.all().delete()
        for name in ("bank:statement_formats", "bank:bank_home"):
            with self.subTest(page=name):
                self.assertPageOK(name)
                assertNoUnrenderedTemplateSyntax(self, self.client.get(reverse(name)), name)
        self.assertEqual(self.client.get(reverse("bank:statement_format", args=[999999])).status_code, 404)

    def test_the_invoice_files(self):
        """The zip of the period's invoices goes back to Banque when nothing
        was paid; a document's file 404s when it has none, or is not there."""
        self.assertEqual(self.client.get(reverse("bank:invoice_zip")).status_code, 302)
        self.assertEqual(self.client.get(reverse("bank:invoice_zip"), {"du": "2026-02-30"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("invoices:invoice_file", args=[999999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("invoices:invoice_file", args=[make_invoice().pk])).status_code, 404)

    def test_the_invoice_search_fragment(self):
        """A fragment, so `assertPageOK`'s whole-page checks do not apply -
        but it answers on a line that exists and 404s on one that does not,
        and it is a GET a stale page can repeat."""
        line = BankTransaction.objects.create(
            operation_date=date(2026, 3, 2),
            amount=Decimal("-12.00"),
            label="PRLV EXEMPLE",
            fingerprint="smoke-invoice-search",
        )
        url = reverse("bank:invoice_search", args=[line.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url, {"recherche": "rien"}).status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, self.client.get(url, {"recherche": "rien"}), "la recherche de factures")
        self.assertEqual(self.client.get(reverse("bank:invoice_search", args=[999999])).status_code, 404)


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
    """The pages read through « du … au … » (common.date_range), under
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
        "bank:spending_home",
        "bank:income_home",
        "bank:treasury",
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
        make_invoice_line(invoice=make_invoice(supplier=supplier, invoice_date=None), product=product, total_ht="30.00")

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
        # One balance typed, so « Trésorerie » draws its curve and its months
        # under every window rather than its « saisissez le solde » - and,
        # once the lines are deleted below, a point with no operation.
        TreasuryCheckpoint.objects.create(date=date(2026, 2, 14), balance=Decimal("1234.56"))

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
            "bank:spending_home": "tout=1",
            "bank:income_home": "tout=1",
            "bank:treasury": "tout=1",
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


class StockGapFillerParameterSmokeTests(TestCase):
    """« Combler les écarts » under every `depuis` an address can carry and
    every `montant` a form can POST - a stale bookmark, a hand-typed amount,
    a number Python's Decimal would read and the page must not (Infinity,
    NaN, 1e999), a count that was deleted. The page always a 200, its
    template rendered whole; the list's actions always one redirect to it,
    which says what was done with the amount - kept only when something was
    proposed for it. Invented data."""

    #: montant -> what the page says about it ("" : nothing, the amount kept).
    AMOUNTS = {
        "50": "",
        "70": "",
        "1 234,50": "",
        "10000": "",
        "10 000": "",
        "1 500,00": "",
        "150,50": "",
        # The field is required: a blank one POSTed all the same is no amount.
        "": "Montant illisible",
        "abc": "Montant illisible",
        # One separator and three figures: ten thousand, or ten euros?
        "10.000": "Montant illisible",
        "1,500": "Montant illisible",
        "2.000": "Montant illisible",
        "-5": "Le montant doit être supérieur à zéro.",
        "0": "Le montant doit être supérieur à zéro.",
        "Infinity": "Montant illisible",
        "NaN": "Montant illisible",
        "1e999": "Montant illisible",
        "1" * 41: "Montant illisible",
        "12,345": "Montant illisible",
        "10000,01": "10\N{NO-BREAK SPACE}000 € au plus.",
        "9999999999.99": "10\N{NO-BREAK SPACE}000 € au plus.",
        '"><i>montant</i>': "Montant illisible",
    }
    #: depuis -> whether the page says the count was not found.
    SINCE = {"": False, "999999": True, "abc": True, "\N{SUPERSCRIPT TWO}": True, "-1": True, "1" * 30: True}
    MESSAGES = (
        "Montant illisible",
        "Le montant doit être supérieur à zéro.",
        "10000 € au plus.",
    )
    ACTIONS = (
        "inventory:stock_gap_filler_add",
        "inventory:stock_gap_filler_undo",
        "inventory:stock_gap_filler_clear",
    )

    @classmethod
    def setUpTestData(cls):
        cls.take = make_gaps_to_fill()
        cls.url = reverse("inventory:stock_gap_filler")

    def get(self, query):
        response = self.client.get(self.url, query)
        self.assertEqual(response.status_code, 200, f"{query} returned {response.status_code}")
        assertNoUnrenderedTemplateSyntax(self, response, f"{self.url} {query}")
        return response

    def add(self, data):
        """POST an amount to the list and follow the answer: one redirect,
        then the page, rendered whole."""
        response = self.client.post(reverse("inventory:stock_gap_filler_add"), data, follow=True)
        self.assertEqual(response.status_code, 200, f"{data} ended on {response.status_code}")
        self.assertEqual([status for _url, status in response.redirect_chain], [302], data)
        assertNoUnrenderedTemplateSyntax(self, response, f"the page after {data}")
        return response

    def test_every_amount(self):
        for typed, said in self.AMOUNTS.items():
            with self.subTest(montant=typed):
                GapFillEntry.objects.all().delete()
                content = self.add({"depuis": self.take.pk, "montant": typed}).content.decode()
                for message in self.MESSAGES:
                    if message == said:
                        self.assertIn(message, content)
                    else:
                        self.assertNotIn(message, content)
                # Never echoed back: a POST is not markup either.
                self.assertNotIn("<i>montant</i>", content)
                kept = GapFillEntry.objects.filter(stock_take=self.take).exists()
                self.assertEqual(kept, not said, "a readable amount is kept, and only it")
                planned = 'data-table-label="recettes à encaisser"' in content
                self.assertEqual(planned, kept, "the list is drawn once it holds the amount")

    def test_an_amount_in_the_address_is_no_amount(self):
        """A bookmark of the page from before the list, ?montant= in its
        address: whatever it holds, nothing is planned, said or kept."""
        for typed in self.AMOUNTS:
            with self.subTest(montant=typed):
                content = self.get({"depuis": self.take.pk, "montant": typed}).content.decode()
                for message in self.MESSAGES:
                    self.assertNotIn(message, content)
                self.assertNotIn("<i>montant</i>", content)
                self.assertNotIn('data-table-label="recettes à encaisser"', content)
        self.assertFalse(GapFillEntry.objects.exists())

    def test_every_count(self):
        for asked, missing in self.SINCE.items():
            for typed in ("", "70", "abc"):
                with self.subTest(depuis=asked, montant=typed):
                    content = self.get({"depuis": asked, "montant": typed}).content.decode()
                    self.assertEqual("Inventaire introuvable" in content, missing)

    def test_every_count_an_amount_is_posted_for(self):
        """Only a count the page knows takes an amount: any other, an empty
        one included, is « Inventaire introuvable. » and keeps nothing."""
        for asked in [*self.SINCE, str(self.take.pk)]:
            with self.subTest(depuis=asked):
                GapFillEntry.objects.all().delete()
                content = self.add({"depuis": asked, "montant": "70"}).content.decode()
                known = asked == str(self.take.pk)
                self.assertEqual("Inventaire introuvable." in content, not known)
                self.assertEqual(GapFillEntry.objects.exists(), known)

    def test_the_list_s_actions_answer_with_a_redirect(self):
        """POSTed, to the count's page; asked for by GET - a link, a
        bookmark - to the page, and nothing done."""
        page = f"{self.url}?depuis={self.take.pk}"
        for name, landing in zip(self.ACTIONS, (f"{page}#a-encaisser", page, page), strict=True):
            with self.subTest(action=name):
                response = self.client.post(reverse(name), {"depuis": self.take.pk, "montant": "70"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], landing)
                for query in ({}, {"depuis": self.take.pk, "montant": "70"}):
                    response = self.client.get(reverse(name), query)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"], self.url)
        # Added, undone, cleared: nothing left, and no GET added anything.
        self.assertFalse(GapFillEntry.objects.exists())

    def test_undo_and_clear_under_every_count(self):
        self.add({"depuis": self.take.pk, "montant": "70"})
        for name in self.ACTIONS[1:]:
            for asked in self.SINCE:
                with self.subTest(action=name, depuis=asked):
                    response = self.client.post(reverse(name), {"depuis": asked})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"], self.url)
                    self.assertEqual(GapFillEntry.objects.count(), 1)

    def test_the_page_with_a_list_under_every_count(self):
        """A list of two amounts, read back whatever `depuis` holds: an
        unknown count falls back on the latest, which is the list's."""
        for amount in ("70", "36"):
            self.add({"depuis": self.take.pk, "montant": amount})
        for asked in self.SINCE:
            with self.subTest(depuis=asked):
                content = self.get({"depuis": asked, "montant": "abc"}).content.decode()
                self.assertIn('data-table-label="recettes à encaisser"', content)
                self.assertIn('data-table-label="montants déjà saisis"', content)
                self.assertIn(GAP_TILL_NAME, content)

    def test_the_parameters_alone(self):
        """No `depuis` at all: the latest count, which is this one - and its
        list."""
        response = self.get({"montant": "70"})
        self.assertNotContains(response, "Inventaire introuvable")
        self.assertContains(response, f'<option value="{self.take.pk}" selected>')
        self.assertNotContains(response, GAP_TILL_NAME)
        self.add({"depuis": self.take.pk, "montant": "70"})
        self.assertContains(self.get({}), GAP_TILL_NAME)

    #: What « Exclure » may carry as its article and find no article by.
    ARTICLES = ("abc", "\N{SUPERSCRIPT TWO}", "-1", "1.5", " ", "999999", "1" * 30, '"><i>article</i>')
    #: What « Exclure la catégorie » may carry and find no article filed
    #: under (every article of the fixture has none: "" is a category).
    CATEGORIES = ("Inconnue exemple", "c" * 300, '"><i>catégorie</i>', "\x00", " ")
    #: What « Réinclure » may carry and find no exclusion by.
    EXCLUSIONS = ("", "abc", "\N{SUPERSCRIPT TWO}", "-1", "999999", "1" * 30, '"><i>exclusion</i>')

    def act(self, name, data) -> str:
        """POST to an exclusion route and follow the answer: one redirect,
        then the page, rendered whole."""
        response = self.client.post(reverse(name), data, follow=True)
        self.assertEqual(response.status_code, 200, f"{name} {data} ended on {response.status_code}")
        self.assertEqual([status for _url, status in response.redirect_chain], [302], data)
        assertNoUnrenderedTemplateSyntax(self, response, f"the page after {name} {data}")
        content = response.content.decode()
        # Never echoed back: a POST is not markup.
        for markup in ("<i>article</i>", "<i>catégorie</i>", "<i>exclusion</i>"):
            self.assertNotIn(markup, content)
        return content

    def test_every_article_category_and_exclusion_that_cannot_be_read(self):
        """Under every `depuis`, the known count's included: a message, the
        page, nothing left out - never a 500."""
        exclude = "inventory:stock_gap_filler_exclude"
        include = "inventory:stock_gap_filler_include"
        for asked in [*self.SINCE, str(self.take.pk)]:
            for article in self.ARTICLES:
                with self.subTest(depuis=asked, article=article):
                    content = self.act(exclude, {"depuis": asked, "article": article})
                    self.assertIn("Article introuvable : rien n&#x27;a été exclu.", content)
            for category in self.CATEGORIES:
                with self.subTest(depuis=asked, categorie=category):
                    content = self.act(exclude, {"depuis": asked, "categorie": category})
                    self.assertIn("Catégorie introuvable : rien n&#x27;a été exclu.", content)
            for exclusion in self.EXCLUSIONS:
                with self.subTest(depuis=asked, exclusion=exclusion):
                    content = self.act(include, {"depuis": asked, "exclusion": exclusion})
                    self.assertIn("Cette exclusion n&#x27;existe plus : rien n&#x27;a changé.", content)
            self.assertFalse(GapExclusion.objects.exists())

    def test_the_exclusion_routes_answer_with_a_redirect(self):
        """POSTed, to the count's page where it was asked - the gaps for an
        article, the fold for a category and a « Réinclure » -, or to the
        page for a count it does not know; a GET to the page, and nothing
        done."""
        page = f"{self.url}?depuis={self.take.pk}"
        red = StockType.objects.get(name="Rouge exemple")
        exclude = reverse("inventory:stock_gap_filler_exclude")
        include = reverse("inventory:stock_gap_filler_include")
        for asked, landing in ((str(self.take.pk), page), ("999999", self.url), ("abc", self.url)):
            with self.subTest(depuis=asked):
                GapExclusion.objects.all().delete()
                anchor = "" if landing == self.url else "#ecarts"
                response = self.client.post(exclude, {"depuis": asked, "article": red.pk})
                self.assertEqual((response.status_code, response["Location"]), (302, f"{landing}{anchor}"))
                anchor = "" if landing == self.url else "#exclusions"
                response = self.client.post(exclude, {"depuis": asked, "categorie": ""})
                self.assertEqual((response.status_code, response["Location"]), (302, f"{landing}{anchor}"))
                self.assertEqual(GapExclusion.objects.count(), 2)
                for exclusion in GapExclusion.objects.all():
                    for query in ({}, {"depuis": self.take.pk, "exclusion": exclusion.pk, "article": red.pk}):
                        for name in (exclude, include):
                            response = self.client.get(name, query)
                            self.assertEqual((response.status_code, response["Location"]), (302, self.url))
                    self.assertTrue(GapExclusion.objects.filter(pk=exclusion.pk).exists())
                    response = self.client.post(include, {"depuis": asked, "exclusion": exclusion.pk})
                    self.assertEqual((response.status_code, response["Location"]), (302, f"{landing}{anchor}"))
                self.assertFalse(GapExclusion.objects.exists())

    def test_a_count_deleted_since(self):
        """A bookmark naming a count since deleted reads as not found, and
        the page falls back on the latest, its list drawn; an amount POSTed
        for the deleted count is not kept."""
        other = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        pk = other.pk
        self.add({"depuis": self.take.pk, "montant": "70"})
        other.delete()
        response = self.get({"depuis": pk, "montant": "70"})
        self.assertContains(response, "Inventaire introuvable")
        self.assertContains(response, GAP_TILL_NAME)
        content = self.add({"depuis": pk, "montant": "70"}).content.decode()
        self.assertIn("Inventaire introuvable.", content)
        self.assertEqual(GapFillEntry.objects.count(), 1)


class IncomeSmokeTests(TestCase):
    """« Entrées d'argent » with everything « En caisse » puts on it: a payout
    printing its gross, a terminal's transfer counted as card through its
    payer retained (no gross printed), a cash deposit, a party's deposit said
    to be an « Avoir » on its own, a transfer nobody named - and its two
    POST-only actions. Every payer and amount INVENTED."""

    JUNE = {"du": "2026-06-01", "au": "2026-06-30"}

    @classmethod
    def setUpTestData(cls):
        from bank import income
        from bank.models import IncomePayer
        from recipes.models import PosDailyPayment

        for day, card in ((date(2026, 6, 1), "120.00"), (date(2026, 6, 2), "80.00")):
            PosDailyPayment.objects.create(sold_on=day, method=PosDailyPayment.CARD, amount=Decimal(card), payments=3)
        PosDailyPayment.objects.create(
            sold_on=date(2026, 6, 2), method=PosDailyPayment.CREDIT, amount=Decimal("150.00"), payments=1
        )

        def credit(number, day, amount, label, counterparty="", bank_type="VIREMENT", income_source=""):
            return BankTransaction.objects.create(
                operation_date=day,
                bank_type=bank_type,
                label=label,
                counterparty=counterparty,
                amount=Decimal(amount),
                kind=BankTransaction.Kind.TRANSFER,
                income_source=income_source,
                fingerprint=f"smoke-entree-{number}",
            )

        cls.payout = credit(
            1, date(2026, 6, 3), "198.60", "VIR SEPA RECU /FRM PRESTATAIRE EXEMPLE TOTAL ENCAISSE 200.00 EUROS"
        )
        cls.terminal = credit(
            2, date(2026, 6, 4), "79.50", "VIR SEPA RECU /FRM TERMINAL EXEMPLE REMISE", "TERMINAL EXEMPLE"
        )
        credit(3, date(2026, 6, 5), "40.00", "VERSEMENT ESPECES", bank_type="VERSEMENT ESPECES")
        credit(4, date(2026, 6, 6), "300.00", "VIR SEPA RECU /FRM ASSOCIATION EXEMPLE", income_source=income.CREDIT)
        credit(5, date(2026, 6, 7), "25.00", "VIR SEPA RECU /FRM CLIENT EXEMPLE", "CLIENT EXEMPLE")
        cls.payer = IncomePayer.objects.create(key=income.payer_key(cls.terminal), source=income.CARD)

    def assertPageOK(self, url, params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, f"{url} {params} returned {response.status_code}")
        assertNoUnrenderedTemplateSyntax(self, response, f"{url} {params}")
        return response

    def test_the_page_over_every_period_and_with_a_payout_s_menu_open(self):
        url = reverse("bank:income_home")
        response = self.assertPageOK(url, self.JUNE)
        for text in (
            "Payeurs retenus",
            "Oublier",
            "Dépôts et autres moyens de paiement",
            "commission inconnue",
            "Acompte (avoir en caisse)",
            "payeur retenu",
            "choisi pour cette entrée",
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)
        for params in (
            {},
            {"tout": "1"},
            {**self.JUNE, "changer": str(self.terminal.pk)},
            {**self.JUNE, "changer": str(self.payout.pk)},
            {**self.JUNE, "changer": "abc"},
        ):
            with self.subTest(params=params):
                self.assertPageOK(url, params)

    def test_banque_s_entries_tab(self):
        response = self.assertPageOK(reverse("bank:bank_home"), {"vue": "entrees", **self.JUNE})
        self.assertContains(response, "brut non imprimé, commission inconnue")
        self.assertContains(response, "payeur retenu")

    def test_the_post_only_actions_redirect_on_get(self):
        """A GET on one goes back to the page and writes nothing - still worth
        hitting, since a broken one raises before it gets to the redirect."""
        from bank.models import IncomePayer

        def written():
            return (
                list(BankTransaction.objects.order_by("pk").values_list("pk", "income_source")),
                list(IncomePayer.objects.values_list("key", "source")),
            )

        before = written()
        for name, pk in (("bank:income_source", self.terminal.pk), ("bank:income_payer_forget", self.payer.pk)):
            with self.subTest(name=name):
                url = reverse(name, kwargs={"pk": pk})
                self.assertEqual(self.client.get(url, {"en_caisse": "other", "retenir": "1"}).status_code, 302)
        self.assertEqual(written(), before)


class TreasurySmokeTests(TestCase):
    """« Trésorerie » with everything it draws: two balances the operations
    explain and one they do not (a card, its adjustment form and its hints),
    one read before its day's operations (« Dater ce point du … »), a
    negative balance, a balance typed today past the statement (pending,
    provisional), an adjustment counted and one counting nowhere - and its
    POST-only routes. Every amount INVENTED."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        for number, (day, amount) in enumerate(
            (
                (date(2026, 6, 2), "-120.00"),
                (date(2026, 6, 9), "450.00"),
                (date(2026, 6, 20), "-80.00"),
                (date(2026, 7, 1), "-40.00"),
            )
        ):
            BankTransaction.objects.create(
                operation_date=day,
                label=f"VIR EXEMPLE {number}",
                amount=Decimal(amount),
                fingerprint=f"smoke-treso-{number}",
            )
        # 01/06 -500,00 (an overdraft); 10/06: -500 - 120 + 450 = -170,00,
        # they agree; 25/06: -170 - 80 = -250,00 expected, -200,00 typed: 50
        # more, 20,00 of it adjusted - to resolve; 01/07: -200,00 typed, read
        # before the 1st's -40 (« Dater ce point du 30/06 »); today, past the
        # statement: pending.
        cls.points = [
            TreasuryCheckpoint.objects.create(date=day, balance=Decimal(balance))
            for day, balance in (
                (date(2026, 6, 1), "-500.00"),
                (date(2026, 6, 10), "-170.00"),
                (date(2026, 6, 25), "-200.00"),
                (date(2026, 7, 1), "-200.00"),
                (today, "1000.00"),
            )
        ]
        cls.adjustment = TreasuryAdjustment.objects.create(date=date(2026, 6, 25), amount=Decimal("20.00"))
        TreasuryAdjustment.objects.create(date=date(2026, 5, 1), amount=Decimal("-3.00"), reason="Hors de deux points")

    def assertPageOK(self, params):
        response = self.client.get(reverse("bank:treasury"), params)
        self.assertEqual(response.status_code, 200, f"{params} returned {response.status_code}")
        assertNoUnrenderedTemplateSyntax(self, response, f"Trésorerie {params}")
        return response

    def test_the_page_draws_every_section(self):
        response = self.assertPageOK({"tout": "1"})
        for text in (
            "Trésorerie au",
            "Écarts à résoudre",
            "Dater ce point du 30/06",
            "Ajouter un ajustement de",
            "Soldes saisis",
            "relevé à importer",
            "ne compte pas",
            'data-chart="line"',
            'data-table-label="mois"',
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)
        for params in ({}, {"du": "2026-06-01", "au": "2026-06-30"}, {"date": "2026-06-25"}, {"date": "abc"}):
            with self.subTest(params=params):
                self.assertPageOK(params)

    def test_the_post_only_actions_redirect_on_get(self):
        """A GET on one goes back to the page and writes nothing."""

        def written():
            return (
                list(TreasuryCheckpoint.objects.order_by("pk").values_list("pk", "date", "balance")),
                list(TreasuryAdjustment.objects.order_by("pk").values_list("pk", "date", "amount")),
            )

        before = written()
        for name, kwargs in (
            ("bank:treasury_adjustment_add", {}),
            ("bank:treasury_point", {"pk": self.points[3].pk}),
            ("bank:treasury_adjustment", {"pk": self.adjustment.pk}),
        ):
            with self.subTest(name=name):
                url = reverse(name, kwargs=kwargs)
                query = {"action": "supprimer", "avant": self.points[1].pk, "apres": self.points[2].pk}
                self.assertEqual(self.client.get(url, query).status_code, 302)
        self.assertEqual(written(), before)


class StaffPageSmokeTests(TestCase):
    """« Personnel »: the employees, an employee, a month saved and one
    not, its PDF. Every name and address is INVENTED - the repository is
    public and a timesheet is personal data."""

    SAVED = date(2026, 5, 1)  # four public holidays: the holidays button is drawn
    UNSAVED = date(2026, 6, 1)

    @classmethod
    def setUpTestData(cls):
        from staff.models import Employee, Establishment
        from staff.timesheet import apply_range

        Establishment.objects.create(
            pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS"
        )
        cls.person = Employee.objects.create(
            last_name="Dupont", first_name="Jeanne", tuesday_hours=Decimal("7"), wednesday_hours=Decimal("8")
        )
        Employee.objects.create(last_name="Martin", first_name="Paul", friday_hours=Decimal("6"), is_active=False)
        # Saved, with an absence and a note on it.
        apply_range(cls.person, cls.SAVED, date(2026, 5, 5), date(2026, 5, 6), "conges", note="demande écrite")

    def assertPageOK(self, name, **kwargs):
        url = reverse(name, kwargs=kwargs)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{name} ({url}) returned {response.status_code}")
        assertNoUnrenderedTemplateSyntax(self, response, url)
        return response

    def test_home(self):
        self.assertContains(self.assertPageOK("staff:home"), "DUPONT Jeanne")

    def test_employee(self):
        self.assertPageOK("staff:employee", pk=self.person.pk)

    def test_month_saved_and_unsaved(self):
        for month in (self.SAVED, self.UNSAVED):
            with self.subTest(month=month):
                self.assertPageOK("staff:month", pk=self.person.pk, month=month)

    def test_month_pdf(self):
        for month in (self.SAVED, self.UNSAVED):
            with self.subTest(month=month):
                response = self.client.get(reverse("staff:month_pdf", kwargs={"pk": self.person.pk, "month": month}))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertTrue(response.content.startswith(b"%PDF-"))
                self.assertTrue(response["Content-Disposition"].startswith("attachment; "))

    def test_the_post_only_actions_redirect_on_get(self):
        """A GET on one goes back to its page and writes nothing - still worth
        hitting, since a broken one raises before it gets to the redirect."""
        month = {"pk": self.person.pk, "month": self.UNSAVED}
        for name, kwargs in (
            ("staff:employee_active", {"pk": self.person.pk}),
            ("staff:open_month", {"pk": self.person.pk}),
            ("staff:month_range", month),
            ("staff:month_holidays_off", month),
            ("staff:month_reset", month),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, kwargs=kwargs)).status_code, 302)

    def test_an_empty_install(self):
        from staff.models import Employee, Establishment, Timesheet

        Timesheet.objects.all().delete()
        Employee.objects.all().delete()
        Establishment.objects.all().delete()
        self.assertPageOK("staff:home")


class EmployeeAccessSmokeTests(TestCase):
    """« Accès des employés » (accounts/members.py) with an employee in each
    state its cards draw - invited, his link expired, active - and the two
    pages an employee meets: « Votre accès », public, where his link lets him
    choose a password, and « Aucune page ouverte ». Every name and address
    INVENTED: the repository is public."""

    @classmethod
    def setUpTestData(cls):
        tenant = Tenant.objects.get(pk=TEST_TENANT_PK)
        cls.invited, cls.token = members.invite(
            tenant, name="Jeanne Exemple", email="jeanne-exemple@example.invalid", pages=DEFAULT_AREAS
        )
        # Invited long enough ago for his link to have expired unused.
        _, cls.expired_token = members.invite(
            tenant,
            name="Marc Exemple",
            email="marc-exemple@example.invalid",
            pages=(),
            now=timezone.now() - timedelta(days=members.INVITATION_DAYS + 1),
        )
        cls.active = employee_of_the_test_tenant(
            "paul-exemple@example.invalid",
            ("returnables", "stock_takes"),
            name="Paul Exemple",
            password="mot-de-passe-essai-42",
        )

    def invitation(self, token) -> str:
        return reverse("accounts:member_invitation", args=[token])

    def test_the_owner_s_page(self):
        url = reverse("accounts:members")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, url)
        for said in (
            "Jeanne Exemple",
            "Invitation en attente",
            "Marc Exemple",
            "Invitation expirée",
            "Paul Exemple",
            "Actif",
            "Inviter un employé",
        ):
            with self.subTest(said=said):
                self.assertContains(response, said)

    def test_the_owner_s_page_showing_a_new_link(self):
        """A link is drawn in the answer to the POST that made it, and
        nowhere else: no GET ever renders that part of the page."""
        url = reverse("accounts:members")
        active = Membership.objects.get(user=self.active)
        for action, data, said in (
            (
                "invite",
                {"name": "Léa Exemple", "email": "lea-exemple@example.invalid", "pages": ["invoices_add"]},
                "Lien d'invitation de Léa Exemple",
            ),
            ("renew", {"member": active.pk}, "Lien pour le nouveau mot de passe de Paul Exemple"),
        ):
            with self.subTest(action=action):
                response = self.client.post(url, {"action": action, **data})
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, f"« Accès des employés » after {action}")
                self.assertContains(response, said)
                self.assertContains(response, "/invitation/")

    def test_no_access_sends_whoever_has_a_page_to_it(self):
        """The owner, and an employee given pages, never stay on « Aucune
        page ouverte »: they go to their first page (Access.home_url)."""
        url = reverse("accounts:no_access")
        self.assertRedirects(self.client.get(url), reverse("inventory:stock_list"), fetch_redirect_response=False)
        self.client.force_login(self.active)
        self.assertRedirects(self.client.get(url), reverse("inventory:stock_take_list"), fetch_redirect_response=False)

    def test_no_access_for_an_employee_given_no_page(self):
        nobody = employee_of_the_test_tenant("sans-page@example.invalid", (), name="Lucie Exemple")
        self.client.force_login(nobody)
        url = reverse("accounts:no_access")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, url)
        self.assertContains(response, "Aucune page ouverte")
        self.assertContains(response, TEST_TENANT_NAME)

    def test_the_invitation_page(self):
        """Opened by nobody logged in - the employee, on his phone - and by a
        browser logged in as somebody else, which it warns."""
        url = self.invitation(self.token)
        response = Client().get(url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "« Votre accès »")
        self.assertContains(response, f"Votre accès à « {TEST_TENANT_NAME} »")
        self.assertContains(response, "jeanne-exemple@example.invalid")
        self.assertContains(response, "Créer mon compte")
        self.assertNotContains(response, "Ce navigateur est connecté avec un autre compte")
        self.assertIn("noindex", response["X-Robots-Tag"])
        # The owner's own browser.
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "« Votre accès » in another login's browser")
        self.assertContains(response, "Ce navigateur est connecté avec un autre compte")

    def test_the_invitation_page_for_a_new_password(self):
        """« Nouveau mot de passe… » on an active employee: the same page, in
        the words of a password changed."""
        token = members.renew(Membership.objects.get(user=self.active))
        response = Client().get(self.invitation(token))
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "« Votre accès » for a new password")
        self.assertContains(response, "choisissez votre nouveau mot de passe")
        self.assertContains(response, "Enregistrer mon mot de passe")

    def test_a_link_that_opens_nothing(self):
        """Unknown or expired: one 404 page, with the way to log in."""
        for token in ("jeton-inconnu", self.expired_token):
            with self.subTest(token=token[:6]):
                response = Client().get(self.invitation(token))
                self.assertEqual(response.status_code, 404)
                assertNoUnrenderedTemplateSyntax(self, response, "« Votre accès » with a dead link")
                self.assertContains(response, "Lien expiré ou déjà utilisé", status_code=404)
                self.assertContains(response, reverse("accounts:login"), status_code=404)
                self.assertIn("noindex", response["X-Robots-Tag"])


class ReturnablesPageSmokeTests(TestCase):
    """« Consignes »: a pickup with photos compared with its slip, one waiting
    for its slip, a slip replaced by another, a slip with a line no type
    recognises. Every value INVENTED (returnables/tests/support.py): the
    owner's real tickets carry his account and his deliveries."""

    @classmethod
    def setUpTestData(cls):
        from returnables.tests.support import CO2_LINE, DELIVERY_DAY, KEG_LINE, make_pickup, make_slip

        cls.pickup = make_pickup(
            date=DELIVERY_DAY, counts={"Fûts": 3, "Bouteilles CO2": 1}, photos=2, note="Un fût cabossé"
        )
        cls.waiting = make_pickup(date=date(2026, 2, 20))
        cls.original = make_slip(lines=(KEG_LINE, CO2_LINE), references=["900001"])
        cls.replacement = make_slip(lines=(KEG_LINE,), references=["900001"], replaces=True)
        cls.unknown = make_slip(
            delivery_date=date(2026, 2, 12), lines=(("PALETTE EXEMPLE", 1, Decimal("12.0000"), Decimal("12.00")),)
        )
        cls.line = cls.unknown.lines.get()
        cls.photo = cls.pickup.photos.first()

    def assertPageOK(self, name, **kwargs):
        url = reverse(name, kwargs=kwargs)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{name} ({url}) returned {response.status_code}")
        assertNoUnrenderedTemplateSyntax(self, response, url)
        return response

    def test_home(self):
        response = self.assertPageOK("returnables:home")
        self.assertContains(response, "Dernière reprise")
        self.assertContains(response, "Fûts 3")

    def test_home_showing_everything(self):
        for which in ("reprises", "bons"):
            with self.subTest(tout=which):
                response = self.client.get(reverse("returnables:home") + f"?tout={which}")
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, which)

    def test_a_pickup(self):
        for pickup in (self.pickup, self.waiting):
            with self.subTest(pickup=pickup.pk):
                self.assertPageOK("returnables:pickup_detail", pk=pickup.pk)

    def test_a_slip(self):
        for slip in (self.original, self.replacement, self.unknown):
            with self.subTest(slip=slip.pk):
                self.assertPageOK("returnables:slip_detail", pk=slip.pk)

    def test_the_settings(self):
        from returnables.tests.support import seeded_format

        fmt = seeded_format()
        self.assertPageOK("returnables:format_list")
        self.assertPageOK("returnables:format_create")
        self.assertPageOK("returnables:format_edit", pk=fmt.pk)
        response = self.client.get(reverse("returnables:format_create") + f"?depuis={fmt.pk}")
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "?depuis=")
        self.assertPageOK("returnables:type_list")

    def test_the_post_only_actions_redirect_on_get(self):
        """A GET on one goes back to a page and writes nothing - still worth
        hitting, since a broken one raises before it gets to the redirect."""
        from returnables.models import Pickup, PickupPhoto, ReturnableType, Slip, SlipFormat
        from returnables.tests.support import seeded_format, seeded_type

        before = (
            Pickup.objects.count(),
            PickupPhoto.objects.count(),
            Slip.objects.count(),
            SlipFormat.objects.count(),
            ReturnableType.objects.count(),
        )
        for name, kwargs in (
            ("returnables:pickup_delete", {"pk": self.pickup.pk}),
            ("returnables:pickup_date", {"pk": self.pickup.pk}),
            ("returnables:photo_delete", {"pk": self.photo.pk}),
            ("returnables:slip_upload", {}),
            ("returnables:slip_delete", {"pk": self.original.pk}),
            ("returnables:slip_reread", {"pk": self.original.pk}),
            ("returnables:line_classify", {"pk": self.line.pk}),
            ("returnables:format_delete", {"pk": seeded_format().pk}),
            ("returnables:format_reread", {"pk": seeded_format().pk}),
            ("returnables:type_edit", {"pk": seeded_type("Fûts").pk}),
            ("returnables:type_delete", {"pk": seeded_type("Fûts").pk}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, kwargs=kwargs)).status_code, 302)
        after = (
            Pickup.objects.count(),
            PickupPhoto.objects.count(),
            Slip.objects.count(),
            SlipFormat.objects.count(),
            ReturnableType.objects.count(),
        )
        self.assertEqual(after, before)

    def test_an_empty_install_with_its_seeds(self):
        """Migration 0002 seeds the three types and UBA's format in every
        database: « empty » is those and nothing else."""
        from returnables.models import Pickup, Slip

        Pickup.objects.all().delete()
        Slip.objects.all().delete()
        for name in ("returnables:home", "returnables:format_list", "returnables:type_list"):
            with self.subTest(page=name):
                self.assertPageOK(name)

    def test_an_empty_install_without_its_seeds(self):
        """After « Données » cleared them: no type, no format."""
        from returnables.models import Pickup, Slip
        from returnables.tests.support import no_defaults

        Pickup.objects.all().delete()
        Slip.objects.all().delete()
        no_defaults()
        response = self.assertPageOK("returnables:home")
        self.assertContains(response, "Aucune reprise enregistrée")
        self.assertContains(self.assertPageOK("returnables:format_list"), "Aucun format de bon")
        self.assertContains(self.assertPageOK("returnables:type_list"), "Aucun type de consigne")
        self.assertPageOK("returnables:format_create")


class StaffSignatureSmokeTests(SigningTestMixin, NoNetworkTestCase):
    """« Personnel »'s monthly signature: the month's page with a request in
    every state, the owner's files, the employee's pages (the public ones,
    /personnel/signer/…), and every POST-only action answering a GET. Keys,
    files and timestamps offline and in a temp folder; names INVENTED."""

    PENDING = date(2026, 6, 1)
    SIGNED = date(2026, 5, 1)
    COMPLETE = date(2026, 4, 1)
    CANCELLED = date(2026, 3, 1)

    def setUp(self):
        super().setUp()
        from staff import signature_requests as workflow
        from staff.models import Employee, Establishment
        from staff.tests.signing_support import drawn_signature, employer_signature
        from staff.timesheet import save_month

        Establishment.objects.create(
            pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS"
        )
        self.person = Employee.objects.create(
            last_name="Dupont", first_name="Jeanne", tuesday_hours=Decimal("7"), wednesday_hours=Decimal("8")
        )
        self.tokens = {}
        for month in (self.PENDING, self.SIGNED, self.COMPLETE, self.CANCELLED):
            save_month(self.person, month, [])
            request, self.tokens[month] = workflow.create_request(self.person, month)
            if month in (self.SIGNED, self.COMPLETE):
                session = {}
                workflow.check_code(request, workflow.issue_code(request, "code_remis"), session)
                request = workflow.sign_for_employee(
                    request, drawn_signature(), session=session, statement_accepted=True, reservation="Le 7."
                )
            if month == self.COMPLETE:
                workflow.countersign_request(request, employer_signature())
            if month == self.CANCELLED:
                workflow.cancel_request(request, "envoyé par erreur")

    def assertPageOK(self, url, status=200):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, url)
        if response["Content-Type"].startswith("text/html"):
            assertNoUnrenderedTemplateSyntax(self, response, url)
        return response

    def test_the_month_s_page_in_every_state(self):
        for month in (self.PENDING, self.SIGNED, self.COMPLETE, self.CANCELLED):
            with self.subTest(month=month):
                self.assertPageOK(reverse("staff:month", args=[self.person.pk, month]))

    def test_the_owner_s_files(self):
        for month, files in (
            (self.PENDING, ("document", "preuve")),
            (self.SIGNED, ("document", "signe-salarie", "dessin", "preuve")),
            (self.COMPLETE, ("document", "signe-salarie", "signe", "dessin", "dessin-employeur", "preuve")),
            (self.CANCELLED, ("document", "preuve")),
        ):
            for file in files:
                with self.subTest(month=month, file=file):
                    self.assertPageOK(reverse("staff:signature_file", args=[self.person.pk, month, 1, file]))

    def test_the_employee_s_pages(self):
        for month in (self.PENDING, self.SIGNED, self.COMPLETE):
            token = self.tokens[month]
            with self.subTest(month=month):
                self.assertPageOK(reverse("staff:sign", args=[token]))
                self.assertPageOK(reverse("staff:sign_document", args=[token]))
                self.assertPageOK(reverse("staff:sign_copy", args=[token]), 404 if month == self.PENDING else 200)
        self.assertPageOK(reverse("staff:sign", args=[self.tokens[self.CANCELLED]]), 410)
        self.assertPageOK(reverse("staff:sign", args=["inconnu"]), 404)
        self.assertPageOK("/personnel/signer/", 404)

    def test_the_post_only_actions_redirect_on_get(self):
        month = {"pk": self.person.pk, "month": self.PENDING}
        version = {**month, "version": 1}
        token = {"token": self.tokens[self.PENDING]}
        for name, kwargs in (
            ("staff:signature_send", month),
            ("staff:month_reopen", month),
            ("staff:signature_link", version),
            ("staff:signature_code", version),
            ("staff:signature_countersign", version),
            ("staff:signature_cancel", version),
            ("staff:signature_verify", version),
            ("staff:sign_send_code", token),
            ("staff:sign_check_code", token),
            ("staff:sign_submit", token),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, kwargs=kwargs)).status_code, 302)


class AccountsPagesSmokeTests(TestCase):
    """The accounts' pages (what they do: accounts/tests), as a visitor
    nobody logged in reaches them: the login and the signup answer, the
    logout is a POST."""

    def setUp(self):
        # Django's own client: anonymous (the suite's logs in on its own).
        self.client = Client()

    def test_the_login(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "accounts:login")

    def test_the_signup(self):
        response = self.client.get(reverse("accounts:signup"))
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "accounts:signup")

    def test_the_logout_is_post_only(self):
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.assertRedirects(
            self.client.post(reverse("accounts:logout")), reverse("accounts:login"), fetch_redirect_response=False
        )
