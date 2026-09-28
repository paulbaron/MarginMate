"""Everything that CAME IN on the account over a window, beside what the till
says it was paid over the same days.

**This is « Dépenses » the other way round, and it is not « Marges ».**
Dépenses counts what the bank took; this counts what the bank received, by
the date it received it. Marges counts what the till SOLD against what was
invoiced; this sets what the till was PAID - card, cash, cheque, as its
tickets record them (`recipes.PosDailyPayment`) - against what reached the
account. The owner's question (27/09): does the money that came in match
what was sold?

What the statement shows, and how each line is recognised - every rule said
on the page, because what the application recognises has to be visible:

* **A card payout** is a credit whose label carries « TOTAL ENCAISSE <number>
  EURO(S) » (`PAYOUT_RE`): the payment terminal's provider pays the card
  takings in one transfer, prints the GROSS in the label (dot decimal), and
  the line's amount is what arrived - net of its commission. Recognised by
  those words, never by the provider's name: a name is the one thing a new
  contract changes. The commission is gross − net, said as it is, even when
  it comes out odd (a net above the gross is a negative commission, printed
  so, never corrected).
* **Cash deposited**: bank type « VERSEMENT ESPECES ». **Cheques deposited**:
  « REMISE CHEQUES ».
* **Everything else** is « Autres entrées »: a private party paying an
  event, a partner's contribution, a supplier's refund, a direct debit
  returned. A person names them with the SAME free-text
  `BankTransaction.category` « Dépenses » uses - blank is « Sans catégorie »,
  listed first - and the datalist offers the words already typed on credits
  only, never the spending ones.

**How a payout is linked to the till's card sales.** Measured on the real
statement (the measurement stays out of a public repository; the rules do
not): a payout lands one to three days after the sale, Monday pays Friday and
Saturday, and over a year the till's card payments and the payouts' gross
agree closely - but a single payout often equals no run of consecutive till
days exactly, because the provider's batches do not follow the till's
service days and a payment after midnight moves. So
the link is a RUNNING BALANCE (`Balance`), « ventes carte pas encore
versées », computed over the WHOLE history so a window cannot change it: it
sits near a steady level (the last few days not yet paid) and a card sale
never paid - a ticket settled in reality by transfer, a payout missing -
shows as a permanent step. An exact run (`exact_runs`) is an annotation,
« même montant au centime », never a claim that a payout paid given days
when the amounts do not say so.

Pure of request and template: a `DateRange` in, an `IncomeReport` out, in
`QUERIES` queries whatever the history holds.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db.models import Max, Min

from common import DateRange, search_key
from recipes.models import PosDailyPayment, PosProductDailyQuantity

from .models import BankTransaction
from .spending import NO_CATEGORY

ZERO = Decimal("0")
HUNDRED = Decimal("100")
RATE_PLACES = Decimal("0.01")

#: « … TOTAL ENCAISSE 987.65 EUROS … »: the words, the gross, the currency.
#: The number is the provider's own (a dot decimal; a comma is read too, and
#: thousands grouped by spaces). Anything else - no number, a number the
#: pattern cannot read whole - is NOT a payout: it falls to « Autres
#: entrées », where it is seen, rather than being read as a wrong gross.
PAYOUT_RE = re.compile(
    r"TOTAL\s+ENCAISS[EÉ]\s+(\d{1,3}(?:\s\d{3})+|\d+)(?:[.,](\d+))?\s+EUROS?\b", re.IGNORECASE
)
#: The bank types of the two deposits, compared accent- and case-blind
#: (`common.search_key`), so « ESPÈCES » and « Especes » are one type.
CASH_TYPE = "versement especes"
CHEQUE_TYPE = "remise cheque"

#: Where a credit came from - one vocabulary for the report, the page and
#: « Banque »'s « Entrées » tab.
CARD, CASH, CHEQUE, OTHER = "carte", "especes", "cheques", "autres"
SOURCES = {
    CARD: "Versements carte",
    CASH: "Espèces déposées",
    CHEQUE: "Chèques remis",
    OTHER: "Autres entrées",
}
#: What one line of each is called on « Banque ».
ONE = {
    CARD: "Versement carte",
    CASH: "Dépôt d'espèces",
    CHEQUE: "Remise de chèques",
}

#: A payout pays sales of the days BEFORE it: an exact run is looked for in
#: [T − RUN_DAYS, T − 1]. Eight, so Monday's payout still reaches back past
#: a long weekend - and never further, where some run of days always adds up
#: to anything.
RUN_DAYS = 8
#: Where the balance's starting day is looked for, before the first payout
#: of the statement: « Monday pays Friday and Saturday », and a week covers
#: the longest gap measured.
ANCHOR_DAYS = 7

#: What `income_for` costs, whatever the history holds: the credits, the
#: till's payments, the till's days of the window, the till's last day, and
#: the statement's first and last day (one aggregate). A test holds it - an
#: N+1 here is one query per day of the year.
QUERIES = 5

#: The sources a till row is compared with - « Autres entrées » is compared
#: with nothing, so nothing of it can be « before the till ».
COMPARED = (CARD, CASH, CHEQUE)


def payout_gross(label) -> Decimal | None:
    """The gross a card payout's label prints, or None when the label is no
    payout's (`PAYOUT_RE`)."""
    found = PAYOUT_RE.search(label or "")
    if found is None:
        return None
    whole, decimals = found.groups()
    try:
        return Decimal(f"{''.join(whole.split())}.{decimals or '0'}")
    except InvalidOperation:  # pragma: no cover - the pattern holds digits only
        return None


def source_of(line: BankTransaction) -> str:
    """CARD, CASH, CHEQUE or OTHER - the rules the page states."""
    if payout_gross(line.label) is not None:
        return CARD
    kind = search_key(line.bank_type or "")
    if CASH_TYPE in kind:
        return CASH
    if CHEQUE_TYPE in kind:
        return CHEQUE
    return OTHER


@dataclass(frozen=True)
class Entry:
    """One credit, as this page reads it: where it came from, and for a
    payout what the provider says it collected."""

    line: BankTransaction
    source: str
    #: The payout's gross, as its label prints it. None on anything else.
    gross: Decimal | None = None

    @property
    def day(self) -> date:
        return self.line.operation_date

    @property
    def net(self) -> Decimal:
        """What arrived on the account."""
        return self.line.amount

    @property
    def commission(self) -> Decimal | None:
        """Gross − net, as it comes: odd (negative) is said, not mended."""
        return None if self.gross is None else self.gross - self.net

    @property
    def commission_rate(self) -> Decimal | None:
        return rate(self.commission, self.gross) if self.gross is not None else None

    @property
    def category(self) -> str:
        """What a person typed on it, « Sans catégorie » for nothing - read
        for « Autres entrées » only: a payout or a deposit is named by the
        rules above, whatever was typed on it."""
        return self.line.category.strip() or NO_CATEGORY

    @property
    def unnamed(self) -> bool:
        return self.source == OTHER and not self.line.category.strip()

    @property
    def name(self) -> str:
        """What « Banque » calls it on its « Entrées » tab."""
        return ONE.get(self.source) or self.category


def entry_for(line: BankTransaction) -> Entry:
    source = source_of(line)
    return Entry(line, source, payout_gross(line.label) if source == CARD else None)


def rate(part: Decimal | None, whole: Decimal | None) -> Decimal | None:
    """`part` as a percentage of `whole`, to the hundredth - None where the
    whole is nothing: a share of nothing is not 0 %."""
    if part is None or whole is None or whole <= 0:
        return None
    return (part / whole * HUNDRED).quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class CategoryTotal:
    """« Autres entrées » of one name."""

    name: str
    amount: Decimal = ZERO
    count: int = 0


@dataclass
class TillMethod:
    """What the till was paid by one means of payment over the window."""

    method: str
    amount: Decimal = ZERO
    payments: int = 0

    @property
    def label(self) -> str:
        return PosDailyPayment.label_for(self.method)


@dataclass
class MethodRow:
    """One means of payment: what the till says it was paid, and what came
    in on the account for it. `till` is None for a row the till knows
    nothing of (« Autres entrées »), `bank` None for one the account cannot
    see (an « Avoir » paid days before)."""

    key: str
    label: str
    till: Decimal | None = None
    till_payments: int = 0
    bank: Decimal | None = None
    bank_count: int = 0
    #: The card row only: what the payouts' gross became on the account.
    net: Decimal | None = None
    commission: Decimal | None = None
    note: str = ""
    #: Of `till`, what was taken on days before the statement's first line:
    #: the account cannot show it, whatever happened to it.
    uncovered: Decimal = ZERO
    #: Of `bank`, what arrived before the first till day whose payments
    #: were read: the till cannot show what it paid.
    bank_uncovered: Decimal = ZERO

    @property
    def difference(self) -> Decimal | None:
        """What came in less what the till took, where both sides exist -
        over the days BOTH sides cover. The columns show the whole window;
        compared whole, a till history reaching back past the statement
        read as card takings that never arrived (and « tout » is where
        Banque's stat lands). The page says how much each side holds that
        the other cannot see."""
        if self.till is None or self.bank is None:
            return None
        return (self.bank - self.bank_uncovered) - (self.till - self.uncovered)


@dataclass
class Month:
    """One month of the window, both sides side by side. `label` is the
    page's (the view names months in French)."""

    first_day: date
    takings: Decimal = ZERO
    card_sold: Decimal = ZERO
    payouts_gross: Decimal = ZERO
    commission: Decimal = ZERO
    net: Decimal = ZERO
    cash_sold: Decimal = ZERO
    cash_deposited: Decimal = ZERO
    label: str = ""


@dataclass
class PayoutRow:
    """One payout of the window, with what the balance says after it."""

    entry: Entry
    #: The first and last till day of an exact run, or None.
    run: tuple[date, date] | None = None
    #: « Ventes carte pas encore versées » after this payout. None for a
    #: payout the balance does not count (dated before the till's history).
    pending: Decimal | None = None


@dataclass
class Balance:
    """The running balance « ventes carte pas encore versées », over the
    whole history.

    `anchor` is the day it counts from: the day, in the week before the
    statement's first payout, from which the card sold up to that payout
    comes closest to what it paid. `pending` is keyed by a payout's pk.
    `reason` says why there is none, and is empty when there is one.
    """

    anchor: date | None = None
    first_payout: date | None = None
    pending: dict[int, Decimal] = field(default_factory=dict)
    reason: str = ""


#: Why there is no balance - the page prints these.
NO_CARD_DAYS = (
    "aucun paiement par carte n'est lu en caisse. Les moyens de paiement se relisent depuis les exports "
    "déjà téléchargés : manage.py laddition_backfill_payments."
)
NO_PAYOUT = "aucun versement carte sur le relevé après le premier jour de caisse lu."


@dataclass
class IncomeReport:
    window: DateRange
    #: Every credit of the window.
    received_total: Decimal = ZERO
    received_count: int = 0
    #: {source: (total, count)} - what arrived, net, from each source.
    by_source: dict[str, tuple[Decimal, int]] = field(default_factory=dict)
    payouts: list[PayoutRow] = field(default_factory=list)
    others: list[Entry] = field(default_factory=list)
    other_categories: list[CategoryTotal] = field(default_factory=list)

    #: The till over the same days. `takings` counts the days whose money
    #: was read (`revenue_read`); `unread_revenue_days` says how many were not.
    takings: Decimal = ZERO
    till: list[TillMethod] = field(default_factory=list)
    till_total: Decimal = ZERO
    #: Payments less takings, on the days both are read - « dont pourboires
    #: et trop-perçus ». A ticket's payments are its total plus its
    #: overpayment, so this is what the tickets say was left on top.
    tips: Decimal = ZERO
    tips_days: int = 0

    rows: list[MethodRow] = field(default_factory=list)
    months: list[Month] = field(default_factory=list)

    #: Till days of the window with sales and no means of payment read.
    days_without_payments: list[date] = field(default_factory=list)
    #: Till days of the window with a product whose money was never read.
    unread_revenue_days: list[date] = field(default_factory=list)
    #: Where each side starts and stops, over the whole history.
    last_till_day: date | None = None
    first_payment_day: date | None = None
    last_payment_day: date | None = None
    first_statement_day: date | None = None
    last_statement_day: date | None = None

    #: {till method: amount} the till took, inside the window, on days
    #: before the statement's first line - and the first and last such day.
    #: Nothing is counted when there is no statement at all: there is no
    #: edge then, only an empty account (the page says « aucun relevé »).
    till_before_statement: dict[str, Decimal] = field(default_factory=dict)
    till_before_from: date | None = None
    till_before_to: date | None = None
    #: {source: amount} that arrived, inside the window, before the first
    #: till day whose payments were read - CARD at the gross, the figure
    #: its row compares - and the first and last such day.
    bank_before_till: dict[str, Decimal] = field(default_factory=dict)
    bank_before_from: date | None = None
    bank_before_to: date | None = None

    balance: Balance = field(default_factory=Balance)

    @property
    def till_before_statement_total(self) -> Decimal:
        return sum(self.till_before_statement.values(), ZERO)

    @property
    def till_card_before_statement(self) -> Decimal:
        return self.till_before_statement.get(PosDailyPayment.CARD, ZERO)

    @property
    def bank_before_till_total(self) -> Decimal:
        return sum(self.bank_before_till.values(), ZERO)

    @property
    def covered_since(self) -> date | None:
        """The first day both sides can speak of: the later of the
        statement's first line and the till's first day of payments read.
        None while either side holds nothing."""
        if self.first_statement_day is None or self.first_payment_day is None:
            return None
        return max(self.first_statement_day, self.first_payment_day)

    def source_total(self, source: str) -> Decimal:
        return self.by_source.get(source, (ZERO, 0))[0]

    def source_count(self, source: str) -> int:
        return self.by_source.get(source, (ZERO, 0))[1]

    @property
    def card_net(self) -> Decimal:
        return self.source_total(CARD)

    @property
    def card_count(self) -> int:
        return self.source_count(CARD)

    @property
    def card_gross(self) -> Decimal:
        return sum((row.entry.gross for row in self.payouts), ZERO)

    @property
    def card_commission(self) -> Decimal:
        return self.card_gross - self.card_net

    @property
    def card_commission_rate(self) -> Decimal | None:
        return rate(self.card_commission, self.card_gross)

    @property
    def cash_total(self) -> Decimal:
        return self.source_total(CASH)

    @property
    def cash_count(self) -> int:
        return self.source_count(CASH)

    @property
    def cheque_total(self) -> Decimal:
        return self.source_total(CHEQUE)

    @property
    def cheque_count(self) -> int:
        return self.source_count(CHEQUE)

    @property
    def others_total(self) -> Decimal:
        return self.source_total(OTHER)

    @property
    def unnamed_others(self) -> int:
        return sum(1 for one in self.others if one.unnamed)

    @property
    def last_payout(self) -> PayoutRow | None:
        """The window's last payout the balance counts - the headline."""
        counted = [row for row in self.payouts if row.pending is not None]
        return counted[-1] if counted else None

    @property
    def balance_points(self) -> list[tuple[date, Decimal]]:
        """The balance over the window, one point per payout DAY (the last
        payout of a day says where the day ended): a line chart has one
        value per date."""
        points: dict[date, Decimal] = {}
        for row in self.payouts:
            if row.pending is not None:
                points[row.entry.day] = row.pending
        return sorted(points.items())

    @property
    def till_read(self) -> bool:
        """Whether the till says anything about the window at all."""
        return bool(self.till) or self.takings != 0


def income_for(window: DateRange) -> IncomeReport:
    """Everything that came in over `window` (bank lines by
    `operation_date`, till days by `sold_on`, both ends included; an empty
    window is all of it), beside the till over the same days - and the
    running balance of card sales not yet paid, which is computed over the
    whole history and only SHOWN for the window."""
    report = IncomeReport(window=window)

    # Every credit, the whole history: the balance and the exact runs are
    # worked out from the first payout on, so that a window cannot change
    # them. Windowed here, in Python, with the one definition of « in the
    # window » that is not SQL (`DateRange.holds`).
    entries = [entry_for(line) for line in BankTransaction.objects.filter(amount__gt=0).order_by("operation_date", "pk")]
    payments = list(PosDailyPayment.objects.values_list("sold_on", "method", "amount", "payments"))
    # Where each side starts and stops - read before the loops below, which
    # set apart what one side holds on days the other cannot see. The
    # statement's ends are one aggregate over every line, debits included:
    # a debit on a day says the statement covers that day.
    statement = BankTransaction.objects.aggregate(first=Min("operation_date"), last=Max("operation_date"))
    report.first_statement_day, report.last_statement_day = statement["first"], statement["last"]
    report.first_payment_day = min((row[0] for row in payments), default=None)
    report.last_payment_day = max((row[0] for row in payments), default=None)
    card_days: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for sold_on, method, amount, _count in payments:
        if method == PosDailyPayment.CARD:
            card_days[sold_on] += amount
    card_days = dict(card_days)
    payouts = [entry for entry in entries if entry.source == CARD]
    report.balance = running_balance(card_days, payouts)
    runs = exact_runs(card_days, payouts)

    months: dict[date, Month] = {}

    def month_of(day: date) -> Month:
        first = day.replace(day=1)
        found = months.get(first)
        if found is None:
            found = months[first] = Month(first)
        return found

    by_source: dict[str, list] = defaultdict(lambda: [ZERO, 0])
    categories: dict[str, CategoryTotal] = {}
    bank_before: dict[str, Decimal] = defaultdict(lambda: ZERO)
    bank_before_days: list[date] = []
    for entry in entries:
        if not window.holds(entry.day):
            continue
        report.received_total += entry.net
        report.received_count += 1
        by_source[entry.source][0] += entry.net
        by_source[entry.source][1] += 1
        # Money in before the till's first day of payments read pays sales
        # the till never read: compared, it reads as money from nowhere.
        if (
            report.first_payment_day is not None
            and entry.day < report.first_payment_day
            and entry.source in COMPARED
        ):
            amount = entry.gross if entry.source == CARD else entry.net
            if amount:
                bank_before[entry.source] += amount
                bank_before_days.append(entry.day)
        month = month_of(entry.day)
        if entry.source == CARD:
            report.payouts.append(
                PayoutRow(entry, runs.get(entry.line.pk), report.balance.pending.get(entry.line.pk))
            )
            month.payouts_gross += entry.gross
            month.net += entry.net
            month.commission += entry.commission
        elif entry.source == CASH:
            month.cash_deposited += entry.net
        elif entry.source == OTHER:
            report.others.append(entry)
            category = categories.setdefault(entry.category, CategoryTotal(entry.category))
            category.amount += entry.net
            category.count += 1
    report.by_source = {key: (value[0], value[1]) for key, value in by_source.items()}
    report.bank_before_till = dict(bank_before)
    if bank_before_days:
        report.bank_before_from, report.bank_before_to = min(bank_before_days), max(bank_before_days)
    report.other_categories = _ordered(categories.values())
    # The unnamed first - they are the work - then the biggest.
    report.others.sort(key=lambda one: (not one.unnamed, -one.net, one.day, one.line.pk))

    # The till. Its payments per day and method, and its takings per day.
    till: dict[str, TillMethod] = {}
    paid_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    till_before: dict[str, Decimal] = defaultdict(lambda: ZERO)
    till_before_days: list[date] = []
    for sold_on, method, amount, count in payments:
        if not window.holds(sold_on):
            continue
        one = till.setdefault(method, TillMethod(method))
        one.amount += amount
        one.payments += count
        paid_by_day[sold_on] += amount
        # Taken on a day before the statement's first line: whatever became
        # of it, the account cannot show it. Counted apart so the Écart is
        # taken over the days both sides cover, and said on the page.
        if report.first_statement_day is not None and sold_on < report.first_statement_day and amount:
            till_before[method] += amount
            till_before_days.append(sold_on)
        month = month_of(sold_on)
        if method == PosDailyPayment.CARD:
            month.card_sold += amount
        elif method == PosDailyPayment.CASH:
            month.cash_sold += amount
    report.till = sorted(till.values(), key=lambda one: PosDailyPayment.sort_key(one.method))
    report.till_total = sum((one.amount for one in report.till), ZERO)
    # Methods that net to nothing over those days are no edge to speak of.
    report.till_before_statement = {method: amount for method, amount in till_before.items() if amount}
    if till_before_days:
        report.till_before_from, report.till_before_to = min(till_before_days), max(till_before_days)

    taken_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    unread: set[date] = set()
    rows = window.limit(PosProductDailyQuantity.objects.all(), "sold_on").values_list(
        "sold_on", "revenue_ttc", "revenue_read"
    )
    for sold_on, revenue_ttc, revenue_read in rows:
        # A day nobody read the money of holds 0,00 € it never took: it is
        # counted apart and named at the top, never read as a quiet day.
        if not revenue_read:
            unread.add(sold_on)
            continue
        taken_by_day[sold_on] += revenue_ttc
        report.takings += revenue_ttc
        month_of(sold_on).takings += revenue_ttc
    sold_days = set(taken_by_day) | unread
    report.days_without_payments = sorted(day for day in sold_days if day not in paid_by_day)
    report.unread_revenue_days = sorted(unread)
    # Tips on the days both sides are read whole - a day half-read would
    # count its unread takings as tips.
    both = [day for day in paid_by_day if day in taken_by_day and day not in unread]
    report.tips = sum((paid_by_day[day] - taken_by_day[day] for day in both), ZERO)
    report.tips_days = len(both)

    report.rows = _method_rows(report, till)
    report.months = _months(months)

    report.last_till_day = PosProductDailyQuantity.objects.aggregate(last=Max("sold_on"))["last"]
    return report


def running_balance(card_days: dict[date, Decimal], payouts: list[Entry]) -> Balance:
    """« Ventes carte pas encore versées » after each payout, over the whole
    history.

    F is the till's first card day, and only a payout dated after it counts:
    before, the till says nothing about what it paid. T1 is the first such
    payout's date and G1 what the payouts of that day collected. The balance
    counts the card sold from an anchor A - the day in [max(F, T1 − 7), T1 −
    1] whose card sold up to T1 comes closest to G1, the latest on a tie -
    so it starts from what the statement's first payout says it paid, not
    from a guess. After each payout i: card sold on A ≤ d < T_i, less every
    gross paid up to and including i.

    A payout that never came, or a card ticket paid in reality by transfer,
    is a step the balance never comes back down from - which is the point:
    that is money the page cannot find on the account.
    """
    days = sorted(card_days)
    if not days:
        return Balance(reason=NO_CARD_DAYS)
    first = days[0]
    counted = [payout for payout in payouts if payout.day > first]
    if not counted:
        return Balance(reason=NO_PAYOUT)
    sold_before = _card_sold_before(days, card_days)

    t1 = counted[0].day
    g1 = sum((payout.gross for payout in counted if payout.day == t1), ZERO)
    anchor, best = None, None
    day = t1 - timedelta(days=1)
    lowest = max(first, t1 - timedelta(days=ANCHOR_DAYS))
    # From the latest day back, a strictly better gap only: a tie keeps the
    # latest day, which counts the fewest days the payout may not have paid.
    while day >= lowest:
        gap = abs(sold_before(t1) - sold_before(day) - g1)
        if best is None or gap < best:
            anchor, best = day, gap
        day -= timedelta(days=1)

    balance = Balance(anchor=anchor, first_payout=t1)
    paid = ZERO
    for payout in counted:
        paid += payout.gross
        balance.pending[payout.line.pk] = sold_before(payout.day) - sold_before(anchor) - paid
    return balance


def _card_sold_before(days: list[date], card_days: dict[date, Decimal]):
    """card sold on every day strictly before `day`, as a function - one
    running sum, bisected, rather than a sum per payout."""
    running = [ZERO]
    for day in days:
        running.append(running[-1] + card_days[day])

    def sold_before(day: date) -> Decimal:
        return running[bisect_left(days, day)]

    return sold_before


def exact_runs(card_days: dict[date, Decimal], payouts: list[Entry]) -> dict[int, tuple[date, date]]:
    """{payout pk: (first day, last day)} for the payouts whose gross equals,
    to the cent, the card sold on a run of consecutive card days.

    Consecutive means adjacent in the sorted list of days that sold by card
    - a day the bar was shut does not break a run - within [T − RUN_DAYS,
    T − 1], no day already claimed by an earlier payout. The run ending
    latest wins, then the shortest; its days are claimed. Payouts are taken
    in date order over the whole history, so a window never changes which
    days a payout's run holds. Nothing found is None, and the page says « — »:
    two amounts equal to the cent is what this says, and nothing beyond it.
    """
    days = sorted(day for day, amount in card_days.items() if amount != 0)
    claimed: set[date] = set()
    found: dict[int, tuple[date, date]] = {}
    for payout in payouts:
        low = bisect_left(days, payout.day - timedelta(days=RUN_DAYS))
        high = bisect_right(days, payout.day - timedelta(days=1))
        run = None
        for end in range(high - 1, low - 1, -1):
            total = ZERO
            for start in range(end, low - 1, -1):
                if days[start] in claimed:
                    break
                total += card_days[days[start]]
                if total == payout.gross:
                    run = (start, end)
                    break
            if run is not None:
                break
        if run is not None:
            start, end = run
            claimed.update(days[start : end + 1])
            found[payout.line.pk] = (days[start], days[end])
    return found


#: What each row of the comparison says beside its figures - said, never
#: judged: a difference between the till and the account has reasons the
#: page cannot see.
NOTES = {
    CARD: (
        "Un versement arrive 1 à 3 jours après la vente : aux bords de la période, quelques jours de ventes "
        "carte passent d'un côté ou de l'autre. Le solde « ventes carte pas encore versées » dit s'il manque "
        "de l'argent."
    ),
    CASH: "La différence est gardée en caisse ou payée en liquide : la page ne sait pas laquelle.",
    CHEQUE: "Un chèque se remet quand on passe à la banque, pas le jour de la vente.",
    PosDailyPayment.CREDIT: (
        "Réglé par un acompte encaissé avant, souvent par virement : voir « Autres entrées »."
    ),
    PosDailyPayment.UNREAD: "Tickets dont la caisse n'a pas pu lire les paiements, comptés à leur total.",
    PosDailyPayment.UNPAID: "Tickets sans paiement enregistré, comptés à leur total.",
    OTHER: "Ce que la caisse ne voit pas : un virement reçu, un apport, un remboursement.",
}


def _method_rows(report: IncomeReport, till: dict[str, TillMethod]) -> list[MethodRow]:
    """« Ce que la caisse a encaissé, et ce qui est arrivé sur le compte »:
    one row per means of payment, the three the account can see first."""

    def paid(method: str) -> TillMethod:
        return till.get(method) or TillMethod(method)

    card = paid(PosDailyPayment.CARD)
    cash = paid(PosDailyPayment.CASH)
    cheque = paid(PosDailyPayment.CHEQUE)

    def edges(method: str, source: str) -> dict:
        """What each side of a row holds on days the other cannot see -
        left out of its Écart (`MethodRow.difference`)."""
        return {
            "uncovered": report.till_before_statement.get(method, ZERO),
            "bank_uncovered": report.bank_before_till.get(source, ZERO),
        }

    rows = [
        MethodRow(
            CARD,
            "Carte",
            card.amount,
            card.payments,
            report.card_gross,
            report.card_count,
            net=report.card_net,
            commission=report.card_commission,
            note=NOTES[CARD],
            **edges(PosDailyPayment.CARD, CARD),
        ),
        MethodRow(
            CASH,
            "Espèces",
            cash.amount,
            cash.payments,
            report.cash_total,
            report.cash_count,
            note=NOTES[CASH],
            **edges(PosDailyPayment.CASH, CASH),
        ),
    ]
    if cheque.payments or report.cheque_count:
        rows.append(
            MethodRow(
                CHEQUE,
                "Chèques",
                cheque.amount,
                cheque.payments,
                report.cheque_total,
                report.cheque_count,
                note=NOTES[CHEQUE],
                **edges(PosDailyPayment.CHEQUE, CHEQUE),
            )
        )
    seen = {PosDailyPayment.CARD, PosDailyPayment.CASH, PosDailyPayment.CHEQUE}
    for one in report.till:
        if one.method in seen:
            continue
        rows.append(
            MethodRow(
                one.method,
                one.label,
                one.amount,
                one.payments,
                note=NOTES.get(one.method, ""),
                uncovered=report.till_before_statement.get(one.method, ZERO),
            )
        )
    if report.others:
        rows.append(
            MethodRow(OTHER, "Autres entrées", bank=report.others_total, bank_count=len(report.others), note=NOTES[OTHER])
        )
    return rows


def _months(months: dict[date, Month]) -> list[Month]:
    """The window's months, from the first holding anything on either side
    to the last - a month between them with nothing at all included, since
    « nothing came in in March » is an answer and a missing row is not. The
    ends stop at the data: a window reaching back to before the bar opened
    is not a column of empty years."""
    if not months:
        return []
    ordered = []
    day, last = min(months), max(months)
    while day <= last:
        ordered.append(months.get(day) or Month(day))
        day = (day + timedelta(days=32)).replace(day=1)
    return ordered


def _ordered(categories) -> list[CategoryTotal]:
    """« Sans catégorie » first - the work to do - then the biggest, by name
    on a tie: the order « Dépenses » gives its own."""
    return [one for one in categories if one.name == NO_CATEGORY] + sorted(
        (one for one in categories if one.name != NO_CATEGORY),
        key=lambda one: (-one.amount, search_key(one.name), one.name),
    )


def known_categories() -> list[str]:
    """The categories already typed on CREDITS, for the datalist - never the
    spending ones (`spending.known_categories` reads debits only): a word
    for money that went out offered for money that came in files an income
    under a spending's name."""
    names = set(
        BankTransaction.objects.filter(amount__gt=0).exclude(category="").values_list("category", flat=True)
    )
    return sorted(names, key=lambda name: (search_key(name), name))
