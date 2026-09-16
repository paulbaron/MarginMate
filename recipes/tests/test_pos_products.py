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

    def test_each_table_has_a_stable_id(self):
        """Without an explicit id, datatable.js falls back to this table's
        position among every data-table on the page (see tableKey in
        static/js/datatable.js) to key its persisted search text. "Rattachés"
        and "Ignorés" only render at all once something has been linked or
        ignored, so without a fixed id, a search box's saved text could end
        up keyed to a position a DIFFERENT table now occupies as soon as the
        set of visible sections changes between visits - a search that
        "sometimes" seems to apply to the wrong table. One id per table,
        regardless of which sections are present, closes that off."""
        make_pos_product("À faire")
        make_pos_product("Lié", recipe=self.recipe)
        make_pos_product("Ignoré", ignored=True)
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertContains(response, 'id="pos-pending-table"')
        self.assertContains(response, 'id="pos-linked-table"')
        self.assertContains(response, 'id="pos-ignored-table"')

    def test_the_recipe_picker_column_is_not_sortable(self):
        """Every row's "Que faire ?" cell carries the identical <select> of
        every recipe - not describing that row, just offering a choice - so
        sorting or searching by it is meaningless. Sorting is opted out with
        data-no-sort; see static/js/datatable.js's searchableText for why
        the SAME <select> also has to be stripped out of what search reads,
        or typing any recipe name (e.g. "pinte") matches every row that
        COULD be linked to a "Pinte…" recipe - which used to be all of
        them."""
        make_pos_product("À faire")
        response = self.client.get(reverse("recipes:pos_product_list"))
        self.assertRegex(response.content.decode(), r"<th data-no-sort[^>]*>Que faire \?")


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

    def test_linking_immediately_backfills_from_daily_quantities_already_on_file(self):
        """The whole point of PosProductDailyQuantity: this product's
        per-day figures have been sitting there since the import that first
        saw it, whether or not it had a recipe yet (see sync_pos_products).
        Linking is therefore a pure local rebuild - no L'Addition contact,
        no waiting - and "Dernières ventes" is correct the moment this POST
        returns, not after a separate recovery step."""
        from recipes.models import PosProductDailyQuantity

        PosProductDailyQuantity.objects.create(product=self.product, sold_on=date(2026, 6, 1), quantity=5)
        PosProductDailyQuantity.objects.create(product=self.product, sold_on=date(2026, 6, 2), quantity=3)

        self.post("link", recipe=self.recipe.pk)

        self.assertEqual(RecipeSale.objects.filter(recipe=self.recipe).count(), 2)
        self.assertEqual(
            sum(RecipeSale.objects.filter(recipe=self.recipe).values_list("quantity", flat=True)), 8
        )

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

    def test_detaching_takes_its_days_back_out_of_the_recipe(self):
        """The other direction of the same rebuild - a day this product no
        longer accounts for must not keep inflating the recipe it used to
        belong to."""
        from recipes.models import PosProductDailyQuantity

        PosProductDailyQuantity.objects.create(product=self.product, sold_on=date(2026, 6, 1), quantity=5)
        self.post("link", recipe=self.recipe.pk)
        self.assertEqual(RecipeSale.objects.filter(recipe=self.recipe).count(), 1)

        self.post("reset")
        self.assertEqual(RecipeSale.objects.filter(recipe=self.recipe).count(), 0)

    def test_detaching_leaves_a_day_another_linked_product_still_covers(self):
        """Two till names can fold into one recipe (see happy hour) - taking
        one of them back out must only remove ITS share of a shared day."""
        from recipes.models import PosProductDailyQuantity

        other = make_pos_product("Alcool + Soda", recipe=self.recipe)
        PosProductDailyQuantity.objects.create(product=other, sold_on=date(2026, 6, 1), quantity=20)
        PosProductDailyQuantity.objects.create(product=self.product, sold_on=date(2026, 6, 1), quantity=5)
        self.post("link", recipe=self.recipe.pk)
        self.assertEqual(RecipeSale.objects.get(recipe=self.recipe).quantity, 25)

        self.post("reset")
        self.assertEqual(RecipeSale.objects.get(recipe=self.recipe).quantity, 20)

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
    def export(self, products, entries):
        """`entries` mirrors `products` the way a real parse always does -
        see parse_rows, which builds both from the same rows in the same
        loop. total_quantity is now computed from entries (per-day), not
        from products' own window-wide total - see PosProductDailyQuantity."""
        return ParsedExport(products=products, entries=entries)

    def test_products_are_created_from_an_import(self):
        sync_pos_products(
            self.export(
                {
                    "Pinte Blonde": {
                        "quantity": 512, "category": "Bières", "typology": "Liquide (Alcool)",
                        "first": date(2026, 6, 1), "last": date(2026, 6, 30),
                    }
                },
                entries=[("Pinte Blonde", date(2026, 6, 15), 512)],
            )
        )
        product = PosProduct.objects.get()
        self.assertEqual(product.total_quantity, 512)
        self.assertEqual(product.category, "Bières")
        self.assertEqual((product.first_seen, product.last_seen), (date(2026, 6, 1), date(2026, 6, 30)))

    def test_a_second_import_extends_the_dates_and_adds_to_the_total(self):
        """Two genuinely different, non-overlapping periods still add up -
        this is not the same thing as re-importing the SAME period twice,
        which IdempotentReSyncTests covers instead."""
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
        sync_pos_products(self.export(first, entries=[("Pinte Blonde", date(2026, 6, 15), 100)]))
        sync_pos_products(self.export(later, entries=[("Pinte Blonde", date(2026, 7, 15), 50)]))
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
                },
                entries=[("Pinte Blonde", date(2026, 6, 1), 10)],
            )
        )
        self.assertEqual(PosProduct.objects.get().recipe, recipe)


class AutoLinkCoincidingNamesTests(TestCase):
    """A till name that already answers to a recipe - its own name, or a
    happy_hour_name - is counted into that recipe's sales by record_sales
    with or without an explicit PosProduct link (see recipe_lookup). Left
    unlinked, the product sat in "needs review" forever for something
    already resolved, and "Produits caisse" undercounted the recipe
    relative to "Dernières ventes" - the two pages stopped reconciling.
    Seen on real till data: "alcool + soda" (matching the recipe's own name
    exactly) sat unlinked while "Alcool + soda HH" was linked, so Rattachés
    showed only a fraction of what Dernières ventes reported for the
    recipe."""

    def export(self, name, quantity, sold_on):
        return ParsedExport(
            products={name: {"quantity": quantity, "category": "", "typology": "", "first": sold_on, "last": sold_on}},
            entries=[(name, sold_on, quantity)],
        )

    def test_a_name_matching_a_recipe_is_linked_automatically(self):
        recipe = make_recipe(name="Alcool + Soda")
        sync_pos_products(self.export("Alcool + Soda", 30, date(2026, 6, 1)))
        product = PosProduct.objects.get(name="Alcool + Soda")
        self.assertEqual(product.recipe, recipe)
        self.assertFalse(product.needs_review)

    def test_a_name_matching_a_happy_hour_name_is_linked_automatically(self):
        recipe = make_recipe(name="Alcool + Soda")
        recipe.happy_hour_name = "Alcool + soda HH"
        recipe.save(update_fields=["happy_hour_name"])
        sync_pos_products(self.export("Alcool + soda HH", 12, date(2026, 6, 1)))
        self.assertEqual(PosProduct.objects.get(name="Alcool + soda HH").recipe, recipe)

    def test_an_ignored_product_is_not_reopened_by_a_coincidence(self):
        """Ignoring is a deliberate decision too - a later import finding a
        name-match must not silently undo it."""
        recipe = make_recipe(name="Café")
        PosProduct.objects.create(name="Café", ignored=True)
        sync_pos_products(self.export("Café", 5, date(2026, 6, 1)))
        product = PosProduct.objects.get(name="Café")
        self.assertIsNone(product.recipe)
        self.assertTrue(product.ignored)

    def test_a_name_with_no_matching_recipe_stays_pending(self):
        sync_pos_products(self.export("Truc Inconnu", 3, date(2026, 6, 1)))
        product = PosProduct.objects.get(name="Truc Inconnu")
        self.assertIsNone(product.recipe)
        self.assertTrue(product.needs_review)

    def test_produits_caisse_and_dernieres_ventes_now_agree(self):
        """The concrete symptom: summed across every till name for one
        recipe, the two pages have to land on the same number."""
        from django.db.models import Sum

        from recipes.models import RecipeSale
        from recipes.sales import record_sales

        recipe = make_recipe(name="Alcool + Soda")
        recipe.happy_hour_name = "Alcool + soda HH"
        recipe.save(update_fields=["happy_hour_name"])
        export = ParsedExport(
            products={
                "Alcool + Soda": {"quantity": 30, "category": "", "typology": "", "first": date(2026, 6, 1), "last": date(2026, 6, 1)},
                "Alcool + soda HH": {"quantity": 12, "category": "", "typology": "", "first": date(2026, 6, 1), "last": date(2026, 6, 1)},
            },
            entries=[("Alcool + Soda", date(2026, 6, 1), 30), ("Alcool + soda HH", date(2026, 6, 1), 12)],
        )
        sync_pos_products(export)
        record_sales(export.entries, source="laddition")

        dernieres_ventes = RecipeSale.objects.filter(recipe=recipe).aggregate(Sum("quantity"))["quantity__sum"]
        produits_caisse = PosProduct.objects.filter(recipe=recipe).aggregate(Sum("total_quantity"))["total_quantity__sum"]
        self.assertEqual(dernieres_ventes, 42)
        self.assertEqual(produits_caisse, 42)


class IdempotentReSyncTests(TestCase):
    """The actual bug: PosProduct.total_quantity used to be a bare counter
    with no memory of which dates a previous import already covered, so
    re-importing an OVERLAPPING window (which pos_products_backfill does
    routinely) added to it again, every time - one real product was found
    reading 4-5x its true total after a handful of backfills."""

    def export(self, entries):
        products: dict = {}
        for name, day, quantity in entries:
            info = products.setdefault(
                name, {"quantity": 0, "category": "", "typology": "", "first": day, "last": day}
            )
            info["quantity"] += quantity
            info["first"] = min(info["first"], day)
            info["last"] = max(info["last"], day)
        return ParsedExport(products=products, entries=entries)

    def test_re_importing_the_exact_same_window_does_not_double_the_total(self):
        entries = [("Pinte Blonde", date(2026, 6, 1), 10), ("Pinte Blonde", date(2026, 6, 2), 15)]
        sync_pos_products(self.export(entries))
        sync_pos_products(self.export(entries))  # the SAME window again
        self.assertEqual(PosProduct.objects.get().total_quantity, 25)

    def test_an_overlapping_window_corrects_the_shared_days_rather_than_adding(self):
        sync_pos_products(self.export([
            ("Pinte Blonde", date(2026, 6, 1), 10), ("Pinte Blonde", date(2026, 6, 2), 15),
        ]))
        # Re-covers June 2nd (unchanged) and adds June 3rd - a wider,
        # overlapping backfill window, exactly what the backfill button
        # sends when it reaches back to the earliest gap.
        sync_pos_products(self.export([
            ("Pinte Blonde", date(2026, 6, 2), 15), ("Pinte Blonde", date(2026, 6, 3), 7),
        ]))
        self.assertEqual(PosProduct.objects.get().total_quantity, 32)  # 10 + 15 + 7, not 47

    def test_re_importing_five_times_still_reads_the_true_total(self):
        entries = [("Pinte Blonde", date(2026, 6, 1), 10)]
        for _ in range(5):
            sync_pos_products(self.export(entries))
        self.assertEqual(PosProduct.objects.get().total_quantity, 10)


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
