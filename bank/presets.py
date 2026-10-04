"""« Partir d'un modèle » (« Format du relevé », `#modeles`): a statement
format and the recognition rules that go with it, added in one click - for a
bar whose bank exports a standard file, or the owner's bank's CSV.

Three presets, `PRESETS`, in the order the page offers them:

* « Relevé OFX » - an OFX format, and rules reading the transaction types
  the OFX standard defines (`TRNTYPE`: POS a card payment, DIRECTDEBIT a
  debit, XFER and DIRECTDEP a transfer);
* « Relevé CAMT.053 » - a CAMT.053 format, and rules reading the ISO 20022
  bank transaction codes (`Domn/Fmly/SubFmly`: PMNT/CCRD/POSD a card
  payment, PMNT/RDDT a direct debit, PMNT/ICDT and PMNT/RCDT a transfer sent
  and received, PMNT/CNTR/CDPT cash deposited, PMNT/RCHQ cheques);
* « BNP Paribas (CSV) » - the format of migration 0007 and the eight rules
  of migration 0006, read off the migrations' own literals (`_migration`):
  one source, never a second copy that could drift from what every
  database was seeded with.

Every word of the first two is a standard's, not a bank's - but **no real
export has been seen yet**: whether a French bank prints those codes (an
OFX may say DEBIT and CREDIT for everything) must be checked against one
(CLAUDE.md, « The statement's layout »). Their rules carry no payee group:
a code says what an operation is, never whom it paid.

`install(preset)` writes what a database lacks, in one transaction: a
format or a rule is looked up by its name as `recognition.name_key` reads
it, and **one already there - under any spelling - is kept as it is**,
never overwritten; a new format comes after every format (the first by
position stays the import's default), new rules after every rule, in the
preset's order (positions are numbered across both questions, each ordered
on its own). Each row goes through its model's `full_clean`, the format's
check and the rules' pattern guard included. Installing twice writes once.

`set_up_new_espace()` is a step of `accounts.provisioning.
HOSTED_ESPACE_STEPS`, run once in a new espace that is not the owner's: it
gets the OFX and CAMT.053 presets AHEAD of the owner's bank's format - the
OFX one becomes its default -, the BNP format and its eight rules kept (a
correct preset for a bar banking there); their rules come after the BNP
ones, which read a payee the codes do not. The owner's espace, the
`_template` and every database migrated alone (the tests') keep the seeds
alone. Their names are durable (`NEW_ESPACE_FORMAT_NAMES`,
`NEW_ESPACE_RULE_NAMES`): espaces already provisioned hold them, and
`transfer.views.holds_only_seeds` knows a new espace's « Règles de la
banque » by them. A preset failing its own checks would fail the signup,
loudly (its ValidationError, the files removed): the tests run every preset
through them.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

from django.db import transaction

from . import recognition
from .models import OperationRule, StatementFormat

Meaning = OperationRule.Meaning
Searched = OperationRule.Searched
FileType = StatementFormat.FileType


def _migration(name: str):
    """A bank migration's module, for the literals it seeded with."""
    return importlib.import_module(f"bank.migrations.{name}")


#: What a format of a kind that names no column stores (as
#: `forms.CANONICAL`, which the page writes): the model's defaults.
_NO_COLUMN = {
    "encoding": StatementFormat.Encoding.AUTO.value,
    "delimiter": StatementFormat.Delimiter.SEMICOLON.value,
    "date_format": StatementFormat.DateFormat.DAY_MONTH_YEAR.value,
    "decimal_mark": StatementFormat.DecimalMark.COMMA.value,
    "date_column": None,
    "label_columns": "",
    "amount_column": None,
    "debit_column": None,
    "credit_column": None,
    "value_date_column": None,
    "bank_type_column": None,
    "account_pattern": "",
}


@dataclass(frozen=True)
class Preset:
    #: What the page posts (`modele`).
    key: str
    title: str
    description: str
    format_name: str
    #: The format's fields, its name and position aside.
    format_fields: dict
    #: (name, meaning, searched, pattern), in the order they are asked.
    rules: tuple = field(default_factory=tuple)


def _bnp() -> Preset:
    seeded_format, seeded_rules = _migration("0007_statement_formats"), _migration("0006_operation_rules")
    return Preset(
        key="bnp-csv",
        title="BNP Paribas (CSV)",
        description="L'export CSV de BNP Paribas, et ses huit règles de reconnaissance.",
        format_name=seeded_format.NAME,
        format_fields={name: value for name, value in seeded_format.FORMAT.items() if name != "position"},
        rules=tuple(
            (name, meaning, searched, pattern) for _position, name, meaning, searched, pattern in seeded_rules.RULES
        ),
    )


OFX = Preset(
    key="ofx",
    title="Relevé OFX",
    description="Un relevé OFX (Money, Quicken), et des règles lisant son type d'opération (TRNTYPE).",
    format_name="Relevé OFX",
    format_fields={**_NO_COLUMN, "file_type": FileType.OFX.value},
    rules=(
        ("Carte (OFX : POS)", Meaning.CARD_PAYMENT.value, Searched.BANK_TYPE.value, r"^POS$"),
        ("Prélèvement (OFX : DIRECTDEBIT)", Meaning.DEBIT.value, Searched.BANK_TYPE.value, r"^DIRECTDEBIT$"),
        ("Virement (OFX : XFER, DIRECTDEP)", Meaning.TRANSFER.value, Searched.BANK_TYPE.value, r"^(?:XFER|DIRECTDEP)$"),
    ),
)
CAMT053 = Preset(
    key="camt053",
    title="Relevé CAMT.053",
    description="Un relevé CAMT.053 (XML ISO 20022), et des règles lisant son code d'opération ISO.",
    format_name="Relevé CAMT.053",
    format_fields={**_NO_COLUMN, "file_type": FileType.CAMT053.value},
    rules=(
        ("Carte (ISO : PMNT/CCRD/POSD)", Meaning.CARD_PAYMENT.value, Searched.BANK_TYPE.value, r"^PMNT/CCRD/POSD\b"),
        ("Prélèvement (ISO : PMNT/RDDT)", Meaning.DEBIT.value, Searched.BANK_TYPE.value, r"^PMNT/RDDT/"),
        ("Virement émis (ISO : PMNT/ICDT)", Meaning.TRANSFER.value, Searched.BANK_TYPE.value, r"^PMNT/ICDT/"),
        ("Virement reçu (ISO : PMNT/RCDT)", Meaning.TRANSFER.value, Searched.BANK_TYPE.value, r"^PMNT/RCDT/"),
        ("Dépôt d'espèces (ISO : PMNT/CNTR/CDPT)", Meaning.CASH.value, Searched.BANK_TYPE.value, r"^PMNT/CNTR/CDPT\b"),
        ("Remise de chèques (ISO : PMNT/RCHQ)", Meaning.CHEQUE.value, Searched.BANK_TYPE.value, r"^PMNT/RCHQ/"),
    ),
)
BNP_CSV = _bnp()
PRESETS = (OFX, CAMT053, BNP_CSV)
BY_KEY = {preset.key: preset for preset in PRESETS}

#: What a new espace that is not the owner's is given ahead of the owner's
#: bank (`set_up_new_espace`). Never rename them: espaces already
#: provisioned hold them, and `transfer.views.holds_only_seeds` knows a new
#: espace by them.
NEW_ESPACE_PRESETS = (OFX, CAMT053)
NEW_ESPACE_FORMAT_NAMES = tuple(preset.format_name for preset in NEW_ESPACE_PRESETS)
NEW_ESPACE_RULE_NAMES = tuple(name for preset in NEW_ESPACE_PRESETS for name, *_rest in preset.rules)


@dataclass
class Installed:
    """What `install` wrote, and what it kept as it was."""

    preset: Preset
    #: The format created, None when one of its name was there.
    format: StatementFormat | None = None
    #: The rules created, in the preset's order.
    rules: list[OperationRule] = field(default_factory=list)
    #: The names already there, as stored: kept as they are.
    kept: list[str] = field(default_factory=list)

    @property
    def wrote(self) -> bool:
        return self.format is not None or bool(self.rules)


@transaction.atomic
def install(preset: Preset) -> Installed:
    """What `preset` brings that this database lacks, written: its format
    after every format, its rules after every rule, each checked by its
    model - and every name already there, whatever its spelling, kept as it
    is (`Installed.kept`). An IntegrityError (another request writing the
    same name between the look-up and the write) rolls all of it back and is
    raised for the caller to say."""
    done = Installed(preset)
    formats = {recognition.name_key(fmt.name): fmt for fmt in StatementFormat.objects.only("pk", "name")}
    there = formats.get(recognition.name_key(preset.format_name))
    if there is not None:
        done.kept.append(there.name)
    else:
        made = StatementFormat(name=preset.format_name, position=_after(StatementFormat), **preset.format_fields)
        made.full_clean()
        made.save()
        done.format = made
    rules = {recognition.name_key(rule.name): rule for rule in OperationRule.objects.only("pk", "name")}
    position = _after(OperationRule)
    for name, meaning, searched, pattern in preset.rules:
        there = rules.get(recognition.name_key(name))
        if there is not None:
            done.kept.append(there.name)
            continue
        made = OperationRule(
            name=name, meaning=meaning, searched=searched, pattern=pattern, position=position, is_active=True
        )
        made.full_clean()
        made.save()
        done.rules.append(made)
        position += 1
    return done


@transaction.atomic
def set_up_new_espace() -> None:
    """In a new espace that is not the owner's (`accounts.provisioning.
    HOSTED_ESPACE_STEPS`, bound to it): the OFX and CAMT.053 presets, their
    formats first in that order - an import reads OFX when nobody chooses -,
    every other format after them in its own order. Nothing is deleted."""
    for preset in NEW_ESPACE_PRESETS:
        install(preset)
    ahead = [recognition.name_key(name) for name in NEW_ESPACE_FORMAT_NAMES]
    formats = list(StatementFormat.objects.order_by("position", "name"))
    first = sorted(
        (fmt for fmt in formats if recognition.name_key(fmt.name) in ahead),
        key=lambda fmt: ahead.index(recognition.name_key(fmt.name)),
    )
    ordered = first + [fmt for fmt in formats if fmt not in first]
    for position, fmt in enumerate(ordered, start=1):
        fmt.position = position
    StatementFormat.objects.bulk_update(ordered, ["position"])


def _after(model) -> int:
    """The position after every row of `model` - 1 when there is none."""
    highest = model.objects.order_by("-position").values_list("position", flat=True).first()
    return (highest or 0) + 1


def holds(preset: Preset, format_names: set[str], rule_names: set[str]) -> bool:
    """Whether `preset`'s format and every one of its rules are there, by
    their `name_key` (`format_names`, `rule_names`): the page then says it is
    added already."""
    return recognition.name_key(preset.format_name) in format_names and all(
        recognition.name_key(name) in rule_names for name, *_rest in preset.rules
    )
