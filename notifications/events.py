"""Alerts on an event (notifications.registry.EVENTS): what the code that saw
the event calls.

    if events.wanted("returnables-comparison"):     # before any work
        ...
        events.emit("returnables-comparison", "differs", title=..., body=...,
                    target=..., content_key=...)

**`emit` follows the caller's transaction and never breaks it.** Its work
runs in `transaction.on_commit` (at once in autocommit): rolled back, the
caller's work alerts nobody. The rule is read, the dispatch inserted in a
savepoint of its own (its `dedupe_key` from the event's `content_key`: the
same result is sent once, however many times it is read) and the espace's
delivery thread started - all inside one `except Exception` that logs
« Notification non créée : … » and returns. A slip stored, a gather ended,
must never fail because a notification could not be made.
"""

from __future__ import annotations

import functools
import logging
import threading  # noqa: F401 - tests patch notifications.events.threading.Thread (sending.start_delivery uses it)

from django.db import DatabaseError, transaction

from . import registry, sending, webpush
from .models import Dispatch, EventRule, create_dispatch, event_key

logger = logging.getLogger(__name__)


def wanted(event) -> bool:
    """Whether an active rule follows `event`: the caller's cue to do the
    work an alert needs. False in an espace without the tables, and False
    said (a warning) when the rule could not be read.

    Inside a caller's transaction the read is a savepoint of its own, so a
    failure never poisons that transaction; outside one it is a plain read -
    an `atomic()` there would BEGIN IMMEDIATE and wait for the espace's
    write lock just to read (WAL readers never wait)."""
    try:
        rules = EventRule.objects.filter(event=event, is_active=True)
        if transaction.get_connection().in_atomic_block:
            with transaction.atomic():
                return rules.exists()
        return rules.exists()
    except DatabaseError as exc:
        if "no such table" not in str(exc):
            logger.warning("Notifications : règle « %s » non lue, alerte non préparée (%s)", event, exc)
        return False


def emit(event, outcome, *, title, body, target, content_key) -> None:
    """Queue the alert of `event` with `outcome` once the caller's
    transaction commits (the module's docstring). Never raises."""
    try:
        transaction.on_commit(
            functools.partial(
                _emit_now, event, outcome, title=title, body=body, target=target, content_key=content_key
            ),
            robust=True,
        )
    except Exception:  # an alert is never worth the caller's work
        logger.exception("Notification non créée : %s (%s)", event, outcome)


def _emit_now(event, outcome, *, title, body, target, content_key) -> Dispatch | None:
    try:
        rule = EventRule.objects.filter(event=event, is_active=True).first()
        if rule is None or not rule.wants(outcome):
            return None
        definition = registry.event(event)
        dispatch = create_dispatch(
            kind=Dispatch.Kind.EVENT,
            event_rule_id=rule.pk,
            rule_name=definition.label if definition is not None else event,
            event=event,
            outcome=outcome,
            dedupe_key=event_key(rule.pk, content_key),
            title=title,
            body=body,
            target=rule.target or target,
            ttl=webpush.TTL_EVENT,
            recipient_ids=rule.recipients(),
            status=Dispatch.Status.PENDING,
        )
        if dispatch is not None:
            sending.start_delivery()
        return dispatch
    except Exception:  # see the module's docstring
        logger.exception("Notification non créée : %s (%s)", event, outcome)
        return None
