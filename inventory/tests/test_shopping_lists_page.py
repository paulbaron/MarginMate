"""« Listes de courses » (/courses/listes/, /courses/liste/…), as the owner
and his employees read them and post their forms: the lists' page, one
store's list - edited, ticked in the store, read once finished - and
« Prévoir les courses »' « Ajouter » and « Tout ajouter » (inventory/views.py,
over inventory/shopping_lists.py).

Every form is read off the page that draws it and posted as drawn. Each POST
answers with one redirect - an htmx tick with the block it swaps, and out of
band the list « Courses terminées » posts -, its message said where it
lands; what cannot be read is said, and nothing is written. Another phone
acting between a view's read and its write is staged by patching
views._item_of (`read_then`): its guards fail these tests when removed.

The add form and the card count in the stock take's units (SPEC_UNITS): an
article in litres in its bottles when the size of one is known, else in
litres; every menu entry is posted with every unit its page offers, and
stored as its label says - and every name the server also accepts (the
aliases' island) as its menu name is; every option a card draws, posted,
stores what its label says.

Invented data throughout (tests.test_views_smoke.make_shopping_history,
make_shopping_lists and make_shopping_bottles): every store, article,
product, login and figure.
"""

from __future__ import annotations

import json
import math
import re
from datetime import timedelta
from decimal import Decimal
from html import unescape
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.tests.test_access import row_counts
from common import plain_number
from inventory import views
from inventory.entries import same_name
from inventory.models import Product, ShoppingList, ShoppingListItem, StockMovement, StockType, UnitChoices
from inventory.shopping_lists import Figures, Finished, finish, list_entries, quantity_words, set_ticked
from inventory.tests.test_gap_filler_page import body_rows, busy_button_of, confirm_of, table_of
from inventory.tests.test_shopping_page import (
    fold_of,
    forms_to,
    hidden_of,
    opened,
    readable,
    said_above_the_list,
    said_at_the_top,
    said_in,
    warnings_of,
)
from margins.tests.test_page import cells_of, row_of
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_type,
    make_supplier,
)
from tests.runner import TEST_EMAIL, employee_of_the_test_tenant
from tests.test_views_smoke import (
    SHOPPING_BEER_PRODUCT,
    SHOPPING_CHEESE_PRODUCT,
    SHOPPING_CUPS_PRODUCT,
    SHOPPING_GIN_PRODUCT,
    SHOPPING_LIST_FINISHER,
    SHOPPING_VODKA_PRODUCT,
    assertNoUnrenderedTemplateSyntax,
    make_shopping_bottles,
    make_shopping_history,
    make_shopping_lists,
)

INDEX = "inventory:shopping_lists"
LIST_PAGE = "inventory:shopping_list_page"
ADD = "inventory:shopping_list_add"
ADD_ALL = "inventory:shopping_list_add_all"
EDIT = "inventory:shopping_list_item_edit"
DELETE = "inventory:shopping_list_item_delete"
TICK = "inventory:shopping_list_item_tick"
FINISH = "inventory:shopping_list_finish"
FORECAST = "inventory:shopping_list"
POST_ROUTES = (ADD, ADD_ALL, EDIT, DELETE, TICK, FINISH)

STORE_TO_CHOOSE = "Enseigne introuvable : choisissez-en une."
STORE_NOT_FOUND_ADD = "Enseigne introuvable : rien n'a été ajouté."
STORE_NOT_FOUND_CHANGE = "Enseigne introuvable : rien n'a changé."
LIST_NOT_FOUND = "Liste introuvable."
ARTICLE_NOT_FOUND = "Article introuvable : rien n'a été ajouté."
PRODUCT_NOT_FOUND = "Produit introuvable : rien n'a été ajouté."
NAME_MISSING = "Article : tapez un nom."
NAME_TOO_LONG = "Article : 255 caractères au plus."
QUANTITY_REFUSED = "Quantité : un nombre plus grand que 0, 3 décimales au plus."
NOTE_TOO_LONG = "Note : 200 caractères au plus."
NOTHING_TO_ADD = "Tout est déjà dans la liste."
NOT_IN_LIST = "Cet article n'est plus dans la liste."
LIST_FINISHED = "Cette liste est terminée : rien n'a changé."
TICK_UNREADABLE = "Rien n'a changé : rechargez la page."
ALREADY_FINISHED = "Ces courses sont déjà terminées."
ALREADY_FINISHED_NEXT = "Ces courses étaient déjà terminées : voici la liste en cours."
UNIT_REFUSED = "Unité : choisissez-en une de la liste proposée."
ADD_FORM_HELP = (
    "Un nom inconnu s'ajoute tel quel. Quantité : dans l'unité choisie, jamais en colis ; vide : celle d'habitude ici."
)
SYRUP_PRODUCT = "SIROP EXEMPLE PRODUIT"
MARKUP = '<i>exemple</i> "test"'
MARKUP_SHOWN = "&lt;i&gt;exemple&lt;/i&gt; &quot;test&quot;"
HTMX = {"HTTP_HX_REQUEST": "true"}
#: The add form's menu (a datalist) and what its unit select reads (a JSON island).
ENTRIES_ID = "shopping-entries"
ENTRY_DATA_ID = "shopping-entry-data"
#: The names the server also accepts, for the select to follow them too.
ENTRY_ALIASES_ID = "shopping-entry-aliases"
#: The longest name the menu can offer (entries.ENTRY_MAX): a product's 255
#: characters, « — » and a store's 255.
ENTRY_MAX = 513
#: The wholesaler's name as a product entry ends with it.
AT_THE_WHOLESALER = " — Grossiste exemple"


def element_at(html: str, start: int) -> str:
    """`html` from `start` to the closing tag of the element opening there."""
    tag = re.match(r"<(\w+)", html[start:]).group(1)
    depth = 0
    for found in re.finditer(rf"<{tag}\b|</{tag}>", html[start:]):
        depth += -1 if found.group(0).startswith("</") else 1
        if depth == 0:
            return html[start : start + found.end()]
    return html[start:]


def run_block(html: str) -> str:
    """The block a tick swaps (#courses), whole, or ""."""
    start = html.find('<div class="shopping-run" id="courses">')
    return element_at(html, start) if start >= 0 else ""


def edit_card(html: str) -> str:
    """The card changing one item (#modifier), whole, or ""."""
    start = html.find('<section class="card shopping-item-edit" id="modifier">')
    return element_at(html, start) if start >= 0 else ""


def notices_of(block: str) -> list[str]:
    """What the tick block says above the items."""
    return [readable(found) for found in re.findall(r'<p class="message message-warning">(.*?)</p>', block, re.DOTALL)]


def ticks_of(block: str) -> list[dict]:
    """Each item of the tick block as drawn, in its order."""
    drawn = []
    for classes, inside in re.findall(r'<li class="(shopping-tick[^"]*)">(.*?)</li>', block, flags=re.DOTALL):
        text = re.search(r'<span class="shopping-tick-text">(.*)</span>\s*</button>', inside, flags=re.DOTALL)
        spans = re.findall(r'<span class="([^"]+)">([^<]*)</span>', text.group(1) if text else "")
        drawn.append(
            {
                "ticked": classes == "shopping-tick is-ticked",
                "name": unescape(dict(spans).get("shopping-tick-name", "")),
                "quantity": unescape(dict(spans).get("shopping-tick-quantity", "")),
                "under": [unescape(words) for kind, words in spans if kind == "muted small"],
                "pressed": re.search(r'aria-pressed="([^"]*)"', inside).group(1),
                "form": re.search(r"<form\b.*?</form>", inside, flags=re.DOTALL).group(0),
            }
        )
    return drawn


def links_of(fragment: str) -> list[tuple[str, str]]:
    """(where, words) of each link, its address read back."""
    return [
        (unescape(href), readable(words))
        for href, words in re.findall(r'<a [^>]*href="([^"]*)"[^>]*>(.*?)</a>', fragment, flags=re.DOTALL)
    ]


def value_of_field(form: str, name: str) -> str:
    found = re.search(r'<input type="text" name="' + name + r'" value="([^"]*)"', form)
    return unescape(found.group(1)) if found else ""


def headers_of(table: str) -> list[str]:
    head = re.search(r"<thead>(.*?)</thead>", table, flags=re.DOTALL)
    return [readable(cell) for cell in re.findall(r"<th[^>]*>(.*?)</th>", head.group(1) if head else "", re.DOTALL)]


def options_of(form: str, name: str) -> list[tuple[str, str, bool]]:
    """The options of the form's select `name` as drawn: (value, label,
    selected); none when the form has no such select."""
    found = re.search(r'<select name="' + name + r'"[^>]*>(.*?)</select>', form, flags=re.DOTALL)
    if found is None:
        return []
    return [
        (unescape(value), unescape(label), bool(selected))
        for value, selected, label in re.findall(
            r'<option value="([^"]*)"( selected)?>([^<]*)</option>', found.group(1), flags=re.DOTALL
        )
    ]


def menu_of(html: str) -> list[str]:
    """The add form's menu (its datalist), each name as read, in its order."""
    found = re.search(rf'<datalist id="{ENTRIES_ID}">(.*?)</datalist>', html, flags=re.DOTALL)
    return [unescape(name) for name in re.findall(r'<option value="([^"]*)">', found.group(1))] if found else []


def entry_data_of(html: str) -> dict:
    """The island the add form's unit select reads, parsed as its script
    parses it (JSON.parse of the element's text); {} when there is none."""
    found = re.search(rf'<script id="{ENTRY_DATA_ID}" type="application/json">(.*?)</script>', html, re.DOTALL)
    return json.loads(found.group(1)) if found else {}


def entry_aliases_of(html: str) -> dict:
    """The names the server accepts beside the island's own, each to the
    island name it resolves to ({"exact": {...}, "folded": {...}}), parsed
    as the script parses it; {} when there is none."""
    found = re.search(rf'<script id="{ENTRY_ALIASES_ID}" type="application/json">(.*?)</script>', html, re.DOTALL)
    return json.loads(found.group(1)) if found else {}


def alias_of(aliases: dict, typed: str) -> str | None:
    """The island name entry_units.js reaches for `typed` through the
    aliases: exactly, else folded (`same_name`'s fold)."""
    return aliases["exact"].get(typed.strip()) or aliases["folded"].get(same_name(typed))


def sizes_of(item: ShoppingListItem) -> tuple:
    """What an item counts, as stored: its quantity, unit, product, pack,
    and the size of one item with its unit."""
    item.refresh_from_db()
    size = plain_number(item.item_size) if item.item_size is not None else None
    return (plain_number(item.quantity), item.unit, item.product_name, item.pack_size, size, item.size_unit)


def words_of(item: ShoppingListItem) -> str:
    """What the pages say an item counts (shopping_lists.quantity_words)."""
    item.refresh_from_db()
    return quantity_words(item.quantity, item.unit, item.item_size, item.size_unit, product_name=item.product_name)


def figures_of(item: ShoppingListItem) -> tuple:
    """An item as a test compares it: its name, quantity, unit, product,
    pack, note and whether it is ticked."""
    item.refresh_from_db()
    return (
        item.name,
        plain_number(item.quantity),
        item.unit,
        item.product_name,
        item.pack_size,
        item.note,
        item.checked_at is not None,
    )


def open_items(store) -> list[tuple]:
    """The store's open list, item by item in its order (figures_of)."""
    return [
        figures_of(item)
        for item in ShoppingListItem.objects.filter(
            shopping_list__supplier=store, shopping_list__finished_at__isnull=True
        ).order_by("added_at", "pk")
    ]


def everything_listed() -> list[tuple]:
    """Every list and item as they stand, to say nothing was written."""
    return [
        *ShoppingList.objects.order_by("pk").values_list("pk", "supplier", "finished_at", "finished_by"),
        *ShoppingListItem.objects.order_by("pk").values_list(
            "pk", "shopping_list", "stock_type", "label", "quantity", "unit", "note", "checked_at", "checked_by"
        ),
    ]


def finish_list_of(block: str) -> str:
    """The list an htmx tick's answer names for « Courses terminées »: its
    out-of-band input, which takes the finish form's place; "" when none."""
    found = re.search(
        r'<input type="hidden" name="liste" value="([^"]*)" id="shopping-finish-list" hx-swap-oob="true">', block
    )
    return found.group(1) if found else ""


#: views._item_of as it is: a race is staged by reading the item with it,
#: then acting before the view writes (`read_then`).
REAL_ITEM_OF = views._item_of


def read_then(action):
    """A stand-in for views._item_of: the item read as the view reads it,
    then `action(item)` - another phone finishing its list or removing it -
    before the view's own write."""

    def item_of(asked, store):
        item = REAL_ITEM_OF(asked, store)
        if item is not None:
            action(item)
        return item

    return item_of


def finish_its_list(item) -> None:
    """Another phone presses « Courses terminées », « Garder » ticked."""
    finish(
        ShoppingList.objects.get(pk=item.shopping_list_id),
        keep=True,
        by="autre-exemple@example.invalid",
        now=timezone.now(),
    )


def remove_it(item) -> None:
    """Another phone presses « Retirer »."""
    ShoppingListItem.objects.filter(pk=item.pk).delete()


def retired_ai_supplier(article):
    """The removed AI reading's supplier as invoices/0038 leaves it where
    something names it - its code OTHER, its reader key emptied -, `article`
    bought there (a document filed under it is why it was kept)."""
    retired = make_supplier(code="OTHER", name="Autre (analyse IA)")
    line = make_invoice_line(
        invoice=make_invoice(supplier=retired),
        product=make_product(supplier=retired, stock_type=article),
        total_ht="40.00",
    )
    make_movement(stock_type=article, quantity="1", unit_cost_ht="40", invoice_line=line)
    return retired


class SentencesTests(SimpleTestCase):
    """What the pages and the messages say, word for word."""

    def test_each_sentence(self):
        for name, said in (
            ("STORE_TO_CHOOSE", STORE_TO_CHOOSE),
            ("STORE_NOT_FOUND_ADD", STORE_NOT_FOUND_ADD),
            ("STORE_NOT_FOUND_CHANGE", STORE_NOT_FOUND_CHANGE),
            ("LIST_NOT_FOUND", LIST_NOT_FOUND),
            ("ARTICLE_NOT_FOUND", ARTICLE_NOT_FOUND),
            ("PRODUCT_NOT_FOUND", PRODUCT_NOT_FOUND),
            ("NAME_MISSING", NAME_MISSING),
            ("NAME_TOO_LONG", NAME_TOO_LONG),
            ("QUANTITY_REFUSED", QUANTITY_REFUSED),
            ("NOTE_TOO_LONG", NOTE_TOO_LONG),
            ("NOTHING_TO_ADD", NOTHING_TO_ADD),
            ("NOT_IN_LIST", NOT_IN_LIST),
            ("LIST_FINISHED", LIST_FINISHED),
            ("TICK_UNREADABLE", TICK_UNREADABLE),
            ("ALREADY_FINISHED", ALREADY_FINISHED),
            ("ALREADY_FINISHED_NEXT", ALREADY_FINISHED_NEXT),
            ("UNIT_REFUSED", UNIT_REFUSED),
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(views, name), said)
        # The unit posted: the HTTP interface, French.
        self.assertEqual(views.UNIT_PARAM, "unite")

    def test_the_sentences_carrying_figures(self):
        self.assertEqual(
            views.ADDED.format(name="Bière exemple", quantity="24"), "« Bière exemple » ajouté à la liste (24)."
        )
        self.assertEqual(
            views.RELISTED.format(name="Bière exemple", quantity="48 · 2 colis de 24"),
            "« Bière exemple » remis dans la liste (48 · 2 colis de 24).",
        )
        self.assertEqual(
            views.ALREADY_LISTED.format(name="Sirop exemple", quantity="2 L"),
            "« Sirop exemple » est déjà dans la liste (2 L).",
        )
        self.assertEqual(views.CHANGED.format(name="Pain exemple", quantity="3"), "Modifié : « Pain exemple » (3).")
        self.assertEqual(views.REMOVED.format(name="Pain exemple"), "« Pain exemple » retiré de la liste.")

    def test_how_many_were_added(self):
        for added, already, said in (
            (1, 0, "1 article ajouté à la liste."),
            (7, 0, "7 articles ajoutés à la liste."),
            (7, 3, "7 articles ajoutés à la liste, 3 y étaient déjà."),
            (2, 1, "2 articles ajoutés à la liste, 1 y était déjà."),
            (0, 4, NOTHING_TO_ADD),
            (0, 0, NOTHING_TO_ADD),
        ):
            with self.subTest(added=added, already=already):
                self.assertEqual(views._added_all_words(added, already), said)

    def test_how_many_were_too_wide(self):
        """A line whose figure the list cannot hold is left out, and counted:
        never « Tout est déjà dans la liste »."""
        for added, already, too_wide, said in (
            (1, 0, 1, "1 article ajouté à la liste. 1 non ajouté : quantité trop grande."),
            (2, 1, 2, "2 articles ajoutés à la liste, 1 y était déjà. 2 non ajoutés : quantité trop grande."),
            (0, 0, 1, "Aucun article ajouté à la liste. 1 non ajouté : quantité trop grande."),
            (0, 3, 1, "Aucun article ajouté à la liste, 3 y étaient déjà. 1 non ajouté : quantité trop grande."),
            (0, 3, 0, NOTHING_TO_ADD),
        ):
            with self.subTest(added=added, already=already, too_wide=too_wide):
                self.assertEqual(views._added_all_words(added, already, too_wide), said)

    def test_what_a_message_says_an_item_counts(self):
        """The quantity, and what it counts when it counts the store's
        product sold in packs: whole packs said, else « à l'unité » - a
        number never read as packs."""
        for counted, said in (
            ((Decimal("24"), "", SHOPPING_BEER_PRODUCT, 24), "24 · 1 colis de 24"),
            ((Decimal("48"), "", SHOPPING_BEER_PRODUCT, 24), "48 · 2 colis de 24"),
            ((Decimal("2"), "", SHOPPING_BEER_PRODUCT, 24), "2 · à l'unité · colis de 24"),
            ((Decimal("3"), "", SYRUP_PRODUCT, None), "3"),
            ((Decimal("1.5"), UnitChoices.LITRE, "", None), "1.5 L"),
            ((Decimal("2"), "", "", None), "2"),
        ):
            with self.subTest(counted=counted):
                self.assertEqual(views._counted_words(*counted), said)

    def test_what_a_message_says_items_of_a_size_count(self):
        """Bottles of a size say so, their packs after them; a UNIT size of 1
        is the piece the article counts: the beer's « 24 » stays."""
        litre, unit = UnitChoices.LITRE, UnitChoices.UNIT
        for counted, said in (
            (
                (Decimal("6"), "", SHOPPING_GIN_PRODUCT, 6, Decimal("0.7"), litre),
                "6 bouteilles de 70 cl · 1 colis de 6",
            ),
            (
                (Decimal("3"), "", SHOPPING_GIN_PRODUCT, 6, Decimal("0.7000"), litre),
                "3 bouteilles de 70 cl · à l'unité · colis de 6",
            ),
            ((Decimal("3"), "", "", None, Decimal("0.7"), litre), "3 bouteilles de 70 cl"),
            ((Decimal("1"), "", "", None, Decimal("0.7"), litre), "1 bouteille de 70 cl"),
            ((Decimal("1"), "", "", None, Decimal("30"), litre), "1 fût de 30 L"),
            # A carton of six bottles is a pack, never a keg.
            ((Decimal("1"), "", "ROSE EXEMPLE CARTON 6X75CL", None, Decimal("4.5"), litre), "1 pack de 4.5 L"),
            ((Decimal("24"), "", SHOPPING_BEER_PRODUCT, 24, Decimal("1"), unit), "24 · 1 colis de 24"),
            ((Decimal("2"), "", SHOPPING_CUPS_PRODUCT, None, Decimal("50"), unit), "2 paquets de 50 u."),
            ((Decimal("4.2"), litre, SHOPPING_GIN_PRODUCT, None, None, ""), "4.2 L"),
        ):
            with self.subTest(counted=counted):
                self.assertEqual(views._counted_words(*counted), said)

    def test_the_shopping_done(self):
        for done, said in (
            (Finished(2, 3, 0), "Courses terminées chez Grossiste exemple : 2 / 3 pris."),
            (
                Finished(2, 3, 1),
                "Courses terminées chez Grossiste exemple : 2 / 3 pris. 1 gardé pour la prochaine liste.",
            ),
            (
                Finished(0, 5, 5),
                "Courses terminées chez Grossiste exemple : 0 / 5 pris. 5 gardés pour la prochaine liste.",
            ),
        ):
            with self.subTest(done=done):
                self.assertEqual(views._finished_words("Grossiste exemple", done), said)


class ListPageTestCase(TestCase):
    """The pages and their forms as the tests below drive them - no test of
    its own. `made` is make_shopping_history()'s stores and articles,
    `lists` make_shopping_lists()' (empty for a class with `WITH_LISTS`
    off), `bottles` make_shopping_bottles()' (with `WITH_BOTTLES` on)."""

    WITH_LISTS = True
    WITH_BOTTLES = False

    @classmethod
    def setUpTestData(cls):
        cls.made = make_shopping_history()
        cls.lists = make_shopping_lists(cls.made) if cls.WITH_LISTS else SimpleNamespace()
        cls.bottles = make_shopping_bottles(cls.made) if cls.WITH_BOTTLES else SimpleNamespace()
        cls.wholesaler, cls.grocer, cls.market = cls.made.wholesaler, cls.made.grocer, cls.made.market

    def html(self, name=LIST_PAGE, client=None, **params) -> str:
        """The page drawn - a 200, its template rendered whole."""
        response = (client or self.client).get(reverse(name), params)
        self.assertEqual(response.status_code, 200, params)
        assertNoUnrenderedTemplateSyntax(self, response, f"{name} {params}")
        return response.content.decode()

    def redirected(self, name=LIST_PAGE, **params) -> str:
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, 302, params)
        return response["Location"]

    def post(self, name, data, client=None):
        """POST and follow: one redirect, and the page it lands on, whole."""
        response = (client or self.client).post(reverse(name), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([status for _url, status in response.redirect_chain], [302], data)
        assertNoUnrenderedTemplateSyntax(self, response, f"after {name} {data}")
        return response

    def landing(self, response) -> str:
        return response.redirect_chain[0][0]

    def page_of(self, store, anchor="", **query) -> str:
        """A store's list page, its parameters in the view's order."""
        params = "".join(f"&{key}={value}" for key, value in query.items())
        return f"{reverse(LIST_PAGE)}?fournisseur={store.pk}{params}" + (f"#{anchor}" if anchor else "")

    def forecast_of(self, store, anchor="", **query) -> str:
        params = "".join(f"&{key}={value}" for key, value in query.items())
        return f"{reverse(FORECAST)}?fournisseur={store.pk}{params}" + (f"#{anchor}" if anchor else "")

    def run_page(self, store=None, client=None) -> str:
        return self.html(client=client, fournisseur=(store or self.wholesaler).pk, mode="courses")

    def add_form(self, store) -> dict:
        """What the list page's « Ajouter » form posts untouched: its hidden
        fields, and the option its unit select is drawn with (« habituelle »,
        the entry's own unit, until a script fills it)."""
        (form,) = forms_to(self.html(fournisseur=store.pk), ADD)
        (drawn,) = [value for value, _label, selected in options_of(form, "unite") if selected]
        return {**hidden_of(form), "unite": drawn}

    def add(self, store, **typed):
        """« Ajouter » on the list page, as drawn, with what is typed (and
        `unite` chosen)."""
        return self.post(ADD, {**self.add_form(store), "nom": "", "quantite": "", "note": "", **typed})

    def card_of(self, item, store=None) -> str:
        """The « Modifier » card's form of `item`, as drawn."""
        html = self.html(fournisseur=(store or self.wholesaler).pk, ligne=item.pk)
        (form,) = forms_to(edit_card(html), EDIT)
        return form

    def tick_form(self, item, client=None, store=None) -> dict:
        """The hidden fields of the item's tick form, as the tick page draws it."""
        drawn = {tick["name"]: tick for tick in ticks_of(run_block(self.run_page(store, client)))}
        return hidden_of(drawn[item.name]["form"])

    def beer_product(self) -> Product:
        return Product.objects.get(raw_name=SHOPPING_BEER_PRODUCT)

    def phone(self, email="lucie-exemple@example.invalid", areas=("shopping",), name="Lucie Exemple"):
        """Another phone: a client logged in as an employee of the espace
        given `areas` (« Liste de courses » alone by default)."""
        client = self.client_class()
        client.force_login(employee_of_the_test_tenant(email, areas, name=name))
        return client


class IndexTests(ListPageTestCase):
    """« Listes de courses »: the lists open, the store menu, the last
    finished."""

    def test_the_open_lists(self):
        html = self.html(INDEX)
        self.assertIn('<p class="page-subtitle">Une liste par enseigne, la même pour toute l\'équipe.</p>', html)
        table = table_of(html, "listes en cours")
        self.assertEqual(headers_of(table), ["Enseigne", "Pris", "Commencée le", ""])
        (row,) = body_rows(table)
        started = timezone.localtime(self.lists.open.created_at)
        self.assertEqual(cells_of(row), ["Grossiste exemple", "1 / 3", f"{started:%d/%m/%Y}", "Faire les courses"])
        self.assertIn('data-label="Pris" data-sort="1"', row)
        self.assertIn(f'data-label="Commencée le" data-sort="{started:%Y-%m-%d}"', row)
        self.assertEqual(
            links_of(row),
            [
                (self.page_of(self.wholesaler), "Grossiste exemple"),
                (self.page_of(self.wholesaler, mode="courses"), "Faire les courses"),
            ],
        )
        self.assertIn((reverse(FORECAST), "Prévoir les courses"), links_of(html))

    def test_the_finished_lists(self):
        table = table_of(self.html(INDEX), "listes terminées")
        self.assertEqual(headers_of(table), ["Enseigne", "Terminée le", "Pris", "Par"])
        (row,) = body_rows(table)
        finished = timezone.localtime(self.lists.finished.finished_at)
        self.assertEqual(cells_of(row), ["Épicerie exemple", f"{finished:%d/%m/%Y}", "1 / 2", "Un ancien membre"])
        self.assertEqual(links_of(row), [(f"{reverse(LIST_PAGE)}?liste={self.lists.finished.pk}", "Épicerie exemple")])
        self.assertNotIn(SHOPPING_LIST_FINISHER, row)

    def test_the_ten_finished_last_newest_first(self):
        now = timezone.now()
        for number in range(11):
            ShoppingList.objects.create(
                supplier=self.market, finished_at=now - timedelta(hours=number + 1), finished_by=TEST_EMAIL
            )
        rows = body_rows(table_of(self.html(INDEX), "listes terminées"))
        self.assertEqual(len(rows), 10)
        self.assertEqual([cells_of(row)[0] for row in rows], ["Marché exemple"] * 10)
        days = [re.search(r'data-label="Terminée le" data-sort="([^"]+)"', row).group(1) for row in rows]
        self.assertEqual(days, sorted(days, reverse=True))
        # The grocer's, finished yesterday, is the eleventh newest.
        self.assertNotIn("Épicerie exemple", "".join(rows))

    def test_who_finished_each(self):
        """« Vous » for the viewer; a login of this espace by its first name,
        else by its role; anyone else « Un ancien membre » - never an
        address."""
        employee = employee_of_the_test_tenant("lucie-exemple@example.invalid", ("products",), name="Lucie Exemple")
        nameless = employee_of_the_test_tenant("sans-nom@example.invalid", ("products",))
        now = timezone.now()
        for hours, who in ((1, TEST_EMAIL), (2, employee.username), (3, nameless.username)):
            ShoppingList.objects.create(supplier=self.market, finished_at=now - timedelta(hours=hours), finished_by=who)
        rows = body_rows(table_of(self.html(INDEX), "listes terminées"))
        self.assertEqual(
            [cells_of(row)[3] for row in rows], ["Vous", "Lucie Exemple", "Un employé", "Un ancien membre"]
        )
        self.assertNotIn("@", "".join(rows))

    def test_an_employee_reads_no_address(self):
        """Read by an employee given the lists alone, a list the owner
        finished (his login has no first name: a signup sets none) says
        « le gérant », one a login since removed finished « un ancien
        membre » - never the address either is, on the lists and on the
        lists themselves."""
        removed = "ancienne-exemple@example.invalid"
        now = timezone.now()
        by_owner = ShoppingList.objects.create(
            supplier=self.market, finished_at=now - timedelta(hours=1), finished_by=TEST_EMAIL
        )
        by_removed = ShoppingList.objects.create(
            supplier=self.market, finished_at=now - timedelta(hours=2), finished_by=removed
        )
        phone = self.phone("barman-exemple@example.invalid", name="Barman Exemple")
        index = self.html(INDEX, client=phone)
        self.assertEqual(
            [cells_of(row)[3] for row in body_rows(table_of(index, "listes terminées"))],
            ["Le gérant", "Un ancien membre", "Un ancien membre"],
        )
        owner_s = self.html(client=phone, liste=by_owner.pk)
        self.assertIn(" par le gérant · 0 / 0 pris.</p>", owner_s)
        removed_s = self.html(client=phone, liste=by_removed.pk)
        self.assertIn(" par un ancien membre · 0 / 0 pris.</p>", removed_s)
        for html in (index, owner_s, removed_s, self.html(client=phone, liste=self.lists.finished.pk)):
            for address in (TEST_EMAIL, removed, SHOPPING_LIST_FINISHER):
                with self.subTest(address=address):
                    self.assertNotIn(address, html)

    def test_only_a_list_holding_an_item_is_in_progress(self):
        """An open list emptied by « Retirer » is no list in progress: not
        under « En cours »."""
        ShoppingListItem.objects.filter(shopping_list=self.lists.open).delete()
        html = self.html(INDEX)
        self.assertIn('<p class="muted">Aucune liste en cours.</p>', html)
        self.assertEqual(table_of(html, "listes en cours"), "")

    def test_the_store_menu_offers_only_the_stores_bought_at(self):
        """Not a supplier of charges, not the removed AI reading's (kept by
        invoices/0038 where something named it: its code OTHER, its reader
        key emptied), not a supplier nothing was bought at - each sorted as
        read."""
        charges = make_supplier(name="Assurance exemple", expenses_only=True)
        robot = make_supplier(code="OTHER", name="Autre (analyse IA)")
        make_supplier(name="Jamais acheté exemple")
        for supplier in (charges, robot):
            line = make_invoice_line(
                invoice=make_invoice(supplier=supplier),
                product=make_product(supplier=supplier, stock_type=self.made.beer),
                total_ht="40.00",
            )
            make_movement(stock_type=self.made.beer, quantity="1", unit_cost_ht="40", invoice_line=line)
        html = self.html(INDEX)
        form = re.search(r'<form method="get" action="' + reverse(LIST_PAGE) + r'"[^>]*>.*?</form>', html, re.DOTALL)
        self.assertIsNotNone(form)
        menu = form.group(0) if form else ""
        self.assertIn('class="inline-form"', menu)
        self.assertIn('<label class="inline-label">Ouvrir la liste de', menu)
        self.assertEqual(
            [(value, unescape(words)) for value, words in re.findall(r'<option value="(\d+)">([^<]*)</option>', menu)],
            [
                (str(self.grocer.pk), "Épicerie exemple"),
                (str(self.wholesaler.pk), "Grossiste exemple"),
                (str(self.market.pk), "Marché exemple"),
            ],
        )
        self.assertIn('<button class="btn btn-secondary" type="submit">Ouvrir</button>', menu)

    def test_the_page_writes_nothing(self):
        before = row_counts()
        self.html(INDEX)
        self.assertEqual(row_counts(), before)


class IndexStatesTests(ListPageTestCase):
    WITH_LISTS = False

    def test_no_list_yet(self):
        html = self.html(INDEX)
        self.assertIn('<p class="muted">Aucune liste en cours.</p>', html)
        self.assertEqual(table_of(html, "listes en cours"), "")
        self.assertNotIn("<h2>Terminées</h2>", html)
        self.assertIn(f'<form method="get" action="{reverse(LIST_PAGE)}" class="inline-form">', html)

    def test_nothing_bought(self):
        StockMovement.objects.all().delete()
        html = self.html(INDEX)
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertIsNotNone(found)
        empty = found.group(1) if found else ""
        self.assertIn("Aucune enseigne : les listes partent de vos factures d'achat.", readable(empty))
        self.assertIn((reverse("invoices:invoice_add"), "Ajouter des factures"), links_of(empty))
        self.assertNotIn("<form", html.split('<div class="empty-state">')[1].split("</main>")[0])


class EditPageTests(ListPageTestCase):
    """One store's list, to prepare: its items, the card changing one, the
    form adding one."""

    def test_the_items_in_their_order(self):
        html = self.html(fournisseur=self.wholesaler.pk)
        self.assertIn('<div class="shopping-list-page">', html)
        self.assertIn("<h1>Courses chez Grossiste exemple</h1>", html)
        self.assertIn('<p class="page-subtitle">Préparez la liste, puis cochez en magasin.</p>', html)
        self.assertIn('<p class="muted">1 / 3 pris</p>', html)
        table = table_of(html, "articles")
        self.assertIn('<table data-table data-table-label="articles" class="phone-cards">', table)
        self.assertEqual(headers_of(table), ["Article", "Produit", "Quantité", "Note", "État", ""])
        rows = body_rows(table)
        self.assertEqual(
            [cells_of(row)[:5] for row in rows],
            [
                ["Bière exemple", f"{SHOPPING_BEER_PRODUCT} 1 colis de 24", "24", "", ""],
                ["Sirop exemple", "", "2 L", "", "pris"],
                ["Pain exemple", "", "2", "", ""],
            ],
        )
        self.assertEqual(
            [re.search(r'data-label="Quantité" data-sort="([^"]*)"', row).group(1) for row in rows], ["24", "2", "2"]
        )

    def test_the_header_s_links(self):
        html = self.html(fournisseur=self.wholesaler.pk)
        header = re.search(r'<div class="page-header">.*?</h1>.*?</div>\s*</div>', html, flags=re.DOTALL).group(0)
        self.assertEqual(
            links_of(header),
            [
                (reverse(INDEX), "← Listes de courses"),
                (self.page_of(self.wholesaler, mode="courses"), "Faire les courses"),
                (self.forecast_of(self.wholesaler), "Ajouter depuis la prévision"),
            ],
        )

    def test_each_row_s_actions(self):
        table = table_of(self.html(fournisseur=self.wholesaler.pk), "articles")
        for item in (self.lists.beer, self.lists.syrup, self.lists.bread):
            with self.subTest(item=item.label):
                row = row_of(table, f"<td>{item.label}</td>")
                actions = re.search(r'<td class="row-actions">(.*?)</td>', row, flags=re.DOTALL).group(1)
                self.assertEqual(
                    links_of(actions), [(self.page_of(self.wholesaler, "modifier", ligne=item.pk), "Modifier")]
                )
                (form,) = forms_to(actions, DELETE)
                self.assertEqual(hidden_of(form), {"ligne": str(item.pk), "fournisseur": str(self.wholesaler.pk)})
                self.assertIn('<button class="btn btn-small btn-secondary" type="submit">Retirer</button>', form)

    def test_the_add_form_and_its_menu(self):
        """The stock take's three parts: a name from a menu offering the
        articles AND the store's products under the stock take's names, a
        quantity, a unit select - one « habituelle » option until
        entry_units.js fills it from the island, which is list_entries'
        very data."""
        html = self.html(fournisseur=self.wholesaler.pk)
        (form,) = forms_to(html, ADD)
        self.assertIn('class="inline-form shopping-add"', form)
        self.assertEqual(hidden_of(form), {"fournisseur": str(self.wholesaler.pk)})
        # As long as the longest name the menu can offer: a pick is never cut.
        self.assertIn(
            '<label class="inline-label">Article ou produit <input type="text" name="nom" list="shopping-entries" '
            f'required maxlength="{ENTRY_MAX}" autocomplete="off"></label>',
            form,
        )
        self.assertIn('<input type="text" name="quantite" inputmode="decimal" size="6" placeholder="habituelle">', form)
        self.assertIn(
            '<label class="inline-label">Unité <select name="unite" data-entry-units="shopping-entry-data" '
            'data-entry-aliases="shopping-entry-aliases" data-entry-field="nom">'
            '<option value="" selected>habituelle</option></select></label>',
            form,
        )
        self.assertIn('<input type="text" name="note" maxlength="200">', form)
        # In the order they are filled: the name, the quantity, its unit, a note.
        self.assertLess(form.index('name="nom"'), form.index('name="quantite"'))
        self.assertLess(form.index('name="quantite"'), form.index('name="unite"'))
        self.assertLess(form.index('name="unite"'), form.index('name="note"'))
        self.assertEqual(busy_button_of(form), ("Ajout…", "Ajouter"))
        # The articles bought here, then the store's products, then the other
        # articles - each group as read, accents and case aside.
        self.assertEqual(
            menu_of(html),
            [
                "Bière exemple (article)",
                "Café exemple (article)",
                "Chips exemple (article)",
                "Fût exemple (article)",
                "Olives exemple (article)",
                "Rhum exemple (article)",
                "Sirop exemple (article)",
                f"{SHOPPING_BEER_PRODUCT}{AT_THE_WHOLESALER}",
                f"CAFÉ EXEMPLE PRODUIT{AT_THE_WHOLESALER}",
                f"CHIPS EXEMPLE PRODUIT{AT_THE_WHOLESALER}",
                f"FÛT EXEMPLE PRODUIT{AT_THE_WHOLESALER}",
                f"OLIVES EXEMPLE PRODUIT{AT_THE_WHOLESALER}",
                f"RHUM EXEMPLE PRODUIT{AT_THE_WHOLESALER}",
                f"{SYRUP_PRODUCT}{AT_THE_WHOLESALER}",
                "Citron exemple (article)",
                "Fraises exemple (article)",
            ],
        )
        # The island: the select's choices for each name of the menu, in its order.
        data = entry_data_of(html)
        expected = {entry.name: entry.as_data() for entry in list_entries(timezone.localdate(), self.wholesaler)}
        self.assertEqual(data, expected)
        self.assertEqual(list(data), menu_of(html))
        for name, shipped in (
            # Its usual product here is a bottle of 1 L: counted in bottles.
            (
                "Sirop exemple (article)",
                {"kind": "stock_type", "unit_choices": [["UNIT", "bouteilles de 1 L"], ["L", "litres"]]},
            ),
            ("Bière exemple (article)", {"kind": "stock_type", "unit_choices": [["UNIT", "unités"]]}),
            (
                "Olives exemple (article)",
                {"kind": "stock_type", "unit_choices": [["UNIT", "paquets de 1 kg"], ["KG", "kg"]]},
            ),
            # Never bought here: its packet is the one bought elsewhere; in kilos by default.
            (
                "Citron exemple (article)",
                {"kind": "stock_type", "unit_choices": [["UNIT", "paquets de 1 kg"], ["KG", "kg"]]},
            ),
            (f"{SHOPPING_BEER_PRODUCT}{AT_THE_WHOLESALER}", {"kind": "product", "unit_choices": [["UNIT", "unités"]]}),
            (
                f"{SYRUP_PRODUCT}{AT_THE_WHOLESALER}",
                {"kind": "product", "unit_choices": [["UNIT", "bouteilles de 1 L"], ["L", "litres"]]},
            ),
        ):
            with self.subTest(entry=name):
                self.assertEqual({key: data[name][key] for key in shipped}, shipped)
        self.assertEqual(
            {name: data[name]["default_unit"] for name in ("Sirop exemple (article)", "Citron exemple (article)")},
            {"Sirop exemple (article)": "UNIT", "Citron exemple (article)": "KG"},
        )
        # The script filling the select: deferred, drawn after the islands it reads.
        script = re.search(r'<script src="/static/js/entry_units\.js\?v=[^"]*" defer></script>', html)
        self.assertIsNotNone(script)
        self.assertLess(html.index(f'id="{ENTRY_DATA_ID}"'), script.start())
        self.assertLess(html.index(f'id="{ENTRY_ALIASES_ID}"'), script.start())
        # Each alias names an island entry.
        aliases = entry_aliases_of(html)
        self.assertEqual(set(aliases), {"exact", "folded"})
        self.assertLessEqual({*aliases["exact"].values(), *aliases["folded"].values()}, set(data))
        self.assertEqual(alias_of(aliases, "sirop exemple"), "Sirop exemple (article)")
        # What a typed quantity counts: the unit chosen, never packs.
        self.assertIn(ADD_FORM_HELP, readable(html.split("</datalist>")[1]))

    def test_the_card_changing_one_item(self):
        """Beside the quantity, the unit it counts - a select drawn by the
        server, its present terms selected: the beer (a UNIT article) its
        one choice, the syrup in litres its bottles of 1 L (its usual
        product here) or litres; a free text none. Then what the number
        counts: the product, and the packs it makes or « à l'unité »."""
        for item, quantity, units, counts in (
            (self.lists.beer, "24", [("UNIT", "unités", True)], f"{SHOPPING_BEER_PRODUCT} · 1 colis de 24"),
            (self.lists.syrup, "2", [("UNIT", "bouteilles de 1 L", False), ("L", "litres", True)], ""),
            (self.lists.bread, "2", [], ""),
        ):
            with self.subTest(item=item.label):
                html = self.html(fournisseur=self.wholesaler.pk, ligne=item.pk)
                card = edit_card(html)
                self.assertIn(f"<h2>Modifier « {item.label} »</h2>", card)
                (form,) = forms_to(card, EDIT)
                self.assertIn('class="inline-form"', form)
                self.assertEqual(hidden_of(form), {"ligne": str(item.pk), "fournisseur": str(self.wholesaler.pk)})
                self.assertIn(
                    f'<input type="text" name="quantite" value="{quantity}" inputmode="decimal" size="6" required '
                    "autofocus></label>",
                    form,
                )
                self.assertEqual(options_of(form, "unite"), units)
                if units:
                    self.assertIn('<label class="inline-label">Unité <select name="unite">', form)
                    self.assertLess(form.index('name="quantite"'), form.index('name="unite"'))
                else:
                    self.assertNotIn("<select", form)
                said = re.findall(r'<span class="muted small">([^<]*)</span>', form)
                self.assertEqual([unescape(words) for words in said], [counts] if counts else [])
                if counts:
                    # Beside the select, before the note.
                    span = form.index('<span class="muted small">')
                    self.assertLess(form.index("</select></label>"), span)
                    self.assertLess(span, form.index('name="note"'))
                self.assertIn('<input type="text" name="note" value="" maxlength="200">', form)
                self.assertEqual(busy_button_of(form), ("Enregistrement…", "Enregistrer"))
                self.assertIn((self.page_of(self.wholesaler), "Annuler"), links_of(form))
                self.assertEqual(warnings_of(html), [])
        ShoppingListItem.objects.filter(pk=self.lists.beer.pk).update(quantity=Decimal("30"))
        said = re.findall(r'<span class="muted small">([^<]*)</span>', self.card_of(self.lists.beer))
        self.assertEqual([unescape(words) for words in said], [f"{SHOPPING_BEER_PRODUCT} · à l'unité · colis de 24"])

    def test_the_card_of_a_free_text_says_what_it_counts(self):
        """An article deleted (SET_NULL), or merged beside a twin counting
        something else, leaves a free text holding its unit or its size: its
        card draws no select, and says beside the quantity what the number
        counts - as the list's row does. A free text typed counts what it
        names: nothing said."""
        ShoppingListItem.objects.filter(pk=self.lists.syrup.pk).update(stock_type=None)
        bottles = ShoppingListItem.objects.create(
            shopping_list=self.lists.open,
            label="Vin exemple",
            quantity=Decimal("1"),
            item_size=Decimal("0.7"),
            size_unit=UnitChoices.LITRE,
        )
        cartons = ShoppingListItem.objects.create(
            shopping_list=self.lists.open,
            label="Gin exemple",
            quantity=Decimal("6"),
            product_name="GIN EXEMPLE 70CL X6",
            pack_size=6,
            item_size=Decimal("0.7"),
            size_unit=UnitChoices.LITRE,
        )
        for item, counts in (
            (self.lists.syrup, "L"),
            (bottles, "bouteilles de 70 cl"),
            (cartons, "bouteilles de 70 cl · GIN EXEMPLE 70CL X6 · 1 colis de 6"),
            (self.lists.bread, ""),
        ):
            with self.subTest(item=item.label):
                form = self.card_of(item)
                self.assertNotIn("<select", form)
                said = re.findall(r'<span class="muted small">([^<]*)</span>', form)
                self.assertEqual([unescape(words) for words in said], [counts] if counts else [])

    def test_the_run_page_and_a_finished_list_ship_no_entries(self):
        """The menu, its island and its script are the add form's: the tick
        page and a finished list have none, and read nothing for them."""
        with patch("inventory.views.list_entries", side_effect=AssertionError("the menu read")):
            for page, html in (("tick", self.run_page()), ("finished", self.html(liste=self.lists.finished.pk))):
                with self.subTest(page=page):
                    self.assertNotIn(ENTRIES_ID, html)
                    self.assertNotIn(ENTRY_DATA_ID, html)
                    self.assertNotIn("entry_units.js", html)

    def test_a_line_not_in_the_list_is_said_in_the_page(self):
        for asked in ("abc", "999999", "\N{SUPERSCRIPT TWO}", str(self.lists.lemon.pk)):
            with self.subTest(ligne=asked):
                html = self.html(fournisseur=self.wholesaler.pk, ligne=asked)
                self.assertEqual(warnings_of(html), [NOT_IN_LIST])
                self.assertEqual(edit_card(html), "")
                self.assertTrue(table_of(html, "articles"))

    def test_a_store_with_no_list_yet(self):
        before = row_counts()
        html = self.html(fournisseur=self.market.pk)
        self.assertEqual(row_counts(), before)
        self.assertIn("<h1>Courses chez Marché exemple</h1>", html)
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertEqual(readable(found.group(1)), "Liste vide.")
        self.assertEqual(table_of(html, "articles"), "")
        self.assertNotIn("Faire les courses", html)
        self.assertNotIn(" pris</p>", html)
        self.assertEqual(len(forms_to(html, ADD)), 1)

    def test_a_store_that_cannot_be_read(self):
        """No store to draw: the lists' page, saying so."""
        charges = make_supplier(name="Assurance exemple", expenses_only=True)
        for asked in ("", "abc", "999999", "\N{SUPERSCRIPT TWO}", "1" * 30, str(charges.pk)):
            with self.subTest(fournisseur=asked):
                self.assertEqual(self.redirected(fournisseur=asked), reverse(INDEX))
                self.assertEqual(said_at_the_top(self.html(INDEX)), [STORE_TO_CHOOSE])

    def test_the_removed_ai_reading_s_supplier_has_no_list(self):
        """Bought at, and still no store: its address is the lists' page."""
        retired = retired_ai_supplier(self.made.beer)
        for run in ("", "courses"):
            with self.subTest(mode=run):
                self.assertEqual(self.redirected(fournisseur=retired.pk, mode=run), reverse(INDEX))
                self.assertEqual(said_at_the_top(self.html(INDEX)), [STORE_TO_CHOOSE])

    def test_a_store_with_an_open_list_and_no_purchase_left(self):
        """Its documents gone, the store is no longer offered: its open list
        still opens."""
        StockMovement.objects.filter(invoice_line__invoice__supplier=self.wholesaler).delete()
        html = self.html(fournisseur=self.wholesaler.pk)
        self.assertEqual(len(body_rows(table_of(html, "articles"))), 3)
        self.assertNotIn(f'<option value="{self.wholesaler.pk}">', self.html(INDEX))

    def test_a_list_by_its_id(self):
        """An open list has one address, its store's; a finished one is read
        as it was."""
        self.assertEqual(self.redirected(liste=self.lists.open.pk), self.page_of(self.wholesaler))
        self.assertEqual(
            self.redirected(liste=self.lists.open.pk, mode="courses"), self.page_of(self.wholesaler, mode="courses")
        )
        for asked in ("abc", "999999", "\N{SUPERSCRIPT TWO}", "-1"):
            with self.subTest(liste=asked):
                self.assertEqual(self.redirected(liste=asked), reverse(INDEX))
                self.assertEqual(said_at_the_top(self.html(INDEX)), [LIST_NOT_FOUND])

    def test_a_finished_list_is_read_only(self):
        html = self.html(liste=self.lists.finished.pk)
        finished = timezone.localtime(self.lists.finished.finished_at)
        self.assertIn("<h1>Courses chez Épicerie exemple</h1>", html)
        self.assertIn(
            f'<p class="page-subtitle">Terminées le {finished:%d/%m/%Y} par un ancien membre · 1 / 2 pris.</p>',
            html,
        )
        self.assertNotIn(SHOPPING_LIST_FINISHER, html)
        table = table_of(html, "courses terminées")
        self.assertEqual(headers_of(table), ["Article", "Produit", "Quantité", "Note", "État"])
        self.assertEqual(
            [cells_of(row) for row in body_rows(table)],
            [["Citron exemple", "", "1 kg", "", "pris"], ["Café exemple", "", "1 kg", "", "non pris"]],
        )
        main = html.split("<main", 1)[1].split("</main>")[0]
        self.assertNotIn("<form", main)
        self.assertNotIn("Modifier", main)
        self.assertEqual(links_of(main.split("</h1>")[0]), [(reverse(INDEX), "← Listes de courses")])

    def test_finished_by_the_viewer(self):
        ShoppingList.objects.filter(pk=self.lists.finished.pk).update(finished_by=TEST_EMAIL)
        self.assertIn(" par vous · 1 / 2 pris.</p>", self.html(liste=self.lists.finished.pk))


class AddTests(ListPageTestCase):
    """« Ajouter » on the list page: an article by its name, its usual
    purchase here when no quantity is typed, or a free text."""

    WITH_LISTS = False

    def test_an_article_with_no_quantity_takes_the_usual_purchase_here(self):
        response = self.add(self.wholesaler, nom="Bière exemple")
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler))
        html = response.content.decode()
        self.assertEqual(said_at_the_top(html), ["« Bière exemple » ajouté à la liste (24 · 1 colis de 24)."])
        self.assertEqual(
            open_items(self.wholesaler), [("Bière exemple", "24", "", SHOPPING_BEER_PRODUCT, 24, "", False)]
        )
        made = ShoppingList.objects.get()
        self.assertEqual((made.supplier, made.created_by, made.finished_at), (self.wholesaler, TEST_EMAIL, None))
        item = ShoppingListItem.objects.get()
        self.assertEqual((item.stock_type, item.added_by), (self.made.beer, TEST_EMAIL))
        self.assertEqual(cells_of(row_of(table_of(html, "articles"), "Bière exemple"))[2], "24")

    def test_a_typed_quantity_counts_what_the_usual_purchase_counts(self):
        self.add(self.wholesaler, nom="Bière exemple", quantite="48", note="Note exemple")
        self.add(self.wholesaler, nom="Sirop exemple", quantite="3")
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Bière exemple", "48", "", SHOPPING_BEER_PRODUCT, 24, "Note exemple", False),
                ("Sirop exemple", "3", "", SYRUP_PRODUCT, None, "", False),
            ],
        )

    def test_a_typed_quantity_no_whole_pack_says_it_counts_units(self):
        """« 2 » typed for the beer, bought in cartons of 24: two bottles,
        said so in the message, under the product and in the store - never a
        bare « colis de 24 » beside the 2."""
        response = self.add(self.wholesaler, nom="Bière exemple", quantite="2")
        html = response.content.decode()
        self.assertEqual(said_at_the_top(html), ["« Bière exemple » ajouté à la liste (2 · à l'unité · colis de 24)."])
        row = row_of(table_of(html, "articles"), "Bière exemple")
        self.assertEqual(unescape(cells_of(row)[1]), f"{SHOPPING_BEER_PRODUCT} à l'unité · colis de 24")
        (tick,) = ticks_of(run_block(self.run_page()))
        self.assertEqual(tick["under"], [f"{SHOPPING_BEER_PRODUCT} · à l'unité · colis de 24"])

    def test_a_ticked_item_typed_again_is_put_back_to_buy(self):
        """Ticked as bought on a list nobody finished, then typed again: put
        back to buy with what is typed - one item, said « remis »."""
        self.add(self.wholesaler, nom="Pain exemple", quantite="2")
        bread = ShoppingListItem.objects.get()
        set_ticked(bread.pk, True, TEST_EMAIL, timezone.now())
        response = self.add(self.wholesaler, nom="pain  exemple", quantite="3", note="Complet")
        self.assertEqual(said_at_the_top(response.content.decode()), ["« Pain exemple » remis dans la liste (3)."])
        self.assertEqual(open_items(self.wholesaler), [("Pain exemple", "3", "", "", None, "Complet", False)])
        self.assertEqual(ShoppingListItem.objects.get().pk, bread.pk)

    def test_a_usual_purchase_too_wide_is_refused_in_french(self):
        """A name typed with no quantity takes the usual purchase here: wider
        than the column holds (a misread invoice), it is refused - said,
        nothing written, never a 500."""
        wide = Figures(Decimal("12345678"), "", None, SHOPPING_BEER_PRODUCT, 24)
        with patch("inventory.views.usual_figures", return_value=wide):
            response = self.add(self.wholesaler, nom="Bière exemple")
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler))
        self.assertEqual(
            said_at_the_top(response.content.decode()),
            ["« Bière exemple » : quantité trop grande, rien n'a été ajouté."],
        )
        self.assertFalse(ShoppingList.objects.exists())

    def test_an_article_never_bought_here(self):
        """In its own unit: 1 when nothing is typed."""
        response = self.add(self.wholesaler, nom="Citron exemple")
        self.assertEqual(said_at_the_top(response.content.decode()), ["« Citron exemple » ajouté à la liste (1 kg)."])
        self.add(self.wholesaler, nom="Fraises exemple", quantite="2,5")
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Citron exemple", "1", UnitChoices.KILOGRAM, "", None, "", False),
                ("Fraises exemple", "2.5", UnitChoices.KILOGRAM, "", None, "", False),
            ],
        )

    def test_a_free_text(self):
        response = self.add(self.wholesaler, nom="  Pain   exemple ")
        self.assertEqual(said_at_the_top(response.content.decode()), ["« Pain exemple » ajouté à la liste (1)."])
        self.add(self.wholesaler, nom="Serviettes exemple", quantite="3", note="Blanches")
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Pain exemple", "1", "", "", None, "", False),
                ("Serviettes exemple", "3", "", "", None, "Blanches", False),
            ],
        )
        self.assertFalse(ShoppingListItem.objects.filter(stock_type__isnull=False).exists())

    def test_a_name_typed_otherwise_finds_the_article(self):
        self.add(self.wholesaler, nom="BIERE exemple")
        item = ShoppingListItem.objects.get()
        self.assertEqual(
            (item.stock_type, item.label, item.product_name), (self.made.beer, "Bière exemple", SHOPPING_BEER_PRODUCT)
        )

    def test_each_refusal_said_and_nothing_written(self):
        for typed, said in (
            ({"nom": ""}, [NAME_MISSING]),
            ({"nom": " \t "}, [NAME_MISSING]),
            ({"nom": "x" * 256}, [NAME_TOO_LONG]),
            ({"nom": "Bière exemple", "quantite": "0"}, [QUANTITY_REFUSED]),
            ({"nom": "Bière exemple", "quantite": "-1"}, [QUANTITY_REFUSED]),
            ({"nom": "Pain exemple", "quantite": "abc"}, [QUANTITY_REFUSED]),
            ({"nom": "Pain exemple", "quantite": "1,2345"}, [QUANTITY_REFUSED]),
            ({"nom": "Pain exemple", "quantite": "1" * 30}, [QUANTITY_REFUSED]),
            ({"nom": "Pain exemple", "note": "n" * 201}, [NOTE_TOO_LONG]),
            ({"nom": "", "quantite": "0", "note": "n" * 201}, [NAME_MISSING, QUANTITY_REFUSED, NOTE_TOO_LONG]),
        ):
            with self.subTest(typed={key: value[:20] for key, value in typed.items()}):
                before = row_counts()
                response = self.add(self.wholesaler, **typed)
                self.assertEqual(self.landing(response), self.page_of(self.wholesaler))
                self.assertEqual(said_at_the_top(response.content.decode()), said)
                self.assertEqual(row_counts(), before)
        self.assertFalse(ShoppingList.objects.exists())

    def test_a_long_name_the_menu_offers_is_added(self):
        """The length is judged by what is stored: a menu name longer than
        the label's column - a long product at the store, a long article and
        its suffix - names an article, whose own name always fits. Only a
        free text, stored as typed, is held to LABEL_MAX; anything longer
        than any menu name is refused before it is read."""
        article = make_stock_type(name="A" * 250, unit=UnitChoices.LITRE)
        make_product(self.wholesaler, "P" * 240, article, unit=UnitChoices.LITRE)
        product_entry, article_entry = f"{'P' * 240}{AT_THE_WHOLESALER}", f"{'A' * 250} (article)"
        html = self.html(fournisseur=self.wholesaler.pk)
        menu = menu_of(html)
        for name in (product_entry, article_entry):
            self.assertIn(name, menu)
            self.assertGreater(len(name), 255)
        drawn = re.search(r'name="nom" list="shopping-entries" required maxlength="(\d+)"', html)
        self.assertGreaterEqual(int(drawn.group(1)), max(len(name) for name in menu))
        for name, product_name in ((product_entry, "P" * 240), (article_entry, "")):
            with self.subTest(name=name[-30:]):
                ShoppingListItem.objects.all().delete()
                response = self.add(self.wholesaler, nom=name)
                item = ShoppingListItem.objects.get()
                self.assertEqual((item.stock_type, item.label, item.product_name), (article, "A" * 250, product_name))
                (said,) = said_at_the_top(response.content.decode())
                self.assertTrue(said.startswith(f"« {'A' * 250} » ajouté à la liste ("), said[-40:])
        # A free text is stored as typed: 255 characters at most.
        ShoppingListItem.objects.all().delete()
        for typed in ("x" * 256, "x" * (ENTRY_MAX + 1), article_entry + "x" * 300):
            with self.subTest(length=len(typed)):
                before = row_counts()
                response = self.add(self.wholesaler, nom=typed, quantite="0")
                self.assertEqual(said_at_the_top(response.content.decode()), [NAME_TOO_LONG, QUANTITY_REFUSED])
                self.assertEqual(row_counts(), before)
        self.add(self.wholesaler, nom="x" * 255)
        self.assertEqual(ShoppingListItem.objects.get().label, "x" * 255)

    def test_a_store_that_cannot_be_read(self):
        charges = make_supplier(name="Assurance exemple", expenses_only=True)
        for asked in ("", "abc", "999999", "\N{SUPERSCRIPT TWO}", str(charges.pk)):
            with self.subTest(fournisseur=asked):
                response = self.post(ADD, {"fournisseur": asked, "nom": "Pain exemple", "quantite": "2"})
                self.assertEqual(self.landing(response), reverse(INDEX))
                self.assertEqual(said_at_the_top(response.content.decode()), [STORE_NOT_FOUND_ADD])

    def test_nothing_is_added_for_the_removed_ai_reading_s_supplier(self):
        retired = retired_ai_supplier(self.made.beer)
        before = row_counts()
        response = self.post(ADD, {"fournisseur": retired.pk, "nom": "Pain exemple", "quantite": "2"})
        self.assertEqual(self.landing(response), reverse(INDEX))
        self.assertEqual(said_at_the_top(response.content.decode()), [STORE_NOT_FOUND_ADD])
        self.assertEqual(row_counts(), before)
        self.assertFalse(ShoppingList.objects.filter(supplier=retired).exists())
        self.assertFalse(ShoppingList.objects.exists())

    def test_twice_is_once(self):
        self.add(self.wholesaler, nom="Bière exemple")
        response = self.add(self.wholesaler, nom="Bière exemple", quantite="72")
        html = response.content.decode()
        self.assertEqual(said_at_the_top(html), ["« Bière exemple » est déjà dans la liste (24 · 1 colis de 24)."])
        self.assertIn('<li class="message message-warning">', html)
        self.add(self.wholesaler, nom="Pain exemple", quantite="2")
        response = self.add(self.wholesaler, nom="PAIN  exemple")
        self.assertEqual(said_at_the_top(response.content.decode()), ["« Pain exemple » est déjà dans la liste (2)."])
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Bière exemple", "24", "", SHOPPING_BEER_PRODUCT, 24, "", False),
                ("Pain exemple", "2", "", "", None, "", False),
            ],
        )
        self.assertEqual(ShoppingList.objects.count(), 1)

    def test_never_on_a_finished_list(self):
        lists = make_shopping_lists(self.made)
        self.add(self.grocer, nom="Café exemple")
        self.assertEqual(ShoppingList.objects.filter(supplier=self.grocer).count(), 2)
        self.assertEqual(lists.finished.items.count(), 2)
        (item,) = open_items(self.grocer)
        self.assertEqual(item[:3], ("Café exemple", "1", ""))


class AddUnitsTests(ListPageTestCase):
    """« Ajouter » in the unit chosen (`unite`, the stock take's values:
    « UNIT » counts items - bottles, kegs, packets -, « L » / « KG » the
    article's own measure): an article in litres in its bottles when the
    size of one is known (its usual product here, else the format it is
    most bought in anywhere), a product of the store in its items or its
    article's measure. Every post is the form as the page draws it, its unit
    one the page's island offers. make_shopping_bottles' data (invented)."""

    WITH_LISTS = False
    WITH_BOTTLES = True

    def menu(self) -> tuple[list[str], dict]:
        html = self.html(fournisseur=self.wholesaler.pk)
        return menu_of(html), entry_data_of(html)

    def added(self, **typed) -> tuple[ShoppingListItem, list[str]]:
        """The one item `typed` adds to an empty list, and what was said."""
        ShoppingListItem.objects.all().delete()
        response = self.add(self.wholesaler, **typed)
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler))
        return ShoppingListItem.objects.get(), said_at_the_top(response.content.decode())

    def refused(self, said, **typed) -> None:
        """`typed` refused with `said`, nothing written."""
        ShoppingListItem.objects.all().delete()
        before = row_counts()
        response = self.add(self.wholesaler, **typed)
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler))
        self.assertEqual(said_at_the_top(response.content.decode()), said)
        self.assertEqual(row_counts(), before)

    def test_the_menu_offers_what_one_item_is(self):
        names, data = self.menu()
        gin, vodka, juice = "Gin exemple (article)", "Vodka exemple (article)", "Jus exemple (article)"
        for name, choices, default in (
            # Its usual product here: 70 cl bottles, in bottles by default.
            (gin, [["UNIT", "bouteilles de 70 cl"], ["L", "litres"]], "UNIT"),
            # Never bought here: the 70 cl it is bought in elsewhere, in bottles.
            (vodka, [["UNIT", "bouteilles de 70 cl"], ["L", "litres"]], "UNIT"),
            # Bought by measure, its volumes never alike: no format known.
            (juice, [["L", "litres (format inconnu)"]], "L"),
            ("Bière pression exemple (article)", [["UNIT", "fûts de 30 L"], ["L", "litres"]], "UNIT"),
            ("Gobelets exemple (article)", [["UNIT", "paquets de 50 u."]], "UNIT"),
            (f"{SHOPPING_GIN_PRODUCT}{AT_THE_WHOLESALER}", [["UNIT", "bouteilles de 70 cl"], ["L", "litres"]], "UNIT"),
            # Weighed at the counter: in kilos, the stock take's rule.
            (f"{SHOPPING_CHEESE_PRODUCT}{AT_THE_WHOLESALER}", [["UNIT", "unités"], ["KG", "kg"]], "KG"),
        ):
            with self.subTest(entry=name):
                self.assertIn(name, names)
                self.assertEqual(data[name]["unit_choices"], choices)
                self.assertEqual(data[name]["default_unit"], default)
        # Another store's product is no entry here.
        self.assertFalse([name for name in names if name.startswith(SHOPPING_VODKA_PRODUCT)])

    def test_each_post_and_what_it_stores(self):
        """SPEC_UNITS §6.1, row by row: what is stored (quantity, unit,
        product, pack, size of one, its unit) and what is said."""
        for typed, stored, said in (
            (
                {"nom": "Gin exemple (article)"},
                ("6", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"),
                "« Gin exemple » ajouté à la liste (6 bouteilles de 70 cl · 1 colis de 6).",
            ),
            (
                {"nom": "gin exemple", "quantite": "3"},
                ("3", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"),
                "« Gin exemple » ajouté à la liste (3 bouteilles de 70 cl · à l'unité · colis de 6).",
            ),
            (
                {"nom": "Gin exemple (article)", "unite": "L"},
                ("4.2", "L", "", None, None, ""),
                "« Gin exemple » ajouté à la liste (4.2 L).",
            ),
            (
                {"nom": "Vodka exemple (article)"},
                ("1", "", "", None, "0.7", "L"),
                "« Vodka exemple » ajouté à la liste (1 bouteille de 70 cl).",
            ),
            (
                {"nom": "Vodka exemple (article)", "quantite": "2", "unite": "L"},
                ("2", "L", "", None, None, ""),
                "« Vodka exemple » ajouté à la liste (2 L).",
            ),
            (
                {"nom": "Jus exemple (article)"},
                ("2.5", "L", "", None, None, ""),
                "« Jus exemple » ajouté à la liste (2.5 L).",
            ),
            (
                {"nom": f"{SHOPPING_GIN_PRODUCT}{AT_THE_WHOLESALER}", "quantite": "12"},
                ("12", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"),
                "« Gin exemple » ajouté à la liste (12 bouteilles de 70 cl · 2 colis de 6).",
            ),
            (
                {"nom": "Bière pression exemple (article)"},
                ("1", "", "BIERE PRESSION EXEMPLE FUT 30L", None, "30", "L"),
                "« Bière pression exemple » ajouté à la liste (1 fût de 30 L).",
            ),
            (
                {"nom": "Gobelets exemple (article)", "unite": "UNIT"},
                ("2", "", SHOPPING_CUPS_PRODUCT, None, "50", "UNIT"),
                "« Gobelets exemple » ajouté à la liste (2 paquets de 50 u.).",
            ),
            # A product counted in its article's measure keeps its name (« mesuré directement »).
            (
                {"nom": f"{SHOPPING_GIN_PRODUCT}{AT_THE_WHOLESALER}", "quantite": "1.5", "unite": "L"},
                ("1.5", "L", SHOPPING_GIN_PRODUCT, None, None, ""),
                "« Gin exemple » ajouté à la liste (1.5 L).",
            ),
            # Nothing typed, the usual product here: its usual bottles in
            # litres - as the article entry says « celle d'habitude ici ».
            (
                {"nom": f"{SHOPPING_GIN_PRODUCT}{AT_THE_WHOLESALER}", "unite": "L"},
                ("4.2", "L", SHOPPING_GIN_PRODUCT, None, None, ""),
                "« Gin exemple » ajouté à la liste (4.2 L).",
            ),
            # Weighed: in kilos unless items are asked for.
            (
                {"nom": f"{SHOPPING_CHEESE_PRODUCT}{AT_THE_WHOLESALER}"},
                ("1", "KG", SHOPPING_CHEESE_PRODUCT, None, None, ""),
                "« Fromage exemple » ajouté à la liste (1 kg).",
            ),
        ):
            with self.subTest(typed=typed):
                item, message = self.added(**typed)
                self.assertEqual(sizes_of(item), stored)
                self.assertEqual(message, [said])
        # A product entry adds its article: one item per article per list.
        item, _said = self.added(nom=f"{SHOPPING_GIN_PRODUCT}{AT_THE_WHOLESALER}")
        self.assertEqual((item.stock_type, item.label), (self.bottles.gin, "Gin exemple"))

    def test_a_unit_not_offered_is_refused_and_nothing_written(self):
        for typed in (
            # No format known: litres only.
            {"nom": "Jus exemple (article)", "unite": "UNIT"},
            # A free text counts what it names: no unit.
            {"nom": "Pain exemple", "unite": "L"},
            {"nom": "Pain exemple", "unite": "UNIT"},
            # A UNIT article counts its items alone.
            {"nom": "Gobelets exemple (article)", "unite": "L"},
            {"nom": "Bière exemple (article)", "unite": "KG"},
            # Never another article's measure, nor a value the app does not know.
            {"nom": "Gin exemple (article)", "unite": "KG"},
            {"nom": "Gin exemple (article)", "unite": "l"},
            {"nom": "Gin exemple (article)", "unite": " L"},
            {"nom": "Gin exemple (article)", "unite": "bouteilles"},
            {"nom": f"{SHOPPING_CHEESE_PRODUCT}{AT_THE_WHOLESALER}", "unite": "L"},
        ):
            with self.subTest(typed=typed):
                self.refused([UNIT_REFUSED], **typed)
        # Said with the other refusals, in the form's order.
        self.refused(
            [QUANTITY_REFUSED, UNIT_REFUSED, NOTE_TOO_LONG],
            nom="Pain exemple",
            quantite="0",
            unite="L",
            note="n" * 201,
        )
        self.refused([NAME_MISSING, QUANTITY_REFUSED], nom="", quantite="0", unite="L")

    def test_an_empty_or_missing_unit_is_the_entry_s_default(self):
        _item, said = self.added(nom="Gin exemple (article)", unite="")
        self.assertEqual(said, ["« Gin exemple » ajouté à la liste (6 bouteilles de 70 cl · 1 colis de 6)."])
        ShoppingListItem.objects.all().delete()
        (form,) = forms_to(self.html(fournisseur=self.wholesaler.pk), ADD)
        # An old page, or no JavaScript: no `unite` at all.
        self.post(ADD, {**hidden_of(form), "nom": "Gin exemple (article)", "quantite": "", "note": ""})
        self.assertEqual(sizes_of(ShoppingListItem.objects.get()), ("6", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))

    def test_the_beer_reads_as_before(self):
        """A UNIT article whose usual product is a piece: its one unit asked
        for, or none, it counts as it always did - « 24 », never « 24
        paquets »."""
        for unite in ("", "UNIT"):
            with self.subTest(unite=unite):
                item, said = self.added(nom="Bière exemple", unite=unite)
                self.assertEqual(said, ["« Bière exemple » ajouté à la liste (24 · 1 colis de 24)."])
                self.assertEqual(sizes_of(item)[:4], ("24", "", SHOPPING_BEER_PRODUCT, 24))
                self.assertEqual(words_of(item), "24")

    def test_another_store_s_product_is_a_free_text_here(self):
        item, said = self.added(nom=f"{SHOPPING_VODKA_PRODUCT} — Épicerie exemple")
        self.assertIsNone(item.stock_type)
        self.assertEqual(sizes_of(item), ("1", "", "", None, None, ""))
        self.assertEqual(said, [f"« {SHOPPING_VODKA_PRODUCT} — Épicerie exemple » ajouté à la liste (1)."])

    def test_every_entry_with_every_unit_it_offers(self):
        """The page and the add agree: each name of the menu, posted with
        each unit its island offers - nothing typed, then 2 - adds its
        article, counted as its label says."""
        names, data = self.menu()
        self.assertEqual(set(names), set(data))
        products = {
            f"{product.raw_name}{AT_THE_WHOLESALER}": product
            for product in Product.objects.filter(supplier=self.wholesaler)
        }
        posted = 0
        for name in names:
            article = products[name].stock_type if data[name]["kind"] == "product" else None
            if article is None:
                article = StockType.objects.get(name=name.removesuffix(" (article)"))
            for value, label in data[name]["unit_choices"]:
                for quantite in ("", "2"):
                    with self.subTest(entry=name, unite=value, quantite=quantite):
                        item, said = self.added(nom=name, quantite=quantite, unite=value)
                        posted += 1
                        self.assertEqual(item.stock_type, article)
                        words = words_of(item)
                        self.assertTrue(said[0].startswith(f"« {article.name} » ajouté à la liste ({words}"), said)
                        if data[name]["kind"] == "product":
                            self.assertEqual(item.product_name, products[name].raw_name)
                        if value != "UNIT":
                            # A measure: « litres », « litres (format inconnu) », « kg ».
                            self.assertEqual(item.unit, value)
                            self.assertTrue(label.startswith({"L": "litres", "KG": "kg"}[value]), label)
                        elif label == "unités":
                            # Items with no size said: a bare number, or the article's pieces.
                            self.assertNotIn(" de ", words)
                        else:
                            # « bouteilles de 70 cl »: the items of that size.
                            self.assertEqual((item.unit, item.size_unit), ("", article.unit))
                            self.assertTrue(words.endswith(" de " + label.split(" de ", 1)[1]), (words, label))
                            if quantite:
                                self.assertEqual(words, f"2 {label}")
        self.assertGreater(posted, 50)

    def test_a_carton_is_a_pack_never_a_keg(self):
        """An article in litres bought at the store by the carton (one
        invoice unit, six 75 cl bottles, 4.5 L): its item is that carton -
        « packs de 4.5 L » in the select, « 1 pack de 4.5 L » in the message,
        the list, the tick page and the forecast; never « fût »."""
        rose = make_stock_type(name="Rosé carton exemple", unit=UnitChoices.LITRE)
        carton = make_product(self.wholesaler, "ROSE EXEMPLE CARTON 6X75CL", rose, stock_equivalent="4.5")
        today = timezone.localdate()
        for weeks in (3, 2, 1):
            line = make_invoice_line(
                invoice=make_invoice(supplier=self.wholesaler, invoice_date=today - timedelta(days=7 * weeks)),
                product=carton,
                quantity=Decimal("1"),
                total_ht="45.00",
            )
            make_movement(stock_type=rose, quantity="4.5", unit_cost_ht="10", invoice_line=line)
        _names, data = self.menu()
        for name in ("Rosé carton exemple (article)", f"ROSE EXEMPLE CARTON 6X75CL{AT_THE_WHOLESALER}"):
            with self.subTest(entry=name):
                self.assertEqual(data[name]["unit_choices"], [["UNIT", "packs de 4.5 L"], ["L", "litres"]])
        item, said = self.added(nom="Rosé carton exemple (article)")
        self.assertEqual(sizes_of(item), ("1", "", "ROSE EXEMPLE CARTON 6X75CL", None, "4.5", "L"))
        self.assertEqual(said, ["« Rosé carton exemple » ajouté à la liste (1 pack de 4.5 L)."])
        row = row_of(table_of(self.html(fournisseur=self.wholesaler.pk), "articles"), "Rosé carton exemple")
        self.assertEqual(cells_of(row)[2], "1 pack de 4.5 L")
        (tick,) = ticks_of(run_block(self.run_page()))
        self.assertEqual(tick["quantity"], "1 pack de 4.5 L")
        forecast = self.html(FORECAST, fournisseur=self.wholesaler.pk)
        self.assertIn("Dans la liste (1 pack de 4.5 L)", forecast)
        self.assertNotIn("fût de 4.5 L", forecast)

    def test_the_names_the_server_accepts_are_the_select_s_too(self):
        """A name typed the way the lists always took it - an article's own
        name in any case, a product's raw name - is no island name; the page
        ships it beside the island (`shopping-entry-aliases`), so the select
        follows it: exactly, then folded. Only names the server resolves to
        that very entry: never another store's product, never a name two
        articles read alike."""
        make_stock_type(name="Cafe exemple", unit=UnitChoices.KILOGRAM)
        html = self.html(fournisseur=self.wholesaler.pk)
        data, aliases = entry_data_of(html), entry_aliases_of(html)
        gin_product = f"{SHOPPING_GIN_PRODUCT}{AT_THE_WHOLESALER}"
        for typed, name in (
            ("vodka exemple", "Vodka exemple (article)"),
            ("Vodka exemple", "Vodka exemple (article)"),
            ("  VODKA  Exemple ", "Vodka exemple (article)"),
            ("gin exemple", "Gin exemple (article)"),
            ("gin exemple (article)", "Gin exemple (article)"),
            ("Bière pression exemple", "Bière pression exemple (article)"),
            ("biere pression exemple", "Bière pression exemple (article)"),
            (SHOPPING_GIN_PRODUCT, gin_product),
            ("gin exemple 70cl x6", gin_product),
            (gin_product.lower(), gin_product),
            # Two articles read alike: each by its exact name.
            ("Café exemple", "Café exemple (article)"),
            ("Cafe exemple", "Cafe exemple (article)"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(alias_of(aliases, typed), name)
                self.assertIn(name, data)
        for typed in (
            # Read alike by two articles: no entry, as the server finds none.
            "cafe exemple",
            "CAFÉ EXEMPLE",
            # Another store's product is no name here.
            SHOPPING_VODKA_PRODUCT,
            same_name(SHOPPING_VODKA_PRODUCT),
            "Pain exemple",
        ):
            with self.subTest(typed=typed):
                self.assertIsNone(alias_of(aliases, typed))
        # The island's own names are its own: no alias repeats one.
        self.assertFalse(set(aliases["exact"]) & set(data))

    def test_every_alias_with_every_unit_stores_as_its_entry(self):
        """The select follows an alias with its entry's units: posting the
        alias with each of them stores what posting the island's name does."""
        make_stock_type(name="Cafe exemple", unit=UnitChoices.KILOGRAM)
        html = self.html(fournisseur=self.wholesaler.pk)
        data, aliases = entry_data_of(html), entry_aliases_of(html)
        stored: dict = {}

        def posted(nom, unite) -> tuple:
            item, _said = self.added(nom=nom, quantite="2", unite=unite)
            return item.stock_type_id, sizes_of(item)

        checked = 0
        for kind in ("exact", "folded"):
            for typed, name in aliases[kind].items():
                for value, _label in data[name]["unit_choices"]:
                    with self.subTest(kind=kind, typed=typed, unite=value):
                        if (name, value) not in stored:
                            stored[name, value] = posted(name, value)
                        self.assertEqual(posted(typed, value), stored[name, value])
                        checked += 1
        self.assertGreater(checked, 50)


class ForecastAddTests(ListPageTestCase):
    """« Ajouter » on a line of « Prévoir les courses »: one redirect back to
    the forecast of that store, its days kept, opened on the section the line
    came from, the message said there."""

    WITH_LISTS = False

    def line_form(self, html: str, label: str, name: str) -> dict:
        (form,) = forms_to(row_of(table_of(html, label), name), ADD)
        return {**hidden_of(form), "quantite": value_of_field(form, "quantite")}

    def test_each_section_lands_where_the_line_came_from(self):
        for label, article, anchor in (
            ("à acheter", self.made.beer, "a-acheter"),
            ("peut-être", self.made.olives, "peut-etre"),
            ("nouveaux ici", self.made.crisps, "nouveaux-ici"),
        ):
            for query in ({}, {"dans": "30"}):
                with self.subTest(section=label, query=query):
                    ShoppingListItem.objects.all().delete()
                    data = self.line_form(
                        self.html(FORECAST, fournisseur=self.wholesaler.pk, **query), label, article.name
                    )
                    response = self.post(ADD, data)
                    self.assertEqual(self.landing(response), self.forecast_of(self.wholesaler, anchor, **query))
                    html = response.content.decode()
                    words = counted = data["quantite"]
                    if article == self.made.beer:
                        # Its product comes in cartons of 24: how many, said.
                        counted += f" · {int(counted) // 24} colis de 24"
                    elif article == self.made.olives:
                        # Its product is a packet of 1 kg (the fixture's sizes are 1).
                        noun = "paquet" if Decimal(words) < 2 else "paquets"
                        words = counted = f"{words} {noun} de 1 kg"
                    said = [f"« {article.name} » ajouté à la liste ({counted})."]
                    if anchor == "a-acheter":
                        self.assertEqual(said_above_the_list(html), said)
                    else:
                        fold = fold_of(html, anchor)
                        self.assertTrue(opened(fold), anchor)
                        self.assertEqual(said_in(fold), said)
                    self.assertEqual(said_at_the_top(html), [])
                    # The line now says it is on the list, in the words the list says it.
                    row = row_of(table_of(html, label), article.name)
                    self.assertIn((self.page_of(self.wholesaler), f"Dans la liste ({words})"), links_of(row))

    def test_the_line_s_figures_reach_the_list(self):
        html = self.html(FORECAST, fournisseur=self.wholesaler.pk)
        self.post(ADD, {**self.line_form(html, "à acheter", "Bière exemple"), "quantite": "48"})
        self.post(ADD, self.line_form(html, "à acheter", "Sirop exemple"))
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Bière exemple", "48", "", SHOPPING_BEER_PRODUCT, 24, "", False),
                ("Sirop exemple", "2", "", SYRUP_PRODUCT, None, "", False),
            ],
        )
        self.assertEqual(ShoppingListItem.objects.get(label="Bière exemple").stock_type, self.made.beer)

    def test_the_product_s_size_reaches_the_list(self):
        """The line counts the store's product: the item keeps the size of
        one, in the article's unit - six bottles of the gin are « 6
        bouteilles de 70 cl », on the list and on the forecast."""
        bottles = make_shopping_bottles(self.made)
        html = self.html(FORECAST, fournisseur=self.wholesaler.pk)
        data = self.line_form(html, "à acheter", "Gin exemple")
        self.assertEqual((data["produit"], data["quantite"]), (str(bottles.gin_product.pk), "6"))
        response = self.post(ADD, data)
        html = response.content.decode()
        self.assertEqual(
            said_above_the_list(html), ["« Gin exemple » ajouté à la liste (6 bouteilles de 70 cl · 1 colis de 6)."]
        )
        gin = ShoppingListItem.objects.get(label="Gin exemple")
        self.assertEqual(sizes_of(gin), ("6", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))
        row = row_of(table_of(html, "à acheter"), "Gin exemple")
        self.assertIn((self.page_of(self.wholesaler), "Dans la liste (6 bouteilles de 70 cl)"), links_of(row))
        # The fixture's: the syrup's bottle of 1 L, the beer's piece (a bare number).
        self.post(ADD, self.line_form(html, "à acheter", "Sirop exemple"))
        self.post(ADD, self.line_form(html, "à acheter", "Bière exemple"))
        self.assertEqual(
            sizes_of(ShoppingListItem.objects.get(label="Sirop exemple")), ("2", "", SYRUP_PRODUCT, None, "1", "L")
        )
        beer = ShoppingListItem.objects.get(label="Bière exemple")
        self.assertEqual(sizes_of(beer), ("24", "", SHOPPING_BEER_PRODUCT, 24, "1", "UNIT"))
        self.assertEqual(words_of(beer), "24")

    def test_a_product_of_another_store_is_refused(self):
        """The line names the store's product: one of the same article bought
        at another store is no product of this list - refused, nothing
        written."""
        elsewhere = make_product(supplier=self.grocer, raw_name="BIERE EXEMPLE AUTRE", stock_type=self.made.beer)
        beer = self.line_form(self.html(FORECAST, fournisseur=self.wholesaler.pk), "à acheter", "Bière exemple")
        before = row_counts()
        response = self.post(ADD, {**beer, "produit": str(elsewhere.pk)})
        self.assertEqual(self.landing(response), self.forecast_of(self.wholesaler, "a-acheter"))
        self.assertEqual(said_above_the_list(response.content.decode()), [PRODUCT_NOT_FOUND])
        self.assertEqual(row_counts(), before)

    def test_unite_is_not_read(self):
        """The line's number counts its product, or its article's measure
        with none: a `unite` posted with it changes nothing."""
        beer = self.line_form(self.html(FORECAST, fournisseur=self.wholesaler.pk), "à acheter", "Bière exemple")
        for unite in ("L", "KG", "abc"):
            with self.subTest(unite=unite):
                ShoppingListItem.objects.all().delete()
                response = self.post(ADD, {**beer, "unite": unite})
                self.assertEqual(
                    said_above_the_list(response.content.decode()),
                    ["« Bière exemple » ajouté à la liste (24 · 1 colis de 24)."],
                )
                self.assertEqual(
                    open_items(self.wholesaler), [("Bière exemple", "24", "", SHOPPING_BEER_PRODUCT, 24, "", False)]
                )

    def test_what_the_line_cannot_carry_is_refused(self):
        html = self.html(FORECAST, fournisseur=self.wholesaler.pk)
        beer = self.line_form(html, "à acheter", "Bière exemple")
        syrup_product = Product.objects.get(raw_name=SYRUP_PRODUCT)
        for changed, said in (
            ({"produit": str(syrup_product.pk)}, [PRODUCT_NOT_FOUND]),
            ({"produit": "abc"}, [PRODUCT_NOT_FOUND]),
            ({"produit": "999999"}, [PRODUCT_NOT_FOUND]),
            ({"article": "999999"}, [ARTICLE_NOT_FOUND]),
            ({"article": "\N{SUPERSCRIPT TWO}"}, [ARTICLE_NOT_FOUND]),
            ({"quantite": "0"}, [QUANTITY_REFUSED]),
            ({"quantite": ""}, [QUANTITY_REFUSED]),
            ({"quantite": "0", "produit": "abc"}, [PRODUCT_NOT_FOUND, QUANTITY_REFUSED]),
        ):
            with self.subTest(changed=changed):
                before = row_counts()
                response = self.post(ADD, {**beer, **changed})
                self.assertEqual(self.landing(response), self.forecast_of(self.wholesaler, "a-acheter"))
                self.assertEqual(said_above_the_list(response.content.decode()), said)
                self.assertEqual(row_counts(), before)

    def test_a_pack_that_cannot_be_read_is_left_out(self):
        beer = self.line_form(self.html(FORECAST, fournisseur=self.wholesaler.pk), "à acheter", "Bière exemple")
        for colis in ("", "abc", "1", "0", "2.5", "10000", "\N{SUPERSCRIPT TWO}"):
            with self.subTest(colis=colis):
                ShoppingListItem.objects.all().delete()
                self.post(ADD, {**beer, "colis": colis})
                self.assertEqual(
                    open_items(self.wholesaler), [("Bière exemple", "24", "", SHOPPING_BEER_PRODUCT, None, "", False)]
                )

    def test_with_no_product_the_quantity_counts_the_article(self):
        beer = self.line_form(self.html(FORECAST, fournisseur=self.wholesaler.pk), "à acheter", "Bière exemple")
        self.post(ADD, {**beer, "produit": ""})
        self.assertEqual(open_items(self.wholesaler), [("Bière exemple", "24", UnitChoices.UNIT, "", None, "", False)])

    def test_a_ticked_line_is_put_back_to_buy(self):
        """The beer added, then ticked as bought on a list nobody finished: no
        longer « Dans la liste », its line offers « Ajouter » again with
        « Pris (24) » beside it, and posting it puts the very item back to
        buy with the figures posted, said « remis dans la liste »."""
        self.post(
            ADD, self.line_form(self.html(FORECAST, fournisseur=self.wholesaler.pk), "à acheter", "Bière exemple")
        )
        beer = ShoppingListItem.objects.get()
        set_ticked(beer.pk, True, "lucie-exemple@example.invalid", timezone.now())
        html = self.html(FORECAST, fournisseur=self.wholesaler.pk)
        row = row_of(table_of(html, "à acheter"), "Bière exemple")
        self.assertNotIn("Dans la liste", row)
        self.assertIn('<span class="muted small">Pris (24)</span>', row)
        response = self.post(ADD, {**self.line_form(html, "à acheter", "Bière exemple"), "quantite": "48"})
        self.assertEqual(self.landing(response), self.forecast_of(self.wholesaler, "a-acheter"))
        html = response.content.decode()
        self.assertEqual(said_above_the_list(html), ["« Bière exemple » remis dans la liste (48 · 2 colis de 24)."])
        self.assertEqual(said_at_the_top(html), [])
        self.assertEqual(
            open_items(self.wholesaler), [("Bière exemple", "48", "", SHOPPING_BEER_PRODUCT, 24, "", False)]
        )
        self.assertEqual(ShoppingListItem.objects.get().pk, beer.pk)
        row = row_of(table_of(html, "à acheter"), "Bière exemple")
        self.assertIn((self.page_of(self.wholesaler), "Dans la liste (48)"), links_of(row))

    def test_already_listed_is_said_where_the_line_is(self):
        html = self.html(FORECAST, fournisseur=self.wholesaler.pk)
        olives = self.line_form(html, "peut-être", "Olives exemple")
        self.post(ADD, olives)
        response = self.post(ADD, {**olives, "quantite": "5"})
        fold = fold_of(response.content.decode(), "peut-etre")
        self.assertEqual(said_in(fold), ["« Olives exemple » est déjà dans la liste (1 paquet de 1 kg)."])
        self.assertIn('<li class="message message-warning">', fold)
        self.assertEqual(open_items(self.wholesaler)[0][1], "1")

    def test_a_store_that_cannot_be_read(self):
        beer = self.line_form(self.html(FORECAST, fournisseur=self.wholesaler.pk), "à acheter", "Bière exemple")
        for asked in ("abc", "999999", ""):
            with self.subTest(fournisseur=asked):
                response = self.post(ADD, {**beer, "fournisseur": asked})
                self.assertEqual(self.landing(response), f"{reverse(FORECAST)}#a-acheter")
                self.assertEqual(said_above_the_list(response.content.decode()), [STORE_NOT_FOUND_ADD])
        self.assertFalse(ShoppingList.objects.exists())


class AddAllTests(ListPageTestCase):
    """« Tout ajouter (N) »: every line of « À acheter » not on the list yet,
    with the figures the page shows."""

    WITH_LISTS = False

    def add_all_form(self, **query) -> dict:
        (form,) = forms_to(self.html(FORECAST, fournisseur=self.wholesaler.pk, **query), ADD_ALL)
        return hidden_of(form)

    def test_every_line_to_buy(self):
        hidden = self.add_all_form()
        self.assertEqual(hidden, {"fournisseur": str(self.wholesaler.pk)})
        response = self.post(ADD_ALL, hidden)
        self.assertEqual(self.landing(response), self.forecast_of(self.wholesaler, "a-acheter"))
        html = response.content.decode()
        self.assertEqual(said_above_the_list(html), ["2 articles ajoutés à la liste."])
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Bière exemple", "24", "", SHOPPING_BEER_PRODUCT, 24, "", False),
                ("Sirop exemple", "2", "", SYRUP_PRODUCT, None, "", False),
            ],
        )
        self.assertEqual(forms_to(html, ADD_ALL), [])
        self.assertEqual(ShoppingList.objects.get().created_by, TEST_EMAIL)

    def test_a_typed_horizon_multiplies_as_the_page_does(self):
        hidden = self.add_all_form(dans="60")
        self.assertEqual(hidden, {"fournisseur": str(self.wholesaler.pk), "dans": "60"})
        response = self.post(ADD_ALL, hidden)
        self.assertEqual(self.landing(response), self.forecast_of(self.wholesaler, "a-acheter", dans=60))
        times = math.floor(60 / 7 + 0.5)
        self.assertEqual(
            [item[:5] for item in open_items(self.wholesaler)],
            [
                ("Bière exemple", str(24 * times), "", SHOPPING_BEER_PRODUCT, 24),
                ("Sirop exemple", str(2 * times), "", SYRUP_PRODUCT, None),
            ],
        )

    def test_the_lines_already_there_are_counted(self):
        hidden = self.add_all_form()
        self.add(self.wholesaler, nom="Sirop exemple", quantite="5")
        response = self.post(ADD_ALL, hidden)
        self.assertEqual(
            said_above_the_list(response.content.decode()), ["1 article ajouté à la liste, 1 y était déjà."]
        )
        self.assertEqual(
            [item[:2] for item in open_items(self.wholesaler)], [("Sirop exemple", "5"), ("Bière exemple", "24")]
        )

    def test_a_ticked_item_is_put_back_and_counted_as_added(self):
        """The beer bought on a trip whose list nobody finished: ticked, it is
        no longer in the list - « Tout ajouter » counts it, puts it back to
        buy and says it was added, never « y était déjà »."""
        self.add(self.wholesaler, nom="Bière exemple")
        set_ticked(ShoppingListItem.objects.get().pk, True, "lucie-exemple@example.invalid", timezone.now())
        (form,) = forms_to(self.html(FORECAST, fournisseur=self.wholesaler.pk), ADD_ALL)
        self.assertEqual(busy_button_of(form), ("Ajout…", "Tout ajouter (2)"))
        response = self.post(ADD_ALL, hidden_of(form))
        self.assertEqual(said_above_the_list(response.content.decode()), ["2 articles ajoutés à la liste."])
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Bière exemple", "24", "", SHOPPING_BEER_PRODUCT, 24, "", False),
                ("Sirop exemple", "2", "", SYRUP_PRODUCT, None, "", False),
            ],
        )
        (beer,) = [tick for tick in ticks_of(run_block(self.run_page())) if tick["name"] == "Bière exemple"]
        self.assertEqual((beer["ticked"], beer["pressed"], hidden_of(beer["form"])["pris"]), (False, "false", "1"))

    def test_a_line_too_wide_is_left_out_and_said(self):
        """A forecast figure wider than the column (a misread purchase): that
        line is not added, the others are, the message counts it - and every
        page still draws."""
        real = views.line_figures

        def figures(line, sizes=None):
            found = real(line, sizes)
            if line.article_id != self.made.beer.pk:
                return found
            return Figures(Decimal("10000000"), found.unit, found.product_id, found.product_name, found.pack_size)

        hidden = self.add_all_form()
        with patch("inventory.views.line_figures", side_effect=figures):
            response = self.post(ADD_ALL, hidden)
        self.assertEqual(
            said_above_the_list(response.content.decode()),
            ["1 article ajouté à la liste. 1 non ajouté : quantité trop grande."],
        )
        self.assertEqual([item[:2] for item in open_items(self.wholesaler)], [("Sirop exemple", "2")])
        self.html(fournisseur=self.wholesaler.pk)
        self.run_page()
        self.html(FORECAST, fournisseur=self.wholesaler.pk)

    def test_the_sizes_reach_the_list(self):
        """Each line counting the store's product keeps the size of one item,
        as « Ajouter » does: the syrup's bottles of 1 L read so on the list;
        the beer's pieces of 1 stay a bare number."""
        response = self.post(ADD_ALL, self.add_all_form())
        self.assertEqual(said_above_the_list(response.content.decode()), ["2 articles ajoutés à la liste."])
        beer, syrup = (ShoppingListItem.objects.get(label=name) for name in ("Bière exemple", "Sirop exemple"))
        self.assertEqual(sizes_of(beer), ("24", "", SHOPPING_BEER_PRODUCT, 24, "1", "UNIT"))
        self.assertEqual(sizes_of(syrup), ("2", "", SYRUP_PRODUCT, None, "1", "L"))
        rows = body_rows(table_of(self.html(fournisseur=self.wholesaler.pk), "articles"))
        self.assertEqual([cells_of(row)[2] for row in rows], ["24", "2 bouteilles de 1 L"])

    def test_a_second_submit_adds_nothing(self):
        hidden = self.add_all_form()
        self.post(ADD_ALL, hidden)
        before = row_counts()
        response = self.post(ADD_ALL, hidden)
        self.assertEqual(said_above_the_list(response.content.decode()), [NOTHING_TO_ADD])
        self.assertEqual(row_counts(), before)

    def test_a_store_that_cannot_be_read(self):
        charges = make_supplier(name="Assurance exemple", expenses_only=True)
        for asked in ("", "abc", "999999", "\N{SUPERSCRIPT TWO}", str(charges.pk)):
            with self.subTest(fournisseur=asked):
                response = self.post(ADD_ALL, {"fournisseur": asked})
                self.assertEqual(self.landing(response), f"{reverse(FORECAST)}#a-acheter")
                self.assertEqual(said_above_the_list(response.content.decode()), [STORE_NOT_FOUND_ADD])
        self.assertFalse(ShoppingList.objects.exists())


class EditDeleteTests(ListPageTestCase):
    """« Modifier » (the card) and « Retirer »."""

    WITH_BOTTLES = True

    def gin_in_bottles(self) -> ShoppingListItem:
        """The gin added from the list page as drawn: six bottles of 70 cl."""
        self.add(self.wholesaler, nom="Gin exemple (article)")
        gin = ShoppingListItem.objects.get(label="Gin exemple")
        self.assertEqual(sizes_of(gin), ("6", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))
        return gin

    def drawn_card(self, item) -> tuple[dict, list[tuple[str, str, bool]]]:
        """The card's form as drawn: what it posts untouched (its hidden
        fields, its quantity, its selected unit), and its unit's options."""
        form = self.card_of(item)
        options = options_of(form, "unite")
        posted = {**hidden_of(form), "quantite": value_of_field(form, "quantite"), "note": ""}
        for value, _label, selected in options:
            if selected:
                posted["unite"] = value
        return posted, options

    def card_form(self, item) -> dict:
        (form,) = forms_to(edit_card(self.html(fournisseur=self.wholesaler.pk, ligne=item.pk)), EDIT)
        return hidden_of(form)

    def delete_form(self, item) -> dict:
        table = table_of(self.html(fournisseur=self.wholesaler.pk), "articles")
        (form,) = forms_to(row_of(table, f"<td>{item.label}</td>"), DELETE)
        return hidden_of(form)

    def test_a_quantity_and_a_note_changed(self):
        response = self.post(EDIT, {**self.card_form(self.lists.beer), "quantite": "48", "note": " Note  exemple "})
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler))
        html = response.content.decode()
        self.assertEqual(said_at_the_top(html), ["Modifié : « Bière exemple » (48 · 2 colis de 24)."])
        self.assertEqual(edit_card(html), "")
        self.assertEqual(
            figures_of(self.lists.beer), ("Bière exemple", "48", "", SHOPPING_BEER_PRODUCT, 24, "Note exemple", False)
        )
        # The note emptied; the syrup in its own unit.
        self.post(EDIT, {**self.card_form(self.lists.beer), "quantite": "48", "note": ""})
        self.assertEqual(figures_of(self.lists.beer)[5], "")
        response = self.post(EDIT, {**self.card_form(self.lists.syrup), "quantite": "1,5", "note": ""})
        self.assertEqual(said_at_the_top(response.content.decode()), ["Modifié : « Sirop exemple » (1.5 L)."])
        # Its tick is kept.
        self.assertEqual(figures_of(self.lists.syrup)[1:], ("1.5", UnitChoices.LITRE, "", None, "", True))

    def test_each_refusal_keeps_the_card_and_writes_nothing(self):
        hidden = self.card_form(self.lists.beer)
        for typed, said in (
            ({"quantite": "0"}, [QUANTITY_REFUSED]),
            ({"quantite": ""}, [QUANTITY_REFUSED]),
            ({"quantite": "abc"}, [QUANTITY_REFUSED]),
            ({"quantite": "1,2345"}, [QUANTITY_REFUSED]),
            ({"quantite": "24", "note": "n" * 201}, [NOTE_TOO_LONG]),
            ({"quantite": "-2", "note": "n" * 201}, [QUANTITY_REFUSED, NOTE_TOO_LONG]),
        ):
            with self.subTest(typed={key: value[:10] for key, value in typed.items()}):
                before = everything_listed()
                response = self.post(EDIT, {**hidden, "note": "", **typed})
                self.assertEqual(
                    self.landing(response), self.page_of(self.wholesaler, "modifier", ligne=self.lists.beer.pk)
                )
                html = response.content.decode()
                card = edit_card(html)
                self.assertTrue(card)
                self.assertEqual(said_in(card), said)
                self.assertEqual(said_at_the_top(html), [])
                self.assertEqual(everything_listed(), before)

    def test_an_item_gone_a_list_finished_another_store(self):
        hidden = self.card_form(self.lists.bread)
        self.lists.bread.delete()
        for data, landing, said in (
            ({**hidden, "quantite": "3"}, self.page_of(self.wholesaler), NOT_IN_LIST),
            (
                {"fournisseur": self.wholesaler.pk, "ligne": self.lists.lemon.pk, "quantite": "3"},
                self.page_of(self.wholesaler),
                NOT_IN_LIST,
            ),
            (
                {"fournisseur": self.grocer.pk, "ligne": self.lists.lemon.pk, "quantite": "3"},
                self.page_of(self.grocer),
                LIST_FINISHED,
            ),
            (
                {"fournisseur": "abc", "ligne": self.lists.beer.pk, "quantite": "3"},
                reverse(INDEX),
                STORE_NOT_FOUND_CHANGE,
            ),
        ):
            with self.subTest(said=said, data=data):
                before = everything_listed()
                response = self.post(EDIT, data)
                self.assertEqual(self.landing(response), landing)
                self.assertEqual(said_at_the_top(response.content.decode()), [said])
                self.assertEqual(everything_listed(), before)

    def test_twice_is_the_same(self):
        hidden = self.card_form(self.lists.bread)
        for _ in range(2):
            self.post(EDIT, {**hidden, "quantite": "4", "note": "Complet"})
        self.assertEqual(figures_of(self.lists.bread), ("Pain exemple", "4", "", "", None, "Complet", False))

    def test_removed(self):
        hidden = self.delete_form(self.lists.bread)
        response = self.post(DELETE, hidden)
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler))
        self.assertEqual(said_at_the_top(response.content.decode()), ["« Pain exemple » retiré de la liste."])
        self.assertFalse(ShoppingListItem.objects.filter(pk=self.lists.bread.pk).exists())
        # Twice: gone already.
        response = self.post(DELETE, hidden)
        self.assertEqual(said_at_the_top(response.content.decode()), [NOT_IN_LIST])
        self.assertEqual([item[0] for item in open_items(self.wholesaler)], ["Bière exemple", "Sirop exemple"])

    def test_a_list_emptied_by_retirer_leaves_en_cours_and_starts_again_with_its_next_item(self):
        """Every item removed: the list is no longer in progress. The next
        item added starts it again - « Commencée le » is that day, never the
        day of a first item long gone."""
        table = table_of(self.html(fournisseur=self.wholesaler.pk), "articles")
        for form in forms_to(table, DELETE):
            self.post(DELETE, hidden_of(form))
        index = self.html(INDEX)
        self.assertIn('<p class="muted">Aucune liste en cours.</p>', index)
        self.assertEqual(table_of(index, "listes en cours"), "")
        ShoppingList.objects.filter(pk=self.lists.open.pk).update(
            created_at=timezone.now() - timedelta(days=21), created_by="ancien-exemple@example.invalid"
        )
        self.add(self.wholesaler, nom="Pain exemple")
        (row,) = body_rows(table_of(self.html(INDEX), "listes en cours"))
        self.assertEqual(cells_of(row)[:3], ["Grossiste exemple", "0 / 1", f"{timezone.localdate():%d/%m/%Y}"])
        self.lists.open.refresh_from_db()
        self.assertEqual(self.lists.open.created_by, TEST_EMAIL)

    def test_an_edit_while_another_phone_finishes_the_list(self):
        """The card posted as another phone presses « Courses terminées »:
        nothing written on the finished list - read-only -, nor on the copy
        carried over; said « terminée »."""
        hidden = self.card_form(self.lists.bread)
        with patch("inventory.views._item_of", side_effect=read_then(finish_its_list)):
            response = self.post(EDIT, {**hidden, "quantite": "9", "note": "Autre"})
        self.assertEqual(said_at_the_top(response.content.decode()), [LIST_FINISHED])
        self.assertEqual(figures_of(self.lists.bread), ("Pain exemple", "2", "", "", None, "", False))
        carried = ShoppingListItem.objects.get(shopping_list__finished_at__isnull=True, label="Pain exemple")
        self.assertEqual(figures_of(carried), ("Pain exemple", "2", "", "", None, "", False))

    def test_the_unit_changed(self):
        """The card's select: posted as drawn, only the number changes; then
        bottles to litres - the number never converted, the product's name
        kept, its packs and size gone -, and litres back to the bottles of
        the usual product here. A unit the card does not offer, or any for a
        free text, is refused in the card, nothing written."""
        gin = self.gin_in_bottles()
        posted, options = self.drawn_card(gin)
        self.assertEqual(options, [("UNIT", "bouteilles de 70 cl", True), ("L", "litres", False)])
        response = self.post(EDIT, {**posted, "quantite": "12"})
        self.assertEqual(
            said_at_the_top(response.content.decode()),
            ["Modifié : « Gin exemple » (12 bouteilles de 70 cl · 2 colis de 6)."],
        )
        self.assertEqual(sizes_of(gin), ("12", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))
        # Bottles to litres: what is typed counts litres.
        posted, _options = self.drawn_card(gin)
        response = self.post(EDIT, {**posted, "quantite": "4.2", "unite": "L"})
        self.assertEqual(said_at_the_top(response.content.decode()), ["Modifié : « Gin exemple » (4.2 L)."])
        self.assertEqual(sizes_of(gin), ("4.2", "L", SHOPPING_GIN_PRODUCT, None, None, ""))
        self.assertEqual(
            cells_of(row_of(table_of(self.html(fournisseur=self.wholesaler.pk), "articles"), "Gin exemple"))[1:3],
            [SHOPPING_GIN_PRODUCT, "4.2 L"],
        )
        # Litres to bottles: those of the usual product here.
        posted, options = self.drawn_card(gin)
        self.assertEqual(options, [("UNIT", "bouteilles de 70 cl", False), ("L", "litres", True)])
        response = self.post(EDIT, {**posted, "quantite": "6", "unite": "UNIT"})
        self.assertEqual(
            said_at_the_top(response.content.decode()),
            ["Modifié : « Gin exemple » (6 bouteilles de 70 cl · 1 colis de 6)."],
        )
        self.assertEqual(sizes_of(gin), ("6", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))
        # Refused in the card, nothing written.
        bread, _options = self.drawn_card(self.lists.bread)
        self.assertNotIn("unite", bread)
        for item, data in (
            (gin, {**self.drawn_card(gin)[0], "unite": "KG"}),
            (gin, {**self.drawn_card(gin)[0], "unite": "abc"}),
            (self.lists.beer, {**self.drawn_card(self.lists.beer)[0], "unite": "L"}),
            (self.lists.bread, {**bread, "unite": "L"}),
            (self.lists.bread, {**bread, "unite": "UNIT"}),
        ):
            with self.subTest(item=item.label, unite=data["unite"]):
                before = everything_listed()
                sizes = sizes_of(item)
                response = self.post(EDIT, {**data, "quantite": "9"})
                self.assertEqual(self.landing(response), self.page_of(self.wholesaler, "modifier", ligne=item.pk))
                html = response.content.decode()
                self.assertEqual(said_in(edit_card(html)), [UNIT_REFUSED])
                self.assertEqual(said_at_the_top(html), [])
                self.assertEqual(everything_listed(), before)
                self.assertEqual(sizes_of(item), sizes)
        # Said with the card's other refusals, in its order.
        response = self.post(EDIT, {**self.drawn_card(gin)[0], "quantite": "0", "unite": "KG", "note": "n" * 201})
        self.assertEqual(said_in(edit_card(response.content.decode())), [QUANTITY_REFUSED, UNIT_REFUSED, NOTE_TOO_LONG])

    def test_the_card_s_items_option_says_what_saving_it_stores(self):
        """A product of the store that is not the usual one, « GIN EXEMPLE
        1L », added in litres: the card's items option is that product's
        bottles, found again at the store with their size - never the usual
        product's 70 cl -, and every option the card draws, posted as drawn,
        stores what its label says. The product gone from the store, the
        option is a bare number of it, « unités », and stores one."""
        gin_litre = make_product(self.wholesaler, "GIN EXEMPLE 1L", self.bottles.gin, unit=UnitChoices.LITRE)
        line = make_invoice_line(
            invoice=make_invoice(supplier=self.wholesaler, invoice_date=timezone.localdate() - timedelta(days=10)),
            product=gin_litre,
            quantity=Decimal("6"),
            total_ht="90.00",
            total_volume="6",
        )
        make_movement(stock_type=self.bottles.gin, quantity="6", unit_cost_ht="15", invoice_line=line)
        self.add(self.wholesaler, nom=f"GIN EXEMPLE 1L{AT_THE_WHOLESALER}", quantite="2", unite="L")
        gin = ShoppingListItem.objects.get(label="Gin exemple")
        self.assertEqual(sizes_of(gin), ("2", "L", "GIN EXEMPLE 1L", None, None, ""))
        posted, options = self.drawn_card(gin)
        self.assertEqual(options, [("UNIT", "bouteilles de 1 L", False), ("L", "litres", True)])
        for value, label, _selected in options:
            with self.subTest(unite=value):
                response = self.post(EDIT, {**self.drawn_card(gin)[0], "quantite": "3", "unite": value})
                words = "3 L" if value == "L" else f"3 {label}"
                self.assertEqual(said_at_the_top(response.content.decode()), [f"Modifié : « Gin exemple » ({words})."])
                self.assertEqual(words_of(gin), words)
                if value == "UNIT":
                    self.assertEqual(sizes_of(gin), ("3", "", "GIN EXEMPLE 1L", None, "1", "L"))
        self.assertEqual(sizes_of(gin), ("3", "L", "GIN EXEMPLE 1L", None, None, ""))
        # The product no longer at the store under that name: a bare number of it.
        Product.objects.filter(pk=gin_litre.pk).update(raw_name="GIN EXEMPLE 1L ANCIEN")
        posted, options = self.drawn_card(gin)
        self.assertEqual(options, [("UNIT", "unités", False), ("L", "litres", True)])
        response = self.post(EDIT, {**posted, "quantite": "3", "unite": "UNIT"})
        self.assertEqual(said_at_the_top(response.content.decode()), ["Modifié : « Gin exemple » (3)."])
        self.assertEqual(sizes_of(gin), ("3", "", "GIN EXEMPLE 1L", None, None, ""))

    def test_a_unit_change_while_another_phone_finishes_the_list(self):
        """Bottles changed to litres as another phone presses « Courses
        terminées »: nothing written on the finished list - read-only -, nor
        on the copy carried over, which keeps its bottles; said « terminée »."""
        gin = self.gin_in_bottles()
        posted, _options = self.drawn_card(gin)
        with patch("inventory.views._item_of", side_effect=read_then(finish_its_list)):
            response = self.post(EDIT, {**posted, "quantite": "4.2", "unite": "L"})
        self.assertEqual(said_at_the_top(response.content.decode()), [LIST_FINISHED])
        self.assertEqual(sizes_of(gin), ("6", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))
        carried = ShoppingListItem.objects.get(shopping_list__finished_at__isnull=True, label="Gin exemple")
        self.assertEqual(sizes_of(carried), ("6", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))

    def test_an_edit_while_another_phone_removes_the_item(self):
        hidden = self.card_form(self.lists.bread)
        with patch("inventory.views._item_of", side_effect=read_then(remove_it)):
            response = self.post(EDIT, {**hidden, "quantite": "9"})
        self.assertEqual(said_at_the_top(response.content.decode()), [NOT_IN_LIST])
        self.assertFalse(ShoppingListItem.objects.filter(label="Pain exemple").exists())

    def test_a_removal_while_another_phone_removes_the_item(self):
        hidden = self.delete_form(self.lists.bread)
        with patch("inventory.views._item_of", side_effect=read_then(remove_it)):
            response = self.post(DELETE, hidden)
        self.assertEqual(said_at_the_top(response.content.decode()), [NOT_IN_LIST])

    def test_a_removal_while_another_phone_finishes_the_list(self):
        hidden = self.delete_form(self.lists.bread)
        with patch("inventory.views._item_of", side_effect=read_then(finish_its_list)):
            response = self.post(DELETE, hidden)
        self.assertEqual(said_at_the_top(response.content.decode()), [LIST_FINISHED])
        self.assertTrue(ShoppingListItem.objects.filter(pk=self.lists.bread.pk).exists())

    def test_nothing_is_removed_from_a_finished_list(self):
        for data, said in (
            ({"fournisseur": self.grocer.pk, "ligne": self.lists.lemon.pk}, LIST_FINISHED),
            ({"fournisseur": self.wholesaler.pk, "ligne": self.lists.lemon.pk}, NOT_IN_LIST),
            ({"fournisseur": self.wholesaler.pk, "ligne": "abc"}, NOT_IN_LIST),
            ({"fournisseur": "", "ligne": self.lists.beer.pk}, STORE_NOT_FOUND_CHANGE),
        ):
            with self.subTest(said=said):
                before = everything_listed()
                response = self.post(DELETE, data)
                self.assertEqual(said_at_the_top(response.content.decode()), [said])
                self.assertEqual(everything_listed(), before)


class TickTests(ListPageTestCase):
    """A tick names the state WANTED (`pris`): bought or not, never a toggle."""

    def employee_client(self):
        client = self.client_class()
        client.force_login(
            employee_of_the_test_tenant("lucie-exemple@example.invalid", ("products",), name="Lucie Exemple")
        )
        return client

    def test_ticked_then_not_as_wanted(self):
        hidden = self.tick_form(self.lists.beer)
        self.assertEqual(
            hidden, {"ligne": str(self.lists.beer.pk), "fournisseur": str(self.wholesaler.pk), "pris": "1"}
        )
        response = self.post(TICK, hidden)
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler, "courses", mode="courses"))
        self.lists.beer.refresh_from_db()
        first = self.lists.beer.checked_at
        self.assertIsNotNone(first)
        self.assertEqual(self.lists.beer.checked_by, TEST_EMAIL)
        self.assertEqual(notices_of(run_block(response.content.decode())), [])
        # Ticked again from a page drawn before: the first tick's when kept.
        self.post(TICK, hidden)
        self.lists.beer.refresh_from_db()
        self.assertEqual(self.lists.beer.checked_at, first)
        unticked = self.tick_form(self.lists.beer)
        self.assertEqual(unticked["pris"], "0")
        self.post(TICK, unticked)
        self.post(TICK, unticked)
        self.lists.beer.refresh_from_db()
        self.assertEqual((self.lists.beer.checked_at, self.lists.beer.checked_by), (None, ""))

    def test_two_phones(self):
        """Each from a page drawn before the other's tick: both land, and the
        same item ticked twice keeps who ticked it first."""
        other = self.employee_client()
        mine = {name: self.tick_form(item) for name, item in (("beer", self.lists.beer), ("bread", self.lists.bread))}
        theirs = {
            name: self.tick_form(item, client=other)
            for name, item in (("beer", self.lists.beer), ("bread", self.lists.bread))
        }
        self.post(TICK, mine["beer"])
        self.post(TICK, theirs["bread"], client=other)
        self.post(TICK, theirs["beer"], client=other)
        for item, who in ((self.lists.beer, TEST_EMAIL), (self.lists.bread, "lucie-exemple@example.invalid")):
            with self.subTest(item=item.label):
                item.refresh_from_db()
                self.assertIsNotNone(item.checked_at)
                self.assertEqual(item.checked_by, who)

    def test_htmx_answers_the_block_alone(self):
        response = self.client.post(reverse(TICK), self.tick_form(self.lists.beer), **HTMX)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "an htmx tick")
        html = response.content.decode()
        self.assertTrue(html.lstrip().startswith('<div class="shopping-run" id="courses">'), html[:80])
        self.assertNotIn("<html", html)
        self.assertEqual(run_block(html), html.strip())
        block = run_block(html)
        self.assertEqual(
            [(tick["name"], tick["ticked"]) for tick in ticks_of(block)],
            [("Pain exemple", False), ("Bière exemple", True), ("Sirop exemple", True)],
        )
        self.assertIn("<strong>2 / 3</strong> pris", block)
        self.assertEqual(notices_of(block), [])
        # It names the list it shows for « Courses terminées », out of band:
        # inside the block, so htmx lifts it out and swaps the finish form's.
        self.assertEqual(finish_list_of(block), str(self.lists.open.pk))
        # Nothing kept for the next page.
        self.assertEqual(said_at_the_top(self.run_page()), [])

    def test_htmx_says_each_refusal_in_the_block(self):
        hidden = self.tick_form(self.lists.bread)
        for data, notice in (
            ({**hidden, "ligne": "999999"}, NOT_IN_LIST),
            ({**hidden, "ligne": str(self.lists.lemon.pk)}, NOT_IN_LIST),
            ({**hidden, "pris": "2"}, TICK_UNREADABLE),
            ({**hidden, "pris": ""}, TICK_UNREADABLE),
            ({**hidden, "pris": "oui"}, TICK_UNREADABLE),
            ({"fournisseur": str(self.grocer.pk), "ligne": str(self.lists.coffee.pk), "pris": "1"}, LIST_FINISHED),
        ):
            with self.subTest(notice=notice, data=data):
                before = everything_listed()
                response = self.client.post(reverse(TICK), data, **HTMX)
                self.assertEqual(response.status_code, 200)
                block = run_block(response.content.decode())
                self.assertEqual(notices_of(block), [notice])
                self.assertEqual(everything_listed(), before)
        self.assertEqual(said_at_the_top(self.run_page()), [])

    def test_htmx_after_another_phone_finished(self):
        """The block is the store's list as it stands: the one carried over."""
        hidden = self.tick_form(self.lists.bread)
        self.post(FINISH, {"liste": self.lists.open.pk, "garder": "1"})
        response = self.client.post(reverse(TICK), hidden, **HTMX)
        block = run_block(response.content.decode())
        self.assertEqual(notices_of(block), [LIST_FINISHED])
        self.assertEqual(
            [(tick["name"], tick["ticked"]) for tick in ticks_of(block)],
            [("Bière exemple", False), ("Pain exemple", False)],
        )
        carried = ShoppingList.objects.get(supplier=self.wholesaler, finished_at__isnull=True)
        self.assertIn(f'name="ligne" value="{carried.items.get(label="Pain exemple").pk}"', block)
        self.assertEqual(finish_list_of(response.content.decode()), str(carried.pk))

    def test_a_tick_while_another_phone_removes_the_item(self):
        hidden = self.tick_form(self.lists.bread)
        with patch("inventory.views._item_of", side_effect=read_then(remove_it)):
            response = self.client.post(reverse(TICK), hidden, **HTMX)
        self.assertEqual(notices_of(run_block(response.content.decode())), [NOT_IN_LIST])

    def test_a_tick_while_another_phone_finishes_the_list(self):
        """Nothing ticked on the finished list - read-only -, said
        « terminée »."""
        hidden = self.tick_form(self.lists.bread)
        with patch("inventory.views._item_of", side_effect=read_then(finish_its_list)):
            response = self.client.post(reverse(TICK), hidden, **HTMX)
        self.assertEqual(notices_of(run_block(response.content.decode())), [LIST_FINISHED])
        self.lists.bread.refresh_from_db()
        self.assertIsNone(self.lists.bread.checked_at)

    def test_htmx_with_a_store_that_cannot_be_read(self):
        before = everything_listed()
        response = self.client.post(
            reverse(TICK), {"fournisseur": "abc", "ligne": self.lists.beer.pk, "pris": "1"}, **HTMX
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], reverse(INDEX))
        self.assertEqual(everything_listed(), before)

    def test_without_javascript_a_refusal_is_said_in_the_block(self):
        before = everything_listed()
        response = self.post(TICK, {**self.tick_form(self.lists.bread), "pris": "x"})
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler, "courses", mode="courses"))
        html = response.content.decode()
        self.assertEqual(notices_of(run_block(html)), [TICK_UNREADABLE])
        self.assertEqual(said_at_the_top(html), [])
        self.assertEqual(everything_listed(), before)
        response = self.post(TICK, {"fournisseur": "abc", "ligne": self.lists.beer.pk, "pris": "1"})
        self.assertEqual(self.landing(response), reverse(INDEX))
        self.assertEqual(said_at_the_top(response.content.decode()), [STORE_NOT_FOUND_CHANGE])


class FinishTests(ListPageTestCase):
    """« Courses terminées »: the list closed once, what was not bought kept
    for the next one when asked."""

    def finish_form(self) -> str:
        (form,) = forms_to(self.run_page(), FINISH)
        return form

    def test_the_form(self):
        form = self.finish_form()
        self.assertEqual(hidden_of(form), {"liste": str(self.lists.open.pk)})
        # Named, the list it posts too: what a tick swaps in out of band.
        self.assertIn('class="shopping-finish" id="shopping-finish"', form)
        self.assertIn(
            f'<input type="hidden" name="liste" value="{self.lists.open.pk}" id="shopping-finish-list">', form
        )
        self.assertIn(
            '<label class="checkbox-label"><input type="checkbox" name="garder" value="1" checked> Garder les '
            "articles non pris pour la prochaine liste</label>",
            form,
        )
        self.assertEqual(confirm_of(form), "Terminer ces courses ?")
        self.assertEqual(busy_button_of(form), ("Enregistrement…", "Courses terminées"))

    def test_finished_and_the_rest_kept(self):
        response = self.post(FINISH, {**hidden_of(self.finish_form()), "garder": "1"})
        self.assertEqual(self.landing(response), reverse(INDEX))
        html = response.content.decode()
        self.assertEqual(
            said_at_the_top(html),
            ["Courses terminées chez Grossiste exemple : 1 / 3 pris. 2 gardés pour la prochaine liste."],
        )
        self.lists.open.refresh_from_db()
        self.assertIsNotNone(self.lists.open.finished_at)
        self.assertEqual(self.lists.open.finished_by, TEST_EMAIL)
        self.assertEqual(self.lists.open.items.count(), 3)
        self.assertEqual(
            open_items(self.wholesaler),
            [
                ("Bière exemple", "24", "", SHOPPING_BEER_PRODUCT, 24, "", False),
                ("Pain exemple", "2", "", "", None, "", False),
            ],
        )
        # The index then shows both: the new list, and the one just finished first.
        self.assertEqual(
            [cells_of(row)[:2] for row in body_rows(table_of(html, "listes en cours"))],
            [["Grossiste exemple", "0 / 2"]],
        )
        self.assertEqual(
            [cells_of(row)[0] for row in body_rows(table_of(html, "listes terminées"))],
            ["Grossiste exemple", "Épicerie exemple"],
        )

    def test_finished_with_nothing_kept(self):
        hidden = hidden_of(self.finish_form())
        response = self.post(FINISH, hidden)
        self.assertEqual(
            said_at_the_top(response.content.decode()), ["Courses terminées chez Grossiste exemple : 1 / 3 pris."]
        )
        self.assertFalse(ShoppingList.objects.filter(finished_at__isnull=True).exists())

    def test_one_kept(self):
        ShoppingListItem.objects.filter(pk=self.lists.beer.pk).update(checked_at=timezone.now(), checked_by=TEST_EMAIL)
        response = self.post(FINISH, {**hidden_of(self.finish_form()), "garder": "1"})
        self.assertEqual(
            said_at_the_top(response.content.decode()),
            ["Courses terminées chez Grossiste exemple : 2 / 3 pris. 1 gardé pour la prochaine liste."],
        )

    def test_twice_is_once(self):
        """A double submit carries nothing twice. With « Garder », the second
        lands on the list carried over, saying so in its block; without, on
        the lists."""
        hidden = {**hidden_of(self.finish_form()), "garder": "1"}
        self.post(FINISH, hidden)
        before = everything_listed()
        response = self.post(FINISH, hidden)
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler, "courses", mode="courses"))
        html = response.content.decode()
        self.assertEqual(notices_of(run_block(html)), [ALREADY_FINISHED_NEXT])
        self.assertEqual(said_at_the_top(html), [])
        self.assertEqual(everything_listed(), before)
        self.assertEqual(ShoppingList.objects.filter(supplier=self.wholesaler).count(), 2)

    def test_twice_with_nothing_kept(self):
        hidden = hidden_of(self.finish_form())
        self.post(FINISH, hidden)
        before = everything_listed()
        response = self.post(FINISH, hidden)
        self.assertEqual(self.landing(response), reverse(INDEX))
        self.assertEqual(said_at_the_top(response.content.decode()), [ALREADY_FINISHED])
        self.assertEqual(everything_listed(), before)

    def test_the_phone_still_ticking_finishes_the_list_carried_over(self):
        """Another phone finished the list, « Garder » ticked: this phone's
        next tick swaps in the list carried over and names it in the finish
        form, so « Courses terminées » closes the list this phone ticked -
        never the one already finished."""
        page = self.run_page()
        (form,) = forms_to(page, FINISH)
        bread = self.tick_form(self.lists.bread)
        self.post(FINISH, {"liste": self.lists.open.pk, "garder": "1"}, client=self.phone())
        carried = ShoppingList.objects.get(supplier=self.wholesaler, finished_at__isnull=True)
        # The stale tap: refused, the block swapped to the list carried over.
        block = self.client.post(reverse(TICK), bread, **HTMX).content.decode()
        self.assertEqual(notices_of(block), [LIST_FINISHED])
        self.assertEqual(finish_list_of(block), str(carried.pk))
        # The carried bread ticked from the block.
        drawn = {tick["name"]: tick for tick in ticks_of(run_block(block))}
        block = self.client.post(reverse(TICK), hidden_of(drawn["Pain exemple"]["form"]), **HTMX).content.decode()
        # « Courses terminées » as the browser holds it: its list the last swap's.
        response = self.post(FINISH, {**hidden_of(form), "liste": finish_list_of(block), "garder": "1"})
        self.assertEqual(
            said_at_the_top(response.content.decode()),
            ["Courses terminées chez Grossiste exemple : 1 / 2 pris. 1 gardé pour la prochaine liste."],
        )
        carried.refresh_from_db()
        self.assertEqual(carried.finished_by, TEST_EMAIL)

    def test_a_finish_from_a_page_drawn_before_lands_on_the_list_in_progress(self):
        """Posted as first drawn, after another phone finished the list with
        « Garder »: nothing finished, the tick page of the list carried over,
        saying so - never finished in this phone's name, which never saw it."""
        hidden = {**hidden_of(self.finish_form()), "garder": "1"}
        self.post(FINISH, {"liste": self.lists.open.pk, "garder": "1"}, client=self.phone())
        before = everything_listed()
        response = self.post(FINISH, hidden)
        self.assertEqual(self.landing(response), self.page_of(self.wholesaler, "courses", mode="courses"))
        html = response.content.decode()
        self.assertEqual(notices_of(run_block(html)), [ALREADY_FINISHED_NEXT])
        self.assertEqual(said_at_the_top(html), [])
        self.assertEqual(everything_listed(), before)
        # The list carried over emptied since: no list in progress, the lists.
        ShoppingListItem.objects.filter(shopping_list__finished_at__isnull=True).delete()
        response = self.post(FINISH, hidden)
        self.assertEqual(self.landing(response), reverse(INDEX))
        self.assertEqual(said_at_the_top(response.content.decode()), [ALREADY_FINISHED])

    def test_the_finish_form_goes_when_no_list_is_left(self):
        """Finished elsewhere with nothing kept: the block swapped in holds no
        list, and the finish form is taken out with it (out of band)."""
        bread = self.tick_form(self.lists.bread)
        self.post(FINISH, {"liste": self.lists.open.pk}, client=self.phone())
        block = self.client.post(reverse(TICK), bread, **HTMX).content.decode()
        self.assertEqual(notices_of(block), [LIST_FINISHED])
        self.assertIn('<div id="shopping-finish" hx-swap-oob="true"></div>', block)
        self.assertEqual(finish_list_of(block), "")

    def test_a_list_that_cannot_be_read(self):
        for asked in ("", "abc", "999999", "\N{SUPERSCRIPT TWO}", "1" * 30):
            with self.subTest(liste=asked):
                before = everything_listed()
                response = self.post(FINISH, {"liste": asked, "garder": "1"})
                self.assertEqual(self.landing(response), reverse(INDEX))
                self.assertEqual(said_at_the_top(response.content.decode()), [LIST_NOT_FOUND])
                self.assertEqual(everything_listed(), before)


class RunPageTests(ListPageTestCase):
    """The tick page (`?mode=courses`), phone first."""

    def test_the_page(self):
        html = self.run_page()
        self.assertIn('<div class="shopping-list-page shopping-run-page">', html)
        self.assertIn("<h1>Courses chez Grossiste exemple</h1>", html)
        self.assertNotIn("page-subtitle", html.split("<main", 1)[1].split("</main>")[0])
        header = re.search(r'<div class="page-header">.*?</h1>.*?</div>\s*</div>', html, flags=re.DOTALL).group(0)
        self.assertEqual(
            links_of(header),
            [(reverse(INDEX), "← Listes de courses"), (self.page_of(self.wholesaler), "Modifier la liste")],
        )
        self.assertEqual(forms_to(html, ADD), [])

    def test_unticked_first_then_ticked(self):
        block = run_block(self.run_page())
        self.assertIn('<p class="shopping-progress"><strong>1 / 3</strong> pris</p>', block)
        drawn = ticks_of(block)
        self.assertEqual(
            [(tick["name"], tick["quantity"], tick["under"], tick["ticked"]) for tick in drawn],
            [
                ("Bière exemple", "24", [f"{SHOPPING_BEER_PRODUCT} · 1 colis de 24"], False),
                ("Pain exemple", "2", [], False),
                ("Sirop exemple", "2 L", [], True),
            ],
        )

    def test_each_tick_form(self):
        url = reverse(TICK)
        for tick in ticks_of(run_block(self.run_page())):
            with self.subTest(item=tick["name"]):
                form = tick["form"]
                self.assertTrue(form.startswith(f'<form method="post" action="{url}"'), form[:80])
                self.assertIn(f'hx-post="{url}" hx-target="#courses" hx-swap="outerHTML"', form)
                self.assertEqual(hidden_of(form)["pris"], "0" if tick["ticked"] else "1")
                self.assertEqual(tick["pressed"], "true" if tick["ticked"] else "false")
                self.assertNotIn("data-busy-label", form)
                self.assertIn('<button type="submit" class="shopping-tick-button"', form)

    def test_an_item_in_bottles_reads_so(self):
        """In the store, on the list and once finished: the number and what
        it counts - « 3 bouteilles de 70 cl » -, the product and its packs
        under it."""
        gin = ShoppingListItem.objects.create(
            shopping_list=self.lists.open,
            stock_type=make_stock_type(name="Gin exemple", unit=UnitChoices.LITRE),
            label="Gin exemple",
            quantity=Decimal("3"),
            product_name=SHOPPING_GIN_PRODUCT,
            pack_size=6,
            item_size=Decimal("0.7"),
            size_unit=UnitChoices.LITRE,
        )
        (tick,) = [tick for tick in ticks_of(run_block(self.run_page())) if tick["name"] == "Gin exemple"]
        self.assertEqual(
            (tick["quantity"], tick["under"]),
            ("3 bouteilles de 70 cl", [f"{SHOPPING_GIN_PRODUCT} · à l'unité · colis de 6"]),
        )
        row = row_of(table_of(self.html(fournisseur=self.wholesaler.pk), "articles"), "Gin exemple")
        self.assertEqual(
            [unescape(cell) for cell in cells_of(row)[1:3]],
            [f"{SHOPPING_GIN_PRODUCT} à l'unité · colis de 6", "3 bouteilles de 70 cl"],
        )
        # Sorted by its bare number, never by its words.
        self.assertIn('data-label="Quantité" data-sort="3">3 bouteilles de 70 cl</td>', row)
        finish(self.lists.open, keep=False, by=TEST_EMAIL, now=timezone.now())
        row = row_of(table_of(self.html(liste=self.lists.open.pk), "courses terminées"), "Gin exemple")
        self.assertEqual(cells_of(row)[2], "3 bouteilles de 70 cl")
        self.assertEqual(sizes_of(gin), ("3", "", SHOPPING_GIN_PRODUCT, 6, "0.7", "L"))

    def test_a_note_is_under_its_item(self):
        ShoppingListItem.objects.filter(pk=self.lists.bread.pk).update(note="Complet")
        (bread,) = [tick for tick in ticks_of(run_block(self.run_page())) if tick["name"] == "Pain exemple"]
        self.assertEqual(bread["under"], ["Complet"])

    def test_the_finish_form_is_outside_the_block(self):
        html = self.run_page()
        block = run_block(html)
        (form,) = forms_to(html, FINISH)
        self.assertNotIn(form, block)
        self.assertGreater(html.index(form), html.index(block) + len(block) - 1)
        self.assertIn('class="shopping-finish"', form)
        # Drawn whole, the block names nothing out of band: only a tick's answer does.
        self.assertNotIn("hx-swap-oob", block)

    def test_an_empty_list(self):
        before = row_counts()
        html = self.run_page(self.market)
        self.assertEqual(row_counts(), before)
        block = run_block(html)
        found = re.search(r'<div class="empty-state">(.*?)</div>', block, flags=re.DOTALL)
        self.assertEqual(readable(found.group(1)), "Liste vide. Ajouter des articles")
        self.assertEqual(links_of(found.group(1)), [(self.page_of(self.market), "Ajouter des articles")])
        self.assertEqual(forms_to(html, FINISH), [])


class PageCostTests(ListPageTestCase):
    """The lists' page, a list - to edit or to tick - and a tick cost the
    same queries whatever the lists hold: never a query per list or item."""

    def queries(self, url, data=None, **headers) -> int:
        self.client.get(reverse(INDEX))  # the session and the login, settled once
        with CaptureQueriesContext(connection) as captured:
            response = self.client.post(url, data, **headers) if data is not None else self.client.get(url, **headers)
        self.assertIn(response.status_code, (200, 302))
        return len(captured)

    def more_items(self, shopping_list, count):
        now = timezone.now()
        for number in range(count):
            ShoppingListItem.objects.create(
                shopping_list=shopping_list,
                label=f"Article {number} exemple",
                quantity=Decimal("1"),
                added_at=now,
                checked_at=now if number % 2 else None,
            )

    def test_the_lists_page(self):
        few = self.queries(reverse(INDEX))
        now = timezone.now()
        for number in range(5):
            store = make_supplier(name=f"Magasin {number} exemple")
            self.more_items(ShoppingList.objects.create(supplier=store), 3)
            self.more_items(
                ShoppingList.objects.create(
                    supplier=store, finished_at=now, finished_by=f"login{number}@example.invalid"
                ),
                2,
            )
        self.assertEqual(self.queries(reverse(INDEX)), few)

    def test_a_list_and_a_tick(self):
        pages = (
            self.page_of(self.wholesaler),
            self.page_of(self.wholesaler, mode="courses"),
            self.page_of(self.wholesaler, ligne=self.lists.bread.pk),
            f"{reverse(LIST_PAGE)}?liste={self.lists.finished.pk}",
        )
        tick = {"fournisseur": self.wholesaler.pk, "ligne": self.lists.bread.pk, "pris": "1"}
        few = [self.queries(url) for url in pages] + [self.queries(reverse(TICK), tick, **HTMX)]
        self.more_items(self.lists.open, 18)
        self.more_items(self.lists.finished, 18)
        more = [self.queries(url) for url in pages] + [self.queries(reverse(TICK), tick, **HTMX)]
        self.assertEqual(more, few)

    def test_the_list_page_costs_no_more_with_more_history(self):
        """The add form's menu - every article, the store's products, each
        with its units - and a card counting in bottles read the same
        queries whatever was bought: more articles, at every store, in
        litres, kilos and units, bottles and weighed, week after week."""
        pages = (self.page_of(self.wholesaler), self.page_of(self.wholesaler, ligne=self.lists.syrup.pk))
        few = [self.queries(url) for url in pages]
        today = timezone.localdate()
        for number in range(6):
            store = (self.wholesaler, self.grocer, self.market)[number % 3]
            unit = (UnitChoices.LITRE, UnitChoices.KILOGRAM, UnitChoices.UNIT)[number % 3]
            article = make_stock_type(name=f"Article {number} exemple", unit=unit)
            bottle = make_product(
                supplier=store, raw_name=f"ARTICLE {number} EXEMPLE 70CL", stock_type=article, unit=unit
            )
            weighed = make_product(
                supplier=store, raw_name=f"ARTICLE {number} EXEMPLE VRAC", stock_type=article, unit=unit
            )
            for week in range(1, 12):
                invoice = make_invoice(supplier=store, invoice_date=today - timedelta(days=7 * week + number))
                for product, volume in ((bottle, "4.2"), (weighed, f"{3 + week}.1")):
                    line = make_invoice_line(
                        invoice=invoice, product=product, quantity=6, total_ht="44.00", total_volume=volume
                    )
                    make_movement(stock_type=article, quantity=volume, invoice_line=line)
        self.assertEqual([self.queries(url) for url in pages], few)


class MarkupTests(ListPageTestCase):
    """A name or a note comes from whoever typed it, or from a supplier's
    document: printed as text on every page and in every message."""

    def test_on_every_page_and_in_every_message(self):
        ShoppingListItem.objects.filter(pk=self.lists.bread.pk).update(label=MARKUP, note=MARKUP)
        ShoppingListItem.objects.filter(pk=self.lists.coffee.pk).update(note=MARKUP)
        self.made.syrup.name = MARKUP
        self.made.syrup.save(update_fields=["name"])
        self.made.grocer.name = f"Épicerie {MARKUP}"
        self.made.grocer.save(update_fields=["name"])
        # A product of the store read off a supplier's document: in the menu
        # and in its island, as text and as data.
        make_product(supplier=self.wholesaler, raw_name=MARKUP, stock_type=self.made.olives)
        html = self.html(fournisseur=self.wholesaler.pk)
        self.assertIn(f"{MARKUP}{AT_THE_WHOLESALER}", menu_of(html))
        self.assertIn(f'<option value="{MARKUP_SHOWN}{AT_THE_WHOLESALER}">', html)
        self.assertEqual(entry_data_of(html)[f"{MARKUP}{AT_THE_WHOLESALER}"]["kind"], "product")
        self.assertEqual(entry_data_of(html)[f"{MARKUP} (article)"]["kind"], "stock_type")
        pages = [
            self.html(INDEX),
            self.html(fournisseur=self.wholesaler.pk),
            self.html(fournisseur=self.wholesaler.pk, ligne=self.lists.bread.pk),
            self.run_page(),
            self.html(liste=self.lists.finished.pk),
            self.post(ADD, {"fournisseur": self.wholesaler.pk, "nom": f"Autre {MARKUP}"}).content.decode(),
            self.post(
                EDIT, {"fournisseur": self.wholesaler.pk, "ligne": self.lists.bread.pk, "quantite": "3"}
            ).content.decode(),
            self.post(DELETE, {"fournisseur": self.wholesaler.pk, "ligne": self.lists.syrup.pk}).content.decode(),
            self.post(FINISH, {"liste": self.lists.open.pk}).content.decode(),
        ]
        for number, html in enumerate(pages):
            with self.subTest(page=number):
                self.assertNotIn("<i>exemple</i>", html)
                self.assertIn(MARKUP_SHOWN, html)
