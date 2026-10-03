"""The « Consignes » alert: when a driver's slip arrives, say whether it
matches the pickup counted on /consignes/, differs, or found no pickup
(notifications.registry « returnables-comparison »).

`notify_slips(slips)` is called ONCE per batch, after it, with the slips the
batch CREATED - by the gather (`mail.store_matches`), the page's upload
(`slips.store_uploads`) and Achats' guard (`invoices.receipts.
route_to_returnables`, its one slip). Never from `store_slip` itself: a
folder of history or the first 90 days of a mailbox would be one alert per
slip, and the « Données » import (which writes slips directly) alerts
nobody.

What it does, in this order:

0. Nothing at all unless an active rule follows the event
   (`events.wanted`): no slip is read for an espace that asked for nothing.
1. The slips grouped by (supplier, delivery day), and only the days within
   RECENT_DAYS before today (tomorrow at most) kept: an old slip arriving
   late is history, not news. A slip without its delivery date is dated by
   its mail's date, else the day it was received - to be kept or not - and
   is a group of its OWN: it pairs with no pickup, so it never stands for
   the dated slips of its mail's day (they keep their own alert). One
   evaluation per group, on its latest slip that still counts (a re-send or
   a replaced slip says nothing).
2. That slip's state on a Board (`comparison.Board.slip_state`), mapped to
   an outcome: « match », « differs », « no_pickup » or « to_check ». A
   pickup within HINT_DAYS that is probably misdated (counted the evening
   before, the slip dated the delivery day) is compared as if it were on the slip's
   day (`Board.as_if_dated`) and the alert says where to move it - the
   Consignes page itself never changes. A day with no pickup and no hint
   is decided on EVERY slip that counts that day, never on the latest
   alone (two deliveries, the empties on the first trip): « no_pickup »
   naming the slips that list empties when any does, else « to_check »
   when one of them has a problem, else « match » (« aucun vide repris »)
   - each empty and passing its checks. A pickup with « Repris par » blank
   within BLANK_SUPPLIER_DAYS makes such a day « to_check » first, whether
   its slips list empties or none (the driver may have left them off).
3. `events.emit` with a `content_key` built from the substance (supplier,
   day, outcome, the slips the result rests on, the numbers compared) -
   never the wording: the same result read again is sent once, and a slip
   listing nothing that arrives after « aucune reprise saisie » says the
   same again rather than « conforme ». The blank « Repris par » alert rests
   on that pickup's date and on no slip: a later slip of the day repeats it.

**Never raises**: a slip stored must not fail, nor its upload say « le
fichier n'a pas été enregistré », because an alert could not be made. Every
failure is logged and dropped.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import NamedTuple

from django.urls import reverse
from django.utils import timezone

from notifications import events
from notifications.registry import RETURNABLES_COMPARISON
from notifications.schedule import DAY_SHORT
from returnables.comparison import DIFFERS, REPLACED, RESENT, SAME, Board, shifted, slip_label, slip_problems
from returnables.models import Pickup, SlipFormat

logger = logging.getLogger(__name__)

EVENT = RETURNABLES_COMPARISON
#: A slip delivered more than this many days before today alerts nobody.
RECENT_DAYS = 3
#: How far around the slip's day a pickup with « Repris par » blank is
#: looked for (comparison.HINT_DAYS' reach).
BLANK_SUPPLIER_DAYS = 3

MATCH = "match"
DIFFERENT = "differs"
NO_PICKUP = "no_pickup"
TO_CHECK = "to_check"

TITLES = {
    MATCH: "Consignes : conforme",
    DIFFERENT: "Consignes : écart",
    NO_PICKUP: "Consignes : aucune reprise saisie",
    TO_CHECK: "Consignes : à vérifier",
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: The slip states an unpaired slip may carry a hint with.
_UNPAIRED = ("no_pickup", "nothing_back", "to_check")


def notify_slips(slips) -> None:
    """The alert of a batch of slips just CREATED (the module's docstring).
    Never raises."""
    try:
        slips = [slip for slip in slips or () if slip is not None and slip.pk is not None]
        if not slips:
            return
        if not events.wanted(EVENT):
            return
        for group in _recent_groups(slips, _today()):
            _evaluate(group)
    except Exception:  # an alert is never worth the slip
        logger.exception("Consignes : alerte du bon non préparée")


def _today() -> date:
    return timezone.localdate()


def short_day(day: date) -> str:
    """« mar. 10/02 »."""
    return f"{DAY_SHORT[day.weekday()]} {day:%d/%m}"


def slip_day(slip) -> date | None:
    """The day a slip is about: its delivery date, else its mail's date,
    else the local day it was received."""
    if slip.delivery_date is not None:
        return slip.delivery_date
    if slip.mail_date is not None:
        return slip.mail_date
    if slip.received_at is not None:
        return timezone.localdate(slip.received_at)
    return None


def _recent_groups(slips, today: date) -> list:
    """[[slip, …], …] - the slips of one (supplier, delivery day), and each
    slip without its delivery date alone, for the days between RECENT_DAYS
    before today and tomorrow; one query (the formats' suppliers)."""
    suppliers = dict(
        SlipFormat.objects.filter(pk__in={slip.format_id for slip in slips}).values_list("pk", "supplier_id")
    )
    first, last = shifted(today, -RECENT_DAYS), shifted(today, 1)
    groups = defaultdict(list)
    for slip in slips:
        day = slip_day(slip)
        if day is None or not first <= day <= last:
            continue
        supplier = suppliers.get(slip.format_id)
        # A slip without its delivery date pairs with no pickup: its own
        # alert, never in place of the dated slips of its mail's day.
        key = (supplier, day) if slip.delivery_date is not None else (supplier, day, slip.pk)
        groups[key].append(slip)
    return [groups[key] for key in sorted(groups, key=lambda key: (key[1], key[0] or 0, key[2:]))]


def _moment(slip) -> tuple:
    """A slip's moment, as comparison.SlipInfo.key orders slips."""
    return (slip.printed_at or slip.received_at or _EPOCH, slip.pk)


def _evaluate(group) -> None:
    """One (supplier, day): the latest slip of the group that counts, its
    alert. A group whose every slip is superseded (a re-send, an original
    arriving after its replacement) says nothing."""
    board = Board.load(slips=group)
    for slip in sorted(group, key=_moment, reverse=True):
        state = board.slip_state(slip)
        if state.key in (RESENT, REPLACED):
            continue
        alert = _alert(board, slip, state)
        # The alert opens the slip it speaks of: this one when it is among
        # those the result rests on (or it rests on none), else the first.
        basis = alert.basis
        subject = slip.pk if not basis or any(info.pk == slip.pk for info in basis) else basis[0].pk
        events.emit(
            EVENT,
            alert.outcome,
            title=TITLES[alert.outcome],
            body=alert.body,
            target=reverse("returnables:slip_detail", args=[subject]),
            content_key=content_key(board, slip, alert.outcome, alert.day, basis, extra=alert.extra),
        )
        return


def _outcome_of(day) -> str:
    """A day's comparison as an outcome: « à vérifier » unless it agrees or
    differs."""
    if day.status == SAME:
        return MATCH
    if day.status == DIFFERS:
        return DIFFERENT
    return TO_CHECK


class _Alert(NamedTuple):
    """What one slip that counts says, and what that rests on."""

    outcome: str
    body: str
    #: The DayComparison it rests on, or None.
    day: object
    #: The SlipInfos it rests on: None is every slip that counts that day,
    #: () none at all (the sentence names no slip).
    basis: Sequence | None
    #: Any other fact the sentence states (`content_key`).
    extra: str = ""


def _alert(board, slip, state) -> _Alert:
    """The alert of one slip that counts."""
    info = board.index.infos[slip.pk]
    if state.key == "paired":
        return _Alert(_outcome_of(state.day), state.day.sentence, state.day, None)
    if state.key in _UNPAIRED and state.hint_date is not None:
        day = board.as_if_dated(info.supplier_id, state.hint_date, info.delivery_date)
        head = f"Reprise du {short_day(state.hint_date)}, à mettre au {short_day(info.delivery_date)} :"
        return _Alert(_outcome_of(day), f"{head} {day.sentence.partition(' : ')[2]}", day, None)
    if state.key in _UNPAIRED:
        return _unpaired_day(board, info)
    # « unreadable », « no_date ».
    return _Alert(TO_CHECK, state.reason or "Bon à vérifier.", None, None)


def _unpaired_day(board, info) -> _Alert:
    """`_alert` for a day with no pickup and no hint, decided on EVERY slip
    that counts that day (the module's docstring, step 2) - the latest may
    list nothing because the empties went back on the first delivery."""
    day_slips = board.effective_on(info.supplier_id, info.delivery_date)
    if not any(other.pk == info.pk for other in day_slips):
        day_slips = sorted([*day_slips, info], key=lambda other: other.key)
    board.index.ensure_lines(other.pk for other in day_slips)
    listing = [other for other in day_slips if other.lines]
    # The slip evaluated first, so a problem of its own is the one said.
    ordered = [info, *(other for other in day_slips if other.pk != info.pk)]
    troubled = [other for other in ordered if not other.lines and slip_problems(other)]
    if listing or not troubled:
        blank = _blank_supplier_pickup_day(info.delivery_date)
        if blank is not None:
            body = f"Une reprise sans « Repris par » le {blank:%d/%m} : précisez le fournisseur."
            # It rests on the blank pickup, not on the slips: a later slip of
            # the day is the same result.
            return _Alert(TO_CHECK, body, None, (), extra=blank.isoformat())
    if listing:
        labels = ", ".join(slip_label(other.number) for other in listing)
        body = f"Aucune reprise saisie pour la livraison du {short_day(info.delivery_date)} ({labels})."
        return _Alert(NO_PICKUP, body, None, listing)
    if troubled:
        return _Alert(TO_CHECK, slip_problems(troubled[0])[0], None, troubled)
    labels = ", ".join(slip_label(other.number) for other in day_slips)
    body = f"{labels[0].upper()}{labels[1:]} : aucun vide repris, aucune reprise saisie."
    return _Alert(MATCH, body, None, day_slips)


def _blank_supplier_pickup_day(day: date) -> date | None:
    """The day of the pickup with « Repris par » blank nearest `day` (within
    BLANK_SUPPLIER_DAYS; ties: the earlier), or None. Such a pickup is
    invisible to the slip's Board, which loads its supplier's pickups only."""
    days = (
        Pickup.objects.filter(
            supplier__isnull=True,
            date__range=(shifted(day, -BLANK_SUPPLIER_DAYS), shifted(day, BLANK_SUPPLIER_DAYS)),
        )
        .order_by()  # Meta.ordering would join the DISTINCT
        .values_list("date", flat=True)
        .distinct()
    )
    nearest = sorted(days, key=lambda other: (abs((other - day).days), other))
    return nearest[0] if nearest else None


def content_key(board, slip, outcome, day, basis=None, extra="") -> str:
    """« consignes: » + 24 hex of the SUBSTANCE of an alert: the supplier,
    the slip's day, the outcome, the slips it rests on (`basis`, else every
    slip that counts that day; `()` none), the numbers compared and any
    other fact it states (`extra`) - never a sentence, so a type renamed or
    a sentence reworded sends nothing again, while a count changed does.
    « Aucune reprise saisie » rests on the slips listing empties: one
    listing nothing arriving later is the same result. A pickup with
    « Repris par » blank rests on no slip, only on that pickup's date: every
    later slip of the day is the same result, another date another one."""
    info = board.index.infos[slip.pk]
    the_day = slip_day(slip)
    if basis is not None:
        pks = {counted.pk for counted in basis}
    else:
        pks = {slip.pk}
        if info.delivery_date is not None:
            pks.update(counted.pk for counted in board.effective_on(info.supplier_id, info.delivery_date))
    compared: list[tuple] = []
    if day is not None and day.comparison is not None:
        compared = sorted((row.returnable_type.pk, row.counted, row.on_slips) for row in day.comparison.rows)
        compared += sorted((group.designation, group.quantity) for group in day.comparison.unclassified)
    substance = f"{info.supplier_id}|{the_day.isoformat() if the_day else ''}|{outcome}|{sorted(pks)}|{compared}"
    if extra:
        substance += f"|{extra}"
    return "consignes:" + hashlib.sha256(substance.encode("utf-8")).hexdigest()[:24]
