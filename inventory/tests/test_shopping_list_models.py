"""« Listes de courses », the two models on their own: `ShoppingList` and
`ShoppingListItem` (inventory 0022).

A list is OPEN while `finished_at` is empty, and a store has at most one open
list - any number of finished ones beside it. An item names an article, once
per list, or is a free text (no article: any number of them, the same label
included). Its quantity is positive, its label never blank, its unit one the
app knows or "" (it counts the product, or the free text), its pack a pack of
several. The database holds all of it whatever writes the row - a create, an
update, a bulk create.

A store deleted takes its lists and their items (CASCADE: its page, the
« Données » clears); an article deleted leaves its items as free texts holding
its name (SET_NULL), even beside a free text of the same label; an article
merged into another (`services.merge_stock_types`) carries its items over by
`shopping_lists.carry_on_merge`'s rule. What the pages do with them is
`test_shopping_lists_page.py`'s, the service module `test_shopping_lists.py`'s.

Invented data throughout: every store, article, name and quantity is made up.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bank.models import CounterpartyAlias
from inventory.models import ShoppingList, ShoppingListItem, StockType, UnitChoices
from inventory.services import merge_stock_types
from invoices.models import ShopItemPrice, Supplier
from tests.factories import make_stock_type, make_supplier

D = Decimal
MIGRATION_MODULE = importlib.import_module("inventory.migrations.0022_shopping_lists")
MIGRATION = MIGRATION_MODULE.Migration

ONE_OPEN_PER_STORE = "shopping_list_one_open_per_store"
QUANTITY_POSITIVE = "shopping_list_item_quantity_positive"
HAS_A_LABEL = "shopping_list_item_has_a_label"
UNIT_KNOWN = "shopping_list_item_unit_known"
PACK_OF_SEVERAL = "shopping_list_item_pack_of_several"
ARTICLE_ONCE = "shopping_list_item_article_once"
#: SQLite names a partial unique index's columns, not the index, when it
#: refuses a row: the supplier's column, which nothing else holds unique.
SECOND_OPEN_LIST = r"UNIQUE constraint failed: inventory_shoppinglist\.supplier_id$"


def make_shopping_list(supplier=None, *, finished=False, **fields) -> ShoppingList:
    """A list at `supplier` (a new invented store unless given), open unless
    `finished`."""
    if finished:
        fields.setdefault("finished_at", timezone.now())
    return ShoppingList.objects.create(supplier=supplier or make_supplier(name="Magasin exemple"), **fields)


def make_shopping_item(
    shopping_list=None, stock_type=None, *, label="", quantity="1", unit="", **fields
) -> ShoppingListItem:
    """An item of `shopping_list` (a new open list unless given): the article's
    name as its label, else « Article exemple »."""
    return ShoppingListItem.objects.create(
        shopping_list=shopping_list or make_shopping_list(),
        stock_type=stock_type,
        label=label or (stock_type.name if stock_type is not None else "Article exemple"),
        quantity=D(quantity),
        unit=unit,
        **fields,
    )


def created_by_the_migration(model) -> list:
    """The constraints the migration's CreateModel of `model` makes."""
    (operation,) = [operation for operation in MIGRATION.operations if operation.name_lower == model._meta.model_name]
    return list(operation.options["constraints"])


class MigrationTests(TestCase):
    def test_it_follows_the_forecast_s_migration(self):
        self.assertIn(("inventory", "0021_shopping_forecast"), MIGRATION.dependencies)
        self.assertIn(("invoices", "0037_auto_gather"), MIGRATION.dependencies)
        self.assertEqual([operation.name for operation in MIGRATION.operations], ["ShoppingList", "ShoppingListItem"])

    def test_the_constraints_are_the_ones_the_migration_made(self):
        for model, names in (
            (ShoppingList, [ONE_OPEN_PER_STORE]),
            (ShoppingListItem, [QUANTITY_POSITIVE, HAS_A_LABEL, UNIT_KNOWN, PACK_OF_SEVERAL, ARTICLE_ONCE]),
        ):
            with self.subTest(model=model.__name__):
                self.assertEqual([constraint.name for constraint in model._meta.constraints], names)
                self.assertEqual(created_by_the_migration(model), list(model._meta.constraints))

    def test_the_models_and_their_migrations_agree(self):
        call_command("makemigrations", "inventory", check=True, dry_run=True, verbosity=0)

    def test_it_says_what_reversing_it_does(self):
        self.assertIn("Reversing drops both tables", MIGRATION_MODULE.__doc__)


class ModelTestCase(TestCase):
    def setUp(self):
        self.wholesaler = make_supplier(name="Grossiste exemple")
        self.grocer = make_supplier(name="Épicerie exemple")
        self.beer = make_stock_type(name="Bière exemple", unit=UnitChoices.UNIT)
        self.syrup = make_stock_type(name="Sirop exemple", unit=UnitChoices.LITRE)


# ---------------------------------------------------------------------------
# ShoppingList
# ---------------------------------------------------------------------------


class OneOpenListTests(ModelTestCase):
    """At most one OPEN list per store; finished lists are kept beside it."""

    def test_a_second_open_list_at_a_store_is_refused(self):
        make_shopping_list(self.wholesaler)
        with self.assertRaisesRegex(IntegrityError, SECOND_OPEN_LIST), transaction.atomic():
            make_shopping_list(self.wholesaler)
        self.assertEqual(ShoppingList.objects.count(), 1)

    def test_any_number_of_finished_lists_beside_one_open(self):
        for _ in range(3):
            make_shopping_list(self.wholesaler, finished=True)
        opened = make_shopping_list(self.wholesaler)
        self.assertEqual(ShoppingList.objects.filter(supplier=self.wholesaler).count(), 4)
        self.assertEqual(list(ShoppingList.objects.filter(finished_at__isnull=True)), [opened])

    def test_two_stores_each_have_their_open_list(self):
        make_shopping_list(self.wholesaler)
        make_shopping_list(self.grocer)
        self.assertEqual(ShoppingList.objects.filter(finished_at__isnull=True).count(), 2)

    def test_an_update_reopening_a_finished_list_is_refused(self):
        finished = make_shopping_list(self.wholesaler, finished=True)
        make_shopping_list(self.wholesaler)
        with self.assertRaisesRegex(IntegrityError, SECOND_OPEN_LIST), transaction.atomic():
            ShoppingList.objects.filter(pk=finished.pk).update(finished_at=None)
        finished.refresh_from_db()
        self.assertIsNotNone(finished.finished_at)

    def test_nor_a_bulk_create(self):
        with self.assertRaisesRegex(IntegrityError, SECOND_OPEN_LIST), transaction.atomic():
            ShoppingList.objects.bulk_create([ShoppingList(supplier=self.grocer), ShoppingList(supplier=self.grocer)])
        self.assertFalse(ShoppingList.objects.exists())

    def test_what_it_holds_and_reads_as(self):
        before = timezone.now()
        opened = make_shopping_list(self.wholesaler, created_by="employe.exemple@exemple.fr")
        opened.refresh_from_db()
        self.assertTrue(before <= opened.created_at <= timezone.now())
        self.assertEqual(
            (opened.created_by, opened.finished_at, opened.finished_by), ("employe.exemple@exemple.fr", None, "")
        )
        self.assertTrue(opened.is_open)
        self.assertEqual(str(opened), "Liste Grossiste exemple")
        self.assertFalse(make_shopping_list(self.grocer, finished=True).is_open)
        # The store reaches its lists as `shopping_lists`.
        self.assertEqual(list(self.wholesaler.shopping_lists.all()), [opened])


# ---------------------------------------------------------------------------
# ShoppingListItem
# ---------------------------------------------------------------------------


class ItemConstraintTests(ModelTestCase):
    def setUp(self):
        super().setUp()
        self.list = make_shopping_list(self.wholesaler)

    def refused(self, constraint, **fields):
        fields.setdefault("label", "Article exemple")
        fields.setdefault("quantity", D("1"))
        with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
            ShoppingListItem.objects.create(shopping_list=self.list, **fields)

    def test_a_quantity_is_positive(self):
        for quantity in ("0", "-1", "-0.001"):
            with self.subTest(quantity=quantity):
                self.refused(QUANTITY_POSITIVE, quantity=D(quantity))
        for quantity in ("0.001", "9999999.999", "24"):
            with self.subTest(quantity=quantity):
                item = make_shopping_item(self.list, label=f"Article {quantity} exemple", quantity=quantity)
                item.refresh_from_db()
                self.assertEqual(item.quantity, D(quantity))

    def test_a_label_is_never_blank(self):
        self.refused(HAS_A_LABEL, label="")
        self.assertFalse(ShoppingListItem.objects.exists())

    def test_a_unit_the_app_knows_or_none(self):
        for unit in ("", *UnitChoices.values):
            with self.subTest(unit=unit):
                item = make_shopping_item(self.list, label=f"Article {unit} exemple", unit=unit)
                item.refresh_from_db()
                self.assertEqual(item.unit, unit)
        for unit in ("X", "l", "kg"):
            with self.subTest(unit=unit):
                self.refused(UNIT_KNOWN, unit=unit)

    def test_a_pack_holds_several(self):
        for pack_size in (0, 1):
            with self.subTest(pack_size=pack_size):
                self.refused(PACK_OF_SEVERAL, pack_size=pack_size)
        for pack_size in (2, 24, None):
            with self.subTest(pack_size=pack_size):
                item = make_shopping_item(self.list, label=f"Article {pack_size} exemple", pack_size=pack_size)
                item.refresh_from_db()
                self.assertEqual(item.pack_size, pack_size)

    def test_an_article_once_per_list(self):
        make_shopping_item(self.list, self.beer)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_shopping_item(self.list, self.beer)
        # The same article on another list is kept.
        make_shopping_item(make_shopping_list(self.grocer), self.beer)
        make_shopping_item(make_shopping_list(self.wholesaler, finished=True), self.beer)
        self.assertEqual(ShoppingListItem.objects.filter(stock_type=self.beer).count(), 3)

    def test_any_number_of_free_texts_the_same_label_included(self):
        for _ in range(3):
            make_shopping_item(self.list, label="Pain exemple", quantity="2")
        make_shopping_item(self.list, label="Sel exemple")
        self.assertEqual(self.list.items.filter(stock_type__isnull=True).count(), 4)

    def test_an_update_cannot_get_round_them(self):
        beer = make_shopping_item(self.list, self.beer, quantity="24")
        syrup = make_shopping_item(self.list, self.syrup, quantity="2", unit=UnitChoices.LITRE)
        for change, constraint in (
            ({"quantity": D("0")}, QUANTITY_POSITIVE),
            ({"label": ""}, HAS_A_LABEL),
            ({"unit": "X"}, UNIT_KNOWN),
            ({"pack_size": 1}, PACK_OF_SEVERAL),
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
                    ShoppingListItem.objects.filter(pk=beer.pk).update(**change)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ShoppingListItem.objects.filter(pk=syrup.pk).update(stock_type=self.beer)
        self.assertEqual(
            set(ShoppingListItem.objects.values_list("stock_type_id", "label", "quantity", "unit", "pack_size")),
            {
                (self.beer.pk, "Bière exemple", D("24"), "", None),
                (self.syrup.pk, "Sirop exemple", D("2"), UnitChoices.LITRE, None),
            },
        )

    def test_nor_a_bulk_create(self):
        for rows, constraint in (
            ([ShoppingListItem(label="Pain exemple", quantity=D("0"))], QUANTITY_POSITIVE),
            ([ShoppingListItem(label="", quantity=D("1"))], HAS_A_LABEL),
            ([ShoppingListItem(label="Pain exemple", quantity=D("1"), unit="X")], UNIT_KNOWN),
            ([ShoppingListItem(label="Pain exemple", quantity=D("1"), pack_size=1)], PACK_OF_SEVERAL),
        ):
            with self.subTest(constraint=constraint):
                for row in rows:
                    row.shopping_list = self.list
                with self.assertRaisesRegex(IntegrityError, constraint), transaction.atomic():
                    ShoppingListItem.objects.bulk_create(rows)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ShoppingListItem.objects.bulk_create(
                [
                    ShoppingListItem(shopping_list=self.list, stock_type=self.beer, label="Bière exemple", quantity=1),
                    ShoppingListItem(shopping_list=self.list, stock_type=self.beer, label="Bière exemple", quantity=2),
                ]
            )
        self.assertFalse(ShoppingListItem.objects.exists())

    def test_what_it_holds_and_reads_as(self):
        before = timezone.now()
        item = make_shopping_item(
            self.list, self.beer, quantity="24", product_name="BIERE EXEMPLE 33CL X24", pack_size=24
        )
        item.refresh_from_db()
        self.assertTrue(before <= item.added_at <= timezone.now())
        self.assertEqual((item.note, item.added_by, item.checked_at, item.checked_by), ("", "", None, ""))
        self.assertEqual((item.name, str(item)), ("Bière exemple", "Bière exemple"))
        # The article's live name: a rename shows.
        self.beer.name = "Bière blonde exemple"
        self.beer.save()
        item.refresh_from_db()
        self.assertEqual((item.name, item.label), ("Bière blonde exemple", "Bière exemple"))
        free = make_shopping_item(self.list, label="Pain exemple")
        self.assertEqual((free.name, str(free)), ("Pain exemple", "Pain exemple"))
        # Reached as `items` from the list and `shopping_list_items` from the article.
        self.assertEqual(set(self.list.items.all()), {item, free})
        self.assertEqual(list(self.beer.shopping_list_items.all()), [item])

    def test_added_at_is_given_not_forced(self):
        # A carry-over copies it: auto_now_add would overwrite it.
        then = timezone.now() - timedelta(days=9)
        item = make_shopping_item(self.list, label="Pain exemple", added_at=then)
        item.refresh_from_db()
        self.assertEqual(item.added_at, then)


# ---------------------------------------------------------------------------
# What goes with a store, a list, an article
# ---------------------------------------------------------------------------


class GoesWithItsStoreTests(ModelTestCase):
    def setUp(self):
        super().setUp()
        self.open = make_shopping_list(self.wholesaler)
        self.finished = make_shopping_list(self.wholesaler, finished=True)
        self.elsewhere = make_shopping_list(self.grocer)
        make_shopping_item(self.open, self.beer)
        make_shopping_item(self.open, label="Pain exemple")
        make_shopping_item(self.finished, self.syrup, unit=UnitChoices.LITRE)
        self.kept = make_shopping_item(self.elsewhere, self.beer)

    def test_deleting_the_store_deletes_its_lists_and_their_items_and_nothing_else(self):
        self.wholesaler.delete()
        self.assertEqual(list(ShoppingList.objects.all()), [self.elsewhere])
        self.assertEqual(list(ShoppingListItem.objects.all()), [self.kept])
        self.assertEqual(StockType.objects.filter(pk__in=(self.beer.pk, self.syrup.pk)).count(), 2)

    def test_the_donnees_clears_delete_by_queryset_too(self):
        # The suppliers' clear deletes a supplier, the associations' clear
        # every article (transfer/sections): the lists go with the one, the
        # items stay as free texts with the other.
        Supplier.objects.filter(pk=self.wholesaler.pk).delete()
        self.assertEqual(list(ShoppingList.objects.all()), [self.elsewhere])
        StockType.objects.all().delete()
        self.kept.refresh_from_db()
        self.assertEqual(
            (self.kept.stock_type_id, self.kept.label, self.kept.name), (None, "Bière exemple", "Bière exemple")
        )

    def test_deleting_a_list_deletes_its_items(self):
        self.open.delete()
        self.assertEqual(ShoppingListItem.objects.filter(shopping_list__supplier=self.wholesaler).count(), 1)
        self.assertEqual(ShoppingListItem.objects.count(), 2)


class ArticleDeletedTests(ModelTestCase):
    """SET_NULL: an article deleted leaves its items as free texts holding its
    name - never a hole in a list, never a constraint tripped."""

    def test_its_items_become_free_texts_holding_its_name(self):
        shopping_list = make_shopping_list(self.wholesaler)
        item = make_shopping_item(shopping_list, self.beer, quantity="24", note="Note exemple")
        finished_item = make_shopping_item(make_shopping_list(self.grocer, finished=True), self.beer)
        self.beer.delete()
        for kept in (item, finished_item):
            kept.refresh_from_db()
            self.assertEqual((kept.stock_type_id, kept.name), (None, "Bière exemple"))
        self.assertEqual((item.quantity, item.note), (D("24"), "Note exemple"))

    def test_even_beside_a_free_text_of_the_same_label(self):
        shopping_list = make_shopping_list(self.wholesaler)
        make_shopping_item(shopping_list, label="Bière exemple")
        make_shopping_item(shopping_list, self.beer)
        self.beer.delete()
        self.assertEqual(
            list(shopping_list.items.values_list("stock_type_id", "label")),
            [(None, "Bière exemple"), (None, "Bière exemple")],
        )

    def test_clearing_the_empty_articles_takes_one_only_a_list_names(self):
        # « Supprimer les articles vides »: no product, no movement - a list
        # does not keep it, and its item stays as a free text.
        item = make_shopping_item(make_shopping_list(self.wholesaler), self.syrup, unit=UnitChoices.LITRE)
        response = self.client.post(reverse("inventory:clear_empty_stock_types"))
        self.assertRedirects(response, reverse("inventory:stock_list"))
        self.assertFalse(StockType.objects.filter(pk=self.syrup.pk).exists())
        self.assertIn("supprimé", " ".join(str(message) for message in get_messages(response.wsgi_request)))
        item.refresh_from_db()
        self.assertEqual((item.stock_type_id, item.name, item.unit), (None, "Sirop exemple", UnitChoices.LITRE))


class MergedArticleTests(ModelTestCase):
    """`merge_stock_types(source, target)` carries the source's items, list by
    list, finished lists included (`shopping_lists.carry_on_merge`): no target
    item there - the item now names the target; a target item counting the
    same thing - one item adding both; a target item counting something
    else - the source's becomes a free text, both lines stay."""

    def setUp(self):
        super().setUp()
        self.source = make_stock_type(name="Bière blonde exemple", unit=UnitChoices.UNIT)
        self.target = make_stock_type(name="Bière exemple bis", unit=UnitChoices.UNIT)
        self.list = make_shopping_list(self.wholesaler)
        self.ticked_at = timezone.now() - timedelta(hours=2)

    def rows(self, shopping_list=None) -> list:
        return list(
            (shopping_list or self.list)
            .items.order_by("added_at", "pk")
            .values_list(
                "stock_type_id", "label", "quantity", "unit", "product_name", "pack_size", "note", "checked_by"
            )
        )

    def test_no_twin_the_item_names_the_target(self):
        item = make_shopping_item(
            self.list,
            self.source,
            quantity="24",
            product_name="BIERE EXEMPLE 33CL X24",
            pack_size=24,
            note="Note exemple",
            added_by="employe.exemple@exemple.fr",
            checked_at=self.ticked_at,
            checked_by="employe.exemple@exemple.fr",
        )
        merge_stock_types(self.source, self.target)
        item.refresh_from_db()
        self.assertEqual(
            (item.stock_type, item.label, item.name), (self.target, "Bière exemple bis", "Bière exemple bis")
        )
        self.assertEqual(
            (item.quantity, item.unit, item.product_name, item.pack_size, item.note, item.added_by),
            (D("24"), "", "BIERE EXEMPLE 33CL X24", 24, "Note exemple", "employe.exemple@exemple.fr"),
        )
        self.assertEqual((item.checked_at, item.checked_by), (self.ticked_at, "employe.exemple@exemple.fr"))
        self.assertFalse(StockType.objects.filter(pk=self.source.pk).exists())

    def test_a_twin_counting_the_same_thing_makes_one_item(self):
        make_shopping_item(self.list, self.source, quantity="12", unit=UnitChoices.UNIT, note="Fraîche exemple")
        twin = make_shopping_item(
            self.list,
            self.target,
            quantity="6",
            unit=UnitChoices.UNIT,
            note="Promo exemple",
            checked_at=self.ticked_at,
            checked_by="employe.exemple@exemple.fr",
        )
        merge_stock_types(self.source, self.target)
        (item,) = self.list.items.all()
        self.assertEqual(item.pk, twin.pk)
        self.assertEqual(
            (item.stock_type, item.label, item.quantity, item.note),
            (self.target, "Bière exemple bis", D("18"), "Promo exemple · Fraîche exemple"),
        )
        # Ticked only if both were: one still has to be bought.
        self.assertEqual((item.checked_at, item.checked_by), (None, ""))

    def test_both_ticked_stay_ticked_with_the_target_s_tick(self):
        make_shopping_item(
            self.list, self.source, quantity="1", checked_at=timezone.now(), checked_by="autre.exemple@exemple.fr"
        )
        make_shopping_item(
            self.list, self.target, quantity="2", checked_at=self.ticked_at, checked_by="employe.exemple@exemple.fr"
        )
        merge_stock_types(self.source, self.target)
        (item,) = self.list.items.all()
        self.assertEqual(
            (item.quantity, item.checked_at, item.checked_by), (D("3"), self.ticked_at, "employe.exemple@exemple.fr")
        )

    def test_the_notes_and_the_pack_of_twins(self):
        for source_note, target_note, joined in (
            ("", "", ""),
            ("Note exemple", "", "Note exemple"),
            ("", "Note exemple", "Note exemple"),
            ("Note exemple", "Note exemple", "Note exemple"),
            ("Une exemple", "Deux exemple", "Deux exemple · Une exemple"),
        ):
            for source_pack, target_pack, kept in ((24, 24, 24), (12, 24, 24), (None, 24, 24), (12, None, None)):
                with self.subTest(notes=(source_note, target_note), packs=(source_pack, target_pack)):
                    source = make_stock_type(name="Source exemple", unit=UnitChoices.UNIT)
                    shopping_list = make_shopping_list(make_supplier(name="Magasin exemple"))
                    common = {"quantity": "24", "product_name": "BIERE EXEMPLE 33CL X24"}
                    make_shopping_item(shopping_list, source, note=source_note, pack_size=source_pack, **common)
                    make_shopping_item(shopping_list, self.target, note=target_note, pack_size=target_pack, **common)
                    merge_stock_types(source, self.target)
                    (item,) = shopping_list.items.all()
                    self.assertEqual((item.quantity, item.note, item.pack_size), (D("48"), joined, kept))

    def test_a_joined_note_keeps_within_the_field(self):
        make_shopping_item(self.list, self.source, note="a" * 150)
        make_shopping_item(self.list, self.target, note="b" * 150)
        merge_stock_types(self.source, self.target)
        (item,) = self.list.items.all()
        self.assertEqual(len(item.note), ShoppingListItem._meta.get_field("note").max_length)
        self.assertTrue(item.note.startswith("b" * 150 + " · a"))

    def test_a_twin_counting_something_else_keeps_both_lines(self):
        for source_figures, target_figures in (
            # The product against the article's unit.
            ({"quantity": "24", "product_name": "BIERE EXEMPLE 33CL X24"}, {"quantity": "6", "unit": UnitChoices.UNIT}),
            # Two products.
            (
                {"quantity": "24", "product_name": "BIERE EXEMPLE 33CL X24"},
                {"quantity": "12", "product_name": "BIERE EXEMPLE 25CL X12"},
            ),
        ):
            with self.subTest(source=source_figures, target=target_figures):
                source = make_stock_type(name="Bière ambrée exemple", unit=UnitChoices.UNIT)
                shopping_list = make_shopping_list(make_supplier(name="Magasin exemple"))
                mine = make_shopping_item(shopping_list, source, **source_figures)
                theirs = make_shopping_item(shopping_list, self.target, **target_figures)
                before = self.rows(shopping_list)
                merge_stock_types(source, self.target)
                mine.refresh_from_db()
                # A free text holding the source's name, its figures kept.
                self.assertEqual((mine.stock_type_id, mine.name), (None, "Bière ambrée exemple"))
                self.assertEqual(self.rows(shopping_list), [(None, *before[0][1:]), before[1]])
                theirs.refresh_from_db()
                self.assertEqual(theirs.stock_type, self.target)

    def test_a_sum_wider_than_the_column_keeps_both_lines(self):
        # 6 000 000 + 5 000 000 does not fit a quantity (10 digits, 3
        # places): stored, the row could never be read again - the list
        # page, the tick page, the forecast and « Courses terminées » all
        # 500, and only raw SQL took it out. The two lines stay instead.
        mine = make_shopping_item(
            self.list, self.source, quantity="6000000", product_name="BIERE EXEMPLE 33CL X24", note="Note exemple"
        )
        theirs = make_shopping_item(self.list, self.target, quantity="5000000", product_name="BIERE EXEMPLE 33CL X24")
        merge_stock_types(self.source, self.target)
        mine = ShoppingListItem.objects.get(pk=mine.pk)
        theirs = ShoppingListItem.objects.get(pk=theirs.pk)
        # A free text holding the source's name, its figures kept.
        self.assertEqual(
            (mine.stock_type_id, mine.label, mine.quantity, mine.product_name, mine.note),
            (None, "Bière blonde exemple", D("6000000"), "BIERE EXEMPLE 33CL X24", "Note exemple"),
        )
        self.assertEqual(
            (theirs.stock_type_id, theirs.label, theirs.quantity), (self.target.pk, "Bière exemple bis", D("5000000"))
        )
        # Every row of the list reads back.
        self.assertEqual(len(list(self.list.items.all())), 2)

    def test_a_sum_at_the_column_s_widest_makes_one_item(self):
        make_shopping_item(self.list, self.source, quantity="4999999.999")
        make_shopping_item(self.list, self.target, quantity="5000000")
        merge_stock_types(self.source, self.target)
        (item,) = self.list.items.all()
        self.assertEqual((item.stock_type, item.quantity), (self.target, D("9999999.999")))

    def test_the_free_text_holds_the_name_the_list_showed(self):
        # The list showed the article's live name, a rename since it was
        # added included: the free text keeps that one, not the older label.
        make_shopping_item(self.list, self.target, quantity="6", unit=UnitChoices.UNIT)
        mine = make_shopping_item(self.list, self.source, quantity="24", product_name="BIERE EXEMPLE 33CL X24")
        self.source.name = "Bière blonde renommée exemple"
        self.source.save()
        merge_stock_types(self.source, self.target)
        mine.refresh_from_db()
        self.assertEqual((mine.stock_type_id, mine.label), (None, "Bière blonde renommée exemple"))

    def test_the_same_on_a_finished_list(self):
        finished = make_shopping_list(self.grocer, finished=True)
        make_shopping_item(finished, self.source, quantity="2", unit=UnitChoices.UNIT)
        make_shopping_item(finished, self.target, quantity="3", unit=UnitChoices.UNIT)
        alone = make_shopping_list(self.wholesaler, finished=True)
        make_shopping_item(alone, self.source, quantity="5", unit=UnitChoices.UNIT)
        merge_stock_types(self.source, self.target)
        self.assertEqual(
            self.rows(finished), [(self.target.pk, "Bière exemple bis", D("3") + D("2"), "UNIT", "", None, "", "")]
        )
        self.assertEqual(self.rows(alone), [(self.target.pk, "Bière exemple bis", D("5"), "UNIT", "", None, "", "")])

    def test_nothing_else_changes(self):
        other_list = make_shopping_list(self.grocer)
        beer = make_shopping_item(other_list, self.beer, quantity="24")
        free = make_shopping_item(other_list, label="Bière blonde exemple", quantity="2")
        target_alone = make_shopping_item(self.list, self.target, quantity="7")
        before = {item.pk: self.rows(item.shopping_list) for item in (beer, free, target_alone)}
        merge_stock_types(self.source, self.target)
        for item in (beer, free, target_alone):
            self.assertEqual(self.rows(item.shopping_list), before[item.pk])
        self.assertFalse(StockType.objects.filter(pk=self.source.pk).exists())


class SupplierDeletePageTests(ModelTestCase):
    """A supplier's delete page names the shopping lists that go with it; the
    CASCADE does the deleting."""

    def setUp(self):
        super().setUp()
        self.store = make_supplier(code="VIDE_X", name="Magasin vide exemple", parser_key="")
        self.url = reverse("invoices:supplier_delete", args=[self.store.pk])

    def test_its_lists_are_named_and_go_with_it(self):
        make_shopping_item(make_shopping_list(self.store), self.beer)
        make_shopping_item(make_shopping_list(self.store, finished=True), label="Pain exemple")
        kept = make_shopping_item(make_shopping_list(self.grocer), self.beer)
        page = self.client.get(self.url)
        self.assertContains(page, "Partent avec lui")
        self.assertContains(page, "2 listes de courses")
        self.assertEqual(page.context["shopping_lists"], 2)
        response = self.client.post(self.url, {"confirme": "1"})
        self.assertRedirects(response, reverse("invoices:supplier_list"), fetch_redirect_response=False)
        self.assertFalse(Supplier.objects.filter(pk=self.store.pk).exists())
        self.assertFalse(ShoppingList.objects.filter(supplier_id=self.store.pk).exists())
        self.assertEqual(list(ShoppingListItem.objects.all()), [kept])

    def test_one_list_and_its_neighbours_read_as_one_sentence(self):
        make_shopping_list(self.store)
        ShopItemPrice.objects.create(supplier=self.store, unit_price_ttc=D("31.50"), label="Pain exemple")
        CounterpartyAlias.objects.create(supplier=self.store, name="MAGASIN EX")
        page = self.client.get(self.url)
        text = " ".join(page.content.decode().split())
        self.assertIn(
            "Partent avec lui : 1 prix connu, 1 liste de courses, 1 libellé bancaire et son historique.", text
        )

    def test_a_list_alone(self):
        make_shopping_list(self.store, finished=True)
        text = " ".join(self.client.get(self.url).content.decode().split())
        self.assertIn("Partent avec lui : 1 liste de courses et son historique.", text)

    def test_no_list_is_not_mentioned(self):
        page = self.client.get(self.url)
        self.assertEqual(page.context["shopping_lists"], 0)
        self.assertNotContains(page, "liste de courses")
        self.assertContains(page, "Son historique part avec lui.")
