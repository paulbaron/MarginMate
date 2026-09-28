"""A month of an employee's hours, as data - pure of request and template.

The owner's sheets were typed by hand from a typical week, correcting the
days that were not typical, and this module does exactly that and nothing
cleverer:

* **A month nobody saved is the typical week** (`planned_days`): « Travail »
  with the typical hours where they are above 0, « Repos » at 0 elsewhere.
  It is worked out when it is drawn, never stored, so the page and the PDF
  of an unsaved month show exactly what saving would store.
* **Public holidays are NOT zeroed.** They travel with their day (its name,
  « Assomption »), and whether one was worked is the owner's decision: the
  owner's own sheets print 1 May at 0 and 14 July worked. Assuming either
  way is a sheet the employee signs with a wrong figure on it.
  `mark_holidays_off` is the one action that makes them « Férié chômé » -
  those on a working day: a holiday on a day off stays a day off.
* **A day off is never a day of leave.** « Du … au … » with an absence
  leaves the days off of the range as they are (`_is_day_off`), and says
  which: a week of leave is the days that would have been worked.
* **Only « Travail » hours exist.** An absence (« Congés payés », « Arrêt
  maladie »…) is 0 h whatever was posted; a half day off is « Travail » with
  its hours and a note. The database refuses anything else
  (`TimesheetDay`'s check constraint), so the sums below cannot meet hours
  that print and do not count.
* **Hours typed on a « Repos » day mean he worked.** A Sunday of the typical
  week is drawn « Repos », and a person who worked it types 4 in its hours
  and saves: that is « Travail » 4 h, and the save says so
  (`Outcome.adjustments`). But a day the person has just SWITCHED to
  « Repos » with its old hours still in the field (a page without
  JavaScript) is a day off: the kind they chose wins, as it does for an
  absence.
* **A week belongs to the month it is printed in.** Weeks are ISO weeks,
  Monday to Sunday, and a week straddling two months has a partial total in
  each - the total of the days THIS month holds, as the owner's sheets add
  them up.
* **A saved month's typical week is its own.** Its sheet copied the
  employee's week when it was first saved, and everything about the month
  reads that copy (`MonthSheet.planned_week`): each day's typical hours, what
  differs from them, « Semaine type », what was planned and the difference,
  a day put back to « Travail » without its hours. The employee's week of
  today reaches it through « Revenir à la semaine type » only. A month
  nobody saved is the employee's week, as it stands.
* **A month sent for signature is read-only** until « Corriger ce mois »
  (`signature_requests.reopen_month`): every write goes through `_store`,
  which refuses it (`MonthLocked`) while a request holds the month.

French names come from this module's constants, never from Django:
`LANGUAGE_CODE` is en-us, and a translated month name would say « June ».
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import cached_property, lru_cache

from django.db import transaction

from .models import ABSENCE_KINDS, MAX_DAY_HOURS, WEEKDAY_FIELDS, Employee, Timesheet, TimesheetDay, TypicalWeek

Kind = TimesheetDay.Kind
KIND_CHOICES = Kind.choices
KIND_LABELS = dict(Kind.choices)
WORK = Kind.WORK.value
REST = Kind.REST.value
PUBLIC_HOLIDAY = Kind.PUBLIC_HOLIDAY.value

ZERO = Decimal("0")
HUNDREDTH = Decimal("0.01")
#: What a negative figure starts with on screen, as elsewhere in the app. It
#: is NOT in cp1252 (the PDF's encoding): whatever prints a difference into
#: the PDF turns it into « - » first.
MINUS = "−"
NOTE_MAX_LENGTH = TimesheetDay._meta.get_field("note").max_length

DAY_NAMES = ("Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche")
DAY_ABBREVIATIONS = ("Lu", "Ma", "Me", "Je", "Ve", "Sa", "Di")
MONTH_NAMES = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

#: The years a month in an address may name. A month's page links to the
#: month before and the one after, and `date` stops at year 1 and 9999: an
#: address naming 0001-01 would 500 drawing « ← décembre 0 ».
FIRST_YEAR, LAST_YEAR = 1900, 2999


# -- Months and days, in French ---------------------------------------------------------------------------------


def first_of_month(day: date) -> date:
    return day.replace(day=1)


def month_days(month: date) -> list[date]:
    """Every day of `month`'s month, the 1st to the last (28 to 31 of them)."""
    first = first_of_month(month)
    length = calendar.monthrange(first.year, first.month)[1]
    return [first + timedelta(days=offset) for offset in range(length)]


def previous_month(month: date) -> date:
    return first_of_month(first_of_month(month) - timedelta(days=1))


def next_month(month: date) -> date:
    return first_of_month(first_of_month(month) + timedelta(days=31))


_MONTH_SLUG = re.compile(r"([0-9]{4})-([0-9]{2})")


def parse_month(text) -> date | None:
    """« 2026-06 » (the month in a page's address) → 1 June 2026. Anything
    else - « 2026-13 », « 2026-6 », « juin », a year out of range - is None,
    which a view answers with a 404, never a 500."""
    if not isinstance(text, str):
        return None
    match = _MONTH_SLUG.fullmatch(text)
    if match is None:
        return None
    year, month = int(match[1]), int(match[2])
    if not (FIRST_YEAR <= year <= LAST_YEAR and 1 <= month <= 12):
        return None
    return date(year, month, 1)


def month_slug(month: date) -> str:
    """The month as its page's address writes it: « 2026-06 »."""
    return f"{month.year:04d}-{month.month:02d}"


def month_name(month: date) -> str:
    """« juin », « août »."""
    return MONTH_NAMES[month.month - 1]


def month_label(month: date) -> str:
    """« juin 2026 »."""
    return f"{month_name(month)} {month.year}"


def month_title(month: date) -> str:
    """« Mois de juin 2026 », and « Mois d'août 2026 »: the elision before a
    vowel (avril, août, octobre) is what a French reader expects to see."""
    name = month_name(month)
    of = "d'" if name[0] in "aeiouy" else "de "
    return f"Mois {of}{name} {month.year}"


def day_name(day: date) -> str:
    """« Mardi 4 » - the whole name of the day, then its number. « Lundi 1 »,
    not « 1er »: it is a column of numbers, as on the owner's sheets."""
    return f"{DAY_NAMES[day.weekday()]} {day.day}"


def _day_in_prose(day: date) -> str:
    return "1er" if day.day == 1 else str(day.day)


def span_label(first: date, last: date) -> str:
    """Some days, in a sentence: « le 2 juin 2026 », « du 11 au 18 juin
    2026 », « du 29 juin au 5 juillet 2026 », « du 1er au 3 juin 2026 » -
    in prose the 1st is « 1er »."""
    if first > last:
        first, last = last, first
    if first == last:
        return f"le {_day_in_prose(first)} {month_label(first)}"
    if (first.year, first.month) == (last.year, last.month):
        return f"du {_day_in_prose(first)} au {_day_in_prose(last)} {month_label(last)}"
    if first.year == last.year:
        return f"du {_day_in_prose(first)} {month_name(first)} au {_day_in_prose(last)} {month_label(last)}"
    return f"du {_day_in_prose(first)} {month_label(first)} au {_day_in_prose(last)} {month_label(last)}"


# -- Public holidays --------------------------------------------------------------------------------------------


def easter_sunday(year: int) -> date:
    """Easter Sunday of the Gregorian calendar - the « anonymous » algorithm
    (Meeus/Jones/Butcher), exact for every Gregorian year with no table."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday_shift = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_shift) // 451
    month, day = divmod(h + weekday_shift - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=64)
def _public_holidays(year: int) -> tuple[tuple[date, str], ...]:
    easter = easter_sunday(year)
    named = (
        (date(year, 1, 1), "Jour de l'an"),
        (easter + timedelta(days=1), "Lundi de Pâques"),
        (date(year, 5, 1), "Fête du Travail"),
        (date(year, 5, 8), "Victoire 1945"),
        (easter + timedelta(days=39), "Ascension"),
        (easter + timedelta(days=50), "Lundi de Pentecôte"),
        (date(year, 7, 14), "Fête nationale"),
        (date(year, 8, 15), "Assomption"),
        (date(year, 11, 1), "Toussaint"),
        (date(year, 11, 11), "Armistice"),
        (date(year, 12, 25), "Noël"),
    )
    holidays: dict[date, str] = {}
    for day, name in named:
        # The Ascension (Easter + 39) can fall on 1 May or 8 May (2008 and
        # 1975): one day with two names, never one name silently lost.
        holidays[day] = f"{holidays[day]} et {name}" if day in holidays else name
    return tuple(sorted(holidays.items()))


def french_public_holidays(year: int) -> dict[date, str]:
    """The eleven public holidays of metropolitan France - Alsace-Moselle's
    Good Friday and 26 December are not in it - as date → name."""
    return dict(_public_holidays(year))


def month_holidays(month: date) -> dict[date, str]:
    """The public holidays that fall in `month`'s month."""
    first = first_of_month(month)
    return {day: name for day, name in _public_holidays(first.year) if day.month == first.month}


# -- Hours, as typed and as printed -----------------------------------------------------------------------------


class HoursError(ValueError):
    """Hours as typed that are no hours. The message is French, says which
    day when told, and is meant to be shown as it is."""


_DECIMAL_HOURS = re.compile(r"[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+")
# « 7h30 », « 7h », « 7:30 ». Minutes are two digits: « 7h5 » could be 7h05
# or 7h50, and guessing is how a sheet ends up with a figure nobody typed.
_CLOCK_HOURS = re.compile(r"([0-9]+)(?:[hH]([0-9]{2})?|:([0-9]{2}))")
_WHITESPACE = re.compile(r"\s+")
# Two figures a space apart: « 1 5 », a slip for 1,5, is not 15 h. `\s` is
# every space, the no-break and narrow ones a French keyboard types too.
_SPLIT_DIGITS = re.compile(r"[0-9]\s+[0-9]")


def _prefixed(where: str, message: str) -> str:
    return f"{where} : {message}" if where else message[0].upper() + message[1:]


def _checked(number: Decimal, typed: str, where: str, *, clock: bool = False) -> Decimal:
    if not number.is_finite():
        raise HoursError(_prefixed(where, f"« {typed} » n'est pas un nombre d'heures."))
    if number < 0:
        raise HoursError(_prefixed(where, "les heures ne peuvent pas être négatives."))
    if number > MAX_DAY_HOURS:
        raise HoursError(_prefixed(where, f"« {typed} », c'est plus que les 24 h d'une journée."))
    rounded = number.quantize(HUNDREDTH)
    if rounded != number:
        if clock:
            raise HoursError(
                _prefixed(
                    where,
                    f"« {typed} » ne tombe pas juste en centièmes d'heure ({format_hours(rounded)} environ) : "
                    f"écrivez-le en heures décimales, par exemple {format_hours(rounded)}.",
                )
            )
        raise HoursError(
            _prefixed(where, f"« {typed} » a plus de deux décimales : les heures s'écrivent au centième (7,25).")
        )
    return rounded


def parse_hours(text, where: str = "") -> Decimal:
    """Hours as a person types them: « 7 », « 7,5 », « 7.5 », « 7h30 »,
    « 7 h 30 », « 7:30 »; blank is 0. Returns a Decimal to the hundredth.

    Refused, with a `HoursError` whose French message starts with `where`
    (« Mardi 4 », « Lundi »): garbage, a negative figure, more than 24 h,
    two figures a space apart, and anything that does not come to two
    decimals once converted - 7h20 is 7,333… h, and rounding it silently
    would print a figure nobody typed.

    Spaces are otherwise ignored (« 7 h 30 », « 7 , 5 »), but only once no
    space sits between two digits: taken out first, they made « 1 5 » -
    a slip for 1,5 - into 15 h and « 0 5 » into 5 h, without a word.
    """
    typed = "" if text is None else str(text).strip()
    if _SPLIT_DIGITS.search(typed):
        shown = typed if len(typed) <= 30 else typed[:30] + "…"
        raise HoursError(
            _prefixed(
                where,
                f"« {shown} » : un espace entre deux chiffres ne dit pas quel nombre d'heures c'est "
                "(écrivez par exemple 7,5 ou 7h30, sans espace entre les chiffres).",
            )
        )
    compact = _WHITESPACE.sub("", typed)
    if not compact:
        return ZERO.quantize(HUNDREDTH)
    if compact[0] in "-" + MINUS:
        raise HoursError(_prefixed(where, "les heures ne peuvent pas être négatives."))
    # Past 12 characters it is no duration anybody meant, and a thousand
    # digits handed to Decimal is work for nothing.
    if len(compact) <= 12:
        if _DECIMAL_HOURS.fullmatch(compact):
            return _checked(Decimal(compact.replace(",", ".")), typed, where)
        clock = _CLOCK_HOURS.fullmatch(compact)
        if clock:
            minutes = int(clock[2] or clock[3] or 0)
            if minutes > 59:
                raise HoursError(_prefixed(where, f"« {typed} » : les minutes vont de 00 à 59."))
            number = Decimal(clock[1]) + Decimal(minutes) / Decimal(60)
            return _checked(number, typed, where, clock=True)
    shown = typed if len(typed) <= 30 else typed[:30] + "…"
    raise HoursError(
        _prefixed(where, f"« {shown} » n'est pas un nombre d'heures (écrivez par exemple 7, 7,5 ou 7h30).")
    )


def parse_optional_hours(text, where: str = "") -> Decimal | None:
    """`parse_hours` for a field where blank means « not said » rather than
    0 - « Du … au … »'s « heures facultatives »: blank there is None, so a
    « Travail » range takes each day's typical hours instead of putting 0 h
    on every day of it."""
    if text is None or not _WHITESPACE.sub("", str(text)):
        return None
    return parse_hours(text, where)


def checked_hours(value, where: str = "") -> Decimal:
    """`value` as the hours of one day, from code rather than from a form: a
    Decimal or an int (a string is read by `parse_hours`). A float is
    refused outright - hours are Decimal everywhere, like money."""
    if isinstance(value, (float, bool)):
        raise TypeError("Les heures sont un Decimal, jamais un float.")
    if isinstance(value, str) or value is None:
        return parse_hours(value, where)
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise HoursError(_prefixed(where, f"« {value} » n'est pas un nombre d'heures.")) from None
    return _checked(number, str(value), where)


def format_hours(value) -> str:
    """« 7 », « 7,5 », « 7,25 », « 0 »: a comma, no trailing zero. The one
    way hours are written, on the page AND in the PDF. "" for None."""
    if value is None or value == "":
        return ""
    number = Decimal(str(value))
    if number == 0:
        return "0"
    if number == number.to_integral_value():
        text = str(number.quantize(Decimal("1")))
    else:
        text = format(number.normalize(), "f")
    return text.replace(".", ",").replace("-", MINUS)


def format_hours_difference(value) -> str:
    """« +3 h », « −8 h », « 0 h » - a month against its typical week."""
    number = Decimal(str(value))
    if number == 0:
        return "0 h"
    sign = "+" if number > 0 else MINUS
    return f"{sign}{format_hours(abs(number))} h"


def week_summary(employee: Employee) -> str:
    """The typical week in one line: « Ma 7,5 · Me 6 · Je–Sa 7,5 · 36 h /
    semaine » (an invented week, like every example here).
    Neighbouring days with the same hours read as one run; a day at 0 is not
    named."""
    week = employee.typical_week
    parts = []
    weekday = 0
    while weekday < 7:
        hours = week[weekday]
        if hours <= 0:
            weekday += 1
            continue
        last = weekday
        while last + 1 < 7 and week[last + 1] == hours:
            last += 1
        days = DAY_ABBREVIATIONS[weekday]
        if last > weekday:
            days = f"{days}–{DAY_ABBREVIATIONS[last]}"
        parts.append(f"{days} {format_hours(hours)}")
        weekday = last + 1
    parts.append(f"{format_hours(employee.weekly_hours)} h / semaine")
    return " · ".join(parts)


# -- The fields of the month's form -----------------------------------------------------------------------------
# Named by the day's ISO date, not by a row index: there is no formset, so no
# gap in the indices can shift a day's hours onto its neighbour.


def hours_field(day: date) -> str:
    return f"heures-{day.isoformat()}"


def kind_field(day: date) -> str:
    return f"motif-{day.isoformat()}"


def note_field(day: date) -> str:
    return f"note-{day.isoformat()}"


# -- The month as data ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DayEntry:
    """One day as it is stored, or as the typical week plans it."""

    date: date
    hours: Decimal
    kind: str
    note: str = ""


def planned_day(week: TypicalWeek, day: date) -> DayEntry:
    """`day` as `week` plans it - an Employee's week, or the copy a saved
    Timesheet keeps."""
    typical = week.typical_hours(day.weekday())
    if typical > 0:
        return DayEntry(day, typical, WORK)
    return DayEntry(day, ZERO, REST)


def planned_days(week: TypicalWeek, month: date) -> list[DayEntry]:
    """The month from the typical week: « Travail » with the typical hours
    where they are above 0, « Repos » at 0 elsewhere. Holidays included, as
    they are - see the module's docstring."""
    return [planned_day(week, day) for day in month_days(month)]


@dataclass(frozen=True)
class MonthDay:
    """A day as the page and the PDF draw it."""

    date: date
    hours: Decimal
    kind: str
    note: str
    holiday: str             # « Assomption », "" when the day is no public holiday
    typical_hours: Decimal   # what the typical week plans for this weekday
    hours_input: str         # what the hours field shows: the hours, or the text typed when the page answers an error
    error: str = ""          # French, for this day, when a post was refused

    @property
    def name(self) -> str:
        return day_name(self.date)

    @property
    def weekday_name(self) -> str:
        return DAY_NAMES[self.date.weekday()]

    @property
    def iso(self) -> str:
        return self.date.isoformat()

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def is_work(self) -> bool:
        return self.kind == WORK

    @property
    def is_rest(self) -> bool:
        return self.kind == REST

    @property
    def is_absence(self) -> bool:
        return self.kind in ABSENCE_KINDS

    @property
    def worked_hours(self) -> Decimal:
        return self.hours if self.is_work else ZERO

    @property
    def differs(self) -> bool:
        """Whether the day is not what the typical week plans (for a discreet
        highlight): another kind, or other hours. A note alone is no change."""
        planned_kind = WORK if self.typical_hours > 0 else REST
        planned_hours = self.typical_hours if planned_kind == WORK else ZERO
        return self.kind != planned_kind or self.worked_hours != planned_hours

    @property
    def hours_text(self) -> str:
        """The « Heures » column: the hours of a « Travail » day (« 0 » when it
        is 0 - the owner's sheets print it), blank for any other day."""
        return format_hours(self.hours) if self.is_work else ""

    @property
    def note_text(self) -> str:
        """The « Motif / note » column. An absence prints its label (and the
        holiday's name for « Férié chômé »), then the note; « Travail » and
        « Repos » print the note alone - a day off is blank, like the owner's
        sheets."""
        if not self.is_absence:
            return self.note
        parts = [self.label]
        if self.kind == PUBLIC_HOLIDAY and self.holiday:
            parts.append(self.holiday)
        if self.note:
            parts.append(self.note)
        return " — ".join(parts)

    @property
    def holiday_label(self) -> str:
        return f"Férié : {self.holiday}" if self.holiday else ""

    @property
    def typical_hours_text(self) -> str:
        return format_hours(self.typical_hours)

    @property
    def hours_field(self) -> str:
        return hours_field(self.date)

    @property
    def kind_field(self) -> str:
        return kind_field(self.date)

    @property
    def note_field(self) -> str:
        return note_field(self.date)


@dataclass(frozen=True)
class MonthWeek:
    """The days of one ISO week (Monday to Sunday) that the month holds."""

    days: tuple[MonthDay, ...]

    @property
    def first(self) -> date:
        return self.days[0].date

    @property
    def last(self) -> date:
        return self.days[-1].date

    @property
    def monday(self) -> date:
        """The ISO week's Monday - in the month before for a partial first week."""
        return self.first - timedelta(days=self.first.weekday())

    @property
    def partial(self) -> bool:
        """The week runs over the month's edge: its total is the days of
        THIS month only."""
        return len(self.days) < 7

    @property
    def total(self) -> Decimal:
        return sum((day.worked_hours for day in self.days), ZERO)

    @property
    def typical_total(self) -> Decimal:
        return sum((day.typical_hours for day in self.days), ZERO)

    @property
    def total_text(self) -> str:
        return format_hours(self.total)

    @property
    def label(self) -> str:
        return "Total (semaine incomplète)" if self.partial else "Total semaine"


@dataclass(frozen=True)
class AbsenceCount:
    kind: str
    label: str
    days: int

    @property
    def days_text(self) -> str:
        return f"{self.days} jour" if self.days == 1 else f"{self.days} jours"


@dataclass(frozen=True)
class MonthSummary:
    worked_hours: Decimal
    # What the typical week plans for the month's days, all of them.
    planned_hours: Decimal
    # « Travail » days with hours above 0.
    days_worked: int
    # Per absence kind, in the order of TimesheetDay.Kind, only the kinds
    # that occur.
    absences: tuple[AbsenceCount, ...]
    # Of `planned_hours`, what the typical week planned for the days that
    # were an absence (leave, a holiday off, sick…).
    absence_hours: Decimal = ZERO

    @property
    def difference(self) -> Decimal:
        """The month's « +3 h » or « −8 h »: the hours worked against what
        the typical week planned for the days that were NOT an absence. An
        absence is counted in days, by kind, and its hours are no shortfall:
        said against every planned day, a month worked exactly as planned
        but for a week of leave printed « Écart : −36 h » - the leave, as a
        deficit, under « Lu et approuvé » (review, 28/09)."""
        return self.worked_hours - (self.planned_hours - self.absence_hours)

    @property
    def difference_label(self) -> str:
        """« Écart hors absences » when absences were set aside, so the
        figure cannot be read against the whole plan printed above it."""
        return "Écart hors absences" if self.absence_hours else "Écart"

    @property
    def worked_text(self) -> str:
        return format_hours(self.worked_hours)

    @property
    def planned_text(self) -> str:
        return format_hours(self.planned_hours)

    @property
    def absence_hours_text(self) -> str:
        return format_hours(self.absence_hours)

    @property
    def difference_text(self) -> str:
        return format_hours_difference(self.difference)

    @property
    def days_worked_text(self) -> str:
        return f"{self.days_worked} jour" if self.days_worked == 1 else f"{self.days_worked} jours"


@dataclass(frozen=True)
class MonthSheet:
    """One employee's month: what the page edits and the PDF prints."""

    employee: Employee
    month: date                    # always the 1st
    days: tuple[MonthDay, ...]     # every day of the month, in order
    timesheet: Timesheet | None    # None: not saved yet, the days are the typical week
    errors: tuple[str, ...] = ()   # French, when the page answers a refused post

    @property
    def saved(self) -> bool:
        return self.timesheet is not None

    @cached_property
    def weeks(self) -> tuple[MonthWeek, ...]:
        weeks: list[list[MonthDay]] = []
        for day in self.days:
            if not weeks or day.date.weekday() == 0:
                weeks.append([])
            weeks[-1].append(day)
        return tuple(MonthWeek(tuple(days)) for days in weeks)

    @cached_property
    def summary(self) -> MonthSummary:
        counts = {kind: 0 for kind in KIND_LABELS if kind in ABSENCE_KINDS}
        for day in self.days:
            if day.is_absence:
                counts[day.kind] += 1
        return MonthSummary(
            worked_hours=sum((day.worked_hours for day in self.days), ZERO),
            planned_hours=sum((day.typical_hours for day in self.days), ZERO),
            days_worked=sum(1 for day in self.days if day.is_work and day.hours > 0),
            absences=tuple(AbsenceCount(kind, KIND_LABELS[kind], count) for kind, count in counts.items() if count),
            absence_hours=sum((day.typical_hours for day in self.days if day.is_absence), ZERO),
        )

    @property
    def holidays(self) -> tuple[MonthDay, ...]:
        return tuple(day for day in self.days if day.holiday)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def employee_name(self) -> str:
        return self.employee.display_name

    @property
    def title(self) -> str:
        return month_title(self.month)

    @property
    def label(self) -> str:
        return month_label(self.month)

    @property
    def slug(self) -> str:
        return month_slug(self.month)

    @property
    def previous_month(self) -> date:
        return previous_month(self.month)

    @property
    def next_month(self) -> date:
        return next_month(self.month)

    @property
    def planned_week(self) -> TypicalWeek:
        """The typical week this month is read against: the copy its sheet
        took when it was first saved, or - not saved yet - the employee's."""
        return self.timesheet if self.timesheet is not None else self.employee

    @property
    def weekly_hours(self) -> Decimal:
        return self.planned_week.weekly_hours

    @property
    def weekly_hours_text(self) -> str:
        return format_hours(self.weekly_hours)

    @property
    def employee_week_changed(self) -> bool:
        """Saved with a typical week the employee no longer has: the page
        says so, and « Revenir à la semaine type » is what takes the new one."""
        return self.saved and self.timesheet.typical_week != self.employee.typical_week

    @property
    def employee_weekly_hours_text(self) -> str:
        return format_hours(self.employee.weekly_hours)


# -- What a post says -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PostedDay:
    """One day as the month's form posted it. A field the post did not carry
    is None and keeps the day's current value: a disabled input is not
    posted at all, and missing is not blank."""

    date: date
    hours: Decimal | None = None
    kind: str | None = None
    note: str | None = None
    hours_text: str | None = None   # as typed, to draw back into the field with an error
    error: str = ""


@dataclass(frozen=True)
class PostedMonth:
    month: date
    days: dict[date, PostedDay]
    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def read_posted_month(month: date, data: Mapping) -> PostedMonth:
    """The month's form, read from `data` (a QueryDict, or any mapping):
    for every day of the month, its `heures-…`, `motif-…` and `note-…`
    fields. A field for a day outside the month is never looked at. Every
    problem is a French sentence naming its day, in `errors` - never an
    exception."""
    first = first_of_month(month)
    days: dict[date, PostedDay] = {}
    errors: list[str] = []
    for day in month_days(first):
        names = (hours_field(day), kind_field(day), note_field(day))
        if not any(name in data for name in names):
            continue
        where = day_name(day)
        problems = []
        hours = hours_text = kind = note = None
        if names[0] in data:
            hours_text = data.get(names[0]) or ""
            try:
                hours = parse_hours(hours_text, where)
            except HoursError as error:
                problems.append(str(error))
        if names[1] in data:
            posted_kind = (data.get(names[1]) or "").strip()
            if posted_kind in KIND_LABELS:
                kind = posted_kind
            else:
                problems.append(f"{where} : motif inconnu « {posted_kind[:30]} ».")
        if names[2] in data:
            note = " ".join((data.get(names[2]) or "").split())
            if len(note) > NOTE_MAX_LENGTH:
                problems.append(f"{where} : la note dépasse {NOTE_MAX_LENGTH} caractères.")
            unprintable = _unprintable(note)
            if unprintable:
                problems.append(f"{where} : {unprintable[0].lower()}{unprintable[1:]}")
        days[day] = PostedDay(day, hours, kind, note, hours_text, " ".join(problems))
        errors.extend(problems)
    return PostedMonth(first, days, tuple(errors))


def _settle(before: DayEntry, posted: PostedDay, typical: Decimal) -> tuple[DayEntry, str]:
    """What one posted day stores, given what it was before the post: the
    post's fields where it carried them, the rest kept - and the two rules of
    the module's docstring (only « Travail » has hours; hours on a day that
    was already « Repos » mean « Travail »). The second value is the French
    sentence saying so when the second rule applied, else ""."""
    kind = posted.kind if posted.kind is not None else before.kind
    note = posted.note if posted.note is not None else before.note
    if posted.hours is not None:
        hours = posted.hours
    elif kind == WORK:
        # No hours posted for a day that works: its own if it was worked
        # already, else the typical week's.
        hours = before.hours if before.kind == WORK else typical
    else:
        hours = ZERO
    adjustment = ""
    if kind == REST and hours > 0 and before.kind == REST:
        kind = WORK
        adjustment = (
            f"{day_name(before.date)} : {format_hours(hours)} h saisies sur un jour de repos, comptées en Travail."
        )
    if kind != WORK:
        hours = ZERO
    return DayEntry(before.date, hours, kind, note), adjustment


# -- Reading and building a month -------------------------------------------------------------------------------


def _entries(week: TypicalWeek, month: date, rows: Iterable) -> dict[date, DayEntry]:
    """The month's days: the stored rows where there are some, `week`'s
    plan for any day missing. A row outside the month is ignored."""
    entries = {entry.date: entry for entry in planned_days(week, month)}
    for row in rows:
        if row.date in entries:
            entries[row.date] = DayEntry(row.date, row.hours, row.kind, row.note)
    return entries


def _week(employee: Employee, timesheet: Timesheet | None) -> TypicalWeek:
    """The typical week a month is read against (`MonthSheet.planned_week`)."""
    return timesheet if timesheet is not None else employee


def _current(employee: Employee, month: date) -> tuple[Timesheet | None, dict[date, DayEntry]]:
    timesheet = Timesheet.objects.filter(employee=employee, month=month).first()
    rows = timesheet.days.all() if timesheet is not None else ()
    return timesheet, _entries(_week(employee, timesheet), month, rows)


def build_sheet(
    employee: Employee,
    month: date,
    entries: Iterable[DayEntry] = (),
    *,
    timesheet: Timesheet | None = None,
    posted: PostedMonth | None = None,
) -> MonthSheet:
    """A `MonthSheet` from days already in hand, with no query: `entries`
    are the stored days (any day missing is the typical week's), `posted`
    a refused post to draw back as it was typed. `month_sheet` is this plus
    the reading; a test can build a sheet from an unsaved Employee. The
    typical week is `timesheet`'s own when there is one."""
    first = first_of_month(month)
    week = _week(employee, timesheet)
    current = _entries(week, first, ())
    for entry in entries:
        if entry.date not in current:
            raise ValueError(f"Le {entry.date:%d/%m/%Y} n'est pas en {month_label(first)}.")
        current[entry.date] = entry
    holidays = month_holidays(first)
    days = []
    for day, entry in current.items():
        typical = week.typical_hours(day.weekday())
        shown, error, hours_input = entry, "", None
        posted_day = posted.days.get(day) if posted is not None else None
        if posted_day is not None:
            shown, _adjustment = _settle(entry, posted_day, typical)
            error, hours_input = posted_day.error, posted_day.hours_text
        if hours_input is None:
            hours_input = format_hours(shown.hours) if shown.kind == WORK else ""
        days.append(
            MonthDay(
                date=day,
                hours=shown.hours if shown.kind == WORK else ZERO,
                kind=shown.kind,
                note=shown.note,
                holiday=holidays.get(day, ""),
                typical_hours=typical,
                hours_input=hours_input,
                error=error,
            )
        )
    errors = posted.errors if posted is not None else ()
    return MonthSheet(employee=employee, month=first, days=tuple(days), timesheet=timesheet, errors=errors)


def month_sheet(employee: Employee, month: date, posted: PostedMonth | None = None) -> MonthSheet:
    """The month of `employee` holding `month`: the saved sheet's days if
    there is one, else the typical week (`saved` False). With `posted`, the
    days carry what was typed, for a page answering an error."""
    first = first_of_month(month)
    timesheet, current = _current(employee, first)
    return build_sheet(employee, first, current.values(), timesheet=timesheet, posted=posted)


def saved_month_sheets(employee: Employee) -> list[MonthSheet]:
    """Every saved month of `employee`, newest first, in two queries however
    many there are (the employee's page lists them with their totals)."""
    sheets = []
    for timesheet in employee.timesheets.order_by("-month").prefetch_related("days"):
        entries = _entries(timesheet, timesheet.month, timesheet.days.all())
        sheets.append(build_sheet(employee, timesheet.month, entries.values(), timesheet=timesheet))
    return sheets


# -- Writing a month --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What a write did, for the message the page answers with."""

    timesheet: Timesheet | None     # None only when nothing was written
    created: bool                   # this write saved the month for the first time
    changed: tuple[date, ...]       # the days now different from before (saved, or the typical week)
    touched: tuple[date, ...]       # the days the action was about
    adjustments: tuple[str, ...] = ()   # French: what was read otherwise than posted
    # « Revenir à la semaine type » only: the month's weekly hours before it
    # took the employee's week of today, when that week was another one.
    week_before: Decimal | None = None
    # « Du … au … » and the holidays: the days off among `touched`, left as
    # they were (`_is_day_off`).
    left_alone: tuple[date, ...] = ()

    @property
    def week_changed(self) -> bool:
        return self.week_before is not None


class MonthLocked(ValueError):
    """The month is held by a signature request - waiting, signed by the
    employee, or finished (staff/signature_requests.py): it is not written.
    A ValueError, so the pages that already answer one say it."""


MONTH_LOCKED = (
    "Ce mois est envoyé pour signature, ou signé : il ne se modifie plus. « Corriger ce mois » annule la "
    "demande et rouvre la saisie - une nouvelle version sera à signer."
)


def _refuse_if_held(timesheet: Timesheet | None) -> None:
    """Read-only is enforced HERE, where every write of a month goes, and not
    only by a page drawing its form disabled: a stale page or a hand-typed
    POST must not change hours the employee is signing, or has signed."""
    if timesheet is None:
        return
    # Imported here: the workflow module imports this one.
    from .signature_requests import month_is_locked

    if month_is_locked(timesheet):
        raise MonthLocked(MONTH_LOCKED)


def _store(employee, month, timesheet, before, after, touched, adjustments=(), *, take_week=False) -> Outcome:
    """Write `after` - every day of the month - creating the sheet on its
    first save, with a copy of the employee's typical week (`take_week`
    copies it again onto a sheet already saved). Rows are created or
    updated, never deleted and re-made, so saving the same thing twice
    changes nothing but `updated_at`. A month a signature request holds is
    refused (`MonthLocked`) before anything is written."""
    _refuse_if_held(timesheet)
    created = timesheet is None
    week_before = None
    # « Dernier enregistrement » on the list of months, whatever else is written.
    saved_fields = ["updated_at"]
    if created:
        timesheet = Timesheet.objects.create(employee=employee, month=month, **employee.week_values())
        rows = {}
    else:
        rows = {row.date: row for row in timesheet.days.all()}
        if take_week and timesheet.typical_week != employee.typical_week:
            week_before = timesheet.weekly_hours
            for name, hours in employee.week_values().items():
                setattr(timesheet, name, hours)
            saved_fields += WEEKDAY_FIELDS
    new_rows, updated_rows = [], []
    for day, entry in after.items():
        # Only « Travail » has hours - enforced here as well as in
        # TimesheetDay.save(), which bulk writes do not call.
        hours = entry.hours if entry.kind == WORK else ZERO
        row = rows.get(day)
        if row is None:
            new_rows.append(TimesheetDay(timesheet=timesheet, date=day, hours=hours, kind=entry.kind, note=entry.note))
        elif (row.hours, row.kind, row.note) != (hours, entry.kind, entry.note):
            row.hours, row.kind, row.note = hours, entry.kind, entry.note
            updated_rows.append(row)
    TimesheetDay.objects.bulk_create(new_rows)
    TimesheetDay.objects.bulk_update(updated_rows, ["hours", "kind", "note"])
    timesheet.save(update_fields=saved_fields)
    changed = tuple(day for day in sorted(after) if after[day] != before[day])
    return Outcome(timesheet, created, changed, tuple(sorted(touched)), tuple(adjustments), week_before)


def _unprintable(note: str) -> str:
    """"" - or the sentence refusing a note that holds a character the
    printed sheet cannot write (`pdf.unprintable_characters`): it would print
    « ? », and the employee signs the printed sheet."""
    from .pdf import unprintable_characters

    found = unprintable_characters(note)
    if not found:
        return ""
    quoted = [f"« {char} »" for char in found]
    shown = quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} et {quoted[-1]}"
    return (
        f"La note contient {shown}, que la fiche imprimée ne sait pas écrire (elle imprimerait « ? ») : "
        f"remplacez-{'les' if len(found) > 1 else 'le'}."
    )


def _checked_note(note: str | None) -> str | None:
    if note is None:
        return None
    note = " ".join(str(note).split())
    if len(note) > NOTE_MAX_LENGTH:
        raise ValueError(f"La note dépasse {NOTE_MAX_LENGTH} caractères.")
    unprintable = _unprintable(note)
    if unprintable:
        raise ValueError(unprintable)
    return note


def save_month(
    employee: Employee, month: date, posted_days: Iterable[PostedDay] | Mapping[date, PostedDay]
) -> Outcome:
    """Save the month from what the form posted, in one transaction: the
    sheet is created on the first save, and every day of the month is
    written (a day not posted keeps its current value). An absence is 0 h
    whatever was posted.

    Refused with ValueError, before anything is written: a day outside the
    month, a day posted twice, a day carrying a reading error
    (`read_posted_month` - check `errors` first), an unknown kind, hours out
    of a day's range."""
    first = first_of_month(month)
    if isinstance(posted_days, Mapping):
        posted_days = posted_days.values()
    posted_days = list(posted_days)
    in_month = set(month_days(first))
    seen = set()
    for posted in posted_days:
        if posted.date not in in_month:
            raise ValueError(f"Le {posted.date:%d/%m/%Y} n'est pas en {month_label(first)}.")
        if posted.date in seen:
            raise ValueError(f"Le {posted.date:%d/%m/%Y} est envoyé deux fois.")
        seen.add(posted.date)
        if posted.error:
            raise ValueError(posted.error)
        if posted.kind is not None and posted.kind not in KIND_LABELS:
            raise ValueError(f"{day_name(posted.date)} : motif inconnu « {posted.kind} ».")
        if posted.hours is not None:
            checked_hours(posted.hours, day_name(posted.date))
        _checked_note(posted.note)
    with transaction.atomic():
        timesheet, before = _current(employee, first)
        week = _week(employee, timesheet)
        after = dict(before)
        adjustments = []
        for posted in posted_days:
            typical = week.typical_hours(posted.date.weekday())
            after[posted.date], adjustment = _settle(before[posted.date], posted, typical)
            if adjustment:
                adjustments.append(adjustment)
        return _store(employee, first, timesheet, before, after, [posted.date for posted in posted_days], adjustments)


def apply_range(
    employee: Employee,
    month: date,
    start: date,
    end: date,
    kind: str,
    hours=None,
    note: str | None = None,
) -> Outcome:
    """« Il était en congés du 11 au 18 », in one action: `kind` on the days
    from `start` to `end`, both included, that the month holds - a range
    running over the month's edge is cut to it (`Outcome.touched` says which
    days), two dates the wrong way round are swapped, and a range with no
    day in the month is refused (ValueError).

    **A day off stays a day off** (`_is_day_off`: the typical week's, or one
    made « Repos » this month) under an absence and under « Travail » with
    `hours`; `Outcome.left_alone` names those days. Written over them, leave
    from Thursday to Thursday was « Congés payés : 8 jours » on the sheet the
    employee signs - two of them a Sunday and a Monday off, which no French
    way of counting leave counts - and « Travail 7 h du 1er au 30 » put 7 h
    on every Sunday (review, 28/09). A range made only of days off writes
    nothing, and does not save an unsaved month.

    « Travail » with `hours` puts those hours on every other day; « Travail »
    without puts each day back to what the month's typical week plans for it
    (a day off stays « Repos », rather than becoming « Travail » at 0 and
    printing « 0 » on every Sunday); « Repos » is every day of the range.
    Any other kind is 0 h, whatever `hours` says. `note`, when given, is
    written on every day changed; when not, a day keeps its note if its
    kind stays the same and loses it otherwise - « arrivé en retard » has
    nothing to say about a day of leave."""
    first = first_of_month(month)
    if kind not in KIND_LABELS:
        raise ValueError(f"Motif inconnu « {kind} ».")
    if start is None or end is None:
        raise ValueError("Il faut une date de début et une date de fin.")
    if start > end:
        start, end = end, start
    days = [day for day in month_days(first) if start <= day <= end]
    if not days:
        raise ValueError(f"Du {start:%d/%m/%Y} au {end:%d/%m/%Y} : aucun jour en {month_label(first)}.")
    if hours is not None:
        hours = checked_hours(hours)
    note = _checked_note(note)
    # Only an absence and hours given reach past a day off: « Travail »
    # without hours is the typical week (days off included), « Repos » is
    # what a day off already is.
    spares_days_off = kind != REST and not (kind == WORK and hours is None)
    with transaction.atomic():
        timesheet, before = _current(employee, first)
        week = _week(employee, timesheet)
        after = dict(before)
        left_alone = []
        for day in days:
            old = before[day]
            if spares_days_off and _is_day_off(old, week):
                left_alone.append(day)
                continue
            if kind == WORK and hours is None:
                entry = planned_day(week, day)
            elif kind == WORK:
                entry = DayEntry(day, hours, WORK)
            else:
                entry = DayEntry(day, ZERO, kind)
            kept_note = note if note is not None else (old.note if entry.kind == old.kind else "")
            after[day] = replace(entry, note=kept_note)
        if len(left_alone) == len(days):
            return Outcome(timesheet, False, (), tuple(days), left_alone=tuple(left_alone))
        return replace(_store(employee, first, timesheet, before, after, days), left_alone=tuple(left_alone))


def _is_day_off(entry: DayEntry, week: TypicalWeek) -> bool:
    """A day no range of leave and no holiday reaches: « Repos » this month
    (a day swapped for another included), or a day off of the month's
    typical week - even one worked this month, whose hours somebody typed
    and no range should turn into a day of leave on a Sunday."""
    return entry.kind == REST or week.typical_hours(entry.date.weekday()) <= 0


def mark_holidays_off(employee: Employee, month: date) -> Outcome:
    """Every public holiday of the month on a working day becomes « Férié
    chômé », 0 h - the owner's decision, taken by pressing the button, never
    assumed. A holiday on a day off (`_is_day_off`: Whit Monday for an
    employee off on Mondays) stays as it is - blank on the sheet, as the
    PDF prints a day off, and not counted as a day of « Férié chômé » - and
    is in `Outcome.left_alone`. A month with no holiday, or with holidays on
    days off only, writes nothing (and does not save an unsaved month)."""
    first = first_of_month(month)
    holidays = sorted(month_holidays(first))
    if not holidays:
        timesheet = Timesheet.objects.filter(employee=employee, month=first).first()
        return Outcome(timesheet, False, (), ())
    with transaction.atomic():
        timesheet, before = _current(employee, first)
        week = _week(employee, timesheet)
        after = dict(before)
        left_alone = tuple(day for day in holidays if _is_day_off(before[day], week))
        if len(left_alone) == len(holidays):
            return Outcome(timesheet, False, (), tuple(holidays), left_alone=left_alone)
        for day in holidays:
            if day in left_alone:
                continue
            old = before[day]
            after[day] = DayEntry(day, ZERO, PUBLIC_HOLIDAY, old.note if old.kind == PUBLIC_HOLIDAY else "")
        return replace(_store(employee, first, timesheet, before, after, holidays), left_alone=left_alone)


def reset_to_typical_week(employee: Employee, month: date) -> Outcome:
    """Rewrite a saved month from the employee's typical week of today,
    notes cleared - the page asks first - and make that week the month's
    own (`Outcome.week_before` when it was another). An unsaved month
    already IS the typical week: nothing is written and `timesheet` is
    None."""
    first = first_of_month(month)
    with transaction.atomic():
        timesheet, before = _current(employee, first)
        if timesheet is None:
            return Outcome(None, False, (), ())
        after = {entry.date: entry for entry in planned_days(employee, first)}
        return _store(employee, first, timesheet, before, after, list(after), take_week=True)
