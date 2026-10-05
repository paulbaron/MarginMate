"""Automatic sales imports: the « Import automatique des ventes » rules
(AutoSalesImport), run by the scheduler of `manage.py serve`
(notifications/scheduler.py: `run_due` is one of its JOBS, called once a
minute with the espace bound) - separate from the automatic gathers of
invoices and bons (invoices/auto_gather.py), on the same slot machinery
(notifications/automation.py).

What each tick does for the bound espace:

- nothing at all unbound (`integration.till_allowed`: any bound espace
  since 04/10/2026, each fetching with its own « Identifiants » - the
  server's .env stands in for the platform owner's espace alone);
- every active rule, each on its own: its due slot is the latest instant of
  its CALENDAR days × times (no night) in (`last_slot_at` or `created_at`,
  now], caught up within CATCH_UP_LIMIT (12 h: an import is idempotent per
  day, late is fine), claimed, given back, skipped on a dev copy or during
  a deploy exactly as the gathers' (automation.run_rule); then:
  - « sautée : source inconnue » - its source is no key of
    recipes/sales_sources.py any more; « sautée : source indisponible » -
    that source's `available()` says no (another bar whose « Identifiants »
    hold no L'Addition account: nothing signs in, no browser starts) - or
    « en attente : identifiants momentanément illisibles » when they could
    not be read at all for a moment (accounts.vault.BUSY): the slot is given
    back (automation.Retry), not skipped;
  - « à jour : ventes importées jusqu'au JJ/MM » - nothing new to import
    (below): no job, no sign-in to the site. This is what keeps L'Addition's
    sign-ins to about one a day whatever the rule's times;
  - « en attente : un import des ventes est en cours » - another import (by
    hand, automatic, or `manage.py laddition_import` downloading) runs: the
    slot is given back;
  - « en attente : navigateurs du serveur occupés » - another bar's import
    would find every browser of the server taken by the other bars'
    (invoices/scrapers/chrome.py: two for all of them) and be refused at
    once: the slot is given back (automation.Retry), so bars whose rules
    share 07:00 take turns instead of failing every morning. Two imports
    started in the same tick can both find one free: the one the browser
    then refuses gives way (`gave_way`) - its job deleted, its rule's slot
    given back, no alert;
  - « sautée : l'import en cours vient d'échouer ou d'être annulé » - the
    rule was waiting and a sales import has failed or been cancelled since
    its slot: the slot is kept, the next one tries again (the owner's
    « Annuler », a refused sign-in, are not repeated a minute later);
  - else « lancé à HH:MM (import n° N, du JJ/MM au JJ/MM) », through the one
    start of a sales import (importing.start_sales_import, trigger
    automatic).
  « à jour », the skip after a failure and the period are decided UNDER
  the start's lock (its `plan`), after the active check: read before it,
  the period could predate an import that ended in between, and sign in
  again for its days;
- the automatic imports older than 30 days are deleted.

**The period of an automatic import** (`period_for`):

- END is the last COMPLETE till day (`last_complete_day`): yesterday once
  the local time is at or past the espace's « La nuit se termine à »
  (notifications' NotificationSettings.night_ends_at; 00:00, the calendar,
  is yesterday at any hour), else the day before yesterday - the till files
  a sale rung after midnight under the day its service began, so before the
  night ends yesterday is still open.
- What has been imported without a gap is the gathers' coverage
  (invoices.GatherCoverage, invoices/coverage.py) under the code
  `ventes-<key>` (« ventes-laddition »), recorded with the gathers' rules
  (`coverage.searched`) when an import of that source COMPLETES - by hand
  from the Ventes tab too -, never by a failed, cancelled or killed one.
  Its `until` is the last complete day when the import finished (an import
  up to today has not seen tonight's sales). A source never recorded is
  read once from the history: the furthest any finished SUCCESS sales job
  reached (its `range_end`, never past the last complete day when it
  finished) - L'Addition's alone (`SalesImportJob.source`): a till's
  file uploaded on « Ventes » is no proof L'Addition's days were imported.
- Nothing new when the day after the coverage is past END. Otherwise START
  is the coverage less OVERLAP_DAYS (3, re-read: a day imported again
  replaces itself), or - never covered - the source's own start
  (`own_start`: the newest day holding till sales less OVERLAP_DAYS, else
  today − 90 days). No 90-day pending catch-up as for the gathers: one
  window is one export. START is only bounded at today − LOOKBACK_FLOOR_DAYS
  (400), which the job's log says when it cuts it.

How an import ended is said by `finish` (the task's last step): its final
status saved - for a SUCCESS in one transaction with the coverage it
records, so a tick sees the import either running or over with its days
covered -, then for an automatic one the « recipes-auto-sales » alert.
`finished` is the same for a job whose status is already saved.

A « Ventes » clear or « Remplacer » in « Données » lowers the coverage to
the day before the first till day it deletes (transfer/sections/sales.py):
the days deleted are no longer imported.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, time, timedelta

from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from accounts import vault
from accounts.tenancy import server_accounts_allowed
from invoices.scrapers import chrome
from notifications import automation, schedule, webpush

from . import importing, sales_sources
from .integration import till_allowed
from .models import AutoSalesImport, PosProductDailyQuantity, SalesImportJob

logger = logging.getLogger(__name__)

#: A missed slot is caught up while it is younger than this.
CATCH_UP_LIMIT = timedelta(hours=12)
#: Automatic imports are kept this long.
KEEP_RUNS = timedelta(days=30)
#: The days imported again before the last day covered.
OVERLAP_DAYS = 3
#: A source with no sales and no coverage starts this far back.
OWN_DEFAULT_DAYS = 90
#: An automatic import never starts further back.
LOOKBACK_FLOOR_DAYS = 400

WAITING = "en attente : un import des ventes est en cours"
ENDED_WHILE_WAITING = "sautée : l'import en cours vient d'échouer ou d'être annulé"
UNKNOWN_SOURCE = "sautée : source inconnue"
SOURCE_UNAVAILABLE = "sautée : source indisponible"
UP_TO_DATE = "à jour : ventes importées jusqu'au {day:%d/%m}"
STARTED = "lancé à {at} (import n° {pk}, du {start:%d/%m} au {end:%d/%m})"
FLOOR_NOTE = (
    "Début ramené au {floor:%d/%m/%Y} ({days} jours au plus) : les ventes d'avant se récupèrent "
    "à la main depuis l'onglet Ventes."
)
#: The alert's sentences.
ALERT_TITLE = "Import automatique des ventes : {label}"
ALERT_BODY = (
    "Ventes du {start:%d/%m} au {end:%d/%m} : {recorded} totaux recette/jour · "
    "{unmatched} produits de caisse sans recette"
)
ALERT_FAILED = "Échec : {reason}"
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


def coverage_code(key: str) -> str:
    """The source's code in invoices.GatherCoverage: « ventes-laddition »."""
    return f"ventes-{key}"


def night_ends_at() -> time:
    """The espace's « La nuit se termine à », read without writing (a fresh
    espace reads the default)."""
    from notifications.models import NotificationSettings

    row = NotificationSettings.objects.filter(pk=NotificationSettings.SINGLETON_PK).first()
    return row.night_ends_at if row is not None else schedule.DEFAULT_NIGHT_END


def last_complete_day(now, night: time | None = None) -> date:
    """The last till day whose sales are all in (the module's docstring)."""
    night = night_ends_at() if night is None else night
    local = timezone.localtime(now)
    yesterday = local.date() - timedelta(days=1)
    if night == schedule.CALENDAR or local.time() >= night:
        return yesterday
    return yesterday - timedelta(days=1)


def own_start(today: date) -> date:
    """Where a source never covered starts: the newest day holding till
    sales (today at most) less OVERLAP_DAYS, else today − OWN_DEFAULT_DAYS."""
    newest = PosProductDailyQuantity.objects.filter(sold_on__lte=today).aggregate(newest=Max("sold_on"))["newest"]
    if newest is None:
        return today - timedelta(days=OWN_DEFAULT_DAYS)
    return newest - timedelta(days=OVERLAP_DAYS)


def lookback_floor(today: date) -> date:
    return today - timedelta(days=LOOKBACK_FLOOR_DAYS)


def _from_history(night: time, exclude=None, key: str = sales_sources.LADDITION) -> date | None:
    """The furthest the finished SUCCESS sales jobs of `key` reached - each
    its `range_end`, never past the last complete day when it finished - or
    None. `exclude`: a job's pk left out (the one being recorded). A till's
    file uploaded on « Ventes » (`SalesImportJob.FILE`) is none of them."""
    jobs = SalesImportJob.objects.filter(status=SalesImportJob.Status.SUCCESS, range_end__isnull=False, source=key)
    if exclude is not None:
        jobs = jobs.exclude(pk=exclude)
    furthest = None
    for started_at, finished_at, range_end in jobs.values_list("started_at", "finished_at", "range_end").iterator(
        chunk_size=100
    ):
        reach = min(range_end, last_complete_day(finished_at or started_at, night))
        furthest = reach if furthest is None else max(furthest, reach)
    return furthest


def coverage_row(key: str, night: time | None = None, exclude=None):
    """`key`'s coverage row, made the first time it is asked - from the
    sales jobs' history for L'Addition (every job before this was its)."""
    from invoices.models import GatherCoverage

    code = coverage_code(key)
    row = GatherCoverage.objects.filter(code=code).first()
    if row is None:
        night = night_ends_at() if night is None else night
        history = _from_history(night, exclude=exclude, key=key) if key == sales_sources.LADDITION else None
        row, _ = GatherCoverage.objects.get_or_create(code=code, defaults={"searched_until": history})
    return row


def covered_until(key: str) -> date | None:
    """How far `key`'s sales are imported without a gap - read only (the
    page asks it, and a GET writes nothing): the coverage row, else what
    the history says it would be made from."""
    from invoices.models import GatherCoverage

    row = GatherCoverage.objects.filter(code=coverage_code(key)).first()
    if row is not None:
        return row.searched_until
    return _from_history(night_ends_at(), key=key) if key == sales_sources.LADDITION else None


def till_covered_until() -> date | None:
    """How far the till's sales are in without a gap, for a page reading
    them (« Prévoir les courses », inventory/shopping_data.py): L'Addition's
    coverage (`covered_until`), carried on by the till's files uploaded on
    « Ventes » that continue it - each SUCCESS file job starting at most the
    day after, up to its last day, never past the last complete day when it
    finished (`_from_history`'s rule). A bar filling days by file, or one
    that left L'Addition, is read past its last fetch. None while
    L'Addition's coverage is unknown: the page then goes by the sales
    themselves. Read only - L'Addition's own coverage, where its imports
    start, is no file's to move."""
    covered = covered_until(sales_sources.LADDITION)
    if covered is None:
        return None
    night = None
    files = (
        SalesImportJob.objects.filter(
            status=SalesImportJob.Status.SUCCESS,
            source=SalesImportJob.FILE,
            range_start__isnull=False,
            range_end__gt=covered,
        )
        .order_by("range_start", "pk")
        .values_list("range_start", "range_end", "started_at", "finished_at")
    )
    for range_start, range_end, started_at, finished_at in files.iterator(chunk_size=100):
        if range_start > covered + timedelta(days=1):
            break  # a gap: what comes after it is not contiguous
        night = night_ends_at() if night is None else night
        covered = max(covered, min(range_end, last_complete_day(finished_at or started_at, night)))
    return covered


@dataclass(frozen=True)
class Period:
    """What an automatic import of a source would fetch now."""

    #: None: nothing new (`covered_until` reaches `end`).
    start: date | None
    end: date
    covered_until: date | None
    #: LOOKBACK_FLOOR_DAYS cut the start.
    cut: bool

    @property
    def up_to_date(self) -> bool:
        return self.start is None


def period_for(key: str, now) -> Period:
    """The period an automatic import of `key` fetches at `now` (the
    module's docstring)."""
    night = night_ends_at()
    today = timezone.localdate(now)
    end = last_complete_day(now, night)
    known = coverage_row(key, night).searched_until
    own = own_start(today)
    first_missing = known + timedelta(days=1) if known is not None else own
    if first_missing > end:
        return Period(start=None, end=end, covered_until=known or end, cut=False)
    start = known - timedelta(days=OVERLAP_DAYS) if known is not None else own
    floor = lookback_floor(today)
    return Period(start=max(start, floor), end=end, covered_until=known, cut=start < floor)


# -- How an import ended ----------------------------------------------------------------------------------------------


def _record(job: SalesImportJob, key: str, own: date | None) -> None:
    """A successful import's coverage (coverage.searched: the gathers'
    rules), up to the last complete day when it finished."""
    from invoices import coverage

    if job.range_start is None or job.range_end is None or own is None:
        return
    finished = job.finished_at or timezone.now()
    night = night_ends_at()
    until = min(job.range_end, last_complete_day(finished, night))
    if until < job.range_start:
        # Only days still open were imported (today, say): nothing complete.
        return
    coverage_row(key, night, exclude=job.pk)
    cut = job.is_automatic and job.range_start <= lookback_floor(timezone.localdate(job.started_at))
    coverage.searched(
        coverage_code(key), job.range_start, until, own=own, bounded=cut, today=timezone.localdate(finished)
    )


def _reason(error: str) -> str:
    """The first line of an import's error - no traceback, no address."""
    lines = [line.strip() for line in str(error or "").splitlines() if line.strip()]
    first = _URL.sub("(adresse masquée)", lines[0]) if lines else "erreur inconnue"
    return first[:200]


def _notify(job: SalesImportJob, error: str) -> None:
    """An automatic import's alert (« recipes-auto-sales »): `failed` once a
    day per rule (its content key holds the local day), else `new` when
    day totals were recorded, else `nothing`; and the rule's `last_failed`.
    Logged, never raised: the import is over and saved."""
    try:
        from notifications import events, registry

        day = timezone.localdate(job.finished_at or timezone.now())
        rules = AutoSalesImport.objects.filter(pk=job.auto_rule_id)
        if job.status == SalesImportJob.Status.FAILED:
            outcome = "failed"
            content_key = f"sales-failed:{job.auto_rule_id}:{day.isoformat()}"
            body = ALERT_FAILED.format(reason=_reason(error))
            rules.update(last_failed=day)
        else:
            outcome = "new" if job.recorded > 0 else "nothing"
            content_key = f"sales:{job.pk}"
            body = ALERT_BODY.format(
                start=job.range_start, end=job.range_end, recorded=job.recorded, unmatched=job.unmatched
            )
            rules.update(last_failed=None)
        definition = registry.event(registry.RECIPES_AUTO_SALES)
        label = definition.outcome_label(outcome) if definition is not None else outcome
        events.emit(
            registry.RECIPES_AUTO_SALES,
            outcome,
            title=ALERT_TITLE.format(label=label),
            body=body,
            target=reverse("recipes:sales_list"),
            content_key=content_key,
        )
    except Exception:  # the import is over and saved: an alert is never worth more
        logger.exception("Import automatique des ventes n° %s : fin non signalée", job.pk)


def _record_guarded(job: SalesImportJob, key: str, own: date | None) -> None:
    """`_record` in a savepoint of its own: a coverage that cannot be
    recorded is logged and rolled back alone - never the status saved in
    the same transaction (`finish`)."""
    try:
        with transaction.atomic():
            _record(job, key, own)
    except Exception:  # a wider import next time, never a failed one
        logger.exception("Import des ventes n° %s : couverture non enregistrée", job.pk)


def _alert(job: SalesImportJob, error: str) -> None:
    if job.is_automatic and job.status in (SalesImportJob.Status.SUCCESS, SalesImportJob.Status.FAILED):
        _notify(job, error)


def finish(job: SalesImportJob, *, fields, source_key: str, own: date | None, error: str = "") -> None:
    """The end of an import task: the job's final state saved (`fields`) -
    for a SUCCESS in ONE transaction with the coverage it records, so no
    tick can read the import over and its days not yet covered (it would
    sign in again for them) - then the alert, as `finished`. Raises only
    what the save itself raises."""
    if job.status == SalesImportJob.Status.SUCCESS:
        with transaction.atomic():
            job.save(update_fields=fields)
            _record_guarded(job, source_key, own)
    else:
        job.save(update_fields=fields)
    _alert(job, error)


def finished(job: SalesImportJob, *, source_key: str, own: date | None, error: str = "") -> None:
    """How an import of `source_key` ended, once its status is saved: a
    SUCCESS moves the source's coverage; an automatic import that succeeded
    or failed sends its alert - never a cancelled one. Never raises. A task
    saving its status itself ends with `finish` instead."""
    if job.status == SalesImportJob.Status.SUCCESS:
        _record_guarded(job, source_key, own)
    _alert(job, error)


#: How far back `gave_way` puts a rule's `last_slot_at`: just before the
#: slot it claimed, so the next tick finds that slot due again.
GIVEN_BACK = timedelta(microseconds=1)


def gave_way(job: SalesImportJob) -> None:
    """An automatic import refused a browser before anything was downloaded
    (chrome.BROWSERS_BUSY: started in the same tick as other bars', which
    took the server's browsers first): nothing ran. Its job is deleted - a
    failed or cancelled one would make the rule skip its slot
    (`_ended_while_waiting`) - and its rule's slot given back, « en attente :
    navigateurs du serveur occupés »: the next tick tries again within
    CATCH_UP_LIMIT. No alert, no `last_failed`: another bar's import holding
    the browsers is no failure of this one. Only the slot that started this
    job is given back (one claimed at or before it started); never raises -
    the slot then stays used, as a failed import's would."""
    try:
        rule = AutoSalesImport.objects.filter(pk=job.auto_rule_id).first() if job.auto_rule_id else None
        with transaction.atomic():
            SalesImportJob.objects.filter(pk=job.pk).delete()
            if rule is not None and rule.last_slot_at is not None and rule.last_slot_at <= job.started_at:
                AutoSalesImport.objects.filter(pk=rule.pk, last_slot_at=rule.last_slot_at).update(
                    last_slot_at=rule.last_slot_at - GIVEN_BACK, last_result=automation.BROWSERS_BUSY
                )
    except Exception:  # the thread's last step: logged, never raised
        logger.exception("Import automatique des ventes n° %s : créneau non rendu", job.pk)


# -- The rules' ticks ------------------------------------------------------------------------------------------------


def _ended_while_waiting(rule: AutoSalesImport, slot) -> bool:
    """The rule gave this slot back behind another import (« en attente »)
    and a sales import has failed or been cancelled since the slot: the
    owner's « Annuler », or a refused sign-in, is not to be repeated a
    minute later. Only an import of the rule's own source: a till's file
    refused on « Ventes » says nothing of L'Addition's sign-in."""
    if slot is None or rule.last_result != WAITING:
        return False
    return SalesImportJob.objects.filter(
        status__in=(SalesImportJob.Status.FAILED, SalesImportJob.Status.CANCELLED),
        finished_at__gte=slot,
        source=rule.source,
    ).exists()


def _credentials_busy() -> bool:
    """Another bar's « Identifiants » could not be read at all just now
    (accounts.vault.BUSY) - which is no account missing."""
    return not server_accounts_allowed() and vault.load().problem == vault.BUSY


def _start(rule: AutoSalesImport, now, slot=None) -> str | None:
    """The slot's import: its sentence, or None when another sales import
    is running (nothing created); automation.Retry when another bar's
    « Identifiants » or the server's browsers are busy (nothing created).
    Whether to import, and what, is decided under the start's lock
    (importing.start_sales_import's `plan`): read before it, the period
    could predate an import ending in between."""
    entry = sales_sources.source(rule.source)
    if entry is None:
        return UNKNOWN_SOURCE
    if not entry.available():
        if _credentials_busy():
            raise automation.Retry(automation.CREDENTIALS_BUSY)
        return SOURCE_UNAVAILABLE
    planned: dict[str, Period] = {}

    def plan():
        if _ended_while_waiting(rule, slot):
            return ENDED_WHILE_WAITING
        period = period_for(entry.key, now)
        if period.up_to_date:
            return UP_TO_DATE.format(day=period.covered_until)
        if entry.uses_browser and not chrome.slot_free():
            # Raised inside the lock's block: rolled back, nothing created.
            raise automation.Retry(automation.BROWSERS_BUSY)
        planned["period"] = period
        notes = [FLOOR_NOTE.format(floor=period.start, days=LOOKBACK_FLOOR_DAYS)] if period.cut else []
        return period.start, period.end, notes

    job = sales_sources.start(entry, trigger=SalesImportJob.Trigger.AUTOMATIC, auto_rule_id=rule.pk, plan=plan)
    if job is None or isinstance(job, str):
        return job
    period = planned["period"]
    return STARTED.format(at=automation.hhmm(now), pk=job.pk, start=period.start, end=period.end)


class _SalesImports(automation.Kind):
    """The automatic sales imports, for the shared slot machinery."""

    model = AutoSalesImport
    logger = logger
    waiting = WAITING
    missed_while_waiting = "manquée : un import des ventes était en cours à {at}"
    log_unreadable = "Import automatique des ventes n° %s ignoré : jours ou heures illisibles"
    log_unsaid = "Import automatique des ventes n° %s : résultat non enregistré (%s)"
    log_busy = "Import automatique des ventes n° %s : base occupée, nouvel essai"
    log_not_given_back = "Import automatique des ventes n° %s : créneau non rendu"
    log_launch_failed = "Import automatique des ventes n° %s : lancement en échec"
    log_rule_failed = "Import automatique des ventes n° %s en échec"

    def instants(self, rule, since, now):
        return schedule.calendar_instants(rule.weekday_list(), rule.time_list(), since, now)

    def catch_up_limit(self, rule):
        return CATCH_UP_LIMIT

    def start(self, rule, slot, now):
        return _start(rule, now, slot)


SALES = _SalesImports()


def due_slot(rule: AutoSalesImport, now):
    """The latest slot of `rule` in (last_slot_at or created_at, now], or
    None. ValueError when its days or times cannot be read."""
    return SALES.due_slot(rule, now)


def next_slots(rule: AutoSalesImport, now, count=5) -> list:
    """The rule's next `count` UTC instants; ValueError when unreadable."""
    return schedule.next_calendar_instants(rule.weekday_list(), rule.time_list(), now, count=count)


def run_rule(rule: AutoSalesImport, now, *, enabled: bool) -> str:
    """One rule's tick; what it wrote in `last_result` ("" when nothing was
    due or the slot was taken)."""
    return automation.run_rule(SALES, rule, now, enabled=enabled)


def prune(now) -> int:
    """Delete the automatic imports older than KEEP_RUNS (never one running)."""
    deleted, _ = (
        SalesImportJob.objects.filter(trigger=SalesImportJob.Trigger.AUTOMATIC, started_at__lt=now - KEEP_RUNS)
        .exclude(status__in=importing.ACTIVE)
        .delete()
    )
    return deleted


def run_due(now=None) -> None:
    """The bound espace's due automatic sales imports (the module's
    docstring)."""
    now = now or timezone.now()
    if not till_allowed():
        return
    enabled = webpush.sending_enabled()
    rules = AutoSalesImport.objects.filter(is_active=True).order_by("pk")
    # This module's run_rule as it is now (the tests patch it).
    automation.run_each(SALES, rules, now, enabled=enabled, run=run_rule)
    prune(now)
