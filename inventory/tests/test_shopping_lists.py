"""« Listes de courses », the service module (`inventory/shopping_lists.py`)
without a request: the words a quantity and a pack are written in, the
figures a forecast line or the store's usual purchase gives an item, what
fits the quantity's and the size's columns, what a typed name and a typed
quantity read as, the stores offered, the one open list per store made by
the first item, adding an item once - a ticked one put back to buy, a list
finished or an add made meanwhile -, the tick as the WANTED state, finishing
a list with its carry-over, the run order, who did what, and the names moved
to entries.py gone from here.

Bottles or litres (SPEC_UNITS §2.2-§2.7): what one item (« une bouteille »)
of an article is at a store (`article_item`, `article_item_of`), the units an
entry offers and its default (`entry_units`), their French labels
(`entry_labels`, `unit_label` - a bottle, a keg or a pack: what the item
is), the figures each entry and unit store (`entry_figures`), what the
card's items option counts (`card_item`) and the card's units, labels and
figures through it (`card_units`, `card_labels`, `card_figures`: the label
is what the save stores) - each a table on unsaved, invented articles -,
and the add menu (`list_entries`) read from the database in seven queries,
agreeing with what the add's own chain stores.

`ConcurrentAddTests` races real connections on a real espace file, with
production's SQLite options.

Invented data throughout: every store, article, login and figure is made up.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
import traceback
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection, connections, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts import paths
from accounts.models import Membership, Tenant
from accounts.tenancy import bound, bound_tenant
from accounts.tests.support import TenancyTestCase
from common import plain_number
from inventory import entries, shopping, shopping_data, shopping_lists
from inventory.entries import ARTICLE_KIND, ITEMS, PRODUCT_KIND, Entry, EntryResolver, EntryUnits
from inventory.models import (
    MovementKind,
    Product,
    ShoppingList,
    ShoppingListItem,
    StockMovement,
    StockType,
    UnitChoices,
)
from inventory.services import is_discrete_count, product_counting_ratios
from inventory.shopping_lists import Figures, Finished
from invoices.models import Supplier
from tests.factories import make_invoice, make_invoice_line, make_movement, make_product, make_stock_type, make_supplier
from tests.runner import TEST_TENANT_PK
from tests.test_views_smoke import make_shopping_history

D = Decimal
L, KG, UNIT = UnitChoices.LITRE, UnitChoices.KILOGRAM, UnitChoices.UNIT
EMPLOYEE = "employe.exemple@exemple.fr"
OTHER = "autre.exemple@exemple.fr"
OWNER = "gerant.exemple@exemple.fr"
BEER_FIGURES = Figures(D("24"), "", 7, "BIERE EXEMPLE 33CL X24", 24)
ONE = Figures(D("1"), "", None, "", None)
GIN_PRODUCT = "GIN EXEMPLE 70CL X6"
BOTTLE = D("0.7")


def gin_bottles(quantity="6") -> Figures:
    """Gin bottles of 70 cl as an item counts them: the store's product, its
    pack of 6, the size of one."""
    return Figures(D(quantity), "", 21, GIN_PRODUCT, 6, BOTTLE, L)


def outcome(name: str):
    """`add_item`'s outcome `name` (ADDED, RELISTED, ALREADY), read when a
    test runs."""
    return getattr(shopping_lists.AddOutcome, name)


def make_shopping_list(supplier=None, *, finished=False, **fields) -> ShoppingList:
    if finished:
        fields.setdefault("finished_at", timezone.now())
    return ShoppingList.objects.create(supplier=supplier or make_supplier(name="Magasin exemple"), **fields)


def make_shopping_item(
    shopping_list=None, stock_type=None, *, label="", quantity="1", unit="", **fields
) -> ShoppingListItem:
    return ShoppingListItem.objects.create(
        shopping_list=shopping_list or make_shopping_list(),
        stock_type=stock_type,
        label=label or (stock_type.name if stock_type is not None else "Article exemple"),
        quantity=D(quantity),
        unit=unit,
        **fields,
    )


def bought(store, article, quantity="1", total_ht="40.00"):
    """One purchase of `article` at `store`: an invoice line and its PURCHASE
    movement."""
    line = make_invoice_line(
        invoice=make_invoice(supplier=store),
        product=make_product(supplier=store, stock_type=article),
        total_ht=total_ht,
    )
    return make_movement(stock_type=article, quantity=quantity, unit_cost_ht="40", invoice_line=line)


def a_line(**fields) -> shopping.Line:
    """A forecast line (the pure module's), the figures a test cares about
    given."""
    values = {
        "article_id": 1,
        "name": "Bière exemple",
        "unit": UnitChoices.UNIT,
        "category": "",
        "section": shopping.TO_BUY,
        "chance": 0.8,
        "confidence": "presque sûr",
        "sentence": "",
        "badges": (),
        "qty": D("24"),
        "product_id": 7,
        "product_name": "BIERE EXEMPLE 33CL X24",
        "product_units": D("24"),
        "packs": (1, D("24")),
        "multiplier": 1,
        "last_bought_here": date(2026, 9, 1),
        "habit": 0.9,
        "need": 1.0,
        "clock": shopping.CALENDAR_CLOCK,
    }
    values.update(fields)
    return shopping.Line(**values)


def invented() -> SimpleNamespace:
    """Unsaved articles and products the pure rules are read on (invented):
    a gin bought at the store as « GIN EXEMPLE 70CL X6 » (70 cl, packs of
    6), a vodka never bought there, a juice bought there by measure, a keg
    of 30 L, olives weighed, cups in packs of 50 and a beer counted in its
    own units - and what the store's usual purchases read as
    (`figures_of_usual`'s answers) and the items they give."""
    made = SimpleNamespace()
    made.gin = StockType(pk=11, name="Gin exemple", unit=L)
    made.vodka = StockType(pk=12, name="Vodka exemple", unit=L)
    made.juice = StockType(pk=13, name="Jus exemple", unit=L)
    made.keg = StockType(pk=14, name="Bière pression exemple", unit=L)
    made.olives = StockType(pk=15, name="Olives exemple", unit=KG)
    made.cups = StockType(pk=16, name="Gobelets exemple", unit=UNIT)
    made.beer = StockType(pk=17, name="Bière exemple", unit=UNIT)
    store = Supplier(pk=900, name="Grossiste exemple")
    made.gin_bottle = Product(pk=21, raw_name=GIN_PRODUCT, stock_type=made.gin, supplier=store, unit=L)
    made.other_gin = Product(pk=22, raw_name="GIN EXEMPLE 1L", stock_type=made.gin, supplier=store, unit=L)
    made.olives_bag = Product(pk=23, raw_name="OLIVES EXEMPLE VRAC", stock_type=made.olives, supplier=store, unit=KG)
    made.cups_pack = Product(pk=24, raw_name="GOBELETS EXEMPLE X50", stock_type=made.cups, supplier=store, unit=UNIT)
    made.gin_carton = Product(
        pk=26, raw_name="GIN EXEMPLE CARTON 6X70CL", stock_type=made.gin, supplier=store, unit=UNIT
    )
    made.gin_usual = Figures(D("6"), "", 21, GIN_PRODUCT, 6)
    made.juice_usual = Figures(D("2.5"), L, None, "", None)
    made.olives_usual = Figures(D("3"), "", 23, "OLIVES EXEMPLE VRAC", None)
    made.cups_usual = Figures(D("4"), "", 24, "GOBELETS EXEMPLE X50", None)
    made.beer_usual = Figures(D("12"), UNIT, None, "", None)
    item_of = shopping_lists.ItemOf
    made.gin_item = item_of(D("0.7000"), L, 21, GIN_PRODUCT, 6)
    made.bottle = item_of(D("0.7000"), L, None, "", None)
    made.litre = item_of(D("1.0000"), L, None, "", None)
    made.keg_item = item_of(D("30.0000"), L, None, "", None)
    made.half_kilo = item_of(D("0.5000"), KG, None, "", None)
    made.olives_item = item_of(None, "", 23, "OLIVES EXEMPLE VRAC", None)
    made.cups_item = item_of(D("50.0000"), UNIT, 24, "GOBELETS EXEMPLE X50", None)
    # « GIN EXEMPLE 1L », another product of the store, found again with its size.
    made.other_gin_item = item_of(D("1.0000"), L, 22, "GIN EXEMPLE 1L", None)
    return made


def article_entry(article) -> Entry:
    return Entry(ARTICLE_KIND, article, None)


def product_entry(product) -> Entry:
    return Entry(PRODUCT_KIND, product.stock_type, product)


def an_item(article=None, quantity="1", **fields) -> ShoppingListItem:
    """An item as the card reads it, never saved: of `article`, else a free
    text."""
    fields.setdefault("label", article.name if article is not None else "Pain exemple")
    return ShoppingListItem(stock_type=article, quantity=D(quantity), **fields)


class ConstantsTests(TestCase):
    def test_the_bounds_are_the_fields(self):
        field = ShoppingListItem._meta.get_field
        self.assertEqual(shopping_lists.LABEL_MAX, field("label").max_length)
        self.assertEqual(shopping_lists.NOTE_MAX, field("note").max_length)
        self.assertEqual(
            (shopping_lists.QUANTITY_PLACES, shopping_lists.QUANTITY_DIGITS),
            (field("quantity").decimal_places, field("quantity").max_digits),
        )
        self.assertEqual(shopping_lists.PACK_RANGE, (2, 9999))
        self.assertEqual(shopping_lists.RECENT_FINISHED, 10)

    def test_every_unit_has_its_symbol(self):
        self.assertEqual(
            shopping_lists.UNIT_SYMBOLS, {UnitChoices.LITRE: "L", UnitChoices.KILOGRAM: "kg", UnitChoices.UNIT: "u."}
        )

    def test_the_words_of_an_item(self):
        self.assertEqual(
            shopping_lists.ITEM_NOUNS,
            {L: ("bouteille", "bouteilles"), KG: ("paquet", "paquets"), UNIT: ("paquet", "paquets")},
        )
        self.assertEqual((shopping_lists.KEG_NOUNS, shopping_lists.KEG_FROM), (("fût", "fûts"), D("3")))
        self.assertEqual(shopping_lists.PACK_NOUNS, ("pack", "packs"))
        self.assertEqual(shopping_lists.UNIT_WORDS, {L: "litres", KG: "kg", UNIT: "unités"})
        self.assertEqual(shopping_lists.NO_FORMAT_WORDS, "litres (format inconnu)")
        self.assertEqual(shopping_lists.USUAL_WORD, "habituelle")
        # « On parle en bouteilles »: an article in litres, never bought at a
        # store, is entered there in bottles; one by the kilo in kilos.
        self.assertEqual(shopping_lists.ITEMS_BY_DEFAULT, frozenset({L}))

    def test_the_size_bounds_are_the_column_s(self):
        field = ShoppingListItem._meta.get_field("item_size")
        self.assertEqual((entries.SIZE_DIGITS, entries.SIZE_PLACES), (field.max_digits, field.decimal_places))
        self.assertEqual((field.max_digits, field.decimal_places), (10, 4))


class WordsTests(TestCase):
    def test_quantity_words(self):
        for quantity, unit, words in (
            (D("24"), "", "24"),
            (D("24.000"), "", "24"),
            ("2", UnitChoices.LITRE, "2 L"),
            (D("1.5"), UnitChoices.KILOGRAM, "1.5 kg"),
            (D("12"), UnitChoices.UNIT, "12 u."),
            (D("0.125"), UnitChoices.LITRE, "0.125 L"),
            (D("9999999.999"), "", "9999999.999"),
            (D("10"), UnitChoices.UNIT, "10 u."),
        ):
            with self.subTest(quantity=quantity, unit=unit):
                self.assertEqual(shopping_lists.quantity_words(quantity, unit), words)

    def test_pack_words(self):
        for quantity, pack_size, words in (
            (D("24"), 24, "1 colis de 24"),
            (D("72.000"), 24, "3 colis de 24"),
            (D("48"), 24, "2 colis de 24"),
            # Not whole packs: the number counts units, and says so - a bare
            # « colis de 24 » beside « 2 » read as two packs.
            (D("30"), 24, "à l'unité · colis de 24"),
            (D("2"), 24, "à l'unité · colis de 24"),
            (D("1.5"), 6, "à l'unité · colis de 6"),
            (D("12"), 6, "2 colis de 6"),
            (D("24"), None, ""),
            (D("24"), 1, ""),
            (D("24"), 0, ""),
        ):
            with self.subTest(quantity=quantity, pack_size=pack_size):
                self.assertEqual(shopping_lists.pack_words(quantity, pack_size), words)

    def test_never_a_bare_pack_size(self):
        for quantity in ("1", "2", "23", "25", "0.5", "47.999"):
            with self.subTest(quantity=quantity):
                words = shopping_lists.pack_words(D(quantity), 24)
                self.assertTrue(words.startswith(("à l'unité", "1 ", "2 ")), words)
                self.assertNotEqual(words, "colis de 24")

    def test_items_of_a_size(self):
        for quantity, size, size_unit, words in (
            ("1", "0.7", L, "1 bouteille de 70 cl"),
            # Singular below 2, the French rule.
            ("1.5", "0.7", L, "1.5 bouteille de 70 cl"),
            ("0.5", "0.7000", L, "0.5 bouteille de 70 cl"),
            ("2", "0.7", L, "2 bouteilles de 70 cl"),
            ("3", "1.5", L, "3 bouteilles de 1.5 L"),
            ("6", "0.375", L, "6 bouteilles de 37.5 cl"),
            # Over 3 L, a keg or a bag-in-box: « 1 bouteille de 30 L » would be wrong.
            ("1", "30", L, "1 fût de 30 L"),
            ("2", "3.5", L, "2 fûts de 3.5 L"),
            ("2", "3", L, "2 bouteilles de 3 L"),
            ("2", "0.5", KG, "2 paquets de 500 g"),
            ("1", "1", KG, "1 paquet de 1 kg"),
            ("2", "50", UNIT, "2 paquets de 50 u."),
            ("12.5", "0.7", L, "12.5 bouteilles de 70 cl"),
        ):
            with self.subTest(quantity=quantity, size=size, size_unit=size_unit):
                self.assertEqual(shopping_lists.quantity_words(D(quantity), "", D(size), size_unit), words)

    def test_the_noun_follows_what_the_item_is(self):
        """A product whose name prints a count times a size, one item holding
        more than that size, is a pack - whatever its size: a carton of six
        75 cl bottles is no keg, a six-pack of 33 cl cans no bottle. A
        bottle sold by six (« 70CL X6 », one item a bottle) stays a bottle,
        a keg a keg; with no product name, the size decides as before."""
        for quantity, size, product_name, words in (
            ("1", "6", "BIERE EXEMPLE PACK 24X25CL", "1 pack de 6 L"),
            ("2", "1.98", "SODA EXEMPLE PACK 6X33CL", "2 packs de 1.98 L"),
            ("1", "4.5", "ROSE EXEMPLE CARTON 6X75CL", "1 pack de 4.5 L"),
            # The count after the size, fused or spaced.
            ("1", "6", "BIERE EXEMPLE 25CLX24", "1 pack de 6 L"),
            ("3", "7.92", "BIERE EXEMPLE 33 CL X 24", "3 packs de 7.92 L"),
            ("1", "1.5", "EAU EXEMPLE 6X25CL", "1 pack de 1.5 L"),
            # One item is one of the size printed: a bottle sold by six.
            ("3", "0.7", "GIN EXEMPLE 70CL X6", "3 bouteilles de 70 cl"),
            ("6", "0.75", "ROSE EXEMPLE 6X75CL", "6 bouteilles de 75 cl"),
            ("6", "0.7500", "ROSE EXEMPLE 6X75CL", "6 bouteilles de 75 cl"),
            # No count printed: the size decides.
            ("1", "30", "FUT BIERE EXEMPLE 30L", "1 fût de 30 L"),
            ("2", "0.7", "VODKA EXEMPLE 70CL", "2 bouteilles de 70 cl"),
            ("1", "4.5", "", "1 fût de 4.5 L"),
            ("1", "0.7", "", "1 bouteille de 70 cl"),
            # A dimension is no count: « 20X20 » with no volume printed.
            ("1", "4.5", "CUBI EXEMPLE 20X20", "1 fût de 4.5 L"),
        ):
            with self.subTest(product_name=product_name, size=size):
                self.assertEqual(
                    shopping_lists.quantity_words(D(quantity), "", D(size), L, product_name=product_name), words
                )
        # Kilos and pieces keep their packets.
        self.assertEqual(
            shopping_lists.quantity_words(D("2"), "", D("0.3"), KG, product_name="CHIPS EXEMPLE 10X30G"),
            "2 paquets de 300 g",
        )
        self.assertEqual(
            shopping_lists.quantity_words(D("2"), "", D("50"), UNIT, product_name="GOBELETS EXEMPLE X50"),
            "2 paquets de 50 u.",
        )
        # The selects say the same nouns, in the plural.
        for size, product_name, words in (
            ("4.5", "ROSE EXEMPLE CARTON 6X75CL", "packs de 4.5 L"),
            ("0.7", "GIN EXEMPLE 70CL X6", "bouteilles de 70 cl"),
            ("30", "FUT BIERE EXEMPLE 30L", "fûts de 30 L"),
            ("4.5", "", "fûts de 4.5 L"),
        ):
            with self.subTest(label=product_name, size=size):
                self.assertEqual(
                    shopping_lists.unit_label(ITEMS, article_unit=L, size=D(size), product_name=product_name), words
                )

    def test_a_bare_number_where_no_size_is_said(self):
        for quantity, unit, size, size_unit, words in (
            # A UNIT size of 1 is the piece the article counts: the beer's « 24 ».
            ("24", "", "1", UNIT, "24"),
            ("24", "", "1.0000", UNIT, "24"),
            # No size: as today.
            ("24", "", None, "", "24"),
            ("2", "", None, "", "2"),
            # With a unit, the unit (a size never stands beside one).
            ("2", L, None, "", "2 L"),
            ("1.5", KG, None, "", "1.5 kg"),
            ("12", UNIT, None, "", "12 u."),
        ):
            with self.subTest(quantity=quantity, unit=unit, size=size):
                self.assertEqual(
                    shopping_lists.quantity_words(D(quantity), unit, None if size is None else D(size), size_unit),
                    words,
                )

    def test_size_words(self):
        for size, unit, words in (
            ("0.025", L, "2.5 cl"),
            ("0.7000", L, "70 cl"),
            ("0.375", L, "37.5 cl"),
            ("0.3333", L, "33.33 cl"),
            ("0.9999", L, "99.99 cl"),
            ("1", L, "1 L"),
            ("1.0000", L, "1 L"),
            ("1.5", L, "1.5 L"),
            ("30", L, "30 L"),
            ("0.5", KG, "500 g"),
            ("0.0001", KG, "0.1 g"),
            ("1", KG, "1 kg"),
            ("2.5", KG, "2.5 kg"),
            ("50", UNIT, "50 u."),
            ("50.0000", UNIT, "50 u."),
        ):
            with self.subTest(size=size, unit=unit):
                self.assertEqual(shopping_lists.size_words(D(size), unit), words)

    def test_unit_label(self):
        label = shopping_lists.unit_label
        for value, article_unit, size, size_unit, words in (
            # Items of a size, plural.
            (ITEMS, L, "0.7", L, "bouteilles de 70 cl"),
            (ITEMS, L, "30", L, "fûts de 30 L"),
            (ITEMS, KG, "1", KG, "paquets de 1 kg"),
            (ITEMS, KG, "0.5", KG, "paquets de 500 g"),
            (ITEMS, UNIT, "50", UNIT, "paquets de 50 u."),
            # The size's unit defaults to the article's.
            (ITEMS, L, "0.7", "", "bouteilles de 70 cl"),
            # Items with no size, or a UNIT size of 1.
            (ITEMS, L, None, "", "unités"),
            (ITEMS, UNIT, None, "", "unités"),
            (ITEMS, UNIT, "1", UNIT, "unités"),
            # The measures.
            (L, L, "0.7", L, "litres"),
            (KG, KG, None, "", "kg"),
            (KG, KG, "0.5", KG, "kg"),
            # An L article whose item size nobody knows: the litres are all there is.
            (L, L, None, "", "litres (format inconnu)"),
            # An item's own measure where the article's unit was edited since.
            (L, KG, None, "", "litres"),
        ):
            with self.subTest(value=value, article_unit=article_unit, size=size):
                self.assertEqual(
                    label(
                        value,
                        article_unit=article_unit,
                        size=None if size is None else D(size),
                        size_unit=size_unit,
                    ),
                    words,
                )


class FitsTests(TestCase):
    """What an item's quantity column (10 digits, 3 places) holds: above 0,
    exact to 3 places, under 10 000 000. SQLite stores a wider figure without
    a word, and every later read of the row raises."""

    def test_the_limit_is_the_column_s(self):
        field = ShoppingListItem._meta.get_field("quantity")
        self.assertEqual(shopping_lists.QUANTITY_LIMIT, D(10) ** (field.max_digits - field.decimal_places))
        self.assertEqual(shopping_lists.QUANTITY_LIMIT, D("10000000"))

    def test_what_fits(self):
        for quantity in (D("0.001"), D("1"), D("24.000"), D("9999999.999"), D("1.5"), 3):
            with self.subTest(quantity=quantity):
                self.assertTrue(shopping_lists.fits(quantity))

    def test_what_does_not(self):
        for quantity in (
            D("10000000"),
            D("10000000.000"),
            D("123456789.000"),
            D("1E+30"),
            D("0"),
            D("0.000"),
            D("-1"),
            D("0.0001"),
            D("1.2345"),
            D("NaN"),
            D("Infinity"),
            None,
            True,
            "24",
            24.0,
        ):
            with self.subTest(quantity=quantity):
                self.assertFalse(shopping_lists.fits(quantity))

    def test_checked_after_rounding(self):
        # 9 999 999,9995 rounds up to the first figure the column cannot hold.
        self.assertFalse(shopping_lists.fits(shopping_lists._quantized(D("9999999.9995"))))
        self.assertTrue(shopping_lists.fits(shopping_lists._quantized(D("9999999.9994"))))

    def test_what_a_size_s_column_holds(self):
        """`item_size` (10 digits, 4 places): above 0, exact to 4 places,
        under 1 000 000 - as `entries.quantized_size` gives it."""
        for size in (D("0.0001"), D("0.7"), D("0.7000"), D("30"), D("999999.9999"), 1, 50):
            with self.subTest(size=size):
                self.assertTrue(shopping_lists.size_fits(size))
        for size in (
            D("0"),
            D("-0.7"),
            D("1000000"),
            D("0.00001"),
            D("0.12345"),
            D("NaN"),
            D("Infinity"),
            None,
            True,
            "0.7",
            0.7,
        ):
            with self.subTest(size=size):
                self.assertFalse(shopping_lists.size_fits(size))
        # What quantized_size gives always fits.
        for size in ("0.00005", "0.33333", "999999.99994", "30"):
            with self.subTest(quantized=size):
                self.assertTrue(shopping_lists.size_fits(entries.quantized_size(D(size))))


class LineFiguresTests(TestCase):
    def test_the_product_s_units_with_its_pack(self):
        self.assertEqual(shopping_lists.line_figures(a_line()), Figures(D("24"), "", 7, "BIERE EXEMPLE 33CL X24", 24))

    def test_a_typed_horizon_multiplies_the_product_s_units(self):
        figures = shopping_lists.line_figures(a_line(multiplier=3, packs=(1, D("24"))))
        self.assertEqual(figures, Figures(D("72"), "", 7, "BIERE EXEMPLE 33CL X24", 24))
        self.assertEqual(shopping_lists.pack_words(figures.quantity, figures.pack_size), "3 colis de 24")

    def test_bought_by_measure_the_article_s_units(self):
        line = a_line(
            unit=UnitChoices.KILOGRAM, qty=D("1.5"), product_units=None, product_name="OLIVES EXEMPLE", packs=None
        )
        self.assertEqual(shopping_lists.line_figures(line), Figures(D("1.5"), UnitChoices.KILOGRAM, None, "", None))
        self.assertEqual(
            shopping_lists.line_figures(dataclasses.replace(line, multiplier=2)),
            Figures(D("3"), UnitChoices.KILOGRAM, None, "", None),
        )

    def test_no_pack_unless_a_whole_one_of_several_the_form_could_post(self):
        for packs in (None, (6, D("1")), (2, D("1.5")), (1, D("10000"))):
            with self.subTest(packs=packs):
                self.assertIsNone(shopping_lists.line_figures(a_line(packs=packs)).pack_size)
        self.assertEqual(shopping_lists.line_figures(a_line(packs=(2, D("12")))).pack_size, 12)

    def test_a_figure_past_the_column_is_given_as_it_is_for_the_writer_to_refuse(self):
        # A misread purchase can carry 10^8 units: the figure says so, and
        # add_item - the one writer - refuses it (AddItemTests).
        figures = shopping_lists.line_figures(a_line(product_units=D("123456789")))
        self.assertEqual(figures.quantity, D("123456789.000"))
        self.assertFalse(shopping_lists.fits(figures.quantity))

    def test_quantities_keep_three_places_rounded_half_up(self):
        # A median of two purchases can carry a fourth place.
        for qty, kept in ((D("0.0015"), D("0.002")), (D("2.0005"), D("2.001")), (D("2.0004"), D("2.000"))):
            with self.subTest(qty=qty):
                figures = shopping_lists.line_figures(
                    a_line(unit=UnitChoices.LITRE, qty=qty, product_units=None, packs=None)
                )
                self.assertEqual(figures.quantity, kept)
                self.assertEqual(figures.quantity.as_tuple().exponent, -3)

    def test_with_the_products_sizes_a_product_line_carries_its_own(self):
        gin = a_line(
            unit=L, qty=D("4.2"), product_id=21, product_name=GIN_PRODUCT, product_units=D("6"), packs=(1, D("6"))
        )
        self.assertEqual(shopping_lists.line_figures(gin, {21: D("0.7000")}), gin_bottles())
        # In the article's unit, and the multiplier counts bottles.
        self.assertEqual(
            shopping_lists.line_figures(dataclasses.replace(gin, multiplier=2), {21: BOTTLE}), gin_bottles("12")
        )
        # The words it is said in.
        figures = shopping_lists.line_figures(gin, {21: BOTTLE})
        self.assertEqual(
            shopping_lists.quantity_words(figures.quantity, figures.unit, figures.item_size, figures.size_unit),
            "6 bouteilles de 70 cl",
        )

    def test_with_no_size_known_today_s_figures(self):
        gin = a_line(
            unit=L, qty=D("4.2"), product_id=21, product_name=GIN_PRODUCT, product_units=D("6"), packs=(1, D("6"))
        )
        today = Figures(D("6"), "", 21, GIN_PRODUCT, 6)
        for sizes in (None, {}, {99: BOTTLE}, {21: D("0")}, {21: D("1000000")}):
            with self.subTest(sizes=sizes):
                figures = shopping_lists.line_figures(gin, sizes)
                self.assertEqual(figures, today)
                self.assertEqual((figures.item_size, figures.size_unit), (None, ""))

    def test_a_measured_line_never_gets_a_size(self):
        line = a_line(unit=L, qty=D("2.5"), product_id=21, product_units=None, packs=None)
        self.assertEqual(shopping_lists.line_figures(line, {21: BOTTLE}), Figures(D("2.5"), L, None, "", None))


class UsualFiguresTests(TestCase):
    """The store's usual purchase of one article (shopping_data's
    `usual_purchase_at`) as an item's figures: its product's units when
    known, else the article's units."""

    def setUp(self):
        self.store = make_supplier(name="Grossiste exemple")
        self.beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        self.today = date(2026, 10, 4)

    def figures(self, usual):
        with mock.patch("inventory.shopping_data.usual_purchase_at", return_value=usual) as read:
            figures = shopping_lists.usual_figures(self.today, self.store, self.beer)
        read.assert_called_once_with(self.today, self.store.pk, self.beer)
        return figures

    def test_read_from_the_purchases(self):
        product = make_product(supplier=self.store, raw_name="BIERE EXEMPLE 33CL X24", stock_type=self.beer)
        for days_ago in (21, 14, 7):
            line = make_invoice_line(
                invoice=make_invoice(supplier=self.store, invoice_date=self.today - timedelta(days=days_ago)),
                product=product,
                quantity=24,
                colisage=24,
                total_ht="45.00",
            )
            make_movement(stock_type=self.beer, quantity="24", unit_cost_ht="1.875", invoice_line=line)
        self.assertEqual(
            shopping_lists.usual_figures(self.today, self.store, self.beer),
            Figures(D("24"), "", product.pk, "BIERE EXEMPLE 33CL X24", 24),
        )
        # Never bought at another store.
        self.assertIsNone(shopping_lists.usual_figures(self.today, make_supplier(name="Épicerie exemple"), self.beer))

    def test_the_usual_product(self):
        usual = SimpleNamespace(
            qty=D("24"), product_id=7, product_name="BIERE EXEMPLE 33CL X24", product_units=D("24"), packs=(1, D("24"))
        )
        self.assertEqual(self.figures(usual), Figures(D("24"), "", 7, "BIERE EXEMPLE 33CL X24", 24))

    def test_bought_by_measure(self):
        usual = SimpleNamespace(qty=D("1.25"), product_id=7, product_name="FUT EXEMPLE", product_units=None, packs=None)
        self.assertEqual(self.figures(usual), Figures(D("1.25"), UnitChoices.UNIT, None, "", None))

    def test_never_bought_there(self):
        self.assertIsNone(self.figures(None))

    def test_figures_of_usual_converts_as_usual_figures_does(self):
        product = SimpleNamespace(
            qty=D("24"), product_id=7, product_name="BIERE EXEMPLE 33CL X24", product_units=D("24"), packs=(1, D("24"))
        )
        measure = SimpleNamespace(
            qty=D("1.2345"), product_id=7, product_name="FUT EXEMPLE", product_units=None, packs=None
        )
        for usual in (product, measure):
            with self.subTest(usual=usual.product_name):
                self.assertEqual(shopping_lists.figures_of_usual(usual, self.beer), self.figures(usual))
        self.assertEqual(shopping_lists.figures_of_usual(measure, self.beer), Figures(D("1.235"), UNIT, None, "", None))


class MovedNamesTests(SimpleTestCase):
    """What a typed name names, and the add menu, live in entries.py and
    `list_entries`: the old one-argument `find_article` and `article_choices`
    are gone from here, so a second copy of the entry rule never grows back
    beside the resolver (test_entries.FindArticleTests and ListEntriesTests
    pin the rule and the menu)."""

    def test_the_moved_names_are_gone(self):
        for name in ("find_article", "article_choices"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(shopping_lists, name))


class CleanTextTests(TestCase):
    def test_control_characters_and_spaces(self):
        for typed, clean in (
            ("Pain exemple", "Pain exemple"),
            ("  Pain \t exemple\n", "Pain exemple"),
            ("Pain\x00exemple", "Pain exemple"),
            ("Pain\x1bexemple\x7f", "Pain exemple"),
            ("\x00\x01 ", ""),
            ("<i>Pain</i> exemple", "<i>Pain</i> exemple"),
            ("", ""),
            (None, ""),
            (7, ""),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(shopping_lists.clean_text(typed), clean)


class ReadQuantityTests(TestCase):
    def test_what_reads(self):
        for typed, quantity in (
            ("1,5", D("1.5")),
            ("1.5", D("1.5")),
            # A shopping quantity, not an amount: read_number's rule.
            ("1,500", D("1.5")),
            (" 2 ", D("2")),
            ("0,001", D("0.001")),
            ("9999999,999", D("9999999.999")),
            ("24", D("24")),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(shopping_lists.read_quantity(typed), quantity)

    def test_what_does_not(self):
        for typed in (
            "0",
            "0,000",
            "-1",
            "1-",
            "1,2345",
            "1e3",
            "1" * 30,
            "10000000",
            "NaN",
            "Infinity",
            "²",
            "abc",
            "",
            None,
        ):
            with self.subTest(typed=typed):
                self.assertIsNone(shopping_lists.read_quantity(typed))


class StoresTests(TestCase):
    """The stores a list is offered for - bought at, neither a supplier of
    charges nor the removed AI reading's - and `store_of`, which also reaches
    a store whose open list outlived its purchases.

    The AI reading is gone (invoices/0038): its supplier, kept as an ordinary
    one where something named it, has its reader key emptied and is told by
    its code (receipts.RETIRED_CODES), as « Prévoir les courses » tells it."""

    def setUp(self):
        self.beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        self.wholesaler = make_supplier(name="Grossiste exemple")
        bought(self.wholesaler, self.beer, "24")
        self.charges = make_supplier(name="Charges exemple", expenses_only=True)
        bought(self.charges, self.beer)
        # As invoices/0038 leaves it where something names it.
        self.ai = make_supplier(code="OTHER", name="Autre (analyse IA)")
        bought(self.ai, self.beer)
        self.returns_only = make_supplier(name="Reprise exemple")
        bought(self.returns_only, self.beer, "-1", "-40.00")
        self.never = make_supplier(name="Jamais exemple")

    def test_the_stores_offered_in_one_query(self):
        with self.assertNumQueries(1):
            offered = list(shopping_lists.offered_stores())
        self.assertEqual(offered, [self.wholesaler])

    def test_a_loss_is_no_purchase(self):
        StockMovement.objects.create(
            stock_type=self.beer,
            kind=MovementKind.LOSS,
            quantity=D("1"),
            invoice_line=make_invoice_line(invoice=make_invoice(supplier=self.never)),
        )
        self.assertEqual(list(shopping_lists.offered_stores()), [self.wholesaler])

    def test_store_of(self):
        with self.assertNumQueries(1):
            self.assertEqual(shopping_lists.store_of(str(self.wholesaler.pk)), self.wholesaler)

    def test_the_removed_ai_reading_s_supplier_is_no_store(self):
        """Bought at (a document filed under it is why 0038 kept it), and
        still no store: not offered, not reached by its id, so no list is
        ever started for it."""
        self.assertEqual(self.ai.parser_key, "")
        self.assertNotIn(self.ai, shopping_lists.offered_stores())
        with self.assertNumQueries(1):
            self.assertIsNone(shopping_lists.store_of(str(self.ai.pk)))
        self.assertFalse(ShoppingList.objects.filter(supplier=self.ai).exists())
        for store in (self.charges, self.ai, self.returns_only, self.never):
            with self.subTest(store=store.name):
                self.assertIsNone(shopping_lists.store_of(str(store.pk)))

    def test_an_open_list_keeps_its_store_reachable(self):
        make_shopping_item(make_shopping_list(self.never), label="Pain exemple")
        make_shopping_item(make_shopping_list(self.returns_only, finished=True), label="Pain exemple")
        with self.assertNumQueries(1):
            self.assertEqual(shopping_lists.store_of(str(self.never.pk)), self.never)
        # A finished list alone does not.
        self.assertIsNone(shopping_lists.store_of(str(self.returns_only.pk)))

    def test_an_emptied_open_list_is_no_list_in_progress(self):
        # Its last item removed, the list counts as none: it no longer keeps
        # a store reachable that nothing else offers.
        make_shopping_list(self.never)
        self.assertIsNone(shopping_lists.store_of(str(self.never.pk)))
        # A store offered stays so, an empty list or not.
        make_shopping_list(self.wholesaler)
        self.assertEqual(shopping_lists.store_of(str(self.wholesaler.pk)), self.wholesaler)

    def test_anything_but_an_id_is_none_with_no_query(self):
        for asked in ("", "abc", "²", "-1", "1.5", "1" * 30, None, 7, f" {self.wholesaler.pk}"):
            with self.subTest(asked=asked):
                with self.assertNumQueries(0):
                    self.assertIsNone(shopping_lists.store_of(asked))
        self.assertIsNone(shopping_lists.store_of("999999"))


class OpenListTests(TestCase):
    def setUp(self):
        self.store = make_supplier(name="Grossiste exemple")

    def test_open_list_of_reads_only(self):
        make_shopping_list(self.store, finished=True)
        self.assertIsNone(shopping_lists.open_list_of(self.store.pk))
        self.assertEqual(ShoppingList.objects.count(), 1)
        opened = make_shopping_list(self.store)
        self.assertEqual(shopping_lists.open_list_of(self.store.pk), opened)

    def test_open_list_for_makes_it_once(self):
        made = shopping_lists.open_list_for(self.store, EMPLOYEE)
        self.assertEqual((made.supplier, made.created_by, made.finished_at), (self.store, EMPLOYEE, None))
        self.assertEqual(shopping_lists.open_list_for(self.store, OTHER), made)
        self.assertEqual(ShoppingList.objects.get().created_by, EMPLOYEE)

    def test_a_list_made_meanwhile_is_returned_never_a_second_one(self):
        # Another request makes the list between this one's read and its create.
        already = make_shopping_list(self.store, created_by=OTHER)
        real = shopping_lists.open_list_of
        reads = []

        def stale_then_real(store_id):
            reads.append(store_id)
            return None if len(reads) == 1 else real(store_id)

        with mock.patch.object(shopping_lists, "open_list_of", side_effect=stale_then_real):
            made = shopping_lists.open_list_for(self.store, EMPLOYEE)
        self.assertEqual(made, already)
        self.assertEqual(len(reads), 2)
        self.assertEqual(ShoppingList.objects.count(), 1)

    def test_it_works_inside_a_transaction(self):
        already = make_shopping_list(self.store)
        with transaction.atomic(), mock.patch.object(shopping_lists, "open_list_of", side_effect=[None, already]):
            self.assertEqual(shopping_lists.open_list_for(self.store, EMPLOYEE), already)
            # The transaction is still usable after the refused create.
            self.assertEqual(ShoppingList.objects.count(), 1)


class AddItemTests(TestCase):
    def setUp(self):
        self.store = make_supplier(name="Grossiste exemple")
        self.beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        self.syrup = make_stock_type(name="Sirop exemple", unit=UnitChoices.LITRE)
        self.beer_figures = BEER_FIGURES
        self.then = timezone.now() - timedelta(days=3)

    def add(self, **fields):
        fields.setdefault("by", EMPLOYEE)
        return shopping_lists.add_item(self.store, **fields)

    def stored(self, item) -> tuple:
        """What the row holds, read again from the database."""
        item = ShoppingListItem.objects.get(pk=item.pk)
        return (
            item.quantity,
            item.unit,
            item.product_name,
            item.pack_size,
            item.note,
            item.checked_at,
            item.checked_by,
        )

    def test_an_article_with_its_figures(self):
        item, said = self.add(
            label="Bière exemple", figures=self.beer_figures, stock_type=self.beer, note="Note exemple"
        )
        self.assertIs(said, outcome("ADDED"))
        item.refresh_from_db()
        self.assertEqual(
            (item.stock_type, item.label, item.quantity, item.unit, item.product_name, item.pack_size),
            (self.beer, "Bière exemple", D("24"), "", "BIERE EXEMPLE 33CL X24", 24),
        )
        self.assertEqual((item.note, item.added_by, item.checked_at), ("Note exemple", EMPLOYEE, None))
        # The first item made the list.
        self.assertEqual((item.shopping_list.supplier, item.shopping_list.created_by), (self.store, EMPLOYEE))

    def test_an_article_twice_is_one_item_unchanged(self):
        first, _ = self.add(label="Bière exemple", figures=self.beer_figures, stock_type=self.beer)
        again, said = self.add(
            by=OTHER, label="Bière exemple", figures=Figures(D("48"), "", None, "", None), stock_type=self.beer
        )
        self.assertIs(said, outcome("ALREADY"))
        self.assertEqual(again, first)
        again.refresh_from_db()
        self.assertEqual(
            (again.quantity, again.product_name, again.added_by), (D("24"), "BIERE EXEMPLE 33CL X24", EMPLOYEE)
        )
        self.assertEqual(ShoppingListItem.objects.count(), 1)

    def test_free_texts_reading_alike_are_one(self):
        figures = Figures(D("2"), "", None, "", None)
        first, said = self.add(label="Pain exemple", figures=figures)
        self.assertIs(said, outcome("ADDED"))
        for label in ("pain exemple", "PAIN  EXEMPLE", "Païn exemple"):
            with self.subTest(label=label):
                again, said = self.add(label=label, figures=Figures(D("5"), "", None, "", None))
                self.assertIs(said, outcome("ALREADY"))
                self.assertEqual(again, first)
        self.assertEqual(list(ShoppingListItem.objects.values_list("label", "quantity")), [("Pain exemple", D("2"))])
        # Another text is another item; and a free text never stands for an article.
        self.assertIs(self.add(label="Sel exemple", figures=figures)[1], outcome("ADDED"))
        self.assertIs(self.add(label="Bière exemple", figures=figures)[1], outcome("ADDED"))
        self.assertIs(
            self.add(label="Bière exemple", figures=self.beer_figures, stock_type=self.beer)[1], outcome("ADDED")
        )
        self.assertEqual(ShoppingListItem.objects.count(), 4)

    def test_on_the_open_list_never_a_finished_one(self):
        finished = make_shopping_list(self.store, finished=True)
        make_shopping_item(finished, self.beer)
        item, said = self.add(label="Bière exemple", figures=self.beer_figures, stock_type=self.beer)
        self.assertIs(said, outcome("ADDED"))
        self.assertNotEqual(item.shopping_list, finished)
        self.assertTrue(item.shopping_list.is_open)
        self.assertEqual(finished.items.count(), 1)

    def test_the_same_article_added_meanwhile(self):
        # Another request adds it between this one's look and its create.
        shopping_list = make_shopping_list(self.store)
        theirs = make_shopping_item(shopping_list, self.beer, quantity="6", unit=UnitChoices.UNIT)
        real = shopping_lists._listed
        looks = []

        def missed_then_real(*args):
            looks.append(args)
            return None if len(looks) == 1 else real(*args)

        with mock.patch.object(shopping_lists, "_listed", side_effect=missed_then_real):
            item, said = self.add(label="Bière exemple", figures=self.beer_figures, stock_type=self.beer)
        self.assertEqual((item, said), (theirs, outcome("ALREADY")))
        self.assertEqual(ShoppingListItem.objects.count(), 1)

    # -- a list finished between the look and the write (another phone's
    # -- « Courses terminées »)

    def finished_at_the_first_look(self, shopping_list, *, keep: bool):
        """`_listed` as it is, but its first call first finishes
        `shopping_list` - « Courses terminées » pressed on another phone
        between this add's look at the open list and its write."""
        real = shopping_lists._listed
        looks = []

        def finished_meanwhile(*args):
            looks.append(args)
            if len(looks) == 1:
                shopping_lists.finish(shopping_list, keep=keep, by=OTHER, now=timezone.now())
            return real(*args)

        return mock.patch.object(shopping_lists, "_listed", side_effect=finished_meanwhile)

    def test_a_list_finished_between_the_look_and_the_write(self):
        for keep in (True, False):
            with self.subTest(keep=keep):
                ShoppingList.objects.all().delete()
                first = make_shopping_list(self.store, created_by=OTHER)
                carried = make_shopping_item(first, self.beer, quantity="24")
                with self.finished_at_the_first_look(first, keep=keep):
                    item, said = self.add(
                        label="Sirop exemple",
                        figures=Figures(D("2"), UnitChoices.LITRE, None, "", None),
                        stock_type=self.syrup,
                    )
                following = shopping_lists.open_list_of(self.store.pk)
                # On the store's open list - the carried one, or a new one -,
                # never on the list finished under it.
                self.assertIsNotNone(following)
                self.assertNotEqual(following.pk, first.pk)
                self.assertEqual(ShoppingListItem.objects.get(pk=item.pk).shopping_list_id, following.pk)
                first.refresh_from_db()
                self.assertFalse(first.is_open)
                self.assertEqual(list(first.items.all()), [carried])
                self.assertEqual(
                    set(following.items.values_list("label", flat=True)),
                    {"Bière exemple", "Sirop exemple"} if keep else {"Sirop exemple"},
                )
                self.assertIs(said, outcome("ADDED"))

    def test_a_ticked_item_on_a_list_finished_meanwhile_is_added_to_the_next(self):
        first = make_shopping_list(self.store, created_by=OTHER)
        ticked = make_shopping_item(first, self.beer, quantity="24", checked_at=self.then, checked_by=OTHER)
        make_shopping_item(first, label="Pain exemple")
        with self.finished_at_the_first_look(first, keep=True):
            item, said = self.add(label="Bière exemple", figures=self.beer_figures, stock_type=self.beer)
        self.assertIs(said, outcome("ADDED"))
        following = shopping_lists.open_list_of(self.store.pk)
        self.assertEqual(ShoppingListItem.objects.get(pk=item.pk).shopping_list_id, following.pk)
        # The finished list keeps its bought item as it was.
        self.assertEqual(self.stored(ticked)[5:], (self.then, OTHER))

    # -- a ticked (bought) item is put back to buy

    def test_a_ticked_article_is_put_back_to_buy_with_the_new_figures(self):
        shopping_list = make_shopping_list(self.store)
        ticked = make_shopping_item(
            shopping_list,
            self.beer,
            quantity="24",
            product_name="BIERE EXEMPLE 33CL X24",
            pack_size=24,
            note="Note exemple",
            added_at=self.then,
            added_by=OTHER,
            checked_at=self.then,
            checked_by=OTHER,
        )
        item, said = self.add(
            label="Bière exemple", figures=Figures(D("48"), "", 9, "BIERE EXEMPLE 25CL X12", 12), stock_type=self.beer
        )
        self.assertIs(said, outcome("RELISTED"))
        self.assertEqual(item.pk, ticked.pk)
        # Unticked, its figures the new ones; no note typed, its note kept.
        self.assertEqual(self.stored(item), (D("48"), "", "BIERE EXEMPLE 25CL X12", 12, "Note exemple", None, ""))
        # The item handed back says what is stored.
        self.assertEqual((item.quantity, item.product_name, item.checked_at), (D("48"), "BIERE EXEMPLE 25CL X12", None))
        # Who added it and when stay.
        item.refresh_from_db()
        self.assertEqual((item.added_by, item.added_at, item.shopping_list_id), (OTHER, self.then, shopping_list.pk))
        # Asked again, it is on the list to buy: « déjà », unchanged.
        again, said = self.add(
            label="Bière exemple", figures=Figures(D("6"), UnitChoices.UNIT, None, "", None), stock_type=self.beer
        )
        self.assertIs(said, outcome("ALREADY"))
        self.assertEqual(again.pk, ticked.pk)
        self.assertEqual(self.stored(again)[:4], (D("48"), "", "BIERE EXEMPLE 25CL X12", 12))
        self.assertEqual(ShoppingListItem.objects.count(), 1)

    def test_put_back_counting_the_article_s_unit_with_a_note_typed(self):
        ticked = make_shopping_item(
            make_shopping_list(self.store),
            self.beer,
            quantity="24",
            product_name="BIERE EXEMPLE 33CL X24",
            pack_size=24,
            note="Note exemple",
            checked_at=self.then,
            checked_by=OTHER,
        )
        _item, said = self.add(
            label="Bière exemple",
            figures=Figures(D("6"), UnitChoices.UNIT, None, "", None),
            stock_type=self.beer,
            note="Fraîche exemple",
        )
        self.assertIs(said, outcome("RELISTED"))
        self.assertEqual(self.stored(ticked), (D("6"), UnitChoices.UNIT, "", None, "Fraîche exemple", None, ""))

    def test_a_ticked_free_text_is_put_back(self):
        ticked = make_shopping_item(
            make_shopping_list(self.store), label="Pain exemple", quantity="2", checked_at=self.then, checked_by=OTHER
        )
        item, said = self.add(label="pain  EXEMPLE", figures=Figures(D("5"), "", None, "", None))
        self.assertIs(said, outcome("RELISTED"))
        self.assertEqual(item.pk, ticked.pk)
        self.assertEqual(self.stored(item), (D("5"), "", "", None, "", None, ""))
        # Its label as it was first typed.
        self.assertEqual(ShoppingListItem.objects.get(pk=item.pk).label, "Pain exemple")

    def test_a_free_text_still_to_buy_is_found_before_a_bought_one(self):
        # Two lines reading alike (an article deleted left one): the one
        # still to buy answers « déjà », the bought one stays bought.
        shopping_list = make_shopping_list(self.store)
        bought_one = make_shopping_item(
            shopping_list, label="Pain exemple", added_at=self.then, checked_at=self.then, checked_by=OTHER
        )
        to_buy = make_shopping_item(shopping_list, label="PAIN exemple", added_at=self.then + timedelta(minutes=1))
        item, said = self.add(label="pain exemple", figures=Figures(D("5"), "", None, "", None))
        self.assertEqual((item.pk, said), (to_buy.pk, outcome("ALREADY")))
        self.assertEqual(self.stored(bought_one)[5:], (self.then, OTHER))
        self.assertEqual(self.stored(to_buy)[0], D("1"))

    def test_relist_off_leaves_a_ticked_item_bought(self):
        # « Tout ajouter » may choose to leave what was just bought alone.
        ticked = make_shopping_item(
            make_shopping_list(self.store), self.beer, quantity="24", checked_at=self.then, checked_by=OTHER
        )
        item, said = self.add(label="Bière exemple", figures=self.beer_figures, stock_type=self.beer, relist=False)
        self.assertEqual((item.pk, said), (ticked.pk, outcome("ALREADY")))
        self.assertEqual(self.stored(ticked)[0], D("24"))
        self.assertEqual(self.stored(ticked)[5:], (self.then, OTHER))

    # -- a quantity wider than its column

    def test_a_quantity_wider_than_the_column_is_refused_and_nothing_written(self):
        for quantity in (D("10000000"), shopping_lists._quantized(D("9999999.9995")), D("123456789.000")):
            with self.subTest(quantity=quantity):
                with self.assertRaises(shopping_lists.QuantityTooWide) as refused:
                    self.add(
                        label="Bière exemple",
                        figures=Figures(quantity, "", 7, "BIERE EXEMPLE 33CL X24", 24),
                        stock_type=self.beer,
                    )
                self.assertIsInstance(refused.exception, ValueError)
                self.assertEqual(
                    str(refused.exception), "« Bière exemple » : quantité trop grande, rien n'a été ajouté."
                )
        self.assertFalse(ShoppingList.objects.exists())
        self.assertFalse(ShoppingListItem.objects.exists())
        # The widest the column holds is written, and reads back.
        item, said = self.add(
            label="Bière exemple",
            figures=Figures(D("9999999.999"), "", 7, "BIERE EXEMPLE 33CL X24", 24),
            stock_type=self.beer,
        )
        self.assertIs(said, outcome("ADDED"))
        self.assertEqual(self.stored(item)[0], D("9999999.999"))

    def test_nor_is_a_ticked_item_put_back_at_it(self):
        ticked = make_shopping_item(
            make_shopping_list(self.store), self.beer, quantity="24", checked_at=self.then, checked_by=OTHER
        )
        with self.assertRaises(shopping_lists.QuantityTooWide):
            self.add(label="Bière exemple", figures=Figures(D("10000000"), "", None, "", None), stock_type=self.beer)
        self.assertEqual(self.stored(ticked)[0], D("24"))
        self.assertEqual(self.stored(ticked)[5:], (self.then, OTHER))

    def test_a_refusal_leaves_the_caller_s_transaction_usable(self):
        # « Tout ajouter » adds inside one transaction, and goes on past a
        # line it cannot add.
        with transaction.atomic():
            with self.assertRaises(shopping_lists.QuantityTooWide):
                self.add(label="Bière exemple", figures=Figures(D("10000000"), "", None, "", None))
            _item, said = self.add(label="Pain exemple", figures=ONE)
        self.assertIs(said, outcome("ADDED"))
        self.assertEqual(list(ShoppingListItem.objects.values_list("label", flat=True)), ["Pain exemple"])

    # -- an emptied open list starts again with its next item

    def test_an_emptied_open_list_starts_again_with_its_next_item(self):
        long_ago = timezone.now() - timedelta(days=21)
        emptied = make_shopping_list(self.store, created_by=OTHER, created_at=long_ago)
        before = timezone.now()
        item, said = self.add(label="Pain exemple", figures=ONE)
        self.assertIs(said, outcome("ADDED"))
        emptied.refresh_from_db()
        # The same list, its start that of the item added.
        self.assertEqual(ShoppingListItem.objects.get(pk=item.pk).shopping_list_id, emptied.pk)
        self.assertTrue(before <= emptied.created_at <= timezone.now())
        self.assertEqual(emptied.created_by, EMPLOYEE)

    def test_a_list_holding_items_keeps_its_start(self):
        long_ago = timezone.now() - timedelta(days=21)
        started = make_shopping_list(self.store, created_by=OTHER, created_at=long_ago)
        make_shopping_item(started, label="Sel exemple")
        make_shopping_item(started, self.beer, checked_at=self.then, checked_by=OTHER)
        self.add(label="Pain exemple", figures=ONE)
        self.add(label="Bière exemple", figures=self.beer_figures, stock_type=self.beer)
        started.refresh_from_db()
        self.assertEqual((started.created_at, started.created_by), (long_ago, OTHER))

    # -- the size of one item

    def sizes(self, item) -> tuple:
        item = ShoppingListItem.objects.get(pk=item.pk)
        return item.quantity, item.unit, item.product_name, item.pack_size, item.item_size, item.size_unit

    def test_a_size_is_written(self):
        item, said = self.add(label="Sirop exemple", figures=gin_bottles("3"), stock_type=self.syrup)
        self.assertIs(said, outcome("ADDED"))
        self.assertEqual(self.sizes(item), (D("3"), "", GIN_PRODUCT, 6, D("0.7000"), L))
        stored = ShoppingListItem.objects.get(pk=item.pk)
        self.assertEqual(
            shopping_lists.quantity_words(stored.quantity, stored.unit, stored.item_size, stored.size_unit),
            "3 bouteilles de 70 cl",
        )

    def test_a_relisted_ticked_item_takes_the_new_size_and_loses_it_in_litres(self):
        ticked = make_shopping_item(
            make_shopping_list(self.store), self.syrup, quantity="2", unit=L, checked_at=self.then, checked_by=OTHER
        )
        item, said = self.add(label="Sirop exemple", figures=gin_bottles(), stock_type=self.syrup)
        self.assertEqual((item.pk, said), (ticked.pk, outcome("RELISTED")))
        self.assertEqual(self.sizes(item), (D("6"), "", GIN_PRODUCT, 6, D("0.7000"), L))
        # Bought again, then asked in litres: the size goes with the bottles.
        ShoppingListItem.objects.filter(pk=item.pk).update(checked_at=self.then, checked_by=OTHER)
        item, said = self.add(
            label="Sirop exemple", figures=Figures(D("4.2"), L, None, "", None), stock_type=self.syrup
        )
        self.assertIs(said, outcome("RELISTED"))
        self.assertEqual(self.sizes(item), (D("4.2"), L, "", None, None, ""))
        # Bottles of another size, once more.
        ShoppingListItem.objects.filter(pk=item.pk).update(checked_at=self.then, checked_by=OTHER)
        item, _said = self.add(
            label="Sirop exemple",
            figures=Figures(D("2"), "", None, "", None, D("1.5"), L),
            stock_type=self.syrup,
        )
        self.assertEqual(self.sizes(item), (D("2"), "", "", None, D("1.5000"), L))

    def test_a_size_the_column_cannot_hold_is_refused_before_anything_is_read_or_written(self):
        """Only code can produce one (entries.quantized_size bounds every
        size): refused as a ValueError, never QuantityTooWide - a page says
        that one; this one is a bug."""
        for size, size_unit, unit in (
            (D("1000000"), L, ""),
            (D("0.00001"), L, ""),
            (D("0.12345"), L, ""),
            (D("0"), L, ""),
            (D("-0.7"), L, ""),
            (D("NaN"), L, ""),
            (0.7, L, ""),
            # Both or neither, a known unit, and only beside a number of items.
            (BOTTLE, "", ""),
            (None, L, ""),
            (BOTTLE, "cl", ""),
            (BOTTLE, L, L),
        ):
            with self.subTest(size=size, size_unit=size_unit, unit=unit):
                figures = Figures(D("3"), unit, None, "", None, size, size_unit)
                with self.assertNumQueries(0), self.assertRaises(ValueError) as refused:
                    self.add(label="Sirop exemple", figures=figures, stock_type=self.syrup)
                self.assertNotIsInstance(refused.exception, shopping_lists.QuantityTooWide)
        self.assertFalse(ShoppingList.objects.exists())
        self.assertFalse(ShoppingListItem.objects.exists())
        # The widest the column holds is written, and reads back.
        item, said = self.add(
            label="Sirop exemple",
            figures=Figures(D("1"), "", None, "", None, D("999999.9999"), L),
            stock_type=self.syrup,
        )
        self.assertIs(said, outcome("ADDED"))
        self.assertEqual(self.sizes(item)[4:], (D("999999.9999"), L))


class SetTickedTests(TestCase):
    def setUp(self):
        self.list = make_shopping_list(make_supplier(name="Grossiste exemple"))
        self.item = make_shopping_item(self.list, label="Pain exemple")
        self.now = timezone.now()

    def tick(self, wanted, by=EMPLOYEE, now=None, pk=None):
        with CaptureQueriesContext(connection) as queries:
            done = shopping_lists.set_ticked(pk or self.item.pk, wanted, by, now or self.now)
        self.assertEqual([query["sql"].split()[0] for query in queries.captured_queries], ["UPDATE"])
        self.item.refresh_from_db()
        return done

    def test_ticked_then_ticked_again_keeps_the_first_who_and_when(self):
        self.assertTrue(self.tick(True))
        self.assertEqual((self.item.checked_at, self.item.checked_by), (self.now, EMPLOYEE))
        # Another phone ticks it again, later: the first tick stays.
        self.assertTrue(self.tick(True, by=OTHER, now=self.now + timedelta(minutes=3)))
        self.assertEqual((self.item.checked_at, self.item.checked_by), (self.now, EMPLOYEE))

    def test_unticked(self):
        self.tick(True)
        self.assertTrue(self.tick(False, by=OTHER))
        self.assertEqual((self.item.checked_at, self.item.checked_by), (None, ""))
        # Twice changes nothing more.
        self.assertTrue(self.tick(False))
        self.assertEqual((self.item.checked_at, self.item.checked_by), (None, ""))

    def test_a_finished_list_is_refused_and_unchanged(self):
        self.tick(True)
        ShoppingList.objects.filter(pk=self.list.pk).update(finished_at=self.now)
        for wanted in (False, True):
            with self.subTest(wanted=wanted):
                self.assertFalse(self.tick(wanted, by=OTHER, now=self.now + timedelta(hours=1)))
                self.assertEqual((self.item.checked_at, self.item.checked_by), (self.now, EMPLOYEE))

    def test_an_item_gone_is_refused(self):
        self.assertFalse(self.tick(True, pk=self.item.pk + 1000))

    def test_only_that_item(self):
        other = make_shopping_item(self.list, label="Sel exemple")
        self.tick(True)
        other.refresh_from_db()
        self.assertIsNone(other.checked_at)


class FinishTests(TestCase):
    def setUp(self):
        self.store = make_supplier(name="Grossiste exemple")
        self.beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        self.syrup = make_stock_type(name="Sirop exemple", unit=UnitChoices.LITRE)
        self.list = make_shopping_list(self.store, created_by=EMPLOYEE)
        self.start = timezone.now() - timedelta(days=2)
        self.now = timezone.now()
        self.beer_item = make_shopping_item(
            self.list,
            self.beer,
            quantity="24",
            product_name="BIERE EXEMPLE 33CL X24",
            pack_size=24,
            note="Note exemple",
            added_at=self.start,
            added_by=EMPLOYEE,
        )
        self.syrup_item = make_shopping_item(
            self.list,
            self.syrup,
            quantity="2",
            unit=UnitChoices.LITRE,
            added_at=self.start + timedelta(minutes=1),
            checked_at=self.start + timedelta(days=1),
            checked_by=EMPLOYEE,
        )
        self.bread = make_shopping_item(
            self.list, label="Pain exemple", quantity="3", added_at=self.start + timedelta(minutes=2), added_by=OTHER
        )

    def figures(self, shopping_list) -> list:
        return list(
            shopping_list.items.order_by("added_at", "pk").values_list(
                "stock_type_id",
                "label",
                "quantity",
                "unit",
                "product_name",
                "pack_size",
                "item_size",
                "size_unit",
                "note",
                "added_at",
                "added_by",
                "checked_at",
                "checked_by",
            )
        )

    def test_a_carried_item_keeps_its_size(self):
        ShoppingListItem.objects.filter(pk=self.syrup_item.pk).update(
            checked_at=None,
            checked_by="",
            quantity=D("6"),
            unit="",
            product_name=GIN_PRODUCT,
            item_size=BOTTLE,
            size_unit=L,
        )
        before = self.figures(self.list)
        done = shopping_lists.finish(self.list, keep=True, by=OTHER, now=self.now)
        self.assertEqual(done, Finished(ticked=0, total=3, carried=3))
        following = shopping_lists.open_list_of(self.store.pk)
        self.assertEqual(self.figures(following), before)
        carried = following.items.get(stock_type=self.syrup)
        self.assertEqual((carried.quantity, carried.item_size, carried.size_unit), (D("6"), D("0.7000"), L))

    def test_finished_with_its_unticked_kept_for_the_next_list(self):
        before = self.figures(self.list)
        done = shopping_lists.finish(self.list, keep=True, by=OTHER, now=self.now)
        self.assertEqual(done, Finished(ticked=1, total=3, carried=2))
        self.list.refresh_from_db()
        self.assertEqual((self.list.finished_at, self.list.finished_by), (self.now, OTHER))
        # The finished list keeps every item as it was.
        self.assertEqual(self.figures(self.list), before)
        following = shopping_lists.open_list_of(self.store.pk)
        self.assertNotEqual(following, self.list)
        self.assertEqual(following.created_by, OTHER)
        # The unticked, in their order, with their figures and who added them - never a tick.
        self.assertEqual(self.figures(following), [before[0], before[2]])

    def test_keep_off_carries_nothing(self):
        done = shopping_lists.finish(self.list, keep=False, by=OTHER, now=self.now)
        self.assertEqual(done, Finished(ticked=1, total=3, carried=0))
        self.assertIsNone(shopping_lists.open_list_of(self.store.pk))
        self.assertEqual(ShoppingList.objects.count(), 1)

    def test_everything_ticked_makes_no_new_list(self):
        self.list.items.update(checked_at=self.now, checked_by=EMPLOYEE)
        self.assertEqual(shopping_lists.finish(self.list, keep=True, by=OTHER, now=self.now), Finished(3, 3, 0))
        self.assertEqual(ShoppingList.objects.count(), 1)

    def test_an_empty_list(self):
        self.list.items.all().delete()
        self.assertEqual(shopping_lists.finish(self.list, keep=True, by=OTHER, now=self.now), Finished(0, 0, 0))

    def test_twice_is_once(self):
        self.assertIsNotNone(shopping_lists.finish(self.list, keep=True, by=OTHER, now=self.now))
        later = self.now + timedelta(minutes=1)
        # A double submit: the same list object, still believed open.
        self.assertIsNone(shopping_lists.finish(self.list, keep=True, by=EMPLOYEE, now=later))
        self.assertEqual(ShoppingList.objects.count(), 2)
        self.assertEqual(ShoppingListItem.objects.count(), 5)
        self.list.refresh_from_db()
        self.assertEqual((self.list.finished_at, self.list.finished_by), (self.now, OTHER))

    def test_an_article_already_on_the_next_list_is_not_copied(self):
        # Another phone added the beer and a « pain exemple » to a new list
        # just as this one finished.
        real = shopping_lists._open_list
        made = {}

        def made_meanwhile(store_id, by):
            following = real(store_id, OTHER)
            made["beer"] = make_shopping_item(following, self.beer, quantity="6", unit=UnitChoices.UNIT)
            made["bread"] = make_shopping_item(following, label="PAIN exemple", quantity="1")
            return following

        with mock.patch.object(shopping_lists, "_open_list", side_effect=made_meanwhile):
            done = shopping_lists.finish(self.list, keep=True, by=EMPLOYEE, now=self.now)
        self.assertEqual(done, Finished(ticked=1, total=3, carried=0))
        following = shopping_lists.open_list_of(self.store.pk)
        self.assertEqual(set(following.items.all()), {made["beer"], made["bread"]})

    def test_two_lines_reading_alike_both_go_on(self):
        # An article deleted left its item as a free text beside one of the
        # same name: two lines on the finished list, two on the next.
        make_shopping_item(self.list, label="Bière exemple", quantity="2", added_at=self.start + timedelta(minutes=3))
        self.beer.delete()
        done = shopping_lists.finish(self.list, keep=True, by=OTHER, now=self.now)
        self.assertEqual(done, Finished(ticked=1, total=4, carried=3))
        following = shopping_lists.open_list_of(self.store.pk)
        self.assertEqual(
            list(following.items.order_by("added_at", "pk").values_list("stock_type_id", "label", "quantity")),
            [(None, "Bière exemple", D("24")), (None, "Pain exemple", D("3")), (None, "Bière exemple", D("2"))],
        )

    def test_one_transaction(self):
        with mock.patch.object(ShoppingListItem.objects, "bulk_create", side_effect=RuntimeError("disque plein")):
            with self.assertRaises(RuntimeError):
                shopping_lists.finish(self.list, keep=True, by=OTHER, now=self.now)
        self.list.refresh_from_db()
        self.assertIsNone(self.list.finished_at)
        self.assertEqual(ShoppingList.objects.count(), 1)


class RunOrderTests(TestCase):
    def test_unticked_first_then_ticked_each_in_added_order(self):
        shopping_list = make_shopping_list()
        start = timezone.now() - timedelta(hours=1)
        items = [
            make_shopping_item(
                shopping_list,
                label=f"Article {index} exemple",
                added_at=start + timedelta(minutes=minutes),
                checked_at=start + timedelta(minutes=30) if ticked else None,
            )
            for index, (minutes, ticked) in enumerate(((5, True), (1, False), (3, True), (4, False), (4, False)))
        ]
        ordered = shopping_lists.run_order(reversed(items))
        self.assertEqual(ordered, [items[1], items[3], items[4], items[2], items[0]])
        self.assertEqual(shopping_lists.run_order([]), [])


class DisplayNamesTests(TestCase):
    """Who did what, as a list says it to anyone the lists are open to: never
    an address - the owner's is his login, an employee's his contact -, and
    only names this espace's logins carry."""

    NAMED_OWNER = "gerant.prenom.exemple@exemple.fr"
    UNNAMED = "employe.sans.prenom@exemple.fr"
    BLANK_NAME = "employe.blanc@exemple.fr"
    ELSEWHERE = "ailleurs.exemple@exemple.fr"
    NO_ESPACE = "sans.espace.exemple@exemple.fr"
    GONE = "parti.exemple@exemple.fr"
    ME = "moi.exemple@exemple.fr"

    def setUp(self):
        users = get_user_model().objects
        neighbour = Tenant.objects.create(name="Bar voisin exemple", dir_name="bar-voisin-exemple")

        def member(username, first_name="", role=Membership.Role.MEMBER, tenant_id=TEST_TENANT_PK):
            user = users.create_user(username=username, email=username, first_name=first_name)
            Membership.objects.create(user=user, tenant_id=tenant_id, role=role)

        member(EMPLOYEE, "Prénom exemple")
        member(OTHER)
        member(self.BLANK_NAME, "   ")
        # The owner: a signup sets no first name.
        member(OWNER, role=Membership.Role.OWNER)
        member(self.NAMED_OWNER, "Prénom gérant exemple", role=Membership.Role.OWNER)
        # A login of another bar - an address reused once its employee here
        # was removed.
        member(self.ELSEWHERE, "Nom autre bar", tenant_id=neighbour.pk)
        users.create_user(username=self.NO_ESPACE, email=self.NO_ESPACE, first_name="Prénom sans espace exemple")

    def test_each_name(self):
        asked = [
            EMPLOYEE,
            OTHER,
            self.BLANK_NAME,
            OWNER,
            self.NAMED_OWNER,
            self.ELSEWHERE,
            self.NO_ESPACE,
            self.GONE,
            self.ME,
            "",
        ]
        with self.assertNumQueries(1, using="accounts"):
            names = shopping_lists.display_names(asked, self.ME, TEST_TENANT_PK)
        self.assertEqual(
            names,
            {
                EMPLOYEE: "Prénom exemple",
                OTHER: "un employé",
                self.BLANK_NAME: "un employé",
                OWNER: "le gérant",
                self.NAMED_OWNER: "Prénom gérant exemple",
                # Not of this espace, or a login gone: nobody is named.
                self.ELSEWHERE: "un ancien membre",
                self.NO_ESPACE: "un ancien membre",
                self.GONE: "un ancien membre",
                self.ME: "Vous",
            },
        )
        self.assertFalse([said for said in names.values() if "@" in said])
        self.assertEqual(
            (shopping_lists.OWNER_WORD, shopping_lists.MEMBER_WORD, shopping_lists.GONE_WORD),
            ("le gérant", "un employé", "un ancien membre"),
        )

    def test_in_another_espace_its_own_members_are_named(self):
        neighbour = Tenant.objects.get(dir_name="bar-voisin-exemple")
        names = shopping_lists.display_names([self.ELSEWHERE, EMPLOYEE], "", neighbour.pk)
        self.assertEqual(names, {self.ELSEWHERE: "Nom autre bar", EMPLOYEE: "un ancien membre"})

    def test_no_query_when_every_name_is_mine_or_blank(self):
        with self.assertNumQueries(0, using="accounts"):
            self.assertEqual(
                shopping_lists.display_names([EMPLOYEE, "", EMPLOYEE], EMPLOYEE, TEST_TENANT_PK), {EMPLOYEE: "Vous"}
            )
            self.assertEqual(shopping_lists.display_names([], EMPLOYEE, TEST_TENANT_PK), {})

    def test_with_no_viewer_name(self):
        self.assertEqual(shopping_lists.display_names([EMPLOYEE], "", TEST_TENANT_PK), {EMPLOYEE: "Prénom exemple"})

    def test_with_no_espace_nobody_is_named(self):
        self.assertEqual(shopping_lists.display_names([EMPLOYEE], "", None), {EMPLOYEE: "un ancien membre"})


# ---------------------------------------------------------------------------
# Bottles or litres: the pure rules (SPEC_UNITS §2.2-§2.6)
# ---------------------------------------------------------------------------


class ArticleItemTests(SimpleTestCase):
    """What one item (« une bouteille ») of an article is at a store
    (`article_item`): the usual product there when the usual purchase counts
    one; else, for an article in litres or kilos, the format it is most
    bought in anywhere; else none."""

    def setUp(self):
        self.made = invented()

    def item(self, article, usual=None, sizes=None, typical=None):
        return shopping_lists.article_item(article, usual, sizes or {}, typical)

    def test_the_usual_product_when_the_usual_purchase_counts_one(self):
        made = self.made
        self.assertEqual(self.item(made.gin, made.gin_usual, {21: D("0.7")}), made.gin_item)
        # Whatever the format bought most elsewhere: it is what the forecast counts.
        self.assertEqual(self.item(made.gin, made.gin_usual, {21: D("0.7")}, D("1.5")), made.gin_item)
        # A UNIT article's too: its pack.
        self.assertEqual(self.item(made.cups, made.cups_usual, {24: D("50")}), made.cups_item)

    def test_a_weighed_usual_product_is_an_item_with_no_size(self):
        made = self.made
        self.assertEqual(self.item(made.olives, made.olives_usual, {}, D("0.5")), made.olives_item)
        # A size no column holds is no size.
        no_size = shopping_lists.ItemOf(None, "", 21, GIN_PRODUCT, 6)
        for size in (D("0"), D("-0.7"), D("1000000"), D("NaN")):
            with self.subTest(size=size):
                self.assertEqual(self.item(made.gin, made.gin_usual, {21: size}), no_size)

    def test_otherwise_the_format_most_bought_anywhere(self):
        made = self.made
        # Bought here by measure.
        self.assertEqual(self.item(made.juice, made.juice_usual, {}, D("1")), made.litre)
        # Never bought here.
        self.assertEqual(self.item(made.vodka, None, {}, D("0.7")), made.bottle)
        self.assertEqual(self.item(made.keg, None, {}, D("30")), made.keg_item)
        self.assertEqual(self.item(made.olives, None, {}, D("0.5")), made.half_kilo)
        # Four places, half up.
        self.assertEqual(self.item(made.vodka, None, {}, D(1) / 3).size, D("0.3333"))
        self.assertEqual(self.item(made.vodka, None, {}, D("0.00005")).size, D("0.0001"))

    def test_no_format_known_no_item(self):
        made = self.made
        for article, usual, typical in (
            (made.vodka, None, None),
            (made.juice, made.juice_usual, None),
            (made.olives, None, None),
            (made.vodka, None, D("0")),
            (made.vodka, None, D("0.00004")),
            (made.vodka, None, D("1000000")),
        ):
            with self.subTest(article=article.name, typical=typical):
                self.assertIsNone(self.item(article, usual, {}, typical))

    def test_a_unit_article_never_gets_a_format(self):
        # Its unit already counts pieces: its items are its usual product's.
        made = self.made
        self.assertIsNone(self.item(made.cups, None, {}, D("50")))
        self.assertIsNone(self.item(made.beer, made.beer_usual, {}, D("24")))


class EntryUnitsTests(SimpleTestCase):
    """The units an entry offers, and the one taken when none is chosen
    (SPEC_UNITS §2.3)."""

    def setUp(self):
        self.made = invented()

    def units(self, entry, usual=None, item=None, discrete=True):
        return shopping_lists.entry_units(entry, usual=usual, item=item, discrete=discrete)

    def test_a_product_the_stock_take_s_rule(self):
        made = self.made
        for product, discrete, expected in (
            (made.cups_pack, True, EntryUnits((UNIT,), UNIT)),
            (made.gin_bottle, True, EntryUnits((UNIT, L), UNIT)),
            # A weighed product: kilos by default.
            (made.olives_bag, False, EntryUnits((UNIT, KG), KG)),
        ):
            with self.subTest(product=product.raw_name):
                self.assertEqual(self.units(product_entry(product), discrete=discrete), expected)
                self.assertEqual(entries.product_units(product, is_discrete=discrete), expected)
        # Whatever the store's usual purchase.
        self.assertEqual(
            self.units(product_entry(made.gin_bottle), made.juice_usual, made.litre), EntryUnits((UNIT, L), UNIT)
        )

    def test_an_article_of_unit_unit_counts_its_units(self):
        made = self.made
        for usual, item in ((None, None), (made.beer_usual, None), (made.cups_usual, made.cups_item)):
            with self.subTest(usual=usual):
                self.assertEqual(self.units(article_entry(made.cups), usual, item), EntryUnits((UNIT,), UNIT))

    def test_an_article_with_an_item(self):
        made = self.made
        for article, usual, item, expected in (
            # Its usual purchase here counts a product: items.
            (made.gin, made.gin_usual, made.gin_item, EntryUnits((UNIT, L), UNIT)),
            (made.olives, made.olives_usual, made.olives_item, EntryUnits((UNIT, KG), UNIT)),
            # Bought here by measure: its measure, as today.
            (made.juice, made.juice_usual, made.litre, EntryUnits((UNIT, L), L)),
            # Never bought here: « on parle en bouteilles » for litres, kilos for kilos.
            (made.vodka, None, made.bottle, EntryUnits((UNIT, L), UNIT)),
            (made.keg, None, made.keg_item, EntryUnits((UNIT, L), UNIT)),
            (made.olives, None, made.half_kilo, EntryUnits((UNIT, KG), KG)),
        ):
            with self.subTest(article=article.name, usual=usual):
                self.assertEqual(self.units(article_entry(article), usual, item), expected)

    def test_an_article_with_no_item_counts_its_measure_alone(self):
        made = self.made
        for article, usual, expected in (
            (made.vodka, None, EntryUnits((L,), L)),
            (made.juice, made.juice_usual, EntryUnits((L,), L)),
            (made.olives, None, EntryUnits((KG,), KG)),
        ):
            with self.subTest(article=article.name):
                self.assertEqual(self.units(article_entry(article), usual, None), expected)


class EntryLabelsTests(SimpleTestCase):
    """What the select says of each unit an entry offers (SPEC_UNITS §2.5)."""

    def setUp(self):
        self.made = invented()

    def labels(self, entry, usual=None, item=None, product_size=None, discrete=True):
        units = shopping_lists.entry_units(entry, usual=usual, item=item, discrete=discrete)
        return shopping_lists.entry_labels(entry, units, item=item, product_size=product_size)

    def test_an_article_s(self):
        made = self.made
        bottles = ((UNIT, "bouteilles de 70 cl"), (L, "litres"))
        beer_can = shopping_lists.ItemOf(D("1"), UNIT, 7, "BIERE EXEMPLE 33CL X24", 24)
        for article, usual, item, expected in (
            (made.gin, made.gin_usual, made.gin_item, bottles),
            (made.vodka, None, made.bottle, bottles),
            (made.juice, made.juice_usual, made.litre, ((UNIT, "bouteilles de 1 L"), (L, "litres"))),
            (made.keg, None, made.keg_item, ((UNIT, "fûts de 30 L"), (L, "litres"))),
            (made.olives, None, made.half_kilo, ((UNIT, "paquets de 500 g"), (KG, "kg"))),
            (made.olives, made.olives_usual, made.olives_item, ((UNIT, "unités"), (KG, "kg"))),
            (made.juice, made.juice_usual, None, ((L, "litres (format inconnu)"),)),
            (made.olives, None, None, ((KG, "kg"),)),
            (made.cups, made.cups_usual, made.cups_item, ((UNIT, "paquets de 50 u."),)),
            (made.beer, made.beer_usual, None, ((UNIT, "unités"),)),
            (made.beer, Figures(D("24"), "", 7, "BIERE EXEMPLE 33CL X24", 24), beer_can, ((UNIT, "unités"),)),
        ):
            with self.subTest(article=article.name, item=item):
                self.assertEqual(self.labels(article_entry(article), usual, item), expected)

    def test_a_product_s(self):
        made = self.made
        for product, size, discrete, expected in (
            (made.gin_bottle, D("0.7000"), True, ((UNIT, "bouteilles de 70 cl"), (L, "litres"))),
            (made.other_gin, D("1.0000"), True, ((UNIT, "bouteilles de 1 L"), (L, "litres"))),
            (made.olives_bag, None, False, ((UNIT, "unités"), (KG, "kg"))),
            (made.cups_pack, D("50.0000"), True, ((UNIT, "paquets de 50 u."),)),
            # Bought by the carton: a pack, never a keg.
            (made.gin_carton, D("4.2000"), True, ((UNIT, "packs de 4.2 L"), (L, "litres"))),
        ):
            with self.subTest(product=product.raw_name):
                self.assertEqual(self.labels(product_entry(product), product_size=size, discrete=discrete), expected)

    def test_an_article_counted_in_its_usual_product_s_packs(self):
        made = self.made
        usual = Figures(D("1"), "", 26, made.gin_carton.raw_name, None)
        item = shopping_lists.ItemOf(D("4.2000"), L, 26, made.gin_carton.raw_name, None)
        self.assertEqual(self.labels(article_entry(made.gin), usual, item), ((UNIT, "packs de 4.2 L"), (L, "litres")))


class EntryFiguresTests(SimpleTestCase):
    """What each entry and unit stores (SPEC_UNITS §2.4), row by row: (q,
    unit, product, name, pack, size, size's unit), `q` the quantity typed or
    None."""

    def setUp(self):
        self.made = invented()

    def figures(self, entry, unit, quantity=None, usual=None, item=None, product_size=None):
        return shopping_lists.entry_figures(
            entry, unit, None if quantity is None else D(quantity), usual=usual, item=item, product_size=product_size
        )

    def test_an_article_in_items_of_its_usual_product(self):
        made = self.made
        gin = article_entry(made.gin)
        # Nothing typed: the usual count, its pack, the size of one bottle.
        self.assertEqual(self.figures(gin, UNIT, None, made.gin_usual, made.gin_item), gin_bottles())
        self.assertEqual(self.figures(gin, UNIT, "3", made.gin_usual, made.gin_item), gin_bottles("3"))
        # Packs of 50 cups.
        self.assertEqual(
            self.figures(article_entry(made.cups), UNIT, None, made.cups_usual, made.cups_item),
            Figures(D("4"), "", 24, "GOBELETS EXEMPLE X50", None, D("50"), UNIT),
        )
        # A weighed product: a bare number of it, as today.
        self.assertEqual(
            self.figures(article_entry(made.olives), UNIT, None, made.olives_usual, made.olives_item),
            Figures(D("3"), "", 23, "OLIVES EXEMPLE VRAC", None),
        )

    def test_an_article_in_items_of_its_format(self):
        made = self.made
        vodka = article_entry(made.vodka)
        self.assertEqual(
            self.figures(vodka, UNIT, None, None, made.bottle), Figures(D("1"), "", None, "", None, BOTTLE, L)
        )
        self.assertEqual(
            self.figures(vodka, UNIT, "2", None, made.bottle), Figures(D("2"), "", None, "", None, BOTTLE, L)
        )
        # Bought here by measure: one bottle, never its usual litres.
        self.assertEqual(
            self.figures(article_entry(made.juice), UNIT, None, made.juice_usual, made.litre),
            Figures(D("1"), "", None, "", None, D("1"), L),
        )

    def test_an_article_of_unit_unit_keeps_today_s_figures(self):
        made = self.made
        beer = article_entry(made.beer)
        self.assertEqual(self.figures(beer, UNIT, None, made.beer_usual), Figures(D("12"), UNIT, None, "", None))
        self.assertEqual(self.figures(beer, UNIT, "5", made.beer_usual), Figures(D("5"), UNIT, None, "", None))
        self.assertEqual(self.figures(beer, UNIT), Figures(D("1"), UNIT, None, "", None))

    def test_an_article_in_its_measure(self):
        made = self.made
        thirds = dataclasses.replace(made.gin_item, size=D("0.3333"))
        tiny = dataclasses.replace(made.gin_item, size=D("0.0005"))
        for article, usual, item, quantity, expected in (
            # Bought here by measure: the usual measure.
            (made.juice, made.juice_usual, made.litre, None, D("2.5")),
            # Counted here in bottles: the usual bottles, in litres.
            (made.gin, made.gin_usual, made.gin_item, None, D("4.2")),
            # Three places, half up.
            (made.gin, Figures(D("3"), "", 21, GIN_PRODUCT, 6), thirds, None, D("1.000")),
            (made.gin, Figures(D("7"), "", 21, GIN_PRODUCT, 6), tiny, None, D("0.004")),
            # A weighed usual product, or never bought here: 1.
            (made.olives, made.olives_usual, made.olives_item, None, D("1")),
            (made.vodka, None, made.bottle, None, D("1")),
            (made.vodka, None, None, None, D("1")),
            # Typed: as typed.
            (made.gin, made.gin_usual, made.gin_item, "2", D("2")),
        ):
            with self.subTest(article=article.name, usual=usual, quantity=quantity):
                figures = self.figures(article_entry(article), article.unit, quantity, usual, item)
                self.assertEqual(figures, Figures(expected, article.unit, None, "", None))
                self.assertTrue(shopping_lists.fits(figures.quantity), figures.quantity)

    def test_a_product_in_items(self):
        made = self.made
        gin = product_entry(made.gin_bottle)
        # The store's usual product: its usual count and its pack.
        self.assertEqual(self.figures(gin, UNIT, None, made.gin_usual, product_size=BOTTLE), gin_bottles())
        self.assertEqual(self.figures(gin, UNIT, "12", made.gin_usual, product_size=BOTTLE), gin_bottles("12"))
        # Another product of the article: 1, and no pack - the usual count is another's.
        self.assertEqual(
            self.figures(product_entry(made.other_gin), UNIT, None, made.gin_usual, product_size=D("1")),
            Figures(D("1"), "", 22, "GIN EXEMPLE 1L", None, D("1"), L),
        )
        # Never bought here.
        self.assertEqual(
            self.figures(gin, UNIT, None, None, product_size=BOTTLE),
            Figures(D("1"), "", 21, GIN_PRODUCT, None, BOTTLE, L),
        )
        # A weighed product: no size.
        self.assertEqual(
            self.figures(product_entry(made.olives_bag), UNIT, None, made.olives_usual),
            Figures(D("3"), "", 23, "OLIVES EXEMPLE VRAC", None),
        )
        # Packs of 50 cups.
        self.assertEqual(
            self.figures(product_entry(made.cups_pack), UNIT, None, made.cups_usual, product_size=D("50")),
            Figures(D("4"), "", 24, "GOBELETS EXEMPLE X50", None, D("50"), UNIT),
        )

    def test_a_product_in_its_article_s_measure_keeps_its_name(self):
        # The stock take's « mesuré directement »: the product, a hint.
        made = self.made
        gin = product_entry(made.gin_bottle)
        # Nothing typed, the usual product here: its usual bottles times the
        # size of one - the article entry's rule (« celle d'habitude ici »).
        self.assertEqual(
            self.figures(gin, L, None, made.gin_usual, product_size=BOTTLE),
            Figures(D("4.2"), L, 21, GIN_PRODUCT, None),
        )
        self.assertEqual(
            self.figures(gin, L, None, made.gin_usual, product_size=BOTTLE).quantity,
            self.figures(article_entry(made.gin), L, None, made.gin_usual, made.gin_item).quantity,
        )
        # Three places, half up.
        self.assertEqual(
            self.figures(gin, L, None, Figures(D("7"), "", 21, GIN_PRODUCT, 6), product_size=D("0.0005")),
            Figures(D("0.004"), L, 21, GIN_PRODUCT, None),
        )
        # Typed: as typed.
        self.assertEqual(
            self.figures(gin, L, "1.5", made.gin_usual, product_size=BOTTLE),
            Figures(D("1.5"), L, 21, GIN_PRODUCT, None),
        )
        # Another product of the article, never bought here, or of no size: 1.
        for entry, usual, size in (
            (product_entry(made.other_gin), made.gin_usual, D("1")),
            (gin, None, BOTTLE),
            (gin, made.gin_usual, None),
            (gin, made.juice_usual, BOTTLE),
        ):
            with self.subTest(product=entry.product.raw_name, usual=usual, size=size):
                figures = self.figures(entry, L, None, usual, product_size=size)
                self.assertEqual(figures, Figures(D("1"), L, entry.product.pk, entry.product.raw_name, None))
        self.assertEqual(
            self.figures(product_entry(made.olives_bag), KG, "2.5", made.olives_usual),
            Figures(D("2.5"), KG, 23, "OLIVES EXEMPLE VRAC", None),
        )

    def test_a_free_text(self):
        self.assertEqual(self.figures(None, ""), ONE)
        self.assertEqual(self.figures(None, "", "2"), Figures(D("2"), "", None, "", None))

    def test_a_unit_not_offered_is_a_bug(self):
        made = self.made
        for entry, unit, item in (
            (None, UNIT, None),
            # No item known: no bottles.
            (article_entry(made.vodka), UNIT, None),
            (article_entry(made.gin), KG, made.gin_item),
            (article_entry(made.cups), L, made.cups_item),
            (product_entry(made.cups_pack), L, None),
            (product_entry(made.gin_bottle), KG, None),
            (article_entry(made.gin), "", made.gin_item),
        ):
            with self.subTest(entry=entry, unit=unit), self.assertRaises(ValueError):
                self.figures(entry, unit, None, None, item)


class CardTestCase(SimpleTestCase):
    """Items as the card reads them, unsaved (invented): the gin in bottles
    of 70 cl and in litres - of its usual product, and of another one, « GIN
    EXEMPLE 1L » -, the vodka in litres, the beer in cans of its product and
    in its own units, cups in packs of 50, gin bottles added before sizes
    existed, a free text, and items whose article's unit was edited since
    (the olives now by the kilo, the cups by the piece)."""

    def setUp(self):
        made = self.made = invented()
        self.gin_bottles = an_item(made.gin, "6", product_name=GIN_PRODUCT, pack_size=6, item_size=BOTTLE, size_unit=L)
        self.gin_litres = an_item(made.gin, "4.2", unit=L, product_name=GIN_PRODUCT)
        self.other_gin_litres = an_item(made.gin, "2", unit=L, product_name="GIN EXEMPLE 1L")
        self.vodka_litres = an_item(made.vodka, "2", unit=L)
        self.beer_cans = an_item(
            made.beer, "24", product_name="BIERE EXEMPLE 33CL X24", pack_size=24, item_size=D("1"), size_unit=UNIT
        )
        self.beer_units = an_item(made.beer, "6", unit=UNIT)
        self.cups_packs = an_item(
            made.cups, "2", product_name="GOBELETS EXEMPLE X50", item_size=D("50"), size_unit=UNIT
        )
        self.old_bottles = an_item(made.gin, "6", product_name=GIN_PRODUCT, pack_size=6)
        self.bread = an_item(None, "2")
        self.olives_litres = an_item(made.olives, "2", unit=L)
        self.cups_litres = an_item(made.cups, "2", unit=L)

    def every_item(self) -> list[ShoppingListItem]:
        return [
            self.gin_bottles,
            self.gin_litres,
            self.other_gin_litres,
            self.vodka_litres,
            self.beer_cans,
            self.beer_units,
            self.cups_packs,
            self.old_bottles,
            self.olives_litres,
            self.cups_litres,
        ]

    def every_item_now(self) -> list:
        made = self.made
        return [
            None,
            made.gin_item,
            made.bottle,
            made.litre,
            made.keg_item,
            made.half_kilo,
            made.olives_item,
            made.cups_item,
        ]


class CardItemTests(CardTestCase):
    """What the card's items option counts (`card_item`), the one rule its
    units, its labels and what it stores go through: an item counting items
    keeps its own; one counting a measure takes the item known now - unless
    it names a product that one does not: that product, found again with its
    size (`own`), else a bare number of it."""

    def test_an_item_counting_items_keeps_its_own(self):
        made = self.made
        for item_now in (None, made.bottle, made.litre):
            with self.subTest(item_now=item_now):
                self.assertEqual(
                    shopping_lists.card_item(self.gin_bottles, item_now),
                    shopping_lists.ItemOf(BOTTLE, L, None, GIN_PRODUCT, 6),
                )
        self.assertEqual(
            shopping_lists.card_item(self.old_bottles, made.gin_item),
            shopping_lists.ItemOf(None, "", None, GIN_PRODUCT, 6),
        )

    def test_a_measure_takes_the_item_known_now(self):
        made = self.made
        self.assertEqual(shopping_lists.card_item(self.gin_litres, made.gin_item), made.gin_item)
        self.assertEqual(shopping_lists.card_item(self.vodka_litres, made.bottle), made.bottle)
        # Its own product found again changes nothing when it is the one known now.
        self.assertEqual(shopping_lists.card_item(self.gin_litres, made.gin_item, made.other_gin_item), made.gin_item)
        self.assertIsNone(shopping_lists.card_item(self.vodka_litres, None))

    def test_unless_it_names_a_product_the_item_now_does_not(self):
        made = self.made
        bare = shopping_lists.ItemOf(None, "", None, "GIN EXEMPLE 1L", None)
        for item_now in (made.gin_item, made.bottle):
            with self.subTest(item_now=item_now):
                # Found again at the store: that product, its size.
                self.assertEqual(
                    shopping_lists.card_item(self.other_gin_litres, item_now, made.other_gin_item),
                    made.other_gin_item,
                )
                # Not found: a bare number of it.
                self.assertEqual(shopping_lists.card_item(self.other_gin_litres, item_now), bare)
        # Nothing known now: its own product found again, else nothing.
        self.assertEqual(
            shopping_lists.card_item(self.other_gin_litres, None, made.other_gin_item), made.other_gin_item
        )
        self.assertIsNone(shopping_lists.card_item(self.other_gin_litres, None))
        self.assertIsNone(shopping_lists.card_item(self.gin_litres, None))

    def test_a_free_text_counts_what_it_names(self):
        self.assertIsNone(shopping_lists.card_item(self.bread, self.made.bottle, self.made.gin_item))


class CardUnitsTests(CardTestCase):
    """The card's units (SPEC_UNITS §2.6): items when the item counts items
    now or one is known now (`item_now`), the article's measure, and the
    item's own measure when the article's unit was edited since."""

    def units(self, item, item_now=None, own=None):
        return shopping_lists.card_units(item, item_now, own)

    def test_a_free_text_has_none(self):
        self.assertIsNone(self.units(self.bread))
        self.assertIsNone(self.units(self.bread, self.made.bottle))

    def test_its_own_product_found_again_is_an_item_known(self):
        made = self.made
        self.assertEqual(self.units(self.other_gin_litres, None, made.other_gin_item), EntryUnits((UNIT, L), L))
        self.assertEqual(self.units(self.other_gin_litres, made.gin_item), EntryUnits((UNIT, L), L))
        self.assertEqual(self.units(self.other_gin_litres, None), EntryUnits((L,), L))

    def test_an_article_of_unit_unit_one_option(self):
        for item in (self.beer_cans, self.beer_units, self.cups_packs):
            with self.subTest(item=item.label, unit=item.unit):
                self.assertEqual(self.units(item), EntryUnits((UNIT,), UNIT))
                self.assertEqual(self.units(item, self.made.cups_item), EntryUnits((UNIT,), UNIT))

    def test_items_and_the_article_s_measure(self):
        made = self.made
        for item, item_now, expected in (
            # Counting items now: items, then the measure; its terms selected.
            (self.gin_bottles, None, EntryUnits((UNIT, L), UNIT)),
            (self.gin_bottles, made.gin_item, EntryUnits((UNIT, L), UNIT)),
            (self.old_bottles, None, EntryUnits((UNIT, L), UNIT)),
            # Counting the measure, an item known now: both.
            (self.gin_litres, made.gin_item, EntryUnits((UNIT, L), L)),
            (self.vodka_litres, made.bottle, EntryUnits((UNIT, L), L)),
            # A measure and no item now: no bottles.
            (self.gin_litres, None, EntryUnits((L,), L)),
            (self.vodka_litres, None, EntryUnits((L,), L)),
        ):
            with self.subTest(item=item.label, unit=item.unit, item_now=item_now):
                self.assertEqual(self.units(item, item_now), expected)

    def test_an_article_whose_unit_was_edited_keeps_the_item_s_measure(self):
        # Saving the card unchanged is never refused.
        self.assertEqual(self.units(self.olives_litres), EntryUnits((KG, L), L))
        self.assertEqual(self.units(self.olives_litres, self.made.half_kilo), EntryUnits((UNIT, KG, L), L))
        self.assertEqual(self.units(self.cups_litres), EntryUnits((UNIT, L), L))


class CardLabelsTests(CardTestCase):
    def labels(self, item, item_now=None, own=None):
        return shopping_lists.card_labels(item, shopping_lists.card_units(item, item_now, own), item_now, own)

    def test_each_item_s(self):
        made = self.made
        bottles = ((UNIT, "bouteilles de 70 cl"), (L, "litres"))
        for item, item_now, expected in (
            (self.gin_bottles, None, bottles),
            (self.gin_bottles, made.gin_item, bottles),
            (self.gin_litres, made.gin_item, bottles),
            (self.vodka_litres, made.bottle, bottles),
            (self.gin_litres, None, ((L, "litres (format inconnu)"),)),
            # Another product than the one known now, not found again: a bare
            # number of it - « unités », never the usual product's bottles.
            (self.other_gin_litres, made.gin_item, ((UNIT, "unités"), (L, "litres"))),
            (self.gin_litres, made.bottle, ((UNIT, "unités"), (L, "litres"))),
            # What the item counts now says its items: bottles of no size known.
            (self.old_bottles, made.gin_item, ((UNIT, "unités"), (L, "litres"))),
            (self.old_bottles, None, ((UNIT, "unités"), (L, "litres (format inconnu)"))),
            (self.beer_cans, None, ((UNIT, "unités"),)),
            (self.beer_units, None, ((UNIT, "unités"),)),
            # Its own units are items already: what it counts, never another's packs.
            (self.beer_units, made.cups_item, ((UNIT, "unités"),)),
            (self.cups_packs, None, ((UNIT, "paquets de 50 u."),)),
            (self.olives_litres, None, ((KG, "kg"), (L, "litres"))),
            (self.olives_litres, made.half_kilo, ((UNIT, "paquets de 500 g"), (KG, "kg"), (L, "litres"))),
        ):
            with self.subTest(item=item.label, unit=item.unit, item_now=item_now):
                self.assertEqual(self.labels(item, item_now), expected)

    def test_its_own_product_found_again(self):
        made = self.made
        litre_bottles = ((UNIT, "bouteilles de 1 L"), (L, "litres"))
        self.assertEqual(self.labels(self.other_gin_litres, made.gin_item, made.other_gin_item), litre_bottles)
        self.assertEqual(self.labels(self.other_gin_litres, made.bottle, made.other_gin_item), litre_bottles)
        self.assertEqual(self.labels(self.other_gin_litres, None, made.other_gin_item), litre_bottles)
        self.assertEqual(
            self.labels(self.gin_litres, made.bottle, made.gin_item), ((UNIT, "bouteilles de 70 cl"), (L, "litres"))
        )

    def test_the_items_option_says_what_the_card_stores(self):
        """Every item, with every item known now and every product found
        again: wherever the card offers items, its label is what saving it
        stores, said back (`unit_label` of the figures `card_figures` gives)."""
        made = self.made
        checked = 0
        for item in self.every_item():
            for item_now in self.every_item_now():
                for own in (None, made.other_gin_item, made.gin_item):
                    units = shopping_lists.card_units(item, item_now, own)
                    if ITEMS not in units.choices:
                        continue
                    with self.subTest(item=item.label, unit=item.unit, item_now=item_now, own=own):
                        label = dict(shopping_lists.card_labels(item, units, item_now, own))[ITEMS]
                        stored = shopping_lists.card_figures(item, ITEMS, D("2"), item_now, own)
                        said = shopping_lists.unit_label(
                            ITEMS,
                            article_unit=item.stock_type.unit,
                            size=stored.item_size,
                            size_unit=stored.size_unit,
                            product_name=stored.product_name,
                        )
                        self.assertEqual(label, said)
                        checked += 1
        self.assertGreater(checked, 100)

    def test_a_free_text_has_none(self):
        self.assertEqual(shopping_lists.card_labels(self.bread, None, None), ())


class CardFiguresTests(CardTestCase):
    """What the card's unit does (SPEC_UNITS §2.6): its present terms change
    only the quantity; items to the measure clear the packs and the sizes and
    keep the product's name; the measure to items takes the item known now -
    the number never converted."""

    def figures(self, item, unit, quantity, item_now=None, own=None):
        return shopping_lists.card_figures(item, unit, D(quantity), item_now, own)

    def test_its_present_terms_only_the_quantity_changes(self):
        made = self.made
        kept_bottles = Figures(D("8"), "", None, GIN_PRODUCT, 6, BOTTLE, L)
        for item, unit, item_now, expected in (
            (self.gin_bottles, UNIT, made.gin_item, kept_bottles),
            # No unit posted (an old page): kept.
            (self.gin_bottles, "", made.gin_item, kept_bottles),
            (self.gin_bottles, None, None, kept_bottles),
            (self.gin_litres, L, made.gin_item, Figures(D("8"), L, None, GIN_PRODUCT, None)),
            (self.beer_cans, UNIT, None, Figures(D("8"), "", None, "BIERE EXEMPLE 33CL X24", 24, D("1"), UNIT)),
            (self.beer_units, UNIT, None, Figures(D("8"), UNIT, None, "", None)),
            (self.bread, "", None, Figures(D("8"), "", None, "", None)),
            (self.bread, None, None, Figures(D("8"), "", None, "", None)),
        ):
            with self.subTest(item=item.label, unit=unit):
                self.assertEqual(self.figures(item, unit, "8", item_now), expected)

    def test_items_to_the_measure(self):
        # Never converted: the number typed counts the unit chosen.
        for item_now in (self.made.gin_item, None):
            with self.subTest(item_now=item_now):
                self.assertEqual(
                    self.figures(self.gin_bottles, L, "4.2", item_now), Figures(D("4.2"), L, None, GIN_PRODUCT, None)
                )

    def test_the_measure_to_items_takes_the_item_now(self):
        made = self.made
        self.assertEqual(self.figures(self.gin_litres, UNIT, "6", made.gin_item), gin_bottles())
        self.assertEqual(
            self.figures(self.vodka_litres, UNIT, "3", made.bottle), Figures(D("3"), "", None, "", None, BOTTLE, L)
        )
        # A weighed usual product: a bare number of it.
        olives = an_item(made.olives, "2", unit=KG)
        self.assertEqual(
            self.figures(olives, UNIT, "3", made.olives_item), Figures(D("3"), "", 23, "OLIVES EXEMPLE VRAC", None)
        )

    def test_unless_the_item_names_a_product_the_item_now_does_not(self):
        made = self.made
        other = an_item(made.gin, "2", unit=L, product_name="GIN EXEMPLE 1L")
        self.assertEqual(
            self.figures(other, UNIT, "2", made.gin_item), Figures(D("2"), "", None, "GIN EXEMPLE 1L", None)
        )
        # The item now a format: the item's product all the same, with no size.
        self.assertEqual(
            self.figures(self.gin_litres, UNIT, "2", made.bottle), Figures(D("2"), "", None, GIN_PRODUCT, None)
        )

    def test_its_own_product_found_again_keeps_its_size(self):
        made = self.made
        litre_bottles = Figures(D("2"), "", 22, "GIN EXEMPLE 1L", None, D("1.0000"), L)
        for item_now in (made.gin_item, made.bottle, None):
            with self.subTest(item_now=item_now):
                self.assertEqual(
                    self.figures(self.other_gin_litres, UNIT, "2", item_now, made.other_gin_item), litre_bottles
                )
        self.assertEqual(self.figures(self.gin_litres, UNIT, "6", made.bottle, made.gin_item), gin_bottles())
        # To the measure, or kept: what is found again changes nothing.
        self.assertEqual(
            self.figures(self.other_gin_litres, L, "3", made.gin_item, made.other_gin_item),
            Figures(D("3"), L, None, "GIN EXEMPLE 1L", None),
        )

    def test_an_article_whose_unit_was_edited(self):
        self.assertEqual(self.figures(self.olives_litres, KG, "2"), Figures(D("2"), KG, None, "", None))
        self.assertEqual(self.figures(self.olives_litres, L, "3"), Figures(D("3"), L, None, "", None))
        # A UNIT article: its own units.
        self.assertEqual(self.figures(self.cups_litres, UNIT, "2"), Figures(D("2"), UNIT, None, "", None))

    def test_a_unit_not_offered_is_a_bug(self):
        made = self.made
        for item, unit, item_now in (
            (self.bread, UNIT, None),
            (self.bread, L, None),
            (self.gin_litres, UNIT, None),
            (self.gin_bottles, KG, made.gin_item),
            (self.beer_units, L, None),
        ):
            with self.subTest(item=item.label, unit=unit), self.assertRaises(ValueError):
                self.figures(item, unit, "2", item_now)


# ---------------------------------------------------------------------------
# Bottles or litres: what is read (SPEC_UNITS §4.2)
# ---------------------------------------------------------------------------


def said_as(label: str, quantity) -> set[str]:
    """The words an item stored under a select's `label` may read as, for
    `quantity`."""
    number = plain_number(quantity)
    if label in ("litres", shopping_lists.NO_FORMAT_WORDS):
        return {f"{number} L"}
    if label == "kg":
        return {f"{number} kg"}
    if label == "unités":
        return {number, f"{number} u."}
    plural, size = label.split(" de ", 1)
    singular = {"bouteilles": "bouteille", "fûts": "fût", "packs": "pack", "paquets": "paquet"}[plural]
    return {f"{number} {plural if quantity >= 2 else singular} de {size}"}


class ArticleItemOfTests(TestCase):
    """`article_item` for one article with what it needs read: the usual
    product's size (two queries), else the format bought most anywhere
    (three), nothing for a UNIT article counting no product here."""

    def setUp(self):
        self.today = timezone.localdate()
        self.store = make_supplier(name="Grossiste exemple")
        grocer = make_supplier(name="Épicerie exemple")
        self.gin = make_stock_type(name="Gin exemple", unit=L)
        self.vodka = make_stock_type(name="Vodka exemple", unit=L)
        self.cups = make_stock_type(name="Gobelets exemple", unit=UNIT)
        self.gin_bottle = make_product(supplier=self.store, raw_name=GIN_PRODUCT, stock_type=self.gin, unit=L)
        for days_ago in (21, 14, 7):
            line = make_invoice_line(
                invoice=make_invoice(supplier=self.store, invoice_date=self.today - timedelta(days=days_ago)),
                product=self.gin_bottle,
                quantity=6,
                colisage=6,
                total_volume="4.2",
                total_ht="45.00",
            )
            make_movement(stock_type=self.gin, quantity="4.2", unit_cost_ht="10.7143", invoice_line=line)
        vodka_bottle = make_product(supplier=grocer, raw_name="VODKA EXEMPLE 70CL", stock_type=self.vodka, unit=L)
        line = make_invoice_line(
            invoice=make_invoice(supplier=grocer, invoice_date=self.today - timedelta(days=3)),
            product=vodka_bottle,
            quantity=2,
            total_volume="1.4",
            total_ht="36.00",
        )
        make_movement(stock_type=self.vodka, quantity="1.4", unit_cost_ht="25.7143", invoice_line=line)

    def test_the_usual_product_s_size_in_two_queries(self):
        usual = shopping_lists.usual_figures(self.today, self.store, self.gin)
        self.assertEqual(usual, Figures(D("6"), "", self.gin_bottle.pk, GIN_PRODUCT, 6))
        with self.assertNumQueries(2):
            item = shopping_lists.article_item_of(self.gin, usual)
        self.assertEqual(item, shopping_lists.ItemOf(D("0.7000"), L, self.gin_bottle.pk, GIN_PRODUCT, 6))

    def test_the_format_bought_most_anywhere_in_three_queries(self):
        usual = shopping_lists.usual_figures(self.today, self.store, self.vodka)
        self.assertIsNone(usual)
        with self.assertNumQueries(3):
            item = shopping_lists.article_item_of(self.vodka, usual)
        self.assertEqual(item, shopping_lists.ItemOf(D("0.7000"), L, None, "", None))

    def test_a_unit_article_counting_no_product_reads_nothing(self):
        with self.assertNumQueries(0):
            self.assertIsNone(shopping_lists.article_item_of(self.cups, None))
            self.assertIsNone(shopping_lists.article_item_of(self.cups, Figures(D("12"), UNIT, None, "", None)))


class ListEntriesTests(TestCase):
    """The add form's menu (`list_entries`): every article and the store's
    classified products, in the store's order, each with its units and their
    labels."""

    def setUp(self):
        self.today = timezone.localdate()
        self.store = make_supplier(name="Grossiste exemple")
        self.elsewhere = make_supplier(name="Épicerie exemple")

    def buy(self, store, article, raw_name="", *, quantity="1", units=1, volume="0", days_ago=7, product=None, **line):
        """One purchase of `article` at `store` as `product` (a new one named
        `raw_name` by default): `units` of it on the line, `quantity` in the
        article's unit on the movement."""
        if product is None:
            product = make_product(supplier=store, raw_name=raw_name, stock_type=article, unit=article.unit)
        line.setdefault("total_ht", "40.00" if D(quantity) > 0 else "-40.00")
        invoice_line = make_invoice_line(
            invoice=make_invoice(supplier=store, invoice_date=self.today - timedelta(days=days_ago)),
            product=product,
            quantity=units,
            total_volume=volume,
            **line,
        )
        make_movement(stock_type=article, quantity=quantity, unit_cost_ht="40", invoice_line=invoice_line)
        return product

    def names(self, store=None) -> list[str]:
        return [entry.name for entry in shopping_lists.list_entries(self.today, store or self.store)]

    def test_the_store_s_articles_then_its_products_then_the_others(self):
        olives = make_stock_type(name="olives exemple", unit=KG)
        beer = make_stock_type(name="Bière exemple", unit=UNIT)
        coffee = make_stock_type(name="Café exemple", unit=KG)
        make_stock_type(name="Écorces exemple", unit=KG)
        make_stock_type(name="Ail exemple", unit=KG)
        self.buy(self.store, olives, "OLIVES EXEMPLE")
        self.buy(self.store, beer, "BIERE EXEMPLE 33CL")
        self.buy(self.elsewhere, coffee, "CAFE EXEMPLE 1KG")
        # A return only is no purchase here; its product is the store's all the same.
        self.buy(self.store, coffee, "Café exemple repris", quantity="-1", units=-1)
        # Not classified: an entry of nothing.
        make_product(supplier=self.store, raw_name="ARTICLE A CLASSER EXEMPLE")
        self.assertEqual(
            self.names(),
            [
                "Bière exemple (article)",
                "olives exemple (article)",
                "BIERE EXEMPLE 33CL — Grossiste exemple",
                "Café exemple repris — Grossiste exemple",
                "OLIVES EXEMPLE — Grossiste exemple",
                "Ail exemple (article)",
                "Café exemple (article)",
                "Écorces exemple (article)",
            ],
        )
        # Another store's product is no entry here; at its store, it is.
        self.assertIn("CAFE EXEMPLE 1KG — Épicerie exemple", self.names(self.elsewhere))
        self.assertNotIn("BIERE EXEMPLE 33CL — Grossiste exemple", self.names(self.elsewhere))

    def test_what_each_entry_ships(self):
        gin = make_stock_type(name="Gin exemple", unit=L)
        vodka = make_stock_type(name="Vodka exemple", unit=L)
        juice = make_stock_type(name="Jus exemple", unit=L)
        # The gin, bought here in bottles of 70 cl, packs of 6.
        bottle = make_product(supplier=self.store, raw_name=GIN_PRODUCT, stock_type=gin, unit=L)
        for days_ago in (21, 14, 7):
            self.buy(
                self.store, gin, quantity="4.2", units=6, volume="4.2", days_ago=days_ago, product=bottle, colisage=6
            )
        # The vodka, bought only at the grocer's, in 70 cl.
        self.buy(self.elsewhere, vodka, "VODKA EXEMPLE 70CL", quantity="1.4", units=2, volume="1.4")
        # The juice, bought here by measure, never twice in one ratio: no format.
        can = make_product(supplier=self.store, raw_name="JUS EXEMPLE VRAC", stock_type=juice, unit=L)
        self.buy(self.store, juice, quantity="2.6", units=D("2.5"), volume="2.6", product=can)
        self.buy(self.store, juice, quantity="3", units=D("3.1"), volume="3", days_ago=14, product=can)
        listed = shopping_lists.list_entries(self.today, self.store)
        for entry in listed:
            self.assertIsInstance(entry, shopping_lists.ListEntry)
        data = {entry.name: entry.as_data() for entry in listed}
        bottles = [["UNIT", "bouteilles de 70 cl"], ["L", "litres"]]
        no_format = ["L", "litres (format inconnu)"]
        self.assertEqual(
            data,
            {
                "Gin exemple (article)": {"kind": "stock_type", "unit_choices": bottles, "default_unit": "UNIT"},
                "Jus exemple (article)": {"kind": "stock_type", "unit_choices": [no_format], "default_unit": "L"},
                f"{GIN_PRODUCT} — Grossiste exemple": {
                    "kind": "product",
                    "unit_choices": bottles,
                    "default_unit": "UNIT",
                },
                "JUS EXEMPLE VRAC — Grossiste exemple": {
                    "kind": "product",
                    "unit_choices": [["UNIT", "unités"], no_format],
                    "default_unit": "L",
                },
                "Vodka exemple (article)": {"kind": "stock_type", "unit_choices": bottles, "default_unit": "UNIT"},
            },
        )
        # The stock take's island's own keys and kinds, and JSON as it is.
        self.assertEqual(json.loads(json.dumps(data)), data)


class ListEntriesHistoryTests(TestCase):
    """`list_entries` over make_shopping_history (invented): its cost, and
    that it says what the add stores."""

    @classmethod
    def setUpTestData(cls):
        cls.made = make_shopping_history()

    def setUp(self):
        self.today = timezone.localdate()

    def queries(self, store) -> int:
        with CaptureQueriesContext(connection) as captured:
            shopping_lists.list_entries(self.today, store)
        return len(captured)

    def more_history(self):
        """More articles - in litres, in kilos, in units - bought at more
        stores, over many more days."""
        for number in range(6):
            store = (self.made.grocer, self.made.market, self.made.wholesaler)[number % 3]
            article = make_stock_type(name=f"Article {number} exemple", unit=(L, KG, UNIT)[number % 3])
            product = make_product(
                supplier=store, raw_name=f"PRODUIT {number} EXEMPLE", stock_type=article, unit=article.unit
            )
            for week in range(1, 9):
                line = make_invoice_line(
                    invoice=make_invoice(supplier=store, invoice_date=self.today - timedelta(days=7 * week + number)),
                    product=product,
                    quantity=2,
                    total_volume="1.5",
                    total_ht="44.00",
                )
                make_movement(stock_type=article, quantity="1.5", invoice_line=line)

    def test_seven_queries_whatever_the_history(self):
        # At the grocer's every read runs: articles in litres and kilos
        # counted by no product there have their format looked up.
        few = self.queries(self.made.grocer)
        self.assertEqual(few, 7)
        self.assertLessEqual(self.queries(self.made.wholesaler), 7)
        self.more_history()
        self.assertEqual(self.queries(self.made.grocer), few)

    def test_the_one_purchase_and_the_store_wide_scan_convert_identically(self):
        # The add (usual_figures, one article) and the menu
        # (usual_purchases_at, every article in one scan) give the same figures.
        articles = list(StockType.objects.order_by("pk"))
        names = dict(Product.objects.filter(stock_type__isnull=False).values_list("id", "raw_name"))
        for store in (self.made.wholesaler, self.made.grocer, self.made.market):
            usuals = shopping_data.usual_purchases_at(self.today, store.pk, articles, names)
            for article in articles:
                with self.subTest(store=store.name, article=article.name):
                    one = shopping_lists.usual_figures(self.today, store, article)
                    wide = usuals.get(article.pk)
                    self.assertEqual(one, None if wide is None else shopping_lists.figures_of_usual(wide, article))

    def test_it_agrees_with_one_article_s_reading_for_every_article_of_every_store(self):
        made = self.made
        for store in (made.wholesaler, made.grocer, made.market):
            menu = {entry.name: entry for entry in shopping_lists.list_entries(self.today, store)}
            for article in StockType.objects.order_by("pk"):
                with self.subTest(store=store.name, article=article.name):
                    usual = shopping_lists.usual_figures(self.today, store, article)
                    item = shopping_lists.article_item_of(article, usual)
                    entry = article_entry(article)
                    units = shopping_lists.entry_units(entry, usual=usual, item=item, discrete=False)
                    listed = menu[entries.stock_type_entry_name(article)]
                    self.assertEqual((listed.kind, listed.units), (ARTICLE_KIND, units))
                    self.assertEqual(
                        listed.labels, shopping_lists.entry_labels(entry, units, item=item, product_size=None)
                    )

    def test_every_entry_and_unit_offered_is_stored_as_its_label_says(self):
        """The page and the add agree: each entry the menu offers, resolved
        as the add form's name is (EntryResolver, forgiving), read as the add
        reads it (`usual_figures`, `article_item_of`, the product's ratios),
        offers the same units under the same labels; and every unit, nothing
        typed or a quantity typed, gives figures `add_item` writes and the
        lists say as the label did."""
        made = self.made
        checked = 0
        for store in (made.wholesaler, made.grocer):
            resolver = EntryResolver(supplier_id=store.pk)
            for listed in shopping_lists.list_entries(self.today, store):
                entry = resolver.resolve(listed.name, forgiving=True)
                self.assertEqual(entry.kind, listed.kind, listed.name)
                usual = shopping_lists.usual_figures(self.today, store, entry.article)
                item = shopping_lists.article_item_of(entry.article, usual)
                size, discrete = None, False
                if entry.product is not None:
                    ratios = product_counting_ratios([entry.product.pk])
                    size = entries.item_size(entry.product, ratios)
                    discrete = is_discrete_count(ratios, entry.product.pk)
                units = shopping_lists.entry_units(entry, usual=usual, item=item, discrete=discrete)
                self.assertEqual(units, listed.units, listed.name)
                self.assertEqual(
                    shopping_lists.entry_labels(entry, units, item=item, product_size=size), listed.labels, listed.name
                )
                for value, label in listed.labels:
                    for typed in (None, D("2")):
                        with self.subTest(store=store.name, entry=listed.name, unit=value, typed=typed):
                            figures = shopping_lists.entry_figures(
                                entry, value, typed, usual=usual, item=item, product_size=size
                            )
                            stored, _said = shopping_lists.add_item(
                                store, by=EMPLOYEE, label=entry.article.name, figures=figures, stock_type=entry.article
                            )
                            stored = ShoppingListItem.objects.get(pk=stored.pk)
                            words = shopping_lists.quantity_words(
                                stored.quantity,
                                stored.unit,
                                stored.item_size,
                                stored.size_unit,
                                product_name=stored.product_name,
                            )
                            self.assertIn(words, said_as(label, stored.quantity))
                            ShoppingListItem.objects.all().delete()
                            checked += 1
        self.assertGreater(checked, 40)


class ConcurrentAddTests(TenancyTestCase):
    """Two requests at once, each on its own connection to a real espace
    file, with production's SQLite options (WAL, IMMEDIATE, a 60 s wait).

    The window is held open the way production holds it: a third connection
    - an import, a gather - has the write lock while the requests arrive.
    As shipped, an add read the open list in autocommit (WAL readers never
    wait) and only then waited for the lock to write: two adds of one free
    text both found nothing, and an add behind a « Courses terminées » wrote
    onto the list just finished. Now the add takes the lock before it reads.
    Every thread is joined with a timeout and the lock always released."""

    WAIT = 30

    def setUp(self):
        super().setUp()
        # The test settings' plain `default` has no SQLITE_OPTIONS (no WAL,
        # no IMMEDIATE): a threaded test patches production's in.
        self.enterContext(
            mock.patch.dict(connections.settings[DEFAULT_DB_ALIAS], {"OPTIONS": dict(settings.SQLITE_OPTIONS)})
        )
        self.tenant = self.make_tenant("Bar Essai")
        with bound_tenant(self.tenant):
            self.store = make_supplier(name="Grossiste exemple")
            beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
            self.list = make_shopping_list(self.store, created_by=OTHER)
            make_shopping_item(self.list, beer, quantity="24")

    def holding_the_write_lock(self) -> sqlite3.Connection:
        """A connection of its own holding the espace's write lock, as a
        running import does."""
        holder = sqlite3.connect(str(paths.tenant_database(self.tenant)), isolation_level=None, timeout=self.WAIT)
        holder.execute("BEGIN IMMEDIATE")
        return holder

    @staticmethod
    def release(holder: sqlite3.Connection) -> None:
        try:
            if holder.in_transaction:
                holder.execute("COMMIT")
        finally:
            holder.close()

    def join(self, *workers) -> None:
        for worker in workers:
            worker.join(self.WAIT)
        self.assertFalse([worker for worker in workers if worker.is_alive()], "a request never finished")

    def test_a_free_text_typed_twice_at_once_is_one_item(self):
        lock = threading.Lock()
        looked, said, errors = [], [], []
        both_looked = threading.Event()
        start = threading.Barrier(2, timeout=self.WAIT)
        real = shopping_lists._listed

        def counted(*args):
            with lock:
                looked.append(args)
                if len(looked) == 2:
                    both_looked.set()
            return real(*args)

        def add(label):
            try:
                start.wait()
                _item, outcome_ = shopping_lists.add_item(self.store, by=EMPLOYEE, label=label, figures=ONE)
            except Exception:  # noqa: BLE001 - reported below, with where it came from
                errors.append(traceback.format_exc())
            else:
                with lock:
                    said.append(outcome_)

        with bound_tenant(self.tenant):
            workers = [threading.Thread(target=bound(add), args=(label,)) for label in ("Pain exemple", "pain exemple")]
        with mock.patch.object(shopping_lists, "_listed", side_effect=counted):
            holder = self.holding_the_write_lock()
            try:
                for worker in workers:
                    worker.start()
                # As shipped both look while the lock is held, and find
                # nothing; now neither looks before it has the lock.
                both_looked.wait(2)
            finally:
                self.release(holder)
            self.join(*workers)
        self.assertEqual(errors, [])
        with bound_tenant(self.tenant):
            labels = list(ShoppingListItem.objects.filter(stock_type__isnull=True).values_list("label", flat=True))
        # One item, as whichever request got the lock first typed it.
        self.assertEqual(len(labels), 1, labels)
        self.assertIn(labels[0], ("Pain exemple", "pain exemple"))
        self.assertCountEqual(said, [outcome("ADDED"), outcome("ALREADY")])

    def test_an_add_behind_a_finish_is_never_lost(self):
        looked = threading.Event()
        results, errors = {}, []
        real = shopping_lists._listed

        def signalled(*args):
            looked.set()
            return real(*args)

        def add():
            try:
                results["add"] = shopping_lists.add_item(self.store, by=EMPLOYEE, label="Truc exemple", figures=ONE)
            except Exception:  # noqa: BLE001 - reported below, with where it came from
                errors.append(traceback.format_exc())

        def finish():
            try:
                results["finish"] = shopping_lists.finish(self.list, keep=True, by=OTHER, now=timezone.now())
            except Exception:  # noqa: BLE001 - reported below, with where it came from
                errors.append(traceback.format_exc())

        with bound_tenant(self.tenant):
            adder = threading.Thread(target=bound(add))
            finisher = threading.Thread(target=bound(finish))
        with mock.patch.object(shopping_lists, "_listed", side_effect=signalled):
            holder = self.holding_the_write_lock()
            try:
                adder.start()
                # « Ajouter » pressed: as shipped it reads the open list now.
                looked.wait(1)
                # « Courses terminées » pressed a moment later, on another
                # phone: it waits for the lock too.
                finisher.start()
                time.sleep(0.3)
            finally:
                self.release(holder)
            self.join(adder, finisher)
        self.assertEqual(errors, [])
        with bound_tenant(self.tenant):
            self.assertFalse(ShoppingList.objects.get(pk=self.list.pk).is_open)
            following = shopping_lists.open_list_of(self.store.pk)
            # Added before the finish (and carried) or after it: on the
            # store's open list either way, never only on the finished one.
            self.assertIsNotNone(following)
            self.assertEqual(sorted(following.items.values_list("label", flat=True)), ["Bière exemple", "Truc exemple"])
        self.assertIs(results["add"][1], outcome("ADDED"))
