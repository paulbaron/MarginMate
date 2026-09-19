"""The top navigation: which workspace a page belongs to, and what waits in
each one.

Three workspaces replaced eight pages - "Produits & charges" (everything
bought, ranged by article, the charges, and the products to classify),
"Achats" (invoices, tickets, their sources and suppliers) and "Recettes &
ventes" (recipes, till products, sales) - so a page's link is decided here,
once, by its app and view, rather than by a list of view names repeated in
the template for every link.
"""

# Views of the inventory app that belong to "Inventaires"; the rest are
# "Produits & charges".
STOCK_TAKE_VIEWS = {
    "stock_take_list",
    "stock_take_create",
    "stock_take_detail",
    "stock_take_update",
    "stock_take_variance",
}

SECTION_BY_APP = {"invoices": "achats", "recipes": "recettes", "bank": "banque", "transfer": "donnees"}


def section_of(match) -> str:
    if match is None:
        return ""
    if match.app_name == "inventory":
        return "inventaires" if match.url_name in STOCK_TAKE_VIEWS else "produits"
    return SECTION_BY_APP.get(match.app_name, "")


def navigation(request):
    from recipes.models import PosProduct

    return {
        "nav_section": section_of(getattr(request, "resolver_match", None)),
        "pos_pending_count_nav": PosProduct.objects.filter(recipe__isnull=True, ignored=False).count(),
    }
