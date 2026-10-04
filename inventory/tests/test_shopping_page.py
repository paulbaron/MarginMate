"""The « Prévoir les courses » pages (/courses/, /courses/rythme/), as the
owner reads them, and their forms.

What he chooses is a store (`?fournisseur=`) and, if he likes, how long a
purchase made there today must last, until the visit after it (`?dans=`, 1
to 90); what he reads is the list for shopping there today - each line its
quantity in the store's own product, its chance
and why - then the folds: « Peut-être », « Nouveaux ici », « Plus acheté ? »,
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
the list.

The view reads today, so the data is dated relative to
`timezone.localdate()` (tests.test_views_smoke.make_shopping_history).
Invented data throughout: every store, article, product, recipe, price and
quantity is made up for these tests.
"""

from __future__ import annotations

import math
import re
from datetime import timedelta
from decimal import Decimal
from html import unescape
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from inventory import shopping, views
from inventory.models import MovementKind, ShoppingExclusion, ShoppingSetting, StockMovement, StockType, UnitChoices
from inventory.tests.test_gap_filler_page import body_rows, busy_button_of, confirm_of, table_of
from invoices.models import GatherCoverage
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
from tests.test_views_smoke import SHOPPING_BEER_PRODUCT, assertNoUnrenderedTemplateSyntax, make_shopping_history

PAGE = "inventory:shopping_list"
RHYTHM = "inventory:shopping_rhythm"
SETTINGS = "inventory:shopping_settings"
EXCLUDE = "inventory:shopping_exclude"
INCLUDE = "inventory:shopping_include"
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


def said_in(fragment: str) -> list[str]:
    """The messages a `ul.messages` of the fragment says, as they read."""
    return [
        readable(found)
        for found in re.findall(r'<li class="message message-[a-z]+">(.*?)</li>', fragment, flags=re.DOTALL)
    ]


def horizon_said(html: str) -> str:
    """The sentence under « À acheter » saying what the list is for."""
    found = re.search(r'<h2 id="a-acheter">À acheter</h2>.*?<p class="muted">(.*?)</p>', html, flags=re.DOTALL)
    return readable(found.group(1)) if found else ""


def said_above_the_list(html: str) -> list[str]:
    found = re.search(r'<h2 id="a-acheter">À acheter</h2>\s*<ul class="messages">(.*?)</ul>', html, flags=re.DOTALL)
    return said_in(found.group(1)) if found else []


def said_at_the_top(html: str) -> list[str]:
    """The messages printed before the page's header: base.html's block."""
    return said_in(html[: html.index('<div class="page-header">')])


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
    """The fields a form carries hidden, its CSRF token aside."""
    found = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))
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
        html = self.html()
        table = table_of(html, TO_BUY)
        self.assertEqual(titles_in(table), ["Bière exemple", "Sirop exemple"])
        beer = row_of(table, "Bière exemple")
        cells = [unescape(cell) for cell in cells_of(beer)]
        self.assertEqual(cells[1], f"24 × {SHOPPING_BEER_PRODUCT} (1 colis de 24) 24 u.")
        self.assertEqual(
            cells[3],
            "Pris 25 fois sur 25 passages en 6 mois ; dernier achat il y a 7 jours, d'habitude tous les 7 jours.",
        )
        self.assertEqual(cells[4], "Pas ici")
        syrup = [unescape(cell) for cell in cells_of(row_of(table, "Sirop exemple"))]
        self.assertEqual(syrup[1], "2 × SIROP EXEMPLE PRODUIT 2 L")

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
        self.assertTrue(fold_of(html, "maybe"))
        self.assertTrue(fold_of(html, "most-bought"))
        self.assertFalse(opened(fold_of(html, "maybe")))
        self.assertFalse(opened(fold_of(html, "most-bought")))
        for anchor in ("reglages", "exclusions"):
            with self.subTest(fold=anchor):
                self.assertFalse(opened(fold_of(html, anchor)))
        self.assertEqual(readable(re.search(r"<summary>(Peut-être.*?)</summary>", html).group(1)), "Peut-être (1)")

    def test_an_id_no_redirect_targets_is_english(self):
        """Only the ids a redirect lands on keep their French (a-acheter,
        reglages, exclusions: SHOPPING_ANCHORS); the others are internal."""
        html = self.html()
        self.assertEqual(sorted(views.SHOPPING_ANCHORS.values()), ["a-acheter", "exclusions", "reglages"])
        ids = set(re.findall(r'<(?:details|h2)[^>]* id="([^"]+)"', html))
        self.assertEqual(ids, {"a-acheter", "maybe", "most-bought", "reglages", "exclusions"})

    def test_the_horizon_is_the_store_s_rhythm(self):
        """The list is worked out for a purchase made today, lasting until
        the visit after it: the sentence says so, and that the days are the
        owner's rhythm at the store."""
        self.assertEqual(horizon_said(self.html()), HORIZON.format(7, OWN_RHYTHM))

    def test_a_typed_horizon_longer_than_the_rhythm_multiplies_the_usual_purchase(self):
        html = self.html(dans="60")
        self.assertIn('name="dans" value="60"', html)
        self.assertEqual(horizon_said(html), HORIZON.format(60, ""))
        quantity = [unescape(cell) for cell in cells_of(row_of(table_of(html, TO_BUY), "Bière exemple"))][1]
        found = re.fullmatch(
            r"(\d+) × \(24 × " + re.escape(SHOPPING_BEER_PRODUCT) + r" \(1 colis de 24\)\) — pour 60 jours (\d+) u\.",
            quantity,
        )
        self.assertIsNotNone(found, quantity)
        times, units = int(found.group(1)), int(found.group(2))
        self.assertGreater(times, 1)
        self.assertEqual(units, 24 * times)

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
        self.assertFalse(opened(fold_of(html, "maybe")))

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
        self.assertFalse(opened(fold_of(html, "maybe")))
        self.assertTrue(opened(fold_of(html, "most-bought")))

    def test_a_rare_store(self):
        html = self.html(fournisseur=self.made.market.pk)
        # Visited once: the days are the default, never called its rhythm.
        self.assertEqual(horizon_said(html), HORIZON.format(14, DEFAULT_RHYTHM) + " " + FEW_VISITS)
        self.assertIn(EMPTY_LIST, readable(html))
        self.assertEqual(titles_in(table_of(html, MAYBE)), ["Fraises exemple"])
        self.assertTrue(opened(fold_of(html, "maybe")))
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
        self.assertTrue(opened(fold_of(html, "maybe")))
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
        self.assertEqual(fold_of(html, "maybe"), "")
        self.assertTrue(opened(fold_of(html, "most-bought")))
        self.assertEqual(titles_in(table_of(html, ELSEWHERE)), ["Tonic exemple"])

        old = make_supplier(name="Ancienne halle exemple")
        pretzels = make_stock_type(name="Bretzels exemple", unit=UnitChoices.UNIT)
        for days_ago in (400, 390):
            self.buy(old, pretzels, days_ago)
        html = self.html(fournisseur=old.pk)
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertEqual(readable(found.group(1)), EMPTY_LIST_BARE)
        self.assertEqual(fold_of(html, "maybe"), "")
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
            unescape(cells_of(row)[3]), r"^Aurait été proposé à vos \d+ derniers passages ici, sans être pris\.$"
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

    def test_never_again_takes_the_article_s_store_rows_with_it(self):
        ShoppingExclusion.objects.create(stock_type=self.made.rum, supplier=self.made.wholesaler)
        ShoppingExclusion.objects.create(stock_type=self.made.rum, supplier=self.made.grocer)
        response = self.post(EXCLUDE, article=self.made.rum.pk, fournisseur=self.made.wholesaler.pk)
        self.assertEqual(
            list(ShoppingExclusion.objects.values_list("stock_type", "category", "supplier")),
            [(self.made.rum.pk, None, None)],
        )
        html = response.content.decode()
        self.assertEqual(said_in(fold_of(html, "exclusions")), ["« Rhum exemple » ne sera plus proposé."])
        self.assertEqual(table_of(html, QUIET), "")
        # Twice: still one row.
        self.post(EXCLUDE, article=self.made.rum.pk, fournisseur=self.made.wholesaler.pk)
        self.assertEqual(ShoppingExclusion.objects.count(), 1)

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
