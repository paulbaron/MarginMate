"""The bank pages: import statements, see which spending lines have their
invoice, settle the rest by hand - the rules for payments that never have
one - and, on « Dépenses », where everything that left the account went;
on « Entrées d'argent », what came in, beside what the till was paid; on
« Trésorerie », the account's balance on every day.

« Dépenses » is deliberately a page of its own rather than a seventh filter
of the operations: those are about a line's invoice, and that page is about
the money, over a period, whether or not there is an invoice at all. The
four pages are Banque's tabs (bank/_tabs.html): « Opérations »,
« Dépenses par catégorie », « Entrées d'argent » and « Trésorerie ». See
bank/spending.py for what « Dépenses » counts and what it refuses to blend
with « Marges », bank/income.py for the money that came in, and
bank/treasury.py for the balances typed and what the operations between
them explain.
"""

from __future__ import annotations

import bisect
import calendar
import copy
import heapq
import math
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.contrib import messages
from django.contrib.messages import get_messages
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, prefetch_related_objects
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import date as date_filter
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import localize
from django.utils.html import escape
from django.utils.http import urlencode
from django.utils.text import capfirst
from django.views.decorators.http import require_GET

from accounts.views import file_response
from common import (
    LEFT_OUT_PARAM,
    SHOWN_PARAM,
    DateRange,
    date_range,
    file_too_big,
    format_money,
    is_id,
    last_twelve_months,
    left_out_from,
    read_date,
    safe_next,
    selection_too_big,
)
from inventory.templatetags.assets import money
from invoices.models import Invoice
from invoices.workspace import documents_matching
from recipes.integration import TILL_TO_CONFIGURE, till_allowed
from returnables.patterns import PatternError

from . import income, invoice_files, matching, recognition, reconcile, spending, statements, treasury
from .forms import (
    FORMAT_NAME_TAKEN,
    NAME_TAKEN,
    REASON_MAX_LENGTH,
    IgnoreRuleForm,
    OperationRuleForm,
    StatementFormatForm,
    TreasuryAdjustmentForm,
    TreasuryPointForm,
)
from .models import (
    BankTransaction,
    CounterpartyAlias,
    IgnoreRule,
    IncomePayer,
    IncomeSource,
    InvoicePayment,
    OperationRule,
    StatementFormat,
    TreasuryAdjustment,
    TreasuryCheckpoint,
)
from .rules import caught, ignoring_rule, reason_of, searcher

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
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)
MAX_CHOICES = 15
# Sort key for an invoice with no date: after every dated one of equal amount.
UNDATED_LAST = 10**6
#: « Depuis le début » on « Dépenses », spelt as « Marges » and the stock
#: page's panels spell it: everything, and keep the dates that were asked so
#: they can be offered back on the next click.
ALL_PARAM = "tout"
#: « Entrées d'argent »: the payout whose « En caisse » menu is drawn.
CHANGE_PARAM = "changer"
#: The ids of a credit's row there, which an « En caisse » choice answers on
#: - in whichever list the line now sits.
ENTRY_ANCHOR = "entree-{pk}"
#: Its list of payers retained, and the section that holds it.
PAYERS_ANCHOR = "payeurs-retenus"
BALANCE_ANCHOR = "card-balance"
MAX_EXAMPLES = 15
#: How many documents a search shows. Raised from 20 once the box stopped
#: reloading the page: a long list was then a long page to scroll back
#: through; it now swaps its results into the row alone, and the cost of
#: showing more is a scroll inside that row. Still a cap - the search is for
#: a document a person already has in mind, and several hundred rows inside
#: one line of a table is not a list anybody reads - and what it leaves out
#: is still counted and said.
MAX_FOUND = 50


@dataclass
class Option:
    invoices: list
    total: Decimal
    gap: Decimal
    #: The other open lines « Propositions » offers one of these invoices
    #: to as well - two debits of one supplier a month apart both list the
    #: same three months. Said on the option (`_share_invoices`): one
    #: invoice pays one line, and the reader ticking picks which.
    also_for: list = field(default_factory=list)

    @property
    def key(self) -> str:
        """The option as « Propositions » posts it back: its invoices' ids,
        space-separated, checked one by one with `is_id` and against a fresh
        match before anything is linked (`reconcile.accept_proposals`)."""
        return " ".join(str(invoice.pk) for invoice in self.invoices)


@dataclass
class Link:
    """One invoice on a line, and the other lines paying that same invoice -
    a document settled in two goes says so on both of them."""

    payment: InvoicePayment
    total: Decimal
    others: list = field(default_factory=list)
    #: The invoice's page (`bank_home`, `_by_pk`).
    url: str = ""

    @property
    def invoice(self) -> Invoice:
        return self.payment.invoice


@dataclass
class Gap:
    """What the invoices linked to a line add up to, beside what left the
    bank - shown whenever the two differ, and never made to agree.

    A person may link an invoice whatever it costs, so « rapprochée » no
    longer means « adds up ». This is what tells the two apart: the line
    stays where the person put it and the figures say the rest. Hidden, a
    debit settling half an invoice would read exactly like one settling it
    whole.
    """

    invoiced: Decimal
    due: Decimal
    shared: bool = False

    @property
    def difference(self) -> Decimal:
        return abs(self.invoiced - self.due)

    @property
    def over(self) -> bool:
        return self.invoiced > self.due

    def __bool__(self) -> bool:
        return self.invoiced != self.due


@dataclass
class Found:
    """A document the search found, and what already pays it."""

    invoice: Invoice
    total: Decimal
    here: bool = False
    elsewhere: list = field(default_factory=list)


@dataclass
class Row:
    line: BankTransaction
    status: str
    rule: IgnoreRule | None = None
    payments: list = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    gap: Gap | None = None
    suggestion: matching.Match | None = None
    options: list[Option] = field(default_factory=list)
    #: The pick-list's options: (value, label), the invoice's pk and what
    #: its option reads, as the template prints them (`_choice_label`).
    choices: list = field(default_factory=list)
    #: Unpaid invoices at these dates that `choices` had no room for, and
    #: documents the search matched beyond `MAX_FOUND`. Both are said on the
    #: page: a list cut in silence reads as « it is not there ».
    more_choices: int = 0
    search: str = ""
    found: list[Found] = field(default_factory=list)
    more_found: int = 0
    #: The line « Propositions » would link the pre-ticked option's invoice
    #: to BEFORE this one - earlier in the pass's order - when both are
    #: pre-ticked on it (`_share_invoices`). Set, this row is not ticked and
    #: says which operation takes the invoice first.
    rival: BankTransaction | None = None
    #: On the « Entrées » tab: what this credit is, as « Entrées d'argent »
    #: reads it (`income.entry_for`) - a card payout and its commission, a
    #: deposit, or the category a person typed.
    entry: income.Entry | None = None
    #: Where its forms post and its search box asks (`bank_home`, `_by_pk`).
    action_url: str = ""
    search_url: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == TODO

    @property
    def preticked(self) -> bool:
        """Ticked before the reader touches « Propositions »: a sure or a
        near-sure suggestion - unless a person took a link off this very
        line (`settled_by_hand` with nothing on it: they said no once, and a
        page that ticks yes for them is the one thing they cannot argue
        with), unless a payment not made by card reaches past the month for
        its invoice (`Match.far_back`: the pass would link it, the page
        shows it and says the distance, a person decides), and unless a line
        earlier in the pass's order is pre-ticked on the same invoice
        (`rival`: the POST could make one link of the two, and would make
        that one). Each exception is said on the row."""
        return (
            self.suggestion is not None
            and self.suggestion.tier in (matching.SURE, matching.NEAR_SURE)
            and not self.line.settled_by_hand
            and not self.suggestion.far_back
            and self.rival is None
        )


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
    """What a rule's pattern catches among the debits - or why it catches
    nothing here: its pattern refused (`problem`, `rules.check`'s reason),
    or too slow on them (`slow`). Neither has a count."""

    count: int | None = None
    total: Decimal | None = None
    linked: int = 0
    examples: list = field(default_factory=list)
    problem: str = ""
    slow: bool = False


def classify(line: BankTransaction, payments, rules) -> tuple[str, IgnoreRule | None]:
    """What a line is, `payments` being what it pays: money in, paid for, not
    expected to have an invoice (by hand, or by the first ignore rule that
    matches), or still missing one. Its invoice beats any rule: a rule never
    hides a payment that has one."""
    if line.amount >= 0:
        return INCOME, None
    if payments:
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
    month, window = _period(request, months)

    # Every line's payments, for its status and the zip's count: a list on
    # each line (`paid_by`) rather than a queryset made for each one, which
    # cost a tenth of the page, and their invoices without their texts.
    # What a link shows besides - the supplier, the invoice's lines, the
    # other lines paying it - `_fill` loads for the rows on screen alone.
    payments = InvoicePayment.objects.select_related("invoice").defer(
        *(f"invoice__{name}" for name in reconcile.UNREAD_INVOICE_FIELDS)
    )
    lines = BankTransaction.objects.prefetch_related(Prefetch("payments", queryset=payments, to_attr="paid_by"))
    # Everything below - the stats, the tab counts, the payee groups - is
    # built from these rows, so it all follows the window by construction.
    lines = _in_period(lines, month, window)
    rules = reconcile.active_rules()
    rows = [Row(line, *classify(line, line.paid_by, rules), payments=line.paid_by) for line in lines]
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
    _search(shown, request)
    # Each row's addresses, reversed once for the page rather than by a
    # `{% url %}` per form - several a row, a fifth of a long tab.
    action, search, detail = (
        _by_pk("bank:bank_line_action"),
        _by_pk("bank:invoice_search"),
        _by_pk("invoices:invoice_detail"),
    )
    for row in shown:
        row.action_url, row.search_url = action(row.line.pk), search(row.line.pk)
        for link in row.links:
            link.url = detail(link.invoice.pk)
    # What each credit is, rather than « Entrée d'argent » for all of them:
    # the same reading « Entrées d'argent » makes - the payers retained and
    # the till rules included, each read once - so the tab and that page
    # cannot call one line two things. No query per row, and none at all
    # where no credit is shown.
    rule_problems = []
    if any(row.status == INCOME for row in shown):
        payers = income.known_payers()
        till_rules = recognition.load()
        for row in shown:
            if row.status == INCOME:
                row.entry = income.entry_for(row.line, payers, till_rules)
        # Said as « Entrées d'argent » says them, and read after the last
        # credit: a rule is found too slow only once it has read lines.
        rule_problems = till_rules.problems
    # « Télécharger les factures »: what the zip will hold, counted off the
    # rows already read (their payments are prefetched) - no query.
    paid = {payment.invoice.pk: payment.invoice for row in rows if row.line.amount < 0 for payment in row.payments}
    tabs = [
        {
            "key": key,
            "label": label,
            "count": stats["counts"][key],
            "active": key == view,
            "url": _page_url(key, month, window),
        }
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
            # A till rule that recognised nothing while the credits were read.
            "rule_problems": rule_problems,
            # An ignore rule that hid nothing - its pattern refused, or too
            # slow on a label - read after every line was classified.
            "ignore_problems": rules.problems,
            "months": months,
            "month": month,
            "date_window": window,
            # Where every form on this page comes back to. Built from what
            # the page actually read rather than from request.get_full_path,
            # so a round trip cannot carry a parameter the page is ignoring
            # (dates left in the URL under a chosen month) back into view.
            "page_url": _page_url(view, month, window),
            # The same parameters as hidden fields, for the searches: a GET
            # form posts what it carries and nothing else, so a search from
            # a row under a window would otherwise answer on the whole
            # statement - the figures changing with nothing to say why.
            "page_fields": _page_parameters(view, month, window),
            "clear_window_url": _page_url(view, "", DateRange()),
            # « Entrées d'argent » and « Dépenses » over the period this page
            # is showing - a chosen month as its first and last day, since
            # those pages read dates and not months.
            "income_url": _bank_income_url(month, window),
            "spending_url": _spending_url(*_bank_period(month, window)),
            # « Trésorerie » over the same period: its curve and its months.
            "treasury_url": _treasury_page_url(*_bank_period(month, window)),
            # Banque's tabs (bank/_tabs.html): this one is the operations.
            "bank_url": _page_url(view, month, window),
            "bank_tab": "operations",
            "invoice_zip_url": _invoice_zip_url(view, month, window),
            "invoice_zip_count": sum(1 for invoice in paid.values() if invoice.source_file),
            "invoice_zip_missing": sum(1 for invoice in paid.values() if not invoice.source_file),
            # What every row links to alike, reversed once.
            "home_path": reverse("bank:bank_home"),
            "proposals_path": reverse("bank:proposals"),
            "rules_path": reverse("bank:rule_list"),
            # Deliberately the whole statement, window or not: windowed, a
            # period with nothing in it would show « Aucun relevé importé »
            # and read as an empty database rather than as empty dates.
            "has_lines": BankTransaction.objects.exists(),
            # The import card's formats, in their order: a choice when there
            # are several, the first being the import's default.
            "formats": list(StatementFormat.objects.order_by("position", "name").only("pk", "name")),
        },
    )


@require_GET
def invoice_zip(request):
    """Every document the spending of Banque's period paid, in one zip,
    each file under its download name (bank/invoice_files.py). A GET only:
    a HEAD would build the whole zip for nothing."""
    view = request.GET.get("vue", DEFAULT_VIEW)
    month, window = _period(request, _months())
    invoices = invoice_files.paid_by(_in_period(BankTransaction.objects.all(), month, window))
    if not any(invoice.source_file for invoice in invoices):
        messages.info(request, "Aucune facture à télécharger sur cette période.")
        return redirect(_page_url(view if view in VIEWS else DEFAULT_VIEW, month, window))
    handle = tempfile.TemporaryFile()  # noqa: SIM115 - the FileResponse closes it
    try:
        invoice_files.write_zip(invoices, handle)
    except BaseException:
        handle.close()
        raise
    handle.seek(0)
    response = file_response(handle, invoice_files.zip_name(_period_label(month, window)), download=True)
    # Windows' registry says « application/x-zip-compressed ».
    response["Content-Type"] = "application/zip"
    return response


def spending_home(request):
    """« Dépenses »: what left the account over a period, by category."""
    asked = date_range(request)
    showing_all = request.GET.get(ALL_PARAM) == "1"
    if SHOWN_PARAM in request.GET:
        # « Recalculer le camembert »: answered with the clean address -
        # `sans` alone, each name as the page reads it - rather than a page
        # drawn under a URL holding every category of the table twice. The
        # form's rows are compared with the address's names once cleaned,
        # so one name spelt two ways cannot be two rows.
        kind = request.GET.get(spending.KIND_PARAM, "")
        left_out = spending.left_out_names(left_out_from(request.GET, key=spending.clean_category))
        return redirect(_spending_url(asked, showing_all, kind if kind in spending.KINDS else "", left_out))

    # The named period takes the window whole rather than being crossed with
    # the dates, which stay in the URL only to be offered back - the same
    # rule « Marges » and « Banque »'s month follow.
    window = DateRange() if showing_all else (asked or last_twelve_months())
    # Which way the debits at the foot were named. Read back off the report
    # rather than off the request: `spending_for` is what decides a value
    # nobody can use is every kind, and the page has to draw the chip that
    # decision lit, not the word somebody typed in the URL. The same for
    # what the pie leaves out: every link carries the names the report
    # UNDERSTOOD, so a garbled one does not travel from link to link.
    report = spending.spending_for(
        window,
        kind=request.GET.get(spending.KIND_PARAM, ""),
        left_out=request.GET.getlist(LEFT_OUT_PARAM),
    )
    left_out = report.left_out
    here = _spending_url(asked, showing_all, report.kind, left_out)
    left_out_rows = _left_out_rows(report, asked, showing_all)
    action = _by_pk("bank:bank_line_action")
    for one in report.listed:
        one.action_url = action(one.line.pk)
    return render(
        request,
        "bank/spending.html",
        {
            "report": report,
            # What was typed, for the inputs; `window` is what the figures
            # are over. The two differ on the default period and under
            # « depuis le début », and the page says which it is showing.
            "date_window": asked,
            "window": window,
            "is_default": not showing_all and not asked,
            "showing_all": showing_all,
            "all_url": _spending_url(asked, True, report.kind, left_out),
            "period_url": _spending_url(asked, False, report.kind, left_out),
            # « Effacer » clears the dates, not the kind nor the selection.
            "clear_url": _spending_url(DateRange(), False, report.kind, left_out),
            # « À classer », « Classées à la main », « Classées par règle »,
            # each with what there IS of it over the window and the address
            # that shows it - the period travelling with every one of them.
            "kind": report.kind,
            "kind_chips": _kind_chips(report, asked, showing_all),
            # What an empty one says, in its own words - the template never
            # spells a kind's stored value out (spending.EMPTY).
            "kind_empty": spending.EMPTY.get(report.kind, ""),
            # Where every category form comes back to, so typing one keeps
            # the period, the kind and the pie the reader is looking at.
            "page_url": here,
            "page_path": reverse("bank:spending_home"),
            # The window form's own inputs are the dates; everything else
            # the page is looking through rides beside them as hidden
            # fields - a GET form sends what it holds and nothing else.
            "window_fields": _spending_fields(DateRange(), False, report.kind, left_out),
            # « Recalculer le camembert » carries the period and the kind,
            # and every name already left out: the ones with no row on this
            # period keep their state (`common.left_out_from`), and the
            # others keep the order they were asked in.
            "selection_fields": _spending_fields(asked, showing_all, report.kind, left_out),
            "left_out": left_out,
            "left_out_rows": left_out_rows,
            # Whether one of them has a row in the table - what the sentence
            # reconciling the pie with the total then has to name.
            "left_out_in_table": any(row.category is not None for row in left_out_rows),
            # « Tout remettre »: the same period and kind, nothing left out.
            "reset_url": _spending_url(asked, showing_all, report.kind),
            "pie_svg": _build_spending_pie_svg(report),
            "known_categories": spending.known_categories(),
            "no_category": spending.NO_CATEGORY,
            # The other pages carry the window alone: none of them reads
            # `sans` nor `classement`. Banque over the window APPLIED - the
            # twelve months by default, the dates typed, or under « tout »
            # an empty window, which there is every month: Banque has no
            # default period of its own to fall back on.
            "bank_url": _page_url(DEFAULT_VIEW, "", window),
            "margins_url": _other_page_url("margins:margins_home", window),
            # Money that came IN, and what the till says it sold - the page
            # this one is not. Empty where it does not exist (yet).
            "income_url": _income_url(window),
            "treasury_url": _other_page_url("bank:treasury", window),
            "spending_url": here,
            "bank_tab": "spending",
            "has_lines": BankTransaction.objects.exists(),
        },
    )


def income_home(request):
    """« Entrées d'argent »: what came in on the account over a period, beside
    what the till was paid over the same days (bank/income.py)."""
    asked = date_range(request)
    showing_all = request.GET.get(ALL_PARAM) == "1"
    # The « Dépenses » page's rules exactly: « tout » takes the window whole,
    # the dates stay in the URL only to be offered back, and no dates at all
    # is the last twelve months, said on screen.
    window = DateRange() if showing_all else (asked or last_twelve_months())
    report = income.income_for(window)
    for month in report.months:
        month.label = _month_label(month.first_day)
    here = _income_page_url(asked, showing_all)
    # « Changer » on a payout: its « En caisse » menu is drawn on that row
    # only. A payout a day is a form a day otherwise, on a list nobody
    # changes but by exception.
    changing = request.GET.get(CHANGE_PARAM, "")
    return render(
        request,
        "bank/income.html",
        {
            "report": report,
            # What was typed, for the inputs; `window` is what the figures
            # are over.
            "date_window": asked,
            "window": window,
            "is_default": not showing_all and not asked,
            "showing_all": showing_all,
            "all_url": _income_page_url(asked, True),
            "period_url": _income_page_url(asked, False),
            "clear_url": _income_page_url(DateRange(), False),
            # Where the category forms come back to - the period kept, and
            # the list they were typed from.
            "page_url": here,
            # « Comparer sur les jours couverts des deux côtés », where one
            # side holds days the other cannot see (`_covered_url`).
            "covered_url": _covered_url(report, window),
            # What the till could not read is said everywhere; the commands
            # that fill it only where they can be used (recipes/integration.py).
            "balance_reason": _balance_reason(report.balance.reason),
            "till_allowed": till_allowed(),
            "till_to_configure": TILL_TO_CONFIGURE,
            "chart_svg": _build_balance_svg(report.balance_points),
            "known_categories": income.known_categories(),
            "no_category": spending.NO_CATEGORY,
            "sources": income.SOURCES,
            # What the page recognises, said on it: the active till rules in
            # their order, each with what it means - one query.
            "till_rules": [(rule, recognition.meaning_label(rule.meaning)) for rule in report.till_rules],
            # « En caisse », on every credit: the values the view accepts,
            # « Automatique » first.
            "source_choices": IncomeSource.choices,
            "changing": int(changing) if is_id(changing) else None,
            "change_url": f"{here}{'&' if '?' in here else '?'}{CHANGE_PARAM}=",
            # The other pages over the same period - the window alone.
            "bank_url": _page_url(DEFAULT_VIEW, "", window),
            "spending_url": _other_page_url("bank:spending_home", window),
            "margins_url": _other_page_url("margins:margins_home", window),
            "treasury_url": _other_page_url("bank:treasury", window),
            # Banque's tabs (bank/_tabs.html): this one is the money in.
            "income_url": here,
            "bank_tab": "income",
            "has_lines": BankTransaction.objects.exists(),
        },
    )


def income_source(request, pk):
    """« En caisse » on one credit of « Entrées d'argent »: what it is in the
    till, for this line or - « retenir pour ce payeur » - for every credit of
    its payer (bank/income.py, `set_source`). Answers on the line's row, in
    whichever list it now sits; a GET writes nothing."""
    line = get_object_or_404(BankTransaction, pk=pk)
    back = _back(request, "bank:income_home").split("#")[0]
    if request.method != "POST":
        return redirect(back)
    try:
        change = income.set_source(line, request.POST.get("en_caisse"), remember=request.POST.get("retenir") == "1")
    except income.SourceRefused as refusal:
        messages.error(request, str(refusal))
        return redirect(back)
    messages.success(request, _source_said(change))
    return redirect(f"{back}#{ENTRY_ANCHOR.format(pk=line.pk)}")


def income_payer_forget(request, pk):
    """« Oublier » a payer retained: its credits go back to the rules - those
    chosen one by one keep their choice. A GET writes nothing; a second click
    is said, never a 404."""
    back = _back(request, "bank:income_home").split("#")[0]
    if request.method != "POST":
        return redirect(back)
    payer = IncomePayer.objects.filter(pk=pk).first()
    if payer is None:
        messages.error(request, "Ce payeur n'est plus retenu.")
    else:
        back_to_rules = income.forget_payer(payer)
        messages.success(
            request,
            f"« {payer.key} » oublié : {back_to_rules} entrée(s) reviennent à la reconnaissance automatique.",
        )
    # The list is drawn only while it holds a payer: the last one forgotten,
    # the section it sat in.
    return redirect(f"{back}#{PAYERS_ANCHOR if IncomePayer.objects.exists() else BALANCE_ANCHOR}")


def _source_said(change: income.SourceChange) -> str:
    """The message after an « En caisse » choice: what the line counts as
    now, and what happened to its payer."""
    entry = change.entry
    counted = f"« {IncomeSource(entry.source).label} »"
    if change.remembered:
        said = f"« {change.payer} » retenu : ses entrées non reconnues comptent comme {counted}"
        said += f", cette entrée et {change.followers} autre(s)." if change.followers else ", à commencer par celle-ci."
    elif change.forgotten:
        said = (
            f"« {change.payer} » oublié : cette entrée et {change.followers} autre(s) reviennent à la "
            f"reconnaissance automatique. Celle-ci compte comme {counted}."
        )
    elif entry.how == income.BY_LINE:
        said = f"Entrée comptée comme {counted}, elle seule."
    elif entry.how == income.BY_PAYER:
        said = f"Entrée rendue à son payeur retenu « {entry.payer} » : elle compte comme {counted}."
    else:
        said = f"Entrée rendue à la reconnaissance automatique : elle compte comme {counted}."
    if change.kept:
        said += f" {change.kept} entrée(s) de ce payeur gardent le choix fait pour elles seules."
    return said


def bank_reconcile(request):
    if request.method == "POST":
        linked = reconcile.reconcile()
        if linked:
            messages.success(request, f"{linked} paiement(s) rattaché(s) automatiquement à leur facture.")
        else:
            messages.info(request, "Aucun nouveau rapprochement certain : les suggestions restent à confirmer.")
    return redirect(_back(request))


#: A stale « Pas de facture » or « Rapprocher automatiquement », refused.
LINKED_MEANWHILE = (
    "Cette opération a été rattachée entre-temps à une facture : rien n'a changé. Déliez-la d'abord si besoin."
)


@transaction.atomic
def _unless_linked(line: BankTransaction, change) -> bool:
    """`change(line)` unless the line pays an invoice now; False then.

    Neither button is drawn on a row that pays one, so a link means the page
    was drawn before another tab, an import's automatic pass or
    « Propositions » made it - and both changes start by deleting every
    link: the stale click took it off in silence, and « pas de facture »
    kept the pass from ever putting it back. Checked in the change's own
    (IMMEDIATE) transaction, so no link is committed between the two.
    """
    if line.payments.exists():
        return False
    change(line)
    return True


def bank_line_action(request, pk):
    line = get_object_or_404(BankTransaction, pk=pk)
    back = _back(request)
    if request.method != "POST":
        return redirect(back)

    action = request.POST.get("action")
    if action == "link":
        if line.amount >= 0:
            # Only a debit settles an invoice. No form is drawn on an income
            # row, so this is a stale or a crafted POST - but linked, the
            # invoice would leave `unpaid_invoices` for ever and read
            # « Payée » on its own page while showing on no bank tab and in
            # no figure of « Dépenses », which reads debits only.
            messages.error(request, "Une entrée d'argent ne règle pas une facture.")
            return redirect(back)
        ids = [value for value in request.POST.getlist("invoice") if is_id(value)]
        invoices = list(Invoice.objects.filter(pk__in=ids).select_related("supplier"))
        if not invoices:
            messages.error(request, "Choisissez la facture que ce paiement a réglée.")
            return redirect(back)
        # Nothing refused: an invoice another line pays, or one that costs
        # something else than what left the account, is the person's to
        # link. The row then says what it adds up to (views.Gap).
        reconcile.link(line, invoices)
        labels = ", ".join(reconcile.invoice_label(invoice) for invoice in invoices)
        messages.success(request, f"Paiement rattaché à {labels}.")
    elif action == "unlink_invoice":
        value = request.POST.get("invoice", "")
        invoice = Invoice.objects.filter(pk=value).select_related("supplier").first() if is_id(value) else None
        if invoice is None:
            messages.error(request, "Choisissez la facture à détacher de cette opération.")
        elif not reconcile.unlink_invoice(line, invoice):
            # A page left open, a second click: what it asks to undo is
            # already undone, and saying so beats a success about nothing.
            messages.error(request, f"Cette opération ne paie pas {reconcile.invoice_label(invoice)}.")
        else:
            left = "" if line.payments.exists() else " Cette ligne ne sera plus rapprochée automatiquement."
            messages.success(request, f"{reconcile.invoice_label(invoice)} détachée de cette opération.{left}")
    elif action == "unlink":
        reconcile.unlink(line)
        messages.success(request, "Rattachement retiré : cette ligne ne sera plus rapprochée automatiquement.")
    elif action == "no_invoice":
        if _unless_linked(line, reconcile.mark_no_invoice):
            messages.success(request, "Ligne marquée « pas de facture attendue ».")
        else:
            messages.error(request, LINKED_MEANWHILE)
    elif action == "category":
        # What the money was for, which says NOTHING about whether the
        # invoice is still to be found: a category must not set
        # `settled_by_hand`, or naming a spending would take its line out of
        # the automatic pass for ever.
        name = spending.set_category(line, request.POST.get("categorie", ""))
        if line.amount >= 0:
            # A credit, named from « Entrées d'argent ». The same field and
            # the same cleaning as a spending's, and none of « Dépenses »'s
            # lists to move out of: those hold debits only.
            if name:
                messages.success(request, f"Entrée classée en « {name} ».")
            else:
                messages.success(request, f"Catégorie retirée : cette entrée compte comme « {spending.NO_CATEGORY} ».")
            return redirect(back)
        moved = _moved_out_of_view(request, line)
        if name:
            messages.success(request, f"Dépense classée en « {name} ».{moved}")
        else:
            messages.success(
                request, f"Catégorie retirée : cette dépense compte comme « {spending.NO_CATEGORY} ».{moved}"
            )
    elif action == "reopen":
        if not _unless_linked(line, reconcile.reopen):
            messages.error(request, LINKED_MEANWHILE)
            return redirect(back)
        reconcile.reconcile()
        if line.payments.exists():
            messages.success(request, "Ligne rendue au rapprochement automatique, et rattachée à sa facture.")
        else:
            messages.success(request, "Ligne rendue au rapprochement automatique.")
    else:
        messages.error(request, "Action inconnue.")
    return redirect(back)


def proposals(request):
    """« Propositions » : every open spending line the matching has a
    suggestion for, on one screen, by tier - the near-sure ones first and
    ticked, the rest to read one by one - to accept in one POST
    (`link_proposals`).

    The whole statement rather than the bank page's month or window: the
    screen is for the moment after an import (statement, then tickets, then
    « Relancer le rapprochement »), when what is left is what the automatic
    pass would not decide, wherever on the statement it sits. Lines an
    active ignore rule covers are not proposals, and a line with no
    suggestion has nothing to accept here: the bank page's pick-list and
    search are for those, and the page says how many there are.

    A SURE suggestion reaches this page too, in a group of its own, ticked:
    the pass has not been run since the invoice arrived (« Relancer le
    rapprochement » not clicked), or a person took the link off this very
    line and the pass never revisits it - unticked then, with its note.
    Left among « À confirmer », it read as a doubt about a line the rules
    card said never appears here. Unticked too, and said, when it reaches
    past the month for its invoice (`Match.far_back`), and when a line
    earlier in the pass's order is pre-ticked on the same invoice
    (`_share_invoices`).
    """
    rules = reconcile.active_rules()
    # An open line pays nothing (`open_lines`), so its row's payments stay
    # the empty list without a query.
    rows = [Row(line, TODO) for line in reconcile.open_lines() if ignoring_rule(line.label, rules) is None]
    _fill(rows, with_choices=False)
    proposed = [row for row in rows if row.suggestion is not None]
    _share_invoices(proposed)
    by_tier = defaultdict(list)
    for row in proposed:
        by_tier[row.suggestion.tier].append(row)
    return render(
        request,
        "bank/proposals.html",
        {
            "sure": by_tier[matching.SURE],
            "near_sure": by_tier[matching.NEAR_SURE],
            "to_confirm": by_tier[matching.TO_CONFIRM],
            "proposed_count": len(proposed),
            "preticked_count": sum(1 for row in proposed if row.preticked),
            # Open lines the matching has nothing for: said, so an empty
            # screen reads « nothing to propose », not « nothing to do ».
            "without_count": len(rows) - len(proposed),
            "tier_rules": matching.TIER_RULES,
            "notes": GROUP_NOTES,
            "has_lines": BankTransaction.objects.exists(),
            "bank_url": reverse("bank:bank_home"),
        },
    )


def _share_invoices(rows) -> None:
    """One invoice proposed to several lines - once everything is unpaid at
    once, a hand-linked line and an auto-linked line of one supplier compete
    for the same months - is said on every option that carries it, and
    pre-ticked for ONE line only: the first in the pass's order, which is the
    one `reconcile.accept_proposals` links (the later ones it skips and says
    « réglée entre-temps »). Pre-ticked on both, the page promised two links
    it could make only one of, and a reader ticking the later line alone -
    or unticking the earlier - linked the wrong one. A line that loses its
    tick takes nothing, as a skipped line takes nothing in the POST.
    """
    wanting = defaultdict(list)
    for row in rows:
        seen = set()
        for option in row.options:
            for invoice in option.invoices:
                if invoice.pk not in seen:
                    seen.add(invoice.pk)
                    wanting[invoice.pk].append(row)
    for row in rows:
        for option in row.options:
            others = {
                other.line.pk: other.line
                for invoice in option.invoices
                for other in wanting[invoice.pk]
                if other is not row
            }
            option.also_for = sorted(others.values(), key=reconcile.pass_order)
    taken: dict[int, Row] = {}
    for row in sorted((row for row in rows if row.preticked), key=lambda row: reconcile.pass_order(row.line)):
        chosen = row.options[0]
        holder = next((taken[invoice.pk] for invoice in chosen.invoices if invoice.pk in taken), None)
        if holder is not None:
            row.rival = holder.line
            continue
        for invoice in chosen.invoices:
            taken[invoice.pk] = row


#: What each group's heading on « Propositions » says beside its count: the
#: tier's pre-tick rule, and every exception `Row.preticked` makes to it -
#: named, with the figure it turns on, rather than « the row says why »: a
#: heading reading « cochées d'avance » over an unticked row owes the reader
#: the rule, not a hunt. Worded from the matching's own constants, so the
#: figure can not drift from the threshold (`matching.TIER_RULES` likewise).
GROUP_NOTES = {
    matching.SURE: (
        "Le rapprochement n'a pas été relancé depuis le dernier import : cochées d'avance, sauf celles déliées à "
        f"la main, celles dont la facture est datée au-delà de {matching.RECURRING_DAYS_BEFORE.days} jours avant "
        f"{matching.NOT_BY_CARD}, et celles dont la facture est aussi proposée à une opération servie avant elles."
    ),
    matching.NEAR_SURE: (
        "Cochées d'avance, sauf celles déliées à la main et celles dont la facture est aussi proposée à une "
        "opération servie avant elles."
    ),
    matching.TO_CONFIRM: "Rien de coché d'avance : cochez celles que vous reconnaissez.",
}

#: What « Propositions » says about a proposal it did not link, by
#: `reconcile.accept_proposals`'s verdict.
SKIPPED = {
    reconcile.MISSING: "l'opération n'existe plus",
    reconcile.INCOME: "une entrée d'argent ne règle pas une facture",
    reconcile.NOT_OPEN: "déjà rattachée, ou marquée « pas de facture », entre-temps",
    reconcile.RULED_OUT: "une règle « pas de facture attendue » la couvre",
    reconcile.PAID_MEANWHILE: "la facture proposée a été réglée entre-temps",
    reconcile.CHANGED: "la proposition a changé depuis l'affichage de la page : revoyez-la",
}


def link_proposals(request):
    """The ticked proposals, linked one by one - or skipped, each with its
    reason said, never quietly.

    Every id is read by hand and checked with `is_id` before it reaches a
    query: a line's, and each invoice's in the option's key (« 12 15 » for
    a sum of two). Which invoices a line is linked to is then decided by
    `reconcile.accept_proposals` against a fresh match, never by the ids
    alone: the page may be an hour old, and an accepted proposal higher up
    may just have taken the invoice this one wanted.
    """
    back = _back(request, default="bank:proposals")
    if request.method != "POST":
        return redirect(back)
    ticked = request.POST.getlist("ligne")
    if not ticked:
        messages.error(request, "Cochez au moins une proposition à rattacher.")
        return redirect(back)
    chosen: dict[int, frozenset[int]] = {}
    unreadable = 0
    no_option: list[str] = []
    for value in ticked:
        if not is_id(value):
            unreadable += 1
            continue
        parts = request.POST.get(f"option-{value}", "").split()
        if not parts:
            no_option.append(value)
            continue
        if not all(is_id(part) for part in parts):
            unreadable += 1
            continue
        chosen[int(value)] = frozenset(int(part) for part in parts)

    outcomes = reconcile.accept_proposals(chosen) if chosen else []
    accepted = [outcome for outcome in outcomes if outcome.status == reconcile.ACCEPTED]
    if accepted:
        messages.success(request, f"{len(accepted)} proposition{_s(accepted)} rattachée{_s(accepted)} à leur facture.")
    skipped = defaultdict(list)
    for outcome in outcomes:
        if outcome.status != reconcile.ACCEPTED:
            skipped[outcome.status].append(outcome)
    for status, group in skipped.items():
        listed = ", ".join(_line_words(outcome) for outcome in group)
        messages.warning(
            request, f"{len(group)} ligne{_s(group)} non rattachée{_s(group)} — {SKIPPED[status]} : {listed}."
        )
    if no_option:
        messages.warning(
            request,
            f"{len(no_option)} ligne{_s(no_option)} cochée{_s(no_option)} sans facture choisie : "
            "choisissez laquelle, puis recommencez.",
        )
    if unreadable:
        messages.warning(
            request,
            f"{unreadable} proposition{'s' if unreadable > 1 else ''} illisible{'s' if unreadable > 1 else ''} ignorée{'s' if unreadable > 1 else ''}.",
        )
    return redirect(back)


def _s(items) -> str:
    return "s" if len(items) > 1 else ""


def _line_words(outcome: reconcile.Acceptance) -> str:
    """One skipped line, as the reader will find it on the page."""
    line = outcome.line
    if line is None:
        return f"opération n° {outcome.pk}"
    who = line.counterparty or line.bank_type or line.label[:40]
    return f"{line.paid_on:%d/%m/%Y} {who} {format_money(line.amount_due)} €"


#: Said on the motif when « Tester » or « Ajouter la règle » stops a pattern
#: on the imported lines: kept, it would be set aside on every page drawn.
RULE_TOO_SLOW = "Motif : le motif est trop lent sur les opérations importées : simplifiez-le."


def rule_list(request):
    form = IgnoreRuleForm(
        request.POST or None,
        initial={"pattern": request.GET.get("motif", ""), "description": request.GET.get("nom", "")},
    )
    debits = list(BankTransaction.objects.filter(amount__lt=0))
    # Which of them pay an invoice: one query, where a prefetch made a
    # queryset per debit for a yes or a no.
    paid = set(InvoicePayment.objects.values_list("transaction_id", flat=True))
    test = None
    if request.method == "POST" and form.is_valid():
        # Run over every debit before anything is saved: a pattern the
        # guard lets through can still stall on some label.
        found = _rule_matches(form.searcher, debits, paid)
        if found.slow:
            form.add_error("pattern", RULE_TOO_SLOW)
        elif request.POST.get("action") == "test":
            test = found
        else:
            form.save()
            messages.success(
                request,
                f"Règle ajoutée : {found.count} dépense(s), {format_money(found.total)} €, "
                "ne comptent plus comme sans facture.",
            )
            return redirect("bank:rule_list")

    # Each stored rule as Banque applies it: one whose pattern the guard
    # refuses (saved before it, or imported) or that stalls on the debits
    # catches nothing there, and its row says so.
    rules = []
    for rule in IgnoreRule.objects.all():
        try:
            regex = searcher(rule.pattern)
        except PatternError as error:
            rules.append((rule, RuleMatches(problem=reason_of(error.message))))
            continue
        rules.append((rule, _rule_matches(regex, debits, paid)))
    return render(
        request,
        "bank/rules.html",
        # The categories that already exist, so a rule names one of them
        # rather than a synonym sitting beside it in the pie.
        {"form": form, "test": test, "rules": rules, "known_categories": spending.known_categories()},
    )


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
    elif action == "category":
        # Only the category: the pattern is what the rule decides on, and a
        # page that edits it in passing would silently change which lines
        # the rule catches. Cleaned by the same rule a line's own category
        # is, since it reaches the same column and the same pie.
        rule.category = spending.clean_category(request.POST.get("categorie", ""))
        rule.save(update_fields=["category"])
        if rule.category:
            messages.success(request, f"Les dépenses de « {rule} » comptent en « {rule.category} ».")
        else:
            messages.success(
                request,
                f"Catégorie retirée : les dépenses de « {rule} » comptent en "
                f"« {spending.NO_CATEGORY} », sauf celles classées à la main.",
            )
    elif action in (SUSPEND, REACTIVATE):
        # The state the page asked for, never « toggle »: a double click or a
        # second tab drawn before the first click flipped the rule back.
        wanted = action == REACTIVATE
        if rule.is_active == wanted:
            messages.info(request, f"Règle « {rule} » déjà {'active' if wanted else 'suspendue'}.")
            return redirect("bank:rule_list")
        rule.is_active = wanted
        rule.save(update_fields=["is_active"])
        linked = 0 if rule.is_active else reconcile.reconcile()
        state = "réactivée" if rule.is_active else "suspendue"
        messages.success(
            request, f"Règle « {rule} » {state}" + (f" ; {linked} paiement(s) rattaché(s)." if linked else ".")
        )
    else:
        messages.error(request, "Action inconnue.")
    return redirect("bank:rule_list")


# -- « Reconnaissance des opérations » ----------------------------------------------------------------------------------
#
# The rules that say what an operation of the statement is (read at import,
# stored on the line) and what a credit is in the till (read whenever
# « Entrées d'argent » is drawn) - bank/recognition.py. A sub-page of Banque,
# like « Dépenses sans facture attendue »: no period, so no tabs.

#: What a button of these pages posts, under `action`.
RULE_ACTION = "action"
TEST, SAVE = "tester", "enregistrer"
SUSPEND, REACTIVATE, DELETE, UP, DOWN = "suspendre", "reactiver", "supprimer", "monter", "descendre"
UNKNOWN_RULE_ACTION = "Action inconnue : rien n'a été modifié."
#: Where a rule's row is, on the list, and where the new rule's card is.
RULE_ANCHOR = "regle-{pk}"
NEW_RULE_ANCHOR = "nouvelle-regle"
#: The two parts of the list, by the question their rules answer.
KIND_SECTION, TILL_SECTION = "nature", "en-caisse"
#: « Relire les opérations »: the digest the preview was drawn with, posted
#: back - what the writing is held to - and how many changed lines it draws.
DIGEST_PARAM = "empreinte"
MAX_CHANGES_SHOWN = 100
CHANGED_MEANWHILE = (
    "Rien n'a changé : les règles ou les opérations ont changé depuis l'aperçu. Vérifiez le nouvel aperçu."
)
#: The name a rule being tried is read under - it is never stored.
TRIED = "(essai)"
#: Said after an import read by no rule of the first kind: every line it
#: brought is « Autre », with no payee - after « Données »' « Effacer », or
#: before another bank's rules are written.
NO_KIND_RULE = (
    "Aucune règle de nature d'opération n'est active : les opérations importées sont toutes « Autre », sans tiers. "
    "Ajoutez des règles sur « Reconnaissance des opérations », puis relisez les opérations."
)


@dataclass(frozen=True)
class PatternExample:
    """A worked example of « Écrire un motif »: an invented label, a pattern
    and what it reads (bank/tests/test_recognition_views.py runs each)."""

    meaning: str
    label: str
    pattern: str
    reads: str
    searched: str = OperationRule.Searched.LABEL

    @property
    def meaning_label(self) -> str:
        return recognition.meaning_label(self.meaning)


PATTERN_EXAMPLES = (
    PatternExample(
        OperationRule.Meaning.CARD_PAYMENT,
        "CB 14/07 BOULANGERIE EXEMPLE",
        r"^CB (?P<jour>[0-9]{2})/(?P<mois>[0-9]{2}) (?P<tiers>.+)$",
        "tiers « BOULANGERIE EXEMPLE », carte utilisée le 14/07 (année : celle de l'opération, ou la précédente)",
    ),
    PatternExample(
        OperationRule.Meaning.PAYOUT,
        "REMISE TPE 0001234 BAR EXEMPLE",
        r"REMISE TPE",
        "le brut n'est pas imprimé : le montant reçu compte comme brut, commission inconnue",
    ),
    PatternExample(
        OperationRule.Meaning.PAYOUT,
        "REMISE CB BRUT 1 234,56 COM 12,34",
        r"REMISE CB BRUT (?P<encaisse>[0-9 ]+,[0-9]{2})",
        "brut 1 234,56 € ; la commission est le brut moins le montant reçu",
    ),
)


def _group_help() -> list[tuple[str, str, str]]:
    """(group, what it reads, the meanings reading it) for « Écrire un motif »."""
    return [
        (
            group,
            said,
            ", ".join(
                recognition.meaning_label(meaning) for meaning, read in recognition.GROUPS.items() if group in read
            ),
        )
        for group, said in recognition.GROUP_LABELS.items()
    ]


def _layer_of(meaning) -> frozenset:
    """The meanings a rule meaning `meaning` is ordered among: its question."""
    return recognition.KIND_MEANINGS if meaning in recognition.KIND_MEANINGS else recognition.TILL_MEANINGS


@dataclass
class RuleRow:
    """One stored rule, as the list draws it."""

    rule: OperationRule
    #: Its place among the rules of its question, from 1.
    order: int
    first: bool = False
    last: bool = False
    #: Why its pattern no longer passes the check, in French - « » when it does.
    problem: str = ""
    #: It ran past the time limit while the page read the lines.
    slow: bool = False
    #: A payout rule: whether it reads the gross printed in the label.
    reads_gross: bool = False
    #: How many lines it decides now; None while it decides nothing (suspended,
    #: invalid).
    decided: int | None = None


def _rule_rows() -> tuple[list[RuleRow], list[RuleRow], int, recognition.Rules]:
    """The rules of both questions in their order, what each decides now, and
    how many stored lines the active rules would read otherwise - the rules
    read once, the lines in one pass."""
    stored = list(OperationRule.objects.order_by("position", "name"))
    rules = recognition.compile_rules(stored)
    decided = Counter()
    pending = 0
    lines = BankTransaction.objects.only(
        "pk", "operation_date", "label", "bank_type", "kind", "counterparty", "card_date", "amount", "income_source"
    )
    for line in lines:
        now = recognition.describe(rules, line.label, line.bank_type, line.operation_date)
        if now.rule:
            decided[now.rule] += 1
        if now.stored != (line.kind, line.counterparty, line.card_date):
            pending += 1
        # A credit's own « En caisse » choice beats every rule (`income.reading_of`).
        if line.amount > 0 and line.income_source not in income.CHOSEN:
            till = recognition.till_reading(rules, line.label, line.bank_type)
            if till is not None:
                decided[till.rule] += 1
    kind_rows, till_rows = [], []
    for rule in stored:
        try:
            compiled = recognition.check(rule.meaning, rule.searched, rule.pattern)
            problem = ""
        except PatternError as error:
            compiled, problem = None, error.message.removeprefix(f"{recognition.PATTERN_LABEL} : ")
        rows = kind_rows if rule.meaning in recognition.KIND_MEANINGS else till_rows
        rows.append(
            RuleRow(
                rule,
                len(rows) + 1,
                problem=problem,
                slow=rule.name in rules.slow,
                reads_gross=(
                    compiled is not None
                    and rule.meaning == OperationRule.Meaning.PAYOUT
                    and recognition.GROSS in compiled.groupindex
                ),
                decided=decided[rule.name] if rule.is_active and compiled is not None else None,
            )
        )
    for rows in (kind_rows, till_rows):
        if rows:
            rows[0].first = rows[-1].last = True
    return kind_rows, till_rows, pending, rules


@dataclass(frozen=True)
class TriedLine:
    """A line the rule being tried finds, and what it reads on it."""

    line: BankTransaction
    payee: str = ""
    card_date: date | None = None
    gross: Decimal | None = None
    #: A payout whose label prints no gross: the amount received counts.
    no_gross: bool = False


@dataclass
class RuleTest:
    """What « Tester » found: the lines the unsaved rule finds, how many of
    them something else already decides, and the newest of them."""

    till: bool
    scanned: int = 0
    count: int = 0
    #: What the credits found add up to - a till rule's, which reads credits only.
    total: Decimal = Decimal("0")
    #: A rule of what the operation is finds both ways: what came in and
    #: what went out, apart - their sum is neither (a transfer each way
    #: nets 0).
    received: Decimal = Decimal("0")
    paid: Decimal = Decimal("0")
    #: Found, and decided by a rule placed before it - or, a credit, by its
    #: own « En caisse » choice: this rule will never read them.
    earlier: int = 0
    examples: list = field(default_factory=list)
    #: It ran past the time limit: what it found after is not counted.
    slow: bool = False

    @property
    def more(self) -> int:
        return self.count - len(self.examples)


def _test_rule(form: OperationRuleForm, rule: OperationRule | None = None) -> RuleTest | None:
    """The posted rule tried over the stored lines - every line for a rule of
    what the operation is, the credits for one of the till - and nothing
    saved. None while what is tried is refused (its errors are on the form):
    the name alone may still be wrong, since it decides nothing. `rule` is
    the stored rule being edited: only the rules before it count as earlier
    - every active rule of the other question when it is moved there, since
    it is saved last of it. A credit chosen by hand counts as decided too:
    its own choice beats every rule (`income.reading_of`)."""
    if form.compiled is None:
        return None
    meaning, searched = form.cleaned_data["meaning"], form.cleaned_data["searched"]
    tried = recognition.Rule(TRIED, meaning, searched, form.compiled)
    till = meaning in recognition.TILL_MEANINGS
    alone = recognition.Rules(till=(tried,)) if till else recognition.Rules(kinds=(tried,))
    before = OperationRule.objects.filter(is_active=True, meaning__in=_layer_of(meaning)).order_by("position", "name")
    if rule is not None and _layer_of(rule.meaning) == _layer_of(meaning):
        before = [one for one in before if (one.position, one.name) < (rule.position, rule.name)]
    earlier = recognition.compile_rules(before)
    lines = BankTransaction.objects.only(
        "pk", "operation_date", "card_date", "label", "bank_type", "counterparty", "amount", "kind", "income_source"
    ).order_by("-operation_date", "-pk")
    if till:
        lines = lines.filter(amount__gt=0)
    test = RuleTest(till)
    for line in lines:
        test.scanned += 1
        if till:
            read = recognition.till_reading(alone, line.label, line.bank_type)
            if read is None:
                continue
            decided = (
                line.income_source in income.CHOSEN
                or recognition.till_reading(earlier, line.label, line.bank_type) is not None
            )
            found = TriedLine(line, gross=read.gross, no_gross=read.source == IncomeSource.CARD and read.gross is None)
            test.total += line.amount
        else:
            read = recognition.describe(alone, line.label, line.bank_type, line.operation_date)
            if not read.rule:
                continue
            decided = bool(recognition.describe(earlier, line.label, line.bank_type, line.operation_date).rule)
            found = TriedLine(line, payee=read.counterparty, card_date=read.card_date)
            if line.amount > 0:
                test.received += line.amount
            else:
                test.paid -= line.amount
        test.count += 1
        test.earlier += decided
        if len(test.examples) < MAX_EXAMPLES:
            test.examples.append(found)
    test.slow = bool(alone.slow)
    return test


def _stored_problem(rule: OperationRule) -> str:
    """Why a stored rule's pattern no longer passes the check - « parenthèse
    non fermée (position 6) » -, or « »."""
    try:
        recognition.check(rule.meaning, rule.searched, rule.pattern)
    except PatternError as error:
        return error.message.removeprefix(f"{recognition.PATTERN_LABEL} : ").rstrip(".")
    return ""


def _rule_url(rule: OperationRule) -> str:
    return f"{reverse('bank:recognition')}#{RULE_ANCHOR.format(pk=rule.pk)}"


def _rule_saved(rule: OperationRule, verb: str) -> str:
    said = f"Règle « {rule.name} » {verb}"
    if rule.meaning in recognition.KIND_MEANINGS:
        return f"{said} : elle vaut pour les relevés importés ensuite."
    return f"{said}."


def _saved(form, *, last: bool, model=OperationRule, taken: str = NAME_TAKEN):
    """The form's rule (or format, `model`), saved - after every one when
    `last` (a new rule, or one moved to the other question: its number would
    otherwise put it wherever it falls among the rules there) - or None when
    another took its name between the form's check and the write (two tabs,
    a double click): said on the name, as the form says it (`taken`),
    nothing written."""
    try:
        with transaction.atomic():
            saved = form.save(commit=False)
            if last:
                highest = model.objects.order_by("-position").values_list("position", flat=True).first()
                saved.position = (highest or 0) + 1
            saved.save()
    except IntegrityError:
        if not model.objects.filter(name=form.instance.name).exclude(pk=form.instance.pk).exists():
            raise
        form.add_error("name", taken)
        return None
    return saved


def _import_refused(rules: recognition.Rules) -> bool:
    """Whether an import refuses to run with these rules: one that fails the
    check, or one of what the operation is found too slow. A till rule never
    runs at import, so one found slow on this page stops nothing."""
    kinds = {rule.name for rule in rules.kinds}
    return bool(rules.invalid) or any(name in kinds for name in rules.slow)


def recognition_page(request):
    """« Reconnaissance des opérations »: the rules of both questions, what
    each decides now, and a new rule - « Tester » tries it on the stored
    lines and saves nothing; « Enregistrer la règle » puts it last of its
    question."""
    form = OperationRuleForm(request.POST or None)
    test = None
    if request.method == "POST":
        action = request.POST.get(RULE_ACTION)
        if action not in (TEST, SAVE):
            messages.error(request, UNKNOWN_RULE_ACTION)
            return redirect("bank:recognition")
        valid = form.is_valid()
        if action == SAVE and valid:
            rule = _saved(form, last=True)
            if rule is not None:
                messages.success(request, _rule_saved(rule, "ajoutée"))
                return redirect(_rule_url(rule))
        if action == TEST:
            test = _test_rule(form)
    kind_rows, till_rows, pending, rules = _rule_rows()
    return render(
        request,
        "bank/recognition.html",
        {
            "form": form,
            "test": test,
            "kind_rows": kind_rows,
            "till_rows": till_rows,
            # The stored lines the active rules of the first question would
            # read otherwise - what « Relire » would write.
            "pending": pending,
            # An import refuses to run past a rule that cannot be applied.
            "blocked": _import_refused(rules),
            "examples": PATTERN_EXAMPLES,
            "groups": _group_help(),
        },
    )


def recognition_rule(request, pk):
    """One rule: its form on a GET (which writes nothing), « Tester » and
    « Enregistrer » on it, and the list's buttons - suspend, reactivate,
    delete, move up or down among the rules of its question."""
    rule = get_object_or_404(OperationRule, pk=pk)
    action = request.POST.get(RULE_ACTION) if request.method == "POST" else None
    if request.method != "POST" or action in (TEST, SAVE):
        test = None
        if request.method != "POST":
            form = OperationRuleForm(instance=rule)
        else:
            # Bound to a copy: validating writes what was typed onto the
            # instance, and the page's title would name a refused name.
            form = OperationRuleForm(request.POST, instance=OperationRule.objects.get(pk=rule.pk))
            valid = form.is_valid()
            if action == SAVE and valid:
                # Moved to the other question, it goes last there, as a new
                # rule does.
                moved = _layer_of(form.cleaned_data["meaning"]) != _layer_of(rule.meaning)
                saved = _saved(form, last=moved)
                if saved is not None:
                    verb = "enregistrée, dernière de sa nouvelle partie" if moved else "enregistrée"
                    messages.success(request, _rule_saved(saved, verb))
                    return redirect(_rule_url(saved))
            if action == TEST:
                test = _test_rule(form, rule)
        return render(
            request,
            "bank/recognition_rule.html",
            {
                "rule": rule,
                "form": form,
                "test": test,
                "problem": _stored_problem(rule),
                "is_kind": rule.meaning in recognition.KIND_MEANINGS,
                "examples": PATTERN_EXAMPLES,
                "groups": _group_help(),
            },
        )
    if action == DELETE:
        name = rule.name
        rule.delete()
        messages.success(request, f"Règle « {name} » supprimée.")
        section = KIND_SECTION if rule.meaning in recognition.KIND_MEANINGS else TILL_SECTION
        return redirect(f"{reverse('bank:recognition')}#{section}")
    if action in (SUSPEND, REACTIVATE):
        rule.is_active = action == REACTIVATE
        rule.save(update_fields=["is_active"])
        messages.success(request, f"Règle « {rule.name} » {'réactivée' if rule.is_active else 'suspendue'}.")
    elif action in (UP, DOWN):
        if _move(rule, -1 if action == UP else 1):
            messages.success(request, f"Règle « {rule.name} » {'montée' if action == UP else 'descendue'}.")
        else:
            messages.info(
                request, f"Règle « {rule.name} » déjà {'la première' if action == UP else 'la dernière'} de sa partie."
            )
    else:
        messages.error(request, UNKNOWN_RULE_ACTION)
    return redirect(_rule_url(rule))


def _swapped(ordered: list, pk, step: int) -> list | None:
    """`ordered` (as a list orders it: position, then name) with the row `pk`
    swapped with its neighbour `step` away: the rows whose position changed,
    or None at either end. Positions two rows share are made distinct first,
    each kept or raised by the least, so a swap always moves something and
    nothing else changes place."""
    here = next(index for index, one in enumerate(ordered) if one.pk == pk)
    there = here + step
    if not 0 <= there < len(ordered):
        return None
    positions = []
    for one in ordered:
        positions.append(max(one.position, positions[-1] + 1) if positions else one.position)
    ordered[here], ordered[there] = ordered[there], ordered[here]
    moved = []
    for one, position in zip(ordered, positions, strict=True):
        if one.position != position:
            one.position = position
            moved.append(one)
    return moved


def _move(rule: OperationRule, step: int) -> bool:
    """Swap `rule` with its neighbour among the rules of its question, as the
    list orders them (position, then name) - False at either end."""
    with transaction.atomic():
        layer = list(OperationRule.objects.filter(meaning__in=_layer_of(rule.meaning)).order_by("position", "name"))
        moved = _swapped(layer, rule.pk, step)
        if moved is None:
            return False
        OperationRule.objects.bulk_update(moved, ["position"])
    return True


@dataclass(frozen=True)
class ChangeRow:
    """A stored line the active rules read otherwise: what it says now, and
    what « Relire » would write."""

    change: recognition.Change
    kind_before: str
    kind_after: str


def _detached(pending: recognition.Changes) -> tuple[int, int]:
    """What « Relire » unties without writing it: (the credits that follow a
    retained payer and whose payer key moves with their payee - they stop
    following it -, the debits whose payee key a learnt alias names and
    moves with it). Nothing stored on them changes, but both keys are read
    off the payee (`income.payer_key`, `reconcile.payee_of`). Three queries,
    none when no payee changes."""
    moving = [one for one in pending.changes if one.payee_changes]
    if not moving:
        return 0, 0
    retained = set(IncomePayer.objects.values_list("key", flat=True))
    learnt = set(CounterpartyAlias.objects.values_list("name", flat=True))
    # `stored_changes` read the lines without their « En caisse » choice:
    # the credits holding one, in one query rather than one a line.
    chosen = dict(
        BankTransaction.objects.filter(amount__gt=0, income_source__in=income.CHOSEN).values_list("pk", "income_source")
    )
    payers = aliases = 0
    for one in moving:
        line = one.line
        after = copy.copy(line)
        after.counterparty = one.now.counterparty
        if line.amount > 0:
            key = income.payer_key(line)
            if key in retained and income.payer_key(after) != key:
                line.income_source = chosen.get(line.pk, income.AUTOMATIC)
                payers += income.follows_its_payer(line, pending.rules)
        elif line.amount < 0:
            key = matching.alias_key(reconcile.payee_of(line))
            if key in learnt and matching.alias_key(reconcile.payee_of(after)) != key:
                aliases += 1
    return payers, aliases


def recognition_reapply(request):
    """« Relire les opérations déjà importées »: a GET shows what the active
    rules now read otherwise on the stored lines, and writes nothing; the
    POST writes exactly that - held to the digest the preview was drawn
    with (`recognition.apply_changes`), so what was shown is what is
    written, or nothing is."""
    if request.method == "POST":
        try:
            changed = recognition.apply_changes(request.POST.get(DIGEST_PARAM, ""))
        except recognition.ChangedMeanwhile:
            messages.warning(request, CHANGED_MEANWHILE)
            return redirect("bank:recognition_reapply")
        except ValueError as refusal:
            messages.error(request, str(refusal))
            return redirect("bank:recognition")
        if changed:
            plural = "s" if changed > 1 else ""
            messages.success(request, f"{changed} opération{plural} relue{plural}.")
        else:
            messages.info(request, "Rien à relire : les opérations importées sont déjà lues ainsi.")
        return redirect(f"{reverse('bank:recognition')}#{KIND_SECTION}")
    pending = recognition.stored_changes()
    kinds = dict(BankTransaction.Kind.choices)
    rows = [
        ChangeRow(one, kinds.get(one.line.kind, one.line.kind), kinds.get(one.now.kind, one.now.kind))
        for one in pending.changes[:MAX_CHANGES_SHOWN]
    ]
    detached_payers, detached_aliases = _detached(pending)
    return render(
        request,
        "bank/recognition_reapply.html",
        {
            "rows": rows,
            "count": len(pending.changes),
            "more": len(pending.changes) - len(rows),
            "digest": pending.digest,
            "digest_param": DIGEST_PARAM,
            "detached_payers": detached_payers,
            "detached_aliases": detached_aliases,
            "problems": pending.rules.problems,
            # What `apply_changes` refuses: the till rules read above for
            # the payers never run there.
            "blocked": _import_refused(pending.rules),
        },
    )


# -- « Format du relevé » ----------------------------------------------------------------------------------------
# How a bank's CSV export is laid out (`StatementFormat`, read by
# bank/statements.py): the formats in their order - the first is the one an
# import reads with when nobody chooses -, a new one, and one format's page.
# Both forms carry « Tester »: a file picked on the page read with the format
# as typed, nothing saved, the file never kept. A sub-page of Banque, like
# « Reconnaissance des opérations »; the buttons post `action` as there.

#: The file « Tester » reads, posted with the form.
TEST_FILE = "fichier_essai"
#: The import card's choice of format, drawn when there are several.
FORMAT_PARAM = "format"
#: Where a format's row is, on the list, and where the new format's card is.
FORMAT_ANCHOR = "format-{pk}"
NEW_FORMAT_ANCHOR = "nouveau-format"
#: What « Tester » shows: the file's first rows, split into numbered columns,
#: and the first operations the format reads in it.
TEST_ROWS_SHOWN = 15
TEST_LINES_SHOWN = 30
NO_TEST_FILE = "Choisissez un fichier pour voir ce que le format en lit."
CSV_ONLY = "seuls les fichiers CSV sont acceptés."
KEEP_ONE_FORMAT = "Gardez au moins un format : modifiez-le plutôt."
UNKNOWN_FORMAT = "Format de relevé inconnu."


@dataclass(frozen=True)
class FormatExample:
    """« Lire un format »'s worked example: an invented export, the format
    that reads it, and what it reads (bank/tests/test_statement_format_views.py
    reads it so)."""

    rows: tuple
    #: The fields of the format, as `statements.check_format` reads them.
    settings: dict
    said: str
    reads: str


FORMAT_EXAMPLE = FormatExample(
    rows=(
        "Date,Libellé,Débit,Crédit",
        "2026-07-03,CB EPICERIE EXEMPLE,12.30,",
        "2026-07-05,VIR CLIENT EXEMPLE,,250.00",
    ),
    settings={
        "delimiter": ",",
        "date_format": "yyyy-mm-dd",
        "decimal_mark": ".",
        "date_column": 1,
        "label_columns": "2",
        "debit_column": 3,
        "credit_column": 4,
    },
    said="Séparateur « , », dates aaaa-mm-jj, décimales au point ; date 1, libellé 2, débits 3, crédits 4",
    reads="deux opérations, -12.30 € et 250.00 € ; la première ligne n'a pas de date, elle est passée",
)


@dataclass
class FormatRow:
    """One stored format, as the list draws it."""

    fmt: StatementFormat
    #: Its place, from 1: the first is the import's default.
    order: int
    #: Which column holds what: « date 1 · libellé 4 · montant 6 ».
    columns: str
    #: Why the format no longer passes the check, in French - « » when it does.
    problem: str = ""
    first: bool = False
    last: bool = False


def _columns_said(fmt: StatementFormat) -> str:
    """« date 1 · libellé 4 · montant 6 »: which column holds what."""
    try:
        labels = ", ".join(str(number) for number in statements.label_columns(fmt.label_columns))
    except statements.FormatError:
        labels = fmt.label_columns
    said = (
        ("date", fmt.date_column),
        ("libellé", labels),
        ("montant", fmt.amount_column),
        ("débits", fmt.debit_column),
        ("crédits", fmt.credit_column),
        ("valeur", fmt.value_date_column),
        ("type", fmt.bank_type_column),
    )
    return " · ".join(f"{word} {number}" for word, number in said if number not in (None, ""))


def _format_problem(fmt: StatementFormat) -> str:
    """Why a stored format no longer passes `check_format` - « Colonne du
    montant : indiquez … » -, or « ». A stored format is never trusted: an
    import refuses to read with one the check refuses."""
    try:
        statements.check_format(fmt)
    except statements.FormatError as error:
        said = error.message.rstrip(".")
        try:
            label = str(capfirst(StatementFormat._meta.get_field(error.field).verbose_name))
        except FieldDoesNotExist:
            return said
        # The pattern's own sentences name their field already.
        return said if said.startswith(label) else f"{label} : {said[:1].lower()}{said[1:]}"
    return ""


def _format_rows() -> list[FormatRow]:
    stored = list(StatementFormat.objects.order_by("position", "name"))
    rows = [
        FormatRow(fmt, order, _columns_said(fmt), _format_problem(fmt)) for order, fmt in enumerate(stored, start=1)
    ]
    if rows:
        rows[0].first = rows[-1].last = True
    return rows


def _format_url(pk) -> str:
    return f"{reverse('bank:statement_formats')}#{FORMAT_ANCHOR.format(pk=pk)}"


@dataclass
class FormatTest:
    """What « Tester » read in a file with the format as typed: its first
    rows in numbered columns - what a person picks the numbers from - and
    the operations the format reads there, or why it reads none."""

    file_name: str = ""
    #: Why the file was not read at all: none chosen, too heavy, no CSV.
    problem: str = ""
    #: The first rows, each as wide as the widest (`width` cells).
    rows: list = field(default_factory=list)
    width: int = 0
    #: A row had more columns than a format may name: those are not shown.
    wider: bool = False
    column_limit: int = statements.MAX_COLUMN
    #: Why the format reads no statement in the file - the import's own
    #: sentence.
    refusal: str = ""
    account: str = ""
    count: int = 0
    #: Read, and already imported: the same fingerprint is stored.
    known: int = 0
    #: (StatementLine, its kind as the page says it) - the first ones.
    lines: list = field(default_factory=list)

    @property
    def columns(self) -> range:
        return range(1, self.width + 1)

    @property
    def more(self) -> int:
        return self.count - len(self.lines)


def _test_format(request, form: StatementFormatForm) -> FormatTest | None:
    """The file posted as `TEST_FILE` read with the format as typed - nothing
    saved, the file never kept. None while the format is refused (its errors
    are on the form): the name alone may still be wrong, since it reads
    nothing."""
    if form.layout is None:
        return None
    upload = request.FILES.get(TEST_FILE)
    if upload is None:
        return FormatTest(problem=NO_TEST_FILE)
    test = FormatTest(upload.name)
    if not upload.name.lower().endswith(".csv"):
        test.problem = f"{upload.name} : {CSV_ONLY}"
        return test
    too_heavy = file_too_big(upload)
    if too_heavy:
        test.problem = too_heavy
        return test
    content = upload.read()
    try:
        # The rows as the reader splits them - decoded, the blank ones left
        # out - so a column numbered here is the column the format names.
        rows = statements.rows(content, form.layout)
    except ValueError as refusal:
        test.refusal = str(refusal)
        return test
    shown = rows[:TEST_ROWS_SHOWN]
    widest = max((len(row) for row in shown), default=0)
    test.width = min(widest, statements.MAX_COLUMN)
    test.wider = widest > statements.MAX_COLUMN
    test.rows = [(row + [""] * test.width)[: test.width] for row in shown]
    try:
        statement = statements.parse_statement(content, recognition.load(), form.layout)
    except ValueError as refusal:
        test.refusal = str(refusal)
        return test
    kinds = dict(BankTransaction.Kind.choices)
    test.account = statement.account
    test.count = len(statement.lines)
    # In chunks, as the import asks: one `__in` of every fingerprint was
    # past SQLite's bound variables on a long statement - a 500.
    test.known = len(reconcile.known_fingerprints(line.fingerprint for line in statement.lines))
    test.lines = [(line, kinds.get(line.kind, line.kind)) for line in statement.lines[:TEST_LINES_SHOWN]]
    return test


def statement_formats(request):
    """« Format du relevé »: the formats in their order, and a new one -
    « Tester » reads a file with it and saves nothing; « Enregistrer le
    format » puts it last."""
    form = StatementFormatForm(request.POST or None)
    test = None
    if request.method == "POST":
        action = request.POST.get(RULE_ACTION)
        if action not in (TEST, SAVE):
            messages.error(request, UNKNOWN_RULE_ACTION)
            return redirect("bank:statement_formats")
        valid = form.is_valid()
        if action == SAVE and valid:
            fmt = _saved(form, last=True, model=StatementFormat, taken=FORMAT_NAME_TAKEN)
            if fmt is not None:
                messages.success(request, f"Format « {fmt.name} » ajouté.")
                return redirect(_format_url(fmt.pk))
        if action == TEST:
            test = _test_format(request, form)
    return render(
        request,
        "bank/statement_formats.html",
        {"form": form, "test": test, "rows": _format_rows(), "example": FORMAT_EXAMPLE},
    )


def statement_format(request, pk):
    """One format: its form on a GET (which writes nothing), « Tester » and
    « Enregistrer » on it, and the list's buttons - delete (never the last
    one: an import needs one), move up or down."""
    fmt = get_object_or_404(StatementFormat, pk=pk)
    action = request.POST.get(RULE_ACTION) if request.method == "POST" else None
    if request.method != "POST" or action in (TEST, SAVE):
        test = None
        if request.method != "POST":
            form = StatementFormatForm(instance=fmt)
        else:
            # Bound to a copy: validating writes what was typed onto the
            # instance, and the page's title would name a refused name.
            form = StatementFormatForm(request.POST, instance=StatementFormat.objects.get(pk=fmt.pk))
            valid = form.is_valid()
            if action == SAVE and valid:
                saved = _saved(form, last=False, model=StatementFormat, taken=FORMAT_NAME_TAKEN)
                if saved is not None:
                    messages.success(request, f"Format « {saved.name} » enregistré.")
                    return redirect(_format_url(saved.pk))
            if action == TEST:
                test = _test_format(request, form)
        return render(
            request,
            "bank/statement_format.html",
            {"fmt": fmt, "form": form, "test": test, "problem": _format_problem(fmt), "example": FORMAT_EXAMPLE},
        )
    if action == DELETE:
        # Deleted, then counted, in one transaction: two tabs deleting the
        # last two formats at once leave one, never none.
        with transaction.atomic():
            fmt.delete()
            kept = StatementFormat.objects.exists()
            if not kept:
                transaction.set_rollback(True)
        if not kept:
            messages.error(request, KEEP_ONE_FORMAT)
            return redirect(_format_url(pk))
        messages.success(request, f"Format « {fmt.name} » supprimé.")
        return redirect("bank:statement_formats")
    if action in (UP, DOWN):
        with transaction.atomic():
            ordered = list(StatementFormat.objects.order_by("position", "name"))
            moved = _swapped(ordered, fmt.pk, -1 if action == UP else 1)
            if moved is not None:
                StatementFormat.objects.bulk_update(moved, ["position"])
        if moved is None:
            messages.info(request, f"Format « {fmt.name} » déjà {'le premier' if action == UP else 'le dernier'}.")
        elif ordered[0].pk == fmt.pk:
            # The first is what every import reads with, nobody choosing.
            messages.success(request, f"Format « {fmt.name} » monté : c'est maintenant celui de l'import.")
        else:
            messages.success(request, f"Format « {fmt.name} » {'monté' if action == UP else 'descendu'}.")
    else:
        messages.error(request, UNKNOWN_RULE_ACTION)
    return redirect(_format_url(fmt.pk))


def _import_format(request) -> tuple[StatementFormat | None, str]:
    """The format an upload is read with - the one chosen on the import card
    when it offers a choice (`FORMAT_PARAM`), the first otherwise - and, when
    there is none, why: no format at all, or one chosen that is not there
    (deleted since the page was drawn, a tampered id)."""
    chosen = request.POST.get(FORMAT_PARAM)
    if chosen is None:
        fmt = reconcile.default_format()
        return fmt, "" if fmt is not None else reconcile.NO_FORMAT
    fmt = StatementFormat.objects.filter(pk=int(chosen)).first() if is_id(chosen) else None
    return fmt, "" if fmt is not None else UNKNOWN_FORMAT


def _import_statements(request):
    # Comes back to the page as it was being read - its tab, its month, its
    # window - rather than to the bare list: an import made to check a given
    # period answered by silently showing every other one.
    back = _back(request)
    uploads = request.FILES.getlist("files")
    if not uploads:
        messages.error(request, "Choisissez au moins un relevé bancaire (fichier CSV).")
        return redirect(back)
    # What an upload may weigh (security audit UPLOAD-1): the selection as a
    # whole, then each file by its name - a bank's CSV is a few KB a month.
    too_heavy = selection_too_big(uploads)
    if too_heavy:
        messages.error(request, f"{too_heavy} Aucun relevé n'a été importé.")
        return redirect(back)
    fmt, refused = _import_format(request)
    if fmt is None:
        messages.error(request, f"{refused} Aucun relevé n'a été importé.")
        return redirect(back)
    created = known = 0
    # Read once for every file, as the format is: a rule found too slow on
    # one refuses the others too, rather than being tried afresh on each.
    rules = recognition.load()
    for upload in uploads:
        if not upload.name.lower().endswith(".csv"):
            messages.error(request, f"{upload.name} : {CSV_ONLY}")
            continue
        if file_too_big(upload):
            messages.error(request, f"{file_too_big(upload)} Ce relevé n'a pas été importé.")
            continue
        try:
            summary = reconcile.import_statement(upload.read(), rules, fmt)
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
    if created and not rules.kinds:
        messages.warning(request, NO_KIND_RULE)
    if created:
        _say_treasury_gaps(request)
    return redirect(back)


def _say_treasury_gaps(request) -> None:
    """« Trésorerie : 2 écarts à résoudre. » after an import that brought
    lines, when a gap is now to resolve: the import completing a pending
    stretch is the moment a gap becomes real (bank/treasury.py, « pending »),
    and nobody opens « Trésorerie » to find out. The treasury is read here,
    on the import's POST alone (`treasury.QUERIES` more queries), and says
    nothing where there is no point at all."""
    gaps = len(treasury.load().to_resolve)
    if gaps:
        messages.warning(request, f"Trésorerie : {_gaps_to_resolve(gaps)}.")


def _period(request, months) -> tuple[str, DateRange]:
    """The month and the free dates Banque is read through - one reading for
    the page and for its zip, so the zip holds what the page shows.

    A month chosen is the answer to the question the free dates ask, so it
    takes it whole rather than being crossed with them: two windows on one
    page, one of them invisible, is how a figure comes out narrower than the
    period the page says it is counting. The inputs are drawn disabled and
    no link carries « du »/« au » while a month is on, so the dates cannot
    come back on the next click either."""
    month = request.GET.get("mois", "")
    if month not in dict(months):
        month = ""
    return month, DateRange() if month else date_range(request)


def _in_period(lines, month: str, window: DateRange):
    """`lines` booked in that month (as its first and last day) or window.
    On the date the bank booked the operation, which is what « Mois »
    filters on too: windowed on the card's own date instead, the two
    pickers would disagree about which period a card payment belongs to."""
    return _bank_period(month, window)[0].limit(lines, "operation_date")


def _invoice_zip_url(view: str, month: str, window: DateRange) -> str:
    """The zip over the period the page is showing; its tab rides along, so
    « rien à télécharger » comes back to it."""
    return f"{reverse('bank:invoice_zip')}?{urlencode(_page_parameters(view, month, window))}"


def _period_label(month: str, window: DateRange) -> str:
    """« juin 2026 », « du 01_06_2026 au 30_06_2026 », « tout l'historique »:
    what the zip's name says it holds, dated the way its files are."""
    if month:
        year, number = (int(part) for part in month.split("-"))
        return f"{MONTH_NAMES[number - 1]} {year}"
    start = window.start.strftime("%d_%m_%Y") if window.start else ""
    end = window.end.strftime("%d_%m_%Y") if window.end else ""
    if start and end:
        return f"du {start} au {end}"
    if start:
        return f"depuis le {start}"
    if end:
        return f"jusqu'au {end}"
    return "tout l'historique"


def _page_url(view: str, month: str, window: DateRange) -> str:
    """This page with everything the reader is looking through it kept: the
    tab, the month and the window travel together.

    The tab links used to paste « ?vue=…&mois=… » together in the template,
    which is exactly where a new parameter gets forgotten: the window would
    then fall off the page on the next click, the figures changing with
    nothing on screen to say why. One place knows the rule instead."""
    return f"{reverse('bank:bank_home')}?{urlencode(_page_parameters(view, month, window))}"


def _page_parameters(view: str, month: str, window: DateRange) -> dict:
    """Everything the reader is looking through the page with. Deliberately
    without the row search (« ligne », « recherche »): a search belongs to
    one row and one moment, so a tab, an import or a link made carries the
    tab, the month and the window - and clears the search."""
    parameters = {"vue": view}
    if month:
        parameters["mois"] = month
    parameters.update(window.parameters)
    return parameters


def _spending_url(window: DateRange, showing_all: bool, kind: str = "", left_out=()) -> str:
    """« Dépenses » with the period the reader is looking through it kept -
    which of the three kinds of naming they are looking at, and what the pie
    leaves out.

    Built here rather than pasted together in the template, for the reason
    `_page_url` above is: that is exactly where a parameter gets forgotten,
    and a window falling off the page changes every figure with nothing on
    screen to say why. The kind travels the same way, so classifying a line
    from inside « Classées par règle » answers there rather than two screens
    up an unfiltered page - and so does `sans`, or naming one line would put
    the VAT back in the pie.
    """
    query = urlencode(_spending_fields(window, showing_all, kind, left_out))
    return f"{reverse('bank:spending_home')}{'?' + query if query else ''}"


def _spending_fields(window: DateRange, showing_all: bool, kind: str = "", left_out=()) -> list[tuple[str, str]]:
    """What `_spending_url` puts in the address, as (name, value) pairs - the
    same pairs a GET form of the page carries as hidden fields, so a link
    and a form cannot carry two different pages. Pairs, because `sans`
    repeats; `urlencode` takes each name through exactly, accents, spaces
    and « & » included."""
    fields = _window_fields(window, showing_all)
    if kind:
        fields.append((spending.KIND_PARAM, kind))
    fields += [(LEFT_OUT_PARAM, name) for name in left_out]
    return fields


@dataclass(frozen=True)
class LeftOutRow:
    """One category left out of the pie: what it holds over the window -
    `category` is None when nothing on this period carries that name - and
    the same page with it put back."""

    name: str
    category: spending.Category | None
    put_back_url: str


def _left_out_rows(report, window: DateRange, showing_all: bool) -> list[LeftOutRow]:
    """« sans : TVA 1 200,00 € (remettre), … », in the order the names were
    asked. A name with nothing on this period is still named - it is the
    reader's question over another one - and « remettre » is how it goes.

    Keyed by the name CLEANED, as `spending_for` compares them: two stored
    spellings of one name (« Frais bancaires » typed on a line, « Frais
    bancaires » with two spaces on a rule) are one key and leave the pie
    together, so what they hold is said as one sum."""
    by_name: dict[str, spending.Category] = {}
    for one in report.categories:
        key = spending.clean_category(one.name)
        held = by_name.get(key)
        if held is None:
            by_name[key] = one
        else:
            by_name[key] = spending.Category(
                key,
                amount=held.amount + one.amount,
                operations=held.operations + one.operations,
                by_rule=held.by_rule + one.by_rule,
                left_out=held.left_out and one.left_out,
            )
    return [
        LeftOutRow(
            name,
            by_name.get(name),
            _spending_url(window, showing_all, report.kind, [other for other in report.left_out if other != name]),
        )
        for name in report.left_out
    ]


def _window_fields(window: DateRange, showing_all: bool) -> list[tuple[str, str]]:
    """A period as the two pages with a default one carry it: the dates, and
    `tout` when the reader asked for everything. One definition, so
    « Dépenses » and « Entrées d'argent » cannot spell a period two ways."""
    fields = list(window.parameters.items())
    if showing_all:
        fields.append((ALL_PARAM, "1"))
    return fields


def _other_page_url(url_name: str, window: DateRange) -> str:
    """Another page of the app over the period this one is showing - the
    window alone: no other page reads `sans` nor `classement`.

    An EMPTY window is everything - « tout l'historique » is the only way
    these pages get one - and it travels as `tout=1`: bare, the page on the
    other end opens on ITS default, the last twelve months, and « tout » on
    one side read a year on the other."""
    url = reverse(url_name)
    return f"{url}?{urlencode(window.parameters if window else {ALL_PARAM: '1'})}"


def _income_url(window: DateRange) -> str:
    """« Entrées d'argent » over the same period."""
    return _other_page_url("bank:income_home", window)


def _income_page_url(window: DateRange, showing_all: bool) -> str:
    """« Entrées d'argent » with the period the reader is looking through it
    - built here, for the reason `_page_url` is."""
    query = urlencode(_window_fields(window, showing_all))
    return f"{reverse('bank:income_home')}{'?' + query if query else ''}"


def _treasury_page_url(window: DateRange, showing_all: bool) -> str:
    """« Trésorerie » with the period the reader is looking through it - its
    curve's and its months' - built here, for the reason `_page_url` is."""
    query = urlencode(_window_fields(window, showing_all))
    return f"{reverse('bank:treasury')}{'?' + query if query else ''}"


def _covered_url(report, window: DateRange) -> str:
    """« Entrées d'argent » from the first day both sides cover to the end of
    the window - or "" when the window holds nothing one side cannot see,
    and when it ends before that day (nothing in it is covered by both).

    The page's comparison already leaves those days out of its Écart; this
    is the same comparison with the columns agreeing with it, and the view
    to read when the till reaches back further than the statement - « tout »
    is where Banque's « Entrées » stat lands."""
    since = report.covered_since
    if since is None or not (report.till_before_statement or report.bank_before_till):
        return ""
    if window.end is not None and window.end < since:
        return ""
    return _income_page_url(DateRange(since, window.end), False)


def _balance_reason(reason: str) -> str:
    """Why « Ventes carte pas encore versées » has no balance, as this tenant
    can act on it. NO_CARD_DAYS names the command that re-reads the till's
    exports - a command on the server, reading the till the server imports:
    the owner's (recipes/integration.py). Another tenant is told « à
    configurer » rather than handed a command it cannot run."""
    if reason == income.NO_CARD_DAYS and not till_allowed():
        return income.NO_CARD_DAYS_TO_CONFIGURE
    return reason


def _bank_period(month: str, window: DateRange) -> tuple[DateRange, bool]:
    """The period « Banque » is showing, as the pages with a default one read
    it: a chosen month as its first and last day, the free dates as they
    are, and nothing chosen - every month - as `tout`. Those pages open on
    the last twelve months by themselves, so a bare link to one read a year
    where Banque was showing a month, or everything.

    One definition for both header links (« Dépenses par catégorie »,
    « Entrées d'argent »): the second carried the period while the first,
    right beside it, did not."""
    if month:
        year, number = (int(part) for part in month.split("-"))
        last = calendar.monthrange(year, number)[1]
        return DateRange(date(year, number, 1), date(year, number, last)), False
    return window, not window


def _bank_income_url(month: str, window: DateRange) -> str:
    """« Entrées d'argent » over the period « Banque » is showing
    (`_bank_period`); the stat linking there counts all of it."""
    return _income_page_url(*_bank_period(month, window))


def _month_label(day: date) -> str:
    """« Juillet 2026 »: `LANGUAGE_CODE` is en-us, so Django's own month
    names would print « July » in a French page."""
    return f"{MONTH_NAMES[day.month - 1].capitalize()} {day.year}"


def _kind_chips(report, window: DateRange, showing_all: bool) -> list[dict]:
    """« Toutes » and the three kinds, each with how many there are and where
    it is shown.

    Every one of them is drawn, a count of 0 included: these say what the
    whole window holds, and « Classées à la main 0 » is an answer where a
    chip that vanished is a page that looks like it lost a control. The
    counts are the work list's, so what the pie leaves out does not move
    them; the links keep it all the same.
    """
    counts = report.counts
    chips = [{"key": "", "label": "Toutes", "count": counts[""]}]
    chips += [{"key": key, "label": label, "count": counts[key]} for key, label in spending.KINDS.items()]
    for chip in chips:
        chip["active"] = chip["key"] == report.kind
        chip["url"] = _spending_url(window, showing_all, chip["key"], report.left_out)
    return chips


def _build_spending_pie_svg(report) -> str:
    """The report's slices as an inline SVG pie - server-rendered, like the
    recipe's cost pie and the price history (`recipes/views.py::
    _build_ingredient_pie_svg`): there is no charting library here, and the
    only thing one would add is the hover, which static/js/charts.js already
    does for all three.

    Drawn from `report.slices` and nothing else, so the wedges and the table
    beside them cannot disagree. Rendered with |safe, so every name that goes
    into it is escaped here - a category is free text somebody typed, and
    the names left out reach the label too: a pie read aloud without them
    is a pie of everything.
    """
    if not report.slices or report.drawn_total <= 0:
        return ""
    label = "Répartition des dépenses par catégorie"
    if report.left_out:
        label += f", sans : {', '.join(report.left_out)}"

    size, radius = 220, 100
    centre = size / 2
    wedges, legend = [], []
    start = -math.pi / 2  # 12 o'clock
    for index, piece in enumerate(report.slices):
        fraction = float(piece.amount / report.drawn_total)
        # « Autres » says how many it holds, or it is a wedge nobody can
        # account for. The table beside the pie still lists each of them.
        held = f" ({piece.held} catégorie{'s' if piece.held > 1 else ''})" if piece.held else ""
        name = escape(f"{piece.name}{held}")
        value = f"{format_money(piece.amount)} € · {piece.share:.1f} %"
        common = f'data-index="{index}" data-label="{name}" data-value="{value}" data-color="{piece.color}"'
        if len(report.slices) == 1:
            # One slice is the whole circle: as an arc its two ends coincide
            # and the path collapses to nothing.
            wedges.append(
                f'<circle class="chart-slice" cx="{centre}" cy="{centre}" r="{radius}" fill="{piece.color}" {common} />'
            )
        else:
            end = start + fraction * 2 * math.pi
            x1, y1 = centre + radius * math.cos(start), centre + radius * math.sin(start)
            x2, y2 = centre + radius * math.cos(end), centre + radius * math.sin(end)
            large = 1 if fraction > 0.5 else 0
            wedges.append(
                f'<path class="chart-slice" d="M{centre},{centre} L{x1:.1f},{y1:.1f} '
                f'A{radius},{radius} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{piece.color}" {common} />'
            )
            start = end
        legend.append(
            f'<span class="chart-legend-item" data-legend-for="{index}">'
            f'<span class="swatch" style="background:{piece.color};"></span>{name}'
            f' <span class="muted">{piece.share:.1f} %</span></span>'
        )
    return (
        '<div class="chart chart-pie" data-chart="pie" style="max-width:260px;">'
        f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="{escape(label)}">'
        f"{''.join(wedges)}</svg>"
        '<div class="chart-tooltip" data-chart-tooltip></div>'
        f'<div class="chart-legend">{"".join(legend)}</div>'
        "</div>"
    )


def _build_balance_svg(points, label: str = "Ventes carte pas encore versées") -> str:
    """The running balance as an inline SVG line - the shape of
    `inventory/views.py::_build_price_history_svg`, hovered by
    static/js/charts.js like it, with a ZERO line: the balance is signed,
    and « above or below nothing owed » is the whole reading.

    `points` is [(date, Decimal)], oldest first. Nothing is drawn from fewer
    than two - a line needs two ends - and the figures are in the table
    under it, since a chart nobody can read a number off is decoration.
    Rendered with |safe: the label is escaped here, and every other text in
    it is a date or a number this function formats.
    """
    if len(points) < 2:
        return ""
    width, height = 640, 220
    pad_left, pad_right, pad_top, pad_bottom = 80, 20, 20, 30
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    days = [day for day, _value in points]
    amounts = [value for _day, value in points]
    # The axis always holds zero. Floats for the geometry only: every figure
    # a reader sees is printed from the Decimals.
    bottom = min(min(amounts), Decimal("0"))
    top = max(max(amounts), Decimal("0"))
    low, high = float(bottom), float(top)
    if low == high:
        low, high = low - 1, high + 1
    first, last = days[0], days[-1]
    span = (last - first).days or 1

    def x_for(day):
        return pad_left + (day - first).days / span * plot_w

    def y_for(value):
        return pad_top + (1 - (float(value) - low) / (high - low)) * plot_h

    coords = [(x_for(day), y_for(value)) for day, value in points]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    zero = y_for(0)
    # Zero, then the top and the bottom - each only where it does not print
    # over one already there: a balance dipping a few euros below nothing
    # put « -12.00 € » on top of « 0.00 € ».
    kept: list[tuple[float, Decimal]] = []
    for y, value in ((zero + 4, Decimal("0")), (pad_top + 4, top), (height - pad_bottom, bottom)):
        if all(abs(y - other) >= 12 for other, _value in kept):
            kept.append((y, value))
    ticks = "".join(
        f'<text x="4" y="{y:.1f}" font-size="11" fill="var(--muted)">{format_money(value)} €</text>'
        for y, value in sorted(kept)
    )
    dots = "".join(
        f'<circle class="chart-point" cx="{x:.1f}" cy="{y:.1f}" r="3" fill="var(--amber)" '
        f'data-x="{x:.1f}" data-y="{y:.1f}" data-label="{day:%d/%m/%Y}" data-value="{format_money(value)} €" />'
        for (x, y), (day, value) in zip(coords, points)
    )
    return (
        f'<div class="chart" data-chart="line" data-plot="{pad_left},{pad_top},{plot_w},{plot_h}">'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
        f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{height - pad_bottom}" stroke="var(--border)" />'
        f'<line x1="{pad_left}" y1="{zero:.1f}" x2="{width - pad_right}" y2="{zero:.1f}" '
        f'stroke="var(--muted)" stroke-dasharray="4 3" />'
        f"{ticks}"
        f'<text x="{pad_left}" y="{height - 8}" font-size="11" fill="var(--muted)">{first:%d/%m/%Y}</text>'
        f'<text x="{width - pad_right}" y="{height - 8}" font-size="11" fill="var(--muted)" '
        f'text-anchor="end">{last:%d/%m/%Y}</text>'
        f'<polyline points="{line}" fill="none" stroke="var(--amber)" stroke-width="2" />'
        f'<line class="chart-hover-line" x1="0" y1="{pad_top}" x2="0" y2="{height - pad_bottom}" />'
        f'<circle class="chart-hover-dot" cx="0" cy="0" r="5" />'
        f"{dots}"
        "</svg>"
        '<div class="chart-tooltip" data-chart-tooltip></div>'
        "</div>"
    )


def _moved_out_of_view(request, line: BankTransaction) -> str:
    """Where a line went, when naming it took it out of the list it was
    named from.

    « Dépenses » lists the debits one way of naming at a time, so a line
    classified from inside « À classer » leaves that list the instant it is
    classified. Said, or it vanishes from under the reader's pointer with
    nothing on screen to explain it - the trap the stock page's own
    « classer » answers with a note and an undo.
    """
    chosen = parse_qs(urlparse(request.POST.get("next", "")).query).get(spending.KIND_PARAM, [""])[0]
    if chosen not in spending.KINDS:
        return ""
    now = spending.kind_of(line)
    if now == chosen:
        return ""
    return f" Elle passe dans « {spending.KINDS[now]} »."


#: A pk no row has, reversed in its place by `_by_pk`.
URL_PLACEHOLDER = "987654321987654321"


def _by_pk(name: str):
    """`reverse(name, args=[pk])` as a function of `pk`, from one reverse().

    An `<int:pk>` address differs from one pk to the next by its digits
    alone, so it is reversed once on a placeholder and the pk written in its
    place - the same characters `reverse` gives. Should the placeholder not
    be found exactly once, every pk is reversed. Made for one request: the
    script prefix `reverse` reads is the request's."""
    before, found, after = reverse(name, args=[int(URL_PLACEHOLDER)]).partition(URL_PLACEHOLDER)
    if not found or URL_PLACEHOLDER in after:
        return lambda pk: reverse(name, args=[pk])
    return lambda pk: f"{before}{pk}{after}"


def _choice_label(invoice: Invoice, total: Decimal) -> str:
    """An invoice as the pick-list's option reads it - « supplier n° number
    · date · amount € » - in the characters the template printed it in from
    its five variables (`localize` is what a template applies to each, the
    filters are the template's own), escaped by it once as a whole.

    One invoice is offered to every line near its date, so it is worded
    once for the page: a long tab printed thousands of these options."""
    number = localize(invoice.invoice_number or invoice.pk)
    day = date_filter(invoice.invoice_date, "d/m/Y") or "sans date"
    return f"{localize(invoice.supplier.name)} n° {number} · {day} · {money(total)} €"


def _back(request, default: str = "bank:bank_home") -> str:
    """The page the action was made from (`next`) when it is a path of this
    site (`common.safe_next`: « abc » was a 500, audit LB-5), `default`
    otherwise."""
    return safe_next(request, reverse(default))


def _months() -> list[tuple[str, str]]:
    """("2026-07", "Juillet 2026") for every month with an operation, newest first."""
    return [
        (f"{day:%Y-%m}", _month_label(day))
        for day in BankTransaction.objects.dates("operation_date", "month", order="DESC")
    ]


def _stats(by_status, payee_count: int) -> dict:
    """Counts and sums, added in Python - SQLite's own sums are not exact
    decimal (see StockType.current_value_ht)."""

    def total(rows):
        return sum((row.line.amount_due for row in rows), Decimal("0"))

    todo, linked, no_invoice = by_status[TODO], by_status[LINKED], by_status[NO_INVOICE]
    # Deliberately not named `spending`: that is the module this file imports,
    # and the same local one function above 500'd the rules page.
    debits = todo + linked + no_invoice
    # Named for what they are, not `income`, for the same reason.
    credits = by_status[INCOME]
    return {
        "spending_count": len(debits),
        "spending_total": total(debits),
        # What came in over the same window - the « Entrées » tab's rows,
        # added up the way they are printed (a credit is positive).
        "income_count": len(credits),
        "income_total": sum((row.line.amount for row in credits), Decimal("0")),
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
            "toutes": len(debits),
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


def _rule_matches(regex, spending, paid: set[int]) -> RuleMatches:
    """What `regex` - `rules.searcher`'s - catches among `spending`, each
    label searched under the time limits a page applies (`rules.caught`):
    stopped on the way, it is `slow` and counts nothing. `paid` holds the
    pks of the lines that pay an invoice."""
    found = caught(regex, spending)
    if found is None:
        return RuleMatches(slow=True)
    return RuleMatches(
        count=len(found),
        total=sum((line.amount_due for line in found), Decimal("0")),
        linked=sum(1 for line in found if line.pk in paid),
        examples=found[:MAX_EXAMPLES],
    )


def _fill(rows, with_choices: bool = True) -> None:
    """Links for every row with payments (`Row.payments`, set by the caller)
    - what they add up to and what else pays them; for the ones still
    missing an invoice, the matching's suggestion and the unpaid invoices a
    person may pick from, all from one load of invoices. « Propositions »
    wants the suggestions alone (`with_choices=False`): it offers what the
    matching found, and the pick-list is the bank page's."""
    # What the links read - each invoice's supplier, its lines for its
    # total, and the other lines paying it (`paid_with`, with their bank
    # line) - in three queries for the whole page rather than per invoice
    # (see « N+1s hide in per-object properties »), and for these rows only:
    # the page reads every line's payments for its counts, and draws a tab of
    # them. Lists (`to_attr`), not the managers: those clone a queryset per
    # invoice.
    prefetch_related_objects(
        [payment for row in rows for payment in row.payments],
        "invoice__supplier",
        Prefetch("invoice__lines", queryset=reconcile.total_lines(), to_attr="line_list"),
        Prefetch(
            "invoice__payments",
            queryset=InvoicePayment.objects.select_related("transaction"),
            to_attr="paid_with",
        ),
    )
    for row in rows:
        row.links = [
            Link(
                payment,
                reconcile.rounded_total(payment.invoice),
                [other.transaction for other in payment.invoice.paid_with if other.transaction_id != row.line.pk],
            )
            for payment in row.payments
        ]
        if row.links:
            row.gap = Gap(
                sum((link.total for link in row.links), Decimal("0")),
                row.line.amount_due,
                any(link.others for link in row.links),
            )
    # The pick-list is built for a row that already has an invoice too: a
    # debit settling two deliveries is one of the things this page is for,
    # and losing the list at the moment the first invoice goes on left the
    # second one reachable only by typing a search - which needs a word the
    # reader has to know. The SUGGESTION stays for open rows alone: it is
    # the matching's answer to « which invoice is this », and that question
    # is answered once somebody has answered it.
    pick_rows = [row for row in rows if row.is_open or row.status == LINKED]
    if not pick_rows:
        return
    open_rows = any(row.is_open for row in pick_rows)

    start, _end = reconcile.search_window([row.line for row in pick_rows])
    end = max(row.line.paid_on for row in pick_rows) + timedelta(days=7)
    invoices = list(reconcile.unpaid_invoices(start, end))
    by_pk = {invoice.pk: invoice for invoice in invoices}
    totals = {invoice.pk: reconcile.rounded_total(invoice) for invoice in invoices}
    # Only the suggestion needs these, and a tab of settled rows has none to
    # make: `supplier_naming` is two queries for nothing there.
    candidates = (
        [
            matching.InvoiceCandidate(invoice.pk, invoice.supplier_id, invoice.invoice_date, totals[invoice.pk])
            for invoice in invoices
        ]
        if open_rows
        else []
    )
    naming = reconcile.supplier_naming() if open_rows else {}
    # Each invoice's option, worded once for the page (`_choice_label`).
    worded: dict[int, tuple[str, str]] = {}
    if with_choices:
        # The invoices by date, for each row to slice its window out of
        # rather than test every one: years of invoices paid in cash, never
        # on the bank, made that rows x invoices. `position` is the order of
        # `unpaid_invoices`, which the sort kept between equals - it still
        # decides between them, so the same fifteen come out in the same
        # order.
        position = {invoice.pk: index for index, invoice in enumerate(invoices)}
        undated = [invoice for invoice in invoices if invoice.invoice_date is None]
        dated = sorted(
            (invoice for invoice in invoices if invoice.invoice_date is not None),
            key=lambda invoice: invoice.invoice_date,
        )
        days = [invoice.invoice_date for invoice in dated]

    for row in pick_rows:
        due = row.line.amount_due
        if row.is_open:
            row.suggestion = matching.match(reconcile.payment_of(row.line), candidates, naming)
        if row.suggestion is not None:
            for option in row.suggestion.options:
                option_total = sum((candidate.total for candidate in option), Decimal("0"))
                row.options.append(
                    Option([by_pk[candidate.pk] for candidate in option], option_total, due - option_total)
                )
        if not with_choices:
            continue
        first, last = reconcile.choices_window(row.line)
        near = undated + dated[bisect.bisect_left(days, first) : bisect.bisect_right(days, last)]
        paid_on = row.line.paid_on
        best = heapq.nsmallest(
            MAX_CHOICES,
            near,
            key=lambda invoice: (
                abs(totals[invoice.pk] - due),
                abs((invoice.invoice_date - paid_on).days) if invoice.invoice_date else UNDATED_LAST,
                position[invoice.pk],
            ),
        )
        for invoice in best:
            if invoice.pk not in worded:
                worded[invoice.pk] = (localize(invoice.pk), _choice_label(invoice, totals[invoice.pk]))
            row.choices.append(worded[invoice.pk])
        # A list cut in silence is a document the reader concludes is not
        # there. Said, the answer is « narrow the dates », which is a
        # control the page already has.
        row.more_choices = max(len(near) - MAX_CHOICES, 0)


def _search(rows, request) -> None:
    """The row a person typed a search on, filled with what it found.

    Beside the suggestions, never instead of them: they are what links a
    document in one click and they are built from the amount, the date and
    the payee. This answers the other half - the document whose amount does
    not match, that is months away, or that another line already pays - and
    it searches the DATABASE (`invoices.workspace.documents_matching`, the
    same supplier / number / date / amount search « Achats » has), not the
    rows the page happens to be showing.

    An invoice already paid is found too, marked with what pays it: that is
    exactly the invoice settled in two goes, and hiding it would leave the
    person with no way to say so.
    """
    line_id = request.GET.get("ligne", "")
    query = request.GET.get("recherche", "").strip()
    if not query or not is_id(line_id):
        return
    row = next((row for row in rows if row.line.pk == int(line_id) and row.status != INCOME), None)
    if row is None:
        return
    row.search = query
    row.found, row.more_found = _found_for(row.line, query)


def _found_for(line: BankTransaction, query: str) -> tuple[list[Found], int]:
    """What `query` finds for one line, and how many the cap left out.

    One rule for both ways in: the search box types into the fragment
    (`invoice_search`), and the same box without JavaScript reloads the page
    (`_search`). Written twice, the two would answer differently the day one
    of them learned something.
    """
    found = documents_matching(
        Invoice.objects.select_related("supplier").prefetch_related("lines", "payments__transaction"), query
    )
    # How many the cap left out, counted rather than guessed: « 12 de plus »
    # tells a reader to narrow the search, « quelques-unes » does not. One
    # extra COUNT, and only on a row somebody typed a search on.
    more = max(found.count() - MAX_FOUND, 0)
    results = []
    for invoice in found.order_by("-invoice_date", "-pk")[:MAX_FOUND]:
        payments = list(invoice.payments.all())
        results.append(
            Found(
                invoice,
                reconcile.rounded_total(invoice),
                here=any(payment.transaction_id == line.pk for payment in payments),
                elsewhere=[payment.transaction for payment in payments if payment.transaction_id != line.pk],
            )
        )
    return results, more


def invoice_search(request, pk):
    """The documents a search finds for one bank line, and nothing else.

    A fragment rather than the page: the box used to be a GET on « Banque »,
    which rebuilt every row, every tab and every figure to change one cell -
    and scrolled the reader back to the top, to hunt for the line they were
    already on. Swapped into the row instead, the page does not move.

    The plain GET is deliberately still there underneath, for a browser with
    no JavaScript; both come through `_found_for`, so they cannot drift.
    """
    line = get_object_or_404(BankTransaction, pk=pk)
    query = request.GET.get("recherche", "").strip()
    # An entry of money settles no invoice (see bank_line_action), and no box
    # is drawn on one: a search on it is stale or crafted, and answering it
    # with a list of linkable documents would be an invitation to a link the
    # POST would then refuse.
    if not query or line.amount >= 0:
        query, found, more = "", [], 0
    else:
        found, more = _found_for(line, query)
    return render(
        request,
        "bank/_found_invoices.html",
        {
            "line": line,
            "search": query,
            "found": found,
            "more_found": more,
            # Where « Rattacher » comes back to, validated against this host
            # by `_back` exactly as a posted one is.
            "page_url": _back(request),
        },
    )


# -- « Trésorerie » ------------------------------------------------------------------------------------------------
#
# Banque's fourth tab: the account's balance on every day, from the balances a
# person typed (« points ») and the operations imported. bank/treasury.py has
# every rule of reading; these views read the request, call it, and say in
# French what happened. Each write runs inside ONE transaction that reads the
# treasury again first (SQLite's IMMEDIATE mode takes the write lock as it
# opens): a page drawn before another tab wrote, or a double click, is
# refused rather than acted on twice.

#: Where a treasury message is said: each carries the section its redirect
#: lands on (extra_tags) and is printed there - said at the top, two screens
#: above « Écarts à résoudre », nobody saw it (CLAUDE.md, « Combler les
#: écarts »). A message whose section is not drawn is said at the top.
SAISIR, ECARTS, POINTS, AJUSTEMENTS = "saisir", "ecarts", "points", "ajustements"
TREASURY_PLACES = (SAISIR, ECARTS, POINTS, AJUSTEMENTS)
#: « Remplacer »: the balance of a date that already has one replaced - never
#: without this button.
REPLACE = "remplacer"
#: « Dater ce point du … »: a point moved to the day before its own.
EVE = "veille"
#: The adjustment POST's fields: the two points and the gap the page showed.
BEFORE_PARAM, AFTER_PARAM, GAP_PARAM = "avant", "apres", "ecart"
#: `?date=` - the « Corriger » links prefilling the form - and the day
#: « Dater ce point du … » posts.
DATE_PARAM = "date"
POINT_GONE = "Ce point n'existe plus."
ADJUSTMENT_GONE = "Cet ajustement n'existe plus."
PAIR_GONE = "Un des deux points n'existe plus : rien n'a été ajouté."
PAIR_SPLIT = "Un autre point est maintenant entre ces deux-là : rien n'a été ajouté."
GAP_NOT_FOUND = "Écart introuvable : rien n'a été ajouté."
PAIR_AGREES = "Ces deux points concordent déjà : rien n'a été ajouté."
PAIR_PENDING = "Ces deux points attendent un relevé à importer : rien n'a été ajouté."
GAP_CHANGED = "L'écart a changé depuis l'affichage : rien n'a été ajouté."
GAP_TOO_BIG = "Écart trop grand pour un ajustement : corrigez un des points. Rien n'a été ajouté."
POINT_CHANGED = "Ce point a changé depuis l'affichage : rien n'a changé."
#: « Dater ce point du … » from a page drawn before the gap changed (an
#: import, an adjustment, a point corrected): the point is as it was, the
#: hint no longer holds.
MOVE_CHANGED = "L'écart a changé depuis l'affichage : rien n'a changé."
#: A gap as the page posts it back (`GapCard.posted`): « -50.00 ».
POSTED_GAP = re.compile(r"-?[0-9]{1,13}\.[0-9]{2}")


@dataclass
class TreasuryEnd:
    """One of a gap's two points, on its card: its « Corriger » link, and
    where its « Supprimer » posts with what it asks first."""

    point: treasury.Point
    correct_url: str
    action_url: str
    confirm: str


@dataclass
class AdjustmentRow:
    """An adjustment, in a gap's card or in « Ajustements »: whether it
    counts (between two points) and its « Supprimer »."""

    correction: treasury.Correction
    counts: bool
    #: « +50.00 », as a sentence says it.
    signed: str
    action_url: str
    confirm: str


@dataclass
class GapCard:
    """A gap to resolve, as « Écarts à résoudre » draws it: what the
    operations explain, the hints that apply, and the actions of
    bank/treasury.py's « Resolving a gap »."""

    gap: treasury.Gap
    said: str
    hints: list[str]
    ends: list[TreasuryEnd]
    #: Its counted adjustments, each with its « Supprimer » - and every one
    #: its suspect hint names (`_gap_card`), oldest first, each once.
    adjustments: list[AdjustmentRow]
    #: What « Ajouter un ajustement de … » adds, « +50.00 », and what its
    #: form posts back as the gap shown, « 50.00 » (`POSTED_GAP`).
    signed: str
    posted: str
    #: « Dater ce point du … », offered only where the POST would take it.
    move_target: date | None = None
    move_url: str = ""
    move_confirm: str = ""


@dataclass
class PointRow:
    """A balance typed, as « Soldes saisis » draws it: what it says beside
    the previous one (`state`: "", "agrees", "pending" or "to_resolve")."""

    point: treasury.Point
    state: str
    said: str
    correct_url: str
    action_url: str
    confirm: str


def _gaps_to_resolve(count: int) -> str:
    """« 1 écart à résoudre », « 3 écarts à résoudre »."""
    return f"{count} écart{'s' if count > 1 else ''} à résoudre"


def _signed(amount: Decimal) -> str:
    """« +50.00 », « -1 234.56 »: an adjustment or a gap, as a sentence says it."""
    return format_money(amount, "+.2f")


def _more_or_less(gap: treasury.Gap) -> str:
    """« 50.00 € de plus », « 12.30 € de moins » - the account against what
    the operations explain, in the words `Gap` (a bank line's) uses."""
    return f"{format_money(abs(gap.missing))} € {'de plus' if gap.missing > 0 else 'de moins'}"


def _left_outside(orphans, *, future: bool) -> str:
    """« 1 ajustement ne comptera plus. » - the adjustments a delete or a
    move leaves outside every pair (bank/treasury.py, « orphans »)."""
    if not orphans:
        return ""
    count = len(orphans)
    many = count > 1
    if future:
        return f" {count} ajustement{'s' if many else ''} ne compter{'ont' if many else 'a'} plus."
    return f" {count} ajustement{'s' if many else ''} ne compte{'nt' if many else ''} plus (voir « Ajustements »)."


def _point_of_day(current: treasury.Treasury, day: date | None) -> treasury.Point | None:
    """The point typed for `day`, among those `current` holds."""
    return next((point for point in current.points if point.day == day), None) if day is not None else None


def _sections_drawn(current: treasury.Treasury) -> set[str]:
    """The sections the page draws for this treasury - where a message can
    be said, and a redirect land."""
    drawn = {SAISIR}
    if current.to_resolve:
        drawn.add(ECARTS)
    if current.points:
        drawn.add(POINTS)
    if current.corrections or current.orphans:
        drawn.add(AJUSTEMENTS)
    return drawn


def _treasury_messages(request, drawn: set[str]) -> dict[str, list]:
    """{place: messages} - "top" the top of the page, each section its own
    (its tag among the message's extra_tags), one whose section is not drawn
    (its last adjustment deleted) at the top. Read once, which also marks
    them said."""
    said: dict[str, list] = {"top": [], **{place: [] for place in TREASURY_PLACES}}
    for message in get_messages(request):
        tags = (message.extra_tags or "").split()
        said[next((tag for tag in TREASURY_PLACES if tag in tags and tag in drawn), "top")].append(message)
    return said


def _treasury_back(request) -> str:
    """The page a treasury form was posted from - its window kept (`next`) -
    without its anchor: each answer picks the section it lands on."""
    return _back(request, "bank:treasury").split("#")[0]


def _treasury_said(request, back: str, text: str, place: str, *, orphans_left: bool = False):
    """One message per write, said where the redirect lands: in « Écarts à
    résoudre », a warning, when a gap is to resolve once it is done; in the
    section acted on otherwise (the top, where that section is no longer
    drawn). `orphans_left` adds the adjustments still counting nowhere."""
    current = treasury.load()
    if orphans_left and current.orphans:
        count = len(current.orphans)
        text += f" {count} ajustement{'s' if count > 1 else ''} ne compte{'nt' if count > 1 else ''} toujours pas."
    gaps = len(current.to_resolve)
    if gaps:
        messages.warning(request, f"{text} {_gaps_to_resolve(gaps)}.", extra_tags=ECARTS)
        return redirect(f"{back}#{ECARTS}")
    messages.success(request, text, extra_tags=place)
    return redirect(f"{back}#{place}" if place in _sections_drawn(current) else back)


def treasury_home(request):
    """« Trésorerie », Banque's fourth tab: the account's balance on every
    day, from the balances typed and the operations imported
    (bank/treasury.py).

    The window - `?du=&au=`, the last twelve months by default, `?tout=1` -
    narrows the curve and the months alone: the headline, the gaps, the
    points and the adjustments are the whole history. A POST saves a point
    (`action` absent, « enregistrer » or « remplacer »; anything else is
    refused): refused, the page is drawn again with the form's errors (200)
    and its window; saved, one redirect to « Saisir un solde » - or to
    « Écarts à résoudre », with a warning, when the balance disagrees with a
    neighbour. A date that already has a balance is never replaced without
    « Remplacer » (`_save_point`), and « Remplacer » is offered with that
    balance said - a refusal of anything else on such a date included."""
    asked = date_range(request)
    showing_all = request.GET.get(ALL_PARAM) == "1"
    window = DateRange() if showing_all else (asked or last_twelve_months())
    here = _treasury_page_url(asked, showing_all)
    today = timezone.localdate()
    if request.method == "POST":
        action = request.POST.get(RULE_ACTION, SAVE)
        if action not in (SAVE, REPLACE):
            messages.error(request, UNKNOWN_RULE_ACTION, extra_tags=SAISIR)
            return redirect(f"{here}#{SAISIR}")
        form = TreasuryPointForm(request.POST, today=today)
        if form.is_valid():
            answer = _save_point(request, form, here, replace=action == REPLACE)
            if answer is not None:
                return answer
        current = treasury.load()
        # « Remplacer » beside a date that has a balance - the one refused
        # as taken, or a refusal of something else on such a date. Never
        # without « Le JJ/MM/AAAA a déjà un solde : X € » (review C7): a stale
        # tab refused for a typo offered « Remplacer » and never said what
        # that date holds, and the typo fixed replaced it. Not when
        # « Remplacer » was pressed already (« Corriger », then a typo): the
        # person knows, and the date is not what is wrong.
        day = form.taken_day or form.cleaned_data.get("date")
        taken = _point_of_day(current, day)
        if taken is not None and form.taken_day is None and action != REPLACE:
            form.refuse_taken(taken.day, taken.balance)
        form.replacing = taken is not None
    else:
        current = treasury.load()
        form = _point_form(request, current, today)
    return render(
        request,
        "bank/treasury.html",
        _treasury_context(request, current, form, asked=asked, showing_all=showing_all, window=window, here=here),
    )


def _point_form(request, current: treasury.Treasury, today: date) -> TreasuryPointForm:
    """« Saisir un solde » as a GET draws it: today's date - or `?date=`,
    from a « Corriger » link, with that point's balance and « Remplacer »
    offered at once."""
    asked = read_date(request.GET.get(DATE_PARAM))
    point = _point_of_day(current, asked)
    initial = {"date": (asked or today).isoformat()}
    if point is not None:
        # Bare, as typed: an input's value is never grouped.
        initial["solde"] = f"{point.balance:.2f}"
    form = TreasuryPointForm(initial=initial, today=today)
    form.replacing = point is not None
    return form


def _point_on(day: date) -> TreasuryCheckpoint | None:
    """The point typed for `day`, if any - read inside the save's
    transaction."""
    return TreasuryCheckpoint.objects.filter(date=day).first()


def _save_point(request, form: TreasuryPointForm, here: str, *, replace: bool):
    """Save the point the form read, inside one transaction - or None, the
    page to draw again: the date already has another balance and
    « Remplacer » was not pressed (a phone tab left open for days posts a
    stale date), or another tab gave it one between the check and the write
    (the unique date's IntegrityError, never a 500). The same balance again
    is « déjà enregistré », and nothing is written."""
    day, balance = form.cleaned_data["date"], form.cleaned_data["solde"]
    with transaction.atomic():
        existing = _point_on(day)
        if existing is not None and existing.balance == balance:
            messages.info(
                request, f"Solde du {day:%d/%m/%Y} déjà enregistré : {format_money(balance)} €.", extra_tags=SAISIR
            )
            return redirect(f"{here}#{SAISIR}")
        if existing is not None and not replace:
            form.refuse_taken(day, existing.balance)
            return None
        replaced = existing.balance if existing is not None else None
        try:
            with transaction.atomic():
                if existing is None:
                    TreasuryCheckpoint.objects.create(date=day, balance=balance)
                else:
                    existing.balance = balance
                    existing.save(update_fields=["balance"])
        except IntegrityError:
            taken = TreasuryCheckpoint.objects.filter(date=day).first()
            form.refuse_taken(day, taken.balance if taken is not None else None)
            return None
    return _point_saved(request, here, day, balance, replaced)


def _point_saved(request, here: str, day: date, balance: Decimal, replaced: Decimal | None):
    """ONE message for a point saved: « Solde du … enregistré : X € », what
    it replaced, and « — concorde avec le point du … » when its gap with the
    previous one agrees - in « Saisir un solde ». When it leaves a gap to
    resolve beside it, a warning in « Écarts à résoudre » instead, where the
    redirect lands. A pending gap (« relevé à importer ») adds nothing."""
    current = treasury.load()
    previous = current.gap_ending_on(day)
    following = next((gap for gap in current.gaps if gap.before.day == day), None)
    unsettled = next((gap for gap in (previous, following) if gap is not None and gap.to_resolve), None)
    if unsettled is not None:
        other = unsettled.before if unsettled.after.day == day else unsettled.after
        messages.warning(
            request,
            f"Le solde du {day:%d/%m} ne concorde pas avec celui du {other.day:%d/%m} : à résoudre.",
            extra_tags=ECARTS,
        )
        return redirect(f"{here}#{ECARTS}")
    text = f"Solde du {day:%d/%m/%Y} enregistré : {format_money(balance)} €"
    if replaced is not None:
        text += f" (remplace {format_money(replaced)} €)"
    if previous is not None and previous.agrees and not previous.pending:
        text += f" — concorde avec le point du {previous.before.day:%d/%m}"
    messages.success(request, f"{text}.", extra_tags=SAISIR)
    return redirect(f"{here}#{SAISIR}")


def _treasury_context(request, current: treasury.Treasury, form, *, asked, showing_all, window, here) -> dict:
    """What treasury.html draws: every figure from `current`, every per-row
    address reversed once for the page (`_by_pk`)."""
    point_url, adjustment_url = _by_pk("bank:treasury_point"), _by_pk("bank:treasury_adjustment")
    base, fields = reverse("bank:treasury"), _window_fields(asked, showing_all)

    def correct_url(day: date) -> str:
        # « Corriger »: the page, its window kept, the form prefilled.
        return f"{base}?{urlencode([*fields, (DATE_PARAM, day.isoformat())])}#{SAISIR}"

    adjustments = _adjustment_rows(current, adjustment_url)
    by_pk = {row.correction.pk: row for row in adjustments}
    cards = [_gap_card(gap, current, correct_url, point_url, by_pk) for gap in current.to_resolve]
    # (« Septembre 2026 », the row), newest first.
    months = [(_month_label(row.month), row) for row in reversed(current.months(window))]
    latest = current.latest
    return {
        "said": _treasury_messages(request, _sections_drawn(current)),
        "point_form": form,
        "latest": latest,
        # Amber only where the headline's own point ends a gap to resolve: a
        # gap of last year must not light today's figure.
        "headline_warn": latest is not None and current.unsettled(latest.reference),
        "last_operation": current.last_operation,
        "complete_through": current.complete_through,
        "accounts": current.accounts,
        "has_lines": current.last_operation is not None,
        "gap_cards": cards,
        "gaps_to_resolve": _gaps_to_resolve(len(cards)),
        "point_rows": _point_rows(current, correct_url, point_url),
        "adjustment_rows": adjustments,
        "months": months,
        # Columns drawn only when a row has something in them. « Écart » by
        # the COUNT of gaps to resolve, never their sum: a suspect point's two
        # gaps cancel out, and summed the column vanished (review C10). A row
        # with one gap shows its figure, with several « N écarts » and their
        # sum when it is not 0.
        "months_adjusted": any(row.adjustments for _label, row in months),
        "months_gaps": any(row.gaps_to_resolve for _label, row in months),
        "chart_svg": _build_balance_svg(current.curve(window), label="Trésorerie"),
        "provisional": current.curve_provisional(window),
        "reason_max": REASON_MAX_LENGTH,
        "adjustment_add_url": reverse("bank:treasury_adjustment_add"),
        # The window, as « Entrées d'argent » draws it: what was typed for
        # the inputs, `window` what the curve and the months are over.
        "date_window": asked,
        "window": window,
        "is_default": not showing_all and not asked,
        "showing_all": showing_all,
        "all_url": _treasury_page_url(asked, True),
        "period_url": _treasury_page_url(asked, False),
        "clear_url": _treasury_page_url(DateRange(), False),
        # Where every form comes back to, its window kept.
        "page_url": here,
        # Banque's tabs (bank/_tabs.html): this one is the treasury.
        "bank_url": _page_url(DEFAULT_VIEW, "", window),
        "spending_url": _other_page_url("bank:spending_home", window),
        "income_url": _income_url(window),
        "treasury_url": here,
        "bank_tab": "treasury",
    }


def _adjustment_rows(current: treasury.Treasury, adjustment_url) -> list[AdjustmentRow]:
    """Every adjustment, newest first - counting or not (`orphans`)."""
    counted = {one.pk for one in current.corrections}
    rows = [
        AdjustmentRow(
            one,
            one.pk in counted,
            _signed(one.amount),
            adjustment_url(one.pk),
            f"Supprimer l'ajustement du {one.day:%d/%m/%Y} ?",
        )
        for one in [*current.corrections, *current.orphans]
    ]
    rows.sort(key=lambda row: (row.correction.day, row.correction.pk or 0), reverse=True)
    return rows


def _adjustments_named(corrections: list[treasury.Correction]) -> str:
    """« l'ajustement du 10/09 (+200.00 €) », « les ajustements du 05/09
    (+50.00 €) et du 12/09 (+20.00 €) »."""
    each = [f"du {one.day:%d/%m} ({_signed(one.amount)} €)" for one in corrections]
    if len(each) == 1:
        return f"l'ajustement {each[0]}"
    return f"les ajustements {', '.join(each[:-1])} et {each[-1]}"


def _gap_card(gap: treasury.Gap, current: treasury.Treasury, correct_url, point_url, adjustments) -> GapCard:
    """One card of « Écarts à résoudre ».

    A suspect point's hint is true of the point ALONE only when the counted
    adjustments between its two neighbours (`Treasury.around`) add up to
    0: the suspect is found on the raw gaps, so without the point its
    neighbours are those adjustments apart. Otherwise the hint names them
    and the card lists each with its « Supprimer » - one made on the
    point's other side, or dated on its own day, is outside this card's
    stretch and was listed on no card (review C2/C12)."""
    said = f"Les opérations expliquent {_signed(gap.operations)} €"
    if gap.adjusted:
        said += f" et les ajustements {_signed(gap.adjusted)} €"
    hints = []
    listed = list(gap.corrections)
    if gap.suspect is not None:
        around = current.around(gap.suspect)
        if sum((one.amount for one in around), Decimal("0")) == 0:
            hints.append(f"Sans le point du {gap.suspect.day:%d/%m}, les points voisins concordent.")
        else:
            hints.append(
                f"Sans le point du {gap.suspect.day:%d/%m} ni {_adjustments_named(around)}, "
                "les points voisins concordent."
            )
            # The card's own adjustments are among them: each listed once.
            by_pk = {one.pk: one for one in (*gap.corrections, *around)}
            listed = sorted(by_pk.values(), key=lambda one: (one.day, one.pk or 0))
    if gap.read_before is not None:
        hints.append(f"L'écart vaut les opérations du {gap.read_before.day:%d/%m} : solde lu avant elles ?")
    hints.append("Relevé manquant entre ces dates ? Importez-le.")
    ends = [
        TreasuryEnd(
            point,
            correct_url(point.day),
            point_url(point.pk),
            f"Supprimer le point du {point.day:%d/%m/%Y} ?"
            + _left_outside(current.orphaned_by_deleting(point.pk), future=True),
        )
        for point in (gap.before, gap.after)
    ]
    card = GapCard(
        gap,
        f"{said}, l'écart est de {_more_or_less(gap)}.",
        hints,
        ends,
        [adjustments[one.pk] for one in listed if one.pk in adjustments],
        _signed(gap.missing),
        f"{gap.missing:.2f}",
    )
    target, moving = gap.move_to, gap.read_before
    # Offered only where the POST would take it: no point that day, and a
    # day a statement can hold.
    if (
        target is not None
        and moving is not None
        and target >= statements.FIRST_DAY
        and _point_of_day(current, target) is None
    ):
        card.move_target = target
        card.move_url = point_url(moving.pk)
        orphans = current.orphaned_by_moving(moving.pk, target)
        if orphans:
            card.move_confirm = f"Dater ce point du {target:%d/%m/%Y} ?" + _left_outside(orphans, future=True)
    return card


def _point_rows(current: treasury.Treasury, correct_url, point_url) -> list[PointRow]:
    """« Soldes saisis », newest first, each with what it says beside the
    previous one: « concorde », the gap « à résoudre », « relevé à
    importer », or « — » for the first."""
    rows = []
    for point in reversed(current.points):
        gap = current.gap_ending_on(point.day)
        if gap is None:
            state, said = "", "—"
        elif gap.pending:
            state, said = "pending", "relevé à importer"
        elif gap.agrees:
            state, said = "agrees", "concorde"
        else:
            state, said = "to_resolve", f"{_more_or_less(gap)} : à résoudre"
        rows.append(
            PointRow(
                point,
                state,
                said,
                correct_url(point.day),
                point_url(point.pk),
                f"Supprimer le point du {point.day:%d/%m/%Y} ?"
                + _left_outside(current.orphaned_by_deleting(point.pk), future=True),
            )
        )
    return rows


def treasury_adjustment_add(request):
    """« Ajouter un ajustement de ±X € » on a gap to resolve: exactly its
    `missing`, dated on its later point's day, with an optional reason -
    after which the pair agrees.

    The POST trusts nothing: it names the two points (`avant`, `apres`, ids
    through `common.is_id`) and the gap the page showed (`ecart`); inside ONE
    transaction the treasury is read again and the adjustment refused,
    nothing written, when a point is gone, another point now sits between
    them, they agree already (a double click: the second is refused), the
    pair waits for a statement, the gap is no longer the one shown, or it is
    wider than an amount can be (refused, never truncated). A GET writes
    nothing."""
    back = _treasury_back(request)
    if request.method != "POST":
        return redirect(back)
    form = TreasuryAdjustmentForm(request.POST)
    gap = None
    if not form.is_valid():
        refused = str(form.errors["raison"][0])
    else:
        with transaction.atomic():
            refused, gap = _adjustment_refused(
                request.POST.get(BEFORE_PARAM), request.POST.get(AFTER_PARAM), request.POST.get(GAP_PARAM)
            )
            if gap is not None and not refused:
                adjustment = TreasuryAdjustment(
                    date=gap.after.day, amount=gap.missing, reason=form.cleaned_data["raison"]
                )
                try:
                    adjustment.clean()
                except ValidationError as error:
                    refused = f"{error.messages[0]} Rien n'a été ajouté."
                else:
                    adjustment.save()
    if refused or gap is None:
        messages.error(request, refused, extra_tags=ECARTS)
        return redirect(f"{back}#{ECARTS}")
    text = (
        f"Ajustement de {_signed(gap.missing)} € ajouté au {gap.after.day:%d/%m/%Y} : "
        f"les points du {gap.before.day:%d/%m} et du {gap.after.day:%d/%m} concordent."
    )
    return _treasury_said(request, back, text, AJUSTEMENTS)


def _adjustment_refused(before, after, shown) -> tuple[str, treasury.Gap | None]:
    """(why the adjustment POST is refused - "" when it is not -, the gap
    read again). Called inside the transaction that writes."""
    if not (is_id(before) and is_id(after)):
        return PAIR_GONE, None
    current = treasury.load()
    gap = current.gap_between(int(before), int(after))
    if gap is None:
        first, second = current.point(int(before)), current.point(int(after))
        if first is None or second is None:
            return PAIR_GONE, None
        return (PAIR_SPLIT if first.day < second.day else GAP_NOT_FOUND), None
    if gap.agrees:
        return PAIR_AGREES, gap
    if gap.pending:
        return PAIR_PENDING, gap
    if not isinstance(shown, str) or not POSTED_GAP.fullmatch(shown) or Decimal(shown) != gap.missing:
        return GAP_CHANGED, gap
    if gap.too_big:
        return GAP_TOO_BIG, gap
    return "", gap


def treasury_point(request, pk):
    """One point: « supprimer », or « veille » - « Dater ce point du … »,
    offered when the gap it ends equals its day's operations (read before
    them), and refused once that no longer holds (`_point_moved`). POST
    only, inside one transaction; a point gone is said, never a
    404 nor a 500. A delete or a move leaving adjustments outside every pair
    says so."""
    back = _treasury_back(request)
    if request.method != "POST":
        return redirect(back)
    action = request.POST.get(RULE_ACTION)
    place = ECARTS if action == EVE else POINTS
    if action not in (DELETE, EVE):
        messages.error(request, UNKNOWN_RULE_ACTION, extra_tags=place)
        return redirect(f"{back}#{place}")
    with transaction.atomic():
        point = TreasuryCheckpoint.objects.filter(pk=pk).first() if is_id(str(pk)) else None
        if point is None:
            text, refused = "", POINT_GONE
        elif action == DELETE:
            orphans = treasury.load().orphaned_by_deleting(point.pk)
            point.delete()
            text, refused = f"Point du {point.date:%d/%m/%Y} supprimé.{_left_outside(orphans, future=False)}", ""
        else:
            text, refused = _point_moved(point, request.POST.get(DATE_PARAM))
    if refused:
        messages.error(request, refused, extra_tags=place)
        return redirect(f"{back}#{place}")
    # Done, it is said where the point is listed - the card asked from is
    # gone when the pair now agrees.
    return _treasury_said(request, back, text, POINTS)


def _point_moved(point: TreasuryCheckpoint, posted) -> tuple[str, str]:
    """« Dater ce point du … »: the point moved to the day before its own -
    the day the page showed (`posted`), so a second click, or a page drawn
    before another tab moved it, moves nothing. Refused when that day has a
    point, or is before statements can exist - and unless the treasury,
    read again inside the transaction, still has a gap TO RESOLVE whose
    « read before » hint names this point and that day (`MOVE_CHANGED`):
    drawn before an import, an adjustment or a correction settled the pair,
    the page moved a right reading to the wrong day and made a gap of its
    own (review C6). (the message, the refusal)."""
    target = point.date - timedelta(days=1) if point.date > date.min else None
    if target is None or read_date(posted) != target:
        return "", POINT_CHANGED
    if target < statements.FIRST_DAY:
        return "", f"Le {target:%d/%m/%Y} est avant le {statements.FIRST_DAY:%d/%m/%Y} : rien n'a changé."
    taken = f"Le {target:%d/%m/%Y} a déjà un solde : rien n'a changé."
    if TreasuryCheckpoint.objects.filter(date=target).exists():
        return "", taken
    current = treasury.load()
    if not any(
        gap.read_before is not None and gap.read_before.pk == point.pk and gap.move_to == target
        for gap in current.to_resolve
    ):
        return "", MOVE_CHANGED
    orphans = current.orphaned_by_moving(point.pk, target)
    was = point.date
    point.date = target
    try:
        with transaction.atomic():
            point.save(update_fields=["date"])
    except IntegrityError:
        return "", taken
    return f"Point du {was:%d/%m/%Y} daté du {target:%d/%m/%Y}.{_left_outside(orphans, future=False)}", ""


def treasury_adjustment(request, pk):
    """One adjustment: « supprimer » - the fix when the real operation came
    in after it, and the way out of one that counts nowhere. POST only,
    inside one transaction; one gone is said, never a 404."""
    back = _treasury_back(request)
    if request.method != "POST":
        return redirect(back)
    if request.POST.get(RULE_ACTION) != DELETE:
        messages.error(request, UNKNOWN_RULE_ACTION, extra_tags=AJUSTEMENTS)
        return redirect(f"{back}#{AJUSTEMENTS}")
    with transaction.atomic():
        adjustment = TreasuryAdjustment.objects.filter(pk=pk).first() if is_id(str(pk)) else None
        if adjustment is not None:
            adjustment.delete()
    if adjustment is None:
        messages.error(request, ADJUSTMENT_GONE, extra_tags=AJUSTEMENTS)
        return redirect(f"{back}#{AJUSTEMENTS}")
    text = f"Ajustement de {_signed(adjustment.amount)} € du {adjustment.date:%d/%m/%Y} supprimé."
    return _treasury_said(request, back, text, AJUSTEMENTS, orphans_left=True)
