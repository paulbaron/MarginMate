"""Which « facture de vente » a credit of the statement paid - by the
numbers first (spec §6.1). Plain values in, plain values out - no database:
bank/sale_reconcile.py reads them.

The debit side's rules (bank/matching.py), the other way round:

* **the amount**, to the cent, of what the invoice still asks - its total,
  less what earlier credits gave it (recipes/sale_payments.py);
* **the date**: a customer pays an invoice after it is dated - months after,
  sometimes - or before, a deposit for an event the invoice is dated on;
* **the payer** the bank prints naming the customer: a word of the
  customer's own name, or a payer an earlier link that ADDED UP taught
  (sale_reconcile.customer_naming). Never a civility, a public body, an
  event word or the bar's own name: matching.GENERIC_WORDS is a supplier
  list, and « Mme … » or « Mairie de … » would name every customer so
  called.

All three, in exactly ONE way - one invoice among every invoice still due,
paid by nothing yet, or one sum of one customer's invoices - is « certaine »:
what the automatic pass links. The tier is `matching.Match`'s, derived from
`confident` and never set beside it, so no rule here can widen the pass
without saying so. Anything short of it is a suggestion with its reason.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from common import format_money

from . import matching

#: A customer pays an invoice after it is dated - a month, often more: the
#: debit side's reach for a late payment.
LATE_PAYMENT_WINDOW = matching.LATER_PAYMENT_WINDOW
#: ... or BEFORE it is dated: a deposit or a payment in advance for an event,
#: the invoice dated on the event's day.
ADVANCE_WINDOW = timedelta(days=90)
#: One transfer for several invoices of one customer: at most this many,
#: among that customer's most recent invoices nothing pays yet.
MAX_DOCUMENTS_PER_CREDIT = 4
MAX_DOCUMENTS_SEARCHED = 12
#: A total rebuilt by hand, a rounding: a few cents, never another invoice.
NEAR_SURE_GAP = matching.NEAR_SURE_GAP
MAX_OPTIONS = 5

#: Words that name no customer (bank critique 4): matching.GENERIC_WORDS is a
#: SUPPLIER list (legal forms, places, the bank's own vocabulary) - it has no
#: civility, public body or event word, so « Mme Exemplaire » was named by
#: every « MME AUTREFOIS » and « Mairie de Villexemple » by any other town
#: hall. Upper-case ASCII, as matching.words gives them.
CUSTOMER_GENERIC_WORDS = frozenset(
    {
        "MME",
        "MMES",
        "MADAME",
        "MESDAMES",
        "MONSIEUR",
        "MESSIEURS",
        "MLLE",
        "MADEMOISELLE",
        "FAMILLE",
        "MAIRIE",
        "COMMUNE",
        "VILLE",
        "DEPARTEMENT",
        "REGION",
        "CONSEIL",
        "PREFECTURE",
        "ASSOCIATION",
        "ASSO",
        "COMITE",
        "CLUB",
        "AMICALE",
        "FEDERATION",
        "SYNDICAT",
        "ECOLE",
        "COLLEGE",
        "LYCEE",
        "UNIVERSITE",
        "ENTREPRISE",
        "GROUPE",
        "SERVICE",
        "SERVICES",
        "EVENEMENT",
        "EVENEMENTS",
        "EVENEMENTIEL",
        "EVENT",
        "EVENTS",
        "MARIAGE",
        "ANNIVERSAIRE",
        "SOIREE",
        "FETE",
        "RECEPTION",
        "SEMINAIRE",
        "RECU",
        "REGLEMENT",
        "ACOMPTE",
        "SOLDE",
    }
)

#: Said on a credit whose payer is read off its label (no payer printed):
#: never SURE, whatever history taught.
NAMED_BY_THE_LABEL = "Payeur reconnu par le libellé seul (remise, ligne de la banque) : à confirmer."

#: Each tier's rule, for the page to state (Banque's « Entrées » tab) -
#: built from the constants, so the words cannot drift from the thresholds.
TIER_RULES = (
    (
        matching.SURE,
        "Certaine",
        (
            "Montant exact au centime de ce qui reste à recevoir sur une facture qu'aucune entrée ne règle encore - "
            "et aucune autre facture de ce client n'attend ce montant -, client nommé par la banque (par son nom, ou "
            f"par un rattachement passé qui tombait juste), facture datée de {LATE_PAYMENT_WINDOW.days} jours avant à "
            f"{ADVANCE_WINDOW.days} jours après l'entrée, et une seule façon d'y arriver (une facture, ou une seule "
            "somme de factures d'un même client) : le rapprochement la rattache de lui-même."
        ),
    ),
    (
        matching.NEAR_SURE,
        "Quasi-sûre",
        (
            "Un client nommé, une seule facture : le solde exact d'une facture déjà réglée en partie, ou un écart de "
            f"{format_money(NEAR_SURE_GAP)} € au plus - ou une entrée certaine que le rapprochement ne prendra pas "
            "seul (déliée à la main, déjà rattachée à une autre facture, ou une entrée dont la banque n'imprime pas "
            "le payeur : remise de chèques, ligne de la banque). Jamais rattachée seule."
        ),
    ),
    (
        matching.TO_CONFIRM,
        "À confirmer",
        (
            "Le reste : un acompte possible, plusieurs factures possibles, le même montant chez un payeur que la "
            "banque ne nomme pas comme ce client. Rien n'est rattaché seul."
        ),
    ),
)


@dataclass(frozen=True)
class SaleCandidate:
    """A sale document still to be paid, as the matching sees it."""

    pk: int
    #: As written on the document ("" for none: never named).
    customer: str
    sold_on: date
    #: Still to be received (recipes.sale_payments.Allocation.due), > 0.
    due: Decimal
    #: No credit is linked to it at all.
    fresh: bool
    #: « n° X · Client · JJ/MM/AAAA » (SaleDocument.label).
    label: str


@dataclass(frozen=True)
class Credit:
    """A credit of the statement, as the matching sees it."""

    operation_date: date
    counterparty: str
    label: str
    #: What is matched: the whole credit, or what the allocation leaves of it.
    amount: Decimal
    #: A till rule, or a payer retained for a COMPARED source, says what it
    #: is (a payout, a deposit): never offered an invoice on its amount alone.
    recognised: bool = False

    @property
    def payee(self) -> str:
        return matching.payee_of(self.counterparty.strip(), self.label)

    @property
    def payee_from_label(self) -> bool:
        """The bank printed no payer: the label stands in for one, and names a
        customer through a payer taught by history only (a cheque deposit,
        the bank's own line - `matching.names_supplier`'s `alias_only`)."""
        return not self.counterparty.strip()


def customer_key(customer: str) -> str:
    """A customer as the history keys it: its words (matching.alias_key)."""
    return matching.alias_key(customer)


def customer_words(customer: str, bar_words: frozenset[str]) -> frozenset[str]:
    """The words that may name `customer`: matching.supplier_words of it,
    less CUSTOMER_GENERIC_WORDS and the words of the bar's own name - a
    payout prints the bar's name as its payee (CLAUDE.md « Order, and
    nothing else »), so a customer « Comptoir Événements » sharing a word
    with a bar « Le Comptoir » was named by every payout nobody recognised."""
    return frozenset(matching.supplier_words(customer) - CUSTOMER_GENERIC_WORDS - bar_words)


def _days(candidate: SaleCandidate, credit: Credit) -> int:
    return abs((candidate.sold_on - credit.operation_date).days)


def _by_distance(candidates, credit: Credit) -> list[SaleCandidate]:
    return sorted(candidates, key=lambda candidate: (_days(candidate, credit), candidate.pk))


def _sure(option, reason: str, credit: Credit) -> matching.Match:
    """`matching.Match`, whose tier is derived from `confident` - its options
    are sale documents here, as matching's are invoices. `days_back` is how
    far the option's nearest document is from the credit."""
    found = matching.Match([option], True, reason)
    found.days_back = min(_days(candidate, credit) for candidate in option)
    return found


def _question(options, reason: str, credit: Credit, tier: str = matching.TO_CONFIRM) -> matching.Match:
    kept = list(options)[:MAX_OPTIONS]
    found = matching.Match(kept, False, reason, tier)
    found.days_back = min(_days(candidate, credit) for candidate in kept[0])
    return found


def match(credit: Credit, candidates, naming: dict[str, matching.Naming]) -> matching.Match | None:
    """The document(s) `credit` paid, among `candidates` - EVERY document
    still due, fresh or partly paid -, or None. `naming` is
    {customer_key: matching.Naming} (sale_reconcile.customer_naming).

    First rule that answers:

    1. nothing for nothing, nor for money out;
    2. the window: dated from LATE_PAYMENT_WINDOW before the credit to
       ADVANCE_WINDOW after it, something still due;
    3. named: the payer names the customer (a word of its own name, or a
       payer taught by history - a blank counterparty through history only);
    4. exact and named: one, paid by nothing yet - SURE; one paid in part -
       its exact balance, NEAR_SURE; several - TO_CONFIRM;
    5. none exact: one sum of one customer's fresh invoices - SURE; several -
       TO_CONFIRM;
    6. a few cents off, named: one - NEAR_SURE; several - TO_CONFIRM;
    7. named, asking more than the credit: a deposit possible - TO_CONFIRM;
    8. a credit nothing recognises: the same amount from a payer the bank
       does not name as the customer - TO_CONFIRM, never linked (a bar
       issues few sales invoices, unlike the debits' months of them)."""
    amount = credit.amount
    if amount <= 0:
        return None
    first, last = credit.operation_date - LATE_PAYMENT_WINDOW, credit.operation_date + ADVANCE_WINDOW
    in_window = [candidate for candidate in candidates if candidate.due > 0 and first <= candidate.sold_on <= last]
    payee, alias_only = credit.payee, credit.payee_from_label
    named_cache: dict[str, bool] = {}

    def is_named(candidate: SaleCandidate) -> bool:
        if not candidate.customer:
            return False
        key = customer_key(candidate.customer)
        if key not in named_cache:
            known = naming.get(key, matching.NO_NAMING)
            named_cache[key] = matching.names_supplier(payee, known, alias_only=alias_only)
        return named_cache[key]

    named = [candidate for candidate in in_window if is_named(candidate)]

    exact = [candidate for candidate in named if candidate.due == amount]
    if len(exact) == 1:
        (only,) = exact
        if only.fresh and alias_only:
            # A label key (no payer printed) is the same for every deposit
            # of that wording: as SURE, the amount alone would link.
            return _question(
                [(only,)],
                f"Montant exact au centime, facture du {only.sold_on:%d/%m/%Y}. {NAMED_BY_THE_LABEL}",
                credit,
                matching.NEAR_SURE,
            )
        if only.fresh:
            return _sure(
                (only,),
                f"Montant exact au centime, client « {only.customer} » nommé par la banque, facture du "
                f"{only.sold_on:%d/%m/%Y}.",
                credit,
            )
        return _question(
            [(only,)], f"Solde exact d'une facture déjà réglée en partie ({only.label}).", credit, matching.NEAR_SURE
        )
    if exact:
        return _question(
            [(candidate,) for candidate in _by_distance(exact, credit)],
            "Plusieurs factures de ce client à ce montant.",
            credit,
        )

    sums = _sums_paying(amount, [candidate for candidate in named if candidate.fresh])
    if len(sums) == 1 and alias_only:
        return _question(
            sums, f"Plusieurs factures du même client, au centime. {NAMED_BY_THE_LABEL}", credit, matching.NEAR_SURE
        )
    if len(sums) == 1:
        return _sure(sums[0], "Plusieurs factures du même client, au centime.", credit)
    if sums:
        return _question(sums, "Plusieurs sommes de factures de ce client donnent ce montant.", credit)

    close = [candidate for candidate in named if abs(candidate.due - amount) <= NEAR_SURE_GAP]
    if len(close) == 1:
        (only,) = close
        return _question(
            [(only,)],
            f"À {format_money(abs(only.due - amount))} € près : un total arrondi.",
            credit,
            matching.NEAR_SURE,
        )
    if close:
        close.sort(key=lambda candidate: (abs(candidate.due - amount), _days(candidate, credit), candidate.pk))
        return _question(
            [(candidate,) for candidate in close], "Plusieurs factures de ce client à quelques centimes près.", credit
        )

    larger = [candidate for candidate in named if candidate.due > amount]
    if larger:
        ordered = _by_distance(larger, credit)
        return _question(
            [(candidate,) for candidate in ordered],
            f"Acompte possible : {format_money(amount)} € sur {format_money(ordered[0].due)} € à recevoir.",
            credit,
        )

    if credit.recognised:
        return None
    same = [candidate for candidate in in_window if candidate.due == amount]
    if same:
        return _question(
            [(candidate,) for candidate in _by_distance(same, credit)],
            "Même montant, payeur non reconnu comme ce client.",
            credit,
        )
    return None


def _sums_paying(amount: Decimal, fresh) -> list[tuple[SaleCandidate, ...]]:
    """Every set of 2 to MAX_DOCUMENTS_PER_CREDIT invoices of ONE customer -
    among its MAX_DOCUMENTS_SEARCHED most recent paid by nothing yet - adding
    up to `amount` (matching._combinations_paying's shape), each set in
    (date, pk) order."""
    by_customer: dict[str, list[SaleCandidate]] = defaultdict(list)
    for candidate in fresh:
        by_customer[customer_key(candidate.customer)].append(candidate)
    found = []
    for documents in by_customer.values():
        pool = sorted(documents, key=lambda candidate: (candidate.sold_on, candidate.pk), reverse=True)
        pool = pool[:MAX_DOCUMENTS_SEARCHED]
        for size in range(2, min(MAX_DOCUMENTS_PER_CREDIT, len(pool)) + 1):
            for combination in itertools.combinations(pool, size):
                if sum((candidate.due for candidate in combination), Decimal("0")) == amount:
                    found.append(tuple(sorted(combination, key=lambda candidate: (candidate.sold_on, candidate.pk))))
    return found
