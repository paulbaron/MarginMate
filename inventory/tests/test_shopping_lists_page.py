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

Invented data throughout (tests.test_views_smoke.make_shopping_history and
make_shopping_lists): every store, article, product, login and figure.
"""

from __future__ import annotations

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
from inventory.models import Product, ShoppingList, ShoppingListItem, StockMovement, UnitChoices
from inventory.shopping_lists import Figures, Finished, finish, set_ticked
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
from tests.factories import make_invoice, make_invoice_line, make_movement, make_product, make_supplier
from tests.runner import TEST_EMAIL, employee_of_the_test_tenant
from tests.test_views_smoke import (
    SHOPPING_BEER_PRODUCT,
    SHOPPING_LIST_FINISHER,
    assertNoUnrenderedTemplateSyntax,
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
ADD_FORM_HELP = "Un nom inconnu s'ajoute tel quel. Quantité : en unités, jamais en colis ; vide : celle d'habitude ici."
SYRUP_PRODUCT = "SIROP EXEMPLE PRODUIT"
MARKUP = '<i>exemple</i> "test"'
MARKUP_SHOWN = "&lt;i&gt;exemple&lt;/i&gt; &quot;test&quot;"
HTMX = {"HTTP_HX_REQUEST": "true"}


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
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(views, name), said)

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
    off)."""

    WITH_LISTS = True

    @classmethod
    def setUpTestData(cls):
        cls.made = make_shopping_history()
        cls.lists = make_shopping_lists(cls.made) if cls.WITH_LISTS else SimpleNamespace()
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
        """What the list page's « Ajouter » form carries hidden."""
        (form,) = forms_to(self.html(fournisseur=store.pk), ADD)
        return hidden_of(form)

    def add(self, store, **typed):
        """« Ajouter » on the list page, as drawn, with what is typed."""
        return self.post(ADD, {**self.add_form(store), "nom": "", "quantite": "", "note": "", **typed})

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
        html = self.html(fournisseur=self.wholesaler.pk)
        (form,) = forms_to(html, ADD)
        self.assertIn('class="inline-form shopping-add"', form)
        self.assertEqual(hidden_of(form), {"fournisseur": str(self.wholesaler.pk)})
        self.assertIn(
            '<input type="text" name="nom" list="shopping-articles" required maxlength="255" autocomplete="off">',
            form,
        )
        self.assertIn('<input type="text" name="quantite" inputmode="decimal" size="6" placeholder="habituelle">', form)
        self.assertIn('<input type="text" name="note" maxlength="200">', form)
        self.assertEqual(busy_button_of(form), ("Ajout…", "Ajouter"))
        menu = re.search(r'<datalist id="shopping-articles">(.*?)</datalist>', html, flags=re.DOTALL).group(1)
        # The store's own articles first, then the others, each as read.
        self.assertEqual(
            [unescape(name) for name in re.findall(r'<option value="([^"]*)">', menu)],
            [
                "Bière exemple",
                "Café exemple",
                "Chips exemple",
                "Fût exemple",
                "Olives exemple",
                "Rhum exemple",
                "Sirop exemple",
                "Citron exemple",
                "Fraises exemple",
            ],
        )
        # What a typed quantity counts: units, never packs.
        self.assertIn(ADD_FORM_HELP, readable(html.split(menu)[1]))

    def test_the_card_changing_one_item(self):
        """Beside the quantity, what it counts - and, for a product sold in
        packs, how many packs it makes or « à l'unité »."""
        for item, quantity, counts in (
            (self.lists.beer, "24", f"{SHOPPING_BEER_PRODUCT} · 1 colis de 24"),
            (self.lists.syrup, "2", "L"),
            (self.lists.bread, "2", ""),
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
                    "autofocus>",
                    form,
                )
                said = re.search(r'autofocus></label>\s*(?:<span class="muted small">([^<]*)</span>)?', form)
                self.assertEqual(unescape(said.group(1) or ""), counts)
                self.assertIn('<input type="text" name="note" value="" maxlength="200">', form)
                self.assertEqual(busy_button_of(form), ("Enregistrement…", "Enregistrer"))
                self.assertIn((self.page_of(self.wholesaler), "Annuler"), links_of(form))
                self.assertEqual(warnings_of(html), [])
        ShoppingListItem.objects.filter(pk=self.lists.beer.pk).update(quantity=Decimal("30"))
        card = edit_card(self.html(fournisseur=self.wholesaler.pk, ligne=self.lists.beer.pk))
        said = re.search(r'autofocus></label>\s*<span class="muted small">([^<]*)</span>', card)
        self.assertEqual(unescape(said.group(1)), f"{SHOPPING_BEER_PRODUCT} · à l'unité · colis de 24")

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
                    counted = data["quantite"]
                    if article == self.made.beer:
                        # Its product comes in cartons of 24: how many, said.
                        counted += f" · {int(counted) // 24} colis de 24"
                    said = [f"« {article.name} » ajouté à la liste ({counted})."]
                    if anchor == "a-acheter":
                        self.assertEqual(said_above_the_list(html), said)
                    else:
                        fold = fold_of(html, anchor)
                        self.assertTrue(opened(fold), anchor)
                        self.assertEqual(said_in(fold), said)
                    self.assertEqual(said_at_the_top(html), [])
                    # The line now says it is on the list.
                    row = row_of(table_of(html, label), article.name)
                    self.assertIn((self.page_of(self.wholesaler), f"Dans la liste ({data['quantite']})"), links_of(row))

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
        self.assertEqual(said_in(fold), ["« Olives exemple » est déjà dans la liste (1)."])
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

        def figures(line):
            found = real(line)
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
