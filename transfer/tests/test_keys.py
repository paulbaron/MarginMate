"""The product resolver's one tolerance (§5.4): a product found by its
folded name, when exactly one folds that way - and never one the archive
names itself.

Why the guard: SQLite's case-blind comparison is ASCII only, so the app's
own matcher makes « BIÈRE DU PONT FÛT 20L » and « BIÈRE DU PONT FûT 20L »
two products of one supplier (a review found such a pair made on a scratch
copy; the till already holds three pairs of that shape). Imported into an
empty database, the first of the pair was created, and the second then
folded onto it: two products merged, the second's classification skipped
as « en double », its invoice line moved onto the first - silently wrong
stock.

Fixtures are invented (the repository is public).
"""

from django.test import TestCase

from tests.factories import make_product, make_supplier
from transfer.archive import ArchiveReader
from transfer.keys import ProductResolver, SupplierResolver, archive_product_keys
from transfer.tests.support import forge

KEG = "BIÈRE DU PONT FÛT 20L"
TWIN = "BIÈRE DU PONT FûT 20L"  # the accented capital only


class ProductResolverTests(TestCase):
    def setUp(self):
        self.brewery = make_supplier(code="BRASSERIE_TEST", name="Brasserie Test")
        self.keg = make_product(self.brewery, KEG)

    def test_the_exact_name_comes_first(self):
        twin = make_product(self.brewery, TWIN)
        resolver = ProductResolver()
        self.assertEqual(resolver.resolve(self.brewery, KEG), self.keg)
        self.assertEqual(resolver.resolve(self.brewery, TWIN), twin)

    def test_a_folded_name_finds_the_only_product_folding_that_way(self):
        """A name written in another case in the other database is still the
        same product here."""
        self.assertEqual(ProductResolver().resolve(self.brewery, "Bière du Pont  Fût 20L"), self.keg)

    def test_a_product_the_archive_names_itself_is_never_found_by_another_name(self):
        resolver = ProductResolver(archive_keys={(self.brewery.pk, KEG)})

        self.assertIsNone(resolver.resolve(self.brewery, TWIN))
        self.assertEqual(resolver.resolve(self.brewery, KEG), self.keg)

    def test_the_archive_names_can_be_given_after_it_is_built(self):
        """A section is handed its resolver by the import's context, then
        tells it what the archive names."""
        resolver = ProductResolver()
        resolver.name_archive({(self.brewery.pk, KEG)})

        self.assertIsNone(resolver.resolve(self.brewery, TWIN))

    def test_the_same_name_under_another_supplier_does_not_count(self):
        other = make_supplier(code="CAVE_TEST", name="Cave Test")
        resolver = ProductResolver(archive_keys={(other.pk, KEG)})

        self.assertEqual(resolver.resolve(self.brewery, TWIN), self.keg)

    def test_two_products_folding_one_way_find_none_unless_the_archive_names_one(self):
        twin = make_product(self.brewery, TWIN)

        self.assertIsNone(ProductResolver().resolve(self.brewery, "bière du pont fût 20l"))
        # The archive's own record of KEG will find KEG; the other spelling
        # can only be the product it does not name - as TillProducts does.
        resolver = ProductResolver(archive_keys={(self.brewery.pk, KEG)})
        self.assertEqual(resolver.resolve(self.brewery, "bière du pont fût 20l"), twin)

    def test_a_product_added_during_the_run_is_found_exactly(self):
        resolver = ProductResolver(archive_keys={(self.brewery.pk, KEG), (self.brewery.pk, TWIN)})
        self.assertIsNone(resolver.resolve(self.brewery, TWIN))
        twin = make_product(self.brewery, TWIN)
        resolver.add(twin)

        self.assertEqual(resolver.resolve(self.brewery, TWIN), twin)
        self.assertEqual(resolver.resolve(self.brewery, KEG), self.keg)


class ArchiveProductKeysTests(TestCase):
    """What the archive names, read from the three sections naming products
    by (supplier code, raw name)."""

    def setUp(self):
        self.brewery = make_supplier(code="BRASSERIE_TEST", name="Brasserie Test")
        self.shop = make_supplier(code="EPI_TEST", name="Épicerie Test")

    def keys_of(self, payloads):
        with ArchiveReader(forge(payloads)) as reader:
            return archive_product_keys(reader, SupplierResolver())

    def test_each_section_naming_products_counts(self):
        found = self.keys_of({
            "associations": {
                "supplier_names": {"BRASSERIE_TEST": "Brasserie Test"},
                "articles": [],
                "products": [{"supplier": "BRASSERIE_TEST", "raw_name": KEG, "article": "Bière", "unit": "L"}],
            },
            "factures": {
                # Another database's code for the same shop: found by its name.
                "supplier_names": {"EPICERIE_2": "ÉPICERIE  test"},
                "products": [{"supplier": "EPICERIE_2", "raw_name": "CITRON VERT", "is_expense": False}],
                "invoices": [{"supplier": "EPICERIE_2", "lines": [{"product": ["EPICERIE_2", "SIROP DE SUCRE"]}]}],
            },
            "inventaires": {
                "supplier_names": {},
                "stock_takes": [{"taken_at": "2026-01-31T22:00:00+00:00", "lines": [
                    {"product": ["BRASSERIE_TEST", TWIN], "article": None},
                    {"product": None, "article": "Bière"},
                ]}],
                "movements": [],
            },
        })

        self.assertEqual(found, {
            (self.brewery.pk, KEG),
            (self.shop.pk, "CITRON VERT"),
            (self.shop.pk, "SIROP DE SUCRE"),
            (self.brewery.pk, TWIN),
        })

    def test_what_cannot_be_read_names_nothing(self):
        """A key of a supplier unknown here, or a record not shaped like
        one, is left out rather than refusing the archive: the section that
        reads it says why when it is imported."""
        found = self.keys_of({
            "associations": {
                "articles": [],
                "products": [
                    {"supplier": "NOWHERE", "raw_name": "X"},
                    {"supplier": "BRASSERIE_TEST", "raw_name": ""},
                    {"supplier": "BRASSERIE_TEST"},
                    "illisible",
                    {"supplier": ["BRASSERIE_TEST"], "raw_name": KEG},
                ],
            },
            "factures": {"products": {"x": 1}, "invoices": [{"lines": "?"}, {"lines": [{"product": ["A"]}]}, 3]},
            "inventaires": {"stock_takes": [{"lines": [{"product": "BRASSERIE_TEST"}]}], "movements": []},
        })

        self.assertEqual(found, set())

    def test_an_unreadable_section_names_nothing(self):
        """A section this run does not import may not even be JSON: it must
        not refuse the sections that are."""
        found = self.keys_of({
            "associations": {"articles": [], "products": [{"supplier": "BRASSERIE_TEST", "raw_name": KEG}]},
            "factures": b"{pas du json",
        })

        self.assertEqual(found, {(self.brewery.pk, KEG)})
