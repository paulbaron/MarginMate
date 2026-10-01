"""The « Combler les écarts » page (/stock-takes/fill-gaps/), in figures.

What the owner types is an amount to ring up at the till; what he reads is
the list of sales to ring up, each figure in its own row - the button to
press, how many, the price, the total - and how far every gap since the
count shrinks. Asserted figure by figure inside its own row or stat, never
anywhere on the page: « 35.00 » could be on it twice.

Amounts are a list (the owner, 01/10/2026: « si je rentre 7 € et encore
7 € … que cela soit mis à jour pour combler les trous au fur et à mesure.
Ensuite je peux "clear" la liste »): each is POSTed to
`stock_gap_filler_add`, kept as a GapFillEntry of its stock take and planned
on top of the ones before; « Annuler la dernière saisie » and « Effacer la
liste » take them back. Each POST answers with a redirect to the page, what
it refused said in a message - never a 500, never a page answering the POST.
`?depuis=` still arrives from an address. The add, undo and clear forms
carry the last entry the page showed (« derniere »): a second click, or a
tab left open, posts a list that has moved since and is refused - checked
again inside the transaction that writes, so another click committing while
this one plans is caught too.

Articles and categories are left out of the gaps (the owner, 01/10/2026:
« exclure des articles/catégories de produit de ces écarts, et que cela
reste en mémoire quand on réouvre la page »): « Exclure » on a gap's row,
« Exclure la catégorie » in « Exclus des écarts » (#exclusions), and
« Réinclure » on each line there - POSTs to `stock_gap_filler_exclude` and
`stock_gap_filler_include`, kept for the espace (GapExclusion), whatever
count's page they were posted from. Their messages are said where the
redirect lands - under « Écarts », or in the fold, opened for them - and at
the top only when there is no count to show either.

The view reads today (`gaps_since(take)` ends today), so the data here is
dated relative to `timezone.localdate()`.

Invented data throughout: every article, recipe, till button, price and
quantity is made up for these tests.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from decimal import Decimal
from html import unescape
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.gaps import gaps_since
from inventory.models import GapExclusion, GapFillEntry, MovementKind, StockTake, StockType, UnitChoices
from margins.tests.test_page import cells_of, row_of, rows_of, stat_of, text_of, value_of
from recipes.models import PosProduct, PosProductDailyQuantity, Recipe, RecipeSale
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_movement,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
)
from tests.runner import TenantClient
from tests.test_navigation import active_labels

PAGE = "inventory:stock_gap_filler"
ADD = "inventory:stock_gap_filler_add"
UNDO = "inventory:stock_gap_filler_undo"
CLEAR = "inventory:stock_gap_filler_clear"
EXCLUDE = "inventory:stock_gap_filler_exclude"
INCLUDE = "inventory:stock_gap_filler_include"
GAPS_TABLE = "écarts"
#: The last cell of every gaps row, as it reads: its « Exclure » button.
EXCLUDE_BUTTON = "Exclure"
PLAN_TABLE = "recettes à encaisser"
EARLIER_TABLE = "montants déjà saisis"
ENTERED = "Saisi (TTC)"
PROPOSED = "Proposé (TTC)"
SHARES = "Écarts réduits de"
SALES = "Ventes"
#: « Ventes »' note: the sales of every entry, not of the last.
WHOLE_LIST = "sur toute la liste"
#: « Proposé (TTC) »'s note when the list's totals are its amounts.
EXACT = "le montant exact, au prix de la carte"
#: What the last entry's line says of its own total.
ENTRY_EXACT = "le montant exact"
NO_COMBINATION = "aucune combinaison de prix ne tombe juste"
GAPS_FULL = "plus aucune vente ne tient dans les écarts"
BELOW_CHEAPEST = "moins que la recette la moins chère"
#: The page's own warning, on an address naming no count it knows.
TAKE_NOT_FOUND = "Inventaire introuvable : les écarts partent du dernier."
#: The message of an amount POSTed for a count it does not know.
TAKE_UNKNOWN = "Inventaire introuvable."
UNREADABLE = "Montant illisible : tapez par exemple 150,50."
NOT_POSITIVE = "Le montant doit être supérieur à zéro."
#: Between an amount's thousands, on the page as in its messages.
NBSP = "\N{NO-BREAK SPACE}"
TOO_BIG = f"10{NBSP}000 € au plus."
UNDONE = "Dernière saisie retirée."
CLEARED = "Liste effacée."
#: What a post from a page the list has moved since is told, by action.
MOVED_ADD = "La liste a changé entre-temps : rien n'a été ajouté, vérifiez-la."
MOVED_UNDO = "La liste a changé entre-temps : rien n'a été retiré, vérifiez-la."
MOVED_CLEAR = "La liste a changé entre-temps : rien n'a été effacé, vérifiez-la."
#: What the add and undo buttons say while their post runs.
ADD_BUSY = ("Calcul…", "Ajouter")
UNDO_BUSY = ("Annulation…", "Annuler la dernière saisie")
#: The note under « À encaisser » on lines the till rings at another price.
ONE_TILL_PRICE = "1 ligne a un autre prix en caisse"
TWO_TILL_PRICES = "2 lignes ont un autre prix en caisse"
STALE = (
    "Des ventes ont été importées depuis cette liste : si elle a été encaissée, ses ventes comptent "
    "maintenant deux fois. Effacez-la."
)
NEWER_PURCHASES = "Des achats sont plus récents que les dernières ventes importées"
NOT_UP_TO_YESTERDAY = "Les ventes ne sont pas importées jusqu'à hier"
#: What either warning goes on to say, and the link under it.
NOT_IMPORTED_YET = (
    " : les ventes pas encore importées comptent comme écart. Importez-les avant de calculer. Importer les ventes"
)
SECONDARY = "aucune vente ne l'atteint encore"
#: « Ce qui ne peut pas être comblé » on the articles no recipe pours, one and
#: two - one article is « le », not « les ».
OUTSIDE_ONE = "1 autre article acheté n'est dans aucune recette (matériel, consommables…) : aucune vente ne le comble."
OUTSIDE_TWO = (
    "2 autres articles achetés ne sont dans aucune recette (matériel, consommables…) : aucune vente ne les comble."
)


def noon(day) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


def table_of(html: str, label: str) -> str:
    """The table labelled `label` (its data-table-label), or ""."""
    found = re.search(
        r'<table[^>]*data-table-label="' + re.escape(label) + r'"[^>]*>.*?</table>', html, flags=re.DOTALL
    )
    return found.group(0) if found else ""


def body_rows(table: str) -> list[str]:
    """A table's rows under its header, in the order drawn."""
    found = re.search(r"<tbody>(.*?)</tbody>", table, flags=re.DOTALL)
    return rows_of(found.group(1)) if found else []


def note_of(stat: str) -> str:
    found = re.search(r'<div class="stat-note[^"]*">(.*?)</div>', stat, flags=re.DOTALL)
    return text_of(found.group(1)) if found else ""


def selected_take(html: str) -> str:
    """The value of the « Depuis » option drawn selected."""
    found = re.search(r'<option value="(\d+)" selected>', html)
    return found.group(1) if found else ""


def messages_of(html: str, kind: str) -> list[str]:
    """Every « message-<kind> » block the page itself draws, as it reads."""
    return [
        text_of(found)
        for found in re.findall(r'<div class="message message-' + kind + r'">(.*?)</div>', html, flags=re.DOTALL)
    ]


def notices_of(html: str, kind: str) -> list[str]:
    """Every message of `kind` an action left for the page it redirected to
    (django.contrib.messages, drawn by base.html), as it reads - « n'a »,
    not the « n&#x27;a » autoescape writes."""
    return [
        unescape(text_of(found))
        for found in re.findall(r'<li class="message message-' + kind + r'">(.*?)</li>', html, flags=re.DOTALL)
    ]


def heading_of(html: str) -> str:
    """« À encaisser : X € », the last entry's heading, or ""."""
    found = re.search(r'<h2 id="a-encaisser">(.*?)</h2>', html, flags=re.DOTALL)
    return text_of(found.group(1)) if found else ""


def entry_note_of(html: str) -> str:
    """The line under that heading: when it was typed, and its total."""
    found = re.search(r'<h2 id="a-encaisser">.*?</h2>\s*<p class="muted">(.*?)</p>', html, flags=re.DOTALL)
    return text_of(found.group(1)) if found else ""


def earlier_summary_of(html: str) -> str:
    found = re.search(r"<summary>(Montants déjà saisis.*?)</summary>", html, flags=re.DOTALL)
    return text_of(found.group(1)) if found else ""


def form_of(html: str, name: str) -> str:
    """The POST form sent to the route `name`, or ""."""
    found = re.search(
        r'<form method="post" action="' + re.escape(reverse(name)) + r'"[^>]*>.*?</form>', html, flags=re.DOTALL
    )
    return found.group(0) if found else ""


def last_shown_in(form: str) -> str | None:
    """The last entry a form carries (its hidden « derniere »), None when it
    carries none."""
    found = re.search(r'<input type="hidden" name="derniere" value="([^"]*)">', form)
    return found.group(1) if found else None


def confirm_of(form: str) -> str:
    """What a form asks before it is sent (its data-confirm), or ""."""
    found = re.search(r'data-confirm="([^"]*)"', form)
    return found.group(1) if found else ""


def busy_button_of(form: str) -> tuple[str, str] | None:
    """(what the form's submit button says once pressed - its
    data-busy-label -, what it says before), None when it has no busy label."""
    found = re.search(r'<button[^>]*type="submit"[^>]*data-busy-label="([^"]*)"[^>]*>(.*?)</button>', form)
    return (found.group(1), text_of(found.group(2))) if found else None


def typed_at(entry) -> str:
    """How the page says when `entry` was typed."""
    local = timezone.localtime(entry.created_at)
    return f"Saisi à {local:%H:%M} le {local:%d/%m/%Y}"


def entries_of(take) -> list[GapFillEntry]:
    return list(GapFillEntry.objects.filter(stock_take=take))


def amounts_of(take) -> list[Decimal]:
    return [entry.amount for entry in entries_of(take)]


def explainer_of(html: str) -> str:
    """The « Ce qui ne peut pas être comblé » fold, or ""."""
    found = re.search(
        r'<details class="explainer">\s*<summary>Ce qui ne peut pas être comblé.*?</details>', html, flags=re.DOTALL
    )
    return found.group(0) if found else ""


def summary_of(html: str) -> str:
    found = re.search(r"<summary>(Ce qui ne peut pas être comblé.*?)</summary>", html, flags=re.DOTALL)
    return text_of(found.group(1)) if found else ""


def sentences_of(fragment: str) -> list[str]:
    """Each paragraph of a fragment as it reads - one sentence a paragraph,
    so each is asserted whole and in its own place."""
    return [text_of(found) for found in re.findall(r'<p class="muted">(.*?)</p>', fragment, flags=re.DOTALL)]


def article(name: str):
    return make_stock_type(name=name, unit=UnitChoices.LITRE)


def counted(take, stock_type, quantity):
    return make_stock_take_line(
        stock_take=take, product=None, stock_type=stock_type, counted_quantity=quantity, unit=stock_type.unit
    )


def bought(stock_type, quantity, day, unit_cost="4.00"):
    return make_movement(
        stock_type=stock_type, quantity=quantity, unit_cost_ht=unit_cost, kind=MovementKind.PURCHASE, occurred_on=day
    )


def recipe(name: str, price, stock_type, amount):
    made = make_recipe(name=name, selling_price_ttc=price)
    make_ingredient(made, stock_type=stock_type, quantity=amount)
    return made


class PageTestCase(TestCase):
    """The page and its list's actions as the tests below drive them - no
    test of its own. `take` is the count a class types its amounts for."""

    take: StockTake

    def get(self, **params):
        response = self.client.get(reverse(PAGE), params)
        self.assertEqual(response.status_code, 200)
        return response

    def html(self, **params) -> str:
        return self.get(**params).content.decode()

    def post(self, name: str, **data):
        """POST to one of the list's actions and follow it: always one
        redirect, and the page it lands on."""
        response = self.client.post(reverse(name), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([status for _url, status in response.redirect_chain], [302])
        return response

    def add(self, amount: str, take=None) -> str:
        """Type `amount` into the list of `take` (the class's own by
        default): the page the POST lands on."""
        return self.post(ADD, depuis=(take or self.take).pk, montant=amount).content.decode()

    def landing(self, response) -> str:
        return response.redirect_chain[0][0]

    def page_of(self, take) -> str:
        return f"{reverse(PAGE)}?depuis={take.pk}"


class NoStockTakeTests(PageTestCase):
    """Nothing counted yet: the gaps have nowhere to run from."""

    def test_the_page_says_so_and_offers_a_count(self):
        html = self.html()
        found = re.search(r'<div class="empty-state">(.*?)</div>', html, flags=re.DOTALL)
        self.assertIsNotNone(found)
        self.assertIn(f'href="{reverse("inventory:stock_take_create")}"', found.group(1) if found else "")
        self.assertEqual(table_of(html, GAPS_TABLE), "")
        self.assertNotIn(PROPOSED, html)
        self.assertEqual(form_of(html, ADD), "")

    def test_an_address_changes_nothing(self):
        # An old bookmark still carrying ?montant=: the page reads no amount.
        html = self.html(montant="420", depuis="3")
        self.assertIn('class="empty-state"', html)
        self.assertNotIn(PROPOSED, html)
        self.assertNotIn(TAKE_NOT_FOUND, html)

    def test_an_amount_posted_finds_no_count_and_keeps_nothing(self):
        response = self.post(ADD, depuis="3", montant="420")
        html = response.content.decode()
        self.assertEqual(self.landing(response), reverse(PAGE))
        self.assertEqual(notices_of(html, "error"), [TAKE_UNKNOWN])
        self.assertIn('class="empty-state"', html)
        self.assertFalse(GapFillEntry.objects.exists())

    def test_undo_and_clear_find_no_count(self):
        for name in (UNDO, CLEAR):
            with self.subTest(action=name):
                response = self.post(name, depuis="3")
                self.assertEqual(self.landing(response), reverse(PAGE))
                self.assertEqual(notices_of(response.content.decode(), "success"), [])

    def test_the_page_lights_inventaires(self):
        self.assertEqual(active_labels(self.get()), ["Inventaires"])


class OnePintBarTests(PageTestCase):
    """100 L of blonde counted 20 days ago, 200 L bought since at 4,00 € HT,
    40 pints of 0,5 L sold since at 35,00 € - imported up to yesterday: gap
    280 L, allowance 30 L, 250 L to fill, worth 1 000,00 € HT. Each pint
    proposed adds 0,5 L."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.take_day = cls.today - timedelta(days=20)
        cls.purchase_day = cls.today - timedelta(days=10)
        cls.sale_day = cls.today - timedelta(days=1)
        cls.take = make_stock_take(taken_at=noon(cls.take_day))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "100")
        bought(cls.blonde, "200", cls.purchase_day)
        cls.pint = recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=cls.sale_day, quantity=40)
        PosProduct.objects.create(name="PINTE CAISSE EXEMPLE", recipe=cls.pint, total_quantity=40)

    def plan_row(self, html):
        return cells_of(row_of(table_of(html, PLAN_TABLE), "Pinte exemple"))

    def gaps_row(self, html):
        return cells_of(row_of(table_of(html, GAPS_TABLE), "Blonde exemple"))

    def assertNothingKept(self, html):
        self.assertEqual(entries_of(self.take), [])
        self.assertNotIn(PROPOSED, html)
        self.assertEqual(heading_of(html), "")
        self.assertEqual(table_of(html, PLAN_TABLE), "")
        # The gaps are still there, without the list's two columns: the
        # value, then the row's « Exclure ».
        row = self.gaps_row(html)
        self.assertEqual(len(row), 10)
        self.assertEqual(row[-2:], [f"1{NBSP}000.00 €", EXCLUDE_BUTTON])

    # -- no list -----------------------------------------------------------

    def test_without_a_list_the_gaps_and_no_plan(self):
        html = self.html()
        self.assertEqual(
            self.gaps_row(html),
            ["Blonde exemple litre", "100", "200", "0", "20", "280", "30", "250", f"1{NBSP}000.00 €", EXCLUDE_BUTTON],
        )
        self.assertNotIn(PROPOSED, html)
        self.assertNotIn(ENTERED, html)
        self.assertEqual(table_of(html, PLAN_TABLE), "")
        self.assertEqual(table_of(html, EARLIER_TABLE), "")
        self.assertNotIn("message-error", html)
        # Nothing to take back: neither button is drawn.
        self.assertEqual(form_of(html, UNDO), "")
        self.assertEqual(form_of(html, CLEAR), "")

    def test_an_old_address_with_an_amount_plans_nothing(self):
        # ?montant= was the page's amount once: a bookmark of it is read as
        # no amount, and keeps nothing.
        html = self.html(depuis=str(self.take.pk), montant="420")
        self.assertNothingKept(html)
        self.assertEqual(notices_of(html, "error"), [])

    def test_the_window_and_how_fresh_its_data_is(self):
        html = self.html()
        self.assertIn(
            f"Du {self.take_day:%d/%m/%Y} au {self.today:%d/%m/%Y} · "
            f"ventes importées jusqu'au {self.sale_day:%d/%m/%Y} · "
            f"achats jusqu'au {self.purchase_day:%d/%m/%Y}",
            text_of(html),
        )
        # Sales up to yesterday, no delivery since: nothing to warn about.
        self.assertEqual(messages_of(html, "warning"), [])

    def test_the_take_is_the_one_selected(self):
        self.assertEqual(selected_take(self.html()), str(self.take.pk))

    def test_the_amount_field_posts_to_the_list_of_this_take(self):
        form = form_of(self.html(), ADD)
        self.assertIn('placeholder="150,00"', form)
        self.assertIn('name="montant"', form)
        self.assertIn(f'<input type="hidden" name="depuis" value="{self.take.pk}">', form)
        self.assertIn('name="csrfmiddlewaretoken"', form)

    def test_the_page_lights_inventaires(self):
        self.assertEqual(active_labels(self.get()), ["Inventaires"])
        self.add("420")
        self.assertEqual(active_labels(self.get(depuis=str(self.take.pk))), ["Inventaires"])

    # -- an amount ---------------------------------------------------------

    def test_an_exact_amount(self):
        response = self.post(ADD, depuis=self.take.pk, montant="420")
        html = response.content.decode()
        self.assertEqual(self.landing(response), f"{self.page_of(self.take)}#a-encaisser")
        entered = stat_of(html, ENTERED)
        self.assertEqual(value_of(entered), "420.00 €")
        self.assertEqual(note_of(entered), "1 montant")
        proposed = stat_of(html, PROPOSED)
        self.assertEqual(value_of(proposed), "420.00 €")
        self.assertEqual(note_of(proposed), EXACT)
        self.assertEqual(heading_of(html), "À encaisser : 420.00 €")
        self.assertEqual(
            self.plan_row(html),
            ["Pinte exemple", "PINTE CAISSE EXEMPLE", "12", "35.00 €", "420.00 €", "Blonde exemple"],
        )
        sales = stat_of(html, SALES)
        self.assertEqual(value_of(sales), "12")
        self.assertEqual(note_of(sales), WHOLE_LIST)
        # Kept, as proposed: one entry of this count.
        (entry,) = entries_of(self.take)
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT}")
        self.assertEqual((entry.amount, entry.total, entry.reason), (Decimal("420.00"), Decimal("420.00"), ""))
        self.assertEqual(entry.sales_up_to, self.sale_day)
        self.assertEqual(
            entry.lines,
            [
                {
                    "recipe": self.pint.pk,
                    "name": "Pinte exemple",
                    "till": "PINTE CAISSE EXEMPLE",
                    "till_price": None,
                    "count": 12,
                    "price": "35.00",
                    "fills": ["Blonde exemple"],
                }
            ],
        )
        # One amount: nothing earlier to fold.
        self.assertEqual(earlier_summary_of(html), "")
        self.assertEqual(table_of(html, EARLIER_TABLE), "")

    def test_how_far_the_gaps_shrink(self):
        html = self.add("420")
        shares = stat_of(html, SHARES)
        self.assertEqual(value_of(shares), "2.4 %")  # 6 L of 250
        self.assertEqual(note_of(shares), "en moyenne, de 2.4 % à 2.4 % selon l'article")
        self.assertEqual(
            self.gaps_row(html),
            [
                "Blonde exemple litre",
                "100",
                "200",
                "0",
                "20",
                "280",
                "30",
                "250",
                f"1{NBSP}000.00 €",
                "6",
                "2.4 %",
                EXCLUDE_BUTTON,
            ],
        )

    def test_a_second_amount_adds_to_the_figures_of_the_whole_list(self):
        self.add("420")
        html = self.add("420")
        self.assertEqual(value_of(stat_of(html, ENTERED)), "840.00 €")
        self.assertEqual(note_of(stat_of(html, ENTERED)), "2 montants")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "840.00 €")
        self.assertEqual(value_of(stat_of(html, SALES)), "24")
        self.assertEqual(value_of(stat_of(html, SHARES)), "4.8 %")  # 12 L of 250
        self.assertEqual(self.gaps_row(html)[-3:], ["12", "4.8 %", EXCLUDE_BUTTON])
        # The second entry holds its own twelve pints, not twenty-four.
        self.assertEqual(
            self.plan_row(html),
            ["Pinte exemple", "PINTE CAISSE EXEMPLE", "12", "35.00 €", "420.00 €", "Blonde exemple"],
        )
        self.assertEqual([entry.sales for entry in entries_of(self.take)], [12, 12])

    def test_amounts_no_combination_of_prices_makes(self):
        # Under 2 €, « non proposé »: French puts a figure below two in the
        # singular.
        for typed, entered, total, rest, count, not_proposed in (
            ("50", "50.00 €", "35.00 €", "15.00", "1", "non proposés"),
            ("420,50", "420.50 €", "420.00 €", "0.50", "12", "non proposé"),
            ("420.50", "420.50 €", "420.00 €", "0.50", "12", "non proposé"),
            ("1 234,50", f"1{NBSP}234.50 €", f"1{NBSP}225.00 €", "9.50", "35", "non proposés"),
            (f"1{NBSP}234,50", f"1{NBSP}234.50 €", f"1{NBSP}225.00 €", "9.50", "35", "non proposés"),
            ("10000", f"10{NBSP}000.00 €", f"9{NBSP}975.00 €", "25.00", "285", "non proposés"),
        ):
            with self.subTest(typed=typed):
                GapFillEntry.objects.all().delete()
                html = self.add(typed)
                self.assertEqual(notices_of(html, "error"), [])
                self.assertEqual(value_of(stat_of(html, ENTERED)), entered)
                proposed = stat_of(html, PROPOSED)
                self.assertEqual(value_of(proposed), total)
                self.assertEqual(note_of(proposed), f"{rest} € {not_proposed}")
                (entry,) = entries_of(self.take)
                self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · reste {rest} € : {NO_COMBINATION}")
                self.assertEqual(
                    self.plan_row(html),
                    ["Pinte exemple", "PINTE CAISSE EXEMPLE", count, "35.00 €", total, "Blonde exemple"],
                )
                self.assertEqual(value_of(stat_of(html, SALES)), count)

    def test_amounts_spelled_every_way_a_person_types_them(self):
        for typed, entered, total, rest, count in (
            ("10 000", f"10{NBSP}000.00 €", f"9{NBSP}975.00 €", "25.00", "285"),
            ("10.000,00", f"10{NBSP}000.00 €", f"9{NBSP}975.00 €", "25.00", "285"),
            ("1 500,00", f"1{NBSP}500.00 €", f"1{NBSP}470.00 €", "30.00", "42"),
            ("1'234,50", f"1{NBSP}234.50 €", f"1{NBSP}225.00 €", "9.50", "35"),
            ("150,50", "150.50 €", "140.00 €", "10.50", "4"),
            ("150.50", "150.50 €", "140.00 €", "10.50", "4"),
        ):
            with self.subTest(typed=typed):
                GapFillEntry.objects.all().delete()
                html = self.add(typed)
                self.assertEqual(notices_of(html, "error"), [])
                self.assertEqual(value_of(stat_of(html, ENTERED)), entered)
                proposed = stat_of(html, PROPOSED)
                self.assertEqual(value_of(proposed), total)
                self.assertEqual(note_of(proposed), f"{rest} € non proposés")
                self.assertEqual(value_of(stat_of(html, SALES)), count)

    def test_a_decimal_comma_with_one_figure(self):
        html = self.add("35,5")
        self.assertEqual(notices_of(html, "error"), [])
        self.assertEqual(value_of(stat_of(html, ENTERED)), "35.50 €")
        self.assertEqual(note_of(stat_of(html, PROPOSED)), "0.50 € non proposé")
        # Under the cheapest recipe, the same reading says what it read.
        html = self.add("12,5")
        self.assertEqual(notices_of(html, "error"), [])
        self.assertEqual(notices_of(html, "warning"), [f"Rien pour 12.50 € : {BELOW_CHEAPEST}."])

    def test_what_is_not_proposed_is_singular_under_two_euros(self):
        # « 0,50 € non proposés » read wrong: below two, French says it in
        # the singular. The whole list's figure, from 2,00 € on, the plural.
        for typed, note in (
            ("35,5", "0.50 € non proposé"),
            ("36,99", "1.99 € non proposé"),
            ("37", "2.00 € non proposés"),
            ("37,01", "2.01 € non proposés"),
        ):
            with self.subTest(typed=typed):
                GapFillEntry.objects.all().delete()
                html = self.add(typed)
                self.assertEqual(value_of(stat_of(html, PROPOSED)), "35.00 €")
                self.assertEqual(note_of(stat_of(html, PROPOSED)), note)
        # Two amounts short of 1,00 € each: the list's 2,00 € is plural.
        GapFillEntry.objects.all().delete()
        self.add("36")
        html = self.add("36")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "70.00 €")
        self.assertEqual(note_of(stat_of(html, PROPOSED)), "2.00 € non proposés")

    # « 10.000 » is ten thousand to whoever typed it, and ten euros to a
    # reader taking « . » for the decimal point: one separator and exactly
    # three figures after it is asked again, never guessed - read as 10,00 €
    # the page planned a plan for the wrong amount, under no warning.
    def test_one_separator_and_three_figures_is_asked_again(self):
        for typed in ("10.000", "1,500", "2.000", "1,000", "-1.500"):
            with self.subTest(typed=typed):
                response = self.post(ADD, depuis=self.take.pk, montant=typed)
                html = response.content.decode()
                self.assertEqual(self.landing(response), self.page_of(self.take))
                self.assertEqual(notices_of(html, "error"), [UNREADABLE])
                self.assertNothingKept(html)

    def test_amounts_are_grouped_and_their_sort_keys_are_not(self):
        html = self.add("1400")
        self.assertEqual(heading_of(html), f"À encaisser : 1{NBSP}400.00 €")
        self.assertEqual(value_of(stat_of(html, ENTERED)), f"1{NBSP}400.00 €")
        # datatable.js sorts by the bare figure.
        self.assertIn(f'data-sort="1400.00">1{NBSP}400.00 €</td>', html)

    def test_the_field_is_empty_for_the_next_amount(self):
        # What was typed is in the list now, not in the field: the next
        # amount is typed on a blank one.
        html = self.add("1 234,50")
        self.assertEqual(heading_of(html), f"À encaisser : 1{NBSP}234.50 €")
        field = re.search(r'<input type="text"[^>]*name="montant"[^>]*>', html)
        self.assertIsNotNone(field)
        self.assertNotIn("value=", field.group(0) if field else "value=")

    def test_under_the_cheapest_recipe(self):
        html = self.add("20")
        self.assertEqual(notices_of(html, "warning"), [f"Rien pour 20.00 € : {BELOW_CHEAPEST}."])
        self.assertEqual(notices_of(html, "error"), [])
        self.assertNothingKept(html)

    def test_amounts_that_are_refused(self):
        for typed, message in (
            ("abc", UNREADABLE),
            ("-5", NOT_POSITIVE),
            ("0", NOT_POSITIVE),
            ("0,00", NOT_POSITIVE),
            ("Infinity", UNREADABLE),
            ("NaN", UNREADABLE),
            ("1e999", UNREADABLE),
            ("12,345", UNREADABLE),
            ("10.000", UNREADABLE),
            ("1,500", UNREADABLE),
            ("2.000", UNREADABLE),
            ("42 50", UNREADABLE),
            ("10000,01", TOO_BIG),
            ("9 999 999 999,99", TOO_BIG),
        ):
            with self.subTest(typed=typed):
                response = self.post(ADD, depuis=self.take.pk, montant=typed)
                html = response.content.decode()
                self.assertEqual(self.landing(response), self.page_of(self.take))
                self.assertEqual(notices_of(html, "error"), [message])
                self.assertEqual(notices_of(html, "warning"), [])
                self.assertNothingKept(html)

    def test_a_refused_amount_leaves_the_list_as_it_was(self):
        self.add("420")
        html = self.add("abc")
        self.assertEqual(notices_of(html, "error"), [UNREADABLE])
        self.assertEqual(amounts_of(self.take), [Decimal("420.00")])
        self.assertEqual(heading_of(html), "À encaisser : 420.00 €")

    # 10 000 000 000 € and more once read as « illisible »: read_amount
    # stops at a column's width (10^10), and that None was taken for
    # unreadable before the size was ever compared with MAX_AMOUNT.
    def test_a_huge_but_readable_amount_is_too_big_not_unreadable(self):
        for typed in ("10000000000", "20 000 000 000", "99999999999999"):
            with self.subTest(typed=typed):
                html = self.add(typed)
                self.assertEqual(notices_of(html, "error"), [TOO_BIG])
                self.assertEqual(entries_of(self.take), [])

    def test_a_blank_amount_is_refused(self):
        # The field is required; a blank POST all the same is no amount.
        self.assertIn(" required>", form_of(self.html(), ADD))
        for typed in ("   ", ""):
            with self.subTest(typed=typed):
                html = self.add(typed)
                self.assertEqual(notices_of(html, "error"), [UNREADABLE])
                self.assertNothingKept(html)
        html = self.post(ADD, depuis=self.take.pk).content.decode()
        self.assertEqual(notices_of(html, "error"), [UNREADABLE])
        self.assertEqual(entries_of(self.take), [])

    # -- what an entry keeps -----------------------------------------------

    def test_an_entry_keeps_its_lines_once_the_recipe_is_renamed_and_repriced(self):
        """The owner may have rung the list up: a recipe renamed or repriced
        since, its button or its article renamed, does not rewrite it. The
        next amount is planned at the new price, under the new names."""
        self.add("420")
        Recipe.objects.filter(pk=self.pint.pk).update(name="Pinte renommée exemple", selling_price_ttc=Decimal("36.50"))
        PosProduct.objects.filter(recipe=self.pint).update(name="PINTE RENOMMEE CAISSE EXEMPLE")
        StockType.objects.filter(pk=self.blonde.pk).update(name="Blonde renommée exemple")
        html = self.html(depuis=str(self.take.pk))
        row = row_of(table_of(html, PLAN_TABLE), "Pinte exemple")
        self.assertEqual(
            cells_of(row), ["Pinte exemple", "PINTE CAISSE EXEMPLE", "12", "35.00 €", "420.00 €", "Blonde exemple"]
        )
        # Still the recipe's own page, by its pk.
        self.assertIn(f'href="{reverse("recipes:recipe_detail", args=[self.pint.pk])}"', row)
        self.assertEqual(row_of(table_of(html, PLAN_TABLE), "renommée"), "")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "420.00 €")

        html = self.add("73")
        self.assertEqual(
            cells_of(row_of(table_of(html, PLAN_TABLE), "Pinte renommée exemple")),
            [
                "Pinte renommée exemple",
                "PINTE RENOMMEE CAISSE EXEMPLE",
                "2",
                "36.50 €",
                "73.00 €",
                "Blonde renommée exemple",
            ],
        )
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)
        self.assertEqual(
            cells_of(row_of(table_of(html, EARLIER_TABLE), "Pinte exemple"))[1:],
            ["420.00 €", "420.00 €", "Pinte exemple × 12"],
        )

    def test_an_entry_of_a_recipe_deleted_since_still_reads(self):
        self.add("420")
        Recipe.objects.filter(pk=self.pint.pk).delete()
        html = self.html(depuis=str(self.take.pk))
        self.assertEqual(
            self.plan_row(html),
            ["Pinte exemple", "PINTE CAISSE EXEMPLE", "12", "35.00 €", "420.00 €", "Blonde exemple"],
        )
        self.assertEqual(value_of(stat_of(html, ENTERED)), "420.00 €")

    # -- a list gone stale -------------------------------------------------

    def test_the_list_goes_stale_once_newer_sales_are_recorded(self):
        self.add("70")
        self.assertEqual(entries_of(self.take)[0].sales_up_to, self.sale_day)
        self.assertNotIn(STALE, messages_of(self.html(depuis=str(self.take.pk)), "warning"))
        # A late import of an older day holds none of the list's sales.
        RecipeSale.objects.create(recipe=self.pint, sold_on=self.today - timedelta(days=3), quantity=2)
        self.assertNotIn(STALE, messages_of(self.html(depuis=str(self.take.pk)), "warning"))
        # Today's sales imported: the list rung up today is in them.
        RecipeSale.objects.create(recipe=self.pint, sold_on=self.today, quantity=2)
        self.assertEqual(messages_of(self.html(depuis=str(self.take.pk)), "warning"), [STALE])
        # Cleared, there is nothing left to count twice.
        html = self.post(CLEAR, depuis=self.take.pk).content.decode()
        self.assertNotIn(STALE, messages_of(html, "warning"))

    def test_an_entry_made_after_the_newer_sales_is_not_stale(self):
        RecipeSale.objects.create(recipe=self.pint, sold_on=self.today, quantity=2)
        html = self.add("70")
        self.assertEqual(entries_of(self.take)[0].sales_up_to, self.today)
        self.assertNotIn(STALE, messages_of(html, "warning"))

    # A day imported again with more sales once left the list fresh. Today's
    # sales imported in the afternoon (the page calls that fresh: sales up to
    # today, no warning), the list made and rung up in the evening, today
    # imported again the next morning: the day's row then held the list's two
    # pints - the gaps counted them as sold AND the list added them again -
    # yet `gaps.list_is_stale` compared days only (last_sale_day >
    # sales_up_to), and the day had not changed. Nothing on the page said to
    # clear the list: silently, every gap read two pints fuller and the next
    # amount proposed less than it should. An entry keeps `sales_seen` since.
    def test_a_day_imported_again_with_more_sales_makes_the_list_stale(self):
        record_sales([("Pinte exemple", self.today, 4)], source="csv")
        html = self.add("70")
        self.assertEqual(messages_of(html, "warning"), [])
        # The same day imported again: the two pints of the list rung up since.
        self.assertEqual(record_sales([("Pinte exemple", self.today, 6)], source="csv").updated, 1)
        self.assertEqual(messages_of(self.html(depuis=str(self.take.pk)), "warning"), [STALE])

    # -- warnings ----------------------------------------------------------

    def test_purchases_newer_than_the_last_sale_are_said(self):
        bought(self.blonde, "10", self.today)
        html = self.html()
        self.assertEqual(messages_of(html, "warning"), [NEWER_PURCHASES + NOT_IMPORTED_YET])
        self.assertIn(f'href="{reverse("recipes:sales_import")}"', html)

    def test_one_till_product_linked_to_no_recipe(self):
        PosProduct.objects.create(name="BOUTON EXEMPLE", total_quantity=3)
        PosProduct.objects.create(name="CAFE EXEMPLE", total_quantity=9, ignored=True)
        html = self.html()
        self.assertIn(
            "1 produit de caisse n'est lié à aucune recette : ce qu'il vend compte comme écart.", text_of(html)
        )
        self.assertIn(f'href="{reverse("recipes:pos_product_list")}"', html)

    def test_several_till_products_linked_to_no_recipe(self):
        PosProduct.objects.create(name="BOUTON EXEMPLE", total_quantity=3)
        PosProduct.objects.create(name="AUTRE BOUTON EXEMPLE", total_quantity=1)
        self.assertIn(
            "2 produits de caisse ne sont liés à aucune recette : ce qu'ils vendent compte comme écart.",
            text_of(self.html()),
        )

    def test_no_such_warning_when_every_till_product_is_linked_or_ignored(self):
        PosProduct.objects.create(name="CAFE EXEMPLE", total_quantity=9, ignored=True)
        self.assertNotIn("produit de caisse", text_of(self.html()))


class WhichTakeTests(PageTestCase):
    """`?depuis=` names the count the gaps run from; the latest by default,
    and an unknown one falls back to it - and says so. An amount is POSTed
    for one count by its pk; for no count it knows, nothing is kept."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.older_day = cls.today - timedelta(days=40)
        cls.latest_day = cls.today - timedelta(days=20)
        cls.blonde = article("Blonde exemple")
        cls.older = make_stock_take(taken_at=noon(cls.older_day), note="Comptage exemple ancien")
        cls.latest = make_stock_take(taken_at=noon(cls.latest_day))
        cls.take = cls.latest
        counted(cls.older, cls.blonde, "50")
        counted(cls.latest, cls.blonde, "100")
        bought(cls.blonde, "200", cls.today - timedelta(days=10))
        cls.pint = recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=cls.today - timedelta(days=5), quantity=40)

    def opening(self, html):
        return cells_of(row_of(table_of(html, GAPS_TABLE), "Blonde exemple"))[1]

    def test_the_latest_take_by_default_and_no_message(self):
        html = self.html()
        self.assertNotIn(TAKE_NOT_FOUND, html)
        self.assertIn(f"Du {self.latest_day:%d/%m/%Y} au", text_of(html))
        self.assertEqual(selected_take(html), str(self.latest.pk))
        self.assertEqual(self.opening(html), "100")

    def test_an_empty_depuis_is_no_depuis(self):
        self.assertNotIn(TAKE_NOT_FOUND, self.html(depuis=""))

    def test_an_older_take(self):
        html = self.html(depuis=str(self.older.pk))
        self.assertNotIn(TAKE_NOT_FOUND, html)
        self.assertIn(f"Du {self.older_day:%d/%m/%Y} au {self.today:%d/%m/%Y}", text_of(html))
        self.assertEqual(selected_take(html), str(self.older.pk))
        self.assertEqual(self.opening(html), "50")
        self.assertIn(f"Inventaire du {self.older_day:%d/%m/%Y} — Comptage exemple ancien", html)

    def test_an_older_take_with_an_amount(self):
        response = self.post(ADD, depuis=self.older.pk, montant="70")
        html = response.content.decode()
        self.assertEqual(self.landing(response), f"{self.page_of(self.older)}#a-encaisser")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "70.00 €")
        self.assertEqual(selected_take(html), str(self.older.pk))
        self.assertEqual(amounts_of(self.older), [Decimal("70.00")])
        self.assertEqual(entries_of(self.latest), [])
        # The latest count's page has no list: the older count's is its own.
        self.assertNotIn(PROPOSED, self.html())

    def test_a_take_that_cannot_be_found_falls_back_on_the_latest(self):
        unknown = str(max(StockTake.objects.values_list("pk", flat=True)) + 1000)
        for asked in (unknown, "abc", "²", "-1", "1.5", "0"):
            with self.subTest(depuis=asked):
                html = self.html(depuis=asked)
                self.assertIn(TAKE_NOT_FOUND, messages_of(html, "warning"))
                self.assertIn(f"Du {self.latest_day:%d/%m/%Y} au", text_of(html))
                self.assertEqual(selected_take(html), str(self.latest.pk))
                self.assertEqual(self.opening(html), "100")
                # And an amount typed there goes to the latest count's list.
                self.assertIn(f'name="depuis" value="{self.latest.pk}"', form_of(html, ADD))

    def test_an_amount_for_a_take_that_cannot_be_found_keeps_nothing(self):
        unknown = str(max(StockTake.objects.values_list("pk", flat=True)) + 1000)
        for asked in (unknown, "abc", "²", "-1", "1.5", "0", "", "1" * 30):
            with self.subTest(depuis=asked):
                response = self.post(ADD, depuis=asked, montant="70")
                html = response.content.decode()
                self.assertEqual(self.landing(response), reverse(PAGE))
                self.assertEqual(notices_of(html, "error"), [TAKE_UNKNOWN])
                self.assertFalse(GapFillEntry.objects.exists())
                self.assertNotIn(PROPOSED, html)
                # Back on the latest count, which nothing was added to.
                self.assertEqual(selected_take(html), str(self.latest.pk))
        html = self.post(ADD, montant="70").content.decode()
        self.assertEqual(notices_of(html, "error"), [TAKE_UNKNOWN])
        self.assertFalse(GapFillEntry.objects.exists())


class WhatCannotBeFilledTests(PageTestCase):
    """« Ce qui ne peut pas être comblé »: each blocked recipe with the
    article holding it back and why, the recipes not sold since, the gaps
    only those recipes pour with their value, and how many articles bought
    are in no recipe at all."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        sale_day = today - timedelta(days=5)

        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "100")
        cls.pint = recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=sale_day, quantity=10)
        recipe("Galopin exemple", "30.50", cls.blonde, "0.125")  # not sold since

        # Sold more than counted and bought.
        cls.syrup = article("Sirop exemple")
        counted(cls.take, cls.syrup, "1")
        lemonade = recipe("Limonade exemple", "31.00", cls.syrup, "0.1")
        RecipeSale.objects.create(recipe=lemonade, sold_on=sale_day, quantity=30)
        # All of the gap is the normal loss.
        cls.cider = article("Cidre exemple")
        counted(cls.take, cls.cider, "10")
        bowl = recipe("Bolée exemple", "32.00", cls.cider, "0.5")
        RecipeSale.objects.create(recipe=bowl, sold_on=sale_day, quantity=19)
        # Not counted, and sold past what was bought since.
        cls.gin = article("Gin exemple")
        bought(cls.gin, "0.5", today - timedelta(days=10), unit_cost="30.00")
        gin_tonic = recipe("Gin tonic exemple", "34.00", cls.gin, "0.05")
        RecipeSale.objects.create(recipe=gin_tonic, sold_on=sale_day, quantity=20)
        # A gap only a recipe not sold since pours: 9 L counted, 8,1 L to
        # fill at 6,00 € HT.
        cls.wine = article("Vin exemple")
        bought(cls.wine, "12", today - timedelta(days=30), unit_cost="6.00")
        counted(cls.take, cls.wine, "9")
        recipe("Verre de vin exemple", "33.00", cls.wine, "0.15")
        # Bought since, and in no recipe at all.
        cls.cloth = make_stock_type(name="Nappe exemple", unit=UnitChoices.UNIT)
        bought(cls.cloth, "10", today - timedelta(days=10), unit_cost="2.00")

    def item_of(self, fragment, name) -> str:
        return next(
            (text_of(item) for item in re.findall(r"<li>(.*?)</li>", fragment, flags=re.DOTALL) if name in item), ""
        )

    def test_its_summary_counts_what_it_holds(self):
        self.assertEqual(
            summary_of(self.html()),
            "Ce qui ne peut pas être comblé · 3 recettes bloquées · 2 recettes pas vendues"
            " · 1 article sans recette proposée",
        )

    def test_each_blocked_recipe_with_its_article_and_why(self):
        part = explainer_of(self.html())
        self.assertEqual(
            self.item_of(part, "Limonade exemple"), "Limonade exemple — Sirop exemple (vendu plus qu'acheté)"
        )
        self.assertEqual(self.item_of(part, "Bolée exemple"), "Bolée exemple — Cidre exemple (dans la perte tolérée)")
        self.assertEqual(
            self.item_of(part, "Gin tonic exemple"), "Gin tonic exemple — Gin exemple (non compté à l'inventaire)"
        )
        self.assertEqual(self.item_of(part, "Pinte exemple"), "")

    def test_the_recipes_not_sold_since_and_the_gaps_no_recipe_pours(self):
        self.assertEqual(
            sentences_of(explainer_of(self.html())),
            [
                "Une seule vente ferait dépasser un écart :",
                (
                    "Pas vendues depuis l'inventaire, donc pas proposées : Galopin exemple (jamais vendue)"
                    " · Verre de vin exemple (jamais vendue)"
                ),
                "Aucune recette proposée ne l'utilise (48.60 € HT à combler) : Vin exemple",
                OUTSIDE_ONE,
            ],
        )

    def test_the_gaps_table_says_why_on_each_article(self):
        table = table_of(self.html(), GAPS_TABLE)
        self.assertIn("vendu plus qu'acheté", cells_of(row_of(table, "Sirop exemple"))[0])
        self.assertIn("dans la perte tolérée", cells_of(row_of(table, "Cidre exemple"))[0])
        self.assertIn("non compté", cells_of(row_of(table, "Gin exemple"))[0])
        self.assertEqual(cells_of(row_of(table, "Sirop exemple"))[7], "—")  # nothing « À combler »
        # The wine is no proposed recipe's, the tablecloth no recipe's at
        # all: neither is a row of the table.
        self.assertEqual(row_of(table, "Vin exemple"), "")
        self.assertEqual(row_of(table, "Nappe exemple"), "")

    def test_a_plan_is_made_of_the_recipe_left(self):
        html = self.add("105")
        self.assertEqual(
            cells_of(row_of(table_of(html, PLAN_TABLE), "Pinte exemple")),
            ["Pinte exemple", "Pinte exemple", "3", "35.00 €", "105.00 €", "Blonde exemple"],
        )
        for name in ("Limonade exemple", "Bolée exemple", "Gin tonic exemple", "Galopin exemple"):
            self.assertEqual(row_of(table_of(html, PLAN_TABLE), name), "", name)
        # The blocked articles add nothing: « — » in the list's columns.
        table = table_of(html, GAPS_TABLE)
        self.assertEqual(cells_of(row_of(table, "Sirop exemple"))[-3:], ["—", "—", EXCLUDE_BUTTON])
        self.assertEqual(cells_of(row_of(table, "Blonde exemple"))[-3:-2], ["1.50"])


class BlockedByLessThanOneServingTests(PageTestCase):
    """10,3 L of blonde counted, 18 pints sold: 0,27 L left to fill once the
    1,03 L allowance is set aside - past the allowance, short of one pint."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "10.3")
        pint = recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=pint, sold_on=today - timedelta(days=5), quantity=18)

    # 0,27 L is still « À combler », only less than one serving: the
    # blocked recipe's reason once fell through to « dans la perte
    # tolérée », which the same page contradicted.
    def test_the_reason_does_not_call_it_within_the_allowance(self):
        html = self.html()
        self.assertEqual(cells_of(row_of(table_of(html, GAPS_TABLE), "Blonde exemple"))[7], "0.27")
        item = next(
            (text_of(item) for item in re.findall(r"<li>(.*?)</li>", html, flags=re.DOTALL) if "Pinte exemple" in item),
            "",
        )
        self.assertTrue(item.startswith("Pinte exemple — Blonde exemple ("), item)
        self.assertNotIn("dans la perte tolérée", item)

    def test_an_amount_with_no_recipe_to_propose_is_not_kept(self):
        html = self.add("70")
        self.assertEqual(notices_of(html, "warning"), [f"Rien pour 70.00 € : {GAPS_FULL}."])
        self.assertEqual(entries_of(self.take), [])
        self.assertNotIn(PROPOSED, html)


def what_cannot_be_filled(how_many: int):
    """`how_many` (1 or 2) of each thing « Ce qui ne peut pas être comblé »
    names, beside a pint that can be proposed: recipes held back by an
    article sold past what it had, recipes not sold since the take - each
    the only one pouring an article, which is then left without a recipe
    proposed - and articles bought in no recipe at all. Dated from today."""
    today = timezone.localdate()
    take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
    sale_day = today - timedelta(days=1)
    blonde = article("Blonde exemple")
    counted(take, blonde, "100")
    pint = recipe("Pinte exemple", "35.00", blonde, "0.5")
    RecipeSale.objects.create(recipe=pint, sold_on=sale_day, quantity=10)
    blocked = (("Sirop exemple", "Limonade exemple"), ("Grenadine exemple", "Diabolo exemple"))
    not_sold = (("Vin exemple", "Verre de vin exemple", "6.00"), ("Porto exemple", "Verre de porto exemple", "10.00"))
    outside = ("Nappe exemple", "Gobelets exemple")
    for index in range(how_many):
        over, name = blocked[index]
        stock_type = article(over)
        counted(take, stock_type, "1")
        RecipeSale.objects.create(recipe=recipe(name, "31.00", stock_type, "0.1"), sold_on=sale_day, quantity=30)
        unreached, name, cost = not_sold[index]
        stock_type = article(unreached)
        bought(stock_type, "1", today - timedelta(days=30), unit_cost=cost)
        counted(take, stock_type, "2")
        recipe(name, "33.00", stock_type, "0.15")
        bought(make_stock_type(name=outside[index], unit=UnitChoices.UNIT), "10", today - timedelta(days=10))
    return take


class OneOfEachCannotBeFilledTests(PageTestCase):
    """Each sentence in the singular. Wine: 2 L counted, 1,8 L to fill at
    6,00 € HT."""

    @classmethod
    def setUpTestData(cls):
        what_cannot_be_filled(1)

    def test_the_summary(self):
        self.assertEqual(
            summary_of(self.html()),
            "Ce qui ne peut pas être comblé · 1 recette bloquée · 1 recette pas vendue · 1 article sans recette proposée",
        )

    def test_each_sentence(self):
        self.assertEqual(
            sentences_of(explainer_of(self.html())),
            [
                "Une seule vente ferait dépasser un écart :",
                "Pas vendue depuis l'inventaire, donc pas proposée : Verre de vin exemple (jamais vendue)",
                "Aucune recette proposée ne l'utilise (10.80 € HT à combler) : Vin exemple",
                OUTSIDE_ONE,
            ],
        )


class TwoOfEachCannotBeFilledTests(PageTestCase):
    """Each sentence in the plural. Wine: 1,8 L to fill at 6,00 € HT;
    port: 1,8 L at 10,00 € HT - the dearer first."""

    @classmethod
    def setUpTestData(cls):
        what_cannot_be_filled(2)

    def test_the_summary(self):
        self.assertEqual(
            summary_of(self.html()),
            "Ce qui ne peut pas être comblé · 2 recettes bloquées · 2 recettes pas vendues"
            " · 2 articles sans recette proposée",
        )

    def test_each_sentence(self):
        self.assertEqual(
            sentences_of(explainer_of(self.html())),
            [
                "Une seule vente ferait dépasser un écart :",
                (
                    "Pas vendues depuis l'inventaire, donc pas proposées : Verre de porto exemple (jamais vendue)"
                    " · Verre de vin exemple (jamais vendue)"
                ),
                "Aucune recette proposée ne les utilise (28.80 € HT à combler) : Porto exemple · Vin exemple",
                OUTSIDE_TWO,
            ],
        )

    def test_each_blocked_recipe_on_its_own_line(self):
        items = [text_of(item) for item in re.findall(r"<li>(.*?)</li>", explainer_of(self.html()), flags=re.DOTALL)]
        self.assertEqual(
            items,
            [
                "Diabolo exemple — Grenadine exemple (vendu plus qu'acheté)",
                "Limonade exemple — Sirop exemple (vendu plus qu'acheté)",
            ],
        )


class OnlyArticlesOutsideTheRecipesTests(PageTestCase):
    """Nothing blocked, every recipe sold, every article some recipe pours
    proposed: the fold still opens on the articles no recipe pours, and its
    summary says nothing it does not hold."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        blonde = article("Blonde exemple")
        counted(take, blonde, "100")
        pint = recipe("Pinte exemple", "35.00", blonde, "0.5")
        RecipeSale.objects.create(recipe=pint, sold_on=today - timedelta(days=1), quantity=10)
        bought(make_stock_type(name="Nappe exemple", unit=UnitChoices.UNIT), "10", today - timedelta(days=10))

    def test_the_fold_holds_that_sentence_alone(self):
        html = self.html()
        self.assertEqual(summary_of(html), "Ce qui ne peut pas être comblé")
        self.assertEqual(
            sentences_of(explainer_of(html)),
            [OUTSIDE_ONE],
        )


class SalesBehindTests(PageTestCase):
    """The till's sales are imported a day behind at best. Imported up to
    yesterday, with no delivery since, the page is fresh; short of that -
    or with a delivery newer than the last sale - every gap still holds
    sales the till has not handed over, and the page says to import them
    before ringing anything up."""

    def setUp(self):
        self.today = timezone.localdate()
        self.take = make_stock_take(taken_at=noon(self.today - timedelta(days=20)))
        self.blonde = article("Blonde exemple")
        counted(self.take, self.blonde, "100")
        self.pint = recipe("Pinte exemple", "35.00", self.blonde, "0.5")

    def sold_on(self, days_ago: int):
        RecipeSale.objects.create(recipe=self.pint, sold_on=self.today - timedelta(days=days_ago), quantity=4)

    def bought_on(self, days_ago: int):
        bought(self.blonde, "30", self.today - timedelta(days=days_ago))

    def warnings(self):
        return messages_of(self.html(), "warning")

    def test_sales_up_to_yesterday_and_older_purchases(self):
        self.bought_on(10)
        self.sold_on(1)
        self.assertEqual(self.warnings(), [])

    def test_sales_up_to_today(self):
        self.sold_on(0)
        self.assertEqual(self.warnings(), [])

    def test_sales_stopping_three_days_ago(self):
        self.bought_on(10)
        self.sold_on(3)
        self.assertEqual(self.warnings(), [NOT_UP_TO_YESTERDAY + NOT_IMPORTED_YET])

    def test_a_purchase_newer_than_the_last_sale(self):
        self.sold_on(3)
        self.bought_on(2)
        self.assertEqual(self.warnings(), [NEWER_PURCHASES + NOT_IMPORTED_YET])

    def test_a_purchase_today_after_sales_up_to_yesterday(self):
        self.sold_on(1)
        self.bought_on(0)
        self.assertEqual(self.warnings(), [NEWER_PURCHASES + NOT_IMPORTED_YET])

    def test_no_sale_at_all(self):
        self.assertEqual(self.warnings(), [NOT_UP_TO_YESTERDAY + NOT_IMPORTED_YET])

    def test_no_sale_at_all_and_a_purchase(self):
        self.bought_on(10)
        self.assertEqual(self.warnings(), [NEWER_PURCHASES + NOT_IMPORTED_YET])

    def test_the_warning_leads_to_the_import(self):
        self.sold_on(3)
        found = re.search(r'<div class="message message-warning">(.*?)</div>', self.html(), flags=re.DOTALL)
        self.assertIn(
            f'<a href="{reverse("recipes:sales_import")}">Importer les ventes</a>', found.group(1) if found else ""
        )

    def test_the_warning_stands_beside_a_list(self):
        self.sold_on(3)
        html = self.add("70")
        self.assertEqual(messages_of(html, "warning"), [NOT_UP_TO_YESTERDAY + NOT_IMPORTED_YET])
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "70.00 €")


def rung_up(button, day, quantity, money):
    """What the till took for `button` on `day`, its money read."""
    return PosProductDailyQuantity.objects.create(
        product=button, sold_on=day, quantity=quantity, revenue_ttc=Decimal(money), revenue_read=True
    )


class TillPriceTests(PageTestCase):
    """The button named on a line is the one ringing the recipe's price;
    a line whose button still rings another one says what the till charges
    under it.

    A pint at 35,00 € and a bowl of cider at 32,00 €, sold since a count 20
    days ago. The pint has its own button at 35,00 € (rung little) and a
    glass button at 13,30 € (rung a lot); the bowl's one button rings
    30,10 €. 67,00 € is one of each and nothing else."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        sale_day = today - timedelta(days=1)
        blonde, cider = article("Blonde exemple"), article("Cidre exemple")
        counted(cls.take, blonde, "100")
        counted(cls.take, cider, "100")
        cls.pint = recipe("Pinte exemple", "35.00", blonde, "0.5")
        cls.bowl = recipe("Bolée exemple", "32.00", cider, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=sale_day, quantity=10)
        RecipeSale.objects.create(recipe=cls.bowl, sold_on=sale_day, quantity=10)
        own = PosProduct.objects.create(name="Pinte caisse exemple", recipe=cls.pint, total_quantity=3)
        rung_up(own, sale_day, 3, "105.00")
        glass = PosProduct.objects.create(name="Verre exemple", recipe=cls.pint, total_quantity=40)
        rung_up(glass, sale_day, 40, "532.00")
        cls.bowl_button = PosProduct.objects.create(name="Bolée caisse exemple", recipe=cls.bowl, total_quantity=10)
        rung_up(cls.bowl_button, sale_day, 9, "270.90")
        # One bowl comped: the day's average is no price.
        rung_up(cls.bowl_button, today - timedelta(days=2), 2, "30.10")

    def plan_row(self, html, name):
        return cells_of(row_of(table_of(html, PLAN_TABLE), name))

    def test_the_line_names_the_button_at_its_price(self):
        html = self.add("35")
        self.assertEqual(
            self.plan_row(html, "Pinte exemple"),
            ["Pinte exemple", "Pinte caisse exemple", "1", "35.00 €", "35.00 €", "Blonde exemple"],
        )
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)
        self.assertEqual(entries_of(self.take)[0].lines[0]["till_price"], "35.00")

    def test_a_button_ringing_another_price_says_it_under_its_name(self):
        html = self.add("32")
        self.assertEqual(
            self.plan_row(html, "Bolée exemple"),
            ["Bolée exemple", "Bolée caisse exemple en caisse 30.10 €", "1", "32.00 €", "32.00 €", "Cidre exemple"],
        )
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)

    def test_two_lines_at_another_price(self):
        # The pint's own button rings 34,00 € most often now, and is the one
        # rung most: no button rings the pint's price any more.
        own = PosProduct.objects.get(name="Pinte caisse exemple")
        rung_up(own, timezone.localdate(), 9, "306.00")
        PosProduct.objects.filter(pk=own.pk).update(total_quantity=60)
        html = self.add("67")
        self.assertEqual(
            self.plan_row(html, "Pinte exemple"),
            ["Pinte exemple", "Pinte caisse exemple en caisse 34.00 €", "1", "35.00 €", "35.00 €", "Blonde exemple"],
        )
        self.assertEqual(
            self.plan_row(html, "Bolée exemple"),
            ["Bolée exemple", "Bolée caisse exemple en caisse 30.10 €", "1", "32.00 €", "32.00 €", "Cidre exemple"],
        )
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)

    def test_a_button_never_rung_since_the_take_says_nothing(self):
        PosProductDailyQuantity.objects.filter(product=self.bowl_button).delete()
        html = self.add("32")
        self.assertEqual(
            self.plan_row(html, "Bolée exemple"),
            ["Bolée exemple", "Bolée caisse exemple", "1", "32.00 €", "32.00 €", "Cidre exemple"],
        )
        self.assertNotIn("en caisse", table_of(html, PLAN_TABLE))
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)

    def test_an_entry_keeps_the_till_price_it_was_proposed_at(self):
        """What the till charged when the bowl was proposed stays on its
        line; the next bowl proposed reads the till as it is now."""
        self.add("32")
        rung_up(self.bowl_button, timezone.localdate(), 20, "620.00")  # 31,00 € a bowl, most often now
        html = self.html(depuis=str(self.take.pk))
        self.assertEqual(
            self.plan_row(html, "Bolée exemple"),
            ["Bolée exemple", "Bolée caisse exemple en caisse 30.10 €", "1", "32.00 €", "32.00 €", "Cidre exemple"],
        )
        html = self.add("32")
        self.assertEqual(
            self.plan_row(html, "Bolée exemple"),
            ["Bolée exemple", "Bolée caisse exemple en caisse 31.00 €", "1", "32.00 €", "32.00 €", "Cidre exemple"],
        )

    def test_the_earlier_entries_name_each_recipe_and_its_count(self):
        self.add("67")
        html = self.add("35")
        self.assertEqual(earlier_summary_of(html), "Montants déjà saisis · 1")
        (first, _second) = entries_of(self.take)
        self.assertEqual(
            cells_of(row_of(table_of(html, EARLIER_TABLE), "67.00 €")),
            [
                timezone.localtime(first.created_at).strftime("%d/%m/%Y %H:%M"),
                "67.00 €",
                "67.00 €",
                "Pinte exemple × 1, Bolée exemple × 1",
            ],
        )

    # -- the note under « À encaisser » ------------------------------------

    def test_the_note_counts_one_line_at_another_till_price(self):
        html = self.add("32")
        (entry,) = entries_of(self.take)
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT} · {ONE_TILL_PRICE}")

    def test_the_note_counts_two_lines_at_another_till_price(self):
        # As in test_two_lines_at_another_price: no button rings the pint's
        # price any more, nor the bowl's.
        own = PosProduct.objects.get(name="Pinte caisse exemple")
        rung_up(own, timezone.localdate(), 9, "306.00")
        PosProduct.objects.filter(pk=own.pk).update(total_quantity=60)
        html = self.add("67")
        (entry,) = entries_of(self.take)
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT} · {TWO_TILL_PRICES}")

    def test_no_word_of_the_till_when_its_button_rings_the_price(self):
        html = self.add("35")
        (entry,) = entries_of(self.take)
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT}")
        self.assertNotIn("en caisse", table_of(html, PLAN_TABLE))

    def test_the_note_is_the_last_entry_s_alone(self):
        # The bowl rung at 30,10 € was in the first amount, not the last.
        self.add("32")
        html = self.add("35")
        _first, last = entries_of(self.take)
        self.assertEqual(entry_note_of(html), f"{typed_at(last)} · {ENTRY_EXACT}")
        # Taken back, the first is the last again, and its note says it.
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        (first,) = entries_of(self.take)
        self.assertEqual(entry_note_of(html), f"{typed_at(first)} · {ENTRY_EXACT} · {ONE_TILL_PRICE}")


class GapsFullTests(PageTestCase):
    """Rum: 0,2 L counted, one shot of 4 cl sold since - 0,14 L to fill once
    its 0,02 L allowance is set aside: three shots fit, a fourth would not.
    200,00 € asked, 96,00 € proposed."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        rum = article("Rhum exemple")
        bought(rum, "1", today - timedelta(days=30), unit_cost="20.00")
        counted(cls.take, rum, "0.2")
        shot = recipe("Shot exemple", "32.00", rum, "0.04")
        RecipeSale.objects.create(recipe=shot, sold_on=today - timedelta(days=1), quantity=1)

    def test_the_note_says_no_sale_fits_any_more(self):
        html = self.add("200")
        proposed = stat_of(html, PROPOSED)
        self.assertEqual(value_of(proposed), "96.00 €")
        self.assertEqual(note_of(proposed), "104.00 € non proposés")
        (entry,) = entries_of(self.take)
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · reste 104.00 € : {GAPS_FULL}")
        self.assertEqual(entry.reason, "gaps_full")
        self.assertEqual(
            cells_of(row_of(table_of(html, PLAN_TABLE), "Shot exemple")),
            ["Shot exemple", "Shot exemple", "3", "32.00 €", "96.00 €", "Rhum exemple"],
        )

    def test_once_the_list_fills_the_gap_an_amount_is_not_kept(self):
        """32 € alone is one shot; on top of a list that took the rum to its
        room, it is nothing - said, and not kept."""
        html = self.add("32")
        self.assertEqual(heading_of(html), "À encaisser : 32.00 €")
        GapFillEntry.objects.all().delete()
        self.add("96")
        html = self.add("32")
        self.assertEqual(notices_of(html, "warning"), [f"Rien pour 32.00 € : {GAPS_FULL}."])
        self.assertEqual(amounts_of(self.take), [Decimal("96.00")])
        self.assertEqual(heading_of(html), "À encaisser : 96.00 €")
        # Under the shot's price it is the cheapest recipe that says why.
        html = self.add("20")
        self.assertEqual(notices_of(html, "warning"), [f"Rien pour 20.00 € : {BELOW_CHEAPEST}."])
        self.assertEqual(amounts_of(self.take), [Decimal("96.00")])


class NoLineReasonsTests(PageTestCase):
    """An amount nothing can be proposed for is said with ITS reason, and
    kept nowhere. A bowl of cider at 31,30 € - 2 L counted, one bowl of 0,5 L
    sold since: 1,3 L to fill once the 0,2 L allowance is set aside, two
    bowls - and a pint at 34,70 € - 3 L counted, one sold: 2,2 L to fill,
    four pints."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        sale_day = today - timedelta(days=1)
        cider, blonde = article("Cidre exemple"), article("Blonde exemple")
        counted(cls.take, cider, "2")
        counted(cls.take, blonde, "3")
        cls.bowl = recipe("Bolée exemple", "31.30", cider, "0.5")
        cls.pint = recipe("Pinte exemple", "34.70", blonde, "0.5")
        for made in (cls.bowl, cls.pint):
            RecipeSale.objects.create(recipe=made, sold_on=sale_day, quantity=1)

    def refused(self, amount: str) -> list[str]:
        """What typing `amount` is told, once it is shown it kept nothing."""
        kept = amounts_of(self.take)
        html = self.add(amount)
        self.assertEqual(amounts_of(self.take), kept)
        self.assertEqual(notices_of(html, "error"), [])
        return notices_of(html, "warning")

    def bowls_on_the_list(self):
        """The list takes the cider to its room: two bowls, 62,60 €."""
        html = self.add("62.60")
        self.assertEqual(
            cells_of(row_of(table_of(html, PLAN_TABLE), "Bolée exemple")),
            ["Bolée exemple", "Bolée exemple", "2", "31.30 €", "62.60 €", "Cidre exemple"],
        )
        self.assertEqual(row_of(table_of(html, PLAN_TABLE), "Pinte exemple"), "")

    def test_under_the_cheapest_recipe(self):
        self.assertEqual(self.refused("20.10"), [f"Rien pour 20.10 € : {BELOW_CHEAPEST}."])

    def test_no_combination_of_prices(self):
        # On an empty list, 33,10 € is a bowl, 1,80 € short - kept.
        html = self.add("33.10")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "31.30 €")
        self.assertEqual(note_of(stat_of(html, PROPOSED)), "1.80 € non proposé")
        GapFillEntry.objects.all().delete()
        # Once the list has filled the cider, the bowl is out; the pint still
        # has room, but 34,70 € is more than the amount: nothing lands on it.
        self.bowls_on_the_list()
        self.assertEqual(self.refused("33.10"), [f"Rien pour 33.10 € : {NO_COMBINATION}."])

    def test_no_more_sale_fits_the_gaps(self):
        self.bowls_on_the_list()
        html = self.add("138.80")
        self.assertEqual(
            cells_of(row_of(table_of(html, PLAN_TABLE), "Pinte exemple")),
            ["Pinte exemple", "Pinte exemple", "4", "34.70 €", "138.80 €", "Blonde exemple"],
        )
        self.assertEqual(self.refused("40.10"), [f"Rien pour 40.10 € : {GAPS_FULL}."])
        # Under the bowl's price it is still the cheapest recipe that says why.
        self.assertEqual(self.refused("20.10"), [f"Rien pour 20.10 € : {BELOW_CHEAPEST}."])

    def test_three_reasons_three_sentences(self):
        self.bowls_on_the_list()
        below = self.refused("20.10")
        no_combination = self.refused("33.10")
        self.add("138.80")
        full = self.refused("40.10")
        self.assertEqual(len({*below, *no_combination, *full}), 3)


class TakeMadeYesterdayTests(PageTestCase):
    """A count made yesterday at noon, and yesterday's sales imported this
    morning: they fall on the count's own day - in the count already, out of
    the gaps - and are still the last day of sales imported, whatever the
    count's day. Read inside the window, the page said « — » and that the
    sales were not imported up to yesterday."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.yesterday = cls.today - timedelta(days=1)
        cls.take = make_stock_take(taken_at=noon(cls.yesterday))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "100")
        cls.pint = recipe("Pinte exemple", "35.00", cls.blonde, "0.5")

    def test_sales_imported_up_to_yesterday(self):
        RecipeSale.objects.create(recipe=self.pint, sold_on=self.yesterday, quantity=12)
        html = self.html()
        self.assertIn(
            f"Du {self.yesterday:%d/%m/%Y} au {self.today:%d/%m/%Y} · "
            f"ventes importées jusqu'au {self.yesterday:%d/%m/%Y} · achats jusqu'au —",
            text_of(html),
        )
        self.assertEqual(messages_of(html, "warning"), [])
        self.assertNotIn(NOT_UP_TO_YESTERDAY, text_of(html))

    def test_sales_stopping_before_the_count_are_still_behind(self):
        two_days_ago = self.today - timedelta(days=2)
        RecipeSale.objects.create(recipe=self.pint, sold_on=two_days_ago, quantity=12)
        html = self.html()
        self.assertIn(f"ventes importées jusqu'au {two_days_ago:%d/%m/%Y}", text_of(html))
        self.assertEqual(messages_of(html, "warning"), [NOT_UP_TO_YESTERDAY + NOT_IMPORTED_YET])


class SecondaryTargetTests(PageTestCase):
    """A shot « au choix » between a dear rum and a cheaper one: the engine
    books every shot on the dear one while it has room, so no sale reaches
    the cheaper rum yet - its row says so, and the share the gaps shrink by
    leaves it out rather than reading 0 % as the lowest."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        premium, standard = article("Rhum vieux exemple"), article("Rhum blanc exemple")
        bought(premium, "1", today - timedelta(days=30), unit_cost="40.00")
        bought(standard, "1", today - timedelta(days=30), unit_cost="20.00")
        counted(cls.take, premium, "2")
        counted(cls.take, standard, "1")
        shot = make_recipe(name="Shot au choix exemple", selling_price_ttc="33.00")
        make_ingredient(shot, stock_type=premium, quantity="0.04", group=0)
        make_ingredient(shot, stock_type=standard, quantity="0.04", group=0)
        RecipeSale.objects.create(recipe=shot, sold_on=today - timedelta(days=1), quantity=10)

    def first_cell(self, html, name):
        return cells_of(row_of(table_of(html, GAPS_TABLE), name))[0]

    def test_the_cheaper_side_says_no_sale_reaches_it(self):
        html = self.html()
        self.assertEqual(self.first_cell(html, "Rhum blanc exemple"), f"Rhum blanc exemple litre {SECONDARY}")
        self.assertEqual(self.first_cell(html, "Rhum vieux exemple"), "Rhum vieux exemple litre")

    def test_the_share_leaves_it_out(self):
        # Three shots, all on the dear rum: 0,12 L of its 1,4 L.
        html = self.add("99")
        shares = stat_of(html, SHARES)
        self.assertEqual(value_of(shares), "8.6 %")
        self.assertEqual(note_of(shares), "en moyenne, de 8.6 % à 8.6 % selon l'article")
        row = cells_of(row_of(table_of(html, GAPS_TABLE), "Rhum blanc exemple"))
        self.assertEqual(row[-3:], ["—", "0.0 %", EXCLUDE_BUTTON])


class AmountAfterAmountTests(PageTestCase):
    """The list itself: amounts typed one after the other, each planned on
    top of the ones before, the last one to ring up first; undone one at a
    time or cleared, for its own count only.

    Two pints at 35,00 €, one keg each, both sold 40 times yesterday and
    bought 200 L of at 4,00 € HT ten days ago. Since the latest count
    (20 days ago): blonde 100 L counted, 250 L to fill; amber 120 L
    counted, 268 L to fill - the bigger gap, so a first pint goes to it, and
    a second, on top of it, to the blonde. Another count, 40 days ago,
    has a list of its own."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        sale_day = cls.today - timedelta(days=1)
        cls.older = make_stock_take(taken_at=noon(cls.today - timedelta(days=40)))
        cls.take = make_stock_take(taken_at=noon(cls.today - timedelta(days=20)))
        cls.blonde, cls.amber = article("Blonde exemple"), article("Ambrée exemple")
        counted(cls.older, cls.blonde, "50")
        counted(cls.take, cls.blonde, "100")
        counted(cls.take, cls.amber, "120")
        for keg in (cls.blonde, cls.amber):
            bought(keg, "200", cls.today - timedelta(days=10))
        cls.blonde_pint = recipe("Pinte blonde exemple", "35.00", cls.blonde, "0.5")
        cls.amber_pint = recipe("Pinte ambrée exemple", "35.00", cls.amber, "0.5")
        for pint in (cls.blonde_pint, cls.amber_pint):
            RecipeSale.objects.create(recipe=pint, sold_on=sale_day, quantity=40)

    def plan_names(self, html) -> list[str]:
        return [cells_of(row)[0] for row in body_rows(table_of(html, PLAN_TABLE))]

    def gaps_rows(self, html) -> dict[str, list[str]]:
        table = table_of(html, GAPS_TABLE)
        return {name: cells_of(row_of(table, name)) for name in ("Blonde exemple", "Ambrée exemple")}

    def test_the_second_amount_is_planned_on_top_of_the_first(self):
        html = self.add("35")
        self.assertEqual(self.plan_names(html), ["Pinte ambrée exemple"])
        # The same 35 € again: not the same pint, the gap it filled is
        # ahead now - the blonde's.
        html = self.add("35")
        self.assertEqual(heading_of(html), "À encaisser : 35.00 €")
        self.assertEqual(self.plan_names(html), ["Pinte blonde exemple"])
        first, second = entries_of(self.take)
        self.assertEqual([line["name"] for line in first.lines], ["Pinte ambrée exemple"])
        self.assertEqual([line["name"] for line in second.lines], ["Pinte blonde exemple"])
        # And the gaps show what both add, not the last alone.
        rows = self.gaps_rows(html)
        self.assertEqual(rows["Ambrée exemple"][-3:], ["0.50", "0.2 %", EXCLUDE_BUTTON])
        self.assertEqual(rows["Blonde exemple"][-3:], ["0.50", "0.2 %", EXCLUDE_BUTTON])
        self.assertEqual(value_of(stat_of(html, SALES)), "2")
        # 35 € alone, on a list cleared, is the amber pint again.
        GapFillEntry.objects.filter(stock_take=self.take).delete()
        self.assertEqual(self.plan_names(self.add("35")), ["Pinte ambrée exemple"])

    def test_each_entry_holds_its_new_sales_only(self):
        self.add("70")
        html = self.add("70")
        self.assertEqual(
            [cells_of(row)[2] for row in body_rows(table_of(html, PLAN_TABLE))],
            ["1", "1"],
        )
        self.assertEqual([entry.sales for entry in entries_of(self.take)], [2, 2])
        self.assertEqual(value_of(stat_of(html, SALES)), "4")
        rows = self.gaps_rows(html)
        self.assertEqual(rows["Ambrée exemple"][-3], "1")
        self.assertEqual(rows["Blonde exemple"][-3], "1")

    def test_the_list_columns_only_with_a_list(self):
        # Nine figures and the row's « Exclure »; with a list, eleven.
        html = self.html(depuis=str(self.take.pk))
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 10)
        html = self.add("35")
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 12)
        # The blonde has no pint yet: nothing proposed, 0 % filled.
        self.assertEqual(self.gaps_rows(html)["Blonde exemple"][-3:], ["—", "0.0 %", EXCLUDE_BUTTON])

    def test_the_earlier_entries_newest_first(self):
        for amount in ("35", "70"):
            self.add(amount)
        html = self.add("105")
        self.assertEqual(heading_of(html), "À encaisser : 105.00 €")
        self.assertEqual(
            self.plan_names(html),
            ["Pinte blonde exemple", "Pinte ambrée exemple"],
        )
        self.assertEqual(earlier_summary_of(html), "Montants déjà saisis · 2")
        first, second, _third = entries_of(self.take)
        self.assertEqual(
            [cells_of(row) for row in body_rows(table_of(html, EARLIER_TABLE))],
            [
                [
                    timezone.localtime(second.created_at).strftime("%d/%m/%Y %H:%M"),
                    "70.00 €",
                    "70.00 €",
                    "Pinte ambrée exemple × 1, Pinte blonde exemple × 1",
                ],
                [
                    timezone.localtime(first.created_at).strftime("%d/%m/%Y %H:%M"),
                    "35.00 €",
                    "35.00 €",
                    "Pinte ambrée exemple × 1",
                ],
            ],
        )
        self.assertEqual(value_of(stat_of(html, ENTERED)), "210.00 €")
        self.assertEqual(note_of(stat_of(html, ENTERED)), "3 montants")
        self.assertEqual(value_of(stat_of(html, SALES)), "6")

    def test_the_proposed_note_exact_then_short_then_exact(self):
        html = self.add("35")
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)
        html = self.add("50")
        self.assertEqual(value_of(stat_of(html, ENTERED)), "85.00 €")
        self.assertEqual(note_of(stat_of(html, ENTERED)), "2 montants")
        proposed = stat_of(html, PROPOSED)
        self.assertEqual(value_of(proposed), "70.00 €")
        self.assertEqual(note_of(proposed), "15.00 € non proposés")
        self.assertEqual(
            entry_note_of(html), f"{typed_at(entries_of(self.take)[-1])} · reste 15.00 € : {NO_COMBINATION}"
        )
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        self.assertEqual(value_of(stat_of(html, ENTERED)), "35.00 €")
        self.assertEqual(note_of(stat_of(html, ENTERED)), "1 montant")
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)

    def test_undo_takes_back_the_last_entry_of_this_take_only(self):
        self.add("35", take=self.older)
        self.add("35")
        self.add("70")
        response = self.post(UNDO, depuis=self.take.pk)
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_of(self.take))
        self.assertEqual(notices_of(html, "success"), [UNDONE])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00")])
        self.assertEqual(amounts_of(self.older), [Decimal("35.00")])
        self.assertEqual(heading_of(html), "À encaisser : 35.00 €")
        self.assertEqual(table_of(html, EARLIER_TABLE), "")
        self.assertEqual(self.gaps_rows(html)["Ambrée exemple"][-3], "0.50")
        self.assertEqual(self.gaps_rows(html)["Blonde exemple"][-3], "—")
        # The last one too: the page is back to the gaps alone.
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        self.assertEqual(notices_of(html, "success"), [UNDONE])
        self.assertEqual(entries_of(self.take), [])
        self.assertNotIn(PROPOSED, html)
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 10)
        # Nothing left to undo: nothing said, the other count's list intact.
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        self.assertEqual(notices_of(html, "success"), [])
        self.assertEqual(amounts_of(self.older), [Decimal("35.00")])

    def test_clear_empties_this_take_s_list_only(self):
        self.add("35", take=self.older)
        self.add("35")
        self.add("70")
        response = self.post(CLEAR, depuis=self.take.pk)
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_of(self.take))
        self.assertEqual(notices_of(html, "success"), [CLEARED])
        self.assertEqual(entries_of(self.take), [])
        self.assertEqual(amounts_of(self.older), [Decimal("35.00")])
        self.assertNotIn(PROPOSED, html)
        self.assertEqual(heading_of(html), "")
        self.assertEqual(form_of(html, UNDO), "")
        self.assertEqual(form_of(html, CLEAR), "")
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 10)
        # The other count still shows its own.
        self.assertEqual(heading_of(self.html(depuis=str(self.older.pk))), "À encaisser : 35.00 €")
        # An empty list: nothing to clear, nothing said.
        html = self.post(CLEAR, depuis=self.take.pk).content.decode()
        self.assertEqual(notices_of(html, "success"), [])

    def test_the_clear_form_asks_first_and_carries_its_token(self):
        self.add("35")
        html = self.add("70")
        form = form_of(html, CLEAR)
        self.assertIn('data-confirm="Effacer les 2 montants de la liste ?"', form)
        self.assertIn('name="csrfmiddlewaretoken"', form)
        self.assertIn(f'<input type="hidden" name="depuis" value="{self.take.pk}">', form)
        self.assertIn("Effacer la liste", text_of(form))
        undo = form_of(html, UNDO)
        self.assertIn('name="csrfmiddlewaretoken"', undo)
        self.assertIn(f'<input type="hidden" name="depuis" value="{self.take.pk}">', undo)
        self.assertIn("Annuler la dernière saisie", text_of(undo))

    def test_the_clear_confirmation_counts_the_amounts(self):
        for typed, asked in (
            ("35", "Effacer le montant de la liste ?"),
            ("70", "Effacer les 2 montants de la liste ?"),
            ("35", "Effacer les 3 montants de la liste ?"),
        ):
            with self.subTest(asked=asked):
                self.assertEqual(confirm_of(form_of(self.add(typed), CLEAR)), asked)
        # One taken back: the count follows the list.
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        self.assertEqual(confirm_of(form_of(html, CLEAR)), "Effacer les 2 montants de la liste ?")

    def test_a_clear_without_its_token_is_refused(self):
        self.add("35")
        client = TenantClient(enforce_csrf_checks=True)
        page = client.get(reverse(PAGE), {"depuis": self.take.pk}).content.decode()
        refused = client.post(reverse(CLEAR), {"depuis": self.take.pk})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(len(entries_of(self.take)), 1)
        found = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form_of(page, CLEAR))
        self.assertIsNotNone(found)
        cleared = client.post(
            reverse(CLEAR), {"depuis": self.take.pk, "csrfmiddlewaretoken": found.group(1) if found else ""}
        )
        self.assertEqual(cleared.status_code, 302)
        self.assertEqual(entries_of(self.take), [])

    def test_a_get_to_an_action_changes_nothing(self):
        self.add("35")
        for name in (ADD, UNDO, CLEAR):
            with self.subTest(action=name):
                response = self.client.get(reverse(name), {"depuis": self.take.pk, "montant": "70"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse(PAGE))
                self.assertEqual(amounts_of(self.take), [Decimal("35.00")])

    def test_undo_and_clear_for_a_take_that_cannot_be_found(self):
        self.add("35")
        for name in (UNDO, CLEAR):
            for asked in ("abc", "999999", ""):
                with self.subTest(action=name, depuis=asked):
                    response = self.post(name, depuis=asked)
                    self.assertEqual(self.landing(response), reverse(PAGE))
                    self.assertEqual(notices_of(response.content.decode(), "success"), [])
                    self.assertEqual(amounts_of(self.take), [Decimal("35.00")])

    def test_deleting_the_take_deletes_its_list(self):
        self.add("35")
        self.add("70")
        self.add("35", take=self.older)
        pk = self.take.pk
        StockTake.objects.get(pk=pk).delete()
        self.assertFalse(GapFillEntry.objects.filter(stock_take_id=pk).exists())
        self.assertEqual(amounts_of(self.older), [Decimal("35.00")])


class LastShownTests(PageTestCase):
    """« Ajouter », « Annuler la dernière saisie » and « Effacer la liste »
    carry the last entry the page showed (« derniere », empty for an empty
    list): a second click, or a tab left open, posts a list that has moved
    since, and is refused rather than acting twice - checked again inside the
    transaction that writes, since planning takes a second and another click
    may commit meanwhile. A post without the field - a page drawn before it
    existed - is not checked.

    One pint at 35,00 €, 250 L of blonde to fill since the latest count (20
    days ago); an older count (40 days ago) has a list of its own."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.older = make_stock_take(taken_at=noon(today - timedelta(days=40)))
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.blonde = article("Blonde exemple")
        counted(cls.older, cls.blonde, "50")
        counted(cls.take, cls.blonde, "100")
        bought(cls.blonde, "200", today - timedelta(days=10))
        cls.pint = recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=today - timedelta(days=1), quantity=40)

    def shown(self, take=None) -> str:
        """The last entry the page of `take` (the class's own by default)
        shows, as its « Ajouter » form carries it."""
        found = last_shown_in(form_of(self.html(depuis=str((take or self.take).pk)), ADD))
        self.assertIsNotNone(found)
        return found or ""

    def add_from(self, shown: str, amount: str = "35", take=None):
        """« Ajouter » sent from a page that showed `shown` as the last entry."""
        return self.post(ADD, depuis=(take or self.take).pk, montant=amount, derniere=shown)

    def undo_from(self, shown: str):
        """« Annuler la dernière saisie » sent from a page that showed `shown`."""
        return self.post(UNDO, depuis=self.take.pk, derniere=shown)

    def clear_from(self, shown: str):
        """« Effacer la liste » sent from a page that showed `shown`."""
        return self.post(CLEAR, depuis=self.take.pk, derniere=shown)

    def another_click(self, amount: str = "70.00") -> GapFillEntry:
        """An entry another click (another tab) committed for the class's
        count: `amount` in pints at 35,00 €."""
        count = int(Decimal(amount) / Decimal("35.00"))
        return GapFillEntry.objects.create(
            stock_take=self.take,
            amount=Decimal(amount),
            total=Decimal(amount),
            lines=[
                {
                    "recipe": self.pint.pk,
                    "name": "Pinte exemple",
                    "till": "Pinte exemple",
                    "till_price": None,
                    "count": count,
                    "price": "35.00",
                    "fills": ["Blonde exemple"],
                }
            ],
        )

    # -- what the forms carry ----------------------------------------------

    def test_an_empty_list_shows_no_last_entry(self):
        html = self.html(depuis=str(self.take.pk))
        self.assertEqual(last_shown_in(form_of(html, ADD)), "")
        # Nothing to take back: no undo nor clear form to carry it.
        self.assertEqual(form_of(html, UNDO), "")
        self.assertEqual(form_of(html, CLEAR), "")

    def test_the_three_forms_carry_the_last_entry_shown(self):
        self.add("35")
        html = self.add("70")
        first, second = entries_of(self.take)
        # The clear form too, now: a clear from a tab left open on a shorter
        # list would wipe amounts it never showed.
        for name in (ADD, UNDO, CLEAR):
            with self.subTest(form=name):
                self.assertEqual(last_shown_in(form_of(html, name)), str(second.pk))
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        for name in (ADD, UNDO, CLEAR):
            with self.subTest(form=name, after="undo"):
                self.assertEqual(last_shown_in(form_of(html, name)), str(first.pk))
        html = self.post(CLEAR, depuis=self.take.pk).content.decode()
        self.assertEqual(last_shown_in(form_of(html, ADD)), "")
        self.assertEqual(form_of(html, UNDO), "")
        self.assertEqual(form_of(html, CLEAR), "")

    def test_the_add_and_undo_buttons_go_busy_once_pressed(self):
        # Planning takes a second: the button says so, and a second press
        # has nothing to press.
        self.assertEqual(busy_button_of(form_of(self.html(depuis=str(self.take.pk)), ADD)), ADD_BUSY)
        html = self.add("35")
        self.assertEqual(busy_button_of(form_of(html, ADD)), ADD_BUSY)
        self.assertEqual(busy_button_of(form_of(html, UNDO)), UNDO_BUSY)

    def test_the_last_entry_is_the_last_typed_as_the_page_orders_it(self):
        """The page and the check agree on which entry is the last: by when
        it was typed, then by pk - not by pk alone."""
        self.add("35")
        self.add("70")
        first, second = entries_of(self.take)
        GapFillEntry.objects.filter(pk=first.pk).update(created_at=second.created_at + timedelta(minutes=5))
        html = self.html(depuis=str(self.take.pk))
        self.assertEqual(heading_of(html), "À encaisser : 35.00 €")
        self.assertEqual(last_shown_in(form_of(html, ADD)), str(first.pk))
        self.assertEqual(last_shown_in(form_of(html, UNDO)), str(first.pk))
        # Typed in the same instant: the higher pk is the last.
        GapFillEntry.objects.filter(pk=first.pk).update(created_at=second.created_at)
        html = self.html(depuis=str(self.take.pk))
        self.assertEqual(heading_of(html), "À encaisser : 70.00 €")
        self.assertEqual(last_shown_in(form_of(html, UNDO)), str(second.pk))
        html = self.undo_from(str(first.pk)).content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        html = self.undo_from(str(second.pk)).content.decode()
        self.assertEqual(notices_of(html, "success"), [UNDONE])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00")])

    # -- a second click ----------------------------------------------------

    def test_a_double_click_on_add_keeps_one_entry(self):
        shown = self.shown()
        self.assertEqual(shown, "")
        response = self.add_from(shown)
        self.assertEqual(self.landing(response), f"{self.page_of(self.take)}#a-encaisser")
        self.assertEqual(notices_of(response.content.decode(), "warning"), [])
        # The same page's second click.
        response = self.add_from(shown)
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_of(self.take))
        self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
        self.assertEqual(notices_of(html, "error"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00")])
        self.assertEqual(heading_of(html), "À encaisser : 35.00 €")
        self.assertEqual(value_of(stat_of(html, SALES)), "1")
        # On a list already holding an amount, the same.
        (first,) = entries_of(self.take)
        shown = self.shown()
        self.assertEqual(shown, str(first.pk))
        self.assertEqual(notices_of(self.add_from(shown, "70").content.decode(), "warning"), [])
        html = self.add_from(shown, "70").content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00")])
        self.assertEqual(value_of(stat_of(html, SALES)), "3")

    def test_a_double_click_on_undo_removes_one_entry(self):
        self.add("35")
        self.add("70")
        shown = self.shown()
        self.assertEqual(notices_of(self.undo_from(shown).content.decode(), "success"), [UNDONE])
        response = self.undo_from(shown)
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_of(self.take))
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        self.assertEqual(notices_of(html, "success"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00")])
        self.assertEqual(heading_of(html), "À encaisser : 35.00 €")
        # The last amount the same way: one click empties the list, the
        # second finds it moved.
        shown = self.shown()
        self.assertEqual(notices_of(self.undo_from(shown).content.decode(), "success"), [UNDONE])
        html = self.undo_from(shown).content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        self.assertEqual(notices_of(html, "success"), [])
        self.assertEqual(entries_of(self.take), [])

    def test_a_post_without_the_field_is_not_checked(self):
        # A page drawn before the field existed acts as it always did.
        self.add("35")
        html = self.add("35")
        self.assertEqual(notices_of(html, "warning"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("35.00")])
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        self.assertEqual(notices_of(html, "success"), [UNDONE])
        self.assertEqual(notices_of(html, "warning"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00")])

    # -- a tab left open ---------------------------------------------------

    def test_a_tab_left_open_is_refused(self):
        self.add("35")
        left_open = self.shown()
        # Another tab adds an amount: the list moves on.
        self.assertEqual(notices_of(self.add_from(self.shown(), "70").content.decode(), "warning"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00")])
        html = self.add_from(left_open, "105").content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
        # Its undo would take back the 70 € it never showed: refused too.
        html = self.undo_from(left_open).content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00")])
        # The page it lands on is the list as it is: its forms carry the 70 €.
        _first, second = entries_of(self.take)
        self.assertEqual(last_shown_in(form_of(html, ADD)), str(second.pk))
        self.assertEqual(last_shown_in(form_of(html, UNDO)), str(second.pk))
        self.assertEqual(notices_of(self.add_from(str(second.pk), "105").content.decode(), "warning"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00"), Decimal("105.00")])

    def test_a_tab_drawn_before_the_first_amount_is_refused_once_there_is_one(self):
        left_open = self.shown()
        self.add("35")
        html = self.add_from(left_open, "70").content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
        html = self.undo_from(left_open).content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00")])

    def test_a_tab_drawn_before_a_clear_is_refused(self):
        self.add("35")
        left_open = self.shown()
        self.post(CLEAR, depuis=self.take.pk)
        html = self.add_from(left_open, "70").content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
        self.assertEqual(entries_of(self.take), [])
        html = self.undo_from(left_open).content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        # Drawn again, the page shows the empty list, and its amount is kept.
        self.assertEqual(self.shown(), "")
        self.add_from("", "70")
        self.assertEqual(amounts_of(self.take), [Decimal("70.00")])

    def test_a_list_back_to_what_the_tab_showed_has_not_moved(self):
        # Another tab added an amount and took it back: the list is the one
        # the tab showed again, and an amount from it is planned on that.
        self.add("35")
        left_open = self.shown()
        self.add("70")
        self.post(UNDO, depuis=self.take.pk)
        html = self.add_from(left_open, "70").content.decode()
        self.assertEqual(notices_of(html, "warning"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00")])

    def test_another_count_s_list_does_not_move_this_one(self):
        self.add("35")
        shown = self.shown()
        self.add_from(self.shown(self.older), "70", take=self.older)
        self.assertEqual(amounts_of(self.older), [Decimal("70.00")])
        html = self.add_from(shown, "70").content.decode()
        self.assertEqual(notices_of(html, "warning"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00")])
        # An entry of the other count's list is no last entry of this one.
        (other,) = entries_of(self.older)
        html = self.undo_from(str(other.pk)).content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00")])
        self.assertEqual(amounts_of(self.older), [Decimal("70.00")])

    def test_a_value_naming_no_last_entry_is_refused(self):
        """Whatever arrives in the field: refused, never a 500 - on an
        empty list, where only "" is right, as on a list of one."""
        for has_entry in (False, True):
            GapFillEntry.objects.all().delete()
            if has_entry:
                self.add("35")
            kept = amounts_of(self.take)
            last = str(entries_of(self.take)[-1].pk) if has_entry else "0"
            for shown in ("abc", "-1", "1.5", "²", "9" * 30, str(int(last) + 1000)):
                with self.subTest(has_entry=has_entry, derniere=shown):
                    html = self.add_from(shown, "70").content.decode()
                    self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
                    html = self.undo_from(shown).content.decode()
                    self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
                    html = self.clear_from(shown).content.decode()
                    self.assertEqual(notices_of(html, "warning"), [MOVED_CLEAR])
                    self.assertEqual(amounts_of(self.take), kept)

    # -- a clear from a tab left open ----------------------------------------

    def test_a_clear_from_a_tab_drawn_on_one_entry_is_refused_once_two_more_came_in(self):
        self.add("35")
        left_open = last_shown_in(form_of(self.html(depuis=str(self.take.pk)), CLEAR))
        (first,) = entries_of(self.take)
        self.assertEqual(left_open, str(first.pk))
        self.add("70")
        self.add("105")
        response = self.clear_from(left_open or "")
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_of(self.take))
        self.assertEqual(notices_of(html, "warning"), [MOVED_CLEAR])
        self.assertEqual(notices_of(html, "success"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00"), Decimal("105.00")])
        self.assertEqual(heading_of(html), "À encaisser : 105.00 €")
        # From the page it lands on - the list as it is - everything goes.
        shown = last_shown_in(form_of(html, CLEAR))
        self.assertEqual(shown, str(entries_of(self.take)[-1].pk))
        html = self.clear_from(shown or "").content.decode()
        self.assertEqual(notices_of(html, "success"), [CLEARED])
        self.assertEqual(notices_of(html, "warning"), [])
        self.assertEqual(entries_of(self.take), [])
        # A second click on the same page finds the list moved, and says so.
        html = self.clear_from(shown or "").content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_CLEAR])
        self.assertEqual(notices_of(html, "success"), [])

    def test_a_clear_leaves_another_count_s_list_alone_whatever_it_carries(self):
        self.add("35")
        self.add_from(self.shown(self.older), "70", take=self.older)
        shown = self.shown()
        html = self.clear_from(shown).content.decode()
        self.assertEqual(notices_of(html, "success"), [CLEARED])
        self.assertEqual(entries_of(self.take), [])
        self.assertEqual(amounts_of(self.older), [Decimal("70.00")])

    # -- two clicks a second of planning apart -------------------------------

    def planning_meanwhile(self):
        """A gaps_since that, the first time it runs, lets another click's
        70,00 € in - committed while this one plans - then reports as the
        real one does; and the counts it was asked to plan for."""
        planned: list[StockTake] = []

        def planning(take, *args, **kwargs):
            if not planned:
                self.another_click("70.00")
            planned.append(take)
            return gaps_since(take, *args, **kwargs)

        return planning, planned

    def add_while_another_commits(self, shown: str, amount: str = "35"):
        """« Ajouter » sent from a page that showed `shown`, another click's
        entry committed while it plans: the page it lands on, and the counts
        it planned for."""
        planning, planned = self.planning_meanwhile()
        with patch("inventory.views.gaps_since", new=planning):
            response = self.client.post(reverse(ADD), {"depuis": self.take.pk, "montant": amount, "derniere": shown})
        self.assertEqual(response.status_code, 302)
        # No « #a-encaisser »: nothing was added.
        self.assertEqual(response["Location"], self.page_of(self.take))
        return self.client.get(response["Location"]).content.decode(), planned

    def test_an_add_planned_while_another_click_commits_keeps_nothing(self):
        # Both clicks of a double click passed the check made before
        # planning; the one planning second wrote its entry over the first's
        # all the same. The check is made again inside the transaction that
        # writes.
        shown = self.shown()
        self.assertEqual(shown, "")
        html, planned = self.add_while_another_commits(shown)
        # It did plan: the check before planning let it through.
        self.assertEqual(planned, [self.take])
        self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
        self.assertEqual(notices_of(html, "error"), [])
        # The other click's entry alone, and the page shows it.
        (other,) = entries_of(self.take)
        self.assertEqual(other.amount, Decimal("70.00"))
        self.assertEqual(heading_of(html), "À encaisser : 70.00 €")
        self.assertEqual(last_shown_in(form_of(html, ADD)), str(other.pk))
        # From that page, the amount is planned on top of it and kept.
        html = self.add_from(str(other.pk)).content.decode()
        self.assertEqual(notices_of(html, "warning"), [])
        self.assertEqual(amounts_of(self.take), [Decimal("70.00"), Decimal("35.00")])

    def test_on_a_list_already_holding_an_amount_the_same(self):
        self.add("35")
        (first,) = entries_of(self.take)
        shown = self.shown()
        self.assertEqual(shown, str(first.pk))
        html, planned = self.add_while_another_commits(shown, "105")
        self.assertEqual(planned, [self.take])
        self.assertEqual(notices_of(html, "warning"), [MOVED_ADD])
        self.assertEqual(amounts_of(self.take), [Decimal("35.00"), Decimal("70.00")])
        self.assertEqual(value_of(stat_of(html, SALES)), "3")

    def test_an_undo_after_another_entry_came_in_removes_nothing(self):
        self.add("35")
        shown = self.shown()
        (first,) = entries_of(self.take)
        # Committed by another click between the page and this post.
        other = self.another_click()
        html = self.undo_from(shown).content.decode()
        self.assertEqual(notices_of(html, "warning"), [MOVED_UNDO])
        self.assertEqual(notices_of(html, "success"), [])
        self.assertEqual(entries_of(self.take), [first, other])
        # From the page as it is now, the other click's entry is the last,
        # and it alone goes.
        self.assertEqual(last_shown_in(form_of(html, UNDO)), str(other.pk))
        html = self.undo_from(str(other.pk)).content.decode()
        self.assertEqual(notices_of(html, "success"), [UNDONE])
        self.assertEqual(entries_of(self.take), [first])


def stored_line(recipe_pk: int, index: int, till_price) -> dict:
    """A line as an entry keeps it, at 33,70 €, its till button's price
    `till_price` (whatever the JSON holds)."""
    return {
        "recipe": recipe_pk,
        "name": f"Pinte exemple {index}",
        "till": f"PINTE CAISSE EXEMPLE {index}",
        "till_price": till_price,
        "count": 1,
        "price": "33.70",
        "fills": ["Blonde exemple"],
    }


class StoredTillPriceNoteTests(PageTestCase):
    """What the note under « À encaisser » says of the till is read from the
    last entry's lines as stored - each till price against its own price -
    never from the till as it is now. Entries written straight to the
    database: one pint at 33,70 €, sold yesterday."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "100")
        cls.pint = recipe("Pinte exemple", "33.70", cls.blonde, "0.5")
        cls.sale_day = today - timedelta(days=1)
        RecipeSale.objects.create(recipe=cls.pint, sold_on=cls.sale_day, quantity=40)

    def stored(self, *till_prices) -> GapFillEntry:
        """An entry of one line a till price, each line one pint at 33,70 €."""
        lines = [stored_line(self.pint.pk, index, price) for index, price in enumerate(till_prices, start=1)]
        total = Decimal("33.70") * len(lines)
        return GapFillEntry.objects.create(
            stock_take=self.take, amount=total, total=total, lines=lines, sales_up_to=self.sale_day
        )

    def page(self) -> str:
        return self.html(depuis=str(self.take.pk))

    def till_cell(self, html: str, index: int) -> str:
        return cells_of(row_of(table_of(html, PLAN_TABLE), f"Pinte exemple {index}"))[1]

    def test_one_line_at_another_price(self):
        entry = self.stored("33.10", None)
        html = self.page()
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT} · {ONE_TILL_PRICE}")
        self.assertEqual(self.till_cell(html, 1), "PINTE CAISSE EXEMPLE 1 en caisse 33.10 €")
        self.assertEqual(self.till_cell(html, 2), "PINTE CAISSE EXEMPLE 2")

    def test_two_lines_at_another_price(self):
        entry = self.stored("33.10", "33.70", "34.20")
        html = self.page()
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT} · {TWO_TILL_PRICES}")
        self.assertEqual(
            [self.till_cell(html, index) for index in (1, 2, 3)],
            [
                "PINTE CAISSE EXEMPLE 1 en caisse 33.10 €",
                "PINTE CAISSE EXEMPLE 2",
                "PINTE CAISSE EXEMPLE 3 en caisse 34.20 €",
            ],
        )

    def test_three_lines_at_another_price(self):
        entry = self.stored("33.10", "34.20", "32.90")
        self.assertEqual(
            entry_note_of(self.page()), f"{typed_at(entry)} · {ENTRY_EXACT} · 3 lignes ont un autre prix en caisse"
        )

    def test_no_word_of_the_till_when_every_line_rings_its_price(self):
        for why, till_prices in {
            "no till price": (None, None),
            "the same price": ("33.70",),
            "the same price, written otherwise": ("33.7", "33.700"),
            "a till price that is no number": ("abc", "", "NaN", "Infinity", True),
        }.items():
            with self.subTest(why):
                GapFillEntry.objects.all().delete()
                entry = self.stored(*till_prices)
                html = self.page()
                self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT}")
                self.assertNotIn("en caisse", table_of(html, PLAN_TABLE))

    def test_only_the_last_entry_s_lines_are_counted(self):
        self.stored("33.10", "34.20")
        last = self.stored(None)
        self.assertEqual(entry_note_of(self.page()), f"{typed_at(last)} · {ENTRY_EXACT}")
        GapFillEntry.objects.filter(pk=last.pk).delete()
        (first,) = entries_of(self.take)
        self.assertEqual(entry_note_of(self.page()), f"{typed_at(first)} · {ENTRY_EXACT} · {TWO_TILL_PRICES}")

    def assert_left_out(self, price: str):
        """An entry whose first line is priced `price` and whose second
        line's till price is `price`: the page draws the second line alone,
        at no till price, and counts it alone."""
        lines = [
            {**stored_line(self.pint.pk, 1, None), "price": price},
            stored_line(self.pint.pk, 2, price),
        ]
        entry = GapFillEntry.objects.create(
            stock_take=self.take,
            amount=Decimal("33.70"),
            total=Decimal("33.70"),
            lines=lines,
            sales_up_to=self.sale_day,
        )
        html = self.page()
        self.assertEqual(row_of(table_of(html, PLAN_TABLE), "Pinte exemple 1"), "")
        self.assertEqual(len(body_rows(table_of(html, PLAN_TABLE))), 1)
        self.assertEqual(self.till_cell(html, 2), "PINTE CAISSE EXEMPLE 2")
        self.assertEqual(entry_note_of(html), f"{typed_at(entry)} · {ENTRY_EXACT}")
        self.assertEqual(value_of(stat_of(html, SALES)), "1")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "33.70 €")
        # The gaps show the one line kept: one pint, 0,5 L.
        row = cells_of(row_of(table_of(html, GAPS_TABLE), "Blonde exemple"))
        self.assertEqual((row[-3], row[-1]), ("0.50", EXCLUDE_BUTTON))

    def test_a_price_of_ten_billion_or_more_is_left_out_without_breaking_the_page(self):
        # « 12345678901 € » printed as a price no column holds. Bounded like a
        # price column now (10^10): the line is left out, a till price is no
        # till price.
        self.assert_left_out("12345678901")

    def test_a_price_past_the_decimal_exponent_is_left_out_without_breaking_the_page(self):
        # « 1E+999999999 » read as a Decimal, and the first sum of it past the
        # context's exponent raised Overflow: the page was a 500 until the
        # list was cleared.
        # The first bound, `abs(number) < 10 ** 10`, was context arithmetic
        # and raised decimal.Overflow on that very value: copy_abs does not
        # (see test_gap_fill_entry, EntryRowsGarbageTests).
        self.assert_left_out("1E+999999999")

    def test_the_till_as_it_is_now_changes_nothing(self):
        # A button linked since, ringing another price every day: the
        # stored lines are what the note reads.
        entry = self.stored("33.70")
        button = PosProduct.objects.create(name="PINTE NOUVELLE EXEMPLE", recipe=self.pint, total_quantity=50)
        rung_up(button, self.sale_day, 50, "1605.00")  # 32,10 € a pint
        self.assertEqual(entry_note_of(self.page()), f"{typed_at(entry)} · {ENTRY_EXACT}")


class StaleListTests(PageTestCase):
    """When the list goes stale: till sales imported since an entry was
    made, on the day before its day or later - the till files a sale rung
    after midnight under the day its service began. A pint at 35,00 €, its
    sales imported up to three days ago when the list is made, today."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(cls.today - timedelta(days=20)))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "100")
        cls.pint = recipe("Pinte exemple", "35.00", cls.blonde, "0.5")
        RecipeSale.objects.create(recipe=cls.pint, sold_on=cls.today - timedelta(days=3), quantity=40, source="csv")

    def imported(self, days_ago: int, quantity: int):
        record_sales([("Pinte exemple", self.today - timedelta(days=days_ago), quantity)], source="csv")

    def stale(self) -> bool:
        return STALE in messages_of(self.html(depuis=str(self.take.pk)), "warning")

    def test_the_entry_watches_from_yesterday(self):
        self.add("70")
        (entry,) = entries_of(self.take)
        self.assertEqual(entry.sales_up_to, self.today - timedelta(days=3))
        self.assertEqual(entry.sales_from, self.today - timedelta(days=1))
        self.assertEqual(entry.sales_seen, Decimal("0"))
        self.assertFalse(self.stale())

    def test_a_late_import_of_two_days_before_the_entry_s_is_not_stale(self):
        self.add("70")
        # Two days ago imported after the list was made: newer than the last
        # day of sales known then, older than the day before the list's -
        # none of its sales can be in it.
        self.imported(2, 5)
        self.assertFalse(self.stale())
        # The last day known again, with more sales: the same.
        self.imported(3, 44)
        self.assertFalse(self.stale())
        # Today's sales imported: the list rung up today is in them.
        self.imported(0, 2)
        self.assertTrue(self.stale())

    def test_a_late_import_of_the_day_before_the_entry_s_is_stale(self):
        # Reversed on purpose (it used to be « older than the list, not
        # stale »): a list made and rung up after midnight is in the sales of
        # the day the service began, which the till files under yesterday.
        # Read by the calendar, those sales were never flagged; a list made
        # in the evening now reads yesterday's late import as stale too - a
        # false alarm, never a double count gone silent.
        self.add("70")
        self.imported(1, 6)
        self.assertTrue(self.stale())

    def test_the_entry_s_day_imported_again(self):
        self.imported(0, 4)
        self.add("70")
        (entry,) = entries_of(self.take)
        self.assertEqual((entry.sales_up_to, entry.sales_seen), (self.today, Decimal("4")))
        # The same import run again: nothing moved.
        self.imported(0, 4)
        self.assertFalse(self.stale())
        # With the list's two pints rung up since: moved, though the last
        # day of sales has not.
        self.imported(0, 6)
        self.assertTrue(self.stale())

    def test_a_sale_typed_by_hand_on_the_entry_s_day_counts_too(self):
        self.add("70")
        RecipeSale.objects.create(recipe=self.pint, sold_on=self.today, quantity=2, source="manual")
        self.assertTrue(self.stale())

    def test_an_entry_kept_without_what_it_saw_falls_back_on_the_last_sale_day(self):
        # An entry from before `sales_seen` - or `sales_from` - was kept is
        # judged on the last day of sales alone: a newer day, even older than
        # the day before the entry's, reads as newer sales.
        for missing in ("sales_seen", "sales_from"):
            with self.subTest(missing=missing):
                GapFillEntry.objects.all().delete()
                RecipeSale.objects.exclude(sold_on=self.today - timedelta(days=3)).delete()
                self.imported(3, 40)
                self.add("70")
                GapFillEntry.objects.filter(stock_take=self.take).update(**{missing: None})
                self.assertFalse(self.stale())
                self.imported(3, 44)
                self.assertFalse(self.stale())
                self.imported(2, 6)
                self.assertTrue(self.stale())


# -- articles and categories left out (the owner, 01/10/2026) ----------------

#: What each exclusion route says, by outcome.
ARTICLE_EXCLUDED = "« {} » ne compte plus dans les écarts."
ARTICLE_BACK = "« {} » compte de nouveau dans les écarts."
CATEGORY_EXCLUDED = "Catégorie « {} » exclue des écarts."
BLANK_EXCLUDED = "Catégorie non renseignée exclue des écarts."
CATEGORY_BACK = "La catégorie « {} » compte de nouveau dans les écarts."
BLANK_BACK = "La catégorie non renseignée compte de nouveau dans les écarts."
ARTICLE_NOT_FOUND = "Article introuvable : rien n'a été exclu."
CATEGORY_NOT_FOUND = "Catégorie introuvable : rien n'a été exclu."
EXCLUSION_GONE = "Cette exclusion n'existe plus : rien n'a changé."
#: « Réinclure » on an article whose category is left out too: its own
#: exclusion goes, and it stays out.
STILL_OUT = "« {} » reste exclu : sa catégorie « {} » l'est aussi."
STILL_OUT_BLANK = "« {} » reste exclu : sa catégorie non renseignée l'est aussi."
#: What the fold's line of such an article adds after its category.
CATEGORY_OUT_TOO = " · catégorie exclue aussi"
#: The fold's sentence while nothing is left out.
NOTHING_EXCLUDED = "Rien d'exclu : tous les articles comptent."
#: The fold's one sentence of help, above the category form.
EXCLUSION_HELP = "Une catégorie exclue vaut aussi pour les articles classés plus tard."
#: What an empty gaps table says: every gap it would hold left out, or none
#: at all to hold.
ALL_LEFT_OUT = "Tous les articles de ces écarts sont exclus."
NO_RECIPE_USES_ONE = "Aucune recette proposée n'utilise un article compté ou acheté."
#: « Comment c'est calculé », on the promise the planner keeps.
NEVER_PAST_A_GAP = (
    "Chaque vente proposée est comptée comme la page Produits la comptera : "
    "aucune ne fait dépasser un écart, hors articles exclus."
)
#: How the fold names the articles with no category.
BLANK_CATEGORY = "Catégorie non renseignée"
#: What every line of the fold ends on, as it reads.
INCLUDE_BUTTON = "Réinclure"


def said_in(fragment: str) -> list[tuple[str, str]]:
    """(level, words) of every message (an <li class="message ...">) in
    `fragment`, in the order drawn - unescaped, as a reader reads it."""
    return [
        (level, unescape(text_of(words)))
        for level, words in re.findall(r'<li class="message message-([a-z]+)">(.*?)</li>', fragment, flags=re.DOTALL)
    ]


def said_at_the_top(html: str) -> list[tuple[str, str]]:
    """The messages drawn above the page's header - the page's own block,
    where base.html says every other page's."""
    start = html.find("<main")
    end = html.find('<div class="page-header">')
    return said_in(html[start:end]) if 0 <= start < end else []


def said_under_the_gaps(html: str) -> list[tuple[str, str]]:
    """The messages drawn right under the « Écarts » heading, nothing between
    the two - where an article's « Exclure » lands (#ecarts)."""
    found = re.search(r'<h2 id="ecarts">Écarts</h2>\s*(<ul class="messages">.*?</ul>)', html, flags=re.DOTALL)
    return said_in(found.group(1)) if found else []


def said_in_the_fold(html: str) -> list[tuple[str, str]]:
    """The messages drawn first thing in « Exclus des écarts », under its
    summary - where a category and every « Réinclure » land (#exclusions)."""
    found = re.search(
        r'<details class="explainer" id="exclusions"[^>]*>\s*<summary>[^<]*</summary>\s*(<ul class="messages">.*?</ul>)',
        html,
        flags=re.DOTALL,
    )
    return said_in(found.group(1)) if found else []


def gap_sentences(html: str) -> list[str]:
    """What the page says in sentences under « Écarts », up to the fold of
    what is left out - where an empty table says why it is empty."""
    start = html.find('<h2 id="ecarts">')
    end = html.find('<details class="explainer" id="exclusions"')
    return [unescape(sentence) for sentence in sentences_of(html[start:end])] if 0 <= start < end else []


def worked_out_of(html: str) -> list[str]:
    """« Comment c'est calculé », each bullet as it reads."""
    found = re.search(
        r"<details class=\"explainer\">\s*<summary>Comment c'est calculé</summary>(.*?)</details>",
        html,
        flags=re.DOTALL,
    )
    if not found:
        return []
    return [unescape(text_of(item)) for item in re.findall(r"<li>(.*?)</li>", found.group(1), flags=re.DOTALL)]


def exclusions_of(html: str) -> str:
    """The « Exclus des écarts » fold (#exclusions), or ""."""
    found = re.search(r'<details class="explainer" id="exclusions"[^>]*>.*?</details>', html, flags=re.DOTALL)
    return found.group(0) if found else ""


def exclusions_open(html: str) -> bool | None:
    """Whether the fold is drawn open; None when there is no fold."""
    found = re.search(r'<details class="explainer" id="exclusions"( open)?>', html)
    return bool(found.group(1)) if found else None


def exclusions_summary_of(html: str) -> str:
    found = re.search(r"<summary>(.*?)</summary>", exclusions_of(html), flags=re.DOTALL)
    return text_of(found.group(1)) if found else ""


def excluded_lines(html: str) -> list[str]:
    """Each line of the fold's list as it reads, its « Réinclure » last -
    unescaped, as a reader reads it."""
    found = re.search(r'<ul class="exclusion-list">(.*?)</ul>', exclusions_of(html), flags=re.DOTALL)
    if not found:
        return []
    return [unescape(text_of(item)) for item in re.findall(r"<li>(.*?)</li>", found.group(1), flags=re.DOTALL)]


def include_form_of(html: str, words: str) -> str:
    """The « Réinclure » form on the fold's line reading `words`, or ""."""
    for item in re.findall(r"<li>(.*?)</li>", exclusions_of(html), flags=re.DOTALL):
        if words in unescape(text_of(item)):
            return form_of(item, INCLUDE)
    return ""


def category_form_of(html: str) -> str:
    """The fold's « Exclure la catégorie » form, or ""."""
    return form_of(exclusions_of(html), EXCLUDE)


def category_choices(html: str) -> list[tuple[str, str]]:
    """(value posted, what it reads) of every category the fold offers,
    both unescaped as the browser reads them."""
    return [
        (unescape(value), unescape(text_of(words)))
        for value, words in re.findall(
            r'<option value="([^"]*)">(.*?)</option>', category_form_of(html), flags=re.DOTALL
        )
    ]


def exclude_form_of(html: str, name: str) -> str:
    """The « Exclure » form on the gaps row of `name`, or ""."""
    return form_of(row_of(table_of(html, GAPS_TABLE), name), EXCLUDE)


def hidden_fields(form: str) -> dict[str, str]:
    """What a form posts of itself: its hidden fields, as the browser sends
    them."""
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
    }


def gap_names(html: str) -> list[str]:
    """The gaps table's rows by their first cell, as they read."""
    return [cells_of(row)[0] for row in body_rows(table_of(html, GAPS_TABLE))]


def filed(name: str, category: str):
    """An article in litres, filed under `category` ("" for none)."""
    return make_stock_type(name=name, unit=UnitChoices.LITRE, category=category)


def fixed_recipe(name: str, price, *pours):
    """A recipe whose every ingredient is fixed: one group each,
    `pours` being (article, amount) pairs."""
    made = make_recipe(name=name, selling_price_ttc=price)
    for group, (stock_type, amount) in enumerate(pours):
        make_ingredient(made, stock_type=stock_type, quantity=amount, group=group)
    return made


class ExclusionTestCase(PageTestCase):
    """The page's exclusion forms as a browser posts them: every action
    answered with one redirect, then the page it lands on."""

    def exclude(self, take=None, **data):
        """« Exclure » (an article) or « Exclure la catégorie » posted from
        the page of `take` (the class's own by default)."""
        return self.post(EXCLUDE, depuis=(take or self.take).pk, **data)

    def include(self, exclusion, take=None):
        """« Réinclure » posted for `exclusion` (a GapExclusion, or what the
        form's field holds) from the page of `take`."""
        posted = exclusion.pk if isinstance(exclusion, GapExclusion) else exclusion
        return self.post(INCLUDE, depuis=(take or self.take).pk, exclusion=posted)

    def send(self, form: str):
        """`form` sent as drawn: its hidden fields, CSRF token included."""
        action = re.search(r'action="([^"]*)"', form)
        self.assertIsNotNone(action, form)
        response = self.client.post(action.group(1) if action else "", hidden_fields(form), follow=True)
        self.assertEqual([status for _url, status in response.redirect_chain], [302])
        return response

    def page_at(self, anchor: str, take=None) -> str:
        return f"{self.page_of(take or self.take)}#{anchor}"


class ExcludeAnArticleTests(ExclusionTestCase):
    """« Exclure » on a gap's row: the article leaves the gaps for the whole
    espace - every count's page, every later visit - until « Réinclure ».

    Since the latest count (20 days ago): a blonde (« Bières exemple »)
    with 250 L to fill, worth 1 000,00 € HT; an amber of the same category,
    268 L, 1 072,00 € HT; a cider filed nowhere, 85 L, never bought so of no
    value. Pints at 35,00 €, a bowl at 32,00 €, all sold yesterday. An older
    count (40 days ago) has the blonde too."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        sale_day = cls.today - timedelta(days=1)
        cls.older = make_stock_take(taken_at=noon(cls.today - timedelta(days=40)))
        cls.take = make_stock_take(taken_at=noon(cls.today - timedelta(days=20)))
        cls.blonde = filed("Blonde exemple", "Bières exemple")
        cls.amber = filed("Ambrée exemple", "Bières exemple")
        cls.cider = filed("Cidre exemple", "")
        counted(cls.older, cls.blonde, "50")
        counted(cls.take, cls.blonde, "100")
        counted(cls.take, cls.amber, "120")
        counted(cls.take, cls.cider, "100")
        for keg in (cls.blonde, cls.amber):
            bought(keg, "200", cls.today - timedelta(days=10))
        cls.blonde_pint = recipe("Pinte blonde exemple", "35.00", cls.blonde, "0.5")
        cls.amber_pint = recipe("Pinte ambrée exemple", "35.00", cls.amber, "0.5")
        cls.bowl = recipe("Bolée exemple", "32.00", cls.cider, "0.5")
        for pint in (cls.blonde_pint, cls.amber_pint):
            RecipeSale.objects.create(recipe=pint, sold_on=sale_day, quantity=40)
        RecipeSale.objects.create(recipe=cls.bowl, sold_on=sale_day, quantity=10)

    ALL = ["Ambrée exemple litre", "Blonde exemple litre", "Cidre exemple litre"]

    # -- the forms -----------------------------------------------------------

    def test_every_row_ends_on_its_own_exclude_form(self):
        html = self.html()
        self.assertEqual(gap_names(html), self.ALL)
        for stock_type in (self.amber, self.blonde, self.cider):
            with self.subTest(article=stock_type.name):
                form = exclude_form_of(html, stock_type.name)
                fields = hidden_fields(form)
                self.assertEqual(fields["depuis"], str(self.take.pk))
                self.assertEqual(fields["article"], str(stock_type.pk))
                self.assertTrue(fields["csrfmiddlewaretoken"])
                self.assertEqual(set(fields), {"csrfmiddlewaretoken", "depuis", "article"})
                self.assertIn(">Exclure</button>", form)
                self.assertEqual(cells_of(row_of(table_of(html, GAPS_TABLE), stock_type.name))[-1], EXCLUDE_BUTTON)
        # Nothing left out yet: the fold is shut and says so.
        self.assertIs(exclusions_open(html), False)
        self.assertEqual(exclusions_summary_of(html), "Exclus des écarts")
        self.assertEqual(excluded_lines(html), [])
        self.assertIn(NOTHING_EXCLUDED, unescape(text_of(exclusions_of(html))))

    def test_exclure_sends_its_row_and_lands_on_the_gaps(self):
        """The row's form as a browser sends it - its token checked - lands
        on the gaps with its message, the row gone and named in the fold."""
        client = TenantClient(enforce_csrf_checks=True)
        page = client.get(reverse(PAGE), {"depuis": self.take.pk}).content.decode()
        form = exclude_form_of(page, "Blonde exemple")
        # Without its token: refused, nothing excluded.
        refused = client.post(reverse(EXCLUDE), {"depuis": self.take.pk, "article": self.blonde.pk})
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(GapExclusion.objects.exists())
        response = client.post(reverse(EXCLUDE), hidden_fields(form))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.page_at("ecarts"))
        html = client.get(reverse(PAGE), {"depuis": self.take.pk}).content.decode()
        self.assertEqual(notices_of(html, "success"), [ARTICLE_EXCLUDED.format("Blonde exemple")])
        self.assertEqual(gap_names(html), ["Ambrée exemple litre", "Cidre exemple litre"])
        self.assertEqual(row_of(table_of(html, GAPS_TABLE), "Blonde exemple"), "")
        self.assertEqual(excluded_lines(html), [f"Blonde exemple Bières exemple {INCLUDE_BUTTON}"])
        self.assertEqual(exclusions_summary_of(html), "Exclus des écarts · 1 article")
        self.assertIs(exclusions_open(html), True)
        self.assertEqual(list(GapExclusion.objects.values_list("stock_type", "category")), [(self.blonde.pk, None)])

    def test_the_line_s_form_carries_its_exclusion_and_its_token(self):
        self.exclude(article=self.blonde.pk)
        exclusion = GapExclusion.objects.get()
        client = TenantClient(enforce_csrf_checks=True)
        page = client.get(reverse(PAGE), {"depuis": self.take.pk}).content.decode()
        fields = hidden_fields(include_form_of(page, "Blonde exemple"))
        self.assertEqual(set(fields), {"csrfmiddlewaretoken", "depuis", "exclusion"})
        self.assertEqual((fields["depuis"], fields["exclusion"]), (str(self.take.pk), str(exclusion.pk)))
        # Without its token: refused, the exclusion kept.
        refused = client.post(reverse(INCLUDE), {"depuis": self.take.pk, "exclusion": exclusion.pk})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(list(GapExclusion.objects.all()), [exclusion])
        response = client.post(reverse(INCLUDE), fields)
        self.assertEqual((response.status_code, response["Location"]), (302, self.page_at("exclusions")))
        self.assertFalse(GapExclusion.objects.exists())

    def test_the_category_form_carries_its_token(self):
        client = TenantClient(enforce_csrf_checks=True)
        page = client.get(reverse(PAGE), {"depuis": self.take.pk}).content.decode()
        refused = client.post(reverse(EXCLUDE), {"depuis": self.take.pk, "categorie": "Bières exemple"})
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(GapExclusion.objects.exists())
        fields = {**hidden_fields(category_form_of(page)), "categorie": "Bières exemple"}
        response = client.post(reverse(EXCLUDE), fields)
        self.assertEqual((response.status_code, response["Location"]), (302, self.page_at("exclusions")))
        self.assertEqual(GapExclusion.objects.get().category, "Bières exemple")

    # -- it stays ----------------------------------------------------------

    def test_it_stays_excluded_on_a_fresh_page(self):
        self.exclude(article=self.blonde.pk)
        for params in ({}, {"depuis": str(self.take.pk)}):
            with self.subTest(params=params):
                html = self.html(**params)
                self.assertEqual(gap_names(html), ["Ambrée exemple litre", "Cidre exemple litre"])
                self.assertEqual(excluded_lines(html), [f"Blonde exemple Bières exemple {INCLUDE_BUTTON}"])
                # Said once, where it was done: a fresh page has no message.
                self.assertEqual(notices_of(html, "success"), [])

    def test_it_is_the_espace_s_not_the_count_s(self):
        """Left out from the latest count's page, the blonde is out of the
        older count's too, its line there taking it back from that page;
        and the other way round."""
        older = self.html(depuis=str(self.older.pk))
        self.assertIn("Blonde exemple litre", gap_names(older))
        self.exclude(article=self.blonde.pk)
        older = self.html(depuis=str(self.older.pk))
        self.assertNotIn("Blonde exemple litre", gap_names(older))
        # The amber and the cider were not counted then: their rows say so.
        self.assertEqual(
            gap_names(older), ["Ambrée exemple litre non compté", "Cidre exemple litre non compté vendu plus qu'acheté"]
        )
        self.assertEqual(excluded_lines(older), [f"Blonde exemple Bières exemple {INCLUDE_BUTTON}"])
        self.assertEqual(hidden_fields(include_form_of(older, "Blonde exemple"))["depuis"], str(self.older.pk))
        # Left out from the older count's page: out of the latest's too.
        response = self.exclude(take=self.older, article=self.amber.pk)
        self.assertEqual(self.landing(response), self.page_at("ecarts", self.older))
        self.assertEqual(gap_names(response.content.decode()), ["Cidre exemple litre non compté vendu plus qu'acheté"])
        self.assertEqual(gap_names(self.html()), ["Cidre exemple litre"])

    def test_a_double_click_keeps_one_exclusion(self):
        for _click in range(2):
            html = self.exclude(article=self.blonde.pk).content.decode()
            self.assertEqual(notices_of(html, "success"), [ARTICLE_EXCLUDED.format("Blonde exemple")])
        self.assertEqual(GapExclusion.objects.count(), 1)
        self.assertEqual(excluded_lines(html), [f"Blonde exemple Bières exemple {INCLUDE_BUTTON}"])

    def test_a_count_the_form_no_longer_names_only_changes_where_it_lands(self):
        """The exclusion is the espace's: posted for a count deleted since,
        it is made all the same, and the page is the latest count's."""
        for asked in ("abc", "999999", ""):
            with self.subTest(depuis=asked):
                GapExclusion.objects.all().delete()
                response = self.post(EXCLUDE, depuis=asked, article=self.blonde.pk)
                self.assertEqual(self.landing(response), reverse(PAGE))
                html = response.content.decode()
                self.assertEqual(notices_of(html, "success"), [ARTICLE_EXCLUDED.format("Blonde exemple")])
                self.assertEqual(selected_take(html), str(self.take.pk))
                self.assertNotIn("Blonde exemple litre", gap_names(html))

    def test_an_article_of_no_gap_is_listed_and_goes_with_its_article(self):
        """Any article may be left out - the form names one of the page's
        rows, but the espace's exclusions are listed on every page, this
        report's articles or not. Deleted, it takes its exclusion with it."""
        glass = make_stock_type(name="Verre exemple", unit=UnitChoices.UNIT, category="Vaisselle exemple")
        html = self.exclude(article=glass.pk).content.decode()
        self.assertEqual(notices_of(html, "success"), [ARTICLE_EXCLUDED.format("Verre exemple")])
        self.assertEqual(gap_names(html), self.ALL)
        self.assertEqual(excluded_lines(html), [f"Verre exemple Vaisselle exemple {INCLUDE_BUTTON}"])
        glass.delete()
        self.assertFalse(GapExclusion.objects.exists())
        html = self.html()
        self.assertEqual(excluded_lines(html), [])
        self.assertIs(exclusions_open(html), False)

    # -- « Réinclure » -------------------------------------------------------

    def test_reinclure_brings_it_back(self):
        self.exclude(article=self.blonde.pk)
        response = self.send(include_form_of(self.html(), "Blonde exemple"))
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("exclusions"))
        self.assertEqual(notices_of(html, "success"), [ARTICLE_BACK.format("Blonde exemple")])
        self.assertEqual(gap_names(html), self.ALL)
        self.assertEqual(hidden_fields(exclude_form_of(html, "Blonde exemple"))["article"], str(self.blonde.pk))
        self.assertEqual(excluded_lines(html), [])
        self.assertIn(NOTHING_EXCLUDED, unescape(text_of(exclusions_of(html))))
        self.assertFalse(GapExclusion.objects.exists())
        # Nothing is left out, but the fold is drawn open for its message,
        # said inside it; a fresh page has it shut again.
        self.assertIs(exclusions_open(html), True)
        self.assertEqual(said_in_the_fold(html), [("success", ARTICLE_BACK.format("Blonde exemple"))])
        self.assertIs(exclusions_open(self.html()), False)

    def test_an_exclusion_taken_back_twice(self):
        """« Réinclure » clicked twice, or from a tab left open: the second
        finds nothing to take back, says so, and the other exclusions stay."""
        self.exclude(article=self.blonde.pk)
        self.exclude(article=self.cider.pk)
        form = include_form_of(self.html(), "Blonde exemple")
        self.assertEqual(
            notices_of(self.send(form).content.decode(), "success"), [ARTICLE_BACK.format("Blonde exemple")]
        )
        response = self.send(form)
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("exclusions"))
        self.assertEqual(notices_of(html, "warning"), [EXCLUSION_GONE])
        self.assertEqual(notices_of(html, "success"), [])
        self.assertEqual(excluded_lines(html), [f"Cidre exemple {INCLUDE_BUTTON}"])
        self.assertEqual(gap_names(html), ["Ambrée exemple litre", "Blonde exemple litre"])

    def test_an_exclusion_that_cannot_be_read_changes_nothing(self):
        self.exclude(article=self.blonde.pk)
        kept = list(GapExclusion.objects.values_list("pk", flat=True))
        unknown = str(max(kept) + 1000)
        for asked in ("abc", "²", "-1", "1.5", "", " ", "9" * 30, unknown):
            with self.subTest(exclusion=asked):
                response = self.include(asked)
                html = response.content.decode()
                self.assertEqual(self.landing(response), self.page_at("exclusions"))
                self.assertEqual(notices_of(html, "warning"), [EXCLUSION_GONE])
                self.assertEqual(notices_of(html, "success"), [])
                self.assertEqual(list(GapExclusion.objects.values_list("pk", flat=True)), kept)
                self.assertNotIn("Blonde exemple litre", gap_names(html))
        # Not sent at all: the same.
        html = self.post(INCLUDE, depuis=self.take.pk).content.decode()
        self.assertEqual(notices_of(html, "warning"), [EXCLUSION_GONE])
        self.assertEqual(list(GapExclusion.objects.values_list("pk", flat=True)), kept)

    # -- what is refused -----------------------------------------------------

    def test_an_article_that_cannot_be_found_is_not_excluded(self):
        unknown = str(max(StockType.objects.values_list("pk", flat=True)) + 1000)
        for asked in ("abc", "²", "-1", "1.5", " ", "0", "9" * 30, unknown):
            with self.subTest(article=asked):
                response = self.exclude(article=asked)
                html = response.content.decode()
                self.assertEqual(self.landing(response), self.page_at("ecarts"))
                self.assertEqual(notices_of(html, "error"), [ARTICLE_NOT_FOUND])
                self.assertEqual(notices_of(html, "success"), [])
                self.assertFalse(GapExclusion.objects.exists())
                self.assertEqual(gap_names(html), self.ALL)

    def test_neither_an_article_nor_a_category_is_not_excluded(self):
        for data in ({}, {"article": ""}):
            with self.subTest(data=data):
                response = self.exclude(**data)
                self.assertEqual(self.landing(response), self.page_at("exclusions"))
                self.assertEqual(notices_of(response.content.decode(), "error"), [CATEGORY_NOT_FOUND])
                self.assertFalse(GapExclusion.objects.exists())

    def test_a_get_changes_nothing(self):
        self.exclude(article=self.cider.pk)
        exclusion = GapExclusion.objects.get()
        for name, query in (
            (EXCLUDE, {"depuis": self.take.pk, "article": self.blonde.pk}),
            (EXCLUDE, {"depuis": self.take.pk, "categorie": "Bières exemple"}),
            (INCLUDE, {"depuis": self.take.pk, "exclusion": exclusion.pk}),
        ):
            with self.subTest(action=name, query=query):
                response = self.client.get(reverse(name), query)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse(PAGE))
                self.assertEqual(list(GapExclusion.objects.all()), [exclusion])

    # -- the fold ------------------------------------------------------------

    def test_the_fold_is_open_only_while_something_is_left_out(self):
        self.assertIs(exclusions_open(self.html()), False)
        self.exclude(article=self.blonde.pk)
        self.assertIs(exclusions_open(self.html()), True)
        self.include(GapExclusion.objects.get())
        self.assertIs(exclusions_open(self.html()), False)
        self.exclude(categorie="")
        self.assertIs(exclusions_open(self.html()), True)
        self.include(GapExclusion.objects.get())
        self.assertIs(exclusions_open(self.html()), False)

    def test_the_fold_s_help_is_one_sentence(self):
        """Above the category form, one sentence - the one thing a category
        does that ticking its articles would not."""
        self.assertEqual(sentences_of(exclusions_of(self.html())), [EXCLUSION_HELP, NOTHING_EXCLUDED])
        html = self.exclude(article=self.blonde.pk).content.decode()
        self.assertEqual(sentences_of(exclusions_of(html)), [EXCLUSION_HELP])
        self.assertEqual(sentences_of(exclusions_of(self.html())), [EXCLUSION_HELP])

    def test_how_it_is_worked_out_keeps_every_gap_but_the_ones_left_out(self):
        """« Comment c'est calculé »: no proposed sale goes past a gap - the
        articles left out aside, which have none to keep to. Said whether
        anything is left out or not."""
        ending = "aucune ne fait dépasser un écart, hors articles exclus."
        for html in (self.html(), self.exclude(article=self.blonde.pk).content.decode()):
            bullets = worked_out_of(html)
            self.assertEqual(len(bullets), 7)
            self.assertEqual(bullets[3], NEVER_PAST_A_GAP)
            self.assertEqual([bullet for bullet in bullets if bullet.endswith(ending)], [NEVER_PAST_A_GAP])

    # -- the list ------------------------------------------------------------

    def test_a_list_already_made_keeps_what_it_proposed(self):
        """An entry may have been rung up: leaving an article out rewrites
        none of it. The next amount fills the gaps left - the blonde's pint
        is no longer one of them."""
        html = self.add("70")
        before = self.plan_rows(html)
        self.assertIn("Pinte blonde exemple", [row[0] for row in before])
        (entry,) = entries_of(self.take)
        lines = entry.lines
        html = self.exclude(article=self.blonde.pk).content.decode()
        self.assertEqual(self.plan_rows(html), before)
        self.assertEqual(GapFillEntry.objects.get(pk=entry.pk).lines, lines)
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "70.00 €")
        self.assertEqual(gap_names(html), ["Ambrée exemple litre", "Cidre exemple litre"])
        html = self.add("70")
        self.assertNotIn("Pinte blonde exemple", [row[0] for row in self.plan_rows(html)])
        self.assertEqual(value_of(stat_of(html, ENTERED)), "140.00 €")

    def plan_rows(self, html) -> list[list[str]]:
        return [cells_of(row) for row in body_rows(table_of(html, PLAN_TABLE))]


class ExcludeACategoryTests(ExclusionTestCase):
    """« Exclure la catégorie »: every article filed under it leaves the
    gaps - those filed there later too - and the fold names the category,
    how many of the page's articles it covers, and « Réinclure ».

    Since the latest count: a blonde and an amber (« Bières exemple »), a
    cider filed nowhere, a red (« Vins exemple »), all poured by a recipe
    sold since; a port (« Vins exemple ») whose glass has not sold since,
    1,8 L to fill at 10,00 € HT; a tablecloth (« Matériel exemple ») bought
    and in no recipe."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        sale_day = today - timedelta(days=1)
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.blonde = filed("Blonde exemple", "Bières exemple")
        cls.amber = filed("Ambrée exemple", "Bières exemple")
        cls.cider = filed("Cidre exemple", "")
        cls.red = filed("Rouge exemple", "Vins exemple")
        cls.port = filed("Porto exemple", "Vins exemple")
        for stock_type, quantity in ((cls.blonde, "100"), (cls.amber, "120"), (cls.cider, "100"), (cls.red, "30")):
            counted(cls.take, stock_type, quantity)
        bought(cls.port, "1", today - timedelta(days=30), unit_cost="10.00")
        counted(cls.take, cls.port, "2")
        for made, sold in (
            (recipe("Pinte blonde exemple", "35.00", cls.blonde, "0.5"), 40),
            (recipe("Pinte ambrée exemple", "35.00", cls.amber, "0.5"), 40),
            (recipe("Bolée exemple", "32.00", cls.cider, "0.5"), 10),
            (recipe("Verre de rouge exemple", "33.00", cls.red, "0.15"), 20),
        ):
            RecipeSale.objects.create(recipe=made, sold_on=sale_day, quantity=sold)
        recipe("Verre de porto exemple", "34.00", cls.port, "0.06")  # not sold since
        cls.cloth = make_stock_type(name="Nappe exemple", unit=UnitChoices.UNIT, category="Matériel exemple")
        bought(cls.cloth, "10", today - timedelta(days=10), unit_cost="2.00")

    ALL = ["Ambrée exemple litre", "Blonde exemple litre", "Cidre exemple litre", "Rouge exemple litre"]
    OFFERED = [
        ("Bières exemple", "Bières exemple (2)"),
        ("Matériel exemple", "Matériel exemple (1)"),
        ("Vins exemple", "Vins exemple (2)"),
        ("", f"{BLANK_CATEGORY} (1)"),
    ]

    def test_the_fold_offers_every_category_of_the_page_s_articles(self):
        html = self.html()
        self.assertEqual(sorted(gap_names(html)), self.ALL)
        # The blank one last, under the words the fold names it by.
        self.assertEqual(category_choices(html), self.OFFERED)
        form = category_form_of(html)
        fields = hidden_fields(form)
        self.assertEqual(set(fields), {"csrfmiddlewaretoken", "depuis"})
        self.assertEqual(fields["depuis"], str(self.take.pk))
        self.assertIn('<select name="categorie">', form)
        self.assertIn(">Exclure la catégorie</button>", form)

    def test_excluding_a_category(self):
        response = self.exclude(categorie="Bières exemple")
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("exclusions"))
        self.assertEqual(notices_of(html, "success"), [CATEGORY_EXCLUDED.format("Bières exemple")])
        self.assertEqual(sorted(gap_names(html)), ["Cidre exemple litre", "Rouge exemple litre"])
        self.assertEqual(category_choices(html), [choice for choice in self.OFFERED if choice[0] != "Bières exemple"])
        self.assertEqual(excluded_lines(html), [f"Catégorie « Bières exemple » · 2 articles {INCLUDE_BUTTON}"])
        self.assertEqual(exclusions_summary_of(html), "Exclus des écarts · 1 catégorie")
        self.assertIs(exclusions_open(html), True)
        self.assertEqual(list(GapExclusion.objects.values_list("stock_type", "category")), [(None, "Bières exemple")])
        # It stays: a fresh page says the same, and no message.
        html = self.html()
        self.assertEqual(sorted(gap_names(html)), ["Cidre exemple litre", "Rouge exemple litre"])
        self.assertEqual(excluded_lines(html), [f"Catégorie « Bières exemple » · 2 articles {INCLUDE_BUTTON}"])
        self.assertEqual(notices_of(html, "success"), [])

    def test_the_category_picked_from_the_select_is_the_one_excluded(self):
        """What the select offers is what the view takes, the blank
        category included: each posted as the browser would."""
        for value, words in self.OFFERED:
            with self.subTest(categorie=value):
                GapExclusion.objects.all().delete()
                form = category_form_of(self.html())
                response = self.client.post(reverse(EXCLUDE), {**hidden_fields(form), "categorie": value}, follow=True)
                self.assertEqual(GapExclusion.objects.get().category, value)
                self.assertNotIn((value, words), category_choices(response.content.decode()))

    def test_an_article_filed_there_afterwards_is_out_too(self):
        """Reclassified into the category, the cider leaves the gaps; so does
        a brown beer filed there after the category was left out."""
        self.exclude(categorie="Bières exemple")
        StockType.objects.filter(pk=self.cider.pk).update(category="Bières exemple")
        html = self.html()
        self.assertEqual(gap_names(html), ["Rouge exemple litre"])
        self.assertEqual(excluded_lines(html), [f"Catégorie « Bières exemple » · 3 articles {INCLUDE_BUTTON}"])
        # No article left without a category: the blank one is no longer offered.
        self.assertNotIn("", [value for value, _words in category_choices(html)])
        brown = filed("Brune exemple", "Bières exemple")
        counted(self.take, brown, "50")
        RecipeSale.objects.create(
            recipe=recipe("Pinte brune exemple", "35.00", brown, "0.5"),
            sold_on=timezone.localdate() - timedelta(days=1),
            quantity=10,
        )
        html = self.html()
        self.assertEqual(gap_names(html), ["Rouge exemple litre"])
        self.assertEqual(excluded_lines(html), [f"Catégorie « Bières exemple » · 4 articles {INCLUDE_BUTTON}"])
        # Taken out of the category, the cider counts again.
        StockType.objects.filter(pk=self.cider.pk).update(category="Cidres exemple")
        self.assertEqual(gap_names(self.html()), ["Cidre exemple litre", "Rouge exemple litre"])

    def test_the_blank_category(self):
        response = self.exclude(categorie="")
        html = response.content.decode()
        self.assertEqual(notices_of(html, "success"), [BLANK_EXCLUDED])
        self.assertEqual(self.landing(response), self.page_at("exclusions"))
        self.assertNotIn("Cidre exemple litre", gap_names(html))
        self.assertEqual(excluded_lines(html), [f"{BLANK_CATEGORY} · 1 article {INCLUDE_BUTTON}"])
        self.assertEqual(category_choices(html), self.OFFERED[:-1])
        self.assertEqual(list(GapExclusion.objects.values_list("stock_type", "category")), [(None, "")])
        response = self.send(include_form_of(html, BLANK_CATEGORY))
        html = response.content.decode()
        self.assertEqual(notices_of(html, "success"), [BLANK_BACK])
        self.assertIn("Cidre exemple litre", gap_names(html))
        self.assertEqual(category_choices(html), self.OFFERED)

    def test_reinclure_a_category(self):
        self.exclude(categorie="Bières exemple")
        response = self.send(include_form_of(self.html(), "Bières exemple"))
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("exclusions"))
        self.assertEqual(notices_of(html, "success"), [CATEGORY_BACK.format("Bières exemple")])
        self.assertEqual(sorted(gap_names(html)), self.ALL)
        self.assertEqual(category_choices(html), self.OFFERED)
        self.assertEqual(excluded_lines(html), [])
        # Open for its message alone, which it holds; shut on a fresh page.
        self.assertIs(exclusions_open(html), True)
        self.assertEqual(said_in_the_fold(html), [("success", CATEGORY_BACK.format("Bières exemple"))])
        self.assertIs(exclusions_open(self.html()), False)

    def test_a_category_no_article_carries_is_not_excluded(self):
        """The gaps compare a category exactly: a name no article carries -
        another case, a space more - would leave out nothing, and is
        refused rather than listed as if it did."""
        for asked in (
            "Inconnue exemple",
            "bières exemple",
            "BIÈRES EXEMPLE",
            " Bières exemple",
            "Bières exemple ",
            "\x00",
        ):
            with self.subTest(categorie=asked):
                response = self.exclude(categorie=asked)
                html = response.content.decode()
                self.assertEqual(self.landing(response), self.page_at("exclusions"))
                self.assertEqual(notices_of(html, "error"), [CATEGORY_NOT_FOUND])
                self.assertEqual(notices_of(html, "success"), [])
                self.assertFalse(GapExclusion.objects.exists())
                self.assertEqual(category_choices(html), self.OFFERED)

    def test_an_article_left_out_alone_and_with_its_category(self):
        """Both listed - the categories first, the article's line saying its
        category is out too - and taking the category back leaves the
        article out still: it was left out on its own, and its line no
        longer says so."""
        self.exclude(article=self.blonde.pk)
        html = self.exclude(categorie="Bières exemple").content.decode()
        self.assertEqual(
            excluded_lines(html),
            [
                f"Catégorie « Bières exemple » · 2 articles {INCLUDE_BUTTON}",
                f"Blonde exemple Bières exemple{CATEGORY_OUT_TOO} {INCLUDE_BUTTON}",
            ],
        )
        self.assertEqual(exclusions_summary_of(html), "Exclus des écarts · 1 catégorie · 1 article")
        html = self.send(include_form_of(html, "Catégorie « Bières exemple »")).content.decode()
        self.assertEqual(
            sorted(gap_names(html)), ["Ambrée exemple litre", "Cidre exemple litre", "Rouge exemple litre"]
        )
        self.assertEqual(excluded_lines(html), [f"Blonde exemple Bières exemple {INCLUDE_BUTTON}"])

    def test_reinclure_on_an_article_whose_category_is_out_too_keeps_it_out(self):
        """Its own exclusion goes, and the page warns that it stays out - its
        category is - rather than saying it counts again; it does once the
        category is taken back."""
        self.exclude(article=self.blonde.pk)
        html = self.exclude(categorie="Bières exemple").content.decode()
        response = self.send(include_form_of(html, "Blonde exemple"))
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("exclusions"))
        warned = STILL_OUT.format("Blonde exemple", "Bières exemple")
        self.assertEqual(said_in_the_fold(html), [("warning", warned)])
        self.assertEqual(said_in(html), [("warning", warned)])
        self.assertEqual(list(GapExclusion.objects.values_list("stock_type", "category")), [(None, "Bières exemple")])
        self.assertEqual(sorted(gap_names(html)), ["Cidre exemple litre", "Rouge exemple litre"])
        self.assertEqual(excluded_lines(html), [f"Catégorie « Bières exemple » · 2 articles {INCLUDE_BUTTON}"])
        # The category taken back, the blonde counts again: nothing of its own
        # exclusion was left to keep it out.
        html = self.send(include_form_of(html, "Catégorie « Bières exemple »")).content.decode()
        self.assertEqual(said_in(html), [("success", CATEGORY_BACK.format("Bières exemple"))])
        self.assertEqual(sorted(gap_names(html)), self.ALL)
        self.assertFalse(GapExclusion.objects.exists())

    def test_only_the_article_whose_category_is_out_is_marked(self):
        """The red, alone, its « Vins exemple » counted; the blonde, its
        « Bières exemple » out too."""
        self.exclude(article=self.red.pk)
        self.exclude(article=self.blonde.pk)
        html = self.exclude(categorie="Bières exemple").content.decode()
        self.assertEqual(
            excluded_lines(html),
            [
                f"Catégorie « Bières exemple » · 2 articles {INCLUDE_BUTTON}",
                f"Blonde exemple Bières exemple{CATEGORY_OUT_TOO} {INCLUDE_BUTTON}",
                f"Rouge exemple Vins exemple {INCLUDE_BUTTON}",
            ],
        )
        # « Réinclure » on the red brings it back, said as such.
        html = self.send(include_form_of(html, "Rouge exemple")).content.decode()
        self.assertEqual(said_in_the_fold(html), [("success", ARTICLE_BACK.format("Rouge exemple"))])
        self.assertIn("Rouge exemple litre", gap_names(html))

    def test_the_blank_category_keeps_its_article_out_too(self):
        self.exclude(article=self.cider.pk)
        html = self.exclude(categorie="").content.decode()
        html = self.send(include_form_of(html, "Cidre exemple")).content.decode()
        self.assertEqual(said_in_the_fold(html), [("warning", STILL_OUT_BLANK.format("Cidre exemple"))])
        self.assertEqual(list(GapExclusion.objects.values_list("stock_type", "category")), [(None, "")])
        self.assertNotIn("Cidre exemple litre", gap_names(html))
        self.assertEqual(excluded_lines(html), [f"{BLANK_CATEGORY} · 1 article {INCLUDE_BUTTON}"])

    def test_the_line_of_an_article_with_no_category_says_the_blank_one_is_out_too(self):
        # An article with no category, left out alone while « Catégorie non
        # renseignée » is out too, is covered, and its « Réinclure » warns
        # that it stays out. Its line once said nothing of it: the mark was
        # drawn inside `{% if exclusion.stock_type.category %}`, which a blank
        # category skips - a line promising nothing, answered by a warning.
        self.exclude(article=self.cider.pk)
        html = self.exclude(categorie="").content.decode()
        (line,) = [line for line in excluded_lines(html) if line.startswith("Cidre exemple")]
        self.assertIn(CATEGORY_OUT_TOO.strip(" ·"), line)

    def test_two_categories_and_two_articles_counted_in_the_summary(self):
        self.exclude(categorie="Bières exemple")
        self.exclude(categorie="")
        self.exclude(article=self.red.pk)
        html = self.exclude(article=self.cloth.pk).content.decode()
        self.assertEqual(exclusions_summary_of(html), "Exclus des écarts · 2 catégories · 2 articles")
        # The categories first, the blank one last of them; then the articles
        # by name.
        self.assertEqual(
            excluded_lines(html),
            [
                f"Catégorie « Bières exemple » · 2 articles {INCLUDE_BUTTON}",
                f"{BLANK_CATEGORY} · 1 article {INCLUDE_BUTTON}",
                f"Nappe exemple Matériel exemple {INCLUDE_BUTTON}",
                f"Rouge exemple Vins exemple {INCLUDE_BUTTON}",
            ],
        )

    def test_what_cannot_be_filled_leaves_out_what_is_left_out(self):
        """The port no proposed recipe pours and the tablecloth no recipe
        pours at all are no gap left behind once their category is out: the
        fold says only what still is."""
        html = self.html()
        self.assertEqual(
            summary_of(html), "Ce qui ne peut pas être comblé · 1 recette pas vendue · 1 article sans recette proposée"
        )
        self.assertEqual(
            sentences_of(explainer_of(html)),
            [
                "Pas vendue depuis l'inventaire, donc pas proposée : Verre de porto exemple (jamais vendue)",
                "Aucune recette proposée ne l'utilise (18.00 € HT à combler) : Porto exemple",
                OUTSIDE_ONE,
            ],
        )
        self.exclude(categorie="Matériel exemple")
        html = self.exclude(categorie="Vins exemple").content.decode()
        self.assertEqual(summary_of(html), "Ce qui ne peut pas être comblé · 1 recette pas vendue")
        # The recipe not sold since is still not proposed: that is about the
        # recipe, not the article.
        self.assertEqual(
            sentences_of(explainer_of(html)),
            ["Pas vendue depuis l'inventaire, donc pas proposée : Verre de porto exemple (jamais vendue)"],
        )
        self.assertNotIn("Rouge exemple litre", gap_names(html))


class ExcludedBlockerTests(ExclusionTestCase):
    """A recipe held back by an article alone: once that article is left out
    it is no longer blocked, and proposed - when it fills something else.

    A syrup counted at 0,1 L and sold far past it (0,9 L: « vendu plus
    qu'acheté »). A diabolo at 31,00 € pours 2 cl of it and 25 cl of a
    lemonade with 82,5 L to fill; a syrup and water at 30,50 € pours the
    syrup alone. Both sold since."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        sale_day = today - timedelta(days=1)
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.syrup = filed("Sirop exemple", "Sirops exemple")
        cls.lemonade = filed("Limonade exemple", "Softs exemple")
        counted(cls.take, cls.syrup, "0.1")
        counted(cls.take, cls.lemonade, "100")
        diabolo = fixed_recipe("Diabolo exemple", "31.00", (cls.syrup, "0.02"), (cls.lemonade, "0.25"))
        water = fixed_recipe("Sirop à l'eau exemple", "30.50", (cls.syrup, "0.03"))
        RecipeSale.objects.create(recipe=diabolo, sold_on=sale_day, quantity=30)
        RecipeSale.objects.create(recipe=water, sold_on=sale_day, quantity=10)

    def blocked_items(self, html) -> list[str]:
        return [unescape(text_of(item)) for item in re.findall(r"<li>(.*?)</li>", explainer_of(html), flags=re.DOTALL)]

    def test_held_back_by_the_syrup(self):
        html = self.html()
        self.assertEqual(
            summary_of(html), "Ce qui ne peut pas être comblé · 2 recettes bloquées · 1 article sans recette proposée"
        )
        self.assertEqual(
            self.blocked_items(html),
            [
                "Diabolo exemple — Sirop exemple (vendu plus qu'acheté)",
                "Sirop à l'eau exemple — Sirop exemple (vendu plus qu'acheté)",
            ],
        )
        self.assertEqual(gap_names(html), ["Sirop exemple litre vendu plus qu'acheté"])
        self.assertEqual(notices_of(self.add("31"), "warning"), [f"Rien pour 31.00 € : {GAPS_FULL}."])
        self.assertEqual(entries_of(self.take), [])

    def test_the_syrup_left_out_the_diabolo_is_proposed(self):
        response = self.send(exclude_form_of(self.html(), "Sirop exemple"))
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("ecarts"))
        # Nothing blocked, nothing left unreached: the fold is gone.
        self.assertEqual(explainer_of(html), "")
        self.assertEqual(summary_of(html), "")
        # The lemonade the diabolo was held back from is a gap to fill now.
        self.assertEqual(
            cells_of(row_of(table_of(html, GAPS_TABLE), "Limonade exemple")),
            ["Limonade exemple litre", "100", "0", "0", "7.50", "92.50", "10", "82.50", "—", EXCLUDE_BUTTON],
        )
        self.assertEqual(gap_names(html), ["Limonade exemple litre"])
        html = self.add("31")
        self.assertEqual(
            [cells_of(row) for row in body_rows(table_of(html, PLAN_TABLE))],
            [["Diabolo exemple", "Diabolo exemple", "1", "31.00 €", "31.00 €", "Limonade exemple"]],
        )
        self.assertEqual(note_of(stat_of(html, PROPOSED)), EXACT)

    def test_a_recipe_pouring_only_what_is_left_out_fills_no_gap(self):
        """The syrup and water is no longer blocked, and still not proposed:
        all it would fill is left out. Under the diabolo's price, nothing."""
        self.exclude(article=self.syrup.pk)
        html = self.add("30.50")
        self.assertEqual(notices_of(html, "warning"), [f"Rien pour 30.50 € : {BELOW_CHEAPEST}."])
        html = self.add("61")
        self.assertEqual(
            [cells_of(row)[:3] for row in body_rows(table_of(html, PLAN_TABLE))],
            [["Diabolo exemple", "Diabolo exemple", "1"]],
        )
        self.assertEqual(note_of(stat_of(html, PROPOSED)), "30.00 € non proposés")

    def test_taken_back_it_holds_the_diabolo_back_again(self):
        self.exclude(article=self.syrup.pk)
        self.include(GapExclusion.objects.get())
        html = self.html()
        self.assertEqual(len(self.blocked_items(html)), 2)
        self.assertEqual(notices_of(self.add("31"), "warning"), [f"Rien pour 31.00 € : {GAPS_FULL}."])


class ExcludedShareTests(ExclusionTestCase):
    """« Écarts réduits de » averages the share of every gap the list fills:
    an article left out is no gap, and leaves the average.

    A mojito at 33,00 € pours 4 cl of rum and 2 cl of mint, ten sold since
    the count: the rum has 1,4 L to fill, the mint 1,15 L. One mojito fills
    2,86 % of the rum's and 1,74 % of the mint's - 2,30 % on average."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.rum = filed("Rhum exemple", "Rhums exemple")
        cls.mint = filed("Menthe exemple", "Herbes exemple")
        bought(cls.rum, "1", today - timedelta(days=30), unit_cost="30.00")
        bought(cls.mint, "1", today - timedelta(days=30), unit_cost="31.50")
        counted(cls.take, cls.rum, "2")
        counted(cls.take, cls.mint, "1.5")
        mojito = fixed_recipe("Mojito exemple", "33.00", (cls.rum, "0.04"), (cls.mint, "0.02"))
        RecipeSale.objects.create(recipe=mojito, sold_on=today - timedelta(days=1), quantity=10)

    def shares(self, html) -> tuple[str, str]:
        found = stat_of(html, SHARES)
        return value_of(found), note_of(found)

    def plan(self, html) -> list[list[str]]:
        return [cells_of(row) for row in body_rows(table_of(html, PLAN_TABLE))]

    def test_the_average_leaves_the_mint_out(self):
        html = self.add("33")
        self.assertEqual(self.shares(html), ("2.3 %", "en moyenne, de 1.7 % à 2.9 % selon l'article"))
        mojito = [["Mojito exemple", "Mojito exemple", "1", "33.00 €", "33.00 €", "Rhum exemple, Menthe exemple"]]
        self.assertEqual(self.plan(html), mojito)
        html = self.exclude(article=self.mint.pk).content.decode()
        self.assertEqual(self.shares(html), ("2.9 %", "en moyenne, de 2.9 % à 2.9 % selon l'article"))
        self.assertEqual(gap_names(html), ["Rhum exemple litre"])
        self.assertEqual(
            cells_of(row_of(table_of(html, GAPS_TABLE), "Rhum exemple"))[-3:], ["0.04", "2.9 %", EXCLUDE_BUTTON]
        )
        # The entry keeps what it proposed, the mint included.
        self.assertEqual(self.plan(html), mojito)
        # The next mojito fills the rum alone.
        html = self.add("33")
        self.assertEqual(
            self.plan(html), [["Mojito exemple", "Mojito exemple", "1", "33.00 €", "33.00 €", "Rhum exemple"]]
        )
        # Taken back, the mint is in the average again.
        html = self.include(GapExclusion.objects.get()).content.decode()
        self.assertEqual(gap_names(html), ["Rhum exemple litre", "Menthe exemple litre"])
        self.assertEqual(self.shares(html)[0], "4.6 %")

    def test_with_every_article_left_out_there_is_no_average(self):
        self.add("33")
        self.exclude(categorie="Rhums exemple")
        self.exclude(categorie="Herbes exemple")
        html = self.html()
        self.assertEqual(stat_of(html, SHARES), "")
        self.assertEqual(table_of(html, GAPS_TABLE), "")
        # Every category is out: none left to offer, and no empty select.
        self.assertEqual(category_form_of(html), "")
        self.assertEqual(
            excluded_lines(html),
            [
                f"Catégorie « Herbes exemple » · 1 article {INCLUDE_BUTTON}",
                f"Catégorie « Rhums exemple » · 1 article {INCLUDE_BUTTON}",
            ],
        )
        # The list stands, its amount still entered.
        self.assertEqual(value_of(stat_of(html, ENTERED)), "33.00 €")
        self.assertEqual(notices_of(self.add("33"), "warning"), [f"Rien pour 33.00 € : {GAPS_FULL}."])

    def test_with_every_article_left_out_the_page_does_not_say_no_recipe_uses_one(self):
        # Regression: with every gap left out, the empty table's sentence
        # claimed « Aucune recette vendue depuis cet inventaire n'utilise un
        # article compté ou acheté. » - false: the mojito was sold and pours
        # the rum and the mint, both counted; they were left out, which the
        # sentence did not say. Fixed 01/10/2026: the page says they are
        # (GapReport.rows_left_out), and never states what it has not checked.
        self.exclude(categorie="Rhums exemple")
        html = self.exclude(categorie="Herbes exemple").content.decode()
        self.assertEqual(table_of(html, GAPS_TABLE), "")
        # What the page says under « Écarts », up to the fold of what is out.
        said = unescape(
            text_of(html[html.index('<h2 id="ecarts">') : html.index('<details class="explainer" id="exclusions"')])
        )
        self.assertNotIn(NO_RECIPE_USES_ONE, said)
        self.assertEqual(said, f"Écarts {ALL_LEFT_OUT}")

    def test_the_last_gap_left_out_from_its_row(self):
        """The mint out, the rum's row is the table's last: its « Exclure »
        lands under the heading on the message, then the sentence where the
        table was. One taken back, the table is back and says nothing."""
        html = self.exclude(article=self.mint.pk).content.decode()
        self.assertEqual(gap_names(html), ["Rhum exemple litre"])
        self.assertEqual(gap_sentences(html), [])
        response = self.send(exclude_form_of(html, "Rhum exemple"))
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("ecarts"))
        self.assertEqual(table_of(html, GAPS_TABLE), "")
        self.assertEqual(said_under_the_gaps(html), [("success", ARTICLE_EXCLUDED.format("Rhum exemple"))])
        self.assertEqual(gap_sentences(html), [ALL_LEFT_OUT])
        self.assertEqual(gap_sentences(self.html()), [ALL_LEFT_OUT])
        html = self.send(include_form_of(html, "Menthe exemple")).content.decode()
        self.assertEqual(gap_names(html), ["Menthe exemple litre"])
        self.assertEqual(gap_sentences(html), [])


class ExcludedDearerSideTests(ExclusionTestCase):
    """A shot « au choix » between a dear rum and a cheaper one, the dear one
    left out. The engine still books every shot on the dear one while it
    has room - an exclusion is the planner's, not the till's - so a shot
    fills none of the cheaper rum's gap, and none is proposed for it: the
    page says no sale reaches it rather than promising a fill the stock page
    would not show. (SecondaryTargetTests' fixture.)"""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.premium, cls.standard = article("Rhum vieux exemple"), article("Rhum blanc exemple")
        bought(cls.premium, "1", today - timedelta(days=30), unit_cost="40.00")
        bought(cls.standard, "1", today - timedelta(days=30), unit_cost="20.00")
        counted(cls.take, cls.premium, "2")
        counted(cls.take, cls.standard, "1")
        shot = make_recipe(name="Shot au choix exemple", selling_price_ttc="33.00")
        make_ingredient(shot, stock_type=cls.premium, quantity="0.04", group=0)
        make_ingredient(shot, stock_type=cls.standard, quantity="0.04", group=0)
        RecipeSale.objects.create(recipe=shot, sold_on=today - timedelta(days=1), quantity=10)

    def test_no_shot_is_proposed_for_the_cheaper_side(self):
        html = self.exclude(article=self.premium.pk).content.decode()
        self.assertEqual(gap_names(html), [f"Rhum blanc exemple litre {SECONDARY}"])
        html = self.add("99")
        self.assertEqual(notices_of(html, "warning"), [f"Rien pour 99.00 € : {GAPS_FULL}."])
        self.assertEqual(entries_of(self.take), [])
        # Taken back, the dear rum is the shots' gap again.
        self.include(GapExclusion.objects.get())
        html = self.add("99")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "99.00 €")


class ExcludedNamesWithMarkupTests(ExclusionTestCase):
    """An article's name and a category come from invoices and from what
    somebody typed: in the fold's list, its select and its messages they
    are text, never markup."""

    NAME = "<em>Gras exemple</em>"
    CATEGORY = '"><em>Catégorie exemple</em>'

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.fat = filed(cls.NAME, cls.CATEGORY)
        counted(cls.take, cls.fat, "100")
        RecipeSale.objects.create(
            recipe=recipe("Verre gras exemple", "34.00", cls.fat, "0.5"), sold_on=today - timedelta(days=1), quantity=10
        )

    def assertNoMarkup(self, html):
        self.assertNotIn(self.NAME, html)
        self.assertNotIn("<em>Catégorie exemple</em>", html)

    def test_the_select_offers_it_as_text(self):
        html = self.html()
        self.assertNoMarkup(html)
        self.assertIn(
            '<option value="&quot;&gt;&lt;em&gt;Catégorie exemple&lt;/em&gt;">'
            "&quot;&gt;&lt;em&gt;Catégorie exemple&lt;/em&gt; (1)</option>",
            category_form_of(html),
        )
        self.assertEqual(category_choices(html), [(self.CATEGORY, f"{self.CATEGORY} (1)")])

    def test_the_article_excluded_is_named_as_text(self):
        html = self.send(exclude_form_of(self.html(), "Gras exemple")).content.decode()
        self.assertNoMarkup(html)
        self.assertEqual(notices_of(html, "success"), [ARTICLE_EXCLUDED.format(self.NAME)])
        self.assertEqual(excluded_lines(html), [f"{self.NAME} {self.CATEGORY} {INCLUDE_BUTTON}"])
        self.assertIn("&lt;em&gt;Gras exemple&lt;/em&gt;", exclusions_of(html))
        html = self.send(include_form_of(html, self.NAME)).content.decode()
        self.assertNoMarkup(html)
        self.assertEqual(notices_of(html, "success"), [ARTICLE_BACK.format(self.NAME)])

    def test_the_category_excluded_is_named_as_text(self):
        html = self.send_category(self.html()).content.decode()
        self.assertNoMarkup(html)
        self.assertEqual(notices_of(html, "success"), [CATEGORY_EXCLUDED.format(self.CATEGORY)])
        self.assertEqual(excluded_lines(html), [f"Catégorie « {self.CATEGORY} » · 1 article {INCLUDE_BUTTON}"])
        html = self.send(include_form_of(html, self.CATEGORY)).content.decode()
        self.assertNoMarkup(html)
        self.assertEqual(notices_of(html, "success"), [CATEGORY_BACK.format(self.CATEGORY)])

    def send_category(self, html):
        """« Exclure la catégorie » with the select's one option, as the
        browser posts its value."""
        ((value, _words),) = category_choices(html)
        return self.client.post(
            reverse(EXCLUDE), {**hidden_fields(category_form_of(html)), "categorie": value}, follow=True
        )


class ExclusionMessagesTests(ExclusionTestCase):
    """Each exclusion message is said where its redirect lands, which is
    where the owner reads the page from: an article's « Exclure » right
    under the « Écarts » heading (#ecarts); a category's, and every
    « Réinclure », first thing in « Exclus des écarts » (#exclusions),
    drawn open for it. Said at the top, two screens above, nobody saw them.
    Every other message stays at the top. Each once, in its place only.

    A blonde (« Bières exemple ») and a cider filed nowhere, counted 20 days
    ago; pints at 35,00 € and bowls at 32,00 €, sold yesterday."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.blonde = filed("Blonde exemple", "Bières exemple")
        cls.cider = filed("Cidre exemple", "")
        counted(cls.take, cls.blonde, "100")
        counted(cls.take, cls.cider, "100")
        bought(cls.blonde, "200", today - timedelta(days=10))
        for made, sold in (
            (recipe("Pinte blonde exemple", "35.00", cls.blonde, "0.5"), 40),
            (recipe("Bolée exemple", "32.00", cls.cider, "0.5"), 10),
        ):
            RecipeSale.objects.create(recipe=made, sold_on=today - timedelta(days=1), quantity=sold)

    def assertSaidOnlyThere(self, html, *, top=(), gaps=(), fold=()):
        """`top` above the page's header, `gaps` under « Écarts », `fold` in
        « Exclus des écarts » - and not one message anywhere else."""
        self.assertEqual(said_at_the_top(html), list(top))
        self.assertEqual(said_under_the_gaps(html), list(gaps))
        self.assertEqual(said_in_the_fold(html), list(fold))
        self.assertEqual(said_in(html), [*top, *gaps, *fold])

    def test_an_article_s_exclure_is_said_under_the_gaps(self):
        # Not found, with nothing left out: under the gaps too - and it is no
        # message of the fold's, which stays shut.
        html = self.exclude(article="999999").content.decode()
        self.assertSaidOnlyThere(html, gaps=[("error", ARTICLE_NOT_FOUND)])
        self.assertIs(exclusions_open(html), False)
        response = self.send(exclude_form_of(self.html(), "Blonde exemple"))
        self.assertEqual(self.landing(response), self.page_at("ecarts"))
        html = response.content.decode()
        self.assertSaidOnlyThere(html, gaps=[("success", ARTICLE_EXCLUDED.format("Blonde exemple"))])
        # Open for what is left out, with no message in it.
        self.assertIs(exclusions_open(html), True)

    def test_a_category_is_said_in_the_fold(self):
        response = self.exclude(categorie="Bières exemple")
        html = response.content.decode()
        self.assertEqual(self.landing(response), self.page_at("exclusions"))
        self.assertSaidOnlyThere(html, fold=[("success", CATEGORY_EXCLUDED.format("Bières exemple"))])
        html = self.exclude(categorie="Inconnue exemple").content.decode()
        self.assertSaidOnlyThere(html, fold=[("error", CATEGORY_NOT_FOUND)])

    def test_every_reinclure_is_said_in_the_fold(self):
        self.exclude(article=self.blonde.pk)
        self.exclude(categorie="")
        blonde = GapExclusion.objects.get(stock_type=self.blonde)
        html = self.include(blonde).content.decode()
        self.assertSaidOnlyThere(html, fold=[("success", ARTICLE_BACK.format("Blonde exemple"))])
        # Clicked again from the same page: gone, said in the same place.
        html = self.include(blonde).content.decode()
        self.assertSaidOnlyThere(html, fold=[("warning", EXCLUSION_GONE)])
        html = self.include(GapExclusion.objects.get()).content.decode()
        self.assertSaidOnlyThere(html, fold=[("success", BLANK_BACK)])

    def test_the_fold_opens_for_its_message_with_nothing_left_out(self):
        """Nothing left out keeps the fold shut - unless a message of its own
        is there: it is drawn open to show it, and shut again on the next
        visit."""
        self.exclude(article=self.cider.pk)
        html = self.include(GapExclusion.objects.get()).content.decode()
        self.assertFalse(GapExclusion.objects.exists())
        self.assertIs(exclusions_open(html), True)
        self.assertSaidOnlyThere(html, fold=[("success", ARTICLE_BACK.format("Cidre exemple"))])
        self.assertEqual(excluded_lines(html), [])
        self.assertIn(NOTHING_EXCLUDED, sentences_of(exclusions_of(html)))
        for name, data, said in (
            (EXCLUDE, {"categorie": "Inconnue exemple"}, ("error", CATEGORY_NOT_FOUND)),
            (INCLUDE, {"exclusion": "999999"}, ("warning", EXCLUSION_GONE)),
        ):
            with self.subTest(said=said):
                html = self.post(name, depuis=self.take.pk, **data).content.decode()
                self.assertFalse(GapExclusion.objects.exists())
                self.assertIs(exclusions_open(html), True)
                self.assertSaidOnlyThere(html, fold=[said])
        html = self.html()
        self.assertIs(exclusions_open(html), False)
        self.assertEqual(said_in(html), [])

    def test_the_list_s_messages_stay_at_the_top(self):
        """An amount refused, an entry taken back, the list cleared: the
        list's forms are at the top of the page, and so are their messages -
        neither under the gaps nor in the fold, which they do not open."""
        html = self.add("abc")
        self.assertSaidOnlyThere(html, top=[("error", UNREADABLE)])
        self.assertIs(exclusions_open(html), False)
        self.add("70")
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        self.assertSaidOnlyThere(html, top=[("success", UNDONE)])
        self.add("70")
        html = self.post(CLEAR, depuis=self.take.pk).content.decode()
        self.assertSaidOnlyThere(html, top=[("success", CLEARED)])
        self.assertIs(exclusions_open(html), False)
        # Something left out: the fold is open, the list's message still not
        # in it.
        self.exclude(article=self.cider.pk)
        html = self.add("abc")
        self.assertSaidOnlyThere(html, top=[("error", UNREADABLE)])
        self.assertIs(exclusions_open(html), True)

    def test_several_actions_each_said_in_its_place(self):
        """Posted one after the other with the page read once at the end
        (a tab left behind): every message in its place, in the order said."""
        for name, data in (
            (EXCLUDE, {"article": self.blonde.pk}),
            (ADD, {"montant": "abc"}),
            (EXCLUDE, {"categorie": ""}),
            (INCLUDE, {"exclusion": "999999"}),
            (EXCLUDE, {"article": "999999"}),
        ):
            response = self.client.post(reverse(name), {"depuis": self.take.pk, **data})
            self.assertEqual(response.status_code, 302)
        html = self.html()
        self.assertSaidOnlyThere(
            html,
            top=[("error", UNREADABLE)],
            gaps=[("success", ARTICLE_EXCLUDED.format("Blonde exemple")), ("error", ARTICLE_NOT_FOUND)],
            fold=[("success", BLANK_EXCLUDED), ("warning", EXCLUSION_GONE)],
        )
        # Said once: the next visit has none.
        self.assertEqual(said_in(self.html()), [])


class ExclusionMessagesWithNoCountTests(PageTestCase):
    """No count at all: no gaps table and no fold to say a message in, so
    every message is said at the top of the page - the exclusions are the
    espace's, and are made all the same. Two articles, nothing counted: a
    blonde (« Bières exemple ») and a cider filed nowhere."""

    @classmethod
    def setUpTestData(cls):
        cls.blonde = filed("Blonde exemple", "Bières exemple")
        cls.cider = filed("Cidre exemple", "")

    def assertSaidAtTheTop(self, response, said):
        html = response.content.decode()
        self.assertEqual(self.landing(response), reverse(PAGE))
        self.assertIn('class="empty-state"', html)
        self.assertNotIn('<h2 id="ecarts">', html)
        self.assertEqual(exclusions_of(html), "")
        self.assertEqual(said_at_the_top(html), [said])
        self.assertEqual(said_in(html), [said])

    def test_each_message_at_the_top(self):
        self.assertSaidAtTheTop(
            self.post(EXCLUDE, depuis="", article=self.blonde.pk),
            ("success", ARTICLE_EXCLUDED.format("Blonde exemple")),
        )
        self.assertSaidAtTheTop(
            self.post(EXCLUDE, depuis="", categorie="Bières exemple"),
            ("success", CATEGORY_EXCLUDED.format("Bières exemple")),
        )
        self.assertSaidAtTheTop(
            self.post(INCLUDE, depuis="", exclusion=GapExclusion.objects.get(stock_type=self.blonde).pk),
            ("warning", STILL_OUT.format("Blonde exemple", "Bières exemple")),
        )
        self.assertSaidAtTheTop(
            self.post(INCLUDE, depuis="", exclusion=GapExclusion.objects.get().pk),
            ("success", CATEGORY_BACK.format("Bières exemple")),
        )
        self.assertFalse(GapExclusion.objects.exists())
        for name, data, said in (
            (EXCLUDE, {"article": "999999"}, ("error", ARTICLE_NOT_FOUND)),
            (EXCLUDE, {"categorie": "Inconnue exemple"}, ("error", CATEGORY_NOT_FOUND)),
            (INCLUDE, {"exclusion": "999999"}, ("warning", EXCLUSION_GONE)),
            (ADD, {"montant": "70"}, ("error", TAKE_UNKNOWN)),
        ):
            with self.subTest(said=said):
                self.assertSaidAtTheTop(self.post(name, depuis="", **data), said)

    def test_several_at_once_all_at_the_top(self):
        for name, data in (
            (EXCLUDE, {"article": self.cider.pk}),
            (ADD, {"montant": "70"}),
            (EXCLUDE, {"categorie": ""}),
            (INCLUDE, {"exclusion": "999999"}),
        ):
            self.assertEqual(self.client.post(reverse(name), {"depuis": "", **data}).status_code, 302)
        html = self.html()
        said = [
            ("success", ARTICLE_EXCLUDED.format("Cidre exemple")),
            ("error", TAKE_UNKNOWN),
            ("success", BLANK_EXCLUDED),
            ("warning", EXCLUSION_GONE),
        ]
        self.assertCountEqual(said_at_the_top(html), said)
        self.assertCountEqual(said_in(html), said)
        self.assertEqual(said_in(self.html()), [])


class EmptyGapsTableTests(ExclusionTestCase):
    """An empty gaps table says why: every gap it would hold is left out
    (« Tous les articles de ces écarts sont exclus. », `rows_left_out`), or
    no recipe sold since pours one - and then that is what it says, left
    out or not, since leaving out an article no recipe sold pours empties
    nothing.

    A blonde (« Bières exemple ») counted 20 days ago, its pint at 35,00 €
    not sold since; tablecloths (« Matériel exemple ») bought, in no
    recipe."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        cls.take = make_stock_take(taken_at=noon(today - timedelta(days=20)))
        cls.blonde = filed("Blonde exemple", "Bières exemple")
        counted(cls.take, cls.blonde, "100")
        recipe("Pinte blonde exemple", "35.00", cls.blonde, "0.5")
        cls.cloth = make_stock_type(name="Nappe exemple", unit=UnitChoices.UNIT, category="Matériel exemple")
        bought(cls.cloth, "10", today - timedelta(days=10))

    def test_no_recipe_sold_says_so(self):
        html = self.html()
        self.assertEqual(table_of(html, GAPS_TABLE), "")
        self.assertEqual(gap_sentences(html), [NO_RECIPE_USES_ONE])

    def test_left_out_or_not_no_recipe_sold_still_says_so(self):
        html = self.exclude(article=self.cloth.pk).content.decode()
        self.assertEqual(said_under_the_gaps(html), [("success", ARTICLE_EXCLUDED.format("Nappe exemple"))])
        self.assertEqual(gap_sentences(html), [NO_RECIPE_USES_ONE])
        html = self.exclude(categorie="Bières exemple").content.decode()
        self.assertEqual(gap_sentences(html), [NO_RECIPE_USES_ONE])
        self.assertNotIn(ALL_LEFT_OUT, html)

    def test_once_its_pint_sold_the_blonde_left_out_empties_the_table(self):
        RecipeSale.objects.create(
            recipe=Recipe.objects.get(name="Pinte blonde exemple"),
            sold_on=timezone.localdate() - timedelta(days=1),
            quantity=10,
        )
        html = self.html()
        self.assertEqual(gap_names(html), ["Blonde exemple litre"])
        self.assertEqual(gap_sentences(html), [])
        html = self.exclude(categorie="Bières exemple").content.decode()
        self.assertEqual(table_of(html, GAPS_TABLE), "")
        self.assertEqual(gap_sentences(html), [ALL_LEFT_OUT])
        self.assertNotIn(NO_RECIPE_USES_ONE, html)
