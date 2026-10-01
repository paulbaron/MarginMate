"""« Combler les écarts »: the planner on its own (`inventory/gap_planner.py`).

No database, no request: the offers are built by hand and the engine is a
`Consumption` handed in. A plan is either made alone or on top of a list's
earlier sales (`already`, the « amount after amount » list): the classes
near the end pin the second. The last ones pin `ignored`, the articles the
owner left out of the page (`GapExclusion`): no gap to fill, no limit on a
sale. Three engines:

* an ADDITIVE engine - what the window's sales consume is the base plus,
  per recipe, the count times its fixed pour. Right for recipes no « OU »
  reaches, which is what `Offer.independent` declares;
* a small hand-written CHOICE engine, `ChoiceEngine`: one kind of « OU »,
  booked priciest option first up to a cap, after every fixed draw - so a
  fixed draw on the capped article pushes a choice serving onto the next
  option, as `variance.allocate_choices` does. That is the trap the
  planner's module docstring tells: ranked on a model of what a recipe
  pours, the plan filled an article the ranking never saw;
* the stock page's OWN engine, `variance.attribute_sales`, over a
  hand-built `SalesRead` whose usage terms are given - no query - for what
  the hand-written one leaves out: a serving rounded UP against the loss
  allowance, which sets a choice aside until a fixed pour moves it on.

Invented data: every article, recipe, price, room, cost and sold count
below is made up for these tests (the names in comments, « Blonde
exemple », « Pinte exemple »..., exist nowhere else), and every price is
off the 0,50 € grid - 3,30 €, 5,70 €, 8,90 € - so that none can be a real
menu's. Money is in cents, as the planner takes it; quantities are
Decimals.
"""

from __future__ import annotations

import itertools
import random
import time
from decimal import Decimal
from math import gcd
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from inventory.gap_planner import (
    BELOW_CHEAPEST,
    EXACT,
    GAPS_FULL,
    NO_COMBINATION,
    NOTHING_TO_FILL,
    Offer,
    Plan,
    _Makeable,
    _Run,
    blocked,
    first_sales,
    limit,
    plan_sales,
    usual_servings,
)
from inventory.models import loss_fraction
from inventory.variance import SalesRead, attribute_sales, loss_allowance

ZERO = Decimal("0")
D = Decimal

REASONS = {EXACT, BELOW_CHEAPEST, GAPS_FULL, NO_COMBINATION, NOTHING_TO_FILL}

# Articles (StockType pks), invented.
BLONDE = 1  # « Blonde exemple », a keg, in litres
AMBREE = 2  # « Ambrée exemple »
RHUM = 3  # « Rhum exemple », in cl
GIN = 4  # « Gin exemple »
CITRON = 5  # « Citron exemple », a garnish
LIMONADE = 6  # « Limonade exemple », in cl
SIROP = 7  # « Sirop exemple », in cl
ACIDE = 8  # « Acide citrique exemple », in g

AMPLE = D("1000000")

# One keg's two glasses, priced off the 0,50 € grid: a pint at 5,70 € and a
# half at 3,30 € - every total they make is a multiple of 0,30 €.
PINT_PRICE, HALF_PRICE = 570, 330


def fixed_offer(recipe_id, price_cents, pours, *, sold=1, name=None, independent=True):
    """An offer with no choice: one term per article, a single option each."""
    terms = tuple(({article: D(str(amount))},) for article, amount in pours.items())
    return Offer(
        recipe_id=recipe_id,
        name=name or f"Recette exemple {recipe_id}",
        price_cents=price_cents,
        sold=sold,
        terms=terms,
        independent=independent,
    )


class AdditiveEngine:
    """base + Σ count × fixed pour - exact for recipes no « OU » reaches.
    Counts its calls in `calls`."""

    def __init__(self, offers, base=None):
        self.pours = {offer.recipe_id: offer.fixed_pour() for offer in offers}
        self.base = {article: D(str(amount)) for article, amount in (base or {}).items()}
        self.calls = 0

    def __call__(self, extra):
        self.calls += 1
        total = dict(self.base)
        for recipe_id, count in extra.items():
            for article, amount in self.pours[recipe_id].items():
                total[article] = total.get(article, ZERO) + amount * count
        return total


def additive_engine(offers, base=None):
    return AdditiveEngine(offers, base)


class ChoiceEngine:
    """A stand-in for the stock page's attribution, with one « OU ».

    `sold` is what the till sold of each recipe since the take; `fixed` what
    one sale pours for sure; `choices` {recipe: (amount, [articles, priciest
    first])}; `caps` what may be booked on an article before a choice moves
    on to the next option (« what was bought less its allowance »). Fixed
    draws are counted first, then each choice is booked priciest first up
    to the cap, whole servings rounded DOWN, the rest on the next option;
    whatever no option can take goes on the priciest, past its cap, as round
    three of `allocate_choices` does.
    """

    def __init__(self, sold, fixed, choices, caps):
        self.sold = sold
        self.fixed = fixed
        self.choices = choices
        self.caps = {article: D(str(cap)) for article, cap in caps.items()}
        self.calls = 0

    def __call__(self, extra):
        self.calls += 1
        counts = {recipe: self.sold.get(recipe, 0) + extra.get(recipe, 0) for recipe in set(self.sold) | set(extra)}
        used: dict[int, Decimal] = {}
        for recipe in sorted(self.fixed):
            for article, amount in self.fixed[recipe].items():
                used[article] = used.get(article, ZERO) + D(str(amount)) * counts.get(recipe, 0)
        for recipe in sorted(self.choices):
            amount, options = self.choices[recipe]
            amount = D(str(amount))
            left = counts.get(recipe, 0)
            for article in options:
                free = self.caps.get(article, ZERO) - used.get(article, ZERO)
                take = min(left, max(0, int(free // amount)))
                used[article] = used.get(article, ZERO) + amount * take
                left -= take
            if left:
                used[options[0]] = used.get(options[0], ZERO) + amount * left
        return used


def reachable_totals(prices, top):
    """[reachable?] for every cent from 0 to `top`: the coin change of the
    prices, any number of each - the brute force the planner is checked
    against."""
    reach = [False] * (top + 1)
    reach[0] = True
    for price in sorted(set(prices)):
        for total in range(price, top + 1):
            if reach[total - price]:
                reach[total] = True
    return reach


def best_below(reach, amount):
    for total in range(amount, -1, -1):
        if reach[total]:
            return total
    return 0


def price_step(prices):
    """The greatest common divisor of the prices: every total they make is
    a multiple of it, so a sweep in that step meets every one."""
    step = 0
    for price in prices:
        step = gcd(step, price)
    return step


def assert_plan_is_consistent(case, plan, amount, offers, rooms):
    """What every plan owes, whatever the instance."""
    price = {offer.recipe_id: offer.price_cents for offer in offers}
    case.assertIn(plan.reason, REASONS)
    case.assertEqual(plan.total_cents + plan.remainder_cents, amount)
    case.assertGreaterEqual(plan.total_cents, 0)
    case.assertLessEqual(plan.total_cents, amount)
    case.assertEqual(plan.total_cents, sum(price[recipe] * count for recipe, count in plan.counts.items()))
    case.assertTrue(all(count > 0 for count in plan.counts.values()), plan.counts)
    case.assertEqual(plan.reason == EXACT, plan.remainder_cents == 0)
    for article in set(rooms) | set(plan.used):
        case.assertLessEqual(
            plan.used.get(article, ZERO),
            limit(rooms, article),
            f"article {article}: added {plan.used.get(article)} past its room {rooms.get(article)}",
        )


class PricesAreInventedTests(SimpleTestCase):
    """The privacy rule this file keeps: no price below sits on the 0,50 €
    grid a real menu is priced on."""

    def test_the_shared_prices_are_off_the_fifty_cent_grid(self):
        shared = (
            PINT_PRICE,
            HALF_PRICE,
            *ExactTotalTests.FOUR_PRICES,
            ReviveTests.SHOT_PRICE,
            ReviveTests.PUNCH_PRICE,
            *SevenEurosAgainTests.PRICES,
        )
        for price in shared:
            with self.subTest(price=price):
                self.assertNotEqual(price % 50, 0)


class AmountEdgeTests(SimpleTestCase):
    """Zero, negative, below the cheapest price, nothing to fill."""

    def setUp(self):
        self.offers = [
            fixed_offer(10, HALF_PRICE, {BLONDE: "0.25"}, sold=5),  # « Demi exemple »
            fixed_offer(11, PINT_PRICE, {BLONDE: "0.5"}, sold=9),  # « Pinte exemple »
        ]
        self.rooms = {BLONDE: D("30")}
        self.engine = additive_engine(self.offers)

    def test_an_amount_of_zero_is_an_empty_exact_plan(self):
        plan = plan_sales(0, self.offers, self.rooms, self.engine)
        self.assertEqual(plan, Plan({}, 0, 0, EXACT))

    def test_zero_with_nothing_to_fill_is_still_exact(self):
        plan = plan_sales(0, [], {}, additive_engine([]))
        self.assertEqual(plan, Plan({}, 0, 0, EXACT))

    def test_a_negative_amount_is_refused(self):
        with self.assertRaises(ValueError):
            plan_sales(-1, self.offers, self.rooms, self.engine)

    def test_below_the_cheapest_price_proposes_nothing_and_says_why(self):
        plan = plan_sales(HALF_PRICE - 1, self.offers, self.rooms, self.engine)
        self.assertEqual(plan.counts, {})
        self.assertEqual(plan.total_cents, 0)
        self.assertEqual(plan.remainder_cents, HALF_PRICE - 1)
        self.assertEqual(plan.reason, BELOW_CHEAPEST)
        self.assertEqual(plan.used, {})

    def test_exactly_the_cheapest_price_is_one_sale(self):
        plan = plan_sales(HALF_PRICE, self.offers, self.rooms, self.engine)
        self.assertEqual(plan.counts, {10: 1})
        self.assertEqual(plan.reason, EXACT)
        self.assertEqual(plan.used, {BLONDE: D("0.25")})

    def test_no_offers_at_all_is_nothing_to_fill(self):
        plan = plan_sales(5000, [], {BLONDE: D("30")}, additive_engine([]))
        self.assertEqual(plan, Plan({}, 0, 5000, NOTHING_TO_FILL))

    def test_offers_reaching_no_positive_room_are_nothing_to_fill(self):
        rooms = {BLONDE: D("0"), AMBREE: D("-4")}
        offers = [
            fixed_offer(10, HALF_PRICE, {BLONDE: "0.25"}),
            fixed_offer(12, HALF_PRICE, {AMBREE: "0.25"}),
            fixed_offer(13, 370, {RHUM: "4"}),  # an article the rooms do not even name
        ]
        plan = plan_sales(5000, offers, rooms, additive_engine(offers))
        self.assertEqual(plan, Plan({}, 0, 5000, NOTHING_TO_FILL))

    def test_an_offer_priced_zero_is_never_proposed(self):
        offers = [fixed_offer(10, 0, {BLONDE: "0.25"})]
        plan = plan_sales(5000, offers, self.rooms, additive_engine(offers))
        self.assertEqual(plan, Plan({}, 0, 5000, NOTHING_TO_FILL))

    def test_an_offer_priced_zero_beside_a_priced_one_is_left_out(self):
        offers = [fixed_offer(10, 0, {BLONDE: "0.25"}, sold=100), fixed_offer(11, PINT_PRICE, {BLONDE: "0.5"})]
        plan = plan_sales(2 * PINT_PRICE, offers, self.rooms, additive_engine(offers))
        self.assertEqual(plan.counts, {11: 2})
        self.assertEqual(plan.reason, EXACT)

    def test_nothing_sold_since_the_take_still_plans(self):
        # sold = 0 is « behind » by (planned + ½) / 1: no division by zero.
        offers = [
            fixed_offer(10, HALF_PRICE, {BLONDE: "0.25"}, sold=0),
            fixed_offer(11, HALF_PRICE, {BLONDE: "0.25"}, sold=0),
        ]
        plan = plan_sales(4 * HALF_PRICE, offers, self.rooms, additive_engine(offers))
        self.assertEqual(sum(plan.counts.values()), 4)
        self.assertEqual(plan.reason, EXACT)

    def test_a_net_negative_sold_count_does_not_break_the_plan(self):
        # Offer.sold is « what the till sold of it since the count », and the
        # till's quantities are signed (refunds, recipes/0014): a recipe taken
        # back more than sold nets -1. Divided by (sold + 1), that raised
        # ZeroDivisionError (and below -1 turned the mix round); it now weighs
        # like a recipe never sold. gaps.gaps_since drops sold <= 0 anyway.
        offers = [
            fixed_offer(10, HALF_PRICE, {BLONDE: "0.25"}, sold=-1),
            fixed_offer(11, HALF_PRICE, {BLONDE: "0.25"}, sold=3),
        ]
        plan = plan_sales(4 * HALF_PRICE, offers, self.rooms, additive_engine(offers))
        self.assertEqual(plan.total_cents, 4 * HALF_PRICE)


class ExactTotalTests(SimpleTestCase):
    """The total is the amount to the cent whenever a combination of the
    prices makes it; otherwise the closest below, and the reason."""

    # 3,10 / 3,70 / 5,30 / 8,90 €: every total they make is a multiple of
    # 0,10 €, and far from every multiple of 0,10 € is one.
    FOUR_PRICES = (310, 370, 530, 890)

    def ample(self, prices):
        offers = [
            fixed_offer(100 + index, price, {index + 1: "1"}, sold=40 - 5 * index) for index, price in enumerate(prices)
        ]
        rooms = {index + 1: AMPLE for index in range(len(prices))}
        return offers, rooms, additive_engine(offers)

    def test_every_step_of_the_prices_up_to_sixty_euros(self):
        prices = list(self.FOUR_PRICES)
        offers, rooms, engine = self.ample(prices)
        step = price_step(prices)
        self.assertEqual(step, 10)
        reach = reachable_totals(prices, 6000)
        for amount in range(0, 6001, step):
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, engine)
                assert_plan_is_consistent(self, plan, amount, offers, rooms)
                self.assertEqual(plan.total_cents, best_below(reach, amount))
                if reach[amount]:
                    self.assertEqual(plan.reason, EXACT)
                elif amount < min(prices):
                    self.assertEqual(plan.reason, BELOW_CHEAPEST)
                else:
                    self.assertEqual(plan.reason, NO_COMBINATION)

    def misses(self, offers, rooms, engine, amounts):
        """(amount, total planned, best total the prices make) wherever the
        plan falls short of what a combination reaches - every plan checked
        for consistency on the way."""
        prices = [offer.price_cents for offer in offers]
        reach = reachable_totals(prices, max(amounts))
        found = []
        for amount in amounts:
            plan = plan_sales(amount, offers, rooms, engine)
            assert_plan_is_consistent(self, plan, amount, offers, rooms)
            best = best_below(reach, amount)
            if plan.total_cents != best or (plan.reason == EXACT) != (best == amount):
                found.append((amount, plan.total_cents, best, plan.reason))
        return found, len(amounts)

    def assert_no_miss(self, result):
        found, asked = result
        if found:
            self.fail(
                f"{len(found)} of {asked} amounts short of the best total their prices make; "
                f"first (amount, planned, best, reason): {found[:6]}"
            )

    def sparse(self):
        return self.ample([310, 3370])  # 3,10 € and 33,70 €

    def test_a_sparse_price_set_takes_no_dear_sale_that_strands_the_rest(self):
        # 77,50 € is 25 x 3,10 € and nothing else: a 33,70 € sale ranked
        # early (the two rooms are alike, the ranking alternates) leaves
        # 43,80 €, which no mix of the two makes. Every sale leaves a rest
        # the prices can make.
        offers, rooms, engine = self.sparse()
        plan = plan_sales(7750, offers, rooms, engine)
        self.assertEqual(plan.counts, {100: 25})
        self.assertEqual(plan.reason, EXACT)
        self.assertFalse(reachable_totals([310, 3370], 4380)[4380])

    def test_a_sparse_price_set_reaches_every_total_its_prices_make(self):
        offers, rooms, engine = self.sparse()
        step = price_step([310, 3370])
        self.assertEqual(step, 10)
        self.assert_no_miss(self.misses(offers, rooms, engine, list(range(0, 15001, step))))

    def pint_and_half(self):
        # One keg, a pint at 5,70 € and a half at 3,30 €.
        offers = [
            fixed_offer(40, PINT_PRICE, {BLONDE: "0.5"}, sold=30, name="Pinte exemple"),
            fixed_offer(41, HALF_PRICE, {BLONDE: "0.25"}, sold=25, name="Demi exemple"),
        ]
        return offers, {BLONDE: AMPLE}, additive_engine(offers)

    def test_a_pint_taken_first_never_strands_four_halves(self):
        # The likeliest menu of all: 13,20 € is four halves, and a pint ranked
        # first (it is the better seller) leaves 7,50 €, which neither glass
        # makes: a pint and two halves, 12,30 €, at best.
        offers, rooms, engine = self.pint_and_half()
        plan = plan_sales(4 * HALF_PRICE, offers, rooms, engine)
        self.assertEqual(plan.counts, {41: 4})
        self.assertEqual(plan.reason, EXACT)
        self.assertFalse(reachable_totals([PINT_PRICE, HALF_PRICE], 750)[4 * HALF_PRICE - PINT_PRICE])

    def test_a_pint_and_a_half_reach_every_total_their_prices_make(self):
        offers, rooms, engine = self.pint_and_half()
        step = price_step([PINT_PRICE, HALF_PRICE])
        self.assertEqual(step, 30)
        # The step's every total, and a few amounts off it (closest below).
        amounts = list(range(0, 10001, step)) + [1025, 2399, 4777, 9999]
        self.assert_no_miss(self.misses(offers, rooms, engine, sorted(amounts)))

    def test_an_amount_off_the_price_grid_is_the_closest_below(self):
        offers, rooms, engine = self.ample([HALF_PRICE, PINT_PRICE])
        plan = plan_sales(1025, offers, rooms, engine)  # 10,25 €
        # 3 x 3,30 = 9,90 € beats 3,30 + 5,70 = 9,00 €; 5,70 x 2 is 11,40 €.
        self.assertEqual(plan.counts, {100: 3})
        self.assertEqual(plan.total_cents, 990)
        self.assertEqual(plan.remainder_cents, 35)
        self.assertEqual(plan.reason, NO_COMBINATION)

    def test_a_remainder_is_never_an_exact_reason(self):
        prices = list(self.FOUR_PRICES)
        offers, rooms, engine = self.ample(prices)
        for amount in (311, 1001, 2399, 4777):  # none a multiple of 0,10 €, none under 3,10 €
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, engine)
                self.assertGreater(plan.remainder_cents, 0)
                self.assertEqual(plan.reason, NO_COMBINATION)
                self.assertEqual(plan.total_cents, best_below(reachable_totals(prices, amount), amount))

    def test_the_rooms_run_out_before_the_money(self):
        # « Pinte exemple » pours 0,5 L of a keg with 10 L to fill: 20 sales.
        offers = [fixed_offer(11, PINT_PRICE, {BLONDE: "0.5"}, sold=12)]
        rooms = {BLONDE: D("10")}
        plan = plan_sales(20000, offers, rooms, additive_engine(offers))
        self.assertEqual(plan.counts, {11: 20})
        self.assertEqual(plan.total_cents, 20 * PINT_PRICE)
        self.assertEqual(plan.remainder_cents, 20000 - 20 * PINT_PRICE)
        self.assertEqual(plan.reason, GAPS_FULL)
        self.assertEqual(plan.used, {BLONDE: D("10.0")})

    def test_gaps_full_with_two_articles_states_the_remainder(self):
        offers = [
            fixed_offer(20, 530, {BLONDE: "25"}, sold=4),
            fixed_offer(21, 530, {AMBREE: "25"}, sold=4),
        ]
        rooms = {BLONDE: D("500"), AMBREE: D("50")}
        plan = plan_sales(23 * 530, offers, rooms, additive_engine(offers))  # 23 sales asked, 22 fit
        self.assertEqual(plan.counts, {20: 20, 21: 2})
        self.assertEqual(plan.total_cents, 22 * 530)
        self.assertEqual(plan.remainder_cents, 530)
        self.assertEqual(plan.reason, GAPS_FULL)


class NeverOverfillTests(SimpleTestCase):
    """The one promise: nothing added past max(0, room), as the engine
    counts it."""

    def test_an_article_whose_room_is_not_positive_receives_nothing(self):
        offers = [
            fixed_offer(30, HALF_PRICE, {BLONDE: "0.25", CITRON: "1"}, sold=5),  # the lemon's room is 0
            fixed_offer(31, PINT_PRICE, {AMBREE: "0.5"}, sold=5),
            fixed_offer(32, 370, {RHUM: "4"}, sold=5),  # the rum was oversold: room -8
        ]
        rooms = {BLONDE: D("40"), CITRON: D("0"), AMBREE: D("40"), RHUM: D("-8")}
        plan = plan_sales(10 * PINT_PRICE, offers, rooms, additive_engine(offers))
        self.assertEqual(plan.counts, {31: 10})
        self.assertEqual(plan.used, {AMBREE: D("5.0")})
        self.assertEqual(plan.reason, EXACT)

    def test_only_blocked_offers_propose_nothing(self):
        offers = [fixed_offer(30, HALF_PRICE, {BLONDE: "0.25", CITRON: "1"}, sold=5)]
        rooms = {BLONDE: D("40"), CITRON: D("0")}
        plan = plan_sales(3000, offers, rooms, additive_engine(offers))
        self.assertEqual(plan.counts, {})
        self.assertEqual(plan.total_cents, 0)
        self.assertEqual(plan.remainder_cents, 3000)
        self.assertEqual(plan.used, {})
        self.assertIn(plan.reason, {GAPS_FULL, NOTHING_TO_FILL})

    def random_additive_case(self, rng):
        articles = list(range(1, rng.randint(2, 6) + 1))
        rooms = {article: D(rng.choice([-12, -1, 0, 0, 3, 5, 8, 12, 20, 40, 75])) for article in articles}
        offers = []
        for index in range(rng.randint(1, 5)):
            poured = rng.sample(articles, rng.randint(1, min(3, len(articles))))
            offers.append(
                fixed_offer(
                    200 + index,
                    rng.choice([310, 370, 430, 570, 890, 1230]),
                    {article: rng.choice(["1", "2", "2.5", "3", "4", "0.5"]) for article in poured},
                    sold=rng.randint(0, 40),
                    name=f"Recette exemple {rng.randint(1, 3)}",  # names may repeat: the id breaks the tie
                    independent=rng.random() < 0.5,  # truthful either way for an additive engine
                )
            )
        base = {article: rng.randint(0, 30) for article in articles if rng.random() < 0.5}
        amount = rng.choice([0, rng.randint(1, 400), rng.randint(0, 600) * 10, rng.randint(0, 15000)])
        return amount, offers, rooms, additive_engine(offers, base)

    def test_sixty_random_instances_never_go_past_a_room(self):
        rng = random.Random(20261001)
        for case in range(60):
            amount, offers, rooms, engine = self.random_additive_case(rng)
            with self.subTest(case=case, amount=amount):
                plan = plan_sales(amount, offers, rooms, engine)
                assert_plan_is_consistent(self, plan, amount, offers, rooms)
                for article, room in rooms.items():
                    if room <= 0:
                        self.assertEqual(plan.used.get(article, ZERO), ZERO, f"article {article}, room {room}")
                # An additive engine never takes anything back.
                self.assertTrue(all(amount_added > 0 for amount_added in plan.used.values()), plan.used)

    def random_choice_case(self, rng):
        # Articles 1-2 are the « OU » (1 the dearer), 3-4 poured by fixed recipes.
        caps = {1: rng.choice([20, 40, 60]), 2: rng.choice([40, 200]), 3: 500, 4: 500}
        sold = {300: rng.randint(0, 15), 301: rng.randint(0, 8), 302: rng.randint(1, 10), 303: rng.randint(1, 10)}
        engine = ChoiceEngine(
            sold=sold,
            fixed={301: {1: 4, 4: 2}, 302: {3: 4}, 303: {2: 4}},
            choices={300: (4, [1, 2])},
            caps=caps,
        )
        offers = [
            Offer(300, "Shot au choix exemple", rng.choice([310, 370]), sold[300], (({1: D(4)}, {2: D(4)}),)),
            Offer(301, "Cocktail exemple", rng.choice([890, 930]), sold[301], (({1: D(4)},), ({4: D(2)},))),
            Offer(302, "Ti punch exemple", PINT_PRICE, sold[302], (({3: D(4)},),), independent=True),
            Offer(303, "Vodka tonic exemple", 630, sold[303], (({2: D(4)},),)),
        ]
        rooms = {article: D(rng.choice([-4, 0, 6, 10, 24, 40, 80])) for article in (1, 2, 3, 4)}
        amount = rng.choice([rng.randint(0, 900) * 10, rng.randint(0, 9000)])
        return amount, offers, rooms, engine

    def test_twenty_random_instances_with_an_ou_never_go_past_a_room(self):
        rng = random.Random(4242)
        for case in range(20):
            amount, offers, rooms, engine = self.random_choice_case(rng)
            with self.subTest(case=case, amount=amount, rooms=rooms):
                plan = plan_sales(amount, offers, rooms, engine)
                assert_plan_is_consistent(self, plan, amount, offers, rooms)
                # `used` is the engine's own answer for the whole plan.
                base, after = engine({}), engine(plan.counts)
                for article in set(base) | set(after):
                    self.assertEqual(plan.used.get(article, ZERO), after.get(article, ZERO) - base.get(article, ZERO))

    def two_recipes_one_keg(self):
        # Two recipes pouring the same two articles; the first article holds
        # two sales in all, whichever recipe takes them.
        offers = [
            fixed_offer(10, HALF_PRICE, {BLONDE: "2", AMBREE: "2"}, sold=1, name="Recette exemple A"),
            fixed_offer(11, HALF_PRICE, {BLONDE: "2", AMBREE: "2"}, sold=5, name="Recette exemple B"),
        ]
        rooms = {BLONDE: D("4"), AMBREE: D("8")}
        return offers, rooms

    def test_two_recipes_sharing_a_room_stop_at_it(self):
        offers, rooms = self.two_recipes_one_keg()
        amount = 3 * HALF_PRICE  # three sales asked, two fit
        plan = plan_sales(amount, offers, rooms, additive_engine(offers))
        assert_plan_is_consistent(self, plan, amount, offers, rooms)
        self.assertEqual(sum(plan.counts.values()), 2)
        self.assertEqual(plan.used, {BLONDE: D("4"), AMBREE: D("4")})
        self.assertEqual(plan.remainder_cents, HALF_PRICE)
        self.assertEqual(plan.reason, GAPS_FULL)


class ProportionalTests(SimpleTestCase):
    """Webster's divisor method on the articles' fill levels, (added + ½
    serving) / room; ties between recipes of one article follow the till's
    own mix."""

    def two_kegs(self, small_room):
        # Priced alike (5,30 €), so that the exact total never steers the split.
        offers = [
            fixed_offer(20, 530, {BLONDE: "25"}, sold=4, name="Pinte blonde exemple"),
            fixed_offer(21, 530, {AMBREE: "25"}, sold=4, name="Pinte ambrée exemple"),
        ]
        rooms = {BLONDE: D("500"), AMBREE: D(small_room)}
        return offers, rooms, additive_engine(offers)

    def test_rooms_of_500_and_50_get_sales_ten_to_one(self):
        offers, rooms, engine = self.two_kegs("50")
        # Levels (k + ½) / 20 against (k + ½) / 2: the small gap's first sale
        # comes 6th, its second 17th.
        expected = {11: {20: 10, 21: 1}, 17: {20: 15, 21: 2}, 22: {20: 20, 21: 2}}
        for sales, counts in expected.items():
            with self.subTest(sales=sales):
                plan = plan_sales(sales * 530, offers, rooms, engine)
                self.assertEqual(plan.counts, counts)
                self.assertEqual(plan.reason, EXACT)
                self.assertEqual(plan.used, {BLONDE: D(25 * counts[20]), AMBREE: D(25 * counts[21])})

    def test_both_gaps_shrink_by_about_the_same_share(self):
        offers, rooms, engine = self.two_kegs("50")
        plan = plan_sales(22 * 530, offers, rooms, engine)
        self.assertEqual(plan.used[BLONDE] / rooms[BLONDE], plan.used[AMBREE] / rooms[AMBREE])

    def test_a_gap_under_half_a_servings_share_waits_for_a_bigger_amount(self):
        # 30 to fill beside 500, one serving 25: 8 sales give it a share of
        # 8 x 30 / 530 = 0,45 of a sale - none; 9 give 0,51 - one.
        offers, rooms, engine = self.two_kegs("30")
        small = plan_sales(8 * 530, offers, rooms, engine)
        self.assertEqual(small.counts, {20: 8})
        self.assertNotIn(AMBREE, small.used)
        larger = plan_sales(9 * 530, offers, rooms, engine)
        self.assertEqual(larger.counts, {20: 8, 21: 1})

    def mix(self, pints_sold, halves_sold):
        # « Pinte exemple » 0,5 L and « Demi exemple » 0,25 L of one keg.
        offers = [
            fixed_offer(40, PINT_PRICE, {BLONDE: "0.5"}, sold=pints_sold, name="Pinte exemple"),
            fixed_offer(41, HALF_PRICE, {BLONDE: "0.25"}, sold=halves_sold, name="Demi exemple"),
        ]
        rooms = {BLONDE: D("1000")}
        return offers, rooms, additive_engine(offers)

    # The split is what these two pin, and the total stays exact: 174,60 €
    # splits exactly three ways (pints + halves), 4 + 46, 15 + 27 and 26 +
    # 8 - 11 pints trade for 19 halves - and none of them is 3 to 1 or 1 to
    # 3. The total wins, then the mix: each plan is the exact split closest
    # to what the till sold.
    MIXED_AMOUNT = 26 * PINT_PRICE + 8 * HALF_PRICE

    def exact_splits(self):
        return [
            (pints, (self.MIXED_AMOUNT - pints * PINT_PRICE) // HALF_PRICE)
            for pints in range(self.MIXED_AMOUNT // PINT_PRICE + 1)
            if (self.MIXED_AMOUNT - pints * PINT_PRICE) % HALF_PRICE == 0
        ]

    def closest_to(self, pints_share):
        return min(self.exact_splits(), key=lambda split: abs(split[0] / (split[0] + split[1]) - pints_share))

    def test_the_mixed_amount_has_three_exact_splits(self):
        self.assertEqual(self.MIXED_AMOUNT, 17460)
        self.assertEqual(self.exact_splits(), [(4, 46), (15, 27), (26, 8)])
        self.assertEqual(self.closest_to(0.75), (26, 8))
        self.assertEqual(self.closest_to(0.25), (15, 27))

    def test_a_tie_on_one_article_follows_what_the_till_sold(self):
        offers, rooms, engine = self.mix(300, 100)
        plan = plan_sales(self.MIXED_AMOUNT, offers, rooms, engine)
        pints, halves = plan.counts.get(40, 0), plan.counts.get(41, 0)
        self.assertGreater(halves, 0)
        self.assertTrue(2.5 <= pints / halves <= 3.5, plan.counts)
        self.assertEqual(plan.counts, {40: 26, 41: 8})
        self.assertEqual(plan.reason, EXACT)

    def test_the_mix_turned_round_turns_the_plan_round(self):
        # Turned round, no exact 174,60 € splits 1 to 3: 15 + 27 is the
        # exact split closest to the till's mix.
        offers, rooms, engine = self.mix(100, 300)
        plan = plan_sales(self.MIXED_AMOUNT, offers, rooms, engine)
        self.assertEqual(plan.reason, EXACT)
        self.assertEqual(plan.counts, {40: 15, 41: 27})

    def test_priced_alike_the_split_is_the_sold_ratio_exactly(self):
        # Priced alike, exactness cannot steer the split: 40 sales of one keg
        # sold 3 to 1 come out 30 and 10.
        offers = [
            fixed_offer(40, 530, {BLONDE: "0.5"}, sold=299, name="Pinte exemple"),
            fixed_offer(41, 530, {BLONDE: "0.5"}, sold=99, name="Pinte happy hour exemple"),
        ]
        plan = plan_sales(40 * 530, offers, {BLONDE: D("1000")}, additive_engine(offers))
        self.assertEqual(plan.counts, {40: 30, 41: 10})
        self.assertEqual(plan.reason, EXACT)

    def test_without_costs_a_garnish_never_drives_the_spirit_past_the_others(self):
        # « Ti punch exemple » pours rum and a lemon wedge whose gap is huge.
        # With no cost known (no `values`), a sale is keyed on its MOST
        # advanced article, the rum, so the rum keeps pace with the gin
        # instead of riding on the lemon's level.
        offers = [
            fixed_offer(50, 530, {RHUM: "4", CITRON: "1"}, sold=10, name="Ti punch exemple"),
            fixed_offer(51, 530, {GIN: "4"}, sold=10, name="Gin tonic exemple"),
        ]
        rooms = {RHUM: D("400"), GIN: D("400"), CITRON: D("1000")}
        plan = plan_sales(20 * 530, offers, rooms, additive_engine(offers))
        self.assertEqual(plan.counts, {50: 10, 51: 10})

    def test_without_costs_a_small_garnish_gap_holds_its_recipe_back(self):
        # The other way round: the lemon nearly full, its level keys the
        # « Ti punch exemple » - no cost says it is worth less than the rum -
        # and the rum waits.
        offers = [
            fixed_offer(50, 530, {RHUM: "4", CITRON: "1"}, sold=10, name="Ti punch exemple"),
            fixed_offer(51, 530, {GIN: "4"}, sold=10, name="Gin tonic exemple"),
        ]
        rooms = {RHUM: D("400"), GIN: D("400"), CITRON: D("3")}
        plan = plan_sales(20 * 530, offers, rooms, additive_engine(offers))
        self.assertLessEqual(plan.counts.get(50, 0), 3)
        self.assertLessEqual(plan.used.get(CITRON, ZERO), D("3"))
        self.assertGreater(plan.counts[51], plan.counts.get(50, 0))


class ValueWeightedLevelTests(SimpleTestCase):
    """`_Run.level`: the levels of the gaps a sale fills, averaged by what it
    pours of each IN VALUE (amount x cost per unit); with no cost known for
    any of them, the most advanced one."""

    def run_with(self, values):
        offers = [fixed_offer(1, HALF_PRICE, {RHUM: "4", CITRON: "1"}, sold=5)]
        # The rum's level is (0 + 4/2) / 400 = 0,005, the lemon's (0 + 1/2) / 10 = 0,05.
        rooms = {RHUM: D("400"), CITRON: D("10"), BLONDE: D("0")}
        return _Run(offers, rooms, additive_engine(offers), values)

    RUM_LEVEL = D("0.005")
    LEMON_LEVEL = D("0.05")
    COSTS = {RHUM: D("0.25"), CITRON: D("0.05")}  # € per cl, € per wedge

    def test_the_levels_are_averaged_by_value(self):
        run = self.run_with(self.COSTS)
        # Weights: 4 cl x 0,25 € = 1,00 for the rum, 1 x 0,05 € for the lemon.
        expected = (D("1.00") * self.RUM_LEVEL + D("0.05") * self.LEMON_LEVEL) / D("1.05")
        self.assertEqual(run.level({RHUM: D(4), CITRON: D(1)}), expected)
        # What is poured weighs too: a double rum leans further towards the rum.
        double = (D("2.00") * self.RUM_LEVEL + D("0.05") * self.LEMON_LEVEL) / D("2.05")
        self.assertEqual(run.level({RHUM: D(8), CITRON: D(1)}), double)

    def test_with_no_cost_at_all_the_most_advanced_article_decides(self):
        for values in (None, {}, {RHUM: ZERO, CITRON: ZERO}):
            with self.subTest(values=values):
                self.assertEqual(self.run_with(values).level({RHUM: D(4), CITRON: D(1)}), self.LEMON_LEVEL)

    def test_an_article_with_no_cost_beside_one_with_a_cost_does_not_count(self):
        run = self.run_with({RHUM: D("0.25")})
        self.assertEqual(run.level({RHUM: D(4), CITRON: D(1)}), self.RUM_LEVEL)
        run = self.run_with({CITRON: D("0.05")})
        self.assertEqual(run.level({RHUM: D(4), CITRON: D(1)}), self.LEMON_LEVEL)

    def test_only_what_a_sale_adds_to_a_gap_counts(self):
        run = self.run_with(self.COSTS)
        # Taken back (a choice pushed onto another bottle), or added to an
        # article with no room to fill: neither is a gap this sale fills.
        self.assertEqual(run.level({RHUM: D(-4), CITRON: D(1)}), self.LEMON_LEVEL)
        self.assertEqual(run.level({RHUM: D(4), BLONDE: D(1)}), self.RUM_LEVEL)
        self.assertIsNone(run.level({BLONDE: D(1)}))
        self.assertIsNone(run.level({RHUM: D(-4)}))
        self.assertIsNone(run.level({}))


class ValueWeightedPlanTests(SimpleTestCase):
    """What the value weighting changes in a plan: a garnish worth almost
    nothing neither drives its cocktail nor holds it back for good."""

    # € per unit of each article (per cl, per g), invented.
    COSTS = {
        GIN: D("0.32"),
        RHUM: D("0.27"),
        LIMONADE: D("0.003"),
        SIROP: D("0.04"),
        ACIDE: D("0.001"),
    }

    def follower(self):
        # Two best-sellers pour the lemonade with a spirit; « Limonade sirop
        # exemple », sold four times, pours it with a syrup no other recipe
        # pours. The lemonade's room holds 20 servings, fewer than the
        # best-sellers take before the follower's turn comes by the till's
        # mix alone (12 and 9).
        offers = [
            fixed_offer(1, 730, {GIN: "4", LIMONADE: "20"}, sold=120, name="Gin limonade exemple"),
            fixed_offer(2, 670, {RHUM: "4", LIMONADE: "20"}, sold=90, name="Rhum limonade exemple"),
            fixed_offer(3, 410, {LIMONADE: "20", SIROP: "3"}, sold=4, name="Limonade sirop exemple"),
        ]
        rooms = {GIN: D("100"), RHUM: D("100"), LIMONADE: D("400"), SIROP: D("150")}
        return offers, rooms, additive_engine(offers)

    def test_a_follower_still_fills_its_own_syrup_at_a_large_amount(self):
        offers, rooms, engine = self.follower()
        for amount in (20000, 100000, 1000000):
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, engine, self.COSTS)
                assert_plan_is_consistent(self, plan, amount, offers, rooms)
                self.assertGreater(plan.counts.get(3, 0), 0, plan.counts)
                self.assertGreater(plan.used.get(SIROP, ZERO), ZERO)
                # The lemonade is what stops everything: all 20 servings went.
                self.assertEqual(plan.used[LIMONADE], rooms[LIMONADE])
                self.assertEqual(plan.reason, GAPS_FULL)

    def test_without_costs_the_follower_never_gets_a_sale(self):
        # The old rule, kept where no cost is known: keyed on the lemonade -
        # its most advanced article - the follower ties with the best-sellers
        # and loses every tie on the till's mix, and the lemonade is full
        # before its turn: its syrup stays at 0 % at any amount.
        offers, rooms, engine = self.follower()
        for amount in (20000, 100000, 1000000):
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, engine)
                self.assertNotIn(3, plan.counts)
                self.assertNotIn(SIROP, plan.used)
                self.assertEqual(plan.used[LIMONADE], rooms[LIMONADE])

    def garnish(self):
        # « Ti punch exemple »: 4 cl of rum and 10 g of citric acid worth a
        # tenth of a centime the gram, whose gap is huge - far behind.
        offers = [
            fixed_offer(50, 530, {RHUM: "4", ACIDE: "10"}, sold=10, name="Ti punch exemple"),
            fixed_offer(51, 530, {GIN: "4"}, sold=10, name="Gin tonic exemple"),
        ]
        rooms = {RHUM: D("400"), GIN: D("400"), ACIDE: D("100000")}
        return offers, rooms, additive_engine(offers)

    def test_a_garnish_worth_almost_nothing_never_drives_the_spirit(self):
        # Weighed by amount, the 10 g of acid would key the ti punch on the
        # acid's level and sell the rum past every other gap; weighed by
        # value, the rum (1,08 € a sale against 0,01 €) keys it.
        offers, rooms, engine = self.garnish()
        one_serving = D(4) / D(400)
        for sales in (5, 20, 41, 60, 120):
            with self.subTest(sales=sales):
                plan = plan_sales(sales * 530, offers, rooms, engine, self.COSTS)
                self.assertEqual(plan.reason, EXACT)
                rum = plan.used.get(RHUM, ZERO) / rooms[RHUM]
                gin = plan.used.get(GIN, ZERO) / rooms[GIN]
                self.assertLessEqual(abs(rum - gin), one_serving, plan.counts)
                self.assertLess(plan.used.get(ACIDE, ZERO) / rooms[ACIDE], D("0.02"))


class MeasuredEffectTests(SimpleTestCase):
    """The plan ranks on what one more sale ADDS as the engine attributes it,
    never on a model of the recipe - the trap of a fixed spirit pushing a
    « shot au choix » onto another bottle."""

    # Invented: « Tequila exemple », « Vodka exemple », « Rhum exemple »,
    # « Gin exemple », « Jus exemple ».
    TEQUILA, VODKA, RUM, GIN_, JUICE = 61, 62, 63, 64, 65
    SHOT, SUNRISE, TI_PUNCH, GIN_TONIC = 70, 71, 72, 73

    def trap(self, tequila_room="400", price=630, declared_independent=False, vodka_room="100"):
        """Every recipe at one price (6,30 €) unless told otherwise, so that
        the exact total never steers the split: what is pinned is the
        ranking. `declared_independent` declares the two recipes the « OU »
        reaches independent - which is FALSE here: the planner then ranks
        them on their terms, a model, as the first version did."""
        engine = ChoiceEngine(
            sold={self.SHOT: 50, self.SUNRISE: 10, self.TI_PUNCH: 30, self.GIN_TONIC: 30},
            fixed={
                self.SUNRISE: {self.TEQUILA: 4, self.JUICE: 10},
                self.TI_PUNCH: {self.RUM: 4},
                self.GIN_TONIC: {self.GIN_: 4},
            },
            # « Shot au choix exemple »: tequila (the dearer) or vodka.
            choices={self.SHOT: (4, [self.TEQUILA, self.VODKA])},
            # The tequila bought holds 30 servings: the 10 sunrises' and 20
            # shots; the other 30 shots are vodka.
            caps={self.TEQUILA: 120, self.VODKA: 1000, self.RUM: 1000, self.GIN_: 1000, self.JUICE: 100000},
        )
        offers = [
            Offer(
                self.SHOT,
                "Shot au choix exemple",
                price,
                50,
                (({self.TEQUILA: D(4)}, {self.VODKA: D(4)}),),
                independent=declared_independent,
            ),
            Offer(
                self.SUNRISE,
                "Sunrise exemple",
                price,
                10,
                (({self.TEQUILA: D(4)},), ({self.JUICE: D(10)},)),
                independent=declared_independent,
            ),
            Offer(self.TI_PUNCH, "Ti punch exemple", price, 30, (({self.RUM: D(4)},),), independent=True),
            Offer(self.GIN_TONIC, "Gin tonic exemple", price, 30, (({self.GIN_: D(4)},),), independent=True),
        ]
        rooms = {
            self.TEQUILA: D(tequila_room),  # what a model would aim the sunrises at
            self.VODKA: D(vodka_room),
            self.RUM: D("400"),
            self.GIN_: D("400"),
            self.JUICE: D("10000"),
        }
        return offers, rooms, engine

    def test_the_engine_moves_a_shot_off_the_tequila(self):
        _offers, _rooms, engine = self.trap()
        base, after = engine({}), engine({self.SUNRISE: 1})
        added = {article: after.get(article, ZERO) - base.get(article, ZERO) for article in after}
        # A sunrise pours 4 cl of tequila, and the engine takes a shot off
        # the tequila for it: as attributed, it fills the VODKA.
        self.assertEqual(added[self.TEQUILA], ZERO)
        self.assertEqual(added[self.VODKA], D(4))
        self.assertEqual(added[self.JUICE], D(10))

    def fill(self, plan, rooms, article):
        return plan.used.get(article, ZERO) / rooms[article]

    def test_ranked_on_a_model_the_plan_falls_into_the_trap(self):
        # The control: declared independent, the shot and the sunrise are
        # ranked as pouring tequila (room 400, like the rum's) and every one
        # of them lands on the vodka. 60 sales: the vodka 80 % full, the rum
        # and the gin 20 % - the shape the module docstring measured.
        offers, rooms, engine = self.trap(declared_independent=True)
        plan = plan_sales(60 * 630, offers, rooms, engine)
        vodka = self.fill(plan, rooms, self.VODKA)
        others = max(self.fill(plan, rooms, self.RUM), self.fill(plan, rooms, self.GIN_))
        self.assertGreater(vodka, 3 * others)

    def test_the_article_a_fixed_draw_displaces_onto_keeps_pace_with_the_others(self):
        offers, rooms, engine = self.trap()
        for sales in (10, 20, 40, 60, 100):
            with self.subTest(sales=sales):
                plan = plan_sales(sales * 630, offers, rooms, engine)
                assert_plan_is_consistent(self, plan, sales * 630, offers, rooms)
                self.assertEqual(plan.reason, EXACT)
                vodka = self.fill(plan, rooms, self.VODKA)
                others = max(self.fill(plan, rooms, self.RUM), self.fill(plan, rooms, self.GIN_))
                # Webster: within one serving's share of the vodka's room (4 / 100).
                self.assertLessEqual(vodka, others + D("0.04"), plan)
                # Nothing the plan proposes reaches the tequila, as attributed.
                self.assertEqual(plan.used.get(self.TEQUILA, ZERO), ZERO)
                self.assertGreater(plan.counts.get(self.TI_PUNCH, 0), 0)
                self.assertGreater(plan.counts.get(self.GIN_TONIC, 0), 0)
                # Every sunrise and every shot adds 4 cl of vodka here, and
                # nothing else does.
                vodka_servings = plan.counts.get(self.SUNRISE, 0) + plan.counts.get(self.SHOT, 0)
                self.assertEqual(plan.used.get(self.VODKA, ZERO), D(4) * vodka_servings)

    def test_at_mixed_prices_the_vodka_never_passes_its_room(self):
        # 3,10 € shots beside 5,70 € and 8,90 € recipes: the exact total may
        # take a few more shots than their share, never past the room.
        offers, rooms, engine = self.trap()
        prices = {self.SHOT: 310, self.SUNRISE: 890, self.TI_PUNCH: 570, self.GIN_TONIC: 570}
        offers = [Offer(o.recipe_id, o.name, prices[o.recipe_id], o.sold, o.terms, o.independent) for o in offers]
        for amount in (5000, 10000, 20000, 40000, 100000):
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, engine)
                assert_plan_is_consistent(self, plan, amount, offers, rooms)
                self.assertEqual(plan.used.get(self.TEQUILA, ZERO), ZERO)

    def test_a_recipe_whose_fixed_draw_the_engine_absorbs_is_not_blocked(self):
        # The tequila is oversold (room -10), but one more sunrise adds nothing
        # to it as the engine counts it: only the vodka and the juice move.
        offers, rooms, engine = self.trap(tequila_room="-10")
        self.assertEqual(blocked(offers, rooms, engine), {})

    def test_declared_independent_or_measured_an_additive_engine_plans_the_same(self):
        rng = random.Random(77)
        for case in range(10):
            offers = [
                fixed_offer(
                    80 + index,
                    rng.choice([310, 370, 570, 890]),
                    {article: rng.choice(["1", "2", "4"]) for article in rng.sample([1, 2, 3, 4], rng.randint(1, 2))},
                    sold=rng.randint(1, 30),
                )
                for index in range(4)
            ]
            measured = [Offer(o.recipe_id, o.name, o.price_cents, o.sold, o.terms, independent=False) for o in offers]
            rooms = {article: D(rng.choice([10, 30, 80])) for article in (1, 2, 3, 4)}
            amount = rng.randint(0, 500) * 10
            with self.subTest(case=case):
                self.assertEqual(
                    plan_sales(amount, offers, rooms, additive_engine(offers)),
                    plan_sales(amount, measured, rooms, additive_engine(measured)),
                )

    def test_an_independent_recipe_is_never_asked_about(self):
        offers = [
            fixed_offer(90, 310, {RHUM: "4"}, sold=10),
            fixed_offer(91, 370, {GIN: "4"}, sold=10),
        ]
        rooms = {RHUM: AMPLE, GIN: AMPLE}
        engine = additive_engine(offers)
        plan = plan_sales(65000, offers, rooms, engine)  # about two hundred sales
        self.assertEqual(plan.reason, EXACT)
        self.assertGreater(sum(plan.counts.values()), 150)
        # The base and the final reading: never one call per sale.
        self.assertLessEqual(engine.calls, 2)


class FirstSalesTests(SimpleTestCase):
    """`first_sales`: what ONE sale of each offer adds on its own, measured
    by the engine, and the articles it would take past their room;
    `blocked` is the offers whose set is not empty."""

    # The articles and recipes of MeasuredEffectTests' trap.
    TEQUILA, VODKA, RUM, GIN_, JUICE = 61, 62, 63, 64, 65
    SHOT, SUNRISE, TI_PUNCH, GIN_TONIC = 70, 71, 72, 73

    def trap(self, **changes):
        return MeasuredEffectTests().trap(**changes)

    def test_each_offer_says_what_one_sale_adds_as_the_engine_books_it(self):
        offers, rooms, engine = self.trap()
        self.assertEqual(
            first_sales(offers, rooms, engine),
            {
                # The shot's dearer option is at its cap: it goes on the vodka.
                self.SHOT: ({self.VODKA: D(4)}, set()),
                # The sunrise's tequila pushes a shot onto the vodka.
                self.SUNRISE: ({self.VODKA: D(4), self.JUICE: D(10)}, set()),
                self.TI_PUNCH: ({self.RUM: D(4)}, set()),
                self.GIN_TONIC: ({self.GIN_: D(4)}, set()),
            },
        )
        # The base, then the two offers an « OU » reaches - the independent
        # ones are never asked about.
        self.assertEqual(engine.calls, 3)

    def test_the_articles_past_their_room_are_named_and_blocked_is_the_non_empty_ones(self):
        offers, rooms, engine = self.trap(vodka_room="2")
        first = first_sales(offers, rooms, engine)
        self.assertEqual(first[self.SHOT], ({self.VODKA: D(4)}, {self.VODKA}))
        self.assertEqual(first[self.SUNRISE], ({self.VODKA: D(4), self.JUICE: D(10)}, {self.VODKA}))
        self.assertEqual(first[self.TI_PUNCH][1], set())
        self.assertEqual(first[self.GIN_TONIC][1], set())
        expected = {recipe_id: past for recipe_id, (_effect, past) in first.items() if past}
        self.assertEqual(expected, {self.SHOT: {self.VODKA}, self.SUNRISE: {self.VODKA}})
        self.assertEqual(blocked(offers, rooms, engine), expected)

    def test_an_effect_may_take_back_and_what_it_takes_back_blocks_nothing(self):
        # A sunrise of 2 cl: one more takes 2 cl of tequila and pushes a whole
        # 4 cl shot off it - the tequila nets -2 as the engine books it. Its
        # room is 0, yet the sunrise is not blocked: it adds nothing there.
        engine = ChoiceEngine(
            sold={self.SHOT: 50, self.SUNRISE: 10},
            fixed={self.SUNRISE: {self.TEQUILA: 2, self.JUICE: 10}},
            choices={self.SHOT: (4, [self.TEQUILA, self.VODKA])},
            caps={self.TEQUILA: 120, self.VODKA: 1000, self.JUICE: 100000},
        )
        offers = [
            Offer(self.SHOT, "Shot au choix exemple", 310, 50, (({self.TEQUILA: D(4)}, {self.VODKA: D(4)}),)),
            Offer(self.SUNRISE, "Sunrise exemple", 890, 10, (({self.TEQUILA: D(2)},), ({self.JUICE: D(10)},))),
        ]
        rooms = {self.TEQUILA: D("0"), self.VODKA: D("100"), self.JUICE: D("10000")}
        first = first_sales(offers, rooms, engine)
        self.assertEqual(first[self.SUNRISE], ({self.TEQUILA: D(-2), self.VODKA: D(4), self.JUICE: D(10)}, set()))
        self.assertEqual(blocked(offers, rooms, engine), {})

    def test_nothing_offered_is_nothing_measured(self):
        engine = additive_engine([])
        self.assertEqual(first_sales([], {BLONDE: D("2")}, engine), {})
        self.assertEqual(engine.calls, 1)  # the base


class ReviveTests(SimpleTestCase):
    """A recipe set aside is measured again once the plan has moved on
    (`_Run.revive`), on the stock page's own engine.

    « Shot au choix exemple » pours 4 cl of « Rhum ambré exemple » (the
    dearer) or of « Rhum blanc exemple »; « Ti punch exemple » 2 cl of the
    amber rum, fixed. The engine books a shot on the amber rum while it is
    under what was bought less its allowance, a serving that starts below
    that cap finished out of the same bottle (rounded UP); a fixed pour is
    counted first. So the 3rd shot of a plan rounds onto the nearly full
    amber rum and goes past its room - set aside -, and once a ti punch has
    taken the amber rum to its cap, the engine books the next shot on the
    white rum, which has room to spare.
    """

    AMBRE, BLANC = 21, 22
    SHOT, PUNCH = 30, 31
    UNIT_COSTS = {AMBRE: D("1.30"), BLANC: D("0.30")}  # € per cl, invented
    LOSS_PERCENT = 10
    SHOT_PRICE, PUNCH_PRICE = 370, 530

    def engine(self):
        """(consumption, rooms): 180 cl of amber rum and 300 of white bought
        since the take, nothing counted, 37 shots and 2 ti punches sold. The
        amber rum's cap is 162 cl (180 less 10 %); the sales took 4 + 37 x 4 =
        152 cl of it, so its room is 10 cl; the white rum's is 270 cl."""
        available = {self.AMBRE: D(180), self.BLANC: D(300)}
        sales = SalesRead(
            sold={self.SHOT: 37, self.PUNCH: 2},
            as_itself={},
            recipes=[SimpleNamespace(pk=self.SHOT), SimpleNamespace(pk=self.PUNCH)],
            pool_of={},
            loss_fractions=dict.fromkeys(available, loss_fraction(self.LOSS_PERCENT)),
            terms={
                self.SHOT: [[{self.AMBRE: D(4)}, {self.BLANC: D(4)}]],
                self.PUNCH: [[{self.AMBRE: D(2)}]],
            },
        )

        def consumption(extra):
            attributed = attribute_sales(sales, available, self.UNIT_COSTS, extra)
            return {article: sold.headline for article, sold in attributed.items()}

        base = consumption({})
        # What gaps.py leaves to fill: bought, less what the sales took, less the allowance.
        rooms = {
            article: available[article]
            - base.get(article, ZERO)
            - loss_allowance(available[article], self.LOSS_PERCENT)
            for article in available
        }
        return consumption, rooms

    def offers(self):
        return [
            Offer(
                self.SHOT,
                "Shot au choix exemple",
                self.SHOT_PRICE,
                37,
                (({self.AMBRE: D(4)}, {self.BLANC: D(4)}),),
            ),
            Offer(self.PUNCH, "Ti punch exemple", self.PUNCH_PRICE, 2, (({self.AMBRE: D(2)},),)),
        ]

    def added(self, consumption, extra):
        base, after = consumption({}), consumption(extra)
        return {
            article: after.get(article, ZERO) - base.get(article, ZERO)
            for article in set(base) | set(after)
            if after.get(article, ZERO) != base.get(article, ZERO)
        }

    def test_the_engine_rounds_a_shot_onto_the_amber_rum_until_a_ti_punch_caps_it(self):
        consumption, rooms = self.engine()
        self.assertEqual(rooms, {self.AMBRE: D(10), self.BLANC: D(270)})
        self.assertEqual(self.added(consumption, {self.SHOT: 2}), {self.AMBRE: D(8)})
        # 158 cl under the cap is 39,5 servings: the 40th is finished out of
        # the amber rum, 2 cl past its room.
        self.assertEqual(self.added(consumption, {self.SHOT: 3}), {self.AMBRE: D(12)})
        # A ti punch takes the amber rum to its cap exactly...
        self.assertEqual(self.added(consumption, {self.SHOT: 2, self.PUNCH: 1}), {self.AMBRE: D(10)})
        # ...and the third shot then goes on the white rum.
        self.assertEqual(self.added(consumption, {self.SHOT: 3, self.PUNCH: 1}), {self.AMBRE: D(10), self.BLANC: D(4)})

    def test_a_shot_set_aside_comes_back_onto_the_white_rum(self):
        # 123,70 € is 32 shots and one ti punch, and no other mix of the two
        # prices. The plan sells two shots, sets the third aside (past the
        # amber rum's room), sells the ti punch - then, nothing else fitting,
        # measures the shot again: it goes on the white rum now.
        consumption, rooms = self.engine()
        offers = self.offers()
        amount = 32 * self.SHOT_PRICE + self.PUNCH_PRICE
        plan = plan_sales(amount, offers, rooms, consumption, self.UNIT_COSTS)
        assert_plan_is_consistent(self, plan, amount, offers, rooms)
        self.assertEqual(plan.counts, {self.SHOT: 32, self.PUNCH: 1})
        self.assertEqual(plan.reason, EXACT)
        self.assertEqual(plan.used, {self.AMBRE: D(10), self.BLANC: D(120)})

    def test_without_the_revive_the_plan_stopped_short_and_said_the_gaps_were_full(self):
        # The control: the shot set aside for good, the plan ended at 12,70 €
        # and blamed full gaps while the white rum had 270 cl to fill.
        consumption, rooms = self.engine()
        amount = 32 * self.SHOT_PRICE + self.PUNCH_PRICE
        with mock.patch.object(_Run, "revive", return_value=0):
            plan = plan_sales(amount, self.offers(), rooms, consumption, self.UNIT_COSTS)
        self.assertEqual(plan.counts, {self.SHOT: 2, self.PUNCH: 1})
        self.assertEqual(plan.total_cents, 2 * self.SHOT_PRICE + self.PUNCH_PRICE)
        self.assertEqual(plan.reason, GAPS_FULL)

    def test_money_left_while_the_revived_shot_still_fits_is_no_combination(self):
        # Five cents off the prices' step: the closest below, and the reason
        # says a recipe still fits - the shot, measured again.
        consumption, rooms = self.engine()
        offers = self.offers()
        amount = 32 * self.SHOT_PRICE + self.PUNCH_PRICE + 5
        plan = plan_sales(amount, offers, rooms, consumption, self.UNIT_COSTS)
        assert_plan_is_consistent(self, plan, amount, offers, rooms)
        self.assertEqual(plan.counts, {self.SHOT: 32, self.PUNCH: 1})
        self.assertEqual(plan.remainder_cents, 5)
        self.assertEqual(plan.reason, NO_COMBINATION)

    def test_a_large_amount_fills_the_white_rum_and_then_the_gaps_are_full(self):
        consumption, rooms = self.engine()
        offers = self.offers()
        plan = plan_sales(50000, offers, rooms, consumption, self.UNIT_COSTS)
        assert_plan_is_consistent(self, plan, 50000, offers, rooms)
        self.assertEqual(plan.used[self.AMBRE], rooms[self.AMBRE])
        # Within one serving of the white rum's room: 67 shots of 4 cl in 270.
        self.assertEqual(plan.used[self.BLANC], D(268))
        self.assertEqual(plan.reason, GAPS_FULL)
        # And it is true: one more of either recipe goes past a room.
        for offer in offers:
            with self.subTest(recipe=offer.name):
                trial = dict(plan.counts)
                trial[offer.recipe_id] = trial.get(offer.recipe_id, 0) + 1
                more = self.added(consumption, trial)
                self.assertTrue(any(amount > limit(rooms, article) for article, amount in more.items()), more)


class DeterministicTests(SimpleTestCase):
    """Same input, same list - whatever order the offers come in."""

    def instance(self):
        offers = [
            fixed_offer(40, PINT_PRICE, {BLONDE: "0.5"}, sold=12, name="Pinte exemple"),
            fixed_offer(41, HALF_PRICE, {BLONDE: "0.25"}, sold=12, name="Demi exemple"),
            fixed_offer(42, PINT_PRICE, {AMBREE: "0.5"}, sold=7, name="Pinte ambrée exemple"),
            fixed_offer(43, 890, {RHUM: "4", CITRON: "1"}, sold=5, name="Ti punch exemple"),
            fixed_offer(44, 890, {GIN: "4", CITRON: "1"}, sold=5, name="Gin tonic exemple"),
            fixed_offer(45, 890, {GIN: "4"}, sold=5, name="Gin tonic exemple"),  # same name: the id decides
        ]
        rooms = {BLONDE: D("30"), AMBREE: D("12"), RHUM: D("200"), GIN: D("150"), CITRON: D("25")}
        return offers, rooms

    def test_the_same_input_gives_the_same_plan(self):
        offers, rooms = self.instance()
        first = plan_sales(12345, offers, rooms, additive_engine(offers))
        second = plan_sales(12345, offers, rooms, additive_engine(offers))
        self.assertEqual(first, second)

    def test_the_offers_order_does_not_matter(self):
        offers, rooms = self.instance()
        reference = plan_sales(12345, offers, rooms, additive_engine(offers))
        rng = random.Random(9)
        orders = [list(reversed(offers))] + [rng.sample(offers, len(offers)) for _ in range(5)]
        for index, order in enumerate(orders):
            with self.subTest(order=index):
                plan = plan_sales(12345, order, rooms, additive_engine(order))
                self.assertEqual(plan, reference)
                # The list itself, in the order the sales were chosen.
                self.assertEqual(list(plan.counts.items()), list(reference.counts.items()))

    def test_the_offers_order_does_not_matter_with_an_ou_either(self):
        trap = MeasuredEffectTests()
        offers, rooms, engine = trap.trap()
        reference = plan_sales(15000, offers, rooms, engine)
        for index, order in enumerate([list(reversed(offers)), offers[2:] + offers[:2]]):
            with self.subTest(order=index):
                self.assertEqual(plan_sales(15000, order, rooms, engine), reference)

    def test_the_offers_order_does_not_matter_with_a_revive_either(self):
        revive = ReviveTests()
        consumption, rooms = revive.engine()
        offers = revive.offers()
        reference = plan_sales(20000, offers, rooms, consumption, revive.UNIT_COSTS)
        self.assertEqual(plan_sales(20000, offers[::-1], rooms, consumption, revive.UNIT_COSTS), reference)


class PouredTests(SimpleTestCase):
    """Plan.poured says which gaps a line fills; for an additive engine the
    lines add up to what the plan adds."""

    def test_poured_per_recipe_adds_up_to_used(self):
        offers, rooms = DeterministicTests().instance()
        for amount in (890, 4000, 12345, 30000):
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, additive_engine(offers))
                self.assertEqual(set(plan.poured), set(plan.counts))
                summed: dict[int, Decimal] = {}
                for poured in plan.poured.values():
                    for article, amount_poured in poured.items():
                        summed[article] = summed.get(article, ZERO) + amount_poured
                self.assertEqual({a: v for a, v in summed.items() if v}, plan.used)

    def test_poured_is_count_times_the_pour(self):
        offers = [fixed_offer(43, 890, {RHUM: "4", CITRON: "1"}, sold=5)]
        rooms = {RHUM: D("200"), CITRON: D("25")}
        plan = plan_sales(890 * 7, offers, rooms, additive_engine(offers))
        self.assertEqual(plan.counts, {43: 7})
        self.assertEqual(plan.poured, {43: {RHUM: D(28), CITRON: D(7)}})


class BlockedTests(SimpleTestCase):
    def test_a_sale_that_cannot_fit_names_the_articles_past_their_room(self):
        offers = [
            fixed_offer(1, PINT_PRICE, {BLONDE: "0.5"}),  # fits: 0,5 L into 2 L
            fixed_offer(2, HALF_PRICE, {AMBREE: "0.5"}),  # 0,5 L into 0,25 L
            fixed_offer(3, 890, {RHUM: "4", CITRON: "1"}),  # the rum fits, the lemon's room is gone
            fixed_offer(4, 890, {GIN: "4"}),  # oversold gin
            fixed_offer(5, 890, {BLONDE: "0.5", AMBREE: "1", GIN: "4"}),
        ]
        rooms = {BLONDE: D("2"), AMBREE: D("0.25"), RHUM: D("40"), CITRON: D("0"), GIN: D("-3")}
        self.assertEqual(
            blocked(offers, rooms, additive_engine(offers)),
            {2: {AMBREE}, 3: {CITRON}, 4: {GIN}, 5: {AMBREE, GIN}},
        )

    def test_a_sale_that_exactly_fills_its_room_is_not_blocked(self):
        offers = [fixed_offer(1, PINT_PRICE, {BLONDE: "0.5"})]
        self.assertEqual(blocked(offers, {BLONDE: D("0.5")}, additive_engine(offers)), {})

    def test_nothing_to_block(self):
        self.assertEqual(blocked([], {BLONDE: D("2")}, additive_engine([])), {})


class MakeableTests(SimpleTestCase):
    """What the prices still proposable can make, each any number of times -
    the guard that keeps every sale's rest makeable."""

    def test_the_totals_a_pint_and_a_half_make(self):
        makeable = _Makeable({PINT_PRICE, HALF_PRICE}, 2000)
        self.assertEqual(makeable.step, 30)
        self.assertTrue(makeable.makes(0))
        self.assertTrue(makeable.makes(1320))  # four halves
        self.assertTrue(makeable.makes(1230))  # a pint and two halves
        self.assertFalse(makeable.makes(750))  # what a pint leaves of four halves
        self.assertFalse(makeable.makes(1225))  # off the 0,30 € step
        self.assertFalse(makeable.makes(-330))
        # Three pints and a half, but past what was asked about.
        self.assertTrue(reachable_totals([PINT_PRICE, HALF_PRICE], 2040)[2040])
        self.assertFalse(makeable.makes(2040))

    def test_the_largest_total_up_to(self):
        makeable = _Makeable({310, 3370}, 15000)
        self.assertEqual(makeable.best_up_to(3700), 3680)  # 33,70 + 3,10 €
        self.assertEqual(makeable.best_up_to(7750), 7750)  # 25 x 3,10 €
        self.assertEqual(makeable.best_up_to(309), 0)
        self.assertEqual(makeable.best_up_to(0), 0)
        # Asked past the top, the best up to the top: 150,00 € is no
        # combination of the two, 148,80 € (48 x 3,10 €) is the closest.
        self.assertEqual(makeable.best_up_to(10**9), 14880)
        self.assertEqual(best_below(reachable_totals([310, 3370], 15000), 15000), 14880)

    def test_against_brute_force(self):
        rng = random.Random(1001)
        for case in range(60):
            step = rng.choice([1, 10, 30])
            prices = set()
            for _ in range(rng.randint(1, 4)):
                price = step * rng.randint(2, 1500 // step)
                if price % 50 == 0:  # never a price a real menu could print
                    price += step
                prices.add(price)
            self.assertTrue(all(price % 50 for price in prices), prices)
            top = rng.randint(0, 3000)
            makeable = _Makeable(prices, top)
            reachable = {0}
            for total in range(1, top + 1):
                if any(total - price in reachable for price in prices):
                    reachable.add(total)
            with self.subTest(case=case, prices=sorted(prices), top=top):
                for total in range(top + 1):
                    self.assertEqual(makeable.makes(total), total in reachable)
                self.assertEqual(makeable.best_up_to(top), max(reachable))


class UsualServingTests(SimpleTestCase):
    def pint_and_half(self, pints_sold, halves_sold):
        return [
            fixed_offer(40, PINT_PRICE, {BLONDE: "0.5"}, sold=pints_sold, name="Pinte exemple"),
            fixed_offer(41, HALF_PRICE, {BLONDE: "0.25"}, sold=halves_sold, name="Demi exemple"),
        ]

    def test_the_most_sold_offer_says_the_serving(self):
        self.assertEqual(usual_servings(self.pint_and_half(300, 100), {BLONDE}), {BLONDE: D("0.5")})
        self.assertEqual(usual_servings(self.pint_and_half(100, 300), {BLONDE}), {BLONDE: D("0.25")})

    def test_a_tie_in_sales_goes_by_name(self):
        # « Demi exemple » before « Pinte exemple ».
        self.assertEqual(usual_servings(self.pint_and_half(50, 50), {BLONDE}), {BLONDE: D("0.25")})

    def test_only_the_targets_and_the_first_option_first(self):
        offers = [
            Offer(1, "Shot au choix exemple", 310, 9, (({RHUM: D(4)}, {GIN: D(5)}),)),
            fixed_offer(2, PINT_PRICE, {RHUM: "6", CITRON: "1"}, sold=3),
        ]
        self.assertEqual(usual_servings(offers, {RHUM, GIN}), {RHUM: D(4), GIN: D(5)})
        self.assertEqual(usual_servings(offers, set()), {})

    def test_a_zero_amount_is_no_serving(self):
        offers = [
            Offer(1, "Recette exemple", 310, 9, (({RHUM: D(0)},),)),
            fixed_offer(2, PINT_PRICE, {RHUM: "6"}, sold=3),
        ]
        self.assertEqual(usual_servings(offers, {RHUM}), {RHUM: D(6)})


class OfferAndLimitTests(SimpleTestCase):
    def test_limit_is_the_room_or_nothing(self):
        rooms = {1: D("12.5"), 2: D("0"), 3: D("-4")}
        self.assertEqual(limit(rooms, 1), D("12.5"))
        self.assertEqual(limit(rooms, 2), ZERO)
        self.assertEqual(limit(rooms, 3), ZERO)
        self.assertEqual(limit(rooms, 99), ZERO)

    def test_reach_and_fixed_pour(self):
        offer = Offer(
            1,
            "Recette exemple",
            HALF_PRICE,
            1,
            (({RHUM: D(4)}, {GIN: D(5)}), ({CITRON: D(1)},), ({CITRON: D(1), BLONDE: D(0)},)),
        )
        self.assertEqual(offer.reach(), {RHUM, GIN, CITRON})
        self.assertEqual(offer.fixed_pour(), {RHUM: D(4), CITRON: D(2), BLONDE: D(0)})
        self.assertFalse(offer.independent)


class LargeAmountTests(SimpleTestCase):
    def test_ten_thousand_euros_is_quick_and_exact(self):
        offers = [
            fixed_offer(1, 310, {RHUM: "4"}, sold=20),
            fixed_offer(2, 370, {GIN: "4"}, sold=10),
        ]
        rooms = {RHUM: D("10000000"), GIN: D("10000000")}
        started = time.perf_counter()
        plan = plan_sales(1_000_000, offers, rooms, additive_engine(offers))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 10)
        self.assertEqual(plan.total_cents, 1_000_000)
        self.assertEqual(plan.reason, EXACT)
        self.assertEqual(310 * plan.counts.get(1, 0) + 370 * plan.counts.get(2, 0), 1_000_000)

    def test_ten_thousand_euros_measured_through_an_engine_is_quick_too(self):
        offers = [
            fixed_offer(1, 310, {RHUM: "4"}, sold=20, independent=False),
            fixed_offer(2, 370, {GIN: "4"}, sold=10, independent=False),
        ]
        rooms = {RHUM: D("10000000"), GIN: D("10000000")}
        started = time.perf_counter()
        plan = plan_sales(1_000_000, offers, rooms, additive_engine(offers))
        self.assertLess(time.perf_counter() - started, 10)
        self.assertEqual(plan.reason, EXACT)

    def test_the_makeable_bitset_stays_small(self):
        # A 1-cent price grid at the largest amount: 1 000 001 bits, 125 kB.
        makeable = _Makeable({1, 499}, 1_000_000)
        self.assertEqual(makeable.size, 1_000_001)
        self.assertLess(makeable.bits.bit_length(), 1_000_002)


# ---------------------------------------------------------------------------
# A list, amount after amount: plan_sales(already=...)
# ---------------------------------------------------------------------------


def with_list(*counts: dict[int, int]) -> dict[int, int]:
    """Several plans' sales added up - what `gaps.list_counts` hands the
    planner for a list's entries."""
    total: dict[int, int] = {}
    for one in counts:
        for recipe_id, count in one.items():
            total[recipe_id] = total.get(recipe_id, 0) + count
    return total


def plan_a_list(amounts, offers, rooms, engine, values=None) -> list[tuple[Plan, dict[int, int]]]:
    """Every amount planned on top of the ones before it, as the page adds
    them: [(its plan, the list's sales once it is in)]. An amount nothing is
    proposed for is not kept, and leaves the list as it was."""
    already: dict[int, int] = {}
    planned = []
    for amount in amounts:
        plan = plan_sales(amount, offers, rooms, engine, values, already)
        already = with_list(already, plan.counts)
        planned.append((plan, already))
    return planned


def engine_adds(engine, counts: dict[int, int]) -> dict[int, Decimal]:
    """What `counts` add to each article, as `engine` attributes them."""
    base, after = engine({}), engine(counts)
    return {
        article: after.get(article, ZERO) - base.get(article, ZERO)
        for article in set(base) | set(after)
        if after.get(article, ZERO) != base.get(article, ZERO)
    }


def fits(engine, rooms, counts: dict[int, int]) -> bool:
    """Whether `counts`, all together, stay within every room."""
    return all(amount <= limit(rooms, article) for article, amount in engine_adds(engine, counts).items())


def assert_list_is_consistent(case, planned, amounts, offers, rooms, engine):
    """What every entry of a list owes: its money adds up, its counts are its
    new sales only, and its `used` is the whole list - old and new - as the
    engine counts it, never past a room."""
    price = {offer.recipe_id: offer.price_cents for offer in offers}
    before: dict[int, int] = {}
    for index, ((plan, after), amount) in enumerate(zip(planned, amounts, strict=True)):
        with case.subTest(entry=index, amount=amount):
            case.assertIn(plan.reason, REASONS)
            case.assertEqual(plan.total_cents + plan.remainder_cents, amount)
            case.assertEqual(plan.total_cents, sum(price[recipe] * count for recipe, count in plan.counts.items()))
            case.assertTrue(all(count > 0 for count in plan.counts.values()), plan.counts)
            case.assertEqual(plan.reason == EXACT, plan.remainder_cents == 0)
            case.assertEqual(set(plan.poured), set(plan.counts))
            case.assertEqual(after, with_list(before, plan.counts))
            case.assertEqual(plan.used, engine_adds(engine, after))
            for article in set(rooms) | set(plan.used):
                case.assertLessEqual(
                    plan.used.get(article, ZERO),
                    limit(rooms, article),
                    f"article {article}: the list adds {plan.used.get(article)}, past its room {rooms.get(article)}",
                )
        before = after


class AlreadyTests(SimpleTestCase):
    """`already`: the list's earlier sales, which a new amount is planned on
    top of. `Plan.counts` and `poured` are the new sales only; `used` is
    what the old and the new add together; the old count as added."""

    def pint_and_shot(self):
        # 3,70 € shots make no multiple of 5,70 € below 37,00 €: an amount
        # of pints is pints only.
        offers = [
            fixed_offer(40, PINT_PRICE, {BLONDE: "0.5"}, sold=10, name="Pinte exemple"),
            fixed_offer(43, 370, {RHUM: "4"}, sold=10, name="Shot exemple"),
        ]
        return offers, {BLONDE: D("30"), RHUM: D("400")}, additive_engine(offers)

    def test_counts_and_poured_are_the_new_sales_used_the_whole_list(self):
        offers, rooms, engine = self.pint_and_shot()
        plan = plan_sales(2 * PINT_PRICE, offers, rooms, engine, already={40: 3})
        self.assertEqual(plan.counts, {40: 2})
        self.assertEqual((plan.total_cents, plan.remainder_cents, plan.reason), (2 * PINT_PRICE, 0, EXACT))
        self.assertEqual(plan.poured, {40: {BLONDE: D("1.0")}})
        # Three pints of the list and two new ones.
        self.assertEqual(plan.used, {BLONDE: D("2.5")})

    def test_a_recipe_only_the_list_holds_is_in_used_never_in_counts(self):
        offers, rooms, engine = self.pint_and_shot()
        plan = plan_sales(PINT_PRICE, offers, rooms, engine, already={43: 5})
        self.assertEqual(plan.counts, {40: 1})
        self.assertEqual(plan.poured, {40: {BLONDE: D("0.5")}})
        self.assertEqual(plan.used, {BLONDE: D("0.5"), RHUM: D(20)})

    def test_a_gap_the_list_half_filled_ranks_behind_an_untouched_one(self):
        offers = [
            fixed_offer(20, 530, {BLONDE: "1"}, sold=4, name="Pinte blonde exemple"),
            fixed_offer(21, 530, {AMBREE: "1"}, sold=4, name="Pinte ambrée exemple"),
        ]
        rooms = {BLONDE: D("10"), AMBREE: D("10")}
        engine = additive_engine(offers)
        # The control: alone, the two gaps tie and the name decides.
        self.assertEqual(plan_sales(530, offers, rooms, engine).counts, {21: 1})
        # The list filled half the amber's gap: the untouched blonde comes
        # first, and takes every sale until it is as far along.
        self.assertEqual(plan_sales(530, offers, rooms, engine, already={21: 5}).counts, {20: 1})
        plan = plan_sales(5 * 530, offers, rooms, engine, already={21: 5})
        self.assertEqual(plan.counts, {20: 5})
        self.assertEqual(plan.used, {BLONDE: D(5), AMBREE: D(5)})

    def test_the_list_s_sales_count_in_the_till_s_mix_too(self):
        # Priced alike and sold 3 to 1: 20 sales come out 15 and 5, and 20
        # more on top of them 15 and 5 again - the 30 and 10 of one plan of 40.
        offers = [
            fixed_offer(40, 530, {BLONDE: "0.5"}, sold=299, name="Pinte exemple"),
            fixed_offer(41, 530, {BLONDE: "0.5"}, sold=99, name="Pinte happy hour exemple"),
        ]
        rooms = {BLONDE: D("1000")}
        engine = additive_engine(offers)
        first = plan_sales(20 * 530, offers, rooms, engine)
        second = plan_sales(20 * 530, offers, rooms, engine, already=first.counts)
        self.assertEqual(first.counts, {40: 15, 41: 5})
        self.assertEqual(second.counts, {40: 15, 41: 5})
        self.assertEqual(with_list(first.counts, second.counts), plan_sales(40 * 530, offers, rooms, engine).counts)

    def test_amount_zero_with_a_list_is_what_the_list_adds_and_no_sale(self):
        offers, rooms, engine = self.pint_and_shot()
        plan = plan_sales(0, offers, rooms, engine, already={40: 3, 43: 2})
        self.assertEqual(plan, Plan({}, 0, 0, EXACT, used={BLONDE: D("1.5"), RHUM: D(8)}))

    def test_an_empty_list_is_no_list(self):
        offers, rooms, engine = self.pint_and_shot()
        alone = plan_sales(2 * PINT_PRICE + 370, offers, rooms, engine)
        for already in (None, {}, {40: 0, 43: 0}):
            with self.subTest(already=already):
                self.assertEqual(plan_sales(0, offers, rooms, engine, already=already), Plan({}, 0, 0, EXACT))
                self.assertEqual(plan_sales(2 * PINT_PRICE + 370, offers, rooms, engine, already=already), alone)

    def test_a_recipe_of_the_list_that_is_no_offer_any_more_is_still_played(self):
        # « Pinte ancienne exemple » was proposed by an earlier entry and is
        # no offer now (repriced, blocked, not sold any more): what its sales
        # poured still fills the keg.
        offers = [fixed_offer(40, PINT_PRICE, {BLONDE: "0.5"}, sold=10, name="Pinte exemple")]
        retired = fixed_offer(99, 590, {BLONDE: "0.5"}, sold=0, name="Pinte ancienne exemple")
        engine = additive_engine([*offers, retired])
        rooms = {BLONDE: D("10")}
        # 2 L of the 10 are the list's: 16 pints fit, not 20.
        plan = plan_sales(20 * PINT_PRICE, offers, rooms, engine, already={99: 4})
        self.assertEqual(plan.counts, {40: 16})
        self.assertEqual(plan.poured, {40: {BLONDE: D("8.0")}})
        self.assertEqual(plan.used, {BLONDE: D("10.0")})
        self.assertEqual(plan.reason, GAPS_FULL)
        # With no amount, or with no offer at all: what the list adds.
        self.assertEqual(plan_sales(0, offers, rooms, engine, already={99: 4}).used, {BLONDE: D("2.0")})
        self.assertEqual(
            plan_sales(5000, [], rooms, engine, already={99: 4}),
            Plan({}, 0, 5000, NOTHING_TO_FILL, used={BLONDE: D("2.0")}),
        )

    def test_the_list_s_old_recipe_is_attributed_by_the_engine_not_by_its_terms(self):
        # MeasuredEffectTests' trap with the sunrise off the menu: three
        # sunrises of the list pour tequila by their terms, and the engine
        # moves three shots off the tequila for them - the list adds vodka.
        trap = MeasuredEffectTests()
        offers, rooms, engine = trap.trap()
        offers = [offer for offer in offers if offer.recipe_id != trap.SUNRISE]
        plan = plan_sales(0, offers, rooms, engine, already={trap.SUNRISE: 3})
        self.assertEqual(plan.used, {trap.VODKA: D(12), trap.JUICE: D(30)})
        self.assertNotIn(trap.TEQUILA, plan.used)

    def test_a_list_already_past_a_room_adds_nothing_more_to_it(self):
        # The rooms are worked out afresh at every amount and may have
        # shrunk since (a delivery corrected): the list's 30 pints poured
        # 15 L into a keg with 10 L to fill. No more pint, and `used` says
        # what the list adds as it is.
        offers, _rooms, engine = self.pint_and_shot()
        rooms = {BLONDE: D("10"), RHUM: D("400")}
        plan = plan_sales(2 * 370, offers, rooms, engine, already={40: 30})
        self.assertEqual(plan.counts, {43: 2})
        self.assertEqual(plan.reason, EXACT)
        self.assertEqual(plan.used, {BLONDE: D("15.0"), RHUM: D(8)})


class SevenEurosAgainTests(SimpleTestCase):
    """The owner's own example: « si je rentre 7 € et encore 7 € … que cela
    soit mis à jour pour combler les trous au fur et à mesure ».

    An invented menu: a half at 3,30 €, a shot at 3,70 €, a syrup and water
    at 2,30 €, a ti punch at 4,70 € (rum and a lemon wedge). 7,00 € is a
    half and a shot, or a syrup and a ti punch, and nothing else."""

    SHOT_PRICE, SYRUP_PRICE, PUNCH_PRICE = 370, 230, 470
    PRICES = (SHOT_PRICE, SYRUP_PRICE, PUNCH_PRICE)
    SEVEN = 700
    HALF, SHOT, SYRUP, PUNCH = 60, 61, 62, 63

    AMPLE = {BLONDE: D("50"), RHUM: D("1000"), SIROP: D("400"), CITRON: D("100")}
    # 8 halves, 15 servings of rum, 5 syrups, 4 lemon wedges.
    TIGHT = {BLONDE: D("2"), RHUM: D("60"), SIROP: D("10"), CITRON: D("4")}

    def bar(self, rooms):
        offers = [
            fixed_offer(self.HALF, HALF_PRICE, {BLONDE: "0.25"}, sold=20, name="Demi exemple"),
            fixed_offer(self.SHOT, self.SHOT_PRICE, {RHUM: "4"}, sold=15, name="Shot exemple"),
            fixed_offer(self.SYRUP, self.SYRUP_PRICE, {SIROP: "2"}, sold=8, name="Sirop à l'eau exemple"),
            fixed_offer(self.PUNCH, self.PUNCH_PRICE, {RHUM: "4", CITRON: "1"}, sold=6, name="Ti punch exemple"),
        ]
        return offers, dict(rooms), additive_engine(offers)

    def seven_euro_sets(self, offers):
        """Every set of sales whose prices make 7,00 €, by brute force (a
        few dozen candidates: no price is under 2,30 €)."""
        ranges = [range(self.SEVEN // offer.price_cents + 1) for offer in offers]
        return [
            {offer.recipe_id: count for offer, count in zip(offers, counts, strict=True) if count}
            for counts in itertools.product(*ranges)
            if sum(offer.price_cents * count for offer, count in zip(offers, counts, strict=True)) == self.SEVEN
        ]

    def test_seven_euros_is_made_two_ways(self):
        offers, _rooms, _engine = self.bar(self.AMPLE)
        sets = self.seven_euro_sets(offers)
        self.assertEqual(len(sets), 2)
        self.assertIn({self.HALF: 1, self.SHOT: 1}, sets)
        self.assertIn({self.SYRUP: 1, self.PUNCH: 1}, sets)

    def test_again_and_again_each_exact_each_filling_what_is_behind(self):
        offers, rooms, engine = self.bar(self.AMPLE)
        amounts = [self.SEVEN] * 12
        planned = plan_a_list(amounts, offers, rooms, engine)
        assert_list_is_consistent(self, planned, amounts, offers, rooms, engine)
        sets = self.seven_euro_sets(offers)
        for index, (plan, _after) in enumerate(planned):
            with self.subTest(entry=index):
                self.assertEqual(plan.reason, EXACT)
                self.assertIn(plan.counts, sets)
        # The second 7 € fills what the first left behind - not the same
        # glasses again, which is what 7 € planned alone proposes every time.
        self.assertNotEqual(planned[1][0].counts, planned[0][0].counts)
        self.assertEqual(plan_sales(self.SEVEN, offers, rooms, engine), planned[0][0])
        # Every recipe has its turn.
        self.assertEqual(set(planned[-1][1]), {self.HALF, self.SHOT, self.SYRUP, self.PUNCH})

    def test_on_tight_gaps_exact_while_it_fits_never_past_a_room_and_then_full(self):
        offers, rooms, engine = self.bar(self.TIGHT)
        amounts = [self.SEVEN] * 20
        planned = plan_a_list(amounts, offers, rooms, engine)
        assert_list_is_consistent(self, planned, amounts, offers, rooms, engine)
        sets = self.seven_euro_sets(offers)
        exact = 0
        for index, (plan, after) in enumerate(planned):
            before = {recipe: count - plan.counts.get(recipe, 0) for recipe, count in after.items()}
            fitting = [found for found in sets if fits(engine, rooms, with_list(before, found))]
            with self.subTest(entry=index, fitting=fitting):
                # Exact whenever some way of making 7,00 € still fits the gaps.
                self.assertEqual(plan.reason == EXACT, bool(fitting))
            exact += plan.reason == EXACT
        self.assertGreater(exact, 5)
        # The list ends, and it is true: one more of anything goes past a room.
        last, final = planned[-1]
        self.assertEqual(last.counts, {})
        self.assertEqual(last.reason, GAPS_FULL)
        for offer in offers:
            with self.subTest(recipe=offer.name):
                self.assertFalse(fits(engine, rooms, with_list(final, {offer.recipe_id: 1})))


class ListInPiecesTests(SimpleTestCase):
    """Priced alike, nothing but the ranking steers a split: a list ends
    where one plan of its whole amount does, however it was cut up."""

    def test_a_list_ends_where_one_plan_of_its_sum_does(self):
        # Rooms of 500 and 50, a serving of 25 (ProportionalTests): 22 sales
        # are 20 and 2.
        offers, rooms, engine = ProportionalTests().two_kegs("50")
        whole = plan_sales(22 * 530, offers, rooms, engine)
        self.assertEqual(whole.counts, {20: 20, 21: 2})
        for pieces in ([11, 11], [5, 6, 11], [3, 19], [1] * 22):
            with self.subTest(pieces=pieces):
                amounts = [sales * 530 for sales in pieces]
                planned = plan_a_list(amounts, offers, rooms, engine)
                assert_list_is_consistent(self, planned, amounts, offers, rooms, engine)
                self.assertEqual(planned[-1][1], whole.counts)
                self.assertEqual(planned[-1][0].used, whole.used)


class ListOnTheStockPageEngineTests(SimpleTestCase):
    """ReviveTests' rums on the stock page's own engine: a shot set aside on
    the amber rum comes back on the white one, entry after entry too."""

    def test_a_list_never_passes_a_room_and_ends_full(self):
        revive = ReviveTests()
        consumption, rooms = revive.engine()
        offers = revive.offers()
        amounts = [900, 1850, 740, 1850, 900] * 6
        planned = plan_a_list(amounts, offers, rooms, consumption, revive.UNIT_COSTS)
        assert_list_is_consistent(self, planned, amounts, offers, rooms, consumption)
        last, final = planned[-1]
        self.assertEqual(last.counts, {})
        self.assertEqual(last.reason, GAPS_FULL)
        self.assertEqual(last.used[revive.AMBRE], rooms[revive.AMBRE])
        for offer in offers:
            with self.subTest(recipe=offer.name):
                self.assertFalse(fits(consumption, rooms, with_list(final, {offer.recipe_id: 1})))


class RandomListTests(SimpleTestCase):
    """Whatever the instance, a list never goes past a room in total, and
    each entry's `used` is the whole list as the engine counts it."""

    def amounts(self, rng):
        return [
            rng.choice([700, 1230, 1850, rng.randint(1, 3000), rng.randint(0, 300) * 10])
            for _ in range(rng.randint(2, 6))
        ]

    def test_forty_random_lists(self):
        rng = random.Random(7001)
        maker = NeverOverfillTests()
        for case in range(40):
            _amount, offers, rooms, engine = maker.random_additive_case(rng)
            amounts = self.amounts(rng)
            with self.subTest(case=case, amounts=amounts):
                planned = plan_a_list(amounts, offers, rooms, engine)
                assert_list_is_consistent(self, planned, amounts, offers, rooms, engine)

    def test_twenty_random_lists_with_an_ou(self):
        rng = random.Random(7002)
        maker = NeverOverfillTests()
        for case in range(20):
            _amount, offers, rooms, engine = maker.random_choice_case(rng)
            amounts = self.amounts(rng)
            with self.subTest(case=case, amounts=amounts, rooms=rooms):
                planned = plan_a_list(amounts, offers, rooms, engine)
                assert_list_is_consistent(self, planned, amounts, offers, rooms, engine)


class ListDeterminismTests(SimpleTestCase):
    """Same list, same plans - whatever order the offers come in."""

    AMOUNTS = [700, 1230, 4560, 890, 2000]

    def counts_in_order(self, planned):
        return [list(plan.counts.items()) for plan, _after in planned]

    def test_the_same_list_twice(self):
        offers, rooms = DeterministicTests().instance()
        first = plan_a_list(self.AMOUNTS, offers, rooms, additive_engine(offers))
        second = plan_a_list(self.AMOUNTS, offers, rooms, additive_engine(offers))
        self.assertEqual(first, second)
        self.assertEqual(self.counts_in_order(first), self.counts_in_order(second))

    def test_the_offers_order_does_not_matter(self):
        offers, rooms = DeterministicTests().instance()
        reference = plan_a_list(self.AMOUNTS, offers, rooms, additive_engine(offers))
        rng = random.Random(31)
        orders = [offers[::-1]] + [rng.sample(offers, len(offers)) for _ in range(4)]
        for index, order in enumerate(orders):
            with self.subTest(order=index):
                planned = plan_a_list(self.AMOUNTS, order, rooms, additive_engine(order))
                self.assertEqual(planned, reference)
                self.assertEqual(self.counts_in_order(planned), self.counts_in_order(reference))

    def test_nor_with_an_ou_or_a_revive(self):
        trap = MeasuredEffectTests()
        offers, rooms, engine = trap.trap()
        amounts = [1890, 630, 6300, 1260]
        reference = plan_a_list(amounts, offers, rooms, engine)
        self.assertEqual(plan_a_list(amounts, offers[::-1], rooms, engine), reference)

        revive = ReviveTests()
        consumption, rooms = revive.engine()
        offers = revive.offers()
        amounts = [900, 1850, 740, 4000]
        reference = plan_a_list(amounts, offers, rooms, consumption, revive.UNIT_COSTS)
        self.assertEqual(plan_a_list(amounts, offers[::-1], rooms, consumption, revive.UNIT_COSTS), reference)


# ---------------------------------------------------------------------------
# Articles the owner left out: plan_sales(ignored=...), first_sales(ignored=...)
# ---------------------------------------------------------------------------


def without(offers, article) -> list[Offer]:
    """`offers` as if none of them poured `article`: an option pouring it
    alone goes, and so does a term left with no option. Leaving an article
    out must plan exactly like this - the sales, the money, the reason - and
    differ only in `used` (and `poured`), which still say what the sales add
    to it."""
    stripped = []
    for offer in offers:
        terms = []
        for options in offer.terms:
            kept = []
            for option in options:
                rest = {other: amount for other, amount in option.items() if other != article}
                if rest:
                    kept.append(rest)
            if kept:
                terms.append(tuple(kept))
        stripped.append(
            Offer(offer.recipe_id, offer.name, offer.price_cents, offer.sold, tuple(terms), offer.independent)
        )
    return stripped


def within_rooms_but(case, plan, rooms, ignored):
    """Every article but the ignored ones stays within its room."""
    for article in (set(rooms) | set(plan.used)) - set(ignored):
        case.assertLessEqual(
            plan.used.get(article, ZERO),
            limit(rooms, article),
            f"article {article}: added {plan.used.get(article)} past its room {rooms.get(article)}",
        )


class IgnoredArticleTests(SimpleTestCase):
    """`ignored`: the articles the owner left out of « Combler les écarts »
    (`GapExclusion`, handed over by `gaps.gaps_since`). An ignored article is
    no gap to fill - nothing ranks a sale on it, and an offer filling nothing
    else is never proposed - and no limit: a sale may take it past its room
    (a herb « vendu plus qu'acheté » no longer holds its cocktail back).
    `used` still says what the plan adds to it, as the engine attributes it:
    the sales pour it all the same."""

    DEMI, SHOT, TI_PUNCH, GIN_TONIC = 10, 43, 50, 51

    def demi_and_shot(self):
        # « Demi exemple » at 3,30 €, « Shot exemple » at 3,70 €.
        offers = [
            fixed_offer(self.DEMI, HALF_PRICE, {BLONDE: "0.25"}, sold=20, name="Demi exemple"),
            fixed_offer(self.SHOT, 370, {RHUM: "4"}, sold=5, name="Shot exemple"),
        ]
        return offers, {BLONDE: D("30"), RHUM: D("400")}, additive_engine(offers)

    def ti_punch(self, lemon_room):
        # « Ti punch exemple » at 8,90 €: 4 cl of rum and a lemon wedge.
        offers = [fixed_offer(self.TI_PUNCH, 890, {RHUM: "4", CITRON: "1"}, sold=6, name="Ti punch exemple")]
        return offers, {RHUM: D("40"), CITRON: D(lemon_room)}, additive_engine(offers)

    def test_an_offer_filling_only_an_ignored_article_is_never_proposed(self):
        offers, rooms, engine = self.demi_and_shot()
        # The control: 13,20 € is four halves, and no other mix of the prices.
        control = plan_sales(4 * HALF_PRICE, offers, rooms, engine)
        self.assertEqual((control.counts, control.reason), ({self.DEMI: 4}, EXACT))
        # The keg left out, a half fills no gap: three shots, and the 2,10 €
        # left no shot makes.
        plan = plan_sales(4 * HALF_PRICE, offers, rooms, engine, ignored={BLONDE})
        self.assertEqual(plan.counts, {self.SHOT: 3})
        self.assertEqual((plan.total_cents, plan.remainder_cents), (3 * 370, 4 * HALF_PRICE - 3 * 370))
        self.assertEqual(plan.reason, NO_COMBINATION)
        self.assertEqual(plan.used, {RHUM: D(12)})
        for amount in (HALF_PRICE, 2 * HALF_PRICE, 3700, 9990, 33000):
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, engine, ignored={BLONDE})
                self.assertNotIn(self.DEMI, plan.counts)
                self.assertNotIn(BLONDE, plan.used)

    def test_with_nothing_else_to_fill_it_is_nothing_to_fill(self):
        offers, rooms, engine = self.demi_and_shot()
        offers = offers[:1]
        self.assertEqual(plan_sales(5000, offers, rooms, engine, ignored={BLONDE}), Plan({}, 0, 5000, NOTHING_TO_FILL))
        # Under its price too: the half is no offer at all, not one too dear.
        self.assertEqual(plan_sales(HALF_PRICE - 1, offers, rooms, engine).reason, BELOW_CHEAPEST)
        self.assertEqual(
            plan_sales(HALF_PRICE - 1, offers, rooms, engine, ignored={BLONDE}),
            Plan({}, 0, HALF_PRICE - 1, NOTHING_TO_FILL),
        )

    def test_one_sale_past_an_ignored_article_s_room_blocks_nothing(self):
        # The lemon sold past what was bought (« vendu plus qu'acheté »): one
        # ti punch takes it further. Counted, it blocks the ti punch.
        offers, rooms, engine = self.ti_punch("-3")
        self.assertEqual(blocked(offers, rooms, engine), {self.TI_PUNCH: {CITRON}})
        self.assertEqual(first_sales(offers, rooms, engine)[self.TI_PUNCH], ({RHUM: D(4), CITRON: D(1)}, {CITRON}))
        # Left out, it blocks nothing - and what one sale adds is still said,
        # the lemon included.
        self.assertEqual(blocked(offers, rooms, engine, ignored={CITRON}), {})
        self.assertEqual(
            first_sales(offers, rooms, engine, ignored={CITRON}),
            {self.TI_PUNCH: ({RHUM: D(4), CITRON: D(1)}, set())},
        )
        # The rum is still a limit: only what is left out stops being one.
        rooms[RHUM] = D("2")
        self.assertEqual(blocked(offers, rooms, engine, ignored={CITRON}), {self.TI_PUNCH: {RHUM}})

    def test_a_plan_goes_past_an_ignored_article_s_room_and_used_says_how_far(self):
        offers, rooms, engine = self.ti_punch("2")
        # The control: two lemon wedges to fill, two ti punches.
        control = plan_sales(20 * 890, offers, rooms, engine)
        self.assertEqual((control.counts, control.reason), ({self.TI_PUNCH: 2}, GAPS_FULL))
        # The lemon left out: the rum's 40 cl is the only limit - ten.
        plan = plan_sales(20 * 890, offers, rooms, engine, ignored={CITRON})
        self.assertEqual(plan.counts, {self.TI_PUNCH: 10})
        self.assertEqual((plan.total_cents, plan.remainder_cents, plan.reason), (10 * 890, 10 * 890, GAPS_FULL))
        self.assertEqual(plan.used, {RHUM: D(40), CITRON: D(10)})
        self.assertEqual(plan.poured, {self.TI_PUNCH: {RHUM: D(40), CITRON: D(10)}})
        self.assertGreater(plan.used[CITRON], rooms[CITRON])
        within_rooms_but(self, plan, rooms, {CITRON})

    def test_the_run_neither_targets_nor_limits_an_ignored_article(self):
        offers, rooms, engine = self.ti_punch("2")
        run = _Run(offers, rooms, engine, ignored=[CITRON])
        self.assertEqual(run.ignored, frozenset({CITRON}))
        self.assertEqual(run.targets, {RHUM})
        self.assertEqual(run.past({RHUM: D(4), CITRON: D(5)}), set())
        self.assertEqual(run.past({RHUM: D(44), CITRON: D(5)}), {RHUM})
        self.assertEqual(run.past_with({self.TI_PUNCH: 11})[1], {RHUM})
        # Nothing left out: the lemon is a target and a limit like the rum.
        counted = _Run(offers, rooms, engine)
        self.assertEqual(counted.ignored, frozenset())
        self.assertEqual(counted.targets, {RHUM, CITRON})
        self.assertEqual(counted.past({RHUM: D(4), CITRON: D(5)}), {CITRON})
        self.assertEqual(counted.past_with({self.TI_PUNCH: 11})[1], {RHUM, CITRON})

    def test_an_ignored_article_neither_holds_a_sale_back_nor_drives_it(self):
        # A ti punch and a gin tonic, priced and sold alike; the lemon has 3
        # wedges to fill. Counted, the lemon - far behind the gin after one
        # wedge - holds the ti punch back: six sales all go to the gin
        # tonic. Left out, the rum and the gin are all there is, and the two
        # take turns.
        offers = [
            fixed_offer(self.TI_PUNCH, 890, {RHUM: "4", CITRON: "1"}, sold=5, name="Ti punch exemple"),
            fixed_offer(self.GIN_TONIC, 890, {GIN: "4"}, sold=5, name="Gin tonic exemple"),
        ]
        rooms = {RHUM: D("400"), GIN: D("400"), CITRON: D("3")}
        engine = additive_engine(offers)
        self.assertEqual(plan_sales(6 * 890, offers, rooms, engine).counts, {self.GIN_TONIC: 6})
        plan = plan_sales(6 * 890, offers, rooms, engine, ignored={CITRON})
        self.assertEqual(plan.counts, {self.GIN_TONIC: 3, self.TI_PUNCH: 3})
        self.assertEqual(plan.used, {RHUM: D(12), GIN: D(12), CITRON: D(3)})
        self.assertEqual(plan.reason, EXACT)

    def test_leaving_an_article_out_plans_like_offers_that_never_poured_it(self):
        rng = random.Random(20261003)
        maker = NeverOverfillTests()
        changed = past_its_room = 0
        for case in range(40):
            amount, offers, rooms, engine = maker.random_additive_case(rng)
            article = rng.choice(sorted(rooms))
            others = without(offers, article)
            alone = plan_sales(amount, others, rooms, AdditiveEngine(others, engine.base))
            plan = plan_sales(amount, offers, rooms, engine, ignored={article})
            with self.subTest(case=case, amount=amount, article=article, rooms=rooms):
                # The same sales, chosen in the same order, for the same reason.
                self.assertEqual(list(plan.counts.items()), list(alone.counts.items()))
                self.assertEqual(
                    (plan.total_cents, plan.remainder_cents, plan.reason),
                    (alone.total_cents, alone.remainder_cents, alone.reason),
                )
                # What they add to the article left out, and to the others.
                pour = {offer.recipe_id: offer.fixed_pour().get(article, ZERO) for offer in offers}
                added = sum((pour[recipe] * count for recipe, count in plan.counts.items()), start=ZERO)
                self.assertEqual(plan.used.get(article, ZERO), added)
                self.assertEqual({other: value for other, value in plan.used.items() if other != article}, alone.used)
                within_rooms_but(self, plan, rooms, {article})
            changed += plan.counts != plan_sales(amount, offers, rooms, engine).counts
            past_its_room += plan.used.get(article, ZERO) > limit(rooms, article)
        # The instances prove something: leaving an article out changed some
        # plans, and some went past the room of what was left out.
        self.assertGreater(changed, 0)
        self.assertGreater(past_its_room, 0)

    def test_measured_a_choice_the_engine_books_on_an_ignored_article_fills_no_gap(self):
        # MeasuredEffectTests' trap with 2 cl of vodka left to fill. As the
        # engine books them, a shot pours the vodka and a sunrise pushes a
        # shot onto it: counted, the vodka blocks both.
        trap = MeasuredEffectTests()
        offers, rooms, engine = trap.trap(vodka_room="2")
        self.assertEqual(blocked(offers, rooms, engine), {trap.SHOT: {trap.VODKA}, trap.SUNRISE: {trap.VODKA}})
        control = plan_sales(20 * 630, offers, rooms, engine)
        self.assertEqual(set(control.counts), {trap.TI_PUNCH, trap.GIN_TONIC})
        # The vodka left out, neither is blocked. The sunrise fills the juice
        # and is proposed; the shot - whose terms reach the tequila, which
        # has room - pours only the vodka as the engine books it: never.
        self.assertEqual(blocked(offers, rooms, engine, ignored={trap.VODKA}), {})
        plan = plan_sales(20 * 630, offers, rooms, engine, ignored={trap.VODKA})
        self.assertEqual(plan.reason, EXACT)
        self.assertNotIn(trap.SHOT, plan.counts)
        self.assertGreater(plan.counts.get(trap.SUNRISE, 0), 0)
        self.assertEqual(plan.used[trap.VODKA], D(4) * plan.counts[trap.SUNRISE])
        self.assertGreater(plan.used[trap.VODKA], rooms[trap.VODKA])
        self.assertEqual(plan.used.get(trap.TEQUILA, ZERO), ZERO)
        within_rooms_but(self, plan, rooms, {trap.VODKA})


class IgnoredDeterminismTests(SimpleTestCase):
    """Same input, same plan, with articles left out too - and nothing left
    out is the plan it always was."""

    def test_nothing_left_out_is_the_plan_it_always_was(self):
        offers, rooms = DeterministicTests().instance()
        trap_offers, trap_rooms, trap_engine = MeasuredEffectTests().trap()
        revive = ReviveTests()
        consumption, revive_rooms = revive.engine()
        cases = [
            (12345, offers, rooms, additive_engine(offers), None),
            (15000, trap_offers, trap_rooms, trap_engine, None),
            (20000, revive.offers(), revive_rooms, consumption, revive.UNIT_COSTS),
        ]
        for index, (amount, case_offers, case_rooms, engine, values) in enumerate(cases):
            reference = plan_sales(amount, case_offers, case_rooms, engine, values)
            first = first_sales(case_offers, case_rooms, engine)
            for ignored in ((), [], set(), frozenset()):
                with self.subTest(case=index, ignored=ignored):
                    plan = plan_sales(amount, case_offers, case_rooms, engine, values, ignored=ignored)
                    self.assertEqual(plan, reference)
                    self.assertEqual(list(plan.counts.items()), list(reference.counts.items()))
                    self.assertEqual(first_sales(case_offers, case_rooms, engine, ignored), first)

    def test_leaving_out_an_article_no_offer_pours_changes_nothing(self):
        offers, rooms = DeterministicTests().instance()
        rooms = {**rooms, 99: D("5")}
        reference = plan_sales(12345, offers, rooms, additive_engine(offers))
        # In the rooms, or not even named there.
        for ignored in ({99}, {98}):
            with self.subTest(ignored=ignored):
                self.assertEqual(plan_sales(12345, offers, rooms, additive_engine(offers), ignored=ignored), reference)

    def test_any_collection_of_the_same_articles_is_the_same_plan(self):
        offers, rooms = DeterministicTests().instance()
        reference = plan_sales(12345, offers, rooms, additive_engine(offers), ignored=frozenset({CITRON, AMBREE}))
        # The test proves something: leaving them out is another plan.
        self.assertNotEqual(reference.counts, plan_sales(12345, offers, rooms, additive_engine(offers)).counts)
        for ignored in ([CITRON, AMBREE], (AMBREE, CITRON), {CITRON, AMBREE}, [CITRON, AMBREE, CITRON]):
            with self.subTest(ignored=ignored):
                plan = plan_sales(12345, offers, rooms, additive_engine(offers), ignored=ignored)
                self.assertEqual(plan, reference)
                self.assertEqual(list(plan.counts.items()), list(reference.counts.items()))

    def test_the_offers_order_does_not_matter(self):
        offers, rooms = DeterministicTests().instance()
        reference = plan_sales(12345, offers, rooms, additive_engine(offers), ignored={CITRON})
        rng = random.Random(19)
        orders = [offers[::-1]] + [rng.sample(offers, len(offers)) for _ in range(5)]
        for index, order in enumerate(orders):
            with self.subTest(order=index):
                plan = plan_sales(12345, order, rooms, additive_engine(order), ignored={CITRON})
                self.assertEqual(plan, reference)
                self.assertEqual(list(plan.counts.items()), list(reference.counts.items()))

    def test_nor_with_an_ou(self):
        trap = MeasuredEffectTests()
        offers, rooms, engine = trap.trap(vodka_room="2")
        reference = plan_sales(20 * 630, offers, rooms, engine, ignored={trap.VODKA})
        for index, order in enumerate([offers[::-1], offers[2:] + offers[:2]]):
            with self.subTest(order=index):
                self.assertEqual(plan_sales(20 * 630, order, rooms, engine, ignored={trap.VODKA}), reference)


class IgnoredWithAListTests(SimpleTestCase):
    """`ignored` with `already`: what a list's earlier sales poured into an
    article left out is counted in `used` and holds nothing back."""

    DEMI, TI_PUNCH, GIN_TONIC = 10, 50, 51

    def test_a_list_that_took_an_ignored_article_past_its_room_holds_nothing_back(self):
        offers = [fixed_offer(self.TI_PUNCH, 890, {RHUM: "4", CITRON: "1"}, sold=6, name="Ti punch exemple")]
        rooms = {RHUM: D("40"), CITRON: D("2")}
        engine = additive_engine(offers)
        # The control: the list's five ti punches took five wedges of two.
        control = plan_sales(3 * 890, offers, rooms, engine, already={self.TI_PUNCH: 5})
        self.assertEqual((control.counts, control.reason), ({}, GAPS_FULL))
        self.assertEqual(control.used, {RHUM: D(20), CITRON: D(5)})
        plan = plan_sales(3 * 890, offers, rooms, engine, already={self.TI_PUNCH: 5}, ignored={CITRON})
        self.assertEqual((plan.counts, plan.reason), ({self.TI_PUNCH: 3}, EXACT))
        # The new sales, and the list as a whole.
        self.assertEqual(plan.poured, {self.TI_PUNCH: {RHUM: D(12), CITRON: D(3)}})
        self.assertEqual(plan.used, {RHUM: D(32), CITRON: D(8)})

    def test_a_list_of_sales_filling_only_an_ignored_article_is_used_and_nothing_more(self):
        offers = [fixed_offer(self.DEMI, HALF_PRICE, {BLONDE: "0.25"}, sold=20, name="Demi exemple")]
        rooms = {BLONDE: D("30")}
        engine = additive_engine(offers)
        already = {self.DEMI: 4}
        self.assertEqual(
            plan_sales(0, offers, rooms, engine, already=already, ignored={BLONDE}),
            Plan({}, 0, 0, EXACT, used={BLONDE: D("1")}),
        )
        self.assertEqual(
            plan_sales(5000, offers, rooms, engine, already=already, ignored={BLONDE}),
            Plan({}, 0, 5000, NOTHING_TO_FILL, used={BLONDE: D("1")}),
        )

    def test_amount_after_amount_ends_where_one_plan_of_the_sum_does(self):
        offers = [
            fixed_offer(self.TI_PUNCH, 890, {RHUM: "4", CITRON: "1"}, sold=5, name="Ti punch exemple"),
            fixed_offer(self.GIN_TONIC, 890, {GIN: "4"}, sold=5, name="Gin tonic exemple"),
        ]
        rooms = {RHUM: D("40"), GIN: D("40"), CITRON: D("1")}
        engine = additive_engine(offers)
        whole = plan_sales(16 * 890, offers, rooms, engine, ignored={CITRON})
        self.assertEqual((whole.counts, whole.reason), ({self.TI_PUNCH: 8, self.GIN_TONIC: 8}, EXACT))
        self.assertEqual(whole.used, {RHUM: D(32), GIN: D(32), CITRON: D(8)})
        for pieces in ([8, 8], [3, 5, 8], [1] * 16):
            with self.subTest(pieces=pieces):
                already: dict[int, int] = {}
                plan = None
                for sales in pieces:
                    plan = plan_sales(sales * 890, offers, rooms, engine, already=already, ignored={CITRON})
                    self.assertEqual(plan.reason, EXACT)
                    self.assertEqual(sum(plan.counts.values()), sales)
                    already = with_list(already, plan.counts)
                    self.assertEqual(plan.used, engine_adds(engine, already))
                    within_rooms_but(self, plan, rooms, {CITRON})
                self.assertEqual(already, whole.counts)
                assert plan is not None
                self.assertEqual(plan.used, whole.used)


class RecordingEngine(AdditiveEngine):
    """An additive engine that keeps every count it was asked about, in
    `asked` - so a test can say which recipes the planner measured."""

    def __init__(self, offers, base=None):
        super().__init__(offers, base)
        self.asked: list[dict[int, int]] = []

    def __call__(self, extra):
        self.asked.append(dict(extra))
        return super().__call__(extra)

    def measured(self, recipe_id) -> bool:
        """Whether a count of `recipe_id` was ever asked about."""
        return any(extra.get(recipe_id) for extra in self.asked)


class OfferReachingNoGapTests(SimpleTestCase):
    """`plan_sales` works on the offers that reach a gap only (`run.offers =
    live`). An offer pouring nothing but ignored articles is never proposed,
    so its price is no way to make the rest of an amount: counted among the
    prices, it made a rest look makeable that no proposed sale then made -
    the ranking's first sale was kept, and the plan fell short with
    NO_COMBINATION where one without that offer was exact. Nor is it ever
    measured or revived: the targets never grow, it would never fill one.

    « Pinte exemple » (A) at 7,10 € pours 1 of the blonde (room 100),
    « Demi ambrée exemple » (C) at 5,10 € 1 of the amber (room 10), « Sirop
    à l'eau exemple » (X) at 3,10 € the syrup alone (room 50), left out.
    10,20 € is A + X, or C + C - and A ranks first, its gap the furthest
    behind."""

    A, C, X = 61, 62, 63
    AMOUNT = 1020

    def offers(self, independent=True) -> list[Offer]:
        return [
            fixed_offer(self.A, 710, {BLONDE: "1"}, sold=10, name="Pinte exemple", independent=independent),
            fixed_offer(self.C, 510, {AMBREE: "1"}, sold=10, name="Demi ambrée exemple", independent=independent),
            fixed_offer(self.X, 310, {SIROP: "1"}, sold=10, name="Sirop à l'eau exemple", independent=independent),
        ]

    def rooms(self, blonde="100", amber="10") -> dict[int, Decimal]:
        return {BLONDE: D(blonde), AMBREE: D(amber), SIROP: D("50")}

    def test_the_amount_is_two_halves_of_amber_exactly(self):
        offers, rooms = self.offers(), self.rooms()
        plan = plan_sales(self.AMOUNT, offers, rooms, additive_engine(offers), ignored={SIROP})
        self.assertEqual(plan, Plan({self.C: 2}, 1020, 0, EXACT, used={AMBREE: D(2)}, poured={self.C: {AMBREE: D(2)}}))
        # Exactly the plan of a menu that never had the syrup and water.
        others = offers[:2]
        self.assertEqual(plan_sales(self.AMOUNT, others, rooms, additive_engine(others)), plan)
        # Not the ranking's doing: ranked alone, the pint comes first (the
        # blonde's gap the furthest behind) - it is the price reckoning that
        # turns it down, no proposed sale making the 3,10 € it would leave.
        run = _Run(offers, rooms, additive_engine(offers), ignored={SIROP})
        pint, half, syrup = offers
        self.assertLess(run.rank(pint), run.rank(half))
        self.assertIsNone(run.rank(syrup))

    def test_the_syrup_counted_its_price_does_make_the_rest(self):
        # The control: a gap of its own, the syrup and water is proposed, and
        # then 10,20 € is the pint and it.
        offers, rooms = self.offers(), self.rooms()
        plan = plan_sales(self.AMOUNT, offers, rooms, additive_engine(offers))
        self.assertEqual((plan.counts, plan.total_cents, plan.reason), ({self.A: 1, self.X: 1}, 1020, EXACT))

    def test_other_amounts_plan_as_if_it_were_not_offered(self):
        offers, rooms = self.offers(), self.rooms()
        others = offers[:2]
        # 8,20 € was the demi alone, short by the syrup's 3,10 € that nothing
        # then proposed: now the pint, short by 1,10 € - no mix of 7,10 € and
        # 5,10 € comes closer.
        plan = plan_sales(820, offers, rooms, additive_engine(offers), ignored={SIROP})
        self.assertEqual((plan.counts, plan.total_cents, plan.reason), ({self.A: 1}, 710, NO_COMBINATION))
        # Under the cheapest offer that fills a gap: nothing, whatever its own
        # price.
        self.assertEqual(
            plan_sales(310, offers, rooms, additive_engine(offers), ignored={SIROP}),
            Plan({}, 0, 310, BELOW_CHEAPEST),
        )
        for amount in range(0, 6001, 30):
            with self.subTest(amount=amount):
                plan = plan_sales(amount, offers, rooms, additive_engine(offers), ignored={SIROP})
                self.assertEqual(plan, plan_sales(amount, others, rooms, additive_engine(others)))
                self.assertNotIn(self.X, plan.counts)

    def test_it_is_never_measured_nor_revived(self):
        """No offer is independent here, so every other one is measured and,
        set aside, measured again by `_Run.revive` - the syrup and water,
        never. Two pints and two demis fill the rooms; then revive runs."""
        offers, rooms = self.offers(independent=False), self.rooms(blonde="2", amber="2")
        seen: list[set[int]] = []
        revive = _Run.revive

        def spy(run):
            seen.append({offer.recipe_id for offer in run.offers})
            return revive(run)

        engine = RecordingEngine(offers)
        with mock.patch.object(_Run, "revive", spy):
            plan = plan_sales(5000, offers, rooms, engine, ignored={SIROP})
        self.assertEqual((plan.counts, plan.total_cents, plan.reason), ({self.A: 2, self.C: 2}, 2440, GAPS_FULL))
        self.assertTrue(seen, "revive never ran: the test proves nothing")
        self.assertTrue(all(self.X not in offered for offered in seen), seen)
        self.assertTrue(engine.measured(self.A))
        self.assertFalse(engine.measured(self.X), engine.asked)
        others = offers[:2]
        self.assertEqual(plan_sales(5000, others, rooms, additive_engine(others)), plan)
        # The control: the syrup counted, the recorder sees it measured.
        counted = RecordingEngine(offers)
        plan_sales(5000, offers, rooms, counted)
        self.assertTrue(counted.measured(self.X))

    def test_a_list_s_earlier_sales_of_it_still_count_in_used(self):
        """Two syrups and water proposed before the syrup was left out: they
        stay the list's, and what they pour is said - nothing more is
        proposed of it."""
        offers, rooms = self.offers(independent=False), self.rooms()
        plan = plan_sales(self.AMOUNT, offers, rooms, additive_engine(offers), already={self.X: 2}, ignored={SIROP})
        self.assertEqual((plan.counts, plan.total_cents, plan.reason), ({self.C: 2}, 1020, EXACT))
        self.assertEqual(plan.used, {SIROP: D(2), AMBREE: D(2)})
        self.assertEqual(plan.poured, {self.C: {AMBREE: D(2)}})
