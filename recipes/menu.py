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
from urllib.parse import urlencode

from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from common import RANGE_END, RANGE_START, DateRange, date_range

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


#: Everything the sales tab reads out of its own address: the window, the
#: search, and whether the list was unfolded. An action posted from the tab
#: is given these back, and nothing else.
SALES_TAB_PARAMS = (RANGE_START, RANGE_END, "vente", "ventes")


def sales_list_url(request) -> str:
    """« Recettes & ventes · Ventes » as the reader had it - period, search
    and « tout afficher » kept.

    Every action on that tab comes back here. Sent back to the bare address,
    a period typed by hand vanished on the first « Supprimer » or on adding
    a sale by hand, and the page returned with all 8 099 sales in it: nothing
    on screen said the dates had been dropped, so the dates read as broken.

    Only the four parameters above travel: an action is posted to whatever
    address the reader was on, and a redirect echoing that query string whole
    would hand back anything anyone had hung on it.
    """
    url = reverse("recipes:sales_list")
    kept = {name: value for name in SALES_TAB_PARAMS if (value := request.GET.get(name))}
    return f"{url}?{urlencode(kept)}" if kept else url


def render_menu(request, tab, *, status=200, **extra):
    """The page on `tab` ("recettes", "a-lier", "ventes"); `extra` goes to the
    tab (a bound sale form with its errors)."""
    to_link = pending_count()
    # The « du … au … » window is the sales tab's, and is read before the
    # tabs so that tab's own link can carry it: clicking « Ventes » from the
    # sales page would otherwise silently mean « and now show all 8 000 ».
    window = date_range(request) if tab == "ventes" else DateRange()
    sales_url = reverse("recipes:sales_list")
    if window:
        sales_url = f"{sales_url}?{urlencode(window.parameters)}"
    tabs = [
        {"key": "recettes", "label": "Recettes", "url": reverse("recipes:recipe_list"),
         "count": Recipe.objects.count(), "attention": False},
        {"key": "a-lier", "label": "À lier", "url": reverse("recipes:pos_product_list"),
         "count": to_link, "attention": bool(to_link)},
        {"key": "ventes", "label": "Ventes", "url": sales_url, "count": None, "attention": False},
    ]
    for entry in tabs:
        entry["active"] = entry["key"] == tab
    context = {"tab": tab, "tabs": tabs, "to_link_count": to_link}
    if tab == "ventes":
        extra.setdefault("query", request.GET.get("vente", ""))
        extra.setdefault("show_all", request.GET.get("ventes") == "toutes")
        extra.setdefault("window", window)
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

#: The same for the sale documents, which are listed whole rather than
#: searched. Windowed, the count beside them says what the cap hides.
DOCUMENTS_PAGE_SIZE = 50


def _window_label(window: DateRange) -> str:
    """The window as the page says it, « Du … au … » - or the half of it a
    reader asked for, since either end alone is a window.

    Said in words beside every figure the window narrows: a list cut down to
    two dates without saying so reads as a page that has lost its data.
    """
    if window.start and window.end:
        return f"Du {window.start:%d/%m/%Y} au {window.end:%d/%m/%Y}"
    if window.start:
        return f"Depuis le {window.start:%d/%m/%Y}"
    if window.end:
        return f"Jusqu'au {window.end:%d/%m/%Y}"
    return ""


def _sales(form=None, query: str = "", show_all: bool = False, window: DateRange | None = None) -> dict:
    """The recent sales, the till import, and a form to add one by hand.

    The list was left whole because the table's own box only searches what is
    rendered - so the search is the database's now (a recipe, a date, an
    origin), as on the Achats list, and the page can stop drawing everything.

    `window` narrows everything said about what was SOLD - the sales, the
    sale documents, the totals by origin - on each one's own date, both ends
    included. All three or none: all-time totals standing above a windowed
    list are read as the window's own figures. It has nothing to do with the
    import card's pair of dates, which says what to fetch from the till, and
    `default_start`/`default_end`/`last_sale` below stay outside it.
    """
    window = window or DateRange()
    sale_documents = window.limit(SaleDocument.objects.all(), "sold_on")
    documents = list(
        sale_documents.prefetch_related("lines__recipe", "lines__stock_type").order_by("-sold_on")[
            :DOCUMENTS_PAGE_SIZE
        ]
    )
    documents_found = sale_documents.count()
    recorded = window.limit(RecipeSale.objects.all(), "sold_on")
    sales = recorded.select_related("recipe").order_by("-sold_on", "recipe__name")
    query = query.strip()
    if query:
        sales = sales.filter(_sales_matching(query))
    counted = sales.count()
    shown = list(sales if show_all else sales[:SALES_PAGE_SIZE])
    totals = recorded.values("source").annotate(rows=Count("id"), units=Sum("quantity")).order_by("-units")
    return {
        "form": form or ManualSaleForm(),
        "sales": shown,
        "sales_query": query,
        # Counted over the window as well as the search, since that is what
        # the list beside it shows: counted over the table, the line says
        # « 5 de plus » under a page of one.
        "sales_found": counted if query or window else None,
        "sales_hidden": max(counted - len(shown), 0),
        "show_all": show_all,
        "date_window": window,
        "date_window_label": _window_label(window),
        "totals": totals,
        "manual_source": MANUAL_SALE_SOURCE,
        "documents": documents,
        "documents_found": documents_found,
        "documents_hidden": max(documents_found - len(documents), 0),
        # The import from the till.
        "job": SalesImportJob.objects.first(),
        "default_start": (timezone.localdate() - timedelta(days=30)).isoformat(),
        "default_end": timezone.localdate().isoformat(),
        "last_sale": RecipeSale.objects.order_by("-sold_on").first(),
    }
