import math
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import ListView

from .forms import RecipeForm, RecipeIngredientFormSet, ingredient_unit_map
from .models import Recipe


def _existing_categories():
    return Recipe.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")

_PIE_COLORS = ["#d99b3f", "#6fbf73", "#e0685f", "#5b9bd9", "#c77dd9", "#d9c73f", "#3fd9c7", "#9fa2ae"]


def _build_ingredient_pie_svg(breakdown: list[dict]) -> str:
    """Hand-rolled inline SVG pie chart of each ingredient's share of a
    recipe's total cost - same "no new dependency" approach as the stock
    page's price-history line chart. Built from the exact same breakdown
    cost_ht() itself sums, so the chart and the displayed total can never
    disagree. Empty string when there's no cost to show a proportion of."""
    total = sum((entry["cost_ht"] for entry in breakdown), start=Decimal("0"))
    non_zero = [entry for entry in breakdown if entry["cost_ht"] > 0]
    if not non_zero or not total:
        return ""

    size, radius = 220, 100
    cx = cy = size / 2

    def legend_item(color, name, fraction):
        return (
            '<span style="display:inline-flex; align-items:center; gap:0.35rem; margin:0 0.75rem 0.35rem 0;">'
            f'<span style="width:0.7rem; height:0.7rem; background:{color}; border-radius:2px; '
            'display:inline-block; flex:none;"></span>'
            f"{name} ({fraction * 100:.1f}%)</span>"
        )

    if len(non_zero) == 1:
        name = non_zero[0]["ingredient"].source_name
        svg = (
            f'<svg viewBox="0 0 {size} {size}" class="recipe-pie-chart" role="img" '
            f'aria-label="Répartition du coût"><circle cx="{cx}" cy="{cy}" r="{radius}" '
            f'fill="{_PIE_COLORS[0]}"><title>{name} : 100%</title></circle></svg>'
        )
        return svg + f'<div style="margin-top:0.5rem;">{legend_item(_PIE_COLORS[0], name, 1)}</div>'

    start_angle = -math.pi / 2  # 12 o'clock
    slices, legend = [], []
    for i, entry in enumerate(non_zero):
        fraction = float(entry["cost_ht"] / total)
        angle = fraction * 2 * math.pi
        end_angle = start_angle + angle
        x1, y1 = cx + radius * math.cos(start_angle), cy + radius * math.sin(start_angle)
        x2, y2 = cx + radius * math.cos(end_angle), cy + radius * math.sin(end_angle)
        large_arc = 1 if angle > math.pi else 0
        color = _PIE_COLORS[i % len(_PIE_COLORS)]
        name = entry["ingredient"].source_name
        slices.append(
            f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{radius},{radius} 0 {large_arc} 1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{color}"><title>{name} : {fraction * 100:.1f}%</title></path>'
        )
        legend.append(legend_item(color, name, fraction))
        start_angle = end_angle

    svg = (
        f'<svg viewBox="0 0 {size} {size}" class="recipe-pie-chart" role="img" '
        f'aria-label="Répartition du coût">{"".join(slices)}</svg>'
    )
    return svg + f'<div style="margin-top:0.5rem;">{"".join(legend)}</div>'


def _range(values: list) -> tuple | None:
    values = [v for v in values if v is not None]
    return (min(values), max(values)) if values else None


class RecipeListView(ListView):
    model = Recipe
    template_name = "recipes/recipe_list.html"
    context_object_name = "recipes"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for recipe in context["recipes"]:
            # Computed once here (rather than via the has_variations /
            # cost_range / margin_range properties, which would each
            # re-run variations() from scratch) since this page lists
            # every recipe at once.
            variations = recipe.variations()
            recipe.variation_count = len(variations)
            recipe.ranges = {
                "cost_ht": _range([v["cost_ht"] for v in variations]),
                "margin_ht": _range([v["margin_ht"] for v in variations]),
                "margin_percent": _range([v["margin_percent"] for v in variations]),
                "price_factor": _range([v["price_factor"] for v in variations]),
            }
        return context


def recipe_detail(request, pk):
    recipe = get_object_or_404(Recipe, pk=pk)
    variations = recipe.variations()
    for variation in variations:
        variation["pie_svg"] = _build_ingredient_pie_svg(variation["breakdown"])
    return render(request, "recipes/recipe_detail.html", {"recipe": recipe, "variations": variations})


def _recipe_form_view(request, recipe):
    if request.method == "POST":
        form = RecipeForm(request.POST, instance=recipe)
        formset = RecipeIngredientFormSet(
            request.POST, instance=recipe, form_kwargs={"parent_recipe": recipe if recipe.pk else None}
        )
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                recipe = form.save()
                formset.save()
            messages.success(request, f'"{recipe.name}" enregistrée.')
            return redirect("recipes:recipe_detail", pk=recipe.pk)
    else:
        form = RecipeForm(instance=recipe)
        formset = RecipeIngredientFormSet(instance=recipe, form_kwargs={"parent_recipe": recipe if recipe.pk else None})
    return render(
        request,
        "recipes/recipe_form.html",
        {
            "form": form,
            "formset": formset,
            "recipe": recipe,
            "existing_categories": _existing_categories(),
            "ingredient_units": ingredient_unit_map(),
        },
    )


def recipe_create(request):
    return _recipe_form_view(request, Recipe())


def recipe_update(request, pk):
    return _recipe_form_view(request, get_object_or_404(Recipe, pk=pk))


def recipe_delete(request, pk):
    if request.method != "POST":
        return redirect("recipes:recipe_list")
    recipe = get_object_or_404(Recipe, pk=pk)
    if recipe.used_in.exists():
        used_by = ", ".join(f'"{ri.recipe.name}"' for ri in recipe.used_in.select_related("recipe"))
        messages.error(request, f'Impossible de supprimer "{recipe.name}" : utilisée comme ingrédient dans {used_by}.')
        return redirect("recipes:recipe_detail", pk=pk)
    name = recipe.name
    recipe.delete()
    messages.success(request, f'"{name}" supprimée.')
    return redirect("recipes:recipe_list")
