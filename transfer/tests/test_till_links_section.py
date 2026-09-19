"""« Liens recettes ↔ ventes » (§7.5), with the checks of §10.2.

The till data here is built the way the app builds it - `sync_pos_products`
and `record_sales` as the till import runs them, `links.link` / `set_aside`
as the « À lier » page does - so « the sales per recipe rebuilt from the
links » is compared with what the app itself writes, not with a copy of the
rebuild's own logic.
"""

from datetime import date

from django.db import transaction
from django.test import TestCase

from recipes import links
from recipes.models import PosProduct, PosProductDailyQuantity, Recipe, RecipeSale
from recipes.pos.laddition_xlsx import ParsedExport
from recipes.sales import record_sales
from recipes.tasks import sync_pos_products
from tests.factories import make_recipe
from transfer.archive import ArchiveError
from transfer.runner import run_clear
from transfer.sections.base import Strategy
from transfer.sections.till_links import TillLinksSection, laddition_rows
from transfer.tests.support import db_fingerprint, forge, import_archive, round_trip
from transfer.tests.test_recipes_section import LaneSectionsMixin, build_recipes, tally

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
EXPORTED = {"liens_ventes", "recettes", "associations", "fournisseurs"}
LINKS_REPLACED = {"liens_ventes": REPLACE, "recettes": MERGE, "associations": MERGE, "fournisseurs": MERGE}
#: What a released link is counted as: the link goes, the till product stays
#: (« Ventes »' data). Counted as « produits caisse » deleted, a clear of both
#: sections announced 279 till products gone for 211 (UX review, 19/09).
LINKS_GONE, IGNORED_GONE = "liens retirés", "statuts « ignoré » retirés"
RELINK_WHY = (
    "Fusionner lie (ou ignore) comme dans l'archive les produits caisse « à lier » ici : rien ne distingue un "
    "produit remis « à lier » à la main d'un produit jamais lié. Si l'un d'eux avait été remis « à lier » exprès, "
    "le détacher de nouveau (Recettes & ventes › À lier, « Rattachés » ou « Ignorés »), ou importer sans "
    "« Liens recettes ↔ ventes »."
)


def day(n: int) -> date:
    return date(2026, 9, n)


def products_deleted(run) -> int:
    """Till products the report says go, whichever section says it."""
    return sum(tally(run, section.key, "produits caisse")[2] for section in run.sections)


def till_export(entries) -> ParsedExport:
    """A parsed till export whose `products` mirror its `entries`, as
    parse_rows builds both from the same rows."""
    products: dict = {}
    for name, sold_on, quantity in entries:
        info = products.setdefault(
            name, {"quantity": 0, "category": "Liquide (Alcool)", "typology": "Cocktails", "first": sold_on, "last": sold_on}
        )
        info["quantity"] += quantity
        info["first"] = min(info["first"], sold_on)
        info["last"] = max(info["last"], sold_on)
    return ParsedExport(products=products, entries=list(entries))


def run_till_import(entries) -> None:
    """What tasks.import_laddition_sales_task does once the file is read."""
    export = till_export(entries)
    sync_pos_products(export)
    record_sales(export.entries, source="laddition")


def build_till():
    """The recipes, then a till import: « Mojito HH » is linked by the
    import itself (the recipe's happy-hour name), « Pinte IPA » too (a
    recipe's name); « MOJITO CLASSIQUE » is linked by hand, « CAFÉ » ignored,
    « PLANCHE » left to link. « Pinte IPA HH » is a happy-hour name no till
    product carries yet."""
    build_recipes()
    make_recipe("Pinte IPA", selling_price_ttc="7.00", happy_hour_name="Pinte IPA HH")
    make_recipe("Planche apéro", selling_price_ttc="15.00")
    run_till_import([
        ("MOJITO CLASSIQUE", day(1), 12), ("MOJITO CLASSIQUE", day(2), 8),
        ("Mojito HH", day(1), 5),
        ("Pinte IPA", day(1), 30), ("Pinte IPA", day(3), 25),
        ("CAFÉ", day(1), 40),
        ("PLANCHE", day(2), 2),
    ])
    links.link(PosProduct.objects.get(name="MOJITO CLASSIQUE"), Recipe.objects.get(name="Mojito"))
    links.set_aside(PosProduct.objects.get(name="CAFÉ"), ignored=True)


def link_of(name: str) -> tuple:
    product = PosProduct.objects.get(name=name)
    return (product.recipe.name if product.recipe_id else None, product.ignored)


def sales_of(recipe: str) -> dict[str, int]:
    return {sold_on: quantity for name, sold_on, quantity in laddition_rows() if name == recipe}


class _Undo(Exception):
    pass


def undone(action):
    """What `action` leaves in the database, then rolled back."""
    try:
        with transaction.atomic():
            action()
            result = (laddition_rows(), sorted(Recipe.objects.exclude(happy_hour_name="").values_list("name", "happy_hour_name")))
            raise _Undo
    except _Undo:
        return result


class TillLinksRoundTripTests(LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_till()

    def test_the_fixture_is_what_the_app_made(self):
        self.assertEqual(link_of("Mojito HH"), ("Mojito", False))
        self.assertEqual(link_of("Pinte IPA"), ("Pinte IPA", False))
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})
        self.assertEqual(
            TillLinksSection().count(), {"produits caisse liés": 3, "ignorés": 1, "noms happy hour": 2}
        )

    def after_clear(self):
        self.assertEqual(TillLinksSection().count(), {"produits caisse liés": 0, "ignorés": 0, "noms happy hour": 0})
        self.assertEqual(laddition_rows(), [])
        # The till products and their days are « Ventes »': they stay.
        self.assertEqual(PosProduct.objects.count(), 5)
        self.assertEqual(PosProductDailyQuantity.objects.count(), 7)

    def test_round_trip_with_merge(self):
        before, after = round_trip({"liens_ventes"}, MERGE, after_clear=self.after_clear)
        self.assertEqual(set(before), EXPORTED)
        self.assertEqual(after, before)

    def test_round_trip_with_replace(self):
        before, after = round_trip({"liens_ventes"}, REPLACE, after_clear=self.after_clear)
        self.assertEqual(after, before)

    def test_a_happy_hour_link_comes_back_and_its_sales_fold_into_the_recipe(self):
        round_trip({"liens_ventes"}, MERGE)
        self.assertEqual(link_of("Mojito HH"), ("Mojito", False))
        self.assertEqual(Recipe.objects.get(name="Mojito").happy_hour_name, "Mojito HH")
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})

    def test_a_happy_hour_name_with_no_till_product_comes_back(self):
        self.assertFalse(PosProduct.objects.filter(name="Pinte IPA HH").exists())
        round_trip({"liens_ventes"}, MERGE)
        self.assertEqual(Recipe.objects.get(name="Pinte IPA").happy_hour_name, "Pinte IPA HH")

    def test_the_sales_are_those_links_link_and_set_aside_give(self):
        """Rebuilt through the rebuild, not through links.link: the result
        must still be the app's own, row for row - for links made, moved,
        undone and a product set aside with its happy-hour name."""
        state_before = undone(lambda: None)
        mojito_classique = PosProduct.objects.get(name="MOJITO CLASSIQUE")
        archive_before = self.export(EXPORTED)

        def by_hand():
            links.set_aside(PosProduct.objects.get(name="MOJITO CLASSIQUE"), ignored=False)
            links.link(PosProduct.objects.get(name="PLANCHE"), Recipe.objects.get(name="Planche apéro"))
            links.link(PosProduct.objects.get(name="Pinte IPA"), Recipe.objects.get(name="Mojito"))
            links.set_aside(PosProduct.objects.get(name="Mojito HH"), ignored=True)

        state_by_hand = undone(by_hand)
        self.assertNotEqual(state_by_hand, state_before)
        by_hand()
        archive_by_hand = self.export(EXPORTED)
        self.assertEqual(Recipe.objects.get(name="Mojito").happy_hour_name, "")  # set_aside took it

        import_archive(archive_before, LINKS_REPLACED)
        self.assertEqual(undone(lambda: None), state_before)
        self.assertEqual(link_of("MOJITO CLASSIQUE"), ("Mojito", False))
        self.assertEqual(PosProduct.objects.get(name="MOJITO CLASSIQUE").pk, mojito_classique.pk)

        import_archive(archive_by_hand, LINKS_REPLACED)
        self.assertEqual(undone(lambda: None), state_by_hand)
        self.assertEqual(link_of("MOJITO CLASSIQUE"), (None, False))
        self.assertEqual(link_of("Mojito HH"), (None, True))


class TillLinksIdempotenceTests(LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_till()
        self.reader = self.export(EXPORTED)

    def assert_nothing_happened(self, run):
        report = run.section("liens_ventes")
        self.assertEqual(tally(run, "liens_ventes", "produits caisse"), (0, 0, 0, 4))
        self.assertEqual(tally(run, "liens_ventes", "noms happy hour"), (0, 0, 0, 2))
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))
        self.assertEqual(run.rebuilt["ventes par recette"], 0)

    def test_merge(self):
        before = db_fingerprint()
        self.assert_nothing_happened(import_archive(self.reader, MERGE))
        self.assertEqual(db_fingerprint(), before)

    def test_replace(self):
        before = db_fingerprint()
        self.assert_nothing_happened(import_archive(self.reader, REPLACE))
        self.assertEqual(db_fingerprint(), before)


class TillLinksMergeAndReplaceTests(LaneSectionsMixin, TestCase):
    """Since the archive: « Pinte IPA » linked elsewhere here, « PLANCHE »
    linked here only, « CAFÉ » (ignored in the archive) gone from here."""

    def setUp(self):
        super().setUp()
        build_till()
        self.reader = self.export(EXPORTED)
        links.link(PosProduct.objects.get(name="Pinte IPA"), Recipe.objects.get(name="Alcool + Soda"))
        links.link(PosProduct.objects.get(name="PLANCHE"), Recipe.objects.get(name="Planche apéro"))
        PosProduct.objects.filter(name="CAFÉ").delete()

    def test_merge(self):
        run = import_archive(self.reader, MERGE)
        report = run.section("liens_ventes")
        self.assertEqual(tally(run, "liens_ventes", "produits caisse"), (1, 0, 0, 2))
        self.assertEqual(
            report.conflicts,
            [
                (
                    "Produit caisse « Pinte IPA » : lié à « Alcool + Soda » ici, lié à « Pinte IPA » dans "
                    "l'archive — gardé tel quel"
                ),
            ],
        )
        self.assertEqual(link_of("Pinte IPA"), ("Alcool + Soda", False))
        self.assertEqual(link_of("PLANCHE"), ("Planche apéro", False))
        # Created as the archive has it: ignored, no day, no total.
        cafe = PosProduct.objects.get(name="CAFÉ")
        self.assertEqual((cafe.ignored, cafe.recipe, cafe.total_quantity, cafe.first_seen), (True, None, 0, None))
        self.assertEqual((cafe.category, cafe.typology), ("Liquide (Alcool)", "Cocktails"))

    def test_replace(self):
        run = import_archive(self.reader, LINKS_REPLACED)
        # PLANCHE's link goes; PLANCHE itself stays, « à lier ».
        self.assertEqual(tally(run, "liens_ventes", "produits caisse"), (1, 1, 0, 2))
        self.assertEqual(tally(run, "liens_ventes", LINKS_GONE), (0, 0, 1, 0))
        self.assertEqual(products_deleted(run), 0)
        self.assertIn("1 produit caisse remis « à lier »", run.section("liens_ventes").notes)
        self.assertEqual(link_of("Pinte IPA"), ("Pinte IPA", False))
        self.assertEqual(link_of("PLANCHE"), (None, False))
        # The recipes on both ends of a moved link, and the one a prune
        # released, are rebuilt: no sale is left on a recipe no link feeds.
        self.assertEqual(sales_of("Pinte IPA"), {"2026-09-01": 30, "2026-09-03": 25})
        self.assertEqual(sales_of("Alcool + Soda"), {})
        self.assertEqual(sales_of("Planche apéro"), {})
        self.assertEqual(run.rebuilt["ventes par recette"], 3)
        self.assertEqual(run.affected(), {"liens_ventes"})

    def test_merge_fills_a_blank(self):
        links.set_aside(PosProduct.objects.get(name="Pinte IPA"), ignored=False)
        PosProduct.objects.filter(name="Pinte IPA").update(category="")
        Recipe.objects.filter(name="Pinte IPA").update(happy_hour_name="")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(link_of("Pinte IPA"), ("Pinte IPA", False))
        self.assertEqual(PosProduct.objects.get(name="Pinte IPA").category, "Liquide (Alcool)")
        self.assertEqual(Recipe.objects.get(name="Pinte IPA").happy_hour_name, "Pinte IPA HH")
        self.assertEqual(tally(run, "liens_ventes", "noms happy hour"), (1, 0, 0, 1))
        self.assertEqual(sales_of("Pinte IPA"), {"2026-09-01": 30, "2026-09-03": 25})

    def test_a_different_category_is_a_conflict_then_replaced(self):
        PosProduct.objects.filter(name="MOJITO CLASSIQUE").update(category="Softs")
        run = import_archive(self.reader, MERGE)
        self.assertIn(
            "Produit caisse « MOJITO CLASSIQUE » : différent dans l'archive (catégorie) — gardé tel quel",
            run.section("liens_ventes").conflicts,
        )
        self.assertEqual(PosProduct.objects.get(name="MOJITO CLASSIQUE").category, "Softs")
        import_archive(self.reader, LINKS_REPLACED)
        self.assertEqual(PosProduct.objects.get(name="MOJITO CLASSIQUE").category, "Liquide (Alcool)")

    def test_a_different_happy_hour_name_is_a_conflict_then_replaced(self):
        Recipe.objects.filter(name="Mojito").update(happy_hour_name="Mojito Happy")
        run = import_archive(self.reader, MERGE)
        self.assertIn(
            "Recette « Mojito » : nom happy hour « Mojito Happy » ici, « Mojito HH » dans l'archive — gardé tel quel",
            run.section("liens_ventes").conflicts,
        )
        self.assertEqual(Recipe.objects.get(name="Mojito").happy_hour_name, "Mojito Happy")
        run = import_archive(self.reader, LINKS_REPLACED)
        self.assertEqual(Recipe.objects.get(name="Mojito").happy_hour_name, "Mojito HH")
        self.assertEqual(tally(run, "liens_ventes", "noms happy hour"), (0, 1, 0, 1))

    def test_replace_swaps_two_happy_hour_names(self):
        """Checked against the state the import ends in, not the one it
        starts from: each name is another recipe's until the other moves."""
        Recipe.objects.filter(name="Mojito").update(happy_hour_name="Pinte IPA HH")
        Recipe.objects.filter(name="Pinte IPA").update(happy_hour_name="Mojito HH")
        run = import_archive(self.reader, LINKS_REPLACED)
        self.assertEqual(run.section("liens_ventes").skipped, [])
        self.assertEqual(Recipe.objects.get(name="Mojito").happy_hour_name, "Mojito HH")
        self.assertEqual(Recipe.objects.get(name="Pinte IPA").happy_hour_name, "Pinte IPA HH")
        self.assertEqual(tally(run, "liens_ventes", "noms happy hour"), (0, 2, 0, 0))

    def test_replace_blanks_a_happy_hour_name_the_archive_does_not_have(self):
        Recipe.objects.filter(name="Planche apéro").update(happy_hour_name="Planche HH")
        run = import_archive(self.reader, LINKS_REPLACED)
        self.assertEqual(Recipe.objects.get(name="Planche apéro").happy_hour_name, "")
        self.assertEqual(tally(run, "liens_ventes", "noms happy hour"), (0, 0, 1, 2))

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = import_archive(self.reader, LINKS_REPLACED, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(callbacks, [])
        confirmed = import_archive(self.reader, LINKS_REPLACED)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertNotEqual(db_fingerprint(), before)


class TillLinksDecisionsHereTests(LaneSectionsMixin, TestCase):
    """What a person decided on the « À lier » page after the export, then
    the export merged back - every section « Fusionner », as in the
    integrity review (« 1664 0% » set back to « à lier », 88 sales back)."""

    def setUp(self):
        super().setUp()
        build_till()
        self.reader = self.export(EXPORTED | {"ventes"})

    def test_a_product_sent_back_to_link_is_linked_again_and_named(self):
        """Nothing on a till product tells « remis à lier » from « never
        linked » (links.set_aside keeps no trace), and after « Effacer » the
        links must come back: so the link is filled in, as §7.5 says - and
        named, where the report only counted « modifié 1 »."""
        links.set_aside(PosProduct.objects.get(name="MOJITO CLASSIQUE"), ignored=False)
        links.set_aside(PosProduct.objects.get(name="CAFÉ"), ignored=False)
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 5})
        run = import_archive(self.reader, MERGE)
        report = run.section("liens_ventes")
        self.assertEqual(link_of("MOJITO CLASSIQUE"), ("Mojito", False))
        self.assertEqual(link_of("CAFÉ"), (None, True))
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})
        self.assertEqual(tally(run, "liens_ventes", "produits caisse"), (0, 2, 0, 2))
        self.assertEqual(
            report.notes,
            [
                RELINK_WHY,
                "Produit caisse « CAFÉ » : « à lier » ici, ignoré comme dans l'archive",
                "Produit caisse « MOJITO CLASSIQUE » : « à lier » ici, lié à « Mojito » comme dans l'archive",
            ],
        )
        self.assertEqual(report.conflicts, [])

    def test_the_preview_names_it_before_anything_is_written(self):
        links.set_aside(PosProduct.objects.get(name="MOJITO CLASSIQUE"), ignored=False)
        preview = import_archive(self.reader, MERGE, preview=True)
        self.assertIn(RELINK_WHY, preview.section("liens_ventes").notes)
        self.assertEqual(link_of("MOJITO CLASSIQUE"), (None, False))

    def test_a_product_ignored_here_is_never_linked_by_a_merge(self):
        """« Ignoré » is on the model: a decision this database holds, so a
        conflict, kept - its sales stay off the recipe."""
        links.set_aside(PosProduct.objects.get(name="MOJITO CLASSIQUE"), ignored=True)
        run = import_archive(self.reader, MERGE)
        report = run.section("liens_ventes")
        self.assertEqual(link_of("MOJITO CLASSIQUE"), (None, True))
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 5})
        self.assertEqual(
            report.conflicts,
            ["Produit caisse « MOJITO CLASSIQUE » : ignoré ici, lié à « Mojito » dans l'archive — gardé tel quel"],
        )
        self.assertEqual(report.notes, [])

    def test_replace_links_it_without_the_merge_warning(self):
        """« Remplacer » is asked to make the links the archive's: that is
        what it does, and nothing is ambiguous about it."""
        links.set_aside(PosProduct.objects.get(name="MOJITO CLASSIQUE"), ignored=False)
        run = import_archive(self.reader, LINKS_REPLACED)
        self.assertEqual(link_of("MOJITO CLASSIQUE"), ("Mojito", False))
        self.assertEqual(run.section("liens_ventes").notes, [])


class TillLinksWithSalesTests(LaneSectionsMixin, TestCase):
    """A till product linked here but never sold, which the archive does not
    link: the links' prune releases it after the sales' prune has run."""

    def setUp(self):
        super().setUp()
        build_till()
        self.reader = self.export(EXPORTED | {"ventes"})
        PosProduct.objects.create(name="PLANCHE XL", recipe=Recipe.objects.get(name="Planche apéro"))

    def test_it_goes_when_the_sales_are_replaced_too(self):
        run = import_archive(self.reader, {**LINKS_REPLACED, "ventes": REPLACE})
        self.assertFalse(PosProduct.objects.filter(name="PLANCHE XL").exists())
        self.assertEqual(tally(run, "ventes", "produits caisse"), (0, 0, 1, 5))
        self.assertEqual(tally(run, "liens_ventes", "produits caisse"), (0, 0, 0, 4))
        self.assertEqual(tally(run, "liens_ventes", LINKS_GONE), (0, 0, 1, 0))
        # One till product went, and the report says one - not one per section.
        self.assertEqual(products_deleted(run), 1)

    def test_it_stays_to_link_when_the_sales_are_merged(self):
        import_archive(self.reader, {**LINKS_REPLACED, "ventes": MERGE})
        self.assertEqual(link_of("PLANCHE XL"), (None, False))


class TillLinksRefusalTests(LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_till()
        self.reader = self.export(EXPORTED)
        run_clear({"liens_ventes"}, preview=False)

    def edited(self, **changes):
        def edit(payload):
            for name, change in changes.items():
                change(payload[name])
            return payload

        return self.open(forge(self.reader, liens_ventes=edit))

    def test_a_link_to_a_recipe_unknown_here(self):
        def change(products):
            products[0]["recipe"] = "Mojito fraise"

        run = import_archive(self.edited(till_products=change), MERGE)
        self.assertEqual(
            run.section("liens_ventes").skipped, ["Produit caisse « CAFÉ » : recette inconnue « Mojito fraise »"]
        )
        self.assertEqual(link_of("CAFÉ"), (None, False))

    def test_linked_and_ignored_at_once_or_neither(self):
        def change(products):
            products[0]["recipe"] = "Planche apéro"  # CAFÉ, ignored
            products[1]["ignored"] = True       # MOJITO CLASSIQUE, linked
            products.append({"name": "PLANCHE", "recipe": None, "ignored": False})
            products.append({"name": "SANS NOM"})
            products.append(["illisible"])

        run = import_archive(self.edited(till_products=change), MERGE)
        self.assertEqual(
            run.section("liens_ventes").skipped,
            [
                "Produit caisse « CAFÉ » : lié à une recette et ignoré à la fois",
                "Produit caisse « MOJITO CLASSIQUE » : lié à une recette et ignoré à la fois",
                "Produit caisse « PLANCHE » : ni lié à une recette ni ignoré : rien à importer",
                "Produit caisse « SANS NOM » : ni lié à une recette ni ignoré : rien à importer",
                "produit caisse n° 7 de l'archive : illisible",
            ],
        )
        self.assertFalse(PosProduct.objects.filter(name="SANS NOM").exists())

    def test_a_happy_hour_name_clashing_with_another_recipe_is_skipped(self):
        def change(names):
            names.append({"recipe": "Alcool + Soda", "name": "menthe"})
            names.append({"recipe": "Menthe", "name": "Menthe"})
            names.append({"recipe": "Planche apéro", "name": "Mojito HH"})
            names.append({"recipe": "Spritz", "name": "Spritz HH"})

        run = import_archive(self.edited(happy_hour_names=change), MERGE)
        self.assertEqual(
            run.section("liens_ventes").skipped,
            [  # read first, then set in the file's order against what is already set
                "Nom happy hour « Spritz HH » : recette inconnue « Spritz »",
                (
                    "Recette « Alcool + Soda » : nom happy hour « menthe » refusé : nom happy hour : Déjà utilisé "
                    "par la recette « Menthe »."
                ),
                (
                    "Recette « Menthe » : nom happy hour « Menthe » refusé : nom happy hour : Identique au nom de "
                    "la recette - laissez vide si la caisse n'a qu'un seul nom."
                ),
                (
                    "Recette « Planche apéro » : nom happy hour « Mojito HH » refusé : nom happy hour : Déjà "
                    "utilisé par la recette « Mojito »."
                ),
            ],
        )
        self.assertEqual(
            sorted(Recipe.objects.exclude(happy_hour_name="").values_list("name", "happy_hour_name")),
            [("Mojito", "Mojito HH"), ("Pinte IPA", "Pinte IPA HH")],
        )

    def test_a_refused_name_under_replace_leaves_the_one_here(self):
        Recipe.objects.filter(name="Planche apéro").update(happy_hour_name="Planche HH")

        def change(names):
            names.append({"recipe": "Planche apéro", "name": "Menthe"})

        run = import_archive(self.edited(happy_hour_names=change), LINKS_REPLACED)
        self.assertEqual(len(run.section("liens_ventes").skipped), 1)
        self.assertEqual(Recipe.objects.get(name="Planche apéro").happy_hour_name, "Planche HH")

    def test_a_file_without_its_lists_is_refused_whole(self):
        reader = self.open(forge(self.reader, liens_ventes={"till_products": []}))
        with self.assertRaisesMessage(ArchiveError, "liens_ventes.json n'a pas de liste « happy_hour_names »"):
            import_archive(reader, MERGE)


class TillLinksClearTests(LaneSectionsMixin, TestCase):
    def test_clear_rebuilds_to_no_sale_and_keeps_the_till_data(self):
        build_till()
        days = PosProductDailyQuantity.objects.count()
        run = run_clear({"liens_ventes"}, preview=False)
        self.assertEqual(TillLinksSection().count(), {"produits caisse liés": 0, "ignorés": 0, "noms happy hour": 0})
        self.assertEqual(RecipeSale.objects.filter(source="laddition").count(), 0)
        self.assertEqual(PosProduct.objects.count(), 5)
        self.assertEqual(PosProductDailyQuantity.objects.count(), days)
        # Three links and one « ignoré » go; no till product does.
        self.assertEqual(tally(run, "liens_ventes", LINKS_GONE), (0, 0, 3, 0))
        self.assertEqual(tally(run, "liens_ventes", IGNORED_GONE), (0, 0, 1, 0))
        self.assertNotIn("produits caisse", run.section("liens_ventes").tallies)
        self.assertEqual(tally(run, "liens_ventes", "noms happy hour"), (0, 0, 2, 0))
        self.assertIn(
            "4 produits caisse remis « à lier » (leurs ventes par jour restent)", run.section("liens_ventes").notes
        )
        # « Pinte IPA » is named like its recipe: the next till import links
        # it again by itself, and the page says so.
        self.assertIn(
            "Le prochain import de la caisse relie de nouveau d'office les produits caisse qui portent le nom "
            "d'une recette (1 des 3 liens d'aujourd'hui).",
            run.section("liens_ventes").notes,
        )

    def test_cleared_with_the_sales_no_empty_till_product_is_left(self):
        """« Ventes » clears first and keeps the linked products as the links';
        released afterwards with no day left, they go by its rule."""
        build_till()
        products = PosProduct.objects.count()
        run = run_clear({"liens_ventes", "ventes"}, preview=False)
        self.assertFalse(PosProduct.objects.exists())
        # The UX review's count: « À supprimer » added up over the table is
        # the till products there were (5 here, 211 on the real copy), not
        # those plus the links (279).
        self.assertEqual(products_deleted(run), products)
        self.assertEqual(tally(run, "ventes", "produits caisse"), (0, 0, 5, 0))
        self.assertEqual(tally(run, "liens_ventes", LINKS_GONE), (0, 0, 3, 0))
        self.assertEqual(tally(run, "liens_ventes", IGNORED_GONE), (0, 0, 1, 0))

    def test_a_clear_preview_changes_nothing(self):
        build_till()
        before = db_fingerprint()
        preview = run_clear({"liens_ventes"}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(preview.outcome(), run_clear({"liens_ventes"}, preview=False).outcome())
