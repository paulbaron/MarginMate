"""« Faire un inventaire » for an employee (accounts/access.py, the owner's
choice of 02/10/2026): he counts the stock and corrects the counts, and is
shown what articles cost only when another box he was given already shows
it (`Access.sees_costs`) - an inventory's value is the bar's purchase
prices summed.

So for an employee given « Faire un inventaire » alone, no page prints a
value - the list, a count, the counting form (its live pricing asks nothing:
VALUE_URL is empty and the saved lines' values are not shipped) - and the
route pricing a line is refused by the gate: hiding the figure in a template
is never the boundary. Deleting a count stays the owner's: it froze the
stock's value at its date. « Écarts d'inventaire » is a box of its own:
what is missing since a count, in value.

Every name, amount and date is INVENTED.
"""

from datetime import date, datetime

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from accounts import access
from inventory.forms import product_display_name
from inventory.models import StockTake, StockTakeLineSource, UnitChoices
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from tests.runner import employee_of_the_test_tenant
from tests.test_json_islands import island

#: The count's value and the price it was taken from, as the pages print
#: them - distinctive enough that no other figure of the page reads the same.
VALUE = "41.16"
UNIT_COST = "10.2900"
INVOICE_DAY = "10/01/2026"


class StockTakeCase(TestCase):
    """One count of 31/03/2026: 4 bottles of an invented vodka bought at
    10,29 € HT each, so worth 41,16 € HT, traced to its invoice."""

    def setUp(self):
        super().setUp()
        supplier = make_supplier(name="Fournisseur Exemple")
        self.vodka = make_stock_type(name="Vodka exemple", unit=UnitChoices.LITRE)
        self.product = make_product(
            supplier=supplier,
            raw_name="VODKA EXEMPLE 70CL",
            stock_type=self.vodka,
            unit=UnitChoices.UNIT,
            stock_equivalent="0.7",
        )
        self.invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 1, 10))
        bought = make_invoice_line(invoice=self.invoice, product=self.product, quantity=12, total_ht="123.48")
        self.take = make_stock_take(taken_at=datetime(2026, 3, 31, 12, 0), note="Comptage de mars")
        line = make_stock_take_line(stock_take=self.take, product=self.product, counted_quantity="4", value_ht="41.16")
        StockTakeLineSource.objects.create(
            stock_take_line=line, invoice_line=bought, quantity_used="4", unit_cost_ht="10.29"
        )

    def log_in(self, *pages):
        """The client logged in as an employee opening `pages`."""
        self.client.force_login(employee_of_the_test_tenant("compteur@example.invalid", pages, name="Compteur"))

    def list_url(self):
        return reverse("inventory:stock_take_list")

    def detail_url(self):
        return reverse("inventory:stock_take_detail", args=[self.take.pk])

    def edit_url(self):
        return reverse("inventory:stock_take_update", args=[self.take.pk])

    def value_url(self):
        query = f"?entry={product_display_name(self.product)}&quantity=2&unit={UnitChoices.UNIT}&as_of=2026-03-31"
        return reverse("inventory:value_stock_take_line") + query

    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response

    def assertRefused(self, response, *, posted=False):
        """The gate's page (accounts/refused.html): no view ran."""
        self.assertContains(response, "Page non accessible", status_code=403)
        self.assertContains(response, escape(access.REFUSED), status_code=403)
        if posted:
            self.assertContains(response, escape(access.REFUSED_POST), status_code=403)

    def assertShowsNoValue(self, response):
        content = response.content.decode()
        for figure in (VALUE, UNIT_COST, "€"):
            with self.subTest(figure=figure):
                self.assertNotIn(figure, content)

    def payload(self):
        """A new count of 2 bottles, as the form posts it."""
        return {
            "taken_at": "2026-04-30 12:00:00",
            "note": "Comptage d'avril",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-entry_search": product_display_name(self.product),
            "lines-0-counted_quantity": "2",
            "lines-0-unit": UnitChoices.UNIT,
        }


class CountingOnlyTests(StockTakeCase):
    """Given « Faire un inventaire » and nothing showing a price."""

    def setUp(self):
        super().setUp()
        self.log_in("stock_takes")

    def test_the_list_shows_the_counts_without_their_value(self):
        response = self.page(self.list_url())
        self.assertContains(response, "Comptage de mars")
        self.assertContains(response, self.detail_url())
        self.assertContains(response, reverse("inventory:stock_take_create"))
        self.assertNotContains(response, "Valeur (HT)")
        self.assertNotContains(response, "Combler les écarts")
        self.assertNotContains(response, reverse("inventory:stock_gap_filler"))
        self.assertShowsNoValue(response)

    def test_a_count_shows_what_was_counted_and_nothing_it_is_worth(self):
        response = self.page(self.detail_url())
        self.assertContains(response, "VODKA EXEMPLE 70CL")
        self.assertContains(response, "Lignes comptées")
        self.assertContains(response, self.edit_url())
        for hidden in ("Valeur comptée", "Valeur (HT)", "Déduit de", f"facture du {INVOICE_DAY}"):
            with self.subTest(hidden=hidden):
                self.assertNotContains(response, hidden)
        self.assertShowsNoValue(response)

    def test_a_count_offers_neither_deleting_nor_the_gaps_nor_the_invoices(self):
        response = self.page(self.detail_url())
        self.assertNotContains(response, "Supprimer")
        self.assertNotContains(response, reverse("inventory:stock_take_delete", args=[self.take.pk]))
        self.assertNotContains(response, "Voir les écarts")
        self.assertNotContains(response, reverse("inventory:stock_take_variance", args=[self.take.pk]))
        self.assertNotContains(response, reverse("invoices:invoice_detail", args=[self.invoice.pk]))

    def test_the_counting_form_prices_nothing_and_ships_no_saved_value(self):
        for url in (self.edit_url(), reverse("inventory:stock_take_create")):
            with self.subTest(url=url):
                response = self.page(url)
                self.assertContains(response, 'var VALUE_URL = "";')
                self.assertNotContains(response, reverse("inventory:value_stock_take_line"))
                self.assertEqual(island(response.content.decode(), "saved-values"), {})
                self.assertNotContains(response, "Valeur de l'inventaire (HT)")
                self.assertShowsNoValue(response)
        # What he edits is still the count's own lines.
        self.assertEqual(self.page(self.edit_url()).context["formset"].forms[0].instance.stock_take, self.take)

    def test_pricing_a_line_is_refused(self):
        """The figure the form would print, asked straight from its address."""
        response = self.client.get(self.value_url())
        self.assertRefused(response)
        self.assertNotContains(response, "value_ht", status_code=403)

    def test_deleting_a_count_is_refused_and_it_stays(self):
        response = self.client.post(reverse("inventory:stock_take_delete", args=[self.take.pk]))
        self.assertRefused(response, posted=True)
        self.assertTrue(StockTake.objects.filter(pk=self.take.pk).exists())
        self.assertEqual(self.take.lines.count(), 1)

    def test_the_gaps_are_refused(self):
        for url in (
            reverse("inventory:stock_take_variance", args=[self.take.pk]),
            reverse("inventory:stock_gap_filler"),
        ):
            with self.subTest(url=url):
                self.assertRefused(self.client.get(url))

    def test_he_counts_a_new_inventory_and_lands_on_it(self):
        response = self.client.post(reverse("inventory:stock_take_create"), self.payload(), follow=True)
        created = StockTake.objects.exclude(pk=self.take.pk).get()
        self.assertRedirects(response, reverse("inventory:stock_take_detail", args=[created.pk]))
        self.assertContains(response, "Inventaire enregistré.")
        self.assertEqual(created.note, "Comptage d'avril")
        line = created.lines.get()
        self.assertEqual((line.product, line.counted_quantity), (self.product, 2))
        # Priced as the owner's count is, though nobody showed him the price:
        # the value frozen at its date is the count's, not the counter's.
        self.assertEqual(str(line.value_ht), "20.58")
        self.assertNotContains(response, "20.58")

    def test_he_corrects_a_count(self):
        data = {
            **self.payload(),
            "taken_at": "2026-03-31 12:00:00",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "1",
            "lines-0-id": str(self.take.lines.get().pk),
            "lines-0-counted_quantity": "5",
        }
        response = self.client.post(self.edit_url(), data)
        self.assertRedirects(response, self.detail_url())
        self.assertEqual(self.take.lines.get().counted_quantity, 5)


class CountingAndProductsTests(StockTakeCase):
    """Given « Produits & charges » too, which already shows every price."""

    def setUp(self):
        super().setUp()
        self.log_in("stock_takes", "products")

    def test_the_values_are_shown(self):
        self.assertContains(self.page(self.list_url()), "Valeur (HT)")
        for url in (self.list_url(), self.detail_url()):
            with self.subTest(url=url):
                self.assertContains(self.page(url), VALUE)
        detail = self.page(self.detail_url())
        self.assertContains(detail, "Déduit de")
        self.assertContains(detail, UNIT_COST)

    def test_the_invoice_is_named_not_linked_without_factures(self):
        detail = self.page(self.detail_url())
        self.assertContains(detail, f"facture du {INVOICE_DAY}")
        self.assertNotContains(detail, reverse("invoices:invoice_detail", args=[self.invoice.pk]))

    def test_the_form_prices_the_lines(self):
        response = self.page(self.edit_url())
        self.assertContains(response, f'var VALUE_URL = "{reverse("inventory:value_stock_take_line")}";')
        line = self.take.lines.get()
        self.assertEqual(island(response.content.decode(), "saved-values"), {str(line.pk): "41.16"})

    def test_pricing_a_line_answers(self):
        response = self.client.get(self.value_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["value_ht"], "20.58")

    def test_still_neither_deleting_nor_the_gaps(self):
        detail = self.page(self.detail_url())
        self.assertNotContains(detail, "Supprimer")
        self.assertNotContains(detail, "Voir les écarts")
        self.assertRefused(self.client.post(reverse("inventory:stock_take_delete", args=[self.take.pk])), posted=True)
        self.assertTrue(StockTake.objects.filter(pk=self.take.pk).exists())


class TheOwnerTests(StockTakeCase):
    """The suite's client: the espace's owner, as before."""

    def test_he_sees_every_value_and_every_action(self):
        listing = self.page(self.list_url())
        self.assertContains(listing, "Valeur (HT)")
        self.assertContains(listing, VALUE)
        self.assertContains(listing, "Combler les écarts")
        detail = self.page(self.detail_url())
        for shown in (VALUE, UNIT_COST, "Valeur comptée", "Déduit de", "Supprimer", "Voir les écarts"):
            with self.subTest(shown=shown):
                self.assertContains(detail, shown)
        self.assertContains(detail, reverse("invoices:invoice_detail", args=[self.invoice.pk]))
        self.assertContains(detail, reverse("inventory:stock_take_delete", args=[self.take.pk]))

    def test_his_form_prices_the_lines(self):
        response = self.page(self.edit_url())
        self.assertContains(response, f'var VALUE_URL = "{reverse("inventory:value_stock_take_line")}";')
        self.assertNotEqual(island(response.content.decode(), "saved-values"), {})
        self.assertEqual(self.client.get(self.value_url()).status_code, 200)

    def test_with_no_count_yet_his_gap_filler_offers_one(self):
        StockTake.objects.all().delete()
        response = self.page(reverse("inventory:stock_gap_filler"))
        self.assertContains(response, f'href="{reverse("inventory:stock_take_create")}"')

    def test_he_deletes_a_count(self):
        response = self.client.post(reverse("inventory:stock_take_delete", args=[self.take.pk]))
        self.assertRedirects(response, self.list_url())
        self.assertFalse(StockTake.objects.filter(pk=self.take.pk).exists())


class GapsOnlyTests(StockTakeCase):
    """Given « Écarts d'inventaire » and not « Faire un inventaire »."""

    def setUp(self):
        super().setUp()
        self.log_in("stock_gaps")

    def test_the_gaps_open(self):
        for url in (
            reverse("inventory:stock_take_variance", args=[self.take.pk]),
            reverse("inventory:stock_gap_filler"),
        ):
            with self.subTest(url=url):
                self.page(url)

    def test_the_counts_are_refused(self):
        for url in (self.list_url(), self.detail_url(), self.edit_url(), reverse("inventory:stock_take_create")):
            with self.subTest(url=url):
                self.assertRefused(self.client.get(url))

    def test_the_gap_filler_offers_no_count(self):
        response = self.page(reverse("inventory:stock_gap_filler"))
        self.assertNotContains(response, reverse("inventory:stock_take_create"))
        self.assertNotContains(response, f'href="{self.list_url()}"')

    def test_with_no_count_yet_the_gap_filler_offers_none_either(self):
        StockTake.objects.all().delete()
        response = self.page(reverse("inventory:stock_gap_filler"))
        self.assertContains(response, "Aucun inventaire")
        self.assertNotContains(response, reverse("inventory:stock_take_create"))
        self.assertNotContains(response, "+ Nouvel inventaire")

    def test_the_variance_links_neither_the_count_nor_the_products(self):
        response = self.page(reverse("inventory:stock_take_variance", args=[self.take.pk]))
        self.assertNotContains(response, f'href="{self.detail_url()}"')
        self.assertNotContains(response, "Retour à l'inventaire")
        self.assertNotContains(response, "Voir article par article")

    def test_the_gap_filler_warns_him_of_unlinked_till_products(self):
        """Their sales count as gaps: the warning is the page's own, not the
        navigation's badge, which is counted for « Recettes » alone (review
        of 02/10/2026) - said, and not linked."""
        from recipes.models import PosProduct

        PosProduct.objects.create(name="Produit caisse exemple A")
        PosProduct.objects.create(name="Produit caisse exemple B")
        response = self.page(reverse("inventory:stock_gap_filler"))
        self.assertContains(response, "2 produits de caisse ne sont liés à aucune recette")
        self.assertNotContains(response, f'href="{reverse("recipes:pos_product_list")}"')

    def test_the_gap_filler_leads_to_the_variance_and_back(self):
        """The two halves of his area: the gaps of a count, and filling them."""
        variance = reverse("inventory:stock_take_variance", args=[self.take.pk])
        self.assertContains(self.page(reverse("inventory:stock_gap_filler")), f'href="{variance}"')
        back = f"{reverse('inventory:stock_gap_filler')}?depuis={self.take.pk}"
        self.assertContains(self.page(variance), f'href="{back}"')

    def test_the_gap_filler_links_no_page_of_recettes(self):
        """« Variance/gap filler: product/recipe/stock-take links by area »
        (the feature's own account): the sales to import, the till products
        to link and the recipes are « Recettes & ventes »'s."""
        response = self.page(reverse("inventory:stock_gap_filler"))
        for route in ("recipes:sales_import", "recipes:pos_product_list"):
            with self.subTest(route=route):
                self.assertNotContains(response, f'href="{reverse(route)}"')
