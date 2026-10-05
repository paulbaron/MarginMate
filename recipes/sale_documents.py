"""The sale documents as their pages list, search and compare them
(« Factures de vente » on « Recettes & ventes · Ventes »).

* `customer_fold` - how a document's customer is compared with another's:
  « Marges »' deposits counted twice (margins/computation.py) and the pages'
  doubts - never the bank's `customer_key`, which matches a statement's words
  against a customer.
* `listed` / `documents_matching` - the tab's list: windowed, searched by the
  database - a number, a customer, a line, a date as written, an amount -
  then cut at its most recent unless « tout afficher » (CLAUDE.md « A bounded
  list needs a search the database answers »).
* `known_customers` - the customers the typed form offers.
* `deposit_doubts`, `deposits_counted_twice`, `counted_twice` - the two
  doubts a deposit invoice raises, said, never repaired (spec §2.5): a
  « Acompte » with no final invoice months on, and a deposit counted that its
  final invoice deducts too.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Max, Prefetch, Q
from django.utils import timezone

from common import DateRange, search_key
from inventory.models import StockType

# Achats' reading of a typed search, its words, dates and amounts: one
# reading for the two lists of documents (invoices.workspace.documents_matching).
from invoices.workspace import _A_GROUPED_AMOUNT, _AN_AMOUNT, MAX_TERMS, a_date

from .models import CENTS, Recipe, SaleDocument, SaleDocumentLine, fallback_total_ttc

#: How many sale documents the tab draws before « tout afficher »: counted
#: over the window and the search before the cut, which the page says.
DOCUMENTS_PAGE_SIZE = 50
#: How many customers the typed form offers (the most recent).
KNOWN_CUSTOMERS = 300
#: A « Acompte » older than this with no final invoice of its customer: said.
DEPOSIT_WITHOUT_FINAL_DAYS = 90

#: The pill of a deposit with no final invoice on the tab, and its sentence
#: on its page.
NO_FINAL_INVOICE_PILL = "Sans facture finale"
NO_FINAL_INVOICE = (
    f"Acompte sans facture finale depuis plus de {DEPOSIT_WITHOUT_FINAL_DAYS} jours : si la vente n'a pas lieu, "
    "l'acompte gardé est une vente : choisissez « Compte dans les marges et le stock »."
)
#: A deposit counted, and its final invoice deducting it: the same sale twice.
COUNTED_TWICE = (
    "L'acompte {deposit} compte dans les marges, et la facture finale {final} le déduit de son total : "
    "la même vente compte deux fois. Marquez l'acompte « Acompte »."
)
#: Asked before a sale document is deleted, on the tab and on its page.
DELETE_QUESTION = "Supprimer la facture de vente {label} ? Son fichier est supprimé, ses règlements bancaires détachés."


def customer_fold(text: str) -> str:
    """A customer as two documents are compared by it: case, accents and
    spaces aside - « Mariage  Exemple » is « MARIAGE EXEMPLE »."""
    return " ".join(search_key(text or "").split())


def named(reference: str, day: date) -> str:
    """« n° F-7 du 20/03/2026 », or « du 20/03/2026 » without a number."""
    return (f"n° {reference} " if reference else "") + f"du {day:%d/%m/%Y}"


# -- the tab's list ---------------------------------------------------------------------------------------------------


@dataclass
class Listed:
    """The documents the tab draws, each with its `total`, its `doubt`
    (« Sans facture finale », else "") and its lines (`line_list`), and how
    many the window and the search hold before the cut."""

    documents: list[SaleDocument]
    found: int

    @property
    def hidden(self) -> int:
        return max(self.found - len(self.documents), 0)


def listed(window: DateRange, query: str = "", show_all: bool = False) -> Listed:
    """The window's sale documents (both ends included, on `sold_on`) the
    search `query` finds, the most recent first, cut at DOCUMENTS_PAGE_SIZE
    unless `show_all`. Counted before the cut, over the same window and
    search: « 3 factures » above a list of none reads as a broken page.

    The same number of queries for 2 documents and for 50: the count, the
    documents, their lines with what they are tied to (one prefetch), and
    the deposits' final invoices when an old « Acompte » is listed - each
    document's money read off its lines (`total_ttc_of`), never a query of
    its own."""
    documents = window.limit(SaleDocument.objects.all(), "sold_on")
    query = (query or "").strip()
    if query:
        documents = documents_matching(documents, query)
    found = documents.count()
    lines = Prefetch(
        "lines",
        queryset=SaleDocumentLine.objects.select_related("recipe", "stock_type").order_by("id"),
        to_attr="line_list",
    )
    ordered = documents.order_by("-sold_on", "-created_at").prefetch_related(lines)
    shown = list(ordered if show_all else ordered[:DOCUMENTS_PAGE_SIZE])
    doubts = deposit_doubts(shown)
    for document in shown:
        document.total = document.total_ttc_of(document.line_list)
        document.doubt = doubts.get(document.pk, "")
    return Listed(shown, found)


class _Names:
    """The customers, recipes and articles a search compares its words with,
    case and accents aside - read once for the whole search, and only when
    there is one. In Python, as Achats compares its suppliers: SQLite's
    `icontains` folds case but not accents, and « fetes » typed in a hurry
    has to find « Comité des Fêtes »."""

    def __init__(self):
        self._customers: list[str] | None = None
        self._recipes: list[tuple[int, str]] | None = None
        self._articles: list[tuple[int, str]] | None = None

    def customers(self, term: str) -> list[str]:
        if self._customers is None:
            self._customers = list(
                SaleDocument.objects.exclude(customer="").order_by().values_list("customer", flat=True).distinct()
            )
        wanted = search_key(term)
        return [customer for customer in self._customers if wanted in search_key(customer)]

    def recipes(self, term: str) -> list[int]:
        if self._recipes is None:
            self._recipes = list(Recipe.objects.order_by().values_list("pk", "name"))
        wanted = search_key(term)
        return [pk for pk, name in self._recipes if wanted in search_key(name)]

    def articles(self, term: str) -> list[int]:
        if self._articles is None:
            self._articles = list(StockType.objects.order_by().values_list("pk", "name"))
        wanted = search_key(term)
        return [pk for pk, name in self._articles if wanted in search_key(name)]


def documents_matching(documents, query: str):
    """The documents a typed search means - Achats' rule
    (invoices.workspace.documents_matching): each word matched against the
    number, the customer, the note, a line's label, the recipe or article a
    line is tied to, the date of sale as it is written (12/07/2026, 07/2026,
    2026) and a total the document states, the words ANDed - « Mariage
    2026 » is that customer's sales of 2026. Past MAX_TERMS words, the rest
    is dropped: that only widens the answer.

    The database answers, because the tab draws its most recent only."""
    query = _A_GROUPED_AMOUNT.sub(lambda amount: "".join(amount.group().split()), query)
    names = _Names()
    for term in query.split()[:MAX_TERMS]:
        matches = Q(reference__icontains=term) | Q(note__icontains=term) | Q(lines__label__icontains=term)
        customers = names.customers(term)
        if customers:
            matches |= Q(customer__in=customers)
        recipes = names.recipes(term)
        if recipes:
            matches |= Q(lines__recipe_id__in=recipes)
        articles = names.articles(term)
        if articles:
            matches |= Q(lines__stock_type_id__in=articles)
        day = a_date(term, "sold_on")
        if day:
            matches |= Q(**day)
        if _AN_AMOUNT.fullmatch(term):
            matches |= Q(stated_total_ttc=Decimal(term.replace(",", ".")))
        documents = documents.filter(matches)
    # A word found on two lines of one document is that document once.
    return documents.distinct()


def known_customers(limit: int = KNOWN_CUSTOMERS) -> list[str]:
    """The customers of earlier sale documents, the most recently sold to
    first, each once as it was written - what the typed form's « Client »
    offers (a <datalist>). One query."""
    rows = (
        SaleDocument.objects.exclude(customer="")
        .values("customer")
        .annotate(last=Max("sold_on"))
        .order_by("-last", "customer")[:limit]
    )
    return [row["customer"] for row in rows]


# -- the deposits' doubts ---------------------------------------------------------------------------------------------


def deposit_doubts(documents, today: date | None = None) -> dict[int, str]:
    """{pk: NO_FINAL_INVOICE} for each « Acompte » of `documents` older than
    DEPOSIT_WITHOUT_FINAL_DAYS for which no later document of its customer
    states an amount already paid (BT-113, « déjà réglé »): if the event did
    not take place, the deposit kept is a sale - which counts nowhere while
    it says « Acompte ». A deposit naming no customer has no final invoice
    that can be found, and is said too. Said, never changed alone.

    One query, and none unless such a deposit is listed."""
    today = today or timezone.localdate()
    limit = today - timedelta(days=DEPOSIT_WITHOUT_FINAL_DAYS)
    old = [
        document
        for document in documents
        if document.counting == SaleDocument.Counting.DEPOSIT and document.sold_on < limit
    ]
    if not old:
        return {}
    finals = list(
        SaleDocument.objects.filter(sold_on__gte=min(document.sold_on for document in old), prepaid_ttc__gt=0)
        .exclude(customer="")
        .order_by()
        .values_list("pk", "customer", "sold_on")
    )
    doubts = {}
    for deposit in old:
        customer = customer_fold(deposit.customer)
        if customer and any(
            pk != deposit.pk and day >= deposit.sold_on and customer_fold(theirs) == customer
            for pk, theirs, day in finals
        ):
            continue
        doubts[deposit.pk] = NO_FINAL_INVOICE
    return doubts


@dataclass(frozen=True)
class Deducted:
    """A document that counts, whose total a final invoice deducts as already
    paid: the deposit of `deposits_counted_twice`, by its number and day."""

    pk: int
    reference: str
    sold_on: date


def deposits_counted_twice(finals) -> list[tuple[Deducted, SaleDocument]]:
    """(deposit, final) for each deposit counted AND deducted by its final
    invoice: of `finals` - documents that count - each stating an amount
    already paid (BT-113) and a customer; a document that counts, of the
    same customer (`customer_fold`: case, accents and spaces aside), dated on
    or before it, whose total is that amount within a cent. A deposit invoice
    typed by hand, or written by a software as type 380, counts one sale
    twice in two months otherwise. Said, never repaired: the person marks the
    deposit « Acompte » (« Marges »' foot, the documents' pages).

    Two queries at most, and none unless such a final invoice is given: the
    documents that count with a customer up to the last of them, compared in
    Python - SQLite compares case for ASCII only and accents not at all, so
    `customer__iexact` missed « Événements » for « EVENEMENTS » -, then the
    lines of those whose total is their lines'."""
    finals = [
        document
        for document in finals
        if document.prepaid_ttc is not None and document.prepaid_ttc > 0 and customer_fold(document.customer)
    ]
    if not finals:
        return []
    customers = {customer_fold(final.customer) for final in finals}
    candidates = [
        row
        for row in SaleDocument.objects.filter(
            counting=SaleDocument.Counting.COUNTED, sold_on__lte=max(final.sold_on for final in finals)
        )
        .exclude(customer="")
        .order_by("sold_on", "pk")
        .values_list(
            "pk", "reference", "sold_on", "customer", "stated_total_ttc", "adjustment_ht", "adjustment_vat_rate"
        )
        if customer_fold(row[3]) in customers
    ]
    lines: dict[int, list[SaleDocumentLine]] = defaultdict(list)
    unstated = [pk for pk, _reference, _day, _customer, stated, _adjustment, _rate in candidates if stated is None]
    if unstated:
        for line in SaleDocumentLine.objects.filter(document_id__in=unstated).select_related("recipe").order_by():
            lines[line.document_id].append(line)
    pairs = []
    for final in sorted(finals, key=lambda document: (document.sold_on, document.pk)):
        customer = customer_fold(final.customer)
        for pk, reference, day, their_customer, stated, adjustment, rate in candidates:
            if pk == final.pk or day > final.sold_on or customer_fold(their_customer) != customer:
                continue
            total = stated if stated is not None else fallback_total_ttc(lines[pk], adjustment, rate)
            if abs(total - final.prepaid_ttc) <= CENTS:
                pairs.append((Deducted(pk, reference, day), final))
    return pairs


def counted_twice(document: SaleDocument) -> list[str]:
    """What a document's page says of it as a deposit counted that a final
    invoice deducts, or as the final invoice deducting one
    (`deposits_counted_twice`). Three queries at most - the final invoices
    of its customer from its day on, then that pair's two - and none for a
    document that counts in nothing or names no customer."""
    customer = customer_fold(document.customer)
    if document.pk is None or not document.counts or not customer:
        return []
    later = [
        other
        for other in SaleDocument.objects.filter(
            counting=SaleDocument.Counting.COUNTED, sold_on__gte=document.sold_on, prepaid_ttc__gt=0
        )
        .exclude(pk=document.pk)
        .exclude(customer="")
        if customer_fold(other.customer) == customer
    ]
    finals = ([document] if (document.prepaid_ttc or 0) > 0 else []) + later
    return [
        COUNTED_TWICE.format(
            deposit=named(deposit.reference, deposit.sold_on), final=named(final.reference, final.sold_on)
        )
        for deposit, final in deposits_counted_twice(finals)
        if document.pk in (deposit.pk, final.pk)
    ]
