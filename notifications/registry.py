"""What a rule may name: the events an alert follows (`EVENTS`), the
conditions that skip a reminder (`SKIP_CONDITIONS`) and the pages a
notification may open (`PAGES`).

The keys are stored in the espace's rows (`EventRule.event`,
`Reminder.skip_if`) and the event keys are also URL segments
(`notifications/evenements/<slug:event>/`): slug-safe, and never renamed.

No model is imported here at module level: the skip conditions import theirs
when they run, the pages resolve their URL names when they are asked (a URL
name that does not exist - an app not installed - is simply left out).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.urls import NoReverseMatch, reverse
from django.utils import timezone


@dataclass(frozen=True)
class Outcome:
    key: str
    label: str


@dataclass(frozen=True)
class Event:
    """An event an alert may follow. `page_label` names the page its
    notification opens by default (« le bon »: the one the event is about)."""

    key: str
    label: str
    outcomes: tuple[Outcome, ...]
    default_outcomes: tuple[str, ...]
    page_label: str

    def outcome_label(self, key: str) -> str:
        return next((outcome.label for outcome in self.outcomes if outcome.key == key), key)

    @property
    def outcome_keys(self) -> tuple[str, ...]:
        return tuple(outcome.key for outcome in self.outcomes)


RETURNABLES_COMPARISON = "returnables-comparison"
INVOICES_AUTO_GATHER = "invoices-auto-gather"
RECIPES_AUTO_SALES = "recipes-auto-sales"

EVENTS: dict[str, Event] = {
    RETURNABLES_COMPARISON: Event(
        key=RETURNABLES_COMPARISON,
        label="Consignes : bon du livreur comparé à la reprise",
        outcomes=(
            Outcome("match", "conforme"),
            Outcome("differs", "écart"),
            Outcome("no_pickup", "aucune reprise saisie"),
            Outcome("to_check", "à vérifier"),
        ),
        default_outcomes=("match", "differs", "no_pickup", "to_check"),
        page_label="le bon",
    ),
    INVOICES_AUTO_GATHER: Event(
        key=INVOICES_AUTO_GATHER,
        label="Récupération automatique",
        outcomes=(
            Outcome("new", "du nouveau"),
            Outcome("failed", "une source a échoué"),
            Outcome("nothing", "rien de nouveau"),
        ),
        default_outcomes=("new", "failed"),
        page_label="Factures",
    ),
    RECIPES_AUTO_SALES: Event(
        key=RECIPES_AUTO_SALES,
        label="Import automatique des ventes",
        outcomes=(
            Outcome("new", "ventes importées"),
            Outcome("failed", "échec"),
            Outcome("nothing", "rien de nouveau"),
        ),
        default_outcomes=("failed",),
        page_label="Ventes",
    ),
}


def event(key) -> Event | None:
    return EVENTS.get(key) if isinstance(key, str) else None


@dataclass(frozen=True)
class SkipCondition:
    """A reason not to send a reminder. `check(now, hours, supplier_id)`
    answers the history's French reason (« sauté : … ») or "" to send."""

    key: str
    #: The sentence of the rule (« Ne pas envoyer si … N dernières heures »).
    label: str
    #: The select's option on the reminder form.
    choice_label: str
    check: Callable[..., str]
    #: The « Prochains envois » line: what skips, from which local time.
    preview: Callable[..., str]


def _recent_pickup(now: datetime, hours: int, supplier_id: int | None = None) -> str:
    """A reprise CREATED in the last `hours` hours (by `supplier_id` when
    given). Its created_at only: re-dating an old reprise or adding a photo
    to it bumps updated_at, and must not silence tonight's reminder."""
    from returnables.models import Pickup

    pickups = Pickup.objects.filter(created_at__gte=now - timedelta(hours=hours))
    if supplier_id:
        pickups = pickups.filter(supplier_id=supplier_id)
    latest = pickups.order_by("-created_at").values_list("created_at", flat=True).first()
    if latest is None:
        return ""
    return f"sauté : une reprise a été enregistrée à {timezone.localtime(latest):%H:%M}"


def _recent_pickup_preview(since: datetime, supplier_name: str = "") -> str:
    """« sauté si une reprise UBA a été saisie depuis 18:00 »."""
    who = f" {supplier_name}" if supplier_name else ""
    return f"sauté si une reprise{who} a été saisie depuis {timezone.localtime(since):%H:%M}"


RECENT_PICKUP = "returnables.recent_pickup"

SKIP_CONDITIONS: dict[str, SkipCondition] = {
    RECENT_PICKUP: SkipCondition(
        key=RECENT_PICKUP,
        label="Ne pas envoyer si une reprise de consignes a été enregistrée dans les N dernières heures",
        choice_label="une reprise de consignes a été enregistrée",
        check=_recent_pickup,
        preview=_recent_pickup_preview,
    ),
}

NO_SKIP_LABEL = "— toujours envoyer —"


def skip_condition(key) -> SkipCondition | None:
    return SKIP_CONDITIONS.get(key) if isinstance(key, str) else None


def skip_choices() -> list[tuple[str, str]]:
    """The « Ne pas envoyer si… » select."""
    return [("", NO_SKIP_LABEL), *((c.key, c.choice_label) for c in SKIP_CONDITIONS.values())]


@dataclass(frozen=True)
class Page:
    """A page a notification may open: its URL name, and a fragment to land
    on (« #new-pickup »: the form of a new reprise)."""

    key: str
    label: str
    url_name: str
    fragment: str = ""


PAGES: tuple[Page, ...] = (
    Page("consignes-reprise", "Consignes — nouvelle reprise", "returnables:home", "#new-pickup"),
    Page("consignes", "Consignes", "returnables:home"),
    Page("factures", "Factures", "invoices:invoice_list"),
    Page("stock", "Stock", "inventory:stock_list"),
    Page("inventaires", "Inventaires", "inventory:stock_take_list"),
    Page("banque", "Banque", "bank:bank_home"),
    Page("recettes", "Recettes & ventes", "recipes:recipe_list"),
    Page("marges", "Marges", "margins:margins_home"),
    Page("personnel", "Personnel", "staff:home"),
    Page("donnees", "Données", "transfer:data_home"),
    Page("notifications", "Notifications", "notifications:home"),
)

#: The choice whose path is typed in the form (« Autre page du site… »).
OTHER_PAGE = "autre"
OTHER_PAGE_LABEL = "Autre page du site…"


def page_path(page: Page) -> str:
    """The page's local path with its fragment, "" when its URL name does not
    exist."""
    try:
        return reverse(page.url_name) + page.fragment
    except NoReverseMatch:
        return ""


def available_pages() -> list[tuple[Page, str]]:
    """(page, path) of every page whose URL name exists, in PAGES' order."""
    return [(page, path) for page in PAGES if (path := page_path(page))]


def page_choices() -> list[tuple[str, str]]:
    """The « Page à ouvrir » select: the pages that exist, then « Autre page
    du site… »."""
    return [*((page.key, page.label) for page, _ in available_pages()), (OTHER_PAGE, OTHER_PAGE_LABEL)]


def page_target(key) -> str:
    """The path of a page key, "" for an unknown one (or « autre »)."""
    page = next((page for page in PAGES if page.key == key), None)
    return page_path(page) if page is not None else ""


def page_key_for(target) -> str:
    """The page key a stored path is, or OTHER_PAGE: what the form selects
    when it draws a saved rule."""
    target = str(target or "")
    return next((page.key for page, path in available_pages() if path == target), OTHER_PAGE)
