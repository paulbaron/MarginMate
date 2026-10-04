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


def counted_when_drawn(count):
    """A badge's number, `count()` run only when a template reads it: base.html
    draws the badges, but every template rendered with the request runs the
    processors - the job cards polled every second among them, which threw
    away a scan of the invoice table (5-20 ms of a 7 ms poll). `{% if %}`,
    `{{ }}` and == take it for the int - not int() nor arithmetic. Read
    under another binding than the processor's - another bar's queue, or
    none - it refuses (TenancyError). Lives no longer than the request's
    context."""
    from django.utils.functional import SimpleLazyObject

    from accounts.tenancy import TenancyError, current_tenant

    tenant = current_tenant()

    def bound_count():
        current = current_tenant()
        if current is None or current.pk != tenant.pk:
            raise TenancyError(f"A badge of espace {tenant.pk} read outside its request's binding.")
        return count()

    return SimpleLazyObject(bound_count)


def navigation(request):
    from accounts.access import access_of
    from accounts.tenancy import current_tenant
    from recipes.models import PosProduct

    if current_tenant() is None:
        # Multi mode, no tenant bound (the login, 404 and CSRF pages): no
        # section to light, no database to count in.
        return {}
    section = section_of(getattr(request, "resolver_match", None))
    # The till products to link: counted for whoever has the link
    # (accounts/access.py).
    pending = (
        counted_when_drawn(PosProduct.objects.filter(recipe__isnull=True, ignored=False).count)
        if access_of(request).allows("recipes")
        else 0
    )
    return {
        "nav_section": section,
        "nav_section_label": SECTION_LABELS.get(section, ""),
        "pos_pending_count_nav": pending,
    }
