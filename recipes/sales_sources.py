"""Where the sales come from: the sites an automatic sales import may fetch
from (recipes/auto_sales.py), in the order its form offers them.

Today one entry, « laddition » (« L'Addition (caisse) »): available where
the espace may fetch from L'Addition with its OWN account - any bound espace
(`integration.till_allowed`), and, outside the platform owner's (whose .env
or « Identifiants » stand, as before), once its « Identifiants » hold the
login and password (`integration.till_login_missing`): a rule whose every
slot could only fail never starts. It runs the EXISTING
`tasks.import_laddition_sales_task` through a SalesImportJob - the same
import the Ventes tab starts, signing in with the espace's account, in the
server's Chrome as the espace may use it (invoices/scrapers/chrome.py), its
log as the espace may read it (common.job_line).

**Adding another site** is an entry here and nothing else in the
scheduling: a key (slug-safe, stored in `AutoSalesImport.source` - never
renamed), its label, `available` (whether this espace may use it) and the
sentence said where it may not, its job's label, `uses_browser` (whether it
needs one of the server's browsers), and `task`, the dotted path
of its own import task - a function `(job_id, start, end)` run in a bound
thread, which downloads that site's sales of [start, end] and records the
same day-level sales the till's do (`recipes.sales.record_sales`,
`PosProductDailyQuantity`), sets the SalesImportJob's status, and ends with
`auto_sales.finish(job, fields=…, …)` as the till's task does: its final
status saved in one transaction with its coverage, then its alert. The rule's form, its page, its slots, its period (the source's own
coverage code `ventes-<key>`) and the one-import-at-a-time start are
shared.

A rule naming a key this registry no longer has is « source inconnue »:
skipped with that result, never a 500.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.utils.module_loading import import_string


@dataclass(frozen=True)
class SalesSource:
    key: str
    label: str
    #: Whether the bound espace may use this site.
    available: Callable[[], bool]
    #: Said where it may not (a sentence).
    unavailable_reason: Callable[[], str]
    #: What its job is called in the log and the running-jobs list.
    job_label: str
    #: The dotted path of its import task, `(job_id, start, end)`.
    task: str
    #: Its task signs in through the server's Chrome (invoices/scrapers/
    #: chrome.py): another bar's slot waits for a free browser rather than
    #: start an import refused at once (auto_sales._start).
    uses_browser: bool = True


def _laddition_ready() -> bool:
    """The till may be used here and its account has a value to sign in
    with (the module's docstring). In the platform owner's espace it is
    `till_allowed()` alone, as on GitHub's main - unlike
    `pos.connectors.LADDITION.ready()`, which draws the Ventes tab's fetch
    card and also wants his account (« Identifiants » or the .env): an owner
    with neither is offered rules whose import fails, and says so in its
    alert, as before."""
    from .integration import till_allowed, till_login_missing

    return till_allowed() and not till_login_missing()


def _laddition_refusal() -> str:
    """Why not: « à configurer » unbound, else the account to type on
    « Identifiants » - never a server variable's name."""
    from .integration import TILL_LOGIN_MISSING, refusal, till_allowed

    return refusal() if not till_allowed() else TILL_LOGIN_MISSING


LADDITION = "laddition"

SOURCES: dict[str, SalesSource] = {
    LADDITION: SalesSource(
        key=LADDITION,
        label="L'Addition (caisse)",
        available=_laddition_ready,
        unavailable_reason=_laddition_refusal,
        job_label="import des ventes de la caisse",
        task="recipes.tasks.import_laddition_sales_task",
    ),
}


def source(key) -> SalesSource | None:
    """The entry of `key`, None for a key this registry does not have."""
    return SOURCES.get(key) if isinstance(key, str) else None


def choices() -> list[tuple[str, str]]:
    """The rule form's « Source » select, in the registry's order."""
    return [(entry.key, entry.label) for entry in SOURCES.values()]


def start(entry: SalesSource, start_day=None, end_day=None, *, trigger: str, auto_rule_id=None, notes=(), plan=None):
    """Start `entry`'s import of [start_day, end_day] - or of what `plan`
    decides under the lock - through the one start of a sales import
    (importing.start_sales_import): its job, None when another sales import
    is running, or the plan's sentence when it found nothing to import."""
    from .importing import start_sales_import

    return start_sales_import(
        start_day,
        end_day,
        trigger=trigger,
        auto_rule_id=auto_rule_id,
        task=import_string(entry.task),
        notes=notes,
        plan=plan,
        source=entry.key,
    )
