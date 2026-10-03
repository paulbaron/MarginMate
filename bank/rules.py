"""Which ignore rule, if any, a payment's label matches. Plain values - see
bank.models.IgnoreRule for what a rule means.

**A pattern is never trusted, typed or stored**, as for the recognition
rules (`bank.recognition`): `check` goes through `returnables.patterns`,
which refuses a pattern before `regex` could freeze the machine compiling it,
and every match runs under the per-match time limit, its rule billed the
time it spends (`Matcher`). The rules of one reading - one page drawn, one
automatic pass - are compiled once (`compile_rules`); a stored rule that no
longer passes `check` (`IgnoreRules.invalid`), or that turns out too slow
(`IgnoreRules.slow`), hides nothing, and the pages say which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import thread_time

from returnables import patterns
from returnables.patterns import PatternError

from . import recognition

#: The label a pattern is refused under - the form's « Motif ».
PATTERN_LABEL = "Motif"


def check(pattern):
    """`pattern` compiled, case ignored - or PatternError, a French sentence
    starting « Motif : ». The guard of `returnables.patterns` first (an empty
    pattern, one too costly to compile), and a pattern that finds something
    in an empty label (".*", "URSSAF|", "^") is refused there too: one stray
    "|" would hide every payment still missing its invoice."""
    return patterns.compile_pattern(pattern, field_label=PATTERN_LABEL)


def searcher(pattern):
    """`check(pattern)`, with a leading greedy « .* » left out of what is
    searched - PatternError as `check` raises it.

    `search` tries every position anyway and « .* » may match nothing, so
    « .*URSSAF.* » is found in a label exactly when « URSSAF.* » is -
    including where « .*A|B » leaves « A|B ». Kept, it runs to the end of
    the label and back at every position: measured on a statement's labels,
    sixty times the search of the same word alone, and most of « Dépenses
    sans facture attendue »'s time. A lazy or possessive « .* » (« .*? »,
    « .*+ ») is left as written, and so is a pattern whose rest the guard
    refuses. The pattern as written is what is checked."""
    regex = check(pattern)
    written = regex.pattern
    rest = written
    while rest.startswith(".*") and rest[2:3] not in ("?", "+", "*", "{"):
        rest = rest[2:]
    if rest == written:
        return regex
    try:
        return check(rest)
    except PatternError:
        return regex


class Matcher:
    """One rule's compiled pattern over one reading. Each search runs as
    `recognition.search` runs a recognition rule's: under the per-match
    limit (`PATTERN_TIMEOUT`, the GIL released), asked once more when out
    of time - the limit is the clock's, and one match can lose it to a busy
    server rather than to its pattern. And the thread's CPU time is billed
    to it (`bill`), against `recognition.RULE_SECONDS`, as a recognition
    rule's is. Out of time twice running, or past that time in all, it is
    `slow` and finds nothing more.

    `regex` is searched here rather than through `recognition.search`, and
    billed by its caller from one clock read per search rather than two:
    every draw of Banque and « Dépenses » searches each open debit with
    each rule, and the `Budget` built per search and the second read were
    two thirds of the time (measured, 02/10/2026: 7 ms for a year's debits
    with `re` and no limit, 22 ms that way, 12-13 ms this one)."""

    def __init__(self, regex):
        self.regex = regex
        self.spent = 0.0
        self.slow = False

    def finds(self, label: str) -> bool:
        if self.slow:
            return False
        try:
            return self.regex.search(label, timeout=patterns.PATTERN_TIMEOUT, concurrent=True) is not None
        except TimeoutError:
            pass
        try:
            return self.regex.search(label, timeout=patterns.PATTERN_TIMEOUT, concurrent=True) is not None
        except TimeoutError:
            self.slow = True
            return False

    def bill(self, seconds: float) -> None:
        """`seconds` of the thread's CPU time spent searching - never the
        clock's: the server's other requests are threads too, and a match
        waiting behind them for the GIL is not a slow pattern (see
        `recognition.Rules.find`)."""
        self.spent += seconds
        if self.spent > recognition.RULE_SECONDS:
            self.slow = True


def caught(regex, lines) -> list | None:
    """The `lines` whose label `regex` - `searcher`'s - finds, each searched
    as a page searches it (`Matcher`) - or None when it turned out too slow
    on them: what « Tester » and the rules page count."""
    matcher = Matcher(regex)
    found = []
    clock = thread_time()
    for line in lines:
        if matcher.finds(line.label):
            found.append(line)
        now = thread_time()
        matcher.bill(now - clock)
        clock = now
        if matcher.slow:
            return None
    return found


@dataclass
class IgnoreRules:
    """The active ignore rules of one reading, compiled once, in their
    order - and what went wrong with any of them since."""

    #: (rule, Matcher) of every rule whose pattern passes `check`.
    rules: tuple = ()
    #: (rule, sentence) of every stored rule whose pattern does not: it
    #: hides nothing.
    invalid: tuple = ()
    #: The rules found too slow so far, in the order found: they hide
    #: nothing more for this reading.
    slow: list = field(default_factory=list)

    def first(self, label: str):
        """The first rule whose pattern is found in `label`, or None."""
        # One clock read a search: what a search spends is the time since
        # the last read, the loop's own few instructions billed with it.
        clock = thread_time()
        for rule, matcher in self.rules:
            if matcher.slow:
                continue
            found = matcher.finds(label)
            now = thread_time()
            matcher.bill(now - clock)
            clock = now
            if matcher.slow:
                self.slow.append(rule)
            if found:
                return rule
        return None

    @property
    def problems(self) -> list[str]:
        """What a page says about the rules that did not apply, in French -
        each read after the last line: a rule turns out slow only once it
        has read lines."""
        said = [
            f"Règle « {rule} » : {reason_of(sentence)} - elle ne s'applique à aucune opération."
            for rule, sentence in self.invalid
        ]
        said += [f"Règle « {rule} » : motif trop lent, ignoré - simplifiez-le." for rule in self.slow]
        return said


def reason_of(sentence: str) -> str:
    """A refusal of `check` without the field it is said on, nor its stop."""
    return sentence.removeprefix(f"{PATTERN_LABEL} : ").rstrip(".")


def compile_rules(rules) -> IgnoreRules:
    """The `IgnoreRules` of `rules` - IgnoreRule rows, or any objects
    carrying `pattern` - in the order given. A pattern that no longer passes
    `check` (saved before the guard, typed or imported) is listed in
    `invalid`, never raised."""
    compiled, invalid = [], []
    for rule in rules:
        try:
            compiled.append((rule, Matcher(searcher(rule.pattern))))
        except PatternError as error:
            invalid.append((rule, error.message))
    return IgnoreRules(tuple(compiled), tuple(invalid))


def ignoring_rule(label: str, rules: IgnoreRules):
    """The first rule whose pattern is found in `label`, or None."""
    return rules.first(label)
