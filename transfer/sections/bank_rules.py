"""« Règles de la banque »: what reads one bank's statements - how its export
is read (« Format du relevé », `StatementFormat`), the rules that say
what an operation is (« Reconnaissance des opérations », `OperationRule`)
and the rules for the payments that never have an invoice (« Dépenses sans
facture attendue », `IgnoreRule`).

Configuration, apart from the lines they read (sections/bank.py): another
bar on the same bank takes them alone, into a new espace, without one line
of this bar's statements. None of the three has a foreign key, so the
section requires nothing, and « Banque » only recommends it
(`registry.INFO`): clearing the rules never takes the lines, nor clearing
the lines the rules. A line keeps the kind, payee, card date and fingerprint
it was imported with - an import writes rows, it reads nothing again - so
rules or a format an archive brings read no statement already imported.

The three are merged the same way, like the payers retained: keyed by their
name as `bank.recognition.name_key` reads it (a format, a recognition rule)
or by their pattern (an ignore rule), every field but the moment compared -
the name as spelt (spelt otherwise, it was renamed) and the position (the
first format is the one an import uses when nobody chooses, the first rule
of its kind that finds its pattern decides) included. Different here: a
conflict kept under « Fusionner », replaced under « Remplacer », whose prune
deletes what the archive does not name. Every format and recognition rule
written, created or replaced, goes through the model's own check, its
patterns through the guard of `returnables.patterns` before anything
compiles them (`_check_format`, `_check_recognition`); every ignore rule
created through its own check too, the same guard (`_check_ignore_rule`) -
one stray « | » would hide every payment still missing its invoice -, its
pattern being its key: a replace never writes another. What no check made
before the lines are read can see - a pattern that backtracks without end on
some label - is stopped where it runs, by the time limit every match runs
under (bank/rules.py, bank/recognition.py).

They travelled inside banque.json until 02/10/2026, and the records keep
that shape: an older archive's banque.json is read through `archive.CARVED`,
its three lists handed to this section as its own payload. So each list may
be absent, and absent is « not said » - nothing created, compared or pruned
of it, never « forget them all »: an archive written before bank/0006 holds
no recognition rule, one written before bank/0007 no format.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError

from bank.forms import CANONICAL
from bank.models import IgnoreRule, OperationRule, StatementFormat
from bank.recognition import PATTERN_LABEL, name_key
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.sections.bank import named_fields, sentence
from transfer.sections.base import Section, delete_ids, restore_moments

KEY = "regles_banque"

# The JSON, field by field. The guard test holds every concrete field of the
# three models to being in one of these or in NOT_EXPORTED, so a field added
# to a model later cannot be left out in silence.
# The statement formats: the name is the key, as `name_key` reads it, and is
# compared all the same - a format spelt otherwise here was renamed. The
# position too: the first format is the one an import uses when nobody
# chooses.
FORMAT_FIELDS = (
    "name",
    "position",
    "file_type",
    "encoding",
    "delimiter",
    "date_format",
    "decimal_mark",
    "date_column",
    "label_columns",
    "amount_column",
    "debit_column",
    "credit_column",
    "value_date_column",
    "bank_type_column",
    "account_pattern",
    "created_at",
)
# Never the moment (§6.4): the same format typed a day later on another
# computer is « inchangé », not a conflict.
FORMAT_COMPARED = tuple(name for name in FORMAT_FIELDS if name != "created_at")
# No default in the model: a format the archive creates without one is
# skipped with « valeur manquante ». The date column is not one: blank since
# bank/0009 (an OFX or a CAMT.053 format names no column), a CSV without one
# is refused by the model's own check (`_check_format`).
FORMAT_REQUIRED = ("label_columns",)
# The kind of file of a format an archive written before bank/0009 holds:
# every format was a CSV then. Said rather than « not said », or a
# « Remplacer » would write that CSV's columns onto an OFX format of the
# same name here, which stays OFX.
FORMAT_FILE_TYPE_BEFORE_0009 = StatementFormat.FileType.CSV.value
# The kinds of file whose format names no column: what an archive says in a
# CSV's fields for one is not read, and is stored as « Format du relevé »
# stores it (`bank.forms.CANONICAL`) - never as said, which the model's
# check, skipping those fields for such a kind, let through.
NO_COLUMN_FILE_TYPES = frozenset(StatementFormat.FileType.values) - {StatementFormat.FileType.CSV.value}
# The recognition rules like the formats: the name is the key and is
# compared, the position too - the first rule of its kind that finds its
# pattern decides.
RECOGNITION_FIELDS = ("name", "meaning", "searched", "pattern", "position", "is_active", "created_at")
RECOGNITION_COMPARED = tuple(name for name in RECOGNITION_FIELDS if name != "created_at")
# No default in the model: a rule the archive creates without one is
# skipped with « valeur manquante ».
RECOGNITION_REQUIRED = ("meaning", "pattern")
# The ignore rules: the pattern is the key.
RULE_FIELDS = ("description", "is_active", "category", "created_at")
RULE_COMPARED = ("description", "is_active", "category")
RULE_KEYS = ("pattern", *RULE_FIELDS)

EXPORTED = {
    StatementFormat: FORMAT_FIELDS,
    OperationRule: RECOGNITION_FIELDS,
    IgnoreRule: RULE_KEYS,
}
NOT_EXPORTED = {
    StatementFormat: {"id": "pk"},
    OperationRule: {"id": "pk"},
    IgnoreRule: {"id": "pk"},
}

# The three lists, each optional (« not said » when absent). An older
# archive's banque.json holds them under the same names (`archive.CARVED`).
TOP_LEVEL = ("statement_formats", "operation_rules", "rules")

# Report rows, in the order of count(): the page's table and the picker's
# counts say the same words - and `archive.CARVED` relabels an older
# archive's counts to these (« règles » was the bank's word for the third).
FORMATS = "formats de relevé"
RECOGNITION = "règles de reconnaissance"
# Not « règles » alone: the section holds three kinds of rules.
RULES = "règles « sans facture »"
ENTITIES = (FORMATS, RECOGNITION, RULES)

FIELD_LABELS = {
    "description": "nom",
    "is_active": "active",
    "category": "catégorie",
    "name": "nom",
    "meaning": "signifie",
    "searched": "cherché dans",
    "pattern": "motif",
    "position": "ordre",
    "file_type": "type de fichier",
    "encoding": "encodage",
    "delimiter": "séparateur",
    "date_format": "format des dates",
    "decimal_mark": "séparateur décimal",
    "date_column": "colonne de la date",
    "label_columns": "colonnes du libellé",
    "amount_column": "colonne du montant",
    "debit_column": "colonne des débits",
    "credit_column": "colonne des crédits",
    "value_date_column": "colonne de la date de valeur",
    "bank_type_column": "colonne du type d'opération",
    "account_pattern": "motif du numéro de compte",
}

# Said when a clear takes the statement formats: the seeded one goes too,
# and an import is refused until one is back.
FORMAT_CLEAR_NOTE = (
    "Sans format de relevé, aucun relevé ne s'importe : ramenez les formats de la sauvegarde ou saisissez-en un "
    "sur « Format du relevé »."
)
# Said when a clear takes the recognition rules: the seeded ones go too.
RECOGNITION_CLEAR_NOTE = (
    "Sans règles de reconnaissance, un relevé importé n'est plus reconnu : ramenez-les de la sauvegarde ou "
    "saisissez-les sur « Reconnaissance des opérations »."
)

# The highest position an archive may give a recognition rule or a format:
# Django's range for a PositiveIntegerField on every backend but SQLite,
# whose own (to 2**63 - 1) leaves the pages no room above it - a new one
# comes at the highest + 1 and positions shared are made distinct by + 1
# (bank/views.py `_saved`, `_swapped`), and past 2**63 - 1 that is an
# OverflowError: every new format or rule saved after it was a 500.
MAX_POSITION = 2**31 - 1


def _check_recognition(rule: OperationRule) -> None:
    """The model's own check, the one the page's form runs: `clean` hands
    the pattern to `bank.recognition.check`, the guard of
    `returnables.patterns` first - a pattern that could freeze the machine
    is refused before anything compiles it. Raised as a FieldValueError, in
    French: the pattern's field validators are left out (Django's « cannot
    be blank » beside the model's own sentence is English), and a field
    Django's validators refuse - a position past what SQLite holds - is
    named, as is a position past `MAX_POSITION`."""
    try:
        rule.full_clean(exclude=["pattern"])
    except ValidationError as exc:
        errors = exc.message_dict
        if "pattern" in errors:
            reason = " ".join(errors["pattern"]).removeprefix(f"{PATTERN_LABEL} : ").rstrip(".")
            raise codec.FieldValueError(f"motif refusé — {reason}") from None
        raise codec.FieldValueError(f"« {next(iter(errors))} » : valeur refusée") from None
    _check_position(rule)


def _check_ignore_rule(rule: IgnoreRule) -> None:
    """The model's own check, the one the page's form runs: `clean` hands
    the pattern to `bank.rules.check`, the guard of `returnables.patterns` -
    a pattern that could freeze the machine is refused before anything
    compiles it, one `re` could not even count (« A{4294967296} », an
    OverflowError) is a refusal and no longer a 500 on the preview, and one
    finding something in an empty label would hide every payment still
    missing its invoice. Raised as a FieldValueError, in French, as a
    recognition rule's (`_check_recognition`): the pattern's field
    validators are left out (its length is `codec.load`'s), and a field
    Django's validators refuse is named."""
    try:
        rule.full_clean(exclude=["pattern"])
    except ValidationError as exc:
        errors = exc.message_dict
        if "pattern" in errors:
            reason = " ".join(errors["pattern"]).removeprefix(f"{PATTERN_LABEL} : ").rstrip(".")
            raise codec.FieldValueError(f"motif refusé — {reason}") from None
        raise codec.FieldValueError(f"« {next(iter(errors))} » : valeur refusée") from None


def _check_position(row: OperationRule | StatementFormat) -> None:
    """A position the pages can still put another after (`MAX_POSITION`)."""
    if row.position > MAX_POSITION:
        raise codec.FieldValueError("« position » : valeur refusée")


def _check_format(fmt: StatementFormat) -> None:
    """The model's own check, the one « Format du relevé »'s form runs:
    `clean` hands the format to `bank.statements.check_format` - the columns,
    the choices, an amount said once, and the account pattern through the
    guard of `returnables.patterns` before anything compiles it. Raised as a
    FieldValueError, in French: that check's own sentence, after the field
    it names. Only then Django's validators (a position past what SQLite
    holds, a name another format here has), whose English is never said:
    the field is named instead - and a position past `MAX_POSITION`."""
    try:
        fmt.clean()
    except ValidationError as exc:
        errors = exc.message_dict if hasattr(exc, "error_dict") else {"": exc.messages}
        field, messages = next(iter(errors.items()))
        label = FIELD_LABELS.get(field, field)
        raise codec.FieldValueError(f"format refusé — {label} : {sentence(' '.join(messages), label)}") from None
    try:
        fmt.full_clean()
    except ValidationError as exc:
        raise codec.FieldValueError(f"« {next(iter(exc.message_dict))} » : valeur refusée") from None
    _check_position(fmt)


@registry.register
class BankRulesSection(Section):
    key = KEY

    # -- what this database holds ------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            FORMATS: StatementFormat.objects.count(),
            RECOGNITION: OperationRule.objects.count(),
            RULES: IgnoreRule.objects.count(),
        }

    def snapshot(self):
        return {
            # By name, which is unique: the lists never compare a column
            # that may be None.
            "statement_formats": sorted(
                list(codec.record(fmt, FORMAT_FIELDS).values()) for fmt in StatementFormat.objects.all()
            ),
            # The order is in it: `position` is a field like the others.
            "operation_rules": sorted(
                list(codec.record(rule, RECOGNITION_FIELDS).values()) for rule in OperationRule.objects.all()
            ),
            "rules": sorted(
                [rule.pattern, *codec.record(rule, RULE_FIELDS).values()] for rule in IgnoreRule.objects.all()
            ),
        }

    # -- export ----------------------------------------------------------------
    def export(self, out) -> None:
        # In the order the import card offers them, the first the default.
        formats = list(StatementFormat.objects.order_by("position", "name"))
        # In the order they are asked: rules of one position come back in it.
        recognition = list(OperationRule.objects.order_by("position", "name"))
        rules = list(IgnoreRule.objects.order_by("id"))
        out.write(
            {
                # Every list always said, empty included: absent, it reads as
                # an archive written before it existed (« not said »), and
                # « Remplacer » would keep this database's. A tab separator
                # is JSON's « \t » and reads back as the tab it was.
                "statement_formats": [codec.record(fmt, FORMAT_FIELDS) for fmt in formats],
                "operation_rules": [codec.record(rule, RECOGNITION_FIELDS) for rule in recognition],
                "rules": [{"pattern": rule.pattern, **codec.record(rule, RULE_FIELDS)} for rule in rules],
            },
            {FORMATS: len(formats), RECOGNITION: len(recognition), RULES: len(rules)},
        )

    # -- import ----------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        # Each list may be left out - None is « not said », never « forget
        # them all here », which is what an empty list says under
        # « Remplacer »: an archive written before bank/0006 holds no
        # recognition rule, one written before bank/0007 no format, and the
        # banque.json of an archive written before this section
        # (`archive.CARVED`) only what it held then. Named after the file
        # actually read: an older archive's is banque.json.
        for name in TOP_LEVEL:
            items = payload.get(name)
            if items is not None and (not isinstance(items, list) or not all(isinstance(item, dict) for item in items)):
                raise ArchiveError(f"Archive refusée : dans {src.member}, « {name} » n'est pas une liste d'objets.")
        self.payload = payload
        self._member = src.member
        self._formats: list | None = payload.get("statement_formats")
        self._recognition: list | None = payload.get("operation_rules")
        self._rules: list | None = payload.get("rules")
        # What the file names, whatever becomes of its records: prune never
        # deletes a format or a rule the archive holds, even one it could not
        # read. By key, and the one row here each key answers to (a second
        # of the same key is one too many).
        self._format_keys: set[str] = set()
        self._format_ids: dict[str, int] = {}
        self._recognition_keys: set[str] = set()
        self._recognition_ids: dict[str, int] = {}
        self._patterns: set[str] = set()
        self._rule_ids: dict[str, int] = {}

    def apply(self, ctx, report) -> None:
        for entity in ENTITIES:  # one row each, even when nothing moves
            report.unchanged(entity, 0)
        codec.note_unknown(report, self.payload, TOP_LEVEL, where=f"{self._member} › ")
        replacing = ctx.replacing(self.key)
        self._apply_formats(report, replacing)
        self._apply_recognition(report, replacing)
        self._apply_rules(report, replacing)

    # statement formats -------------------------------------------------------
    def _apply_formats(self, report, replacing: bool) -> None:
        """How the bank's export is read (« Format du relevé »):
        configuration - one changed here is a conflict, kept. Every format
        written goes through the model's own check (`_check_format`), created
        or replaced: an archive may say any column and any account pattern.
        An archive saying nothing of them (written before bank/0007) leaves
        them alone; a format saying no kind of file (written before
        bank/0009) is a CSV, as every format was then, and one of a kind
        that names no column has a CSV's fields as the page stores them
        (`NO_COLUMN_FILE_TYPES`), whatever the archive says there."""
        if self._formats is None:
            return
        existing: dict[str, StatementFormat] = {}
        for fmt in StatementFormat.objects.order_by("position", "name"):
            existing.setdefault(name_key(fmt.name), fmt)
        created = []
        for record in self._formats:
            codec.note_unknown(report, record, FORMAT_FIELDS, where="formats de relevé › ")
            if "file_type" not in record:
                record = {**record, "file_type": FORMAT_FILE_TYPE_BEFORE_0009}
            file_type = record.get("file_type")
            if isinstance(file_type, str) and file_type in NO_COLUMN_FILE_TYPES:
                record = {**record, **CANONICAL}
            name = record.get("name")
            key = name_key(name) if isinstance(name, str) else ""
            if not key:
                report.skip("Format de relevé sans nom")
                continue
            said = f"Format de relevé « {name} »"
            if key in self._format_keys:
                report.skip(f"{said} : en double dans l'archive")
                continue
            # Named before its record is read: the prune never deletes a
            # format the archive holds, even one it could not read.
            self._format_keys.add(key)
            fmt = existing.get(key)
            try:
                codec.load(StatementFormat, "name", name)
                if fmt is None:
                    for field_name in FORMAT_REQUIRED:
                        codec.load(StatementFormat, field_name, record.get(field_name))
                    fmt = StatementFormat()
                    codec.assign(fmt, record, FORMAT_FIELDS)
                    _check_format(fmt)
                    moment = fmt.created_at
                    fmt.save()
                    created.append((fmt, moment))
                    self._format_ids[key] = fmt.pk
                    report.created(FORMATS)
                    continue
                self._format_ids[key] = fmt.pk
                different = codec.differences(fmt, record, FORMAT_COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"{said} : {exc}")
                continue
            if not different:
                report.unchanged(FORMATS)
            elif replacing:
                # Every field, the moment included, then the model's check:
                # the archive's columns replacing these are checked as a
                # format it creates. What cannot be read leaves it as it was.
                try:
                    changed = codec.assign(fmt, record, FORMAT_FIELDS)
                    _check_format(fmt)
                except codec.FieldValueError as exc:
                    report.skip(f"{said} : {exc}")
                    continue
                fmt.save(update_fields=changed)
                report.updated(FORMATS)
            else:
                report.conflict(
                    f"{said} : différent dans l'archive ({named_fields(different, FIELD_LABELS)}) — gardé tel quel"
                )
        restore_moments(created, "created_at")

    # recognition rules -------------------------------------------------------
    def _apply_recognition(self, report, replacing: bool) -> None:
        """What an operation of the statement is (« Reconnaissance des
        opérations »): configuration - one changed here is a conflict, kept.
        Every rule written goes through the model's own check
        (`_check_recognition`), created or replaced, since the pattern is no
        key here and an archive may say any. An archive saying nothing of
        them (written before bank/0006) leaves them alone. Nothing already
        imported is read again with them."""
        if self._recognition is None:
            return
        existing: dict[str, OperationRule] = {}
        for rule in OperationRule.objects.order_by("position", "name"):
            existing.setdefault(name_key(rule.name), rule)
        created = []
        for record in self._recognition:
            codec.note_unknown(report, record, RECOGNITION_FIELDS, where="règles de reconnaissance › ")
            name = record.get("name")
            key = name_key(name) if isinstance(name, str) else ""
            if not key:
                report.skip("Règle de reconnaissance sans nom")
                continue
            said = f"Règle de reconnaissance « {name} »"
            if key in self._recognition_keys:
                report.skip(f"{said} : en double dans l'archive")
                continue
            # Named before its record is read: the prune never deletes a rule
            # the archive holds, even one it could not read.
            self._recognition_keys.add(key)
            rule = existing.get(key)
            try:
                codec.load(OperationRule, "name", name)
                if rule is None:
                    for field_name in RECOGNITION_REQUIRED:
                        codec.load(OperationRule, field_name, record.get(field_name))
                    rule = OperationRule()
                    codec.assign(rule, record, RECOGNITION_FIELDS)
                    _check_recognition(rule)
                    moment = rule.created_at
                    rule.save()
                    created.append((rule, moment))
                    self._recognition_ids[key] = rule.pk
                    report.created(RECOGNITION)
                    continue
                self._recognition_ids[key] = rule.pk
                different = codec.differences(rule, record, RECOGNITION_COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"{said} : {exc}")
                continue
            if not different:
                report.unchanged(RECOGNITION)
            elif replacing:
                # Every field, the moment included, then the model's check:
                # an archive's pattern replacing this one is checked as one
                # it creates. What cannot be read leaves the rule as it was.
                try:
                    changed = codec.assign(rule, record, RECOGNITION_FIELDS)
                    _check_recognition(rule)
                except codec.FieldValueError as exc:
                    report.skip(f"{said} : {exc}")
                    continue
                rule.save(update_fields=changed)
                report.updated(RECOGNITION)
            else:
                report.conflict(
                    f"{said} : différente dans l'archive ({named_fields(different, FIELD_LABELS)}) — "
                    "gardée telle quelle"
                )
        restore_moments(created, "created_at")

    # ignore rules ------------------------------------------------------------
    def _apply_rules(self, report, replacing: bool) -> None:
        """The payments that never have an invoice (« Dépenses sans facture
        attendue »), by their pattern: one changed here is a conflict, kept.
        A rule created goes through the model's own check
        (`_check_ignore_rule`), the guard of `returnables.patterns`; one
        already here is its pattern, and was checked when it was written
        (one written before the guard, and refused by it now, hides nothing
        and says so where it is drawn - bank/rules.py). An archive saying
        nothing of them (a hand-made one: every archive written so far holds
        the list) leaves them alone."""
        if self._rules is None:
            return
        existing: dict[str, IgnoreRule] = {}
        for rule in IgnoreRule.objects.order_by("id"):
            existing.setdefault(rule.pattern, rule)
        created = []
        for record in self._rules:
            # Under its row's label, as the formats' and the recognition
            # rules' notes are: « règles » alone was banque.json's word.
            codec.note_unknown(report, record, RULE_KEYS, where=f"{RULES} › ")
            pattern = record.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                report.skip("Règle sans motif")
                continue
            if pattern in self._patterns:
                report.skip(f"Règle « {pattern} » : en double dans l'archive")
                continue
            self._patterns.add(pattern)
            rule = existing.get(pattern)
            try:
                codec.load(IgnoreRule, "pattern", pattern)
                if rule is None:
                    rule = IgnoreRule(pattern=pattern)
                    codec.assign(rule, record, RULE_FIELDS)
                    _check_ignore_rule(rule)
                    moment = rule.created_at
                    rule.save()
                    created.append((rule, moment))
                    self._rule_ids[pattern] = rule.pk
                    report.created(RULES)
                    continue
                self._rule_ids[pattern] = rule.pk
                different = codec.differences(rule, record, RULE_COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"Règle « {pattern} » : {exc}")
                continue
            if not different:
                report.unchanged(RULES)
            elif replacing:
                # Every field, not just the compared ones: a moment that
                # cannot be read is that record's reason, never a 500.
                try:
                    changed = codec.assign(rule, record, RULE_FIELDS)
                except codec.FieldValueError as exc:
                    report.skip(f"Règle « {pattern} » : {exc}")
                    continue
                rule.save(update_fields=changed)
                report.updated(RULES)
            else:
                report.conflict(
                    f"Règle « {pattern} » : différente dans l'archive ({named_fields(different, FIELD_LABELS)}) — "
                    f"gardée telle quelle"
                )
        restore_moments(created, "created_at")

    def prune(self, ctx, report) -> None:
        # Nothing in any other section points at a format or a rule: what the
        # archive does not name goes - of a list it said. A list it left out
        # prunes nothing.
        if self._formats is not None:
            doomed = []
            for pk, name in StatementFormat.objects.values_list("pk", "name"):
                key = name_key(name)
                if key not in self._format_keys or self._format_ids.get(key, pk) != pk:
                    doomed.append(pk)
            if doomed:
                report.deleted(FORMATS, delete_ids(StatementFormat, doomed))
        if self._recognition is not None:
            doomed = []
            for pk, name in OperationRule.objects.values_list("pk", "name"):
                key = name_key(name)
                if key not in self._recognition_keys or self._recognition_ids.get(key, pk) != pk:
                    doomed.append(pk)
            if doomed:
                report.deleted(RECOGNITION, delete_ids(OperationRule, doomed))
        if self._rules is not None:
            doomed = [
                pk
                for pk, pattern in IgnoreRule.objects.values_list("pk", "pattern")
                # A second rule with a pattern the archive has once is one too many.
                if pattern not in self._patterns or (pattern in self._rule_ids and self._rule_ids[pattern] != pk)
            ]
            if doomed:
                report.deleted(RULES, delete_ids(IgnoreRule, doomed))

    # -- clear -----------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        # The format bank/0007 seeds and the rules bank/0006 seeds go too: the
        # Effacer tab says so before (`registry.INFO`'s clear_note), the notes
        # after.
        counts = {
            FORMATS: StatementFormat.objects.all().delete()[1].get(StatementFormat._meta.label, 0),
            RECOGNITION: OperationRule.objects.all().delete()[1].get(OperationRule._meta.label, 0),
            RULES: IgnoreRule.objects.all().delete()[1].get(IgnoreRule._meta.label, 0),
        }
        for entity in ENTITIES:
            report.deleted(entity, counts[entity])
        if counts[FORMATS]:
            report.note(FORMAT_CLEAR_NOTE)
        if counts[RECOGNITION]:
            report.note(RECOGNITION_CLEAR_NOTE)
