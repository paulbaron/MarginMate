from django.core.exceptions import ValidationError

from .models import Recipe, RecipeIngredient


def assert_no_cycle(recipe: Recipe, candidate_sub_recipe: Recipe) -> None:
    """Raises ValidationError if using `candidate_sub_recipe` as an
    ingredient of `recipe` would create a dependency loop - which would
    make Recipe.cost_ht()/unit_cost_ht() recurse forever.

    A loop exists exactly when `recipe` is reachable by walking forward
    from `candidate_sub_recipe` through its own ingredients' sub-recipes
    (i.e. candidate_sub_recipe already depends, directly or transitively,
    on recipe) - since adding this ingredient would then make `recipe`
    depend on something that depends on `recipe`.
    """
    if recipe.pk is None:
        return  # a brand new recipe can't be depended on by anything yet
    if candidate_sub_recipe.pk == recipe.pk:
        raise ValidationError("Une recette ne peut pas s'utiliser elle-même comme ingrédient.")

    visited = set()
    stack = [candidate_sub_recipe.pk]
    while stack:
        current_id = stack.pop()
        if current_id == recipe.pk:
            raise ValidationError(
                f'Impossible : "{candidate_sub_recipe.name}" dépend déjà de "{recipe.name}" '
                "(directement ou via une autre recette) - cela créerait une boucle."
            )
        if current_id in visited:
            continue
        visited.add(current_id)
        stack.extend(
            RecipeIngredient.objects.filter(recipe_id=current_id, sub_recipe__isnull=False).values_list(
                "sub_recipe_id", flat=True
            )
        )
