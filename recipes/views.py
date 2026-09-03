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
    SaleDocumentForm,
    SaleDocumentLineFormSet,
    RecipeForm,
    RecipeIngredientFormSet,
    ingredient_unit_map,
)
from .models import (
    PosProduct,
    Recipe,
    RecipeSale,
    SaleDocument,
    SalesImportJob,
    variation_scope,
)
from .tasks import import_laddition_sales_task


def _existing_categories():
    return Recipe.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")

_PIE_COLORS = ["#d99b3f", "#6fbf73", "#e0685f", "#5b9bd9", "#c77dd9", "#d9c73f", "#3fd9c7", "#9fa2ae"]


def _build_ingredient_pie_svg(breakdown: list[dict]) -> str:
    """Each ingredient's share of a recipe's cost, as an inline SVG pie.

    Built from the exact same breakdown cost_ht() sums, so the chart and the
    displayed total can never disagree. Empty string when there's no cost to
    show a proportion of.

    Hand-rolled for the same reason as the price chart (see
    inventory/views.py::_build_price_history_svg): a charting library's only
    real contribution here would be the tooltip, and static/js/charts.js does
    that for both charts at once.

    The result is rendered with |safe, so every name that goes into it is
    escaped here - those come from invoice text and from whatever was typed
    on the stock page, neither of which is trustworthy markup.
    """
    total = sum((entry["cost_ht"] for entry in breakdown), start=Decimal("0"))
    non_zero = [entry for entry in breakdown if entry["cost_ht"] > 0]
    if not non_zero or not total:
        return ""

    size, radius = 220, 100
    cx = cy = size / 2

    slices, legend = [], []
    start_angle = -math.pi / 2  # 12 o'clock
    for index, entry in enumerate(non_zero):
        fraction = float(entry["cost_ht"] / total)
        color = _PIE_COLORS[index % len(_PIE_COLORS)]
        name = escape(entry.get("name") or entry["ingredient"].source_name)
        value = f"{entry['cost_ht']:.2f} € · {fraction * 100:.1f} %"
        common = f'data-index="{index}" data-label="{name}" data-value="{value}" data-color="{color}"'

        if len(non_zero) == 1:
            # A single 100% slice can't be drawn as an arc - the start and end
            # points coincide and the path collapses to nothing.
            slices.append(
                f'<circle class="chart-slice" cx="{cx}" cy="{cy}" r="{radius}" fill="{color}" {common} />'
            )
        else:
            end_angle = start_angle + fraction * 2 * math.pi
            x1, y1 = cx + radius * math.cos(start_angle), cy + radius * math.sin(start_angle)
            x2, y2 = cx + radius * math.cos(end_angle), cy + radius * math.sin(end_angle)
            large_arc = 1 if fraction > 0.5 else 0
            slices.append(
                f'<path class="chart-slice" d="M{cx},{cy} L{x1:.1f},{y1:.1f} '
                f'A{radius},{radius} 0 {large_arc} 1 {x2:.1f},{y2:.1f} Z" fill="{color}" {common} />'
            )
            start_angle = end_angle

        legend.append(
            f'<span class="chart-legend-item" data-legend-for="{index}">'
            f'<span class="swatch" style="background:{color};"></span>{name}'
            f' <span class="muted">{fraction * 100:.1f} %</span></span>'
        )

    return (
        f'<div class="chart chart-pie" data-chart="pie" style="max-width:260px;">'
        f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="Répartition du coût">'
        f'{"".join(slices)}</svg>'
        f'<div class="chart-tooltip" data-chart-tooltip></div>'
        f'<div class="chart-legend">{"".join(legend)}</div>'
        f"</div>"
    )


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

    "Happy hour" is a MODIFIER on linking rather than an action of its own:
    it links to the same recipe, and additionally records this as the name
    the till uses during happy hour so both sets of sales land together.
    Presenting it as a fifth button implied a fifth kind of answer.
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

    elif action in ("link", "happy_hour"):
        # "happy_hour" is still accepted so an old bookmark or a half-submitted
        # form doesn't 400; the checkbox is what the page sends now.
        as_happy_hour = action == "happy_hour" or bool(request.POST.get("as_happy_hour"))
        recipe = Recipe.objects.filter(pk=request.POST.get("recipe") or 0).first()
        if recipe is None:
            messages.error(request, "Choisissez une recette.")
        elif as_happy_hour:
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
        else:
            product.recipe = recipe
            product.ignored = False
            product.save(update_fields=["recipe", "ignored"])
            messages.success(request, f'"{product.name}" lié à « {recipe.name} ».')

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

    documents = list(
        SaleDocument.objects.prefetch_related("lines__recipe", "lines__stock_type").order_by("-sold_on")[:50]
    )
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
            "documents": documents,
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


def pos_products_bulk(request):
    """Set aside several till products at once.

    With a hundred-odd unmatched products after a first import, most of which
    are food, coffee or one-off oddities, doing this a row at a time means a
    hundred full page reloads. Only "ignore" is offered in bulk: linking needs
    a different recipe per product, so there's nothing to batch, and a bulk
    action that quietly linked the wrong ones would be exactly the silent
    mis-attribution the rest of this app works hard to avoid.
    """
    if request.method != "POST":
        return redirect("recipes:pos_product_list")

    names = request.POST.getlist("selected")
    if not names:
        messages.error(request, "Aucun produit sélectionné.")
        return redirect("recipes:pos_product_list")

    updated = PosProduct.objects.filter(name__in=names).update(ignored=True, recipe=None)
    messages.success(request, f"{updated} produit{'s' if updated > 1 else ''} ignoré{'s' if updated > 1 else ''}.")
    return redirect("recipes:pos_product_list")


def sale_document_form(request, pk=None):
    """Create or edit a hand-written sale document.

    Lines can be recipes or stock items sold as themselves; both feed the
    variance report, a recipe through its ingredients and a stock item
    directly.
    """
    document = get_object_or_404(SaleDocument, pk=pk) if pk else SaleDocument()

    if request.method == "POST":
        form = SaleDocumentForm(request.POST, instance=document)
        formset = SaleDocumentLineFormSet(request.POST, instance=document)
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                document = form.save()
                formset.instance = document
                formset.save()
            messages.success(request, f"{document} enregistrée.")
            return redirect("recipes:sales_list")
    else:
        form = SaleDocumentForm(instance=document)
        formset = SaleDocumentLineFormSet(instance=document)

    return render(
        request,
        "recipes/sale_document_form.html",
        {"form": form, "formset": formset, "document": document if document.pk else None},
    )


def sale_document_delete(request, pk):
    if request.method != "POST":
        return redirect("recipes:sales_list")
    document = get_object_or_404(SaleDocument, pk=pk)
    label = str(document)
    document.delete()
    messages.success(request, f"{label} supprimée.")
    return redirect("recipes:sales_list")
