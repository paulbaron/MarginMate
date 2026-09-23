"""Small shared form helpers.

Project-level rather than per-app because the problem below has now bitten
three different formsets across three different apps.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_date


def is_id(value) -> bool:
    """Whether `value`, read from a request, is an id: ASCII digits only.
    str.isdigit() also says yes to "²" or "٣" - and "²" is no int, so the
    query it reached raised: a 500 where a tampered form gets a message."""
    # And no longer than an SQLite integer can be: past it, the query
    # raised OverflowError.
    return isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18



def plain_number(value) -> str:
    """A decimal as a person writes it: 0.82, 2, 10, -1 - never "0.820",
    nor the "1E+1" Decimal.normalize() makes of 10. "" for nothing."""
    if value is None or value == "":
        return ""
    number = Decimal(str(value))
    if number == number.to_integral_value():
        return str(number.quantize(Decimal("1")))
    return format(number.normalize(), "f")


#: The two query parameters every page reads its window from - « du » and
#: « au », as the pages say it. One pair of names, so a window survives
#: being carried from a link on one page to another.
RANGE_START, RANGE_END = "du", "au"


@dataclass(frozen=True)
class DateRange:
    """A window a person typed: « du 01/02/2026 au 28/02/2026 ».

    **Both ends are included.** A person asking for the 28th means the 28th,
    and a document dated that day is in. This is deliberately NOT the
    half-open window the stock pages slice with (variance.movements_between,
    recipes.sales.sales_between): there, the opening date belongs to the
    count taken that day, so a delivery on it is already counted. Here there
    is no count - only two dates and what falls between them.

    Either end may be missing: « depuis le 1er février » and « jusqu'au 28 »
    are both windows a person asks for, and neither is an error.
    """

    start: date | None = None
    end: date | None = None

    def __bool__(self) -> bool:
        return self.start is not None or self.end is not None

    def limit(self, queryset, field: str):
        """`queryset` narrowed to the window on `field`.

        A row whose date is NULL drops out as soon as either end is set: an
        undated document is in no window (« Sans date » is where those are
        looked at). `__gte`/`__lte` already exclude NULL in SQL; this is
        said here because it is a decision, not an accident.
        """
        if self.start is not None:
            queryset = queryset.filter(**{f"{field}__gte": self.start})
        if self.end is not None:
            queryset = queryset.filter(**{f"{field}__lte": self.end})
        return queryset

    def holds(self, day: date | None) -> bool:
        """Whether `day` is in the window - for what is summed in Python
        rather than filtered in SQL (a stock movement's effective_date).

        An undated row is never held, the way `limit` drops it; an empty
        window holds everything, the way `limit` filters nothing.
        """
        if day is None:
            return not self
        if self.start is not None and day < self.start:
            return False
        if self.end is not None and day > self.end:
            return False
        return True

    @property
    def start_value(self) -> str:
        """What to put in `value=""` of an `<input type="date">`."""
        return self.start.isoformat() if self.start else ""

    @property
    def end_value(self) -> str:
        return self.end.isoformat() if self.end else ""

    @property
    def parameters(self) -> dict:
        """The window as query parameters, to hang on a link that must keep
        it - a filter chip, a tab. Empty when there is no window."""
        return {
            key: value
            for key, value in ((RANGE_START, self.start_value), (RANGE_END, self.end_value))
            if value
        }


def read_date(value) -> date | None:
    """One date read from a request, or None for anything else.

    `<input type="date">` posts ISO 8601, which is all this reads. Anything
    else - a stale bookmark, a hand-typed URL, a browser with no date input
    where someone wrote "hier" - is no window rather than a 500: parse_date
    returns None on a shape it doesn't know and RAISES on a shape it does
    know that is no date ("2026-02-30"), and both mean the same thing here.
    """
    try:
        return parse_date((value or "").strip())
    except (ValueError, TypeError):
        return None


def date_range(request, start_param: str = RANGE_START, end_param: str = RANGE_END) -> DateRange:
    """The window `?du=&au=` asks for, empty when it asks for none.

    Two dates the wrong way round are swapped rather than answered with an
    empty page: « du 28/02 au 01/02 » is a typo, and what the person means
    by it is unambiguous.
    """
    start = read_date(request.GET.get(start_param))
    end = read_date(request.GET.get(end_param))
    if start is not None and end is not None and start > end:
        start, end = end, start
    return DateRange(start, end)


class BlankRowTolerantFormMixin:
    """Makes a formset row count as blank unless the user actually filled
    something in, ignoring fields that only carry defaults or bookkeeping.

    Django only skips validating an extra formset row when its
    ``has_changed()`` is False, and ``has_changed()`` is True as soon as ANY
    field differs from its initial. That breaks two things at once:

    * A row removed client-side leaves a GAP in the posted indices (0, 1, 3
      with TOTAL_FORMS=4) - index 2 is absent from the POST entirely. A field
      declared with an ``initial`` then reads as "changed" (initial 0 vs
      nothing submitted), so the invisible row gets validated and fails with
      "this field is required" on a row the user cannot see or fix.
    * A row left at its pre-filled default (a VAT rate of 20%) reads as
      changed too, so an untouched trailing row blocks the save.

    Both are invisible from a happy-path test, because the browser's own
    payloads - non-contiguous indices, defaults echoed back - are not what a
    hand-written test naturally posts. See recipes/tests/test_forms.py.

    Subclasses list the field names that don't count as user input.
    """

    #: Fields that carry a default or bookkeeping value rather than a real
    #: user entry, and so must not by themselves make a row "filled in".
    bookkeeping_fields: tuple[str, ...] = ()

    def has_changed(self) -> bool:
        # Django asks this question for two different reasons, and only one
        # of them wants the bookkeeping fields ignored:
        #
        #   1. "May I skip validating this blank row?" - asked only of rows
        #      that are allowed to be blank (empty_permitted), i.e. the extra
        #      ones. That's the question this mixin exists to answer.
        #   2. "Has this SAVED row changed, so should I write it back?" -
        #      asked by BaseModelFormSet.save_existing_objects of every
        #      initial row, which never has empty_permitted set.
        #
        # Answering (2) with the bookkeeping fields stripped out silently
        # discards real edits: regrouping an existing ingredient via the "OU"
        # button changes nothing BUT `group`, so the save became a no-op and
        # the recipe reopened with the old grouping. Hence the guard.
        if not self.empty_permitted:
            return super().has_changed()
        return bool(set(self.changed_data) - set(self.bookkeeping_fields))


class BlankRowTolerantForm(BlankRowTolerantFormMixin, forms.Form):
    pass


class BlankRowTolerantModelForm(BlankRowTolerantFormMixin, forms.ModelForm):
    pass


class JobLogMixin(models.Model):
    """Everything a background job needs beyond its own fields.

    Both job models append timestamped lines to one text field, and both are
    driven by a daemon thread that cannot be relied on to reach its own
    `finally`: the dev server's autoreloader kills it outright on any code
    change. The job is then left RUNNING for ever, which blocks every future
    run behind "already in progress" - and its Cancel button does nothing,
    because there is no thread left to notice. That combination is a deadlock
    with no way out from the UI, and it is what stopped a three-year sales
    import from ever starting again.

    `last_heartbeat` is how a run says "still here" during the long silent
    stretches; anything quiet for STALE_AFTER is presumed dead and reaped.
    """

    #: Deliberately generous: one export of three years of tickets takes
    #: about a minute to generate and fetch, and declaring a working job dead
    #: is worse than waiting a little longer.
    STALE_AFTER = timedelta(minutes=10)

    last_heartbeat = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    #: "[+  12.3s] " - real information while a job runs, noise on the one
    #: line shown as a live status, where the spinner already says "running".
    _ELAPSED_PREFIX = re.compile(r"^\[\+\s*[\d.]+s\]\s*")

    def beat(self) -> None:
        """Say "still alive" without writing a log line - the download has
        long silent stretches, and a line every two seconds would bury the
        log it shares."""
        self.last_heartbeat = timezone.now()
        self.save(update_fields=["last_heartbeat"])

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.PENDING, self.Status.RUNNING)

    @property
    def is_stale(self) -> bool:
        """Nominally running, but nothing has been heard from it."""
        if not self.is_active:
            return False
        since = self.last_heartbeat or self.started_at
        return timezone.now() - since > self.STALE_AFTER

    @classmethod
    def reap_stale(cls) -> int:
        """Mark abandoned runs as failed, so they stop blocking new ones."""
        stale = [
            job
            for job in cls.objects.filter(status__in=[cls.Status.PENDING, cls.Status.RUNNING])
            if job.is_stale
        ]
        for job in stale:
            job.status = cls.Status.FAILED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at"])
            job.append_log(
                "Interrompue : plus aucune nouvelle de cette exécution. "
                "Le serveur a probablement redémarré pendant qu'elle tournait."
            )
        return len(stale)

    @property
    def log_lines(self) -> int:
        return len([line for line in (self.log or "").splitlines() if line.strip()])

    @property
    def last_log_line(self) -> str:
        for line in reversed((self.log or "").splitlines()):
            if line.strip():
                return self._ELAPSED_PREFIX.sub("", line).strip()
        return ""
