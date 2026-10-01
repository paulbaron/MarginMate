"""« Combler les écarts », what the owner leaves out: the `GapExclusion` model
(inventory 0019) on its own.

A row is one article (`stock_type`) or one category name (`category`, ""
being the articles with none) - exactly one of the two, which a check
constraint holds whatever writes the row (a create, an update, a bulk
create). A category is left out once, an article once. An article deleted
takes its exclusion with it (CASCADE), and so does one merged into another
(`services.merge_stock_types` deletes the article it merges): the article
that remains is left out only if it was. What the page does with the rows
is `test_gaps.py`'s and the planner's `test_gap_planner.py`'s.

Invented data throughout: every article, category, recipe, price and
quantity is made up for these tests; every price is above 30 €.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from inventory.gaps import gaps_since
from inventory.models import GapExclusion, StockTakeLine, StockType
from inventory.services import merge_stock_types
from recipes.models import RecipeIngredient, RecipeSale
from tests.factories import make_ingredient, make_recipe, make_stock_take, make_stock_take_line, make_stock_type

CONSTRAINT = "gap_exclusion_article_or_category"
END = date(2026, 3, 31)


class ExactlyOneOfTheTwoTests(TestCase):
    """One article, or one category name - never both, never neither."""

    def setUp(self):
        self.rum = make_stock_type(name="Rhum exemple", category="Spiritueux exemple")
        self.gin = make_stock_type(name="Gin exemple", category="Spiritueux exemple")

    def refused(self, **fields):
        with self.assertRaisesRegex(IntegrityError, CONSTRAINT), transaction.atomic():
            GapExclusion.objects.create(**fields)

    def test_the_constraint_is_the_one_the_migration_made(self):
        self.assertEqual([constraint.name for constraint in GapExclusion._meta.constraints], [CONSTRAINT])

    def test_an_article_alone_is_kept(self):
        exclusion = GapExclusion.objects.create(stock_type=self.rum)
        exclusion.refresh_from_db()
        self.assertEqual((exclusion.stock_type, exclusion.category), (self.rum, None))
        self.assertIsNotNone(exclusion.created_at)
        # The article reaches it as `gap_exclusion`.
        self.assertEqual(list(StockType.objects.filter(gap_exclusion=exclusion)), [self.rum])

    def test_a_category_alone_is_kept_the_blank_one_included(self):
        for category in ("Spiritueux exemple", ""):
            with self.subTest(category=category):
                exclusion = GapExclusion.objects.create(category=category)
                exclusion.refresh_from_db()
                self.assertEqual((exclusion.stock_type, exclusion.category), (None, category))
                self.assertIsNotNone(exclusion.created_at)

    def test_both_are_refused(self):
        self.refused(stock_type=self.rum, category="Spiritueux exemple")
        # "" is a category - the articles with none - not the lack of one.
        self.refused(stock_type=self.rum, category="")
        self.assertFalse(GapExclusion.objects.exists())

    def test_neither_is_refused(self):
        self.refused()
        self.refused(stock_type=None, category=None)
        self.assertFalse(GapExclusion.objects.exists())

    def test_an_update_cannot_get_round_it(self):
        by_article = GapExclusion.objects.create(stock_type=self.rum)
        by_category = GapExclusion.objects.create(category="Bières exemple")
        changes = (
            (by_article, {"category": "Vins exemple"}),
            (by_article, {"stock_type": None}),
            (by_category, {"category": None}),
            (by_category, {"stock_type": self.gin}),
        )
        for exclusion, change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(IntegrityError, CONSTRAINT), transaction.atomic():
                    GapExclusion.objects.filter(pk=exclusion.pk).update(**change)
        self.assertEqual(
            set(GapExclusion.objects.values_list("stock_type_id", "category")),
            {(self.rum.pk, None), (None, "Bières exemple")},
        )

    def test_nor_a_bulk_create(self):
        with self.assertRaisesRegex(IntegrityError, CONSTRAINT), transaction.atomic():
            GapExclusion.objects.bulk_create([GapExclusion(category="Vins exemple"), GapExclusion()])
        self.assertFalse(GapExclusion.objects.exists())

    def test_what_it_reads_as(self):
        self.assertEqual(str(GapExclusion(category="Bières exemple")), "Catégorie « Bières exemple »")
        self.assertEqual(str(GapExclusion(stock_type=self.rum)), "Rhum exemple")


class OnceEachTests(TestCase):
    """A category name is left out once, an article once; any number of
    articles beside any number of categories."""

    def refused(self, **fields):
        with self.assertRaises(IntegrityError), transaction.atomic():
            GapExclusion.objects.create(**fields)

    def test_a_category_is_left_out_once(self):
        GapExclusion.objects.create(category="Bières exemple")
        self.refused(category="Bières exemple")
        self.assertEqual(GapExclusion.objects.count(), 1)

    def test_the_blank_category_once_too(self):
        GapExclusion.objects.create(category="")
        self.refused(category="")
        self.assertEqual(GapExclusion.objects.count(), 1)

    def test_a_name_written_otherwise_is_another_category(self):
        # As the page matches it: exactly as written, case and accents included.
        for category in ("Bières exemple", "bières exemple", "Bieres exemple", "BIÈRES EXEMPLE"):
            GapExclusion.objects.create(category=category)
        self.assertEqual(GapExclusion.objects.count(), 4)

    def test_an_article_is_left_out_once(self):
        rum = make_stock_type(name="Rhum exemple")
        GapExclusion.objects.create(stock_type=rum)
        self.refused(stock_type=rum)
        self.assertEqual(GapExclusion.objects.count(), 1)

    def test_any_number_of_articles_beside_any_number_of_categories(self):
        # Every article exclusion has no category (NULL): never a clash.
        for index in range(4):
            GapExclusion.objects.create(stock_type=make_stock_type(name=f"Article exemple {index}"))
        GapExclusion.objects.create(category="")
        GapExclusion.objects.create(category="Bières exemple")
        self.assertEqual(GapExclusion.objects.filter(category__isnull=True).count(), 4)
        self.assertEqual(GapExclusion.objects.count(), 6)


class GoesWithItsArticleTests(TestCase):
    """CASCADE: an article deleted takes its exclusion, and nothing else."""

    def setUp(self):
        self.rum = make_stock_type(name="Rhum exemple", category="Spiritueux exemple")
        self.gin = make_stock_type(name="Gin exemple", category="Spiritueux exemple")

    def test_deleting_the_article_deletes_its_exclusion_and_nothing_else(self):
        GapExclusion.objects.create(stock_type=self.rum)
        by_gin = GapExclusion.objects.create(stock_type=self.gin)
        by_category = GapExclusion.objects.create(category="Spiritueux exemple")
        self.rum.delete()
        self.assertEqual(set(GapExclusion.objects.all()), {by_gin, by_category})
        # A category stays left out with no article left under it.
        self.gin.delete()
        self.assertEqual(list(GapExclusion.objects.all()), [by_category])

    def test_taking_an_exclusion_back_leaves_the_article(self):
        exclusion = GapExclusion.objects.create(stock_type=self.rum)
        exclusion.delete()
        self.assertTrue(StockType.objects.filter(pk=self.rum.pk).exists())
        self.assertFalse(GapExclusion.objects.exists())


def at(day: int) -> datetime:
    """Noon on a day of March 2026."""
    return timezone.make_aware(datetime(2026, 3, day, 12, 0))


def counted(take, stock_type, quantity):
    return make_stock_take_line(
        stock_take=take, product=None, stock_type=stock_type, counted_quantity=quantity, unit=stock_type.unit
    )


class MergedArticleTests(TestCase):
    """`merge_stock_types(source, target)` moves everything onto the target
    and deletes the source: the source's exclusion goes with it, and the
    target keeps its own. Pinned as it is: the article that remains is left
    out only if IT was - a merge never carries the source's exclusion over,
    so merging a duplicate the owner had left out brings its stock back into
    the gaps. It never crashes, both excluded included."""

    def setUp(self):
        self.source = make_stock_type(name="Rhum blanc exemple", category="Rhums exemple")
        self.target = make_stock_type(name="Rhum blanc bis exemple", category="Spiritueux exemple")

    def test_the_source_s_exclusion_goes_with_it(self):
        GapExclusion.objects.create(stock_type=self.source)
        merge_stock_types(self.source, self.target)
        self.assertFalse(StockType.objects.filter(pk=self.source.pk).exists())
        self.assertFalse(GapExclusion.objects.exists())

    def test_the_target_keeps_its_own(self):
        kept = GapExclusion.objects.create(stock_type=self.target)
        merge_stock_types(self.source, self.target)
        self.assertEqual(list(GapExclusion.objects.all()), [kept])

    def test_both_left_out_merge_into_one_left_out(self):
        GapExclusion.objects.create(stock_type=self.source)
        kept = GapExclusion.objects.create(stock_type=self.target)
        merge_stock_types(self.source, self.target)
        self.assertEqual(list(GapExclusion.objects.all()), [kept])

    def test_a_category_left_out_stays_and_covers_what_carries_it(self):
        by_category = GapExclusion.objects.create(category="Rhums exemple")
        merge_stock_types(self.source, self.target)
        self.assertEqual(list(GapExclusion.objects.all()), [by_category])
        self.assertEqual(StockType.objects.get(pk=self.target.pk).category, "Spiritueux exemple")

    def test_on_the_page_the_merged_article_counts_again(self):
        # 2 L of the source and 1 L of the target counted; a ti punch pours
        # the source, 5 sold since the take.
        take = make_stock_take(taken_at=at(1))
        counted(take, self.source, "2")
        counted(take, self.target, "1")
        ti_punch = make_recipe(name="Ti punch exemple", selling_price_ttc="34.00")
        make_ingredient(ti_punch, stock_type=self.source, quantity="0.04", group=0)
        RecipeSale.objects.create(recipe=ti_punch, sold_on=date(2026, 3, 15), quantity=5, source="manual")
        GapExclusion.objects.create(stock_type=self.source)
        report = gaps_since(take, end=END)
        self.assertTrue(report.articles[self.source.pk].excluded)
        self.assertFalse(report.articles[self.source.pk].target)

        merge_stock_types(self.source, self.target)
        self.assertEqual(RecipeIngredient.objects.get(recipe=ti_punch).stock_type, self.target)
        self.assertEqual(StockTakeLine.objects.get(stock_take=take).counted_quantity, Decimal("3"))
        report = gaps_since(take, end=END)
        self.assertNotIn(self.source.pk, report.articles)
        merged = report.articles[self.target.pk]
        self.assertFalse(merged.excluded)
        self.assertEqual(report.ignored, frozenset())
        self.assertEqual(report.exclusions, [])
        # 3 L counted, 0,2 L sold, 0,3 L allowed: 2,5 L to fill again.
        self.assertEqual(merged.room, Decimal("2.5"))
        self.assertTrue(merged.target)
        self.assertEqual([offer.recipe_id for offer in report.offers], [ti_punch.pk])
