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

from django.db.models import Count, Prefetch, Q, Sum
from django.shortcuts import render
from django.template import Context
from django.template.base import render_value_in_context
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe

from common import RANGE_END, RANGE_START, DateRange, date_range, is_id
from inventory.models import StockMovement, StockType

from .forms import MANUAL_SALE_SOURCE, ManualSaleForm
from .integration import TILL_TO_CONFIGURE, till_allowed
from .links import RecipeSuggester
from .models import (
    PosProduct,
    Recipe,
    RecipeSale,
    SaleDocument,
    SalesImportJob,
    variation_scope,
)
from .usage import article_uses


def pending_count() -> int:
    return PosProduct.objects.filter(recipe__isnull=True, ignored=False).count()


#: Everything the sales tab reads out of its own address: the window, the
#: search, and whether the list was unfolded. An action posted from the tab
#: is given these back, and nothing else.
SALES_TAB_PARAMS = (RANGE_START, RANGE_END, "vente", "ventes")

#: The recipes tab reads which article the list is filtered on from its own
#: address, so the filter is a link that can be sent, bookmarked and gone
#: back to.
ARTICLE_PARAM = "article"

#: What StockType.current_unit_cost_ht reads of a movement (its quantity and
#: its unit cost; the article it belongs to, for the prefetch to file it).
#: Reading another field of one loaded with only these is a query per
#: movement, which the recipes tab's query-count tests would show.
COST_FIELDS = ("stock_type", "quantity", "unit_cost_ht")


def sales_list_url(request) -> str:
    """« Recettes & ventes · Ventes » as the reader had it - period, search
    and « tout afficher » kept.

    Every action on that tab comes back here. Sent back to the bare address,
    a period typed by hand vanished on the first « Supprimer » or on adding
    a sale by hand, and the page returned with every sale ever recorded in
    it: nothing on screen said the dates had been dropped, so the dates read
    as broken.

    Only the four parameters above travel: an action is posted to whatever
    address the reader was on, and a redirect echoing that query string whole
    would hand back anything anyone had hung on it.
    """
    url = reverse("recipes:sales_list")
    kept = {name: value for name in SALES_TAB_PARAMS if (value := request.GET.get(name))}
    return f"{url}?{urlencode(kept)}" if kept else url


def render_menu(request, tab, *, status=200, **extra):
    """The page on `tab` ("recipes", "to-link", "sales"); `extra` goes to the
    tab (a bound sale form with its errors)."""
    to_link = pending_count()
    # The « du … au … » window is the sales tab's, and is read before the
    # tabs so that tab's own link can carry it: clicking « Ventes » from the
    # sales page would otherwise silently mean « and now show all 8 000 ».
    window = date_range(request) if tab == "sales" else DateRange()
    sales_url = reverse("recipes:sales_list")
    if window:
        sales_url = f"{sales_url}?{urlencode(window.parameters)}"
    # The article filter is the recipes tab's own, and is read here rather
    # than in the builder so that tab's link can carry it: clicking
    # « Recettes » while a filter is on would otherwise silently mean « and
    # now show me all of them ». Built here too, never pasted together in
    # the template - that is exactly where a parameter gets forgotten.
    # Only an id travels: garbage in the query string is no filter, and a
    # link handing it back would make a stale bookmark permanent.
    article = request.GET.get(ARTICLE_PARAM, "") if tab == "recipes" else ""
    recipes_url = reverse("recipes:recipe_list")
    if is_id(article):
        recipes_url = f"{recipes_url}?{urlencode({ARTICLE_PARAM: article})}"
    tabs = [
        {
            "key": "recipes",
            "label": "Recettes",
            "url": recipes_url,
            "count": Recipe.objects.count(),
            "attention": False,
        },
        {
            "key": "to-link",
            "label": "À lier",
            "url": reverse("recipes:pos_product_list"),
            "count": to_link,
            "attention": bool(to_link),
        },
        {"key": "sales", "label": "Ventes", "url": sales_url, "count": None, "attention": False},
    ]
    for entry in tabs:
        entry["active"] = entry["key"] == tab
    context = {"tab": tab, "tabs": tabs, "to_link_count": to_link}
    if tab == "recipes":
        extra.setdefault("article", article)
    if tab == "sales":
        extra.setdefault("query", request.GET.get("vente", ""))
        extra.setdefault("show_all", request.GET.get("ventes") == "toutes")
        extra.setdefault("window", window)
    builders = {"recipes": _recipes, "to-link": _to_link, "sales": _sales}
    context.update(builders[tab](**extra))
    return render(request, "recipes/menu.html", context, status=status)


def _recipes(article: str = "") -> dict:
    """The recipes, and the picker that narrows them to one article.

    `article` is `?article=` exactly as it arrived. Anything that is not an
    id, and any article the picker does not offer, is **the whole list**:
    these come from a query string, so a stale bookmark and a hand-typed URL
    both land here, and an empty page under a filter nobody can see reads as
    a page that has broken rather than as a question with no answer. Where
    the article exists but no recipe uses it, the page says so by name.
    """
    # Every recipe's ingredients in one extra query, so summary() below
    # never goes back to the database per row - and of their articles'
    # movements, thousands of them, only what an article's cost is worked
    # out from (StockType.current_unit_cost_ht).
    recipes = list(
        Recipe.objects.prefetch_related(
            Prefetch("ingredients__stock_type__movements", queryset=StockMovement.objects.only(*COST_FIELDS)),
            "ingredients__sub_recipe",
        )
    )
    with variation_scope() as scope:
        # Every recipe's groups, from the prefetch above, for the scope to
        # hand out: inside summary() a sub-recipe is asked for its own (its
        # count, its cost range), and each ask was two queries - its
        # ingredients, then their movements - per sub-recipe.
        for recipe in recipes:
            scope["groups"].setdefault(recipe.pk, recipe.choice_groups(list(recipe.ingredients.all())))
        for recipe in recipes:
            # summary() is linear in the number of ingredients, so a recipe
            # with a million variations costs the same here as one with two.
            recipe.summary_data = recipe.summary(list(recipe.ingredients.all()))
        # Asked over every recipe, filtered or not: the picker has to offer
        # the articles of the recipes the filter is hiding, or choosing one
        # would be the only way back to the rest.
        uses = article_uses(recipes)
    articles = list(StockType.objects.filter(pk__in=uses.keys()).order_by("name"))
    choices = [{"article": item, "count": len(uses[item.pk])} for item in articles]
    chosen = unused = None
    if is_id(article):
        wanted = int(article)
        chosen = next((item for item in articles if item.pk == wanted), None)
        if chosen is None:
            unused = StockType.objects.filter(pk=wanted).first()
    maybe_count = 0
    if chosen is not None:
        ways = uses[chosen.pk]
        recipes = [recipe for recipe in recipes if recipe.pk in ways]
        for recipe in recipes:
            recipe.article_uses = ways[recipe.pk]
            # One certain way is enough: a recipe using sugar directly AND
            # offering a syrup that may carry more of it still uses sugar.
            recipe.article_certain = any(use.certain for use in recipe.article_uses)
        maybe_count = sum(1 for recipe in recipes if not recipe.article_certain)
    till_names: dict[int, list[str]] = {}
    for recipe_id, name in (
        PosProduct.objects.filter(recipe__isnull=False).order_by("name").values_list("recipe_id", "name")
    ):
        till_names.setdefault(recipe_id, []).append(name)
    units_sold = dict(
        RecipeSale.objects.values("recipe_id").annotate(units=Sum("quantity")).values_list("recipe_id", "units")
    )
    for recipe in recipes:
        recipe.till_names = till_names.get(recipe.pk, [])
        recipe.units_sold = units_sold.get(recipe.pk, 0)
    return {
        "recipes": recipes,
        "till_names": till_names,
        "units_sold": units_sold,
        "article_choices": choices,
        "chosen_article": chosen,
        "unused_article": unused,
        "article_count": len(recipes) if chosen is not None else 0,
        "article_maybe_count": maybe_count,
        "all_recipes_url": reverse("recipes:recipe_list"),
    }


def _as_printed(value) -> str:
    """`value` exactly as {{ value }} prints it in a page: localised, then
    escaped."""
    return render_value_in_context(value, Context())


#: An id no row has, to find where an id sits in an address.
_PROBE_ID = 9_876_543_210_123


def url_for_each(name: str):
    """reverse(name, args=[pk]) as a function of the pk, for the price of one
    reverse() - built per request, like any address.

    A list drawing an address per row paid a reverse() per row: thousands on
    the sales tab drawn whole, most of its time. An <int:pk> address is the
    same text around the pk whatever the pk (Django writes it with str()), so
    it is cut once around an id no row has, and each pk put in between.
    """
    probe = str(_PROBE_ID)
    before, found, after = reverse(name, args=[_PROBE_ID]).partition(probe)
    if not found or probe in after:
        return lambda pk: reverse(name, args=[pk])
    return lambda pk: f"{before}{pk}{after}"


#: One turn of the loop _pos_row.html drew the recipes with, its whitespace
#: included, so the row is byte for byte what that loop printed.
RECIPE_OPTION = '\n                        <option value="{value}"{selected}>{name}</option>\n                    '


class ToLinkRows:
    """What each row of « À lier » proposes - the recipe its till name most
    likely is, chosen in a <select> of every recipe - worked out once per
    request.

    Every row lists the same recipes: a hundred rows of sixty recipes were
    thousands of turns of a template loop and, with every recipe measured
    against every row's name, most of the page's time. Each <option> is
    printed once here, chosen and not, its value and name escaped as the
    loop printed them, and a row joins them. Its addresses are built here
    too (`url_for_each`).
    """

    def __init__(self, recipes):
        recipes = list(recipes)
        self.suggest = RecipeSuggester(recipes)
        self._assign_url = url_for_each("recipes:pos_product_assign")
        self._recipe_url = url_for_each("recipes:recipe_detail")
        self._create_url = reverse("recipes:recipe_create")
        self._options = []
        for recipe in recipes:
            value, name = _as_printed(recipe.pk), _as_printed(recipe.name)
            self._options.append(
                (
                    recipe.pk,
                    RECIPE_OPTION.format(value=value, selected="", name=name),
                    RECIPE_OPTION.format(value=value, selected=" selected", name=name),
                )
            )

    def prepare(self, product):
        """`product` with its suggestion, its row's options and addresses."""
        product.suggested_recipe, product.suggested_happy_hour = self.suggest(product.name)
        chosen = product.suggested_recipe.pk if product.suggested_recipe is not None else None
        # Safe: every name and value in it was escaped by _as_printed.
        product.recipe_options = mark_safe(
            "".join(selected if pk == chosen else unchosen for pk, unchosen, selected in self._options)
        )
        product.create_url = f"{self._create_url}?caisse={_as_printed(product.pk)}"
        return self.address(product)

    def address(self, product):
        """`product` with the addresses its row links to: its own actions,
        and the recipe it is linked to."""
        product.assign_url = self._assign_url(product.pk)
        if product.recipe_id:
            product.recipe_url = self._recipe_url(product.recipe_id)
        return product


def with_suggestion(product, recipes):
    """One row's proposal - for the row a link answers with."""
    return ToLinkRows(recipes).prepare(product)


def _to_link() -> dict:
    """The backlog of till products with no recipe yet - biggest sellers first,
    since that's where the unexplained stock is - with the handled ones
    folded away below."""
    products = list(PosProduct.objects.select_related("recipe"))
    recipes = list(Recipe.objects.order_by("name"))
    rows = ToLinkRows(recipes)
    pending = [rows.prepare(product) for product in products if product.needs_review]
    linked = [rows.address(product) for product in products if product.recipe_id]
    ignored = [rows.address(product) for product in products if product.ignored and not product.recipe_id]
    return {
        "pending": pending,
        "linked": linked,
        "ignored": ignored,
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


#: How many sales the page draws before it asks to be asked. Every one of
#: them was megabytes of HTML on a single page, and they only ever grow.
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
        sale_documents.prefetch_related("lines__recipe", "lines__stock_type").order_by("-sold_on")[:DOCUMENTS_PAGE_SIZE]
    )
    documents_found = sale_documents.count()
    recorded = window.limit(RecipeSale.objects.all(), "sold_on")
    sales = recorded.select_related("recipe").order_by("-sold_on", "recipe__name")
    query = query.strip()
    if query:
        sales = sales.filter(_sales_matching(query))
    counted = sales.count()
    shown = list(sales if show_all else sales[:SALES_PAGE_SIZE])
    recipe_url = url_for_each("recipes:recipe_detail")
    for sale in shown:
        sale.recipe_url = recipe_url(sale.recipe_id)
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
        # The import from the till - offered only where the server's account
        # may be used (recipes/integration.py); elsewhere « à configurer ».
        "till_allowed": till_allowed(),
        "till_to_configure": TILL_TO_CONFIGURE,
        "job": SalesImportJob.objects.first(),
        "default_start": (timezone.localdate() - timedelta(days=30)).isoformat(),
        "default_end": timezone.localdate().isoformat(),
        "last_sale": RecipeSale.objects.order_by("-sold_on").first(),
    }
