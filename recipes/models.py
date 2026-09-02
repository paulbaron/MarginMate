import itertools
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from inventory.models import StockType, UnitChoices


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
    category = models.CharField(max_length=255, blank=True)
    yield_quantity = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("1"),
        help_text="Quantité produite par une préparation complète de cette recette.",
    )
    yield_unit = models.CharField(max_length=4, choices=UnitChoices.choices, default=UnitChoices.UNIT)
    selling_price_ttc = models.DecimalField(max_digits=8, decimal_places=2, help_text="Prix de vente affiché (TTC).")
    happy_hour_price_ttc = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True, help_text="Optionnel - prix TTC en happy hour.",
    )
    vat_rate = models.DecimalField(
        max_digits=4, decimal_places=3, default=Decimal("0.20"), help_text="Ex : 0.20 pour 20%.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def selling_price_ht(self) -> Decimal:
        return self.selling_price_ttc / (Decimal("1") + self.vat_rate)

    @property
    def happy_hour_price_ht(self) -> Decimal | None:
        if self.happy_hour_price_ttc is None:
            return None
        return self.happy_hour_price_ttc / (Decimal("1") + self.vat_rate)

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

    def variations(self) -> list[dict]:
        """One entry per combination of alternatives (one per `group`) -
        each a full costed version of this recipe. A recipe with no
        choice groups (every ingredient in its own group) has exactly one
        entry. Every entry shares this recipe's own yield/selling price/
        happy hour price; only the ingredients (and so the cost/margin)
        differ between entries.
        """
        ingredients = list(self.ingredients.select_related("stock_type", "sub_recipe").order_by("group", "id"))
        if not ingredients:
            return []
        groups: dict[int, list] = {}
        for ingredient in ingredients:
            groups.setdefault(ingredient.group, []).append(ingredient)
        group_lists = list(groups.values())

        selling_price_ht = self.selling_price_ht
        happy_hour_price_ht = self.happy_hour_price_ht
        results = []
        for combo in itertools.product(*group_lists):
            breakdown = [
                {"ingredient": ing, "unit_cost_ht": ing.unit_cost_ht(), "cost_ht": ing.unit_cost_ht() * ing.quantity}
                for ing in combo
            ]
            cost_ht = sum((entry["cost_ht"] for entry in breakdown), start=Decimal("0"))
            varying_names = [ing.source_name for ing, options in zip(combo, group_lists) if len(options) > 1]
            results.append(
                {
                    "name": f"{self.name} ({', '.join(varying_names)})" if varying_names else self.name,
                    "breakdown": breakdown,
                    "cost_ht": cost_ht,
                    **self._price_metrics(selling_price_ht, cost_ht),
                    "happy_hour": self._price_metrics(happy_hour_price_ht, cost_ht) if happy_hour_price_ht else None,
                }
            )
        return results

    @property
    def has_variations(self) -> bool:
        return len(self.variations()) > 1

    def _variation_range(self, key: str) -> tuple[Decimal, Decimal] | None:
        """(min, max) of variations()[*][key] - the same value twice when
        there's only one variation, or when every variation happens to
        land on the same number. None when there are no variations yet
        (e.g. a group with no ingredients) or every value is None (e.g.
        margin_percent/price_factor before a selling price is set)."""
        values = [v[key] for v in self.variations() if v[key] is not None]
        if not values:
            return None
        return min(values), max(values)

    @property
    def cost_range(self) -> tuple[Decimal, Decimal] | None:
        return self._variation_range("cost_ht")

    @property
    def margin_range(self) -> tuple[Decimal, Decimal] | None:
        return self._variation_range("margin_ht")

    @property
    def margin_percent_range(self) -> tuple[Decimal, Decimal] | None:
        return self._variation_range("margin_percent")

    @property
    def price_factor_range(self) -> tuple[Decimal, Decimal] | None:
        return self._variation_range("price_factor")

    def cost_ht(self) -> Decimal:
        """Cost of the single variation - only meaningful (and only ever
        called) for a recipe with exactly one variation, since a recipe
        with real alternatives can't be used as a sub-recipe ingredient
        elsewhere (see RecipeIngredientForm) and has no single "the" cost
        of its own on the list/detail pages - those use variations()."""
        variations = self.variations()
        return variations[0]["cost_ht"] if variations else Decimal("0")

    def unit_cost_ht(self) -> Decimal:
        """Cost per yield_unit - what a parent recipe pays per unit when
        using this recipe as one of its own ingredients."""
        if not self.yield_quantity:
            return Decimal("0")
        return self.cost_ht() / self.yield_quantity


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
    quantity = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
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

    def unit_cost_ht(self) -> Decimal:
        """Cost per unit of whichever source (stock item or sub-recipe)
        this ingredient's quantity is expressed in."""
        if self.stock_type_id:
            return self.stock_type.current_unit_cost_ht
        return self.sub_recipe.unit_cost_ht()

    @property
    def source_name(self) -> str:
        return self.stock_type.name if self.stock_type_id else self.sub_recipe.name

    @property
    def source_unit_display(self) -> str:
        if self.stock_type_id:
            return self.stock_type.get_unit_display()
        return self.sub_recipe.get_yield_unit_display()
