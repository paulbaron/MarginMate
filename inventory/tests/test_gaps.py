"""« Combler les écarts » at the database level: inventory/gaps.py.

What the page promises, pinned on worked examples with round numbers:

* the gap is the owner's own definition - counted at the take + bought since
  - lost since - sold since, « sold » being the stock page's « Vendu » from
  the same engine (`variance.attribute_sales`);
* the loss allowance is the « Écarts » page's own rule (one function for
  both, `variance.loss_allowance`), and only what is left after it - the
  room, « À combler » - is to fill;
* a plan never adds to an article more than max(0, room) as the engine counts
  it - checked here by actually ringing the plan up and reading the stock
  page's own figure back (`quantities_sold`);
* an amount is planned on top of the list's earlier entries, and every gap
  shows what the WHOLE list adds (`fill_gaps(..., already)`, `show_list`);
* an article the owner left out (`GapExclusion`, alone or with its
  category) is no gap to fill and no limit, out of the table and the lists,
  while every figure the stock page shows stays as it was.

The window is the stock pages' half-open one: a purchase or a sale ON the
take's day is in the count already, one on the end day is in the window.
`gaps_since` is always given its `end` here, so no test depends on today.

Invented data throughout: every article, recipe, till button, price and
quantity is made up for these tests.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from inventory.gap_planner import BELOW_CHEAPEST, EXACT, GAPS_FULL, NOTHING_TO_FILL
from inventory.gaps import (
    FILLABLE,
    MAX_AMOUNT,
    OVER,
    WITHIN_ALLOWANCE,
    TillButton,
    entry_lines,
    fill_gaps,
    gaps_since,
    list_counts,
    list_is_stale,
    share_summary,
    show_list,
)
from inventory.models import GapExclusion, GapFillEntry, MovementKind, StockType, UnitChoices
from inventory.variance import (
    _quantities_sold,
    attribute_sales,
    compute_variance,
    loss_allowance,
    quantities_sold,
    read_sales,
    unit_costs_ht,
)
from recipes.models import (
    PosProduct,
    PosProductDailyQuantity,
    Recipe,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
    variation_scope,
)
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
)

START = date(2026, 3, 1)
END = date(2026, 3, 31)
SALE_DAY = date(2026, 3, 15)
BEFORE_THE_TAKE = date(2026, 2, 20)
ZERO = Decimal("0")


def at(day: int) -> datetime:
    """Noon on a day of March 2026 - the stock-take idiom of these tests."""
    return timezone.make_aware(datetime(2026, 3, day, 12, 0))


def article(name: str, loss_percent=None, unit=UnitChoices.LITRE, category=""):
    extra = {} if loss_percent is None else {"loss_percent": Decimal(loss_percent)}
    return make_stock_type(name=name, unit=unit, category=category, **extra)


def counted(take, stock_type, quantity):
    """The article counted directly at `take`, in its own unit."""
    return make_stock_take_line(
        stock_take=take, product=None, stock_type=stock_type, counted_quantity=quantity, unit=stock_type.unit
    )


def bought(stock_type, quantity, day, unit_cost="4.00"):
    return make_movement(
        stock_type=stock_type, quantity=quantity, unit_cost_ht=unit_cost, kind=MovementKind.PURCHASE, occurred_on=day
    )


def lost(stock_type, quantity, day, unit_cost="4.00"):
    """A known loss, at the article's own cost so its average cost stays put."""
    return make_movement(
        stock_type=stock_type,
        quantity=-Decimal(quantity),
        unit_cost_ht=unit_cost,
        kind=MovementKind.LOSS,
        occurred_on=day,
    )


def recipe(name: str, price, *pours):
    """A recipe pouring each (article, amount) as a fixed ingredient - one
    choice group each, since ingredients sharing a group are an « OU »."""
    made = make_recipe(name=name, selling_price_ttc=price)
    for group, (stock_type, amount) in enumerate(pours):
        make_ingredient(made, stock_type=stock_type, quantity=amount, group=group)
    return made


def choice(name: str, price, *options):
    """A recipe whose one ingredient is « au choix » between `options`."""
    made = make_recipe(name=name, selling_price_ttc=price)
    for stock_type, amount in options:
        make_ingredient(made, stock_type=stock_type, quantity=amount, group=0)
    return made


def sold(made, quantity, day=SALE_DAY, source="manual"):
    return RecipeSale.objects.create(recipe=made, sold_on=day, quantity=quantity, source=source)


def till_button(name: str, made=None, rung=0, ignored=False):
    return PosProduct.objects.create(name=name, recipe=made, total_quantity=rung, ignored=ignored)


def rung_up(button, day, quantity, money, read=True):
    """What the till took for `button` on `day`: `quantity` rung for `money`
    TTC in all - one export's day, as laddition_import stores it."""
    return PosProductDailyQuantity.objects.create(
        product=button, sold_on=day, quantity=quantity, revenue_ttc=Decimal(money), revenue_read=read
    )


def window_available(report) -> dict[int, Decimal]:
    """What the window could have sold of each article - the ceiling
    `gaps_since` hands the engine (PeriodStock.sellable with no closing)."""
    return {
        article_id: max(ZERO, row.opening + row.purchases - row.known_losses)
        for article_id, row in report.articles.items()
    }


# ---------------------------------------------------------------------------
# The gap, the allowance, the room
# ---------------------------------------------------------------------------


class GapFormulaTests(TestCase):
    """Blonde: 30 L counted, 60 L bought, 2 L lost, 20 pints of 0,5 L sold.
    Gap 30 + 60 - 2 - 10 = 78 L; allowance 10 % of 90 = 9 L; room 69 L.

    Rum at 12,5 %: 2 L counted, 3 L bought, 25 shots of 4 cl sold.
    Gap 2 + 3 - 1 = 4 L; allowance 12,5 % of 5 = 0,625 L; room 3,375 L."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        cls.rum = article("Rhum exemple", loss_percent="12.5")
        counted(cls.take, cls.blonde, "30")
        counted(cls.take, cls.rum, "2")
        bought(cls.blonde, "60", date(2026, 3, 10))
        lost(cls.blonde, "2", date(2026, 3, 11))
        bought(cls.rum, "3", date(2026, 3, 12), unit_cost="20.00")
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        cls.shot = recipe("Shot exemple", "32.00", (cls.rum, "0.04"))
        sold(cls.pint, 20)
        sold(cls.shot, 25)

    def report(self):
        return gaps_since(self.take, end=END)

    def test_the_gap_is_counted_plus_bought_less_lost_less_sold(self):
        row = self.report().articles[self.blonde.pk]
        self.assertEqual(row.opening, Decimal("30"))
        self.assertEqual(row.purchases, Decimal("60"))
        self.assertEqual(row.known_losses, Decimal("2"))
        self.assertEqual(row.sold, Decimal("10"))
        self.assertEqual(row.gap, Decimal("78"))
        self.assertTrue(row.counted)

    def test_the_allowance_is_the_loss_percent_of_what_was_counted_and_bought(self):
        report = self.report()
        self.assertEqual(report.articles[self.blonde.pk].allowance, Decimal("9"))
        self.assertEqual(report.articles[self.rum.pk].allowance, Decimal("0.625"))

    def test_the_room_is_the_gap_less_the_allowance(self):
        report = self.report()
        self.assertEqual(report.articles[self.blonde.pk].room, Decimal("69"))
        self.assertEqual(report.articles[self.rum.pk].room, Decimal("3.375"))
        self.assertEqual(report.rooms, {self.blonde.pk: Decimal("69"), self.rum.pk: Decimal("3.375")})

    def test_both_are_fillable_targets(self):
        report = self.report()
        for stock_type in (self.blonde, self.rum):
            row = report.articles[stock_type.pk]
            self.assertEqual(row.status, FILLABLE)
            self.assertTrue(row.target)
            self.assertFalse(row.secondary)

    def test_the_room_is_valued_at_the_average_cost(self):
        report = self.report()
        self.assertEqual(report.articles[self.blonde.pk].room_value, Decimal("276"))  # 69 L x 4,00
        self.assertEqual(report.articles[self.rum.pk].room_value, Decimal("67.5"))  # 3,375 L x 20,00

    def test_nothing_proposed_yet_reads_zero_percent_filled(self):
        row = self.report().articles[self.blonde.pk]
        self.assertEqual(row.proposed, ZERO)
        self.assertEqual(row.filled_percent, ZERO)

    def test_the_allowance_is_the_one_the_variance_page_takes(self):
        """The « Écarts » page between this take and a closing count at 0 on
        the end day: what left the shelf is opening + purchases, so its
        allowance must be this page's to the digit, per article."""
        closing = make_stock_take(taken_at=at(31))
        counted(closing, self.blonde, "0")
        counted(closing, self.rum, "0")
        variance = compute_variance(closing, opening_take=self.take)
        report = self.report()
        for stock_type in (self.blonde, self.rum):
            pool = next(
                pool for pool in variance.pools if [member.pk for member in pool.stock_types] == [stock_type.pk]
            )
            self.assertEqual(pool.loss_allowance, report.articles[stock_type.pk].allowance, stock_type.name)

    def test_loss_allowance_is_floored_at_zero(self):
        self.assertEqual(loss_allowance(Decimal("90"), Decimal("10")), Decimal("9"))
        self.assertEqual(loss_allowance(Decimal("-5"), Decimal("10")), ZERO)
        self.assertEqual(loss_allowance(Decimal("5"), None), Decimal("0.5"))  # the default 10 %

    def test_the_end_defaults_to_today(self):
        report = gaps_since(self.take)
        self.assertEqual(report.start, START)
        self.assertEqual(report.end, timezone.localdate())


class WindowEdgeTests(TestCase):
    """The take's own day is in the count; the end day is in the window."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "10")
        bought(cls.blonde, "100", START)  # on the take's day: already counted
        bought(cls.blonde, "6", END)  # on the end day: in
        bought(cls.blonde, "50", date(2026, 4, 1))  # after the end: out
        lost(cls.blonde, "3", START)
        lost(cls.blonde, "1", END)
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 50, day=START)
        sold(cls.pint, 4, day=END)
        sold(cls.pint, 7, day=date(2026, 4, 1))

    def test_purchases_losses_and_sales_on_the_take_day_are_out_on_the_end_day_in(self):
        row = gaps_since(self.take, end=END).articles[self.blonde.pk]
        self.assertEqual(row.opening, Decimal("10"))
        self.assertEqual(row.purchases, Decimal("6"))
        self.assertEqual(row.known_losses, Decimal("1"))
        self.assertEqual(row.sold, Decimal("2"))
        self.assertEqual(row.gap, Decimal("13"))
        self.assertEqual(row.allowance, Decimal("1.6"))

    def test_the_freshness_days_follow_the_same_window(self):
        report = gaps_since(self.take, end=END)
        self.assertEqual(report.last_sale_day, END)
        self.assertEqual(report.last_purchase_day, END)
        self.assertFalse(report.purchases_after_sales)

    def test_a_shorter_end_drops_what_came_after_it(self):
        row = gaps_since(self.take, end=date(2026, 3, 30)).articles[self.blonde.pk]
        self.assertEqual(row.purchases, ZERO)
        self.assertEqual(row.known_losses, ZERO)
        self.assertEqual(row.sold, ZERO)
        self.assertEqual(row.gap, Decimal("10"))


class UncountedArticleTests(TestCase):
    """An article absent from the count reads 0 there - which can only
    understate its gap - and stays fillable up to what was bought since."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "30")
        cls.rum = article("Rhum exemple")  # not counted
        bought(cls.rum, "3", date(2026, 3, 12), unit_cost="20.00")
        cls.shot = recipe("Shot exemple", "32.00", (cls.rum, "0.04"))
        sold(cls.shot, 25)  # 1 L
        cls.syrup = article("Sirop exemple")  # not counted either
        bought(cls.syrup, "1", date(2026, 3, 12))
        cls.lemonade = recipe("Limonade exemple", "31.00", (cls.syrup, "0.1"))
        sold(cls.lemonade, 9)  # 0,9 L

    def test_it_reads_zero_at_the_count_and_is_flagged(self):
        row = gaps_since(self.take, end=END).articles[self.rum.pk]
        self.assertFalse(row.counted)
        self.assertEqual(row.opening, ZERO)
        self.assertEqual(row.purchases, Decimal("3"))
        self.assertEqual(row.sold, Decimal("1"))

    def test_it_is_still_a_target_while_bought_less_sold_less_allowance_is_positive(self):
        report = gaps_since(self.take, end=END)
        row = report.articles[self.rum.pk]
        self.assertEqual(row.gap, Decimal("2"))
        self.assertEqual(row.allowance, Decimal("0.3"))
        self.assertEqual(row.room, Decimal("1.7"))
        self.assertEqual(row.status, FILLABLE)
        self.assertTrue(row.target)
        self.assertIn(self.shot.pk, [offer.recipe_id for offer in report.offers])

    def test_one_whose_purchases_are_all_sold_or_allowed_is_not(self):
        report = gaps_since(self.take, end=END)
        row = report.articles[self.syrup.pk]
        self.assertFalse(row.counted)
        self.assertEqual(row.gap, Decimal("0.1"))
        self.assertEqual(row.allowance, Decimal("0.1"))
        self.assertEqual(row.status, WITHIN_ALLOWANCE)
        self.assertFalse(row.target)
        self.assertNotIn(self.lemonade.pk, [offer.recipe_id for offer in report.offers])


class StatusAndBlockedTests(TestCase):
    """An article sold past what it had (OVER), or whose gap is all normal
    loss (WITHIN_ALLOWANCE), blocks every recipe one sale of which would
    pour it - and the report names the article."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "30")
        bought(cls.blonde, "60", date(2026, 3, 10))
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 20)

        # OVER: 1 L counted, nothing bought, 3,2 L poured.
        cls.syrup = article("Sirop exemple")
        counted(cls.take, cls.syrup, "1")
        cls.lemonade = recipe("Limonade exemple", "31.00", (cls.syrup, "0.1"))
        sold(cls.lemonade, 30)
        # A recipe pouring the fillable beer AND the over syrup.
        cls.monaco = recipe("Monaco exemple", "33.00", (cls.blonde, "0.25"), (cls.syrup, "0.02"))
        sold(cls.monaco, 10)

        # WITHIN_ALLOWANCE: 10 L counted, 9,5 L poured, 1 L allowed.
        cls.cider = article("Cidre exemple")
        counted(cls.take, cls.cider, "10")
        cls.bowl = recipe("Bolée exemple", "32.00", (cls.cider, "0.5"))
        sold(cls.bowl, 19)

        # The edge: a gap exactly the allowance leaves nothing to fill.
        cls.wine = article("Vin exemple")
        counted(cls.take, cls.wine, "10")
        cls.glass = recipe("Verre de vin exemple", "34.00", (cls.wine, "0.5"))
        sold(cls.glass, 18)

    def setUp(self):
        self.report = gaps_since(self.take, end=END)

    def blocked_names(self):
        return {row.recipe.name: [gap.stock_type.name for gap in row.articles] for row in self.report.blocked}

    def test_sold_more_than_counted_and_bought_is_over(self):
        row = self.report.articles[self.syrup.pk]
        self.assertEqual(row.sold, Decimal("3.2"))
        self.assertEqual(row.gap, Decimal("-2.2"))
        self.assertEqual(row.status, OVER)
        self.assertFalse(row.target)
        self.assertIsNone(row.room_value)
        self.assertIsNone(row.filled_percent)

    def test_a_gap_within_the_allowance_is_not_to_fill(self):
        row = self.report.articles[self.cider.pk]
        self.assertEqual(row.gap, Decimal("0.5"))
        self.assertEqual(row.allowance, Decimal("1"))
        self.assertEqual(row.status, WITHIN_ALLOWANCE)
        self.assertFalse(row.target)

    def test_a_gap_exactly_the_allowance_is_within_it(self):
        row = self.report.articles[self.wine.pk]
        self.assertEqual(row.gap, Decimal("1"))
        self.assertEqual(row.room, ZERO)
        self.assertEqual(row.status, WITHIN_ALLOWANCE)
        self.assertFalse(row.target)

    def test_every_recipe_pouring_such_an_article_is_blocked_with_it(self):
        self.assertEqual(
            self.blocked_names(),
            {
                "Bolée exemple": ["Cidre exemple"],
                "Limonade exemple": ["Sirop exemple"],
                "Monaco exemple": ["Sirop exemple"],
                "Verre de vin exemple": ["Vin exemple"],
            },
        )
        # Sorted by recipe name, case aside.
        self.assertEqual(
            [row.recipe.name for row in self.report.blocked],
            ["Bolée exemple", "Limonade exemple", "Monaco exemple", "Verre de vin exemple"],
        )

    def test_a_blocked_recipe_is_never_offered_and_the_fillable_one_is(self):
        offered = {offer.recipe_id for offer in self.report.offers}
        self.assertEqual(offered, {self.pint.pk})
        self.assertTrue(self.report.articles[self.blonde.pk].target)

    def test_the_blocking_articles_are_on_the_gaps_table(self):
        shown = {row.stock_type.pk for row in self.report.rows}
        self.assertEqual(shown, {self.blonde.pk, self.syrup.pk, self.cider.pk, self.wine.pk})
        # Targets first.
        self.assertEqual(self.report.rows[0].stock_type, self.blonde)

    def test_a_plan_never_pours_a_blocked_article(self):
        result = fill_gaps(self.report, Decimal("350.00"))
        self.assertEqual(set(result.plan.counts), {self.pint.pk})
        for stock_type in (self.syrup, self.cider, self.wine):
            self.assertEqual(self.report.articles[stock_type.pk].proposed, ZERO, stock_type.name)


class LessThanOneServingTests(TestCase):
    """A gap past its allowance but smaller than one pour: 10,3 L counted,
    18 pints sold - gap 1,3 L, allowance 1,03 L, 0,27 L to fill, and a pint
    pours 0,5. Fillable, yet no pint may be proposed."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "10.3")
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 18)

    def test_the_article_is_fillable_and_the_recipe_blocked_by_it(self):
        report = gaps_since(self.take, end=END)
        row = report.articles[self.blonde.pk]
        self.assertEqual(row.room, Decimal("0.27"))
        self.assertEqual(row.status, FILLABLE)
        self.assertFalse(row.target)
        self.assertEqual([(blocked.recipe, blocked.articles) for blocked in report.blocked], [(self.pint, [row])])
        self.assertEqual(report.offers, [])
        self.assertEqual(report.unreached, [row])


class PricedAndSoldTests(TestCase):
    """Only a recipe sold as itself at a price above zero, and sold since the
    take, is proposed; the others still consume what their sales poured."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "30")
        bought(cls.blonde, "60", date(2026, 3, 10))
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 10)
        cls.comped = recipe("Pinte offerte exemple", "0", (cls.blonde, "0.5"))
        sold(cls.comped, 4)
        cls.unpriced = recipe("Fond de fût exemple", None, (cls.blonde, "0.5"))
        sold(cls.unpriced, 2)
        cls.half = recipe("Demi exemple", "31.00", (cls.blonde, "0.25"))
        sold(cls.half, 12, day=START)  # on the take's day only: not since
        cls.galopin = recipe("Galopin exemple", "30.50", (cls.blonde, "0.125"))  # never sold
        cls.board = make_recipe(name="Planche exemple", selling_price_ttc="38.00")  # nothing poured
        sold(cls.board, 3)

    def setUp(self):
        self.report = gaps_since(self.take, end=END)

    def test_a_price_of_zero_or_none_is_never_an_offer(self):
        offered = {offer.recipe_id for offer in self.report.offers}
        self.assertNotIn(self.comped.pk, offered)
        self.assertNotIn(self.unpriced.pk, offered)
        self.assertNotIn(self.comped.pk, self.report.recipes)
        self.assertNotIn(self.unpriced.pk, self.report.recipes)
        self.assertNotIn(self.comped, self.report.not_sold_since)
        self.assertNotIn(self.unpriced, self.report.not_sold_since)

    def test_their_sales_still_count_in_what_was_sold(self):
        # 10 pints + 4 comped + 2 unpriced, half a litre each.
        self.assertEqual(self.report.articles[self.blonde.pk].sold, Decimal("8"))

    def test_a_priced_recipe_not_sold_since_the_take_is_said_not_offered(self):
        self.assertEqual(self.report.not_sold_since, [self.half, self.galopin])
        offered = {offer.recipe_id for offer in self.report.offers}
        self.assertEqual(offered, {self.pint.pk})

    def test_a_recipe_pouring_nothing_is_neither_offered_nor_blocked(self):
        self.assertNotIn(self.board.pk, {offer.recipe_id for offer in self.report.offers})
        self.assertNotIn(self.board, [row.recipe for row in self.report.blocked])

    def test_the_offer_carries_its_price_in_cents_and_its_sales(self):
        offer = next(offer for offer in self.report.offers if offer.recipe_id == self.pint.pk)
        self.assertEqual(offer.price_cents, 3500)
        self.assertEqual(offer.sold, 10)
        self.assertTrue(offer.independent)
        self.assertEqual(offer.terms, (({self.blonde.pk: Decimal("0.5")},),))


class TillButtonTests(TestCase):
    """The button to press, when no button says what it charges: the most
    rung till product linked to the recipe, never its happy-hour one nor an
    ignored one, else the recipe's name."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "30")
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        cls.pint.happy_hour_name = "Pinte HH exemple"
        cls.pint.save(update_fields=["happy_hour_name"])
        cls.half = recipe("Demi exemple", "31.00", (cls.blonde, "0.25"))
        sold(cls.pint, 10)
        sold(cls.half, 10)
        till_button("PINTE CAISSE EXEMPLE", cls.pint, rung=120)
        till_button("PINTE 2 EXEMPLE", cls.pint, rung=30)
        till_button("  PINTE HH EXEMPLE ", cls.pint, rung=900)  # the happy-hour button
        till_button("PINTE ANCIENNE EXEMPLE", cls.pint, rung=5000, ignored=True)
        till_button("BOUTON SANS RECETTE EXEMPLE", None, rung=9000)

    def test_the_most_rung_ordinary_button(self):
        report = gaps_since(self.take, end=END)
        self.assertEqual(report.till_buttons[self.pint.pk], TillButton("PINTE CAISSE EXEMPLE", None))

    def test_no_button_falls_back_on_the_recipe_name(self):
        report = gaps_since(self.take, end=END)
        self.assertEqual(report.till_buttons[self.half.pk], TillButton("Demi exemple", None))

    def test_every_priced_recipe_has_a_button_and_nothing_else_does(self):
        report = gaps_since(self.take, end=END)
        self.assertEqual(set(report.till_buttons), {self.pint.pk, self.half.pk})

    def test_a_plan_line_carries_the_button(self):
        report = gaps_since(self.take, end=END)
        result = fill_gaps(report, Decimal("35.00"))
        self.assertEqual(
            [(line.recipe, line.till.name, line.till.price) for line in result.lines],
            [(self.pint, "PINTE CAISSE EXEMPLE", None)],
        )
        # A button never read rings no other price.
        self.assertFalse(result.lines[0].till_differs)
        self.assertEqual(result.till_differs, 0)


class TillButtonByPriceTests(TestCase):
    """The button whose till price is the recipe's comes first, however
    little it is rung: a recipe linked to its own button and to the glass of
    the same spirit would otherwise send the owner to ring the glass.

    A button's price is what it charged most often since the take - a day's
    money over its units, weighted by units - over the days whose money was
    read, inside the window (the take's own day out, the end day in).

    Rum: 2 L counted, 4 cocktails of 5 cl sold since; every recipe pours it."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.rum = article("Rhum ambré exemple")
        bought(cls.rum, "1", BEFORE_THE_TAKE, unit_cost="20.00")
        counted(cls.take, cls.rum, "2")

        # Rung less, at the recipe's price; rung more, at another one.
        cls.cocktail = recipe("Cocktail du jour exemple", "33.00", (cls.rum, "0.05"))
        sold(cls.cocktail, 4)
        cls.own = till_button("Cocktail exemple", cls.cocktail, rung=8)
        rung_up(cls.own, date(2026, 3, 10), 3, "99.00")
        rung_up(cls.own, date(2026, 3, 12), 2, "66.00")
        cls.glass = till_button("Verre exemple", cls.cocktail, rung=60)
        rung_up(cls.glass, date(2026, 3, 10), 20, "266.00")
        rung_up(cls.glass, date(2026, 3, 11), 10, "133.00")

        # One button only, charging 33,00 € most days and one drink of three
        # comped on another: that day's 22,00 € is no price.
        cls.punch = recipe("Punch exemple", "34.00", (cls.rum, "0.04"))
        sold(cls.punch, 2)
        cls.punch_button = till_button("Punch caisse exemple", cls.punch, rung=8)
        rung_up(cls.punch_button, date(2026, 3, 10), 5, "165.00")
        rung_up(cls.punch_button, date(2026, 3, 11), 3, "66.00")

        # Rung on the take's day and before only: never since.
        cls.old = recipe("Grog exemple", "32.00", (cls.rum, "0.04"))
        sold(cls.old, 2)
        cls.old_button = till_button("Grog caisse exemple", cls.old, rung=40)
        rung_up(cls.old_button, START, 20, "200.00")
        rung_up(cls.old_button, BEFORE_THE_TAKE, 20, "200.00")

        # 35,00 € inside the window; what is outside it, unread or a refund
        # day says nothing, however many units it carries.
        cls.neat = recipe("Rhum sec exemple", "36.00", (cls.rum, "0.04"))
        sold(cls.neat, 2)
        cls.neat_button = till_button("Rhum sec caisse exemple", cls.neat, rung=200)
        rung_up(cls.neat_button, date(2026, 3, 10), 2, "70.00")
        rung_up(cls.neat_button, END, 1, "35.00")  # the end day is in
        rung_up(cls.neat_button, START, 50, "500.00")  # the take's day is out
        rung_up(cls.neat_button, date(2026, 4, 2), 50, "500.00")  # after the end
        rung_up(cls.neat_button, date(2026, 3, 20), 40, "400.00", read=False)  # money never read
        rung_up(cls.neat_button, date(2026, 3, 21), -1, "-36.00")  # a refund day

        # The recipe's price is charged by its happy-hour button and by an
        # ignored one: neither is a button to press.
        cls.ti_punch = recipe("Ti punch exemple", "31.00", (cls.rum, "0.04"))
        cls.ti_punch.happy_hour_name = "Ti punch HH exemple"
        cls.ti_punch.save(update_fields=["happy_hour_name"])
        sold(cls.ti_punch, 2)
        cls.ti_button = till_button("Ti punch caisse exemple", cls.ti_punch, rung=5)
        rung_up(cls.ti_button, date(2026, 3, 10), 5, "150.50")
        happy_hour = till_button("TI PUNCH HH EXEMPLE", cls.ti_punch, rung=90)
        rung_up(happy_hour, date(2026, 3, 10), 9, "279.00")
        ignored = till_button("Ti punch ancien exemple", cls.ti_punch, rung=90, ignored=True)
        rung_up(ignored, date(2026, 3, 10), 9, "279.00")

    def setUp(self):
        self.report = gaps_since(self.take, end=END)

    def test_the_button_at_the_recipe_s_price_beats_the_most_rung(self):
        self.assertEqual(self.report.till_buttons[self.cocktail.pk], TillButton("Cocktail exemple", Decimal("33.00")))

    def test_the_plan_line_names_it_and_rings_the_same_price(self):
        result = fill_gaps(self.report, Decimal("33.00"))
        self.assertEqual(result.plan.counts, {self.cocktail.pk: 1})
        (line,) = result.lines
        self.assertEqual(line.till, TillButton("Cocktail exemple", Decimal("33.00")))
        self.assertEqual(line.price, Decimal("33.00"))
        self.assertFalse(line.till_differs)
        self.assertEqual(result.till_differs, 0)

    def test_a_comped_drink_does_not_move_the_price(self):
        self.assertEqual(self.report.till_buttons[self.punch.pk], TillButton("Punch caisse exemple", Decimal("33.00")))

    def test_a_button_ringing_another_price_says_so_on_its_line(self):
        result = fill_gaps(self.report, Decimal("34.00"))
        self.assertEqual(result.plan.counts, {self.punch.pk: 1})
        (line,) = result.lines
        self.assertEqual((line.price, line.till.price), (Decimal("34.00"), Decimal("33.00")))
        self.assertTrue(line.till_differs)
        self.assertEqual(result.till_differs, 1)

    def test_a_button_not_rung_since_the_take_has_no_price(self):
        self.assertEqual(self.report.till_buttons[self.old.pk], TillButton("Grog caisse exemple", None))
        result = fill_gaps(self.report, Decimal("32.00"))
        self.assertEqual(result.plan.counts, {self.old.pk: 1})
        self.assertFalse(result.lines[0].till_differs)
        self.assertEqual(result.till_differs, 0)

    def test_days_outside_the_window_unread_or_refunded_are_left_out(self):
        self.assertEqual(
            self.report.till_buttons[self.neat.pk], TillButton("Rhum sec caisse exemple", Decimal("35.00"))
        )

    def test_never_the_happy_hour_nor_an_ignored_button_even_at_the_recipe_s_price(self):
        self.assertEqual(
            self.report.till_buttons[self.ti_punch.pk], TillButton("Ti punch caisse exemple", Decimal("30.10"))
        )

    def test_two_lines_at_another_price_are_counted(self):
        # 34 + 36: the only way to make 70,00 € with these prices.
        result = fill_gaps(self.report, Decimal("70.00"))
        self.assertEqual(result.plan.counts, {self.punch.pk: 1, self.neat.pk: 1})
        self.assertEqual(result.till_differs, 2)


def ou_rums(take, premium_count, standard_count):
    """Two rums an « OU » chooses between: the dear one at 40 €/L, the
    cheaper at 20 €/L - priced by a delivery before the take, so nothing is
    bought inside the window."""
    premium = article("Rhum vieux exemple")
    standard = article("Rhum blanc exemple")
    bought(premium, "1", BEFORE_THE_TAKE, unit_cost="40.00")
    bought(standard, "1", BEFORE_THE_TAKE, unit_cost="20.00")
    counted(take, premium, premium_count)
    counted(take, standard, standard_count)
    shot = choice("Shot au choix exemple", "33.00", (premium, "0.04"), (standard, "0.04"))
    return premium, standard, shot


class SecondaryArticleTests(TestCase):
    """The engine books an « OU » on its dearest option first, so the cheaper
    rum is reached by no sale while the dearer one has room."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.premium, cls.standard, cls.shot = ou_rums(cls.take, "2", "1")
        sold(cls.shot, 10)  # 0,4 L, all on the dear rum

    def test_the_cheaper_side_is_secondary_while_the_dearer_has_room(self):
        report = gaps_since(self.take, end=END)
        premium, standard = report.articles[self.premium.pk], report.articles[self.standard.pk]
        self.assertEqual(premium.sold, Decimal("0.4"))
        self.assertEqual(premium.room, Decimal("1.4"))
        self.assertEqual(standard.sold, ZERO)
        self.assertEqual(standard.room, Decimal("0.9"))
        self.assertTrue(premium.target)
        self.assertFalse(premium.secondary)
        self.assertTrue(standard.target)
        self.assertTrue(standard.secondary)
        self.assertFalse(report.offers[0].independent)

    def test_a_fixed_pour_of_the_cheaper_side_makes_it_primary(self):
        ti_punch = recipe("Ti punch exemple", "34.00", (self.standard, "0.05"))
        sold(ti_punch, 4)
        report = gaps_since(self.take, end=END)
        standard = report.articles[self.standard.pk]
        self.assertTrue(standard.target)
        self.assertFalse(standard.secondary)
        # Measured, not assumed: the punch pours a bottle an « OU » can
        # reach, and the engine books its next sale there.
        punch = next(offer for offer in report.offers if offer.recipe_id == ti_punch.pk)
        self.assertFalse(punch.independent)
        added = report.consumption({ti_punch.pk: 1})[self.standard.pk] - report.consumption({})[self.standard.pk]
        self.assertEqual(added, Decimal("0.05"))

    def test_the_share_summary_leaves_the_secondary_side_out(self):
        """Three shots: all three on the dear rum (0,12 of its 1,4 L), none on
        the cheaper one - which would otherwise read 0 % as the lowest."""
        report = gaps_since(self.take, end=END)
        result = fill_gaps(report, Decimal("99.00"))
        self.assertEqual(result.plan.reason, EXACT)
        self.assertEqual(result.plan.counts, {self.shot.pk: 3})
        self.assertEqual(report.articles[self.premium.pk].proposed, Decimal("0.12"))
        self.assertEqual(report.articles[self.standard.pk].proposed, ZERO)
        share = Decimal("0.12") / Decimal("1.4") * 100
        self.assertEqual(share_summary(report), (share, share, share))


class SecondaryWhenTheDearerSideIsFullTests(TestCase):
    """The dear rum within its allowance: the next shot is booked on the
    cheaper one, which is then no « secondary » side at all."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.premium, cls.standard, cls.shot = ou_rums(cls.take, "0.5", "1")
        sold(cls.shot, 12)  # 0,48 L of the dear rum's 0,5

    def test_the_cheaper_side_is_primary_and_the_recipe_not_blocked(self):
        report = gaps_since(self.take, end=END)
        premium, standard = report.articles[self.premium.pk], report.articles[self.standard.pk]
        self.assertEqual(premium.sold, Decimal("0.48"))
        self.assertEqual(premium.status, WITHIN_ALLOWANCE)
        self.assertFalse(premium.target)
        self.assertTrue(standard.target)
        self.assertFalse(standard.secondary)
        self.assertEqual(report.blocked, [])
        self.assertEqual([offer.recipe_id for offer in report.offers], [self.shot.pk])

    def test_the_shots_proposed_land_on_the_cheaper_rum(self):
        report = gaps_since(self.take, end=END)
        fill_gaps(report, Decimal("66.00"))
        self.assertEqual(report.articles[self.premium.pk].proposed, ZERO)
        self.assertEqual(report.articles[self.standard.pk].proposed, Decimal("0.08"))


class SecondaryMeasuredTests(TestCase):
    """« Secondary » is measured, not modelled: a target no proposed recipe's
    first sale adds to as the engine books it.

    A ti punch (white rum 4 cl, cane syrup 1 cl - every ingredient fixed) is
    sold as itself AND is the dearer side of a cocktail « au choix » (the ti
    punch, or 4 cl of vodka). The white rum is shared and at its cap: 1 L
    counted, 0,9 once its allowance is set aside; 5 ti punches poured 0,2 L
    of it, so 18 of the 30 cocktails fit on the ti punch side (rounded up:
    0,92 L in all) and 12 went to the vodka.

    One more ti punch pours 4 cl of rum and 1 cl of syrup - and leaves the
    cocktails room for 17 ti punches only, so one cocktail moves to the
    vodka: rum and syrup do not move, the vodka takes 4 cl. One more cocktail
    goes to the vodka too. So the syrup - which only the ti punch pours, and
    which has 1,57 L to fill - is a target no sale reaches: secondary."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.rum = article("Rhum blanc exemple")
        cls.syrup = article("Sirop de canne exemple")
        cls.vodka = article("Vodka exemple")
        for stock_type, quantity, cost in (
            (cls.rum, "1", "20.00"),
            (cls.syrup, "2", "8.00"),
            (cls.vodka, "2", "15.00"),
        ):
            bought(stock_type, quantity, BEFORE_THE_TAKE, unit_cost=cost)
            counted(cls.take, stock_type, quantity)
        cls.ti_punch = recipe("Ti punch exemple", "34.00", (cls.rum, "0.04"), (cls.syrup, "0.01"))
        cls.cocktail = make_recipe(name="Cocktail au choix exemple", selling_price_ttc="33.00")
        make_ingredient(cls.cocktail, sub_recipe=cls.ti_punch, quantity="1", group=0)
        make_ingredient(cls.cocktail, stock_type=cls.vodka, quantity="0.04", group=0)
        sold(cls.ti_punch, 5)
        sold(cls.cocktail, 30)

    def setUp(self):
        self.report = gaps_since(self.take, end=END)

    def row(self, stock_type):
        return self.report.articles[stock_type.pk]

    def test_the_rooms(self):
        self.assertEqual(self.row(self.rum).sold, Decimal("0.92"))
        self.assertEqual(self.row(self.rum).room, Decimal("-0.02"))
        self.assertEqual(self.row(self.rum).status, WITHIN_ALLOWANCE)
        self.assertEqual(self.row(self.syrup).sold, Decimal("0.23"))
        self.assertEqual(self.row(self.syrup).room, Decimal("1.57"))
        self.assertEqual(self.row(self.vodka).sold, Decimal("0.48"))
        self.assertEqual(self.row(self.vodka).room, Decimal("1.32"))

    def test_both_recipes_are_proposed_neither_blocked(self):
        self.assertEqual({offer.recipe_id for offer in self.report.offers}, {self.ti_punch.pk, self.cocktail.pk})
        self.assertEqual(self.report.blocked, [])
        self.assertFalse(any(offer.independent for offer in self.report.offers))

    def test_one_more_ti_punch_moves_a_cocktail_onto_the_vodka(self):
        before = self.report.consumption({})
        after = self.report.consumption({self.ti_punch.pk: 1})
        self.assertEqual(after[self.rum.pk], before[self.rum.pk])
        self.assertEqual(after[self.syrup.pk], before[self.syrup.pk])
        self.assertEqual(after[self.vodka.pk] - before[self.vodka.pk], Decimal("0.04"))

    def test_the_syrup_is_a_secondary_target_the_vodka_a_primary_one(self):
        syrup, vodka, rum = self.row(self.syrup), self.row(self.vodka), self.row(self.rum)
        self.assertTrue(syrup.target)
        self.assertTrue(syrup.secondary)
        self.assertTrue(vodka.target)
        self.assertFalse(vodka.secondary)
        self.assertFalse(rum.target)
        self.assertFalse(rum.secondary)

    def test_a_secondary_target_is_not_unreached(self):
        self.assertEqual(self.report.unreached, [])
        self.assertEqual(self.report.outside_recipes, 0)

    def test_the_share_summary_leaves_it_out(self):
        """34 + 33 € is one of each, the only way to make 67,00 €: both go to
        the vodka (8 cl of its 1,32 L), the syrup takes nothing - and would
        read 0 % as the lowest share."""
        result = fill_gaps(self.report, Decimal("67.00"))
        self.assertEqual(result.plan.reason, EXACT)
        self.assertEqual(result.plan.counts, {self.ti_punch.pk: 1, self.cocktail.pk: 1})
        self.assertEqual(self.row(self.syrup).proposed, ZERO)
        self.assertEqual(self.row(self.syrup).filled_percent, ZERO)
        self.assertEqual(self.row(self.vodka).proposed, Decimal("0.08"))
        share = Decimal("0.08") / Decimal("1.32") * 100
        self.assertEqual(share_summary(self.report), (share, share, share))


class UnreachedTests(TestCase):
    """A gap to fill that some recipe pours but no recipe proposed does is
    « unreached », listed and valued; a gap NO recipe pours (the equipment,
    the paper towels) is only counted - no sale could ever fill it.

    Gin: 2 L counted, poured only by a recipe nobody ordered since the take
    - room 1,8 L at 30,00 €. Wine: 9 L counted, poured by a kir held back
    by its syrup (sold 0,1 L of none) - 9 - 0,5 sold - 0,9 allowed = 7,6 L
    at 6,00 €. Cider: 5 L counted, no cost known, poured only by a recipe
    not sold since - 4,5 L. A tablecloth and cups, in no recipe."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "30")
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 10)
        # Only a recipe nobody ordered since the take.
        cls.gin = article("Gin exemple")
        bought(cls.gin, "1", BEFORE_THE_TAKE, unit_cost="30.00")
        counted(cls.take, cls.gin, "2")
        cls.gin_tonic = recipe("Gin tonic exemple", "34.00", (cls.gin, "0.05"))
        # Only a recipe one sale of which goes past another gap.
        cls.wine = article("Vin exemple")
        bought(cls.wine, "12", BEFORE_THE_TAKE, unit_cost="6.00")
        counted(cls.take, cls.wine, "9")
        cls.syrup = article("Sirop exemple")
        counted(cls.take, cls.syrup, "0")
        cls.kir = recipe("Kir exemple", "33.00", (cls.wine, "0.1"), (cls.syrup, "0.02"))
        sold(cls.kir, 5)
        # No cost known at all, and only a recipe not sold since.
        cls.cider = article("Cidre exemple")
        counted(cls.take, cls.cider, "5")
        cls.bowl = recipe("Bolée exemple", "32.00", (cls.cider, "0.5"))
        # In no recipe at all: bought since, or counted.
        cls.cloth = article("Nappe exemple", unit=UnitChoices.UNIT)
        bought(cls.cloth, "10", date(2026, 3, 12), unit_cost="2.00")
        cls.cups = article("Gobelets exemple", unit=UnitChoices.UNIT)
        counted(cls.take, cls.cups, "100")
        # In no recipe, and nothing left to fill: not counted at all.
        cls.towels = article("Torchons exemple", unit=UnitChoices.UNIT)
        counted(cls.take, cls.towels, "0")

    def setUp(self):
        self.report = gaps_since(self.take, end=END)

    def test_every_gap_no_proposed_recipe_pours_is_listed_biggest_value_first(self):
        self.assertEqual([row.stock_type for row in self.report.unreached], [self.gin, self.wine, self.cider])

    def test_each_with_its_value_and_the_total(self):
        values = {row.stock_type.name: row.room_value for row in self.report.unreached}
        self.assertEqual(values, {"Gin exemple": Decimal("54"), "Vin exemple": Decimal("45.6"), "Cidre exemple": None})
        self.assertEqual(self.report.unreached_value, Decimal("99.6"))

    def test_an_article_poured_only_by_a_blocked_recipe_is_unreached(self):
        self.assertEqual([row.recipe for row in self.report.blocked], [self.kir])
        wine = self.report.articles[self.wine.pk]
        self.assertEqual(wine.room, Decimal("7.6"))
        self.assertFalse(wine.target)
        self.assertIn(wine, self.report.unreached)

    def test_an_article_in_no_recipe_is_counted_apart_never_listed(self):
        unreached = [row.stock_type for row in self.report.unreached]
        for stock_type in (self.cloth, self.cups, self.towels):
            self.assertNotIn(stock_type, unreached, stock_type.name)
        self.assertEqual(self.report.articles[self.cloth.pk].room, Decimal("9"))  # 10 bought, 10 % allowed
        self.assertEqual(self.report.articles[self.towels.pk].room, ZERO)
        # The tablecloth and the cups; the towels have nothing to fill.
        self.assertEqual(self.report.outside_recipes, 2)

    def test_which_articles_some_recipe_pours(self):
        self.assertEqual(
            self.report.in_recipes,
            {self.blonde.pk, self.gin.pk, self.wine.pk, self.syrup.pk, self.cider.pk},
        )

    def test_the_values_are_the_average_costs(self):
        self.assertEqual(self.report.values, unit_costs_ht())
        self.assertEqual(self.report.values[self.wine.pk], Decimal("6"))

    def test_a_target_is_not_unreached(self):
        self.assertTrue(self.report.articles[self.blonde.pk].target)
        self.assertNotIn(self.blonde, [row.stock_type for row in self.report.unreached])
        self.assertEqual(self.report.targets, [self.report.articles[self.blonde.pk]])

    def test_an_over_article_is_neither_listed_nor_counted(self):
        syrup = self.report.articles[self.syrup.pk]
        self.assertEqual(syrup.status, OVER)
        self.assertNotIn(syrup, self.report.unreached)


class UnpricedPreparationTests(TestCase):
    """An article poured only by a preparation that is not sold as itself
    (a house syrup, no price) is some recipe's: no sale proposed fills it,
    and it is listed as such - not taken for equipment."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.sugar = article("Sucre exemple", unit=UnitChoices.KILOGRAM)
        counted(cls.take, cls.sugar, "5")
        recipe("Sirop maison exemple", None, (cls.sugar, "1"))

    def test_it_is_unreached_not_outside_the_recipes(self):
        report = gaps_since(self.take, end=END)
        self.assertEqual([row.stock_type for row in report.unreached], [self.sugar])
        self.assertEqual(report.outside_recipes, 0)
        self.assertEqual(report.offers, [])


class FreshnessTests(TestCase):
    """« Des achats sont plus récents que les dernières ventes importées »:
    a delivery after the last sale imported holds sales not handed over yet."""

    def setUp(self):
        self.take = make_stock_take(taken_at=at(1))
        self.blonde = article("Blonde exemple")
        counted(self.take, self.blonde, "30")
        self.pint = recipe("Pinte exemple", "35.00", (self.blonde, "0.5"))

    def report(self):
        return gaps_since(self.take, end=END)

    def test_a_purchase_after_the_last_sale(self):
        sold(self.pint, 4, day=date(2026, 3, 15))
        bought(self.blonde, "30", date(2026, 3, 20))
        report = self.report()
        self.assertEqual(report.last_sale_day, date(2026, 3, 15))
        self.assertEqual(report.last_purchase_day, date(2026, 3, 20))
        self.assertTrue(report.purchases_after_sales)

    def test_a_purchase_before_the_last_sale(self):
        bought(self.blonde, "30", date(2026, 3, 10))
        sold(self.pint, 4, day=date(2026, 3, 15))
        self.assertFalse(self.report().purchases_after_sales)

    def test_a_purchase_the_same_day_as_the_last_sale(self):
        bought(self.blonde, "30", date(2026, 3, 15))
        sold(self.pint, 4, day=date(2026, 3, 15))
        self.assertFalse(self.report().purchases_after_sales)

    def test_no_purchase_at_all(self):
        sold(self.pint, 4)
        report = self.report()
        self.assertIsNone(report.last_purchase_day)
        self.assertFalse(report.purchases_after_sales)

    def test_purchases_and_no_sale_since_the_take(self):
        bought(self.blonde, "30", date(2026, 3, 10))
        report = self.report()
        self.assertIsNone(report.last_sale_day)
        self.assertTrue(report.purchases_after_sales)

    def test_a_loss_is_no_purchase(self):
        sold(self.pint, 4, day=date(2026, 3, 15))
        lost(self.blonde, "1", date(2026, 3, 25))
        report = self.report()
        self.assertIsNone(report.last_purchase_day)
        self.assertFalse(report.purchases_after_sales)

    # « Les ventes ne sont pas importées jusqu'à hier »: the till's sales are
    # imported a day behind at best, so yesterday is fresh; earlier, every
    # gap still holds sales the till has not handed over.
    def test_sales_up_to_yesterday_are_not_behind(self):
        bought(self.blonde, "30", date(2026, 3, 10))
        sold(self.pint, 4, day=END - timedelta(days=1))
        report = self.report()
        self.assertEqual(report.last_sale_day, date(2026, 3, 30))
        self.assertFalse(report.sales_behind)

    def test_sales_up_to_the_end_day_are_not_behind(self):
        sold(self.pint, 4, day=END)
        self.assertFalse(self.report().sales_behind)

    def test_sales_stopping_before_yesterday_are_behind(self):
        bought(self.blonde, "30", date(2026, 3, 10))
        sold(self.pint, 4, day=END - timedelta(days=2))
        report = self.report()
        self.assertFalse(report.purchases_after_sales)
        self.assertTrue(report.sales_behind)

    def test_a_purchase_after_yesterday_s_sales_is_behind(self):
        sold(self.pint, 4, day=END - timedelta(days=1))
        bought(self.blonde, "30", END)
        report = self.report()
        self.assertTrue(report.purchases_after_sales)
        self.assertTrue(report.sales_behind)

    def test_no_sale_at_all_is_behind(self):
        report = self.report()
        self.assertIsNone(report.last_sale_day)
        self.assertIsNone(report.last_purchase_day)
        self.assertFalse(report.purchases_after_sales)
        self.assertTrue(report.sales_behind)

    def test_nothing_outside_the_window_counts(self):
        sold(self.pint, 4, day=date(2026, 3, 15))
        bought(self.blonde, "30", START)
        bought(self.blonde, "30", date(2026, 4, 2))
        sold(self.pint, 4, day=date(2026, 4, 2), source="caisse")
        report = self.report()
        self.assertIsNone(report.last_purchase_day)
        self.assertEqual(report.last_sale_day, date(2026, 3, 15))

    # The last day of sales imported is the till's, whatever the count's day
    # (`sold_on <= end`, no lower bound). Read inside the window, a count
    # taken yesterday - yesterday's sales imported this morning, in the count
    # already - read as « no sale imported » and the page said the sales
    # were not imported up to yesterday.
    def test_a_take_made_yesterday_with_yesterday_s_sales_is_not_behind(self):
        yesterday = END - timedelta(days=1)
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 30, 12, 0)))
        counted(take, self.blonde, "30")
        sold(self.pint, 12, day=yesterday)
        report = gaps_since(take, end=END)
        self.assertEqual(report.start, yesterday)
        self.assertEqual(report.last_sale_day, yesterday)
        self.assertFalse(report.sales_behind)
        # On the count's own day, those sales are in the count: no gap moves.
        self.assertEqual(report.articles[self.blonde.pk].sold, ZERO)
        self.assertEqual(report.articles[self.blonde.pk].gap, Decimal("30"))

    def test_the_last_sale_day_may_be_before_the_take(self):
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 30, 12, 0)))
        counted(take, self.blonde, "30")
        sold(self.pint, 4, day=date(2026, 2, 26))
        report = gaps_since(take, end=END)
        self.assertEqual(report.last_sale_day, date(2026, 2, 26))
        self.assertTrue(report.sales_behind)
        # A sale after the end is still none of it.
        sold(self.pint, 4, day=date(2026, 4, 2))
        self.assertEqual(gaps_since(take, end=END).last_sale_day, date(2026, 2, 26))

    def test_a_purchase_with_no_date_of_its_own_is_dated_by_its_invoice(self):
        sold(self.pint, 4, day=date(2026, 3, 15))
        invoice = make_invoice(invoice_date=date(2026, 3, 22))
        line = make_invoice_line(
            invoice=invoice, product=make_product(supplier=invoice.supplier, stock_type=self.blonde)
        )
        make_movement(stock_type=self.blonde, quantity="30", unit_cost_ht="4.00", invoice_line=line)
        report = self.report()
        self.assertEqual(report.last_purchase_day, date(2026, 3, 22))
        self.assertTrue(report.purchases_after_sales)
        self.assertEqual(report.articles[self.blonde.pk].purchases, Decimal("30"))


class ShareSummaryTests(TestCase):
    """(mean, lowest, highest) share of their room the plan fills."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        cls.cider = article("Cidre exemple")
        counted(cls.take, cls.blonde, "50")  # room 50 - 0,5 - 5 = 44,5
        counted(cls.take, cls.cider, "20")  # room 20 - 0,5 - 2 = 17,5
        cls.premium, cls.standard, cls.shot = ou_rums(cls.take, "2", "1")
        pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        bowl = recipe("Bolée exemple", "32.00", (cls.cider, "0.5"))
        for made in (pint, bowl, cls.shot):
            sold(made, 1)

    def test_over_every_target_but_the_secondary_side(self):
        report = gaps_since(self.take, end=END)
        self.assertTrue(report.articles[self.standard.pk].secondary)
        self.assertEqual(report.articles[self.blonde.pk].room, Decimal("44.5"))
        self.assertEqual(report.articles[self.cider.pk].room, Decimal("17.5"))
        self.assertEqual(report.articles[self.premium.pk].room, Decimal("1.76"))  # 2 - 0,04 - 0,2
        report.articles[self.blonde.pk].proposed = Decimal("8.9")  # 20 % of 44,5
        report.articles[self.cider.pk].proposed = Decimal("8.75")  # 50 % of 17,5
        report.articles[self.premium.pk].proposed = Decimal("0.44")  # 25 % of 1,76
        report.articles[self.standard.pk].proposed = ZERO  # 0 %, but secondary: left out
        shares = [Decimal("20"), Decimal("50"), Decimal("25")]
        self.assertEqual(share_summary(report), (sum(shares, start=ZERO) / 3, min(shares), max(shares)))

    def test_none_when_no_gap_can_be_filled(self):
        take = make_stock_take(taken_at=at(2))
        report = gaps_since(take, end=END)
        self.assertEqual(report.targets, [])
        self.assertIsNone(share_summary(report))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


class SimpleBarTests(TestCase):
    """One keg, a pint and a half of it; one rum and its shot.

    Blonde: 30 + 60 - (40 x 0,5 + 20 x 0,25) = 65 L, room 56 L.
    Rum: 2 + 3 - 25 x 0,04 = 4 L, room 3,5 L.
    Prices 35, 31 and 32 €: 99 € is one pint and two shots and nothing else,
    66 € a pint and a half and nothing else."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        cls.rum = article("Rhum exemple")
        counted(cls.take, cls.blonde, "30")
        counted(cls.take, cls.rum, "2")
        bought(cls.blonde, "60", date(2026, 3, 10))
        bought(cls.rum, "3", date(2026, 3, 12), unit_cost="20.00")
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        cls.half = recipe("Demi exemple", "31.00", (cls.blonde, "0.25"))
        cls.shot = recipe("Shot exemple", "32.00", (cls.rum, "0.04"))
        sold(cls.pint, 40)
        sold(cls.half, 20)
        sold(cls.shot, 25)
        till_button("PINTE BLONDE EXEMPLE", cls.pint, rung=40)
        till_button("SHOT RHUM EXEMPLE", cls.shot, rung=25)

    def setUp(self):
        self.report = gaps_since(self.take, end=END)

    def test_the_rooms(self):
        self.assertEqual(self.report.articles[self.blonde.pk].room, Decimal("56"))
        self.assertEqual(self.report.articles[self.rum.pk].room, Decimal("3.5"))

    def test_an_exact_total_with_its_only_combination(self):
        result = fill_gaps(self.report, Decimal("99.00"))
        self.assertEqual(result.plan.reason, EXACT)
        self.assertEqual(result.plan.counts, {self.pint.pk: 1, self.shot.pk: 2})
        self.assertEqual(result.total, Decimal("99"))
        self.assertEqual(result.remainder, ZERO)
        self.assertEqual(result.sales, 3)

    def test_lines_are_sorted_by_total_and_name_what_they_fill(self):
        result = fill_gaps(self.report, Decimal("99.00"))
        self.assertEqual(
            [(line.recipe, line.till.name, line.count, line.price, line.total) for line in result.lines],
            [
                (self.shot, "SHOT RHUM EXEMPLE", 2, Decimal("32.00"), Decimal("64.00")),
                (self.pint, "PINTE BLONDE EXEMPLE", 1, Decimal("35.00"), Decimal("35.00")),
            ],
        )
        fills = {line.recipe.name: [row.stock_type for row in line.fills] for line in result.lines}
        self.assertEqual(fills, {"Shot exemple": [self.rum], "Pinte exemple": [self.blonde]})

    def test_what_the_plan_adds_is_on_each_article(self):
        fill_gaps(self.report, Decimal("99.00"))
        blonde, rum = self.report.articles[self.blonde.pk], self.report.articles[self.rum.pk]
        self.assertEqual(blonde.proposed, Decimal("0.5"))
        self.assertEqual(rum.proposed, Decimal("0.08"))
        self.assertEqual(blonde.filled_percent, Decimal("0.5") / Decimal("56") * 100)
        self.assertEqual(rum.filled_percent, Decimal("0.08") / Decimal("3.5") * 100)
        mean = (blonde.filled_percent + rum.filled_percent) / 2
        self.assertEqual(share_summary(self.report), (mean, blonde.filled_percent, rum.filled_percent))

    def test_a_pint_and_a_half_of_the_same_keg(self):
        result = fill_gaps(self.report, Decimal("66.00"))
        self.assertEqual(result.plan.reason, EXACT)
        self.assertEqual(result.plan.counts, {self.pint.pk: 1, self.half.pk: 1})
        self.assertEqual([line.recipe for line in result.lines], [self.pint, self.half])
        self.assertEqual([line.till.name for line in result.lines], ["PINTE BLONDE EXEMPLE", "Demi exemple"])
        self.assertEqual(self.report.articles[self.blonde.pk].proposed, Decimal("0.75"))

    # 420 € is twelve pints, or 3 pints + 5 halves + 5 shots, every one
    # within its room. It once came out at 400 €, « no_combination »: the
    # ranking spent until 70 € was left, and nothing between 36 and 61 € is
    # made of 31, 32 and 35 €. Every sale now leaves a rest the prices make.
    def test_an_exact_total_is_found_whenever_a_combination_exists(self):
        result = fill_gaps(self.report, Decimal("420.00"))
        self.assertEqual(result.plan.reason, EXACT)
        self.assertEqual(result.total, Decimal("420"))

    def test_under_the_cheapest_recipe(self):
        result = fill_gaps(self.report, Decimal("20.00"))
        self.assertEqual(result.plan.reason, BELOW_CHEAPEST)
        self.assertEqual(result.lines, [])
        self.assertEqual(result.total, ZERO)
        self.assertEqual(result.remainder, Decimal("20"))

    def test_every_plan_stays_within_every_room_and_adds_up(self):
        for amount in ("31.00", "64.00", "99.00", "150.00", "333.33", "500.50", "1000.00", str(MAX_AMOUNT)):
            with self.subTest(amount=amount):
                report = gaps_since(self.take, end=END)
                result = fill_gaps(report, Decimal(amount))
                self.assertEqual(result.total + result.remainder, Decimal(amount))
                self.assertEqual(sum((line.total for line in result.lines), start=ZERO), result.total)
                self.assertEqual(result.lines, sorted(result.lines, key=lambda line: -line.total))
                self.assertTrue(all(line.count > 0 for line in result.lines))
                for row in report.articles.values():
                    self.assertLessEqual(row.proposed, max(ZERO, row.room), row.stock_type.name)

    def test_fill_gaps_asks_the_database_nothing(self):
        with self.assertNumQueries(0):
            fill_gaps(self.report, Decimal("500.00"))


class GapsFullTests(TestCase):
    """Rum: 0,2 L counted, 1 shot sold - room 0,16 - 0,02 = 0,14 L: three
    shots fit (0,12), a fourth would not."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.rum = article("Rhum exemple")
        bought(cls.rum, "1", BEFORE_THE_TAKE, unit_cost="20.00")
        counted(cls.take, cls.rum, "0.2")
        cls.shot = recipe("Shot exemple", "32.00", (cls.rum, "0.04"))
        sold(cls.shot, 1)

    def test_the_plan_stops_at_the_room_and_says_so(self):
        report = gaps_since(self.take, end=END)
        self.assertEqual(report.articles[self.rum.pk].room, Decimal("0.14"))
        result = fill_gaps(report, Decimal("200.00"))
        self.assertEqual(result.plan.reason, GAPS_FULL)
        self.assertEqual(result.plan.counts, {self.shot.pk: 3})
        self.assertEqual(result.total, Decimal("96"))
        self.assertEqual(result.remainder, Decimal("104"))
        self.assertEqual(report.articles[self.rum.pk].proposed, Decimal("0.12"))


class NothingToFillTests(TestCase):
    """Every gap within its allowance: no recipe can be proposed."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.cider = article("Cidre exemple")
        counted(cls.take, cls.cider, "10")
        cls.bowl = recipe("Bolée exemple", "32.00", (cls.cider, "0.5"))
        sold(cls.bowl, 19)

    def test_nothing_is_proposed_and_the_reason_says_why(self):
        report = gaps_since(self.take, end=END)
        result = fill_gaps(report, Decimal("100.00"))
        self.assertEqual(result.plan.reason, NOTHING_TO_FILL)
        self.assertEqual(result.lines, [])
        self.assertEqual(result.remainder, Decimal("100"))
        self.assertIsNone(share_summary(report))


# ---------------------------------------------------------------------------
# A list, amount after amount
# ---------------------------------------------------------------------------


class ListOnTopTests(TestCase):
    """SimpleBarTests' bar - blonde room 56 L, rum room 3,5 L; a pint at
    35 €, a half at 31 €, a shot at 32 € - with amounts added one after the
    other. Each is planned on top of the list's earlier sales, and every gap
    shows what the WHOLE list adds. 99 € is one pint and two shots and
    nothing else; 35 € is one pint."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        cls.rum = article("Rhum exemple")
        counted(cls.take, cls.blonde, "30")
        counted(cls.take, cls.rum, "2")
        bought(cls.blonde, "60", date(2026, 3, 10))
        bought(cls.rum, "3", date(2026, 3, 12), unit_cost="20.00")
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        cls.half = recipe("Demi exemple", "31.00", (cls.blonde, "0.25"))
        cls.shot = recipe("Shot exemple", "32.00", (cls.rum, "0.04"))
        sold(cls.pint, 40)
        sold(cls.half, 20)
        sold(cls.shot, 25)

    def setUp(self):
        self.report = gaps_since(self.take, end=END)

    def engine_adds(self, report, counts):
        """{article: what `counts` add to it}, as the stock page's engine
        attributes them - every article of the report, 0 included."""
        base, after = report.consumption({}), report.consumption(counts)
        return {article_id: after.get(article_id, ZERO) - base.get(article_id, ZERO) for article_id in report.articles}

    def proposed(self, report):
        return {article_id: row.proposed for article_id, row in report.articles.items()}

    def the_list(self):
        """The take's entries, oldest first."""
        return GapFillEntry.objects.filter(stock_take=self.take)

    def add(self, amount):
        """What the page does with an amount typed in: plan it on top of the
        list and keep it. Returns the report it was planned on, and the plan."""
        report = gaps_since(self.take, end=END)
        result = fill_gaps(report, Decimal(amount), list_counts(self.the_list()))
        GapFillEntry.objects.create(
            stock_take=self.take,
            amount=Decimal(amount),
            total=result.total,
            reason="" if result.plan.reason == EXACT else result.plan.reason,
            lines=entry_lines(result),
            sales_up_to=report.last_sale_day,
        )
        return report, result

    def test_a_second_amount_is_planned_on_top_of_the_first(self):
        first = fill_gaps(self.report, Decimal("99.00"))
        self.assertEqual(first.plan.counts, {self.pint.pk: 1, self.shot.pk: 2})
        report = gaps_since(self.take, end=END)
        second = fill_gaps(report, Decimal("99.00"), dict(first.plan.counts))
        # The lines are the new sales only...
        self.assertEqual(second.plan.counts, {self.pint.pk: 1, self.shot.pk: 2})
        self.assertEqual([(line.recipe, line.count) for line in second.lines], [(self.shot, 2), (self.pint, 1)])
        self.assertEqual((second.total, second.remainder, second.sales), (Decimal("99"), ZERO, 3))
        # ...and every gap shows what the two add together.
        self.assertEqual(report.articles[self.blonde.pk].proposed, Decimal("1.0"))
        self.assertEqual(report.articles[self.rum.pk].proposed, Decimal("0.16"))
        self.assertEqual(report.articles[self.rum.pk].filled_percent, Decimal("0.16") / Decimal("3.5") * 100)

    def test_every_gap_shows_what_the_whole_list_adds(self):
        for amount in ("99.00", "66.00", "150.00", "35.00", "64.00"):
            with self.subTest(amount=amount):
                report, result = self.add(amount)
                self.assertTrue(result.lines, "nothing proposed: the test proves nothing")
                whole = list_counts(self.the_list())
                self.assertEqual(self.proposed(report), self.engine_adds(report, whole))
                for row in report.articles.values():
                    self.assertLessEqual(row.proposed, max(ZERO, row.room), row.stock_type.name)
        # Shown again with no new amount, the list reads the same.
        self.assertEqual(GapFillEntry.objects.count(), 5)
        whole = list_counts(self.the_list())
        report = gaps_since(self.take, end=END)
        show_list(report, whole)
        self.assertEqual(self.proposed(report), self.engine_adds(report, whole))

    def test_show_list_writes_what_the_list_adds_and_asks_nothing(self):
        with self.assertNumQueries(0):
            show_list(self.report, {self.pint.pk: 2, self.half.pk: 1, self.shot.pk: 4})
        self.assertEqual(self.report.articles[self.blonde.pk].proposed, Decimal("1.25"))
        self.assertEqual(self.report.articles[self.rum.pk].proposed, Decimal("0.16"))

    def test_show_list_of_an_empty_list_leaves_every_gap_at_zero(self):
        show_list(self.report, {})
        self.assertEqual(set(self.proposed(self.report).values()), {ZERO})

    def test_fill_gaps_on_top_of_a_list_asks_the_database_nothing(self):
        with self.assertNumQueries(0):
            fill_gaps(self.report, Decimal("150.00"), {self.pint.pk: 3, self.shot.pk: 2})

    def test_a_recipe_of_the_list_off_the_menu_since_still_counts(self):
        # The half was proposed, then its price taken off: it is no offer
        # now, and the list's two halves still fill the keg.
        Recipe.objects.filter(pk=self.half.pk).update(selling_price_ttc=None)
        report = gaps_since(self.take, end=END)
        self.assertNotIn(self.half.pk, {offer.recipe_id for offer in report.offers})
        show_list(report, {self.half.pk: 2})
        self.assertEqual(report.articles[self.blonde.pk].proposed, Decimal("0.5"))
        result = fill_gaps(report, Decimal("35.00"), {self.half.pk: 2})
        self.assertEqual(result.plan.counts, {self.pint.pk: 1})
        self.assertEqual([line.recipe for line in result.lines], [self.pint])
        self.assertEqual(report.articles[self.blonde.pk].proposed, Decimal("1.0"))

    def test_a_recipe_of_the_list_deleted_since_breaks_nothing(self):
        gone = recipe("Pinte supprimée exemple", "33.30", (self.blonde, "0.5"))
        gone_pk = gone.pk
        gone.delete()
        report = gaps_since(self.take, end=END)
        show_list(report, {gone_pk: 4})
        self.assertEqual(set(self.proposed(report).values()), {ZERO})
        result = fill_gaps(report, Decimal("35.00"), {gone_pk: 4})
        self.assertEqual(result.plan.counts, {self.pint.pk: 1})
        self.assertEqual(report.articles[self.blonde.pk].proposed, Decimal("0.5"))


class ListStaleTests(TestCase):
    """`list_is_stale` on real reports: till sales imported after an entry
    was made hold that entry's sales if it was rung up."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "30")
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 4, day=SALE_DAY)

    def entry(self, report):
        result = fill_gaps(report, Decimal("70.00"))
        return GapFillEntry.objects.create(
            stock_take=self.take,
            amount=Decimal("70.00"),
            total=result.total,
            lines=entry_lines(result),
            sales_up_to=report.last_sale_day,
        )

    def test_fresh_until_newer_sales_are_imported(self):
        report = gaps_since(self.take, end=END)
        entry = self.entry(report)
        self.assertEqual(entry.sales_up_to, SALE_DAY)
        self.assertFalse(list_is_stale(report, [entry]))
        # Sales of the same day, or older, are no newer day.
        sold(self.pint, 1, day=SALE_DAY, source="caisse")
        sold(self.pint, 1, day=date(2026, 3, 10), source="caisse")
        self.assertFalse(list_is_stale(gaps_since(self.take, end=END), [entry]))
        sold(self.pint, 2, day=SALE_DAY + timedelta(days=1), source="caisse")
        self.assertTrue(list_is_stale(gaps_since(self.take, end=END), [entry]))

    def test_an_entry_made_before_any_sale_was_imported(self):
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 20, 12, 0)))
        counted(take, self.blonde, "30")
        # The last day of sales has no lower bound at the take any more
        # (FreshnessTests): the class's sale of the 15th, before this take,
        # would be it. Taken out, no sale at all has been imported.
        RecipeSale.objects.all().delete()
        report = gaps_since(take, end=END)
        self.assertIsNone(report.last_sale_day)
        entry = GapFillEntry.objects.create(stock_take=take, amount=Decimal("35.00"), total=ZERO)
        self.assertFalse(list_is_stale(report, [entry]))
        sold(self.pint, 1, day=date(2026, 3, 25), source="caisse")
        self.assertTrue(list_is_stale(gaps_since(take, end=END), [entry]))


def rum_bar(take):
    """Two rums under an « OU », a punch on the cheaper and a neat pour of the
    dearer: the shape where a fixed ingredient pushes a choice off a bottle.

    Dear rum: 1 + 1 L, cheap rum: 2 + 1 L; 30 shots « au choix », 10 punches
    (0,05 L of the cheap one), 5 neat pours (0,04 L of the dear one)."""
    premium = article("Rhum vieux exemple")
    standard = article("Rhum blanc exemple")
    counted(take, premium, "1")
    counted(take, standard, "2")
    bought(premium, "1", date(2026, 3, 12), unit_cost="40.00")
    bought(standard, "1", date(2026, 3, 12), unit_cost="20.00")
    shot = choice("Shot au choix exemple", "33.00", (premium, "0.04"), (standard, "0.04"))
    ti_punch = recipe("Ti punch exemple", "34.00", (standard, "0.05"))
    neat = recipe("Rhum vieux sec exemple", "36.00", (premium, "0.04"))
    sold(shot, 30)
    sold(ti_punch, 10)
    sold(neat, 5)
    till_button("SHOT AU CHOIX EXEMPLE", shot, rung=30)
    till_button("TI PUNCH EXEMPLE", ti_punch, rung=10)
    return premium, standard, shot, ti_punch, neat


class ThePageCountsLikeTheStockPageTests(TestCase):
    """Play a plan, ring it up through the one door sales come in by, and
    read the stock page's own « Vendu » back: it must be what the page
    predicted, article by article - and no gap may have gone past its room."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.premium, cls.standard, cls.shot, cls.ti_punch, cls.neat = rum_bar(cls.take)
        cls.blonde = article("Blonde exemple")
        counted(cls.take, cls.blonde, "30")
        bought(cls.blonde, "60", date(2026, 3, 10))
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 40)

    def ring_up(self, amount):
        report = gaps_since(self.take, end=END)
        result = fill_gaps(report, Decimal(amount))
        self.assertTrue(result.lines, "nothing proposed: the test proves nothing")
        predicted = report.consumption(result.plan.counts)
        available = window_available(report)
        recorded = record_sales([(line.till.name, END, line.count) for line in result.lines], source="plan")
        self.assertEqual(recorded.unmatched, [])
        return report, result, predicted, available

    def test_the_closure_is_the_stock_page_once_the_sales_are_rung_up(self):
        proposed = set()
        for amount in ("150.00", "420.00", "1000.00"):
            with self.subTest(amount=amount):
                RecipeSale.objects.filter(source="plan").delete()
                report, result, predicted, available = self.ring_up(amount)
                proposed |= set(result.plan.counts)
                after = quantities_sold(START, END, available=available)
                for article_id, row in report.articles.items():
                    stock_page = after[article_id].headline if article_id in after else ZERO
                    self.assertEqual(stock_page, predicted.get(article_id, ZERO), row.stock_type.name)
        # The « OU » and the fixed pour that pushes it were both played.
        self.assertIn(self.shot.pk, proposed)
        self.assertIn(self.neat.pk, proposed)

    def test_what_the_page_proposed_is_what_the_next_report_sold(self):
        report, _result, _predicted, _available = self.ring_up("420.00")
        again = gaps_since(self.take, end=END)
        for article_id, before in report.articles.items():
            now = again.articles[article_id]
            name = before.stock_type.name
            self.assertEqual(now.sold - before.sold, before.proposed, name)
            self.assertEqual(now.room, before.room - before.proposed, name)
            self.assertGreaterEqual(now.room, min(ZERO, before.room), name)

    def test_with_nothing_added_the_closure_is_what_was_sold(self):
        report = gaps_since(self.take, end=END)
        base = report.consumption({})
        for article_id, row in report.articles.items():
            self.assertEqual(base.get(article_id, ZERO), row.sold, row.stock_type.name)


class AttributeSalesRegressionTests(TestCase):
    """`_quantities_sold` was cut in two - `read_sales` once, then
    `attribute_sales` - so the same sales can be attributed again with a few
    more added. Cut or not, the stock page must read the same figures.

    Worked out by hand: certain consumption first (5 neat pours = 0,2 L of
    the dear rum; 12 punches - 10 at the till, 2 on a sale document - and a
    0,7 L bottle sold as itself = 1,3 L of the cheap one); then 30 shots,
    dearest first up to its allowance: the dear rum holds 2 L, 1,8 once its
    10 % is set aside, 0,2 already gone, so 40 shots would fit - all 30 go
    there (1,2 L)."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.premium, cls.standard, cls.shot, cls.ti_punch, cls.neat = rum_bar(cls.take)
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 20))
        SaleDocumentLine.objects.create(document=document, stock_type=cls.standard, quantity=Decimal("0.7"))
        SaleDocumentLine.objects.create(document=document, recipe=cls.ti_punch, quantity=Decimal("2"))
        cls.available = {cls.premium.pk: Decimal("2"), cls.standard.pk: Decimal("3")}

    def attributed(self, extra=None):
        with variation_scope():
            return attribute_sales(read_sales(START, END), self.available, unit_costs_ht(), extra)

    def test_the_cut_reads_what_the_whole_did(self):
        unit_costs = unit_costs_ht()
        with variation_scope():
            whole = _quantities_sold(START, END, unit_costs, self.available)
        self.assertEqual(self.attributed(), whole)
        self.assertEqual(quantities_sold(START, END, unit_costs=unit_costs, available=self.available), whole)

    def test_the_figures_worked_out_by_hand(self):
        result = self.attributed()
        premium, standard = result[self.premium.pk], result[self.standard.pk]
        self.assertEqual((premium.exact, premium.shared), (Decimal("0.2"), Decimal("1.2")))
        self.assertEqual((standard.exact, standard.shared), (Decimal("1.3"), ZERO))
        self.assertEqual(premium.pool, frozenset({self.premium.pk, self.standard.pk}))
        self.assertEqual(premium.available, Decimal("2"))

    def test_extra_shots_spill_onto_the_cheaper_rum_past_the_allowance(self):
        """30 shots more, 60 in all: 40 fit on the dear rum in round one
        (1,6 L); the cheap rum's 2,7 L less the 1,3 L certain leaves room
        for 35, so it takes the other 20 (0,8 L)."""
        result = self.attributed({self.shot.pk: 30})
        premium, standard = result[self.premium.pk], result[self.standard.pk]
        self.assertEqual(premium.shared, Decimal("1.6"))
        self.assertEqual(standard.shared, Decimal("0.8"))

    def test_a_fixed_pour_pushes_a_choice_onto_the_other_bottle(self):
        """Five more neat pours of the dear rum (0,2 L certain) leave the
        shots 0,2 L less of it: five shots move to the cheap rum. The dear
        rum's « Vendu » does not move; the cheap one's takes 0,2 L."""
        before = self.attributed({self.shot.pk: 30})
        after = self.attributed({self.shot.pk: 30, self.neat.pk: 5})
        self.assertEqual(after[self.premium.pk].headline, before[self.premium.pk].headline)
        self.assertEqual(after[self.premium.pk].exact, Decimal("0.4"))
        self.assertEqual(after[self.standard.pk].headline - before[self.standard.pk].headline, Decimal("0.2"))

    def test_extra_is_what_recording_the_same_sales_gives(self):
        predicted = self.attributed({self.shot.pk: 7, self.neat.pk: 3})
        sold(self.shot, 7, day=END, source="plan")
        sold(self.neat, 3, day=END, source="plan")
        self.assertEqual(self.attributed(), predicted)


# ---------------------------------------------------------------------------
# Articles and categories left out
# ---------------------------------------------------------------------------


def figures(row):
    """What the stock page's engine says of an article, and what follows."""
    return (
        row.opening,
        row.purchases,
        row.known_losses,
        row.counted,
        row.sold,
        row.gap,
        row.allowance,
        row.room,
        row.unit_cost,
        row.status,
    )


class ExclusionTests(TestCase):
    """What the owner left out (`GapExclusion`): an article alone, or every
    article filed under a category name. A small bar:

    * Blonde (« Bières exemple »): 30 L counted, 60 L bought at 4,00 €, 40
      pints of 0,5 L sold - gap 70 L, allowance 9, room 61 L (244,00 €).
    * Ambrée (« Bières exemple »): 20 L counted, 10 pints - gap 15 L,
      allowance 2, room 13 L (65,00 € at 5,00 €).
    * Rum (« Spiritueux exemple »): 2 L counted, 3 L bought at 20,00 €, 25
      shots of 4 cl and 10 mojitos of 5 cl - gap 3,5 L, allowance 0,5,
      room 3 L (60,00 €).
    * Mint (no category): 0,1 kg counted, the mojitos took 0,2 - « vendu
      plus qu'acheté »: the mojito is held back by it.
    * Gin (« Spiritueux exemple »): 2 L counted, poured only by a gin tonic
      nobody ordered since the take - unreached, room 1,8 L.
    * Tablecloths (no category): 10 bought, in no recipe - room 9.
    """

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.blonde = article("Blonde exemple", category="Bières exemple")
        counted(cls.take, cls.blonde, "30")
        bought(cls.blonde, "60", date(2026, 3, 10))
        cls.pint = recipe("Pinte exemple", "35.00", (cls.blonde, "0.5"))
        sold(cls.pint, 40)

        cls.amber = article("Ambrée exemple", category="Bières exemple")
        bought(cls.amber, "1", BEFORE_THE_TAKE, unit_cost="5.00")
        counted(cls.take, cls.amber, "20")
        cls.amber_pint = recipe("Pinte ambrée exemple", "36.00", (cls.amber, "0.5"))
        sold(cls.amber_pint, 10)

        cls.rum = article("Rhum exemple", category="Spiritueux exemple")
        counted(cls.take, cls.rum, "2")
        bought(cls.rum, "3", date(2026, 3, 12), unit_cost="20.00")
        cls.shot = recipe("Shot exemple", "32.00", (cls.rum, "0.04"))
        sold(cls.shot, 25)

        cls.mint = article("Menthe exemple", unit=UnitChoices.KILOGRAM)
        counted(cls.take, cls.mint, "0.1")
        cls.mojito = recipe("Mojito exemple", "38.00", (cls.rum, "0.05"), (cls.mint, "0.02"))
        sold(cls.mojito, 10)

        cls.gin = article("Gin exemple", category="Spiritueux exemple")
        bought(cls.gin, "1", BEFORE_THE_TAKE, unit_cost="30.00")
        counted(cls.take, cls.gin, "2")
        cls.gin_tonic = recipe("Gin tonic exemple", "34.00", (cls.gin, "0.05"))

        cls.cloth = article("Nappe exemple", unit=UnitChoices.UNIT)
        bought(cls.cloth, "10", date(2026, 3, 12), unit_cost="2.00")

    def report(self):
        return gaps_since(self.take, end=END)

    def names(self, rows):
        return [row.stock_type.name for row in rows]

    def offered(self, report):
        return {offer.recipe_id for offer in report.offers}

    def held_back(self, report):
        return {row.recipe.name: self.names(row.articles) for row in report.blocked}

    def test_with_nothing_left_out(self):
        report = self.report()
        self.assertEqual((report.ignored, report.exclusions), (frozenset(), []))
        self.assertFalse(any(row.excluded for row in report.articles.values()))
        rooms = {row.stock_type.name: row.room for row in report.articles.values()}
        self.assertEqual(
            rooms,
            {
                "Blonde exemple": Decimal("61"),
                "Ambrée exemple": Decimal("13"),
                "Rhum exemple": Decimal("3"),
                "Menthe exemple": Decimal("-0.11"),
                "Gin exemple": Decimal("1.8"),
                "Nappe exemple": Decimal("9"),
            },
        )
        self.assertEqual(report.articles[self.mint.pk].status, OVER)
        self.assertEqual(self.offered(report), {self.pint.pk, self.amber_pint.pk, self.shot.pk})
        self.assertEqual(self.held_back(report), {"Mojito exemple": ["Menthe exemple"]})
        self.assertEqual(
            self.names(report.rows), ["Blonde exemple", "Ambrée exemple", "Rhum exemple", "Menthe exemple"]
        )
        self.assertEqual(self.names(report.unreached), ["Gin exemple"])
        self.assertEqual(report.outside_recipes, 1)
        self.assertEqual((report.excluded_rows, report.excluded_categories, report.excluded_articles), ([], [], []))
        self.assertEqual(report.categories_to_exclude, [("Bières exemple", 2), ("Spiritueux exemple", 2), ("", 2)])

    def test_an_article_left_out_alone(self):
        exclusion = GapExclusion.objects.create(stock_type=self.rum)
        report = self.report()
        rum = report.articles[self.rum.pk]
        self.assertTrue(rum.excluded)
        self.assertEqual(report.ignored, frozenset({self.rum.pk}))
        # Its figures stay the stock page's...
        self.assertEqual((rum.sold, rum.room, rum.status), (Decimal("1.5"), Decimal("3"), FILLABLE))
        # ...but it is no gap to fill: no target, off the table, and not
        # « unreached » either, though a recipe pours it and none fills it.
        self.assertFalse(rum.target)
        self.assertFalse(rum.secondary)
        self.assertNotIn(rum, report.targets)
        self.assertEqual(self.names(report.rows), ["Blonde exemple", "Ambrée exemple", "Menthe exemple"])
        self.assertEqual(self.names(report.unreached), ["Gin exemple"])
        self.assertEqual(report.outside_recipes, 1)
        self.assertEqual(report.excluded_rows, [rum])
        self.assertEqual(report.excluded_articles, [exclusion])
        self.assertEqual(report.excluded_categories, [])
        # The other spirit, under the same category, is not touched.
        self.assertFalse(report.articles[self.gin.pk].excluded)
        self.assertEqual(report.categories_to_exclude, [("Bières exemple", 2), ("Spiritueux exemple", 2), ("", 2)])

    def test_a_recipe_filling_only_an_article_left_out_is_never_proposed(self):
        GapExclusion.objects.create(stock_type=self.rum)
        report = self.report()
        # Nothing holds the shot back - it is offered - but it fills no gap:
        # at 32,00 € the cheapest recipe left is the 35,00 € pint.
        self.assertIn(self.shot.pk, self.offered(report))
        result = fill_gaps(report, Decimal("32.00"))
        self.assertEqual(result.plan.reason, BELOW_CHEAPEST)
        self.assertEqual(result.lines, [])
        for amount in ("64.00", "96.00", "500.00"):
            with self.subTest(amount=amount):
                result = fill_gaps(self.report(), Decimal(amount))
                self.assertNotIn(self.shot.pk, result.plan.counts)

    def test_the_average_leaves_an_article_left_out_out(self):
        GapExclusion.objects.create(stock_type=self.amber)
        report = self.report()
        # 67,00 € is a pint and a shot, and nothing else.
        result = fill_gaps(report, Decimal("67.00"))
        self.assertEqual(result.plan.counts, {self.pint.pk: 1, self.shot.pk: 1})
        amber = report.articles[self.amber.pk]
        self.assertEqual(amber.proposed, ZERO)
        self.assertIsNone(amber.filled_percent)
        # Counted, the amber would read 0 % as the lowest share.
        blonde = Decimal("0.5") / Decimal("61") * 100
        rum = Decimal("0.04") / Decimal("3") * 100
        self.assertEqual(share_summary(report), ((blonde + rum) / 2, blonde, rum))

    def test_a_category_left_out_takes_every_article_filed_under_it(self):
        exclusion = GapExclusion.objects.create(category="Spiritueux exemple")
        report = self.report()
        rum, gin = report.articles[self.rum.pk], report.articles[self.gin.pk]
        self.assertTrue(rum.excluded)
        self.assertTrue(gin.excluded)
        self.assertEqual(report.ignored, frozenset({self.rum.pk, self.gin.pk}))
        self.assertFalse(rum.target)
        self.assertEqual(self.names(report.rows), ["Blonde exemple", "Ambrée exemple", "Menthe exemple"])
        # The gin was the one gap left unreached.
        self.assertEqual(report.unreached, [])
        self.assertEqual(report.unreached_value, ZERO)
        self.assertEqual(report.excluded_rows, [gin, rum])
        self.assertEqual(report.excluded_categories, [exclusion])
        self.assertEqual(report.excluded_categories[0].covers, 2)
        self.assertEqual(report.excluded_articles, [])
        self.assertEqual(report.categories_to_exclude, [("Bières exemple", 2), ("", 2)])
        # The mint still holds the mojito back.
        self.assertEqual(self.held_back(report), {"Mojito exemple": ["Menthe exemple"]})

    def test_a_recipe_held_back_only_by_an_article_left_out_is_proposed_again(self):
        report = self.report()
        self.assertNotIn(self.mojito.pk, self.offered(report))
        self.assertNotIn(self.mojito.pk, fill_gaps(report, Decimal("38.00")).plan.counts)

        GapExclusion.objects.create(stock_type=self.mint)
        report = self.report()
        mint = report.articles[self.mint.pk]
        self.assertEqual(mint.status, OVER)  # what the stock page says, still
        self.assertEqual(report.blocked, [])
        self.assertIn(self.mojito.pk, self.offered(report))
        self.assertEqual(self.names(report.rows), ["Blonde exemple", "Ambrée exemple", "Rhum exemple"])
        self.assertEqual(self.names(report.excluded_rows), ["Menthe exemple"])
        # 38,00 € is one mojito and nothing else.
        with self.assertNumQueries(0):
            result = fill_gaps(report, Decimal("38.00"))
        self.assertEqual((result.plan.counts, result.plan.reason), ({self.mojito.pk: 1}, EXACT))
        (line,) = result.lines
        self.assertEqual([row.stock_type for row in line.fills], [self.rum])
        # What it adds to the mint is still shown: past a room the mint no
        # longer has, and filling nothing.
        self.assertEqual(mint.proposed, Decimal("0.02"))
        self.assertIsNone(mint.filled_percent)
        self.assertEqual(report.articles[self.rum.pk].proposed, Decimal("0.05"))

    def test_a_recipe_held_back_by_two_articles_names_the_one_still_counted(self):
        lime = article("Citron vert exemple", unit=UnitChoices.KILOGRAM, category="Fruits exemple")
        counted(self.take, lime, "0.1")
        caipi = recipe("Caïpi exemple", "37.00", (self.mint, "0.01"), (lime, "0.02"))
        sold(caipi, 10)  # 0,2 kg of the lime's 0,1, and the mint further over
        self.assertEqual(
            self.held_back(self.report()),
            {"Caïpi exemple": ["Citron vert exemple", "Menthe exemple"], "Mojito exemple": ["Menthe exemple"]},
        )
        GapExclusion.objects.create(stock_type=self.mint)
        report = self.report()
        self.assertEqual(self.held_back(report), {"Caïpi exemple": ["Citron vert exemple"]})
        self.assertNotIn(caipi.pk, self.offered(report))
        self.assertIn(self.mojito.pk, self.offered(report))
        # Both left out: nothing holds it back, and it fills no gap.
        GapExclusion.objects.create(category="Fruits exemple")
        report = self.report()
        self.assertEqual(report.blocked, [])
        self.assertIn(caipi.pk, self.offered(report))
        for amount in ("37.00", "74.00", "111.00"):
            with self.subTest(amount=amount):
                self.assertNotIn(caipi.pk, fill_gaps(report, Decimal(amount)).plan.counts)

    def test_with_every_article_left_out_there_is_nothing_to_fill(self):
        for category in ("Bières exemple", "Spiritueux exemple", ""):
            GapExclusion.objects.create(category=category)
        report = self.report()
        self.assertEqual(report.ignored, frozenset(report.articles))
        self.assertEqual((report.targets, report.rows, report.unreached, report.outside_recipes), ([], [], [], 0))
        self.assertEqual(len(report.excluded_rows), 6)
        self.assertEqual(report.categories_to_exclude, [])
        # Nothing is held back any more, and nothing fills a gap.
        self.assertEqual(report.blocked, [])
        self.assertEqual(self.offered(report), {self.pint.pk, self.amber_pint.pk, self.shot.pk, self.mojito.pk})
        result = fill_gaps(report, Decimal("100.00"))
        self.assertEqual(result.plan.reason, NOTHING_TO_FILL)
        self.assertEqual((result.lines, result.total, result.remainder), ([], ZERO, Decimal("100")))
        self.assertIsNone(share_summary(report))

    def test_rows_left_out_counts_the_gaps_of_the_table_left_out(self):
        """What an empty table says rests on this: how many of the gaps a
        recipe of the menu touches - poured by a recipe offered, or holding
        one back - are left out. Not every article left out: the gin (its gin
        tonic not sold since) and the tablecloths (in no recipe) would be no
        row anyway."""
        report = self.report()
        with self.assertNumQueries(0):
            self.assertEqual(report.rows_left_out, 0)
        cases = (
            ([{"stock_type": self.rum}], 1),
            # The rum and the gin: the gin is no row.
            ([{"category": "Spiritueux exemple"}], 1),
            # The mint, holding the mojito back, and the tablecloths.
            ([{"category": ""}], 1),
            ([{"stock_type": self.gin}, {"stock_type": self.cloth}], 0),
            ([{"category": "Bières exemple"}, {"stock_type": self.mint}], 3),
        )
        for exclusions, left_out in cases:
            with self.subTest(exclusions=exclusions):
                GapExclusion.objects.all().delete()
                for fields in exclusions:
                    GapExclusion.objects.create(**fields)
                report = self.report()
                self.assertEqual(report.rows_left_out, left_out)
                self.assertTrue(report.rows)

    def test_with_every_article_left_out_rows_left_out_is_every_row(self):
        report = self.report()
        rows = len(report.rows)
        self.assertEqual(rows, 4)
        for category in ("Bières exemple", "Spiritueux exemple", ""):
            GapExclusion.objects.create(category=category)
        report = self.report()
        # Six articles left out; four of them the table's: the blonde, the
        # amber, the rum, and the mint - the mojito it held back offered now.
        self.assertEqual(len(report.ignored), 6)
        self.assertEqual((report.rows, report.rows_left_out), ([], rows))

    def test_a_category_left_out_covers_an_article_classified_into_it_later(self):
        exclusion = GapExclusion.objects.create(category="Herbes exemple")
        report = self.report()
        self.assertFalse(report.articles[self.mint.pk].excluded)
        self.assertEqual(report.excluded_categories[0].covers, 0)
        # The mint classified into it, and an article created since under it.
        StockType.objects.filter(pk=self.mint.pk).update(category="Herbes exemple")
        basil = article("Basilic exemple", unit=UnitChoices.KILOGRAM, category="Herbes exemple")
        counted(self.take, basil, "0.5")
        report = self.report()
        self.assertTrue(report.articles[self.mint.pk].excluded)
        self.assertTrue(report.articles[basil.pk].excluded)
        self.assertEqual(report.excluded_categories, [exclusion])
        self.assertEqual(report.excluded_categories[0].covers, 2)
        self.assertEqual(report.blocked, [])
        self.assertIn(self.mojito.pk, self.offered(report))
        # The basil, in no recipe, is not counted among what no sale fills.
        self.assertEqual(report.outside_recipes, 1)

    def test_a_category_is_matched_as_written_case_and_accents_included(self):
        for written in ("bières exemple", "Bieres exemple", "BIÈRES EXEMPLE"):
            GapExclusion.objects.create(category=written)
        report = self.report()
        self.assertFalse(report.articles[self.blonde.pk].excluded)
        self.assertFalse(report.articles[self.amber.pk].excluded)
        self.assertEqual(report.ignored, frozenset())
        self.assertEqual(
            {exclusion.category: exclusion.covers for exclusion in report.excluded_categories},
            {"bières exemple": 0, "Bieres exemple": 0, "BIÈRES EXEMPLE": 0},
        )
        self.assertIn(("Bières exemple", 2), report.categories_to_exclude)
        GapExclusion.objects.create(category="Bières exemple")
        report = self.report()
        self.assertTrue(report.articles[self.blonde.pk].excluded)
        self.assertTrue(report.articles[self.amber.pk].excluded)
        self.assertEqual(
            {exclusion.category: exclusion.covers for exclusion in report.excluded_categories},
            {"bières exemple": 0, "Bieres exemple": 0, "BIÈRES EXEMPLE": 0, "Bières exemple": 2},
        )
        self.assertNotIn("Bières exemple", dict(report.categories_to_exclude))

    def test_the_blank_category_left_out_covers_the_articles_with_none(self):
        exclusion = GapExclusion.objects.create(category="")
        report = self.report()
        self.assertEqual(report.ignored, frozenset({self.mint.pk, self.cloth.pk}))
        # The tablecloths were what no sale fills; the mint held the mojito.
        self.assertEqual(report.outside_recipes, 0)
        self.assertEqual(report.blocked, [])
        self.assertEqual(report.excluded_categories, [exclusion])
        self.assertEqual(report.excluded_categories[0].covers, 2)
        self.assertEqual(report.categories_to_exclude, [("Bières exemple", 2), ("Spiritueux exemple", 2)])
        for stock_type in (self.blonde, self.amber, self.rum, self.gin):
            self.assertFalse(report.articles[stock_type.pk].excluded, stock_type.name)

    def test_what_is_left_out_is_listed_in_order_with_what_it_covers(self):
        # Articles nothing moved since the take: in no report.
        absent = make_stock_type(name="Sirop absent exemple", category="Sirops exemple")
        liqueur = make_stock_type(name="Liqueur exemple", category="Spiritueux exemple")
        GapExclusion.objects.create(category="")
        GapExclusion.objects.create(category="Spiritueux exemple")
        GapExclusion.objects.create(category="Absente exemple")
        GapExclusion.objects.create(stock_type=self.rum)  # under « Spiritueux exemple » too
        GapExclusion.objects.create(stock_type=absent)
        GapExclusion.objects.create(stock_type=self.amber)
        report = self.report()
        self.assertNotIn(absent.pk, report.articles)
        self.assertNotIn(liqueur.pk, report.articles)
        with self.assertNumQueries(0):
            categories = [(exclusion.category, exclusion.covers) for exclusion in report.excluded_categories]
            articles = [exclusion.stock_type.name for exclusion in report.excluded_articles]
            rows = self.names(report.excluded_rows)
            to_exclude = report.categories_to_exclude
        # By name, case aside, the blank one last; what each covers is the
        # report's articles - not the liqueur, which nothing moved.
        self.assertEqual(categories, [("Absente exemple", 0), ("Spiritueux exemple", 2), ("", 2)])
        self.assertEqual(articles, ["Ambrée exemple", "Rhum exemple", "Sirop absent exemple"])
        self.assertEqual(rows, ["Ambrée exemple", "Gin exemple", "Menthe exemple", "Nappe exemple", "Rhum exemple"])
        # The amber, left out alone, still counts under its category.
        self.assertEqual(to_exclude, [("Bières exemple", 2)])

    def test_an_article_left_out_alone_says_whether_its_category_is_out_too(self):
        """`.covered`: taking such an article back alone changes nothing, and
        the fold says so on its line. The blank category covers the articles
        with none, as it leaves them out."""
        GapExclusion.objects.create(stock_type=self.rum)
        GapExclusion.objects.create(stock_type=self.amber)
        GapExclusion.objects.create(stock_type=self.mint)
        report = self.report()
        self.assertEqual([exclusion.covered for exclusion in report.excluded_articles], [False, False, False])
        GapExclusion.objects.create(category="Spiritueux exemple")
        GapExclusion.objects.create(category="")
        report = self.report()
        with self.assertNumQueries(0):
            covered = {exclusion.stock_type.name: exclusion.covered for exclusion in report.excluded_articles}
        self.assertEqual(covered, {"Ambrée exemple": False, "Menthe exemple": True, "Rhum exemple": True})
        # A category written otherwise covers nothing.
        GapExclusion.objects.create(category="bières exemple")
        report = self.report()
        self.assertFalse(next(e.covered for e in report.excluded_articles if e.stock_type_id == self.amber.pk))

    def test_categories_to_exclude_go_by_name_case_aside_the_blank_one_last(self):
        cider = article("Cidre exemple", category="cidres exemple")
        counted(self.take, cider, "5")
        self.assertEqual(
            self.report().categories_to_exclude,
            [("Bières exemple", 2), ("cidres exemple", 1), ("Spiritueux exemple", 2), ("", 2)],
        )

    def test_the_stock_page_s_figures_do_not_move(self):
        before = self.report()
        available = window_available(before)
        stock_page = quantities_sold(START, END, available=available)
        GapExclusion.objects.create(stock_type=self.rum)
        for category in ("Bières exemple", ""):
            GapExclusion.objects.create(category=category)
        after = self.report()
        self.assertEqual(
            after.ignored, frozenset({self.rum.pk, self.blonde.pk, self.amber.pk, self.mint.pk, self.cloth.pk})
        )
        self.assertEqual(set(after.articles), set(before.articles))
        for article_id, row in before.articles.items():
            self.assertEqual(figures(after.articles[article_id]), figures(row), row.stock_type.name)
        for extra in ({}, {self.pint.pk: 3, self.mojito.pk: 2}, {self.shot.pk: 7}):
            with self.subTest(extra=extra):
                self.assertEqual(after.consumption(extra), before.consumption(extra))
        # The stock page itself, which knows nothing of what is left out.
        self.assertEqual(quantities_sold(START, END, available=available), stock_page)
        for article_id, row in after.articles.items():
            on_the_page = stock_page[article_id].headline if article_id in stock_page else ZERO
            self.assertEqual(on_the_page, row.sold, row.stock_type.name)

    def test_a_list_made_before_an_article_was_left_out_still_adds_to_it(self):
        GapExclusion.objects.create(stock_type=self.rum)
        report = self.report()
        the_list = {self.shot.pk: 3, self.pint.pk: 1}
        show_list(report, the_list)
        rum = report.articles[self.rum.pk]
        self.assertEqual(rum.proposed, Decimal("0.12"))
        self.assertIsNone(rum.filled_percent)
        self.assertEqual(report.articles[self.blonde.pk].proposed, Decimal("0.5"))
        # A new amount on top of it never adds a shot: 70,00 € is two pints.
        result = fill_gaps(report, Decimal("70.00"), the_list)
        self.assertEqual((result.plan.counts, result.plan.reason), ({self.pint.pk: 2}, EXACT))
        self.assertEqual(rum.proposed, Decimal("0.12"))
        self.assertEqual(report.articles[self.blonde.pk].proposed, Decimal("1.5"))


class ChoiceWithASideLeftOutTests(TestCase):
    """An « OU » one side of which is left out. Only the planner looks away:
    the engine still books a shot « au choix » on the dearer rum while it has
    what was bought less its allowance, so leaving the dearer rum out does
    NOT send the shots onto the cheaper one - proposed, a shot would fill no
    gap as the stock page counts it, and none is. The cheaper rum stays a
    target no sale reaches (secondary), and the plan says no sale fits.

    SecondaryArticleTests' rums: 2 L of the dear one, 1 L of the cheaper,
    10 shots sold - all on the dear rum (room 1,4 L), the cheaper's room
    0,9 L."""

    @classmethod
    def setUpTestData(cls):
        cls.take = make_stock_take(taken_at=at(1))
        cls.premium, cls.standard, cls.shot = ou_rums(cls.take, "2", "1")
        sold(cls.shot, 10)

    def test_the_dearer_side_left_out_the_shot_fills_no_gap(self):
        GapExclusion.objects.create(stock_type=self.premium)
        report = gaps_since(self.take, end=END)
        premium, standard = report.articles[self.premium.pk], report.articles[self.standard.pk]
        self.assertTrue(premium.excluded)
        self.assertFalse(premium.target)
        self.assertEqual((premium.sold, premium.room), (Decimal("0.4"), Decimal("1.4")))
        self.assertTrue(standard.target)
        self.assertTrue(standard.secondary)
        self.assertEqual([row.stock_type for row in report.rows], [self.standard])
        self.assertEqual(report.unreached, [])
        # Nothing holds the shot back; as the engine books it, it pours the
        # dear rum only.
        self.assertEqual(report.blocked, [])
        self.assertEqual([offer.recipe_id for offer in report.offers], [self.shot.pk])
        added = report.consumption({self.shot.pk: 1})
        base = report.consumption({})
        self.assertEqual(added[self.premium.pk] - base[self.premium.pk], Decimal("0.04"))
        self.assertEqual(added.get(self.standard.pk, ZERO) - base.get(self.standard.pk, ZERO), ZERO)
        result = fill_gaps(report, Decimal("99.00"))
        self.assertEqual(result.plan.counts, {})
        self.assertEqual(result.plan.reason, GAPS_FULL)
        self.assertEqual(standard.proposed, ZERO)
        self.assertIsNone(share_summary(report))

    def test_the_cheaper_side_left_out_the_shots_fill_the_dearer_one(self):
        GapExclusion.objects.create(stock_type=self.standard)
        report = gaps_since(self.take, end=END)
        premium, standard = report.articles[self.premium.pk], report.articles[self.standard.pk]
        self.assertTrue(premium.target)
        self.assertFalse(premium.secondary)
        self.assertFalse(standard.target)
        self.assertFalse(standard.secondary)
        result = fill_gaps(report, Decimal("99.00"))
        self.assertEqual((result.plan.counts, result.plan.reason), ({self.shot.pk: 3}, EXACT))
        self.assertEqual(premium.proposed, Decimal("0.12"))
        share = Decimal("0.12") / Decimal("1.4") * 100
        self.assertEqual(share_summary(report), (share, share, share))


class QueryCountTests(TestCase):
    """`gaps_since` reads like the « Écarts » page: what it costs must not
    grow with the sales, the purchases, the counted lines or the recipes -
    their sub-recipes and « OU » included: every recipe's choice groups are
    read in one go (`Recipe.load_choice_groups`), where each one asked cost
    two queries (CLAUDE.md, « Combler les écarts », « Cost »)."""

    def setUp(self):
        self.take = make_stock_take(taken_at=at(1))
        self.made = []

    def independent_recipe(self, index):
        stock_type = article(f"Article exemple {index}")
        counted(self.take, stock_type, "30")
        bought(stock_type, "60", date(2026, 3, 10))
        made = recipe(f"Recette exemple {index}", "35.00", (stock_type, "0.5"))
        sold(made, 4)
        self.made.append((made, stock_type))

    def nested_recipe(self, index):
        """A punch pouring a fixed rum and a base, the base a syrup « sucre
        OU miel » - two levels of sub-recipes, a choice at the bottom."""
        sugar = article(f"Sucre exemple {index}", unit=UnitChoices.KILOGRAM)
        honey = article(f"Miel exemple {index}", unit=UnitChoices.KILOGRAM)
        rum = article(f"Rhum exemple {index}")
        for stock_type in (sugar, honey, rum):
            counted(self.take, stock_type, "5")
            bought(stock_type, "2", date(2026, 3, 10))
        syrup = choice(f"Sirop exemple {index}", None, (sugar, "1"), (honey, "1"))
        base = make_recipe(name=f"Base exemple {index}", selling_price_ttc=None)
        make_ingredient(base, sub_recipe=syrup, quantity="0.5", group=0)
        made = recipe(f"Punch exemple {index}", "9.00", (rum, "0.04"))
        make_ingredient(made, sub_recipe=base, quantity="0.1", group=1)
        sold(made, 4)

    def queries(self):
        with CaptureQueriesContext(connection) as captured:
            gaps_since(self.take, end=END)
        return len(captured)

    def test_more_sales_purchases_and_lines_cost_nothing_more(self):
        for index in range(3):
            self.independent_recipe(index)
        few = self.queries()
        for day in range(2, 28):
            for made, stock_type in self.made:
                sold(made, 1, day=date(2026, 3, day), source="caisse")
                bought(stock_type, "1", date(2026, 3, day))
        self.assertEqual(self.queries(), few)

    def test_three_times_the_recipes_cost_no_more_queries(self):
        for index in range(3):
            self.independent_recipe(index)
        few = self.queries()
        for index in range(3, 9):
            self.independent_recipe(index)
        self.assertEqual(self.queries(), few)

    def test_three_times_the_nested_recipes_cost_no_more_queries(self):
        for index in range(2):
            self.nested_recipe(index)
        few = self.queries()
        for index in range(2, 6):
            self.nested_recipe(index)
        self.assertEqual(self.queries(), few)

    def test_what_is_left_out_costs_one_query_however_much_there_is(self):
        for index in range(3):
            self.independent_recipe(index)
        none = self.queries()
        GapExclusion.objects.create(stock_type=self.made[0][1])
        one = self.queries()
        for _made, stock_type in self.made[1:]:
            GapExclusion.objects.create(stock_type=stock_type)
        for index in range(4):
            GapExclusion.objects.create(category=f"Catégorie exemple {index}")
        many = self.queries()
        self.assertEqual((one, many), (none, none))
