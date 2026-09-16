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
            compiled.append((rule, re.compile(rule.pattern, re.IGNORECASE)))
        except re.error:
            continue
    return compiled


def ignoring_rule(label: str, compiled):
    """The first rule whose pattern is found in `label`, or None."""
    for rule, regex in compiled:
        if regex.search(label):
            return rule
    return None
