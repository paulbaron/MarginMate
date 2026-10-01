"""What the bank tests share about recognising operations (bank/recognition.py).

The rules of migration 0006 are in every test database, the _template every
new espace is copied from included: a test reading through the database
(`reconcile.import_statement`, `income.income_for`, the pages) is read by
them with nothing to set up. A pure test - `statements.parse_statement`,
`income.entry_for` and their kin, on unsaved rows - is handed them instead:
`SEEDED`, compiled from the migration's own list, no database.

Every label, payee and amount the tests read with them is invented.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from bank import recognition
from bank.models import OperationRule

#: The seed migration itself, read for its data (test_recognition pins it
#: apart, with literals, so a slip in the migration is caught rather than
#: copied here).
SEED = importlib.import_module("bank.migrations.0006_operation_rules")
SEEDED_NAMES = tuple(name for _position, name, _meaning, _searched, _pattern in SEED.RULES)


def rule(name, meaning, pattern, searched=OperationRule.Searched.LABEL, *, position=0, is_active=True):
    """One rule as `recognition.compile_rules` reads it - no database."""
    return SimpleNamespace(
        name=name, meaning=meaning, searched=searched, pattern=pattern, position=position, is_active=is_active
    )


def seeded_rows() -> list:
    """The seeded rules as rows, in their order."""
    return [
        rule(name, meaning, pattern, searched, position=position)
        for position, name, meaning, searched, pattern in SEED.RULES
    ]


def seeded() -> recognition.Rules:
    """The seeded rules compiled afresh: for a test that may find one of
    them too slow (`Rules.slow` is kept on the object it is found on)."""
    return recognition.compile_rules(seeded_rows())


def rules_of(*rows) -> recognition.Rules:
    """`rows` compiled in the order given."""
    return recognition.compile_rules(rows)


#: The seeded rules, compiled once - what every database recognises with.
#: Never handed to a test that makes a rule run past its time limit.
SEEDED = seeded()


def make_rule(name, meaning, pattern, searched=OperationRule.Searched.LABEL, *, position=None, is_active=True):
    """A rule in the database, checked like the form checks it, after every
    rule already there unless `position` says otherwise."""
    if position is None:
        last = OperationRule.objects.order_by("-position").values_list("position", flat=True).first()
        position = (last or 0) + 1
    made = OperationRule(
        name=name, meaning=meaning, searched=searched, pattern=pattern, position=position, is_active=is_active
    )
    made.full_clean()
    made.save()
    return made


def pause_seeded_rules() -> None:
    """The seeded rules turned off: an espace at another bank."""
    OperationRule.objects.filter(name__in=SEEDED_NAMES).update(is_active=False)
