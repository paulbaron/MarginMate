"""La page « Marges » : ce qu'elle promet, en chiffres.

The owner asked three questions and the page answers them in his own order -
**la marge réelle** (tout ce qui a été facturé, charges comprises), **la
marge produits** (ce que les recettes vendues ont consommé, plus les articles
cochés) et **les marges par catégorie**. These tests assert the FIGURES it
prints rather than that it returns 200: a margin page that renders
beautifully and prints the wrong number is exactly the failure this codebase
keeps having.

Four things it must never do, each pinned here:

* **print a margin with no coverage beside it.** A category can read
  a 90 % margin on a third of its units costed; the figure alone is a
  fiction, and the page has to say so in words, not only as a number.
* **print 100 % for a category nothing of which has a recipe.** No cost known
  is no margin at all - « — », and the reason beside it.
* **draw a margin out of part of the money.** A day whose revenue was never
  read counts its units and its cost but no revenue, so the page says at the
  top how many days those are and what to run.
* **lose the window on a click.** Every link the page draws carries the
  period it is showing, and « depuis le début » is always reachable.

Data invented throughout - no real product name, amount or till export.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from inventory.models import MovementKind, StockType
from invoices.models import Invoice
from recipes.models import PosProduct, PosProductDailyQuantity, RecipeSale
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

PAGE = "margins:margins_home"


def till_product(name, recipe=None, category="", typology="") -> PosProduct:
    return PosProduct.objects.create(name=name, recipe=recipe, category=category, typology=typology)


def rang_up(product, day, quantity, ttc="0", ht="0", read=True, without_rate="0"):
    """One (till product, day) as the L'Addition import writes it."""
    return PosProductDailyQuantity.objects.create(
        product=product,
        sold_on=day,
        quantity=quantity,
        revenue_ttc=Decimal(ttc),
        revenue_ht=Decimal(ht),
        revenue_without_rate_ttc=Decimal(without_rate),
        revenue_read=read,
    )


def priced_article(name, unit_cost, quantity="10"):
    article = make_stock_type(name=name)
    make_movement(stock_type=article, quantity=quantity, unit_cost_ht=unit_cost)
    return article


def invoiced(day, total_ht, supplier=None, vat_rate="0.20"):
    invoice = make_invoice(supplier=supplier or make_supplier(name="Fournisseur de boissons"), invoice_date=day)
    make_invoice_line(invoice=invoice, total_ht=total_ht, vat_rate=Decimal(vat_rate))
    return invoice


def bought(article, day, total_ht, vat_rate="0.20"):
    """A purchase of `article`, invoice line and stock movement - what the
    flagged articles are counted from."""
    invoice = make_invoice(invoice_date=day)
    product = make_product(supplier=invoice.supplier, stock_type=article)
    line = make_invoice_line(
        invoice=invoice, product=product, quantity=1, total_ht=total_ht, vat_rate=Decimal(vat_rate)
    )
    return make_movement(
        stock_type=article,
        quantity="1",
        unit_cost_ht=total_ht,
        invoice_line=line,
        kind=MovementKind.PURCHASE,
        occurred_on=day,
    )


def rows_of(html: str) -> list[str]:
    return re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.S)


def row_of(html: str, name: str) -> str:
    """The table row naming `name` - a figure is asserted inside its own row,
    never anywhere on the page: « 100 » is on every page ever written."""
    return next((row for row in rows_of(html) if name in row), "")


def text_of(fragment: str) -> str:
    """A fragment as it reads on screen: no tags, and the template's own line
    breaks collapsed. A sentence asserted against raw HTML passes or fails on
    where the indentation happens to fall."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def cells_of(row: str) -> list[str]:
    """A row's cells as they read on screen. The whole row is asserted at
    once where it matters: « the margin is somewhere in this row » passes just
    as happily when the cost and the margin have swapped columns."""
    return [text_of(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]


def _element(html: str, start: int) -> str:
    """`html` from `start` to the closing tag of the <div> opening there."""
    depth = 0
    for match in re.finditer(r"<div\b|</div>", html[start:]):
        depth += 1 if match.group(0).startswith("<div") else -1
        if depth == 0:
            return html[start : start + match.end()]
    return html[start:]


def stat_of(html: str, label: str) -> str:
    """The headline figure labelled `label`, and nothing else.

    Read to the block's own closing tag rather than to the next figure: the
    last stat of a row is followed by a paragraph full of amounts, and a test
    that stopped there would pass on any of them.
    """
    for match in re.finditer(r'<div class="stat(?=["\s])', html):
        block = _element(html, match.start())
        if label in block.split('class="stat-value')[0]:
            return block
    return ""


def value_of(stat: str) -> str:
    """The big figure of a stat block, without its note - the note carries
    the TTC and the percentage, so a value read from the whole block would
    pass whichever of the three sat where."""
    found = re.search(r'<div class="stat-value[^"]*">(.*?)</div>', stat, flags=re.S)
    return text_of(found.group(1)) if found else ""


class OneSellingDayTests(TestCase):
    """The whole page over one honest day: a drink with a recipe, a planche
    with none, a delivery, a charge and the paper towels.

    Encaissé 145,45 € HT (160,00 TTC) · facturé 122,00 € HT (92,00 de
    marchandises, 30,00 de charges) · coût des recettes 20,00 € · essuie-tout
    12,00 €. Marge réelle 23,45 € (16,1 %), marge produits 113,45 € (78,0 %),
    20 unités chiffrées sur 25 et 75 % du chiffre d'affaires.
    """

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.day = cls.today - timedelta(days=10)

        syrup = priced_article("Sirop de bergamote", unit_cost="4.00")
        cls.recipe = make_recipe(name="Limonade maison", selling_price_ttc="6.00", vat_rate="0.10")
        make_ingredient(cls.recipe, stock_type=syrup, quantity="0.25")  # 1,00 € HT le verre
        cls.lemonade = till_product(
            "Limonade maison", recipe=cls.recipe, category="Sans alcool", typology="Liquide (Non alcool)"
        )
        rang_up(cls.lemonade, cls.day, 20, ttc="120.00", ht="109.09")

        cls.board = till_product("Planche du comptoir", category="Planches", typology="Solide")
        rang_up(cls.board, cls.day, 5, ttc="40.00", ht="36.36")

        invoiced(cls.today - timedelta(days=15), total_ht="80.00")
        charges = make_supplier(name="Bailleur Exemple", expenses_only=True)
        invoiced(cls.today - timedelta(days=15), total_ht="30.00", supplier=charges)

        cls.towels = make_stock_type(name="Essuie-tout", count_in_products_margin=True)
        bought(cls.towels, cls.today - timedelta(days=12), "12.00")

    def page(self):
        response = self.client.get(reverse(PAGE))
        self.assertEqual(response.status_code, 200)
        return response

    def html(self) -> str:
        return self.page().content.decode()

    # -- la marge réelle --------------------------------------------------

    def test_the_real_margin_is_the_first_answer_on_the_page(self):
        html = self.html()
        self.assertLess(html.index("Marge réelle"), html.index("Marge produits"))
        self.assertLess(html.index("Marge produits"), html.index("Marges par catégorie"))

    def test_the_real_margin_is_every_income_less_every_invoice(self):
        stat = stat_of(self.html(), "Marge réelle (HT)")
        self.assertEqual(value_of(stat), "23.45 €")
        self.assertIn("16.1 % de l'encaissé", text_of(stat))
        self.assertIn("marge TTC 13.60 €", text_of(stat))

    def test_it_shows_what_came_in_and_what_was_invoiced_both_ways(self):
        """HT is the headline and TTC is beside it, never the other way
        round: the till takes TTC and the invoices charge HT, and the two
        compared raw would overstate every margin on the page."""
        html = self.html()
        income = stat_of(html, "Encaissé (HT)")
        self.assertEqual(value_of(income), "145.45 €")
        self.assertIn("160.00 € TTC", text_of(income))
        spend = stat_of(html, "Facturé (HT)")
        self.assertEqual(value_of(spend), "122.00 €")
        self.assertIn("marchandises 92.00 €, charges 30.00 €", text_of(spend))

    def test_it_says_in_a_sentence_what_the_real_margin_counts(self):
        self.assertContains(self.page(), "charges comprises")

    # -- la marge produits ------------------------------------------------

    def test_the_products_margin_is_the_income_less_the_recipes_and_the_flagged_articles(self):
        stat = stat_of(self.html(), "Marge produits (HT)")
        self.assertEqual(value_of(stat), "113.45 €")
        self.assertIn("78.0 % de l'encaissé", text_of(stat))

    def test_the_cost_of_the_recipes_sold_is_shown_on_its_own(self):
        self.assertEqual(value_of(stat_of(self.html(), "Coût des recettes vendues (HT)")), "20.00 €")

    def test_the_flagged_articles_say_where_they_went(self):
        html = self.html()
        self.assertEqual(value_of(stat_of(html, "Achats des articles cochés (HT)")), "12.00 €")
        # Read in its own table: the article is also a row of « Ce qui a été
        # facturé », higher up, since it was invoiced.
        extra = html[html.index("Articles comptés en plus") :]
        self.assertEqual(cells_of(row_of(extra, "Essuie-tout")), ["Essuie-tout", "12.00 €", "14.40 €"])

    def test_it_says_in_a_sentence_what_the_products_margin_counts(self):
        response = self.page()
        self.assertContains(response, "ce que les recettes vendues ont consommé")
        self.assertContains(response, "compter dans la marge produits")

    def test_the_products_margin_has_no_ttc_because_a_recipe_cost_has_none(self):
        self.assertContains(self.page(), "Le coût d'une recette n'existe qu'en HT")

    # -- la couverture ----------------------------------------------------

    def test_the_page_says_how_much_of_the_money_is_costed(self):
        """In money first - four planches at 18 € weigh far more than four
        cafés - and in units beside it."""
        stat = stat_of(self.html(), "Part chiffrée")
        self.assertEqual(value_of(stat), "75 %")
        self.assertIn("20 unités sur 25 (80 %)", text_of(stat))

    def test_a_margin_that_is_not_fully_costed_says_so_in_words(self):
        response = self.page()
        self.assertContains(response, "surestimée")

    def test_the_till_products_with_no_recipe_are_listed_with_what_they_took(self):
        self.assertEqual(
            cells_of(row_of(self.html(), "Planche du comptoir")),
            ["Planche du comptoir", "5", "36.36 €", "aucune recette"],
        )

    def test_the_biggest_one_comes_first(self):
        """The list exists to be worked through, and the recipe worth writing
        first is the one holding up the most MONEY - the coffee below sells
        six times as many units as the planches for less than a third of the
        revenue, and comes second."""
        coffee = till_product("Café", category="Boissons chaudes", typology="Liquide (Non alcool)")
        rang_up(coffee, self.day, 30, ttc="12.00", ht="10.91")

        rows = [cells_of(row) for row in rows_of(self.html())]
        listed = [cells[0] for cells in rows if len(cells) == 4 and "recette" in cells[3]]

        self.assertEqual(listed, ["Planche du comptoir", "Café"])

    def test_it_links_to_a_lier_to_do_something_about_them(self):
        self.assertContains(self.page(), reverse("recipes:pos_product_list"))

    # -- les marges par catégorie -----------------------------------------

    def test_each_category_prints_its_income_its_cost_and_its_margin(self):
        self.assertEqual(
            cells_of(row_of(self.html(), "Sans alcool")),
            [
                "Sans alcool", "109.09 €", "20", "20.00 €", "89.09 €", "81.7 %",
                "100 % — tout est chiffré (20 unités sur 20)",
            ],
        )

    def test_a_category_with_no_costed_unit_prints_no_margin(self):
        """The one way this page can be silently, expensively wrong: revenue
        with no cost reads as a 100 % margin."""
        row = row_of(self.html(), "Planches")

        self.assertEqual(
            cells_of(row),
            [
                "Planches", "36.36 €", "5", "—", "—", "—",
                "0 % — aucune recette : pas de marge calculable (0 unités sur 5)",
            ],
        )
        self.assertNotIn("100", row)

    def test_the_two_groupings_read_the_same_money_twice(self):
        """A page where they disagree is a page where one of them is wrong,
        and nothing on it says which."""
        html = self.html()

        self.assertEqual(
            cells_of(row_of(html, "Liquide (Non alcool)"))[1:6],
            ["109.09 €", "20", "20.00 €", "89.09 €", "81.7 %"],
        )
        self.assertEqual(cells_of(row_of(html, "Solide"))[1:4], ["36.36 €", "5", "—"])

    # -- the page's own chrome --------------------------------------------

    def test_the_tables_are_searchable_and_sortable_like_every_other_list(self):
        self.assertContains(self.page(), "data-table")

    def test_the_page_says_what_it_is_for(self):
        self.assertContains(self.page(), "page-subtitle")


class NothingCostedTests(TestCase):
    """A window whose every sale is a planche: there is no products margin to
    state, and printing the revenue as one would be the same lie as a 100 %
    margin on a category."""

    def setUp(self):
        day = timezone.localdate() - timedelta(days=3)
        board = till_product("Planche du comptoir", category="Planches", typology="Solide")
        rang_up(board, day, 5, ttc="40.00", ht="36.36")
        invoiced(day, total_ht="10.00")

    def test_the_real_margin_is_still_answered(self):
        self.assertIn("26.36", stat_of(self.client.get(reverse(PAGE)).content.decode(), "Marge réelle (HT)"))

    def test_the_products_margin_is_not_a_number(self):
        stat = stat_of(self.client.get(reverse(PAGE)).content.decode(), "Marge produits (HT)")
        self.assertIn("—", stat)
        self.assertNotIn("36.36", stat)

    def test_and_the_page_says_why(self):
        self.assertContains(self.client.get(reverse(PAGE)), "aucune vente chiffrée")


class CostRangeTests(TestCase):
    """A recipe with an « OU » costs a range, so the margin built on it is a
    range - and the page prints both ends rather than picking one."""

    def setUp(self):
        day = timezone.localdate() - timedelta(days=4)
        cheap = priced_article("Rhum blanc", unit_cost="10.00")
        dear = priced_article("Rhum ambré", unit_cost="20.00")
        recipe = make_recipe(name="Punch du comptoir", selling_price_ttc="9.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=cheap, quantity="0.05", group=1)
        make_ingredient(recipe, stock_type=dear, quantity="0.05", group=1)
        product = till_product("Punch du comptoir", recipe=recipe, category="Cocktails", typology="Liquide (Alcool)")
        rang_up(product, day, 10, ttc="90.00", ht="75.00")

    def test_both_ends_of_the_cost_are_printed(self):
        cells = cells_of(row_of(self.client.get(reverse(PAGE)).content.decode(), "Cocktails"))

        self.assertEqual(cells[3], "5.00 à 10.00 €")

    def test_the_margin_is_a_range_too_and_the_smallest_comes_from_the_dearest(self):
        """Read the other way round, the cheapest variation would print the
        smallest margin - and the page would promise the worst case where it
        is showing the best."""
        cells = cells_of(row_of(self.client.get(reverse(PAGE)).content.decode(), "Cocktails"))

        self.assertEqual(cells[4], "65.00 à 70.00 €")
        self.assertEqual(cells[5], "86.7 à 93.3 %")


class UnreadDaysTests(TestCase):
    """A day whose money was never read counts its units and its cost but no
    revenue: the margin reads LOW. That is the safe direction, and the page
    has to say it at the top rather than draw a margin from part of the
    money."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=5)
        syrup = priced_article("Sirop de bergamote", unit_cost="4.00")
        recipe = make_recipe(name="Limonade maison", selling_price_ttc="6.00", vat_rate="0.10")
        make_ingredient(recipe, stock_type=syrup, quantity="0.25")
        self.product = till_product("Limonade maison", recipe=recipe, category="Sans alcool")

    def test_it_says_how_many_days_have_no_price_yet(self):
        rang_up(self.product, self.day, 12, read=False)

        response = self.client.get(reverse(PAGE))

        self.assertContains(response, "n'ont pas encore de prix")
        self.assertContains(response, "12 unité")

    def test_it_says_what_to_run_about_it(self):
        rang_up(self.product, self.day, 12, read=False)

        self.assertContains(self.client.get(reverse(PAGE)), "laddition_backfill_revenue")

    def test_a_window_whose_money_is_all_read_says_nothing_of_the_kind(self):
        rang_up(self.product, self.day, 12, ttc="72.00", ht="65.45")

        self.assertNotContains(self.client.get(reverse(PAGE)), "n'ont pas encore de prix")

    def test_money_with_no_vat_rate_is_declared_rather_than_absorbed(self):
        rang_up(self.product, self.day, 12, ttc="72.00", ht="60.00", without_rate="6.00")

        self.assertContains(self.client.get(reverse(PAGE)), "sans taux de TVA")


class WhatTheFiguresDoNotSayTests(TestCase):
    """The gaps that are not about the margins themselves are declared at the
    foot of the page rather than absorbed into it: money with no VAT rate, an
    invoice nobody could date, a sale typed on a recipe by hand.

    Each of them silently moves a margin - the undated invoice downwards if
    it were counted, the hand-typed sale downwards if it were costed - so the
    page's job is to name them where the reader can act."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=6)
        syrup = priced_article("Sirop de bergamote", unit_cost="4.00")
        self.recipe = make_recipe(name="Limonade rose", selling_price_ttc="6.00", vat_rate="0.10")
        make_ingredient(self.recipe, stock_type=syrup, quantity="0.25")
        product = till_product("Limonade rose", recipe=self.recipe, category="Sans alcool")
        rang_up(product, self.day, 10, ttc="60.00", ht="54.55")

    def html(self):
        response = self.client.get(reverse(PAGE))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_an_invoice_nobody_could_date_is_named_and_left_out(self):
        """It is in no period - and counted in one it does not belong to, it
        would take that month's margin down for a bill from another year."""
        supplier = make_supplier(name="Fournisseur sans date")
        invoice = Invoice.objects.create(
            supplier=supplier, invoice_number="SD-1", invoice_date=None
        )
        make_invoice_line(invoice=invoice, total_ht="40.00", vat_rate=Decimal("0.20"))

        html = self.html()

        self.assertIn("facture sans date", html)
        self.assertIn("40.00 €", html)
        self.assertIn("pas dans les chiffres ci-dessus", html)
        # « Leur donner une date » has to be a door, not advice.
        self.assertIn(f'href="{reverse("invoices:invoice_list")}?sans_date=1"', html)

    def test_a_sale_typed_by_hand_is_named_as_being_in_neither_figure(self):
        """It has no price anywhere: in the revenue it would be free, in the
        cost alone it would be a loss. So it is in neither, and said."""
        RecipeSale.objects.create(
            recipe=self.recipe, sold_on=self.day, source="manual", quantity=7
        )

        html = self.html()

        self.assertIn("7 unité", html)
        self.assertIn("à la main", html)

    def test_a_page_with_none_of_those_gaps_says_nothing_of_the_kind(self):
        """The foot is a declaration, not furniture: printed empty every
        time, it becomes something nobody reads on the day it matters."""
        html = self.html()

        self.assertNotIn("Ce que ces chiffres ne disent pas", html)


class WindowTests(TestCase):
    """« Du … au … », the same window as the other four pages - and a default
    that says what it is, because an all-time margin mixes three years of
    prices."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.recent = cls.today - timedelta(days=10)
        cls.old = cls.today - timedelta(days=400)
        product = till_product("Pinte du comptoir", category="Bières", typology="Liquide (Alcool)")
        rang_up(product, cls.recent, 10, ttc="60.00", ht="50.00")
        rang_up(product, cls.old, 10, ttc="600.00", ht="500.00")

    def html(self, **params):
        response = self.client.get(reverse(PAGE), params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_with_no_dates_the_page_shows_the_last_twelve_months_and_names_them(self):
        html = self.html()
        self.assertIn("12 derniers mois", html)
        self.assertIn((self.today - timedelta(days=365)).strftime("%d/%m/%Y"), html)
        self.assertIn("50.00", stat_of(html, "Encaissé (HT)"))
        self.assertNotIn("550.00", html)

    def test_depuis_le_debut_is_one_click_away(self):
        self.assertIn("tout=1", self.html())

    def test_and_it_shows_everything_ever_sold(self):
        html = self.html(tout="1")
        self.assertIn("550.00", stat_of(html, "Encaissé (HT)"))
        self.assertIn("depuis le début", html)

    def test_two_dates_are_the_window(self):
        html = self.html(du=self.recent.isoformat(), au=self.recent.isoformat())
        self.assertIn("50.00", stat_of(html, "Encaissé (HT)"))
        self.assertIn(self.recent.strftime("%d/%m/%Y"), html)

    def test_one_end_alone_is_a_window_too(self):
        html = self.html(du=(self.today - timedelta(days=20)).isoformat())
        self.assertIn("50.00", stat_of(html, "Encaissé (HT)"))
        self.assertIn("Depuis le", html)

    def test_a_date_that_is_no_date_is_the_default_window_and_never_a_500(self):
        for value in ("hier", "2026-02-30", ""):
            with self.subTest(value=value):
                html = self.html(du=value)
                self.assertIn("12 derniers mois", html)

    def test_the_form_gives_the_dates_back_as_they_were_typed(self):
        html = self.html(du=self.recent.isoformat(), au=self.today.isoformat())
        self.assertIn('class="date-range"', html)
        self.assertIn(f'name="du" value="{self.recent.isoformat()}"', html)
        self.assertIn(f'name="au" value="{self.today.isoformat()}"', html)

    def test_effacer_goes_back_to_the_default_period(self):
        html = self.html(du=self.recent.isoformat())
        self.assertIn("Effacer", html)

    def test_effacer_is_the_page_without_any_period_at_all(self):
        """« Effacer » that kept the dates it is offering to clear is a
        button that does nothing, and one carrying `tout=1` would clear the
        dates into all of the history - the opposite of what it says."""
        response = self.client.get(reverse(PAGE), {"du": self.recent.isoformat()})

        self.assertEqual(response.context["clear_url"], reverse(PAGE))

    def test_under_depuis_le_debut_the_dates_are_drawn_disabled(self):
        """A named period wins over the dates (Banque's `?mois=` precedent),
        so the inputs must not stay live under figures they no longer
        command: typed into and left, they say the page is showing a window
        it is not."""
        html = self.html(du=self.recent.isoformat(), au=self.today.isoformat(), tout="1")

        form = html[html.index('class="date-range"') : html.index("</form>")]
        self.assertEqual(form.count("disabled"), 3)
        self.assertIn(f"Revenir du {self.recent.strftime('%d/%m/%Y')}", form)

    def test_depuis_le_debut_keeps_the_dates_to_offer_them_back(self):
        """The door swings both ways, as the stock page's panels do: dropped,
        the period a person typed is gone for good."""
        html = self.html(du=self.recent.isoformat(), au=self.today.isoformat())
        self.assertIn(f"du={self.recent.isoformat()}", html)
        showing_all = self.html(du=self.recent.isoformat(), au=self.today.isoformat(), tout="1")
        self.assertIn("550.00", stat_of(showing_all, "Encaissé (HT)"))
        self.assertIn(self.recent.strftime("%d/%m/%Y"), showing_all)

    def test_every_link_the_page_draws_carries_the_window(self):
        """The page's own links - not the topbar, which belongs to every page
        and to no period. A list opened on another period than the figures it
        was reached from is how two screens come to disagree with nothing on
        either of them saying why."""
        response = self.client.get(
            reverse(PAGE), {"du": self.recent.isoformat(), "au": self.today.isoformat()}
        )
        html = response.content.decode()

        for key in ("documents_url", "purchases_url", "sales_url"):
            with self.subTest(link=key):
                url = response.context[key]
                self.assertIn(f"du={self.recent.isoformat()}", url)
                self.assertIn(f"au={self.today.isoformat()}", url)
                self.assertIn(escape(url), html)


class QueryCountTests(TestCase):
    """Every row of this page reads properties of its own - a margin, a
    coverage, a range - and that is precisely where an N+1 hides (CLAUDE.md).
    Three times the categories must cost the same number of queries."""

    def sell(self, index):
        day = timezone.localdate() - timedelta(days=3)
        article = priced_article(f"Article {index}", unit_cost="4.00")
        made = make_recipe(name=f"Recette {index}", selling_price_ttc="6.00", vat_rate="0.10")
        make_ingredient(made, stock_type=article, quantity="0.25")
        product = till_product(
            f"Produit {index}", recipe=made, category=f"Catégorie {index}", typology=f"Typologie {index}"
        )
        rang_up(product, day, 10, ttc="60.00", ht="54.55")

    def test_costing_three_times_as_many_categories_costs_no_more_queries(self):
        for index in range(3):
            self.sell(index)
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.client.get(reverse(PAGE)).status_code, 200)

        for index in range(3, 9):
            self.sell(index)
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(self.client.get(reverse(PAGE)).status_code, 200)

        self.assertEqual(len(large), len(small), "une requête par ligne s'est glissée dans la page")


class EmptyDatabaseTests(TestCase):
    """A brand-new install: no sale, no invoice, no recipe. Every percentage
    on this page divides by a revenue, and there is none."""

    def test_the_page_renders_and_offers_the_next_step(self):
        response = self.client.get(reverse(PAGE))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "empty-state")
        self.assertContains(response, reverse("recipes:sales_import"))

    def test_it_draws_no_margin_out_of_nothing(self):
        html = self.client.get(reverse(PAGE)).content.decode()

        self.assertNotIn("100.0 %", html)
        self.assertNotIn("0.0 %", html)

    def test_a_window_on_an_empty_database_is_no_500_either(self):
        response = self.client.get(reverse(PAGE), {"du": "2026-03-01", "au": "2026-03-31"})

        self.assertEqual(response.status_code, 200)


class PartChiffreeMeansOneThingTests(TestCase):
    """« Part chiffrée » appears twice on this page and has to mean the same
    thing both times.

    The headline is the share of the MONEY; the column beside each category
    was the share of the UNITS. On a category selling 100 cafés at 2 € with
    no recipe and 100 cocktails at 10 € with one, that is 50 % against 83 % -
    two figures 33 points apart, under two identical labels, on one screen.
    """

    @classmethod
    def setUpTestData(cls):
        cls.day = timezone.localdate() - timedelta(days=5)
        spirit = priced_article("Alcool clair", unit_cost="2.00", quantity="1000")
        recipe = make_recipe(name="Cocktail maison", selling_price_ttc="12.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=spirit, quantity="1")  # 2,00 € HT
        costed = till_product("Cocktail maison", recipe=recipe, category="Boissons")
        rang_up(costed, cls.day, 100, ttc="1200.00", ht="1000.00")

        coffee = till_product("Café filtre", category="Boissons")
        rang_up(coffee, cls.day, 100, ttc="240.00", ht="200.00")

    def html(self):
        response = self.client.get(reverse(PAGE))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_column_is_the_share_of_the_money_like_the_headline(self):
        html = self.html()

        self.assertEqual(text_of(value_of(stat_of(html, "Part chiffrée"))), "83 %")
        self.assertEqual(cells_of(row_of(html, "Boissons"))[6].split(" — ")[0], "83 %")

    def test_and_the_units_are_said_in_the_same_cell(self):
        """Dropped for the money share, the unit count would be nowhere on
        the page: a category selling four planches at 18 € is a different
        problem from one selling forty cafés at 2 €."""
        self.assertIn("100 unités sur 200", cells_of(row_of(self.html(), "Boissons"))[6])


class WhatIsSaidBesideAMarginTests(TestCase):
    """The words in the « Part chiffrée » cell are read as the verdict on the
    margin beside them, so they answer the money, never the unit count."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=5)
        self.article = priced_article("Fût clair", unit_cost="2.00", quantity="1000")
        recipe = make_recipe(name="Pinte maison", selling_price_ttc="6.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=self.article, quantity="0.5")  # 1,00 € HT
        self.beer = till_product("Pinte maison", recipe=recipe, category="Bières")

    def html(self):
        response = self.client.get(reverse(PAGE))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def cell(self):
        return cells_of(row_of(self.html(), "Bières"))[6]

    def test_a_fully_costed_category_says_so(self):
        rang_up(self.beer, self.day, 10, ttc="60.00", ht="50.00")

        self.assertIn("tout est chiffré", self.cell())

    def test_units_that_net_out_do_not_make_a_category_fully_costed(self):
        """One sold at 6,00 € and one taken back at 5,00 € - a partial refund
        typed at the till - leave the units equal to the costed units while
        1,00 € of the money has no cost behind it. Read off the unit counts,
        the row printed « tout est chiffré » under a banner saying the margin
        was overstated."""
        rang_up(self.beer, self.day, 10, ttc="60.00", ht="50.00")
        planche = till_product("Planche du bar", category="Bières")
        rang_up(planche, self.day - timedelta(days=1), 0, ttc="12.00", ht="10.00")

        cell = self.cell()

        self.assertIn("surestimée", cell)
        self.assertNotIn("tout est chiffré", cell)

    def test_a_category_whose_money_has_no_vat_rate_says_that_instead(self):
        """Its cost is known and its HT is not, so the row prints a cost
        against a revenue of 0,00 € - a loss, under « tout est chiffré ».
        The TTC is real and is named."""
        rang_up(self.beer, self.day, 10, ttc="60.00", ht="0", without_rate="60.00")

        cells = cells_of(row_of(self.html(), "Bières"))

        self.assertIn("sans taux de TVA", cells[6])
        self.assertIn("60.00", cells[6])
        self.assertNotIn("tout est chiffré", cells[6])


class TheTablesFootTests(TestCase):
    """The page claims the two tables make the same total and never showed
    one. Worse, the headline margin and the rows are two different
    arithmetics - the headline counts the flagged articles and the off-till
    sales, the rows cannot - and nothing on screen said so."""

    @classmethod
    def setUpTestData(cls):
        cls.day = timezone.localdate() - timedelta(days=5)
        article = priced_article("Fût clair", unit_cost="2.00", quantity="1000")
        recipe = make_recipe(name="Pinte maison", selling_price_ttc="6.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=article, quantity="0.5")  # 1,00 € HT
        beer = till_product("Pinte maison", recipe=recipe, category="Bières", typology="Liquide (Alcool)")
        rang_up(beer, cls.day, 100, ttc="600.00", ht="500.00")
        planche = till_product("Planche du bar", category="Planches", typology="Solide")
        rang_up(planche, cls.day, 10, ttc="120.00", ht="100.00")

        towels = make_stock_type(name="Essuie-tout", count_in_products_margin=True)
        bought(towels, cls.day, "30.00")

    def html(self):
        response = self.client.get(reverse(PAGE))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_each_table_foots_itself(self):
        totals = [cells_of(row) for row in rows_of(self.html()) if row.startswith("<tr class=\"total\"")]

        self.assertEqual(len(totals), 2)
        for cells in totals:
            self.assertEqual(cells[:5], ["Total", "600.00 €", "110", "100.00 €", "500.00 €"])

    def test_the_page_says_why_the_total_is_not_the_headline(self):
        """500,00 € in the table against 470,00 € in the headline: the
        difference is the essuie-tout, which belong to no till category."""
        html = self.html()

        self.assertEqual(value_of(stat_of(html, "Marge produits (HT)")), "470.00 €")
        self.assertIn("articles cochés", text_of(html.split("Marges par catégorie")[1]))


class WhenNothingIsInvoicedYetTests(TestCase):
    """The current month, every month, until the suppliers' bills are
    scanned: money in, nothing out, « Marge réelle 100,0 % ».

    The products margin has a whole apparatus for « no cost known is not zero
    cost ». The real margin had none of it, and never said how many invoices
    its figure was made of."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=3)
        product = till_product("Planche du bar", category="Planches")
        rang_up(product, self.day, 10, ttc="120.00", ht="100.00")

    def html(self):
        response = self.client.get(reverse(PAGE))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_window_with_no_invoice_at_all_says_so_rather_than_100_percent(self):
        html = self.html()

        self.assertIn("aucune facture", text_of(stat_of(html, "Facturé (HT)")))
        self.assertIn("Aucune facture sur cette période", text_of(html))

    def test_it_says_how_many_invoices_the_figure_is_made_of(self):
        invoiced(self.day, total_ht="40.00")
        invoiced(self.day, total_ht="10.00")

        stat = stat_of(self.html(), "Facturé (HT)")

        self.assertEqual(value_of(stat), "50.00 €")
        self.assertIn("2 factures", text_of(stat))

    def test_with_invoices_the_warning_is_gone(self):
        invoiced(self.day, total_ht="40.00")

        self.assertNotIn("Aucune facture sur cette période", text_of(self.html()))


class TheUncostedListIsATopTests(TestCase):
    """Twelve rows are shown of however many there are - far more on the real
    till. A reader works the list to its end and believes the hole is
    closed, so the page says what the list is a top OF."""

    @classmethod
    def setUpTestData(cls):
        day = timezone.localdate() - timedelta(days=5)
        for index in range(20):
            product = till_product(f"Produit sans recette {index:02d}", category="Divers")
            rang_up(product, day, 1, ttc="12.00", ht="10.00")

    def test_the_heading_says_how_many_there_are_and_what_they_took(self):
        html = self.client.get(reverse(PAGE)).content.decode()

        self.assertIn("12 plus gros sur 20", text_of(html))
        self.assertIn("200.00 € HT", text_of(html))

    def test_a_list_showing_every_one_of_them_claims_no_top(self):
        PosProductDailyQuantity.objects.all().delete()
        PosProduct.objects.exclude(name__endswith="00").delete()
        day = timezone.localdate() - timedelta(days=5)
        rang_up(PosProduct.objects.get(name__endswith="00"), day, 1, ttc="12.00", ht="10.00")

        self.assertNotIn("plus gros sur", text_of(self.client.get(reverse(PAGE)).content.decode()))


class TickedArticlesTests(TestCase):
    """« Compter dans la marge produits » is the one box on this page a
    person can get wrong, and the page could not answer the two questions
    that follow from it: what is ticked, and is any of it also in a recipe."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=5)
        self.keg = priced_article("Fût clair", unit_cost="2.00", quantity="1000")
        recipe = make_recipe(name="Pinte maison", selling_price_ttc="6.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=self.keg, quantity="0.5")
        beer = till_product("Pinte maison", recipe=recipe, category="Bières")
        rang_up(beer, self.day, 100, ttc="600.00", ht="500.00")

    def html(self):
        response = self.client.get(reverse(PAGE))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_an_article_ticked_and_used_in_a_recipe_is_named_as_counted_twice(self):
        """The keg is consumed by the recipe AND bought as a flagged
        article: 100,00 € of pints plus 400,00 € of purchases, and the page
        printed « Marge produits -300,00 € » with no explanation on it."""
        StockType.objects.filter(pk=self.keg.pk).update(count_in_products_margin=True)
        bought(self.keg, self.day, "400.00")

        html = self.html()

        self.assertIn("Compté deux fois", text_of(html))
        self.assertIn("Fût clair", text_of(html.split("Compté deux fois")[1][:200]))

    def test_an_article_ticked_and_in_no_recipe_says_nothing_of_the_kind(self):
        towels = make_stock_type(name="Essuie-tout", count_in_products_margin=True)
        bought(towels, self.day, "30.00")

        self.assertNotIn("Compté deux fois", text_of(self.html()))

    def test_an_article_ticked_with_no_purchase_in_the_window_is_still_counted(self):
        """« Rien de coché » and « coché, rien acheté ici » print the same
        empty table, and the reader cannot tell which he is looking at."""
        make_stock_type(name="Essuie-tout", count_in_products_margin=True)

        text = text_of(self.html())

        self.assertIn("1 article coché", text)
        self.assertIn("rien n'en a été acheté sur cette période", text)

    def test_with_nothing_ticked_the_page_says_where_the_box_is(self):
        text = text_of(self.html())

        self.assertIn("Aucun article coché", text)


class RefundOnlyWindowTests(TestCase):
    """A day that only refunded is stored at quantity 0 with negative money,
    so « aucune vente » read off the units alone printed « Aucune vente
    enregistrée » over a page showing -15,00 € of takings, and offered an
    import of sales that had already been imported."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=4)
        product = till_product("Pinte maison", category="Bières")
        rang_up(product, self.day, 0, ttc="-15.00", ht="-12.50")

    def test_a_window_that_only_gave_money_back_is_not_a_window_with_no_sales(self):
        html = self.client.get(reverse(PAGE)).content.decode()

        self.assertNotIn("Aucune vente enregistrée", html)
        self.assertEqual(value_of(stat_of(html, "Encaissé (HT)")), "-12.50 €")


class TheCostNoteTests(TestCase):
    """« Coût des recettes vendues » counts the servings sold off the till
    too, and `costed_units` does not - so the note under it divided the cost
    by a tenth of the servings it was made of."""

    def setUp(self):
        self.day = timezone.localdate() - timedelta(days=4)
        article = priced_article("Fût clair", unit_cost="2.00", quantity="1000")
        self.recipe = make_recipe(name="Pinte maison", selling_price_ttc="6.00", vat_rate="0.20")
        make_ingredient(self.recipe, stock_type=article, quantity="0.5")  # 1,00 € HT
        beer = till_product("Pinte maison", recipe=self.recipe, category="Bières")
        rang_up(beer, self.day, 10, ttc="60.00", ht="50.00")

    def test_the_servings_sold_off_the_till_are_named_beside_the_cost(self):
        from recipes.models import SaleDocument, SaleDocumentLine

        document = SaleDocument.objects.create(sold_on=self.day, reference="Événement")
        SaleDocumentLine.objects.create(document=document, recipe=self.recipe, quantity="40")

        stat = stat_of(self.client.get(reverse(PAGE)).content.decode(), "Coût des recettes vendues (HT)")

        self.assertEqual(value_of(stat), "50.00 €")
        self.assertIn("10 unités de caisse", text_of(stat))
        self.assertIn("40 hors caisse", text_of(stat))

    def test_with_no_sale_document_only_the_till_is_named(self):
        stat = stat_of(self.client.get(reverse(PAGE)).content.decode(), "Coût des recettes vendues (HT)")

        self.assertIn("10 unités de caisse", text_of(stat))
        self.assertNotIn("hors caisse", text_of(stat))
