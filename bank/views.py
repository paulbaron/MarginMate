"""The bank page: import statements, see which spending lines have their
invoice, settle the rest by hand - and the rules for payments that never
have one."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from common import is_id

from invoices.models import Invoice

from . import matching, reconcile
from .forms import IgnoreRuleForm
from .models import BankTransaction, IgnoreRule, InvoicePayment
from .rules import ignoring_rule

TODO, LINKED, NO_INVOICE, INCOME = "todo", "linked", "no_invoice", "income"
DEFAULT_VIEW = "a-traiter"
VIEWS = {
    "a-traiter": "Sans facture",
    "par-beneficiaire": "Sans facture, par bénéficiaire",
    "rapprochees": "Rapprochées",
    "sans-facture": "Pas de facture attendue",
    "toutes": "Toutes les dépenses",
    "entrees": "Entrées",
}
MONTH_NAMES = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
MAX_CHOICES = 15
# Sort key for an invoice with no date: after every dated one of equal amount.
UNDATED_LAST = 10**6
MAX_EXAMPLES = 15


@dataclass
class Option:
    invoices: list
    total: Decimal
    gap: Decimal


@dataclass
class Row:
    line: BankTransaction
    status: str
    rule: IgnoreRule | None = None
    payments: list = field(default_factory=list)
    suggestion: matching.Match | None = None
    options: list[Option] = field(default_factory=list)
    choices: list = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == TODO


@dataclass
class PayeeGroup:
    name: str
    count: int = 0
    total: Decimal = Decimal("0")
    last: date | None = None

    @property
    def pattern(self) -> str:
        """The payee as a pattern a person can still read and edit:
        re.escape turns "URSSAF D ILE" into "URSSAF\\ D\\ ILE", and neither a
        space nor a hyphen means anything outside a character class."""
        return re.escape(self.name).replace("\\ ", " ").replace("\\-", "-")


@dataclass
class RuleMatches:
    count: int
    total: Decimal
    linked: int
    examples: list


def classify(line: BankTransaction, rules) -> tuple[str, IgnoreRule | None]:
    """What a line is: money in, paid for, not expected to have an invoice
    (by hand, or by the first ignore rule that matches), or still missing one.
    Its invoice beats any rule: a rule never hides a payment that has one."""
    if line.amount >= 0:
        return INCOME, None
    if line.payments.all():
        return LINKED, None
    if line.no_invoice:
        return NO_INVOICE, None
    rule = ignoring_rule(line.label, rules)
    if rule is not None:
        return NO_INVOICE, rule
    return TODO, None


def bank_home(request):
    if request.method == "POST":
        return _import_statements(request)

    view = request.GET.get("vue", DEFAULT_VIEW)
    if view not in VIEWS:
        view = DEFAULT_VIEW
    months = _months()
    month = request.GET.get("mois", "")
    if month not in dict(months):
        month = ""

    lines = BankTransaction.objects.prefetch_related(
        Prefetch(
            "payments",
            queryset=InvoicePayment.objects.select_related("invoice__supplier").prefetch_related("invoice__lines"),
        )
    )
    if month:
        year, number = (int(part) for part in month.split("-"))
        lines = lines.filter(operation_date__year=year, operation_date__month=number)
    rules = reconcile.active_rules()
    rows = [Row(line, *classify(line, rules)) for line in lines]
    by_status = defaultdict(list)
    for row in rows:
        by_status[row.status].append(row)
    groups = _payee_groups(by_status[TODO])
    stats = _stats(by_status, len(groups))

    shown = {
        "a-traiter": by_status[TODO],
        "par-beneficiaire": [],
        "rapprochees": by_status[LINKED],
        "sans-facture": by_status[NO_INVOICE],
        "toutes": [row for row in rows if row.status != INCOME],
        "entrees": by_status[INCOME],
    }[view]
    _fill(shown)
    tabs = [
        {"key": key, "label": label, "count": stats["counts"][key], "active": key == view}
        for key, label in VIEWS.items()
    ]
    return render(
        request,
        "bank/bank_home.html",
        {
            "rows": shown,
            "groups": groups,
            "tabs": tabs,
            "view": view,
            "stats": stats,
            "months": months,
            "month": month,
            "has_lines": BankTransaction.objects.exists(),
        },
    )


def bank_reconcile(request):
    if request.method == "POST":
        linked = reconcile.reconcile()
        if linked:
            messages.success(request, f"{linked} paiement(s) rattaché(s) automatiquement à leur facture.")
        else:
            messages.info(
                request, "Aucun nouveau rapprochement certain. Les suggestions restent à confirmer ligne par ligne."
            )
    return redirect(_back(request))


def bank_line_action(request, pk):
    line = get_object_or_404(BankTransaction, pk=pk)
    back = _back(request)
    if request.method != "POST":
        return redirect(back)

    action = request.POST.get("action")
    if action == "link":
        ids = [value for value in request.POST.getlist("invoice") if is_id(value)]
        invoices = list(Invoice.objects.filter(pk__in=ids).select_related("supplier"))
        if not invoices:
            messages.error(request, "Choisissez la facture que ce paiement a réglée.")
            return redirect(back)
        try:
            reconcile.link(line, invoices)
        except reconcile.AlreadyPaidError as exc:
            messages.error(request, str(exc))
        else:
            labels = ", ".join(reconcile.invoice_label(invoice) for invoice in invoices)
            messages.success(request, f"Paiement rattaché à {labels}.")
    elif action == "unlink":
        reconcile.unlink(line)
        messages.success(request, "Rattachement retiré : cette ligne ne sera plus rapprochée automatiquement.")
    elif action == "no_invoice":
        reconcile.mark_no_invoice(line)
        messages.success(request, "Ligne marquée « pas de facture attendue ».")
    elif action == "reopen":
        reconcile.reopen(line)
        reconcile.reconcile()
        if line.payments.exists():
            messages.success(request, "Ligne rendue au rapprochement automatique, et rattachée à sa facture.")
        else:
            messages.success(request, "Ligne rendue au rapprochement automatique.")
    else:
        messages.error(request, "Action inconnue.")
    return redirect(back)


def rule_list(request):
    form = IgnoreRuleForm(
        request.POST or None,
        initial={"pattern": request.GET.get("motif", ""), "description": request.GET.get("nom", "")},
    )
    spending = list(BankTransaction.objects.filter(amount__lt=0).prefetch_related("payments"))
    test = None
    if request.method == "POST" and form.is_valid():
        regex = re.compile(form.cleaned_data["pattern"], re.IGNORECASE)
        if request.POST.get("action") == "test":
            test = _rule_matches(regex, spending)
        else:
            form.save()
            found = _rule_matches(regex, spending)
            messages.success(
                request,
                f"Règle ajoutée : {found.count} dépense(s), {found.total:.2f} €, "
                "ne comptent plus comme sans facture.",
            )
            return redirect("bank:rule_list")

    rules = []
    for rule in IgnoreRule.objects.all():
        try:
            rules.append((rule, _rule_matches(re.compile(rule.pattern, re.IGNORECASE), spending)))
        except re.error:
            rules.append((rule, None))
    return render(request, "bank/rules.html", {"form": form, "test": test, "rules": rules})


def rule_action(request, pk):
    rule = get_object_or_404(IgnoreRule, pk=pk)
    if request.method != "POST":
        return redirect("bank:rule_list")
    action = request.POST.get("action")
    if action == "delete":
        rule.delete()
        linked = reconcile.reconcile()
        messages.success(
            request,
            f"Règle « {rule} » supprimée : ses dépenses comptent de nouveau comme sans facture"
            + (f", {linked} rattachée(s) automatiquement." if linked else "."),
        )
    elif action == "toggle":
        rule.is_active = not rule.is_active
        rule.save(update_fields=["is_active"])
        linked = 0 if rule.is_active else reconcile.reconcile()
        state = "réactivée" if rule.is_active else "suspendue"
        messages.success(
            request, f"Règle « {rule} » {state}" + (f" ; {linked} paiement(s) rattaché(s)." if linked else ".")
        )
    else:
        messages.error(request, "Action inconnue.")
    return redirect("bank:rule_list")


def _import_statements(request):
    uploads = request.FILES.getlist("files")
    if not uploads:
        messages.error(request, "Choisissez au moins un relevé bancaire (fichier CSV).")
        return redirect("bank:bank_home")
    created = known = 0
    for upload in uploads:
        if not upload.name.lower().endswith(".csv"):
            messages.error(request, f"{upload.name} : seuls les relevés exportés en CSV sont acceptés.")
            continue
        try:
            summary = reconcile.import_statement(upload.read())
        except ValueError as exc:
            messages.error(request, f"{upload.name} : {exc}")
            continue
        created += summary.created
        known += summary.known
    if created or known:
        linked = reconcile.reconcile()
        messages.success(
            request,
            f"{created} opération(s) importée(s), {known} déjà connue(s) ; "
            f"{linked} paiement(s) rattaché(s) automatiquement à leur facture.",
        )
    return redirect("bank:bank_home")


def _back(request) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse("bank:bank_home")


def _months() -> list[tuple[str, str]]:
    """("2026-07", "Juillet 2026") for every month with an operation, newest first."""
    return [
        (f"{day:%Y-%m}", f"{MONTH_NAMES[day.month - 1].capitalize()} {day.year}")
        for day in BankTransaction.objects.dates("operation_date", "month", order="DESC")
    ]


def _stats(by_status, payee_count: int) -> dict:
    """Counts and sums, added in Python - SQLite's own sums are not exact
    decimal (see StockType.current_value_ht)."""

    def total(rows):
        return sum((row.line.amount_due for row in rows), Decimal("0"))

    todo, linked, no_invoice = by_status[TODO], by_status[LINKED], by_status[NO_INVOICE]
    spending = todo + linked + no_invoice
    return {
        "spending_count": len(spending),
        "spending_total": total(spending),
        "linked_count": len(linked),
        "linked_total": total(linked),
        "todo_count": len(todo),
        "todo_total": total(todo),
        "no_invoice_count": len(no_invoice),
        "no_invoice_total": total(no_invoice),
        "by_rule_count": sum(1 for row in no_invoice if row.rule is not None),
        "counts": {
            "a-traiter": len(todo),
            "par-beneficiaire": payee_count,
            "rapprochees": len(linked),
            "sans-facture": len(no_invoice),
            "toutes": len(spending),
            "entrees": len(by_status[INCOME]),
        },
    }


def _payee_groups(rows) -> list[PayeeGroup]:
    """The payments still missing an invoice, one group per payee, biggest first."""
    groups: dict[str, PayeeGroup] = {}
    for row in rows:
        line = row.line
        name = line.counterparty or line.bank_type or line.label[:40]
        group = groups.setdefault(name, PayeeGroup(name))
        group.count += 1
        group.total += line.amount_due
        if group.last is None or line.paid_on > group.last:
            group.last = line.paid_on
    return sorted(groups.values(), key=lambda group: (-group.total, group.name))


def _rule_matches(regex, spending) -> RuleMatches:
    found = [line for line in spending if regex.search(line.label)]
    return RuleMatches(
        count=len(found),
        total=sum((line.amount_due for line in found), Decimal("0")),
        linked=sum(1 for line in found if line.payments.all()),
        examples=found[:MAX_EXAMPLES],
    )


def _fill(rows) -> None:
    """Payments for every row; for the ones still missing an invoice, the
    matching's suggestion and the unpaid invoices a person may pick from -
    all from one load of invoices."""
    for row in rows:
        row.payments = list(row.line.payments.all())
    open_rows = [row for row in rows if row.is_open]
    if not open_rows:
        return

    start, _end = reconcile.search_window([row.line for row in open_rows])
    end = max(row.line.paid_on for row in open_rows) + timedelta(days=7)
    invoices = list(reconcile.unpaid_invoices(start, end))
    by_pk = {invoice.pk: invoice for invoice in invoices}
    totals = {invoice.pk: reconcile.rounded_total(invoice) for invoice in invoices}
    candidates = [
        matching.InvoiceCandidate(invoice.pk, invoice.supplier_id, invoice.invoice_date, totals[invoice.pk])
        for invoice in invoices
    ]
    naming = reconcile.supplier_naming()

    for row in open_rows:
        due = row.line.amount_due
        row.suggestion = matching.match(reconcile.payment_of(row.line), candidates, naming)
        if row.suggestion is not None:
            for option in row.suggestion.options:
                option_total = sum((candidate.total for candidate in option), Decimal("0"))
                row.options.append(
                    Option([by_pk[candidate.pk] for candidate in option], option_total, due - option_total)
                )
        first, last = reconcile.choices_window(row.line)
        near = [
            invoice for invoice in invoices if invoice.invoice_date is None or first <= invoice.invoice_date <= last
        ]
        near.sort(
            key=lambda invoice: (
                abs(totals[invoice.pk] - due),
                abs((invoice.invoice_date - row.line.paid_on).days) if invoice.invoice_date else UNDATED_LAST,
            )
        )
        row.choices = [(invoice, totals[invoice.pk]) for invoice in near[:MAX_CHOICES]]
