"""« Ventes » (§7.8), with the checks of §10.2.

The till's days are built by the till import's own code (`sync_pos_products`
+ `record_sales`, see test_till_links_section.build_till), so the totals and
the sales per recipe an import rebuilds are compared with what the app wrote,
not with a copy of the rebuild.
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from inventory.models import StockType
from recipes.forms import MANUAL_SALE_SOURCE
from recipes.models import (
    PosProduct,
    PosProductDailyQuantity,
    Recipe,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
)
from transfer.archive import ArchiveError
from transfer.runner import run_clear
from transfer.sections.base import Strategy
from transfer.sections.sales import SalesSection
from transfer.sections.till_links import laddition_rows
from transfer.tests.support import db_fingerprint, forge, import_archive, round_trip
from transfer.tests.test_recipes_section import LaneSectionsMixin, tally
from transfer.tests.test_till_links_section import build_till, day, link_of, sales_of

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
EXPORTED = {"ventes", "recettes", "associations", "fournisseurs"}
SALES_REPLACED = {"ventes": REPLACE, "recettes": MERGE, "associations": MERGE, "fournisseurs": MERGE}
#: What the per-day rows are, in words: one row is one till product on one
#: day. Called « jours de vente (caisse) », 15 850 of them read as 43 years
#: of sales (UX review, 19/09) - the real copy holds 662 days.
QUANTITIES, DAYS = "quantités par produit et par jour (caisse)", "jours de caisse"


def moment(hour: int) -> datetime:
    return datetime(2026, 9, 10, hour, 5, 30, 250000, tzinfo=dt_timezone.utc)


def add_document(sold_on, lines, reference="", note="", created=None) -> SaleDocument:
    document = SaleDocument.objects.create(sold_on=sold_on, reference=reference, note=note)
    for target, quantity, price in lines:
        kind = "recipe" if isinstance(target, Recipe) else "stock_type"
        SaleDocumentLine.objects.create(document=document, quantity=Decimal(quantity), unit_price_ttc=price, **{kind: target})
    if created is not None:
        SaleDocument.objects.filter(pk=document.pk).update(created_at=created)
    return document


def build_sales():
    """The till's days (build_till), two sales typed in by hand, a document
    with a recipe line and an article line, and two identical documents (a
    tab typed twice on purpose: two, not one)."""
    build_till()
    mojito, soda, mint = (Recipe.objects.get(name=name) for name in ("Mojito", "Alcool + Soda", "Menthe"))
    for recipe, quantity, hour in ((mojito, 3, 8), (soda, 2, 9)):
        sale = RecipeSale.objects.create(recipe=recipe, sold_on=day(5), quantity=quantity, source=MANUAL_SALE_SOURCE)
        RecipeSale.objects.filter(pk=sale.pk).update(recorded_at=moment(hour))
    add_document(
        day(6), [(mojito, "10", Decimal("7.50")), (StockType.objects.get(name="Gin"), "0.7", None)],
        reference="SOIRÉE-1", note="Anniversaire", created=moment(10),
    )
    add_document(day(7), [(mint, "1", None)], created=moment(11))
    add_document(day(7), [(mint, "1", None)], created=moment(12))


def till_totals() -> dict[str, tuple]:
    return {
        product.name: (product.total_quantity, product.first_seen, product.last_seen)
        for product in PosProduct.objects.all()
    }


class SalesRoundTripTests(LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_sales()

    def test_counts(self):
        """Seven rows - five till products over three days: the rows are
        quantities per product and per day, and the days are counted apart."""
        self.assertEqual(
            SalesSection().count(),
            {QUANTITIES: 7, DAYS: 3, "produits caisse": 5, "ventes saisies": 2, "bons de vente": 3},
        )

    def test_the_archive_counts_as_the_database_does(self):
        """The import tab prints the archive's counts beside this database's:
        the same labels and the same rule, or one side reads as more."""
        self.assertEqual(
            self.export(EXPORTED).counts("ventes"),
            {QUANTITIES: 7, DAYS: 3, "produits caisse": 5, "ventes saisies": 2, "bons de vente": 3},
        )

    def after_clear(self):
        self.assertEqual(
            SalesSection().count(),
            {QUANTITIES: 0, DAYS: 0, "produits caisse": 0, "ventes saisies": 0, "bons de vente": 0},
        )
        self.assertEqual(RecipeSale.objects.count(), 0)
        # Linked and ignored till products are the links': kept, at zero.
        self.assertEqual(
            till_totals(),
            {name: (0, None, None) for name in ("MOJITO CLASSIQUE", "Mojito HH", "Pinte IPA", "CAFÉ")},
        )

    def test_round_trip_with_merge(self):
        totals, sales = till_totals(), laddition_rows()
        before, after = round_trip({"ventes"}, MERGE, after_clear=self.after_clear)
        self.assertEqual(set(before), EXPORTED)
        self.assertEqual(after, before)
        # Rebuilt, not copied - and equal to what the till import wrote.
        self.assertEqual(till_totals(), totals)
        self.assertEqual(laddition_rows(), sales)
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})
        self.assertEqual(PosProduct.objects.get(name="PLANCHE").recipe, None)

    def test_round_trip_with_replace(self):
        before, after = round_trip({"ventes"}, REPLACE, after_clear=self.after_clear)
        self.assertEqual(after, before)

    def test_round_trip_with_the_links(self):
        """Both cleared, both imported: the sales per recipe are rebuilt
        through the links the same run brings back."""
        before, after = round_trip({"ventes", "liens_ventes"}, MERGE)
        self.assertEqual(after, before)
        self.assertEqual(sales_of("Pinte IPA"), {"2026-09-01": 30, "2026-09-03": 25})

    def test_the_sales_per_recipe_follow_the_links_present(self):
        """Without the links in the run, the days come back but sell no
        recipe - the till products arrive « à lier » - until the links do."""
        reader = self.export(EXPORTED | {"liens_ventes"})
        run_clear({"ventes", "liens_ventes"}, preview=False)
        import_archive(reader, {"ventes": MERGE, "recettes": MERGE, "associations": MERGE, "fournisseurs": MERGE})
        self.assertEqual(PosProductDailyQuantity.objects.count(), 7)
        self.assertEqual(laddition_rows(), [])
        self.assertEqual(till_totals()["MOJITO CLASSIQUE"], (20, day(1), day(2)))
        import_archive(reader, MERGE)
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})

    def test_a_document_with_a_recipe_line_and_an_article_line(self):
        round_trip({"ventes"}, MERGE)
        document = SaleDocument.objects.get(reference="SOIRÉE-1")
        self.assertEqual((document.sold_on, document.note, document.created_at), (day(6), "Anniversaire", moment(10)))
        self.assertEqual(
            [(line.source_name, line.quantity, line.unit_price_ttc) for line in document.lines.order_by("id")],
            [("Mojito", Decimal("10.0000"), Decimal("7.50")), ("Gin", Decimal("0.7000"), None)],
        )
        self.assertEqual(SaleDocument.objects.filter(sold_on=day(7)).count(), 2)


class SalesIdempotenceTests(LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_sales()
        self.reader = self.export(EXPORTED)

    def assert_nothing_happened(self, run):
        report = run.section("ventes")
        self.assertEqual(tally(run, "ventes", "produits caisse"), (0, 0, 0, 5))
        self.assertEqual(tally(run, "ventes", QUANTITIES), (0, 0, 0, 7))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (0, 0, 0, 2))
        self.assertEqual(tally(run, "ventes", "bons de vente"), (0, 0, 0, 3))
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))
        self.assertEqual(run.rebuilt, {"mouvements de stock": 0, "statuts de factures": 0, "ventes par recette": 0, "produits caisse": 0})

    def test_merge(self):
        before = db_fingerprint()
        self.assert_nothing_happened(import_archive(self.reader, MERGE))
        self.assertEqual(db_fingerprint(), before)

    def test_replace(self):
        before = db_fingerprint()
        self.assert_nothing_happened(import_archive(self.reader, REPLACE))
        self.assertEqual(db_fingerprint(), before)


class SalesMergeAndReplaceTests(LaneSectionsMixin, TestCase):
    """Since the archive: a day's quantity changed here, a day and a till
    product only here, a day gone from here; the same for a hand-typed sale
    and a document."""

    def setUp(self):
        super().setUp()
        build_sales()
        self.reader = self.export(EXPORTED)
        PosProductDailyQuantity.objects.filter(product__name="MOJITO CLASSIQUE", sold_on=day(2)).update(quantity=9)
        extra = PosProduct.objects.create(name="PLANCHE XL")
        PosProductDailyQuantity.objects.create(product=extra, sold_on=day(4), quantity=1)
        PosProductDailyQuantity.objects.filter(product__name="Pinte IPA", sold_on=day(3)).delete()
        RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE, recipe__name="Mojito").update(quantity=4)
        RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE, recipe__name="Alcool + Soda").delete()
        RecipeSale.objects.create(
            recipe=Recipe.objects.get(name="Menthe"), sold_on=day(5), quantity=1, source=MANUAL_SALE_SOURCE
        )
        SaleDocument.objects.filter(reference="SOIRÉE-1").delete()
        add_document(day(8), [(Recipe.objects.get(name="Menthe"), "2", None)], reference="ICI")

    def test_merge(self):
        run = import_archive(self.reader, MERGE)
        report = run.section("ventes")
        self.assertEqual(tally(run, "ventes", QUANTITIES), (1, 0, 0, 5))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (1, 0, 0, 0))
        self.assertEqual(tally(run, "ventes", "bons de vente"), (1, 0, 0, 2))
        self.assertEqual(
            report.conflicts,
            [
                "Produit caisse « MOJITO CLASSIQUE » le 02/09/2026 : 9 ici, 8 dans l'archive — gardé tel quel",
                "Vente saisie de « Mojito » le 05/09/2026 : 4 ici, 3 dans l'archive — gardée telle quelle",
            ],
        )
        self.assertIn(
            "Les bons de vente sont rapprochés sur tout leur contenu : un bon modifié d'un côté arrive comme un "
            "nouveau bon.",
            report.notes,
        )
        self.assertEqual(till_totals()["Pinte IPA"], (55, day(1), day(3)))
        self.assertEqual(sales_of("Pinte IPA"), {"2026-09-01": 30, "2026-09-03": 25})
        self.assertEqual(till_totals()["PLANCHE XL"], (0, None, None))  # only here: untouched
        self.assertEqual(SaleDocument.objects.count(), 4)

    def test_replace(self):
        run = import_archive(self.reader, SALES_REPLACED)
        self.assertEqual(tally(run, "ventes", QUANTITIES), (1, 1, 1, 5))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (1, 1, 1, 0))
        self.assertEqual(tally(run, "ventes", "bons de vente"), (1, 0, 1, 2))
        # Left with no day and no link: pure data, gone.
        self.assertEqual(tally(run, "ventes", "produits caisse")[2], 1)
        self.assertFalse(PosProduct.objects.filter(name="PLANCHE XL").exists())
        self.assertEqual(till_totals()["MOJITO CLASSIQUE"], (20, day(1), day(2)))
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})
        self.assertEqual(
            sorted(RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).values_list("recipe__name", "quantity")),
            [("Alcool + Soda", 2), ("Mojito", 3)],
        )
        self.assertFalse(SaleDocument.objects.filter(reference="ICI").exists())
        self.assertEqual(run.affected(), {"ventes"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = import_archive(self.reader, SALES_REPLACED, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(callbacks, [])
        confirmed = import_archive(self.reader, SALES_REPLACED)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertNotEqual(db_fingerprint(), before)


class SalesDocumentsTests(LaneSectionsMixin, TestCase):
    def test_the_same_document_imported_twice_is_not_duplicated(self):
        build_sales()
        reader = self.export(EXPORTED)
        SaleDocument.objects.all().delete()
        for _ in range(2):
            import_archive(reader, MERGE)
        self.assertEqual(SaleDocument.objects.count(), 3)
        self.assertEqual(SaleDocumentLine.objects.count(), 4)

    def test_identical_documents_are_matched_one_for_one(self):
        """Two identical tabs are two documents: one here, two in the
        archive, the second comes in."""
        build_sales()
        reader = self.export(EXPORTED)
        SaleDocument.objects.filter(sold_on=day(7)).order_by("-created_at").first().delete()
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, "ventes", "bons de vente"), (1, 0, 0, 2))
        self.assertEqual(SaleDocument.objects.filter(sold_on=day(7)).count(), 2)


class SalesRefusalTests(LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_sales()
        self.reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)

    def edited(self, **changes):
        def edit(payload):
            for name, change in changes.items():
                change(payload[name])
            return payload

        return self.open(forge(self.reader, ventes=edit))

    def test_a_day_of_a_till_product_unknown_here_creates_it_to_link(self):
        def change(rows):
            rows.append(["NOUVEAU COCKTAIL", "2026-09-04", 6])

        run = import_archive(self.edited(daily=change), MERGE)
        product = PosProduct.objects.get(name="NOUVEAU COCKTAIL")
        self.assertEqual((product.recipe, product.ignored), (None, False))
        self.assertEqual(till_totals()["NOUVEAU COCKTAIL"], (6, day(4), day(4)))
        self.assertEqual(tally(run, "ventes", "produits caisse")[0], 2)  # PLANCHE (cleared) and this one

    def test_days_the_fields_refuse(self):
        def change(rows):
            rows[1][2] = True
            rows[2][2] = "8"
            rows[3][1] = "2026-02-30"
            rows.append(["MOJITO CLASSIQUE", "2026-09-01", 12])
            rows.append(["MOJITO CLASSIQUE"])

        run = import_archive(self.edited(daily=change), MERGE)
        self.assertEqual(
            run.section("ventes").skipped,
            [
                "Produit caisse « MOJITO CLASSIQUE » le 01/09/2026 : « quantity » : nombre entier attendu (« True »)",
                "Produit caisse « MOJITO CLASSIQUE » le 02/09/2026 : « quantity » : nombre entier attendu (« 8 »)",
                "Produit caisse « Mojito HH » : « sold_on » : date illisible (« 2026-02-30 »)",
                "Produit caisse « MOJITO CLASSIQUE » le 01/09/2026 : deux fois dans l'archive",
                "vente par jour n° 9 de l'archive : illisible",
            ],
        )
        self.assertEqual(PosProductDailyQuantity.objects.count(), 4)

    def test_a_day_that_nets_negative_is_a_refund_and_comes_back(self):
        """A pint sold one day and taken back the next nets -1 on the day of
        the refund. The archive has to carry it: refused as « nombre positif
        attendu », a restore silently dropped that day, and the next till
        import - which nets the same figure - could not write it either."""
        def change(rows):
            rows[0][2] = -3

        import_archive(self.edited(daily=change), MERGE)

        self.assertEqual(
            PosProductDailyQuantity.objects.get(product__name="CAFÉ", sold_on=day(1)).quantity, -3
        )

    def test_a_sale_typed_in_for_a_recipe_unknown_here_is_skipped(self):
        def change(sales):
            sales[0]["recipe"] = "Mojito fraise"

        run = import_archive(self.edited(manual_sales=change), MERGE)
        self.assertEqual(
            run.section("ventes").skipped, ["Vente saisie du 05/09/2026 : recette inconnue « Mojito fraise »"]
        )
        self.assertEqual(RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).count(), 1)

    def test_a_document_whose_line_cannot_be_found_is_skipped_whole(self):
        def change(documents):
            documents[0]["lines"][1]["article"] = "Gin rose"
            documents[1]["lines"][0]["article"] = "Menthe"

        run = import_archive(self.edited(documents=change), MERGE)
        self.assertEqual(
            run.section("ventes").skipped,
            [
                "Bon de vente du 06/09/2026 (SOIRÉE-1) : article inconnu « Gin rose »",
                "bon de vente n° 2 de l'archive : ligne n° 1 : une recette ou un article, jamais les deux ni aucun",
            ],
        )
        self.assertEqual(SaleDocument.objects.count(), 1)
        self.assertEqual(SaleDocumentLine.objects.count(), 1)

    def test_the_columns_of_the_days_are_checked(self):
        reader = self.open(forge(self.reader, ventes=lambda payload: {**payload, "daily_columns": ["sold_on", "till_product", "quantity"]}))
        with self.assertRaisesMessage(ArchiveError, "« daily_columns » doit être"):
            import_archive(reader, MERGE)
        reader = self.open(forge(self.reader, ventes=lambda payload: {**payload, "daily": {"MOJITO": 1}}))
        with self.assertRaisesMessage(ArchiveError, "« daily » n'est pas une liste"):
            import_archive(reader, MERGE)


class SalesClearTests(LaneSectionsMixin, TestCase):
    def test_clear_keeps_linked_till_products_at_zero(self):
        build_sales()
        run = run_clear({"ventes"}, preview=False)
        self.assertEqual(PosProductDailyQuantity.objects.count(), 0)
        self.assertEqual(RecipeSale.objects.count(), 0)
        self.assertEqual(SaleDocument.objects.count(), 0)
        self.assertFalse(PosProduct.objects.filter(name="PLANCHE").exists())
        self.assertEqual(link_of("MOJITO CLASSIQUE"), ("Mojito", False))
        self.assertEqual(link_of("CAFÉ"), (None, True))
        self.assertEqual(till_totals()["Pinte IPA"], (0, None, None))
        self.assertEqual(tally(run, "ventes", QUANTITIES), (0, 0, 7, 0))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (0, 0, 2, 0))
        self.assertEqual(tally(run, "ventes", "ventes par recette"), (0, 0, 4, 0))
        self.assertEqual(tally(run, "ventes", "bons de vente"), (0, 0, 3, 0))
        self.assertEqual(tally(run, "ventes", "produits caisse"), (0, 0, 1, 0))
