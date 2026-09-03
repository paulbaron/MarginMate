import math
import threading
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.html import escape
from django.views.generic import ListView

from .forms import (
    MANUAL_SALE_SOURCE,
    ManualSaleForm,
    RecipeForm,
    RecipeIngredientFormSet,
    ingredient_unit_map,
)
from .models import PosProduct, Recipe, RecipeSale, SalesImportJob, variation_scope
from .tasks import import_laddition_sales_task


def _existing_categories():
    return Recipe.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")

_PIE_COLORS = ["#d99b3f", "#6fbf73", "#e0685f", "#5b9bd9", "#c77dd9", "#d9c73f", "#3fd9c7", "#9fa2ae"]


def _build_ingredient_pie_svg(breakdown: list[dict]) -> str:
    """Hand-rolled inline SVG pie chart of each ingredient's share of a
    recipe's total cost - same "no new dependency" approach as the stock
    page's price-history line chart. Built from the exact same breakdown
    cost_ht() itself sums, so the chart and the displayed total can never
    disagree. Empty string when there's no cost to show a proportion of.

    The result is rendered with |safe, so every stock item / sub-recipe name
    that goes into it has to be escaped here - those names come from invoice
    text and from whatever the user typed into the stock page, neither of
    which is trustworthy markup.
    """
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
        name = escape(non_zero[0]["ingredient"].source_name)
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
        name = escape(entry["ingredient"].source_name)
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


class RecipeListView(ListView):
    model = Recipe
    template_name = "recipes/recipe_list.html"
    context_object_name = "recipes"

    def get_queryset(self):
        # Every recipe's ingredients in one extra query, so summary() below
        # never goes back to the database per row.
        return super().get_queryset().prefetch_related(
            "ingredients__stock_type__movements", "ingredients__sub_recipe"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        with variation_scope():
            for recipe in context["recipes"]:
                # summary() is linear in the number of ingredients, so a
                # recipe with a million variations costs the same here as one
                # with two.
                recipe.summary_data = recipe.summary(list(recipe.ingredients.all()))
        return context


# Beyond this many variations the picker stops listing them individually and
# offers one dropdown per choice group instead. Well below the point where
# rendering would actually struggle - a list of a few hundred near-identical
# option labels has stopped being useful to read long before that.
MAX_LISTED_VARIATIONS = 60


def recipe_detail(request, pk):
    recipe = get_object_or_404(Recipe, pk=pk)
    ingredients = list(
        recipe.ingredients.select_related("stock_type", "sub_recipe").prefetch_related("stock_type__movements")
    )
    # One scope around the whole page: the summary, the chosen variation and
    # the picker labels all ask the same sub-recipes the same questions.
    with variation_scope():
        return _render_recipe_detail(request, recipe, ingredients)


def _render_recipe_detail(request, recipe, ingredients):
    groups = recipe.choice_groups(ingredients)
    summary = recipe.summary(ingredients)

    # Which option is picked in each group, from ?v=0.2.1 - one index per
    # group. Out-of-range or malformed values fall back to the first option
    # (see variation_for) rather than 404ing, so an old bookmark still works
    # after the recipe has been edited.
    selection = [_to_int(part) for part in (request.GET.get("v") or "").split(".") if part != ""]
    variation = recipe.variation_for(selection, ingredients)

    listed_variations = None
    if 1 < summary["variation_count"] <= MAX_LISTED_VARIATIONS:
        listed_variations = [
            {"value": _selection_key(indices), "name": name}
            for indices, name in recipe.variation_selections(ingredients)
        ]

    normalised = _normalised_selection(selection, groups)
    return render(
        request,
        "recipes/recipe_detail.html",
        {
            "recipe": recipe,
            "summary": summary,
            "variation": variation,
            # One entry per choice group, for the per-group pickers used
            # when there are too many variations to list individually.
            "group_pickers": [
                {
                    "position": position,
                    "selected": normalised[position],
                    "options": [
                        {"index": index, "name": ingredient.source_name}
                        for index, ingredient in enumerate(group)
                    ],
                }
                for position, group in enumerate(groups)
                if len(group) > 1
            ],
            "listed_variations": listed_variations,
            "selection_key": _selection_key(normalised),
            "pie_svg": _build_ingredient_pie_svg(variation["breakdown"]) if variation else "",
        },
    )


def _to_int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def _normalised_selection(selection: list[int], groups) -> list[int]:
    selection = list(selection) + [0] * (len(groups) - len(selection))
    return [
        selection[position] if 0 <= selection[position] < len(group) else 0
        for position, group in enumerate(groups)
    ]


def _selection_key(indices) -> str:
    """The ?v= value for one selection: one option index per group, e.g.
    "0.2.1". Positional rather than by ingredient id so a link stays short
    however many groups there are."""
    return ".".join(str(index) for index in indices)


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
    # ?name= prefills the form from the "Créer la recette" button on the
    # till-products screen, so working through that backlog doesn't mean
    # retyping (and risking mistyping) a name that has to match exactly.
    return _recipe_form_view(request, Recipe(name=(request.GET.get("name") or "").strip()))


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


# --- Till (L'Addition) -----------------------------------------------------

def pos_product_list(request):
    """The backlog of till products with no recipe yet.

    Deliberately the same shape as inventory's review queue: the unmapped
    ones first, biggest sellers at the top (that's where the unexplained
    stock is), with the already-handled ones tucked below.
    """
    products = PosProduct.objects.select_related("recipe")
    pending = [p for p in products if p.needs_review]
    linked = [p for p in products if p.recipe_id]
    ignored = [p for p in products if p.ignored and not p.recipe_id]
    return render(
        request,
        "recipes/pos_product_list.html",
        {
            "pending": pending,
            "linked": linked,
            "ignored": ignored,
            "recipes": Recipe.objects.order_by("name"),
            "pending_quantity": sum(p.total_quantity for p in pending),
        },
    )


def pos_product_assign(request, pk):
    """Link one till product to a recipe, or set it aside.

    Four actions, because those are the four real answers to "what is this
    thing?": it's an existing recipe, it's the happy-hour version of one, it
    needs a recipe of its own, or it isn't something we track.
    """
    if request.method != "POST":
        return redirect("recipes:pos_product_list")
    product = get_object_or_404(PosProduct, pk=pk)
    action = request.POST.get("action")

    if action == "ignore":
        product.ignored = True
        product.recipe = None
        product.save(update_fields=["ignored", "recipe"])
        messages.success(request, f'"{product.name}" ignoré.')

    elif action == "reset":
        product.ignored = False
        product.recipe = None
        product.save(update_fields=["ignored", "recipe"])
        messages.success(request, f'"{product.name}" remis à traiter.')

    elif action == "link":
        recipe = Recipe.objects.filter(pk=request.POST.get("recipe") or 0).first()
        if recipe is None:
            messages.error(request, "Choisissez une recette.")
        else:
            product.recipe = recipe
            product.ignored = False
            product.save(update_fields=["recipe", "ignored"])
            messages.success(request, f'"{product.name}" lié à « {recipe.name} ».')

    elif action == "happy_hour":
        recipe = Recipe.objects.filter(pk=request.POST.get("recipe") or 0).first()
        if recipe is None:
            messages.error(request, "Choisissez la recette de base.")
        else:
            recipe.happy_hour_name = product.name
            try:
                recipe.full_clean()
            except ValidationError as exc:
                messages.error(request, "; ".join(m for msgs in exc.message_dict.values() for m in msgs))
            else:
                recipe.save(update_fields=["happy_hour_name"])
                product.recipe = recipe
                product.ignored = False
                product.save(update_fields=["recipe", "ignored"])
                messages.success(
                    request, f'"{product.name}" enregistré comme happy hour de « {recipe.name} ».'
                )

    return redirect("recipes:pos_product_list")


def sales_import(request):
    """The "fetch sales from the till" page - the sales-side twin of the
    invoice gather screen."""
    job = SalesImportJob.objects.first()
    return render(
        request,
        "recipes/sales_import.html",
        {
            "job": job,
            "default_start": (timezone.localdate() - timedelta(days=30)).isoformat(),
            "default_end": timezone.localdate().isoformat(),
            "pending_count": PosProduct.objects.filter(recipe__isnull=True, ignored=False).count(),
            "last_sale": RecipeSale.objects.order_by("-sold_on").first(),
        },
    )


def trigger_sales_import(request):
    if request.method != "POST":
        return redirect("recipes:sales_import")
    if SalesImportJob.objects.filter(
        status__in=[SalesImportJob.Status.PENDING, SalesImportJob.Status.RUNNING]
    ).exists():
        messages.error(request, "Une récupération est déjà en cours.")
        return redirect("recipes:sales_import")

    start = _parse_date(request.POST.get("start_date"))
    end = _parse_date(request.POST.get("end_date"))
    if not start or not end:
        messages.error(request, "Renseignez les deux dates.")
        return redirect("recipes:sales_import")
    if start > end:
        messages.error(request, "La date de début est après la date de fin.")
        return redirect("recipes:sales_import")

    job = SalesImportJob.objects.create(range_start=start, range_end=end)
    threading.Thread(
        target=import_laddition_sales_task, args=(job.id, start, end), daemon=True
    ).start()
    return redirect("recipes:sales_import")


def sales_import_status(request, job_id):
    return render(
        request,
        "recipes/_sales_import_status.html",
        {"job": get_object_or_404(SalesImportJob, pk=job_id)},
    )


def cancel_sales_import(request, job_id):
    if request.method != "POST":
        return redirect("recipes:sales_import")
    job = get_object_or_404(SalesImportJob, pk=job_id)
    if job.is_active:
        job.cancel_requested = True
        job.save(update_fields=["cancel_requested"])
    return render(request, "recipes/_sales_import_status.html", {"job": job})


def _parse_date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def sales_list(request):
    """Every recorded sale, and a form to add one by hand.

    Manual entries are for what the till never saw - a tab settled off the
    books, a private event. They're stored under their own source, so an
    import can correct its own figures without touching them (see
    RecipeSale's uniqueness constraint).
    """
    if request.method == "POST":
        form = ManualSaleForm(request.POST)
        if form.is_valid():
            sale = form.save()
            messages.success(request, f"{sale.quantity} × {sale.recipe.name} le {sale.sold_on:%d/%m/%Y}.")
            return redirect("recipes:sales_list")
    else:
        form = ManualSaleForm()

    sales = RecipeSale.objects.select_related("recipe").order_by("-sold_on", "recipe__name")[:400]
    totals = RecipeSale.objects.values("source").annotate(
        rows=Count("id"), units=Sum("quantity")
    ).order_by("-units")
    return render(
        request,
        "recipes/sales_list.html",
        {
            "form": form,
            "sales": sales,
            "totals": totals,
            "manual_source": MANUAL_SALE_SOURCE,
        },
    )


def sales_delete(request, pk):
    """Only hand-entered rows can be deleted here - an imported figure is a
    record of what the till reported, and correcting it means re-importing,
    not editing it away."""
    if request.method != "POST":
        return redirect("recipes:sales_list")
    sale = get_object_or_404(RecipeSale, pk=pk)
    if sale.source != MANUAL_SALE_SOURCE:
        messages.error(request, "Seules les ventes saisies à la main peuvent être supprimées ici.")
    else:
        sale.delete()
        messages.success(request, "Vente supprimée.")
    return redirect("recipes:sales_list")
