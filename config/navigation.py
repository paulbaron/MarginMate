"""The top navigation: which workspace a page belongs to, and what waits in
each one.

Three workspaces replaced eight pages - "Produits & charges" (everything
bought, ranged by article, the charges, and the products to classify),
"Achats" (invoices, tickets, their sources and suppliers) and "Recettes &
ventes" (recipes, till products, sales) - so a page's link is decided here,
once, by its app and view, rather than by a list of view names repeated in
the template for every link.

Beside them, links of their own: Banque, Marges, Personnel, Inventaires and
« Consignes » (the empties handed back to the delivery driver, app
`returnables`, URL namespace "returnables"). A page lights the link of its
URL namespace - `match.app_name`, the `app_name` of its app's urls.py - so a
new app lights nothing until it is in SECTION_BY_APP.
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

SECTION_BY_APP = {
    "invoices": "achats",
    "recipes": "recettes",
    "bank": "banque",
    "margins": "marges",
    "transfer": "donnees",
    "staff": "personnel",
    "returnables": "consignes",
}

#: What the folded topbar says under 860 px (base.html's .topbar-section):
#: the words of the link a page lights. The links keep their own words in
#: base.html; tests/test_navigation.py checks each page shows its lit link's.
SECTION_LABELS = {
    "produits": "Produits & charges",
    "achats": "Achats",
    "banque": "Banque",
    "recettes": "Recettes & ventes",
    "marges": "Marges",
    "personnel": "Personnel",
    "inventaires": "Inventaires",
    "consignes": "Consignes",
    "donnees": "Données",
}


def section_of(match) -> str:
    if match is None:
        return ""
    if match.app_name == "inventory":
        return "inventaires" if match.url_name in STOCK_TAKE_VIEWS else "produits"
    return SECTION_BY_APP.get(match.app_name, "")


def navigation(request):
    from accounts.tenancy import current_tenant
    from recipes.models import PosProduct

    if current_tenant() is None:
        # Multi mode, no espace bound (the login, 404 and CSRF pages): no
        # section to light, no database to count in.
        return {}
    section = section_of(getattr(request, "resolver_match", None))
    return {
        "nav_section": section,
        "nav_section_label": SECTION_LABELS.get(section, ""),
        "pos_pending_count_nav": PosProduct.objects.filter(recipe__isnull=True, ignored=False).count(),
    }
