"""When a reminder or an automatic gather is due: weekdays, times, the bar's
night, and the UTC instants they give.

**UTC only.** Wall time (Europe/Paris) is used for one thing: to enumerate
the candidate (date, time) pairs, each made with
`datetime.combine(day, t, tzinfo=PARIS)` (fold=0) and converted to UTC at
once. Every comparison, subtraction, window bound, grace check and dedupe key
works on UTC datetimes: zoneinfo compares and subtracts two datetimes of the
same zone by their wall clock, ignoring fold - on the October night 02:40
(second time) minus 30 minutes is 02:10 (first time), 90 real minutes
earlier.

**Daylight saving.** A time that does not exist (the last Sunday of March,
02:xx) is, with fold=0, the instant of 03:xx summer time: sent once - and
when two times of a rule fall on the same instant (« 02:00 03:00 » that
night) the instant is sent once. A time that happens twice (the last Sunday
of October) is its first occurrence. A preview prints the local time of the
UTC instant (« à 03:00 »).

**The bar's night** (reminders only): the espace's « la nuit se termine à »
(`night_ends_at`, 00:00 = the calendar, or 04:00 to 12:00). A reminder's
ticked day is an EVENING: a time strictly before the night's end is sent the
next calendar day, a time from it on the same day. An invented example:
evenings Monday, Wednesday and Saturday at « 00:00 02:00 » send on
Tuesday, Thursday and Sunday at 00:00 and 02:00 - the nights before
Tuesday, Thursday and Sunday morning deliveries.

Weekdays are Python's `date.weekday()` everywhere: 0 = lundi … 6 =
dimanche, stored as "0,2,5", the index into staff.timesheet.DAY_NAMES.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")

DAY_SHORT = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")

#: A reminder is still sent this late (a tick held up, a server restarted);
#: later, its history says « manqué ».
REMINDER_GRACE = timedelta(minutes=30)

#: The night's end that follows the calendar: no time counts for the eve.
CALENDAR = time(0, 0)
NIGHT_END_EARLIEST = time(4, 0)
NIGHT_END_LATEST = time(12, 0)
DEFAULT_NIGHT_END = time(6, 0)
NIGHT_END_REFUSED = (
    "La nuit doit se terminer après la fermeture du bar (04:00 au plus tôt), ou à 00:00 pour suivre le calendrier."
)

MAX_TIMES = 12
NO_TIME = "Indiquez au moins une heure."
TOO_MANY_TIMES = "12 heures au plus."
# ASCII digits only: \d would take any script's digits.
_TIME = re.compile(r"([0-9]{1,2})(?:h([0-9]{2})?|:([0-9]{2}))?")


def _not_a_time(token: str) -> str:
    shown = token if len(token) <= 20 else token[:19] + "…"
    return f"« {shown} » n'est pas une heure (00:00 à 23:59)."


def _day_names():
    # Imported here: staff.timesheet imports staff's models.
    from staff.timesheet import DAY_NAMES

    return DAY_NAMES


def parse_weekdays(values) -> tuple[int, ...]:
    """Weekdays (0 = lundi … 6 = dimanche) from the stored "0,2,5" or from
    the checked boxes' values: sorted, each once. ValueError on anything
    else."""
    if isinstance(values, str):
        values = [value for value in values.split(",") if value.strip()]
    days = set()
    for value in values:
        text = str(value).strip()
        if not (len(text) == 1 and "0" <= text <= "6"):
            raise ValueError("Jour inconnu.")
        days.add(int(text))
    return tuple(sorted(days))


def weekdays_value(days: Iterable[int]) -> str:
    """The stored form: "0,2,5"."""
    return ",".join(str(day) for day in sorted(set(days)))


def _join(items: list[str]) -> str:
    """« a », « a et b », « a, b et c »."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} et {items[-1]}"


def format_weekdays(days: Iterable[int]) -> str:
    """« lun., mer. et sam. », « tous les jours »."""
    days = sorted(set(days))
    if len(days) == 7:
        return "tous les jours"
    return _join([DAY_SHORT[day] for day in days])


def parse_times(text) -> tuple[time, ...]:
    """The times typed in a field - « 0h et 2h », « 00:00 02:00 », « 2h30;
    18:00 » - sorted, each once. Separators: spaces, commas, semicolons and
    « et ». ValueError with the French refusal."""
    words = str(text or "").lower().replace(",", " ").replace(";", " ").split()
    found = set()
    for word in words:
        if word == "et":
            continue
        match = _TIME.fullmatch(word)
        if match is None:
            raise ValueError(_not_a_time(word))
        hour = int(match[1])
        minute = int(match[2] or match[3] or 0)
        if hour > 23 or minute > 59:
            raise ValueError(_not_a_time(word))
        found.add(time(hour, minute))
    if not found:
        raise ValueError(NO_TIME)
    if len(found) > MAX_TIMES:
        raise ValueError(TOO_MANY_TIMES)
    return tuple(sorted(found))


def times_value(times: Iterable[time]) -> str:
    """The stored, normalised form: "00:00 02:00"."""
    return " ".join(f"{t:%H:%M}" for t in sorted(set(times)))


def format_times(times: Iterable[time]) -> str:
    """« 00:00 et 02:00 »."""
    return _join([f"{t:%H:%M}" for t in sorted(set(times))])


def check_night_end(t: time) -> str:
    """The French refusal of a night's end, or ""."""
    if t == CALENDAR or NIGHT_END_EARLIEST <= t <= NIGHT_END_LATEST:
        return ""
    return NIGHT_END_REFUSED


def _send_day(evening: date, t: time, night_ends_at: time) -> date:
    """The calendar day an evening's time is sent: the next one when the time
    is before the night's end (never with 00:00, the calendar)."""
    return evening + timedelta(days=1) if t < night_ends_at else evening


def _evening_of(local: datetime, night_ends_at: time) -> date:
    """The evening a local instant belongs to."""
    day = local.date()
    return day - timedelta(days=1) if local.time() < night_ends_at else day


def _instant(day: date, t: time) -> datetime:
    """The UTC instant of a wall time (fold=0: see the docstring)."""
    return datetime.combine(day, t, tzinfo=PARIS).astimezone(UTC)


def _local_days(start_utc: datetime, end_utc: datetime, margin: int):
    """Every local date that may hold an instant of (start, end], `margin`
    days added on each side."""
    first = start_utc.astimezone(PARIS).date() - timedelta(days=margin)
    last = end_utc.astimezone(PARIS).date() + timedelta(days=margin)
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def _window(candidates, start_utc: datetime, end_utc: datetime) -> list[datetime]:
    return sorted({instant for instant in candidates if start_utc < instant <= end_utc})


def reminder_instants(weekdays, times, night_ends_at: time, start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """The UTC instants of a reminder in (start, end]: sorted, each once."""
    days, times = set(weekdays), tuple(times)
    if not days or not times or end_utc <= start_utc:
        return []
    candidates = (
        _instant(_send_day(evening, t, night_ends_at), t)
        for evening in _local_days(start_utc, end_utc, margin=2)
        if evening.weekday() in days
        for t in times
    )
    return _window(candidates, start_utc, end_utc)


def range_times(start_time: time, end_time: time, every_minutes: int) -> tuple[time, ...]:
    """An automatic gather's wall times in a day: from `start_time` every
    `every_minutes` up to `end_time` included; once when they are equal."""
    if every_minutes < 1:
        raise ValueError("every_minutes is at least 1")
    if end_time < start_time:
        raise ValueError("end before start")
    first = start_time.hour * 60 + start_time.minute
    last = end_time.hour * 60 + end_time.minute
    return tuple(time(minute // 60, minute % 60) for minute in range(first, last + 1, every_minutes))


def gather_instants(
    weekdays, start_time: time, end_time: time, every_minutes: int, start_utc, end_utc
) -> list[datetime]:
    """The UTC instants of an automatic gather in (start, end]: its days are
    calendar days (no night)."""
    days = set(weekdays)
    if not days or end_utc <= start_utc:
        return []
    times = range_times(start_time, end_time, every_minutes)
    candidates = (
        _instant(day, t) for day in _local_days(start_utc, end_utc, margin=1) if day.weekday() in days for t in times
    )
    return _window(candidates, start_utc, end_utc)


def _next(instants_in, now_utc: datetime, count: int) -> list[datetime]:
    # Every week holds at least one instant of a rule with a day and a time.
    return instants_in(now_utc, now_utc + timedelta(days=7 * count + 2))[:count]


def next_reminder_instants(weekdays, times, night_ends_at: time, now_utc: datetime, count=5) -> list[datetime]:
    """The reminder's next `count` UTC instants after `now_utc`."""
    return _next(lambda start, end: reminder_instants(weekdays, times, night_ends_at, start, end), now_utc, count)


def next_gather_instants(
    weekdays, start_time: time, end_time: time, every_minutes: int, now_utc: datetime, count=5
) -> list[datetime]:
    """The automatic gather's next `count` UTC instants after `now_utc`."""
    return _next(
        lambda start, end: gather_instants(weekdays, start_time, end_time, every_minutes, start, end), now_utc, count
    )


def calendar_instants(weekdays, times, start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """The UTC instants of calendar days × times in (start, end] - a
    reminder's rule read with the calendar (00:00: no night). The automatic
    sales imports (recipes/auto_sales.py)."""
    return reminder_instants(weekdays, times, CALENDAR, start_utc, end_utc)


def next_calendar_instants(weekdays, times, now_utc: datetime, count=5) -> list[datetime]:
    """The next `count` UTC instants of calendar days × times after
    `now_utc`."""
    return next_reminder_instants(weekdays, times, CALENDAR, now_utc, count)


def _when(local: datetime) -> str:
    return f"{DAY_SHORT[local.weekday()]} {local:%d/%m} à {local:%H:%M}"


def describe_reminder_instant(dt_utc: datetime, night_ends_at: time) -> str:
    """« dim. 04/10 à 02:00 — nuit de samedi »; « mer. 07/10 à 18:00 » when
    the instant is on its evening's own day."""
    local = dt_utc.astimezone(PARIS)
    evening = _evening_of(local, night_ends_at)
    if evening == local.date():
        return _when(local)
    return f"{_when(local)} — nuit de {_day_names()[evening.weekday()].lower()}"


def describe_gather_instant(dt_utc: datetime) -> str:
    """« mer. 07/10 à 06:30 »."""
    return _when(dt_utc.astimezone(PARIS))


def day_label(weekday: int, night_ends_at: time) -> str:
    """A day's box on the reminder form: « samedi soir », or « samedi » with
    the calendar."""
    name = _day_names()[weekday].lower()
    return name if night_ends_at == CALENDAR else f"{name} soir"


def sends_label(weekday: int, times, night_ends_at: time) -> str:
    """Where an evening's times land: « dim. 00:00 et 02:00 », « sam. 22:00
    puis dim. 02:00 »."""
    same = sorted(t for t in times if not t < night_ends_at)
    after = sorted(t for t in times if t < night_ends_at)
    groups = []
    if same:
        groups.append(f"{DAY_SHORT[weekday]} {format_times(same)}")
    if after:
        groups.append(f"{DAY_SHORT[(weekday + 1) % 7]} {format_times(after)}")
    return " puis ".join(groups)


def evening_label(weekday: int, times, night_ends_at: time) -> str:
    """« samedi soir → dim. 00:00 et 02:00 » (the day alone without times)."""
    sends = sends_label(weekday, times, night_ends_at)
    label = day_label(weekday, night_ends_at)
    return f"{label} → {sends}" if sends else label
