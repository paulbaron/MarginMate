"""What the margins page promises, in arithmetic.

Three margins, over one « du … au … » window:

* **la marge réelle** - everything the bar took against everything it was
  invoiced, whatever the invoice was for;
* **la marge produits** - what it took against what the recipes it sold
  consumed, plus the articles flagged « compter dans la marge produits »
  (the paper towels: no recipe eats them, so what was bought of them is the
  only measure there is);
* **les marges par catégorie**, on both of the till's own dimensions - its
  13 « TAG_Catégorie » and its 3 « TAG_Typologie » (« food, drinks »).

Every figure below is asserted to the cent. The three the page can be
silently and expensively wrong about get a test of their own:

* a till product with **no recipe** has revenue and no cost, and would print
  a 100 % margin - so its units never count as costed, and the slice holding
  it says what share of it is;
* a day whose money was **never read** counts its units and its cost but no
  revenue at all - counted as « took 0 € » it reads as a pure loss;
* a recipe with **variations** costs a range, so the margin built on it is a
  range, and nothing here ever enumerates those variations.

Data invented throughout - no real product name, amount or till export.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase

from common import DateRange
from inventory.models import MovementKind, StockType
from invoices.models import Invoice
from margins.computation import Money, margins_for
from recipes.models import (
    PosProduct,
    PosProductDailyQuantity,
    Recipe,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
)
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

MARCH = DateRange(date(2026, 3, 1), date(2026, 3, 31))


def till_product(name, recipe=None, category="", typology="", ignored=False) -> PosProduct:
    return PosProduct.objects.create(
        name=name, recipe=recipe, category=category, typology=typology, ignored=ignored
    )


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
    """An article whose purchases give it `unit_cost` per unit - which is
    what costs an ingredient (StockType.current_unit_cost_ht)."""
    article = make_stock_type(name=name)
    make_movement(stock_type=article, quantity=quantity, unit_cost_ht=unit_cost)
    return article


def undated(supplier, total_ht, vat_rate="0.20"):
    """An invoice the reader could not find a date on - make_invoice always
    dates one, and « sans date » is exactly what this asserts about."""
    invoice = Invoice.objects.create(supplier=supplier, invoice_number=f"SD{supplier.pk}", invoice_date=None)
    make_invoice_line(invoice=invoice, total_ht=total_ht, vat_rate=Decimal(vat_rate))
    return invoice


def goods_invoice(day, total_ht, vat_rate="0.20", supplier=None):
    supplier = supplier or make_supplier(name="Fournisseur de boissons")
    invoice = make_invoice(supplier=supplier, invoice_date=day)
    make_invoice_line(invoice=invoice, total_ht=total_ht, vat_rate=Decimal(vat_rate))
    return invoice


class OneRecipeOneInvoiceTests(TestCase):
    """The whole page on the smallest honest dataset: one drink sold, one
    delivery invoiced."""

    def setUp(self):
        syrup = priced_article("Sirop de bergamote", unit_cost="4.00")
        self.recipe = make_recipe(name="Limonade maison", selling_price_ttc="6.00", vat_rate="0.10")
        make_ingredient(self.recipe, stock_type=syrup, quantity="0.25")  # 1,00 € HT la limonade
        self.pos = till_product(
            "Limonade maison", recipe=self.recipe, category="Sans alcool", typology="Liquide (Non alcool)"
        )
        rang_up(self.pos, date(2026, 3, 10), 20, ttc="120.00", ht="109.09")
        goods_invoice(date(2026, 3, 5), total_ht="80.00")
        self.report = margins_for(MARCH)

    def test_revenue_is_what_the_till_took_both_ways(self):
        self.assertEqual(self.report.revenue_till, Money(Decimal("109.09"), Decimal("120.00")))
        self.assertEqual(self.report.revenue_documents, Money())
        self.assertEqual(self.report.revenue, Money(Decimal("109.09"), Decimal("120.00")))

    def test_spend_is_what_was_invoiced_both_ways(self):
        self.assertEqual(self.report.spend_goods, Money(Decimal("80.00"), Decimal("96.00")))
        self.assertEqual(self.report.spend_charges, Money())
        self.assertEqual(self.report.spend, Money(Decimal("80.00"), Decimal("96.00")))

    def test_cogs_is_the_recipe_cost_times_what_was_rung_up(self):
        self.assertEqual(self.report.cogs_low, Decimal("20.00"))
        self.assertEqual(self.report.cogs_high, Decimal("20.00"))
        self.assertEqual(self.report.units, 20)
        self.assertEqual(self.report.costed_units, 20)

    def test_real_margin_is_every_income_less_every_invoice(self):
        self.assertEqual(self.report.real_margin_ht, Decimal("29.09"))
        self.assertEqual(self.report.real_margin_ttc, Decimal("24.00"))
        self.assertEqual(self.report.real_margin_percent.quantize(Decimal("0.01")), Decimal("26.67"))

    def test_products_margin_is_every_income_less_what_the_recipes_consumed(self):
        self.assertEqual(self.report.products_margin_ht_low, Decimal("89.09"))
        self.assertEqual(self.report.products_margin_ht_high, Decimal("89.09"))
        self.assertEqual(
            self.report.products_margin_percent_low.quantize(Decimal("0.01")), Decimal("81.67")
        )

    def test_everything_sold_is_costed(self):
        self.assertEqual(self.report.coverage, Decimal(1))
        self.assertEqual(self.report.revenue_uncosted, Money())
        self.assertEqual(self.report.top_uncosted, [])
        self.assertTrue(self.report.is_costed)

    def test_the_category_and_the_typology_say_the_same_thing_twice(self):
        (category,) = self.report.by_category
        (typology,) = self.report.by_typology
        self.assertEqual(category.name, "Sans alcool")
        self.assertEqual(typology.name, "Liquide (Non alcool)")
        for slice_ in (category, typology):
            self.assertEqual(slice_.revenue, Money(Decimal("109.09"), Decimal("120.00")))
            self.assertEqual(slice_.cost_ht_low, Decimal("20.00"))
            self.assertEqual(slice_.cost_ht_high, Decimal("20.00"))
            self.assertEqual(slice_.margin_ht_low, Decimal("89.09"))
            self.assertEqual(slice_.margin_ht_high, Decimal("89.09"))
            self.assertEqual(slice_.units, 20)
            self.assertEqual(slice_.costed_units, 20)
            self.assertEqual(slice_.coverage, Decimal(1))
            self.assertTrue(slice_.is_costed)


class WindowBoundaryTests(TestCase):
    """« du 1er au 31 » holds the 1st and the 31st - both ends included, the
    same window the four other pages read (common.date_range)."""

    def setUp(self):
        self.pos = till_product("Café", category="Boissons chaudes")
        for day in (date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 31), date(2026, 4, 1)):
            rang_up(self.pos, day, 1, ttc="2.00", ht="1.82")

    def test_both_ends_are_in(self):
        report = margins_for(MARCH)
        self.assertEqual(report.units, 2)
        self.assertEqual(report.revenue_till, Money(Decimal("3.64"), Decimal("4.00")))

    def test_an_empty_window_is_everything(self):
        report = margins_for(DateRange())
        self.assertEqual(report.units, 4)
        self.assertEqual(report.revenue_till, Money(Decimal("7.28"), Decimal("8.00")))

    def test_one_end_alone_is_a_window(self):
        since = margins_for(DateRange(start=date(2026, 3, 1)))
        self.assertEqual(since.units, 3)
        until = margins_for(DateRange(end=date(2026, 3, 1)))
        self.assertEqual(until.units, 2)

    def test_an_invoice_on_the_last_day_is_in(self):
        goods_invoice(date(2026, 3, 31), total_ht="10.00")
        goods_invoice(date(2026, 4, 1), total_ht="99.00")
        self.assertEqual(margins_for(MARCH).spend_goods.ht, Decimal("10.00"))


class VariationRangeTests(TestCase):
    """A recipe with an « OU » costs a range, so the margin built on it is a
    range - and the two ends are read off the per-group extremes, never off
    an enumeration of the variations."""

    def test_two_alternatives_give_two_ends(self):
        cheap = priced_article("Gin bas de gamme", unit_cost="10.00")
        dear = priced_article("Gin de marque", unit_cost="30.00")
        recipe = make_recipe(name="Gin tonic", selling_price_ttc="12.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=cheap, quantity="0.05", group=0)  # 0,50 €
        make_ingredient(recipe, stock_type=dear, quantity="0.05", group=0)  # 1,50 €
        pos = till_product("Gin tonic", recipe=recipe, category="Cocktails", typology="Liquide (Alcool)")
        rang_up(pos, date(2026, 3, 12), 10, ttc="120.00", ht="100.00")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("5.00"))
        self.assertEqual(report.cogs_high, Decimal("15.00"))
        self.assertEqual(report.products_margin_ht_low, Decimal("85.00"))
        self.assertEqual(report.products_margin_ht_high, Decimal("95.00"))
        self.assertEqual(report.products_margin_percent_low, Decimal("85"))
        self.assertEqual(report.products_margin_percent_high, Decimal("95"))

    def test_the_smallest_margin_comes_from_the_dearest_variation(self):
        """The two ends cross over: least margin where the gin costs most.
        Read straight through, the page prints its best case as its worst."""
        cheap = priced_article("Vodka bas de gamme", unit_cost="10.00")
        dear = priced_article("Vodka de marque", unit_cost="30.00")
        recipe = make_recipe(name="Vodka soda", selling_price_ttc="12.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=cheap, quantity="0.05", group=0)  # 0,50 €
        make_ingredient(recipe, stock_type=dear, quantity="0.05", group=0)  # 1,50 €
        pos = till_product("Vodka soda", recipe=recipe, category="Cocktails")
        rang_up(pos, date(2026, 3, 12), 10, ttc="120.00", ht="100.00")

        (slice_,) = margins_for(MARCH).by_category

        self.assertEqual(slice_.cost_ht_low, Decimal("5.00"))
        self.assertEqual(slice_.cost_ht_high, Decimal("15.00"))
        self.assertEqual(slice_.margin_ht_low, Decimal("85.00"))
        self.assertEqual(slice_.margin_ht_high, Decimal("95.00"))
        self.assertEqual(slice_.margin_percent_low, Decimal("85"))
        self.assertEqual(slice_.margin_percent_high, Decimal("95"))

    def test_twenty_choices_is_a_million_variations_and_still_two_sums(self):
        """A recipe with 20 either/or groups has 1 048 576 variations. An
        implementation that enumerated them would never return; this asserts
        the arithmetic as well, so a wrong shortcut fails rather than hangs."""
        cheap = priced_article("Alcool bon marché", unit_cost="1.00")
        dear = priced_article("Alcool cher", unit_cost="3.00")
        recipe = make_recipe(name="Punch de la maison", selling_price_ttc="10.00", vat_rate="0.20")
        for group in range(20):
            make_ingredient(recipe, stock_type=cheap, quantity="1", group=group)
            make_ingredient(recipe, stock_type=dear, quantity="1", group=group)
        pos = till_product("Punch de la maison", recipe=recipe, category="Cocktails")
        rang_up(pos, date(2026, 3, 12), 2, ttc="20.00", ht="16.67")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("40.00"))
        self.assertEqual(report.cogs_high, Decimal("120.00"))


class CoverageTests(TestCase):
    """A till product with no recipe has revenue and no cost. Counted as
    costed it would print a 100 % margin, which is the one way this page can
    be silently and expensively wrong."""

    def setUp(self):
        article = priced_article("Fût du comptoir", unit_cost="2.00")
        beer = make_recipe(name="Pinte du comptoir", selling_price_ttc="6.00", vat_rate="0.20")
        make_ingredient(beer, stock_type=article, quantity="0.5")  # 1,00 €
        linked = till_product("Pinte du comptoir", recipe=beer, category="Bières", typology="Liquide (Alcool)")
        rang_up(linked, date(2026, 3, 4), 10, ttc="60.00", ht="50.00")

        self.board = till_product("Planche du comptoir", category="Planches", typology="Solide")
        rang_up(self.board, date(2026, 3, 5), 4, ttc="72.00", ht="65.45")
        self.report = margins_for(MARCH)

    def test_coverage_is_the_share_of_units_actually_costed(self):
        self.assertEqual(self.report.units, 14)
        self.assertEqual(self.report.costed_units, 10)
        self.assertEqual(self.report.coverage, Decimal(10) / Decimal(14))

    def test_the_uncosted_revenue_is_stated_rather_than_taken_as_margin(self):
        self.assertEqual(self.report.revenue_uncosted, Money(Decimal("65.45"), Decimal("72.00")))
        self.assertEqual(self.report.cogs_low, Decimal("10.00"))

    def test_a_slice_with_no_costed_unit_has_no_margin_at_all(self):
        planches = self.slice_named("Planches")
        self.assertFalse(planches.is_costed)
        self.assertIsNone(planches.margin_ht_low)
        self.assertIsNone(planches.margin_ht_high)
        self.assertIsNone(planches.margin_percent_low)
        self.assertIsNone(planches.margin_percent_high)
        self.assertEqual(planches.coverage, Decimal(0))

    def test_a_fully_costed_slice_beside_it_keeps_its_margin(self):
        beers = self.slice_named("Bières")
        self.assertTrue(beers.is_costed)
        self.assertEqual(beers.margin_ht_low, Decimal("40.00"))
        self.assertEqual(beers.coverage, Decimal(1))

    def test_the_biggest_uncosted_products_are_named_to_act_on(self):
        (first,) = self.report.top_uncosted
        self.assertEqual(first.name, "Planche du comptoir")
        self.assertEqual(first.units, 4)
        self.assertEqual(first.revenue, Money(Decimal("65.45"), Decimal("72.00")))
        self.assertEqual(first.reason, "aucune recette")

    def test_a_recipe_that_costs_nothing_is_not_a_costed_recipe(self):
        """An ingredient whose article has never been bought prices at 0, so
        a recipe made only of those « costs » nothing - and printing its
        whole revenue as margin is the same lie as having no recipe at all."""
        empty_article = make_stock_type(name="Sirop jamais acheté")
        recipe = make_recipe(name="Sirop à l'eau", selling_price_ttc="3.00")
        make_ingredient(recipe, stock_type=empty_article, quantity="0.1")
        pos = till_product("Sirop à l'eau", recipe=recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 6), 5, ttc="15.00", ht="12.50")

        report = margins_for(MARCH)

        self.assertEqual(report.costed_units, 10)
        self.assertIn("Sirop à l'eau", [row.name for row in report.top_uncosted])
        self.assertEqual(
            [row.reason for row in report.top_uncosted if row.name == "Sirop à l'eau"],
            ["recette sans coût"],
        )

    def test_an_ignored_till_product_is_listed_and_marked_rather_than_hidden(self):
        """The coffee is « ignoré » on purpose - it will never have a recipe.
        Its revenue is real and its cost is not, so it still weighs on the
        coverage; hidden from the list, the page would show a gap with
        nothing accounting for it."""
        coffee = till_product("Café filtre", category="Boissons chaudes", ignored=True)
        rang_up(coffee, date(2026, 3, 7), 30, ttc="60.00", ht="54.55")

        report = margins_for(MARCH)

        (entry,) = [row for row in report.top_uncosted if row.name == "Café filtre"]
        self.assertTrue(entry.ignored)
        self.assertEqual(entry.units, 30)
        self.assertEqual(report.costed_units, 10)
        self.assertEqual(report.units, 44)

    def test_only_the_biggest_uncosted_products_are_named(self):
        """The list is there to be worked through, so it is cut - and cut by
        the MONEY, since that is what the margin is short of. Uncut, a real
        till puts two hundred rows on the page and nobody works through it."""
        for index in range(15):
            product = till_product(f"Produit sans recette {index:02d}", category="Divers")
            rang_up(product, date(2026, 3, 8), 1, ttc=str(index + 1), ht=str(index + 1))

        named = [row.name for row in margins_for(MARCH).top_uncosted]

        self.assertEqual(len(named), 12)
        self.assertEqual(named[:2], ["Planche du comptoir", "Produit sans recette 14"])
        self.assertNotIn("Produit sans recette 00", named)

    def slice_named(self, name):
        return next(slice_ for slice_ in self.report.by_category if slice_.name == name)


class RecipesWithNothingToCostTests(TestCase):
    """Two recipes this page must survive reading, because neither can be
    costed and both exist in a real database: one with no ingredients at all
    and one that contains itself."""

    def test_a_recipe_with_no_ingredients_costs_nothing_and_is_not_costed(self):
        """A recipe written down before anyone listed what goes in it. Its
        cost is not 0 € - it is unknown - so its units are uncosted and its
        revenue is stated as such, rather than printed as pure margin."""
        recipe = make_recipe(name="Recette à écrire", selling_price_ttc="7.00", vat_rate="0.20")
        pos = till_product("Recette à écrire", recipe=recipe, category="Cocktails")
        rang_up(pos, date(2026, 3, 12), 4, ttc="28.00", ht="23.33")

        report = margins_for(MARCH)

        self.assertFalse(report.is_costed)
        self.assertEqual(report.costed_units, 0)
        self.assertIsNone(report.products_margin_ht_low)
        self.assertEqual(report.revenue_uncosted, Money(Decimal("23.33"), Decimal("28.00")))
        self.assertEqual([row.reason for row in report.top_uncosted], ["recette sans coût"])

    def test_a_recipe_that_contains_itself_is_read_rather_than_recursed_into(self):
        """The ingredient form refuses a cycle (services.assert_no_cycle), so
        one only exists where rows were written another way - and this page
        merely READS recipes. The model's guard stops the recursion and
        prices the loop at 0, which lands it in « sans coût »: a page that
        recursed instead would 500 on a window nobody could narrow."""
        first = make_recipe(name="Punch A", selling_price_ttc="8.00", vat_rate="0.20")
        second = make_recipe(name="Punch B", selling_price_ttc="8.00", vat_rate="0.20")
        make_ingredient(first, sub_recipe=second, quantity="1")
        make_ingredient(second, sub_recipe=first, quantity="1")
        pos = till_product("Punch A", recipe=first, category="Cocktails")
        rang_up(pos, date(2026, 3, 13), 2, ttc="16.00", ht="13.33")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_high, Decimal("0"))
        self.assertEqual(report.costed_units, 0)
        self.assertEqual([row.name for row in report.top_uncosted], ["Punch A"])


class UnreadMoneyTests(TestCase):
    """A day imported before the export's money columns were read holds
    quantities and no money. Counting it as « took 0 € » turns its cost into
    a pure loss, so its units and its cost count and its revenue does not -
    and the report says how many days are in that state."""

    def setUp(self):
        article = priced_article("Vodka", unit_cost="20.00")
        recipe = make_recipe(name="Vodka tonic", selling_price_ttc="8.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=article, quantity="0.04")  # 0,80 €
        self.pos = till_product("Vodka tonic", recipe=recipe, category="Cocktails")
        rang_up(self.pos, date(2026, 3, 2), 10, ttc="80.00", ht="66.67")
        rang_up(self.pos, date(2026, 3, 3), 5, read=False)
        self.report = margins_for(MARCH)

    def test_the_unread_day_brings_its_units_but_no_revenue(self):
        self.assertEqual(self.report.units, 15)
        self.assertEqual(self.report.revenue_till, Money(Decimal("66.67"), Decimal("80.00")))

    def test_its_cost_still_counts(self):
        self.assertEqual(self.report.cogs_low, Decimal("12.00"))
        self.assertEqual(self.report.cogs_high, Decimal("12.00"))

    def test_the_gap_is_counted_where_the_page_can_say_it(self):
        self.assertEqual(self.report.unread_days, 1)
        self.assertEqual(self.report.unread_units, 5)

    def test_the_flag_decides_and_not_the_amount_beside_it(self):
        """An unread row holds 0,00 € today, so counting its money or not
        comes to the same on real data - which is exactly why this pins the
        RULE. A row with an amount and `revenue_read` False is what a
        half-run backfill, or an archive restored on top of one, leaves
        behind; read, it would silently double that day."""
        rang_up(self.pos, date(2026, 3, 4), 2, ttc="16.00", ht="13.33", read=False)

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_till, Money(Decimal("66.67"), Decimal("80.00")))
        self.assertEqual(report.units, 17)
        self.assertEqual(report.unread_days, 2)
        self.assertEqual(report.unread_units, 7)

    def test_a_rate_nobody_read_on_an_unread_day_is_not_declared_either(self):
        rang_up(self.pos, date(2026, 3, 5), 1, ttc="8.00", ht="0.00", without_rate="8.00", read=False)

        self.assertEqual(margins_for(MARCH).revenue_without_rate_ttc, Decimal("0"))


class CompedAndRefundedTests(TestCase):
    def setUp(self):
        article = priced_article("Rhum ambré", unit_cost="25.00")
        self.recipe = make_recipe(name="Punch du comptoir", selling_price_ttc="9.00", vat_rate="0.20")
        make_ingredient(self.recipe, stock_type=article, quantity="0.05")  # 1,25 €
        self.pos = till_product("Punch du comptoir", recipe=self.recipe, category="Cocktails")

    def test_a_comped_drink_costs_its_stock_and_brings_no_money(self):
        """L'Addition prices an « offert » line at 0, which is exactly what a
        margin wants: the bottle emptied and nothing came in."""
        rang_up(self.pos, date(2026, 3, 7), 4, ttc="36.00", ht="30.00")
        rang_up(self.pos, date(2026, 3, 8), 1, ttc="0.00", ht="0.00")

        report = margins_for(MARCH)

        self.assertEqual(report.units, 5)
        self.assertEqual(report.revenue_till, Money(Decimal("30.00"), Decimal("36.00")))
        self.assertEqual(report.cogs_low, Decimal("6.25"))
        self.assertEqual(report.products_margin_ht_low, Decimal("23.75"))

    def test_a_refund_is_negative_revenue_and_stays_negative(self):
        rang_up(self.pos, date(2026, 3, 9), 1, ttc="-9.00", ht="-7.50")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_till, Money(Decimal("-7.50"), Decimal("-9.00")))
        self.assertEqual(report.products_margin_ht_low, Decimal("-8.75"))

    def test_a_percentage_of_a_negative_revenue_is_no_percentage(self):
        """-8,75 € of margin on -7,50 € of revenue works out to +116 %. A
        loss printed as a gain is worse than no figure at all."""
        rang_up(self.pos, date(2026, 3, 9), 1, ttc="-9.00", ht="-7.50")

        report = margins_for(MARCH)

        self.assertIsNone(report.products_margin_percent_low)
        self.assertIsNone(report.real_margin_percent)


class SpendTests(TestCase):
    """« Toutes les dépenses »: the goods and the charges, told apart by
    Supplier.expenses_only and added up the same way every other page adds a
    document up - its own two totals, each to the cent."""

    def setUp(self):
        self.goods = make_supplier(name="Grossiste boissons")
        self.charges = make_supplier(name="Assurance du bar", expenses_only=True)

    def test_goods_and_charges_are_two_figures_and_one_total(self):
        goods_invoice(date(2026, 3, 3), total_ht="100.00", supplier=self.goods)
        goods_invoice(date(2026, 3, 4), total_ht="50.00", vat_rate="0.055", supplier=self.goods)
        goods_invoice(date(2026, 3, 10), total_ht="200.00", supplier=self.charges)

        report = margins_for(MARCH)

        self.assertEqual(report.spend_goods, Money(Decimal("150.00"), Decimal("172.75")))
        self.assertEqual(report.spend_charges, Money(Decimal("200.00"), Decimal("240.00")))
        self.assertEqual(report.spend, Money(Decimal("350.00"), Decimal("412.75")))

    def test_a_charge_counts_in_the_real_margin_and_not_in_the_products_one(self):
        article = priced_article("Jus de kiwi", unit_cost="3.00")
        recipe = make_recipe(name="Jus pressé", selling_price_ttc="4.00", vat_rate="0.10")
        make_ingredient(recipe, stock_type=article, quantity="0.2")  # 0,60 €
        pos = till_product("Jus pressé", recipe=recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 11), 100, ttc="400.00", ht="363.64")
        goods_invoice(date(2026, 3, 10), total_ht="200.00", supplier=self.charges)

        report = margins_for(MARCH)

        self.assertEqual(report.real_margin_ht, Decimal("163.64"))
        self.assertEqual(report.products_margin_ht_low, Decimal("303.64"))

    def test_an_undated_invoice_is_in_no_window_and_is_counted_apart(self):
        goods_invoice(date(2026, 3, 3), total_ht="100.00", supplier=self.goods)
        undated(self.goods, "40.00")

        report = margins_for(MARCH)

        self.assertEqual(report.spend.ht, Decimal("100.00"))
        self.assertEqual(report.undated_invoices, 1)
        self.assertEqual(report.undated_spend, Money(Decimal("40.00"), Decimal("48.00")))
        self.assertFalse(report.undated_in_spend)

    def test_without_a_window_an_undated_invoice_is_part_of_all_the_spending(self):
        undated(self.goods, "40.00")

        report = margins_for(DateRange())

        self.assertEqual(report.spend.ht, Decimal("40.00"))
        self.assertEqual(report.undated_invoices, 1)
        self.assertTrue(report.undated_in_spend)

    def test_each_document_is_rounded_to_the_cent_before_it_is_added(self):
        """Twelve bills of 29,99 € HT at 20 % print 35,99 €, and a page
        listing them must foot to what it prints - raw, the sum is 431,86 €
        under twelve lines saying 35,99 €."""
        for _ in range(12):
            goods_invoice(date(2026, 3, 6), total_ht="29.99", supplier=self.charges)

        report = margins_for(MARCH)

        self.assertEqual(report.spend_charges.ttc, Decimal("431.88"))


class FlaggedArticleTests(TestCase):
    """« Compter dans la marge produits »: an article no recipe consumes -
    the paper towels - whose PURCHASES over the window join the products
    margin's cost, because there is no recipe to consume them and what was
    bought is the only measure there is."""

    def setUp(self):
        article = priced_article("Limonade", unit_cost="2.00")
        recipe = make_recipe(name="Limonade au verre", selling_price_ttc="5.00", vat_rate="0.10")
        make_ingredient(recipe, stock_type=article, quantity="0.25")  # 0,50 €
        pos = till_product("Limonade au verre", recipe=recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 15), 20, ttc="100.00", ht="90.91")

        self.towels = make_stock_type(name="Essuie-tout", count_in_products_margin=True)
        self.cups = make_stock_type(name="Gobelets")

    def bought(self, article, day, total_ht, vat_rate="0.20"):
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

    def test_a_flagged_article_joins_the_products_margin_at_what_was_bought(self):
        self.bought(self.towels, date(2026, 3, 2), "30.00")

        report = margins_for(MARCH)

        self.assertEqual(report.extra_products_ht, Decimal("30.00"))
        self.assertEqual(report.extra_products_ttc, Decimal("36.00"))
        self.assertEqual(report.cogs_low, Decimal("10.00"))
        self.assertEqual(report.products_margin_ht_low, Decimal("50.91"))
        self.assertEqual([row.name for row in report.extra_products], ["Essuie-tout"])

    def test_an_unflagged_article_stays_out_of_it(self):
        self.bought(self.cups, date(2026, 3, 2), "25.00")

        report = margins_for(MARCH)

        self.assertEqual(report.extra_products_ht, Decimal("0"))
        self.assertEqual(report.products_margin_ht_low, Decimal("80.91"))

    def test_only_the_purchases_inside_the_window_count(self):
        self.bought(self.towels, date(2026, 2, 27), "99.00")
        self.bought(self.towels, date(2026, 3, 2), "30.00")
        self.bought(self.towels, date(2026, 4, 2), "99.00")

        self.assertEqual(margins_for(MARCH).extra_products_ht, Decimal("30.00"))

    def test_a_purchase_counts_the_line_s_own_total_not_the_unit_cost_stored_beside_it(self):
        """2 000 pailles charged 24,64 € store a unit cost of 0,0123 € - four
        decimals, which is all the column has - and 2 000 x 0,0123 is 24,60 €.
        The four cents are the whole reason CLAUDE.md says a purchase is the
        invoice line's own `total_ht`; on a year of consumables the same
        rounding walks off by euros, in the direction that flatters the
        margin."""
        invoice = make_invoice(invoice_date=date(2026, 3, 3))
        product = make_product(supplier=invoice.supplier, stock_type=self.towels)
        line = make_invoice_line(
            invoice=invoice, product=product, quantity=2000, total_ht="24.64",
            vat_rate=Decimal("0.20"),
        )
        movement = make_movement(
            stock_type=self.towels, quantity="2000", unit_cost_ht=Decimal("24.64") / 2000,
            invoice_line=line, kind=MovementKind.PURCHASE, occurred_on=date(2026, 3, 3),
        )
        movement.refresh_from_db()

        report = margins_for(MARCH)

        self.assertEqual(movement.quantity * movement.unit_cost_ht, Decimal("24.6000000"))
        self.assertEqual(report.extra_products_ht, Decimal("24.64"))

    def test_a_deposit_given_back_takes_the_figure_down_rather_than_up(self):
        """« Consigne fûts » flagged is signed both ways: the kegs returned
        are a negative purchase, and a figure that took their absolute value
        would charge the bar twice for barrels it gave back."""
        self.bought(self.towels, date(2026, 3, 2), "30.00")
        self.bought(self.towels, date(2026, 3, 9), "-18.00")

        report = margins_for(MARCH)

        self.assertEqual(report.extra_products_ht, Decimal("12.00"))
        self.assertEqual(report.products_margin_ht_low, Decimal("68.91"))

    def test_a_known_loss_is_not_a_purchase(self):
        """Only what was BOUGHT counts here. A broken pack written off is
        stock already paid for, and counted again it is paid for twice."""
        self.bought(self.towels, date(2026, 3, 2), "30.00")
        make_movement(
            stock_type=self.towels, quantity="-2", unit_cost_ht="5.00",
            kind=MovementKind.LOSS, occurred_on=date(2026, 3, 3),
        )

        self.assertEqual(margins_for(MARCH).extra_products_ht, Decimal("30.00"))

    def test_a_purchase_with_no_date_of_its_own_counts_on_its_invoice_s_date(self):
        """`StockMovement.effective_date` falls back on the invoice, and this
        page reproduces that fallback in SQL rather than reading the property
        row by row - so the two have to be shown to agree, or a delivery with
        no `occurred_on` lands in no window at all and its purchase vanishes."""
        invoice = make_invoice(invoice_date=date(2026, 3, 6))
        product = make_product(supplier=invoice.supplier, stock_type=self.towels)
        line = make_invoice_line(
            invoice=invoice, product=product, quantity=1, total_ht="21.00", vat_rate=Decimal("0.20")
        )
        make_movement(
            stock_type=self.towels, quantity="1", unit_cost_ht="21.00", invoice_line=line,
            kind=MovementKind.PURCHASE,
        )

        self.assertEqual(margins_for(MARCH).extra_products_ht, Decimal("21.00"))
        self.assertEqual(
            margins_for(DateRange(date(2026, 4, 1), date(2026, 4, 30))).extra_products_ht, Decimal("0")
        )

    def test_a_purchase_that_printed_its_own_ttc_is_counted_at_what_it_printed(self):
        """33,33 € HT at 20 % works back out to 40,00 € where the ticket says
        39,99 €, and 39,99 € is what left the bank - the rule every other
        page of this app follows (InvoiceLine.total_ttc, the charges fold,
        the article panel)."""
        invoice = make_invoice(invoice_date=date(2026, 3, 5))
        product = make_product(supplier=invoice.supplier, stock_type=self.towels)
        line = make_invoice_line(
            invoice=invoice, product=product, quantity=1, total_ht="33.33",
            vat_rate=Decimal("0.20"), printed_ttc=Decimal("39.99"),
        )
        make_movement(
            stock_type=self.towels, quantity="1", unit_cost_ht="33.33", invoice_line=line,
            kind=MovementKind.PURCHASE, occurred_on=date(2026, 3, 5),
        )

        (entry,) = margins_for(MARCH).extra_products

        self.assertEqual(entry.ht, Decimal("33.33"))
        self.assertEqual(entry.ttc, Decimal("39.99"))

    def test_a_movement_typed_by_hand_has_an_ht_and_no_ttc_invented_for_it(self):
        """A correction with no invoice line behind it has no VAT rate
        anywhere. Its HT counts - something really was bought - and its TTC
        is left alone rather than worked out at a rate nobody stated."""
        make_movement(
            stock_type=self.towels, quantity="3", unit_cost_ht="2.00",
            kind=MovementKind.PURCHASE, occurred_on=date(2026, 3, 4),
        )

        report = margins_for(MARCH)

        (entry,) = report.extra_products
        self.assertEqual(entry.ht, Decimal("6.00"))
        self.assertEqual(entry.ttc, Decimal("0"))
        self.assertEqual(report.extra_products_ht, Decimal("6.00"))

    def test_the_flagged_purchases_belong_to_no_till_category(self):
        """They were never rung up, so no slice can hold them - the report
        carries them and the page says where they went."""
        self.bought(self.towels, date(2026, 3, 2), "30.00")

        report = margins_for(MARCH)

        self.assertEqual(
            sum((slice_.cost_ht_high for slice_ in report.by_category), start=Decimal("0")),
            Decimal("10.00"),
        )


class SaleDocumentTests(TestCase):
    """A sale made off the till - a tab settled by hand, a private event -
    is income too, and what it sold cost the same as anything else."""

    def setUp(self):
        article = priced_article("Champagne", unit_cost="40.00")
        self.recipe = make_recipe(name="Coupe de champagne", selling_price_ttc="9.00", vat_rate="0.20")
        make_ingredient(self.recipe, stock_type=article, quantity="0.1")  # 4,00 €
        self.document = SaleDocument.objects.create(sold_on=date(2026, 3, 20), reference="Soirée privée")

    def test_its_revenue_joins_the_till_and_its_ht_comes_from_the_recipe_rate(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity="10")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("75.00"), Decimal("90.00")))
        self.assertEqual(report.revenue_till, Money())
        self.assertEqual(report.revenue, Money(Decimal("75.00"), Decimal("90.00")))

    def test_what_it_sold_costs_what_the_recipe_costs(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity="10")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("40.00"))
        self.assertEqual(report.products_margin_ht_low, Decimal("35.00"))

    def test_a_line_priced_by_hand_wins_over_the_recipe_price(self):
        SaleDocumentLine.objects.create(
            document=self.document, recipe=self.recipe, quantity="10", unit_price_ttc="6.00"
        )

        self.assertEqual(margins_for(MARCH).revenue_documents.ttc, Decimal("60.00"))

    def test_a_recipe_taxed_at_minus_one_does_not_divide_by_zero(self):
        """A rate of exactly -1 makes (1 + rate) zero. The validators keep it
        out of anything saved through a form, but a row written by a raw
        update must not be able to 500 a page that merely reads it -
        Recipe._vat_divisor is that guard and this page goes through it."""
        from recipes.models import Recipe

        Recipe.objects.filter(pk=self.recipe.pk).update(vat_rate=Decimal("-1"))
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity="10")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents.ttc, Decimal("90.00"))
        self.assertEqual(report.revenue_documents.ht, Decimal("90.00"))

    def test_an_article_sold_as_itself_has_no_vat_rate_and_says_so(self):
        """A stock item sold as itself carries no rate anywhere - a recipe
        has one, an article does not. Its money stays TTC and is declared:
        converted at the drink rate « to have an HT », it would invent 20 %
        of an amount nothing supports."""
        article = priced_article("Bouteille entière", unit_cost="12.00")
        SaleDocumentLine.objects.create(
            document=self.document, stock_type=article, quantity="2", unit_price_ttc="30.00"
        )

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_documents, Money(Decimal("0"), Decimal("60.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("60.00"))

    def test_an_article_sold_as_itself_costs_nothing_here_because_it_earns_no_ht(self):
        """Its purchase price is known and its revenue's HT is not, so the
        cost has nothing to be taken off. Counted anyway, it came off an HT
        that money never joined: two bottles bought at 12 € and sold at
        30 € printed a products margin of **-24,00 €** - a profitable sale
        shown as a loss. Both sides stay out, and the foot of the page names
        the amount."""
        article = priced_article("Bouteille entière", unit_cost="12.00")
        SaleDocumentLine.objects.create(
            document=self.document, stock_type=article, quantity="2", unit_price_ttc="30.00"
        )

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("0"))
        self.assertEqual(report.cogs_high, Decimal("0"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("0"), Decimal("60.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("60.00"))
        self.assertIsNone(report.products_margin_ht_low)

    def test_an_article_nobody_has_ever_bought_is_uncosted_revenue(self):
        """No movement behind it means no price behind it, and counted as
        costed it would print its whole amount as margin - the same lie as a
        till product with no recipe."""
        never_bought = make_stock_type(name="Article jamais acheté")
        SaleDocumentLine.objects.create(
            document=self.document, stock_type=never_bought, quantity="1", unit_price_ttc="5.00"
        )

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("0"))
        self.assertEqual(report.revenue_uncosted, Money(Decimal("0"), Decimal("5.00")))

    def test_a_till_sale_recorded_as_a_recipe_sale_is_not_hand_typed(self):
        """`record_sales` writes a RecipeSale for every till sale too, so
        « saisi à la main » is « from another source than the till » and
        nothing else. Counted whole, this figure would say the bar sells
        everything twice - once priced, once not."""
        RecipeSale.objects.create(
            recipe=self.recipe, sold_on=date(2026, 3, 21), source="laddition", quantity=40
        )
        RecipeSale.objects.create(
            recipe=self.recipe, sold_on=date(2026, 3, 22), source="manual", quantity=3
        )

        self.assertEqual(margins_for(MARCH).hand_typed_units, 3)

    def test_a_document_outside_the_window_is_out(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity="10")
        self.assertEqual(margins_for(DateRange(date(2026, 4, 1), date(2026, 4, 30))).revenue, Money())

    def test_a_sale_typed_by_hand_on_a_recipe_carries_no_price_and_is_counted_apart(self):
        """RecipeSale from another source than the till has no money attached
        anywhere. Costed here it would read as a loss; ignored silently it
        would be stock leaving with nothing said."""
        RecipeSale.objects.create(
            recipe=self.recipe, sold_on=date(2026, 3, 21), source="manual", quantity=7
        )

        report = margins_for(MARCH)

        self.assertEqual(report.hand_typed_units, 7)
        self.assertEqual(report.cogs_low, Decimal("0"))
        self.assertEqual(report.revenue, Money())


class EmptyWindowTests(TestCase):
    """Nothing sold, nothing bought: every figure is zero and every
    percentage is None. Nothing divided by nothing is not « 0 % » - printed,
    it says the bar lost everything it took."""

    def setUp(self):
        self.report = margins_for(MARCH)

    def test_every_amount_is_zero(self):
        self.assertEqual(self.report.revenue, Money())
        self.assertEqual(self.report.spend, Money())
        self.assertEqual(self.report.cogs_low, Decimal("0"))
        self.assertEqual(self.report.cogs_high, Decimal("0"))
        self.assertEqual(self.report.extra_products_ht, Decimal("0"))
        self.assertEqual(self.report.real_margin_ht, Decimal("0"))

    def test_every_percentage_is_none_rather_than_zero(self):
        self.assertIsNone(self.report.real_margin_percent)
        self.assertIsNone(self.report.products_margin_percent_low)
        self.assertIsNone(self.report.products_margin_percent_high)
        self.assertIsNone(self.report.coverage)
        self.assertIsNone(self.report.revenue_coverage)

    def test_there_is_no_products_margin_when_nothing_is_costed(self):
        self.assertFalse(self.report.is_costed)
        self.assertIsNone(self.report.products_margin_ht_low)
        self.assertIsNone(self.report.products_margin_ht_high)

    def test_the_lists_are_empty_rather_than_absent(self):
        self.assertEqual(self.report.by_category, [])
        self.assertEqual(self.report.by_typology, [])
        self.assertEqual(self.report.top_uncosted, [])
        self.assertEqual(self.report.extra_products, [])

    def test_the_window_comes_back_with_the_report(self):
        self.assertEqual(self.report.window, MARCH)


class DivisionByZeroTests(TestCase):
    """Every percentage on this page divides by something that can be zero -
    a window with no revenue, a slice with no unit, a refund-only day."""

    def test_a_window_that_only_spent_has_no_percentage_but_a_real_margin(self):
        goods_invoice(date(2026, 3, 3), total_ht="100.00")

        report = margins_for(MARCH)

        self.assertEqual(report.real_margin_ht, Decimal("-100.00"))
        self.assertIsNone(report.real_margin_percent)

    def test_a_slice_whose_revenue_is_nil_keeps_its_margin_and_loses_its_percentage(self):
        article = priced_article("Sirop offert", unit_cost="2.00")
        recipe = make_recipe(name="Sirop à l'eau offert", selling_price_ttc="0.00")
        make_ingredient(recipe, stock_type=article, quantity="0.1")  # 0,20 €
        pos = till_product("Sirop à l'eau offert", recipe=recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 4), 3, ttc="0.00", ht="0.00")

        (slice_,) = margins_for(MARCH).by_category

        self.assertEqual(slice_.margin_ht_low, Decimal("-0.60"))
        self.assertIsNone(slice_.margin_percent_low)
        self.assertIsNone(slice_.margin_percent_high)

    def test_a_slice_with_no_unit_at_all_has_no_coverage(self):
        from margins.computation import Slice

        self.assertIsNone(Slice(name="Vide").coverage)


class TillWithoutARateTests(TestCase):
    """A line whose rate the export did not state has a TTC and no HT. The
    HT must not pretend otherwise, and the page has to be able to say how
    much money that is."""

    def test_the_ttc_with_no_ht_behind_it_is_reported(self):
        pos = till_product("Article divers", category="Divers")
        rang_up(pos, date(2026, 3, 5), 2, ttc="12.00", ht="0.00", without_rate="12.00")

        report = margins_for(MARCH)

        self.assertEqual(report.revenue_till, Money(Decimal("0.00"), Decimal("12.00")))
        self.assertEqual(report.revenue_without_rate_ttc, Decimal("12.00"))


class GroupingTests(TestCase):
    """The 13 categories and the 3 typologies are two readings of the same
    money, so they add up to the same totals."""

    def setUp(self):
        article = priced_article("Bière", unit_cost="2.00")
        beer = make_recipe(name="Pinte", selling_price_ttc="6.00", vat_rate="0.20")
        make_ingredient(beer, stock_type=article, quantity="0.5")  # 1,00 €
        pinte = till_product("Pinte", recipe=beer, category="Bières", typology="Liquide (Alcool)")
        shot = till_product("Shot", recipe=beer, category="Shots", typology="Liquide (Alcool)")
        board = till_product("Planche", category="Planches", typology="Solide")
        rang_up(pinte, date(2026, 3, 4), 10, ttc="60.00", ht="50.00")
        rang_up(shot, date(2026, 3, 4), 6, ttc="24.00", ht="20.00")
        rang_up(board, date(2026, 3, 4), 2, ttc="36.00", ht="32.73")
        self.report = margins_for(MARCH)

    def test_both_dimensions_foot_to_the_same_revenue(self):
        for slices in (self.report.by_category, self.report.by_typology):
            self.assertEqual(
                sum((slice_.revenue.ht for slice_ in slices), start=Decimal("0")), Decimal("102.73")
            )
            self.assertEqual(sum(slice_.units for slice_ in slices), 18)

    def test_the_typology_folds_the_categories_the_owner_asked_about(self):
        drinks = next(s for s in self.report.by_typology if s.name == "Liquide (Alcool)")
        food = next(s for s in self.report.by_typology if s.name == "Solide")
        self.assertEqual(drinks.units, 16)
        self.assertEqual(drinks.revenue.ht, Decimal("70.00"))
        self.assertEqual(drinks.cost_ht_high, Decimal("16.00"))
        self.assertEqual(food.units, 2)
        self.assertFalse(food.is_costed)

    def test_a_till_product_with_no_category_is_named_rather_than_dropped(self):
        """And named something the till cannot also print: it has a category
        of its own called « _Sans catégorie » and a typology called « N/D »,
        so « Sans catégorie » would sit beside it an underscore apart."""
        stray = till_product("Divers")
        labelled = till_product("Divers maison", category="_Sans catégorie", typology="N/D")
        rang_up(stray, date(2026, 3, 5), 1, ttc="1.00", ht="0.91")
        rang_up(labelled, date(2026, 3, 5), 1, ttc="2.00", ht="1.82")

        report = margins_for(MARCH)

        names = [slice_.name for slice_ in report.by_category]
        self.assertIn("Catégorie non renseignée", names)
        self.assertIn("_Sans catégorie", names)
        self.assertIn("Typologie non renseignée", [slice_.name for slice_ in report.by_typology])

    def test_the_biggest_category_comes_first(self):
        self.assertEqual([slice_.name for slice_ in self.report.by_category], ["Bières", "Planches", "Shots"])


class LinearityTests(TestCase):
    """This page reads every recipe sold and every one of their ingredients.
    A query per recipe - which is what asking a recipe for its own groups
    costs outside a variation_scope - is how the other two pages of this app
    reached 290 queries."""

    def setUp(self):
        self.article = priced_article("Article commun", unit_cost="2.00")
        self.sold = 0

    def sell(self, count):
        for _ in range(count):
            self.sold += 1
            recipe = make_recipe(name=f"Recette {self.sold}", selling_price_ttc="6.00")
            make_ingredient(recipe, stock_type=self.article, quantity="0.5")
            pos = till_product(f"Produit {self.sold}", recipe=recipe, category="Bières")
            rang_up(pos, date(2026, 3, 4), 1, ttc="6.00", ht="5.00")

    def test_costing_three_times_as_many_recipes_costs_no_more_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.sell(3)
        with CaptureQueriesContext(connection) as few:
            margins_for(MARCH)
        self.sell(9)
        with CaptureQueriesContext(connection) as many:
            report = margins_for(MARCH)

        self.assertEqual(report.costed_units, 12)
        self.assertEqual(len(many), len(few))

    def test_a_sub_recipe_reached_by_every_recipe_is_read_once(self):
        """Checking that every article of a recipe has a price walks its
        sub-recipes too. Asked for their ingredients directly, that is one
        query per recipe REACHING the sub-recipe - the N+1 this page's whole
        costing pass is written around."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests.factories import make_ingredient, make_recipe

        preparation = make_recipe(name="Préparation partagée", selling_price_ttc="1")
        make_ingredient(preparation, stock_type=self.article, quantity="0.1")

        def sell_using_it(count):
            for _ in range(count):
                self.sold += 1
                recipe = make_recipe(name=f"Recette partagée {self.sold}", selling_price_ttc="6.00")
                make_ingredient(recipe, sub_recipe=preparation, quantity="1", group=1)
                make_ingredient(recipe, stock_type=self.article, quantity="0.5", group=2)
                pos = till_product(f"Produit partagé {self.sold}", recipe=recipe, category="Bières")
                rang_up(pos, date(2026, 3, 4), 1, ttc="6.00", ht="5.00")

        sell_using_it(3)
        with CaptureQueriesContext(connection) as few:
            margins_for(MARCH)
        sell_using_it(9)
        with CaptureQueriesContext(connection) as many:
            report = margins_for(MARCH)

        self.assertEqual(report.costed_units, 12)
        self.assertEqual(len(many), len(few))


class CostPerServingTests(TestCase):
    """What ONE sale consumed, never what one preparation costs.

    `Recipe.summary` prices a whole run of the recipe: a syrup made ten
    glasses at a time costs its batch, not a glass. The variance report has
    divided by `yield_quantity` since it was written
    (inventory/variance.py) - a cogs that does not charges a sale ten times
    what it ate, and puts two pages of this app a factor of ten apart with
    nothing on either saying why.
    """

    def setUp(self):
        sugar = priced_article("Sucre roux", unit_cost="2.00", quantity="1000")
        self.recipe = make_recipe(
            name="Sirop de verveine", selling_price_ttc="4.00", vat_rate="0.20", yield_quantity="10"
        )
        make_ingredient(self.recipe, stock_type=sugar, quantity="10")  # 20,00 € la préparation

    def test_a_recipe_that_yields_ten_costs_a_tenth_of_its_batch_per_sale(self):
        pos = till_product("Verre de sirop", recipe=self.recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 10), 5, ttc="20.00", ht="16.67")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_low, Decimal("10.00"))
        self.assertEqual(report.cogs_high, Decimal("10.00"))
        self.assertEqual(report.products_margin_ht_low, Decimal("6.67"))

    def test_the_variance_report_and_the_margin_agree_on_what_a_sale_ate(self):
        """The same five glasses, priced by the engine that actually takes
        the stock out. Two answers here is two pages of one app disagreeing
        about one sale."""
        pos = till_product("Verre de sirop", recipe=self.recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 10), 5, ttc="20.00", ht="16.67")

        per_serving = self.recipe.unit_cost_bounds()

        self.assertEqual(margins_for(MARCH).cogs_high, per_serving[1] * 5)

    def test_a_recipe_yielding_less_than_one_costs_more_per_sale(self):
        """The validator allows a yield down to 0,0001, so the division is
        not always the safe direction: half a batch per sale costs double,
        and ignoring the yield would print the flattering half."""
        Recipe.objects.filter(pk=self.recipe.pk).update(yield_quantity=Decimal("0.5"))
        pos = till_product("Double dose", recipe=self.recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 10), 1, ttc="4.00", ht="3.33")

        self.assertEqual(margins_for(MARCH).cogs_high, Decimal("40.00"))

    def test_a_recipe_yielding_nothing_at_all_is_not_costed(self):
        """A yield of 0 has no per-serving cost to state - `unit_cost_bounds`
        refuses to divide by it. Counted at its batch price instead, one
        sale would eat the whole preparation."""
        Recipe.objects.filter(pk=self.recipe.pk).update(yield_quantity=Decimal("0"))
        pos = till_product("Verre de sirop", recipe=self.recipe, category="Sans alcool")
        rang_up(pos, date(2026, 3, 10), 5, ttc="20.00", ht="16.67")

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_high, Decimal("0"))
        self.assertEqual(report.costed_units, 0)


class IngredientWithoutAPriceTests(TestCase):
    """A recipe is costed only when ALL of it is.

    An article nobody has ever been invoiced for prices at 0
    (`StockType.current_unit_cost_ht` with no movement behind it), so a
    recipe holding one is short of exactly that ingredient - and prints the
    shortfall as margin, at « 100 % chiffré », with nothing anywhere saying
    a third of the cost is missing. The same all-or-nothing rule as a recipe
    whose WHOLE cost is 0, which this page already refuses.
    """

    def setUp(self):
        spirit = priced_article("Alcool clair", unit_cost="2.00", quantity="100")
        juice = priced_article("Jus de kiwi", unit_cost="1.00", quantity="100")
        self.never_bought = make_stock_type(name="Sirop jamais facturé")
        self.recipe = make_recipe(name="Long drink", selling_price_ttc="12.00", vat_rate="0.20")
        # Distinct groups: ingredients sharing one group are ALTERNATIVES,
        # and this recipe pours both.
        make_ingredient(self.recipe, stock_type=spirit, quantity="1", group=1)
        make_ingredient(self.recipe, stock_type=juice, quantity="1", group=2)
        self.pos = till_product("Long drink", recipe=self.recipe, category="Cocktails")
        rang_up(self.pos, date(2026, 3, 10), 100, ttc="1200.00", ht="1000.00")

    def test_with_every_ingredient_priced_the_recipe_is_costed(self):
        report = margins_for(MARCH)

        self.assertEqual(report.cogs_high, Decimal("300.00"))
        self.assertEqual(report.costed_units, 100)

    def test_one_ingredient_with_no_price_leaves_the_whole_recipe_uncosted(self):
        make_ingredient(self.recipe, stock_type=self.never_bought, quantity="1", group=3)

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_high, Decimal("0"))
        self.assertEqual(report.costed_units, 0)
        self.assertEqual(report.coverage, Decimal(0))
        self.assertEqual(
            [(row.name, row.reason) for row in report.top_uncosted],
            [("Long drink", "ingrédient sans prix")],
        )

    def test_the_margin_it_would_have_printed_is_the_measure_of_the_lie(self):
        """Two thirds of the cost known reads as a 300,00 € cost on 1 000 €
        of sales - a 700,00 € margin where the truth is unknown and lower.
        Nothing is stated instead."""
        make_ingredient(self.recipe, stock_type=self.never_bought, quantity="1", group=3)

        report = margins_for(MARCH)

        self.assertIsNone(report.products_margin_ht_low)
        self.assertEqual(report.revenue_uncosted, Money(Decimal("1000.00"), Decimal("1200.00")))

    def test_a_sub_recipe_with_an_unpriced_ingredient_carries_up(self):
        """A recipe made of another recipe is only as costed as that one."""
        preparation = make_recipe(name="Préparation maison", selling_price_ttc="1", vat_rate="0.20")
        make_ingredient(preparation, stock_type=priced_article("Eau plate en carafe", unit_cost="1.00"), quantity="1", group=1)
        make_ingredient(preparation, stock_type=self.never_bought, quantity="1", group=2)
        make_ingredient(self.recipe, sub_recipe=preparation, quantity="1", group=3)

        report = margins_for(MARCH)

        self.assertEqual(report.costed_units, 0)


class FlaggedArticleAlsoInARecipeTests(TestCase):
    """An article both ticked « compter dans la marge produits » AND used in
    a recipe is paid for twice: once as what the recipe consumed, once as
    what was bought of it. The article's own help text says not to do it,
    and nothing checked - so the page could print a negative products margin
    with no explanation anywhere on it.
    """

    def setUp(self):
        self.syrup = priced_article("Sirop de verveine", unit_cost="2.00", quantity="100")
        recipe = make_recipe(name="Cocktail verveine", selling_price_ttc="12.00", vat_rate="0.20")
        make_ingredient(recipe, stock_type=self.syrup, quantity="0.1")  # 0,20 €
        pos = till_product("Cocktail verveine", recipe=recipe, category="Cocktails")
        rang_up(pos, date(2026, 3, 10), 100, ttc="1200.00", ht="1000.00")

    def tick(self, article):
        StockType.objects.filter(pk=article.pk).update(count_in_products_margin=True)

    def test_an_article_ticked_and_used_in_a_recipe_is_named(self):
        self.tick(self.syrup)

        report = margins_for(MARCH)

        self.assertEqual(report.flagged_in_recipes, ["Sirop de verveine"])

    def test_an_article_ticked_and_in_no_recipe_is_not(self):
        towels = make_stock_type(name="Essuie-tout")
        self.tick(towels)

        self.assertEqual(margins_for(MARCH).flagged_in_recipes, [])

    def test_a_recipe_ingredient_nobody_ticked_is_not(self):
        self.assertEqual(margins_for(MARCH).flagged_in_recipes, [])

    def test_the_double_count_is_real_and_not_just_named(self):
        """20,00 € consumed and 200,00 € bought, of one article: the figure
        the page prints is short by the smaller of the two, and the page has
        to say which."""
        self.tick(self.syrup)
        invoice = make_invoice(invoice_date=date(2026, 3, 2))
        product = make_product(supplier=invoice.supplier, stock_type=self.syrup)
        line = make_invoice_line(invoice=invoice, product=product, quantity=1, total_ht="200.00")
        make_movement(
            stock_type=self.syrup, quantity="100", unit_cost_ht="2.00", invoice_line=line,
            kind=MovementKind.PURCHASE, occurred_on=date(2026, 3, 2),
        )

        report = margins_for(MARCH)

        self.assertEqual(report.cogs_high, Decimal("20.00"))
        self.assertEqual(report.extra_products_ht, Decimal("200.00"))
        self.assertEqual(report.flagged_in_recipes, ["Sirop de verveine"])


class WhenAPurchaseCountsTests(TestCase):
    """The two margins must date one purchase on one day.

    « La marge réelle » counts an invoice on its own date - what was
    invoiced, not what was delivered. A flagged article's purchase is part
    of the OTHER margin, and dating it by the delivery instead put one
    document in February on one figure and in March on the next, on the same
    page, with nothing saying so.
    """

    def setUp(self):
        self.towels = make_stock_type(name="Essuie-tout", count_in_products_margin=True)

    def delivered(self, invoice_date, occurred_on, total_ht="50.00"):
        invoice = make_invoice(invoice_date=invoice_date)
        product = make_product(supplier=invoice.supplier, stock_type=self.towels)
        line = make_invoice_line(invoice=invoice, product=product, quantity=1, total_ht=total_ht)
        return make_movement(
            stock_type=self.towels, quantity="1", unit_cost_ht=total_ht, invoice_line=line,
            kind=MovementKind.PURCHASE, occurred_on=occurred_on,
        )

    def test_invoiced_in_february_and_received_in_march_counts_in_february(self):
        self.delivered(date(2026, 2, 25), date(2026, 3, 10))

        february = margins_for(DateRange(date(2026, 2, 1), date(2026, 2, 28)))
        march = margins_for(MARCH)

        self.assertEqual(february.spend.ht, Decimal("50.00"))
        self.assertEqual(february.extra_products_ht, Decimal("50.00"))
        self.assertEqual(march.spend.ht, Decimal("0"))
        self.assertEqual(march.extra_products_ht, Decimal("0"))

    def test_a_movement_with_no_invoice_behind_it_keeps_its_own_date(self):
        """A correction typed by hand has no invoice to be dated by; its own
        day is all there is."""
        make_movement(
            stock_type=self.towels, quantity="1", unit_cost_ht="12.00",
            kind=MovementKind.PURCHASE, occurred_on=date(2026, 3, 9),
        )

        self.assertEqual(margins_for(MARCH).extra_products_ht, Decimal("12.00"))


class WhatTheFiguresAreMadeOfTests(TestCase):
    """Counts the page needs in order to say how much of a figure it holds.

    Each of these is a way the page flatters itself when the number is
    missing: a month whose invoices nobody has scanned yet reads as a 100 %
    real margin, a list of twelve uncosted products reads as the whole hole,
    and a cost note counting only the till's units divides the cogs by the
    wrong denominator.
    """

    def setUp(self):
        article = priced_article("Fût clair", unit_cost="2.00", quantity="1000")
        self.recipe = make_recipe(name="Pinte maison", selling_price_ttc="6.00", vat_rate="0.20")
        make_ingredient(self.recipe, stock_type=article, quantity="0.5")  # 1,00 €
        pos = till_product("Pinte maison", recipe=self.recipe, category="Bières")
        rang_up(pos, date(2026, 3, 4), 10, ttc="60.00", ht="50.00")

    def test_how_many_invoices_the_spending_is_made_of(self):
        """« Facturé 0,00 € » and « facturé 0,00 €, 0 facture » are two
        different months: one bought nothing, the other has not been scanned
        yet."""
        self.assertEqual(margins_for(MARCH).invoice_count, 0)

        goods_invoice(date(2026, 3, 5), total_ht="80.00")
        goods_invoice(date(2026, 3, 6), total_ht="20.00")

        self.assertEqual(margins_for(MARCH).invoice_count, 2)

    def test_an_invoice_outside_the_window_is_not_counted_either(self):
        goods_invoice(date(2026, 4, 5), total_ht="80.00")

        self.assertEqual(margins_for(MARCH).invoice_count, 0)

    def test_the_uncosted_list_says_how_many_it_is_a_top_of(self):
        """Twelve rows out of twenty is a page a reader finishes believing
        the hole is closed."""
        for index in range(20):
            product = till_product(f"Produit sans recette {index:02d}", category="Divers")
            rang_up(product, date(2026, 3, 8), 1, ttc="12.00", ht="10.00")

        report = margins_for(MARCH)

        self.assertEqual(len(report.top_uncosted), 12)
        self.assertEqual(report.uncosted_products, 20)
        self.assertEqual(report.uncosted_revenue, Money(Decimal("200.00"), Decimal("240.00")))

    def test_with_nothing_uncosted_the_count_is_nil_rather_than_absent(self):
        report = margins_for(MARCH)

        self.assertEqual(report.uncosted_products, 0)
        self.assertEqual(report.uncosted_revenue, Money())

    def test_the_cost_note_counts_the_servings_sold_off_the_till_too(self):
        """The cogs holds them and `costed_units` does not, so « 50,00 €
        pour 10 unités chiffrées » read 5,00 € a serving for a recipe
        costing 1,00 €."""
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 20), reference="Événement")
        SaleDocumentLine.objects.create(document=document, recipe=self.recipe, quantity="40")

        report = margins_for(MARCH)

        self.assertEqual(report.costed_units, 10)
        self.assertEqual(report.document_costed_units, Decimal("40"))
        self.assertEqual(report.cogs_high, Decimal("50.00"))

    def test_how_many_articles_are_ticked_at_all(self):
        """« Rien de coché » and « coché, rien acheté sur la période »
        print the same empty table; only this count tells them apart."""
        self.assertEqual(margins_for(MARCH).flagged_articles, 0)

        make_stock_type(name="Essuie-tout", count_in_products_margin=True)

        report = margins_for(MARCH)

        self.assertEqual(report.flagged_articles, 1)
        self.assertEqual(report.extra_products, [])
