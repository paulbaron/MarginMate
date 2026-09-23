"""« Compter dans la marge produits », on the article's own form.

The paper towels, the cups, the straws: no recipe consumes them, so nothing
takes them out of stock as a sale is rung up - and yet every sale costs them.
The flag says « count what was bought of this in the products margin », and
it has to be set from the page a person already edits the article on, or it
is a field nobody can reach.

Two things it must NOT do: default to on (an article counted here and in a
recipe is paid for twice), and be lost by a page that saves the article for
another reason.

Data invented.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from inventory.forms import StockTypeForm
from inventory.models import StockType, UnitChoices
from tests.factories import make_stock_type


class ProductsMarginFlagTests(TestCase):
    def test_an_article_does_not_count_unless_it_is_asked_to(self):
        self.assertFalse(make_stock_type(name="Vodka").count_in_products_margin)

    def test_the_form_offers_it_in_french(self):
        form = StockTypeForm()
        self.assertIn("count_in_products_margin", form.fields)
        self.assertEqual(
            form.fields["count_in_products_margin"].label, "Compter dans la marge produits"
        )

    def test_the_form_says_what_ticking_it_does_and_what_it_must_not_be_used_for(self):
        """A flag whose meaning lives only in the code is a flag ticked by
        guess. Two things have to be on screen beside it: what the figure
        means (what was BOUGHT over the period - no recipe consumes these, so
        there is nothing else to measure) and the one mistake it invites (an
        article that IS in a recipe, then paid for twice)."""
        help_text = StockTypeForm().fields["count_in_products_margin"].help_text

        self.assertIn("aucune recette", help_text)
        self.assertIn("sur la période", help_text)
        self.assertIn("compté deux fois", help_text)
        page = self.client.get(reverse("inventory:stock_type_create")).content.decode()
        self.assertIn(escape(help_text), page)

    def test_it_is_saved_from_the_article_page(self):
        article = make_stock_type(name="Essuie-tout", unit=UnitChoices.UNIT)

        response = self.client.post(
            reverse("inventory:stock_type_update", args=[article.pk]),
            {
                "name": "Essuie-tout",
                "unit": UnitChoices.UNIT,
                "category": "Consommables",
                "loss_percent": "0",
                "count_in_products_margin": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        article.refresh_from_db()
        self.assertTrue(article.count_in_products_margin)

    def test_unticking_it_takes_the_article_back_out(self):
        article = make_stock_type(name="Gobelets", unit=UnitChoices.UNIT, count_in_products_margin=True)

        self.client.post(
            reverse("inventory:stock_type_update", args=[article.pk]),
            {"name": "Gobelets", "unit": UnitChoices.UNIT, "category": "", "loss_percent": "10"},
        )

        article.refresh_from_db()
        self.assertFalse(article.count_in_products_margin)

    def test_a_new_article_can_be_created_with_it(self):
        self.client.post(
            reverse("inventory:stock_type_create"),
            {
                "name": "Touillettes en bois",
                "unit": UnitChoices.UNIT,
                "category": "",
                "loss_percent": "0",
                "count_in_products_margin": "on",
            },
        )

        article = StockType.objects.get(name="Touillettes en bois")
        self.assertTrue(article.count_in_products_margin)
        self.assertEqual(article.loss_percent, Decimal("0"))
