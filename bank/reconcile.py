"""The database side of bank matching: importing statements, linking what
bank.matching is sure of, and the decisions a person takes on the bank page.

A line a person linked, unlinked or declared without invoice is never
revisited by the automatic pass (`BankTransaction.settled_by_hand`), and
neither is one an active ignore rule matches.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Min, Prefetch, Q

from invoices.models import Invoice, InvoiceLine, Supplier

from . import matching, recognition
from .models import BankTransaction, CounterpartyAlias, IgnoreRule, InvoicePayment, StatementFormat
from .rules import compile_rules, ignoring_rule
from .statements import FormatError, Layout, check_format, parse_statement

CENTS = Decimal("0.01")
#: Why an import does not run in an espace with no format left.
NO_FORMAT = "Aucun format de relevé : ajoutez-en un sur « Format du relevé »."
#: How many fingerprints one query asks about. Django never splits an
#: `__in` list on SQLite, whose bound variables are capped (32 766 in the
#: bundled build, 999 in older ones): a statement of more operations than
#: that was an OperationalError - a 500 on the import and on « Tester ».
FINGERPRINT_CHUNK = 900


@dataclass
class ImportSummary:
    lines: int
    created: int

    @property
    def known(self) -> int:
        return self.lines - self.created


def invoice_label(invoice: Invoice) -> str:
    label = f"{invoice.supplier} n° {invoice.invoice_number or invoice.pk}"
    if invoice.invoice_date:
        label += f" du {invoice.invoice_date:%d/%m/%Y}"
    return label


def default_format() -> StatementFormat | None:
    """The format an import reads with when nobody chose one: the first by
    position, then name - None when the espace has none (one query)."""
    return StatementFormat.objects.order_by("position", "name").first()


def _layout(fmt) -> Layout:
    """`fmt` checked - a stored format the check refuses now (a pattern
    the guard of returnables.patterns has since learnt to refuse) says
    which format to correct, rather than a field's sentence out of context."""
    if isinstance(fmt, Layout):
        return fmt
    try:
        return check_format(fmt)
    except FormatError as error:
        raise ValueError(
            f"Import annulé : le format « {getattr(fmt, 'name', '')} » est à corriger sur « Format du relevé » - "
            f"{error.message}"
        ) from None


@transaction.atomic
def import_statement(
    content: bytes, rules: recognition.Rules | None = None, fmt: StatementFormat | Layout | None = None
) -> ImportSummary:
    """The statement's new lines, written - laid out as `fmt` says (the
    default format, `default_format()`, when not given) and described by
    `rules` (the active rules, `recognition.load()`, when not given): one
    query each, so a caller importing several files reads both once and
    hands them in. Refused whole (ValueError, the sentence to show) before
    anything is written: one file refused never leaves half its lines, and
    with no format at all nothing is read."""
    if fmt is None:
        fmt = default_format()
        if fmt is None:
            raise ValueError(NO_FORMAT)
    layout = _layout(fmt)
    statement = parse_statement(content, recognition.load() if rules is None else rules, layout)
    known = known_fingerprints(line.fingerprint for line in statement.lines)
    new = [
        BankTransaction(
            account=statement.account,
            operation_date=line.operation_date,
            value_date=line.value_date,
            card_date=line.card_date,
            bank_type=line.bank_type,
            kind=line.kind,
            label=line.label,
            counterparty=line.counterparty,
            amount=line.amount,
            fingerprint=line.fingerprint,
        )
        for line in statement.lines
        if line.fingerprint not in known
    ]
    BankTransaction.objects.bulk_create(new)
    return ImportSummary(lines=len(statement.lines), created=len(new))


def known_fingerprints(fingerprints) -> set[str]:
    """Those of `fingerprints` already stored - asked `FINGERPRINT_CHUNK`
    at a time, so a statement of any length is a few queries, never one
    past what SQLite binds."""
    fingerprints = list(dict.fromkeys(fingerprints))
    known: set[str] = set()
    for start in range(0, len(fingerprints), FINGERPRINT_CHUNK):
        chunk = fingerprints[start : start + FINGERPRINT_CHUNK]
        known.update(BankTransaction.objects.filter(fingerprint__in=chunk).values_list("fingerprint", flat=True))
    return known


def open_lines():
    """Spending lines still waiting for their invoice (ignore rules aside)."""
    return BankTransaction.objects.filter(amount__lt=0, no_invoice=False, payments__isnull=True)


def active_rules():
    return compile_rules(IgnoreRule.objects.filter(is_active=True))


def statements_start():
    """The first day the imported statements speak for, or None when none
    have been imported.

    Before it, « not reconciled » is not a fact about an invoice: the
    statements simply do not go back that far, and most of the documents
    with no payment are only that - they predate the first statement. So
    any list or count of unreconciled documents is drawn from this day on -
    see `invoices.workspace.unreconciled_q`.
    """
    return BankTransaction.objects.aggregate(first=Min("operation_date"))["first"]


#: What matching an invoice to the bank never reads of it, and is most of
#: what its row weighs: the document's texts, its checks, its VAT table.
#: Left in the database by the queries the bank pages make (`defer`) - read
#: on one of those invoices, each is a query of its own, which the bank
#: pages' query counts would show (bank/tests/test_page_cost.py).
UNREAD_INVOICE_FIELDS = ("ocr_text", "source_text", "parse_checks", "vat_breakdown")


def total_lines():
    """An invoice's lines as `Invoice.total_ttc_of` reads them, and nothing
    more: prefetched with these, any other column read on one is a query per
    line (bank/tests/test_page_cost.py)."""
    return InvoiceLine.objects.only("invoice", "total_ht", "vat_rate", "taxes", "printed_ttc", "discount_ttc")


def unpaid_invoices(start=None, end=None):
    """Invoices NO line pays at all, dated from `start` to `end` - and those
    with no date at all, which matching only ever suggests (OCR misses a
    receipt's date).

    « No payment at all » rather than « no payment from this line » on
    purpose: an invoice may now be paid by several lines, but only because a
    person said so. Left to the automatic pass, the next debit of the same
    amount would quietly settle an invoice already settled, and every figure
    on the page would still add up. This is what both the pass and the
    suggestions it proposes are drawn from.
    """
    dated = Q(invoice_date__isnull=False)
    if start is not None:
        dated &= Q(invoice_date__gte=start)
    if end is not None:
        dated &= Q(invoice_date__lte=end)
    invoices = Invoice.objects.filter(dated | Q(invoice_date__isnull=True), payments__isnull=True)
    # Into a list (`to_attr`): through the manager, Django clones a filtered
    # queryset for every invoice, which was most of the prefetch's time.
    lines = Prefetch("lines", queryset=total_lines(), to_attr="line_list")
    return invoices.select_related("supplier").defer(*UNREAD_INVOICE_FIELDS).prefetch_related(lines)


def rounded_total(invoice: Invoice) -> Decimal:
    """The invoice's `total_ttc` to the cent - from its `line_list` when it
    was loaded with one (`unpaid_invoices`, `views._fill`)."""
    lines = getattr(invoice, "line_list", None)
    total = invoice.total_ttc if lines is None else invoice.total_ttc_of(lines)
    return total.quantize(CENTS, rounding=ROUND_HALF_UP)


def candidates_from(invoices) -> list[matching.InvoiceCandidate]:
    return [
        matching.InvoiceCandidate(invoice.pk, invoice.supplier_id, invoice.invoice_date, rounded_total(invoice))
        for invoice in invoices
    ]


def supplier_naming() -> dict[int, matching.Naming]:
    aliases = defaultdict(set)
    for supplier_id, name in CounterpartyAlias.objects.values_list("supplier_id", "name"):
        aliases[supplier_id].add(name)
    return {
        supplier.pk: matching.Naming(
            matching.supplier_words(supplier.name, supplier.code), frozenset(aliases[supplier.pk])
        )
        for supplier in Supplier.objects.all()
    }


def payment_of(line: BankTransaction) -> matching.Payment:
    return matching.Payment(
        kind=line.kind,
        operation_date=line.operation_date,
        card_date=line.card_date,
        counterparty=line.counterparty,
        amount_due=-line.amount,
        label=line.label,
    )


def payee_of(line: BankTransaction) -> str:
    """The payee as the matching and the aliases see it: what the bank
    printed, or the label's words without their digits when it printed
    none (`matching.payee_of`)."""
    return matching.payee_of(line.counterparty, line.label)


def search_window(lines) -> tuple:
    dates = [line.paid_on for line in lines]
    return min(dates) - matching.LATER_PAYMENT_WINDOW, max(dates) + matching.CARD_DAYS_AFTER


def pass_order(line: BankTransaction) -> tuple:
    """The one order lines are taken in: card payments first - they look a
    few days around one date where a debit reaches months back, so a receipt
    goes to its own card payment before a wider search can take it - then by
    date, then by pk. The automatic pass links in this order, `accept_proposals`
    too, and « Propositions » ticks an invoice two lines want for the first
    of them only (`views._share_invoices`): the page promises what the POST
    will do, no more."""
    return (line.kind != BankTransaction.Kind.CARD, line.operation_date, line.pk)


@transaction.atomic
def reconcile() -> int:
    """Link every open line the matching is sure of; returns how many."""
    rules = active_rules()
    lines = [line for line in open_lines().filter(settled_by_hand=False) if ignoring_rule(line.label, rules) is None]
    if not lines:
        return 0
    start, end = search_window(lines)
    candidates = candidates_from(unpaid_invoices(start, end))
    naming = supplier_naming()
    lines.sort(key=pass_order)
    linked = 0
    for line in lines:
        found = matching.match(payment_of(line), candidates, naming)
        if found is None or not found.confident:
            continue
        (option,) = found.options
        InvoicePayment.objects.bulk_create(
            [
                InvoicePayment(transaction=line, invoice_id=candidate.pk, method=InvoicePayment.Method.AUTO)
                for candidate in option
            ]
        )
        taken = {candidate.pk for candidate in option}
        candidates = [candidate for candidate in candidates if candidate.pk not in taken]
        linked += 1
    return linked


@transaction.atomic
def link(line: BankTransaction, invoices) -> None:
    """A person says `line` paid `invoices` - whatever they cost, and whoever
    else pays them.

    Nothing is refused here: an invoice another line already pays is linked
    all the same (an invoice settled in two goes), and invoices that do not
    add up to what left the account are linked all the same (the page says
    the gap - `views.Gap` - rather than hiding it). A person looking at the
    statement and the document knows something no rule here does.
    """
    invoices = list(invoices)
    for invoice in invoices:
        InvoicePayment.objects.get_or_create(
            transaction=line, invoice=invoice, defaults={"method": InvoicePayment.Method.MANUAL}
        )
    line.no_invoice = False
    line.settled_by_hand = True
    line.save(update_fields=["no_invoice", "settled_by_hand"])
    _learn_payee(line, invoices)


def _learn_payee(line: BankTransaction, invoices) -> None:
    """The next payment to this payee links on its own - but only from a
    link the automatic pass itself could have made.

    An alias is permanent, nothing on any page shows it, and `reconcile()`
    acts on it without asking. Since a person may now link ANY document at
    ANY amount (« Chercher une facture » searches the whole table), the one
    corroboration left is the amount: invoices adding up to exactly what
    left the account say « the bank prints this payee for that supplier ».
    A link that does not add up is the case this page was opened up for - a
    part payment, a document found by number months later - and it is
    evidence about one debit, not about a name. Taught from it, a single
    mis-tick renames the payee for every statement to come, and next
    month's rent quietly settles a wholesaler's invoice.

    Measured on everything the line pays, not on the invoices just added:
    a debit settled by two documents linked one at a time is one link that
    adds up, and it should teach exactly as much as linking both at once.
    """
    # A line the bank printed no payee on is learnt by its label's words
    # (the bank's own fee, month after month under a new number):
    # `matching.payee_of` is what the matching names a supplier from, so it
    # is what the alias has to be keyed on.
    payee = payee_of(line)
    key = matching.alias_key(payee)
    if not key:
        return
    paid = line.payments.select_related("invoice").prefetch_related("invoice__lines")
    if sum((rounded_total(payment.invoice) for payment in paid), Decimal("0")) != line.amount_due:
        return
    naming = supplier_naming()
    # Asked exactly as the matching asks it: a label standing in for a blank
    # payee names a supplier through an alias only, so whatever a word of
    # the supplier's name would have named there is precisely what the alias
    # is learnt for - the label's words ARE the alias.
    alias_only = not line.counterparty
    for supplier_id in {invoice.supplier_id for invoice in invoices}:
        if not matching.names_supplier(payee, naming.get(supplier_id, matching.NO_NAMING), alias_only=alias_only):
            CounterpartyAlias.objects.get_or_create(supplier_id=supplier_id, name=key)


#: What became of one proposal a person ticked on « Propositions ».
ACCEPTED, MISSING, INCOME, NOT_OPEN, RULED_OUT, PAID_MEANWHILE, CHANGED = (
    "accepted",
    "missing",
    "income",
    "not_open",
    "ruled_out",
    "paid_meanwhile",
    "changed",
)


@dataclass
class Acceptance:
    pk: int
    status: str
    line: BankTransaction | None = None
    invoices: list = field(default_factory=list)


@transaction.atomic
def accept_proposals(chosen: dict[int, frozenset[int]]) -> list[Acceptance]:
    """A person ticked proposals on « Propositions »: `chosen` maps a line's
    pk to the pks of the invoices of the option they picked. Each one is
    linked through `link` - a person accepted it, so MANUAL, settled by
    hand, alias learnt when it adds up - or skipped with a reason.

    Nothing posted is trusted: the page was drawn some time ago, and another
    tab, an import or the proposal accepted just above may have changed
    what it showed. So a line is skipped, and said, when it does not exist
    any more (MISSING), takes money in (INCOME - no form is drawn on one;
    see `views.bank_line_action`), is not open any more (NOT_OPEN: paid or
    « pas de facture » meanwhile), is covered by an active ignore rule
    (RULED_OUT - the page never offers one), when one of its invoices is
    paid now (PAID_MEANWHILE - by another line, or by a proposal accepted
    a moment before in this same batch: the candidates a link takes are
    withdrawn from the ones below, as the automatic pass does), or when a
    fresh match of the line against what is unpaid no longer offers exactly
    that set of invoices (CHANGED). Lines are taken in the order the
    automatic pass takes them - card payments first, then by date - so a
    receipt goes to its own card payment before a debit's wider search can
    claim it.
    """
    rules = active_rules()
    lines = {line.pk: line for line in BankTransaction.objects.filter(pk__in=chosen).prefetch_related("payments")}
    outcomes: list[Acceptance] = []
    open_lines_chosen = []
    for pk in chosen:
        line = lines.get(pk)
        if line is None:
            outcomes.append(Acceptance(pk, MISSING))
        elif line.amount >= 0:
            outcomes.append(Acceptance(pk, INCOME, line))
        elif line.payments.all() or line.no_invoice:
            outcomes.append(Acceptance(pk, NOT_OPEN, line))
        elif ignoring_rule(line.label, rules) is not None:
            outcomes.append(Acceptance(pk, RULED_OUT, line))
        else:
            open_lines_chosen.append(line)
    if not open_lines_chosen:
        return outcomes

    start, end = search_window(open_lines_chosen)
    unpaid = {invoice.pk: invoice for invoice in unpaid_invoices(start, end)}
    candidates = candidates_from(unpaid.values())
    naming = supplier_naming()
    open_lines_chosen.sort(key=pass_order)
    for line in open_lines_chosen:
        wanted = chosen[line.pk]
        available = {candidate.pk for candidate in candidates}
        if not wanted or not wanted <= available:
            # Gone from the unpaid set since the page was drawn - or never in
            # it, which is a stale or crafted option. One query, on the
            # skip path only, tells the two apart.
            paid_now = Invoice.objects.filter(pk__in=wanted - available, payments__isnull=False).exists()
            outcomes.append(Acceptance(line.pk, PAID_MEANWHILE if paid_now else CHANGED, line))
            continue
        found = matching.match(payment_of(line), candidates, naming)
        offered = [frozenset(candidate.pk for candidate in option) for option in found.options] if found else []
        if wanted not in offered:
            outcomes.append(Acceptance(line.pk, CHANGED, line))
            continue
        invoices = [unpaid[pk] for pk in sorted(wanted)]
        link(line, invoices)
        candidates = [candidate for candidate in candidates if candidate.pk not in wanted]
        outcomes.append(Acceptance(line.pk, ACCEPTED, line, invoices))
    return outcomes


@transaction.atomic
def unlink(line: BankTransaction) -> None:
    line.payments.all().delete()
    line.settled_by_hand = True
    line.save(update_fields=["settled_by_hand"])


@transaction.atomic
def unlink_invoice(line: BankTransaction, invoice: Invoice) -> bool:
    """One invoice off `line`, the other links left alone; False when this
    line does not pay it (a stale page, a second click).

    The line stays `settled_by_hand` even when nothing is left on it, for
    the same reason `unlink` does: a person took the link off, and the
    automatic pass putting it straight back is the one thing they cannot
    argue with.
    """
    removed, _details = InvoicePayment.objects.filter(transaction=line, invoice=invoice).delete()
    line.settled_by_hand = True
    line.save(update_fields=["settled_by_hand"])
    return bool(removed)


@transaction.atomic
def mark_no_invoice(line: BankTransaction) -> None:
    line.payments.all().delete()
    line.no_invoice = True
    line.settled_by_hand = True
    line.save(update_fields=["no_invoice", "settled_by_hand"])


@transaction.atomic
def reopen(line: BankTransaction) -> None:
    """Hand the line back to the automatic pass."""
    line.payments.all().delete()
    line.no_invoice = False
    line.settled_by_hand = False
    line.save(update_fields=["no_invoice", "settled_by_hand"])


def choices_window(line: BankTransaction) -> tuple:
    """Dates of the invoices a person may pick for `line` by hand: the
    automatic window, a week wider on the late side."""
    return line.paid_on - matching.LATER_PAYMENT_WINDOW, line.paid_on + timedelta(days=7)
