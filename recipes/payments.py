"""The till's means of payment, per day: the one door they come in through.

What the till was PAID, by card, in cash, by cheque..., is what the bank
account is paid from - the sales pages say what was sold, this says how the
money arrived, and the bank's « Entrées d'argent » page sets the two side by
side. The reading is laddition_xlsx.parse_payment_rows; this module stores
it (PosDailyPayment), for the import job, `laddition_import` and
`laddition_backfill_payments` alike, and says what was unusual about it.

A day is the unit. Read again, a day REPLACES what was stored for it -
every method, one the new reading lacks included - exactly as the sales'
days do: a day imported half-way through its service is corrected by the
next import, and a folder of overlapping exports never adds a day to itself.
A day the reading does not cover is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TypeVar

from django.db import transaction

from .models import PosDailyPayment, PosProductDailyQuantity

#: Rows written or deleted per query: SQLite caps a statement's parameters.
BATCH = 500

#: Whatever a {method: ...} dict holds, handed back as it is (`by_method`).
_Held = TypeVar("_Held")


@dataclass
class RecordedPayments:
    """What a write did, in days - the unit a reader thinks in."""

    #: Days whose stored payments were replaced (or first written).
    days_written: int = 0
    #: Days read whose stored payments were already exactly that: left as
    #: they are, so a second run writes nothing at all.
    days_unchanged: int = 0
    rows_created: int = 0
    rows_deleted: int = 0
    #: Days the reading paid something on and « Ventes » holds no sale of
    #: (`beside_sales`): left as they are, and said.
    days_without_sales: list = field(default_factory=list)


def _shape(payments: dict) -> frozenset:
    """A day's payments as a comparable value: {(method, amount, count)}.
    Decimal compares by value, so the database's 4.50 and a reading's 4.5
    are one amount."""
    return frozenset((method, payment.amount, payment.count) for method, payment in payments.items())


def day_total(payments: dict):
    """What a day's {method: DayPayment} adds up to."""
    return sum((payment.amount for payment in payments.values()), Decimal("0"))


def stored_by_day(days) -> dict[date, frozenset]:
    """{day: its stored payments, as _shape} for the days given that hold
    any. One query over the days' span, filtered here: a backfill asks about
    a few hundred days at once, more than one IN list may name."""
    days = set(days)
    if not days:
        return {}
    stored: dict[date, set] = {}
    for sold_on, method, amount, count in PosDailyPayment.objects.filter(
        sold_on__gte=min(days), sold_on__lte=max(days)
    ).values_list("sold_on", "method", "amount", "payments"):
        if sold_on in days:
            stored.setdefault(sold_on, set()).add((method, amount, count))
    return {day: frozenset(rows) for day, rows in stored.items()}


def changed_days(readings: dict, days) -> tuple[list[date], int]:
    """(the days of `days` whose stored payments are not what `readings`
    says, in order; how many already are). What a dry run reports and what
    replace_days writes are this one answer."""
    days = sorted(set(days))
    stored = stored_by_day(days)
    changed = [day for day in days if stored.get(day, frozenset()) != _shape(readings.get(day, {}))]
    return changed, len(days) - len(changed)


def replace_days(readings: dict, days) -> RecordedPayments:
    """Make each of `days` hold exactly what `readings[day]` says
    ({method: DayPayment}; {} = read, nothing paid), in one transaction.

    A day whose stored rows already say that is left alone - not deleted and
    written back - so a second run changes nothing, not even an id.
    """
    result = RecordedPayments()
    with transaction.atomic():
        changed, result.days_unchanged = changed_days(readings, days)
        for start in range(0, len(changed), BATCH):
            deleted, _ = PosDailyPayment.objects.filter(sold_on__in=changed[start : start + BATCH]).delete()
            result.rows_deleted += deleted
        rows = [
            PosDailyPayment(sold_on=day, method=method, amount=payment.amount, payments=payment.count)
            for day in changed
            for method, payment in readings.get(day, {}).items()
        ]
        PosDailyPayment.objects.bulk_create(rows, batch_size=BATCH)
        result.rows_created = len(rows)
        result.days_written = len(changed)
    return result


def days_with_sales(days=None) -> set[date]:
    """The days « Ventes » holds a till sale of (`PosProductDailyQuantity`) -
    among `days` when given (one query over their span), else all of them."""
    queryset = PosProductDailyQuantity.objects.all()
    if days is not None:
        days = set(days)
        if not days:
            return set()
        queryset = queryset.filter(sold_on__gte=min(days), sold_on__lte=max(days))
    held = set(queryset.values_list("sold_on", flat=True).distinct())
    return held if days is None else held & days


def record_payments(export, *, beside_sales: bool = False) -> RecordedPayments:
    """Store the payments a parsed export read, every day it read replaced
    whole. An export whose payments were not read (no payments sheet, or
    one that failed) replaces nothing: its days keep what they have - the
    same care the day's money takes (tasks._sync_pos_products).

    `beside_sales`: only the days « Ventes » holds a sale of are written -
    payments on a day with no sales are money the sales pages contradict
    (« A day's payments only beside a day « Ventes » holds »). The import
    job held that rule by its order, writing the lines first; a file of
    payments alone holds it here. The days it paid something on and left
    are `days_without_sales`; a day it paid nothing on (every ticket
    comped) is nothing to write, and is not listed."""
    if not getattr(export, "payments_read", False):
        return RecordedPayments()
    readings = export.payments_by_day()
    if not beside_sales:
        return replace_days(readings, export.payment_days)
    sold = days_with_sales(export.payment_days)
    result = replace_days(readings, sold)
    result.days_without_sales = sorted(day for day in export.payment_days if day not in sold and readings.get(day))
    return result


def by_method(payments: dict[str, _Held]) -> list[tuple[str, _Held]]:
    """{method: anything} as [(method, anything)] in PosDailyPayment's
    display order."""
    return sorted(payments.items(), key=lambda item: PosDailyPayment.sort_key(item[0]))


def oddities(export) -> list[str]:
    """Everything unusual about a payments reading, in French, counts only
    (each caller formats the money its own way). Each of these has never
    happened on the exports stored, which is exactly why each is said: the
    day one fires, it must not read as a quiet day."""
    said = []
    if export.unread_payment_tickets:
        said.append(
            f"{export.unread_payment_tickets} ticket(s) aux paiements illisibles : leur total est classé "
            f"« {PosDailyPayment.LABELS[PosDailyPayment.UNREAD]} », pour que le jour reste juste."
        )
    if export.unpaid_tickets:
        said.append(
            f"{export.unpaid_tickets} ticket(s) sans paiement mais avec un total : classé(s) "
            f"« {PosDailyPayment.LABELS[PosDailyPayment.UNPAID]} », à leur total."
        )
    if export.tickets_not_adding_up:
        said.append(
            f"{export.tickets_not_adding_up} ticket(s) dont les paiements ne font pas « Total TTC » + "
            "« Trop perçus » - gardés tels que payés, à vérifier."
        )
    if export.duplicate_tickets:
        said.append(f"{export.duplicate_tickets} ticket(s) en double dans un même fichier, lus une fois.")
    return said
