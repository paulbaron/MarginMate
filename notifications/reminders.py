"""The reminders that are due (`run_due`), a job of the scheduler's tick
(notifications/scheduler.py), run bound to one espace.

Each active reminder's instants since the last tick (UTC, notifications/
schedule.py) become dispatches: `pending` when at most REMINDER_GRACE late,
`skipped` when its condition holds (« sauté : une reprise a été enregistrée
à 00:15 »), `missed` when later (« manqué : serveur arrêté ou ordinateur en
veille à 00:00 » - never sent: a reminder for 00:00 is useless at 07:00).
The window reaches back at most LOOKBACK, never before the reminder was made
nor before the espace's last tick; each instant's row is unique
(`reminder_key`), so two ticks of one minute - two processes - insert it
once.

The tick's housekeeping of the espace follows (`housekeeping`): the devices
of its logins (tombstones, failing devices), the dispatches abandoned
(`pending` past their TTL, `sending` for ten minutes) and the history's
retention.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from accounts.tenancy import require_tenant

from . import devices, registry, schedule, webpush
from .models import ACTIVE_STATUSES, Dispatch, NotificationSettings, Reminder, create_dispatch, reminder_key
from .sending import EXPIRED, INTERRUPTED, SENDING_STALE_AFTER

logger = logging.getLogger(__name__)

REMINDER_GRACE = schedule.REMINDER_GRACE
#: How far back a tick looks for an instant it has not seen.
LOOKBACK = timedelta(hours=24)
#: The history kept: rows younger than this, and the newest KEEP_DISPATCHES.
RETENTION = timedelta(days=90)
KEEP_DISPATCHES = 1000


def missed_detail(instant) -> str:
    return f"manqué : serveur arrêté ou ordinateur en veille à {timezone.localtime(instant):%H:%M}"


def run_due(now=None) -> int:
    """Queue the bound espace's due reminders (the module's docstring), then
    the housekeeping. How many dispatches were inserted."""
    now = now or timezone.now()
    settings = NotificationSettings.get_solo()
    window_start = max(settings.last_tick_at or now - REMINDER_GRACE, now - LOOKBACK)
    inserted = 0
    for reminder in Reminder.objects.filter(is_active=True).order_by("pk"):
        try:
            weekdays, times = reminder.weekday_list(), reminder.time_list()
        except ValueError:
            logger.warning("Rappel n° %s ignoré : jours ou heures illisibles", reminder.pk)
            continue
        start = max(window_start, reminder.created_at)
        for instant in schedule.reminder_instants(weekdays, times, settings.night_ends_at, start, now):
            if _queue(reminder, instant, now) is not None:
                inserted += 1
    NotificationSettings.objects.filter(pk=settings.pk).update(last_tick_at=now)
    housekeeping(now)
    return inserted


def _skip_reason(reminder: Reminder, now) -> str:
    condition = registry.skip_condition(reminder.skip_if)
    if condition is None:
        return ""
    try:
        return condition.check(now, reminder.skip_hours, reminder.skip_supplier_id)
    except Exception:  # a condition that cannot be read sends the reminder: better twice than never
        logger.exception("Rappel n° %s : condition « %s » illisible, rappel envoyé", reminder.pk, reminder.skip_if)
        return ""


def _queue(reminder: Reminder, instant, now) -> Dispatch | None:
    key = reminder_key(reminder.pk, instant)
    if Dispatch.objects.filter(dedupe_key=key).exists():
        return None
    fields = {
        "kind": Dispatch.Kind.REMINDER,
        "reminder_id": reminder.pk,
        "rule_name": reminder.name,
        "scheduled_for": instant,
        "dedupe_key": key,
        "title": reminder.title,
        "body": reminder.body,
        "target": reminder.target,
        "ttl": webpush.TTL_REMINDER,
        # A still-undelivered 00:00 is replaced by the 02:00 at the push
        # service.
        "topic": f"r{reminder.pk}",
        "recipient_ids": reminder.recipients(),
        "created_at": now,
    }
    if now - instant > REMINDER_GRACE:
        fields.update(status=Dispatch.Status.MISSED, detail=missed_detail(instant))
    elif reason := _skip_reason(reminder, now):
        fields.update(status=Dispatch.Status.SKIPPED, detail=reason)
    else:
        fields["status"] = Dispatch.Status.PENDING
    return create_dispatch(**fields)


# -- Housekeeping ---------------------------------------------------------------------------------------------------


def housekeeping(now=None) -> None:
    """The bound espace's devices, abandoned dispatches and history."""
    now = now or timezone.now()
    devices.prune(require_tenant().pk, now)
    sweep(now)
    prune_history(now)


def sweep(now=None) -> int:
    """`pending` past its TTL and `sending` for SENDING_STALE_AFTER (the
    server stopped while it sent) become `failed`, saying which. How many."""
    now = now or timezone.now()
    swept = 0
    for pk, created_at, ttl in Dispatch.objects.filter(status=Dispatch.Status.PENDING).values_list(
        "pk", "created_at", "ttl"
    ):
        if now - created_at > timedelta(seconds=max(int(ttl or 0), 1)):
            swept += Dispatch.objects.filter(pk=pk, status=Dispatch.Status.PENDING).update(
                status=Dispatch.Status.FAILED, detail=EXPIRED, sent_at=now
            )
    stale = now - SENDING_STALE_AFTER
    swept += Dispatch.objects.filter(
        Q(sent_at__lt=stale) | Q(sent_at__isnull=True, created_at__lt=stale), status=Dispatch.Status.SENDING
    ).update(status=Dispatch.Status.FAILED, detail=INTERRUPTED)
    return swept


def prune_history(now=None) -> int:
    """Delete the history older than RETENTION and beyond the newest
    KEEP_DISPATCHES - never a dispatch still pending or being sent."""
    now = now or timezone.now()
    finished = Dispatch.objects.exclude(status__in=ACTIVE_STATUSES)
    deleted, _ = finished.filter(created_at__lt=now - RETENTION).delete()
    edge = Dispatch.objects.order_by("-created_at", "-pk").values_list("created_at", "pk")[
        KEEP_DISPATCHES : KEEP_DISPATCHES + 1
    ]
    for created_at, pk in edge:
        beyond, _ = finished.filter(Q(created_at__lt=created_at) | Q(created_at=created_at, pk__lte=pk)).delete()
        deleted += beyond
    return deleted
