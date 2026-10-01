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
from inventory.models import GapFillEntry, MovementKind, StockTake, StockType, UnitChoices
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
GAPS_TABLE = "écarts"
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
TOO_BIG = "10000 € au plus."
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
        # The gaps are still there, without the list's two columns.
        self.assertEqual(len(self.gaps_row(html)), 9)

    # -- no list -----------------------------------------------------------

    def test_without_a_list_the_gaps_and_no_plan(self):
        html = self.html()
        self.assertEqual(
            self.gaps_row(html),
            ["Blonde exemple litre", "100", "200", "0", "20", "280", "30", "250", "1000.00 €"],
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
            ["Blonde exemple litre", "100", "200", "0", "20", "280", "30", "250", "1000.00 €", "6", "2.4 %"],
        )

    def test_a_second_amount_adds_to_the_figures_of_the_whole_list(self):
        self.add("420")
        html = self.add("420")
        self.assertEqual(value_of(stat_of(html, ENTERED)), "840.00 €")
        self.assertEqual(note_of(stat_of(html, ENTERED)), "2 montants")
        self.assertEqual(value_of(stat_of(html, PROPOSED)), "840.00 €")
        self.assertEqual(value_of(stat_of(html, SALES)), "24")
        self.assertEqual(value_of(stat_of(html, SHARES)), "4.8 %")  # 12 L of 250
        self.assertEqual(self.gaps_row(html)[-2:], ["12", "4.8 %"])
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
            ("1 234,50", "1234.50 €", "1225.00 €", "9.50", "35", "non proposés"),
            ("1\N{NO-BREAK SPACE}234,50", "1234.50 €", "1225.00 €", "9.50", "35", "non proposés"),
            ("10000", "10000.00 €", "9975.00 €", "25.00", "285", "non proposés"),
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
            ("10 000", "10000.00 €", "9975.00 €", "25.00", "285"),
            ("10.000,00", "10000.00 €", "9975.00 €", "25.00", "285"),
            ("1 500,00", "1500.00 €", "1470.00 €", "30.00", "42"),
            ("1'234,50", "1234.50 €", "1225.00 €", "9.50", "35"),
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

    def test_the_field_is_empty_for_the_next_amount(self):
        # What was typed is in the list now, not in the field: the next
        # amount is typed on a blank one.
        html = self.add("1 234,50")
        self.assertEqual(heading_of(html), "À encaisser : 1234.50 €")
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
            "Ce qui ne peut pas être comblé · 3 recettes bloquées · 1 article sans recette proposée",
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
                "Pas vendues depuis l'inventaire, donc pas proposées : Galopin exemple · Verre de vin exemple",
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
        self.assertEqual(cells_of(row_of(table, "Sirop exemple"))[-2:], ["—", "—"])
        self.assertEqual(cells_of(row_of(table, "Blonde exemple"))[-2:-1], ["1.50"])


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
            "Ce qui ne peut pas être comblé · 1 recette bloquée · 1 article sans recette proposée",
        )

    def test_each_sentence(self):
        self.assertEqual(
            sentences_of(explainer_of(self.html())),
            [
                "Une seule vente ferait dépasser un écart :",
                "Pas vendue depuis l'inventaire, donc pas proposée : Verre de vin exemple",
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
            "Ce qui ne peut pas être comblé · 2 recettes bloquées · 2 articles sans recette proposée",
        )

    def test_each_sentence(self):
        self.assertEqual(
            sentences_of(explainer_of(self.html())),
            [
                "Une seule vente ferait dépasser un écart :",
                "Pas vendues depuis l'inventaire, donc pas proposées : Verre de porto exemple · Verre de vin exemple",
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
    30,00 €. 67,00 € is one of each and nothing else."""

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
        rung_up(cls.bowl_button, sale_day, 9, "270.00")
        # One bowl comped: the day's average is no price.
        rung_up(cls.bowl_button, today - timedelta(days=2), 2, "30.00")

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
            ["Bolée exemple", "Bolée caisse exemple en caisse 30.00 €", "1", "32.00 €", "32.00 €", "Cidre exemple"],
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
            ["Bolée exemple", "Bolée caisse exemple en caisse 30.00 €", "1", "32.00 €", "32.00 €", "Cidre exemple"],
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
            ["Bolée exemple", "Bolée caisse exemple en caisse 30.00 €", "1", "32.00 €", "32.00 €", "Cidre exemple"],
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
        # The bowl rung at 30,00 € was in the first amount, not the last.
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
        self.assertEqual(row[-2:], ["—", "0.0 %"])


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
        self.assertEqual(rows["Ambrée exemple"][-2:], ["0.50", "0.2 %"])
        self.assertEqual(rows["Blonde exemple"][-2:], ["0.50", "0.2 %"])
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
        self.assertEqual(rows["Ambrée exemple"][-2], "1")
        self.assertEqual(rows["Blonde exemple"][-2], "1")

    def test_the_list_columns_only_with_a_list(self):
        html = self.html(depuis=str(self.take.pk))
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 9)
        html = self.add("35")
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 11)
        # The blonde has no pint yet: nothing proposed, 0 % filled.
        self.assertEqual(self.gaps_rows(html)["Blonde exemple"][-2:], ["—", "0.0 %"])

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
        self.assertEqual(self.gaps_rows(html)["Ambrée exemple"][-2], "0.50")
        self.assertEqual(self.gaps_rows(html)["Blonde exemple"][-2], "—")
        # The last one too: the page is back to the gaps alone.
        html = self.post(UNDO, depuis=self.take.pk).content.decode()
        self.assertEqual(notices_of(html, "success"), [UNDONE])
        self.assertEqual(entries_of(self.take), [])
        self.assertNotIn(PROPOSED, html)
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 9)
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
        self.assertEqual(len(self.gaps_rows(html)["Blonde exemple"]), 9)
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
        self.assertEqual(cells_of(row_of(table_of(html, GAPS_TABLE), "Blonde exemple"))[-2], "0.50")

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
