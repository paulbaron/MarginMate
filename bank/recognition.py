"""Recognising what an operation of the bank statement is, by rules a person
edits (`models.OperationRule`, « Reconnaissance des opérations ») - never by
words written in the code. The owner's bank is seeded as rules (migration
0006); another bank's statement is read by editing them.

Two questions, each answered by the first active rule of its kind, in their
order (`OperationRule.position`), whose pattern is found:

* **What the operation is** (`KIND_MEANINGS`): a card payment, a direct
  debit, a transfer, anything else. Asked at import (`describe`, from
  `statements.parse_statement`) and STORED on the line: `kind`,
  `counterparty` - the pattern's `(?P<tiers>…)` - and, for a card payment,
  `card_date` - its `(?P<jour>…)` and `(?P<mois>…)`, with `(?P<annee>…)` or
  without (the year is then the latest one that does not put the card after
  the booking). Nothing found: « Autre », no payee, no card date. A rule
  edited later changes nothing already imported until a person asks
  (`stored_changes`, `apply_changes`): the payee feeds the aliases and the
  payers learnt, and the matching, so it never moves in silence.
* **What a credit is in the till** (`TILL_MEANINGS`): a card terminal's
  payout, cash or cheques deposited, meal vouchers, an « Avoir », no sale.
  Asked whenever « Entrées d'argent » or Banque's « Entrées » tab is drawn
  (`till_reading`), never stored. A payout's gross is its pattern's
  `(?P<encaisse>…)`, read whole or not at all: a capture that is no amount is
  no payout of that rule. A payout rule WITHOUT that group is a terminal that
  prints no gross: the amount received counts as the gross and the
  commission is unknown (`income.Entry.gross_from_amount`).

**Patterns go through `returnables.patterns`**, the guard that refuses a
pattern before `regex` could freeze the machine compiling it, and the time
limit on every match. Case never matters; an accent counts only where the
pattern spells one - a text the pattern does not find as printed is tried
again without its accents (`common.search_key`), so « VERSEMENT ESPECES »
finds « Versement espèces ». A stored pattern is never trusted: one that no
longer passes the check (`Rules.invalid`), or that ran past the time limit
(`Rules.slow`), recognises nothing, and the pages say which - an import
refuses to run past either (`statements.parse_statement`), since a kind
stored wrong is never read again.

Pure - plain values in, plain values out - but for the three functions at
the end, which read and write the database.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from time import thread_time

from django.db import transaction

from common import search_key
from returnables import patterns
from returnables.patterns import PatternError

from .models import BankTransaction, IncomeSource, OperationRule

Meaning = OperationRule.Meaning
Searched = OperationRule.Searched

#: What the operation is, by meaning - `BankTransaction.kind`.
KIND_OF = {
    Meaning.CARD_PAYMENT: BankTransaction.Kind.CARD.value,
    Meaning.DEBIT: BankTransaction.Kind.DEBIT.value,
    Meaning.TRANSFER: BankTransaction.Kind.TRANSFER.value,
    Meaning.OTHER_OPERATION: BankTransaction.Kind.OTHER.value,
}
#: What a credit is in the till, by meaning - `IncomeSource`.
SOURCE_OF = {
    Meaning.PAYOUT: IncomeSource.CARD.value,
    Meaning.CASH: IncomeSource.CASH.value,
    Meaning.CHEQUE: IncomeSource.CHEQUE.value,
    Meaning.VOUCHER: IncomeSource.VOUCHER.value,
    Meaning.CREDIT: IncomeSource.CREDIT.value,
    Meaning.NOT_A_SALE: IncomeSource.OTHER.value,
}
KIND_MEANINGS = frozenset(KIND_OF)
TILL_MEANINGS = frozenset(SOURCE_OF)

#: The named groups a pattern may hold, and what each reads.
PAYEE = "tiers"
DAY, MONTH, YEAR = "jour", "mois", "annee"
GROSS = "encaisse"
GROUPS = {
    Meaning.CARD_PAYMENT: (PAYEE, DAY, MONTH, YEAR),
    Meaning.DEBIT: (PAYEE,),
    Meaning.TRANSFER: (PAYEE,),
    Meaning.OTHER_OPERATION: (PAYEE,),
    Meaning.PAYOUT: (GROSS,),
}
#: What each group is called on the page.
GROUP_LABELS = {
    PAYEE: "le tiers (bénéficiaire ou payeur)",
    DAY: "le jour d'utilisation de la carte",
    MONTH: "son mois",
    YEAR: "son année (2 ou 4 chiffres, facultative)",
    GROSS: "la somme encaissée (brut, avant commission)",
}

#: What a text is matched on: a label longer than this is cut. A bank's
#: label is a few hundred characters; the cut bounds what one match costs.
TEXT_LIMIT = 1000
#: `BankTransaction.counterparty`'s width: a payee captured longer is cut.
PAYEE_MAX = 255
#: The card dates a capture may give: two digits are 20yy, four are read
#: as written, and nothing outside these years is a date (a year 1 put
#: every matching window out of the calendar).
FIRST_YEAR, LAST_YEAR = 2000, 2099

#: Seconds one rule may spend matching over one reading (a page, an
#: import) before it is taken for too slow. A bank's label takes a rule
#: microseconds; this is a pattern just short of the per-match limit on
#: line after line, which no single match would give away.
RULE_SECONDS = 5.0

#: The label a pattern is refused or explained under.
PATTERN_LABEL = "Motif"


def name_key(name) -> str:
    """What makes two rule names one: case, accents and spaces aside."""
    return " ".join(search_key(str(name or "")).split())


def meaning_label(meaning) -> str:
    return str(Meaning(meaning).label) if meaning in Meaning.values else str(meaning)


def check(meaning, searched, pattern):
    """The compiled pattern of a rule meaning `meaning` searched in
    `searched` - or PatternError, a French sentence starting « Motif : ». The
    guard of `returnables.patterns` first (an empty pattern, one matching an
    empty text, one too costly to compile), then the named groups: only those
    `meaning` reads (`GROUPS`), the day and the month together, the year
    only beside them."""
    if meaning not in Meaning.values:
        raise PatternError(f"{PATTERN_LABEL} : signification inconnue (« {meaning} »).")
    if searched not in Searched.values:
        raise PatternError(f"{PATTERN_LABEL} : champ inconnu (« {searched} »).")
    compiled = patterns.compile_pattern(pattern, field_label=PATTERN_LABEL)
    names = set(compiled.groupindex)
    allowed = GROUPS.get(meaning, ())
    stray = sorted(names - set(allowed))
    if stray:
        if allowed:
            said = ", ".join(f"(?P<{name}>…)" for name in allowed)
            hint = f" ; seuls {said} servent à « {meaning_label(meaning)} »"
        else:
            hint = f" ; « {meaning_label(meaning)} » ne lit aucun groupe nommé"
        raise PatternError(f"{PATTERN_LABEL} : le groupe (?P<{stray[0]}>…) ne sert à rien{hint}.")
    if (DAY in names) != (MONTH in names):
        raise PatternError(f"{PATTERN_LABEL} : (?P<{DAY}>…) et (?P<{MONTH}>…) vont ensemble.")
    if YEAR in names and DAY not in names:
        raise PatternError(f"{PATTERN_LABEL} : (?P<{YEAR}>…) ne sert qu'avec (?P<{DAY}>…) et (?P<{MONTH}>…).")
    return compiled


@dataclass(frozen=True)
class Rule:
    """One active rule, compiled."""

    name: str
    meaning: str
    searched: str
    pattern: object
    pk: int | None = None

    @property
    def reads_gross(self) -> bool:
        return self.meaning == Meaning.PAYOUT and GROSS in self.pattern.groupindex


@dataclass
class Rules:
    """The active rules of both kinds, in their order, compiled once - and
    what went wrong with any of them since."""

    kinds: tuple = ()
    till: tuple = ()
    #: {name: sentence}: stored rules whose pattern no longer passes `check`.
    invalid: dict = field(default_factory=dict)
    #: Names of the rules that ran past the time limit on some text - or
    #: spent more than RULE_SECONDS matching in all - in the order found:
    #: they recognise nothing more for this reading.
    slow: list = field(default_factory=list)
    #: {name: seconds spent matching}, what RULE_SECONDS is counted against.
    spent: dict = field(default_factory=dict)

    def find(self, rule: Rule, label: str, bank_type: str):
        """`rule`'s match on the text it searches, as printed, else without
        its accents - or None (a rule found too slow is skipped). A match
        found without the accents is read back on the text as printed, so
        what it captures keeps its accents and its case."""
        if rule.name in self.slow:
            return None
        text = (bank_type if rule.searched == Searched.BANK_TYPE else label) or ""
        text = text[:TEXT_LIMIT]
        if not text:
            return None
        # The thread's own CPU time, not the clock's: the server is one
        # process whose gathers and imports are threads, and a match waiting
        # for the GIL behind them is not a slow pattern (review, 01/10/2026).
        started = thread_time()
        try:
            found = search(rule.pattern, text)
            if found is None:
                folded = search_key(text)
                if folded != text.lower():
                    found = search(rule.pattern, folded)
                    if found is not None and _aligned(text):
                        found = _AsPrinted(found, text)
        except PatternError:
            self.slow.append(rule.name)
            return None
        finally:
            self.spent[rule.name] = self.spent.get(rule.name, 0.0) + thread_time() - started
        if self.spent[rule.name] > RULE_SECONDS:
            # Every match under the time limit, and still the slowest thing
            # the page does: a pattern just short of the limit on every line.
            self.slow.append(rule.name)
        return found

    @property
    def problems(self) -> list[str]:
        """What a page says about rules that recognised nothing, in French."""
        said = [
            f"Règle « {name} » : {sentence.removeprefix(PATTERN_LABEL + ' : ').rstrip('.')} - elle ne reconnaît rien."
            for name, sentence in self.invalid.items()
        ]
        said += [f"Règle « {name} » : motif trop lent, ignoré - simplifiez-le." for name in self.slow]
        return said

    @property
    def refusal(self) -> str:
        """Why an import does not run, or « »."""
        return self.refused("Import annulé", "importez à nouveau")

    def refused(self, outcome: str, then: str) -> str:
        """« <outcome> : <which rules cannot be applied>. Corrigez-la(-les) …,
        puis <then>. », or « » while every rule can be."""
        if not (self.invalid or self.slow):
            return ""
        named = [*self.invalid, *self.slow]
        names = ", ".join(f"« {name} »" for name in named)
        if len(named) == 1:
            said = f"la règle de reconnaissance {names} ne peut pas être appliquée (motif invalide ou trop lent)"
            fix = "Corrigez-la"
        else:
            said = (
                f"les règles de reconnaissance {names} ne peuvent pas être appliquées (motifs invalides ou trop lents)"
            )
            fix = "Corrigez-les"
        return f"{outcome} : {said}. {fix} sur « Reconnaissance des opérations », puis {then}."


def search(pattern, text: str):
    """`pattern.search(text)` under the per-match time limit, asked twice
    before it is taken for too slow (PatternError): the limit is the clock's,
    and one match can lose it to a busy server rather than to its pattern.
    Every per-match search of a statement goes through it - the rules' and
    the account pattern's (`statements._AccountSearch`)."""
    try:
        return patterns.search(pattern, text, patterns.Budget(patterns.PATTERN_TIMEOUT))
    except PatternError:
        return patterns.search(pattern, text, patterns.Budget(patterns.PATTERN_TIMEOUT))


def _aligned(text: str) -> bool:
    """Whether every character of `text` folds to exactly one: a match on
    `search_key(text)` then has the positions it would have on `text`."""
    return all(len(search_key(letter)) == 1 for letter in text)


class _AsPrinted:
    """A match found on the accent-free text, read on the text as printed -
    what `patterns.captured` asks of a match (`re`, `group`)."""

    def __init__(self, match, text: str):
        self._match = match
        self._text = text
        self.re = match.re

    def group(self, name):
        start, end = self._match.span(name)
        return None if start < 0 else self._text[start:end]


def compile_rules(rows) -> Rules:
    """The `Rules` of `rows` - OperationRule rows, or any objects carrying
    `name`, `meaning`, `searched` and `pattern` (a test's, a form's) - in the
    order given, the inactive ones (`is_active` False) left out. A pattern
    that no longer passes `check` is listed in `invalid`, never raised."""
    kinds, till, invalid = [], [], {}
    for row in rows:
        if not getattr(row, "is_active", True):
            continue
        try:
            compiled = check(row.meaning, row.searched, row.pattern)
        except PatternError as error:
            invalid[row.name] = error.message
            continue
        rule = Rule(row.name, row.meaning, row.searched, compiled, getattr(row, "pk", None))
        (kinds if row.meaning in KIND_MEANINGS else till).append(rule)
    return Rules(tuple(kinds), tuple(till), invalid)


# -- What the operation is ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Description:
    """What an operation is, as stored on its line at import."""

    kind: str = BankTransaction.Kind.OTHER.value
    counterparty: str = ""
    card_date: date | None = None
    #: The name of the rule that said so, « » for none.
    rule: str = ""

    @property
    def stored(self) -> tuple:
        return (self.kind, self.counterparty, self.card_date)


NOTHING = Description()


def describe(rules: Rules, label: str, bank_type: str = "", operation_date: date | None = None) -> Description:
    """What the first rule of `rules.kinds` finding its pattern says the
    operation is - « Autre », no payee and no card date where none does."""
    for rule in rules.kinds:
        match = rules.find(rule, label, bank_type)
        if match is None:
            continue
        card_date = _card_date(match, operation_date) if rule.meaning == Meaning.CARD_PAYMENT else None
        return Description(KIND_OF[rule.meaning], _payee(match), card_date, rule.name)
    return NOTHING


def _payee(match) -> str:
    payee = patterns.captured(match, PAYEE) or ""
    return " ".join(payee.split())[:PAYEE_MAX].rstrip()


def _number(text) -> int | None:
    return int(text) if text is not None and text.isascii() and text.isdigit() else None


def _card_date(match, operation_date: date | None) -> date | None:
    """The day the card was used: its day and month as captured, its year
    captured (2 digits are 20yy) or, without one, the latest year that does
    not put the card after the booking. None for anything that is no date."""
    day, month = _number(patterns.captured(match, DAY)), _number(patterns.captured(match, MONTH))
    if day is None or month is None:
        return None
    printed = patterns.captured(match, YEAR)
    if printed is not None:
        year = _number(printed)
        if year is None or len(printed) not in (2, 4):
            return None
        years = [2000 + year if len(printed) == 2 else year]
    elif operation_date is not None:
        years = [operation_date.year, operation_date.year - 1]
    else:
        return None
    for year in years:
        if not FIRST_YEAR <= year <= LAST_YEAR:
            continue
        try:
            used = date(year, month, day)
        except (ValueError, OverflowError):
            # OverflowError: a day or a month of ten digits and more, which
            # `date` refuses before it can say it is no day.
            continue
        if printed is not None or operation_date is None or used <= operation_date:
            return used
    return None


# -- What a credit is in the till -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TillReading:
    """What a rule says a credit is in the till."""

    source: str
    #: A payout's gross as its label prints it - None where the rule reads
    #: none (the amount received then counts as the gross) and on anything
    #: but a payout.
    gross: Decimal | None
    rule: str


def till_reading(rules: Rules, label: str, bank_type: str = "") -> TillReading | None:
    """What the first rule of `rules.till` finding its pattern says the
    credit is, or None where no rule recognises it. A payout rule whose
    `(?P<encaisse>…)` captured something that is no amount does not
    recognise the line: a gross misread would be a wrong commission, and
    the next rule is asked."""
    for rule in rules.till:
        match = rules.find(rule, label, bank_type)
        if match is None:
            continue
        if not rule.reads_gross:
            return TillReading(SOURCE_OF[rule.meaning], None, rule.name)
        printed = patterns.captured(match, GROSS)
        if printed is None:
            return TillReading(IncomeSource.CARD.value, None, rule.name)
        gross = patterns.read_amount(printed)
        if gross is not None:
            return TillReading(IncomeSource.CARD.value, gross, rule.name)
    return None


def printed_gross(rules: Rules, label: str, bank_type: str = "") -> Decimal | None:
    """The gross printed on a credit a PERSON said is a card payout (on the
    line, or for its payer): what the first payout rule reading a gross
    reads on it, or None - the amount received then counts as the gross."""
    for rule in rules.till:
        if not rule.reads_gross:
            continue
        match = rules.find(rule, label, bank_type)
        printed = patterns.captured(match, GROSS) if match is not None else None
        gross = patterns.read_amount(printed) if printed is not None else None
        if gross is not None:
            return gross
    return None


# -- The database ---------------------------------------------------------------------------------------------------


def load() -> Rules:
    """The active rules, in their order - one query."""
    return compile_rules(OperationRule.objects.filter(is_active=True).order_by("position", "name"))


@dataclass(frozen=True)
class Change:
    """A stored line the rules now read otherwise."""

    line: BankTransaction
    now: Description

    @property
    def kind_changes(self) -> bool:
        return self.line.kind != self.now.kind

    @property
    def payee_changes(self) -> bool:
        return self.line.counterparty != self.now.counterparty

    @property
    def date_changes(self) -> bool:
        return self.line.card_date != self.now.card_date


@dataclass
class Changes:
    """Every stored line the active rules read otherwise, and a digest of
    exactly that - what a confirmation is held to."""

    changes: list
    rules: Rules

    @property
    def digest(self) -> str:
        text = "\n".join(
            f"{one.line.pk}|{one.now.kind}|{one.now.counterparty}|{one.now.card_date or ''}" for one in self.changes
        )
        return hashlib.sha256(text.encode()).hexdigest()


def stored_changes(rules: Rules | None = None) -> Changes:
    """What `apply_changes` would write: every imported line whose kind,
    payee or card date the active rules now read otherwise, oldest first -
    one query for the lines (and one for the rules when not given)."""
    rules = load() if rules is None else rules
    lines = BankTransaction.objects.order_by("operation_date", "pk").only(
        "pk", "operation_date", "label", "bank_type", "kind", "counterparty", "card_date", "amount"
    )
    changes = []
    for line in lines:
        now = describe(rules, line.label, line.bank_type, line.operation_date)
        if now.stored != (line.kind, line.counterparty, line.card_date):
            changes.append(Change(line, now))
    return Changes(changes, rules)


class ChangedMeanwhile(Exception):
    """The lines or the rules moved since the changes were shown."""


def apply_changes(expected_digest: str) -> int:
    """Write what `stored_changes` reads - only if it is still exactly what
    was shown (`expected_digest`), else ChangedMeanwhile and nothing written.
    Refused (ValueError, the import's sentence) while a rule cannot be
    applied. Returns how many lines changed. Links, decisions and categories
    are left as they are; nothing is matched again here."""
    with transaction.atomic():
        pending = stored_changes()
        if pending.rules.refusal:
            raise ValueError(pending.rules.refused("Rien n'a changé", "relisez les opérations"))
        if pending.digest != expected_digest:
            raise ChangedMeanwhile
        for one in pending.changes:
            one.line.kind, one.line.counterparty, one.line.card_date = one.now.stored
        BankTransaction.objects.bulk_update(
            [one.line for one in pending.changes], ["kind", "counterparty", "card_date"], batch_size=500
        )
    return len(pending.changes)
