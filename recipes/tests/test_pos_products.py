"""Tests for the till-products screen - the backlog of things the till sells
that don't have a recipe yet.

Every unmapped product is stock leaving the shelf with nothing to explain it,
so it lands in the variance report as missing. Getting an item OFF this list
is therefore the one action that makes shrinkage numbers trustworthy, and
each of the four ways of doing so is checked here.
"""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from recipes.models import PosProduct, RecipeSale
from recipes.sales import record_sales
from recipes.tasks import sync_pos_products
from recipes.pos.laddition_xlsx import ParsedExport
from tests.factories import make_recipe


def make_pos_product(name, **kwargs):
    kwargs.setdefault("total_quantity", 10)
    return PosProduct.objects.create(name=name, **kwargs)


class PosProductListTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Alcool + Soda")

    def test_the_page_lists_what_needs_doing(self):
        make_pos_product("Pinte Blonde", total_quantity=512)
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pinte Blonde")
        self.assertEqual([p.name for p in response.context["pending"]], ["Pinte Blonde"])

    def test_biggest_sellers_come_first(self):
        """That's where the unexplained stock is."""
        make_pos_product("Rare", total_quantity=3)
        make_pos_product("Pinte Blonde", total_quantity=512)
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertEqual([p.name for p in response.context["pending"]], ["Pinte Blonde", "Rare"])

    def test_linked_and_ignored_are_kept_out_of_the_worklist(self):
        make_pos_product("Lié", recipe=self.recipe)
        make_pos_product("Ignoré", ignored=True)
        make_pos_product("À faire")
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertEqual([p.name for p in response.context["pending"]], ["À faire"])
        self.assertEqual([p.name for p in response.context["linked"]], ["Lié"])
        self.assertEqual([p.name for p in response.context["ignored"]], ["Ignoré"])

    def test_an_empty_backlog_says_so(self):
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertContains(response, "Aucun produit de caisse")


class PosProductAssignTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Alcool + Soda")
        self.product = make_pos_product("Alcool + soda HH", total_quantity=147)

    def post(self, action, **extra):
        return self.client.post(
            reverse("recipes:pos_product_assign", kwargs={"pk": self.product.pk}),
            {"action": action, **extra},
        )

    def test_linking_to_a_recipe(self):
        self.post("link", recipe=self.recipe.pk)
        self.product.refresh_from_db()
        self.assertEqual(self.product.recipe, self.recipe)
        self.assertFalse(self.product.needs_review)

    def test_linking_makes_the_sales_import_recognise_the_name(self):
        """The whole point: the next import stops discarding it."""
        record_sales([("Alcool + soda HH", date(2026, 6, 1), 5)])
        self.assertEqual(RecipeSale.objects.count(), 0)

        self.post("link", recipe=self.recipe.pk)
        record_sales([("Alcool + soda HH", date(2026, 6, 1), 5)])
        self.assertEqual(RecipeSale.objects.get().recipe, self.recipe)

    def test_marking_it_as_a_happy_hour_variant(self):
        """Sets the name on the RECIPE, so both till names fold into one."""
        self.post("happy_hour", recipe=self.recipe.pk)
        self.recipe.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "Alcool + soda HH")
        self.assertEqual(self.product.recipe, self.recipe)

        record_sales(
            [("Alcool + Soda", date(2026, 6, 1), 20), ("Alcool + soda HH", date(2026, 6, 1), 3)]
        )
        self.assertEqual(RecipeSale.objects.get().quantity, 23)

    def test_a_happy_hour_name_already_taken_is_refused(self):
        """One till name can only mean one recipe - see Recipe.clean."""
        other = make_recipe(name="Autre", happy_hour_name="Alcool + soda HH")
        response = self.client.post(
            reverse("recipes:pos_product_assign", kwargs={"pk": self.product.pk}),
            {"action": "happy_hour", "recipe": self.recipe.pk},
            follow=True,
        )
        self.recipe.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "")
        self.assertIsNone(self.product.recipe)
        self.assertContains(response, "Autre")  # the message names the clash
        other.refresh_from_db()
        self.assertEqual(other.happy_hour_name, "Alcool + soda HH")

    def test_ignoring_something_that_consumes_no_tracked_stock(self):
        self.post("ignore")
        self.product.refresh_from_db()
        self.assertTrue(self.product.ignored)
        self.assertFalse(self.product.needs_review)

    def test_putting_one_back_on_the_list(self):
        self.post("link", recipe=self.recipe.pk)
        self.post("reset")
        self.product.refresh_from_db()
        self.assertIsNone(self.product.recipe)
        self.assertFalse(self.product.ignored)
        self.assertTrue(self.product.needs_review)

    def test_linking_without_choosing_a_recipe_changes_nothing(self):
        self.post("link", recipe="")
        self.product.refresh_from_db()
        self.assertIsNone(self.product.recipe)

    def test_a_get_does_not_change_anything(self):
        response = self.client.get(
            reverse("recipes:pos_product_assign", kwargs={"pk": self.product.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.recipe)

    def test_an_explicit_link_beats_a_coinciding_recipe_name(self):
        """A mapping made by hand is a deliberate decision about this exact
        till product; a name that merely matches is not."""
        deliberate = make_recipe(name="Autre chose")
        product = make_pos_product("Alcool + Soda", recipe=deliberate)
        record_sales([("Alcool + Soda", date(2026, 6, 1), 4)])
        self.assertEqual(RecipeSale.objects.get().recipe, deliberate)
        self.assertEqual(product.recipe, deliberate)


class SyncPosProductsTests(TestCase):
    def export(self, products):
        return ParsedExport(products=products)

    def test_products_are_created_from_an_import(self):
        sync_pos_products(
            self.export(
                {
                    "Pinte Blonde": {
                        "quantity": 512, "category": "Bières", "typology": "Liquide (Alcool)",
                        "first": date(2026, 6, 1), "last": date(2026, 6, 30),
                    }
                }
            )
        )
        product = PosProduct.objects.get()
        self.assertEqual(product.total_quantity, 512)
        self.assertEqual(product.category, "Bières")
        self.assertEqual((product.first_seen, product.last_seen), (date(2026, 6, 1), date(2026, 6, 30)))

    def test_a_second_import_extends_the_dates_and_adds_to_the_total(self):
        first = {
            "Pinte Blonde": {
                "quantity": 100, "category": "Bières", "typology": "",
                "first": date(2026, 6, 1), "last": date(2026, 6, 30),
            }
        }
        later = {
            "Pinte Blonde": {
                "quantity": 50, "category": "", "typology": "Liquide (Alcool)",
                "first": date(2026, 7, 1), "last": date(2026, 7, 31),
            }
        }
        sync_pos_products(self.export(first))
        sync_pos_products(self.export(later))
        product = PosProduct.objects.get()
        self.assertEqual(product.total_quantity, 150)
        self.assertEqual((product.first_seen, product.last_seen), (date(2026, 6, 1), date(2026, 7, 31)))
        # Metadata missing from the later import doesn't wipe what we knew.
        self.assertEqual(product.category, "Bières")
        self.assertEqual(product.typology, "Liquide (Alcool)")

    def test_an_existing_mapping_survives_a_re_import(self):
        recipe = make_recipe(name="Blonde")
        PosProduct.objects.create(name="Pinte Blonde", recipe=recipe)
        sync_pos_products(
            self.export(
                {
                    "Pinte Blonde": {
                        "quantity": 10, "category": "", "typology": "",
                        "first": date(2026, 6, 1), "last": date(2026, 6, 30),
                    }
                }
            )
        )
        self.assertEqual(PosProduct.objects.get().recipe, recipe)


class RecipeCreatePrefillTests(TestCase):
    def test_the_create_form_is_prefilled_from_the_till_name(self):
        """Retyping a name that has to match the till exactly is exactly how
        it ends up not matching."""
        response = self.client.get(reverse("recipes:recipe_create"), {"name": "Spritz Aperol"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Spritz Aperol"')

    def test_without_the_parameter_the_form_is_blank(self):
        response = self.client.get(reverse("recipes:recipe_create"))
        self.assertEqual(response.status_code, 200)


class HappyHourModifierTests(TestCase):
    """"Happy hour" is a modifier on linking, not a separate action - it links
    to the same recipe AND records the till's name for it."""

    def setUp(self):
        self.recipe = make_recipe(name="Pinte Blonde")
        self.product = PosProduct.objects.create(name="Pinte Blonde HH", total_quantity=197)

    def post(self, **extra):
        data = {"action": "link", "recipe": self.recipe.pk}
        data.update(extra)
        return self.client.post(
            reverse("recipes:pos_product_assign", kwargs={"pk": self.product.pk}), data
        )

    def test_linking_without_the_checkbox_leaves_the_happy_hour_name_alone(self):
        self.post()
        self.product.refresh_from_db()
        self.recipe.refresh_from_db()
        self.assertEqual(self.product.recipe, self.recipe)
        self.assertEqual(self.recipe.happy_hour_name, "")

    def test_the_checkbox_records_the_till_name_as_the_happy_hour_one(self):
        self.post(as_happy_hour="1")
        self.product.refresh_from_db()
        self.recipe.refresh_from_db()
        self.assertEqual(self.product.recipe, self.recipe)
        self.assertEqual(self.recipe.happy_hour_name, "Pinte Blonde HH")

    def test_the_old_separate_action_still_works(self):
        """An open tab or a bookmark shouldn't 400 after the redesign."""
        self.client.post(
            reverse("recipes:pos_product_assign", kwargs={"pk": self.product.pk}),
            {"action": "happy_hour", "recipe": self.recipe.pk},
        )
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "Pinte Blonde HH")

    def test_a_clashing_happy_hour_name_is_refused_with_a_message(self):
        make_recipe(name="Pinte Blonde HH")   # already answers to that name
        self.post(as_happy_hour="1")
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.happy_hour_name, "")
        self.assertTrue(any("Déjà utilisé" in str(m) for m in response.context["messages"]))


class BulkIgnoreTests(TestCase):
    """A first import leaves a hundred-odd unmatched products, most of them
    food and coffee. One page reload each is not a workflow."""

    def setUp(self):
        for name in ("Café", "Thé", "Planche", "Mule"):
            PosProduct.objects.create(name=name, total_quantity=10)

    def test_several_products_are_ignored_at_once(self):
        response = self.client.post(
            reverse("recipes:pos_products_bulk"), {"selected": ["Café", "Thé", "Planche"]}, follow=True
        )
        self.assertEqual(PosProduct.objects.filter(ignored=True).count(), 3)
        self.assertFalse(PosProduct.objects.get(name="Mule").ignored)
        self.assertTrue(any("3 produits ignorés" in str(m) for m in response.context["messages"]))

    def test_one_product_reads_as_singular(self):
        response = self.client.post(
            reverse("recipes:pos_products_bulk"), {"selected": ["Café"]}, follow=True
        )
        self.assertTrue(any("1 produit ignoré" in str(m) for m in response.context["messages"]))

    def test_an_empty_selection_says_so_rather_than_silently_doing_nothing(self):
        response = self.client.post(reverse("recipes:pos_products_bulk"), {}, follow=True)
        self.assertEqual(PosProduct.objects.filter(ignored=True).count(), 0)
        self.assertTrue(any("Aucun produit" in str(m) for m in response.context["messages"]))

    def test_a_get_does_nothing(self):
        self.client.get(reverse("recipes:pos_products_bulk"))
        self.assertEqual(PosProduct.objects.filter(ignored=True).count(), 0)

    def test_bulk_ignore_clears_any_existing_link(self):
        recipe = make_recipe(name="Mule")
        product = PosProduct.objects.get(name="Mule")
        product.recipe = recipe
        product.save()
        self.client.post(reverse("recipes:pos_products_bulk"), {"selected": ["Mule"]})
        product.refresh_from_db()
        self.assertTrue(product.ignored)
        self.assertIsNone(product.recipe)
