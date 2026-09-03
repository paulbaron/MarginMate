import itertools
import math
import threading
from contextlib import contextmanager
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone

from common import JobLogMixin

from inventory.models import StockType, UnitChoices


_costing = threading.local()


def _recipes_being_costed() -> set:
    """Which recipes are part-way through being costed on this thread.

    A recipe can use another as an ingredient, so costing recurses. The
    ingredient FORM refuses to create a cycle (see services.assert_no_cycle),
    but the Django admin doesn't go through that form - so A->B->A is
    creatable, and without this guard it recurses until the stack runs out.
    That doesn't just break the two recipes involved: it 500s the recipe
    LIST, and the admin page you'd need to fix it from.

    A recipe already on the stack contributes 0 rather than raising, so the
    pages stay usable and the bad data is visible instead of fatal.
    """
    if not hasattr(_costing, "recipes"):
        _costing.recipes = set()
    return _costing.recipes


_variation_counts = threading.local()


@contextmanager
def _counting_scope():
    """Memoise sub-recipe variation counts for the duration of one
    computation.

    Nesting made this necessary. Working out how many ways a group can be
    satisfied asks every sub-recipe for its own count, which runs a query -
    and that question gets asked again for every group of every variation
    being built. A recipe with one sub-recipe and three variations was
    issuing 55 queries to render its detail page.

    Scoped rather than cached on the instance: the cache lives exactly as
    long as the outermost call that opened it, so a count taken after
    ingredients change can never be served from a stale entry - which is the
    trap an instance-level cache falls into, and a recipe's grouping changes
    every time someone presses "OU".
    """
    outer = getattr(_variation_counts, "cache", None)
    if outer is not None:
        yield outer  # reuse the enclosing scope's cache
        return
    _variation_counts.cache = {"counts": {}, "groups": {}}
    try:
        yield _variation_counts.cache
    finally:
        _variation_counts.cache = None


#: Public name for the same thing. A view rendering one recipe several ways
#: (its summary, the selected variation, the picker's labels) should wrap the
#: lot in this, so the sub-recipes behind it are read once rather than once
#: per question asked.
variation_scope = _counting_scope


def _bounds(low, high) -> tuple | None:
    """(low, high) unless either end is undefined, in which case there's no
    range to state - matching how a single variation with no computable
    margin/factor is left blank rather than shown as zero."""
    if low is None or high is None:
        return None
    return (low, high)


class Recipe(models.Model):
    """Something the bar sells (a cocktail, a dish) or an intermediate
    preparation (a house-made syrup, an infusion) that can itself be used
    inside other recipes - see RecipeIngredient.sub_recipe.

    yield_quantity/yield_unit say how much one full run of this recipe
    produces - a plain cocktail defaults to "1 Unité" (one serving), but a
    syrup batch might be "1 Litre". That's what makes composability work:
    a sub-recipe's total ingredient cost, divided by its own yield, gives a
    per-unit price a parent recipe can multiply by however much of it it
    actually uses (unit_cost_ht()).

    A recipe can also have VARIATIONS: ingredients sharing the same `group`
    (see RecipeIngredient) are alternatives to each other (e.g. "4cl Vodka"
    OR "5cl Gin"), and variations() is the cartesian product of one choice
    per group - each with its own cost/margin, but always the same yield
    and selling price (see variations() itself).
    """

    name = models.CharField(max_length=255, unique=True)
    # What the till calls the happy-hour version of this drink, when that
    # differs - L'Addition sells "Alcool + soda HH" as its own product,
    # separate from "Alcool + Soda". It pours the same thing out of the same
    # bottles, so for stock purposes it IS this recipe and its sales are
    # added to this recipe's; only the price differs, and that's already
    # covered by happy_hour_price_ttc. Naming it here rather than pattern-
    # matching on "HH" keeps it explicit and editable per recipe.
    happy_hour_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Nom du produit en happy hour sur la caisse, s'il diffère (ex : « Alcool + soda HH »).",
    )
    category = models.CharField(max_length=255, blank=True)
    yield_quantity = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("1"),
        # Must be positive: it's a divisor (see unit_cost_ht), and a recipe
        # that produces nothing isn't a recipe.
        validators=[MinValueValidator(Decimal("0.0001"))],
        help_text="Quantité produite par une préparation complète de cette recette.",
    )
    yield_unit = models.CharField(max_length=4, choices=UnitChoices.choices, default=UnitChoices.UNIT)
    selling_price_ttc = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal("0"))],
        help_text="Prix de vente affiché (TTC).",
    )
    happy_hour_price_ttc = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Optionnel - prix TTC en happy hour.",
    )
    vat_rate = models.DecimalField(
        max_digits=4, decimal_places=3, default=Decimal("0.20"),
        # A rate of exactly -1 makes (1 + vat_rate) zero, and selling_price_ht
        # then divides by it - which 500s the recipe list, the detail page and
        # the admin all at once. Bounded to a plausible range rather than just
        # excluding -1, since nothing outside 0-100% is a real VAT rate.
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        help_text="Ex : 0.20 pour 20%.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        """A till name may only mean one recipe.

        If two recipes claimed the same name - or one claimed another's name
        as its happy-hour name - importing sales would have to guess which,
        and a wrong guess silently moves stock consumption between drinks
        with nothing on screen to say so.
        """
        happy_hour_name = (self.happy_hour_name or "").strip()
        if not happy_hour_name:
            return
        if happy_hour_name.lower() == (self.name or "").strip().lower():
            raise ValidationError(
                {"happy_hour_name": "Identique au nom de la recette - laissez vide si la caisse n'a qu'un seul nom."}
            )
        clash = Recipe.objects.exclude(pk=self.pk).filter(
            models.Q(name__iexact=happy_hour_name) | models.Q(happy_hour_name__iexact=happy_hour_name)
        ).first()
        if clash is not None:
            raise ValidationError(
                {"happy_hour_name": f"Déjà utilisé par la recette « {clash.name} »."}
            )

    @property
    def _vat_divisor(self) -> Decimal:
        """1 + VAT, never zero. The validators keep vat_rate in range for
        anything saved through a form, but a row written before they existed
        (or through a raw update) must not be able to 500 every page that
        merely LISTS this recipe."""
        divisor = Decimal("1") + self.vat_rate
        return divisor if divisor else Decimal("1")

    @property
    def selling_price_ht(self) -> Decimal:
        return self.selling_price_ttc / self._vat_divisor

    @property
    def happy_hour_price_ht(self) -> Decimal | None:
        if self.happy_hour_price_ttc is None:
            return None
        return self.happy_hour_price_ttc / self._vat_divisor

    @property
    def vat_percent(self) -> Decimal:
        return self.vat_rate * Decimal("100")

    def _price_metrics(self, price_ht: Decimal | None, cost_ht: Decimal) -> dict:
        if price_ht is None:
            return {"margin_ht": None, "margin_percent": None, "price_factor": None}
        margin_ht = price_ht - cost_ht
        return {
            "margin_ht": margin_ht,
            "margin_percent": (margin_ht / price_ht * Decimal("100")) if price_ht else Decimal("0"),
            # "Combien de fois le coût" - the standard F&B costing multiplier
            # (selling price = cost x factor), not margin/cost - e.g. a 2€
            # cost sold at 9€ is "x4.5", not "x3.5".
            "price_factor": (price_ht / cost_ht) if cost_ht else None,
        }

    # --- nested alternatives ------------------------------------------
    #
    # An option in a choice group can itself be a recipe with choices of its
    # own ("4cl Vodka OU <Sirop maison>", where the syrup is "sucre OU
    # miel"). Picking that option therefore isn't one choice but several, so
    # a group offers not len(options) ways to satisfy it but the SUM of what
    # each option offers. Everything below follows from that one change.
    #
    # Options are addressed by a flat index per group, counting through each
    # option's own variations in turn - so a selection stays one number per
    # group however deeply recipes nest, and the ?v= links on the detail page
    # keep working unchanged.

    def option_variation_count(self, ingredient) -> int:
        """How many distinct ways this one option can be realised."""
        if not ingredient.sub_recipe_id:
            return 1
        being_counted = _recipes_being_costed()
        if ingredient.sub_recipe_id in being_counted:
            return 1  # a cycle - see _recipes_being_costed
        with _counting_scope() as cache:
            counts = cache["counts"]
            if ingredient.sub_recipe_id in counts:
                return counts[ingredient.sub_recipe_id]
            being_counted.add(ingredient.sub_recipe_id)
            try:
                count = max(1, ingredient.sub_recipe.variation_count)
            finally:
                being_counted.discard(ingredient.sub_recipe_id)
            counts[ingredient.sub_recipe_id] = count
            return count

    def group_size(self, group) -> int:
        return sum(self.option_variation_count(ingredient) for ingredient in group)

    def resolve_option(self, group, index: int):
        """(ingredient, which of ITS variations) for a group's flat index.

        Walks the options accumulating their sizes rather than building the
        expanded list, so a sub-recipe with a million variations costs no
        more to address than one with two.
        """
        if not group:
            return None, 0
        index = max(0, index)
        for ingredient in group:
            size = self.option_variation_count(ingredient)
            if index < size:
                return ingredient, index
            index -= size
        # Out of range - a stale ?v= link after the recipe was edited.
        # The FIRST option is the sane landing place, not the last.
        return group[0], 0

    def choice_groups(self, ingredients=None) -> list[list["RecipeIngredient"]]:
        """This recipe's ingredients bucketed into choice groups, in the
        order they're displayed. One bucket per `group`; a bucket with more
        than one entry is a real choice.

        `ingredients` lets a caller that already loaded them (a list page
        prefetching every recipe at once) pass them in instead of hitting
        the database again.
        """
        if ingredients is None:
            if self.pk is None:
                # An unsaved recipe has no ingredients to fetch - Django
                # refuses the reverse relation outright rather than returning
                # nothing - and "no groups" is the truthful answer for one
                # being typed in on the "new recipe" page. Without this,
                # anything asking a blank form's recipe for its variations
                # (a template, a helper) 500s the page.
                return []
            cache = getattr(_variation_counts, "cache", None)
            # Inside a variation_scope the same recipe gets asked for its
            # groups repeatedly - once per variation built, per sub-recipe
            # name resolved, per cost bound taken - and each ask was a query.
            if cache is not None and self.pk in cache["groups"]:
                return cache["groups"][self.pk]
            ingredients = list(
                self.ingredients.select_related("stock_type", "sub_recipe")
                # An ingredient's cost is its stock item's average across
                # every movement, so without this each one is another query.
                .prefetch_related("stock_type__movements")
                .order_by("group", "id")
            )
            if cache is not None:
                groups = self._bucket(ingredients)
                cache["groups"][self.pk] = groups
                return groups
        return self._bucket(ingredients)

    @staticmethod
    def _bucket(ingredients) -> list[list["RecipeIngredient"]]:
        groups: dict[int, list] = {}
        for ingredient in ingredients:
            groups.setdefault(ingredient.group, []).append(ingredient)
        # Dict order is insertion order, and the query is ordered by group,
        # so this comes out in display order without a second sort.
        return list(groups.values())

    def summary(self, ingredients=None) -> dict:
        """Everything the list and detail pages need about this recipe's
        variations, computed in time linear in the number of INGREDIENTS
        rather than the number of variations.

        That distinction is the whole point. The variations are the
        cartesian product of the choice groups, so 20 either/or choices is
        already 1,048,576 of them - enumerating those to find the cheapest
        and dearest is hopeless, and it doesn't take an unusual recipe to
        get there. But every quantity a range is built from is monotonic in
        the total cost, and the total cost is just a sum of one choice per
        group, so the extremes can be read straight off the per-group
        extremes:

            cheapest variation  = sum of each group's cheapest option
            dearest variation   = sum of each group's dearest option

        and margin (price - cost), margin % and price factor (price / cost)
        all follow from those two, the first two increasing as cost falls
        and the factor likewise. No enumeration anywhere.
        """
        with _counting_scope():
            return self._summary(self.choice_groups(ingredients))

    def _summary(self, groups) -> dict:
        if not groups:
            return {
                "variation_count": 0,
                "cost_range": None,
                "margin_range": None,
                "margin_percent_range": None,
                "price_factor_range": None,
            }

        # A group offers the SUM of what its options offer, because an
        # option can be a recipe with choices of its own - see
        # option_variation_count. With plain stock items every option offers
        # one, and this is just len(group) as before.
        variation_count = math.prod(self.group_size(group) for group in groups)
        # Each option contributes a RANGE, not a number, for the same reason.
        group_bounds = [[ingredient.cost_bounds() for ingredient in group] for group in groups]
        min_cost = sum((min(low for low, _ in bounds) for bounds in group_bounds), start=Decimal("0"))
        max_cost = sum((max(high for _, high in bounds) for bounds in group_bounds), start=Decimal("0"))
        # Per group: its cheapest cost, and its cheapest cost that isn't
        # FREE. The second is what the price factor needs, and it isn't just
        # "the smallest positive low": an option ranging 0 to 5 can be free,
        # but it can also be made for 5, and that variation has a perfectly
        # good factor. None when the group can only ever be free.
        group_mins = [min(low for low, _ in bounds) for bounds in group_bounds]
        group_min_positives = [
            min(
                (low if low > 0 else high for low, high in bounds if high > 0),
                default=None,
            )
            for bounds in group_bounds
        ]

        selling_price_ht = self.selling_price_ht
        # Margin and margin% are highest when the cost is lowest, so the
        # bounds swap over.
        cheap = self._price_metrics(selling_price_ht, min_cost)
        dear = self._price_metrics(selling_price_ht, max_cost)

        return {
            "variation_count": variation_count,
            "cost_range": (min_cost, max_cost),
            "margin_range": _bounds(dear["margin_ht"], cheap["margin_ht"]),
            "margin_percent_range": _bounds(dear["margin_percent"], cheap["margin_percent"]),
            "price_factor_range": self._price_factor_range(
                selling_price_ht, group_mins, group_min_positives, min_cost, max_cost
            ),
        }

    def _price_factor_range(
        self, selling_price_ht, group_mins, group_min_positives, min_cost, max_cost
    ):
        """price / cost, so the factor is highest for the cheapest variation
        - and undefined for a variation that costs nothing at all (an
        ingredient whose stock item has no purchases yet).

        Those free variations are skipped rather than allowed to make the
        whole range meaningless, so the top of the range comes from the
        cheapest variation that actually costs something. That is still
        computable without enumeration: a total is zero only when every group
        contributes zero, so the smallest non-zero total is reached by letting
        exactly ONE group pay its own smallest non-zero cost while every other
        stays at its minimum. Any second group going positive only adds.
        """
        if selling_price_ht is None or not max_cost:
            return None
        if min_cost > 0:
            min_positive_cost = min_cost
        else:
            candidates = [
                min_cost - group_min + min_positive
                for group_min, min_positive in zip(group_mins, group_min_positives)
                if min_positive is not None
            ]
            # Every group can only ever be free, yet max_cost was non-zero:
            # not reachable, but a zero here would divide by zero below.
            if not candidates:
                return None
            min_positive_cost = min(candidates)
        if min_positive_cost <= 0:
            return None
        return _bounds(selling_price_ht / max_cost, selling_price_ht / min_positive_cost)

    def variation_at(self, index: int, ingredients=None) -> dict | None:
        """The index-th variation, in the same order variations() lists
        them, without building the ones before it - the last group varies
        fastest, exactly like itertools.product. O(number of groups)."""
        with _counting_scope():
            return self._variation_at(index, self.choice_groups(ingredients))

    def _variation_at(self, index: int, groups) -> dict | None:
        sizes = [self.group_size(group) for group in groups]
        if not groups or not 0 <= index < math.prod(sizes):
            return None
        chosen = [None] * len(groups)
        for position in range(len(groups) - 1, -1, -1):
            chosen[position] = self.resolve_option(groups[position], index % sizes[position])
            index //= sizes[position]
        return self._build_variation(chosen, groups, sizes)

    def variation_for(self, selection: list[int], ingredients=None) -> dict | None:
        """One specific variation, given which option was picked in each
        group. Out-of-range picks fall back to that group's first option
        rather than raising, so a stale URL or a since-edited recipe still
        renders something sensible."""
        groups = self.choice_groups(ingredients)
        if not groups:
            return None
        selection = list(selection) + [0] * (len(groups) - len(selection))
        chosen = [
            self.resolve_option(group, max(0, selection[position]))
            for position, group in enumerate(groups)
        ]
        return self._build_variation(chosen, groups)

    def _build_variation(self, chosen, groups, sizes=None) -> dict:
        """`chosen` is one (ingredient, sub-variation index) pair per group.

        `sizes` is the groups' own sizes when the caller already knows them -
        they cost a query per sub-recipe to work out, and a caller building
        many variations would otherwise pay that for every one of them.
        """
        if sizes is None:
            sizes = [self.group_size(group) for group in groups]
        breakdown = [
            {
                "ingredient": ing,
                "sub_index": sub_index,
                "unit_cost_ht": ing.unit_cost_ht(sub_index),
                "cost_ht": ing.cost_ht(sub_index),
                "name": ing.variation_name(sub_index),
            }
            for ing, sub_index in chosen
        ]
        cost_ht = sum((entry["cost_ht"] for entry in breakdown), start=Decimal("0"))
        happy_hour_price_ht = self.happy_hour_price_ht
        varying_names = [
            entry["name"] for entry, size in zip(breakdown, sizes) if size > 1
        ]
        return {
            "name": f"{self.name} ({', '.join(varying_names)})" if varying_names else self.name,
            "breakdown": breakdown,
            "cost_ht": cost_ht,
            **self._price_metrics(self.selling_price_ht, cost_ht),
            "happy_hour": self._price_metrics(happy_hour_price_ht, cost_ht) if happy_hour_price_ht else None,
        }

    def variations(self, ingredients=None) -> list[dict]:
        """Every variation, fully costed.

        Beware: this is the cartesian product, so it grows multiplicatively
        with the number of choice groups and is only safe on a recipe known
        to be small. Nothing the app renders uses it - the pages use
        summary() for the aggregate numbers and variation_at()/
        variation_for() for the one being looked at. Kept because
        "enumerate them all" is genuinely the clearest way to express what a
        variation IS, which is what the tests check against.
        """
        groups = self.choice_groups(ingredients)
        if not groups:
            return []
        with _counting_scope():
            return self._variations(groups)

    def _variations(self, groups) -> list[dict]:
        sizes = [self.group_size(group) for group in groups]
        expanded = [
            [self.resolve_option(group, index) for index in range(size)]
            for group, size in zip(groups, sizes)
        ]
        return [
            self._build_variation(list(combo), groups, sizes)
            for combo in itertools.product(*expanded)
        ]

    def variation_selections(self, ingredients=None):
        """(option indices, display name) for every variation, in
        variations() order. Same combinatorial cost as variations(), so
        callers must know the recipe is small - the detail page only uses it
        below MAX_LISTED_VARIATIONS - but it builds no breakdowns or costs,
        which is all a picker's labels need.
        """
        groups = self.choice_groups(ingredients)
        if not groups:
            return
        sizes = [self.group_size(group) for group in groups]
        for indices in itertools.product(*(range(size) for size in sizes)):
            varying = []
            for position, index in enumerate(indices):
                if sizes[position] <= 1:
                    continue
                ingredient, sub_index = self.resolve_option(groups[position], index)
                varying.append(ingredient.variation_name(sub_index))
            yield list(indices), (f"{self.name} ({', '.join(varying)})" if varying else self.name)

    @property
    def variation_count(self) -> int:
        return self.summary()["variation_count"]

    @property
    def has_variations(self) -> bool:
        return self.variation_count > 1

    @property
    def cost_range(self) -> tuple[Decimal, Decimal] | None:
        return self.summary()["cost_range"]

    @property
    def margin_range(self) -> tuple[Decimal, Decimal] | None:
        return self.summary()["margin_range"]

    @property
    def margin_percent_range(self) -> tuple[Decimal, Decimal] | None:
        return self.summary()["margin_percent_range"]

    @property
    def price_factor_range(self) -> tuple[Decimal, Decimal] | None:
        return self.summary()["price_factor_range"]

    def cost_ht(self, sub_index: int = 0) -> Decimal:
        """Cost of one variation - only meaningful (and only ever
        called) for a recipe with exactly one variation, since a recipe
        with real alternatives can't be used as a sub-recipe ingredient
        elsewhere (see RecipeIngredientForm) and has no single "the" cost
        of its own on the list/detail pages.

        Every path that recurses into a sub-recipe's cost comes through
        here, so this is the one place the cycle guard has to sit.
        """
        being_costed = _recipes_being_costed()
        if self.pk in being_costed:
            return Decimal("0")  # a cycle - see _recipes_being_costed
        being_costed.add(self.pk)
        try:
            variation = self.variation_at(sub_index)
            return variation["cost_ht"] if variation else Decimal("0")
        finally:
            being_costed.discard(self.pk)

    def unit_cost_ht(self, sub_index: int = 0) -> Decimal:
        """Cost per yield_unit - what a parent recipe pays per unit when
        using this recipe as one of its own ingredients. `sub_index` picks
        which of THIS recipe's variations the parent is using."""
        if not self.yield_quantity:
            return Decimal("0")
        return self.cost_ht(sub_index) / self.yield_quantity

    def unit_cost_bounds(self) -> tuple[Decimal, Decimal]:
        """(cheapest, dearest) per yield unit, across every variation."""
        if not self.yield_quantity:
            return Decimal("0"), Decimal("0")
        being_costed = _recipes_being_costed()
        if self.pk in being_costed:
            return Decimal("0"), Decimal("0")  # a cycle
        being_costed.add(self.pk)
        try:
            cost_range = self.summary()["cost_range"] or (Decimal("0"), Decimal("0"))
        finally:
            being_costed.discard(self.pk)
        return cost_range[0] / self.yield_quantity, cost_range[1] / self.yield_quantity


class RecipeSale(models.Model):
    """How many of one recipe were sold on one day.

    Deliberately the dumbest shape that can express a sale, because nothing
    fills it in yet: the till isn't connected, and when it is it might be an
    API, a CSV export, or something else entirely. Anything that can produce
    "(recipe, date, how many)" can feed this - see recipes/sales.py, which is
    the single entry point every importer should go through.

    One row per recipe per day, so re-importing a day corrects it instead of
    doubling it (see record_sales). A till that only reports weekly totals
    can post them against any one date in the week; the variance report only
    cares which stock-take window a sale falls into.
    """

    recipe = models.ForeignKey(Recipe, related_name="sales", on_delete=models.CASCADE)
    sold_on = models.DateField()
    quantity = models.PositiveIntegerField()
    # Free-form provenance ("manual", "csv", "api:lightspeed") - kept so a
    # bad import can be found and re-run without guessing which rows it wrote.
    source = models.CharField(max_length=50, default="manual")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sold_on", "recipe__name"]
        constraints = [
            # Unique per SOURCE as well as per recipe and day, so a sale
            # typed in by hand and one imported from the till can coexist on
            # the same day and are added together (see sales_between).
            # Keyed on (recipe, day) alone, re-importing a period would
            # overwrite the manual entry for every day it touches - silently
            # deleting the very sales that were entered BECAUSE the till
            # doesn't know about them.
            models.UniqueConstraint(
                fields=["recipe", "sold_on", "source"], name="unique_recipe_sale_per_day_and_source"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.recipe.name} ({self.sold_on})"


class SalesImportJob(JobLogMixin):
    """One run of "fetch the sales from the till".

    Same shape as invoices' ScrapeJob and for the same reason: the run drives
    a real browser for a minute or more per date window, which is far too
    long to hold a request open, so it happens on a background thread and the
    page polls this row.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Terminé"
        FAILED = "FAILED", "Échoué"
        CANCELLED = "CANCELLED", "Annulé"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    range_start = models.DateField(null=True, blank=True)
    range_end = models.DateField(null=True, blank=True)
    # A thread can't be killed safely, so cancelling is cooperative: this is
    # checked between date windows and the run stops itself.
    cancel_requested = models.BooleanField(default=False)
    log = models.TextField(blank=True)
    items_sold = models.IntegerField(default=0)
    recorded = models.IntegerField(default=0)
    unmatched = models.IntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def append_log(self, message: str) -> None:
        now = timezone.now()
        elapsed = (now - self.started_at).total_seconds()
        line = f"[+{elapsed:6.1f}s] {message}"
        self.log = f"{self.log}{line}\n" if self.log else f"{line}\n"
        self.last_heartbeat = now
        self.save(update_fields=["log", "last_heartbeat"])


class PosProduct(models.Model):
    """A product as the till knows it.

    The mirror of inventory's review queue: an invoice line creates a Product
    that needs a stock item, and a till line creates one of these that needs
    a recipe. Until it has one, whatever stock it consumes is unaccounted
    for and shows up as missing in the variance report - so this exists to
    make that backlog visible and workable rather than a list printed once
    at the end of an import and then lost.

    `ignored` is for the things that legitimately never get a recipe: coffee,
    food, anything whose stock isn't tracked. They stop appearing in the
    worklist without pretending to be mapped.
    """

    name = models.CharField(max_length=255, unique=True)
    recipe = models.ForeignKey(
        Recipe, null=True, blank=True, on_delete=models.SET_NULL, related_name="pos_products"
    )
    ignored = models.BooleanField(default=False)
    # Straight from the export's own TAG_ columns - handy for triage, since
    # "Liquide (Alcool)" is where the money leaks and "Solide" mostly isn't.
    category = models.CharField(max_length=255, blank=True)
    typology = models.CharField(max_length=255, blank=True)
    # How much this has sold across every import, so the worklist can be
    # ordered by what actually matters.
    total_quantity = models.PositiveIntegerField(default=0)
    first_seen = models.DateField(null=True, blank=True)
    last_seen = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-total_quantity", "name"]

    def __str__(self):
        return self.name

    @property
    def needs_review(self) -> bool:
        return self.recipe_id is None and not self.ignored

    @property
    def status(self) -> str:
        if self.recipe_id:
            return "linked"
        return "ignored" if self.ignored else "pending"


class RecipeIngredient(models.Model):
    """One line of a recipe: a quantity of either a stock item or another
    recipe (exactly one of the two - see the CheckConstraint below and
    Recipe's own docstring on composability).

    `group`: ingredients sharing the same group number (within the same
    recipe) are alternatives to each other - exactly one is used per
    variation (see Recipe.variations()). An ingredient in a group by
    itself is just a fixed ingredient, always used. The number itself has
    no meaning beyond grouping rows of the same recipe - it's assigned by
    the form's "OU" button, never typed in directly.
    """

    recipe = models.ForeignKey(Recipe, related_name="ingredients", on_delete=models.CASCADE)
    group = models.PositiveIntegerField(default=0)
    stock_type = models.ForeignKey(StockType, null=True, blank=True, on_delete=models.PROTECT)
    sub_recipe = models.ForeignKey(Recipe, null=True, blank=True, related_name="used_in", on_delete=models.PROTECT)
    # Positive: a zero-quantity ingredient contributes nothing and only
    # clutters the breakdown, and a negative one silently reduces the
    # recipe's cost - which reads as a better margin.
    quantity = models.DecimalField(max_digits=10, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])

    class Meta:
        # Group first, then insertion order. This is the order the detail
        # page costs variations in, so the edit form's rows have to come out
        # the same way or the two pages describe the recipe differently:
        # ordered by pk alone (Django's fallback for an unordered model), an
        # alternative added to the FIRST ingredient gets the highest pk and
        # renders at the BOTTOM of the form, far from the ingredient it's an
        # alternative to - and since the "OU" connector only joins adjacent
        # rows, the choice silently stops looking like a choice at all.
        ordering = ["group", "id"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(stock_type__isnull=False, sub_recipe__isnull=True)
                    | models.Q(stock_type__isnull=True, sub_recipe__isnull=False)
                ),
                name="recipeingredient_exactly_one_source",
            )
        ]

    def __str__(self):
        return f"{self.quantity} x {self.source_name}"

    def clean(self):
        if bool(self.stock_type_id) == bool(self.sub_recipe_id):
            raise ValidationError("Choisissez soit un type de stock, soit une recette - pas les deux, pas aucun.")

    def unit_cost_ht(self, sub_index: int = 0) -> Decimal:
        """Cost per unit of whichever source (stock item or sub-recipe) this
        ingredient's quantity is expressed in.

        `sub_index` picks which of the sub-recipe's own variations is being
        used - meaningless for a stock item, which has one price.
        """
        if self.stock_type_id:
            return self.stock_type.current_unit_cost_ht
        return self.sub_recipe.unit_cost_ht(sub_index)

    def cost_ht(self, sub_index: int = 0) -> Decimal:
        """What this line contributes to a variation's total."""
        return self.unit_cost_ht(sub_index) * self.quantity

    def cost_bounds(self) -> tuple[Decimal, Decimal]:
        """(cheapest, dearest) this line can cost.

        A stock item is a single price, so both ends are the same. A
        sub-recipe with choices of its own spans a range, and that range is
        what makes the parent's own range come out right - which is why
        summary() works from bounds rather than from one number per option.
        """
        if self.stock_type_id:
            cost = self.cost_ht()
            return cost, cost
        low, high = self.sub_recipe.unit_cost_bounds()
        return low * self.quantity, high * self.quantity

    def variation_name(self, sub_index: int = 0) -> str:
        """What to call this option in a variation's name. A sub-recipe with
        choices of its own needs disambiguating - "Sirop maison" alone would
        read identically for every one of its variants."""
        if self.stock_type_id or not self.sub_recipe_id:
            return self.source_name
        if self.sub_recipe.variation_count <= 1:
            return self.source_name
        sub = self.sub_recipe.variation_at(sub_index)
        return sub["name"] if sub else self.source_name

    @property
    def source_name(self) -> str:
        return self.stock_type.name if self.stock_type_id else self.sub_recipe.name

    @property
    def source_unit_display(self) -> str:
        if self.stock_type_id:
            return self.stock_type.get_unit_display()
        return self.sub_recipe.get_yield_unit_display()


class SaleDocument(models.Model):
    """Something sold, recorded by hand: a bar tab settled off the books, a
    private event, a case sold to a friend at cost.

    Separate from RecipeSale because a line here can be a stock item sold
    AS ITSELF - a bottle over the counter - which is not a recipe and has no
    ingredients to expand. Both kinds feed the variance report: a recipe line
    consumes whatever the recipe consumes, a stock line consumes itself.
    """

    reference = models.CharField(max_length=100, blank=True, help_text="Optionnel — votre propre numéro.")
    sold_on = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sold_on", "-created_at"]

    def __str__(self):
        return f"Vente du {self.sold_on:%d/%m/%Y}" + (f" ({self.reference})" if self.reference else "")

    @property
    def total_ttc(self) -> Decimal:
        return sum((line.total_ttc for line in self.lines.all()), start=Decimal("0"))


class SaleDocumentLine(models.Model):
    """One line of a sale document: a recipe OR a stock item, never both and
    never neither - the same exactly-one-source shape as RecipeIngredient and
    StockTakeLine, and enforced the same way."""

    document = models.ForeignKey(SaleDocument, related_name="lines", on_delete=models.CASCADE)
    recipe = models.ForeignKey(Recipe, null=True, blank=True, on_delete=models.PROTECT, related_name="sale_lines")
    stock_type = models.ForeignKey(
        StockType, null=True, blank=True, on_delete=models.PROTECT, related_name="sale_lines"
    )
    # Decimal rather than an integer count: a recipe is sold by the serving,
    # but a stock item can be sold by the litre.
    quantity = models.DecimalField(max_digits=10, decimal_places=4)
    unit_price_ttc = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Optionnel — laissez vide pour le prix de vente de la recette.",
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(recipe__isnull=False, stock_type__isnull=True)
                    | models.Q(recipe__isnull=True, stock_type__isnull=False)
                ),
                name="saledocumentline_exactly_one_source",
            )
        ]

    def __str__(self):
        return f"{self.quantity} x {self.source_name}"

    def clean(self):
        if bool(self.recipe_id) == bool(self.stock_type_id):
            raise ValidationError("Choisissez soit une recette, soit un type de stock — pas les deux.")

    @property
    def source_name(self) -> str:
        return self.recipe.name if self.recipe_id else self.stock_type.name

    @property
    def unit_display(self) -> str:
        return self.recipe.get_yield_unit_display() if self.recipe_id else self.stock_type.get_unit_display()

    @property
    def total_ttc(self) -> Decimal:
        """The line's own price when given, else the recipe's own selling
        price. A stock item sold as itself has no default - there's no
        "price" on a stock item, only a cost."""
        if self.unit_price_ttc is not None:
            return self.unit_price_ttc * self.quantity
        if self.recipe_id:
            return self.recipe.selling_price_ttc * self.quantity
        return Decimal("0")
