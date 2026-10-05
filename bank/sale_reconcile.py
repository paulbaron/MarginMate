"""The database side of a credit paying a « facture de vente » (spec §6.2):
the automatic pass, the history that names a customer, the links a person
makes and takes off, and what Banque's « Entrées » tab and a document's
« Règlement » draw. bank/sale_matching.py decides, on plain values.

The debit side's rules (bank/reconcile.py, CLAUDE.md « Bank statements »),
kept for the credits:

* the pass links only what is SURE - one exact named invoice among EVERY
  invoice still due (a partly paid one included), paid by nothing yet, or
  one sum of one customer's - and withdraws what it took;
* a person's decision freezes the line: link and unlink set
  `settled_by_hand`, which the pass never revisits, and « Rapprocher
  automatiquement » hands it back;
* a credit a till rule, or a payer retained for a source the till compares,
  reads (a payout, a deposit: `is_recognised`) is never linked alone, and a
  credit's own « En caisse » choice is left alone;
* a payer is taught to a customer only by links that ADD UP, measured over
  everything joined by links - nothing stored, so unlinking forgets.

A link is `recipes.SaleDocumentPayment`, never `InvoicePayment`: written
here and by « Données », deleted here and by CASCADE. What a credit gives
each invoice is recipes/sale_payments.py's allocation, read once.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import NamedTuple

from django.db import transaction
from django.db.models import Prefetch, Q

from common import format_money
from invoices.workspace import _A_GROUPED_AMOUNT, _AN_AMOUNT, MAX_TERMS, a_date
from recipes.models import SaleDocument, SaleDocumentLine, SaleDocumentPayment
from recipes.sale_documents import documents_matching
from recipes.sale_payments import TOTAL_FIELDS, Allocation, LinkFact, read_links
from staff.models import Establishment

from . import income, matching, recognition, sale_matching
from .models import BankTransaction, IncomeSource

ZERO = Decimal("0")
#: The pick-list reaches a week further each way than the matching does,
#: as the debit side's (`reconcile.choices_window`).
WEEK = timedelta(days=7)
#: Banque's search of the sale documents, and a document's search of the
#: credits: how many each shows - the rest counted and said.
MAX_FOUND = 50
MAX_FOUND_CREDITS = 20
TIER_ORDER = {matching.SURE: 0, matching.NEAR_SURE: 1, matching.TO_CONFIRM: 2}

AUTO = SaleDocumentPayment.Method.AUTO
MANUAL = SaleDocumentPayment.Method.MANUAL


class SaleLinkRefused(ValueError):
    """A link this module will not write, with the French sentence to say."""


DEBIT_PAYS_NO_SALE = "Une dépense ne règle pas une facture de vente."
#: Said after a link on a credit holding its own « En caisse » choice: the
#: line's choice beats the sale (`income.reading_of`), so it still reads as
#: chosen - a person said what it is (bank critique 14).
OWN_CHOICE_KEPT = (
    "Elle reste comptée « {source} », choisi pour elle : choisissez « Automatique » sur « Entrées d'argent » pour "
    "la compter en facture de vente."
)
#: Why a credit the matching is sure of is only « quasi-sûre » on a page:
#: the pass would not act on it (CLAUDE.md « A SURE line is on the page in
#: two cases only »).
CAPPED_LINKED_ELSEWHERE = "Entrée déjà rattachée {documents} : jamais rattachée seule."
CAPPED_BY_HAND = "Entrée déliée à la main : à rattacher par vous."


def _refuse_a_debit(line: BankTransaction) -> None:
    if line.amount <= 0:
        raise SaleLinkRefused(DEBIT_PAYS_NO_SALE)


def is_recognised(line: BankTransaction, payers: dict[str, str], rules: recognition.Rules) -> bool:
    """A till rule reads it, or a payer retained for a source the till
    compares (bank critique 3: a terminal recognised by a payer « Carte » is
    a card payout) - never offered to a sale automatically, never read
    « Facture de vente » (income.reading_of)."""
    return income.automatic_source(line, rules) is not None or payers.get(income.payer_key(line)) in income.COMPARED


def open_credits():
    """The pass's credits: money in, no person's decision on it, paying no
    invoice yet, and no « En caisse » choice of its own - a decision the
    pass leaves alone."""
    return BankTransaction.objects.filter(amount__gt=0, settled_by_hand=False, sale_payments__isnull=True).exclude(
        income_source__in=income.CHOSEN
    )


def load_documents(start, end) -> list[SaleDocument]:
    """The sale documents dated from `start` to `end`, each with its lines
    in `line_list` - read for what `total_ttc` reads, nothing more: two
    queries. What each is paid comes from the allocation, never from a
    property reading `self.lines.all()` (a query a document)."""
    lines = Prefetch(
        "lines",
        queryset=SaleDocumentLine.objects.select_related("recipe").only(*TOTAL_FIELDS).order_by(),
        to_attr="line_list",
    )
    return list(
        SaleDocument.objects.filter(sold_on__gte=start, sold_on__lte=end)
        .order_by("sold_on", "pk")
        .prefetch_related(lines)
    )


def candidates_from(documents, allocation: Allocation) -> list[sale_matching.SaleCandidate]:
    """Every document of `documents` still asking something of the bank -
    whatever it counts in (a « Déjà comptée par la caisse » tab is paid by
    transfer too), fresh or partly paid: SURE needs the one exact to be the
    only one, so a partly paid invoice whose balance equals a credit stops
    the pass instead of hiding from it."""
    found = []
    for document in documents:
        due = allocation.due(document.pk, document.to_pay_of(document.line_list))
        if due <= 0:
            continue
        found.append(
            sale_matching.SaleCandidate(
                document.pk,
                document.customer,
                document.sold_on,
                due,
                not allocation.linked(document.pk),
                document.label,
            )
        )
    return found


def bar_words() -> frozenset[str]:
    """The words of the bar's own name - its sales e-invoices' seller, and
    the establishment the timesheets print: a payout prints it as its
    payee. Two queries; the establishment read, never made (`current()`
    writes)."""
    names = set(
        SaleDocument.objects.exclude(seller_name="").order_by().values_list("seller_name", flat=True).distinct()
    )
    names.update(Establishment.objects.filter(pk=Establishment.SINGLETON_PK).values_list("name", flat=True))
    return matching.supplier_words(*(name for name in names if name))


def _groups(facts) -> list[list[LinkFact]]:
    """The links, in groups of credits and documents joined by links - one
    transfer for two invoices, a deposit and its balance on one invoice."""
    parent: dict[tuple, tuple] = {}

    def root(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for fact in facts:
        credit, document = root(("credit", fact.credit_pk)), root(("document", fact.document_pk))
        if credit != document:
            parent[credit] = document
    groups: dict[tuple, list[LinkFact]] = defaultdict(list)
    for fact in facts:
        groups[root(("credit", fact.credit_pk))].append(fact)
    return list(groups.values())


def customer_naming(allocation: Allocation, customers, bar: frozenset[str]) -> dict[str, matching.Naming]:
    """{customer key: how the bank may name it} for `customers`: the words
    of its own name (sale_matching.customer_words), and the payers of the
    links that ADD UP - a group of credits and documents joined by links
    whose credits come to exactly what its documents ask teaches each of its
    credits' payer to each of its documents' customer (CLAUDE.md « An alias
    is taught only by a link that ADDS UP », measured over everything the
    line pays). One transfer for two invoices of a customer teaches; a part
    payment, or a mis-tick, does not. Nothing stored: unlinking forgets. No
    query: the allocation carries every column.

    **A key read off a label** (a credit with no payer printed: a cheque
    deposit, the bank's own line) is the same for every credit of that
    wording - « REMISE CHEQUES N » for every deposit -: taught to two
    customers it names neither, and it never makes a match SURE
    (sale_matching.match). A payer the bank prints is kept whoever it was
    taught to."""
    learnt: dict[str, set[str]] = defaultdict(set)
    label_keys: dict[str, set[str]] = defaultdict(set)
    for group in _groups(allocation.facts):
        credits = {fact.credit_pk: fact for fact in group}
        documents = {fact.document_pk: fact for fact in group}
        paid = sum((fact.credit_amount for fact in credits.values()), ZERO)
        if paid != sum((fact.to_pay for fact in documents.values()), ZERO):
            continue
        keys = {income.payer_key_of(fact.counterparty, fact.label) for fact in credits.values()} - {""}
        from_labels = {
            income.payer_key_of(fact.counterparty, fact.label)
            for fact in credits.values()
            if not (fact.counterparty or "").strip()
        } - {""}
        for fact in documents.values():
            if fact.customer:
                customer = sale_matching.customer_key(fact.customer)
                learnt[customer] |= keys
                for key in from_labels:
                    label_keys[key].add(customer)
    shared = {key for key, taught_to in label_keys.items() if len(taught_to) > 1}
    naming = {}
    for customer in customers:
        if not customer:
            continue
        key = sale_matching.customer_key(customer)
        naming[key] = matching.Naming(
            sale_matching.customer_words(customer, bar), frozenset(learnt.get(key, set()) - shared)
        )
    return naming


def credit_of(line: BankTransaction, *, recognised: bool, amount: Decimal | None = None) -> sale_matching.Credit:
    """`line` as the matching sees it - its whole amount, or `amount` (what
    the allocation leaves of it)."""
    return sale_matching.Credit(
        operation_date=line.operation_date,
        counterparty=line.counterparty or "",
        label=line.label or "",
        amount=line.amount if amount is None else amount,
        recognised=recognised,
    )


@transaction.atomic
def reconcile_sales(rules: recognition.Rules | None = None) -> list[tuple[BankTransaction, list[SaleDocument]]]:
    """The automatic pass (D6): every open credit nothing recognises,
    oldest first, linked AUTO where the matching is SURE - and the documents
    it took withdrawn, so the pass never pays one twice. Returns what it
    linked. It never sets `settled_by_hand`. `rules` are the rules an import
    read already, once for every file of its POST (`recognition.load`
    otherwise)."""
    rules = recognition.load() if rules is None else rules
    payers = income.known_payers()
    credits = [line for line in open_credits() if not is_recognised(line, payers, rules)]
    if not credits:
        return []
    links = read_links()
    days = [line.operation_date for line in credits]
    documents = load_documents(min(days) - sale_matching.LATE_PAYMENT_WINDOW, max(days) + sale_matching.ADVANCE_WINDOW)
    candidates = candidates_from(documents, links)
    if not candidates:
        return []
    naming = customer_naming(links, {candidate.customer for candidate in candidates}, bar_words())
    by_pk = {document.pk: document for document in documents}
    linked = []
    for line in sorted(credits, key=lambda one: (one.operation_date, one.pk)):
        found = sale_matching.match(credit_of(line, recognised=False), candidates, naming)
        if found is None or not found.confident:
            continue
        (option,) = found.options
        SaleDocumentPayment.objects.bulk_create(
            [SaleDocumentPayment(transaction=line, document_id=candidate.pk, method=AUTO) for candidate in option]
        )
        taken = {candidate.pk for candidate in option}
        candidates = [candidate for candidate in candidates if candidate.pk not in taken]
        linked.append((line, [by_pk[candidate.pk] for candidate in option]))
    return linked


@transaction.atomic
def link(line: BankTransaction, documents) -> str:
    """A person says `line` paid `documents` - whatever they ask, and
    whoever else pays them (`reconcile.link`'s rule: the allocation and the
    gap then say what it adds up to). MANUAL, `settled_by_hand`; nothing
    else of the line touched. Returns what to say of the line's own « En
    caisse » choice, which still beats the sale - "" when it has none, or
    when every document is a « Déjà comptée par la caisse » one, which never
    reads as a sale."""
    _refuse_a_debit(line)
    documents = list(documents)
    for document in documents:
        SaleDocumentPayment.objects.get_or_create(transaction=line, document=document, defaults={"method": MANUAL})
    line.settled_by_hand = True
    line.save(update_fields=["settled_by_hand"])
    sold = any(document.counting != SaleDocument.Counting.TILL for document in documents)
    if line.income_source in income.CHOSEN and sold:
        return OWN_CHOICE_KEPT.format(source=IncomeSource(line.income_source).label)
    return ""


@transaction.atomic
def unlink_document(line: BankTransaction, document: SaleDocument) -> bool:
    """One invoice off `line`, its others left; False when it did not pay it
    (a page left open, a second click). The line stays decided even with
    nothing left on it (CLAUDE.md « One invoice comes off at a time »)."""
    _refuse_a_debit(line)
    removed, _details = SaleDocumentPayment.objects.filter(transaction=line, document=document).delete()
    line.settled_by_hand = True
    line.save(update_fields=["settled_by_hand"])
    return bool(removed)


@transaction.atomic
def unlink_as_shown(line: BankTransaction, shown: set[int]) -> bool:
    """Every invoice off `line` if it pays exactly `shown` - those its row
    was drawn with -, else nothing written and False: a stale « Tout
    délier » takes off what its row showed, or nothing (CLAUDE.md « A stale
    « Délier » »). Checked inside this IMMEDIATE transaction."""
    _refuse_a_debit(line)
    links = SaleDocumentPayment.objects.filter(transaction=line)
    if not shown or set(links.values_list("document_id", flat=True)) != shown:
        return False
    links.delete()
    line.settled_by_hand = True
    line.save(update_fields=["settled_by_hand"])
    return True


@transaction.atomic
def reopen(line: BankTransaction) -> bool:
    """Hand `line` back to the automatic pass - unless it was linked
    meanwhile (another tab, « Données »): False, nothing written. The caller
    then runs `reconcile_sales`."""
    _refuse_a_debit(line)
    if SaleDocumentPayment.objects.filter(transaction=line).exists():
        return False
    line.settled_by_hand = False
    line.save(update_fields=["settled_by_hand"])
    return True


class CreditOffer(NamedTuple):
    """A credit that could pay a document, as its « Règlement » proposes it:
    the match narrowed to the option(s) holding the document - a sum of
    invoices carries all of them -, and the amount matched (what the
    allocation leaves of the credit)."""

    line: BankTransaction
    match: matching.Match
    amount: Decimal


def _capped(found: matching.Match, options, line: BankTransaction, allocation: Allocation, document_pk: int | None):
    """`found` narrowed to `options` - capped at NEAR_SURE, with its reason,
    where the pass would not act: a credit already paying another invoice
    than `document_pk`, or unlinked by hand."""
    elsewhere = [fact.number for fact in allocation.of_credit(line.pk) if fact.document_pk != document_pk]
    why = ""
    if elsewhere:
        named = (
            f"à la facture de vente {elsewhere[0]}"
            if len(elsewhere) == 1
            else f"aux factures de vente {', '.join(elsewhere)}"
        )
        why = CAPPED_LINKED_ELSEWHERE.format(documents=named)
    elif line.settled_by_hand:
        why = CAPPED_BY_HAND
    if found.confident and why:
        narrowed = matching.Match(options, False, found.reason, matching.NEAR_SURE, why)
    else:
        narrowed = matching.Match(options, found.confident, found.reason, found.tier, why or found.tier_reason)
    narrowed.days_back = found.days_back
    return narrowed


def credits_for(document: SaleDocument, allocation: Allocation | None = None) -> list[CreditOffer]:
    """The credits that could pay `document` (its page's « Entrées
    possibles », bank critique 5): dated from ADVANCE_WINDOW before its day
    to LATE_PAYMENT_WINDOW after, money in, not linked to it already, with
    no « En caisse » choice of their own, nothing recognising them - each
    matched on what the allocation LEAVES of it (a credit with nothing left
    is dropped) against the window's documents, and kept when one of its
    options holds this document. SURE first, then by distance in days; at
    most MAX_OPTIONS."""
    allocation = read_links() if allocation is None else allocation
    start = document.sold_on - sale_matching.ADVANCE_WINDOW
    end = document.sold_on + sale_matching.LATE_PAYMENT_WINDOW
    rules = recognition.load()
    payers = income.known_payers()
    lines = [
        line
        for line in BankTransaction.objects.filter(amount__gt=0, operation_date__gte=start, operation_date__lte=end)
        .exclude(income_source__in=income.CHOSEN)
        .exclude(sale_payments__document=document)
        .order_by("operation_date", "pk")
        if not is_recognised(line, payers, rules)
    ]
    if not lines:
        return []
    days = [line.operation_date for line in lines]
    documents = load_documents(min(days) - sale_matching.LATE_PAYMENT_WINDOW, max(days) + sale_matching.ADVANCE_WINDOW)
    candidates = candidates_from(documents, allocation)
    if not any(candidate.pk == document.pk for candidate in candidates):
        return []
    naming = customer_naming(allocation, {candidate.customer for candidate in candidates}, bar_words())
    offers = []
    for line in lines:
        amount = allocation.left.get(line.pk, line.amount)
        if amount <= 0:
            continue
        found = sale_matching.match(credit_of(line, recognised=False, amount=amount), candidates, naming)
        if found is None:
            continue
        options = [option for option in found.options if any(candidate.pk == document.pk for candidate in option)]
        if options:
            offers.append(CreditOffer(line, _capped(found, options, line, allocation, document.pk), amount))
    offers.sort(
        key=lambda offer: (
            TIER_ORDER[offer.match.tier],
            abs((offer.line.operation_date - document.sold_on).days),
            offer.line.pk,
        )
    )
    return offers[: sale_matching.MAX_OPTIONS]


def credits_matching(query: str) -> tuple[list[BankTransaction], int]:
    """The credits a typed search means (a document's « Chercher une
    entrée »): each word ANDed, matched against the payer and the label, an
    amount (equal to the credit's) or a date as written (12/06/2026,
    06/2026, 2026) - the one reading of a typed search
    (invoices.workspace). The most recent MAX_FOUND_CREDITS, and how many
    more: two queries."""
    query = _A_GROUPED_AMOUNT.sub(lambda amount: "".join(amount.group().split()), query or "")
    terms = query.split()[:MAX_TERMS]
    if not terms:
        return [], 0
    found = BankTransaction.objects.filter(amount__gt=0)
    for term in terms:
        matches = Q(counterparty__icontains=term) | Q(label__icontains=term)
        day = a_date(term, "operation_date")
        if day:
            matches |= Q(**day)
        if _AN_AMOUNT.fullmatch(term):
            matches |= Q(amount=Decimal(term.replace(",", ".")))
        found = found.filter(matches)
    total = found.count()
    shown = list(found.order_by("-operation_date", "-pk")[:MAX_FOUND_CREDITS])
    return shown, max(total - len(shown), 0)


@dataclass
class FoundCredit:
    """A credit a document's search found, and what already holds it."""

    line: BankTransaction
    #: It pays this document already.
    here: bool
    #: The other documents it pays.
    elsewhere: list[LinkFact] = field(default_factory=list)
    #: Its own « En caisse » choice, in words - "" for none.
    chosen: str = ""


def credits_found_for(document: SaleDocument, query: str, allocation: Allocation) -> tuple[list[FoundCredit], int]:
    """`credits_matching(query)`, each credit marked for `document`'s page."""
    lines, more = credits_matching(query)
    found = []
    for line in lines:
        links = allocation.of_credit(line.pk)
        chosen = str(IncomeSource(line.income_source).label) if line.income_source in income.CHOSEN else ""
        found.append(
            FoundCredit(
                line,
                any(fact.document_pk == document.pk for fact in links),
                [fact for fact in links if fact.document_pk != document.pk],
                chosen,
            )
        )
    return found, more


@dataclass
class FoundSale:
    """A sale document Banque's search found for a credit, and what pays it."""

    document: SaleDocument
    to_pay: Decimal
    due: Decimal
    #: This credit pays it already.
    here: bool
    #: The other credits paying it.
    elsewhere: list[LinkFact] = field(default_factory=list)
    #: Its page (set by the view; drawn for « Recettes & ventes » only).
    url: str = ""


def documents_found_for(
    line: BankTransaction, query: str, allocation: Allocation | None = None
) -> tuple[list[FoundSale], int]:
    """The sale documents a typed search means (recipes.sale_documents's
    search: a number, a customer, a line, a date, an amount), the most
    recent MAX_FOUND, each marked for `line`: already paid by it, or by
    another credit - found all the same: an invoice settled in two goes is
    exactly the one a person must be able to say so of."""
    query = (query or "").strip()
    if not query:
        return [], 0
    allocation = read_links() if allocation is None else allocation
    found = documents_matching(SaleDocument.objects.all(), query)
    more = max(found.count() - MAX_FOUND, 0)
    lines = Prefetch(
        "lines",
        queryset=SaleDocumentLine.objects.select_related("recipe").only(*TOTAL_FIELDS).order_by(),
        to_attr="line_list",
    )
    results = []
    for document in found.order_by("-sold_on", "-pk").prefetch_related(lines)[:MAX_FOUND]:
        to_pay = document.to_pay_of(document.line_list)
        links = allocation.of_document(document.pk)
        results.append(
            FoundSale(
                document,
                to_pay,
                allocation.due(document.pk, to_pay),
                any(fact.credit_pk == line.pk for fact in links),
                [fact for fact in links if fact.credit_pk != line.pk],
            )
        )
    return results, more


@dataclass
class SaleLink:
    """A sales invoice a credit pays, on Banque's row: what the credit gives
    it, and the other credits paying it."""

    fact: LinkFact
    share: Decimal
    others: list[LinkFact] = field(default_factory=list)
    #: Its page (set by the view; drawn for « Recettes & ventes » only).
    url: str = ""


@dataclass(frozen=True)
class SaleGap:
    """What a credit's invoices take of it, beside what it is - said, never
    repaired: the part that goes to no invoice, and whether an invoice is
    paid by another credit too."""

    amount: Decimal
    given: Decimal
    left: Decimal
    shared: bool = False


@dataclass
class SaleOption:
    """One option of a credit's suggestion: the invoice(s) it would pay, and
    the other rows proposing one of them (`views._share_sales`)."""

    candidates: tuple[sale_matching.SaleCandidate, ...]
    total: Decimal
    also_for: list = field(default_factory=list)


def _choices(line: BankTransaction, amount: Decimal, candidates, linked: set[int], limit: int):
    """The pick-list: the documents still due dated in the matching's window
    a week wider each way, nearest first by (|due − amount|, days apart,
    position) - `views._fill`'s shape - and how many it had no room for."""
    first = line.operation_date - sale_matching.LATE_PAYMENT_WINDOW - WEEK
    last = line.operation_date + sale_matching.ADVANCE_WINDOW + WEEK
    near = [
        (position, candidate)
        for position, candidate in enumerate(candidates)
        if first <= candidate.sold_on <= last and candidate.pk not in linked
    ]
    best = heapq.nsmallest(
        limit,
        near,
        key=lambda pair: (
            abs(pair[1].due - amount),
            abs((pair[1].sold_on - line.operation_date).days),
            pair[0],
        ),
    )
    choices = [
        (str(candidate.pk), f"{candidate.label} · {format_money(candidate.due)} €") for _position, candidate in best
    ]
    return choices, max(len(near) - limit, 0)


def _read_by_the_till(row, payers, rules) -> bool:
    """`is_recognised`, from the row's entry when Banque read it already
    (`income.entry_for`, its `how` and `rule`): asked again, every till rule
    would run twice over the tab's credits, and a rule's time is counted
    over the reading (recognition.RULE_SECONDS) - found « trop lent » twice
    as soon. A line's own choice hides whether a rule reads it: asked."""
    entry = getattr(row, "entry", None)
    if entry is None or entry.how == income.BY_LINE:
        return is_recognised(row.line, payers, rules)
    if entry.how == income.BY_RULE:
        return bool(entry.rule)
    return entry.how == income.BY_PAYER and entry.source in income.COMPARED


def fill_credit_rows(rows, allocation: Allocation, payers, rules, *, asked: str = "", choices: int = 15) -> None:
    """Banque's « Entrées » rows (`views.Row`, credits): the invoices each
    pays with its share, what it gives none (`sale_gap`), a suggestion where
    it pays nothing, nothing recognises it and it holds no « En caisse »
    choice of its own (a person said what it is - bank critique 14), and the
    pick-list - drawn on such a credit, and on a recognised or chosen one
    only when it is the row `asked` for (« Rattacher une facture de vente »:
    a payout a day is a form a day otherwise, bank critique 8).

    The documents near the rows (two queries) and the bar's own names (two)
    - with `read_links`, six queries whatever the statement holds."""
    rows = list(rows)
    if not rows:
        return
    days = [row.line.operation_date for row in rows]
    documents = load_documents(
        min(days) - sale_matching.LATE_PAYMENT_WINDOW - WEEK, max(days) + sale_matching.ADVANCE_WINDOW + WEEK
    )
    candidates = candidates_from(documents, allocation)
    naming = customer_naming(allocation, {candidate.customer for candidate in candidates}, bar_words())
    for row in rows:
        line = row.line
        facts = allocation.of_credit(line.pk)
        row.sale_links = [
            SaleLink(
                fact,
                allocation.share(line.pk, fact.document_pk),
                [other for other in allocation.of_document(fact.document_pk) if other.credit_pk != line.pk],
            )
            for fact in facts
        ]
        if facts:
            row.sale_gap = SaleGap(
                line.amount,
                sum((link.share for link in row.sale_links), ZERO),
                allocation.left.get(line.pk, ZERO),
                any(link.others for link in row.sale_links),
            )
        free = line.income_source not in income.CHOSEN and not _read_by_the_till(row, payers, rules)
        if free and not facts:
            found = sale_matching.match(credit_of(line, recognised=False), candidates, naming)
            if found is not None:
                # Unlinked by hand, the pass never comes back to it: never
                # « certaine » here either - the document's « Règlement »
                # says the same (`credits_for`).
                found = _capped(found, found.options, line, allocation, None)
                row.sale_suggestion = found
                row.sale_options = [
                    SaleOption(option, sum((candidate.due for candidate in option), ZERO)) for option in found.options
                ]
        if free or asked == str(line.pk):
            left = allocation.left.get(line.pk, line.amount)
            row.sale_choices, row.more_sale_choices = _choices(
                line, left if left > 0 else line.amount, candidates, {fact.document_pk for fact in facts}, choices
            )
            row.sale_pick = True
