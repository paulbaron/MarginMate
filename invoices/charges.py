"""What a charge document charges, and what that is made of.

A charge supplier's document has no products (see `importing.expense_lines`):
what is filed is what was paid. Some of them say what that is made of,
though - a rent statement prints the rent, the building provision, the water
provision and the tax, and those are worth keeping apart: the rent is not
the charges, and the charges are the ones that get regularised.

Nothing here knows a word of it. A poste is a label and the amount printed
after it, and a document's postes are the run of them adding up to an amount
the document prints below them - which is what proves the reading, and what
settles which amount is the one charged: a statement prints last month's
échéance, the direct debit that paid it and this month's, and the largest
amount printed twice is last month's. The run that adds up is this month's.

A tax line is the poste that is a French rate of another one; it is not a
poste of its own, it is that one's VAT.

No database, no parser: text and arithmetic, so it can be tested from a
hand-written statement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .parsers.receipt_base import CENTS, KNOWN_VAT_RATES, MONEY_RE, line_amounts, money_value

ZERO = Decimal("0")
# A label is what stands between the amount before it and its own: at least
# three letters, so a column of figures does not pass for one.
LETTERS = re.compile(r"[A-Za-zÀ-ÿ]")
MIN_LABEL_LETTERS = 3
# One poste is not a breakdown - it is the total under another name.
MIN_POSTES = 2
# What it takes for a breakdown to overrule the total read from the document:
# three labelled amounts in a row adding up to a fourth printed under them is
# not a coincidence; two could be.
MIN_POSTES_TO_SETTLE_THE_TOTAL = 3


@dataclass(frozen=True)
class Poste:
    """One line of what a charge is made of: what it is called, what it came
    to (tax included) and the rate its tax was worked out at."""

    name: str
    amount: Decimal
    rate: Decimal = ZERO

    @property
    def total_ht(self) -> Decimal:
        return (self.amount / (Decimal("1") + self.rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


def read_charge(text: str, read_total: Decimal | None) -> tuple[Decimal | None, list[Poste]]:
    """What the document charges and the postes it is made of.

    `read_total` is what the reader made of it; it stands unless a long
    enough run of postes says otherwise. The postes are [] when nothing
    proves a breakdown - a phone bill details its calls, not its charges.
    """
    run = _longest_run_adding_up(_labelled_amounts(text), text)
    if len(run) < MIN_POSTES:
        return read_total, []
    total = sum((poste.amount for poste in run), start=ZERO)
    disagrees = read_total is not None and abs(total - read_total) > CENTS
    if disagrees and len(run) < MIN_POSTES_TO_SETTLE_THE_TOTAL:
        return read_total, []
    kept = _with_the_tax_folded_in(run)
    return total, kept if len(kept) >= MIN_POSTES else []


def _labelled_amounts(text: str) -> list[Poste]:
    """The last amount of each line, and the label printed in front of it.

    The last one because a statement puts two columns on one line - "Solde
    antérieur au 21/10/2025 301,99 LOYER LOCAUX ACTIVITE HT 634,39" is the
    left column's balance and the right column's rent, and only the right
    column is this month's. A minus printed on its own in front of the
    amount is its sign, whatever the spaces around it ("REMBOURSEMENT DEPOT
    GARANTIE - 3,19", a deposit given back).
    """
    found = []
    for line in text.split("\n"):
        matches = list(MONEY_RE.finditer(line))
        position = max((index for index, match in enumerate(matches) if money_value(match)), default=None)
        if position is None:
            continue
        last = matches[position]
        # From the amount before it, even a zero one: the label of the right
        # column starts where the left column's figure ends.
        before = line[matches[position - 1].end() if position else 0 : last.start()]
        # Two columns on one line, the left one's label wrapped onto this
        # one ("Solde au 21/07/2026 avant avis  Plan d'apurement"): the
        # label is the last column of it that reads like a label - the last
        # one is "57 m³" where the label is "Votre consommation" beside it.
        chunks = [chunk.strip(" \t.:-|()") for chunk in re.split(r"\s{2,}", before)]
        label = next(
            (chunk for chunk in reversed(chunks) if len(LETTERS.findall(chunk)) >= MIN_LABEL_LETTERS), ""
        )
        if not label:
            continue
        amount = money_value(last)
        if before.rstrip().endswith("-"):
            amount = -amount
        found.append(Poste(name=label, amount=amount))
    return found


def _longest_run_adding_up(pairs: list[Poste], text: str) -> list[Poste]:
    """The longest run of postes, in the order they are printed, adding up to
    an amount the document prints under them - what it charges for the
    period, its own total ("Total de votre avis d'échéance"). What is taken
    from the account can be more, the arrears of an avis already filed
    among them, and a document is worth what it charges.

    A subtotal printed among them ("Total de votre avis d'échéance (B)") is
    the run so far restated: it is stepped over rather than counted twice.
    Each run stops at the first amount that answers - carried on, one that
    had already added up swallowed the direct debit paying it and the total
    restating it, and read them as postes of the month.
    """
    printed = {value for line in text.split("\n") for value in line_amounts(line) if value}
    best: list[Poste] = []
    for start in range(len(pairs)):
        running, kept = ZERO, []
        for poste in pairs[start:]:
            if kept and abs(poste.amount - running) <= CENTS:
                continue
            kept.append(poste)
            running += poste.amount
            if len(kept) >= MIN_POSTES and any(abs(running - amount) <= CENTS for amount in printed):
                if len(kept) > len(best):
                    best = list(kept)
                break
    return best


def _with_the_tax_folded_in(run: list[Poste]) -> list[Poste]:
    """A poste that is a French rate of exactly one other is that one's tax,
    not a poste: "TVA TAUX NORMAL 126,88" is the 20% of the rent above it,
    and the rent is what is filed, at 20%."""
    for position, tax in enumerate(run):
        rated = [
            (other, rate)
            for other in run
            if other is not tax
            for rate in KNOWN_VAT_RATES
            if abs((other.amount * rate).quantize(CENTS, rounding=ROUND_HALF_UP) - tax.amount) <= CENTS
        ]
        if len(rated) != 1:
            continue
        base, rate = rated[0]
        folded = []
        for other_position, poste in enumerate(run):
            if other_position == position:
                continue
            folded.append(
                Poste(name=poste.name, amount=poste.amount + tax.amount, rate=rate) if poste is base else poste
            )
        return folded
    return run
