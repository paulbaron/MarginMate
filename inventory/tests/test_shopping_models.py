"""« Prévoir les courses », what the owner sets and leaves out: the
`ShoppingSetting` and `ShoppingExclusion` models (inventory 0021) on their
own.

`ShoppingSetting` is the page's settings for the espace: one row (pk 1),
absent until the owner changes something. `current()` hands the defaults
back unsaved - a page drawn writes nothing - and the database refuses a
second row and a value out of its range, whatever writes it.

A `ShoppingExclusion` is one article left out everywhere, one article left
out at one store (« Pas ici »), or one category name left out everywhere
("" being the articles with none) - never a category at one store. The
check and unique constraints hold whatever writes the row (a create, an
update, a bulk create). An article deleted takes its rows with it, and so
does a store (CASCADE: the « Données » clears delete both); an article
merged into another (`services.merge_stock_types` deletes the article it
merges) never carries its rows over. What the page does with the rows is
`test_shopping_page.py`'s.

Invented data throughout: every article, category and store is made up for
these tests.
"""

from __future__ import annotations

import importlib

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.models import ShoppingExclusion, ShoppingSetting, StockType
from inventory.services import merge_stock_types
from invoices.models import Supplier
from tests.factories import make_stock_type, make_supplier

MIGRATION = importlib.import_module("inventory.migrations.0021_shopping_forecast").Migration

ONE_ROW = "shopping_setting_one_row"
THRESHOLD_IN_RANGE = "shopping_setting_threshold_in_range"
MEMORY_IN_RANGE = "shopping_setting_memory_in_range"
ARTICLE_OR_CATEGORY = "shopping_exclusion_article_or_category"
CATEGORY_EVERYWHERE = "shopping_exclusion_category_everywhere"
ARTICLE_ONCE = "shopping_exclusion_article_once"
ARTICLE_ONCE_PER_STORE = "shopping_exclusion_article_once_per_store"


def created_by_the_migration(model) -> list:
    """The constraints the migration's CreateModel of `model` makes."""
    (operation,) = [operation for operation in MIGRATION.operations if operation.name_lower == model._meta.model_name]
    return list(operation.options["constraints"])


class MigrationTests(TestCase):
    def test_it_follows_the_gap_filler_s_setting(self):
        self.assertIn(("inventory", "0020_gap_fill_setting"), MIGRATION.dependencies)
        self.assertEqual(
            [operation.name for operation in MIGRATION.operations], ["ShoppingSetting", "ShoppingExclusion"]
        )

    def test_the_constraints_are_the_ones_the_migration_made(self):
        for model, names in (
            (ShoppingSetting, [ONE_ROW, THRESHOLD_IN_RANGE, MEMORY_IN_RANGE]),
            (
                ShoppingExclusion,
                [ARTICLE_OR_CATEGORY, CATEGORY_EVERYWHERE, ARTICLE_ONCE, ARTICLE_ONCE_PER_STORE],
            ),
        ):
            with self.subTest(model=model.__name__):
                self.assertEqual([constraint.name for constraint in model._meta.constraints], names)
                self.assertEqual(created_by_the_migration(model), list(model._meta.constraints))

    def test_the_models_and_their_migrations_agree(self):
        call_command("makemigrations", "inventory", check=True, dry_run=True, verbosity=0)


# ---------------------------------------------------------------------------
# ShoppingSetting
# ---------------------------------------------------------------------------


def stored(**values) -> ShoppingSetting:
    return ShoppingSetting.objects.create(pk=ShoppingSetting.SINGLETON_PK, **values)


class ShoppingSettingTests(TestCase):
    def refused(self, constraint, **values):
        with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
            stored(**values)

    def test_no_row_is_the_default_and_reading_it_writes_nothing(self):
        with CaptureQueriesContext(connection) as queries:
            setting = ShoppingSetting.current()
        self.assertEqual([query["sql"].split()[0] for query in queries.captured_queries], ["SELECT"])
        self.assertEqual(setting.pk, ShoppingSetting.SINGLETON_PK)
        self.assertEqual((setting.threshold_percent, setting.memory_months, setting.use_till), (25, 6, True))
        self.assertFalse(ShoppingSetting.objects.exists())

    def test_the_ranges_and_defaults_are_the_spec_s(self):
        self.assertEqual(ShoppingSetting.SINGLETON_PK, 1)
        self.assertEqual(ShoppingSetting.THRESHOLD_RANGE, (10, 60))
        self.assertEqual(ShoppingSetting.MEMORY_RANGE, (2, 24))
        defaults = ShoppingSetting()
        low, high = ShoppingSetting.THRESHOLD_RANGE
        self.assertTrue(low <= defaults.threshold_percent <= high)
        low, high = ShoppingSetting.MEMORY_RANGE
        self.assertTrue(low <= defaults.memory_months <= high)
        self.assertIs(defaults.use_till, True)

    def test_the_stored_row(self):
        stored(threshold_percent=40, memory_months=12, use_till=False)
        setting = ShoppingSetting.current()
        self.assertEqual((setting.threshold_percent, setting.memory_months, setting.use_till), (40, 12, False))
        self.assertIsNotNone(setting.updated_at)

    def test_one_row_for_the_espace(self):
        stored()
        with self.assertRaisesRegex(IntegrityError, ONE_ROW), transaction.atomic():
            ShoppingSetting.objects.create(pk=2)
        # Nor a first row under another pk.
        ShoppingSetting.objects.all().delete()
        with self.assertRaisesRegex(IntegrityError, ONE_ROW), transaction.atomic():
            ShoppingSetting.objects.create(pk=2)
        self.assertFalse(ShoppingSetting.objects.exists())

    def test_both_ends_of_each_range_are_kept(self):
        for field, (low, high) in (
            ("threshold_percent", ShoppingSetting.THRESHOLD_RANGE),
            ("memory_months", ShoppingSetting.MEMORY_RANGE),
        ):
            for value in (low, high):
                with self.subTest(field=field, value=value):
                    setting = stored(**{field: value})
                    setting.refresh_from_db()
                    self.assertEqual(getattr(setting, field), value)
                    setting.delete()

    def test_the_database_refuses_a_value_out_of_range(self):
        low, high = ShoppingSetting.THRESHOLD_RANGE
        for value in (0, low - 1, high + 1, 100):
            with self.subTest(threshold_percent=value):
                self.refused(THRESHOLD_IN_RANGE, threshold_percent=value)
        low, high = ShoppingSetting.MEMORY_RANGE
        for value in (0, low - 1, high + 1, 120):
            with self.subTest(memory_months=value):
                self.refused(MEMORY_IN_RANGE, memory_months=value)
        self.assertFalse(ShoppingSetting.objects.exists())

    def test_an_update_cannot_get_round_it(self):
        stored(threshold_percent=30, memory_months=9)
        for change, constraint in (
            ({"threshold_percent": ShoppingSetting.THRESHOLD_RANGE[1] + 1}, THRESHOLD_IN_RANGE),
            ({"memory_months": ShoppingSetting.MEMORY_RANGE[0] - 1}, MEMORY_IN_RANGE),
            ({"id": 2}, ONE_ROW),
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
                    ShoppingSetting.objects.filter(pk=ShoppingSetting.SINGLETON_PK).update(**change)
        setting = ShoppingSetting.current()
        self.assertEqual((setting.pk, setting.threshold_percent, setting.memory_months), (1, 30, 9))

    def test_nor_a_bulk_create(self):
        with self.assertRaisesRegex(IntegrityError, THRESHOLD_IN_RANGE), transaction.atomic():
            ShoppingSetting.objects.bulk_create([ShoppingSetting(pk=1, threshold_percent=61)])
        self.assertFalse(ShoppingSetting.objects.exists())


# ---------------------------------------------------------------------------
# ShoppingExclusion
# ---------------------------------------------------------------------------


class ExclusionTestCase(TestCase):
    def setUp(self):
        self.rum = make_stock_type(name="Rhum exemple", category="Spiritueux exemple")
        self.gin = make_stock_type(name="Gin exemple", category="Spiritueux exemple")
        self.wholesaler = make_supplier(name="Grossiste exemple")
        self.grocer = make_supplier(name="Épicerie exemple")

    def stored_rows(self) -> set:
        return set(ShoppingExclusion.objects.values_list("stock_type_id", "category", "supplier_id"))


class ExactlyOneOfTheTwoTests(ExclusionTestCase):
    """One article - everywhere or at one store - or one category name,
    everywhere: never both, never neither, never a category at a store."""

    def refused(self, constraint, **fields):
        with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
            ShoppingExclusion.objects.create(**fields)

    def test_an_article_everywhere_is_kept(self):
        exclusion = ShoppingExclusion.objects.create(stock_type=self.rum)
        exclusion.refresh_from_db()
        self.assertEqual((exclusion.stock_type, exclusion.category, exclusion.supplier), (self.rum, None, None))
        self.assertIsNotNone(exclusion.created_at)
        # The article reaches it as `shopping_exclusions`.
        self.assertEqual(list(self.rum.shopping_exclusions.all()), [exclusion])

    def test_an_article_at_one_store_is_kept(self):
        exclusion = ShoppingExclusion.objects.create(stock_type=self.rum, supplier=self.wholesaler)
        exclusion.refresh_from_db()
        self.assertEqual(
            (exclusion.stock_type, exclusion.category, exclusion.supplier), (self.rum, None, self.wholesaler)
        )
        # The store reaches it as `shopping_exclusions` too.
        self.assertEqual(list(self.wholesaler.shopping_exclusions.all()), [exclusion])
        self.assertEqual(list(self.rum.shopping_exclusions.all()), [exclusion])

    def test_a_category_everywhere_is_kept_the_blank_one_included(self):
        for category in ("Spiritueux exemple", ""):
            with self.subTest(category=category):
                exclusion = ShoppingExclusion.objects.create(category=category)
                exclusion.refresh_from_db()
                self.assertEqual((exclusion.stock_type, exclusion.category, exclusion.supplier), (None, category, None))
                self.assertIsNotNone(exclusion.created_at)

    def test_both_are_refused(self):
        self.refused(ARTICLE_OR_CATEGORY, stock_type=self.rum, category="Spiritueux exemple")
        # "" is a category - the articles with none - not the lack of one.
        self.refused(ARTICLE_OR_CATEGORY, stock_type=self.rum, category="")
        self.refused(ARTICLE_OR_CATEGORY, stock_type=self.rum, category="", supplier=self.wholesaler)
        self.assertFalse(ShoppingExclusion.objects.exists())

    def test_neither_is_refused_a_store_alone_included(self):
        self.refused(ARTICLE_OR_CATEGORY)
        self.refused(ARTICLE_OR_CATEGORY, stock_type=None, category=None)
        self.refused(ARTICLE_OR_CATEGORY, supplier=self.wholesaler)
        self.assertFalse(ShoppingExclusion.objects.exists())

    def test_a_category_at_one_store_is_refused(self):
        self.refused(CATEGORY_EVERYWHERE, category="Spiritueux exemple", supplier=self.wholesaler)
        self.refused(CATEGORY_EVERYWHERE, category="", supplier=self.wholesaler)
        self.assertFalse(ShoppingExclusion.objects.exists())

    def test_an_update_cannot_get_round_them(self):
        everywhere = ShoppingExclusion.objects.create(stock_type=self.rum)
        not_here = ShoppingExclusion.objects.create(stock_type=self.gin, supplier=self.wholesaler)
        by_category = ShoppingExclusion.objects.create(category="Bières exemple")
        changes = (
            (everywhere, {"category": "Vins exemple"}, ARTICLE_OR_CATEGORY),
            (everywhere, {"stock_type": None}, ARTICLE_OR_CATEGORY),
            (not_here, {"stock_type": None}, ARTICLE_OR_CATEGORY),
            (not_here, {"stock_type": None, "category": "Vins exemple"}, CATEGORY_EVERYWHERE),
            (by_category, {"category": None}, ARTICLE_OR_CATEGORY),
            (by_category, {"stock_type": self.gin}, ARTICLE_OR_CATEGORY),
            (by_category, {"supplier": self.wholesaler}, CATEGORY_EVERYWHERE),
        )
        for exclusion, change, constraint in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
                    ShoppingExclusion.objects.filter(pk=exclusion.pk).update(**change)
        self.assertEqual(
            self.stored_rows(),
            {(self.rum.pk, None, None), (self.gin.pk, None, self.wholesaler.pk), (None, "Bières exemple", None)},
        )

    def test_nor_a_bulk_create(self):
        for rows, constraint in (
            ([ShoppingExclusion(category="Vins exemple"), ShoppingExclusion()], ARTICLE_OR_CATEGORY),
            (
                [ShoppingExclusion(stock_type=self.rum), ShoppingExclusion(category="", supplier=self.grocer)],
                CATEGORY_EVERYWHERE,
            ),
        ):
            with self.subTest(constraint=constraint):
                with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
                    ShoppingExclusion.objects.bulk_create(rows)
        self.assertFalse(ShoppingExclusion.objects.exists())

    def test_what_it_reads_as(self):
        self.assertEqual(str(ShoppingExclusion(category="Bières exemple")), "Catégorie « Bières exemple »")
        self.assertEqual(str(ShoppingExclusion(category="")), "Catégorie «  »")
        self.assertEqual(str(ShoppingExclusion(stock_type=self.rum)), "Rhum exemple")
        self.assertEqual(
            str(ShoppingExclusion(stock_type=self.rum, supplier=self.wholesaler)),
            "Rhum exemple (chez Grossiste exemple)",
        )


class OnceEachTests(ExclusionTestCase):
    """A category is left out once, an article once everywhere and once at
    each store; any number of them side by side."""

    def refused(self, **fields):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ShoppingExclusion.objects.create(**fields)

    def test_a_category_is_left_out_once(self):
        ShoppingExclusion.objects.create(category="Bières exemple")
        self.refused(category="Bières exemple")
        self.assertEqual(ShoppingExclusion.objects.count(), 1)

    def test_the_blank_category_once_too(self):
        ShoppingExclusion.objects.create(category="")
        self.refused(category="")
        self.assertEqual(ShoppingExclusion.objects.count(), 1)

    def test_a_name_written_otherwise_is_another_category(self):
        # As the page matches it: exactly as written, case and accents included.
        for category in ("Bières exemple", "bières exemple", "Bieres exemple", "BIÈRES EXEMPLE"):
            ShoppingExclusion.objects.create(category=category)
        self.assertEqual(ShoppingExclusion.objects.count(), 4)

    def test_an_article_is_left_out_everywhere_once(self):
        ShoppingExclusion.objects.create(stock_type=self.rum)
        self.refused(stock_type=self.rum)
        self.assertEqual(self.stored_rows(), {(self.rum.pk, None, None)})

    def test_an_article_is_left_out_at_a_store_once(self):
        ShoppingExclusion.objects.create(stock_type=self.rum, supplier=self.wholesaler)
        self.refused(stock_type=self.rum, supplier=self.wholesaler)
        self.assertEqual(self.stored_rows(), {(self.rum.pk, None, self.wholesaler.pk)})

    def test_an_article_at_several_stores_and_several_articles_at_one(self):
        ShoppingExclusion.objects.create(stock_type=self.rum, supplier=self.wholesaler)
        ShoppingExclusion.objects.create(stock_type=self.rum, supplier=self.grocer)
        ShoppingExclusion.objects.create(stock_type=self.gin, supplier=self.wholesaler)
        self.assertEqual(
            self.stored_rows(),
            {
                (self.rum.pk, None, self.wholesaler.pk),
                (self.rum.pk, None, self.grocer.pk),
                (self.gin.pk, None, self.wholesaler.pk),
            },
        )

    def test_everywhere_beside_a_store_is_the_page_s_to_tidy(self):
        # Excluding an article everywhere deletes its store rows on the page;
        # the database does not ask it, and holds both.
        ShoppingExclusion.objects.create(stock_type=self.rum, supplier=self.wholesaler)
        ShoppingExclusion.objects.create(stock_type=self.rum)
        self.assertEqual(self.stored_rows(), {(self.rum.pk, None, self.wholesaler.pk), (self.rum.pk, None, None)})

    def test_any_number_of_articles_beside_any_number_of_categories(self):
        # Every article row has no category (NULL), every category row no
        # article (NULL): never a clash.
        for index in range(4):
            ShoppingExclusion.objects.create(stock_type=make_stock_type(name=f"Article exemple {index}"))
        ShoppingExclusion.objects.create(category="")
        ShoppingExclusion.objects.create(category="Bières exemple")
        self.assertEqual(ShoppingExclusion.objects.filter(category__isnull=True).count(), 4)
        self.assertEqual(ShoppingExclusion.objects.filter(stock_type__isnull=True).count(), 2)

    def test_an_update_cannot_get_round_them(self):
        ShoppingExclusion.objects.create(stock_type=self.rum)
        ShoppingExclusion.objects.create(stock_type=self.gin, supplier=self.grocer)
        not_here = ShoppingExclusion.objects.create(stock_type=self.rum, supplier=self.wholesaler)
        other = ShoppingExclusion.objects.create(stock_type=self.gin, supplier=self.wholesaler)
        ShoppingExclusion.objects.create(category="Bières exemple")
        by_category = ShoppingExclusion.objects.create(category="Vins exemple")
        before = self.stored_rows()
        for exclusion, change in (
            (not_here, {"supplier": None}),  # the rum is left out everywhere already
            (other, {"supplier": self.grocer}),  # the gin is left out there already
            (by_category, {"category": "Bières exemple"}),
        ):
            with self.subTest(change=change):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ShoppingExclusion.objects.filter(pk=exclusion.pk).update(**change)
        self.assertEqual(self.stored_rows(), before)

    def test_nor_a_bulk_create(self):
        for rows in (
            [ShoppingExclusion(stock_type=self.rum), ShoppingExclusion(stock_type=self.rum)],
            [
                ShoppingExclusion(stock_type=self.rum, supplier=self.grocer),
                ShoppingExclusion(stock_type=self.rum, supplier=self.grocer),
            ],
            [ShoppingExclusion(category="Bières exemple"), ShoppingExclusion(category="Bières exemple")],
        ):
            with self.subTest(rows=rows):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ShoppingExclusion.objects.bulk_create(rows)
        self.assertFalse(ShoppingExclusion.objects.exists())


class GoesWithItsArticleAndItsStoreTests(ExclusionTestCase):
    """CASCADE on both: an article deleted takes its rows, a store deleted
    its « Pas ici » rows - and nothing else."""

    def setUp(self):
        super().setUp()
        self.rum_everywhere = ShoppingExclusion.objects.create(stock_type=self.rum)
        self.rum_not_here = ShoppingExclusion.objects.create(stock_type=self.rum, supplier=self.grocer)
        self.gin_not_here = ShoppingExclusion.objects.create(stock_type=self.gin, supplier=self.wholesaler)
        self.gin_everywhere = ShoppingExclusion.objects.create(stock_type=self.gin)
        self.by_category = ShoppingExclusion.objects.create(category="Spiritueux exemple")

    def test_deleting_the_article_deletes_its_rows_and_nothing_else(self):
        self.rum.delete()
        self.assertEqual(
            set(ShoppingExclusion.objects.all()), {self.gin_not_here, self.gin_everywhere, self.by_category}
        )
        # A category stays left out with no article left under it.
        self.gin.delete()
        self.assertEqual(list(ShoppingExclusion.objects.all()), [self.by_category])
        self.assertEqual(Supplier.objects.filter(pk__in=(self.grocer.pk, self.wholesaler.pk)).count(), 2)

    def test_deleting_the_store_deletes_its_rows_and_nothing_else(self):
        self.wholesaler.delete()
        self.assertEqual(
            set(ShoppingExclusion.objects.all()),
            {self.rum_everywhere, self.rum_not_here, self.gin_everywhere, self.by_category},
        )
        self.assertEqual(StockType.objects.filter(pk__in=(self.rum.pk, self.gin.pk)).count(), 2)

    def test_the_donnees_clears_delete_by_queryset_too(self):
        # The suppliers' clear deletes a supplier, the associations' clear
        # every article (transfer/sections): a row here never holds them.
        Supplier.objects.filter(pk=self.grocer.pk).delete()
        self.assertFalse(ShoppingExclusion.objects.filter(supplier=self.grocer.pk).exists())
        StockType.objects.all().delete()
        self.assertEqual(list(ShoppingExclusion.objects.all()), [self.by_category])

    def test_taking_an_exclusion_back_leaves_the_article_and_the_store(self):
        self.rum_not_here.delete()
        self.rum_everywhere.delete()
        self.assertTrue(StockType.objects.filter(pk=self.rum.pk).exists())
        self.assertTrue(Supplier.objects.filter(pk=self.grocer.pk).exists())
        self.assertFalse(self.rum.shopping_exclusions.exists())


class MergedArticleTests(ExclusionTestCase):
    """`merge_stock_types(source, target)` moves everything onto the target
    and deletes the source: the source's rows go with it, everywhere and
    store alike, and the target keeps its own. The article that remains is
    left out only where IT was - a merge never carries the source's
    exclusion over. It never crashes, both left out at one store included."""

    def setUp(self):
        super().setUp()
        self.source = make_stock_type(name="Rhum blanc exemple", category="Rhums exemple")
        self.target = make_stock_type(name="Rhum blanc bis exemple", category="Spiritueux exemple")

    def test_the_source_s_rows_go_with_it(self):
        ShoppingExclusion.objects.create(stock_type=self.source)
        ShoppingExclusion.objects.create(stock_type=self.source, supplier=self.wholesaler)
        merge_stock_types(self.source, self.target)
        self.assertFalse(StockType.objects.filter(pk=self.source.pk).exists())
        self.assertFalse(ShoppingExclusion.objects.exists())

    def test_the_target_keeps_its_own(self):
        everywhere = ShoppingExclusion.objects.create(stock_type=self.target)
        not_here = ShoppingExclusion.objects.create(stock_type=self.target, supplier=self.grocer)
        merge_stock_types(self.source, self.target)
        self.assertEqual(set(ShoppingExclusion.objects.all()), {everywhere, not_here})

    def test_both_left_out_at_one_store_merge_into_one(self):
        ShoppingExclusion.objects.create(stock_type=self.source, supplier=self.wholesaler)
        ShoppingExclusion.objects.create(stock_type=self.source, supplier=self.grocer)
        kept = ShoppingExclusion.objects.create(stock_type=self.target, supplier=self.wholesaler)
        merge_stock_types(self.source, self.target)
        # Left out at the grocer through the source only: proposed there again.
        self.assertEqual(list(ShoppingExclusion.objects.all()), [kept])

    def test_a_category_left_out_stays(self):
        by_category = ShoppingExclusion.objects.create(category="Rhums exemple")
        merge_stock_types(self.source, self.target)
        self.assertEqual(list(ShoppingExclusion.objects.all()), [by_category])
        self.assertEqual(StockType.objects.get(pk=self.target.pk).category, "Spiritueux exemple")
