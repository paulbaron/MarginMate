"""« Articles comptés dans la marge produits », in arithmetic.

The owner ticks « compter dans la marge produits » from the Marges page, a
category at a time, rather than article form by article form. What the
report hands the page for it (`MarginReport.countable`) promises:

* **every article, under its category as it is today** - every category
  once, alphabetical, the blank one last and named « Catégorie non
  renseignée », never « Sans catégorie » (the till has one of its own);
* **the state of a category in words**: « aucun », « 3 sur 70 », « tous »;
* **beside each article, what was bought of it over the window, HT - which
  is exactly what ticking it adds to the products margin's cost.** Read by
  the very code that counts the ticked ones (`_bought_over`), so the figure
  a box promises is the figure the margin moves by, to the cent;
* **an article a recipe uses is marked**: unticked, as something that would
  be counted twice; ticked, as counted twice - where the tick is.

Data invented throughout - no real supplier, article or amount.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from common import DateRange
from inventory.models import MovementKind, StockType
from margins.computation import NO_CATEGORY, margins_for
from recipes.models import PosProduct, PosProductDailyQuantity
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_type,
)

MARCH = DateRange(date(2026, 3, 1), date(2026, 3, 31))


def bought(article, day, total_ht, vat_rate="0.20"):
    """A purchase of `article` on its own invoice: the line and the stock
    movement, the way the review queue writes them."""
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


def tick(*articles, value=True):
    StockType.objects.filter(pk__in=[article.pk for article in articles]).update(count_in_products_margin=value)


def category_of(report, name):
    return next(category for category in report.countable if category.name == name)


def article_of(report, article):
    return next(
        row for category in report.countable for row in category.articles if row.pk == article.pk
    )


class CountableFixture:
    """Three categories and a blank one, bought in March (and once in April):

    Matériel - Perceuse 100,00 · Nappe 50,00 · Tabouret (never bought)
    Consommables - Essuie-tout 30,00 (ticked) · Gobelets 12,50 · Touillettes en bois -4,00
      (a deposit given back: signed, like everything counted here)
    Spiritueux - Rhum ambré 200,00, in a recipe
    (blank) - Sirop d'églantier 10,00
    """

    @classmethod
    def setUpTestData(cls):
        cls.drill = make_stock_type(name="Perceuse sans fil", category="Matériel")
        cls.cloth = make_stock_type(name="Nappe en lin", category="Matériel")
        cls.stool = make_stock_type(name="Tabouret haut", category="Matériel")
        cls.towels = make_stock_type(name="Essuie-tout", category="Consommables", count_in_products_margin=True)
        cls.cups = make_stock_type(name="Gobelets", category="Consommables")
        cls.straws = make_stock_type(name="Touillettes en bois", category="Consommables")
        cls.rum = make_stock_type(name="Rhum ambré", category="Spiritueux")
        cls.syrup = make_stock_type(name="Sirop d'églantier", category="")

        bought(cls.drill, date(2026, 3, 3), "100.00")
        bought(cls.drill, date(2026, 4, 3), "999.00")  # outside March
        bought(cls.cloth, date(2026, 3, 4), "50.00")
        bought(cls.towels, date(2026, 3, 5), "30.00")
        bought(cls.cups, date(2026, 3, 6), "12.50")
        bought(cls.straws, date(2026, 3, 7), "6.00")
        bought(cls.straws, date(2026, 3, 8), "-10.00")
        bought(cls.rum, date(2026, 3, 9), "200.00")
        bought(cls.syrup, date(2026, 3, 10), "10.00")

        punch = make_recipe(name="Punch du comptoir", selling_price_ttc="9.00", vat_rate="0.20")
        make_ingredient(punch, stock_type=cls.rum, quantity="0.05")
        pos = PosProduct.objects.create(name="Punch du comptoir", recipe=punch, category="Cocktails")
        PosProductDailyQuantity.objects.create(
            product=pos,
            sold_on=date(2026, 3, 15),
            quantity=100,
            revenue_ttc=Decimal("900.00"),
            revenue_ht=Decimal("750.00"),
            revenue_read=True,
        )


class EveryArticleUnderItsCategoryTests(CountableFixture, TestCase):
    def test_every_category_once_alphabetical_and_the_blank_one_last(self):
        report = margins_for(MARCH)

        self.assertEqual(
            [(category.name, category.label) for category in report.countable],
            [
                ("Consommables", "Consommables"),
                ("Matériel", "Matériel"),
                ("Spiritueux", "Spiritueux"),
                ("", NO_CATEGORY),
            ],
        )

    def test_every_article_is_listed_even_one_never_bought(self):
        """The box is a setting, not a purchase: an article bought in no
        window at all can still be ticked."""
        materiel = category_of(margins_for(MARCH), "Matériel")

        self.assertEqual(
            [row.name for row in materiel.articles], ["Nappe en lin", "Perceuse sans fil", "Tabouret haut"]
        )
        stool = article_of(margins_for(MARCH), self.stool)
        self.assertFalse(stool.was_bought)
        self.assertEqual(stool.bought.ht, Decimal("0"))

    def test_a_category_is_read_as_it_is_today(self):
        """Reclassified since its purchase, an article is under its NEW
        category: the box is the article's, not the invoice line's."""
        StockType.objects.filter(pk=self.cups.pk).update(category="Matériel")

        report = margins_for(MARCH)

        self.assertIn("Gobelets", [row.name for row in category_of(report, "Matériel").articles])
        self.assertNotIn("Gobelets", [row.name for row in category_of(report, "Consommables").articles])

    def test_each_box_says_whether_it_is_ticked(self):
        report = margins_for(MARCH)

        self.assertTrue(article_of(report, self.towels).ticked)
        self.assertFalse(article_of(report, self.cups).ticked)


class TheStateInWordsTests(CountableFixture, TestCase):
    def test_none_some_all(self):
        tick(self.rum)
        report = margins_for(MARCH)

        self.assertEqual(category_of(report, "Matériel").state, "aucun")
        self.assertEqual(category_of(report, "Consommables").state, "1 sur 3")
        self.assertEqual(category_of(report, "Spiritueux").state, "tous")

    def test_an_article_classified_into_a_ticked_category_later_reads_n_sur_m(self):
        """« Tout cocher » ticks what the category holds TODAY: a newcomer
        arrives unticked, and « 3 sur 4 » is how that is seen."""
        tick(self.drill, self.cloth, self.stool)
        self.assertEqual(category_of(margins_for(MARCH), "Matériel").state, "tous")

        make_stock_type(name="Escabeau", category="Matériel")

        self.assertEqual(category_of(margins_for(MARCH), "Matériel").state, "3 sur 4")

    def test_the_counts_behind_the_words(self):
        tick(self.cups)
        consumables = category_of(margins_for(MARCH), "Consommables")

        self.assertEqual(consumables.ticked, 2)
        self.assertEqual(len(consumables.articles), 3)
        self.assertFalse(consumables.all_ticked)
        self.assertFalse(consumables.none_ticked)


class WhatTickingWouldAddTests(CountableFixture, TestCase):
    def test_each_article_carries_what_was_bought_of_it_over_the_window(self):
        report = margins_for(MARCH)

        self.assertEqual(article_of(report, self.drill).bought.ht, Decimal("100.00"))
        self.assertEqual(article_of(report, self.drill).bought.ttc, Decimal("120.00"))
        self.assertEqual(article_of(report, self.cups).bought.ht, Decimal("12.50"))
        self.assertEqual(article_of(report, self.syrup).bought.ht, Decimal("10.00"))

    def test_a_deposit_given_back_is_signed_here_too(self):
        """6,00 € of straws and 10,00 € given back: ticked, the margin's cost
        goes DOWN by 4,00 €, and the box has to say so rather than 16,00 €."""
        self.assertEqual(article_of(margins_for(MARCH), self.straws).bought.ht, Decimal("-4.00"))

    def test_a_category_adds_up_its_articles_and_what_is_already_counted(self):
        consumables = category_of(margins_for(MARCH), "Consommables")

        self.assertEqual(consumables.bought_ht, Decimal("38.50"))
        self.assertEqual(consumables.counted_ht, Decimal("30.00"))

    def test_the_ticked_articles_are_exactly_the_products_margin_s_extra_cost(self):
        """One definition: what the page says a box adds is what the margin
        counts for it, the two read by the same code."""
        tick(self.cups, self.straws, self.drill)
        report = margins_for(MARCH)

        counted = sum(
            (row.bought.ht for category in report.countable for row in category.articles if row.ticked),
            start=Decimal("0"),
        )
        self.assertEqual(counted, report.extra_products_ht)
        self.assertEqual(report.extra_products_ht, Decimal("138.50"))

    def test_ticking_moves_the_products_margin_by_exactly_what_the_box_said(self):
        before = margins_for(MARCH)
        promised = article_of(before, self.cloth).bought.ht

        tick(self.cloth)
        after = margins_for(MARCH)

        self.assertEqual(promised, Decimal("50.00"))
        self.assertEqual(before.products_margin_ht_low - after.products_margin_ht_low, promised)
        self.assertEqual(before.products_margin_ht_high - after.products_margin_ht_high, promised)
        # The real margin is what was invoiced, and ticking changes nothing
        # that was invoiced.
        self.assertEqual(before.real_margin_ht, after.real_margin_ht)

    def test_all_of_history_counts_all_of_the_purchases(self):
        self.assertEqual(article_of(margins_for(DateRange()), self.drill).bought.ht, Decimal("1099.00"))


class CountedTwiceTests(CountableFixture, TestCase):
    """An article a recipe uses is already a cost: the recipe consumes it.
    Ticked as well, it is paid for twice."""

    def test_an_unticked_article_a_recipe_uses_is_marked_before_it_is_ticked(self):
        rum = article_of(margins_for(MARCH), self.rum)

        self.assertTrue(rum.in_recipe)
        self.assertFalse(rum.counted_twice)
        self.assertEqual(category_of(margins_for(MARCH), "Spiritueux").in_recipes, 1)

    def test_ticked_it_is_counted_twice_and_its_category_opens_on_it(self):
        tick(self.rum)
        report = margins_for(MARCH)

        self.assertTrue(article_of(report, self.rum).counted_twice)
        self.assertEqual(category_of(report, "Spiritueux").counted_twice, 1)
        # Folded, the mark would be out of sight - where the tick is.
        self.assertTrue(category_of(report, "Spiritueux").opened)
        self.assertFalse(category_of(report, "Consommables").opened)
        self.assertEqual(report.flagged_in_recipes, ["Rhum ambré"])

    def test_an_article_no_recipe_uses_is_not_marked(self):
        towels = article_of(margins_for(MARCH), self.towels)

        self.assertFalse(towels.in_recipe)
        self.assertFalse(towels.counted_twice)


class NothingAtAllTests(TestCase):
    def test_no_article_is_no_category_and_no_error(self):
        report = margins_for(MARCH)

        self.assertEqual(report.countable, [])
        self.assertEqual(report.flagged_articles, 0)
        self.assertEqual(report.extra_products_ht, Decimal("0"))


class QueryCountTests(CountableFixture, TestCase):
    """Every article of the bar is on the panel, each with its purchases and
    whether a recipe uses it: three reads, whatever their number."""

    def test_three_times_the_articles_and_categories_cost_no_more_queries(self):
        with CaptureQueriesContext(connection) as small:
            margins_for(MARCH)

        for index in range(16):
            article = make_stock_type(name=f"Article {index}", category=f"Catégorie {index % 5}")
            bought(article, date(2026, 3, 12), "3.00")
            recipe = make_recipe(name=f"Recette {index}")
            make_ingredient(recipe, stock_type=article, quantity="1")
            if index % 2:
                tick(article)
        with CaptureQueriesContext(connection) as large:
            margins_for(MARCH)

        self.assertEqual(len(large), len(small), "une requête par article s'est glissée dans le panneau")
