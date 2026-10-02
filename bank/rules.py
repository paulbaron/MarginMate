"""Which ignore rule, if any, a payment's label matches. Plain values - see
bank.models.IgnoreRule for what a rule means."""

from __future__ import annotations

import re


def compile_rules(rules) -> list[tuple[object, re.Pattern]]:
    """(rule, compiled pattern) for each rule, case ignored. A pattern that
    does not compile - never saved through the form - hides nothing."""
    compiled = []
    for rule in rules:
        try:
            compiled.append((rule, searcher(rule.pattern)))
        except re.error:
            continue
    return compiled


def searcher(pattern: str) -> re.Pattern:
    """`pattern` compiled for `search`, case ignored; re.error for a pattern
    that does not compile, exactly as `re.compile` raises it.

    A leading greedy « .* » is left out of what is searched: `search` tries
    every position anyway and « .* » may match nothing, so « .*URSSAF.* »
    is found in a label exactly when « URSSAF.* » is - including where
    « .*A|B » leaves « A|B ». Kept, it runs to the end of the label and back
    at every position: measured on a statement's labels, sixty times the
    search of the same word alone, and most of « Dépenses sans facture
    attendue »'s time. A lazy or possessive « .* » (« .*? », « .*+ ») is
    left as written, and so is a pattern whose rest does not compile."""
    regex = re.compile(pattern, re.IGNORECASE)
    rest = pattern
    while rest.startswith(".*") and rest[2:3] not in ("?", "+", "*", "{"):
        rest = rest[2:]
    if rest == pattern:
        return regex
    try:
        return re.compile(rest, re.IGNORECASE)
    except re.error:
        return regex


def ignoring_rule(label: str, compiled):
    """The first rule whose pattern is found in `label`, or None."""
    for rule, regex in compiled:
        if regex.search(label):
            return rule
    return None
