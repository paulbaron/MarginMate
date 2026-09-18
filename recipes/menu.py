"""The "Recettes & ventes" page: recipes, the till products to link to one,
and sales, in one place.

It was three pages - recipes, till products, sales (and a fourth to import
them) - and writing a recipe for something the till sells meant going from
one to the other and back. Now three tabs of one page, each at its old
address (views.recipe_list, pos_product_list, sales_list, sales_import); a
till product is linked where it is listed, and a recipe is created for, and
linked to, the till product it is for.
"""

import re
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from .forms import MANUAL_SALE_SOURCE, ManualSaleForm
from .links import suggest_recipe
from .models import (
    PosProduct,
    Recipe,
    RecipeSale,
    SaleDocument,
    SalesImportJob,
    variation_scope,
)


def pending_count() -> int:
    return PosProduct.objects.filter(recipe__isnull=True, ignored=False).count()


def render_menu(request, tab, *, status=200, **extra):
    """The page on `tab` ("recettes", "a-lier", "ventes"); `extra` goes to the
    tab (a bound sale form with its errors)."""
    to_link = pending_count()
    tabs = [
        {"key": "recettes", "label": "Recettes", "url": reverse("recipes:recipe_list"),
         "count": Recipe.objects.count(), "attention": False},
        {"key": "a-lier", "label": "À lier", "url": reverse("recipes:pos_product_list"),
         "count": to_link, "attention": bool(to_link)},
        {"key": "ventes", "label": "Ventes", "url": reverse("recipes:sales_list"), "count": None, "attention": False},
    ]
    for entry in tabs:
        entry["active"] = entry["key"] == tab
    context = {"tab": tab, "tabs": tabs, "to_link_count": to_link}
    if tab == "ventes":
        extra.setdefault("query", request.GET.get("vente", ""))
        extra.setdefault("show_all", request.GET.get("ventes") == "toutes")
    builders = {"recettes": _recipes, "a-lier": _to_link, "ventes": _sales}
    context.update(builders[tab](**extra))
    return render(request, "recipes/menu.html", context, status=status)


def _recipes() -> dict:
    # Every recipe's ingredients in one extra query, so summary() below
    # never goes back to the database per row.
    recipes = list(Recipe.objects.prefetch_related("ingredients__stock_type__movements", "ingredients__sub_recipe"))
    with variation_scope():
        for recipe in recipes:
            # summary() is linear in the number of ingredients, so a recipe
            # with a million variations costs the same here as one with two.
            recipe.summary_data = recipe.summary(list(recipe.ingredients.all()))
    till_names: dict[int, list[str]] = {}
    for recipe_id, name in PosProduct.objects.filter(recipe__isnull=False).order_by("name").values_list(
        "recipe_id", "name"
    ):
        till_names.setdefault(recipe_id, []).append(name)
    units_sold = dict(RecipeSale.objects.values("recipe_id").annotate(units=Sum("quantity")).values_list("recipe_id", "units"))
    for recipe in recipes:
        recipe.till_names = till_names.get(recipe.pk, [])
        recipe.units_sold = units_sold.get(recipe.pk, 0)
    return {"recipes": recipes, "till_names": till_names, "units_sold": units_sold}


def with_suggestion(product, recipes):
    product.suggested_recipe, product.suggested_happy_hour = suggest_recipe(product.name, recipes)
    return product


def _to_link() -> dict:
    """The backlog of till products with no recipe yet - biggest sellers first,
    since that's where the unexplained stock is - with the handled ones
    folded away below."""
    products = list(PosProduct.objects.select_related("recipe"))
    recipes = list(Recipe.objects.order_by("name"))
    pending = [with_suggestion(product, recipes) for product in products if product.needs_review]
    return {
        "pending": pending,
        "linked": [product for product in products if product.recipe_id],
        "ignored": [product for product in products if product.ignored and not product.recipe_id],
        "recipes": recipes,
        "pending_quantity": sum(product.total_quantity for product in pending),
    }


def _sales_matching(query: str):
    """What a typed search means on the sales: a recipe, a date as it is
    written (12/07/2026, 07/2026, 2026), or where the sale came from."""
    matches = Q(recipe__name__icontains=query) | Q(source__icontains=query)
    written = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", query)
    month = re.fullmatch(r"(\d{1,2})[/.-](\d{4})", query)
    if written:
        day, month_of, year = (int(part) for part in written.groups())
        matches |= Q(sold_on__day=day, sold_on__month=month_of, sold_on__year=year)
    elif month:
        month_of, year = (int(part) for part in month.groups())
        matches |= Q(sold_on__month=month_of, sold_on__year=year)
    elif re.fullmatch(r"(19|20)\d{2}", query):
        matches |= Q(sold_on__year=int(query))
    return matches


#: How many sales the page draws before it asks to be asked. All 8 099 of
#: them was 2,6 Mo of HTML on one page, and they only ever grow.
SALES_PAGE_SIZE = 300


def _sales(form=None, query: str = "", show_all: bool = False) -> dict:
    """The recent sales, the till import, and a form to add one by hand.

    The list was left whole because the table's own box only searches what is
    rendered - so the search is the database's now (a recipe, a date, an
    origin), as on the Achats list, and the page can stop drawing everything.
    """
    documents = list(
        SaleDocument.objects.prefetch_related("lines__recipe", "lines__stock_type").order_by("-sold_on")[:50]
    )
    sales = RecipeSale.objects.select_related("recipe").order_by("-sold_on", "recipe__name")
    query = query.strip()
    if query:
        sales = sales.filter(_sales_matching(query))
    counted = sales.count()
    shown = list(sales if show_all else sales[:SALES_PAGE_SIZE])
    totals = RecipeSale.objects.values("source").annotate(rows=Count("id"), units=Sum("quantity")).order_by("-units")
    return {
        "form": form or ManualSaleForm(),
        "sales": shown,
        "sales_query": query,
        "sales_found": counted if query else None,
        "sales_hidden": max(counted - len(shown), 0),
        "totals": totals,
        "manual_source": MANUAL_SALE_SOURCE,
        "documents": documents,
        # The import from the till.
        "job": SalesImportJob.objects.first(),
        "default_start": (timezone.localdate() - timedelta(days=30)).isoformat(),
        "default_end": timezone.localdate().isoformat(),
        "last_sale": RecipeSale.objects.order_by("-sold_on").first(),
    }
