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

**A suggestion carries a tier** (`Match.tier`), so a person can accept the
near-certain ones in one go and read the rest one by one:

* `SURE` is exactly `Match.confident` - what the automatic pass links. The
  tier is derived from it, never set beside it, so widening a tier can not
  widen the pass by accident. A sure match says how far back its invoice is
  (`Match.days_back`), and for a payment not made by card one reaching past
  the month (`Match.far_back`) is one the review page shows but does not
  tick in advance: the pass links it all the same - a late payment is
  months, LATER_PAYMENT_WINDOW - but once a supplier's other invoices of
  that figure are linked, the single exact one left may be another month's,
  the month's own not imported.
* `NEAR_SURE` (« quasi-sûre ») is one best option with a clear margin, at
  ONE named supplier: the recurring debit whose nearest invoice of that
  amount is the month's and every other a month away, or the supplier's
  one invoice a cent or two from the bank's figure - dated within the month
  too, for any payment not made by card, whose window reaches months back.
  The thresholds are named below with the reasoning behind them.
* `TO_CONFIRM` (« à confirmer ») is everything else - a payee naming two
  suppliers included, whatever the dates or the gaps then say.

A line the bank printed no payee on (its own fee) is named by its label's
words, and only through an alias a person taught (`payee_of`,
`names_supplier`): the pass never linked such a line on its own, and the
fallback is there so it can be taught, not to widen the pass.

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
#: How the rules name the payments whose window reaches months back - every
#: kind but a card payment, the bank's own fee lines (kind OTHER) included.
#: One phrase, so what the page states is what the code tests.
NOT_BY_CARD = "un paiement autre que par carte (prélèvement, virement, frais bancaires)"

# ----------------------------------------------------------------- tiers
SURE, NEAR_SURE, TO_CONFIRM = "sure", "near_sure", "to_confirm"
TIER_LABELS = {SURE: "certaine", NEAR_SURE: "quasi-sûre", TO_CONFIRM: "à confirmer"}

# A recurring debit - a rent, a subscription, an alarm - pays the invoice
# dated a few days before it, and the supplier's other invoices of exactly
# that amount are the other months', each a month or more away: sorted by
# date distance, the first option is the month's. Unless the month's invoice
# is not there at all - not imported yet, or at another amount this month -
# when the nearest of THIS amount is last month's, further back than a
# month's invoice ever is. Hence a cap on the nearest, and not just a margin
# over the second; the same cap holds the cent-gap rule below for a debit or
# a transfer, whose window reaches months back.
#
# 35 days: a monthly invoice paid within its month, plus a few days for a
# debit booked after a weekend or a bank holiday.
RECURRING_DAYS_BEFORE = timedelta(days=35)
# 20 days: two months' invoices are 28 to 31 days apart, so the second
# nearest is at least that much farther than the first; two deliveries of
# one wholesaler in a fortnight are not, and stay a question.
RECURRING_MARGIN = timedelta(days=20)
# A receipt rebuilt from its HT lines, or an invoice whose VAT rounds the
# other way, sits a cent or two from what left the account; another document
# of the same supplier is euros off. Five cents is room for a rounding on
# each of a few lines, and no room for a different invoice.
NEAR_SURE_GAP = Decimal("0.05")

#: Each tier's rule, in the order the matching applies them, for the page
#: to state: a screen that pre-ticks a link owes the reader the rule it
#: pre-ticked it by. Built from the constants so the words can not drift
#: from the thresholds.
TIER_RULES = (
    (
        SURE,
        "Certaine",
        "Montant exact au centime, fournisseur nommé par la banque, facture datée dans la fenêtre du rapprochement "
        f"(quelques jours autour d'un paiement par carte, jusqu'à {LATER_PAYMENT_WINDOW.days} jours avant pour "
        f"{NOT_BY_CARD}), et une seule façon d'y arriver (une facture, ou une seule somme de factures) : le "
        "rapprochement la rattache de lui-même. Elle n'apparaît ici que dans deux cas : le rapprochement n'a pas "
        "été relancé depuis le dernier import (cochée d'avance), ou la ligne a été déliée à la main (pas cochée : "
        f"le rapprochement automatique n'y revient plus). Pas cochée d'avance non plus quand, pour {NOT_BY_CARD}, "
        f"la facture est datée au-delà de {RECURRING_DAYS_BEFORE.days} jours avant le paiement : le rapprochement la "
        "rattacherait, mais à cette distance c'est peut-être la facture d'un autre mois au même montant, celle du "
        "mois n'étant pas importée - la ligne dit la distance, à vous de voir.",
    ),
    (
        NEAR_SURE,
        "Quasi-sûre",
        "Une seule meilleure option, avec une marge nette, chez un seul fournisseur nommé. Soit plusieurs "
        f"factures de ce fournisseur ont exactement ce montant, la plus proche est datée dans les "
        f"{RECURRING_DAYS_BEFORE.days} jours avant le paiement et chacune des autres en est éloignée d'au moins "
        f"{RECURRING_MARGIN.days} jours de plus (une facture mensuelle : la bonne est celle du mois). Soit une "
        f"seule facture est à {NEAR_SURE_GAP:.2f} € au plus du montant, aucune autre aussi près (un total lu à "
        f"un centime près) - et, pour {NOT_BY_CARD}, datée elle aussi dans les "
        f"{RECURRING_DAYS_BEFORE.days} jours : à quelques centimes près et un mois plus loin, c'est la facture "
        "du mois précédent. Cochée d'avance ; à décocher si le doute existe.",
    ),
    (
        TO_CONFIRM,
        "À confirmer",
        "Tout le reste : le même montant chez un bénéficiaire que la banque ne nomme pas, deux fournisseurs "
        "nommés par le même bénéficiaire, un écart de plus de quelques centimes, plusieurs factures ou "
        "plusieurs sommes aussi proches les unes que les autres, la seule facture proche datée d'au-delà des "
        f"{RECURRING_DAYS_BEFORE.days} jours, un ticket dont la date n'a pas été lue. Rien n'est coché "
        "d'avance.",
    ),
)

# Words that say nothing about which supplier it is: legal forms, family
# words, places, trades half the payees on a statement share - and the
# bank's own vocabulary, which a counterparty parsed out of a label may
# still carry (« VIR SEPA … »): a supplier whose name carries « CARTE » or
# « FRAIS » is not named by every such line.
GENERIC_WORDS = frozenset(
    {
        "SAS", "SASU", "SARL", "EURL", "SCEA", "EARL", "GAEC", "SNC", "SCI", "SOC", "STE", "SOCIETE",
        "CIE", "ETS", "ETABLISSEMENTS", "THE", "AND", "LES", "DES", "AUX", "SUR", "PERE", "FILS",
        "FRERES", "FILLE", "MAISON", "DOMAINE", "CHATEAU", "CAVE", "CAVES", "VIGNERONS", "CHAMPAGNE",
        "FRANCE", "PARIS", "AUTRE", "ANALYSE", "OTHER",
        "FACTURE", "FACTURES", "CARTE", "CARTES", "FRAIS", "COMMISSION", "COMMISSIONS", "COTISATION",
        "COTISATIONS", "PRELEVEMENT", "PRLV", "VIREMENT", "VIR", "SEPA", "PAIEMENT", "NUMERO",
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
    # The whole label the bank printed - what stands in for the payee when
    # the bank printed no counterparty at all (its own fee line reads « FRAIS
    # TENUE DE COMPTE N° 000123 DU 05/06/26 » and nothing else).
    label: str = ""

    @property
    def paid_on(self) -> date:
        return self.card_date or self.operation_date

    @property
    def payee(self) -> str:
        return payee_of(self.counterparty, self.label)

    @property
    def payee_from_label(self) -> bool:
        """Whether `payee` is the label standing in for a blank counterparty
        - which names a supplier through a learnt alias and nothing else
        (`names_supplier`)."""
        return not self.counterparty


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
    # SURE, NEAR_SURE or TO_CONFIRM. SURE is `confident` and nothing else:
    # set from it here, so no branch can make a link sure without making it
    # automatic, nor the other way round.
    tier: str = TO_CONFIRM
    # Why this tier and not another, in the words the review page shows
    # beside `reason` - the figures the rule was decided on.
    tier_reason: str = ""
    # Days between the payment and the most recent invoice of the first
    # option - the figure the tiers are decided on, said for a sure match
    # too. None when that invoice has no date (an undated receipt).
    days_back: int | None = None
    # For a payment not made by card: the first option is dated beyond
    # RECURRING_DAYS_BEFORE. Both near-sure rules already refuse that
    # (TO_CONFIRM); a SURE match is linked by the pass all the same - a late
    # payment is months, LATER_PAYMENT_WINDOW - but « Propositions » does not
    # tick it in advance (`views.Row.preticked`): once a supplier's other
    # invoices of that figure are linked, the single exact one left may be
    # another month's, the month's own not imported. Said, never repaired.
    far_back: bool = False

    def __post_init__(self):
        if self.confident:
            self.tier = SURE
        elif self.tier == SURE:
            raise ValueError("a match that is not confident can not be sure")
        if self.tier not in TIER_LABELS:
            raise ValueError(f"unknown tier {self.tier!r}")

    @property
    def tier_label(self) -> str:
        return TIER_LABELS[self.tier]

    @property
    def near_sure(self) -> bool:
        return self.tier == NEAR_SURE


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


def payee_of(counterparty: str, label: str) -> str:
    """Who was paid, as the matching and the aliases see it.

    The payee the bank prints, when it prints one. When it prints none (a
    line of kind OTHER: the bank's own fees), the label's words with every
    run of digits taken out - « FRAIS TENUE DE COMPTE N° 000123 DU 05/06/26 »
    is « FRAIS TENUE DE COMPTE N DU », the same words next month under
    another number and another date. That is what lets such a line be learnt
    as an alias of a supplier once a person links it
    (`reconcile._learn_payee`), and what lets `alias_key` of it be the same
    key month after month. The fallback names nobody on its own: an alias
    has to exist - never a word of the supplier's name, exact or a letter
    off, which on a label carrying a motif, a date and the bank's own words
    is a coincidence the pass would act on (`names_supplier`).
    """
    if counterparty:
        return counterparty
    return " ".join(words(re.sub(r"\d+", " ", label)))


def supplier_words(*names: str) -> frozenset[str]:
    return frozenset(
        word
        for name in names
        for word in words(name)
        if len(word) >= 3 and not word.isdigit() and word not in GENERIC_WORDS
    )


def names_supplier(payee: str, naming: Naming, *, alias_only: bool = False) -> bool:
    """Whether the payee names this supplier: an alias a person taught, or a
    word of the supplier's own name - which, at five letters or more, may be
    one letter off: shops spell themselves one way on the ticket and another
    at the bank ("SABBAH", "Sabbh Oriental").

    `alias_only` is for a label standing in for a blank payee
    (`Payment.payee_from_label`): the alias, and nothing else. A label
    carries a motif, a date and the bank's own words beside whoever was
    paid, and the pass acts on whatever is named here - so a word of a
    supplier's name in it, exact or a letter off, would link AUTOMATICALLY
    on a coincidence (a supplier « Assurance Exemple », a fee line reading
    « ASSURANCE MOYENS DE PAIEMENT »). No line the bank printed no payee on
    was ever linked without a person; the fallback lets such lines be
    taught, and that is all.
    """
    if not payee:
        return False
    if alias_key(payee) in naming.aliases:
        return True
    if alias_only:
        return False
    for word in words(payee):
        for known in naming.words:
            if word == known:
                return True
            if len(word) >= 5 and len(known) >= 5 and Levenshtein.distance(word, known, score_cutoff=1) <= 1:
                return True
    return False


def match(payment: Payment, candidates, naming: dict[int, Naming]) -> Match | None:
    """The invoices `payment` paid, among `candidates`, or None.

    On top of the tier: how far back the first option is dated
    (`Match.days_back`) and, for a payment not made by card, whether that is
    past the month (`Match.far_back`) - the one figure a SURE match's rule
    never looked at, said in its `tier_reason` so a review page never shows
    « certaine » over a five-month reach without a word.
    """
    found = _match(payment, candidates, naming)
    if found is None:
        return None
    found.days_back = _days_back(found.options[0], payment)
    found.far_back = (
        payment.kind not in PAID_ON_THE_SPOT
        and found.days_back is not None
        and found.days_back > RECURRING_DAYS_BEFORE.days
    )
    if found.tier == SURE:
        found.tier_reason = _sure_reason(found, payment)
    return found


def _match(payment: Payment, candidates, naming: dict[int, Naming]) -> Match | None:
    due = payment.amount_due
    if due <= 0:
        return None
    named_cache: dict[int, bool] = {}
    payee = payment.payee
    alias_only = payment.payee_from_label

    def is_named(supplier_id: int) -> bool:
        if supplier_id not in named_cache:
            named_cache[supplier_id] = names_supplier(payee, naming.get(supplier_id, NO_NAMING), alias_only=alias_only)
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
        ordered = _by_distance(exact, payment)
        tier, why = _recurring_tier(ordered, payment)
        return Match(
            [(candidate,) for candidate in ordered][:MAX_OPTIONS],
            False,
            f"{len(exact)} factures de ce fournisseur ont exactement ce montant : laquelle ?",
            tier,
            why,
        )

    combinations = _combinations_paying(due, named)
    if len(combinations) == 1:
        return Match(combinations, True, f"Somme exacte de {len(combinations[0])} factures du fournisseur nommé.")
    if combinations:
        return Match(
            combinations[:MAX_OPTIONS],
            False,
            f"{len(combinations)} combinaisons de factures donnent ce montant : laquelle ?",
            TO_CONFIRM,
            "Plusieurs sommes de factures donnent exactement ce montant : rien ne les départage.",
        )

    close = [candidate for candidate in named if candidate.total > 0 and abs(candidate.total - due) <= due * SUGGESTION_GAP]
    if close:
        close.sort(key=lambda candidate: (abs(candidate.total - due), _days_apart(candidate, payment)))
        tier, why = _close_tier(close, payment)
        return Match(
            [(candidate,) for candidate in close[:MAX_OPTIONS]],
            False,
            "Fournisseur nommé, mais le montant diffère : ticket mal lu, ou une autre facture ?",
            tier,
            why,
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
            TO_CONFIRM,
            "Sans date lue, rien ne dit que ce ticket est celui de ce jour-là plutôt que d'un autre mois.",
        )

    same_amount = [candidate for candidate in in_window if candidate.total == due]
    if same_amount:
        return Match(
            [(candidate,) for candidate in _by_distance(same_amount, payment)][:MAX_OPTIONS],
            False,
            "Même montant à ces dates, mais la banque ne nomme pas ce fournisseur.",
            TO_CONFIRM,
            "Le bénéficiaire imprimé par la banque ne nomme pas ce fournisseur : à vous de le reconnaître. "
            "Rattaché, il sera reconnu le mois prochain.",
        )
    return None


def _several_suppliers(candidates) -> str | None:
    """Why neither near-sure rule applies when the payee names more than one
    supplier: « the named supplier's invoices » is then no premise at all,
    and a person has to say which supplier this line is for. None when the
    candidates are one supplier's."""
    count = len({candidate.supplier_id for candidate in candidates})
    if count <= 1:
        return None
    return f"{count} fournisseurs sont nommés par ce bénéficiaire : rien ne les départage."


def _recurring_tier(ordered, payment: Payment) -> tuple[str, str]:
    """Several invoices of the named supplier at exactly this amount, sorted
    by date distance: near-sure when the nearest is the month's and every
    other a month away, a question otherwise (see RECURRING_DAYS_BEFORE)."""
    several = _several_suppliers(ordered)
    if several:
        return TO_CONFIRM, several
    nearest_days = _days_apart(ordered[0], payment)
    second_days = _days_apart(ordered[1], payment)
    if nearest_days > RECURRING_DAYS_BEFORE.days:
        return TO_CONFIRM, (
            f"La plus proche est datée {nearest_days} jours avant le paiement, au-delà de "
            f"{RECURRING_DAYS_BEFORE.days} : la facture du mois n'est peut-être pas importée."
        )
    if second_days - nearest_days < RECURRING_MARGIN.days:
        return TO_CONFIRM, (
            f"Deux factures à {nearest_days} et {second_days} jours du paiement : moins de "
            f"{RECURRING_MARGIN.days} jours d'écart, rien ne les départage."
        )
    return NEAR_SURE, (
        f"La plus proche est datée {nearest_days} jour{'s' if nearest_days > 1 else ''} avant le paiement, "
        f"les autres à {second_days} jours au moins : une facture mensuelle, la bonne est celle du mois."
    )


def _close_tier(close, payment: Payment) -> tuple[str, str]:
    """The named supplier's invoices within SUGGESTION_GAP, nearest amount
    first: near-sure when exactly one is within NEAR_SURE_GAP - and, for any
    payment not made by card (a debit, a transfer, the bank's own fee line),
    dated within RECURRING_DAYS_BEFORE. Months of invoices are in reach of
    one, and a recurring supplier whose figure moved by a few cents has last
    month's invoice a few cents off the moment this month's is not imported:
    the wrong-month case, not a misreading. A card payment's window is days
    wide, so the cap says nothing there."""
    due = payment.amount_due
    several = _several_suppliers(close)
    if several:
        return TO_CONFIRM, several
    within = [candidate for candidate in close if abs(candidate.total - due) <= NEAR_SURE_GAP]
    if len(within) == 1:
        gap = abs(within[0].total - due)
        days = _days_apart(within[0], payment)
        if payment.kind not in PAID_ON_THE_SPOT and days > RECURRING_DAYS_BEFORE.days:
            return TO_CONFIRM, (
                f"La seule facture à {gap:.2f} € du montant est datée {days} jours avant le paiement, au-delà de "
                f"{RECURRING_DAYS_BEFORE.days} : la facture du mois n'est peut-être pas importée."
            )
        return NEAR_SURE, (
            f"Une seule facture du fournisseur nommé à {gap:.2f} € du montant, aucune autre à moins de "
            f"{NEAR_SURE_GAP:.2f} € : un total lu à un centime près."
        )
    if within:
        return TO_CONFIRM, f"{len(within)} factures à moins de {NEAR_SURE_GAP:.2f} € du montant : laquelle ?"
    gap = abs(close[0].total - due)
    return TO_CONFIRM, (
        f"L'écart le plus faible est de {gap:.2f} €, plus que les {NEAR_SURE_GAP:.2f} € d'une erreur de "
        "lecture : c'est peut-être une autre facture."
    )


def _days_back(option, payment: Payment) -> int | None:
    """Days between the payment and the most recent invoice of `option`;
    None when one of them has no date."""
    if any(candidate.invoice_date is None for candidate in option):
        return None
    return min(_days_apart(candidate, payment) for candidate in option)


def _sure_reason(found: Match, payment: Payment) -> str:
    """What a sure match says beside its reason: how far back its invoice is
    - the one figure the rule did not weigh, and the one a reader wants when
    it is months (see `Match.far_back`)."""
    option = found.options[0]
    days = found.days_back
    what = "Facture" if len(option) == 1 else f"Somme de {len(option)} factures, la plus récente"
    if days == 0:
        return f"{what} du jour du paiement."
    plural = "s" if days > 1 else ""
    if payment.kind in PAID_ON_THE_SPOT:
        # A receipt may be dated the day after the card was used
        # (CARD_DAYS_AFTER): « à N jours », not « avant ».
        return f"{what} à {days} jour{plural} du paiement."
    dated = f"{what} datée {days} jour{plural} avant le paiement"
    if not found.far_back:
        return f"{dated}."
    return (
        f"{dated}, au-delà de {RECURRING_DAYS_BEFORE.days} : le rapprochement automatique la rattache tout de même "
        f"(il remonte jusqu'à {LATER_PAYMENT_WINDOW.days} jours), mais elle n'est pas cochée d'avance - à cette "
        "distance, c'est peut-être la facture d'un autre mois au même montant, celle du mois n'étant pas importée."
    )


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
