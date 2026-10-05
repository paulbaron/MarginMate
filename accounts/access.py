"""What an employee may open: the employer's choice, page by page (the
owner, 02/10/2026: « séparer les opérations employés et employeur … choisir
depuis la page employeur à quelles pages l'employé peut avoir accès »).

An OWNER of the espace opens everything, as he always did. A MEMBER - an
employee the owner invited from « Accès des employés » (accounts/members.py)
- opens only the AREAS his membership lists (`Membership.pages`), each a
group of pages the owner ticks as one box (`AREAS`). What no area opens is
the owner's alone (`OWNER_ONLY_PAGES`): « Données », « Identifiants »,
« Accès des employés », and inside the areas whatever reaches beyond the
bar's daily work - the sources of invoices (a mailbox search shows every
sender and subject), the slips' formats (they decide which PDF Achats files
as a slip), the timesheets' signatures (an employee would sign as his
employer), deleting a stock take, the reminders, alerts and automatic
gathers and sales imports (they write to every phone, search the mailbox
and sign in to the till on their own). Each login's own notification
devices are every login's. The admin keeps its own gate (superusers,
accounts/admin_site.py).

**Deny by default.** Every route of the project is named here, through its
app's areas (`APP_AREAS`) or its own (`VIEW_AREAS`); a route named nowhere
is the owner's, and accounts/tests/test_access.py lists every route so a new
one is classified on purpose. What a route alone cannot say is decided where
it is asked: a stored file by the folder it RESOLVES to (`areas_of_file`,
here), a gather by its sources and an import by who sent it
(invoices/views.py, with `access_of`).

**The gate is `AccessMiddleware.process_view`**, after the login and the
tenant's binding (settings.MIDDLEWARE): it sees the resolved route and the
membership TenantMiddleware read - in the same query as the tenant, so a
request asks the accounts database nothing more (test_middleware pins 3).
It fails closed: a logged-in request reaching a non-public page with no
access read is refused. A refused page is drawn inside base.html, the
employee's own links on top; « / » (the login's landing, the brand, the
error pages' « Revenir à l'accueil ») takes him to his first page instead
(`Access.home_url`); htmx is sent there too (a poll refused would ask again
every second).

**Hiding a link is never the boundary**: the templates draw the links an
employee may follow (`can`, the context processor below), but every refusal
is the gate's. A request that went through no membership - anonymous, a
public view, a request built by hand in a test - draws every link as it
always did (`FULL`).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse

#: The route « / » answers: an employee who may not open it is sent to his
#: own first page (`Access.home_url`), never refused.
HOME = "inventory:stock_list"


@dataclass(frozen=True)
class Area:
    """One box of « Accès des employés »: a group of pages opened together."""

    key: str
    label: str
    help: str
    #: Where the area opens: the route of its first page.
    entry: str


#: In the order the owner reads them, the ones an employee is most often
#: given first. The keys are stored (`Membership.pages`): never renamed.
#: Each help says what the area shows that an owner may not want shown.
AREAS = (
    Area(
        "invoices_add",
        "Ajouter des factures",
        "Photographier ou déposer tickets et factures ; l'employé ne voit que ses propres envois.",
        "invoices:invoice_add",
    ),
    Area(
        "stock_takes",
        "Faire un inventaire",
        "Compter le stock et corriger les inventaires ; les prix d'achat n'y sont montrés que si une page "
        "cochée plus bas les montre déjà.",
        "inventory:stock_take_list",
    ),
    Area(
        "returnables",
        "Consignes",
        "Saisir les reprises (photos, comptes), déposer et lire les bons du livreur, avec les numéros et avoirs "
        "des factures comparées.",
        "returnables:home",
    ),
    # Ticked by default, so with the defaults (the owner, 04/10/2026: every
    # employee gets it). Its routes: `_SHOPPING`, below. Beside any other
    # area it is never the start page (`Access.home_url`).
    Area(
        "shopping",
        "Liste de courses",
        "Les listes de courses par enseigne et la prévision des achats : articles, quantités et rythme, sans les prix.",
        "inventory:shopping_lists",
    ),
    Area(
        "invoices",
        "Factures : tout consulter et corriger",
        "Toutes les factures et leurs montants, leur vérification et les fournisseurs ; les sources restent à vous.",
        "invoices:invoice_list",
    ),
    Area(
        "stock_gaps",
        "Écarts d'inventaire",
        "Ce qui manque depuis un inventaire, en valeur, et « Combler les écarts ».",
        "inventory:stock_gap_filler",
    ),
    Area(
        "products",
        "Produits & charges",
        "Les articles achetés et leurs prix, les charges et leurs documents, les produits à classer.",
        "inventory:stock_list",
    ),
    Area(
        "recipes",
        "Recettes & ventes",
        "Les recettes avec leur coût et leur marge, la caisse et les ventes.",
        "recipes:recipe_list",
    ),
    Area(
        "bank",
        "Banque",
        "Les relevés, toutes les opérations du compte, la trésorerie et les fichiers des factures payées.",
        "bank:bank_home",
    ),
    Area("margins", "Marges", "Les marges du bar.", "margins:margins_home"),
    Area(
        "staff",
        "Personnel",
        "Les fiches de temps et les coordonnées de tous les salariés ; l'envoi pour signature et la contresignature "
        "restent à vous.",
        "staff:home",
    ),
)
AREA_KEYS = frozenset(area.key for area in AREAS)
#: Ticked for a new employee.
DEFAULT_AREAS = ("invoices_add", "stock_takes", "returnables", "shopping")
#: The areas already showing what articles cost: with one of them, an
#: inventory shows its values too (`Access.sees_costs`).
COST_AREAS = frozenset({"products", "invoices", "recipes", "margins", "stock_gaps"})

#: What an employee never opens, whatever is ticked (said on « Accès des
#: employés »).
OWNER_ONLY_PAGES = (
    "Données",
    "Identifiants",
    "Accès des employés",
    "les sources de factures",
    "les formats et types de consignes",
    "les signatures des fiches de temps",
    "la suppression d'un inventaire",
    "les rappels, alertes, récupérations et imports automatiques",
)

#: No area opens it: the owner's alone.
OWNER_ONLY = frozenset()
#: Every login of the espace opens it.
EVERYONE = frozenset({"*"})

#: Every route of an app opens with these, unless VIEW_AREAS says otherwise.
APP_AREAS = {
    "inventory": frozenset({"products"}),
    "invoices": frozenset({"invoices"}),
    "recipes": frozenset({"recipes"}),
    "bank": frozenset({"bank"}),
    "margins": frozenset({"margins"}),
    "staff": frozenset({"staff"}),
    "returnables": frozenset({"returnables"}),
    "transfer": OWNER_ONLY,
    "accounts": OWNER_ONLY,
    # The reminders, the alerts and the night's hour (notifications/): the
    # owner's - they write to every phone of the espace. Each login's own
    # devices are every login's (VIEW_AREAS).
    "notifications": OWNER_ONLY,
}

_ADDING = frozenset({"invoices", "invoices_add"})
_STOCK_TAKES = frozenset({"stock_takes"})
_STOCK_GAPS = frozenset({"stock_gaps"})
_GATHER = frozenset({"invoices", "returnables"})
_SHOPPING = frozenset({"products", "shopping"})

#: Routes whose areas are not their app's.
VIEW_AREAS = {
    # « Ajouter des factures »: the page, the upload it posts, the import it
    # follows - one its login sent, for one who may only add (invoices/views.py).
    "invoices:invoice_add": _ADDING,
    "invoices:receipt_upload": _ADDING,
    "invoices:receipt_batch_status": _ADDING,
    "invoices:receipt_batch_cancel": _ADDING,
    "invoices:receipt_batch_resume": _ADDING,
    # « Récupérer les bons » of Consignes goes through Achats' gather: a
    # gather of slips only, for one given Consignes and not « Factures »
    # (invoices.views.trigger_gather, gather_status, cancel_gather).
    "invoices:gather": _GATHER,
    "invoices:gather_status": _GATHER,
    "invoices:gather_cancel": _GATHER,
    # A source's form searches the owner's mailbox (« Tester » lists every
    # sender and subject it matches) or types his portals' passwords.
    "invoices:invoice_type_create": OWNER_ONLY,
    "invoices:invoice_type_update": OWNER_ONLY,
    # « Récupération automatique » searches the owner's mailbox on its own,
    # at the hours its rules say (invoices/auto_gather.py); its runs list
    # what each source found.
    "invoices:auto_gathers": OWNER_ONLY,
    "invoices:auto_gather_edit": OWNER_ONLY,
    "invoices:auto_gather_delete": OWNER_ONLY,
    # « Import automatique des ventes » signs in to the till's account,
    # the owner's (recipes/auto_sales.py).
    "recipes:auto_sales": OWNER_ONLY,
    "recipes:auto_sales_edit": OWNER_ONLY,
    "recipes:auto_sales_delete": OWNER_ONLY,
    # « Inventaires »: counting. Pricing a line is what an inventory shows
    # to one who sees what articles cost (Access.sees_costs); deleting a
    # count is the owner's (it froze the stock's value at its date).
    "inventory:stock_take_list": _STOCK_TAKES,
    "inventory:stock_take_create": _STOCK_TAKES,
    "inventory:stock_take_update": _STOCK_TAKES,
    "inventory:stock_take_detail": _STOCK_TAKES,
    "inventory:stock_take_delete": OWNER_ONLY,
    "inventory:value_stock_take_line": COST_AREAS,
    "inventory:stock_take_variance": _STOCK_GAPS,
    "inventory:stock_gap_filler": _STOCK_GAPS,
    "inventory:stock_gap_filler_add": _STOCK_GAPS,
    "inventory:stock_gap_filler_undo": _STOCK_GAPS,
    "inventory:stock_gap_filler_clear": _STOCK_GAPS,
    "inventory:stock_gap_filler_exclude": _STOCK_GAPS,
    "inventory:stock_gap_filler_include": _STOCK_GAPS,
    "inventory:stock_gap_filler_recent": _STOCK_GAPS,
    # « Prévoir les courses », « Rythme d'achat » and the shopping lists open
    # with « Liste de courses » or « Produits & charges ». The forecast's
    # settings and exclusions change it for everybody: they stay its app's
    # (« Produits & charges »), and the pages draw their forms only for a
    # login that may post them (inventory.views.shopping_list's `may_tune`).
    "inventory:shopping_list": _SHOPPING,
    "inventory:shopping_rhythm": _SHOPPING,
    "inventory:shopping_lists": _SHOPPING,
    "inventory:shopping_list_page": _SHOPPING,
    "inventory:shopping_list_add": _SHOPPING,
    "inventory:shopping_list_add_all": _SHOPPING,
    "inventory:shopping_list_item_edit": _SHOPPING,
    "inventory:shopping_list_item_delete": _SHOPPING,
    "inventory:shopping_list_item_tick": _SHOPPING,
    "inventory:shopping_list_finish": _SHOPPING,
    # Old addresses of « Données »'s associations: they only redirect there.
    "inventory:export_associations": OWNER_ONLY,
    "inventory:import_associations": OWNER_ONLY,
    # A slip's format decides which PDF Achats files as a slip, and which
    # mails the slips' gather reads; the types are counted by every pickup.
    "returnables:format_list": OWNER_ONLY,
    "returnables:format_create": OWNER_ONLY,
    "returnables:format_edit": OWNER_ONLY,
    "returnables:format_delete": OWNER_ONLY,
    "returnables:format_reread": OWNER_ONLY,
    "returnables:type_list": OWNER_ONLY,
    "returnables:type_edit": OWNER_ONLY,
    "returnables:type_delete": OWNER_ONLY,
    # « Classer comme » on a slip's line writes the chosen type's motifs.
    "returnables:line_classify": OWNER_ONLY,
    # The timesheets' signatures: the employer's countersignature, the
    # employee's link and code, the proof - an employee would sign for his
    # employer, or for a colleague.
    "staff:signature_send": OWNER_ONLY,
    "staff:month_reopen": OWNER_ONLY,
    "staff:signature_link": OWNER_ONLY,
    "staff:signature_code": OWNER_ONLY,
    "staff:signature_countersign": OWNER_ONLY,
    "staff:signature_cancel": OWNER_ONLY,
    "staff:signature_verify": OWNER_ONLY,
    "staff:signature_file": OWNER_ONLY,
    "staff:signature_delete": OWNER_ONLY,
    "staff:signature_delete_confirm": OWNER_ONLY,
    # The till's file formats decide what an upload writes into the till's
    # sales and payments, which « Entrées d'argent » holds against the bank:
    # an employee could rewrite them (recipes/till_views.py).
    "recipes:till_formats": OWNER_ONLY,
    "recipes:till_format": OWNER_ONLY,
    # An uploaded file writes the till's sales AND payments, which nothing
    # tells from the till's own: an employee could hide a shortfall.
    "recipes:upload_sales_file": OWNER_ONLY,
    # A stored file: by the folder it resolves to (`areas_of_file`).
    "accounts:media": EVERYONE,
    # Pages of every login: « Aucune page ouverte », and the password asked
    # again (the admin's own gate sends a superuser there).
    "accounts:no_access": EVERYONE,
    "accounts:confirm_password": EVERYONE,
    # « Notifications »: each login's own devices (notifications/devices.py
    # - the membership is always the request's). static/js/push_sync.js
    # calls the key and the sync from every logged-in page, an employee's
    # too: refused, his phone would stop receiving his reminders. On the
    # page, a member sees « Cet appareil » and « Mes appareils » only.
    "notifications:home": EVERYONE,
    "notifications:key": EVERYONE,
    "notifications:subscribe": EVERYONE,
    "notifications:sync": EVERYONE,
    "notifications:device_delete": EVERYONE,
    "notifications:test": EVERYONE,
}

#: A stored file's areas, by the top folder its model files it under
#: (invoices.models, returnables.models): an employee given Consignes sees
#: a pickup's photos, never an invoice's PDF. A folder named nowhere is the
#: owner's.
MEDIA_AREAS = {
    "invoices": frozenset({"invoices"}),
    "receipts": frozenset({"invoices"}),
    "consignes": frozenset({"returnables"}),
}


def areas_of_route(view_name: str, app_name: str) -> frozenset:
    """The areas opening a route (any one of them does): its own, else its
    app's, else none - the owner's."""
    if view_name in VIEW_AREAS:
        return VIEW_AREAS[view_name]
    return APP_AREAS.get(app_name, OWNER_ONLY)


def areas_of_file(name) -> frozenset:
    """The areas opening the stored file `name` (accounts.views.media): its
    top folder once the name is resolved the way the file view resolves it -
    « consignes/../invoices/… » and « consignes\\..\\invoices\\… » (the
    server's separator) are invoices. A name climbing out, absolute, or
    naming a drive or a stream (« : ») opens for nobody but the owner."""
    text = str(name).replace("\\", "/")
    if ":" in text:
        return OWNER_ONLY
    clean = posixpath.normpath(text)
    if clean.startswith(("/", "..")) or clean == ".":
        return OWNER_ONLY
    return MEDIA_AREAS.get(clean.split("/", 1)[0].casefold(), OWNER_ONLY)


class Access:
    """What one login may open in its espace: everything (an owner), or the
    areas of its membership. In a template (`can`): ``can.invoices`` is
    whether the area is open, ``can.owner`` whether everything is."""

    def __init__(self, *, owner: bool, areas=()):
        self.owner = owner
        self.areas = AREA_KEYS if owner else frozenset(areas) & AREA_KEYS

    @classmethod
    def of(cls, membership) -> Access:
        from .models import Membership

        if membership.role == Membership.Role.OWNER:
            return cls(owner=True)
        pages = membership.pages if isinstance(membership.pages, list) else []
        return cls(owner=False, areas=[key for key in pages if isinstance(key, str)])

    def __getitem__(self, key):
        # A template's `can.<area>`: a dict lookup first - an unknown key
        # falls through to the attributes (`can.owner`, `can.home_url`).
        if key in AREA_KEYS:
            return key in self.areas
        raise KeyError(key)

    def __repr__(self):
        return "<Access owner>" if self.owner else f"<Access {sorted(self.areas)}>"

    def allows(self, area: str) -> bool:
        return area in self.areas

    def allows_any(self, areas) -> bool:
        return self.owner or "*" in areas or bool(self.areas & areas)

    @property
    def sees_costs(self) -> bool:
        """Whether this login is shown what articles cost: an inventory's
        values, the price of a line being counted."""
        return self.owner or bool(self.areas & COST_AREAS)

    def opens(self, match, kwargs=None) -> bool:
        """Whether this login may open the route `match` (a ResolverMatch) -
        a stored file by the folder it resolves to."""
        if self.owner:
            return True
        if match.view_name == "accounts:media":
            return self.allows_any(areas_of_file((kwargs if kwargs is not None else match.kwargs).get("name", "")))
        return self.allows_any(areas_of_route(match.view_name, match.app_name))

    @property
    def opened(self) -> tuple[Area, ...]:
        return tuple(area for area in AREAS if area.key in self.areas)

    @property
    def home_url(self) -> str:
        """Where this login starts: « / » for an owner, and for anyone given
        « Produits & charges » (the area « / » opens); else the entry of his
        first area in AREAS order, « Liste de courses » passed over beside
        any other - accounts 0005 gave it to every employee, and none of them
        was to start somewhere new (one given the lists alone starts there);
        « Aucune page ouverte » when he has none."""
        if self.owner or "products" in self.areas:
            return reverse(HOME)
        opened = [area for area in self.opened if area.key != "shopping"] or list(self.opened)
        return reverse(opened[0].entry) if opened else reverse("accounts:no_access")


#: What a request with no membership read draws: every link, as before.
FULL = Access(owner=True)


def access_of(request) -> Access:
    """The request's access (TenantMiddleware's), or FULL when it went
    through no membership - a public view, a request built by hand. A
    logged-in request on a page of the espace always has one: the gate
    refuses it otherwise (`AccessMiddleware`)."""
    return getattr(request, "access", None) or FULL


def context(request) -> dict:
    """The context processor: `can`, what the topbar and the pages draw
    links by. Reads nothing from any database."""
    return {"can": access_of(request)}


REFUSED = "Votre employeur ne vous a pas donné accès à cette page."
REFUSED_POST = "Rien n'a été enregistré."


def refused(request, access: Access | None = None):
    """The answer to a page the employee may not open: drawn inside the
    site, his own links on top, with the way back to his first page. htmx
    is sent there (`HX-Redirect`): a poll answered a bare 403 would ask
    again every second."""
    home = access.home_url if access is not None else reverse("accounts:no_access")
    if request.headers.get("HX-Request") == "true":
        response = HttpResponseForbidden()
        response["HX-Redirect"] = home
        return response
    context = {
        "refused": REFUSED,
        "nothing_saved": REFUSED_POST if request.method == "POST" else "",
        "home_url": home,
        # No link lit: the page asked for is none of his.
        "nav_section": "",
        "nav_section_label": "",
    }
    return render(request, "accounts/refused.html", context, status=403)


class AccessMiddleware:
    """The gate (the module's docstring). After TenantMiddleware and
    MessageMiddleware: its pages are drawn bound, with their messages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not getattr(view_func, "login_required", True):
            return None
        match = request.resolver_match
        if match is None or "admin" in match.namespaces:
            # The admin's own gate: superusers, the password asked again.
            return None
        access = getattr(request, "access", None)
        if access is None:
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                # Logged in, on a page of the espace, and no membership was
                # read: never taken for the owner.
                return HttpResponseForbidden()
            return None
        if access.opens(match, view_kwargs):
            return None
        if match.view_name == HOME:
            return redirect(access.home_url)
        return refused(request, access)
