"""The slot machinery every scheduled rule of an espace shares: the automatic
gathers of invoices and bons (invoices/auto_gather.py) and the automatic
sales imports (recipes/auto_sales.py). Each is a `Kind` - its model, its
instants, its catch-up limit, its launch, its words - and this module runs
one rule of it at a tick, the same way for both.

One rule's tick (`run_rule`):

- its due slot is the latest instant of its schedule in (`last_slot_at` or
  `created_at`, now] - UTC arithmetic only (notifications/schedule.py).
  None: nothing. Days or hours nobody can read: the window is CLAIMED ONCE
  and said (« sautée : jours ou heures illisibles »), never a warning a
  minute;
- the slot is CLAIMED by a conditional UPDATE on the `last_slot_at` read (0
  rows: a concurrent tick took it), and the claim says why nothing ran when
  nothing does (`last_result`, written guarded: a database locked past its
  timeout loses the sentence, never the tick):
  - « sautée : serveur de développement » - a dev copy never runs a rule by
    itself (`notifications.webpush.sending_enabled`, asked by the caller);
  - « manquée : … à HH:MM » - a slot older than the kind's catch-up limit
    (the PC off or asleep; a rule that waited all that time for another
    run, or for a busy database, says so);
  - « sautée : mise à jour du site en cours » - deploy.cmd's mark exists;
  - else the kind's own launch (`Kind.start`), guarded: None - another run
    of its kind is active - GIVES THE SLOT BACK (the claim undone) and says
    the kind's « en attente : … », so the next tick tries again until it
    starts or passes its limit; a DatabaseError (the espace locked past the
    scheduler's short wait: nothing was created) gives it back too, « en
    attente : base occupée »; so does a `Retry` the launch raises before it
    created anything, saying its own « en attente : … » (one of `WAITS`:
    another bar's « Identifiants » that could not be read for a moment, the
    server's browsers all taken by other bars); anything else is logged
    with its traceback, « échec : erreur interne à HH:MM », the slot kept.

`run_each` runs every active rule of a kind, each in its own try: one rule
raising is logged and the next one runs all the same.

Nothing here knows what a rule launches: the kinds' modules say it, and
their tests patch their own module's names (a kind's hooks look them up at
call time).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

DEV_SERVER = "sautée : serveur de développement"
DEPLOYING = "sautée : mise à jour du site en cours"
UNREADABLE = "sautée : jours ou heures illisibles"
BUSY = "en attente : base occupée"
#: Another bar's « Identifiants » could not be read at the slot
#: (accounts.vault.BUSY: a save, a backup or an antivirus holding the file) -
#: not « à renseigner »: they are there.
CREDENTIALS_BUSY = "en attente : identifiants momentanément illisibles"
#: Another bar's run would find every browser of the server taken by the
#: other bars' (invoices/scrapers/chrome.py) and be refused at once.
BROWSERS_BUSY = "en attente : navigateurs du serveur occupés"
#: What a `Retry` may say while it waits → what the slot says once past its
#: catch-up limit, « {at} » its time.
WAITS = {
    CREDENTIALS_BUSY: "manquée : identifiants illisibles à {at}",
    BROWSERS_BUSY: "manquée : navigateurs du serveur occupés à {at}",
}


class Retry(Exception):
    """Raised by a kind's `start` BEFORE it created anything: the slot is
    given back and the sentence said - a key of WAITS -, the next tick tries
    again within the catch-up limit."""

    def __init__(self, sentence: str):
        if sentence not in WAITS:
            raise ValueError(f"not a waiting sentence: {sentence!r}")
        super().__init__(sentence)
        self.sentence = sentence


def deploy_mark() -> Path:
    """The folder deploy.cmd makes while it runs (« Two copies », CLAUDE.md)."""
    return Path(settings.BASE_DIR) / ".git" / "marginmate-deploy"


def hhmm(moment) -> str:
    """« 07:00 »: the local time of a UTC instant."""
    return f"{timezone.localtime(moment):%H:%M}"


class Kind:
    """One kind of scheduled rule. A subclass sets `model` (a model with
    `pk`, `last_slot_at`, `last_result`, `created_at`), `logger`, its
    sentences and its log lines, and implements `instants`,
    `catch_up_limit` and `start`; the other hooks have the model's default
    and may be overridden (a kind whose tests patch its module's own
    functions points them there)."""

    model: type
    logger: logging.Logger
    #: What the rule says while another run of its kind is active.
    waiting: str
    #: Past its catch-up limit after waiting, with « {at} » the slot's time.
    missed_while_waiting: str
    missed_while_busy = "manquée : base occupée à {at}"
    missed_server_off = "manquée : serveur arrêté à {at}"
    #: The log lines, each with « %s » for the rule's pk (and the result).
    log_unreadable: str
    log_unsaid: str
    log_busy: str
    log_not_given_back: str
    log_launch_failed: str
    log_rule_failed: str

    def instants(self, rule, since, now) -> list:
        """The rule's UTC instants in (since, now]; ValueError when its days
        or hours cannot be read."""
        raise NotImplementedError

    def catch_up_limit(self, rule) -> timedelta:
        raise NotImplementedError

    def start(self, rule, slot, now) -> str | None:
        """Launch the slot's run: the sentence saying what came of it, or
        None when another run of this kind is active (nothing created)."""
        raise NotImplementedError

    def due_slot(self, rule, now):
        """The latest instant in (last_slot_at or created_at, now], or None.
        ValueError when the rule's days or hours cannot be read."""
        since = rule.last_slot_at or rule.created_at
        slots = self.instants(rule, since, now)
        return slots[-1] if slots else None

    def claim(self, rule, slot) -> bool:
        """Take `slot`: a conditional UPDATE on the `last_slot_at` this tick
        read (IS NULL when it was none). False when another tick took it."""
        return bool(self.model.objects.filter(pk=rule.pk, last_slot_at=rule.last_slot_at).update(last_slot_at=slot))

    def give_back(self, rule, slot) -> None:
        """Undo this tick's claim of `slot` (conditional on it still being
        the one stored): the next tick finds the slot due again."""
        self.model.objects.filter(pk=rule.pk, last_slot_at=slot).update(last_slot_at=rule.last_slot_at)

    def say(self, rule, result: str) -> None:
        self.model.objects.filter(pk=rule.pk).update(last_result=result[:200])


def write(kind: Kind, rule, result: str) -> None:
    """`kind.say`, guarded: a database locked past its timeout loses the
    sentence, never the tick."""
    try:
        kind.say(rule, result)
    except DatabaseError:
        kind.logger.warning(kind.log_unsaid, rule.pk, result)


def unreadable(kind: Kind, rule, now) -> str:
    """A rule whose days or hours cannot be read: its window claimed once
    and said - the next ticks find the sentence already there and stay
    quiet."""
    if rule.last_result == UNREADABLE or not kind.claim(rule, now):
        return ""
    kind.logger.warning(kind.log_unreadable, rule.pk)
    write(kind, rule, UNREADABLE)
    return UNREADABLE


def _give_back_guarded(kind: Kind, rule, slot) -> None:
    try:
        kind.give_back(rule, slot)
    except DatabaseError:
        kind.logger.warning(kind.log_not_given_back, rule.pk)


def launch(kind: Kind, rule, slot, now) -> str:
    """Start the slot's run (`kind.start`); the sentence saying what came of
    it."""
    try:
        result = kind.start(rule, slot, now)
        if result is None:
            kind.give_back(rule, slot)
            return kind.waiting
    except Retry as wait:
        # Refused before anything was created: tried again at the next
        # tick, within the slot's catch-up limit.
        _give_back_guarded(kind, rule, slot)
        return wait.sentence
    except DatabaseError:
        # Locked past the scheduler's short wait: the start's transaction
        # never opened (or rolled back), nothing was created - tried again
        # at the next tick, within the slot's catch-up limit.
        kind.logger.warning(kind.log_busy, rule.pk, exc_info=True)
        _give_back_guarded(kind, rule, slot)
        return BUSY
    except Exception:  # the claimed slot must say why nothing ran
        kind.logger.exception(kind.log_launch_failed, rule.pk)
        return f"échec : erreur interne à {hhmm(now)}"
    return result


def missed(kind: Kind, rule, slot) -> str:
    """Why a slot past its catch-up limit was not run."""
    if rule.last_result == kind.waiting:
        return kind.missed_while_waiting.format(at=hhmm(slot))
    if rule.last_result == BUSY:
        return kind.missed_while_busy.format(at=hhmm(slot))
    if rule.last_result in WAITS:
        return WAITS[rule.last_result].format(at=hhmm(slot))
    return kind.missed_server_off.format(at=hhmm(slot))


def run_rule(kind: Kind, rule, now, *, enabled: bool) -> str:
    """One rule's tick (the module's docstring); what it wrote in
    `last_result` ("" when nothing was due or the slot was taken).
    `enabled`: `notifications.webpush.sending_enabled()`, asked once per
    tick by the caller."""
    try:
        slot = kind.due_slot(rule, now)
    except ValueError:
        return unreadable(kind, rule, now)
    if slot is None or not kind.claim(rule, slot):
        return ""
    if not enabled:
        result = DEV_SERVER
    elif now - slot >= kind.catch_up_limit(rule):
        result = missed(kind, rule, slot)
    elif deploy_mark().exists():
        result = DEPLOYING
    else:
        result = launch(kind, rule, slot, now)
    write(kind, rule, result)
    return result


def run_each(kind: Kind, rules, now, *, enabled: bool, run=None) -> None:
    """Every rule of `rules` on its own: one raising is logged, the next one
    runs. `run(rule, now, enabled=...)` is the kind's module's own
    `run_rule` (its tests patch it), else this module's for `kind`."""
    for rule in rules:
        try:
            if run is not None:
                run(rule, now, enabled=enabled)
            else:
                run_rule(kind, rule, now, enabled=enabled)
        except Exception:  # one rule's failure is its own
            kind.logger.exception(kind.log_rule_failed, rule.pk)
