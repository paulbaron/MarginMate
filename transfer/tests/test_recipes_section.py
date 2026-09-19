"""« Recettes » (§7.4), with the checks of §10.2 every section owes.

The sections it requires - fournisseurs, associations - are the core's fakes
here. They hold nothing of these tests (the articles the recipes use are
plain rows, which no fake exports or clears), so this lane's tests stand on
their own whatever the other lanes' state; the full round trip with every
real section is the integration's (test_full_round_trip.py).
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from inventory.models import UnitChoices
from recipes.forms import MANUAL_SALE_SOURCE
from recipes.models import (
    PosProduct,
    Recipe,
    RecipeIngredient,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
)
from tests.factories import make_ingredient, make_recipe, make_stock_type
from transfer import registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections.base import Strategy
from transfer.sections.recipes import (
    NOT_EXPORTED,
    RECIPE_FIELDS,
    STAMP,
    RecipesSection,
    dependency_order,
    find_cycles,
)
from transfer.sections.sales import SalesSection
from transfer.sections.till_links import TillLinksSection
from transfer.tests.support import (
    FAKES,
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    media_listing,
    reset_fakes,
    round_trip,
)

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
#: The export closure of « recettes »: what an archive of it holds.
EXPORTED = {"recettes", "associations", "fournisseurs"}
#: The usual replace: the recipes become the archive's, what they require
#: is merged (§2.2) - so what the other sections do to their own rows is
#: not part of these tests.
RECIPES_REPLACED = {"recettes": REPLACE, "associations": MERGE, "fournisseurs": MERGE}


class LaneSectionsMixin:
    """The three sections of lane C, real; every other key is the core's
    fake. Imported by the lane's other test modules (a plain class, so the
    test loader does not collect it twice)."""

    def setUp(self):
        super().setUp()
        reset_fakes()
        swap = registry.swap(
            {**FAKES, "recettes": RecipesSection, "liens_ventes": TillLinksSection, "ventes": SalesSection}
        )
        swap.__enter__()
        self.addCleanup(swap.__exit__, None, None, None)
        self.addCleanup(reset_fakes)

    def open(self, path) -> ArchiveReader:
        reader = ArchiveReader(path)
        self.addCleanup(reader.close)
        return reader

    def export(self, keys) -> ArchiveReader:
        reader = export_archive(keys)
        self.addCleanup(reader.close)
        return reader


def stamp(day: int) -> datetime:
    """A creation date that is not "now": the round trip must bring it back."""
    return datetime(2025, 3, day, 9, 30, 15, 123456, tzinfo=dt_timezone.utc)


def tally(run, key: str, what: str) -> tuple[int, int, int, int]:
    """(created, updated, deleted, unchanged) of one row of a report."""
    section = run.section(key)
    found = section.tallies.get(what) if section else None
    return (found.created, found.updated, found.deleted, found.unchanged) if found else (0, 0, 0, 0)


def build_recipes():
    """A small bar: a recipe with two choice groups, a sub-recipe used by a
    recipe that comes before it by name, a recipe named like an article, a
    happy-hour name and prices at every place the fields allow."""
    gin, vodka = make_stock_type("Gin"), make_stock_type("Vodka")
    tonic, cola = make_stock_type("Tonic"), make_stock_type("Cola")
    sugar = make_stock_type("Sucre", unit=UnitChoices.KILOGRAM)
    mint = make_stock_type("Menthe", unit=UnitChoices.UNIT)
    rum = make_stock_type("Rhum")

    soda = make_recipe(
        "Alcool + Soda", selling_price_ttc="8.00", happy_hour_price_ttc=Decimal("6.00"), category="Cocktail"
    )
    make_ingredient(soda, gin, quantity="0.04", group=0)
    make_ingredient(soda, vodka, quantity="0.04", group=0)
    make_ingredient(soda, tonic, quantity="0.2", group=1)
    make_ingredient(soda, cola, quantity="0.2", group=1)

    syrup = make_recipe("Sirop maison", selling_price_ttc="0", yield_quantity="1.5", yield_unit=UnitChoices.LITRE)
    make_ingredient(syrup, sugar, quantity="0.7500")

    mint_recipe = make_recipe("Menthe", selling_price_ttc="3.50", vat_rate="0.10")
    make_ingredient(mint_recipe, mint, quantity="12")

    mojito = make_recipe("Mojito", selling_price_ttc="9.50", happy_hour_name="Mojito HH")
    make_ingredient(mojito, rum, quantity="0.05")
    make_ingredient(mojito, sub_recipe=syrup, quantity="0.02")
    make_ingredient(mojito, mint, quantity="8")

    for day, recipe in enumerate((soda, syrup, mint_recipe, mojito), start=1):
        Recipe.objects.filter(pk=recipe.pk).update(created_at=stamp(day))
    return {"soda": soda, "syrup": syrup, "mint": mint_recipe, "mojito": mojito}


def ingredients_of(name: str) -> list[tuple]:
    recipe = Recipe.objects.get(name=name)
    return [
        (ingredient.group, ingredient.source_name, "recette" if ingredient.sub_recipe_id else "article", ingredient.quantity)
        for ingredient in recipe.ingredients.order_by("group", "id")
    ]


class GraphTests(SimpleTestCase):
    """The file's sub-recipe graph: pure functions over the archive, nothing
    of the database."""

    def test_nothing_uses_anything(self):
        self.assertEqual(find_cycles({}), {})
        self.assertEqual(find_cycles({"a": [], "b": []}), {})
        self.assertEqual(dependency_order({}, []), [])
        self.assertEqual(dependency_order({"a": [], "b": []}, ["b", "a"]), ["b", "a"])

    def test_a_recipe_using_itself(self):
        self.assertEqual(find_cycles({"a": ["a"], "b": []}), {"a": ["a", "a"]})

    def test_two_recipes_using_each_other_and_one_using_them(self):
        loops = find_cycles({"a": ["b"], "b": ["a"], "c": ["a"]})
        self.assertEqual(loops, {"a": ["a", "b", "a"], "b": ["b", "a", "b"]})

    def test_a_longer_loop(self):
        loops = find_cycles({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]})
        self.assertEqual(set(loops), {"a", "b", "c"})
        self.assertEqual(loops["b"], ["b", "c", "a", "b"])

    def test_a_recipe_outside_the_file_is_no_edge(self):
        self.assertEqual(find_cycles({"a": ["ailleurs"]}), {})

    def test_sub_recipes_come_first_and_the_file_order_otherwise(self):
        graph = {"mojito": ["sirop"], "spritz": [], "sirop": ["base"], "base": []}
        self.assertEqual(
            dependency_order(graph, ["mojito", "spritz", "sirop", "base"]), ["base", "sirop", "mojito", "spritz"]
        )

    def test_a_loop_left_in_is_broken_not_followed(self):
        self.assertEqual(sorted(dependency_order({"a": ["b"], "b": ["a"]}, ["a", "b"])), ["a", "b"])

    def test_a_long_chain_does_not_recurse(self):
        """A forged file cannot exhaust Python's recursion limit."""
        chain = {f"r{n}": [f"r{n + 1}"] for n in range(5000)}
        chain["r5000"] = []
        self.assertEqual(find_cycles(chain), {})
        self.assertEqual(dependency_order(chain, list(chain))[:2], ["r5000", "r4999"])
        chain["r5000"] = ["r0"]
        self.assertEqual(len(find_cycles(chain)), 5001)


class FieldGuardTests(SimpleTestCase):
    def test_every_field_is_exported_or_said_not_to_be(self):
        exported = {
            "Recipe": {"name", *RECIPE_FIELDS, STAMP},
            "RecipeIngredient": {"group", "quantity"},
        }
        for model in (Recipe, RecipeIngredient):
            name = model.__name__
            fields = {field.name for field in model._meta.concrete_fields}
            self.assertEqual(fields - exported[name] - set(NOT_EXPORTED[name]), set(), name)
            self.assertEqual(exported[name] & set(NOT_EXPORTED[name]), set(), name)


class RecipesRoundTripTests(LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.bar = build_recipes()

    def assert_empty(self):
        self.assertEqual(RecipesSection().count(), {"recettes": 0, "ingrédients": 0})

    def test_counts(self):
        self.assertEqual(RecipesSection().count(), {"recettes": 4, "ingrédients": 9})

    def test_round_trip_with_merge(self):
        before, after = round_trip({"recettes"}, MERGE, after_clear=self.assert_empty)
        self.assertEqual(set(before), EXPORTED)
        self.assertEqual(after, before)
        self.assertEqual(after["recettes"][2]["created_at"], stamp(4).isoformat())

    def test_round_trip_with_replace(self):
        before, after = round_trip({"recettes"}, REPLACE, after_clear=self.assert_empty)
        self.assertEqual(after, before)

    def test_the_same_variation_after_a_round_trip(self):
        """?v= indices count through a group in id order: re-created in
        another order, « v=1 » would quietly price another drink."""
        def variation(selection):
            soda = Recipe.objects.get(name="Alcool + Soda")
            response = self.client.get(reverse("recipes:recipe_detail", args=[soda.pk]) + f"?v={selection}")
            self.assertEqual(response.status_code, 200)
            return response.context["variation"]["name"]

        before = {selection: variation(selection) for selection in ("0.0", "1", "0.1", "1.1")}
        self.assertEqual(before["1"], "Alcool + Soda (Vodka, Tonic)")
        round_trip({"recettes"}, REPLACE)
        self.assertEqual({selection: variation(selection) for selection in before}, before)

    def test_a_recipe_named_like_an_article_stays_separate(self):
        round_trip({"recettes"}, MERGE)
        self.assertEqual(ingredients_of("Menthe"), [(0, "Menthe", "article", Decimal("12.0000"))])
        self.assertEqual(
            ingredients_of("Mojito"),
            [
                (0, "Rhum", "article", Decimal("0.0500")),
                (0, "Sirop maison", "recette", Decimal("0.0200")),
                (0, "Menthe", "article", Decimal("8.0000")),
            ],
        )

    def test_the_happy_hour_name_is_neither_exported_nor_touched(self):
        reader = self.export(EXPORTED)
        records = reader.section("recettes").payload()["recipes"]
        self.assertFalse(any("happy_hour_name" in record for record in records))
        self.assertNotIn("Mojito HH", str(records))

        Recipe.objects.filter(name="Mojito").update(happy_hour_name="Mojito (HH)")
        import_archive(reader, REPLACE)
        self.assertEqual(Recipe.objects.get(name="Mojito").happy_hour_name, "Mojito (HH)")

    def test_created_dates_are_brought_back(self):
        reader = self.export(EXPORTED)
        run_clear({"recettes"}, preview=False, closed=False)
        import_archive(reader, MERGE)
        self.assertEqual(Recipe.objects.get(name="Mojito").created_at, stamp(4))


class RecipesIdempotenceTests(LaneSectionsMixin, TestCase):
    """Importing a fresh export of the same database changes nothing: what
    catches a Decimal's places, a time zone or '' against None."""

    def setUp(self):
        super().setUp()
        build_recipes()
        self.reader = self.export(EXPORTED)

    def assert_nothing_happened(self, run):
        report = run.section("recettes")
        self.assertEqual(tally(run, "recettes", "recettes"), (0, 0, 0, 4))
        self.assertEqual(tally(run, "recettes", "ingrédients"), (0, 0, 0, 9))
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))

    def test_merge(self):
        before = db_fingerprint()
        self.assert_nothing_happened(import_archive(self.reader, MERGE))
        self.assertEqual(db_fingerprint(), before)

    def test_replace(self):
        before = db_fingerprint()
        self.assert_nothing_happened(import_archive(self.reader, REPLACE))
        self.assertEqual(db_fingerprint(), before)


class RecipesMergeAndReplaceTests(LaneSectionsMixin, TestCase):
    """One recipe changed here since the archive, one only here, one only in
    the archive."""

    def setUp(self):
        super().setUp()
        self.bar = build_recipes()
        self.reader = self.export(EXPORTED)
        Recipe.objects.filter(name="Alcool + Soda").update(selling_price_ttc=Decimal("8.50"))
        RecipeIngredient.objects.filter(recipe__name="Alcool + Soda", stock_type__name="Cola").update(quantity="0.25")
        self.spritz = make_recipe("Spritz", selling_price_ttc="7.00")
        make_ingredient(self.spritz, make_stock_type("Apérol"), quantity="0.06")
        RecipeIngredient.objects.filter(recipe__name="Menthe").delete()
        Recipe.objects.filter(name="Menthe").delete()

    def test_merge_keeps_what_differs_and_adds_what_is_missing(self):
        run = import_archive(self.reader, MERGE)
        report = run.section("recettes")
        self.assertEqual(tally(run, "recettes", "recettes"), (1, 0, 0, 2))
        self.assertEqual(
            report.conflicts,
            ["Recette « Alcool + Soda » : différente dans l'archive (prix de vente, ingrédients) — gardée telle quelle"],
        )
        self.assertEqual(Recipe.objects.get(name="Alcool + Soda").selling_price_ttc, Decimal("8.50"))
        self.assertTrue(Recipe.objects.filter(name="Spritz").exists())
        self.assertEqual(ingredients_of("Menthe"), [(0, "Menthe", "article", Decimal("12.0000"))])

    def test_replace_makes_the_recipes_exactly_the_archive(self):
        run = import_archive(self.reader, RECIPES_REPLACED)
        self.assertEqual(tally(run, "recettes", "recettes"), (1, 1, 1, 2))
        self.assertEqual(tally(run, "recettes", "ingrédients"), (5, 0, 5, 4))
        self.assertEqual(Recipe.objects.get(name="Alcool + Soda").selling_price_ttc, Decimal("8.00"))
        self.assertEqual(ingredients_of("Alcool + Soda")[3], (1, "Cola", "article", Decimal("0.2000")))
        self.assertFalse(Recipe.objects.filter(name="Spritz").exists())
        self.assertEqual(run.affected(), {"recettes"})

    def test_replace_puts_back_the_order_of_a_group(self):
        """Ingredients are re-created in the archive's order, not updated in
        place: the id order within a group is what ?v= counts in."""
        RecipeIngredient.objects.filter(recipe__name="Alcool + Soda", stock_type__name="Gin").delete()
        make_ingredient(Recipe.objects.get(name="Alcool + Soda"), make_stock_type("Whisky"), quantity="0.04")
        gin = RecipeIngredient.objects.filter(recipe__name="Alcool + Soda")
        self.assertEqual([i.source_name for i in gin.order_by("group", "id")][:2], ["Vodka", "Whisky"])
        import_archive(self.reader, RECIPES_REPLACED)
        self.assertEqual([row[1] for row in ingredients_of("Alcool + Soda")], ["Gin", "Vodka", "Tonic", "Cola"])

    def test_a_recipe_only_here_that_something_kept_needs_is_kept_and_said(self):
        base = make_recipe("Base spritz", selling_price_ttc="0")
        make_ingredient(base, make_stock_type("Prosecco"), quantity="0.1")
        make_ingredient(self.spritz, sub_recipe=base, quantity="1")
        PosProduct.objects.create(name="SPRITZ", recipe=self.spritz)
        tab = make_recipe("Planche", selling_price_ttc="15.00")
        document = SaleDocument.objects.create(sold_on="2026-09-01")
        SaleDocumentLine.objects.create(document=document, recipe=tab, quantity="2")
        hand = make_recipe("Pinte offerte", selling_price_ttc="0")
        RecipeSale.objects.create(recipe=hand, sold_on="2026-09-01", quantity=3, source=MANUAL_SALE_SOURCE)
        gone = make_recipe("Ancienne carte", selling_price_ttc="5.00", happy_hour_name="Ancienne HH")

        run = import_archive(self.reader, RECIPES_REPLACED)
        self.assertEqual(
            run.section("recettes").kept,
            [
                "Recette « Base spritz » : sous-recette de « Spritz », gardée",
                "Recette « Pinte offerte » : 1 vente saisie à la main s'y rapporte (Ventes non remplacées)",
                "Recette « Planche » : 1 ligne de bon de vente la cite (Ventes non remplacées)",
                "Recette « Spritz » : liée à 1 produit caisse (liens non remplacés)",
            ],
        )
        self.assertFalse(Recipe.objects.filter(pk=gone.pk).exists())
        self.assertEqual(tally(run, "recettes", "recettes")[2], 1)
        # Its happy-hour name went with it: the links' data, said in theirs.
        self.assertEqual(tally(run, "liens_ventes", "noms happy hour"), (0, 0, 1, 0))
        self.assertEqual(run.affected(), {"recettes", "liens_ventes"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)
        self.assertEqual(callbacks, [])
        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertNotEqual(db_fingerprint(), before)


class RecipesRefusalTests(LaneSectionsMixin, TestCase):
    """Each check of §7.4 on a hand-edited archive, imported onto a database
    whose recipes were cleared."""

    def setUp(self):
        super().setUp()
        build_recipes()
        self.reader = self.export(EXPORTED)
        run_clear({"recettes"}, preview=False, closed=False)

    def edited(self, change):
        def edit(payload):
            records = {record["name"]: record for record in payload["recipes"]}
            change(records)
            payload["recipes"] = list(records.values())
            return payload

        return self.open(forge(self.reader, recettes=edit))

    def test_a_missing_article_skips_the_whole_recipe(self):
        def change(records):
            records["Mojito"]["ingredients"][0]["article"] = "Rhum ambré"

        run = import_archive(self.edited(change), MERGE)
        self.assertEqual(run.section("recettes").skipped, ["Recette « Mojito » : article inconnu « Rhum ambré »"])
        self.assertFalse(Recipe.objects.filter(name="Mojito").exists())
        self.assertEqual(RecipeIngredient.objects.count(), 6)
        self.assertEqual(tally(run, "recettes", "recettes"), (3, 0, 0, 0))

    def test_a_sub_recipe_written_after_its_user_is_created_first(self):
        def change(records):
            syrup = records.pop("Sirop maison")
            records["Sirop maison"] = syrup  # now last, after the Mojito that uses it

        reader = self.edited(change)
        names = [record["name"] for record in reader.section("recettes").payload()["recipes"]]
        self.assertLess(names.index("Mojito"), names.index("Sirop maison"))
        run = import_archive(reader, MERGE)
        self.assertEqual(run.section("recettes").skipped, [])
        self.assertEqual(ingredients_of("Mojito")[1], (0, "Sirop maison", "recette", Decimal("0.0200")))

    def test_a_loop_is_refused_and_so_is_what_uses_it(self):
        def change(records):
            records["Sirop maison"]["ingredients"].append({"group": 1, "recipe": "Mojito", "quantity": "1.0000"})
            records["Menthe"]["ingredients"].append({"group": 1, "recipe": "Mojito", "quantity": "1.0000"})

        run = import_archive(self.edited(change), MERGE)
        self.assertEqual(
            run.section("recettes").skipped,
            [  # in the file's order, which is by name
                "Recette « Mojito » : sous-recettes en boucle : Mojito → Sirop maison → Mojito",
                "Recette « Sirop maison » : sous-recettes en boucle : Sirop maison → Mojito → Sirop maison",
                "Recette « Menthe » : sous-recette « Mojito » ignorée",
            ],
        )
        self.assertEqual(list(Recipe.objects.values_list("name", flat=True)), ["Alcool + Soda"])
        self.assertEqual(RecipeIngredient.objects.count(), 4)

    def test_a_recipe_using_a_skipped_one_says_so(self):
        def change(records):
            records["Sirop maison"]["vat_rate"] = "1.500"

        run = import_archive(self.edited(change), MERGE)
        self.assertEqual(
            run.section("recettes").skipped,
            [
                "Recette « Sirop maison » : TVA : Assurez-vous que cette valeur est inférieure ou égale à 1.",
                "Recette « Mojito » : sous-recette « Sirop maison » ignorée",
            ],
        )

    def test_values_the_fields_refuse(self):
        def change(records):
            records["Alcool + Soda"]["selling_price_ttc"] = "-1.00"
            records["Menthe"]["ingredients"][0]["quantity"] = "0.0000"
            records["Mojito"]["yield_unit"] = "LITRE"

        run = import_archive(self.edited(change), MERGE)
        self.assertEqual(
            run.section("recettes").skipped,
            [
                (
                    "Recette « Alcool + Soda » : prix de vente : Assurez-vous que cette valeur est supérieure ou "
                    "égale à 0."
                ),
                (
                    "Recette « Menthe » : ingrédient n° 1 : quantité : Assurez-vous que cette valeur est supérieure "
                    "ou égale à 0.0001."
                ),
                "Recette « Mojito » : « yield_unit » : valeur inconnue (« LITRE »)",
            ],
        )
        self.assertEqual(list(Recipe.objects.values_list("name", flat=True)), ["Sirop maison"])

    def test_an_ingredient_is_an_article_or_a_recipe(self):
        def change(records):
            records["Menthe"]["ingredients"][0]["recipe"] = "Sirop maison"
            records["Alcool + Soda"]["ingredients"][0].pop("article")
            records["Mojito"]["ingredients"][0]["group"] = -1

        run = import_archive(self.edited(change), MERGE)
        skipped = run.section("recettes").skipped
        self.assertIn(
            "Recette « Menthe » : ingrédient n° 1 : un article ou une sous-recette, jamais les deux ni aucun", skipped
        )
        self.assertIn(
            "Recette « Alcool + Soda » : ingrédient n° 1 : un article ou une sous-recette, jamais les deux ni aucun",
            skipped,
        )
        self.assertIn("Recette « Mojito » : ingrédient n° 1 : « group » : nombre positif attendu (« -1 »)", skipped)

    def test_a_record_without_a_name_or_twice_in_the_file(self):
        def edit(payload):
            payload["recipes"] += [{"category": "Sans nom"}, "illisible", dict(payload["recipes"][0])]
            return payload

        run = import_archive(self.open(forge(self.reader, recettes=edit)), MERGE)
        self.assertEqual(
            run.section("recettes").skipped,
            [
                "recette n° 5 de l'archive : sans nom",
                "recette n° 6 de l'archive : illisible",
                "Recette « Alcool + Soda » : deux fois dans l'archive, la seconde est ignorée",
            ],
        )
        self.assertEqual(Recipe.objects.count(), 4)

    def test_an_unknown_field_is_said_once(self):
        def change(records):
            for record in records.values():
                record["couleur"] = "verte"

        run = import_archive(self.edited(change), MERGE)
        self.assertEqual(run.section("recettes").notes, ["champ inconnu ignoré : couleur"])
        self.assertEqual(Recipe.objects.count(), 4)

    def test_a_file_without_its_list_is_refused_whole(self):
        reader = self.open(forge(self.reader, recettes={"recettes": []}))
        with self.assertRaisesMessage(ArchiveError, "recettes.json n'a pas de liste « recipes »"):
            import_archive(reader, MERGE)


class RecipesReplaceLoopTests(LaneSectionsMixin, TestCase):
    def test_replace_does_not_close_a_loop_through_a_recipe_of_this_database(self):
        """The file's graph has no loop, but a recipe it does not know uses
        the one it replaces: the form's own check (assert_no_cycle) refuses
        the ingredient that would close it."""
        build_recipes()
        reader = self.export(EXPORTED)
        syrup = Recipe.objects.get(name="Sirop maison")
        base = make_recipe("Base", selling_price_ttc="0")
        make_ingredient(base, sub_recipe=Recipe.objects.get(name="Mojito"), quantity="1")

        def edit(payload):
            for record in payload["recipes"]:
                if record["name"] == "Sirop maison":
                    record["ingredients"].append({"group": 1, "recipe": "Base", "quantity": "1.0000"})
            return payload

        run = import_archive(self.open(forge(reader, recettes=edit)), RECIPES_REPLACED)
        self.assertEqual(len(run.section("recettes").skipped), 1)
        self.assertIn("Recette « Sirop maison » : Impossible", run.section("recettes").skipped[0])
        self.assertEqual(syrup.ingredients.count(), 1)


class RecipesClearTests(LaneSectionsMixin, TestCase):
    def test_clear_takes_every_recipe_sub_recipes_included(self):
        build_recipes()
        PosProduct.objects.create(name="MOJITO", recipe=Recipe.objects.get(name="Mojito"))
        run = run_clear({"recettes", "liens_ventes", "ventes"}, preview=False)
        self.assertEqual(RecipesSection().count(), {"recettes": 0, "ingrédients": 0})
        self.assertEqual(tally(run, "recettes", "recettes"), (0, 0, 4, 0))
        self.assertEqual(tally(run, "recettes", "ingrédients"), (0, 0, 9, 0))
        # The links went first, in their own section; the till product, left
        # with no link and no day, went with the sales.
        self.assertEqual(tally(run, "liens_ventes", "liens retirés"), (0, 0, 1, 0))
        self.assertEqual(tally(run, "ventes", "produits caisse"), (0, 0, 1, 0))
        self.assertFalse(PosProduct.objects.exists())

    def test_cleared_alone_the_links_it_takes_are_links_not_till_products(self):
        """The recipes going take their links (SET_NULL): the till product
        stays, so the links' report counts a link, never a till product
        deleted - the UX review's double count, from this side."""
        build_recipes()
        PosProduct.objects.create(name="MOJITO", recipe=Recipe.objects.get(name="Mojito"))
        run = run_clear({"recettes"}, preview=False, closed=False)
        self.assertEqual(tally(run, "liens_ventes", "liens retirés"), (0, 0, 1, 0))
        self.assertNotIn("produits caisse", run.section("liens_ventes").tallies)
        self.assertEqual(PosProduct.objects.get(name="MOJITO").recipe, None)
