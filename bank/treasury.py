"""The bar's treasury - « Trésorerie », Banque's fourth tab: the balance of the
account on every day, worked out from the balances a person typed and the
operations imported, and the « écarts » where the two do not agree.

The owner's request (02/10/2026): he types the treasury on a date - usually
today's -, every other day is computed from the bank operations before it
(and after it, when the date is in the past), and where several typed
balances do not « match » (the operations between them do not explain the
two), the page asks for a resolution: an adjustment adding or removing the
difference, or a typed balance deleted.

**This module is the reference for the rules of reading.** Plain values in,
plain values out - `compute(points, lines, adjustments)` touches no database,
no request, no template and never reads today - except `load()`, which reads
the three tables in exactly `QUERIES` queries, whatever the history holds.
Money is Decimal, summed in Python (SQLite's own sums are not exact decimal),
through prefix sums looked up with `bisect`.

The vocabulary
--------------

* A **point** (`Point`, the model `TreasuryCheckpoint`, « point de
  trésorerie »): the balance a person read for ONE day - what the bank shows
  at the END of that day, every operation booked that day included. Days are
  `BankTransaction.operation_date`, the date every Banque page reads. One
  point per day at most. Signed (an overdraft is negative); with several
  accounts, their total.
* A **line** (`Line`): an imported operation - its day, its signed amount,
  its account and the local day it was imported on.
* An **adjustment** (`Correction`, the model `TreasuryAdjustment`,
  « ajustement »): a signed amount, dated on one day, that the operations do
  not carry, made by a person to settle two points. It counts in the
  treasury ONLY, never on « Dépenses », « Entrées d'argent » or the
  reconciliation - it is no `BankTransaction`, which would need a
  fingerprint, be counted as a debit or a credit and be offered invoices.

The rules of reading
--------------------

* **An adjustment COUNTS only between two points**: `first point's day <
  its day <= last point's day` - the stretch the consecutive pairs tile
  exactly. Any other - left outside when a point was deleted, or brought by
  « Données » - counts nowhere and is listed in `Treasury.orphans`, never
  silently added: counted, it would shift every balance with no gap saying
  why (review money-2).
* **M(d)**, the movements of day d: every line of that day, all accounts
  together, plus every COUNTED adjustment of that day. **S(d)** is the sum of
  M over every day on or before d - **d INCLUDED** (`bisect_right`), unlike
  `bank.income._card_sold_before`, which is strictly before: a point is an
  end-of-day balance, so its own day's movements are inside it.
* **known_from** = the earlier of the first line's day and the first point's
  day: nothing is known before it.
* **balance_on(d)** is None with no point at all, before `known_from` and past
  `horizon`. Otherwise its reference point r is the LAST point dated on or
  before d - or the FIRST point when d is before every point (computed
  backward) - and `balance_on(d) = r.balance + S(d) - S(r.day)`. On a
  point's own day it is exactly the typed balance; the next day it is that
  balance plus that next day's movements.
* **last_operation** is the latest line's day. **complete_through** is the
  last day whose operations are ALL imported: an export made during a day
  carries only part of it, and the owner usually imports a statement made
  that same day. So it is `last_operation` when the lines DATED on it were
  imported on a LATER day (the local day of the newest import of THOSE
  lines is after it), else the day before. The lines of that last day are
  the only witness: an older statement imported afterwards - the very thing
  a gap card asks for - says nothing of the last day, and taken as proof it
  turned the part of today not imported yet into a gap to resolve (review
  C1). And it is never the newest import's own day or later: a line dated
  after the day it was imported leaves that day as partial as any other.
  None without a line. With several accounts (more than one distinct
  non-blank `account`), each account's own is worked out and the EARLIEST
  is used; lines with a blank account then join none of them (they name no
  account to hold the others back).
* **horizon** = the later of the last point's day and `last_operation`: the
  last day a balance is given for. Nothing past it - no extrapolation.
* **provisional(d)**: d is not a point's day and the computation crosses a day
  whose operations are not all imported - forward (r.day <= d) when d >
  complete_through, backward (d < r.day) when r.day > complete_through,
  always when complete_through is None. A provisional figure is SHOWN,
  marked « provisoire », never hidden: the everyday case - today's balance
  typed while the statement is imported up to yesterday - is a backward
  computation across today, and it must still give the history.

The gaps (« écarts »)
---------------------

For each pair of CONSECUTIVE points (a, b) - `Gap` -, the non-consecutive
pairs following from them:

* `operations` is the sum of the lines dated in (a.day, b.day], `adjusted`
  the sum of the counted adjustments dated there (`corrections`), and
  `explained = operations + adjusted`;
* `missing = b.balance - a.balance - explained`: zero, the two agree
  (« concordent »), to the cent; positive, the account holds more than the
  operations explain;
* **pending** (« relevé à importer ») when complete_through is None or b.day >
  complete_through: the gap may be nothing but a statement not imported
  yet. A pending gap is never to resolve, never offered an adjustment, and
  shows no figure. It becomes a real gap - or vanishes - on the import that
  completes b.day. A point dated before the first line is therefore not
  pending (b.day <= complete_through): its gap says a statement may be
  missing.
* **to resolve** = not agreeing and not pending.
* **Suspect point**: a middle point whose two gaps are both not pending, at
  least one to resolve, and whose RAW gaps (`b - a - operations`,
  adjustments left out) cancel out and are not zero: without it its
  neighbours agree. Raw, so an adjustment made on one side neither makes a
  correct point suspect nor hides a wrong one (review money-7). The
  adjustments counted between its neighbours still count once it is gone:
  `Treasury.around(point)` names them, and without the point AND them the
  neighbours agree (review C2/C12).
* **Read before its day's operations**: on a gap to resolve, when `missing ==
  -M_ops(b.day) != 0` (M_ops: the lines of that day alone) b was read before
  them - the page offers « Dater ce point du » the day before; when `missing
  == +M_ops(a.day) != 0`, the same for a. Exact Decimal equality only.
* Computed live, never stored: an import bringing the missing operations
  makes a gap vanish by itself; an adjustment made and the real operation
  imported afterwards makes the gap reappear with the opposite sign, listing
  that adjustment among its `corrections` - deleting it is the fix.

What the page draws over a window (`span`, `curve`, `months`) is the history
narrowed to `[max(window.start, known_from), min(window.end, horizon)]`; the
headline, the gaps, the points and the adjustments are the whole history.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import NamedTuple

from django.utils import timezone

from common import AMBIGUOUS_THOUSANDS, DateRange, read_amount, read_date

from .models import BankTransaction, TreasuryAdjustment, TreasuryCheckpoint
from .statements import FIRST_DAY, MAX_AMOUNT

ZERO = Decimal("0")
ONE_DAY = timedelta(days=1)

#: What `load()` costs, whatever the history holds: the points, the
#: adjustments, and ONE query over the lines (pinned by a test).
QUERIES = 3

# -- What a typed balance or date is told (« Saisir un solde ») ------------------------------------------------------

#: A balance missing, unreadable, with three decimals or wider than the
#: (12, 2) column (« 1e999 » included).
BALANCE_UNREADABLE = "Solde illisible : tapez un montant comme 1 234,56 ou -250."
#: « 12.500 » or « -1,500 »: twelve thousand five hundred, or twelve and a
#: half? Read as the latter, a balance in the thousands became a gap in the
#: thousands - asked again rather than guessed (common.AMBIGUOUS_THOUSANDS).
BALANCE_AMBIGUOUS = "Solde ambigu : tapez 12 500 ou 12,50."
#: A date missing, or anything but the ISO date an `<input type="date">`
#: posts - never « 02/10/2026 », which en-us would read as 10 February.
DATE_UNREADABLE = "Date illisible."
#: A day before statements can exist, or after today: formatted with
#: `first` (FIRST_DAY) and `today`.
DATE_IMPOSSIBLE = "Date impossible : entre le {first:%d/%m/%Y} et aujourd'hui ({today:%d/%m/%Y})."


# -- Plain values ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    """A balance typed for one day: the account at the END of that day."""

    pk: int | None
    day: date
    balance: Decimal


@dataclass(frozen=True)
class Correction:
    """An adjustment (`TreasuryAdjustment`): a signed amount on one day."""

    pk: int | None
    day: date
    amount: Decimal
    reason: str


@dataclass(frozen=True)
class Line:
    """An imported operation, as the treasury reads it. `imported_on` is the
    LOCAL day it was imported on - what tells a statement made during a day
    from one made after it (`Treasury.complete_through`)."""

    day: date
    amount: Decimal
    account: str
    imported_on: date


@dataclass
class Gap:
    """Two CONSECUTIVE points and what the operations between them explain
    (see the module's docstring, « The gaps »)."""

    before: Point
    after: Point
    #: The lines dated in (before.day, after.day].
    operations: Decimal
    #: The counted adjustments dated there (`corrections`).
    adjusted: Decimal
    #: after - before - explained: positive, the account holds more than the
    #: operations explain.
    missing: Decimal
    #: « relevé à importer »: after.day is past `complete_through`.
    pending: bool
    #: The counted adjustments dated in (before.day, after.day], oldest first.
    corrections: list[Correction] = field(default_factory=list)
    #: The middle point without which the neighbours agree - set on both
    #: gaps around it (the earlier one when both ends of a gap qualify).
    #: Without it AND the adjustments `Treasury.around` names.
    suspect: Point | None = None
    #: The point whose day's operations the gap equals: read before them.
    read_before: Point | None = None

    @property
    def explained(self) -> Decimal:
        return self.operations + self.adjusted

    @property
    def agrees(self) -> bool:
        """The two points agree, to the cent (« concordent »)."""
        return self.missing == 0

    @property
    def to_resolve(self) -> bool:
        """What the page asks a resolution for: not agreeing, not pending."""
        return not self.agrees and not self.pending

    @property
    def raw_missing(self) -> Decimal:
        """The gap with the adjustments left out - what the suspect rule
        compares."""
        return self.after.balance - self.before.balance - self.operations

    @property
    def move_to(self) -> date | None:
        """The day « Dater ce point du … » moves `read_before` to: the day
        before its own. None when the hint does not apply."""
        if self.read_before is None or self.read_before.day == date.min:
            return None
        return self.read_before.day - ONE_DAY

    @property
    def too_big(self) -> bool:
        """An adjustment of `missing` would not fit the (12, 2) column - the
        difference of two balances can reach twice its width. Refused, never
        truncated."""
        return abs(self.missing) > MAX_AMOUNT


class Latest(NamedTuple):
    """The headline, « Trésorerie au <day> »."""

    day: date
    balance: Decimal
    provisional: bool
    #: The point it is computed from (« depuis le solde du JJ/MM »).
    reference: Point


@dataclass
class MonthRow:
    """One month of the window: the row covers [start, end], the month met
    with the span - its figures are summed over those days only, so a
    window starting mid-month shows no false gap."""

    #: The month's first day.
    month: date
    start: date
    end: date
    #: Money in and money out over [start, end] (debits <= 0), lines only.
    credits: Decimal
    debits: Decimal
    #: The counted adjustments over [start, end].
    adjustments: Decimal
    #: balance_on(end), and whether it is provisional.
    closing: Decimal | None
    closing_provisional: bool
    #: The `missing` of the gaps TO RESOLVE whose after.day is in [start, end],
    #: signed and summed (what the column sorts by).
    gap_to_resolve: Decimal
    #: How many gaps to resolve end in [start, end]. Asked apart from the
    #: sum, never read off it: one wrong balance between two right ones (a
    #: suspect point) makes two gaps of opposite sign, which cancel - the sum
    #: then reads 0, « concorde », over a month the page asks two resolutions
    #: for (review C10).
    gaps_to_resolve: int
    #: A pending gap's after.day is in [start, end] (no figure: « relevé à
    #: importer »).
    gap_pending: bool

    @property
    def partial(self) -> bool:
        """The row stops before the month's last day (« au JJ/MM »)."""
        return self.end.day != monthrange(self.end.year, self.end.month)[1]


# -- Prefix sums ----------------------------------------------------------------------------------------------------


class _Running:
    """Amounts by day, added up through any day - that day INCLUDED
    (`bisect_right`), the end-of-day rule. One sorted list and its running
    totals: each lookup is O(log n), never a sum per day asked."""

    def __init__(self, by_day: dict[date, Decimal]):
        self.days = sorted(by_day)
        self.totals = [ZERO]
        for day in self.days:
            self.totals.append(self.totals[-1] + by_day[day])

    def through(self, day: date) -> Decimal:
        """The amounts dated on or before `day`."""
        return self.totals[bisect_right(self.days, day)]

    def within(self, start: date, end: date) -> Decimal:
        """The amounts dated in [start, end], both included."""
        if end < start:
            return ZERO
        return self.totals[bisect_right(self.days, end)] - self.totals[bisect_left(self.days, start)]


def _day_before(day: date) -> date | None:
    return None if day == date.min else day - ONE_DAY


def _complete_through(lines: list[Line], accounts: int) -> date | None:
    """The last day whose operations are all imported (module docstring):
    per account when there are several, the earliest of them.

    For each account, the last day is whole when a line DATED that day was
    imported a later day - the import of an older line says nothing of it
    (review C1: a July export imported on 03/10 completed 02/10, and the
    part of 02/10 not imported yet became a gap to resolve). And never the
    newest import's own day or later: a line dated after the day it was
    imported (an export of 02/10 carrying an operation of 03/10) leaves that
    import's day as partial as any other."""
    if not lines:
        return None
    if accounts > 1:
        groups: dict[str, list[Line]] = defaultdict(list)
        for line in lines:
            if line.account.strip():
                groups[line.account.strip()].append(line)
        sets = list(groups.values())
    else:
        sets = [lines]
    days = []
    for group in sets:
        last = max(line.day for line in group)
        last_imported = max(line.imported_on for line in group if line.day == last)
        complete = last if last_imported > last else _day_before(last)
        newest_import = max(line.imported_on for line in group)
        if complete is not None and newest_import <= complete:
            complete = _day_before(newest_import)
        if complete is None:
            return None
        days.append(complete)
    return min(days)


# -- The treasury ---------------------------------------------------------------------------------------------------


class Treasury:
    """Everything the page reads, computed once from plain values
    (`compute`). The attributes:

    * `points` - one per day, oldest first;
    * `corrections` - the adjustments that COUNT (between two points), and
      `orphans` - those that count nowhere, both oldest first;
    * `gaps` - one per pair of consecutive points, oldest first;
    * `first_operation`, `last_operation`, `complete_through`, `known_from`,
      `horizon` - days, or None (module docstring);
    * `accounts` - how many distinct non-blank accounts the lines carry.
    """

    def __init__(self, points: Iterable[Point], lines: Iterable[Line], adjustments: Iterable[Correction]):
        by_day: dict[date, Point] = {}
        for point in points:
            by_day[point.day] = point  # one per day, as the model's unique date guarantees
        self.points: list[Point] = [by_day[day] for day in sorted(by_day)]
        self._point_days = [point.day for point in self.points]
        self._point_day_set = set(self._point_days)

        lines = list(lines)
        operations: dict[date, Decimal] = defaultdict(lambda: ZERO)
        credits: dict[date, Decimal] = defaultdict(lambda: ZERO)
        debits: dict[date, Decimal] = defaultdict(lambda: ZERO)
        for line in lines:
            operations[line.day] += line.amount
            if line.amount > 0:
                credits[line.day] += line.amount
            elif line.amount < 0:
                debits[line.day] += line.amount
        self._operations_by_day = dict(operations)
        self.first_operation: date | None = min(operations) if operations else None
        self.last_operation: date | None = max(operations) if operations else None
        self.accounts: int = len({line.account.strip() for line in lines if line.account.strip()})
        self.complete_through: date | None = _complete_through(lines, self.accounts)

        ordered = sorted(adjustments, key=lambda one: one.day)  # stable: the given order within a day
        if len(self.points) >= 2:
            first, last = self._point_days[0], self._point_days[-1]
            self.corrections: list[Correction] = [one for one in ordered if first < one.day <= last]
            self.orphans: list[Correction] = [one for one in ordered if not first < one.day <= last]
        else:
            self.corrections, self.orphans = [], ordered
        adjusted: dict[date, Decimal] = defaultdict(lambda: ZERO)
        for one in self.corrections:
            adjusted[one.day] += one.amount

        self._operations = _Running(operations)
        self._adjusted = _Running(adjusted)
        self._credits = _Running(credits)
        self._debits = _Running(debits)
        self._correction_days = [one.day for one in self.corrections]
        #: The days the curve must show: a movement or a point.
        self._event_days = sorted(set(operations) | set(adjusted) | self._point_day_set)

        known = [day for day in (self.first_operation, self._point_days[0] if self.points else None) if day is not None]
        self.known_from: date | None = min(known) if known else None
        self.horizon: date | None = None
        if self.points:
            self.horizon = max(self._point_days[-1], self.last_operation or self._point_days[-1])
        self.gaps: list[Gap] = self._gaps()

    # -- Reading one day --------------------------------------------------------------------------------------------

    def operations_on(self, day: date) -> Decimal:
        """M_ops(day): the lines of that day alone, adjustments left out."""
        return self._operations_by_day.get(day, ZERO)

    def _through(self, day: date) -> Decimal:
        """S(day): every movement on or before `day`, that day included."""
        return self._operations.through(day) + self._adjusted.through(day)

    def _holds(self, day: date) -> bool:
        """Whether a balance is given for `day`: a point exists and `day` is in
        [known_from, horizon]."""
        return self.horizon is not None and self.known_from is not None and self.known_from <= day <= self.horizon

    def reference(self, day: date) -> Point | None:
        """The point `day` is computed from: the last one dated on or before
        it, else the first (backward). None with no point."""
        if not self.points:
            return None
        return self.points[max(bisect_right(self._point_days, day) - 1, 0)]

    def balance_on(self, day: date) -> Decimal | None:
        """The balance at the END of `day` - r.balance + S(day) - S(r.day) -,
        None with no point, before `known_from` or past `horizon`."""
        if not self._holds(day):
            return None
        reference = self.reference(day)
        assert reference is not None  # _holds: there is a point
        return reference.balance + self._through(day) - self._through(reference.day)

    def provisional(self, day: date) -> bool:
        """Whether `balance_on(day)` crosses a day whose operations are not
        all imported (module docstring). False on a point's own day, and
        where no balance is given."""
        if not self._holds(day) or day in self._point_day_set:
            return False
        if self.complete_through is None:
            return True
        reference = self.reference(day)
        assert reference is not None
        if reference.day <= day:
            return day > self.complete_through
        return reference.day > self.complete_through

    # -- The gaps ---------------------------------------------------------------------------------------------------

    def _pending(self, day: date) -> bool:
        return self.complete_through is None or day > self.complete_through

    def _gaps(self) -> list[Gap]:
        gaps = []
        for before, after in pairwise(self.points):
            operations = self._operations.through(after.day) - self._operations.through(before.day)
            corrections = self.corrections[
                bisect_right(self._correction_days, before.day) : bisect_right(self._correction_days, after.day)
            ]
            adjusted = sum((one.amount for one in corrections), ZERO)
            gaps.append(
                Gap(
                    before=before,
                    after=after,
                    operations=operations,
                    adjusted=adjusted,
                    missing=after.balance - before.balance - operations - adjusted,
                    pending=self._pending(after.day),
                    corrections=corrections,
                )
            )
        for previous, following in pairwise(gaps):
            if previous.pending or following.pending or not (previous.to_resolve or following.to_resolve):
                continue
            if previous.raw_missing != 0 and previous.raw_missing == -following.raw_missing:
                previous.suspect = previous.suspect or previous.after
                following.suspect = following.suspect or previous.after
        for gap in gaps:
            if not gap.to_resolve:
                continue
            after_day, before_day = self.operations_on(gap.after.day), self.operations_on(gap.before.day)
            if after_day != 0 and gap.missing == -after_day:
                gap.read_before = gap.after
            elif before_day != 0 and gap.missing == before_day:
                gap.read_before = gap.before
        return gaps

    @property
    def to_resolve(self) -> list[Gap]:
        """The gaps the page asks a resolution for, oldest first."""
        return [gap for gap in self.gaps if gap.to_resolve]

    @property
    def pending(self) -> list[Gap]:
        """The gaps waiting for a statement (« relevé à importer »)."""
        return [gap for gap in self.gaps if gap.pending]

    def gap_between(self, before_pk: int | None, after_pk: int | None) -> Gap | None:
        """The gap of these two points when they are CONSECUTIVE - what the
        adjustment POST checks again. None when either is gone or another
        point now sits between them."""
        if before_pk is None or after_pk is None:
            return None
        return next((gap for gap in self.gaps if gap.before.pk == before_pk and gap.after.pk == after_pk), None)

    def gap_ending_on(self, day: date) -> Gap | None:
        """The gap whose later point is dated `day` - « Avec le précédent » of
        that point. None for the first point (or no point that day)."""
        return next((gap for gap in self.gaps if gap.after.day == day), None)

    def point(self, pk: int | None) -> Point | None:
        return next((point for point in self.points if pk is not None and point.pk == pk), None)

    def around(self, point: Point) -> list[Correction]:
        """The counted adjustments between `point`'s two neighbours - dated
        in (previous point's day, next point's day] -, oldest first: the
        `corrections` of its two gaps together.

        What has to go WITH a suspect point (`Gap.suspect`) for its
        neighbours to agree. The suspect is found on the RAW gaps, so the
        neighbours' gap without it is minus the adjustments in that stretch:
        « Sans le point du JJ/MM, les points voisins concordent » holds only
        when these sum to 0, and the one that keeps them apart may be dated
        on the suspect's own day - its first card's stretch, not its second's
        (review C2/C12). Without the point AND these, they agree.

        An end point has one gap, and gives its corrections. A point this
        treasury does not hold (another day, or another balance that day)
        has none."""
        index = bisect_left(self._point_days, point.day)
        if index == len(self.points) or self.points[index] != point:
            return []
        low = self.points[index - 1].day if index > 0 else point.day
        high = self.points[index + 1].day if index + 1 < len(self.points) else point.day
        return self.corrections[bisect_right(self._correction_days, low) : bisect_right(self._correction_days, high)]

    def unsettled(self, point: Point) -> bool:
        """`point` is one end of a gap to resolve - the headline's amber is lit
        only when its reference point is."""
        return any(gap.to_resolve and point in (gap.before, gap.after) for gap in self.gaps)

    def orphaned_by_deleting(self, pk: int | None) -> list[Correction]:
        """The adjustments that count now and would count nowhere once that
        point is deleted - what its confirmation and its message name."""
        return self._left_outside([point.day for point in self.points if point.pk != pk])

    def orphaned_by_moving(self, pk: int | None, day: date) -> list[Correction]:
        """The same, once that point is dated `day` (« Dater ce point du … »:
        an adjustment dated on the last point's day is left outside when it
        moves back)."""
        return self._left_outside([day if point.pk == pk else point.day for point in self.points])

    def _left_outside(self, point_days: list[date]) -> list[Correction]:
        if len(point_days) < 2:
            return list(self.corrections)
        first, last = min(point_days), max(point_days)
        return [one for one in self.corrections if not first < one.day <= last]

    # -- The headline -----------------------------------------------------------------------------------------------

    @property
    def latest(self) -> Latest | None:
        """« Trésorerie au <horizon> »: its balance, whether it is provisional,
        and the point it is computed from. None without a point."""
        if self.horizon is None:
            return None
        balance, reference = self.balance_on(self.horizon), self.reference(self.horizon)
        assert balance is not None and reference is not None
        return Latest(self.horizon, balance, self.provisional(self.horizon), reference)

    # -- A window ---------------------------------------------------------------------------------------------------

    def span(self, window: DateRange | None = None) -> tuple[date, date] | None:
        """The days the window shows: [max(window.start, known_from),
        min(window.end, horizon)]. None when that is empty - no point, or a
        window entirely before or after the data. The empty window (« tout »)
        is the whole history. A far start (?du=0001-01-01) costs nothing: the
        span starts at known_from."""
        if self.horizon is None or self.known_from is None:
            return None
        start, end = self.known_from, self.horizon
        if window is not None:
            if window.start is not None and window.start > start:
                start = window.start
            if window.end is not None and window.end < end:
                end = window.end
        return (start, end) if start <= end else None

    def curve(self, window: DateRange | None = None) -> list[tuple[date, Decimal]]:
        """(day, balance) oldest first, inside the span only: its first day,
        every day with a movement or a point, its last day - and, before each
        of those whose previous one is more than a day earlier, the day
        before it, so the line draws STEPS: a balance does not slope between
        two movements. One point per day (`views._build_balance_svg`)."""
        bounds = self.span(window)
        if bounds is None:
            return []
        start, end = bounds
        days = [start, *self._event_days[bisect_right(self._event_days, start) : bisect_left(self._event_days, end)]]
        if end != start:
            days.append(end)
        curve: list[tuple[date, Decimal]] = []
        previous = None
        for day in days:
            if previous is not None and (day - previous).days > 1:
                step = day - ONE_DAY
                curve.append((step, self._shown(step)))
            curve.append((day, self._shown(day)))
            previous = day
        return curve

    def _shown(self, day: date) -> Decimal:
        balance = self.balance_on(day)
        assert balance is not None  # every caller stays inside the span
        return balance

    def curve_provisional(self, window: DateRange | None = None) -> tuple[date, date] | None:
        """The first and last provisional days inside the span, for the
        muted line under the curve. Provisional is everything but the points'
        days when the first point is past `complete_through` (or nothing is
        imported), else every day past `complete_through` but the points':
        one stretch, whose ends are trimmed of point days (a point inside it
        is exact, the line simplifies). None when nothing shown is
        provisional."""
        bounds = self.span(window)
        if bounds is None:
            return None
        low, high = bounds
        ready = self.complete_through
        if ready is not None and self._point_days[0] <= ready:
            if high <= ready:
                return None
            low = max(low, ready + ONE_DAY)
        while low in self._point_day_set:
            if low >= high:
                return None
            low += ONE_DAY
        while high in self._point_day_set:
            high -= ONE_DAY  # stops at `low` at the latest, which is no point day
        return low, high

    def months(self, window: DateRange | None = None) -> list[MonthRow]:
        """Every month meeting the span, oldest first, each row covering the
        month met with the span (`MonthRow`). A far window start costs
        nothing and never computes a day below date.min: rows start at the
        span's start."""
        bounds = self.span(window)
        if bounds is None:
            return []
        start, end = bounds
        after_days = [gap.after.day for gap in self.gaps]
        rows = []
        first = start.replace(day=1)
        while True:
            last = first.replace(day=monthrange(first.year, first.month)[1])
            row_start, row_end = max(first, start), min(last, end)
            gaps = self.gaps[bisect_left(after_days, row_start) : bisect_right(after_days, row_end)]
            to_resolve = [gap for gap in gaps if gap.to_resolve]
            closing = self.balance_on(row_end)
            rows.append(
                MonthRow(
                    month=first,
                    start=row_start,
                    end=row_end,
                    credits=self._credits.within(row_start, row_end),
                    debits=self._debits.within(row_start, row_end),
                    adjustments=self._adjusted.within(row_start, row_end),
                    closing=closing,
                    closing_provisional=self.provisional(row_end),
                    gap_to_resolve=sum((gap.missing for gap in to_resolve), ZERO),
                    gaps_to_resolve=len(to_resolve),
                    gap_pending=any(gap.pending for gap in gaps),
                )
            )
            if last >= end:
                return rows
            first = last + ONE_DAY


def compute(points: Iterable[Point], lines: Iterable[Line], adjustments: Iterable[Correction]) -> Treasury:
    """The treasury of these points, lines and adjustments - pure: no
    database, no today."""
    return Treasury(points, lines, adjustments)


def _local_day(moment: datetime) -> date:
    """The day `moment` fell on, in the site's time zone. Through
    `localtime`, never `localdate`: tests freeze today by patching
    `timezone.localdate` - one attribute of one module for every caller -
    and every import would then read as made « today »."""
    if timezone.is_naive(moment):
        return moment.date()
    return timezone.localtime(moment).date()


def load() -> Treasury:
    """The treasury of this database, in exactly `QUERIES` queries: the
    points, the adjustments, and one query over every line, explicitly
    ordered (`BankTransaction.Meta.ordering` is newest first)."""
    points = [
        Point(pk, day, balance)
        for pk, day, balance in TreasuryCheckpoint.objects.order_by("date").values_list("pk", "date", "balance")
    ]
    adjustments = [
        Correction(pk, day, amount, reason)
        for pk, day, amount, reason in TreasuryAdjustment.objects.order_by("date", "pk").values_list(
            "pk", "date", "amount", "reason"
        )
    ]
    lines = [
        Line(day, amount, account, _local_day(imported_at))
        for day, amount, account, imported_at in BankTransaction.objects.order_by("operation_date", "pk").values_list(
            "operation_date", "amount", "account", "imported_at"
        )
    ]
    return compute(points, lines, adjustments)


# -- Reading what was typed (« Saisir un solde ») -------------------------------------------------------------------


def read_balance(typed) -> tuple[Decimal | None, str]:
    """(the balance, "") or (None, the French refusal) for a typed balance.

    Signed (an overdraft is « -250 »), read by `common.read_amount`: spaces
    between the thousands, a decimal comma or point, at most two decimals,
    within the (12, 2) column - anything else is BALANCE_UNREADABLE, a blank
    included. A single separator followed by exactly three digits
    (« 12.500 », « -1,500 ») is BALANCE_AMBIGUOUS, asked before reading:
    read_amount would take it for 12,50. A NUL is the form field's own
    refusal (`forms.NUL_REFUSED`), and unreadable here."""
    text = typed if isinstance(typed, str) else ""
    compact = "".join(text.split()).replace("\N{MINUS SIGN}", "-")
    if compact.endswith("-") and not compact.startswith("-"):
        compact = "-" + compact[:-1]  # « 1,500- » is « -1,500 »
    if AMBIGUOUS_THOUSANDS.fullmatch(compact):
        return None, BALANCE_AMBIGUOUS
    amount = read_amount(text)
    if amount is None:
        return None, BALANCE_UNREADABLE
    return amount, ""


def check_point_date(value, *, today: date) -> tuple[date | None, str]:
    """(the day, "") or (None, the French refusal) for a point's date.

    `value` is what the form posted - an ISO date, read by `common.read_date`
    (never « 02/10/2026 »: en-us reads it as 10 February) - or a date already
    read. Missing or unreadable is DATE_UNREADABLE; outside 01/01/2000 ..
    `today` is DATE_IMPOSSIBLE - a future point would move the headline into
    the future. `today` is the caller's (`timezone.localdate()`): this module
    never reads it."""
    if isinstance(value, datetime):
        day = None
    elif isinstance(value, date):
        day = value
    else:
        day = read_date(value) if isinstance(value, str) else None
    if day is None:
        return None, DATE_UNREADABLE
    if not FIRST_DAY <= day <= today:
        return None, DATE_IMPOSSIBLE.format(first=FIRST_DAY, today=today)
    return day, ""
