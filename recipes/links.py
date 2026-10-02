"""What the till sells, linked to the recipe that makes it.

One place for it, because two pages do it - the till products to link, and
the recipe form ("Vendue en caisse sous") - and each link has consequences
beyond the row: the recipe's sales are rebuilt from the per-day quantities
already on file, and a happy-hour name leaves the recipe with the till
product that carried it (or the import would go on counting that product's
sales there, and relink it).
"""

import difflib
import re
import unicodedata

from django.core.exceptions import ValidationError

from .sales import resync_recipe_from_daily_quantities

HAPPY_HOUR_RE = re.compile(r"\b(?:hh|happy\s*hour)\b")
#: How alike a till name and a recipe name must be (difflib's ratio) for the
#: recipe to be chosen in advance - "Mojitos" for "Mojito", not "Mule" for
#: "Mojito". Only a suggestion: nothing is linked until someone confirms.
SUGGESTION_THRESHOLD = 0.8


class LinkError(ValueError):
    """A link refused, with the reason in words."""


def plain(text: str) -> str:
    """ "Spritz Apérol (HH)" -> "spritz aperol hh"."""
    decomposed = unicodedata.normalize("NFD", text or "")
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unaccented).split())


def suggest_recipe(name: str, recipes) -> tuple:
    """The recipe a till product most likely is, or None - and whether its
    name says it is the happy-hour one ("Pinte Blonde HH")."""
    return RecipeSuggester(recipes)(name)


class RecipeSuggester:
    """`suggest_recipe` for many till names against one list of recipes -
    « À lier » asks it once per row, every row against every recipe.

    The answer is the one the plain loop gives, name for name: the FIRST
    recipe whose plain name is the target, else the first one with the best
    ratio, kept only at SUGGESTION_THRESHOLD and above. It is just cheaper to
    reach. Each recipe's name is made plain and analysed once (difflib keeps
    what it learnt of `b` across `set_seq1`), and a recipe whose ratio cannot
    reach the threshold, or cannot beat the best so far, is never measured:
    `real_quick_ratio` and `quick_ratio` bound `ratio` from above (same
    formula, more matches counted). Neither test can skip the recipe the
    loop chooses: its bound is at least its ratio, which is the highest, at
    least the threshold, and higher than every ratio before it.
    """

    def __init__(self, recipes):
        self._exact: dict[str, object] = {}
        self._matchers = []
        for recipe in recipes:
            candidate = plain(recipe.name)
            self._exact.setdefault(candidate, recipe)
            self._matchers.append((recipe, difflib.SequenceMatcher(None, "", candidate)))

    def __call__(self, name: str) -> tuple:
        target = plain(name)
        happy_hour = bool(HAPPY_HOUR_RE.search(target))
        target = " ".join(HAPPY_HOUR_RE.sub(" ", target).split())
        if not target:
            return None, happy_hour
        if target in self._exact:
            return self._exact[target], happy_hour
        best, best_score = None, 0.0
        for recipe, matcher in self._matchers:
            matcher.set_seq1(target)
            if not _may_win(matcher.real_quick_ratio(), best_score) or not _may_win(matcher.quick_ratio(), best_score):
                continue
            score = matcher.ratio()
            if score > best_score:
                best, best_score = recipe, score
        return (best if best_score >= SUGGESTION_THRESHOLD else None), happy_hour


def _may_win(bound: float, best_score: float) -> bool:
    """Whether a ratio at most `bound` could be the suggestion: kept only
    from the threshold up, and only when strictly better than the best."""
    return bound >= SUGGESTION_THRESHOLD and bound > best_score


def link(product, recipe, happy_hour: bool = False) -> None:
    """Sell `product` as `recipe`. As its happy-hour name, the till name is
    recorded on the recipe too, so both names' sales fold into one."""
    previous = product.recipe if product.recipe_id else None
    if happy_hour:
        before = recipe.happy_hour_name
        recipe.happy_hour_name = product.name
        try:
            recipe.full_clean()
        except ValidationError as exc:
            recipe.happy_hour_name = before
            raise LinkError("; ".join(message for messages in exc.message_dict.values() for message in messages))
        recipe.save(update_fields=["happy_hour_name"])
    product.recipe = recipe
    product.ignored = False
    product.save(update_fields=["recipe", "ignored"])
    _settle(product, previous, {recipe})


def set_aside(product, ignored: bool) -> None:
    """Take `product` off its recipe: ignored (it consumes no tracked stock),
    or back to the products to link."""
    previous = product.recipe if product.recipe_id else None
    product.ignored = ignored
    product.recipe = None
    product.save(update_fields=["ignored", "recipe"])
    _settle(product, previous, set())


def _settle(product, previous, touched: set) -> None:
    # A happy-hour variant taken off its recipe takes its name with it: the
    # import counts sales by that name, and would go on adding this product's
    # to the recipe - and relink a product sent back to the worklist.
    if (
        previous is not None
        and product.recipe_id != previous.pk
        and previous.happy_hour_name.strip().lower() == product.name.strip().lower()
    ):
        previous.happy_hour_name = ""
        previous.save(update_fields=["happy_hour_name"])
    if previous is not None:
        touched.add(previous)
    # The sales of every recipe this touched are rebuilt from the per-day
    # quantities already on file - a local rebuild, not a new download.
    for recipe in touched:
        resync_recipe_from_daily_quantities(recipe)
