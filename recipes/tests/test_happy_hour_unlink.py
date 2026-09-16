"""Taking a till product back off a recipe.

Linked as a recipe's happy-hour variant, a till product writes its name on
the recipe (`happy_hour_name`), and the import counts every sale by that
name. Ignoring the product, or sending it back to the worklist, left the name
there: the next import still added its sales to the recipe - and put a
product sent back to the worklist straight back on it.
"""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from recipes.models import PosProduct, RecipeSale
from recipes.sales import record_sales
from recipes.tasks import sync_pos_products
from tests.factories import make_recipe


class HappyHourUnlinkTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Pinte Blonde")
        self.product = PosProduct.objects.create(name="Pinte Blonde HH", total_quantity=5)
        self.act("link", recipe=self.recipe.pk, as_happy_hour="1")
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "Pinte Blonde HH")

    def act(self, action, **data):
        return self.client.post(
            reverse("recipes:pos_product_assign", args=[self.product.pk]), {"action": action, **data}
        )

    def import_sales(self):
        return record_sales([("Pinte Blonde HH", date(2026, 7, 1), 5)], source="laddition")

    def test_an_ignored_variant_no_longer_sells_as_the_recipe(self):
        self.act("ignore")
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "")
        result = self.import_sales()
        self.assertFalse(RecipeSale.objects.filter(recipe=self.recipe).exists())
        self.assertEqual(result.unmatched, [], "an ignored product is not a name waiting for a recipe")

    def test_a_variant_sent_back_to_the_worklist_stays_there(self):
        self.act("reset")
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "")

        class Export:
            products = {"Pinte Blonde HH": {"category": "", "typology": "", "first": date(2026, 7, 1),
                                            "last": date(2026, 7, 1)}}
            entries = [("Pinte Blonde HH", date(2026, 7, 1), 5)]

        sync_pos_products(Export())
        self.product.refresh_from_db()
        self.assertIsNone(self.product.recipe)

    def test_a_variant_moved_to_another_recipe_leaves_the_first(self):
        other = make_recipe(name="Pinte Ambrée")
        self.act("link", recipe=other.pk)
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "")
        self.import_sales()
        self.assertEqual(list(RecipeSale.objects.values_list("recipe__name", flat=True)), ["Pinte Ambrée"])

    def test_an_ignored_product_named_like_a_recipe_does_not_sell_as_it(self):
        coffee = PosProduct.objects.create(name="Pinte Blonde", ignored=True)
        record_sales([(coffee.name, date(2026, 7, 1), 3)], source="laddition")
        self.assertFalse(RecipeSale.objects.exists())

    def test_relinking_the_same_variant_keeps_its_name(self):
        self.act("link", recipe=self.recipe.pk, as_happy_hour="1")
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "Pinte Blonde HH")
        self.import_sales()
        self.assertEqual(RecipeSale.objects.get().quantity, 5)
