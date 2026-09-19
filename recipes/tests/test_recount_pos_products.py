"""`recount_pos_products` - the till products' totals as « Données » rebuilds
them - against what the till import itself writes.

An import copies the till's per-day quantities and rebuilds each till
product's total and first/last day from them, rather than copying the
totals: a copied total could only disagree with the days it is supposed to
sum. So the rebuild must land exactly where `sync_pos_products` lands, or
the « Produits caisse » worklist - ordered by what sold most - would be
ordered by numbers no till ever produced.
"""

from datetime import date
from unittest import mock

from django.test import TestCase

from recipes import sales
from recipes.models import PosProduct, PosProductDailyQuantity
from recipes.pos.laddition_xlsx import ParsedExport
from recipes.sales import recount_pos_products
from recipes.tasks import sync_pos_products
from tests.factories import make_recipe


def till_export(entries):
    """A parsed export whose `products` mirror its `entries` the way
    parse_rows builds both from the same rows - first/last are the days the
    product was rung up in this window."""
    products: dict = {}
    for name, day, quantity in entries:
        info = products.setdefault(name, {"quantity": 0, "category": "", "typology": "", "first": day, "last": day})
        info["quantity"] += quantity
        info["first"] = min(info["first"], day)
        info["last"] = max(info["last"], day)
    return ParsedExport(products=products, entries=list(entries))


def totals():
    return {
        product.name: (product.total_quantity, product.first_seen, product.last_seen)
        for product in PosProduct.objects.all()
    }


class RecountEqualsTheTillImportTests(TestCase):
    def import_two_overlapping_windows(self):
        sync_pos_products(till_export([
            ("Pinte Blonde", date(2026, 6, 1), 10),
            ("Pinte Blonde", date(2026, 6, 2), 15),
            ("Mojito", date(2026, 6, 2), 3),
        ]))
        # A backfill reaching back over June 2nd: the shared day is replaced,
        # not added to, and the dates widen.
        sync_pos_products(till_export([
            ("Pinte Blonde", date(2026, 6, 2), 15),
            ("Pinte Blonde", date(2026, 6, 3), 7),
            ("Café", date(2026, 7, 1), 1),
        ]))

    def test_the_totals_and_days_are_those_the_till_import_wrote(self):
        self.import_two_overlapping_windows()
        written = totals()
        self.assertEqual(written["Pinte Blonde"], (32, date(2026, 6, 1), date(2026, 6, 3)))

        PosProduct.objects.update(total_quantity=999, first_seen=date(2000, 1, 1), last_seen=None)
        changed = recount_pos_products(PosProduct.objects.values_list("pk", flat=True))

        self.assertEqual(changed, 3)
        self.assertEqual(totals(), written)

    def test_a_day_sold_nothing_still_counts_as_seen(self):
        """A day whose rows net to zero (a drink rung up then cancelled) is
        stored as a 0 row, and the till import widens the dates over it."""
        sync_pos_products(till_export([("Café", date(2026, 6, 5), 0), ("Café", date(2026, 6, 6), 2)]))
        written = totals()
        self.assertEqual(written["Café"], (2, date(2026, 6, 5), date(2026, 6, 6)))

        PosProduct.objects.update(total_quantity=0, first_seen=None, last_seen=None)
        recount_pos_products(PosProduct.objects.values_list("pk", flat=True))
        self.assertEqual(totals(), written)

    def test_already_right_changes_nothing(self):
        self.import_two_overlapping_windows()
        self.assertEqual(recount_pos_products(PosProduct.objects.values_list("pk", flat=True)), 0)

    def test_a_product_with_no_day_reads_zero_and_no_dates(self):
        """A till product the links created before any sale came in, or
        whose days an import removed."""
        product = PosProduct.objects.create(
            name="Pinte IPA", recipe=make_recipe(name="Pinte IPA"), total_quantity=40,
            first_seen=date(2026, 5, 1), last_seen=date(2026, 5, 31),
        )
        self.assertEqual(recount_pos_products([product.pk]), 1)
        product.refresh_from_db()
        self.assertEqual((product.total_quantity, product.first_seen, product.last_seen), (0, None, None))

    def test_only_the_products_asked_for_are_recounted(self):
        self.import_two_overlapping_windows()
        PosProduct.objects.update(total_quantity=999)
        mojito = PosProduct.objects.get(name="Mojito")
        recount_pos_products([mojito.pk])
        self.assertEqual(totals()["Mojito"][0], 3)
        self.assertEqual(totals()["Pinte Blonde"][0], 999)

    def test_nothing_asked_is_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(recount_pos_products([]), 0)
            self.assertEqual(recount_pos_products(set()), 0)

    def test_an_unknown_id_is_ignored(self):
        """A till product deleted earlier in the same run (an import's prune)."""
        self.assertEqual(recount_pos_products([123456]), 0)

    def test_many_products_are_recounted_in_batches(self):
        """SQLite caps the parameters of one query: the ids go in batches."""
        names = [f"Produit {n}" for n in range(7)]
        sync_pos_products(till_export([(name, date(2026, 6, 1 + n), n + 1) for n, name in enumerate(names)]))
        written = totals()
        PosProduct.objects.update(total_quantity=0, first_seen=None, last_seen=None)
        with mock.patch.object(sales, "RECOUNT_BATCH", 3):
            changed = recount_pos_products(list(PosProduct.objects.values_list("pk", flat=True)))
        self.assertEqual(changed, 7)
        self.assertEqual(totals(), written)
        self.assertEqual(PosProductDailyQuantity.objects.count(), 7)
