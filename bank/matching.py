"""Which imported invoice(s) a bank payment paid - by the numbers first.

A payment is matched on three things, in order of how much they prove:

* The amount, to the cent. The bank's figure is exact; an invoice that is a
  cent off is another invoice or a misread one.
* The date. A receipt is printed when the card is used, so it carries the
  card date, give or take a day. A supplier's invoice is paid by debit or
  transfer after it is dated - UBA and Metro ten to twenty days later - and
  never before.
* The payee. What the bank prints (card merchant, debit creditor, transfer
  beneficiary) has to name the invoice's supplier: "SABBAH" names "Sabbh
  Oriental", "U.B.A." names "UBA".

All three, satisfied in exactly one way: linked automatically. Several
invoices of the named supplier adding up exactly to one debit count too (two
deliveries and a returned-deposit credit note, paid together). Anything less
is a suggestion a person confirms, never a link.

Plain values in, plain values out - no database, see bank.reconcile.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from rapidfuzz.distance import Levenshtein

# The bank books a card payment a day or three after the card was used.
CARD_DAYS_BEFORE = timedelta(days=3)
CARD_DAYS_AFTER = timedelta(days=1)
# How far back a debit or a transfer may reach - a late payment is months.
LATER_PAYMENT_WINDOW = timedelta(days=180)
# One debit for several invoices: at most this many, among the supplier's
# most recent unpaid ones.
MAX_INVOICES_PER_PAYMENT = 4
MAX_INVOICES_SEARCHED = 12
# A named supplier's invoice this close to the amount is shown as a possible
# misreading (a receipt read 13,02 for a payment of 13,06).
SUGGESTION_GAP = Decimal("0.10")
# A receipt's total is rebuilt from its HT lines, so it can sit a cent away
# from what the ticket printed (7,75 for a printed 7,76) - allowed only where
# nothing is linked on its own.
RECEIPT_ROUNDING = Decimal("0.01")
MAX_OPTIONS = 5

PAID_ON_THE_SPOT = frozenset({"CARD"})

# Words that say nothing about which supplier it is: legal forms, family
# words, places, trades half the payees on a statement share.
GENERIC_WORDS = frozenset(
    {
        "SAS", "SASU", "SARL", "EURL", "SCEA", "EARL", "GAEC", "SNC", "SCI", "SOC", "STE", "SOCIETE",
        "CIE", "ETS", "ETABLISSEMENTS", "THE", "AND", "LES", "DES", "AUX", "SUR", "PERE", "FILS",
        "FRERES", "FILLE", "MAISON", "DOMAINE", "CHATEAU", "CAVE", "CAVES", "VIGNERONS", "CHAMPAGNE",
        "FRANCE", "PARIS", "AUTRE", "ANALYSE", "OTHER",
    }
)


@dataclass(frozen=True)
class InvoiceCandidate:
    pk: int
    supplier_id: int
    # None when it was never read - OCR misses a receipt's date more often
    # than its total.
    invoice_date: date | None
    total: Decimal


@dataclass(frozen=True)
class Payment:
    kind: str
    operation_date: date
    card_date: date | None
    counterparty: str
    amount_due: Decimal

    @property
    def paid_on(self) -> date:
        return self.card_date or self.operation_date


@dataclass(frozen=True)
class Naming:
    """How the bank may spell one supplier: the words of its own name, and
    payee names a person has already linked to it."""

    words: frozenset[str]
    aliases: frozenset[str] = frozenset()


NO_NAMING = Naming(frozenset())


@dataclass
class Match:
    # Each option is one set of invoices that pays the line in full.
    options: list[tuple[InvoiceCandidate, ...]]
    confident: bool
    reason: str


def words(text: str) -> list[str]:
    """Upper-case ASCII words. "U.B.A." is one word, "S.A.S.-METRO" two."""
    ascii_text = unicodedata.normalize("NFKD", text.upper()).encode("ascii", "ignore").decode()
    found = []
    for chunk in re.split(r"[\s/\-*&()+,;:'\"]+", ascii_text):
        word = re.sub(r"[^A-Z0-9]", "", chunk)
        if word:
            found.append(word)
    return found


def alias_key(counterparty: str) -> str:
    return " ".join(words(counterparty))


def supplier_words(*names: str) -> frozenset[str]:
    return frozenset(
        word
        for name in names
        for word in words(name)
        if len(word) >= 3 and not word.isdigit() and word not in GENERIC_WORDS
    )


def names_supplier(counterparty: str, naming: Naming) -> bool:
    """Whether the payee the bank printed is this supplier. A word of five
    letters or more may be one letter off: shops spell themselves one way on
    the ticket and another at the bank ("SABBAH", "Sabbh Oriental")."""
    if not counterparty:
        return False
    if alias_key(counterparty) in naming.aliases:
        return True
    for word in words(counterparty):
        for known in naming.words:
            if word == known:
                return True
            if len(word) >= 5 and len(known) >= 5 and Levenshtein.distance(word, known, score_cutoff=1) <= 1:
                return True
    return False


def match(payment: Payment, candidates, naming: dict[int, Naming]) -> Match | None:
    due = payment.amount_due
    if due <= 0:
        return None
    named_cache: dict[int, bool] = {}

    def is_named(supplier_id: int) -> bool:
        if supplier_id not in named_cache:
            named_cache[supplier_id] = names_supplier(payment.counterparty, naming.get(supplier_id, NO_NAMING))
        return named_cache[supplier_id]

    in_window = [candidate for candidate in candidates if candidate.total and _in_window(payment, candidate)]
    named = [candidate for candidate in in_window if is_named(candidate.supplier_id)]

    exact = [candidate for candidate in named if candidate.total == due]
    if len(exact) == 1:
        return Match([(exact[0],)], True, "Montant exact, fournisseur nommé par la banque.")
    if exact:
        nearest = _nearest(exact, payment) if payment.kind in PAID_ON_THE_SPOT else None
        if nearest is not None:
            return Match(
                [(nearest,)], True, "Montant exact, fournisseur nommé ; le ticket du jour le plus proche du paiement."
            )
        return Match(
            [(candidate,) for candidate in _by_distance(exact, payment)][:MAX_OPTIONS],
            False,
            f"{len(exact)} factures de ce fournisseur ont exactement ce montant : laquelle ?",
        )

    combinations = _combinations_paying(due, named)
    if len(combinations) == 1:
        return Match(combinations, True, f"Somme exacte de {len(combinations[0])} factures du fournisseur nommé.")
    if combinations:
        return Match(
            combinations[:MAX_OPTIONS],
            False,
            f"{len(combinations)} combinaisons de factures donnent ce montant : laquelle ?",
        )

    close = [candidate for candidate in named if candidate.total > 0 and abs(candidate.total - due) <= due * SUGGESTION_GAP]
    if close:
        close.sort(key=lambda candidate: (abs(candidate.total - due), _days_apart(candidate, payment)))
        return Match(
            [(candidate,) for candidate in close[:MAX_OPTIONS]],
            False,
            "Fournisseur nommé, mais le montant diffère : ticket mal lu, ou une autre facture ?",
        )

    if payment.kind not in PAID_ON_THE_SPOT:
        return None

    undated = [
        candidate
        for candidate in candidates
        if candidate.invoice_date is None
        and candidate.total
        and abs(candidate.total - due) <= RECEIPT_ROUNDING
        and is_named(candidate.supplier_id)
    ]
    if undated:
        undated.sort(key=lambda candidate: (abs(candidate.total - due), candidate.pk))
        return Match(
            [(candidate,) for candidate in undated[:MAX_OPTIONS]],
            False,
            "Ticket du fournisseur nommé, au même montant, mais sa date n'a pas été lue.",
        )

    same_amount = [candidate for candidate in in_window if candidate.total == due]
    if same_amount:
        return Match(
            [(candidate,) for candidate in _by_distance(same_amount, payment)][:MAX_OPTIONS],
            False,
            "Même montant à ces dates, mais la banque ne nomme pas ce fournisseur.",
        )
    return None


def _in_window(payment: Payment, candidate: InvoiceCandidate) -> bool:
    if candidate.invoice_date is None:
        return False
    paid_on = payment.paid_on
    if payment.kind in PAID_ON_THE_SPOT:
        return paid_on - CARD_DAYS_BEFORE <= candidate.invoice_date <= paid_on + CARD_DAYS_AFTER
    return paid_on - LATER_PAYMENT_WINDOW <= candidate.invoice_date <= paid_on


def _days_apart(candidate: InvoiceCandidate, payment: Payment) -> int:
    return abs((candidate.invoice_date - payment.paid_on).days)


def _by_distance(candidates, payment: Payment):
    return sorted(candidates, key=lambda candidate: (_days_apart(candidate, payment), candidate.pk))


def _nearest(candidates, payment: Payment) -> InvoiceCandidate | None:
    ordered = _by_distance(candidates, payment)
    if len(ordered) == 1 or _days_apart(ordered[0], payment) < _days_apart(ordered[1], payment):
        return ordered[0]
    return None


def _combinations_paying(due: Decimal, named) -> list[tuple[InvoiceCandidate, ...]]:
    """Every set of two or more invoices of one supplier adding up to `due`."""
    by_supplier = defaultdict(list)
    for candidate in named:
        by_supplier[candidate.supplier_id].append(candidate)
    found = []
    for invoices in by_supplier.values():
        pool = sorted(invoices, key=lambda candidate: candidate.invoice_date, reverse=True)[:MAX_INVOICES_SEARCHED]
        for size in range(2, min(MAX_INVOICES_PER_PAYMENT, len(pool)) + 1):
            for combination in itertools.combinations(pool, size):
                if sum((candidate.total for candidate in combination), Decimal("0")) == due:
                    found.append(tuple(sorted(combination, key=lambda candidate: (candidate.invoice_date, candidate.pk))))
    return found
