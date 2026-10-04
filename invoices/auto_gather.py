"""Automatic gathers: the « Récupération automatique » rules (AutoGather), run
by the scheduler of `manage.py serve` (notifications/scheduler.py: `run_due`
is one of its JOBS, called once a minute with the espace bound).

What each tick does for the bound espace, in this order:

- nothing at all unbound (`integrations_allowed`: any bound espace since
  04/10/2026 - each searches ITS mailbox, signed in with its own
  « Identifiants »; the server's .env stands in for the platform owner's
  espace alone, and Metro and the portals are his and never automatic);
- every active rule, each on its own (one raising is logged and the next
  rule and the prune run all the same): its due slot is the latest instant
  of its schedule in (`last_slot_at` or `created_at`, now] - UTC arithmetic
  only (notifications.schedule.gather_instants). None: nothing. A slot is
  CLAIMED by a conditional UPDATE on the `last_slot_at` read (0 rows: a
  concurrent tick took it), and the claim writes why nothing ran when
  nothing does (`last_result`):
  - « sautée : jours ou heures illisibles » - a row the admin or a hand left
    with days nobody can read: the window is claimed ONCE and said, never
    a warning a minute;
  - « sautée : serveur de développement » - a dev copy never gathers by
    itself (`notifications.webpush.sending_enabled`: an https SITE_URL,
    DEBUG off, a strong key);
  - « manquée : serveur arrêté à HH:MM » - a slot is caught up only within
    its rule's `catch_up_limit` (min(every_minutes, 120) minutes, twelve
    hours for a once-a-day rule); an older one was the PC off or asleep -
    or « manquée : une récupération était en cours à HH:MM » when the rule
    was waiting for another gather all that time;
  - « sautée : mise à jour du site en cours » - deploy.cmd's mark exists;
  - « sautée : boîte mail à renseigner sur la page Identifiants » -
    another bar whose « Identifiants » do not hold its mailbox
    (`integrations.mailbox_offered`): nothing signs in, ever with the
    owner's;
  - « sautée : aucune source disponible » - none of its sources is offered
    to an automatic gather any more (`workspace.gather_sources`: the
    mailbox's invoice sources and slip formats only, never Metro nor a
    portal);
  - « en attente : une récupération est en cours » - another gather is
    active: the slot is GIVEN BACK (the claim undone), so the next tick
    tries again until it starts or passes its catch-up limit. The page's
    own suggested pair (slips every half hour from 06:00, invoices once a
    day at 07:00) is due at the same instant: the second waits for the
    first rather than losing its day;
  - « en attente : base occupée » - the launch met a database error (the
    espace locked past the scheduler's short wait, notifications/
    scheduler.BUSY_TIMEOUT_MS): nothing was created, so the slot is GIVEN
    BACK like a waiting one, and past its catch-up limit it is « manquée :
    base occupée à HH:MM »;
  - « échec : erreur interne à HH:MM » - the launch itself raised anything
    else (a bug): logged with its traceback, the slot kept;
  - else « lancée à HH:MM (récupération n° N) », through the one start of a
    gather (gathering.start_gather: trigger automatic, unattended);
- the automatic runs older than 30 days are deleted (their history is the
  rule's page's ten latest; the manual ones stay as they always did).

How a run ended is said by the task itself (tasks._notify_auto_gather).

The slot machinery - the claim, the give-back, the catch-up, the dev and
deploy skips, the guarded writes - is the one every scheduled rule shares
(notifications/automation.py, which the automatic sales imports run on
too); this module is its gathers' `Kind`. Its own names stay here and are
looked up at call time (`claim`, `_say`, `run_rule`…): the tests patch them.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from accounts import vault
from accounts.tenancy import integrations_allowed, server_accounts_allowed
from notifications import automation, schedule, webpush
from notifications.automation import BUSY, DEPLOYING, DEV_SERVER, UNREADABLE, deploy_mark

from . import gathering, integrations
from .models import AutoGather, ScrapeJob
from .workspace import gather_sources

#: The shared machinery's names this module has always offered, still
#: importable from here (the gathers' tests and pages read them).
__all__ = ["BUSY", "DEPLOYING", "DEV_SERVER", "UNREADABLE", "deploy_mark", "run_due"]

logger = logging.getLogger(__name__)

#: A missed slot of a rule running several times a day is caught up only
#: while it is younger than this and than the rule's own period.
CATCH_UP_LIMIT = timedelta(minutes=120)
#: A once-a-day rule's slot (start = end) is caught up this long: its
#: « Toutes les » sets nothing else, and a PC woken at 08:10 must still
#: fetch the 07:00 invoices.
ONCE_A_DAY_CATCH_UP = timedelta(hours=12)
#: Automatic runs are kept this long.
KEEP_RUNS = timedelta(days=30)

NO_SOURCE = "sautée : aucune source disponible"
#: Another bar's mailbox not on its « Identifiants »: its rules wait for it.
MAILBOX_TO_FILL = "sautée : boîte mail à renseigner sur la page Identifiants"
WAITING = "en attente : une récupération est en cours"

_hhmm = automation.hhmm


def catch_up_limit(rule: AutoGather) -> timedelta:
    """How late a missed slot of `rule` is still run: min(every_minutes, 120)
    minutes, or twelve hours for a once-a-day rule."""
    if rule.start_time == rule.end_time:
        return ONCE_A_DAY_CATCH_UP
    return min(timedelta(minutes=rule.every_minutes), CATCH_UP_LIMIT)


def describe_catch_up(rule: AutoGather) -> str:
    """« 30 min », « 2 h », « 12 h » - the rule's catch-up limit as its card
    states it."""
    minutes = int(catch_up_limit(rule).total_seconds() // 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h"


def due_slot(rule: AutoGather, now):
    """The latest slot of `rule` in (last_slot_at or created_at, now], or
    None. ValueError when its days or hours cannot be read."""
    since = rule.last_slot_at or rule.created_at
    slots = schedule.gather_instants(
        rule.weekday_list(), rule.start_time, rule.end_time, rule.every_minutes, since, now
    )
    return slots[-1] if slots else None


def claim(rule: AutoGather, slot) -> bool:
    """Take `slot` for `rule`: a conditional UPDATE on the `last_slot_at`
    this tick read (IS NULL when it was none). False when another tick
    took it first."""
    return automation.Kind.claim(GATHERS, rule, slot)


def give_back(rule: AutoGather, slot) -> None:
    """Undo this tick's claim of `slot` (conditional on it still being the
    one stored): the next tick finds the slot due again."""
    automation.Kind.give_back(GATHERS, rule, slot)


def _say(rule: AutoGather, result: str) -> None:
    automation.Kind.say(GATHERS, rule, result)


def _write(rule: AutoGather, result: str) -> None:
    """`_say`, guarded: a database locked past its timeout loses the
    sentence, never the tick."""
    automation.write(GATHERS, rule, result)


def available_codes(rule: AutoGather, state=None) -> list[str]:
    """The rule's sources still offered to an automatic gather, in its order.
    `state`: the « Identifiants » store already read for this slot."""
    sources, _ = gather_sources(for_auto=True, state=state)
    allowed = {source["code"] for source in sources if source.get("allowed")}
    return [code for code in rule.source_list() if code in allowed]


def _unreadable(rule: AutoGather, now) -> str:
    """A rule whose days or hours cannot be read: its window claimed once and
    said - the next ticks find the sentence already there and stay quiet."""
    return automation.unreadable(GATHERS, rule, now)


def _start(rule: AutoGather, now) -> str | None:
    """The slot's gather, through the one start of a gather: None when
    another gather is active (nothing created). Another bar's gather waits
    for its mailbox on its « Identifiants » - its store read once here."""
    state = None if server_accounts_allowed() else vault.load()
    if not integrations.mailbox_offered(state):
        return MAILBOX_TO_FILL
    codes = available_codes(rule, state=state)
    if not codes:
        return NO_SOURCE
    job = gathering.start_gather(
        codes,
        None,
        None,
        metro_now=False,
        trigger=ScrapeJob.Trigger.AUTOMATIC,
        auto_gather_id=rule.pk,
        unattended=True,
    )
    if job is None:
        return None
    return f"lancée à {_hhmm(now)} (récupération n° {job.pk})"


def _launch(rule: AutoGather, slot, now) -> str:
    """Start the slot's gather; the sentence saying what came of it."""
    return automation.launch(GATHERS, rule, slot, now)


class _Gathers(automation.Kind):
    """The automatic gathers, for the shared slot machinery. Every hook goes
    through this module's own functions, looked up at call time."""

    model = AutoGather
    logger = logger
    waiting = WAITING
    missed_while_waiting = "manquée : une récupération était en cours à {at}"
    log_unreadable = "Récupération automatique n° %s ignorée : jours ou heures illisibles"
    log_unsaid = "Récupération automatique n° %s : résultat non enregistré (%s)"
    log_busy = "Récupération automatique n° %s : base occupée, nouvel essai"
    log_not_given_back = "Récupération automatique n° %s : créneau non rendu"
    log_launch_failed = "Récupération automatique n° %s : lancement en échec"
    log_rule_failed = "Récupération automatique n° %s en échec"

    def instants(self, rule, since, now):
        return schedule.gather_instants(
            rule.weekday_list(), rule.start_time, rule.end_time, rule.every_minutes, since, now
        )

    def due_slot(self, rule, now):
        return due_slot(rule, now)

    def catch_up_limit(self, rule):
        return catch_up_limit(rule)

    def claim(self, rule, slot):
        return claim(rule, slot)

    def give_back(self, rule, slot):
        give_back(rule, slot)

    def say(self, rule, result):
        _say(rule, result)

    def start(self, rule, slot, now):
        return _start(rule, now)


GATHERS = _Gathers()


def run_rule(rule: AutoGather, now, *, enabled: bool) -> str:
    """One rule's tick; what it wrote in `last_result` ("" when nothing was
    due or the slot was taken)."""
    return automation.run_rule(GATHERS, rule, now, enabled=enabled)


def prune(now) -> int:
    """Delete the automatic runs older than KEEP_RUNS (never one running)."""
    deleted, _ = (
        ScrapeJob.objects.filter(trigger=ScrapeJob.Trigger.AUTOMATIC, started_at__lt=now - KEEP_RUNS)
        .exclude(status__in=gathering.ACTIVE)
        .delete()
    )
    return deleted


def run_due(now=None) -> None:
    """The bound espace's due automatic gathers (the module's docstring)."""
    now = now or timezone.now()
    if not integrations_allowed():
        return
    enabled = webpush.sending_enabled()
    rules = AutoGather.objects.filter(is_active=True).order_by("pk")
    # This module's run_rule as it is now (the tests patch it).
    automation.run_each(GATHERS, rules, now, enabled=enabled, run=run_rule)
    prune(now)
