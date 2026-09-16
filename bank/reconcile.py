"""The database side of bank matching: importing statements, linking what
bank.matching is sure of, and the decisions a person takes on the bank page.

A line a person linked, unlinked or declared without invoice is never
revisited by the automatic pass (`BankTransaction.settled_by_hand`), and
neither is one an active ignore rule matches.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q

from invoices.models import Invoice, Supplier

from . import matching
from .models import BankTransaction, CounterpartyAlias, IgnoreRule, InvoicePayment
from .rules import compile_rules, ignoring_rule
from .statements import parse_statement

CENTS = Decimal("0.01")


class AlreadyPaidError(Exception):
    def __init__(self, invoices):
        self.invoices = invoices
        super().__init__(
            "Déjà rattachée à un autre paiement : " + ", ".join(invoice_label(invoice) for invoice in invoices) + "."
        )


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


@transaction.atomic
def import_statement(content: bytes) -> ImportSummary:
    statement = parse_statement(content)
    known = set(
        BankTransaction.objects.filter(fingerprint__in=[line.fingerprint for line in statement.lines]).values_list(
            "fingerprint", flat=True
        )
    )
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


def open_lines():
    """Spending lines still waiting for their invoice (ignore rules aside)."""
    return BankTransaction.objects.filter(amount__lt=0, no_invoice=False, payments__isnull=True)


def active_rules():
    return compile_rules(IgnoreRule.objects.filter(is_active=True))


def unpaid_invoices(start=None, end=None):
    """Unpaid invoices dated from `start` to `end` - and those with no date at
    all, which matching only ever suggests (OCR misses a receipt's date)."""
    dated = Q(invoice_date__isnull=False)
    if start is not None:
        dated &= Q(invoice_date__gte=start)
    if end is not None:
        dated &= Q(invoice_date__lte=end)
    invoices = Invoice.objects.filter(dated | Q(invoice_date__isnull=True), payment__isnull=True)
    return invoices.select_related("supplier").prefetch_related("lines")


def rounded_total(invoice: Invoice) -> Decimal:
    return invoice.total_ttc.quantize(CENTS, rounding=ROUND_HALF_UP)


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
    )


def search_window(lines) -> tuple:
    dates = [line.paid_on for line in lines]
    return min(dates) - matching.LATER_PAYMENT_WINDOW, max(dates) + matching.CARD_DAYS_AFTER


@transaction.atomic
def reconcile() -> int:
    """Link every open line the matching is sure of; returns how many."""
    rules = active_rules()
    lines = [
        line for line in open_lines().filter(settled_by_hand=False) if ignoring_rule(line.label, rules) is None
    ]
    if not lines:
        return 0
    start, end = search_window(lines)
    candidates = candidates_from(unpaid_invoices(start, end))
    naming = supplier_naming()
    # Card payments first: they look a few days around one date where a debit
    # reaches months back, so a receipt goes to its own card payment before
    # a wider search can take it.
    lines.sort(key=lambda line: (line.kind != BankTransaction.Kind.CARD, line.operation_date, line.pk))
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
    """A person says `line` paid `invoices`."""
    invoices = list(invoices)
    taken = [
        invoice
        for invoice in invoices
        if InvoicePayment.objects.filter(invoice=invoice).exclude(transaction=line).exists()
    ]
    if taken:
        raise AlreadyPaidError(taken)
    for invoice in invoices:
        InvoicePayment.objects.get_or_create(
            transaction=line, invoice=invoice, defaults={"method": InvoicePayment.Method.MANUAL}
        )
    line.no_invoice = False
    line.settled_by_hand = True
    line.save(update_fields=["no_invoice", "settled_by_hand"])
    _learn_payee(line, invoices)


def _learn_payee(line: BankTransaction, invoices) -> None:
    """The next payment to this payee links on its own."""
    key = matching.alias_key(line.counterparty)
    if not key:
        return
    naming = supplier_naming()
    for supplier_id in {invoice.supplier_id for invoice in invoices}:
        if not matching.names_supplier(line.counterparty, naming.get(supplier_id, matching.NO_NAMING)):
            CounterpartyAlias.objects.get_or_create(supplier_id=supplier_id, name=key)


@transaction.atomic
def unlink(line: BankTransaction) -> None:
    line.payments.all().delete()
    line.settled_by_hand = True
    line.save(update_fields=["settled_by_hand"])


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
