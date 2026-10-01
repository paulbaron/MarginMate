"""« Combler les écarts »: which recipes to ring up, for an amount typed in,
so that every stock gap since a count shrinks by about the same share.

Pure: no model, no query, no request. `inventory/gaps.py` hands it what the
database says; this decides.

**What a gap is here** is decided before this module: per article, what is
left to fill once its loss allowance is taken off - its `room` (« À
combler »). A room can be negative (sold more than was ever there). The one
promise kept below is that **the plan never adds more to an article than its
room** - and nothing at all to an article whose room is not positive -
measured the way the stock page will measure it once the sales are rung up.
The articles the owner left out (`ignored`) are the exception: no gap to
fill, no room to keep to.

**Measured by the engine itself, not by a model of it.** The till records a
recipe, never which side of an « OU » was poured: once imported, a choice is
attributed by `variance.allocate_choices`, priciest option first, and a fixed
ingredient pushes a choice off a bottle it was attributed to and onto
another. So what one more sale of a recipe ADDS - its effect - is asked of
`consumption`, the stock page's own attribution with the plan's sales added;
a sale is kept only if no article (left out ones aside) goes past its room. Ranked on a model
instead (« this cocktail pours that spirit »), a cocktail with a fixed spirit
kept coming back: each one pushed a « shot au choix » off that spirit onto
another one the ranking never saw, which ended far past every other gap. The
engine is asked lazily: an effect measured a few sales ago ranks the recipe
until it comes out on top, and is measured again then (`_Run.next_sale`). A
recipe whose articles no « OU » can reach adds exactly its own terms and is
never asked about (`Offer.independent`). For the same reason a recipe set
aside - its next sale would go past a room, or fill no gap - is measured
again once the plan has moved on (`_Run.revive`): a fixed pour taking a
bottle to its cap sends the next « au choix » onto another bottle with room.

**Proportional, the Webster way.** The owner's rule: « chaque écart est
réduit dans la même proportion : un gros écart reçoit plus de ventes, mais
tous en reçoivent ». An article's level is (added + half its usual serving) /
room - the midpoint of the divisor method that apportions seats in proportion
to votes, so a gap smaller than half a serving's share waits for a bigger
amount rather than getting a whole sale it does not deserve; its usual
serving is what its most-sold recipe pours, so a pint and a half of the same
keg rank alike. A sale's level is the levels of the gaps it fills averaged by
what it pours of each IN VALUE (`values`, the cost per unit), and each sale
goes to the recipe whose level is the lowest. Weighted by value, a garnish
worth a few centimes neither drives a cocktail (keyed on its least advanced
article, a garnish far behind sells the spirit past every other gap) nor
holds one back for good (keyed on its most advanced, a cocktail is never
chosen while a lemonade the best-sellers also pour keeps ahead, and its own
syrup is never filled). Recipes tie when they fill the same gaps alike (pint or
half, the same spirit neat or in a long drink); the tie goes to the recipe
furthest behind its share of what was really sold over the menu's window -
since the count, or the months the owner chose (Webster again: (planned + ½)
/ (sold + 1)), so the list reads like the bar's own orders. Then name, then id: the same input always gives the same list.

**The total is the amount, to the cent, whenever the prices can make it.**
Every sale is chosen among those that leave a rest the prices of the recipes
still proposable can make up - a coin-change reachability kept as a bitset
on a Python int (`_Makeable`): ranked freely, a pint at 37,00 € taken first
left 87,00 € that no mix of 37,00 € pints and 31,00 € halves makes, where
four halves made 124,00 €. When a recipe drops out (a gap full), what can be
made is worked out again, and if the rest can no longer be made the plan aims
at the closest total below that can. Rooms are not part of that reckoning, so
once they bind the total is the closest the ranking finds, not a proven best;
`reason` says why a plan is short.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from math import gcd

ZERO = Decimal("0")
TWO = Decimal("2")

# Why a plan's total is not the amount typed.
EXACT = "exact"
BELOW_CHEAPEST = "below_cheapest"  # the amount is under the cheapest recipe left to propose
GAPS_FULL = "gaps_full"  # no recipe can take one more sale without going past a gap
NO_COMBINATION = "no_combination"  # recipes still fit, but no combination of their prices lands on the rest
NOTHING_TO_FILL = "nothing_to_fill"  # no recipe can be proposed at all

#: {recipe_id: servings added} -> {article: everything the window's sales,
#: real and added, consume of it}, as the stock page attributes them.
Consumption = Callable[[dict[int, int]], dict[int, Decimal]]


@dataclass(frozen=True)
class Offer:
    """A recipe that may be proposed.

    `terms` are its usage terms (variance.recipe_usage_terms), per sale, each
    a tuple of options {article: amount}; a choice's options in the order the
    engine fills them (variance.order_options), options that pour nothing
    left out. `sold` is what the till sold of it over the menu's window (since
    the count, or the months chosen) - the mix a tie follows, 0 or below for
    a recipe whose sales there were refunded. `independent` says that it has no choice and that no « OU »
    of any recipe can reach an article it pours, so what it adds is exactly
    its own terms.
    """

    recipe_id: int
    name: str
    price_cents: int
    sold: Decimal | int
    terms: tuple[tuple[dict[int, Decimal], ...], ...]
    independent: bool = False

    def reach(self) -> set[int]:
        """Every article any of its options can pour."""
        return {
            article for options in self.terms for option in options for article, amount in option.items() if amount > 0
        }

    def fixed_pour(self) -> dict[int, Decimal]:
        """What one sale pours when nothing is a choice: its terms added up
        (the first option of each, should a choice slip through)."""
        poured: dict[int, Decimal] = {}
        for options in self.terms:
            for article, amount in options[0].items():
                poured[article] = poured.get(article, ZERO) + amount
        return poured


@dataclass
class Plan:
    counts: dict[int, int]
    total_cents: int
    remainder_cents: int
    reason: str
    # What the plan adds to each article, as the engine attributes it - can
    # be negative where a fixed ingredient pushed a choice onto another bottle.
    used: dict[int, Decimal] = field(default_factory=dict)
    # Per recipe, what its sales added to each article (to say which gaps a
    # line fills).
    poured: dict[int, dict[int, Decimal]] = field(default_factory=dict)


def limit(rooms: dict[int, Decimal], article: int) -> Decimal:
    """The most a plan may add to an article: its room, or nothing."""
    return max(ZERO, rooms.get(article, ZERO))


def usual_servings(offers: list[Offer], targets: set[int]) -> dict[int, Decimal]:
    """{article: what one sale usually pours of it} - the amount the most-sold
    offer able to pour it pours, first option first."""
    servings: dict[int, Decimal] = {}
    for offer in sorted(offers, key=lambda offer: (-offer.sold, offer.name, offer.recipe_id)):
        for options in offer.terms:
            for option in options:
                for article, amount in option.items():
                    if article in targets and amount > 0 and article not in servings:
                        servings[article] = amount
    return servings


def _added(totals: dict[int, Decimal], extra: dict[int, Decimal]) -> dict[int, Decimal]:
    return {article: totals.get(article, ZERO) + extra.get(article, ZERO) for article in set(totals) | set(extra)}


class _Run:
    """One plan being built: the sales so far, what everything consumes with
    them (`total`, as the engine counts it), and what one more sale of each
    recipe adds (`effects`, measured at some `version` of the plan)."""

    def __init__(
        self,
        offers: list[Offer],
        rooms: dict[int, Decimal],
        consumption: Consumption,
        values: dict[int, Decimal] | None = None,
        already: dict[int, int] | None = None,
        ignored=(),
    ):
        self.offers = offers
        self.rooms = rooms
        self.consumption = consumption
        self.values = values or {}
        # Articles left out by the owner: no gap to fill, no limit on a sale.
        self.ignored = frozenset(ignored)
        self.base = consumption({})
        # Sales proposed before (the list's earlier entries) count as added
        # already: what they fill is no longer behind.
        self.already = {recipe_id: count for recipe_id, count in (already or {}).items() if count}
        self.total = consumption(dict(self.already)) if self.already else dict(self.base)
        self.targets = {
            article
            for offer in offers
            for article in offer.reach()
            if rooms.get(article, ZERO) > 0 and article not in self.ignored
        }
        self.servings = usual_servings(offers, self.targets)
        self.price = {offer.recipe_id: offer.price_cents for offer in offers}
        self.counts: dict[int, int] = dict(self.already)
        self.spent = 0
        self.dead: set[int] = set()
        # Offers whose freshly measured sale fills no gap any more.
        self.idle: set[int] = set()
        # recipe_id -> the version of the plan it was set aside at (dead or idle).
        self.set_aside: dict[int, int] = {}
        self.version = 0
        # recipe_id -> (version measured at, effect, totals after that sale)
        self.effects: dict[int, tuple[int, dict[int, Decimal], dict[int, Decimal]]] = {}
        self.poured: dict[int, dict[int, Decimal]] = {}

    def copy(self) -> _Run:
        twin = object.__new__(_Run)
        twin.__dict__.update(self.__dict__)
        twin.total = dict(self.total)
        twin.counts = dict(self.counts)
        twin.dead = set(self.dead)
        twin.idle = set(self.idle)
        twin.set_aside = dict(self.set_aside)
        twin.effects = dict(self.effects)
        twin.poured = dict(self.poured)
        return twin

    def added(self, article: int) -> Decimal:
        """What the plan so far adds to an article."""
        return self.total.get(article, ZERO) - self.base.get(article, ZERO)

    def added_all(self) -> dict[int, Decimal]:
        """What the plan so far adds, per article it changes."""
        return {
            article: self.total.get(article, ZERO) - self.base.get(article, ZERO)
            for article in set(self.total) | set(self.base)
            if self.total.get(article, ZERO) != self.base.get(article, ZERO)
        }

    def measure(self, offer: Offer) -> dict[int, Decimal]:
        """Ask the engine what one more sale of `offer` adds, now."""
        trial = dict(self.counts)
        trial[offer.recipe_id] = trial.get(offer.recipe_id, 0) + 1
        after = self.consumption(trial)
        effect = {
            article: after.get(article, ZERO) - self.total.get(article, ZERO)
            for article in set(after) | set(self.total)
            if after.get(article, ZERO) != self.total.get(article, ZERO)
        }
        self.effects[offer.recipe_id] = (self.version, effect, after)
        return effect

    def effect(self, offer: Offer) -> dict[int, Decimal]:
        if offer.independent:
            return offer.fixed_pour()
        known = self.effects.get(offer.recipe_id)
        return known[1] if known is not None else self.measure(offer)

    def fresh(self, offer: Offer) -> bool:
        known = self.effects.get(offer.recipe_id)
        return offer.independent or (known is not None and known[0] == self.version)

    def past(self, effect: dict[int, Decimal]) -> set[int]:
        """The articles `effect` would take past their room (an ignored one
        has none to go past)."""
        return {
            article
            for article, amount in effect.items()
            if amount > 0 and article not in self.ignored and self.added(article) + amount > limit(self.rooms, article)
        }

    def level(self, effect: dict[int, Decimal]) -> Decimal | None:
        """How advanced the gaps a sale fills are, each at the midpoint of its
        usual serving, averaged over what the sale pours of each in value -
        so a garnish worth a few centimes neither holds a cocktail back nor
        drives it, and the spirit does. Articles with no known cost count
        only when none has one (then the most advanced decides). None when
        the sale fills no gap at all."""
        weighted, weights, worst = ZERO, ZERO, None
        for article, amount in effect.items():
            if amount <= 0 or article not in self.targets:
                continue
            level = (self.added(article) + self.servings[article] / TWO) / self.rooms[article]
            worst = level if worst is None or level > worst else worst
            weight = amount * self.values.get(article, ZERO)
            if weight > 0:
                weighted += weight * level
                weights += weight
        if worst is None:
            return None
        return weighted / weights if weights > 0 else worst

    def rank(self, offer: Offer):
        """The order the next sale is chosen in, smallest first; None when the
        offer fills no gap."""
        level = self.level(self.effect(offer))
        if level is None:
            return None
        # Refunds can net a recipe's sales below zero: it then weighs like one never sold.
        behind = Fraction(2 * self.counts.get(offer.recipe_id, 0) + 1, 2) / (Fraction(max(offer.sold, 0)) + 1)
        return (level, behind, offer.name, offer.recipe_id)

    def accept(self, offer: Offer, effect: dict[int, Decimal]) -> None:
        known = self.effects.get(offer.recipe_id)
        if offer.independent or known is None:
            self.total = _added(self.total, effect)
        else:
            self.total = dict(known[2])
        self.counts[offer.recipe_id] = self.counts.get(offer.recipe_id, 0) + 1
        self.spent += offer.price_cents
        self.poured[offer.recipe_id] = _added(self.poured.get(offer.recipe_id, {}), effect)
        self.version += 1

    def next_sale(self, allowed: Callable[[Offer], bool] | None = None) -> Offer | None:
        """Add the best sale `allowed` lets through (any when None). Each pass
        either measures a stale effect again, sets an offer aside (it would go
        past a room, or no longer fills any gap - until `revive`), or adds a
        sale - so it ends."""
        while True:
            best = None
            for offer in self.offers:
                if offer.recipe_id in self.dead or offer.recipe_id in self.idle:
                    continue
                rank = self.rank(offer)
                if rank is None:
                    if not self.fresh(offer):
                        self.measure(offer)
                        rank = self.rank(offer)
                    if rank is None:
                        self.idle.add(offer.recipe_id)
                        self.set_aside[offer.recipe_id] = self.version
                        continue
                if allowed is not None and not allowed(offer):
                    continue
                if best is None or rank < best[0]:
                    best = (rank, offer)
            if best is None:
                return None
            offer = best[1]
            if not self.fresh(offer):
                self.measure(offer)
                continue
            effect = self.effect(offer)
            if self.past(effect):
                self.dead.add(offer.recipe_id)
                self.set_aside[offer.recipe_id] = self.version
                continue
            self.accept(offer, effect)
            return offer

    def revive(self) -> int:
        """Measure again every offer set aside before the last sale, and take
        back those that fit and fill a gap now. A choice is booked on the
        dearest option with room, so a sale elsewhere - a fixed pour taking a
        bottle to its cap - can send the next one onto another bottle that
        has plenty: an offer set aside is not out for good. Independent
        offers are: their pour never changes and their rooms only shrink."""
        revived = 0
        for offer in self.offers:
            stamp = self.set_aside.get(offer.recipe_id)
            if offer.independent or stamp is None or stamp >= self.version:
                continue
            effect = self.measure(offer)
            self.set_aside[offer.recipe_id] = self.version
            if self.level(effect) is not None and not self.past(effect):
                self.dead.discard(offer.recipe_id)
                self.idle.discard(offer.recipe_id)
                del self.set_aside[offer.recipe_id]
                revived += 1
        return revived

    def alive(self) -> list[Offer]:
        """The offers that may still take a sale, as far as is known."""
        return [offer for offer in self.offers if offer.recipe_id not in self.dead and offer.recipe_id not in self.idle]

    def past_with(self, counts: dict[int, int]) -> tuple[dict[int, Decimal], set[int]]:
        """What `counts` (the whole plan) consume, and the articles they take
        past their room."""
        after = self.consumption(counts)
        past = {
            article
            for article in set(after) | set(self.base)
            if article not in self.ignored
            and after.get(article, ZERO) - self.base.get(article, ZERO) > limit(self.rooms, article)
        }
        return after, past


def first_sales(
    offers: list[Offer], rooms: dict[int, Decimal], consumption: Consumption, ignored=()
) -> dict[int, tuple[dict[int, Decimal], set[int]]]:
    """{recipe_id: (what ONE sale of it, on its own, adds to each article as
    the engine attributes it, the articles that sale would take past their
    room)} - what is known of a recipe before any plan. `ignored` articles
    have no room to go past."""
    run = _Run(offers, rooms, consumption, ignored=ignored)
    found = {}
    for offer in offers:
        effect = run.effect(offer)
        found[offer.recipe_id] = (effect, run.past(effect))
    return found


def blocked(
    offers: list[Offer], rooms: dict[int, Decimal], consumption: Consumption, ignored=()
) -> dict[int, set[int]]:
    """{recipe_id: the articles ONE sale of it, on its own, would take past
    their room} for every offer that cannot be proposed at all."""
    return {
        recipe_id: past
        for recipe_id, (_effect, past) in first_sales(offers, rooms, consumption, ignored).items()
        if past
    }


class _Makeable:
    """Which totals, up to `top` cents, some prices make - each price used
    any number of times. A bitset on a Python int, in steps of the prices'
    greatest common divisor: bit k is k steps. Each price is folded in by
    doubling (k, 2k, 4k... at once), so it is a handful of shifts per price
    whatever the amount - 10 000 € in cents is 1 000 000 bits, 125 kB."""

    def __init__(self, prices, top: int):
        self.step = 0
        for price in prices:
            self.step = gcd(self.step, price)
        self.size = top // self.step + 1
        mask = (1 << self.size) - 1
        bits = 1
        for price in sorted(set(prices)):
            shift = price // self.step
            while shift < self.size:
                bits = (bits | (bits << shift)) & mask
                shift <<= 1
        self.bits = bits

    def makes(self, cents: int) -> bool:
        if cents < 0 or cents % self.step:
            return False
        index = cents // self.step
        return index < self.size and bool((self.bits >> index) & 1)

    def best_up_to(self, cents: int) -> int:
        """The largest total <= `cents` these prices make (0 at worst)."""
        if cents <= 0:
            return 0
        index = min(cents // self.step, self.size - 1)
        return ((self.bits & ((1 << (index + 1)) - 1)).bit_length() - 1) * self.step


def plan_sales(
    amount_cents: int,
    offers: list[Offer],
    rooms: dict[int, Decimal],
    consumption: Consumption,
    values: dict[int, Decimal] | None = None,
    already: dict[int, int] | None = None,
    ignored=(),
) -> Plan:
    """The sales to ring up for `amount_cents`, filling every room by about
    the same share. `offers` are the recipes that may be proposed (each
    priced above zero); `rooms` what each article has left to fill;
    `consumption` the engine (see Consumption); `values` each article's cost
    per unit, which weighs the articles one sale fills against each other;
    `already` the sales proposed before ({recipe_id: count}), which the plan
    builds on - `Plan.counts` and `poured` are the new sales only, `used`
    what the old and the new add together; `ignored` the articles the owner
    left out, neither a gap to fill nor a limit."""
    if amount_cents < 0:
        raise ValueError("amount_cents must not be negative")
    run = _Run([offer for offer in offers if offer.price_cents > 0], rooms, consumption, values, already, ignored)
    live = [offer for offer in run.offers if offer.reach() & run.targets]
    # An offer reaching no gap - every article it pours left out, say - is
    # never proposed, and must not count among the prices the rest of the
    # amount can be made of: its price made a rest look makeable that no
    # proposed sale could then make. Targets never grow, so it never would.
    run.offers = live
    if not live or amount_cents == 0:
        reason = EXACT if amount_cents == 0 else NOTHING_TO_FILL
        return Plan({}, 0, amount_cents, reason, used=run.added_all())

    makeable, prices = None, None
    while True:
        alive = run.alive()
        goal = 0
        if alive:
            if prices != frozenset(offer.price_cents for offer in alive):
                prices = frozenset(offer.price_cents for offer in alive)
                makeable = _Makeable(prices, amount_cents)
            goal = makeable.best_up_to(amount_cents - run.spent)
        if goal <= 0:
            if run.revive():
                continue
            break
        set_aside = len(run.dead) + len(run.idle)
        sale = run.next_sale(lambda offer, goal=goal, makeable=makeable: makeable.makes(goal - offer.price_cents))
        if sale is None and len(run.dead) + len(run.idle) == set_aside:
            if run.revive():
                continue
            break

    after, _past = run.past_with(run.counts)
    used = {
        article: after.get(article, ZERO) - run.base.get(article, ZERO)
        for article in set(after) | set(run.base)
        if after.get(article, ZERO) != run.base.get(article, ZERO)
    }
    remainder = amount_cents - run.spent
    if remainder == 0:
        reason = EXACT
    elif amount_cents < min(offer.price_cents for offer in live):
        reason = BELOW_CHEAPEST
    else:
        later = run.copy()
        later.total = after
        later.version += 1
        later.revive()
        reason = GAPS_FULL if later.next_sale() is None else NO_COMBINATION
    return Plan(
        counts={
            recipe_id: count - run.already.get(recipe_id, 0)
            for recipe_id, count in run.counts.items()
            if count - run.already.get(recipe_id, 0)
        },
        total_cents=run.spent,
        remainder_cents=remainder,
        reason=reason,
        used=used,
        poured={recipe_id: poured for recipe_id, poured in run.poured.items() if run.poured.get(recipe_id)},
    )
