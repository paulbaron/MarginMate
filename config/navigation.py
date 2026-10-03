"""The top navigation: which workspace a page belongs to, and what waits in
each one.

Three workspaces replaced eight pages - "Produits & charges" (everything
bought, ranged by article, the charges, and the products to classify),
"Factures" (invoices, tickets, their sources and suppliers) and "Recettes &
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
    "stock_gap_filler",
    "stock_gap_filler_add",
    "stock_gap_filler_undo",
    "stock_gap_filler_clear",
    "stock_gap_filler_exclude",
    "stock_gap_filler_include",
    "stock_gap_filler_recent",
}

SECTION_BY_APP = {
    "invoices": "purchases",
    "recipes": "recipes",
    "bank": "bank",
    "margins": "margins",
    "transfer": "data",
    "staff": "staff",
    "returnables": "returnables",
    # « Identifiants » (accounts/credentials.py), the one page of the accounts
    # app with the navigation: a setting of the espace, beside « Données ».
    "accounts": "data",
    # « Notifications » (notifications/), reached from Données' header like
    # « Identifiants »: a setting of the espace, no link of its own. An
    # employee reaches his devices from the topbar's account (base.html),
    # and lights nothing there (`navigation`).
    "notifications": "data",
}

#: Routes lighting another link than their app's: « Accès des employés »
#: is about the employees (accounts/members.py), « Aucune page ouverte »
#: lights nothing - the employee seeing it has no link to light.
SECTION_BY_VIEW = {
    "accounts:members": "staff",
    "accounts:no_access": "",
}

#: What the folded topbar says under 860 px (base.html's .topbar-section):
#: the words of the link a page lights. The links keep their own words in
#: base.html; tests/test_navigation.py checks each page shows its lit link's.
SECTION_LABELS = {
    "products": "Produits & charges",
    "purchases": "Factures",
    "bank": "Banque",
    "recipes": "Recettes & ventes",
    "margins": "Marges",
    "staff": "Personnel",
    "stock_takes": "Inventaires",
    "returnables": "Consignes",
    "data": "Données",
}


def section_of(match) -> str:
    if match is None:
        return ""
    view_name = getattr(match, "view_name", None)
    if view_name in SECTION_BY_VIEW:
        return SECTION_BY_VIEW[view_name]
    if match.app_name == "inventory":
        return "stock_takes" if match.url_name in STOCK_TAKE_VIEWS else "products"
    return SECTION_BY_APP.get(match.app_name, "")


def navigation(request):
    from accounts.access import access_of
    from accounts.tenancy import current_tenant
    from recipes.models import PosProduct

    if current_tenant() is None:
        # Multi mode, no tenant bound (the login, 404 and CSRF pages): no
        # section to light, no database to count in.
        return {}
    access = access_of(request)
    section = section_of(getattr(request, "resolver_match", None))
    if section == "data" and not access.owner:
        # An employee's « Notifications » (his own devices): « Données » is
        # no link of his, there is none to light nor to name.
        section = ""
    # The till products to link: counted for whoever has the link
    # (accounts/access.py).
    pending = PosProduct.objects.filter(recipe__isnull=True, ignored=False).count() if access.allows("recipes") else 0
    return {
        "nav_section": section,
        "nav_section_label": SECTION_LABELS.get(section, ""),
        "pos_pending_count_nav": pending,
    }
