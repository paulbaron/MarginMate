"""The « Prévoir les courses » pages (/courses/, /courses/rythme/), as the
owner reads them, and their forms.

What he chooses is a store (`?fournisseur=`) and, if he likes, how long a
purchase made there today must last, until the visit after it (`?dans=`, 1
to 90); what he reads is the list for shopping there today - each line the
store's own product (« Produit », its packs under it), ONE number to buy of
it (« À acheter »), its chance and why - then the folds: « Peut-être », « Nouveaux ici », « Plus acheté ? »,
« Acheté ailleurs maintenant », « À acheter ailleurs », « Les plus achetés
ici », « Comment c'est calculé », « Réglages » and « Exclusions ». The
figures are the pure module's (inventory/shopping.py, pinned on its own in
test_shopping.py): here each is asserted in its own row and section, as the
page draws it, never anywhere on the page.

The forms - « Réglages », « Pas ici », « Ne plus proposer », « Ne jamais
proposer la catégorie », « Réinclure » - are POSTs answering with one
redirect to the list of the store they came from, opened where their
message is said: above « À acheter » for a line of the list, in the fold
otherwise, at the top of « Rythme d'achat » for its own. What cannot be
read is one message, and nothing is written. A GET to any of them goes to
the list. An exclusion just made is taken back from its own message
(« Annuler »), its answer said where it was pressed.

The view reads today, so the data is dated relative to
`timezone.localdate()` (tests.test_views_smoke.make_shopping_history).
Invented data throughout: every store, article, product, recipe, price and
quantity is made up for these tests.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from decimal import Decimal
from html import unescape
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.access import Access
from inventory import shopping, views
from inventory.models import (
    MovementKind,
    Product,
    ShoppingExclusion,
    ShoppingList,
    ShoppingListItem,
    ShoppingSetting,
    StockMovement,
    StockType,
    UnitChoices,
)
from inventory.tests.test_gap_filler_page import body_rows, busy_button_of, confirm_of, table_of
from invoices.models import GatherCoverage, InvoiceLine
from margins.tests.test_page import cells_of, row_of, stat_of, text_of, value_of
from recipes import auto_sales, sales_sources
from recipes.models import RecipeSale
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_type,
    make_supplier,
)
from tests.test_views_smoke import (
    SHOPPING_BEER_PRODUCT,
    assertNoUnrenderedTemplateSyntax,
    make_shopping_history,
    make_shopping_lists,
)

PAGE = "inventory:shopping_list"
RHYTHM = "inventory:shopping_rhythm"
SETTINGS = "inventory:shopping_settings"
EXCLUDE = "inventory:shopping_exclude"
INCLUDE = "inventory:shopping_include"
LIST_PAGE = "inventory:shopping_list_page"
LIST_ADD = "inventory:shopping_list_add"
ADD_ALL = "inventory:shopping_list_add_all"
TO_BUY = "à acheter"
MAYBE = "peut-être"
NEW = "nouveaux ici"
QUIET = "plus acheté"
ELSEWHERE = "acheté ailleurs"
DUE_ELSEWHERE = "à acheter ailleurs"
TOP_HERE = "les plus achetés ici"
RHYTHM_TABLE = "rythme d'achat"
NBSP = "\N{NO-BREAK SPACE}"
STORE_NOT_FOUND = "Enseigne introuvable : voici la plus fréquentée."
DAYS_REFUSED = "Passage suivant : un nombre de jours de 1 à 90."
FEW_VISITS = "Peu de passages ici : liste indicative."
FIRST_VISIT = "Premier passage ici : rien à prévoir."
ALL_EXCLUDED = "Tous les articles achetés ici sont exclus."
#: The empty list names only the folds the page draws.
EMPTY_LIST = "Rien de sûr à racheter ici aujourd'hui. Regardez « Peut-être » et vos achats habituels ci-dessous."
EMPTY_LIST_MOST_BOUGHT = "Rien de sûr à racheter ici aujourd'hui. Regardez vos achats habituels ci-dessous."
EMPTY_LIST_BARE = "Rien de sûr à racheter ici aujourd'hui."
#: The list assumes the purchase is made today: the days are how long it must last.
HORIZON = "Si vous y allez aujourd'hui : de quoi tenir {} jours, jusqu'au passage suivant{}."
OWN_RHYTHM = " (votre rythme ici)"
DEFAULT_RHYTHM = " (par défaut)"
STALE_TILL = "Ventes de la caisse à jour au {} : la suite est estimée à votre rythme de vente."
STALE_TILL_RHYTHM = "Ventes de la caisse à jour au {} : « Caisse / semaine » s'arrête à cette date."
SAVED = "Réglages enregistrés."
DEFAULTS = "Réglages remis par défaut."
THRESHOLD_REFUSED = "Seuil : un nombre entier de 10 à 60."
MEMORY_REFUSED = "Mémoire : un nombre de mois de 2 à 24."
GONE = "Cette exclusion n'existe plus : rien n'a changé."
#: The undo an exclusion's message carries: a small button of the page's kind.
UNDO_BUTTON = '<button class="btn btn-small btn-secondary" type="submit">Annuler</button>'


def day_of(days_ago: int) -> str:
    return f"{timezone.localdate() - timedelta(days=days_ago):%d/%m/%Y}"


def readable(fragment: str) -> str:
    """A fragment as it reads: no tags, the autoescaped quotes read back."""
    return unescape(text_of(fragment))


def names_in(table: str) -> list[str]:
    """Each row's title (its first cell, badges aside), in the order drawn."""
    return [readable(re.search(r"<td[^>]*>(.*?)</td>", row, flags=re.DOTALL).group(1)) for row in body_rows(table)]


def titles_in(table: str) -> list[str]:
    """Each row's article name alone."""
    return [
        unescape(
            re.match(r"\s*([^<]*)", re.search(r"<td[^>]*>(.*?)</td>", row, flags=re.DOTALL).group(1)).group(1)
        ).strip()
        for row in body_rows(table)
    ]


def fold_of(html: str, anchor: str) -> str:
    """The fold `<details id=anchor>`, whole, or ""."""
    found = re.search(r'<details class="explainer" id="' + anchor + r'"[^>]*>.*?</details>', html, flags=re.DOTALL)
    return found.group(0) if found else ""


def fold_named(html: str, summary: str) -> str:
    """The fold whose summary starts with `summary`, whole, or ""."""
    found = re.search(
        r'<details class="explainer"[^>]*>\s*<summary>' + re.escape(summary) + r".*?</details>", html, flags=re.DOTALL
    )
    return found.group(0) if found else ""


def opened(fold: str) -> bool:
    return bool(re.match(r"<details [^>]*\bopen\b", fold))


def message_items(fragment: str) -> list[str]:
    """Each message of the fragment's `ul.messages`, its inside as drawn."""
    return re.findall(r'<li class="message message-[a-z]+">(.*?)</li>', fragment, flags=re.DOTALL)


def said_in(fragment: str) -> list[str]:
    """The messages a `ul.messages` of the fragment says, as they read - an
    exclusion's « Annuler » form aside (`undo_forms` reads it)."""
    return [readable(re.sub(r"<form\b.*?</form>", "", found, flags=re.DOTALL)) for found in message_items(fragment)]


def undo_forms(fragment: str) -> list[str]:
    """The « Annuler » forms the fragment's messages carry, each drawn
    inside its message, in the order drawn."""
    return [form for message in message_items(fragment) for form in forms_to(message, INCLUDE)]


def horizon_said(html: str) -> str:
    """The sentence under « À acheter » saying what the list is for."""
    found = re.search(r'<h2 id="a-acheter">À acheter</h2>.*?<p class="muted">(.*?)</p>', html, flags=re.DOTALL)
    return readable(found.group(1)) if found else ""


def above_the_list(html: str) -> str:
    """The messages said above « À acheter » (its `ul.messages`), or ""."""
    found = re.search(r'<h2 id="a-acheter">À acheter</h2>\s*<ul class="messages">(.*?)</ul>', html, flags=re.DOTALL)
    return found.group(1) if found else ""


def said_above_the_list(html: str) -> list[str]:
    return said_in(above_the_list(html))


def at_the_top(html: str) -> str:
    """What is printed before the page's header: base.html's messages."""
    return html[: html.index('<div class="page-header">')]


def said_at_the_top(html: str) -> list[str]:
    """The messages printed before the page's header: base.html's block."""
    return said_in(at_the_top(html))


def warnings_of(html: str) -> list[str]:
    """The page's own warnings (not a form's message)."""
    return [
        readable(found)
        for found in re.findall(r'<div class="message message-warning">(.*?)</div>', html, flags=re.DOTALL)
    ]


def forms_to(fragment: str, name: str) -> list[str]:
    return re.findall(
        r'<form method="post" action="' + re.escape(reverse(name)) + r'"[^>]*>.*?</form>', fragment, flags=re.DOTALL
    )


def hidden_of(form: str) -> dict[str, str]:
    """The fields a form carries hidden, its CSRF token aside (an id after
    the value included: the finish form's list, which a tick swaps)."""
    found = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"[^>]*>', form))
    found.pop("csrfmiddlewaretoken", None)
    return found


def option_words(html: str) -> dict[str, str]:
    """The « Enseigne » menu: {value: words}."""
    select = re.search(r'<select name="fournisseur">(.*?)</select>', html, flags=re.DOTALL)
    return {
        value: unescape(words)
        for value, words in re.findall(
            r'<option value="(\d*)"[^>]*>([^<]*)</option>', select.group(1) if select else ""
        )
    }


def selected_store(html: str) -> str:
    found = re.search(r'<option value="(\d+)" selected>', html)
    return found.group(1) if found else ""


def chance_of_row(row: str) -> tuple[float, str]:
    """(its data-sort, the cell as it reads) of a row's « Chance »."""
    found = re.search(r'<td class="num" data-label="Chance" data-sort="([^"]+)">(.*?)</td>', row, flags=re.DOTALL)
    return float(found.group(1)), readable(found.group(2))


def product_of(row: str) -> tuple[str, str]:
    """(the product, the pack hint under it) of a row's « Produit »."""
    found = re.search(r'<td data-label="Produit">([^<]*)(?:<br><span class="muted small">([^<]*)</span>)?</td>', row)
    if found is None:
        return ("", "")
    return unescape(found.group(1)), unescape(found.group(2) or "")


def to_buy_of(row: str) -> tuple[str, str, str]:
    """(its data-sort, the number, the grey note under it) of a row's
    « À acheter »."""
    found = re.search(
        r'<td class="num" data-label="À acheter" data-sort="([^"]*)">([^<]*)'
        r'(?:<br><span class="muted small">([^<]*)</span>)?</td>',
        row,
    )
    if found is None:
        return ("", "", "")
    return found.group(1), unescape(found.group(2)), unescape(found.group(3) or "")


class ReadBoundedNumberTests(SimpleTestCase):
    """What « … jusqu'au passage suivant, dans », « Proposer à partir de » and
    « Mémoire des habitudes » accept: ASCII digits, within the range, both ends
    included. Anything else is None - a digit Python reads (« ² », « ٣ »),
    a sign, a decimal - and a number longer than the range's is out of range
    whatever it says, never handed to int()."""

    def test_within_the_range(self):
        for typed, read in (("1", 1), ("90", 90), (" 7 ", 7), ("007", 7), ("45", 45)):
            with self.subTest(typed=typed):
                self.assertEqual(views.read_bounded_number(typed, 1, 90), read)

    def test_anything_else(self):
        for typed in (
            None,
            "",
            " ",
            "0",
            "91",
            "-1",
            "+5",
            "2.5",
            "2,5",
            "abc",
            "1e2",
            "\N{SUPERSCRIPT TWO}",
            "\N{ARABIC-INDIC DIGIT THREE}",
        ):
            with self.subTest(typed=typed):
                self.assertIsNone(views.read_bounded_number(typed, 1, 90))

    def test_a_long_number_never_reaches_int(self):
        with patch("inventory.views.int", side_effect=AssertionError("int() reached"), create=True):
            self.assertIsNone(views.read_bounded_number("9" * 5000, 1, 90))
            self.assertIsNone(views.read_bounded_number("100", 1, 90))
        self.assertEqual(views.read_bounded_number("0" * 40 + "12", 10, 60), 12)


#: The product of the invented line `a_line` draws.
BEER_LINE = "BIERE EXEMPLE 33CL X24"


def a_line(**fields) -> shopping.Line:
    """A forecast line (the pure module's): one usual purchase of 24 cans of
    an invented beer, in its store's product, a pack of 24, no typed
    horizon - `fields` replacing what a test is about."""
    values = {
        "article_id": 1,
        "name": "Bière exemple",
        "unit": UnitChoices.UNIT,
        "category": "",
        "section": shopping.TO_BUY,
        "chance": 0.9,
        "confidence": "presque sûr",
        "sentence": "",
        "badges": (),
        "qty": Decimal("24"),
        "product_id": 5,
        "product_name": BEER_LINE,
        "product_units": Decimal("24"),
        "packs": (1, Decimal("24")),
        "multiplier": 1,
        "last_bought_here": date(2031, 3, 5),
        "habit": 0.9,
        "need": None,
        "clock": None,
    }
    values.update(fields)
    return shopping.Line(**values)


class LineRowsTests(SimpleTestCase):
    """`views._line_rows`: a forecast line as its « Produit » and « À
    acheter » cells say it - the store's product and its packs, ONE number,
    and under it the article's own units when they say something else, then
    « pour N jours » when a typed horizon multiplied it. Lines invented
    (`a_line`: 24 cans of a beer, in packs of 24)."""

    def row(self, horizon=None, **fields) -> dict:
        [row] = views._line_rows([a_line(**fields)], horizon)
        return row

    def cells(self, row) -> tuple[str, str, str, str, str]:
        return row["product"], row["packs"], row["to_buy"], row["to_buy_note"], row["to_buy_sort"]

    def test_the_store_s_product_counted_in_the_article_s_own_unit(self):
        self.assertEqual(self.cells(self.row()), (BEER_LINE, "1 colis de 24", "24", "", "24"))

    def test_a_product_counting_otherwise_than_the_article(self):
        # Four packs of six cans: the cans under the packs.
        row = self.row(product_name="BIERE PACK X6 EXEMPLE", product_units=Decimal("4"), packs=None)
        self.assertEqual(self.cells(row), ("BIERE PACK X6 EXEMPLE", "", "4", "24 u.", "4"))
        row = self.row(unit=UnitChoices.LITRE, qty=Decimal("2"), product_units=Decimal("2"), packs=None)
        self.assertEqual(self.cells(row), (BEER_LINE, "", "2", "2 L", "2"))

    def test_bought_by_measure(self):
        row = self.row(
            unit=UnitChoices.KILOGRAM, qty=Decimal("1.5"), product_units=None, packs=None, product_name="OLIVES"
        )
        self.assertEqual(self.cells(row), ("", "", "1.5 kg", "", "1.5"))

    def test_a_typed_horizon_multiplies_the_one_number(self):
        self.assertEqual(
            self.cells(self.row(30, multiplier=3)), (BEER_LINE, "3 colis de 24", "72", "pour 30 jours", "72")
        )
        row = self.row(
            20, unit=UnitChoices.LITRE, qty=Decimal("2"), product_units=Decimal("2"), packs=None, multiplier=2
        )
        self.assertEqual(self.cells(row), (BEER_LINE, "", "4", "4 L · pour 20 jours", "4"))
        row = self.row(30, unit=UnitChoices.KILOGRAM, qty=Decimal("1.5"), product_units=None, packs=None, multiplier=3)
        self.assertEqual(self.cells(row), ("", "", "4.5 kg", "pour 30 jours", "4.5"))
        # No days typed, no « pour … »: a multiplier is a typed horizon's.
        self.assertEqual(self.cells(self.row(None, multiplier=3))[3], "")

    def test_a_median_of_two_purchases_is_shown_to_three_places(self):
        row = self.row(unit=UnitChoices.LITRE, qty=Decimal("1.8125"), product_units=None, packs=None)
        self.assertEqual(self.cells(row), ("", "", "1.813 L", "", "1.813"))

    def test_the_line_and_its_chance_ride_along_and_the_old_cell_is_gone(self):
        row = self.row()
        self.assertEqual(row["line"].name, "Bière exemple")
        self.assertEqual((row["percent"], row["chance_sort"]), (90, "0.9000"))
        self.assertNotIn("quantity", row)
        self.assertNotIn("article_quantity", row)


class ShoppingPageTestCase(TestCase):
    """The pages and their forms as the tests below drive them - no test of
    its own. `made` is make_shopping_history()'s stores and articles."""

    @classmethod
    def setUpTestData(cls):
        cls.made = make_shopping_history()

    def html(self, name=PAGE, **params) -> str:
        """The page drawn - always a 200, its template rendered whole."""
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"{name} {params}")
        return response.content.decode()

    def post(self, name: str, **data):
        """POST to a form's route and follow it: one redirect, and the page
        it lands on, rendered whole."""
        response = self.client.post(reverse(name), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([status for _url, status in response.redirect_chain], [302])
        assertNoUnrenderedTemplateSyntax(self, response, f"after {name} {data}")
        return response

    def landing(self, response) -> str:
        return response.redirect_chain[0][0]

    def list_of(self, store, anchor: str = "", **query) -> str:
        params = "&".join([f"fournisseur={store.pk}", *(f"{key}={value}" for key, value in query.items())])
        return f"{reverse(PAGE)}?{params}" + (f"#{anchor}" if anchor else "")

    def buy(self, store, article, days_ago, quantity="1", total_ht="34.00"):
        """One purchase of `article` at `store`, `days_ago` days back, in the
        store's own product for it (made the first time)."""
        product = article.products.filter(supplier=store).first() or make_product(
            supplier=store, raw_name=f"{article.name.upper()} PRODUIT", stock_type=article
        )
        invoice = make_invoice(supplier=store, invoice_date=timezone.localdate() - timedelta(days=days_ago))
        line = make_invoice_line(invoice=invoice, product=product, quantity=int(quantity), total_ht=total_ht)
        make_movement(stock_type=article, quantity=quantity, unit_cost_ht="40", invoice_line=line)


class TheListTests(ShoppingPageTestCase):
    """The wholesaler's list, the store most visited: every section with its
    own lines, each figure in its own row."""

    def test_the_most_visited_store_is_the_default_and_the_menu_says_each_rhythm(self):
        html = self.html()
        self.assertEqual(selected_store(html), str(self.made.wholesaler.pk))
        self.assertEqual(
            option_words(html),
            {
                str(self.made.wholesaler.pk): "Grossiste exemple — tous les 7 jours, dernier passage il y a 7 jours",
                str(self.made.grocer.pk): "Épicerie exemple — tous les 7 jours, dernier passage il y a 14 jours",
                str(self.made.market.pk): "Marché exemple — dernier passage il y a 30 jours",
            },
        )
        # Visited once in the year: with the stores visited rarely.
        rare = re.search(r'<optgroup label="Autres enseignes">(.*?)</optgroup>', html, flags=re.DOTALL)
        self.assertIsNotNone(rare)
        self.assertIn("Marché exemple", rare.group(1) if rare else "")
        self.assertNotIn("Grossiste exemple", rare.group(1) if rare else "")
        self.assertEqual(warnings_of(html), [])

    def test_the_choice_form(self):
        html = self.html()
        form = re.search(r'<form method="get" class="filter-row[^"]*">.*?</form>', html, flags=re.DOTALL).group(0)
        self.assertIn(
            '<input type="number" name="dans" value="" min="1" max="90" step="1" inputmode="numeric" placeholder="7">',
            form,
        )
        # The days are how long today's purchase must last, not when the
        # trip is: the field says so.
        label = re.search(r'<label class="inline-label">([^<]*)<input type="number" name="dans"', form)
        self.assertEqual(
            " ".join(unescape(label.group(1)).split()), "Ces achats doivent tenir jusqu'au passage suivant, dans"
        )
        self.assertEqual(busy_button_of(form), ("Calcul…", "Afficher"))

    def test_the_figures_at_the_head(self):
        html = self.html()
        self.assertTrue(value_of(stat_of(html, "Panier habituel ici")).startswith("≈ "))
        proposed = stat_of(html, "Proposés")
        self.assertEqual(value_of(proposed), "2")
        self.assertIn("et 1 peut-être", text_of(proposed))
        last = stat_of(html, "Dernier passage")
        self.assertEqual(value_of(last), "il y a 7 jours")
        # Twenty-six weekly visits and three older ones.
        self.assertIn(f"le {day_of(7)} · 29 passages", text_of(last))
        # No till sale in this history: nothing said about the till.
        self.assertEqual(stat_of(html, "Ventes de la caisse"), "")

    def test_to_buy_each_line_in_its_own_row(self):
        """« Produit » names the store's product, the packs under it; « À
        acheter » is ONE number - what to take of that product -, the
        article's own units under it only when they say something else."""
        html = self.html()
        table = table_of(html, TO_BUY)
        self.assertEqual(titles_in(table), ["Bière exemple", "Sirop exemple"])
        header = re.search(r"<thead>(.*?)</thead>", table, flags=re.DOTALL).group(1)
        self.assertEqual(
            [readable(cell) for cell in re.findall(r"<th[^>]*>(.*?)</th>", header, flags=re.DOTALL)],
            ["Article", "Produit", "À acheter", "Chance", "Pourquoi", "Liste", ""],
        )
        self.assertIn('<th class="num">À acheter</th>', header)
        beer = row_of(table, "Bière exemple")
        cells = [unescape(cell) for cell in cells_of(beer)]
        self.assertEqual(cells[1:3], [f"{SHOPPING_BEER_PRODUCT} 1 colis de 24", "24"])
        self.assertEqual(product_of(beer), (SHOPPING_BEER_PRODUCT, "1 colis de 24"))
        # 24 cans of a beer counted in cans: « 24 u. » under « 24 » would
        # only say it again.
        self.assertEqual(to_buy_of(beer), ("24", "24", ""))
        self.assertEqual(
            cells[4],
            "Pris 25 fois sur 25 passages en 6 mois ; dernier achat il y a 7 jours, d'habitude tous les 7 jours.",
        )
        # « Liste », then the line's action, last.
        self.assertEqual(cells[5:], ["Ajouter", "Pas ici"])
        syrup = row_of(table, "Sirop exemple")
        self.assertEqual(product_of(syrup), ("SIROP EXEMPLE PRODUIT", ""))
        # Two of its product, which make 2 L of the article.
        self.assertEqual(to_buy_of(syrup), ("2", "2", "2 L"))

    def test_no_quantity_cell_multiplies_anything(self):
        """The old « m × (n × PRODUIT) » is gone from every section that
        draws a quantity, a typed horizon included."""
        for query in ({}, {"dans": "60"}):
            html = self.html(**query)
            for label in (TO_BUY, MAYBE, NEW):
                for row in body_rows(table_of(html, label)):
                    with self.subTest(query=query, section=label, row=readable(row)[:30]):
                        product, hint = product_of(row)
                        _sort, number, note = to_buy_of(row)
                        self.assertTrue(number)
                        self.assertNotIn("×", f"{product} {hint} {number} {note}")
                        self.assertNotIn('data-label="Quantité"', row)

    def test_a_product_bought_by_measure_shows_the_article_s_units(self):
        """Olives weighed at the till (1,5 kg): no count of a product to
        name - « Produit » is empty and « À acheter » reads the kilos."""
        InvoiceLine.objects.filter(product__stock_type=self.made.olives).update(quantity=Decimal("1.5"))
        StockMovement.objects.filter(stock_type=self.made.olives).update(quantity=Decimal("1.5"))
        row = row_of(table_of(self.html(), MAYBE), "Olives exemple")
        self.assertEqual(product_of(row), ("", ""))
        self.assertEqual(to_buy_of(row), ("1.5", "1.5 kg", ""))

    def test_the_chance_reads_as_its_sort_key_says(self):
        """« 98 % · presque sûr »: the percent rounded down from the very
        figure the column sorts by, and its word from the same figure - on
        a line with a live rhythm, which every line of this list is (a
        nagged or « plus acheté ? » line reads « à vérifier »: NaggedTests)."""
        html = self.html()
        for row in body_rows(table_of(html, TO_BUY)) + body_rows(table_of(html, MAYBE)):
            chance, reads = chance_of_row(row)
            with self.subTest(row=readable(row)[:30]):
                self.assertNotIn(shopping.DROPPED_LABEL, row)
                self.assertNotIn("Proposé à vos", unescape(row))
                self.assertEqual(reads, f"{math.floor(chance * 100 + 1e-9)} % · {shopping.confidence_of(chance)}")
        beer_chance, _reads = chance_of_row(row_of(table_of(html, TO_BUY), "Bière exemple"))
        self.assertGreaterEqual(beer_chance, shopping.SURE)

    def test_every_other_section(self):
        html = self.html()
        self.assertEqual(titles_in(table_of(html, MAYBE)), ["Olives exemple"])
        self.assertEqual(titles_in(table_of(html, NEW)), ["Chips exemple"])
        quiet = table_of(html, QUIET)
        self.assertEqual(titles_in(quiet), ["Rhum exemple"])
        self.assertIn('<span class="status-pill status-pending">plus acheté ?</span>', quiet)
        self.assertEqual(
            [unescape(cell) for cell in cells_of(row_of(quiet, "Rhum exemple"))][1:],
            ["Plus acheté depuis 326 jours ; d'habitude tous les 7 jours environ.", "Ne plus proposer"],
        )
        elsewhere = table_of(html, ELSEWHERE)
        self.assertEqual(
            [unescape(cell) for cell in cells_of(row_of(elsewhere, "Café exemple"))][1:],
            ["Vos 3 derniers achats étaient chez Épicerie exemple."],
        )
        due = table_of(html, DUE_ELSEWHERE)
        self.assertEqual(titles_in(due), ["Citron exemple"])
        self.assertIn(f'<a href="{reverse(PAGE)}?fournisseur={self.made.grocer.pk}">Épicerie exemple</a>', due)
        self.assertEqual(
            [unescape(cell) for cell in cells_of(row_of(due, "Citron exemple"))][1:],
            ["Épicerie exemple", "Dernier achat il y a 14 jours, d'habitude tous les 7 jours.", "Ne plus proposer"],
        )

    def test_the_most_bought_here_in_money(self):
        table = table_of(self.html(), TOP_HERE)
        self.assertEqual(titles_in(table)[:3], ["Bière exemple", "Sirop exemple", "Fût exemple"])
        beer = row_of(table, "Bière exemple")
        self.assertEqual(cells_of(beer)[1:], ["26", f"1{NBSP}170.00 €"])
        self.assertIn('data-sort="1170.00"', beer)

    def test_the_folds_are_shut_while_the_list_has_lines(self):
        html = self.html()
        self.assertTrue(fold_of(html, "peut-etre"))
        self.assertTrue(fold_of(html, "most-bought"))
        self.assertFalse(opened(fold_of(html, "peut-etre")))
        self.assertFalse(opened(fold_of(html, "most-bought")))
        for anchor in ("reglages", "exclusions"):
            with self.subTest(fold=anchor):
                self.assertFalse(opened(fold_of(html, anchor)))
        self.assertEqual(readable(re.search(r"<summary>(Peut-être.*?)</summary>", html).group(1)), "Peut-être (1)")

    def test_an_id_no_redirect_targets_is_english(self):
        """Only the ids a redirect lands on keep their French (a-acheter,
        peut-etre, nouveaux-ici - « Ajouter » on their lines -, reglages,
        exclusions: SHOPPING_ANCHORS); the others are internal."""
        html = self.html()
        self.assertEqual(
            sorted(views.SHOPPING_ANCHORS.values()),
            ["a-acheter", "exclusions", "nouveaux-ici", "peut-etre", "reglages"],
        )
        ids = set(re.findall(r'<(?:details|h2)[^>]* id="([^"]+)"', html))
        self.assertEqual(ids, {"a-acheter", "peut-etre", "nouveaux-ici", "most-bought", "reglages", "exclusions"})
        self.assertEqual(views.FORECAST_ADD_PLACES, ("liste", "peut-etre", "nouveaux"))

    def test_the_horizon_is_the_store_s_rhythm(self):
        """The list is worked out for a purchase made today, lasting until
        the visit after it: the sentence says so, and that the days are the
        owner's rhythm at the store."""
        self.assertEqual(horizon_said(self.html()), HORIZON.format(7, OWN_RHYTHM))

    def test_a_typed_horizon_longer_than_the_rhythm_multiplies_the_usual_purchase(self):
        """60 days at a weekly store: as many usual purchases as the days
        hold weeks (shopping._multiplier), counted in ONE number - and the
        packs it makes - with « pour 60 jours » under it."""
        html = self.html(dans="60")
        self.assertIn('name="dans" value="60"', html)
        self.assertEqual(horizon_said(html), HORIZON.format(60, ""))
        times = math.floor(60 / 7 + 0.5)
        self.assertGreater(times, 1)
        beer = row_of(table_of(html, TO_BUY), "Bière exemple")
        self.assertEqual(product_of(beer), (SHOPPING_BEER_PRODUCT, f"{times} colis de 24"))
        self.assertEqual(to_buy_of(beer), (str(24 * times), str(24 * times), "pour 60 jours"))
        syrup = row_of(table_of(html, TO_BUY), "Sirop exemple")
        self.assertEqual(to_buy_of(syrup), (str(2 * times), str(2 * times), f"{2 * times} L · pour 60 jours"))

    def test_a_typed_horizon_within_the_rhythm_says_nothing_more(self):
        beer = row_of(table_of(self.html(dans="5"), TO_BUY), "Bière exemple")
        self.assertEqual(to_buy_of(beer), ("24", "24", ""))
        self.assertEqual(product_of(beer), (SHOPPING_BEER_PRODUCT, "1 colis de 24"))

    def test_the_forms_of_the_lines(self):
        """« Pas ici » on the list and in « Peut-être », « Ne plus proposer »
        in « Plus acheté ? »: each carries its article, the store to come
        back to and where its message is said; only « Pas ici » names the
        store it leaves the article out at, and only the other asks first."""
        html = self.html()
        wholesaler = str(self.made.wholesaler.pk)
        for label, article, landing in ((TO_BUY, self.made.beer, "liste"), (MAYBE, self.made.olives, "exclusions")):
            with self.subTest(section=label):
                (form,) = forms_to(row_of(table_of(html, label), article.name), EXCLUDE)
                self.assertEqual(
                    hidden_of(form),
                    {"article": str(article.pk), "chez": wholesaler, "fournisseur": wholesaler, "retour": landing},
                )
                self.assertEqual(confirm_of(form), "")
        (form,) = forms_to(table_of(html, QUIET), EXCLUDE)
        self.assertEqual(
            hidden_of(form), {"article": str(self.made.rum.pk), "fournisseur": wholesaler, "retour": "exclusions"}
        )
        self.assertEqual(unescape(confirm_of(form)), "Ne plus proposer « Rhum exemple » dans aucune enseigne ?")
        # « À acheter ailleurs » is dismissed where it is read: everywhere,
        # asked first, said in « Exclusions ».
        (form,) = forms_to(table_of(html, DUE_ELSEWHERE), EXCLUDE)
        self.assertEqual(
            hidden_of(form), {"article": str(self.made.lemon.pk), "fournisseur": wholesaler, "retour": "exclusions"}
        )
        self.assertEqual(unescape(confirm_of(form)), "Ne plus proposer « Citron exemple » dans aucune enseigne ?")
        # A line of « Acheté ailleurs maintenant » has nothing to act on.
        self.assertEqual(forms_to(table_of(html, ELSEWHERE), EXCLUDE), [])

    def test_a_typed_horizon_is_carried_by_every_form(self):
        html = self.html(dans="30")
        forms = forms_to(html, EXCLUDE) + forms_to(html, SETTINGS)
        self.assertGreater(len(forms), 3)
        for form in forms:
            self.assertEqual(hidden_of(form).get("dans"), "30")

    def test_how_it_is_worked_out_says_the_settings(self):
        fold = fold_named(self.html(), "Comment c'est calculé")
        said = readable(fold)
        self.assertIn("un passage d'il y a 6 mois compte moitié", said)
        self.assertIn("La liste garde les articles à 25 % ou plus.", said)
        self.assertIn("Un article jamais acheté ici ne peut pas être prévu.", said)
        self.assertIn(
            "« À acheter » : la quantité du milieu de vos 3 derniers achats ici, comptée dans le produit que vous y prenez.",
            said,
        )
        # Worked out for a purchase made today, which must last until the
        # visit after it.
        self.assertIn("la chance que vous le preniez si vous y passez aujourd'hui.", said)
        self.assertIn("épuisé d'ici le passage suivant", said)
        self.assertNotIn("prochain passage", said)

    def test_the_rhythm_view_is_one_link_away(self):
        self.assertIn(f'href="{reverse(RHYTHM)}?fournisseur={self.made.wholesaler.pk}"', self.html())

    def test_markup_in_a_name_is_printed_as_text(self):
        """An article's name comes from whoever typed it: on the list, in a
        sentence, in a confirmation, never as markup."""
        StockType.objects.filter(pk=self.made.beer.pk).update(name='Bière <i>exemple</i> "test"')
        html = self.html()
        self.assertNotIn("<i>exemple</i>", html)
        self.assertIn("Bière &lt;i&gt;exemple&lt;/i&gt; &quot;test&quot;", html)


class StatesTests(ShoppingPageTestCase):
    """The other states the list is drawn in."""

    def test_an_address_naming_no_store_it_offers(self):
        for asked in ("abc", "999999", str(self.made.wholesaler.pk + 1000), "\N{SUPERSCRIPT TWO}"):
            with self.subTest(fournisseur=asked):
                html = self.html(fournisseur=asked)
                self.assertEqual(warnings_of(html), [STORE_NOT_FOUND])
                self.assertEqual(selected_store(html), str(self.made.wholesaler.pk))
                self.assertIn("Bière exemple", titles_in(table_of(html, TO_BUY)))

    def test_days_that_cannot_be_read_are_set_aside(self):
        for typed in ("0", "91", "abc", "-3", "2.5", "1" * 30):
            with self.subTest(dans=typed):
                html = self.html(dans=typed)
                self.assertEqual(warnings_of(html), [DAYS_REFUSED])
                self.assertIn('name="dans" value=""', html)
                self.assertEqual(horizon_said(html), HORIZON.format(7, OWN_RHYTHM))

    def test_another_store(self):
        html = self.html(fournisseur=self.made.grocer.pk)
        self.assertEqual(selected_store(html), str(self.made.grocer.pk))
        self.assertEqual(sorted(titles_in(table_of(html, TO_BUY))), ["Café exemple", "Citron exemple"])
        # Three visits: its own rhythm, but few, so the list says it is
        # indicative and opens the most bought - never « Peut-être » while
        # the list has lines.
        self.assertEqual(horizon_said(html), HORIZON.format(7, OWN_RHYTHM) + " " + FEW_VISITS)
        self.assertTrue(opened(fold_of(html, "most-bought")))
        self.assertFalse(opened(fold_of(html, "peut-etre")))

    def test_a_few_visits_leave_maybe_folded_while_the_list_has_lines(self):
        """A store under 5 visits opens « Les plus achetés ici » only: its
        « Peut-être » stays folded while « À acheter » has lines. Mint bought
        once, at the grocer's last visit: « Peut-être », as the olives are
        at the wholesaler's."""
        self.buy(self.made.grocer, make_stock_type(name="Menthe exemple", unit=UnitChoices.UNIT), 14)
        html = self.html(fournisseur=self.made.grocer.pk)
        self.assertIn(FEW_VISITS, horizon_said(html))
        self.assertTrue(table_of(html, TO_BUY))
        self.assertEqual(titles_in(table_of(html, MAYBE)), ["Menthe exemple"])
        self.assertFalse(opened(fold_of(html, "peut-etre")))
        self.assertTrue(opened(fold_of(html, "most-bought")))

    def test_a_rare_store(self):
        html = self.html(fournisseur=self.made.market.pk)
        # Visited once: the days are the default, never called its rhythm.
        self.assertEqual(horizon_said(html), HORIZON.format(14, DEFAULT_RHYTHM) + " " + FEW_VISITS)
        self.assertIn(EMPTY_LIST, readable(html))
        self.assertEqual(titles_in(table_of(html, MAYBE)), ["Fraises exemple"])
        self.assertTrue(opened(fold_of(html, "peut-etre")))
        self.assertTrue(opened(fold_of(html, "most-bought")))

    def test_the_store_s_rhythm_from_its_third_visit(self):
        """« (votre rythme ici) » from the visit that gives the store a gap
        of its own (shopping's usual gap), as the « Enseigne » menu says
        « tous les N jours » from it; « (par défaut) » under it."""
        market = self.made.market
        for days_ago, rhythm in ((20, DEFAULT_RHYTHM), (10, OWN_RHYTHM)):
            self.buy(market, self.made.strawberries, days_ago, "2", "33.00")
            with self.subTest(visits=2 if days_ago == 20 else 3):
                html = self.html(fournisseur=market.pk)
                days = 14 if rhythm == DEFAULT_RHYTHM else 10
                self.assertEqual(horizon_said(html), HORIZON.format(days, rhythm) + " " + FEW_VISITS)
                menu = option_words(html)[str(market.pk)]
                self.assertEqual("tous les 10 jours" in menu, rhythm == OWN_RHYTHM, menu)
        # A typed number of days is the owner's, whatever the visits.
        self.assertEqual(
            horizon_said(self.html(fournisseur=market.pk, dans="14")), HORIZON.format(14, "") + " " + FEW_VISITS
        )

    def test_the_menu_and_the_sentence_share_shopping_s_rule(self):
        """The view's GAP_MIN_VISITS is where shopping's usual gap stops
        being DEFAULT_GAP_DAYS: one visit under it, the default; at it, a
        gap measured (10 days, not the default's 14)."""
        today = timezone.localdate()
        visits = tuple(today - timedelta(days=10 * number) for number in range(views.GAP_MIN_VISITS, 0, -1))
        self.assertEqual(shopping._usual_gap(visits[1:]), shopping.DEFAULT_GAP_DAYS)
        self.assertEqual(shopping._usual_gap(visits), 10.0)

    def test_an_empty_list_opens_maybe_and_the_most_bought(self):
        for article in (self.made.beer, self.made.syrup):
            ShoppingExclusion.objects.create(stock_type=article, supplier=self.made.wholesaler)
        html = self.html()
        self.assertEqual(table_of(html, TO_BUY), "")
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertEqual(readable(found.group(1)), EMPTY_LIST)
        self.assertTrue(opened(fold_of(html, "peut-etre")))
        self.assertTrue(opened(fold_of(html, "most-bought")))

    def test_an_empty_list_names_only_the_folds_drawn(self):
        """Nothing sure here, and no « Peut-être » to look at: the sentence
        sends nobody to a fold the page does not draw. A tonic bought twice
        at a hall, then every fortnight at the grocer's: « Acheté ailleurs
        maintenant » and « Les plus achetés ici » only. Bought there only
        more than a year ago: neither, and the sentence stops."""
        hall = make_supplier(name="Halle exemple")
        tonic = make_stock_type(name="Tonic exemple", unit=UnitChoices.UNIT)
        for days_ago in (200, 150):
            self.buy(hall, tonic, days_ago)
        for days_ago in range(14, 140, 14):
            self.buy(self.made.grocer, tonic, days_ago)
        html = self.html(fournisseur=hall.pk)
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertEqual(readable(found.group(1)), EMPTY_LIST_MOST_BOUGHT)
        self.assertEqual(fold_of(html, "peut-etre"), "")
        self.assertTrue(opened(fold_of(html, "most-bought")))
        self.assertEqual(titles_in(table_of(html, ELSEWHERE)), ["Tonic exemple"])

        old = make_supplier(name="Ancienne halle exemple")
        pretzels = make_stock_type(name="Bretzels exemple", unit=UnitChoices.UNIT)
        for days_ago in (400, 390):
            self.buy(old, pretzels, days_ago)
        html = self.html(fournisseur=old.pk)
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertEqual(readable(found.group(1)), EMPTY_LIST_BARE)
        self.assertEqual(fold_of(html, "peut-etre"), "")
        self.assertEqual(fold_of(html, "most-bought"), "")

    def test_a_store_whose_every_article_is_left_out(self):
        for article in (self.made.coffee, self.made.lemon):
            ShoppingExclusion.objects.create(stock_type=article, supplier=self.made.grocer)
        html = self.html(fournisseur=self.made.grocer.pk)
        self.assertIn(ALL_EXCLUDED, readable(html))
        self.assertNotIn(FIRST_VISIT, readable(html))
        self.assertEqual(table_of(html, TO_BUY), "")

    def test_a_first_visit(self):
        """A store visited once, on nothing the page knows yet: no candidate
        at all. Not reachable from the database (every movement has its
        article), so the prepared data is handed in."""
        today = timezone.localdate()
        prepared = shopping.Prepared.build(
            today=today,
            purchases=[shopping.PurchaseRow(999999, self.made.market.pk, today - timedelta(days=3), Decimal("1"))],
            articles=[],
            stores=[shopping.StoreInfo(self.made.market.pk, "Marché exemple")],
        )
        with patch("inventory.views.prepare", return_value=prepared):
            html = self.html()
        self.assertIn(FIRST_VISIT, readable(html))
        self.assertEqual(table_of(html, TO_BUY), "")

    def test_nothing_bought_at_all(self):
        StockMovement.objects.all().delete()
        html = self.html()
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertIn(f'href="{reverse("invoices:invoice_add")}"', found.group(1))
        self.assertNotIn('class="stat-row"', html)
        self.assertNotIn('id="reglages"', html)


class FoldsTests(ShoppingPageTestCase):
    """What the folds say beyond the fixture's one line each: a count past
    what is shown, a label carried, a word that is not calibrated."""

    def test_eleven_bought_elsewhere_show_ten_and_say_one_more(self):
        """Ten coffees more like the fixture's - bought here once, then three
        times at the grocer's: eleven in all, ten drawn, the eleventh said."""
        for number in range(10):
            article = make_stock_type(name=f"Café {number} exemple", unit=UnitChoices.KILOGRAM)
            self.buy(self.made.wholesaler, article, 70)
            for days_ago in (28, 21, 14):
                self.buy(self.made.grocer, article, days_ago)
        html = self.html()
        fold = fold_named(html, "Acheté ailleurs maintenant")
        self.assertIn("<summary>Acheté ailleurs maintenant (11)</summary>", fold)
        self.assertEqual(len(body_rows(table_of(fold, ELSEWHERE))), 10)
        self.assertIn('<p class="muted">Et 1 autre.</p>', fold)

    def test_a_nagged_line_reads_a_verifier(self):
        """Taken at every visit for months, then left at the last five (a
        habit: five visits, not three): moved to « Peut-être », its chance
        kept, its word « à vérifier » - the words were calibrated on live
        rhythms only. Silent 42 days: not yet « plus acheté ? »."""
        lemonade = make_stock_type(name="Limonade exemple", unit=UnitChoices.UNIT)
        for week in range(6, 27):
            self.buy(self.made.wholesaler, lemonade, 7 * week, "12", "36.00")
        html = self.html()
        self.assertNotIn("Limonade exemple", titles_in(table_of(html, TO_BUY)))
        row = row_of(table_of(html, MAYBE), "Limonade exemple")
        chance, reads = chance_of_row(row)
        self.assertEqual(reads, f"{math.floor(chance * 100 + 1e-9)} % · {shopping.UNSURE_WORD}")
        self.assertRegex(
            unescape(cells_of(row)[4]), r"^Aurait été proposé à vos \d+ derniers passages ici, sans être pris\.$"
        )

    def test_a_line_no_longer_bought_reads_a_verifier_on_the_list(self):
        """A tonic taken weekly at a cellar until ten months ago, the cellar
        visited twice since: on « À acheter » with its chance, labelled
        « plus acheté ? », its word « à vérifier »."""
        cellar = make_supplier(name="Cave exemple")
        tonic = make_stock_type(name="Tonic exemple", unit=UnitChoices.UNIT)
        other = make_stock_type(name="Glaçons exemple", unit=UnitChoices.UNIT)
        for days_ago in range(300, 357, 7):
            self.buy(cellar, tonic, days_ago, "6", "31.00")
        for days_ago in (100, 50):
            self.buy(cellar, other, days_ago, "1", "32.00")
        html = self.html(fournisseur=cellar.pk)
        row = row_of(table_of(html, TO_BUY), "Tonic exemple")
        self.assertIn(f'<span class="status-pill status-pending">{shopping.DROPPED_LABEL}</span>', row)
        chance, reads = chance_of_row(row)
        self.assertEqual(reads, f"{math.floor(chance * 100 + 1e-9)} % · {shopping.UNSURE_WORD}")

    def test_an_article_a_recipe_still_sells_is_due_elsewhere_with_its_badge(self):
        """A gin taken weekly at the wholesaler's until a hundred days ago,
        still poured by a punch the till sells: « en pause », due at the
        grocer's from the wholesaler's, its label beside its name as on the
        list."""
        gin = make_stock_type(name="Gin exemple", unit=UnitChoices.LITRE)
        for days_ago in range(100, 171, 7):
            self.buy(self.made.wholesaler, gin, days_ago, "1", "41.00")
        punch = make_recipe(name="Punch exemple", selling_price_ttc="32.00")
        make_ingredient(punch, stock_type=gin, quantity="0.04")
        today = timezone.localdate()
        for days_ago in (12, 5):
            RecipeSale.objects.create(
                recipe=punch, sold_on=today - timedelta(days=days_ago), quantity=20, source="caisse"
            )
        GatherCoverage.objects.update_or_create(
            code=auto_sales.coverage_code(sales_sources.LADDITION),
            defaults={"searched_until": auto_sales.last_complete_day(timezone.now())},
        )
        due = table_of(self.html(fournisseur=self.made.grocer.pk), DUE_ELSEWHERE)
        row = row_of(due, "Gin exemple")
        self.assertTrue(row, due)
        self.assertIn(f'<span class="status-pill status-pending">{shopping.PAUSED_LABEL}</span>', row)
        self.assertEqual(titles_in(due).count("Gin exemple"), 1)
        self.assertIn(f'<a href="{reverse(PAGE)}?fournisseur={self.made.wholesaler.pk}">Grossiste exemple</a>', row)


class TillTests(ShoppingPageTestCase):
    """The till's sales, read when « Tenir compte des ventes de la caisse »
    is on: how fresh they are heads the list, and a lagging import is said
    above it."""

    def setUp(self):
        drink = make_recipe(name="Sirop à l'eau exemple", selling_price_ttc="32.00")
        make_ingredient(drink, stock_type=self.made.syrup, quantity="0.04")
        today = timezone.localdate()
        for days_ago in (60, 45, 30, 25):
            RecipeSale.objects.create(
                recipe=drink, sold_on=today - timedelta(days=days_ago), quantity=20, source="caisse"
            )

    def cover(self, until):
        GatherCoverage.objects.update_or_create(
            code=auto_sales.coverage_code(sales_sources.LADDITION), defaults={"searched_until": until}
        )

    def test_a_lagging_import_is_said_above_the_list(self):
        covered = timezone.localdate() - timedelta(days=20)
        self.cover(covered)
        html = self.html()
        self.assertEqual(value_of(stat_of(html, "Ventes de la caisse à jour au")), f"{covered:%d/%m/%Y}")
        self.assertEqual(warnings_of(html), [STALE_TILL.format(f"{covered:%d/%m/%Y}")])
        self.assertLess(html.index("la suite est estimée"), html.index('<h2 id="a-acheter">'))

    def test_a_fresh_import_is_only_dated(self):
        covered = auto_sales.last_complete_day(timezone.now())
        self.cover(covered)
        html = self.html()
        self.assertEqual(value_of(stat_of(html, "Ventes de la caisse à jour au")), f"{covered:%d/%m/%Y}")
        self.assertEqual(warnings_of(html), [])

    def test_the_till_left_out_is_not_read(self):
        self.cover(timezone.localdate() - timedelta(days=20))
        ShoppingSetting.objects.create(pk=ShoppingSetting.SINGLETON_PK, use_till=False)
        html = self.html()
        self.assertEqual(stat_of(html, "Ventes de la caisse à jour au"), "")
        self.assertEqual(warnings_of(html), [])
        self.assertIn("sans la caisse", readable(fold_named(html, "Réglages")))
        self.assertEqual(warnings_of(self.html(RHYTHM)), [])

    def test_the_rhythm_says_a_lagging_import_too(self):
        """« Caisse / semaine » counts the days the import covers: past a
        lag, « Rythme d'achat » says up to when - for every store and for
        one -, and says nothing while the import is fresh."""
        covered = timezone.localdate() - timedelta(days=20)
        self.cover(covered)
        for query in ({}, {"fournisseur": self.made.wholesaler.pk}):
            with self.subTest(query=query):
                html = self.html(RHYTHM, **query)
                self.assertEqual(warnings_of(html), [STALE_TILL_RHYTHM.format(f"{covered:%d/%m/%Y}")])
                self.assertLess(html.index("s'arrête à cette date"), html.index("<table"))
        self.cover(auto_sales.last_complete_day(timezone.now()))
        self.assertEqual(warnings_of(self.html(RHYTHM)), [])


class SettingsFormTests(ShoppingPageTestCase):
    """« Réglages » (#reglages): what is stored, refused or put back, said
    in the fold, opened for it."""

    def test_the_form(self):
        """Two forms, one submit button each, each the busy one of its own
        form: ui.js labels the first busy button of the form sent, so with
        « Valeurs par défaut » beside « Enregistrer » in one form, pressing
        it said « Enregistrement… » on the other and left itself live."""
        fold = fold_of(self.html(), "reglages")
        save, defaults = forms_to(fold, SETTINGS)
        self.assertEqual(hidden_of(save), {"fournisseur": str(self.made.wholesaler.pk)})
        self.assertIn('name="seuil" value="25" min="10" max="60"', save)
        self.assertIn('name="memoire" value="6" min="2" max="24"', save)
        self.assertIn('<input type="checkbox" name="caisse" value="1" checked>', save)
        self.assertEqual(busy_button_of(save), ("Enregistrement…", "Enregistrer"))
        self.assertEqual(hidden_of(defaults), {"fournisseur": str(self.made.wholesaler.pk), "defaut": "1"})
        self.assertEqual(busy_button_of(defaults), ("Remise par défaut…", "Valeurs par défaut"))
        for form in (save, defaults):
            with self.subTest(form=readable(form)[-20:]):
                self.assertEqual(len(re.findall(r"<button\b", form)), 1)
        self.assertNotIn("defaut", save)

    def test_the_defaults_form_as_the_page_draws_it(self):
        """Posted exactly as drawn: the defaults back, said in the fold."""
        ShoppingSetting.objects.create(pk=ShoppingSetting.SINGLETON_PK, threshold_percent=50, use_till=False)
        _save, defaults = forms_to(fold_of(self.html(dans="30"), "reglages"), SETTINGS)
        self.assertEqual(hidden_of(defaults)["dans"], "30")
        response = self.post(SETTINGS, **hidden_of(defaults))
        self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "reglages", dans=30))
        self.assertFalse(ShoppingSetting.objects.exists())
        self.assertEqual(said_in(fold_of(response.content.decode(), "reglages")), [DEFAULTS])

    def test_saved_said_in_the_fold_and_the_list_redrawn(self):
        response = self.post(SETTINGS, fournisseur=self.made.wholesaler.pk, seuil="60", memoire="8")
        self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "reglages"))
        stored = ShoppingSetting.objects.get()
        self.assertEqual((stored.threshold_percent, stored.memory_months, stored.use_till), (60, 8, False))
        html = response.content.decode()
        fold = fold_of(html, "reglages")
        self.assertTrue(opened(fold))
        self.assertEqual(said_in(fold), [SAVED])
        self.assertEqual(said_at_the_top(html), [])
        self.assertIn("à partir de 60 %, mémoire 8 mois, sans la caisse", readable(fold))
        self.assertIn("La liste garde les articles à 60 % ou plus.", readable(html))
        # « Peut-être » is now from 30 %: the olives, far under it, are only new.
        self.assertNotIn("Olives exemple", titles_in(table_of(html, MAYBE)))
        self.assertIn("Olives exemple", titles_in(table_of(html, NEW)))
        # Said once.
        self.assertEqual(said_in(fold_of(self.html(), "reglages")), [])

    def test_each_refusal_said_in_the_fold_and_nothing_written(self):
        ShoppingSetting.objects.create(pk=ShoppingSetting.SINGLETON_PK, threshold_percent=30, memory_months=4)
        for data, said in (
            ({"seuil": "61", "memoire": "8"}, [THRESHOLD_REFUSED]),
            ({"seuil": "40", "memoire": "1"}, [MEMORY_REFUSED]),
            ({"seuil": "abc", "memoire": "99"}, [THRESHOLD_REFUSED, MEMORY_REFUSED]),
            ({}, [THRESHOLD_REFUSED, MEMORY_REFUSED]),
        ):
            with self.subTest(data=data):
                response = self.post(SETTINGS, fournisseur=self.made.wholesaler.pk, caisse="1", **data)
                self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "reglages"))
                fold = fold_of(response.content.decode(), "reglages")
                self.assertTrue(opened(fold))
                self.assertEqual(said_in(fold), said)
                stored = ShoppingSetting.objects.get()
                self.assertEqual((stored.threshold_percent, stored.memory_months, stored.use_till), (30, 4, True))

    def test_the_defaults_back(self):
        ShoppingSetting.objects.create(pk=ShoppingSetting.SINGLETON_PK, threshold_percent=50, use_till=False)
        response = self.post(SETTINGS, fournisseur=self.made.wholesaler.pk, defaut="1", seuil="abc")
        self.assertFalse(ShoppingSetting.objects.exists())
        self.assertEqual(said_in(fold_of(response.content.decode(), "reglages")), [DEFAULTS])
        self.assertIn('name="seuil" value="25"', response.content.decode())

    def test_the_store_and_the_days_come_back(self):
        response = self.client.post(
            reverse(SETTINGS), {"fournisseur": self.made.grocer.pk, "dans": "30", "seuil": "30", "memoire": "6"}
        )
        self.assertEqual(response["Location"], self.list_of(self.made.grocer, "reglages", dans=30))
        # Days that cannot be read are not carried back.
        response = self.client.post(
            reverse(SETTINGS), {"fournisseur": self.made.grocer.pk, "dans": "400", "seuil": "30", "memoire": "6"}
        )
        self.assertEqual(response["Location"], self.list_of(self.made.grocer, "reglages"))


class ExcludeTests(ShoppingPageTestCase):
    """« Pas ici », « Ne plus proposer » and « Ne jamais proposer la
    catégorie »: what each writes, where its message is said."""

    def test_not_here_from_the_list(self):
        response = self.post(
            EXCLUDE,
            article=self.made.syrup.pk,
            chez=self.made.wholesaler.pk,
            fournisseur=self.made.wholesaler.pk,
            retour="liste",
        )
        self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "a-acheter"))
        self.assertEqual(
            list(ShoppingExclusion.objects.values_list("stock_type", "category", "supplier")),
            [(self.made.syrup.pk, None, self.made.wholesaler.pk)],
        )
        html = response.content.decode()
        self.assertEqual(said_above_the_list(html), ["« Sirop exemple » ne sera plus proposé chez Grossiste exemple."])
        self.assertEqual(said_at_the_top(html), [])
        self.assertNotIn("Sirop exemple", titles_in(table_of(html, TO_BUY)))
        self.assertFalse(opened(fold_of(html, "exclusions")))
        # Never sent back to the store it is left out at: bought nowhere
        # else, it is due at no other store's list either.
        grocer = self.html(fournisseur=self.made.grocer.pk)
        self.assertNotIn("Sirop exemple", titles_in(table_of(grocer, DUE_ELSEWHERE)))

    def test_never_again_from_due_elsewhere(self):
        """« Ne plus proposer » on a line of « À acheter ailleurs », posted
        as the page draws it: left out everywhere, said in « Exclusions »,
        gone from the fold and from its usual store's list."""
        (form,) = forms_to(table_of(self.html(), DUE_ELSEWHERE), EXCLUDE)
        response = self.post(EXCLUDE, **hidden_of(form))
        self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "exclusions"))
        self.assertEqual(
            list(ShoppingExclusion.objects.values_list("stock_type", "category", "supplier")),
            [(self.made.lemon.pk, None, None)],
        )
        html = response.content.decode()
        self.assertEqual(said_in(fold_of(html, "exclusions")), ["« Citron exemple » ne sera plus proposé."])
        self.assertEqual(table_of(html, DUE_ELSEWHERE), "")
        self.assertNotIn("Citron exemple", titles_in(table_of(self.html(fournisseur=self.made.grocer.pk), TO_BUY)))

    def test_a_deposit_with_no_category_is_left_out_by_name(self):
        """A deposit-like article filed under no category has no category to
        exclude: « Ne plus proposer « … » », everywhere, asked first - its
        article and the store to come back to, nothing else."""
        StockType.objects.filter(pk=self.made.keg.pk).update(category="")
        fold = fold_of(self.html(), "exclusions")
        self.assertIn("Ces articles sont surtout rendus (consignes ?) : Fût exemple.", readable(fold))
        self.assertNotIn("Exclure la catégorie", readable(fold))
        (form,) = [form for form in forms_to(fold, EXCLUDE) if "Ne plus proposer « " in unescape(form)]
        self.assertEqual(
            hidden_of(form), {"article": str(self.made.keg.pk), "fournisseur": str(self.made.wholesaler.pk)}
        )
        self.assertEqual(unescape(confirm_of(form)), "Ne plus proposer « Fût exemple » dans aucune enseigne ?")
        self.assertIn("Ne plus proposer « Fût exemple »", readable(form))
        response = self.post(EXCLUDE, **hidden_of(form))
        self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "exclusions"))
        self.assertTrue(ShoppingExclusion.objects.filter(stock_type=self.made.keg, supplier=None).exists())
        self.assertEqual(ShoppingExclusion.objects.count(), 1)
        fold = fold_of(response.content.decode(), "exclusions")
        self.assertEqual(said_in(fold), ["« Fût exemple » ne sera plus proposé."])
        self.assertNotIn("consignes ?", readable(fold))

    def test_not_here_from_a_fold_is_said_in_exclusions(self):
        response = self.post(
            EXCLUDE,
            article=self.made.olives.pk,
            chez=self.made.wholesaler.pk,
            fournisseur=self.made.wholesaler.pk,
            retour="exclusions",
        )
        self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "exclusions"))
        fold = fold_of(response.content.decode(), "exclusions")
        self.assertTrue(opened(fold))
        self.assertEqual(said_in(fold), ["« Olives exemple » ne sera plus proposé chez Grossiste exemple."])
        self.assertIn("Olives exemple pas chez Grossiste exemple", readable(fold))

    def test_never_again_keeps_the_article_s_store_rows(self):
        """Left out everywhere, the article keeps its « Pas ici » rows: its
        « Annuler » puts back exactly what was there before (UndoTests)."""
        ShoppingExclusion.objects.create(stock_type=self.made.rum, supplier=self.made.wholesaler)
        ShoppingExclusion.objects.create(stock_type=self.made.rum, supplier=self.made.grocer)
        response = self.post(EXCLUDE, article=self.made.rum.pk, fournisseur=self.made.wholesaler.pk)
        rows = sorted(
            ShoppingExclusion.objects.values_list("stock_type", "category", "supplier"), key=lambda row: row[2] or 0
        )
        self.assertEqual(
            rows,
            sorted(
                [
                    (self.made.rum.pk, None, None),
                    (self.made.rum.pk, None, self.made.wholesaler.pk),
                    (self.made.rum.pk, None, self.made.grocer.pk),
                ],
                key=lambda row: row[2] or 0,
            ),
        )
        html = response.content.decode()
        fold = fold_of(html, "exclusions")
        self.assertEqual(said_in(fold), ["« Rhum exemple » ne sera plus proposé."])
        self.assertIn("Rhum exemple pas chez Grossiste exemple · exclu partout aussi", readable(fold))
        self.assertEqual(table_of(html, QUIET), "")
        # Twice: still those rows.
        self.post(EXCLUDE, article=self.made.rum.pk, fournisseur=self.made.wholesaler.pk)
        self.assertEqual(ShoppingExclusion.objects.count(), 3)

    def test_a_category(self):
        html = self.html()
        (picker,) = [form for form in forms_to(fold_of(html, "exclusions"), EXCLUDE) if 'name="categorie">' in form]
        self.assertIn("Ne jamais proposer la catégorie", readable(picker))
        self.assertIn('<option value="Consignes exemple">Consignes exemple (1)</option>', picker)
        self.assertEqual(confirm_of(picker), "Ne plus proposer aucun article de cette catégorie ?")
        response = self.post(EXCLUDE, categorie="Consignes exemple", fournisseur=self.made.wholesaler.pk)
        self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "exclusions"))
        self.assertTrue(ShoppingExclusion.objects.filter(category="Consignes exemple", stock_type=None).exists())
        fold = fold_of(response.content.decode(), "exclusions")
        self.assertEqual(said_in(fold), ["Catégorie « Consignes exemple » : plus proposée."])
        # The keg was its only article: no deposit hint left.
        self.assertNotIn("consignes ?", readable(fold))
        self.assertNotIn('<option value="Consignes exemple">', fold)

    def test_the_articles_with_no_category(self):
        response = self.post(EXCLUDE, categorie="", fournisseur=self.made.wholesaler.pk)
        self.assertTrue(ShoppingExclusion.objects.filter(category="").exists())
        html = response.content.decode()
        self.assertEqual(said_in(fold_of(html, "exclusions")), ["Catégorie non renseignée : plus proposée."])
        self.assertEqual(titles_in(table_of(html, TO_BUY)), ["Bière exemple"])

    def test_the_deposit_hint(self):
        fold = fold_of(self.html(), "exclusions")
        self.assertIn("Exclusions (0) · 1 consigne ?", readable(fold))
        self.assertIn("Ces articles sont surtout rendus (consignes ?) : Fût exemple.", readable(fold))
        (form,) = [form for form in forms_to(fold, EXCLUDE) if 'name="categorie" value=' in form]
        self.assertEqual(hidden_of(form)["categorie"], "Consignes exemple")
        self.assertEqual(
            unescape(confirm_of(form)), "Ne plus proposer aucun article de la catégorie « Consignes exemple » ?"
        )
        self.assertIn("Exclure la catégorie « Consignes exemple »", readable(form))

    def test_refused_said_where_it_lands_and_nothing_written(self):
        for data, said, where in (
            ({"article": "abc", "retour": "liste"}, "Article introuvable : rien n'a été exclu.", "list"),
            ({"article": "999999"}, "Article introuvable : rien n'a été exclu.", "fold"),
            (
                {"article": self.made.beer.pk, "chez": "999999", "retour": "liste"},
                "Enseigne introuvable : rien n'a été exclu.",
                "list",
            ),
            ({"article": self.made.beer.pk, "chez": ""}, "Enseigne introuvable : rien n'a été exclu.", "fold"),
            ({"categorie": "Inconnue exemple"}, "Catégorie introuvable : rien n'a été exclu.", "fold"),
            ({}, "Catégorie introuvable : rien n'a été exclu.", "fold"),
        ):
            with self.subTest(data=data):
                html = self.post(EXCLUDE, fournisseur=self.made.wholesaler.pk, **data).content.decode()
                placed = said_above_the_list(html) if where == "list" else said_in(fold_of(html, "exclusions"))
                self.assertEqual(placed, [said])
                self.assertFalse(ShoppingExclusion.objects.exists())

    def test_a_supplier_of_charges_is_no_store(self):
        self.made.grocer.expenses_only = True
        self.made.grocer.save(update_fields=["expenses_only"])
        html = self.post(EXCLUDE, article=self.made.coffee.pk, chez=self.made.grocer.pk).content.decode()
        self.assertEqual(said_in(fold_of(html, "exclusions")), ["Enseigne introuvable : rien n'a été exclu."])
        self.assertFalse(ShoppingExclusion.objects.exists())


class IncludeTests(ShoppingPageTestCase):
    """« Réinclure », each exclusion on its line of the fold."""

    def test_each_line_its_words_and_its_button(self):
        ShoppingExclusion.objects.create(category="Bières exemple")
        ShoppingExclusion.objects.create(stock_type=self.made.beer)
        ShoppingExclusion.objects.create(stock_type=self.made.syrup)
        ShoppingExclusion.objects.create(stock_type=self.made.syrup, supplier=self.made.grocer)
        fold = fold_of(self.html(), "exclusions")
        self.assertIn("Exclusions (4)", readable(fold))
        lines = re.findall(
            r"<li>(.*?)</li>", re.search(r'<ul class="exclusion-list">(.*?)</ul>', fold, re.DOTALL).group(1), re.DOTALL
        )
        self.assertEqual(
            [readable(re.sub(r"<form\b.*?</form>", "", line, flags=re.DOTALL)) for line in lines],
            [
                "Catégorie « Bières exemple » · 1 article",
                "Bière exemple catégorie exclue aussi",
                "Sirop exemple",
                "Sirop exemple pas chez Épicerie exemple · exclu partout aussi",
            ],
        )
        for line in lines:
            (form,) = forms_to(line, INCLUDE)
            self.assertIn('<button class="btn btn-small btn-secondary" type="submit">Réinclure</button>', form)
            self.assertEqual(hidden_of(form)["fournisseur"], str(self.made.wholesaler.pk))

    def test_taken_back(self):
        """Each alone: nothing else keeps the article out."""
        for fields, said in (
            ({"stock_type": self.made.rum}, "Réinclus : « Rhum exemple »."),
            (
                {"stock_type": self.made.syrup, "supplier": self.made.wholesaler},
                "Réinclus chez Grossiste exemple : « Sirop exemple ».",
            ),
            ({"category": "Consignes exemple"}, "Réinclus : catégorie « Consignes exemple »."),
            ({"category": ""}, "Réinclus : catégorie non renseignée."),
        ):
            with self.subTest(said=said):
                exclusion = ShoppingExclusion.objects.create(**fields)
                response = self.post(INCLUDE, exclusion=exclusion.pk, fournisseur=self.made.wholesaler.pk)
                self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "exclusions"))
                fold = fold_of(response.content.decode(), "exclusions")
                self.assertTrue(opened(fold))
                self.assertEqual(said_in(fold), [said])
                self.assertFalse(ShoppingExclusion.objects.filter(pk=exclusion.pk).exists())
        self.assertIn("Sirop exemple", titles_in(table_of(self.html(), TO_BUY)))

    def test_gone(self):
        exclusion = ShoppingExclusion.objects.create(stock_type=self.made.rum)
        pk = exclusion.pk
        exclusion.delete()
        html = self.post(INCLUDE, exclusion=pk, fournisseur=self.made.wholesaler.pk).content.decode()
        self.assertEqual(said_in(fold_of(html, "exclusions")), [GONE])

    def test_still_out_through_its_category_or_everywhere(self):
        ShoppingExclusion.objects.create(category="Bières exemple")
        beer = ShoppingExclusion.objects.create(stock_type=self.made.beer)
        ShoppingExclusion.objects.create(stock_type=self.made.syrup)
        syrup_here = ShoppingExclusion.objects.create(stock_type=self.made.syrup, supplier=self.made.wholesaler)
        for exclusion, said in (
            (beer, "« Bière exemple » reste exclu : sa catégorie « Bières exemple » l'est aussi."),
            (syrup_here, "« Sirop exemple » reste exclu : il l'est aussi partout."),
        ):
            with self.subTest(said=said):
                response = self.post(INCLUDE, exclusion=exclusion.pk, fournisseur=self.made.wholesaler.pk)
                html = response.content.decode()
                self.assertEqual(said_in(fold_of(html, "exclusions")), [said])
                self.assertIn('<li class="message message-warning">', fold_of(html, "exclusions"))
                self.assertFalse(ShoppingExclusion.objects.filter(pk=exclusion.pk).exists())
                self.assertEqual(table_of(html, TO_BUY), "")


def exclusions_fold(html: str) -> str:
    return fold_of(html, "exclusions")


class UndoTests(ShoppingPageTestCase):
    """« Annuler »: an exclusion just made - « Pas ici », « Ne plus
    proposer », « Ne jamais proposer », a category - is taken back from its
    own message, posted as drawn, its answer said where it was pressed, the
    page as it was before. Only the exclusion that very answer made, while
    it exists: never an older one a double submit found already there."""

    def not_here(self, html: str, label: str, article) -> str:
        """« Pas ici » on `article`'s line of the table `label`, as drawn."""
        (form,) = forms_to(row_of(table_of(html, label), article.name), EXCLUDE)
        return form

    def test_not_here_undone_from_the_list(self):
        wholesaler = str(self.made.wholesaler.pk)
        for typed in ({}, {"dans": "30"}):
            with self.subTest(**typed):
                form = self.not_here(self.html(**typed), TO_BUY, self.made.syrup)
                html = self.post(EXCLUDE, **hidden_of(form)).content.decode()
                exclusion = ShoppingExclusion.objects.get()
                self.assertEqual(
                    said_above_the_list(html), ["« Sirop exemple » ne sera plus proposé chez Grossiste exemple."]
                )
                (undo,) = undo_forms(above_the_list(html))
                self.assertEqual(undo_forms(html), [undo])
                self.assertEqual(
                    hidden_of(undo),
                    {"exclusion": str(exclusion.pk), "retour": "liste", "fournisseur": wholesaler, **typed},
                )
                self.assertIn(UNDO_BUTTON, undo)
                # It is the undo: nothing asked first.
                self.assertEqual(confirm_of(undo), "")
                response = self.post(INCLUDE, **hidden_of(undo))
                self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "a-acheter", **typed))
                html = response.content.decode()
                self.assertEqual(said_above_the_list(html), ["Réinclus chez Grossiste exemple : « Sirop exemple »."])
                self.assertEqual(undo_forms(html), [])
                self.assertFalse(ShoppingExclusion.objects.exists())
                self.assertIn("Sirop exemple", titles_in(table_of(html, TO_BUY)))

    def test_not_here_undone_from_a_fold(self):
        """« Pas ici » in « Peut-être » and « Nouveaux ici » is said in
        « Exclusions »: its « Annuler » is there, said there, and the line is
        back in its fold."""
        wholesaler = str(self.made.wholesaler.pk)
        for label, article in ((MAYBE, self.made.olives), (NEW, self.made.crisps)):
            with self.subTest(section=label):
                response = self.post(EXCLUDE, **hidden_of(self.not_here(self.html(), label, article)))
                exclusion = ShoppingExclusion.objects.get()
                fold = exclusions_fold(response.content.decode())
                self.assertEqual(said_in(fold), [f"« {article.name} » ne sera plus proposé chez Grossiste exemple."])
                (undo,) = undo_forms(fold)
                self.assertEqual(
                    hidden_of(undo), {"exclusion": str(exclusion.pk), "retour": "exclusions", "fournisseur": wholesaler}
                )
                response = self.post(INCLUDE, **hidden_of(undo))
                self.assertEqual(self.landing(response), self.list_of(self.made.wholesaler, "exclusions"))
                html = response.content.decode()
                self.assertEqual(
                    said_in(exclusions_fold(html)), [f"Réinclus chez Grossiste exemple : « {article.name} »."]
                )
                self.assertFalse(ShoppingExclusion.objects.exists())
                self.assertIn(article.name, titles_in(table_of(html, label)))

    def test_never_again_undone_leaves_the_not_here_rows(self):
        """« Ne plus proposer » over an article left out at two stores:
        undone, it is out at those two again - their rows were kept."""
        here = ShoppingExclusion.objects.create(stock_type=self.made.rum, supplier=self.made.wholesaler)
        there = ShoppingExclusion.objects.create(stock_type=self.made.rum, supplier=self.made.grocer)
        response = self.post(EXCLUDE, article=self.made.rum.pk, fournisseur=self.made.wholesaler.pk)
        everywhere = ShoppingExclusion.objects.get(stock_type=self.made.rum, supplier=None)
        (undo,) = undo_forms(exclusions_fold(response.content.decode()))
        self.assertEqual(hidden_of(undo)["exclusion"], str(everywhere.pk))
        response = self.post(INCLUDE, **hidden_of(undo))
        fold = exclusions_fold(response.content.decode())
        self.assertEqual(said_in(fold), ["Réinclus : « Rhum exemple »."])
        self.assertEqual(sorted(ShoppingExclusion.objects.values_list("pk", flat=True)), sorted([here.pk, there.pk]))
        self.assertIn("Rhum exemple pas chez Grossiste exemple", readable(fold))
        self.assertNotIn("exclu partout aussi", readable(fold))

    def test_a_category_undone(self):
        html = self.html()
        (picker,) = [form for form in forms_to(exclusions_fold(html), EXCLUDE) if 'name="categorie">' in form]
        response = self.post(EXCLUDE, **hidden_of(picker), categorie="Consignes exemple")
        exclusion = ShoppingExclusion.objects.get()
        (undo,) = undo_forms(exclusions_fold(response.content.decode()))
        self.assertEqual(
            hidden_of(undo),
            {"exclusion": str(exclusion.pk), "retour": "exclusions", "fournisseur": str(self.made.wholesaler.pk)},
        )
        response = self.post(INCLUDE, **hidden_of(undo))
        fold = exclusions_fold(response.content.decode())
        self.assertEqual(said_in(fold), ["Réinclus : catégorie « Consignes exemple »."])
        self.assertFalse(ShoppingExclusion.objects.exists())
        self.assertIn("Ces articles sont surtout rendus (consignes ?) : Fût exemple.", readable(fold))

    def test_never_propose_undone_on_the_rhythm(self):
        """« Ne jamais proposer » says its message at the top of « Rythme
        d'achat », its « Annuler » in it, which comes back there - with its
        store, or every store."""
        for query in ({"fournisseur": str(self.made.wholesaler.pk)}, {}):
            with self.subTest(**query):
                html = self.html(RHYTHM, **query)
                (form,) = forms_to(row_of(table_of(html, RHYTHM_TABLE), "<td>Rhum exemple"), EXCLUDE)
                html = self.post(EXCLUDE, **hidden_of(form)).content.decode()
                exclusion = ShoppingExclusion.objects.get()
                self.assertEqual(said_at_the_top(html), ["« Rhum exemple » ne sera plus proposé."])
                (undo,) = undo_forms(at_the_top(html))
                self.assertEqual(hidden_of(undo), {"exclusion": str(exclusion.pk), "retour": "rythme", **query})
                self.assertIn(UNDO_BUTTON, undo)
                response = self.post(INCLUDE, **hidden_of(undo))
                rhythm = reverse(RHYTHM) + (f"?fournisseur={query['fournisseur']}" if query else "")
                self.assertEqual(self.landing(response), rhythm)
                html = response.content.decode()
                self.assertEqual(said_at_the_top(html), ["Réinclus : « Rhum exemple »."])
                self.assertEqual(undo_forms(html), [])
                self.assertFalse(ShoppingExclusion.objects.exists())
                self.assertEqual(len(forms_to(row_of(table_of(html, RHYTHM_TABLE), "<td>Rhum exemple"), EXCLUDE)), 1)

    def test_no_undo_for_an_exclusion_already_there(self):
        """A double submit, or a form drawn before: the second answer finds
        the exclusion made already and offers no « Annuler » - it would take
        back the first decision."""
        wholesaler = self.made.wholesaler.pk
        for data, where in (
            ({"article": self.made.syrup.pk, "chez": wholesaler, "retour": "liste"}, above_the_list),
            ({"article": self.made.rum.pk}, exclusions_fold),
            ({"categorie": "Consignes exemple"}, exclusions_fold),
        ):
            with self.subTest(data=data):
                first = where(self.post(EXCLUDE, fournisseur=wholesaler, **data).content.decode())
                self.assertEqual(len(undo_forms(first)), 1)
                second = where(self.post(EXCLUDE, fournisseur=wholesaler, **data).content.decode())
                self.assertEqual(len(said_in(second)), 1)
                self.assertEqual(undo_forms(second), [])
        self.assertEqual(ShoppingExclusion.objects.count(), 3)

    def test_no_undo_once_the_exclusion_is_gone(self):
        """Taken back twice - two taps, two tabs -: the second says it no
        longer exists, with no « Annuler ». Gone before its message is drawn
        (taken back from « Exclusions » meanwhile): the message alone."""
        wholesaler = self.made.wholesaler.pk
        data = {"article": self.made.syrup.pk, "chez": wholesaler, "fournisseur": wholesaler, "retour": "liste"}
        (undo,) = undo_forms(above_the_list(self.post(EXCLUDE, **data).content.decode()))
        self.post(INCLUDE, **hidden_of(undo))
        html = self.post(INCLUDE, **hidden_of(undo)).content.decode()
        self.assertEqual(said_above_the_list(html), [GONE])
        self.assertEqual(undo_forms(html), [])
        self.assertEqual(self.client.post(reverse(EXCLUDE), data).status_code, 302)
        ShoppingExclusion.objects.all().delete()
        html = self.html(fournisseur=wholesaler)
        self.assertEqual(said_above_the_list(html), ["« Sirop exemple » ne sera plus proposé chez Grossiste exemple."])
        self.assertEqual(undo_forms(html), [])

    def test_markup_in_a_name_is_printed_as_text_beside_its_undo(self):
        StockType.objects.filter(pk=self.made.syrup.pk).update(name='Sirop <i>exemple</i> "test"')
        wholesaler = self.made.wholesaler.pk
        response = self.post(
            EXCLUDE, article=self.made.syrup.pk, chez=wholesaler, fournisseur=wholesaler, retour="liste"
        )
        html = response.content.decode()
        self.assertNotIn("<i>exemple</i>", html)
        self.assertIn("« Sirop &lt;i&gt;exemple&lt;/i&gt; &quot;test&quot; » ne sera plus proposé", html)
        self.assertEqual(
            said_above_the_list(html), ['« Sirop <i>exemple</i> "test" » ne sera plus proposé chez Grossiste exemple.']
        )
        self.assertEqual(len(undo_forms(above_the_list(html))), 1)


class RhythmPageTests(ShoppingPageTestCase):
    """« Rythme d'achat »: every article bought, how regularly and where."""

    def cells(self, html: str, name: str) -> dict[str, str]:
        """{header: what the row of `name` says under it}."""
        table = table_of(html, RHYTHM_TABLE)
        headers = [readable(found) for found in re.findall(r"<th[^>]*>(.*?)</th>", table, flags=re.DOTALL)]
        row = row_of(table, f"<td>{name}")
        return dict(zip(headers, [unescape(cell) for cell in cells_of(row)], strict=True))

    def test_every_store(self):
        html = self.html(RHYTHM)
        table = table_of(html, RHYTHM_TABLE)
        self.assertEqual(len(body_rows(table)), 9)
        self.assertNotIn("Habitude ici", table)
        beer = self.cells(html, "Bière exemple")
        self.assertEqual(beer["État"], "régulier")
        self.assertEqual(beer["Rythme"], "environ tous les 7 jours")
        self.assertEqual(beer["Dernier achat"], day_of(7))
        self.assertEqual(beer["Où"], "Grossiste exemple 100 %")
        self.assertEqual(beer["Quantité habituelle"], "24 u.")
        self.assertEqual(beer["Caisse / semaine"], "")
        coffee = self.cells(html, "Café exemple")
        self.assertEqual(coffee["Où"], "Épicerie exemple 75 % · Grossiste exemple 25 %")
        self.assertEqual(coffee["Quantité habituelle"], "1 kg")
        self.assertEqual(self.cells(html, "Rhum exemple")["État"], "plus acheté ?")
        self.assertEqual(self.cells(html, "Fût exemple")["État"], "consigne ?")
        # « Prochain achat estimé » is « Dernier achat » + « Rythme »: the
        # beer's is today; none for an article no longer bought or a deposit.
        self.assertEqual(beer["Prochain achat estimé"], "à racheter")
        syrup = self.cells(html, "Sirop exemple")
        self.assertEqual((syrup["Rythme"], syrup["Dernier achat"]), ("environ tous les 14 jours", day_of(7)))
        self.assertEqual(syrup["Prochain achat estimé"], day_of(-7))
        self.assertEqual(self.cells(html, "Rhum exemple")["Prochain achat estimé"], "")
        self.assertEqual(self.cells(html, "Fût exemple")["Prochain achat estimé"], "")
        # The olives, bought once: no rhythm, no next purchase.
        self.assertEqual(self.cells(html, "Olives exemple")["Prochain achat estimé"], "")
        self.assertIn(
            'data-sort="' + f"{timezone.localdate() - timedelta(days=7):%Y-%m-%d}" + '"',
            row_of(table, "<td>Bière exemple"),
        )

    def test_one_store_with_the_habit_there(self):
        html = self.html(RHYTHM, fournisseur=self.made.wholesaler.pk)
        table = table_of(html, RHYTHM_TABLE)
        self.assertEqual(
            sorted(titles_in(table)),
            [
                "Bière exemple",
                "Café exemple",
                "Chips exemple",
                "Fût exemple",
                "Olives exemple",
                "Rhum exemple",
                "Sirop exemple",
            ],
        )
        self.assertEqual(self.cells(html, "Bière exemple")["Habitude ici"], "25 fois sur 25 passages")
        self.assertIn(f'href="{reverse(PAGE)}?fournisseur={self.made.wholesaler.pk}"', html)

    def test_an_unknown_store_shows_every_store(self):
        html = self.html(RHYTHM, fournisseur="999999")
        self.assertEqual(warnings_of(html), ["Enseigne introuvable : voici toutes les enseignes."])
        self.assertEqual(len(body_rows(table_of(html, RHYTHM_TABLE))), 9)

    def test_never_propose_from_the_rhythm(self):
        html = self.html(RHYTHM, fournisseur=self.made.wholesaler.pk)
        (form,) = forms_to(row_of(table_of(html, RHYTHM_TABLE), "<td>Rhum exemple"), EXCLUDE)
        self.assertEqual(
            hidden_of(form),
            {"article": str(self.made.rum.pk), "fournisseur": str(self.made.wholesaler.pk), "retour": "rythme"},
        )
        self.assertEqual(unescape(confirm_of(form)), "Ne plus proposer « Rhum exemple » dans aucune enseigne ?")
        response = self.post(EXCLUDE, **hidden_of(form))
        self.assertEqual(self.landing(response), f"{reverse(RHYTHM)}?fournisseur={self.made.wholesaler.pk}")
        html = response.content.decode()
        self.assertEqual(said_at_the_top(html), ["« Rhum exemple » ne sera plus proposé."])
        rum = self.cells(html, "Rhum exemple")
        self.assertEqual(rum[""], "jamais proposé")


class PageCostTests(TestCase):
    """The list costs the same queries whatever the history: one scan of the
    purchases and, with the till, a fixed number of reads (shopping_data's
    docstring) - never a query per article, store or day."""

    def setUp(self):
        self.made = make_shopping_history()
        self.drink = make_recipe(name="Sirop à l'eau exemple", selling_price_ttc="32.00")
        make_ingredient(self.drink, stock_type=self.made.syrup, quantity="0.04")
        self.today = timezone.localdate()
        RecipeSale.objects.create(
            recipe=self.drink, sold_on=self.today - timedelta(days=9), quantity=12, source="caisse"
        )

    def queries(self, name=PAGE) -> list[str]:
        self.client.get(reverse(name))  # the session and the login, settled once
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse(name), {"fournisseur": self.made.wholesaler.pk})
        self.assertEqual(response.status_code, 200)
        return [query["sql"] for query in captured]

    def more_history(self):
        """More articles, at more stores, over many more days and sales."""
        today = self.today
        for number in range(6):
            store = self.made.grocer if number % 2 else self.made.market
            article = make_stock_type(unit=UnitChoices.UNIT)
            product = make_product(supplier=store, stock_type=article)
            for week in range(1, 12):
                invoice = make_invoice(supplier=store, invoice_date=today - timedelta(days=7 * week + number))
                line = make_invoice_line(invoice=invoice, product=product, quantity=2, total_ht="44.00")
                make_movement(stock_type=article, quantity="2", invoice_line=line, kind=MovementKind.PURCHASE)
            make_ingredient(self.drink, stock_type=article, quantity="0.01", group=number + 1)
        for days_ago in range(10, 60, 3):
            RecipeSale.objects.create(
                recipe=self.drink, sold_on=today - timedelta(days=days_ago), quantity=5, source="caisse"
            )

    def test_more_history_costs_no_more_queries_with_the_till(self):
        few = len(self.queries())
        self.more_history()
        self.assertEqual(len(self.queries()), few)

    def test_more_history_costs_no_more_queries_without_the_till(self):
        ShoppingSetting.objects.create(pk=ShoppingSetting.SINGLETON_PK, use_till=False)
        few = len(self.queries())
        self.more_history()
        self.assertEqual(len(self.queries()), few)

    def test_the_rhythm_costs_no_more_either(self):
        few = len(self.queries(RHYTHM))
        self.more_history()
        self.assertEqual(len(self.queries(RHYTHM)), few)

    def test_the_till_left_out_reads_no_sale(self):
        ShoppingSetting.objects.create(pk=ShoppingSetting.SINGLETON_PK, use_till=False)
        read = " ".join(self.queries())
        for table in ("recipes_recipesale", "recipes_saledocumentline", "invoices_gathercoverage"):
            with self.subTest(table=table):
                self.assertNotIn(table, read)

    def test_a_longer_shopping_list_costs_no_more_queries(self):
        """The store's open list is read once, whatever it holds."""
        shopping_list = ShoppingList.objects.create(supplier=self.made.wholesaler)
        for article in (self.made.beer, self.made.syrup):
            ShoppingListItem.objects.create(
                shopping_list=shopping_list, stock_type=article, label=article.name, quantity=Decimal("2")
            )
        few = len(self.queries())
        for number in range(18):
            ShoppingListItem.objects.create(
                shopping_list=shopping_list, label=f"Article {number} exemple", quantity=Decimal("1")
            )
        self.assertEqual(len(self.queries()), few)


def list_cell_of(row: str) -> str:
    """A row's « Liste » cell, its inside."""
    found = re.search(
        r'<td class="shopping-add-cell phone-card-wide" data-label="Liste">(.*?)</td>', row, flags=re.DOTALL
    )
    return found.group(1) if found else ""


class ListColumnTests(ShoppingPageTestCase):
    """« Liste »: each line of « À acheter », « Peut-être » and « Nouveaux
    ici » goes on the store's shopping list with the figures the line shows
    - its quantity may be changed first -, or says it is there; « Tout
    ajouter (N) » and « Liste de courses (N) » above. What those forms do:
    test_shopping_lists_page (ForecastAddTests, AddAllTests)."""

    def add_form(self, html: str, label: str, name: str) -> str:
        (form,) = forms_to(list_cell_of(row_of(table_of(html, label), name)), LIST_ADD)
        return form

    def product_of_article(self, article) -> str:
        return str(article.products.get(supplier=self.made.wholesaler).pk)

    def test_each_line_s_form(self):
        html = self.html()
        wholesaler = str(self.made.wholesaler.pk)
        beer_product = str(Product.objects.get(raw_name=SHOPPING_BEER_PRODUCT).pk)
        for label, article, landing, hidden, quantity in (
            (TO_BUY, self.made.beer, "liste", {"produit": beer_product, "colis": "24"}, "24"),
            (TO_BUY, self.made.syrup, "liste", {"produit": self.product_of_article(self.made.syrup)}, "2"),
            (MAYBE, self.made.olives, "peut-etre", {"produit": self.product_of_article(self.made.olives)}, "1"),
            (NEW, self.made.crisps, "nouveaux", {"produit": self.product_of_article(self.made.crisps)}, "1"),
        ):
            with self.subTest(section=label, article=article.name):
                form = self.add_form(html, label, article.name)
                self.assertIn('class="shopping-add-line"', form)
                self.assertEqual(
                    hidden_of(form),
                    {"article": str(article.pk), **hidden, "fournisseur": wholesaler, "retour": landing},
                )
                self.assertIn(
                    f'<input type="text" name="quantite" value="{quantity}" inputmode="decimal" size="4" '
                    'aria-label="Quantité à ajouter">',
                    form,
                )
                self.assertIn('<button class="btn btn-small" type="submit">Ajouter</button>', form)
                # It counts the product « Produit » names: no unit beside it.
                self.assertEqual(readable(form), "Ajouter")
        # Neither « Plus acheté ? » nor « Acheté ailleurs maintenant » has a quantity, nor a « Liste ».
        for label in (QUIET, ELSEWHERE):
            with self.subTest(section=label):
                self.assertEqual(forms_to(table_of(html, label), LIST_ADD), [])
                self.assertNotIn('data-label="Liste"', table_of(html, label))

    def test_a_typed_horizon_rides_along(self):
        form = self.add_form(self.html(dans="60"), TO_BUY, "Bière exemple")
        times = math.floor(60 / 7 + 0.5)
        self.assertEqual(hidden_of(form)["dans"], "60")
        self.assertIn(f'name="quantite" value="{24 * times}"', form)

    def test_bought_by_measure_counts_the_article(self):
        InvoiceLine.objects.filter(product__stock_type=self.made.olives).update(quantity=Decimal("1.5"))
        StockMovement.objects.filter(stock_type=self.made.olives).update(quantity=Decimal("1.5"))
        form = self.add_form(self.html(), MAYBE, "Olives exemple")
        self.assertEqual(
            hidden_of(form),
            {
                "article": str(self.made.olives.pk),
                "fournisseur": str(self.made.wholesaler.pk),
                "retour": "peut-etre",
            },
        )
        self.assertIn('name="quantite" value="1.5"', form)
        self.assertEqual(readable(form), "kg Ajouter")

    def test_a_line_already_listed_says_so(self):
        """An item still to buy is « Dans la liste (24) », a link to the list
        and no form. One ticked as bought (« pris ») on a list nobody
        finished is no longer in the list: « Pris (2 L) » beside its
        « Ajouter », which puts it back to buy."""
        make_shopping_lists(self.made)
        html = self.html()
        page = f"{reverse(LIST_PAGE)}?fournisseur={self.made.wholesaler.pk}"
        cell = list_cell_of(row_of(table_of(html, TO_BUY), "Bière exemple"))
        self.assertEqual(forms_to(cell, LIST_ADD), [])
        self.assertEqual(cell.strip(), f'<a href="{page}">Dans la liste (24)</a>')
        # The fixture's syrup is ticked: taken, and offered again.
        cell = list_cell_of(row_of(table_of(html, TO_BUY), "Sirop exemple"))
        self.assertNotIn("Dans la liste", cell)
        self.assertTrue(cell.strip().startswith('<span class="muted small">Pris (2 L)</span>'), cell[:120])
        self.assertEqual(len(forms_to(cell, LIST_ADD)), 1)
        # Not on it: its form alone.
        self.assertNotIn("Pris", list_cell_of(row_of(table_of(html, MAYBE), "Olives exemple")))
        self.assertTrue(self.add_form(html, MAYBE, "Olives exemple"))

    def test_listed_says_what_the_item_counts(self):
        """« Dans la liste (…) » and « Pris (…) » say the item's number in the
        words the list says it: bottles of a size, packets of a weight, the
        article's measure. Read with the open list, in its one query."""
        shopping_list = ShoppingList.objects.create(supplier=self.made.wholesaler)
        ShoppingListItem.objects.create(
            shopping_list=shopping_list,
            stock_type=self.made.syrup,
            label=self.made.syrup.name,
            quantity=Decimal("3"),
            product_name="SIROP EXEMPLE 70CL",
            item_size=Decimal("0.7"),
            size_unit=UnitChoices.LITRE,
        )
        ShoppingListItem.objects.create(
            shopping_list=shopping_list,
            stock_type=self.made.olives,
            label=self.made.olives.name,
            quantity=Decimal("2"),
            item_size=Decimal("0.5"),
            size_unit=UnitChoices.KILOGRAM,
            checked_at=timezone.now(),
        )
        html = self.html()
        page = f"{reverse(LIST_PAGE)}?fournisseur={self.made.wholesaler.pk}"
        cell = list_cell_of(row_of(table_of(html, TO_BUY), "Sirop exemple"))
        self.assertEqual(cell.strip(), f'<a href="{page}">Dans la liste (3 bouteilles de 70 cl)</a>')
        cell = list_cell_of(row_of(table_of(html, MAYBE), "Olives exemple"))
        self.assertTrue(
            cell.strip().startswith('<span class="muted small">Pris (2 paquets de 500 g)</span>'), cell[:120]
        )
        # Changed to litres on the card, then ticked: the measure.
        ShoppingListItem.objects.filter(stock_type=self.made.syrup).update(
            quantity=Decimal("2"), unit=UnitChoices.LITRE, item_size=None, size_unit="", checked_at=timezone.now()
        )
        cell = list_cell_of(row_of(table_of(self.html(), TO_BUY), "Sirop exemple"))
        self.assertTrue(cell.strip().startswith('<span class="muted small">Pris (2 L)</span>'), cell[:120])

    def test_listed_says_a_carton_is_a_pack(self):
        """An item counting cartons of its product (six 75 cl bottles, 4.5 L
        each) is « 1 pack de 4.5 L », as the list says it - never a keg."""
        ShoppingListItem.objects.create(
            shopping_list=ShoppingList.objects.create(supplier=self.made.wholesaler),
            stock_type=self.made.syrup,
            label=self.made.syrup.name,
            quantity=Decimal("1"),
            product_name="SIROP EXEMPLE CARTON 6X75CL",
            item_size=Decimal("4.5"),
            size_unit=UnitChoices.LITRE,
        )
        page = f"{reverse(LIST_PAGE)}?fournisseur={self.made.wholesaler.pk}"
        cell = list_cell_of(row_of(table_of(self.html(), TO_BUY), "Sirop exemple"))
        self.assertEqual(cell.strip(), f'<a href="{page}">Dans la liste (1 pack de 4.5 L)</a>')

    def test_the_list_s_button(self):
        page = f"{reverse(LIST_PAGE)}?fournisseur={self.made.wholesaler.pk}"
        self.assertIn(f'<a class="btn" href="{page}">Liste de courses</a>', self.html())
        make_shopping_lists(self.made)
        # Three items, a free text included.
        self.assertIn(f'<a class="btn" href="{page}">Liste de courses (3)</a>', self.html())
        # The grocer's list is finished: nothing on its next one yet.
        grocer = f"{reverse(LIST_PAGE)}?fournisseur={self.made.grocer.pk}"
        self.assertIn(
            f'<a class="btn" href="{grocer}">Liste de courses</a>', self.html(fournisseur=self.made.grocer.pk)
        )

    def test_add_all_counts_the_lines_not_listed(self):
        html = self.html()
        (form,) = forms_to(html, ADD_ALL)
        self.assertIn('class="inline-form shopping-add-all"', form)
        self.assertEqual(hidden_of(form), {"fournisseur": str(self.made.wholesaler.pk)})
        self.assertEqual(busy_button_of(form), ("Ajout…", "Tout ajouter (2)"))
        # Above the list it adds.
        self.assertLess(html.index(form), html.index(table_of(html, TO_BUY)))
        self.assertEqual(hidden_of(forms_to(self.html(dans="30"), ADD_ALL)[0])["dans"], "30")
        shopping_list = ShoppingList.objects.create(supplier=self.made.wholesaler)
        for article in (self.made.beer, self.made.syrup):
            ShoppingListItem.objects.create(
                shopping_list=shopping_list, stock_type=article, label=article.name, quantity=Decimal("2")
            )
            listed = forms_to(self.html(), ADD_ALL)
            with self.subTest(listed=article.name):
                if article == self.made.beer:
                    self.assertEqual(busy_button_of(listed[0]), ("Ajout…", "Tout ajouter (1)"))
                else:
                    self.assertEqual(listed, [])
        # A ticked item is no longer in the list: counted again.
        ShoppingListItem.objects.filter(stock_type=self.made.syrup).update(checked_at=timezone.now())
        self.assertEqual(busy_button_of(forms_to(self.html(), ADD_ALL)[0]), ("Ajout…", "Tout ajouter (1)"))

    def test_a_fold_gone_says_its_message_at_the_top(self):
        """The olives left out here after their « Ajouter » was drawn: no
        « Peut-être » to say it in, so it is said at the top."""
        form = self.add_form(self.html(), MAYBE, "Olives exemple")
        ShoppingExclusion.objects.create(stock_type=self.made.olives, supplier=self.made.wholesaler)
        response = self.post(LIST_ADD, **{**hidden_of(form), "quantite": "1"})
        html = response.content.decode()
        self.assertEqual(fold_of(html, "peut-etre"), "")
        # Its product is a packet of 1 kg (the fixture's sizes are 1).
        self.assertEqual(said_at_the_top(html), ["« Olives exemple » ajouté à la liste (1 paquet de 1 kg)."])


class ViewerWhoMayNotTuneTests(ShoppingPageTestCase):
    """A login who may not change « Prévoir les courses » - one given the
    shopping lists without « Produits & charges » - reads the list and adds
    to the store's shopping list, and is shown no form he may not post: no
    « Pas ici », « Ne plus proposer », « Réglages », « Exclusions », nor on
    « Rythme d'achat » « Ne jamais proposer »; « Total HT » only with an
    area already showing what articles cost (Access.sees_costs). The
    login's access is handed to the page - the view's `may_tune` and the
    `can` the templates read -, the gate itself being accounts' tests'."""

    def html_as(self, areas, name=PAGE, **params) -> str:
        access = Access(owner=False, areas=areas)
        with (
            patch("inventory.views.access_of", return_value=access),
            patch("accounts.access.access_of", return_value=access),
        ):
            return self.html(name, **params)

    def headers(self, table: str) -> list[str]:
        head = re.search(r"<thead>(.*?)</thead>", table, flags=re.DOTALL).group(1)
        return [readable(cell) for cell in re.findall(r"<th[^>]*>(.*?)</th>", head, flags=re.DOTALL)]

    def test_no_form_he_may_not_post(self):
        html = self.html_as(["stock_takes"])
        for name in (SETTINGS, EXCLUDE, INCLUDE):
            with self.subTest(form=name):
                self.assertEqual(forms_to(html, name), [])
        page = readable(html.split("<main", 1)[1].split("</main>")[0])
        for words in ("Pas ici", "Ne plus proposer", "Total HT", "Réglages", "Exclusions"):
            with self.subTest(words=words):
                self.assertNotIn(words, page)
        self.assertEqual(fold_of(html, "reglages"), "")
        self.assertEqual(fold_of(html, "exclusions"), "")
        # What he may do is all there: the list, its folds, « Ajouter ».
        self.assertEqual(titles_in(table_of(html, TO_BUY)), ["Bière exemple", "Sirop exemple"])
        self.assertEqual(len(forms_to(html, LIST_ADD)), 4)
        self.assertEqual(len(forms_to(html, ADD_ALL)), 1)
        for label, headers in (
            (TO_BUY, ["Article", "Produit", "À acheter", "Chance", "Pourquoi", "Liste"]),
            (QUIET, ["Article", "Pourquoi"]),
            (DUE_ELSEWHERE, ["Article", "Où", "Pourquoi"]),
            (TOP_HERE, ["Article", "Achats (12 mois)"]),
        ):
            with self.subTest(table=label):
                table = table_of(html, label)
                self.assertEqual(self.headers(table), headers)
                self.assertNotIn("row-actions", table)
        rhythm = self.html_as(["stock_takes"], RHYTHM, fournisseur=self.made.wholesaler.pk)
        self.assertEqual(forms_to(rhythm, EXCLUDE), [])
        self.assertNotIn("Ne jamais proposer", rhythm)
        self.assertEqual(self.headers(table_of(rhythm, RHYTHM_TABLE))[-1], "Caisse / semaine")

    def test_an_area_showing_what_articles_cost(self):
        html = self.html_as(["invoices"])
        top = table_of(html, TOP_HERE)
        self.assertEqual(self.headers(top), ["Article", "Achats (12 mois)", "Total HT"])
        self.assertIn('data-label="Total HT" data-sort="1170.00"', top)
        for name in (SETTINGS, EXCLUDE, INCLUDE):
            with self.subTest(form=name):
                self.assertEqual(forms_to(html, name), [])

    def test_a_message_of_a_fold_he_is_not_shown_is_said_at_the_top(self):
        self.client.post(reverse(SETTINGS), {"fournisseur": self.made.wholesaler.pk, "seuil": "30", "memoire": "6"})
        self.assertEqual(said_at_the_top(self.html_as(["stock_takes"])), ["Réglages enregistrés."])

    def test_an_exclusion_s_message_carries_no_undo_for_him(self):
        """An exclusion's message read by one given the lists alone - the
        owner's, on a phone they share - says what was done, and draws no
        « Annuler »: taking it back is « Produits & charges »'."""
        wholesaler = self.made.wholesaler.pk
        data = {"article": self.made.syrup.pk, "chez": wholesaler, "fournisseur": wholesaler, "retour": "liste"}
        self.assertEqual(self.client.post(reverse(EXCLUDE), data).status_code, 302)
        html = self.html_as(["shopping"], fournisseur=wholesaler)
        self.assertEqual(said_above_the_list(html), ["« Sirop exemple » ne sera plus proposé chez Grossiste exemple."])
        self.assertEqual(forms_to(html, INCLUDE), [])
        data = {"article": self.made.rum.pk, "fournisseur": wholesaler, "retour": "rythme"}
        self.assertEqual(self.client.post(reverse(EXCLUDE), data).status_code, 302)
        rhythm = self.html_as(["shopping"], RHYTHM, fournisseur=wholesaler)
        self.assertEqual(said_at_the_top(rhythm), ["« Rhum exemple » ne sera plus proposé."])
        self.assertEqual(forms_to(rhythm, INCLUDE), [])
        self.assertNotIn("Annuler", at_the_top(rhythm))
