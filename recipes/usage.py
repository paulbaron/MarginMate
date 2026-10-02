"""Which recipes use a given article.

The question behind it is « le sucre augmente, qu'est-ce que je dois
reprendre ? », and it is not answered by the ingredient rows alone: a
cocktail whose house syrup contains sugar IS a recipe that uses sugar, and a
re-pricing that skipped it would be wrong by exactly the thing that moved.

So reachability is the question, and `inventory.variance.reachable_stock_types`
already answers it - it walks sub-recipes and alternatives, is deliberately
never capped, and carries the cycle guard that goes with it. It is reused
here rather than re-implemented: a second walker over the same graph is a
second set of rules about sub-recipes and « OU » groups, and the two would
drift apart on the first recipe nobody thought of.

**« OU » is a choice, not a certainty.** A recipe offering « Vodka OU Gin »
may use either, and a shopping decision taken on it would be wrong. So every
use carries whether it is settled, and a use is settled only when nothing on
the way to the article is a choice: the ingredient is alone in its group, and
- when it is a sub-recipe - that sub-recipe has no variations at all
(`Recipe.variation_count`, which is 1 exactly when there is no choice
anywhere below it). That is deliberately cautious in one direction: a syrup
whose only choice is between mint and basil reads as « peut utiliser » for
its sugar, which is certain. Understating a certainty leaves the recipe in
the list with a hedge on it; overstating a maybe is a purchase made on
something that may never be poured.
"""

from __future__ import annotations

from dataclasses import dataclass

from inventory.variance import ingredients_by_recipe, reachable_stock_types


@dataclass(frozen=True)
class Use:
    """One way a recipe reaches an article.

    `via` is the sub-recipe the recipe itself lists - "" when the article is
    an ingredient of the recipe. It is the recipe's OWN ingredient and never
    the deepest one: that is the row the reader opens next, and everything
    below it is on that row's own page.

    `certain` is False when something on the way is a choice; the page then
    says the recipe only *may* use the article.
    """

    via: str = ""
    certain: bool = True


def article_uses(recipes) -> dict[int, dict[int, list[Use]]]:
    """{article id: {recipe pk: the ways that recipe reaches it}}.

    Linear in the number of ingredients, never in the number of variations
    (20 either/or ingredients is already a million of those). A recipe naming
    the same article twice is one entry saying it once; an article no recipe
    reaches is absent rather than present and empty, so the picker is built
    from the keys.

    The walk is memoised by sub-recipe, so a house syrup used by thirty
    cocktails is read once: the answer depends on the sub-recipe alone, and
    without it the « Recettes » tab read the whole graph once per row. And it
    is handed the ingredients the recipes were read with (the tab prefetches
    every recipe's): walked from the database, each sub-recipe on the way
    was a query - 44 of the tab's 60.
    """
    recipes = list(recipes)
    ingredients_of = ingredients_by_recipe(recipes)
    reached: dict[int, frozenset[int]] = {}
    settled: dict[int, bool] = {}
    uses: dict[int, dict[int, list[Use]]] = {}
    for recipe in recipes:
        for group in recipe.choice_groups(ingredients_of[recipe.pk]):
            alone = len(group) == 1
            for ingredient in group:
                via = "" if ingredient.stock_type_id else ingredient.sub_recipe.name
                use = Use(via=via, certain=alone and _settled(ingredient, settled))
                for article_id in _reached(ingredient, reached, ingredients_of):
                    ways = uses.setdefault(article_id, {}).setdefault(recipe.pk, [])
                    if use not in ways:
                        ways.append(use)
    return uses


def _reached(ingredient, cache: dict[int, frozenset[int]], ingredients_of=None):
    """Every article this ingredient could draw on - the variance engine's
    own answer, asked once per sub-recipe."""
    if ingredient.stock_type_id:
        return (ingredient.stock_type_id,)
    if not ingredient.sub_recipe_id:
        return ()
    if ingredient.sub_recipe_id not in cache:
        cache[ingredient.sub_recipe_id] = frozenset(reachable_stock_types(ingredient, ingredients_of=ingredients_of))
    return cache[ingredient.sub_recipe_id]


def _settled(ingredient, cache: dict[int, bool]) -> bool:
    """Whether choosing this option leaves nothing else to be chosen.

    An article is one thing. A sub-recipe is settled only when it has a
    single variation: a « sucre OU miel » syrup reaches sugar without
    promising it.
    """
    if ingredient.stock_type_id:
        return True
    if not ingredient.sub_recipe_id:
        return False
    if ingredient.sub_recipe_id not in cache:
        cache[ingredient.sub_recipe_id] = ingredient.sub_recipe.variation_count <= 1
    return cache[ingredient.sub_recipe_id]
