"""How far each gathered source has been searched without a gap
(models.GatherCoverage), and where an automatic gather starts it again.

A source is a mailbox invoice type (« type-<id> ») or a slip format
(« bons-<id> »): the only sources an automatic gather searches.

- `searched(code, since, until, own=..., bounded=..., floor=...)` is said by
  the gather when a source's search COMPLETED (tasks._gather_email,
  tasks._gather_slips): the mail server handed over every mail of the range
  (generic_email.IncompleteSearch otherwise), its import or store loop ran
  to the end, nothing raised, the run was not cancelled, and none of its
  documents was left unimported for want of the OCR or the database (one
  refused for what it is - a duplicate, a file its reader cannot read -
  still lets it count: retried, it would hold the source back for good). A
  source in error, a cancelled run or a killed one says nothing - what it
  did not reach is searched again, and so does one whose search settings
  were saved meanwhile (tasks._searched: it searched for the old mails).
  Every gather says it, by hand or automatic. The rule:
  - a search starting on or before the day after `searched_until` - or,
    never searched, starting on or before its own start `own` and reaching
    it - moves it to `until`, never back;
  - one starting after a gap - a manual gather from Achats' default start,
    the newest invoice any source brought in - leaves it where it is, so the
    next automatic run searches the gap; never searched (or searched over a
    past period ending before its own start), the source is then
    pinned at its own start (`searched_until` = `own` + OVERLAP_DAYS), which
    the search's own imports would otherwise move past the gap;
  - `bounded` (an automatic run cut at the 90-day bound) moves it to `until`
    all the same: the stretch below the bound is `pending_from`'s, up to
    `pending_until`;
  - a search starting on or before `pending_from` and reaching it ends the
    stretch when it reaches `pending_until` (the day before the bound that
    cut it; on a row written before it was kept, the day before today's
    bound), and otherwise moves `pending_from` to the day after it stopped;
    one ending before it changes nothing. `floor` (a slip format's, returnables.mail.lookback_floor): no
    search starts before it, so a stretch begun before it starts there.
- `restart(code, own)`: the source's search settings changed (its patterns,
  invoices/views.py, returnables/views.py) - its coverage goes back to `own`,
  never forward: the days covered were searched for other mails.
- `unattended_start(code, own, today)` is where an automatic run starts:
  `searched_until` less OVERLAP_DAYS when known - never the supplier's own
  newest invoice, which another channel (a photo, a PDF dropped by hand) may
  have brought in while the mailbox failed -, else `own`. Never before
  today − DEFAULT_LOOKBACK_DAYS: a start the bound cuts becomes (or lowers)
  `pending_from` and sets `pending_until` to the day before that bound (the
  latest cut's), and while one is pending every automatic run's line says
  the stretch to catch up by hand (« catch_up », « au » the bound that cut
  it - never today's, which moves a day a day).
- A code with no row is read ONCE from the history: the furthest any
  finished gather (SUCCESS, by hand or automatic) whose line for it has no
  error reached - its end (`range_end`), never past its local start day - is
  `searched_until`: a past period gathered again covered nothing after it,
  and gathered last it hides no older gather that reached further.
  A FAILED run is never one: a run killed mid-search is reaped FAILED with
  its current line clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.utils import timezone

from .models import GatherCoverage, ScrapeJob

#: The days re-checked before the last day searched (tasks.OVERLAP_DAYS).
OVERLAP_DAYS = 3
#: An automatic run never searches further back (tasks.DEFAULT_LOOKBACK_DAYS).
DEFAULT_LOOKBACK_DAYS = 90
#: Said on a source's line while a stretch is to be caught up by hand.
CATCH_UP_TO_DO = "Rattrapage à faire à la main depuis Factures, du {start:%d/%m/%Y} au {bound:%d/%m/%Y}"


@dataclass(frozen=True)
class Start:
    """Where an automatic run searches a source from."""

    start: date
    #: The sentence for the source's line (« catch_up »), "" when none.
    catch_up: str
    #: The 90-day bound cut the start this time.
    bounded: bool


def lookback_bound(today: date) -> date:
    return today - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def stretch_end(pending_until: date | None, today: date) -> date:
    """The last day of a stretch to catch up by hand: the day before the
    bound that cut it (`pending_until`), else - a row written before that
    was kept - the day before today's bound."""
    return pending_until if pending_until is not None else lookback_bound(today) - timedelta(days=1)


def catch_up_sentence(pending_from: date, today: date, pending_until: date | None = None) -> str:
    """The stretch left to catch up by hand: from `pending_from` to the bound
    that cut it - the day after its last day, from which on the automatic
    runs searched it."""
    return CATCH_UP_TO_DO.format(start=pending_from, bound=stretch_end(pending_until, today) + timedelta(days=1))


def _from_history(code: str) -> date | None:
    """How far the SUCCESS gathers whose line for `code` has no error
    searched it - the furthest of them, not the newest's: a past period
    gathered again last hid an older gather that searched up to its own day
    - or None. Each reached the end it was asked for (every source searches
    up to the job's `range_end`), never past the local day it started - a
    past period gathered again by hand covered nothing after its end. A job
    with no `range_end` (every gather has set it since the first version) is
    read by its start day. Newest first, the walk stops at the first that
    reached its own start day: none older can reach further."""
    history = (
        ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER, status=ScrapeJob.Status.SUCCESS)
        .order_by("-started_at", "-pk")
        .values_list("started_at", "range_end", "progress")
    )
    furthest = None
    for started_at, range_end, progress in history.iterator(chunk_size=100):
        entry = progress.get(code) if isinstance(progress, dict) else None
        if isinstance(entry, dict) and not entry.get("error"):
            started = timezone.localdate(started_at)
            reach = min(started, range_end) if range_end else started
            furthest = reach if furthest is None else max(furthest, reach)
            if reach == started:
                break
    return furthest


def _row(code: str) -> GatherCoverage:
    """`code`'s row, made from the history the first time it is asked."""
    row = GatherCoverage.objects.filter(code=code).first()
    if row is None:
        row, _ = GatherCoverage.objects.get_or_create(code=code, defaults={"searched_until": _from_history(code)})
    return row


def unattended_start(code: str, own: date, today: date | None = None) -> Start:
    """Where an automatic run searches `code` from (the module's docstring);
    `own` is the source's own start, used only when it was never searched."""
    today = today or timezone.localdate()
    row = _row(code)
    start = row.searched_until - timedelta(days=OVERLAP_DAYS) if row.searched_until else own
    bound = lookback_bound(today)
    bounded = start < bound
    if bounded:
        pending = min(row.pending_from, start) if row.pending_from else start
        # The stretch ends the day before the bound that cut it - the latest
        # cut's: this run searches from that bound on.
        end = bound - timedelta(days=1)
        if row.pending_from is not None and row.pending_until is not None:
            end = max(row.pending_until, end)
        if (pending, end) != (row.pending_from, row.pending_until):
            row.pending_from, row.pending_until = pending, end
            row.save(update_fields=["pending_from", "pending_until", "updated_at"])
        start = bound
    sentence = catch_up_sentence(row.pending_from, today, row.pending_until) if row.pending_from else ""
    return Start(start=start, catch_up=sentence, bounded=bounded)


def searched(
    code: str,
    since: date,
    until: date,
    *,
    own: date,
    bounded: bool = False,
    today: date | None = None,
    floor: date | None = None,
) -> None:
    """`code` was searched whole from `since` to `until` (the module's
    docstring). A day after today was not searched yet: `until` stops at
    today. `own`: the source's own start, where an automatic run starts it
    when it was never searched - worked out BEFORE the search, whose own
    imports move a supplier's newest invoice. `floor`: the earliest day
    this source can be searched at all (a slip format's,
    returnables.mail.lookback_floor), None for none."""
    today = today or timezone.localdate()
    until = min(until, today)
    row = _row(code)
    fields = []
    known = row.searched_until
    # Never searched, a search counts only if it covered its own start: one
    # ending before it (a past period) covered nothing an automatic run
    # would search, and moved its next start months back.
    contiguous = since <= known + timedelta(days=1) if known is not None else since <= own <= until
    if bounded or contiguous:
        reached = until if bounded or known is None else max(known, until)
        if reached != known:
            row.searched_until = reached
            fields.append("searched_until")
    elif known is None:
        # Never searched, and searched from after its own start: the days
        # before stay the next automatic run's - pinned here, since the
        # supplier's newest invoice (its own start) moves with what this
        # very search imported.
        row.searched_until = min(own + timedelta(days=OVERLAP_DAYS), today)
        fields.append("searched_until")
    pending = row.pending_from
    if pending is not None:
        # Before the floor nothing can be searched: what is left of the
        # stretch starts there.
        start = max(pending, floor) if floor is not None else pending
        if since <= start <= until:
            # A search reaching the stretch's end - the day before the bound
            # that cut it - ends it; a shorter one moves its start past what
            # it covered.
            if until >= stretch_end(row.pending_until, today):
                row.pending_from = row.pending_until = None
                fields += ["pending_from", "pending_until"]
            else:
                row.pending_from = until + timedelta(days=1)
                fields.append("pending_from")
    if fields:
        row.save(update_fields=[*fields, "updated_at"])


def restart(code: str, own: date) -> None:
    """`code`'s search settings changed (its patterns): the days it covered
    were searched for other mails. Its coverage goes back to its own start
    `own` - never forward -, so the next automatic run searches from there
    (unattended_start takes OVERLAP_DAYS off again). The row is kept, or
    made: deleted, it would be read again from the history, whose last clean
    line is the old settings' search; left None, a later search from any
    start would count as its first. Called inside the save's transaction."""
    until = own + timedelta(days=OVERLAP_DAYS)
    row, created = GatherCoverage.objects.get_or_create(code=code, defaults={"searched_until": until})
    if not created and (row.searched_until is None or row.searched_until > until):
        row.searched_until = until
        row.save(update_fields=["searched_until", "updated_at"])


def pending(codes) -> dict[str, date]:
    """code → the start of its stretch to catch up by hand, for `codes`."""
    return dict(
        GatherCoverage.objects.filter(code__in=list(codes), pending_from__isnull=False).values_list(
            "code", "pending_from"
        )
    )


def catch_ups(codes, today: date) -> dict[str, str]:
    """code → the sentence of its stretch to catch up by hand, for `codes`
    (catch_up_sentence: up to the bound that cut it)."""
    rows = GatherCoverage.objects.filter(code__in=list(codes), pending_from__isnull=False).values_list(
        "code", "pending_from", "pending_until"
    )
    return {code: catch_up_sentence(start, today, end) for code, start, end in rows}
