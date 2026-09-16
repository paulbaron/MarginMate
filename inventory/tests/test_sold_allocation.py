"""Which bottle did an ambiguous sale come out of?

"Alcool + soda" is vodka OR gin OR whisky, so 200 of them tell you 8 litres
of *something* went. The variance report refuses to guess and pools the three
(see test_variance.py). The stock page's "Vendu" column can't do that - it has
one row per item and has to put a number on each - so it guesses, but only
where the shelf leaves it a choice:

* the priciest alternative is assumed first, because nobody reaches for the
  well brand while the good stuff is open;
* only until it hits what was actually bought, less that item's own loss
  allowance (StockType.loss_percent - the part of every purchase that goes
  down the drain rather than into a glass);
* then the next-priciest takes over;
* and once every alternative has had that turn, the allowance is released and
  they're filled again up to 100% of what was bought.

Whatever is still unaccounted for after all that was poured out of stock
nobody ever bought, so it goes on the priciest option and the row turns red.

The numbers below are round on purpose: this column is what someone looks at
to decide an invoice is missing.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from inventory.models import UnitChoices
from inventory.variance import allocate_choices, order_options, quantities_sold
from recipes.sales import record_sales
from tests.factories import make_ingredient, make_movement, make_recipe, make_stock_type

NO_OVERRIDES = {}  # every item falls back to the default 10% allowance


class AllocateChoicesTests(SimpleTestCase):
    """The allocator on its own, with plain dictionaries - no database, no
    recipes, just "here are N servings, here is what's on the shelf"."""

    def test_nothing_to_allocate(self):
        self.assertEqual(allocate_choices([], {1: Decimal("10")}, NO_OVERRIDES, {}), {})

    def test_a_demand_of_zero_servings_is_ignored(self):
        options = [{1: Decimal("0.5")}]
        self.assertEqual(allocate_choices([(0, options)], {1: Decimal("10")}, NO_OVERRIDES, {}), {})

    def test_an_option_that_consumes_nothing_is_not_an_option(self):
        """It could absorb any number of servings without ever running out,
        which would let a half-built recipe swallow a whole pool's
        consumption and report every real bottle as untouched."""
        real = {1: Decimal("0.5")}
        allocated = allocate_choices([(4, [{}, real])], {1: Decimal("10")}, NO_OVERRIDES, {})
        self.assertEqual(allocated, {1: Decimal("2.0")})

    def test_the_priciest_option_is_filled_first(self):
        cheap, dear = {1: Decimal("0.5")}, {2: Decimal("0.5")}
        allocated = allocate_choices(
            [(4, [cheap, dear])],
            available={1: Decimal("100"), 2: Decimal("100")},
            loss_fractions=NO_OVERRIDES,
            unit_costs={1: Decimal("1"), 2: Decimal("9")},
        )
        self.assertEqual(allocated, {2: Decimal("2.0")})

    def test_an_item_with_nothing_bought_covers_nothing_it_could_avoid(self):
        """The dearest option is the obvious pick, but nothing was ever
        bought of it - so the sale came out of the one that was."""
        never_bought, on_the_shelf = {1: Decimal("0.5")}, {2: Decimal("0.5")}
        allocated = allocate_choices(
            [(4, [never_bought, on_the_shelf])],
            available={2: Decimal("100")},
            loss_fractions=NO_OVERRIDES,
            unit_costs={1: Decimal("99"), 2: Decimal("1")},
        )
        self.assertEqual(allocated, {2: Decimal("2.0")})

    def test_certain_consumption_is_charged_to_the_shelf_first(self):
        """A fixed ingredient already emptied that bottle, so an ambiguous
        sale can't be credited to it as well - that stock is gone twice
        over."""
        dear, cheap = {1: Decimal("1")}, {2: Decimal("1")}
        allocated = allocate_choices(
            [(4, [dear, cheap])],
            available={1: Decimal("10"), 2: Decimal("100")},
            loss_fractions=NO_OVERRIDES,
            unit_costs={1: Decimal("9"), 2: Decimal("1")},
            already_used={1: Decimal("8")},
        )
        # 10 bought of the dear one, 8 already certainly gone: only the last
        # litre of its 9-litre allowance is left to cover an ambiguous sale,
        # and the cheap one takes the other three servings.
        self.assertEqual(allocated, {1: Decimal("1"), 2: Decimal("3")})

    def test_what_no_shelf_can_explain_lands_on_the_priciest(self):
        dear, cheap = {1: Decimal("1")}, {2: Decimal("1")}
        allocated = allocate_choices(
            [(10, [dear, cheap])],
            available={1: Decimal("2"), 2: Decimal("3")},
            loss_fractions=NO_OVERRIDES,
            unit_costs={1: Decimal("9"), 2: Decimal("1")},
        )
        # 2 + 3 really existed; the other 5 servings came from nowhere.
        self.assertEqual(allocated, {1: Decimal("7"), 2: Decimal("3")})

    def test_a_negative_loss_allowance_cannot_stretch_the_shelf(self):
        """-50% would read as "150% of what was bought is available". The
        fraction is clamped where it's read from the model; passed straight
        in, the hard cap still holds."""
        allocated = allocate_choices(
            [(10, [{1: Decimal("1")}])],
            available={1: Decimal("4")},
            loss_fractions={1: Decimal("-0.5")},
            unit_costs={1: Decimal("1")},
        )
        # 4 bought, 6 unexplained - never 6 covered and 4 left over.
        self.assertEqual(allocated, {1: Decimal("10")})


class OrderOptionsTests(SimpleTestCase):
    def test_options_come_back_priciest_first(self):
        cheap, dear = {1: Decimal("1")}, {2: Decimal("1")}
        self.assertEqual(
            order_options([cheap, dear], {1: Decimal("2"), 2: Decimal("5")}), [dear, cheap]
        )

    def test_an_option_is_valued_as_a_whole_not_by_its_dearest_item(self):
        """A double of the well brand can beat a single of the good stuff."""
        single_premium = {1: Decimal("0.04")}  # 0.04 x 30 = 1.20
        double_well = {2: Decimal("0.30")}  # 0.30 x 5  = 1.50
        self.assertEqual(
            order_options([single_premium, double_well], {1: Decimal("30"), 2: Decimal("5")}),
            [double_well, single_premium],
        )

    def test_a_tie_is_broken_by_the_lowest_id(self):
        first, second = {1: Decimal("1")}, {2: Decimal("1")}
        costs = {1: Decimal("5"), 2: Decimal("5")}
        self.assertEqual(order_options([second, first], costs), [first, second])

    def test_options_that_consume_nothing_are_dropped(self):
        self.assertEqual(order_options([{}, {1: Decimal("0")}], {}), [])


class ClampedToWhatWasBoughtTests(TestCase):
    """The same rules through the real thing: recipes, sales, movements."""

    def sell(self, recipe, count):
        record_sales([(recipe.name, date(2026, 3, 5), count)])

    def bought(self, name, quantity, unit_cost_ht, **kwargs):
        stock_type = make_stock_type(name=name, unit=UnitChoices.LITRE, **kwargs)
        make_movement(stock_type=stock_type, quantity=quantity, unit_cost_ht=unit_cost_ht)
        return stock_type

    def either(self, name, first, second, quantity="0.05"):
        recipe = make_recipe(name=name)
        make_ingredient(recipe, stock_type=first, quantity=quantity, group=0)
        make_ingredient(recipe, stock_type=second, quantity=quantity, group=0)
        return recipe

    def test_the_worked_beer_example(self):
        """Two beers at 0.5L a pint, 6 litres of each bought, 22 pints sold.

        The dearer keg is assumed first, but only up to 6L less its 10%
        allowance - 5.4L. A pint is 0.5L and doesn't divide into 5.4, and
        stopping at 5.0L would leave 0.4L of a keg that no sale could have
        produced, so the pint that starts under the allowance is finished:
        11 pints, 5.5L, still inside the 6L bought. The second keg then does
        exactly the same, and the 22 pints come out 11 and 11.
        """
        blonde = self.bought("Bière blonde", "6", "0.50")
        brune = self.bought("Bière brune", "6", "0.40")
        self.sell(self.either("Pinte", blonde, brune, quantity="0.5"), 22)

        sold = quantities_sold()
        self.assertEqual(sold[blonde.pk].headline, Decimal("5.5"))
        self.assertEqual(sold[brune.pk].headline, Decimal("5.5"))
        self.assertFalse(sold[blonde.pk].is_over)
        self.assertFalse(sold[brune.pk].is_over)

    def test_it_moves_to_the_next_alternative_when_the_first_runs_out(self):
        """1L of vodka bought, 10L of gin, 40 measures of 5cl sold. The vodka
        covers 0.9L of it - what was bought less its allowance - and the gin
        covers the remaining 1.1L, rather than the vodka being credited with
        the whole 2L it never had."""
        vodka = self.bought("Vodka", "1", "30")
        gin = self.bought("Gin", "10", "10")
        self.sell(self.either("Mule", vodka, gin), 40)

        sold = quantities_sold()
        self.assertEqual(sold[vodka.pk].headline, Decimal("0.90"))
        self.assertEqual(sold[gin.pk].headline, Decimal("1.10"))

    def test_the_allowance_is_only_released_once_everything_has_had_a_turn(self):
        """1L of each and exactly 2L sold. Both stop at 0.9L first; only then
        does the last 0.2L get taken out of the two allowances. Filling the
        vodka to the brim before the gin had been touched would have said the
        bar poured its premium bottle dry while a full one stood next to
        it."""
        vodka = self.bought("Vodka", "1", "30")
        gin = self.bought("Gin", "1", "10")
        self.sell(self.either("Mule", vodka, gin), 40)

        sold = quantities_sold()
        self.assertEqual(sold[vodka.pk].headline, Decimal("1.00"))
        self.assertEqual(sold[gin.pk].headline, Decimal("1.00"))
        self.assertEqual(sold[vodka.pk].missing, Decimal("0"))

    def test_more_sold_than_ever_bought_is_flagged_not_hidden(self):
        """3L poured out of 2L bought. The extra litre has to land somewhere
        visible: an invoice is missing, or a recipe names the wrong bottle."""
        vodka = self.bought("Vodka", "1", "30")
        gin = self.bought("Gin", "1", "10")
        self.sell(self.either("Mule", vodka, gin), 60)

        sold = quantities_sold()
        self.assertEqual(sold[vodka.pk].headline, Decimal("2.00"))
        self.assertTrue(sold[vodka.pk].is_over)
        self.assertEqual(sold[vodka.pk].missing, Decimal("1.00"))
        self.assertEqual(sold[gin.pk].headline, Decimal("1.00"))
        self.assertFalse(sold[gin.pk].is_over)

    def test_a_fixed_ingredient_is_never_clamped(self):
        """There is no alternative to switch to. If the recipe says every
        Caipirinha takes 50g of lime then 100 of them took 5kg, whether or
        not 5kg was ever bought - and saying so is the entire point."""
        lime = make_stock_type(name="Citrons", unit=UnitChoices.KILOGRAM)
        make_movement(stock_type=lime, quantity="1", unit_cost_ht="3")
        recipe = make_recipe(name="Caipi")
        make_ingredient(recipe, stock_type=lime, quantity="0.05", group=0)
        self.sell(recipe, 100)

        sold = quantities_sold()[lime.pk]
        self.assertEqual(sold.exact, Decimal("5.00"))
        self.assertEqual(sold.missing, Decimal("4.00"))
        self.assertTrue(sold.is_over)

    def test_certain_consumption_takes_the_shelf_before_an_ambiguous_one(self):
        """The gin is poured neat in one recipe and offered as an alternative
        in another. The neat pour is a fact and gets the bottle first - and
        having spent the gin's whole allowance, it pushes the ambiguous sale
        onto the vodka even though the gin is the pricier bottle."""
        gin = self.bought("Gin", "1", "30")
        vodka = self.bought("Vodka", "10", "10")
        neat = make_recipe(name="Gin tonic")
        make_ingredient(neat, stock_type=gin, quantity="0.05", group=0)
        self.sell(neat, 18)  # 0.90L - the whole allowance
        self.sell(self.either("Mule", gin, vodka), 20)

        sold = quantities_sold()
        self.assertEqual(sold[gin.pk].exact, Decimal("0.90"))
        self.assertEqual(sold[gin.pk].shared, Decimal("0"))
        self.assertEqual(sold[vodka.pk].headline, Decimal("1.00"))

    def test_the_loss_allowance_is_per_item(self):
        """A draught beer loses far more to foam than a bottle of syrup loses
        to anything, so the allowance is a property of the item."""
        vodka = self.bought("Vodka", "1", "30", loss_percent=Decimal("0"))
        gin = self.bought("Gin", "10", "10")
        self.sell(self.either("Mule", vodka, gin), 40)

        sold = quantities_sold()
        # No allowance at all: the vodka is assumed poured to the last drop.
        self.assertEqual(sold[vodka.pk].headline, Decimal("1.00"))
        self.assertEqual(sold[gin.pk].headline, Decimal("1.00"))

    def test_a_bigger_allowance_hands_more_to_the_alternative(self):
        vodka = self.bought("Vodka", "1", "30", loss_percent=Decimal("50"))
        gin = self.bought("Gin", "10", "10")
        self.sell(self.either("Mule", vodka, gin), 40)

        sold = quantities_sold()
        self.assertEqual(sold[vodka.pk].headline, Decimal("0.50"))
        self.assertEqual(sold[gin.pk].headline, Decimal("1.50"))

    def test_the_answer_does_not_move_between_page_loads(self):
        vodka = self.bought("Vodka", "1", "30")
        gin = self.bought("Gin", "10", "10")
        self.sell(self.either("Mule", vodka, gin), 40)

        first = {pk: sold.headline for pk, sold in quantities_sold().items()}
        second = {pk: sold.headline for pk, sold in quantities_sold().items()}
        self.assertEqual(first, second)


class LossAllowanceIsEditableTests(TestCase):
    """The allowance is a guess about one bottle's habits, so it has to be
    changeable per item - a default nobody can reach is just a magic number
    with extra steps."""

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)

    def test_it_defaults_to_ten_percent(self):
        self.assertEqual(self.vodka.loss_percent, Decimal("10"))
        self.assertEqual(self.vodka.loss_fraction, Decimal("0.1"))

    def test_the_edit_form_offers_it(self):
        response = self.client.get(
            reverse("inventory:stock_type_update", kwargs={"pk": self.vodka.pk})
        )
        self.assertContains(response, "loss_percent")

    def test_saving_a_new_value_sticks(self):
        response = self.client.post(
            reverse("inventory:stock_type_update", kwargs={"pk": self.vodka.pk}),
            {"name": "Vodka", "unit": UnitChoices.LITRE, "category": "", "loss_percent": "25"},
        )
        self.assertEqual(response.status_code, 302)
        self.vodka.refresh_from_db()
        self.assertEqual(self.vodka.loss_percent, Decimal("25"))

    def test_an_impossible_allowance_is_refused(self):
        """Over 100% would mean more was lost than was ever bought; below 0
        would let an item cover more sales than it was delivered."""
        for value in ("-5", "150"):
            with self.subTest(value=value):
                response = self.client.post(
                    reverse("inventory:stock_type_update", kwargs={"pk": self.vodka.pk}),
                    {"name": "Vodka", "unit": UnitChoices.LITRE, "category": "", "loss_percent": value},
                )
                self.assertEqual(response.status_code, 200)  # redisplayed, not saved
                self.vodka.refresh_from_db()
                self.assertEqual(self.vodka.loss_percent, Decimal("10"))


class OverStockOnThePageTests(TestCase):
    def setUp(self):
        self.lime = make_stock_type(name="Citrons", unit=UnitChoices.KILOGRAM)
        make_movement(stock_type=self.lime, quantity="1", unit_cost_ht="3")
        recipe = make_recipe(name="Caipi")
        make_ingredient(recipe, stock_type=self.lime, quantity="0.05", group=0)
        record_sales([("Caipi", date(2026, 3, 5), 100)])
        self.response = self.client.get(reverse("inventory:stock_list"))

    def row(self):
        rows = [row for category in self.response.context["categories"] for row in category["rows"]]
        return next(row for row in rows if row["stock_type"].pk == self.lime.pk)

    def test_the_row_is_marked_as_over_its_purchases(self):
        self.assertTrue(self.row()["sold"].is_over)
        self.assertContains(self.response, "sold-over")

    def test_the_page_counts_them(self):
        self.assertEqual(self.response.context["over_stock_count"], 1)

    def test_an_item_within_its_purchases_is_not_marked(self):
        make_movement(stock_type=self.lime, quantity="10", unit_cost_ht="3")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(response.context["over_stock_count"], 0)
        self.assertNotContains(response, "sold-over")
