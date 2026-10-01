"""« Consignes »: which slips count, what each of their lines is, and how a
day's pickups compare with that day's slips - worked out when a page is
drawn, never stored (a pattern edited reclassifies at once; a slip replaced
stops counting at once).

**Which slips count** (`effective_slips`, over EVERY slip of the formats
involved - one values() query - never over the few a page shows: the slip
that replaces one on screen may be off it):

1. A slip's moment is (printed_at or received_at, pk): both aware datetimes.
   The mail's date never orders anything.
2. Re-sends: slips of one format with the same non-blank number AND the same
   delivery date are one ticket (the driver e-mails it again, printed the
   next morning). The latest gives the content, the others are « renvoyé »;
   the ticket's moment is its EARLIEST slip's. The same number on two
   delivery dates is two tickets (a seller restarting its numbering).
3. Replacements, per format: a ticket that says « annule et remplace »
   supersedes every ticket sharing a reference with it that does not say so,
   whatever order they arrived in - the original re-sent after its
   replacement included. Among replacing tickets sharing a reference, the
   latest wins. A ticket with no reference is never superseded by one.
4. Only for a format with NO reference pattern: a replacing ticket supersedes
   the single latest earlier ticket of the same delivery date and another
   number (two blank numbers are not « the same number »).

**What a line is** (`classify`, `Classifier`): the first type, in (position,
pk) order and active or not, one of whose patterns is found in the line's
designation. A type whose pattern no longer compiles, or runs out of the
page's time, stops the search for that line: it is « non classée : motif
invalide / trop lent », never filed under the type after it.

**What is compared** (`Board`): per (supplier, day). All the pickups of a
supplier on one day are ONE side, counts summed (« 2 reprises ce jour-là,
additionnées »); the other side is every slip that counts, of that
supplier's formats (active or not), delivered that day. A slip whose
delivery date was not read is never paired. An unpaired slip within
HINT_DAYS of an unpaired pickup day is offered to the NEAREST such day only
(ties: the earlier), once.

Status, the first that applies: no_supplier, no_format (the supplier has no
format at all), waiting, to_check (a paired slip failed its reading or one
of its checks, or a line could not be classified for want of time or of a
valid pattern), differs, same.

Every sentence here is a plain str (never a SafeString): templates escape
them, and a type name is whatever somebody typed. Type names are shown
EXACTLY as typed; counts read « Fûts 15 · Bouteilles CO2 1 ».
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q

from common import format_money
from returnables import patterns
from returnables.models import Pickup, ReturnableType, Slip, SlipFormat, SlipLine
from returnables.patterns import Budget, PatternError

CENT = Decimal("0.01")
#: How far (in days) an unpaired slip is offered to a pickup day.
HINT_DAYS = 3
#: The pickups loaded around a page's days: a hint's slip is within HINT_DAYS
#: of the day, and its nearest rival day within HINT_DAYS of the slip.
_AROUND = 2 * HINT_DAYS

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: The reading's check listing the lines it could not read (reading.py).
UNREAD_CHECK = "Aucune ligne ignorée"

# -- Words --------------------------------------------------------------------------------------------------------------


def euros(value) -> str:
    """30,00 €, 1 234,50 € - the one way a sentence built in Python writes
    money (a template prints `{{ x|money }} €`): thousands grouped by a
    no-break space. Half away from zero, never « -0,00 »; "" for None."""
    if value is None:
        return ""
    amount = Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP) + 0
    return format_money(amount).replace(".", ",") + "\N{NO-BREAK SPACE}€"


def slip_label(number, delivery_date=None, *, with_date: bool = False, capital: bool = False) -> str:
    """« bon n° 1001 », « bon n° 1001 du 14/05/2025 » (with_date), « bon sans
    numéro du 14/05/2025 », « bon sans numéro » - mid-sentence, or « Bon … »
    with `capital`."""
    if number:
        label = f"bon n° {number}"
        if with_date and delivery_date is not None:
            label += f" du {delivery_date:%d/%m/%Y}"
    elif delivery_date is not None:
        label = f"bon sans numéro du {delivery_date:%d/%m/%Y}"
    else:
        label = "bon sans numéro"
    return label[0].upper() + label[1:] if capital else label


def counts_summary(counts: dict, types) -> str:
    """« Fûts 15 · Bouteilles CO2 1 »: each type's name as typed, then its
    number, in the types' order; a zero is left out. "" when nothing."""
    return " \N{MIDDLE DOT} ".join(f"{kind.name} {counts[kind.pk]}" for kind in _ordered(types) if counts.get(kind.pk))


def counts_of(pickup) -> dict:
    """{type pk: quantity} of one pickup (its counts prefetched, or one
    query)."""
    counts = defaultdict(int)
    for count in pickup.counts.all():
        counts[count.returnable_type_id] += count.quantity
    return dict(counts)


def pickup_summary(pickup, types) -> str:
    """« Fûts 15 · Bouteilles CO2 1 » for one pickup."""
    return counts_summary(counts_of(pickup), types)


def summed_note(count: int) -> str:
    """« 2 reprises ce jour-là, additionnées » - "" for one."""
    return f"{count} reprises ce jour-là, additionnées" if count > 1 else ""


def _plural(count, singular: str, plural: str) -> str:
    return f"{count} {plural if abs(count) > 1 else singular}"


def shifted(day: date, days: int) -> date:
    """`day` moved by `days`, cut at the calendar's ends: a date a damaged
    archive brought in before « Données » refused it (0001-01-02,
    9999-12-31) is never an OverflowError - /consignes/ shows the newest
    pickup first, so one such row made every drawing of it a 500."""
    try:
        return day + timedelta(days=days)
    except OverflowError:
        return date.min if days < 0 else date.max


# -- Statuses ---------------------------------------------------------------------------------------------------------

NO_SUPPLIER = "no_supplier"
NO_FORMAT = "no_format"
WAITING = "waiting"
TO_CHECK = "to_check"
DIFFERS = "differs"
SAME = "same"


@dataclass(frozen=True)
class Status:
    """A state's pill and its colour: `css` is the status-pill class's suffix
    (status-COMPLETE green, status-ERROR red, status-pending amber,
    status-ignored grey)."""

    pill: str
    css: str


#: In the order they are decided.
STATUSES = {
    NO_SUPPLIER: Status("fournisseur non précisé", "ignored"),
    NO_FORMAT: Status("pas de format de bon", "ignored"),
    WAITING: Status("en attente du bon", "pending"),
    TO_CHECK: Status("à vérifier", "pending"),
    DIFFERS: Status("écart", "ERROR"),
    SAME: Status("conforme", "COMPLETE"),
}

# -- Slips, as the comparison sees them -------------------------------------------------------------------------------


@dataclass
class SlipInfo:
    """What the comparison and the invoice check need of a slip, read by ONE
    values() query (SlipIndex): no text, no file. `lines` is None until
    loaded (SlipIndex.ensure_lines), then the slip's SlipLines in order."""

    pk: int
    format_id: int
    supplier_id: int | None = None
    number: str = ""
    delivery_date: date | None = None
    printed_at: datetime | None = None
    received_at: datetime | None = None
    references: list = field(default_factory=list)
    replaces: bool = False
    read_error: str = ""
    checks: list = field(default_factory=list)
    format_name: str = ""
    lines: list | None = None

    @property
    def id(self) -> int:
        return self.pk

    @property
    def key(self) -> tuple:
        """Its moment: (printed_at or received_at, pk)."""
        return (self.printed_at or self.received_at or _EPOCH, self.pk)

    @property
    def label(self) -> str:
        return slip_label(self.number, self.delivery_date)

    @property
    def title(self) -> str:
        return slip_label(self.number, self.delivery_date, with_date=True)

    @property
    def failed_checks(self) -> list:
        return [check for check in self.checks if isinstance(check, dict) and check.get("passed") is not True]

    @classmethod
    def from_values(cls, row: dict) -> SlipInfo:
        references = row.get("references")
        checks = row.get("checks")
        return cls(
            pk=row["pk"],
            format_id=row["format_id"],
            supplier_id=row.get("format__supplier"),
            number=row.get("number") or "",
            delivery_date=row.get("delivery_date"),
            printed_at=row.get("printed_at"),
            received_at=row.get("received_at"),
            references=[value for value in references if isinstance(value, str)]
            if isinstance(references, list)
            else [],
            replaces=bool(row.get("replaces")),
            read_error=row.get("read_error") or "",
            checks=list(checks) if isinstance(checks, list) else [],
            format_name=row.get("format__name") or "",
        )


RESENT = "resent"
REPLACED = "replaced"
SUPERSEDED_PILLS = {RESENT: "renvoyé", REPLACED: "annulé et remplacé"}


@dataclass
class Superseded:
    """Why a slip does not count: `by` the slip that took its place, `final`
    the one that counts in the end (a replacement can be replaced in turn).
    Unpacks as (by, reason)."""

    by: SlipInfo
    kind: str
    reason: str
    final: SlipInfo

    def __iter__(self):
        return iter((self.by, self.reason))

    @property
    def pill(self) -> str:
        return SUPERSEDED_PILLS[self.kind]


class _Ticket:
    """One ticket: a slip and its re-sends."""

    def __init__(self, slips):
        self.slips = sorted(slips, key=lambda info: info.key)
        self.rep = self.slips[-1]
        self.time = self.slips[0].key

    @property
    def replaces(self) -> bool:
        return self.rep.replaces

    @property
    def references(self) -> set:
        return set(self.rep.references)


def _same_number(one: _Ticket, other: _Ticket) -> bool:
    return bool(one.rep.number) and one.rep.number == other.rep.number


def _supersede(infos, formats_without_references) -> dict:
    """{pk: Superseded} over `infos` (every slip of the formats involved)."""
    decided = {}
    by_format = defaultdict(list)
    for info in infos:
        by_format[info.format_id].append(info)
    for format_id, members in by_format.items():
        groups = defaultdict(list)
        tickets = []
        for info in members:
            if info.number and info.delivery_date is not None:
                groups[(info.number, info.delivery_date)].append(info)
            else:
                tickets.append(_Ticket([info]))
        tickets.extend(_Ticket(group) for group in groups.values())
        for ticket in tickets:
            for other in ticket.slips[:-1]:
                decided[other.pk] = (ticket.rep, RESENT)

        winners = {}
        by_reference = defaultdict(list)
        for ticket in tickets:
            for reference in ticket.references:
                by_reference[reference].append(ticket)
        for ticket in tickets:
            sharing = {
                id(other): other for ref in ticket.references for other in by_reference[ref] if other is not ticket
            }
            rivals = [
                other
                for other in sharing.values()
                if other.replaces and (not ticket.replaces or other.time > ticket.time)
            ]
            if rivals:
                winners[id(ticket)] = (ticket, max(rivals, key=lambda other: other.time))

        if format_id in formats_without_references:
            for replacing in tickets:
                if not replacing.replaces or replacing.rep.delivery_date is None:
                    continue
                earlier = [
                    other
                    for other in tickets
                    if other is not replacing
                    and other.rep.delivery_date == replacing.rep.delivery_date
                    and not _same_number(other, replacing)
                    and other.time < replacing.time
                ]
                if not earlier:
                    continue
                target = max(earlier, key=lambda other: other.time)
                current = winners.get(id(target))
                if current is None or current[1].time < replacing.time:
                    winners[id(target)] = (target, replacing)

        for target, winner in winners.values():
            decided[target.rep.pk] = (winner.rep, REPLACED)

    superseded = {}
    for pk, (by, kind) in decided.items():
        final, seen = by, {pk}
        while final.pk in decided and final.pk not in seen:
            seen.add(final.pk)
            final = decided[final.pk][0]
        if kind == RESENT:
            reason = f"Renvoyé : le {by.label} a été reçu plusieurs fois, le plus récent fait foi."
        else:
            reason = f"Annulé et remplacé par le {by.title}."
        superseded[pk] = Superseded(by=by, kind=kind, reason=reason, final=final)
    return superseded


class SlipIndex:
    """Every slip of some formats, read in ONE values() query: what decides
    which slips count, and what the invoice check searches. Lines are loaded
    on demand, in one query for all the slips asked (`ensure_lines`)."""

    FIELDS = (
        "pk",
        "format_id",
        "format__supplier",
        "format__name",
        "format__reference_patterns",
        "number",
        "delivery_date",
        "printed_at",
        "received_at",
        "references",
        "replaces",
        "read_error",
        "checks",
    )

    def __init__(self, infos=None, format_ids=(), formats_without_references=()):
        self.infos = {info.pk: info for info in (infos or [])}
        self.format_ids = set(format_ids)
        self.formats_without_references = set(formats_without_references)
        self._superseded = None

    @classmethod
    def load(cls, format_ids) -> SlipIndex:
        format_ids = {pk for pk in format_ids if pk is not None}
        if not format_ids:
            return cls()
        infos, without = [], set()
        for row in Slip.objects.filter(format_id__in=format_ids).order_by().values(*cls.FIELDS):
            infos.append(SlipInfo.from_values(row))
            if not (row.get("format__reference_patterns") or "").strip():
                without.add(row["format_id"])
        return cls(infos, format_ids, without)

    @property
    def superseded(self) -> dict:
        if self._superseded is None:
            self._superseded = _supersede(self.infos.values(), self.formats_without_references)
        return self._superseded

    def is_effective(self, pk) -> bool:
        return pk in self.infos and pk not in self.superseded

    def effective(self) -> list:
        return [info for pk, info in self.infos.items() if pk not in self.superseded]

    def ensure_lines(self, pks) -> None:
        """Load the lines of those of `pks` not loaded yet - one query."""
        missing = [pk for pk in set(pks) if pk in self.infos and self.infos[pk].lines is None]
        if not missing:
            return
        for pk in missing:
            self.infos[pk].lines = []
        for line in SlipLine.objects.filter(slip_id__in=missing).order_by("slip_id", "position", "pk"):
            self.infos[line.slip_id].lines.append(line)


def effective_slips(slips, *, index: SlipIndex | None = None) -> tuple:
    """(kept, superseded): `kept` the given slips that count, in their order;
    `superseded` {pk: Superseded} over EVERY slip of their formats (one
    values() query, unless `index` already holds them). Works on Slip rows
    and on SlipInfo alike."""
    slips = list(slips)
    if index is None or not {slip.format_id for slip in slips} <= index.format_ids:
        index = SlipIndex.load({slip.format_id for slip in slips})
    superseded = index.superseded
    return [slip for slip in slips if slip.pk not in superseded], superseded


# -- What a line is -----------------------------------------------------------------------------------------------------

SLOW = "motif trop lent"
INVALID = "motif invalide"


@dataclass(frozen=True)
class Classification:
    """A designation's type (None: none), the pattern that found it, or why it
    could not be classified (`problem`: SLOW or INVALID, and the type whose
    pattern failed)."""

    returnable_type: object = None
    pattern: str = ""
    problem: str = ""
    type_in_error: object = None

    @property
    def note(self) -> str:
        if self.returnable_type is not None:
            return ""
        if self.problem:
            return f"non classée : {self.problem}"
        return "sans type de consigne"


def _ordered(types) -> list:
    return sorted(types, key=lambda kind: (kind.position, kind.pk))


def _type_patterns(kind) -> list:
    return patterns.compile_field(patterns.TYPE_FIELD, getattr(kind, "slip_patterns", "") or "")


def _classification(designation: str, types, patterns_of, budget) -> Classification:
    for kind in types:
        try:
            compiled_patterns = patterns_of(kind)
        except PatternError:
            return Classification(problem=INVALID, type_in_error=kind)
        for compiled in compiled_patterns:
            try:
                found = patterns.search(compiled, designation, budget)
            except PatternError:
                return Classification(problem=SLOW, type_in_error=kind)
            if found is not None:
                return Classification(kind, pattern=compiled.pattern)
    return Classification()


def classify(designation: str, types, budget, memo: dict):
    """The type of a slip's line (None: no type), memoised in `memo` per
    designation: the first type in (position, pk) order - active or not -
    one of whose patterns is found in it. A pattern that fails or runs out of
    `budget` leaves it unclassified (`memo[designation].problem` says why)."""
    if designation not in memo:
        memo[designation] = _classification(designation, _ordered(types), _type_patterns, budget)
    return memo[designation].returnable_type


class Classifier:
    """One page's classification: the types' patterns compiled once, one
    budget (PAGE_SECONDS, started at the first line classified, not when the
    page is set up), one memo per designation. `errors` {type pk: message}
    names the types whose patterns no longer compile - the page says « motif
    invalide : … — corrigez-le »."""

    def __init__(self, types, budget=None):
        self.types = _ordered(types)
        self._budget = budget
        self.memo = {}
        self.errors = {}
        self._patterns = {}
        for kind in self.types:
            try:
                self._patterns[kind.pk] = _type_patterns(kind)
            except PatternError as error:
                self.errors[kind.pk] = error.message

    @property
    def budget(self):
        if self._budget is None:
            self._budget = Budget(patterns.PAGE_SECONDS)
        return self._budget

    def _patterns_of(self, kind) -> list:
        if kind.pk in self.errors:
            raise PatternError(self.errors[kind.pk])
        return self._patterns.get(kind.pk, [])

    def classify(self, designation: str) -> Classification:
        if designation not in self.memo:
            self.memo[designation] = _classification(designation, self.types, self._patterns_of, self.budget)
        return self.memo[designation]

    def type_of(self, designation: str):
        return self.classify(designation).returnable_type


# -- Comparing counts with lines ----------------------------------------------------------------------------------------


def _line_unit(line) -> Decimal | None:
    """A line's unit price: the printed one, else amount / quantity (a
    zero quantity gives none), to the cent, half away from zero."""
    if line.unit_amount is not None:
        unit = abs(Decimal(line.unit_amount))
    elif line.amount is not None and line.quantity:
        unit = abs(Decimal(line.amount) / Decimal(line.quantity))
    else:
        return None
    return unit.quantize(CENT, rounding=ROUND_HALF_UP)


def _unit(lines) -> Decimal | None:
    """The type's unit price, only when every line with a count has one and
    they agree (spec §5): a counted line whose price was not printed may be
    of another price, and the gap's euros would rest on the others'. A zero
    count carries no unit and moves no gap: it never blocks one."""
    units = set()
    for line in lines:
        unit = _line_unit(line)
        if unit is None:
            if line.quantity:
                return None
            continue
        units.add(unit)
    return units.pop() if len(units) == 1 else None


@dataclass
class Row:
    """One type: counted on the pickup(s), on the slip(s) (|Σ quantity|:
    summed first, then its absolute value - a correction line « -1 »
    subtracts), the unit price when the slips agree on one."""

    returnable_type: object
    counted: int
    on_slips: int
    unit: Decimal | None = None
    lines: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.returnable_type.name

    @property
    def gap(self) -> int:
        """On the slip less counted: negative, some are missing on the slip."""
        return self.on_slips - self.counted

    @property
    def agrees(self) -> bool:
        return self.gap == 0

    @property
    def money(self) -> Decimal | None:
        if self.unit is None or not self.gap:
            return None
        return (abs(self.gap) * self.unit).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def sentence(self) -> str:
        text = f"{self.name} — compté : {self.counted} \N{MIDDLE DOT} sur le bon : {self.on_slips}"
        if self.agrees:
            return f"{text} \N{CHECK MARK}"
        if self.gap < 0:
            text += f" \N{RIGHTWARDS ARROW} il en manque {-self.gap} sur le bon"
        else:
            text += f" \N{RIGHTWARDS ARROW} {self.gap} de plus sur le bon"
        if self.money is not None:
            text += f" ({euros(self.money)})"
        return text


@dataclass
class Unclassified:
    """Lines of the slip(s) of one designation that no type took: `problem`
    is "" (no type recognises it), SLOW or INVALID (`type_in_error`)."""

    designation: str
    quantity: int
    lines: list = field(default_factory=list)
    problem: str = ""
    type_in_error: object = None

    @property
    def note(self) -> str:
        return Classification(problem=self.problem).note

    @property
    def sentence(self) -> str:
        return f"{self.designation} — sur le bon : {self.quantity}, {self.note}"


@dataclass
class Comparison:
    rows: list = field(default_factory=list)
    unclassified: list = field(default_factory=list)

    @property
    def agrees(self) -> bool:
        return all(row.agrees for row in self.rows) and not self.unclassified

    @property
    def differing(self) -> list:
        return [row for row in self.rows if not row.agrees]

    @property
    def incomplete(self) -> list:
        """The lines left unclassified for want of time or of a valid pattern."""
        return [group for group in self.unclassified if group.problem]

    @property
    def sentences(self) -> list:
        return [row.sentence for row in self.rows] + [group.sentence for group in self.unclassified]


def compare(counts: dict, slip_lines, types, *, classifier: Classifier | None = None) -> Comparison:
    """Counts {type pk: number} against slip lines (anything with
    designation, quantity, unit_amount, amount): one Row per type present on
    either side, in the types' order, plus the lines no type took."""
    classifier = classifier or Classifier(types)
    by_type = defaultdict(list)
    unclassified = {}
    for line in slip_lines:
        found = classifier.classify(line.designation)
        if found.returnable_type is not None:
            by_type[found.returnable_type.pk].append(line)
            continue
        group = unclassified.get((line.designation, found.problem))
        if group is None:
            group = unclassified[(line.designation, found.problem)] = Unclassified(
                line.designation, 0, problem=found.problem, type_in_error=found.type_in_error
            )
        group.quantity += line.quantity
        group.lines.append(line)
    rows = []
    for kind in _ordered(types):
        if kind.pk not in counts and kind.pk not in by_type:
            continue
        lines = by_type.get(kind.pk, [])
        rows.append(
            Row(
                kind,
                counted=int(counts.get(kind.pk, 0)),
                on_slips=abs(sum(line.quantity for line in lines)),
                unit=_unit(lines),
                lines=lines,
            )
        )
    return Comparison(rows, list(unclassified.values()))


# -- One day of one supplier ------------------------------------------------------------------------------------------


@dataclass
class Hint:
    """An unpaired slip offered to this pickup day: « Mettre la reprise au
    <date> »."""

    slip: SlipInfo
    date: date
    days: int  # the slip's day less the pickup's

    @property
    def sentence(self) -> str:
        return f"Le {slip_label(self.slip.number)} du {self.date:%d/%m/%Y} n'a pas de reprise ce jour-là."


def _unread_count(check: dict) -> int | None:
    match = re.match(r"\s*(\d+)", str(check.get("detail") or ""))
    return int(match.group(1)) if match else None


def slip_problems(info: SlipInfo) -> list:
    """Why a paired slip makes the comparison « à vérifier »."""
    label = slip_label(info.number, info.delivery_date)
    if info.read_error:
        return [f"Le {label} n'a pas pu être lu ({info.read_error.rstrip('.')}) : comparaison impossible."]
    reasons = []
    for check in info.failed_checks:
        if check.get("label") == UNREAD_CHECK:
            count = _unread_count(check)
            if count == 1:
                unread = "une ligne non lue"
            elif count:
                unread = f"{count} lignes non lues"
            else:
                unread = "des lignes non lues"
            reasons.append(f"Le {label} a {unread} : comparaison incomplète.")
            continue
        detail = str(check.get("detail") or "").strip()
        shown = f" ({detail})" if detail else ""
        reasons.append(f"Le {label} : contrôle « {check.get('label', '')} » en échec{shown} : comparaison à vérifier.")
    return reasons


def _incomplete_reasons(comparison: Comparison) -> list:
    reasons = []
    slow = [group for group in comparison.incomplete if group.problem == SLOW]
    if slow:
        count = len(slow)
        reasons.append(
            f"{_plural(count, 'ligne du bon non classée', 'lignes du bon non classées')} : "
            "motif trop lent — comparaison incomplète."
        )
    invalid = {}
    for group in comparison.incomplete:
        if group.problem == INVALID and group.type_in_error is not None:
            invalid[group.type_in_error.pk] = group.type_in_error
    for kind in invalid.values():
        reasons.append(f"Le type « {kind.name} » a un motif invalide — corrigez-le : comparaison incomplète.")
    return reasons


@dataclass
class DayComparison:
    """A supplier's pickups of one day against the slips that count for that
    day. `comparison` is None when there is no slip to compare with."""

    supplier: object
    date: date
    pickups: list
    counts: dict
    types: list
    status: str
    slips: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    comparison: Comparison | None = None
    hints: list = field(default_factory=list)

    @property
    def pill(self) -> str:
        return STATUSES[self.status].pill

    @property
    def css(self) -> str:
        return STATUSES[self.status].css

    @property
    def summed(self) -> bool:
        return len(self.pickups) > 1

    @property
    def summed_note(self) -> str:
        return summed_note(len(self.pickups))

    @property
    def counts_summary(self) -> str:
        return counts_summary(self.counts, self.types)

    @property
    def sentence(self) -> str:
        """The day in one line - what the gather's progress note says."""
        head = f"Reprise du {self.date:%d/%m/%Y}"
        if self.summed:
            head += f" ({len(self.pickups)} reprises additionnées)"
        if self.status == SAME:
            return f"{head} : conforme ({', '.join(info.label for info in self.slips)})."
        if self.status == DIFFERS:
            details = [row.sentence for row in self.comparison.differing]
            details += [group.sentence for group in self.comparison.unclassified]
            return f"{head} : écart — {' ; '.join(details)}."
        if self.status == TO_CHECK:
            return f"{head} : à vérifier — {self.reasons[0]}"
        if self.status == WAITING:
            return f"{head} : en attente du bon."
        if self.status == NO_FORMAT:
            return f"{head} : pas de format de bon pour {self.supplier}."
        return f"{head} : fournisseur non précisé."


@dataclass
class SlipState:
    """How the « Bons reçus » table says a slip: its pill and why. `day` is
    the comparison it is part of (paired); `hint_date` the pickup day it is
    offered to (unpaired)."""

    key: str
    pill: str
    css: str
    reason: str = ""
    superseded: Superseded | None = None
    day: DayComparison | None = None
    hint_date: date | None = None


class Board:
    """The comparison for one page, loaded in a FIXED number of queries
    whatever it shows (types, formats, every slip of those formats, the
    pickups around the days shown and their counts, the lines needed):

        board = Board.load(pickups=pickups, slips=slips)
        board.day(pickup)         # DayComparison
        board.slip_state(slip)    # SlipState
        board.classify(designation)
        board.index               # for invoice_check.check_many(..., index=board.index)

    Only the pickups and slips handed to `load` (and their days) can be
    asked about."""

    def __init__(self):
        self.types = []
        self.classifier = Classifier([])
        self.index = SlipIndex()
        self.formats_of = {}
        self.suppliers = set()
        self.units = {}
        self.window = None
        self.no_supplier_dates = set()
        self._by_day = None
        self._days = {}
        self._hints = {}

    @classmethod
    def load(cls, pickups=(), slips=(), *, budget=None) -> Board:
        board = cls()
        pickups, slips = list(pickups), list(slips)
        board.types = list(ReturnableType.objects.order_by("position", "pk"))
        board.classifier = Classifier(board.types, budget)

        supplier_ids = {pickup.supplier_id for pickup in pickups if pickup.supplier_id is not None}
        slip_format_ids = {slip.format_id for slip in slips}
        formats = []
        if supplier_ids or slip_format_ids:
            wanted = Q(supplier_id__in=supplier_ids)
            if slip_format_ids:
                wanted |= Q(supplier_id__in=SlipFormat.objects.filter(pk__in=slip_format_ids).values("supplier_id"))
            formats = list(SlipFormat.objects.filter(wanted).order_by("name", "pk").values("pk", "supplier_id", "name"))
        formats_of = defaultdict(list)
        for row in formats:
            formats_of[row["supplier_id"]].append(row)
        board.formats_of = dict(formats_of)
        board.index = SlipIndex.load({row["pk"] for row in formats})
        board.suppliers = supplier_ids | set(board.formats_of)

        dates = [pickup.date for pickup in pickups if pickup.supplier_id is not None]
        for slip in slips:
            info = board.index.infos.get(slip.pk)
            if info is not None and info.delivery_date is not None:
                dates.append(info.delivery_date)
        board.no_supplier_dates = {pickup.date for pickup in pickups if pickup.supplier_id is None}
        wanted = []
        if dates and board.suppliers:
            board.window = (shifted(min(dates), -_AROUND), shifted(max(dates), _AROUND))
            wanted.append(Q(supplier_id__in=board.suppliers, date__range=board.window))
        if board.no_supplier_dates:
            wanted.append(Q(supplier__isnull=True, date__in=board.no_supplier_dates))
        units = defaultdict(list)
        if wanted:
            query = wanted[0]
            for more in wanted[1:]:
                query |= more
            loaded = Pickup.objects.filter(query).select_related("supplier").prefetch_related("counts")
            for pickup in loaded.order_by("date", "pk"):
                units[(pickup.supplier_id, pickup.date)].append(pickup)
        board.units = dict(units)

        needed = {slip.pk for slip in slips}
        for supplier_id, day in board.units:
            needed.update(info.pk for info in board.effective_on(supplier_id, day))
        board.index.ensure_lines(needed)
        return board

    # -- the slips --

    @property
    def superseded(self) -> dict:
        return self.index.superseded

    def effective_on(self, supplier_id, day) -> list:
        """The slips that count, of `supplier_id`'s formats, delivered `day`."""
        if self._by_day is None:
            by_day = defaultdict(list)
            for info in self.index.effective():
                if info.delivery_date is not None:
                    by_day[(info.supplier_id, info.delivery_date)].append(info)
            for infos in by_day.values():
                infos.sort(key=lambda info: info.key)
            self._by_day = dict(by_day)
        return self._by_day.get((supplier_id, day), [])

    def classify(self, designation: str) -> Classification:
        return self.classifier.classify(designation)

    @property
    def type_errors(self) -> list:
        """[(type, message)] of the types whose patterns no longer compile."""
        return [(kind, self.classifier.errors[kind.pk]) for kind in self.types if kind.pk in self.classifier.errors]

    # -- the days --

    def _loaded(self, supplier_id, day) -> bool:
        if supplier_id is None:
            return day in self.no_supplier_dates
        return supplier_id in self.suppliers and self.window is not None and self.window[0] <= day <= self.window[1]

    def day(self, pickup) -> DayComparison:
        """The comparison `pickup` is part of: every pickup of its supplier
        that day, against the slips that count for that day."""
        key = (pickup.supplier_id, pickup.date)
        if not self._loaded(*key):
            raise LookupError(f"La reprise {pickup.pk} n'a pas été chargée dans ce Board.")
        if key not in self._days:
            self._days[key] = self._compute(key, self.units.get(key) or [pickup])
        return self._days[key]

    def _compute(self, key, pickups) -> DayComparison:
        supplier_id, day = key
        counts = defaultdict(int)
        for pickup in pickups:
            for count in pickup.counts.all():
                counts[count.returnable_type_id] += count.quantity
        base = {
            "supplier": pickups[0].supplier,
            "date": day,
            "pickups": pickups,
            "counts": dict(counts),
            "types": self.types,
        }
        if supplier_id is None:
            return DayComparison(status=NO_SUPPLIER, **base)
        if not self.formats_of.get(supplier_id):
            return DayComparison(status=NO_FORMAT, **base)
        slips = self.effective_on(supplier_id, day)
        if not slips:
            return DayComparison(status=WAITING, hints=self._hints_of(supplier_id).get(day, []), **base)
        self.index.ensure_lines(info.pk for info in slips)
        lines = [line for info in slips for line in info.lines]
        comparison = compare(base["counts"], lines, self.types, classifier=self.classifier)
        reasons = [reason for info in slips for reason in slip_problems(info)] + _incomplete_reasons(comparison)
        if reasons:
            status = TO_CHECK
        else:
            status = SAME if comparison.agrees else DIFFERS
        return DayComparison(status=status, slips=slips, reasons=reasons, comparison=comparison, **base)

    def _hints_of(self, supplier_id) -> dict:
        """{pickup day: [Hint]} for one supplier: each unpaired slip within
        HINT_DAYS of an unpaired pickup day is offered to the nearest one
        (ties: the earlier) - once."""
        if supplier_id in self._hints:
            return self._hints[supplier_id]
        days_with_pickups = {day for (supplier, day) in self.units if supplier == supplier_id}
        unpaired_days = sorted(day for day in days_with_pickups if not self.effective_on(supplier_id, day))
        hints = defaultdict(list)
        if unpaired_days and self.window is not None:
            # Only a slip whose every rival day was loaded: within HINT_DAYS
            # of the days the page asked about.
            first = shifted(self.window[0], HINT_DAYS)
            last = shifted(self.window[1], -HINT_DAYS)
            for info in sorted(self.index.effective(), key=lambda info: info.key):
                day = info.delivery_date
                if info.supplier_id != supplier_id or day is None or not first <= day <= last:
                    continue
                if day in days_with_pickups:
                    continue
                near = [candidate for candidate in unpaired_days if abs((candidate - day).days) <= HINT_DAYS]
                if not near:
                    continue
                best = min(near, key=lambda candidate: (abs((candidate - day).days), candidate))
                hints[best].append(Hint(info, day, (day - best).days))
        self._hints[supplier_id] = dict(hints)
        return self._hints[supplier_id]

    def slip_state(self, slip) -> SlipState:
        """What the « Bons reçus » table says of a slip handed to `load`."""
        info = self.index.infos.get(slip.pk)
        if info is None:
            raise LookupError(f"Le bon {slip.pk} n'a pas été chargé dans ce Board.")
        superseded = self.superseded.get(info.pk)
        if superseded is not None:
            return SlipState(superseded.kind, superseded.pill, "ignored", superseded.reason, superseded=superseded)
        if info.read_error:
            return SlipState(
                "unreadable", STATUSES[TO_CHECK].pill, "pending", f"Lecture impossible : {info.read_error}"
            )
        if info.delivery_date is None:
            return SlipState(
                "no_date",
                "date de livraison non lue",
                "pending",
                "Date de livraison non lue : ce bon ne peut être rapproché d'aucune reprise.",
            )
        key = (info.supplier_id, info.delivery_date)
        pickups = self.units.get(key)
        if pickups:
            day = self.day(pickups[0])
            return SlipState("paired", day.pill, day.css, day.reasons[0] if day.reasons else "", day=day)
        hint_date = next(
            (
                day
                for day, hints in self._hints_of(info.supplier_id).items()
                for hint in hints
                if hint.slip.pk == info.pk
            ),
            None,
        )
        self.index.ensure_lines([info.pk])
        if not info.lines:
            # No line may be a part read empty - or rows the line pattern no
            # longer reads (a layout change): then a check failed, and
            # « aucun vide repris » would say the opposite of the slip.
            problems = slip_problems(info)
            if problems:
                return SlipState(TO_CHECK, STATUSES[TO_CHECK].pill, "pending", problems[0], hint_date=hint_date)
            return SlipState(
                "nothing_back", "aucun vide repris", "ignored", "Rien n'a été repris selon ce bon.", hint_date=hint_date
            )
        return SlipState(
            "no_pickup",
            "sans reprise enregistrée",
            "ignored",
            f"Aucune reprise enregistrée le {info.delivery_date:%d/%m/%Y}.",
            hint_date=hint_date,
        )


def latest_note() -> str:
    """The latest pickup's comparison in one sentence ("" when there is no
    pickup) - what the gather writes as its progress NOTE, never as an
    error."""
    pickup = Pickup.objects.order_by("-date", "-pk").first()
    if pickup is None:
        return ""
    return Board.load(pickups=[pickup]).day(pickup).sentence
