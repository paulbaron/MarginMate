"""« Listes de courses », the service module (`inventory/shopping_lists.py`)
without a request: the words a quantity and a pack are written in, the
figures a forecast line or the store's usual purchase gives an item, what
fits the quantity's column, what a typed name and a typed quantity read as,
the stores offered, the one open list per store made by the first item,
adding an item once - a ticked one put back to buy, a list finished or an
add made meanwhile -, the tick as the WANTED state, finishing a list with its
carry-over, the run order, who did what, and the article menu.

`ConcurrentAddTests` races real connections on a real espace file, with
production's SQLite options.

Invented data throughout: every store, article, login and figure is made up.
"""

from __future__ import annotations

import dataclasses
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
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts import paths
from accounts.models import Membership, Tenant
from accounts.tenancy import bound, bound_tenant
from accounts.tests.support import TenancyTestCase
from inventory import shopping, shopping_lists
from inventory.models import MovementKind, ShoppingList, ShoppingListItem, StockMovement, UnitChoices
from inventory.shopping_lists import Figures, Finished
from invoices.models import Supplier
from tests.factories import make_invoice, make_invoice_line, make_movement, make_product, make_stock_type, make_supplier
from tests.runner import TEST_TENANT_PK

D = Decimal
EMPLOYEE = "employe.exemple@exemple.fr"
OTHER = "autre.exemple@exemple.fr"
OWNER = "gerant.exemple@exemple.fr"
BEER_FIGURES = Figures(D("24"), "", 7, "BIERE EXEMPLE 33CL X24", 24)
ONE = Figures(D("1"), "", None, "", None)


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


class FindArticleTests(TestCase):
    def setUp(self):
        self.beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        self.coffee = make_stock_type(name="Café exemple", unit=UnitChoices.KILOGRAM)
        self.plain_coffee = make_stock_type(name="Cafe exemple", unit=UnitChoices.KILOGRAM)

    def test_the_exact_name_first(self):
        self.assertEqual(shopping_lists.find_article("Bière exemple"), self.beer)
        # Two articles read alike: the exact name still decides.
        self.assertEqual(shopping_lists.find_article("Café exemple"), self.coffee)
        self.assertEqual(shopping_lists.find_article("Cafe exemple"), self.plain_coffee)

    def test_case_and_accents_ignored_when_one_article_reads_so(self):
        for typed in ("biere exemple", "BIÈRE EXEMPLE", "Biere  exemple"):
            with self.subTest(typed=typed):
                self.assertEqual(shopping_lists.find_article(typed), self.beer)

    def test_two_articles_reading_alike_is_a_free_text(self):
        self.assertIsNone(shopping_lists.find_article("CAFE EXEMPLE"))

    def test_an_unknown_name_is_a_free_text(self):
        for typed in ("Pain exemple", "", "Bière"):
            with self.subTest(typed=typed):
                self.assertIsNone(shopping_lists.find_article(typed))


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
                "note",
                "added_at",
                "added_by",
                "checked_at",
                "checked_by",
            )
        )

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


class ArticleChoicesTests(TestCase):
    def test_the_store_s_articles_first_each_by_search_key(self):
        store = make_supplier(name="Grossiste exemple")
        elsewhere = make_supplier(name="Épicerie exemple")
        olives = make_stock_type(name="olives exemple", unit=UnitChoices.KILOGRAM)
        beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        coffee = make_stock_type(name="Café exemple", unit=UnitChoices.KILOGRAM)
        make_stock_type(name="Écorces exemple", unit=UnitChoices.KILOGRAM)
        make_stock_type(name="Ail exemple", unit=UnitChoices.KILOGRAM)
        for article in (olives, beer):
            bought(store, article)
        bought(elsewhere, coffee)
        # A return only is no purchase there.
        bought(store, coffee, "-1", "-40.00")
        with self.assertNumQueries(1):
            here, others = shopping_lists.article_choices(store)
        self.assertEqual(here, ["Bière exemple", "olives exemple"])
        self.assertEqual(others, ["Ail exemple", "Café exemple", "Écorces exemple"])


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
