"""`inventory/entries.py`: what can be typed for a count or a purchase, and
in which unit - the vocabulary, the matcher and the units the stock take's
rows and the shopping lists share (SPEC_UNITS §3).

- the names and the old suffix, moved from forms.py unchanged, the longest
  name they make (`ENTRY_MAX`), and forms.py handing out only what it uses;
- `find_article` over loaded articles (the four cases of the shopping lists'
  old `find_article`);
- `EntryResolver.resolve`: the stock take's two exact rules, the lists' six
  forgiving ones, scoped to one store, in two queries whatever is typed;
- the units an entry offers (`product_units`, `article_units`), one item's
  size (`item_size`, `product_item_sizes`) and the size's bounds
  (`quantized_size`);
- the stock take's island (`forms.stock_take_entry_lookup`) the same, byte
  for byte and query for query, as the code it was before (`OldLookup`);
- `static/js/entry_units.js` - the list page's aliases, the stock take's
  exact lookup - and the stock take's page using it.

Invented data throughout: every store, article, product and figure is made up.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.html import json_script

from inventory import entries, forms
from inventory.entries import (
    ARTICLE_KIND,
    ITEMS,
    OLD_STOCK_TYPE_ENTRY_SUFFIXES,
    PRODUCT_KIND,
    SIZE_LIMIT,
    STOCK_TYPE_ENTRY_SUFFIX,
    Entry,
    EntryResolver,
    EntryUnits,
    article_units,
    current_entry_name,
    find_article,
    is_stock_type_entry,
    item_size,
    product_display_name,
    product_item_sizes,
    product_units,
    quantized_size,
    same_name,
    stock_type_entry_name,
)
from inventory.models import Product, StockType, UnitChoices
from inventory.services import first_purchase_dates, product_counting_ratios
from invoices.models import Invoice
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type, make_supplier

D = Decimal
ROOT = Path(settings.BASE_DIR)
STORE = "Grossiste exemple"
OTHER_STORE = "Épicerie exemple"


def bought(product, *pairs, day=date(2026, 1, 5)):
    """One invoice line per (quantity, total_volume) pair, at the product's
    own supplier."""
    invoice = make_invoice(supplier=product.supplier, invoice_date=day)
    for quantity, volume in pairs:
        make_invoice_line(invoice=invoice, product=product, quantity=quantity, total_volume=volume, total_ht="40")
    return product


# --------------------------------------------------------------------- the names
class NamesTests(TestCase):
    def setUp(self):
        self.vodka = make_stock_type(name="Vodka exemple", unit=UnitChoices.LITRE)
        self.product = make_product(
            supplier=make_supplier(name=STORE), raw_name="VODKA EXEMPLE 70CL", stock_type=self.vodka
        )

    def test_a_product_by_its_name_and_its_store(self):
        self.assertEqual(product_display_name(self.product), "VODKA EXEMPLE 70CL — Grossiste exemple")

    def test_an_article_by_its_name_and_the_suffix(self):
        self.assertEqual(STOCK_TYPE_ENTRY_SUFFIX, " (article)")
        self.assertEqual(stock_type_entry_name(self.vodka), "Vodka exemple (article)")
        self.assertTrue(is_stock_type_entry("Vodka exemple (article)"))
        self.assertFalse(is_stock_type_entry("Vodka exemple"))

    def test_the_old_suffix_is_renamed(self):
        self.assertEqual(OLD_STOCK_TYPE_ENTRY_SUFFIXES, (" (type de stock)",))
        self.assertEqual(current_entry_name("Vodka exemple (type de stock)"), "Vodka exemple (article)")
        self.assertEqual(current_entry_name("Vodka exemple (article)"), "Vodka exemple (article)")
        self.assertEqual(current_entry_name("Vodka exemple"), "Vodka exemple")
        self.assertTrue(is_stock_type_entry("Vodka exemple (type de stock)"))

    def test_the_longest_entry_name(self):
        """ENTRY_MAX: the longest name the vocabulary makes - a product's
        longest raw name at a store of the longest name -, read off the
        columns; an article's, old suffix included, is shorter."""
        self.assertEqual(entries.ENTRY_MAX, 255 + len(" — ") + 255)
        longest = make_product(supplier=make_supplier(name="S" * 255), raw_name="P" * 255, stock_type=self.vodka)
        self.assertEqual(len(product_display_name(longest)), entries.ENTRY_MAX)
        article = make_stock_type(name="A" * 255, unit=UnitChoices.LITRE)
        for name in (stock_type_entry_name(article), article.name + OLD_STOCK_TYPE_ENTRY_SUFFIXES[0]):
            with self.subTest(name=name[-20:]):
                self.assertLess(len(name), entries.ENTRY_MAX)

    def test_same_name_ignores_case_accents_and_spacing(self):
        for typed in ("Bière exemple", "biere exemple", "BIÈRE  EXEMPLE", "  bière\texemple "):
            with self.subTest(typed=typed):
                self.assertEqual(same_name(typed), "biere exemple")
        self.assertEqual(same_name(""), "")
        self.assertNotEqual(same_name("Bière exemple"), same_name("Bièreexemple"))

    def test_forms_still_hands_them_out(self):
        """The stock take's tests import these from forms (forms uses each
        itself); they must be entries' objects."""
        for name in ("product_display_name", "stock_type_entry_name", "is_stock_type_entry", "EntryResolver"):
            with self.subTest(name=name):
                self.assertIs(getattr(forms, name), getattr(entries, name))

    def test_forms_hands_out_nothing_it_does_not_use(self):
        """views.py imports the suffixes from entries: forms keeps no name
        only to hand it out, and needs no blanket `noqa` for one."""
        for name in ("OLD_STOCK_TYPE_ENTRY_SUFFIXES", "STOCK_TYPE_ENTRY_SUFFIX", "current_entry_name"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(forms, name))
        self.assertNotIn("noqa: F401", (ROOT / "inventory" / "forms.py").read_text(encoding="utf-8"))


# --------------------------------------------------------------------- find_article
class FindArticleTests(TestCase):
    """The shopping lists' rule, over articles already loaded."""

    def setUp(self):
        self.beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        self.coffee = make_stock_type(name="Café exemple", unit=UnitChoices.KILOGRAM)
        self.plain_coffee = make_stock_type(name="Cafe exemple", unit=UnitChoices.KILOGRAM)
        self.articles = list(StockType.objects.all())

    def test_the_exact_name_first(self):
        self.assertEqual(find_article("Bière exemple", self.articles), self.beer)
        # Two articles read alike: the exact name still decides.
        self.assertEqual(find_article("Café exemple", self.articles), self.coffee)
        self.assertEqual(find_article("Cafe exemple", self.articles), self.plain_coffee)

    def test_case_and_accents_ignored_when_one_article_reads_so(self):
        for typed in ("biere exemple", "BIÈRE EXEMPLE", "Biere  exemple"):
            with self.subTest(typed=typed):
                self.assertEqual(find_article(typed, self.articles), self.beer)

    def test_two_articles_reading_alike_is_a_free_text(self):
        self.assertIsNone(find_article("CAFE EXEMPLE", self.articles))

    def test_an_unknown_name_is_a_free_text(self):
        for typed in ("Pain exemple", "", "Bière"):
            with self.subTest(typed=typed):
                self.assertIsNone(find_article(typed, self.articles))

    def test_it_reads_nothing(self):
        with self.assertNumQueries(0):
            self.assertEqual(find_article("biere exemple", self.articles), self.beer)

    def test_any_iterable_is_read_once(self):
        self.assertEqual(find_article("biere exemple", iter(self.articles)), self.beer)


# --------------------------------------------------------------------- resolve
class ResolveTests(TestCase):
    """SPEC_UNITS §2.1, the rules in their order."""

    def setUp(self):
        self.store = make_supplier(name=STORE)
        self.other_store = make_supplier(name=OTHER_STORE)
        self.vodka = make_stock_type(name="Vodka exemple", unit=UnitChoices.LITRE)
        self.gin = make_stock_type(name="Gin exemple", unit=UnitChoices.LITRE)
        self.coffee = make_stock_type(name="Café exemple", unit=UnitChoices.KILOGRAM)
        self.plain_coffee = make_stock_type(name="Cafe exemple", unit=UnitChoices.KILOGRAM)
        self.soda = make_stock_type(name="Soda exemple", unit=UnitChoices.LITRE)
        self.tonic = make_stock_type(name="Tonic exemple", unit=UnitChoices.LITRE)
        self.vodka_70 = make_product(supplier=self.store, raw_name="VODKA EXEMPLE 70CL", stock_type=self.vodka)
        self.gin_70 = make_product(supplier=self.store, raw_name="GIN EXEMPLE 70CL X6", stock_type=self.gin)
        # A product of the store whose name reads like another article's.
        self.tonic_can = make_product(supplier=self.store, raw_name="TONIC EXEMPLE", stock_type=self.soda)
        self.unclassified = make_product(supplier=self.store, raw_name="SAC EXEMPLE", stock_type=None)
        self.vodka_1l = make_product(supplier=self.other_store, raw_name="VODKA EXEMPLE 1L", stock_type=self.vodka)
        self.lists = EntryResolver(supplier_id=self.store.pk)

    def article(self, article):
        return Entry(ARTICLE_KIND, article, None)

    def product(self, product):
        return Entry(PRODUCT_KIND, product.stock_type, product)

    def test_rule_1_a_product_of_the_store_by_its_display_name(self):
        typed = "VODKA EXEMPLE 70CL — Grossiste exemple"
        for forgiving in (False, True):
            with self.subTest(forgiving=forgiving):
                self.assertEqual(self.lists.resolve(typed, forgiving=forgiving), self.product(self.vodka_70))

    def test_rule_2_an_article_by_its_entry_name_or_the_old_one(self):
        for typed in ("Vodka exemple (article)", "Vodka exemple (type de stock)", "  Vodka exemple (article) "):
            for forgiving in (False, True):
                with self.subTest(typed=typed, forgiving=forgiving):
                    self.assertEqual(self.lists.resolve(typed, forgiving=forgiving), self.article(self.vodka))

    def test_rule_3_the_suffix_written_otherwise(self):
        for typed in (
            "vodka EXEMPLE (Article)",
            "Vodka exemple(article)",
            "VODKA EXEMPLE (TYPE DE STOCK)",
            "Vodka exemple ( article )",
            "vodka exemple (type  de stock)",
            "Vodka exemple (ARTICLÉ)",
        ):
            with self.subTest(typed=typed):
                self.assertEqual(self.lists.resolve(typed, forgiving=True), self.article(self.vodka))
                self.assertIsNone(self.lists.resolve(typed))
        # The exact name still decides between two articles reading alike.
        self.assertEqual(self.lists.resolve("Café exemple (ARTICLE)", forgiving=True), self.article(self.coffee))
        self.assertIsNone(self.lists.resolve("CAFE EXEMPLE (article)", forgiving=True))
        # The suffix names no article whatever is in front of it: a free text.
        self.assertIsNone(self.lists.resolve("Pain exemple (article)", forgiving=True))
        self.assertIsNone(self.lists.resolve("(article)", forgiving=True))

    def test_rule_4_an_article_by_its_name_alone(self):
        for typed in ("Vodka exemple", "vodka exemple", " VODKA  EXEMPLE ", "Gin Exemple"):
            with self.subTest(typed=typed):
                expected = self.gin if "gin" in typed.lower() else self.vodka
                self.assertEqual(self.lists.resolve(typed, forgiving=True), self.article(expected))

    def test_two_articles_reading_alike_and_no_product_is_nothing(self):
        self.assertIsNone(self.lists.resolve("CAFE EXEMPLE", forgiving=True))
        self.assertEqual(self.lists.resolve("Café exemple", forgiving=True), self.article(self.coffee))

    def test_rule_5_a_product_of_the_store_by_its_raw_name(self):
        self.assertEqual(self.lists.resolve("GIN EXEMPLE 70CL X6", forgiving=True), self.product(self.gin_70))
        for typed in ("gin exemple 70cl x6", "Gin  Exemple 70cl X6", "gin exemple 70cl x6 — grossiste exemple"):
            with self.subTest(typed=typed):
                self.assertEqual(self.lists.resolve(typed, forgiving=True), self.product(self.gin_70))

    def test_an_article_wins_over_a_product_reading_alike(self):
        self.assertEqual(self.lists.resolve("tonic exemple", forgiving=True), self.article(self.tonic))
        self.assertEqual(self.lists.resolve("TONIC EXEMPLE", forgiving=True), self.article(self.tonic))
        # By its display name, the product is still reached.
        self.assertEqual(
            self.lists.resolve("TONIC EXEMPLE — Grossiste exemple", forgiving=True), self.product(self.tonic_can)
        )

    def test_another_store_s_product_is_no_product_here(self):
        for typed in ("VODKA EXEMPLE 1L — Épicerie exemple", "VODKA EXEMPLE 1L", "vodka exemple 1l"):
            with self.subTest(typed=typed):
                self.assertIsNone(self.lists.resolve(typed, forgiving=True))
        # The stock take's resolver is every store's.
        self.assertEqual(EntryResolver().resolve("VODKA EXEMPLE 1L — Épicerie exemple"), self.product(self.vodka_1l))

    def test_not_forgiving_is_the_stock_take_s_two_exact_rules(self):
        for typed in ("Vodka exemple", "vodka exemple (article)", "GIN EXEMPLE 70CL X6", "gin exemple 70cl x6"):
            with self.subTest(typed=typed):
                self.assertIsNone(self.lists.resolve(typed))
                self.assertIsNone(EntryResolver().resolve(typed))

    def test_an_unclassified_product_is_nothing(self):
        for typed in ("SAC EXEMPLE — Grossiste exemple", "SAC EXEMPLE", "sac exemple"):
            with self.subTest(typed=typed):
                self.assertIsNone(self.lists.resolve(typed, forgiving=True))
                self.assertIsNone(EntryResolver().resolve(typed, forgiving=True))

    def test_nothing_typed_is_nothing(self):
        for typed in ("", "   ", None, 7):
            with self.subTest(typed=typed):
                self.assertIsNone(self.lists.resolve(typed, forgiving=True))

    def test_two_queries_whatever_is_resolved(self):
        resolver = EntryResolver(supplier_id=self.store.pk)
        with self.assertNumQueries(2):
            for _ in range(3):
                for typed in (
                    "VODKA EXEMPLE 70CL — Grossiste exemple",
                    "Vodka exemple (article)",
                    "vodka EXEMPLE (Article)",
                    "vodka exemple",
                    "gin exemple 70cl x6",
                    "CAFE EXEMPLE",
                    "Pain exemple",
                ):
                    resolver.resolve(typed, forgiving=True)

    def test_the_stock_take_s_lookups_are_unchanged(self):
        resolver = EntryResolver()
        self.assertEqual(resolver.product("VODKA EXEMPLE 70CL — Grossiste exemple"), self.vodka_70)
        self.assertEqual(resolver.product("VODKA EXEMPLE 1L — Épicerie exemple"), self.vodka_1l)
        self.assertIsNone(resolver.product("vodka exemple 70cl — grossiste exemple"))
        self.assertEqual(resolver.stock_type("Vodka exemple (type de stock)"), self.vodka)
        self.assertIsNone(resolver.stock_type("vodka exemple (article)"))
        # Scoped: the store's products only, every article.
        self.assertIsNone(self.lists.product("VODKA EXEMPLE 1L — Épicerie exemple"))
        self.assertEqual(self.lists.stock_type("Gin exemple (article)"), self.gin)

    def test_an_entry_is_a_value(self):
        self.assertEqual(self.lists.resolve("Vodka exemple", forgiving=True), Entry(ARTICLE_KIND, self.vodka, None))
        entry = self.lists.resolve("GIN EXEMPLE 70CL X6", forgiving=True)
        self.assertEqual((entry.kind, entry.article, entry.product), (PRODUCT_KIND, self.gin, self.gin_70))
        with self.assertNumQueries(0):
            self.assertEqual(entry.article.unit, UnitChoices.LITRE)


# --------------------------------------------------------------------- units
class ProductUnitsTests(TestCase):
    """The stock take's rule (forms._unit_choices_for_product), as values."""

    def setUp(self):
        self.supplier = make_supplier(name=STORE)

    def product(self, unit):
        return make_product(supplier=self.supplier, stock_type=make_stock_type(unit=unit), stock_equivalent="0.7")

    def test_a_product_of_a_unit_article_is_counted_in_items_only(self):
        product = self.product(UnitChoices.UNIT)
        for discrete in (True, False):
            with self.subTest(discrete=discrete):
                self.assertEqual(product_units(product, is_discrete=discrete), EntryUnits((ITEMS,), ITEMS))

    def test_a_product_of_a_litre_article(self):
        product = self.product(UnitChoices.LITRE)
        self.assertEqual(product_units(product, is_discrete=True), EntryUnits((ITEMS, "L"), ITEMS))
        self.assertEqual(product_units(product, is_discrete=False), EntryUnits((ITEMS, "L"), "L"))

    def test_a_weighed_product_of_a_kilo_article_defaults_to_kilos(self):
        product = self.product(UnitChoices.KILOGRAM)
        self.assertEqual(product_units(product, is_discrete=False), EntryUnits((ITEMS, "KG"), "KG"))
        self.assertEqual(product_units(product, is_discrete=True), EntryUnits((ITEMS, "KG"), ITEMS))

    def test_items_are_the_stock_take_s_own_value(self):
        self.assertEqual(ITEMS, UnitChoices.UNIT)
        self.assertEqual((PRODUCT_KIND, ARTICLE_KIND), ("product", "stock_type"))

    def test_the_stock_take_s_labels_are_unchanged(self):
        self.assertEqual(
            forms._unit_choices_for_product(self.product(UnitChoices.LITRE), True),
            ([["UNIT", "Unité (bouteilles/packs)"], ["L", "Litre (mesuré directement)"]], "UNIT"),
        )
        self.assertEqual(
            forms._unit_choices_for_product(self.product(UnitChoices.KILOGRAM), False),
            ([["UNIT", "Unité (bouteilles/packs)"], ["KG", "Kilogramme (mesuré directement)"]], "KG"),
        )
        self.assertEqual(
            forms._unit_choices_for_product(self.product(UnitChoices.UNIT), False), ([["UNIT", "Unité"]], "UNIT")
        )


class ArticleUnitsTests(TestCase):
    def test_no_flag_is_the_article_s_own_unit(self):
        for unit in (UnitChoices.LITRE, UnitChoices.KILOGRAM):
            with self.subTest(unit=unit):
                self.assertEqual(article_units(make_stock_type(unit=unit)), EntryUnits((unit,), unit))

    def test_with_items_the_measure_is_the_default(self):
        litre = make_stock_type(unit=UnitChoices.LITRE)
        self.assertEqual(article_units(litre, items=True), EntryUnits((ITEMS, "L"), "L"))

    def test_items_by_default(self):
        litre = make_stock_type(unit=UnitChoices.LITRE)
        self.assertEqual(article_units(litre, items=True, items_by_default=True), EntryUnits((ITEMS, "L"), ITEMS))
        # With no item to count, nothing to choose by default.
        self.assertEqual(article_units(litre, items_by_default=True), EntryUnits(("L",), "L"))

    def test_a_unit_article_always_counts_items(self):
        pieces = make_stock_type(unit=UnitChoices.UNIT)
        for flags in ({}, {"items": True}, {"items": True, "items_by_default": True}, {"items_by_default": True}):
            with self.subTest(flags=flags):
                self.assertEqual(article_units(pieces, **flags), EntryUnits((ITEMS,), ITEMS))


# --------------------------------------------------------------------- one item's size
class QuantizedSizeTests(SimpleTestCase):
    def test_four_places_half_up(self):
        for size, expected in (
            (D("0.7"), D("0.7000")),
            (D("0.00005"), D("0.0001")),
            (D("0.33333"), D("0.3333")),
            (D("0.66665"), D("0.6667")),
            (D("30"), D("30.0000")),
            (7, D("7.0000")),
            (D("999999.9999"), D("999999.9999")),
        ):
            with self.subTest(size=size):
                quantized = quantized_size(size)
                self.assertEqual(quantized, expected)
                self.assertEqual(quantized.as_tuple().exponent, -4)

    def test_what_is_no_size(self):
        for size in (
            None,
            D("0"),
            D("0.00004"),
            D("-0.7"),
            D("-0.00004"),
            D("999999.99995"),
            SIZE_LIMIT,
            D("1E+30"),
            D("NaN"),
            D("sNaN"),
            D("Infinity"),
            D("-Infinity"),
            True,
            "abc",
        ):
            with self.subTest(size=size):
                self.assertIsNone(quantized_size(size))

    def test_the_bound_is_the_column_s(self):
        self.assertEqual(SIZE_LIMIT, D("1000000"))
        self.assertEqual((entries.SIZE_PLACES, entries.SIZE_DIGITS), (4, 10))


class ItemSizeTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(name=STORE)
        self.vodka = make_stock_type(name="Vodka exemple", unit=UnitChoices.LITRE)

    def product(self, unit=UnitChoices.LITRE, stock_equivalent="1"):
        return make_product(supplier=self.supplier, stock_type=self.vodka, unit=unit, stock_equivalent=stock_equivalent)

    def size(self, product):
        return item_size(product, product_counting_ratios([product.pk]))

    def test_a_bottle_from_its_printed_volume(self):
        bottle = bought(self.product(), (6, "4.2"), (12, "8.4"))
        self.assertEqual(self.size(bottle), D("0.7000"))

    def test_a_bottle_from_its_factor(self):
        self.assertEqual(self.size(self.product(UnitChoices.UNIT, "0.7")), D("0.7000"))

    def test_a_keg(self):
        self.assertEqual(self.size(self.product(UnitChoices.UNIT, "30")), D("30.0000"))
        self.assertEqual(self.size(bought(self.product(), (2, "60"))), D("30.0000"))

    def test_a_weighed_product_has_no_size(self):
        weighed = bought(self.product(), (1, "1.234"), (1, "0.987"))
        self.assertIsNone(self.size(weighed))

    def test_a_third_is_four_places_half_up(self):
        self.assertEqual(self.size(bought(self.product(), (3, "1"))), D("0.3333"))

    def test_what_no_column_holds_is_no_size(self):
        self.assertIsNone(self.size(self.product(UnitChoices.UNIT, "0")))
        self.assertIsNone(self.size(self.product(UnitChoices.UNIT, "-0.7")))
        self.assertIsNone(self.size(bought(self.product(stock_equivalent="1000"), (1, "1000"))))

    def test_the_ratios_handed_in_are_all_it_reads(self):
        bottle = bought(self.product(), (6, "4.2"))
        ratios = product_counting_ratios([bottle.pk])
        with self.assertNumQueries(0):
            self.assertEqual(item_size(bottle, ratios), D("0.7000"))
            # No ratio at all: the factor alone, as the stock take counts it.
            self.assertEqual(item_size(bottle, {}), D("1.0000"))

    def test_many_products_in_two_queries(self):
        bottle = bought(self.product(), (6, "4.2"))
        weighed = bought(self.product(), (1, "1.234"), (1, "0.987"))
        keg = self.product(UnitChoices.UNIT, "30")
        nothing = self.product(UnitChoices.UNIT, "0")
        with self.assertNumQueries(2):
            sizes = product_item_sizes([bottle.pk, weighed.pk, keg.pk, nothing.pk])
        self.assertEqual(sizes, {bottle.pk: D("0.7000"), keg.pk: D("30.0000")})
        more = [bought(self.product(UnitChoices.UNIT, "0.75")).pk for _ in range(6)]
        with self.assertNumQueries(2):
            sizes = product_item_sizes([bottle.pk, *more])
        self.assertEqual(len(sizes), 7)
        with self.assertNumQueries(0):
            self.assertEqual(product_item_sizes([]), {})
            self.assertEqual(product_item_sizes([None]), {})


# --------------------------------------------------------------------- the stock take's island
def old_stock_take_entry_lookup() -> dict[str, dict]:
    """forms.stock_take_entry_lookup as it was before entries.py (a281be2),
    copied whole: what the page shipped, to compare the new one with."""
    products = list(
        Product.objects.select_related("supplier", "stock_type")
        .filter(stock_type__isnull=False)
        .only("raw_name", "supplier__name", "stock_type__unit")
    )
    ratios = product_counting_ratios([p.id for p in products])
    first_purchases = first_purchase_dates([product.id for product in products])
    found = {}
    for product in products:
        is_discrete = len(ratios.get(product.id, set())) <= 1
        stock_unit = product.stock_type.unit
        if stock_unit == UnitChoices.UNIT:
            choices, default = [[UnitChoices.UNIT, "Unité"]], UnitChoices.UNIT
        else:
            choices = [
                [UnitChoices.UNIT, "Unité (bouteilles/packs)"],
                [stock_unit, f"{product.stock_type.get_unit_display()} (mesuré directement)"],
            ]
            default = UnitChoices.UNIT if is_discrete else stock_unit
        first = first_purchases.get(product.id)
        found[f"{product.raw_name} — {product.supplier.name}"] = {
            "kind": "product",
            "unit_choices": choices,
            "default_unit": default,
            "available_from": first.isoformat() if first else None,
        }
    earliest_by_type: dict[int, object] = {}
    for product in products:
        first = first_purchases.get(product.id)
        if first is None:
            continue
        current = earliest_by_type.get(product.stock_type_id)
        if current is None or first < current:
            earliest_by_type[product.stock_type_id] = first
    for stock_type in StockType.objects.all():
        first = earliest_by_type.get(stock_type.id)
        found[f"{stock_type.name} (article)"] = {
            "kind": "stock_type",
            "unit_choices": [[stock_type.unit, stock_type.get_unit_display()]],
            "default_unit": stock_type.unit,
            "available_from": first.isoformat() if first else None,
        }
    return found


class OldLookupTests(TestCase):
    """The stock take's island is the same, byte for byte, in the same four
    queries: every kind of product and article it can hold."""

    def setUp(self):
        store = make_supplier(name=STORE)
        other = make_supplier(name=OTHER_STORE)
        vodka = make_stock_type(name="Vodka exemple", unit=UnitChoices.LITRE)
        lemon = make_stock_type(name="Citron exemple", unit=UnitChoices.KILOGRAM)
        cups = make_stock_type(name="Gobelets exemple", unit=UnitChoices.UNIT)
        make_stock_type(name="Jamais acheté exemple", unit=UnitChoices.LITRE)
        bought(make_product(supplier=store, raw_name="VODKA EXEMPLE 70CL", stock_type=vodka, unit="L"), (6, "4.2"))
        bought(
            make_product(supplier=other, raw_name="VODKA EXEMPLE VRAC", stock_type=vodka, unit="L"),
            (1, "1.234"),
            (1, "0.987"),
            day=date(2025, 12, 1),
        )
        make_product(supplier=store, raw_name="VODKA SANS ACHAT", stock_type=vodka, unit="L")
        bought(
            make_product(supplier=store, raw_name="CITRON VRAC", stock_type=lemon, unit="KG"), (1, "2.5"), (1, "3.1")
        )
        bought(make_product(supplier=store, raw_name="CITRON FILET 1KG", stock_type=lemon, unit="KG"), (4, "4"))
        bought(make_product(supplier=store, raw_name="GOBELETS X50", stock_type=cups, stock_equivalent="50"), (2, "0"))
        make_product(supplier=store, raw_name="SAC EXEMPLE", stock_type=None)
        undated = bought(
            make_product(supplier=other, raw_name="CITRON SANS DATE", stock_type=lemon, unit="KG"), (1, "1.5")
        )
        self.assertEqual(Invoice.objects.filter(lines__product=undated).update(invoice_date=None), 1)

    def test_the_same_island_byte_for_byte(self):
        old = old_stock_take_entry_lookup()
        with self.assertNumQueries(4):
            new = forms.stock_take_entry_lookup()
        self.assertEqual(list(new), list(old))
        self.assertEqual(new, old)
        self.assertEqual(json.dumps(new, ensure_ascii=False), json.dumps(old, ensure_ascii=False))
        self.assertEqual(json_script(new, "entry-data"), json_script(old, "entry-data"))
        # What it holds, read back: every kind is there.
        self.assertEqual(new["VODKA EXEMPLE VRAC — Épicerie exemple"]["default_unit"], "L")
        self.assertEqual(new["CITRON VRAC — Grossiste exemple"]["default_unit"], "KG")
        self.assertEqual(new["CITRON FILET 1KG — Grossiste exemple"]["default_unit"], "UNIT")
        self.assertEqual(new["GOBELETS X50 — Grossiste exemple"]["unit_choices"], [["UNIT", "Unité"]])
        self.assertEqual(new["Gobelets exemple (article)"]["unit_choices"], [["UNIT", "Unité"]])
        self.assertEqual(new["Jamais acheté exemple (article)"]["available_from"], None)
        self.assertEqual(new["Vodka exemple (article)"]["available_from"], "2025-12-01")
        self.assertNotIn("SAC EXEMPLE — Grossiste exemple", new)


# --------------------------------------------------------------------- the script
#: What a script writing markup looks like (tests/test_ui.py's MARKUP_WRITERS).
MARKUP_WRITERS = (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*[+]?=", r"insertAdjacentHTML", r"document\.write")
#: ES2015 and later, as written in a statement (tests/test_ui.py's NOT_ES5).
NOT_ES5 = (
    r"=>",
    r"`",
    r"\blet\s+[A-Za-z_$]",
    r"\bconst\s+[A-Za-z_$]",
    r"\basync\s+function",
    r"\bawait\s",
    r"\bclass\s+[A-Z]",
    r"\.\.\.[A-Za-z_$]",
)
SCRIPT = "static/js/entry_units.js"
STOCK_TAKE_TEMPLATE = "inventory/templates/inventory/stock_take_form.html"


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def code(relative: str) -> str:
    """The file with its comments taken out."""
    text = re.sub(r"/\*.*?\*/", "", source(relative), flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def function_body(text: str, name: str) -> str:
    """`function name(...) { ... }` as written, up to its closing brace at
    the start of a line."""
    found = re.search(rf"\nfunction {name}\([^)]*\) \{{\n(.*?)\n\}}\n", text, re.DOTALL)
    assert found, f"no function {name}"
    return found.group(1)


class EntryUnitsScriptTests(SimpleTestCase):
    def test_es5_in_a_strict_function(self):
        text = code(SCRIPT)
        self.assertTrue(text.lstrip().startswith("(function () {"))
        self.assertIn('"use strict";', text)
        self.assertTrue(text.rstrip().endswith("})();"))
        for pattern in NOT_ES5:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))

    def test_nodes_and_text_only(self):
        text = source(SCRIPT)
        for pattern in MARKUP_WRITERS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))
        self.assertIn('document.createElement("option")', text)
        self.assertIn(".textContent = ", text)

    def test_nothing_kept_in_the_browser(self):
        self.assertIsNone(re.search(r"\b(?:localStorage|sessionStorage|indexedDB|document\.cookie)\b", source(SCRIPT)))

    def test_what_it_hands_out_and_what_wires_itself(self):
        text = code(SCRIPT)
        self.assertIn("window.MarginMateEntryUnits = { lookup: lookup, fill: fill };", text)
        self.assertIn('"select[data-entry-units][data-entry-field]"', text)
        # A name typed with spaces around it, and only the island's own names.
        self.assertIn(".trim()", function_body_of_script("lookup"))
        self.assertIn("hasOwnProperty", function_body_of_script("lookup"))
        # The options the server drew are kept to be put back.
        self.assertIn("new WeakMap()", text)

    def test_the_list_page_s_aliases_and_the_stock_take_s_exact_names(self):
        """A name the lists' server accepts though it is no name of the
        island (« vodka exemple »): `lookup` falls back to the aliases the
        page ships - exactly, then folded (case, accents and spacing aside,
        `entries.same_name`'s fold) - and only when it is handed them. The
        stock take hands none: its server is exact, and so is its lookup."""
        lookup = function_body_of_script("lookup")
        self.assertIn("function lookup(data, typed, aliases)", code(SCRIPT))
        self.assertIn("aliases.exact", lookup)
        self.assertIn("aliases.folded", lookup)
        self.assertIn("fold(name)", lookup)
        # Exact first: the island's own names before any alias.
        self.assertLess(lookup.index("hasOwnProperty"), lookup.index("aliases"))
        fold = function_body_of_script("fold")
        for step in (".toLowerCase()", '.normalize("NFD")', "COMBINING_MARKS", ".trim()"):
            with self.subTest(step=step):
                self.assertIn(step, fold)
        # Read from the island the select names; the stock take's names none.
        wire = function_body_of_script("wire")
        self.assertIn('select.getAttribute("data-entry-aliases")', wire)
        self.assertIn("lookup(data, field.value, aliases)", wire)
        self.assertNotIn("data-entry-aliases", source(STOCK_TAKE_TEMPLATE))
        self.assertNotIn("MarginMateEntryUnits.lookup(entryData, input.value,", code(STOCK_TAKE_TEMPLATE))


def function_body_of_script(name: str) -> str:
    """A function of entry_units.js (indented four spaces inside its IIFE)."""
    found = re.search(rf"\n    function {name}\([^)]*\) \{{\n(.*?)\n    \}}\n", code(SCRIPT), re.DOTALL)
    assert found, f"no function {name} in {SCRIPT}"
    return found.group(1)


class StockTakeFormScriptTests(TestCase):
    """The stock take's rows fill their select through entry_units.js: a
    name typed with spaces around it gets its units and its price, and a
    name edited into an unknown one puts the server's options back."""

    def test_the_page_loads_the_script_deferred(self):
        page = self.client.get(reverse("inventory:stock_take_create")).content.decode()
        found = re.search(r'<script src="(/static/js/entry_units\.js\?v=[^"]*)" defer></script>', page)
        self.assertIsNotNone(found)
        # Before the page's own script, which calls it once the page is read.
        self.assertLess(page.index("js/entry_units.js"), page.index("function updateUnitChoices"))

    def test_the_rows_fill_their_select_through_it(self):
        text = code(STOCK_TAKE_TEMPLATE)
        body = function_body(text, "updateUnitChoices")
        self.assertIn(
            "MarginMateEntryUnits.fill(select, MarginMateEntryUnits.lookup(entryData, input.value), useDefault)", body
        )
        self.assertNotIn("innerHTML", body)

    def test_a_row_is_priced_by_the_same_lookup(self):
        body = function_body(code(STOCK_TAKE_TEMPLATE), "priceRow")
        self.assertIn("MarginMateEntryUnits.lookup(entryData, entry.value)", body)

    def test_no_entry_is_looked_up_by_the_raw_text_any_more(self):
        self.assertIsNone(re.search(r"entryData\[\s*(?:input|entry)\.value\s*\]", code(STOCK_TAKE_TEMPLATE)))

    def test_a_script_that_never_arrived_stops_nothing(self):
        """The rows, the first row of a new count and the draft offered back
        run at DOMContentLoaded through updateUnitChoices: should
        entry_units.js not have arrived, they must not throw there."""
        text = code(STOCK_TAKE_TEMPLATE)
        for name in ("updateUnitChoices", "priceRow"):
            with self.subTest(function=name):
                body = function_body(text, name)
                self.assertIn("window.MarginMateEntryUnits", body)
                self.assertLess(body.index("window.MarginMateEntryUnits"), body.index("MarginMateEntryUnits.lookup"))
