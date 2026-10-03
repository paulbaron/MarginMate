"""« Règles de la banque » (`regles_banque`, sections/bank_rules.py, §10.2):
the statement formats (« Format du relevé »), the recognition rules
(« Reconnaissance des opérations ») and the ignore rules (« Dépenses sans
facture attendue ») - what reads one bank's statements, apart from the lines
it read (test_bank_section.py).

What these guard is how the owner's bank is read, typed once and taken to
another bar on the same bank: a round trip must bring every format and rule
back exactly, in its order; a merge must never overwrite one changed here; a
list an archive does not hold - written before bank/0006 or bank/0007 - must
never read as « forget them all »; and an archive written before this
section existed (every safety backup until 02/10/2026) must still bring
them, its banque.json read through `archive.CARVED`.

Every test database holds the recognition rules bank/0006 seeds (`SEEDED`)
and the format bank/0007 seeds (`SEEDED_FORMAT`): a new database still reads
and recognises the owner's bank.

Every name, pattern and label below is invented.
"""

import importlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

import regex
from django.test import TestCase
from django.urls import reverse

from bank import recognition
from bank.models import BankTransaction, IgnoreRule, OperationRule, StatementFormat
from returnables.tests.test_patterns import NeverCompile
from transfer import archive, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections import bank
from transfer.sections import bank_rules as section
from transfer.sections.bank_rules import (
    ENTITIES,
    FORMAT_CLEAR_NOTE,
    FORMATS,
    KEY,
    RECOGNITION,
    RECOGNITION_CLEAR_NOTE,
    RULES,
    TOP_LEVEL,
    BankRulesSection,
)
from transfer.sections.base import Strategy
from transfer.tests.support import db_fingerprint, export_archive, forge, import_archive, round_trip

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
RULE_MOMENT = datetime(2026, 7, 20, 8, 30, tzinfo=UTC)
RECOGNITION_MOMENT = datetime(2026, 7, 21, 9, 15, 45, 500000, tzinfo=UTC)
FORMAT_MOMENT = datetime(2026, 7, 22, 16, 40, 5, 750000, tzinfo=UTC)
#: (position, name, meaning, searched, pattern) of the rules bank/0006
#: seeds into every database, this test's included.
SEEDED = importlib.import_module("bank.migrations.0006_operation_rules").RULES
#: A rule a person typed for another bank's payment terminal, which prints
#: no gross (RulesData).
TERMINAL_RULE = "Versement TPE (EXEMPLE PAY)"
#: A seeded rule: the third question's « Remise de chèques ».
CHEQUES = "Remise de chèques (REMISE CHEQUE)"
#: The name and the layout of the format bank/0007 seeds into every
#: database, this test's included.
_SEEDED_FORMATS = importlib.import_module("bank.migrations.0007_statement_formats")
SEEDED_FORMAT, SEEDED_LAYOUT = _SEEDED_FORMATS.NAME, _SEEDED_FORMATS.FORMAT
#: A format a person typed for another bank's export (RulesData): cells
#: split by tabulations, ISO dates, decimal points, the label over two
#: columns, the debits and the credits apart, the account in a header line.
TAB_FORMAT = "Banque exemple (tabulations)"
TAB_LAYOUT = {
    "position": 2,
    "encoding": "utf-8",
    "delimiter": "\t",
    "date_format": "yyyy-mm-dd",
    "decimal_mark": ".",
    "date_column": 1,
    "value_date_column": 2,
    "label_columns": "3, 4",
    "debit_column": 5,
    "credit_column": 6,
    "account_pattern": r"COMPTE (?P<compte>[0-9]{5,})",
}
#: A format valid on its own, which the archive's records below change.
ONE_COLUMN_LAYOUT = {
    "encoding": "auto",
    "delimiter": ";",
    "date_format": "dd/mm/yyyy",
    "decimal_mark": ",",
    "date_column": 1,
    "label_columns": "2",
    "amount_column": 3,
}
#: What Django's own validators say, in English: never on a report.
DJANGO_ENGLISH = r"Ensure this value|This field|is not a valid|already exists"

#: How the code before 02/10/2026 counted « Banque », in its order: the
#: rules among the lines' counts (`archive.CARVED` takes them back out).
OLD_BANK_LABELS = (
    "opérations",
    "paiements",
    "règles",
    "règles de reconnaissance",
    "formats de relevé",
    "noms de payeurs appris",
    "payeurs retenus (entrées d'argent)",
)
#: Each list of the rules, and its count: as « Banque » labelled it then,
#: and as this section labels it now.
OLD_LABELS = {
    "statement_formats": "formats de relevé",
    "operation_rules": "règles de reconnaissance",
    "rules": "règles",
}
NEW_LABELS = {"statement_formats": FORMATS, "operation_rules": RECOGNITION, "rules": RULES}


def make_rule(pattern, description="", is_active=True, category="") -> IgnoreRule:
    """An ignore rule as « Dépenses sans facture attendue » saves it, made at
    a moment in the past: a round trip that forgot to restore it would show
    today's instead."""
    rule = IgnoreRule.objects.create(pattern=pattern, description=description, is_active=is_active, category=category)
    IgnoreRule.objects.filter(pk=rule.pk).update(created_at=RULE_MOMENT)
    rule.refresh_from_db()
    return rule


def make_operation_rule(name, meaning, pattern, *, searched="label", position=9, is_active=True) -> OperationRule:
    """A recognition rule as « Reconnaissance des opérations » saves it
    (checked by the model as the form is), made at a moment in the past: a
    round trip that forgot to restore it would show today's instead."""
    rule = OperationRule(
        name=name, meaning=meaning, searched=searched, pattern=pattern, position=position, is_active=is_active
    )
    rule.full_clean()
    rule.save()
    OperationRule.objects.filter(pk=rule.pk).update(created_at=RECOGNITION_MOMENT)
    rule.refresh_from_db()
    return rule


def make_statement_format(name, **layout) -> StatementFormat:
    """A statement format as « Format du relevé » saves it (checked by the
    model as the form is), made at a moment in the past: a round trip that
    forgot to restore it would show today's instead."""
    fmt = StatementFormat(name=name, **layout)
    fmt.full_clean()
    fmt.save()
    StatementFormat.objects.filter(pk=fmt.pk).update(created_at=FORMAT_MOMENT)
    fmt.refresh_from_db()
    return fmt


def wipe_rules() -> None:
    """Everything the section holds, as its clear leaves it - the seeded
    format and recognition rules included: an archive importing into it has
    to bring them back."""
    StatementFormat.objects.all().delete()
    OperationRule.objects.all().delete()
    IgnoreRule.objects.all().delete()


def statement_formats() -> list[tuple]:
    """Every field of every statement format, its moment included, in the
    order an import offers them (the first is the default)."""
    return list(StatementFormat.objects.order_by("position", "name").values_list(*section.FORMAT_FIELDS))


def recognition_rules() -> list[tuple]:
    """(position, name, meaning, searched, pattern, is_active, created_at)
    of every recognition rule, in the order they are asked."""
    return list(
        OperationRule.objects.order_by("position", "name").values_list(
            "position", "name", "meaning", "searched", "pattern", "is_active", "created_at"
        )
    )


def ignore_rules() -> list[tuple]:
    """Every field of every ignore rule, its moment included."""
    return sorted(IgnoreRule.objects.values_list("pattern", *section.RULE_FIELDS))


#: « Banque »'s lists of the treasury (bank/0008), which no archive written
#: before « Règles de la banque » holds.
TREASURY_LISTS = ("treasury_checkpoints", "treasury_adjustments")


def written_by_the_old_code(current: ArchiveReader, *, without=()) -> Path:
    """The archive the code before 02/10/2026 wrote of the same database:
    « Banque » alone, its banque.json holding the lines and the rules - this
    version's two files as one -, counted under the labels it gave them then
    (`OLD_BANK_LABELS`). `current` holds both sections. `without`: the lists
    of the rules an archive older still did not hold (« operation_rules »
    before bank/0006, « statement_formats » before bank/0007), their counts
    with them. The treasury's lists are left out: that code had none
    (bank/0008 came after it)."""
    lines, rules = current.section("banque"), current.section(KEY)
    kept = {name: items for name, items in lines.payload().items() if name not in TREASURY_LISTS}
    payload = {**kept, **{name: items for name, items in rules.payload().items() if name not in without}}
    counts = dict(lines.counts)
    counts.update({OLD_LABELS[name]: rules.counts[NEW_LABELS[name]] for name in TOP_LEVEL if name not in without})

    def manifest(data):
        data["sections"]["banque"]["counts"] = {label: counts[label] for label in OLD_BANK_LABELS if label in counts}
        return data

    return forge({"banque": payload}, manifest=manifest)


def rules_report(run):
    return run.section(KEY)


def tally(run, entity):
    return rules_report(run).tallies[entity]


class RulesData:
    """Beside what bank/0006 and bank/0007 seed: a rule typed for another
    bank's payment terminal, a format typed for another bank's export, and
    two ignore rules - one that also says what its payments count as on
    « Dépenses », one paused."""

    def setUp(self):
        super().setUp()
        make_rule("URSSAF", "Cotisations", category="Cotisations sociales")
        make_rule("PRET LOCAL", "Prêt du local", is_active=False)
        # It recognises no line: nothing here is read again with it.
        self.terminal_rule = make_operation_rule(TERMINAL_RULE, "payout", r"EXEMPLE PAY REMISE")
        self.tab_format = make_statement_format(TAB_FORMAT, **TAB_LAYOUT)

    def export(self) -> ArchiveReader:
        reader = export_archive({KEY})
        self.addCleanup(reader.close)
        return reader

    def forged(self, change) -> ArchiveReader:
        """This database's export with regles_banque.json changed by `change`."""
        reader = ArchiveReader(forge(self.export(), regles_banque=change))
        self.addCleanup(reader.close)
        return reader

    def older(self, *, without=()) -> ArchiveReader:
        """This database as the code before 02/10/2026 exported it
        (`written_by_the_old_code`)."""
        current = export_archive({"banque", KEY})
        self.addCleanup(current.close)
        reader = ArchiveReader(written_by_the_old_code(current, without=without))
        self.addCleanup(reader.close)
        return reader

    def move_on(self):
        """Of each kind, after the export: one changed here, one only here,
        one only in the archive."""
        StatementFormat.objects.filter(pk=self.tab_format.pk).update(decimal_mark=",")
        make_statement_format("Banque locale (CSV)", position=3, **ONE_COLUMN_LAYOUT)
        StatementFormat.objects.filter(name=SEEDED_FORMAT).delete()
        OperationRule.objects.filter(pk=self.terminal_rule.pk).update(pattern=r"EXEMPLE PAY VIREMENT")
        make_operation_rule("Titres-restaurant (EXEMPLE TR)", "voucher", r"EXEMPLE TR", position=10)
        OperationRule.objects.filter(name=CHEQUES).delete()
        IgnoreRule.objects.filter(pattern="URSSAF").update(description="URSSAF trimestre")
        make_rule("LOYER", "Loyer")
        IgnoreRule.objects.filter(pattern="PRET LOCAL").delete()


#: The conflicts a merge says after `RulesData.move_on`, in the order of the
#: section's rows.
MOVED_ON_CONFLICTS = [
    f"Format de relevé « {TAB_FORMAT} » : différent dans l'archive (séparateur décimal) — gardé tel quel",
    f"Règle de reconnaissance « {TERMINAL_RULE} » : différente dans l'archive (motif) — gardée telle quelle",
    "Règle « URSSAF » : différente dans l'archive (nom) — gardée telle quelle",
]


class ContractTests(TestCase):
    def test_every_field_is_exported_or_said_why_not(self):
        # A field added to one of these models later cannot be left out of
        # the archive in silence.
        for model, exported in section.EXPORTED.items():
            with self.subTest(model=model.__name__):
                concrete = {field.name for field in model._meta.concrete_fields}
                self.assertEqual(concrete, set(exported) | set(section.NOT_EXPORTED[model]))
                self.assertFalse(set(exported) & set(section.NOT_EXPORTED[model]))

    def test_the_section_is_registered_under_its_key(self):
        self.assertIsInstance(registry.get(KEY), BankRulesSection)

    def test_count_of_a_new_database(self):
        # Nothing typed - and the owner's bank read and recognised all the
        # same, by the format bank/0007 and the rules bank/0006 seed.
        self.assertEqual(BankRulesSection().count(), {FORMATS: 1, RECOGNITION: len(SEEDED), RULES: 0})

    def test_each_kind_of_rule_has_a_count_of_its_own(self):
        # « règles » alone was the bank's word for the « sans facture » ones:
        # beside two other kinds of rules it names none.
        self.assertEqual(ENTITIES, (FORMATS, RECOGNITION, RULES))
        self.assertEqual(len(set(ENTITIES)), len(ENTITIES))
        self.assertNotIn("règles", ENTITIES)

    def test_an_older_archive_is_carved_into_these_lists_and_labels(self):
        # Both ends of `archive.CARVED`: this section's lists and labels, and
        # the labels « Banque » gave them then. And « Banque » counts none of
        # those any more: its export alone would be read as carrying them.
        carve = archive.CARVED[KEY]
        self.assertEqual(carve.within, "banque")
        self.assertEqual(set(carve.keys), set(TOP_LEVEL))
        self.assertEqual(carve.shared, ())
        self.assertEqual([label for label, _old in carve.counts], list(ENTITIES))
        self.assertEqual(dict(carve.counts), {NEW_LABELS[name]: OLD_LABELS[name] for name in TOP_LEVEL})
        self.assertFalse(set(OLD_LABELS.values()) & set(bank.ENTITIES))


class ExportTests(RulesData, TestCase):
    def test_counts(self):
        expected = {FORMATS: 2, RECOGNITION: len(SEEDED) + 1, RULES: 2}
        self.assertEqual(BankRulesSection().count(), expected)
        self.assertEqual(self.export().section(KEY).counts, expected)

    def test_the_three_lists_and_nothing_else(self):
        # Nothing of the bank's lines, and no pk: what another bar takes.
        payload = self.export().section(KEY).payload()
        self.assertEqual(list(payload), list(TOP_LEVEL))

        def names(value):
            if isinstance(value, dict):
                for name, item in value.items():
                    yield name
                    yield from names(item)
            elif isinstance(value, list):
                for item in value:
                    yield from names(item)

        found = set(names(payload))
        self.assertFalse({"id", "pk"} & found)
        self.assertFalse({name for name in found if name.endswith("_id")})

    def test_the_statement_formats_are_exported_in_the_order_an_import_offers_them(self):
        # Every field but the id, the first the default - and the tab comes
        # out of the JSON as the tab it was.
        payload = self.export().section(KEY).payload()
        formats = payload["statement_formats"]
        self.assertEqual([item["name"] for item in formats], [SEEDED_FORMAT, TAB_FORMAT])
        for item in formats:
            self.assertEqual(set(item), set(section.FORMAT_FIELDS))
        seeded = formats[0]
        self.assertEqual({name: seeded[name] for name in SEEDED_LAYOUT}, SEEDED_LAYOUT)
        self.assertEqual((seeded["debit_column"], seeded["credit_column"]), (None, None))
        self.assertEqual(
            formats[1],
            {
                "name": TAB_FORMAT,
                **TAB_LAYOUT,
                "amount_column": None,
                "bank_type_column": None,
                "created_at": "2026-07-22T16:40:05.750000+00:00",
            },
        )
        self.assertEqual(formats[1]["delimiter"], "\t")

    def test_the_recognition_rules_are_exported_in_the_order_they_are_asked(self):
        payload = self.export().section(KEY).payload()
        self.assertEqual(
            [
                (item["position"], item["name"], item["meaning"], item["searched"], item["pattern"])
                for item in payload["operation_rules"]
            ],
            [*SEEDED, (9, TERMINAL_RULE, "payout", "label", r"EXEMPLE PAY REMISE")],
        )
        self.assertEqual(
            payload["operation_rules"][-1],
            {
                "name": TERMINAL_RULE,
                "meaning": "payout",
                "searched": "label",
                "pattern": r"EXEMPLE PAY REMISE",
                "position": 9,
                "is_active": True,
                "created_at": "2026-07-21T09:15:45.500000+00:00",
            },
        )

    def test_the_ignore_rules_are_exported_with_what_they_say(self):
        payload = self.export().section(KEY).payload()
        self.assertEqual(
            payload["rules"],
            [
                {
                    "pattern": "URSSAF",
                    "description": "Cotisations",
                    "is_active": True,
                    "category": "Cotisations sociales",
                    "created_at": "2026-07-20T08:30:00+00:00",
                },
                {
                    "pattern": "PRET LOCAL",
                    "description": "Prêt du local",
                    "is_active": False,
                    "category": "",
                    "created_at": "2026-07-20T08:30:00+00:00",
                },
            ],
        )

    def test_none_is_an_empty_list_never_a_missing_one(self):
        # Absent, a list reads as an archive written before it existed
        # (« not said »), and « Remplacer » would keep this database's.
        wipe_rules()
        reader = self.export()
        self.assertEqual(reader.section(KEY).payload(), {name: [] for name in TOP_LEVEL})
        self.assertEqual(reader.section(KEY).counts, dict.fromkeys(ENTITIES, 0))


class RoundTripTests(RulesData, TestCase):
    def assert_empty(self):
        self.assertEqual(BankRulesSection().count(), dict.fromkeys(ENTITIES, 0))

    def assert_back(self, formats, rules, ignored):
        # Every field, in their order, each with the moment it was made - the
        # seeded ones with their database's, those typed with their own -, the
        # tab a tab, and what an ignore rule's payments count as on
        # « Dépenses ».
        self.assertEqual(statement_formats(), formats)
        self.assertEqual(recognition_rules(), rules)
        self.assertEqual(ignore_rules(), ignored)
        tab = StatementFormat.objects.get(name=TAB_FORMAT)
        self.assertEqual((tab.delimiter, tab.created_at), ("\t", FORMAT_MOMENT))
        self.assertEqual(StatementFormat.objects.first().name, SEEDED_FORMAT)
        self.assertEqual(OperationRule.objects.get(name=TERMINAL_RULE).created_at, RECOGNITION_MOMENT)
        self.assertEqual(set(IgnoreRule.objects.values_list("created_at", flat=True)), {RULE_MOMENT})
        self.assertEqual(IgnoreRule.objects.get(pattern="URSSAF").category, "Cotisations sociales")

    def test_merge(self):
        kept = statement_formats(), recognition_rules(), ignore_rules()
        before, after = round_trip({KEY}, MERGE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_back(*kept)

    def test_replace(self):
        kept = statement_formats(), recognition_rules(), ignore_rules()
        before, after = round_trip({KEY}, REPLACE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_back(*kept)

    def test_formats_of_one_position_keep_their_order(self):
        # The first by (position, name) is the one an import uses when
        # nobody chooses - never the first by id, which an import gives anew.
        make_statement_format("Banque exemple B", position=0, date_column=1, label_columns="2", amount_column=3)
        make_statement_format("Banque exemple A", position=0, date_column=1, label_columns="2", amount_column=3)
        formats = statement_formats()
        self.assertEqual(StatementFormat.objects.first().name, "Banque exemple A")
        round_trip({KEY}, REPLACE)
        self.assertEqual(statement_formats(), formats)
        self.assertEqual(StatementFormat.objects.first().name, "Banque exemple A")

    def test_rules_of_one_position_keep_their_order(self):
        # Rules at one position are asked by name - never by id, which an
        # import gives anew - and must still be once created again.
        make_operation_rule("Virement exemple (B)", "transfer", r"^EXEMPLE B", position=3)
        make_operation_rule("Virement exemple (A)", "transfer", r"^EXEMPLE A", position=3)
        rules = recognition_rules()
        round_trip({KEY}, REPLACE)
        self.assertEqual(recognition_rules(), rules)
        self.assertEqual(
            [rule[1] for rule in rules if rule[0] == 3],
            ["Virement exemple (A)", "Virement exemple (B)", "Virement émis (/BEN)"],
        )

    def test_rules_of_one_position_keep_their_order_when_one_of_them_is_still_here(self):
        # The restore that brings back one of two rules of one position, the
        # other still here: by id, the one created again came last (review,
        # 01/10/2026); by name, the reading is the archive's.
        make_operation_rule("Virement exemple (A)", "transfer", r"^EXEMPLE", position=3)
        make_operation_rule("Virement exemple (B)", "transfer", r"^EXEMPLE 1", position=3)
        reading = recognition.describe(recognition.load(), "EXEMPLE 123").rule
        self.assertEqual(reading, "Virement exemple (A)")
        reader = self.export()
        OperationRule.objects.filter(name="Virement exemple (A)").delete()
        import_archive(reader, {KEY: REPLACE})
        self.assertEqual(recognition.describe(recognition.load(), "EXEMPLE 123").rule, reading)


class IdempotenceTests(RulesData, TestCase):
    def assert_nothing_moves(self, run):
        report = rules_report(run)
        expected = {FORMATS: 2, RECOGNITION: len(SEEDED) + 1, RULES: 2}
        for entity, number in expected.items():
            with self.subTest(entity=entity):
                counted = report.tallies[entity]
                self.assertEqual(
                    (counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, number)
                )
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))
        self.assertFalse(run.affected())

    def test_merging_its_own_export_changes_nothing(self):
        before = db_fingerprint()
        self.assert_nothing_moves(import_archive(self.export(), MERGE))
        self.assertEqual(db_fingerprint(), before)

    def test_replacing_with_its_own_export_changes_nothing(self):
        before = db_fingerprint()
        self.assert_nothing_moves(import_archive(self.export(), REPLACE))
        self.assertEqual(db_fingerprint(), before)

    def test_the_same_records_made_at_other_moments_are_unchanged(self):
        # The same rules typed on another computer a day later: the moments
        # differ, the records do not (§6.4). A record equal on every compared
        # field is not written, not even its moment.
        reader = self.export()
        IgnoreRule.objects.update(created_at=datetime(2026, 9, 1, 12, 2, tzinfo=UTC))
        OperationRule.objects.update(created_at=datetime(2026, 9, 1, 12, 4, tzinfo=UTC))
        StatementFormat.objects.update(created_at=datetime(2026, 9, 1, 12, 5, tzinfo=UTC))
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                self.assert_nothing_moves(import_archive(reader, strategy))
                self.assertEqual(db_fingerprint(), before)


class MergeAndReplaceTests(RulesData, TestCase):
    """The rules moved on after the export: of each kind, one record
    changed, one only here, one only in the archive (`RulesData.move_on`).
    Merged like the payers retained: one changed here is a conflict, kept."""

    def setUp(self):
        super().setUp()
        self.before = BankRulesSection().snapshot()
        self.reader = self.export()
        self.move_on()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        expected = {FORMATS: (1, 0, 0, 0), RECOGNITION: (1, 0, 0, len(SEEDED) - 1), RULES: (1, 0, 0, 0)}
        for entity, numbers in expected.items():
            with self.subTest(entity=entity):
                counted = tally(run, entity)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), numbers)
        self.assertEqual(rules_report(run).conflicts, MOVED_ON_CONFLICTS)
        # Kept as they are here.
        self.assertEqual(StatementFormat.objects.get(pk=self.tab_format.pk).decimal_mark, ",")
        self.assertEqual(OperationRule.objects.get(name=TERMINAL_RULE).pattern, r"EXEMPLE PAY VIREMENT")
        self.assertEqual(IgnoreRule.objects.get(pattern="URSSAF").description, "URSSAF trimestre")
        # Only here: untouched.
        self.assertTrue(StatementFormat.objects.filter(name="Banque locale (CSV)").exists())
        self.assertTrue(OperationRule.objects.filter(name="Titres-restaurant (EXEMPLE TR)").exists())
        self.assertTrue(IgnoreRule.objects.filter(pattern="LOYER").exists())
        # Only in the archive: created as it was, in its place, with its moment.
        seeded = StatementFormat.objects.get(name=SEEDED_FORMAT)
        self.assertEqual({name: getattr(seeded, name) for name in SEEDED_LAYOUT}, SEEDED_LAYOUT)
        self.assertEqual(StatementFormat.objects.first(), seeded)
        cheques = OperationRule.objects.get(name=CHEQUES)
        self.assertEqual(
            (cheques.position, cheques.meaning, cheques.searched, cheques.pattern),
            (8, "cheque", "bank_type", "REMISE CHEQUE"),
        )
        paused = IgnoreRule.objects.get(pattern="PRET LOCAL")
        self.assertEqual(
            (paused.is_active, paused.description, paused.created_at), (False, "Prêt du local", RULE_MOMENT)
        )
        # A merge updates and deletes nothing: no safety export is needed.
        self.assertFalse(run.affected())

    def test_replace_makes_the_rules_exactly_the_archives(self):
        run = import_archive(self.reader, REPLACE)
        expected = {FORMATS: (1, 1, 1, 0), RECOGNITION: (1, 1, 1, len(SEEDED) - 1), RULES: (1, 1, 1, 0)}
        for entity, numbers in expected.items():
            with self.subTest(entity=entity):
                counted = tally(run, entity)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), numbers)
        self.assertEqual(rules_report(run).conflicts, [])
        self.assertEqual(BankRulesSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {KEY})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                preview = import_archive(self.reader, strategy, preview=True)
                self.assertEqual(db_fingerprint(), before)
                confirmed = import_archive(self.reader, strategy)
                self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_an_empty_list_forgets_every_one_of_its_kind_under_replace_only(self):
        # Said empty, the archive holds none of them: « Remplacer » forgets
        # every one of that kind, « Fusionner » adds nothing and forgets
        # nothing - and the other two lists are merged as ever.
        models = {"statement_formats": StatementFormat, "operation_rules": OperationRule, "rules": IgnoreRule}
        for name, model in models.items():
            with self.subTest(name=name):

                def change(payload, name=name):
                    payload[name] = []
                    return payload

                reader = self.forged(change)
                here = model.objects.count()
                run = import_archive(reader, MERGE)
                self.assertEqual(tally(run, NEW_LABELS[name]).deleted, 0)
                self.assertEqual(model.objects.count(), here)
                run = import_archive(reader, REPLACE)
                self.assertEqual(tally(run, NEW_LABELS[name]).deleted, here)
                self.assertFalse(model.objects.exists())


class RecognitionOrderAndNameTests(RulesData, TestCase):
    """The order is part of what a rule says (the first of its kind that
    finds its pattern decides), and a rule is its name whatever its case and
    accents (`bank.recognition.name_key`)."""

    TRANSFERS = "Autre virement (VIR)"
    DEBITS = "Prélèvement (PRLV SEPA)"

    def setUp(self):
        super().setUp()
        self.rules = recognition_rules()
        self.reader = self.export()

    def test_a_rule_moved_here_is_a_conflict_and_replace_puts_it_back(self):
        # Asked first, « ^VIR » would take every transfer before the rules
        # reading their payee.
        OperationRule.objects.filter(name=self.TRANSFERS).update(position=1)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            rules_report(run).conflicts,
            [f"Règle de reconnaissance « {self.TRANSFERS} » : différente dans l'archive (ordre) — gardée telle quelle"],
        )
        self.assertEqual(OperationRule.objects.get(name=self.TRANSFERS).position, 1)
        import_archive(self.reader, REPLACE)
        self.assertEqual(recognition_rules(), self.rules)

    def test_a_rule_spelt_otherwise_here_is_the_same_rule(self):
        OperationRule.objects.filter(name=self.DEBITS).update(name="PRELEVEMENT  (prlv sepa)")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            rules_report(run).conflicts,
            [f"Règle de reconnaissance « {self.DEBITS} » : différente dans l'archive (nom) — gardée telle quelle"],
        )
        self.assertEqual((tally(run, RECOGNITION).created, OperationRule.objects.count()), (0, len(SEEDED) + 1))
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 1, 0))
        self.assertEqual(recognition_rules(), self.rules)

    def test_a_second_rule_of_one_name_here_is_one_too_many_under_replace(self):
        # The page refuses it; made some other way, the archive's rule is
        # the first of that name, and « Remplacer » leaves one.
        OperationRule.objects.create(
            name="prélèvement (PRLV SEPA)", meaning="debit", pattern=r"^PRLV", position=20, is_active=False
        )
        run = import_archive(self.reader, MERGE)
        self.assertEqual(tally(run, RECOGNITION).deleted, 0)
        self.assertEqual(OperationRule.objects.count(), len(SEEDED) + 2)
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 0, 1))
        self.assertEqual(recognition_rules(), self.rules)


class FormatOrderAndNameTests(RulesData, TestCase):
    """The order is part of what a format says (the first is the one an
    import uses when nobody chooses), and a format is its name whatever its
    case and accents (`bank.recognition.name_key`)."""

    def setUp(self):
        super().setUp()
        self.formats = statement_formats()
        self.reader = self.export()

    def test_a_format_moved_here_is_a_conflict_and_replace_puts_it_back(self):
        # First here, the tab format is what an import reads a file with.
        StatementFormat.objects.filter(pk=self.tab_format.pk).update(position=0)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            rules_report(run).conflicts,
            [f"Format de relevé « {TAB_FORMAT} » : différent dans l'archive (ordre) — gardé tel quel"],
        )
        self.assertEqual(StatementFormat.objects.first().name, TAB_FORMAT)
        import_archive(self.reader, REPLACE)
        self.assertEqual(statement_formats(), self.formats)
        self.assertEqual(StatementFormat.objects.first().name, SEEDED_FORMAT)

    def test_a_format_spelt_otherwise_here_is_the_same_format(self):
        StatementFormat.objects.filter(name=SEEDED_FORMAT).update(name="bnp  PARIBAS (csv)")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            rules_report(run).conflicts,
            [f"Format de relevé « {SEEDED_FORMAT} » : différent dans l'archive (nom) — gardé tel quel"],
        )
        self.assertEqual((tally(run, FORMATS).created, StatementFormat.objects.count()), (0, 2))
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 1, 0))
        self.assertEqual(statement_formats(), self.formats)

    def test_a_second_format_of_one_name_here_is_one_too_many_under_replace(self):
        # The page refuses it; made some other way, the archive's format is
        # the first of that name, and « Remplacer » leaves one.
        StatementFormat.objects.create(name="BNP PARIBAS (CSV)", position=20, **ONE_COLUMN_LAYOUT)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(tally(run, FORMATS).deleted, 0)
        self.assertEqual(StatementFormat.objects.count(), 3)
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 0, 1))
        self.assertEqual(statement_formats(), self.formats)


class IgnoreRuleOrderTests(RulesData, TestCase):
    """An ignore rule is its pattern: two of one pattern here is one too
    many, and the archive's is the first."""

    def test_a_second_rule_of_one_pattern_here_is_one_too_many_under_replace(self):
        reader = self.export()
        IgnoreRule.objects.create(pattern="URSSAF", description="URSSAF bis")
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, RULES).deleted, 0)
        self.assertEqual(IgnoreRule.objects.filter(pattern="URSSAF").count(), 2)
        run = import_archive(reader, REPLACE)
        counted = tally(run, RULES)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 0, 1))
        self.assertEqual(IgnoreRule.objects.get(pattern="URSSAF").description, "Cotisations")


class OlderArchiveTests(RulesData, TestCase):
    """An archive written before this section existed - every safety backup
    taken until 02/10/2026 - holds the rules in banque.json, counted among
    the lines' counts. It is read as if it held « Règles de la banque » too
    (`archive.CARVED`): offered, counted under this section's labels, and
    imported by this section's own code. What « Banque » makes of the same
    file is test_bank_section.py's."""

    def setUp(self):
        super().setUp()
        self.before = BankRulesSection().snapshot()
        self.reader = self.older()

    def test_it_offers_the_rules_counted_under_their_own_labels(self):
        self.assertEqual(self.reader.sections, {"banque", KEY})
        rules = self.reader.section(KEY)
        self.assertEqual(rules.member, "banque.json")
        self.assertEqual(rules.counts, {FORMATS: 2, RECOGNITION: len(SEEDED) + 1, RULES: 2})
        self.assertEqual(set(rules.payload()), set(TOP_LEVEL))
        # « Banque » keeps its own lists and counts - the old code's, no
        # treasury yet -, and none of these.
        lines = self.reader.section("banque")
        self.assertEqual(set(lines.counts), set(bank.ENTITIES) - {bank.CHECKPOINTS, bank.ADJUSTMENTS})
        self.assertFalse(set(lines.payload()) & set(TOP_LEVEL))

    def test_they_come_back_into_a_wiped_database(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                wipe_rules()
                run = import_archive(self.reader, {KEY: strategy})
                self.assertEqual(BankRulesSection().snapshot(), self.before)
                self.assertEqual(
                    {entity: tally(run, entity).created for entity in ENTITIES},
                    {FORMATS: 2, RECOGNITION: len(SEEDED) + 1, RULES: 2},
                )
                report = rules_report(run)
                self.assertEqual((report.conflicts, report.skipped, report.notes), ([], [], []))

    def test_importing_it_again_changes_nothing(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                run = import_archive(self.reader, {KEY: strategy})
                self.assertEqual(db_fingerprint(), before)
                self.assertEqual(
                    {entity: tally(run, entity).unchanged for entity in ENTITIES},
                    {FORMATS: 2, RECOGNITION: len(SEEDED) + 1, RULES: 2},
                )
                self.assertFalse(run.affected())

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        self.move_on()
        run = import_archive(self.reader, {KEY: MERGE})
        self.assertEqual(rules_report(run).conflicts, MOVED_ON_CONFLICTS)
        self.assertEqual({entity: tally(run, entity).created for entity in ENTITIES}, dict.fromkeys(ENTITIES, 1))
        self.assertEqual({entity: tally(run, entity).deleted for entity in ENTITIES}, dict.fromkeys(ENTITIES, 0))
        self.assertFalse(run.affected())

    def test_replace_makes_the_rules_exactly_the_archives(self):
        self.move_on()
        before = db_fingerprint()
        preview = import_archive(self.reader, {KEY: REPLACE}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        run = import_archive(self.reader, {KEY: REPLACE})
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(rules_report(run).conflicts, [])
        self.assertEqual(BankRulesSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {KEY})

    def test_one_whose_counts_say_nothing_still_offers_them(self):
        # A manifest's counts missing or empty say nothing: the rules are
        # offered, and read from the lists the file holds.
        def without_counts(data):
            data["sections"]["banque"]["counts"] = {}
            return data

        reader = ArchiveReader(forge(self.reader.path, manifest=without_counts))
        self.addCleanup(reader.close)
        self.assertEqual(reader.sections, {"banque", KEY})
        self.assertEqual(reader.section(KEY).counts, {})
        wipe_rules()
        import_archive(reader, {KEY: MERGE})
        self.assertEqual(BankRulesSection().snapshot(), self.before)


class ThisVersionTests(RulesData, TestCase):
    """An archive of this version holds the rules in a file of their own -
    or not at all: « Banque » exported alone does not carry them."""

    def test_an_archive_of_the_bank_alone_does_not_offer_the_rules(self):
        reader = export_archive({"banque"})
        self.addCleanup(reader.close)
        self.assertEqual(reader.sections, {"banque"})
        with self.assertRaisesMessage(ArchiveError, f"Cette archive ne contient pas la partie « {KEY} »."):
            reader.section(KEY)
        # Its counts name no label the rules had there, which is what tells
        # it from an older archive (`archive.carved`).
        self.assertFalse(set(reader.section("banque").counts) & set(OLD_LABELS.values()))
        self.assertFalse(set(reader.section("banque").payload()) & set(TOP_LEVEL))

    def test_each_section_reads_its_own_file(self):
        reader = export_archive({"banque", KEY})
        self.addCleanup(reader.close)
        self.assertEqual(reader.sections, {"banque", KEY})
        self.assertEqual((reader.section(KEY).member, reader.section("banque").member), (f"{KEY}.json", "banque.json"))


class NotSaidTests(RulesData, TestCase):
    """A list the archive does not hold says nothing of what it covers: an
    archive written before bank/0006 holds no recognition rule, one written
    before bank/0007 no format - which is not « recognise nothing » nor
    « read no statement »: whatever this database holds of them, merged or
    replaced, stays as it is, and nothing is said about them."""

    def assert_untouched(self, reader, *entities):
        """Merged then replaced, `entities` neither move nor are said; and
        nothing else does when the archive equals this database."""
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                preview = import_archive(reader, {KEY: strategy}, preview=True)
                run = import_archive(reader, {KEY: strategy})
                self.assertEqual(preview.outcome(), run.outcome())
                report = rules_report(run)
                self.assertEqual((report.conflicts, report.skipped), ([], []))
                self.assertEqual([note for note in report.notes if "champ inconnu" in note], [])
                for entity in entities:
                    counted = tally(run, entity)
                    self.assertEqual(
                        (counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 0)
                    )
                self.assertEqual(db_fingerprint(), before)
                self.assertFalse(run.affected())

    def test_an_archive_written_before_the_formats_leaves_them_alone(self):
        reader = self.older(without=("statement_formats",))
        self.assertEqual(reader.section(KEY).counts, {RECOGNITION: len(SEEDED) + 1, RULES: 2})
        # Moved on since: one changed, one added, one deleted.
        StatementFormat.objects.filter(pk=self.tab_format.pk).update(date_format="dd.mm.yyyy")
        make_statement_format("Banque locale (CSV)", position=3, **ONE_COLUMN_LAYOUT)
        StatementFormat.objects.filter(name=SEEDED_FORMAT).delete()
        self.assert_untouched(reader, FORMATS)

    def test_an_archive_written_before_the_recognition_rules_brings_the_ignore_rules_alone(self):
        reader = self.older(without=("operation_rules", "statement_formats"))
        self.assertEqual(reader.section(KEY).counts, {RULES: 2})
        self.assertEqual(set(reader.section(KEY).payload()), {"rules"})
        # The ignore rules come, into a database that lost them.
        IgnoreRule.objects.all().delete()
        run = import_archive(reader, {KEY: MERGE})
        self.assertEqual(tally(run, RULES).created, 2)
        self.assertEqual([rule[0] for rule in ignore_rules()], ["PRET LOCAL", "URSSAF"])
        # The formats and the recognition rules moved on since: one changed,
        # one added, one deleted of each - and none created, none pruned.
        OperationRule.objects.filter(pk=self.terminal_rule.pk).update(pattern=r"EXEMPLE PAY VIREMENT")
        make_operation_rule("Titres-restaurant (EXEMPLE TR)", "voucher", r"EXEMPLE TR", position=10)
        OperationRule.objects.filter(name=CHEQUES).delete()
        StatementFormat.objects.filter(pk=self.tab_format.pk).update(date_format="dd.mm.yyyy")
        make_statement_format("Banque locale (CSV)", position=3, **ONE_COLUMN_LAYOUT)
        StatementFormat.objects.filter(name=SEEDED_FORMAT).delete()
        self.assert_untouched(reader, FORMATS, RECOGNITION)

    def test_nothing_said_into_a_wiped_database_is_nothing_made(self):
        # The page then refuses an import until a format is typed - never a
        # format guessed for an archive -, and every line reads « Autre ».
        reader = self.older(without=("operation_rules", "statement_formats"))
        wipe_rules()
        run = import_archive(reader, {KEY: MERGE})
        self.assertFalse(StatementFormat.objects.exists())
        self.assertFalse(OperationRule.objects.exists())
        self.assertEqual(tally(run, RULES).created, 2)

    def test_ignore_rules_left_out_are_not_said_either(self):
        # Every archive written so far holds them, but the rule is one for
        # the three lists: absent or null, none created, none pruned.
        def absent(payload):
            del payload["rules"]
            return payload

        def null(payload):
            payload["rules"] = None
            return payload

        readers = {"absent": self.forged(absent), "null": self.forged(null)}
        # Moved on since: one changed, one added, one deleted.
        IgnoreRule.objects.filter(pattern="URSSAF").update(description="URSSAF trimestre")
        make_rule("LOYER", "Loyer")
        IgnoreRule.objects.filter(pattern="PRET LOCAL").delete()
        for left_out, reader in readers.items():
            with self.subTest(left_out=left_out):
                self.assert_untouched(reader, RULES)


class RecognitionCheckTests(RulesData, TestCase):
    """What the archive says of the recognition rules, read before it is
    trusted: the model's own check runs on every rule written - created, or
    replaced - and a pattern the guard of `returnables.patterns` refuses is
    never compiled."""

    TRAP = "Prélèvement piégé"

    def change_record(self, name, **fields):
        """This database's export with the rule `name` said otherwise."""

        def change(payload):
            next(item for item in payload["operation_rules"] if item["name"] == name).update(fields)
            return payload

        return self.forged(change)

    def adding(self, *records) -> ArchiveReader:
        def change(payload):
            payload["operation_rules"] += list(records)
            return payload

        return self.forged(change)

    def test_rules_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        for value in ({}, "Carte", [TERMINAL_RULE], [{"name": "Carte exemple"}, 3]):
            with self.subTest(value=value):

                def change(payload, value=value):
                    payload["operation_rules"] = value
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                with self.assertRaisesMessage(
                    ArchiveError,
                    "Archive refusée : dans regles_banque.json, « operation_rules » n'est pas une liste d'objets.",
                ):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)

    def test_a_rule_it_cannot_read_is_skipped_with_its_reason_and_the_others_come(self):
        first = dict(zip(("position", "name", "meaning", "searched", "pattern"), SEEDED[0], strict=True))
        refused = "motif refusé — "
        cases = [
            ({"meaning": "debit", "pattern": "PRLV"}, "sans nom"),
            ({"name": "  ", "meaning": "debit", "pattern": "PRLV"}, "sans nom"),
            ({"name": 7, "meaning": "debit", "pattern": "PRLV"}, "sans nom"),
            # The same name whatever its case, accents and spaces.
            (dict(first, name="PAIEMENT PAR CARTE  (facture carte)"), "en double dans l'archive"),
            (
                {"name": "Tout", "meaning": "other_operation", "pattern": "X|"},
                f"{refused}le motif accepte une ligne vide : il trouverait quelque chose sur n'importe quelle ligne",
            ),
            (
                {"name": "Prélèvement exemple", "meaning": "debit", "pattern": r"PRLV (?P<encaisse>[0-9]+)"},
                f"{refused}le groupe (?P<encaisse>…) ne sert à rien ; seuls (?P<tiers>…) servent à « Prélèvement »",
            ),
            ({"name": "Motif vide", "meaning": "debit", "pattern": ""}, f"{refused}le motif est vide"),
            (
                {"name": "Dépôt exemple", "meaning": "depot", "pattern": "DEPOT"},
                "« meaning » : valeur inconnue (« depot »)",
            ),
            (
                {"name": "Montant exemple", "meaning": "cash", "searched": "amount", "pattern": "ESPECES"},
                "« searched » : valeur inconnue (« amount »)",
            ),
            ({"name": "Sans sens", "pattern": "SENS"}, "« meaning » : valeur manquante"),
            ({"name": "Sans motif", "meaning": "debit"}, "« pattern » : valeur manquante"),
            ({"name": "Motif long", "meaning": "debit", "pattern": "P" * 301}, "« pattern » : plus de 300 caractères"),
            (
                {"name": "Ordre négatif", "meaning": "debit", "pattern": "NEGATIF", "position": -1},
                "« position » : nombre positif attendu (« -1 »)",
            ),
            # Past what SQLite holds: Django's own validator, said in French.
            (
                {"name": "Ordre énorme", "meaning": "debit", "pattern": "ENORME", "position": 2**63},
                "« position » : valeur refusée",
            ),
            # What SQLite still holds, and Django's validator lets through, but
            # past `MAX_POSITION`: the page's « last + 1 » would overflow.
            (
                {"name": "Ordre presque énorme", "meaning": "debit", "pattern": "PRESQUE", "position": 2**63 - 1},
                "« position » : valeur refusée",
            ),
            (
                {"name": "Active peut-être", "meaning": "debit", "pattern": "PEUT-ETRE", "is_active": "oui"},
                "« is_active » : oui ou non attendu (« oui »)",
            ),
            ({"name": "R" * 101, "meaning": "debit", "pattern": "LONG"}, "« name » : plus de 100 caractères"),
        ]
        reader = self.adding(*(record for record, _reason in cases))
        wipe_rules()
        run = import_archive(reader, MERGE)
        self.assertEqual(
            rules_report(run).skipped,
            [
                "Règle de reconnaissance sans nom"
                if reason == "sans nom"
                else f"Règle de reconnaissance « {record['name']} » : {reason}"
                for record, reason in cases
            ],
        )
        # Every rule it could read came, the first of two of one name kept.
        self.assertEqual(tally(run, RECOGNITION).created, len(SEEDED) + 1)
        self.assertEqual(OperationRule.objects.get(name=first["name"]).position, first["position"])

    def test_a_pattern_that_is_no_regular_expression_is_refused(self):
        reader = self.adding({"name": "Loyer exemple", "meaning": "debit", "pattern": "LOYER ("})
        run = import_archive(reader, MERGE)
        (skipped,) = rules_report(run).skipped
        self.assertTrue(skipped.startswith("Règle de reconnaissance « Loyer exemple » : motif refusé — "), skipped)
        self.assertFalse(OperationRule.objects.filter(name="Loyer exemple").exists())

    def test_a_pattern_the_guard_refuses_is_never_compiled(self):
        # Created, or replacing this database's: refused before `regex`
        # could freeze the machine compiling it (returnables/patterns.py).
        for pattern, reason in (
            (r"(?:x{65535}){65535}", "répétition trop grande"),
            (r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
            (r"a{e<=1}", "accolade"),
        ):
            for strategy, reader in (
                (MERGE, self.adding({"name": self.TRAP, "meaning": "debit", "pattern": pattern})),
                (REPLACE, self.change_record(TERMINAL_RULE, pattern=pattern)),
            ):
                with self.subTest(pattern=pattern, strategy=strategy):
                    never = NeverCompile()
                    with mock.patch.object(regex, "compile", new=never):
                        run = import_archive(reader, strategy)
                    self.assertEqual(never.calls, [])
                    (skipped,) = rules_report(run).skipped
                    self.assertIn(" : motif refusé — ", skipped)
                    self.assertIn(reason, skipped)
                    self.assertFalse(OperationRule.objects.filter(name=self.TRAP).exists())
                    self.assertEqual(OperationRule.objects.get(pk=self.terminal_rule.pk).pattern, r"EXEMPLE PAY REMISE")

    def test_replace_checks_the_pattern_it_writes_and_keeps_the_rule_when_refused(self):
        # The pattern is no key: « Remplacer » writes the archive's over this
        # database's, and checks it as one it creates.
        reader = self.change_record(TERMINAL_RULE, pattern=r"EXEMPLE|", position=12)
        before = db_fingerprint()
        preview = import_archive(reader, REPLACE, preview=True)
        run = import_archive(reader, REPLACE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(
            rules_report(run).skipped,
            [
                (
                    f"Règle de reconnaissance « {TERMINAL_RULE} » : motif refusé — le motif accepte une ligne vide : "
                    "il trouverait quelque chose sur n'importe quelle ligne"
                )
            ],
        )
        self.assertEqual(db_fingerprint(), before)
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, len(SEEDED)))

    def test_replace_keeps_a_rule_whose_record_it_cannot_read(self):
        reader = self.change_record(TERMINAL_RULE, meaning="tpe")
        run = import_archive(reader, REPLACE)
        self.assertEqual(
            rules_report(run).skipped,
            [f"Règle de reconnaissance « {TERMINAL_RULE} » : « meaning » : valeur inconnue (« tpe »)"],
        )
        self.assertEqual(OperationRule.objects.get(pk=self.terminal_rule.pk).meaning, "payout")
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, len(SEEDED)))

    def test_an_unknown_field_of_a_rule_is_said_once(self):
        def change(payload):
            for item in payload["operation_rules"]:
                item["humeur"] = "calme"
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(rules_report(run).notes.count("champ inconnu ignoré : règles de reconnaissance › humeur"), 1)
        self.assertEqual(rules_report(run).skipped, [])

    def test_a_position_past_the_bound_is_refused_and_the_page_still_saves_after_one_at_it(self):
        """As for the formats: a new rule comes at the highest + 1, rules
        sharing a position are made distinct by + 1 (`views._saved`,
        `_move`), so a position the page cannot put another after is
        refused. One at `MAX_POSITION` is taken, and the page still saves
        and moves."""

        def made(name, position):
            return {"name": name, "meaning": "debit", "pattern": name.upper(), "position": position}

        reader = self.adding(
            made("Ordre presque énorme", 2**63 - 1),
            made("Ordre au plus haut A", section.MAX_POSITION),
            made("Ordre au plus haut B", section.MAX_POSITION),
        )
        run = import_archive(reader, MERGE)
        self.assertEqual(
            rules_report(run).skipped,
            ["Règle de reconnaissance « Ordre presque énorme » : « position » : valeur refusée"],
        )
        response = self.client.post(
            reverse("bank:recognition"),
            {
                "action": "enregistrer",
                "name": "Règle après",
                "meaning": "debit",
                "searched": "label",
                "pattern": "APRES",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OperationRule.objects.get(name="Règle après").position, section.MAX_POSITION + 1)
        first = OperationRule.objects.get(name="Ordre au plus haut A")
        response = self.client.post(reverse("bank:recognition_rule", args=[first.pk]), {"action": "descendre"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(OperationRule.objects.filter(position__gte=section.MAX_POSITION).values_list("name", flat=True)),
            ["Ordre au plus haut B", "Ordre au plus haut A", "Règle après"],
        )


class FormatCheckTests(RulesData, TestCase):
    """What the archive says of the statement formats, read before it is
    trusted: the model's own check (`bank.statements.check_format`) runs on
    every format written - created, or replaced - its refusal said in
    French after the field it names, and an account pattern the guard of
    `returnables.patterns` refuses is never compiled."""

    TRAP = "Format piégé"

    def change_record(self, name, **fields):
        """This database's export with the format `name` said otherwise."""

        def change(payload):
            next(item for item in payload["statement_formats"] if item["name"] == name).update(fields)
            return payload

        return self.forged(change)

    def adding(self, *records) -> ArchiveReader:
        def change(payload):
            payload["statement_formats"] += list(records)
            return payload

        return self.forged(change)

    def test_formats_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        for value in ({}, "BNP", [TAB_FORMAT], [{"name": "Banque exemple"}, 3]):
            with self.subTest(value=value):

                def change(payload, value=value):
                    payload["statement_formats"] = value
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                with self.assertRaisesMessage(
                    ArchiveError,
                    "Archive refusée : dans regles_banque.json, « statement_formats » n'est pas une liste d'objets.",
                ):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)

    def test_a_format_it_cannot_read_is_skipped_with_its_reason_and_the_others_come(self):
        def made(name, **fields):
            return {"name": name, **ONE_COLUMN_LAYOUT, **fields}

        refused = "format refusé — "
        without_labels = {name: value for name, value in made("Sans libellé").items() if name != "label_columns"}
        cases = [
            (dict(ONE_COLUMN_LAYOUT), "sans nom"),
            (made("  "), "sans nom"),
            (made(7), "sans nom"),
            # The same name whatever its case, accents and spaces.
            (made("bnp  PARIBAS (csv)"), "en double dans l'archive"),
            (made("Encodage exemple", encoding="latin-9"), "« encoding » : valeur inconnue (« latin-9 »)"),
            (made("Séparateur exemple", delimiter=":"), "« delimiter » : valeur inconnue (« : »)"),
            (made("Dates exemple", date_format="jj/mm/aaaa"), "« date_format » : valeur inconnue (« jj/mm/aaaa »)"),
            (made("Décimales exemple", decimal_mark=";"), "« decimal_mark » : valeur inconnue (« ; »)"),
            (made("Sans date", date_column=None), "« date_column » : valeur manquante"),
            (without_labels, "« label_columns » : valeur manquante"),
            (made("Date en texte", date_column="1"), "« date_column » : nombre entier attendu (« 1 »)"),
            (made("Date oui", date_column=True), "« date_column » : nombre entier attendu (« True »)"),
            (made("Montant négatif", amount_column=-3), "« amount_column » : nombre positif attendu (« -3 »)"),
            (made("Colonne zéro", date_column=0), f"{refused}colonne de la date : un numéro de colonne de 1 à 50"),
            # Past what SQLite holds, where Django's own validator refuses it
            # too: the check's own French, never Django's « Ensure this value
            # is less than or equal to … » beside it.
            (
                made("Colonne énorme", date_column=2**63),
                f"{refused}colonne de la date : un numéro de colonne de 1 à 50",
            ),
            (
                made("Libellé vide", label_columns=" "),
                f"{refused}colonnes du libellé : indiquez au moins une colonne pour le libellé",
            ),
            (
                made("Libellé illisible", label_columns="2, x"),
                f"{refused}colonnes du libellé : « x » n'est pas un numéro de colonne (de 1 à 50)",
            ),
            (
                made("Colonne deux fois", label_columns="1"),
                f"{refused}colonnes du libellé : la colonne 1 sert deux fois : pour la date et pour le libellé",
            ),
            (
                made("Sans montant", amount_column=None),
                f"{refused}colonne du montant : indiquez la colonne du montant, ou celles des débits et des crédits",
            ),
            (
                made("Montant deux fois", debit_column=4),
                (
                    f"{refused}colonne du montant : un montant signé OU des débits et des crédits : pas les deux "
                    "(laissez l'un vide)"
                ),
            ),
            (
                made("Compte exemple", account_pattern=r"COMPTE (?P<numero>[0-9]+)"),
                (
                    f"{refused}motif du numéro de compte : le groupe (?P<numero>…) ne sert à rien ; seul "
                    "(?P<compte>…) est lu"
                ),
            ),
            (made("Ordre négatif", position=-1), "« position » : nombre positif attendu (« -1 »)"),
            # Past what SQLite holds: Django's own validator, said in French.
            (made("Ordre énorme", position=2**63), "« position » : valeur refusée"),
            # What SQLite still holds, and Django's validator lets through, but
            # past `MAX_POSITION`: the page's « last + 1 » would overflow.
            (made("Ordre presque énorme", position=2**63 - 1), "« position » : valeur refusée"),
            (made("N" * 101), "« name » : plus de 100 caractères"),
            (made("Libellé long", label_columns="1" * 51), "« label_columns » : plus de 50 caractères"),
            (made("Motif long", account_pattern="C" * 301), "« account_pattern » : plus de 300 caractères"),
            # Debits alone, no credit column: a format the check takes.
            (made("Débits seuls", amount_column=None, debit_column=3), None),
        ]
        reader = self.adding(*(record for record, _reason in cases))
        wipe_rules()
        run = import_archive(reader, MERGE)
        skipped = rules_report(run).skipped
        self.assertEqual(
            skipped,
            [
                "Format de relevé sans nom"
                if reason == "sans nom"
                else f"Format de relevé « {record['name']} » : {reason}"
                for record, reason in cases
                if reason is not None
            ],
        )
        for reason in skipped:
            self.assertNotRegex(reason, DJANGO_ENGLISH)
        # Every format it could read came, the first of two of one name kept.
        self.assertEqual(tally(run, FORMATS).created, 3)
        seeded = StatementFormat.objects.get(name=SEEDED_FORMAT)
        self.assertEqual({name: getattr(seeded, name) for name in SEEDED_LAYOUT}, SEEDED_LAYOUT)
        self.assertEqual(StatementFormat.objects.get(name="Débits seuls").debit_column, 3)

    def test_an_account_pattern_that_is_no_regular_expression_is_refused(self):
        reader = self.adding({"name": "Banque exemple", **ONE_COLUMN_LAYOUT, "account_pattern": "COMPTE ("})
        run = import_archive(reader, MERGE)
        (skipped,) = rules_report(run).skipped
        self.assertTrue(
            skipped.startswith("Format de relevé « Banque exemple » : format refusé — motif du numéro de compte : "),
            skipped,
        )
        self.assertNotRegex(skipped, DJANGO_ENGLISH)
        self.assertFalse(StatementFormat.objects.filter(name="Banque exemple").exists())

    def test_an_account_pattern_the_guard_refuses_is_never_compiled(self):
        # Created, or replacing this database's: refused before `regex`
        # could freeze the machine compiling it (returnables/patterns.py).
        for pattern, reason in (
            (r"(?:x{65535}){65535}", "répétition trop grande"),
            (r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
            (r"a{e<=1}", "accolade"),
        ):
            for strategy, reader in (
                (MERGE, self.adding({"name": self.TRAP, **ONE_COLUMN_LAYOUT, "account_pattern": pattern})),
                (REPLACE, self.change_record(TAB_FORMAT, account_pattern=pattern)),
            ):
                with self.subTest(pattern=pattern, strategy=strategy):
                    never = NeverCompile()
                    with mock.patch.object(regex, "compile", new=never):
                        run = import_archive(reader, strategy)
                    self.assertEqual(never.calls, [])
                    (skipped,) = rules_report(run).skipped
                    self.assertIn(" : format refusé — motif du numéro de compte : ", skipped)
                    self.assertIn(reason, skipped)
                    self.assertFalse(StatementFormat.objects.filter(name=self.TRAP).exists())
                    self.assertEqual(
                        StatementFormat.objects.get(pk=self.tab_format.pk).account_pattern,
                        TAB_LAYOUT["account_pattern"],
                    )

    def test_replace_checks_the_format_it_writes_and_keeps_it_when_refused(self):
        # The columns are no key: « Remplacer » writes the archive's over this
        # database's, and checks them as a format it creates.
        reader = self.change_record(TAB_FORMAT, amount_column=7, position=12)
        before = db_fingerprint()
        preview = import_archive(reader, REPLACE, preview=True)
        run = import_archive(reader, REPLACE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(
            rules_report(run).skipped,
            [
                (
                    f"Format de relevé « {TAB_FORMAT} » : format refusé — colonne du montant : un montant signé OU "
                    "des débits et des crédits : pas les deux (laissez l'un vide)"
                )
            ],
        )
        self.assertEqual(db_fingerprint(), before)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, 1))

    def test_replace_keeps_a_format_whose_record_it_cannot_read(self):
        reader = self.change_record(TAB_FORMAT, encoding="latin-9")
        run = import_archive(reader, REPLACE)
        self.assertEqual(
            rules_report(run).skipped,
            [f"Format de relevé « {TAB_FORMAT} » : « encoding » : valeur inconnue (« latin-9 »)"],
        )
        self.assertEqual(StatementFormat.objects.get(pk=self.tab_format.pk).encoding, "utf-8")
        counted = tally(run, FORMATS)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, 1))

    def test_an_unknown_field_of_a_format_is_said_once(self):
        def change(payload):
            for item in payload["statement_formats"]:
                item["humeur"] = "calme"
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(rules_report(run).notes.count("champ inconnu ignoré : formats de relevé › humeur"), 1)
        self.assertEqual(rules_report(run).skipped, [])

    def test_a_position_past_the_bound_is_refused_and_the_page_still_saves_after_one_at_it(self):
        """A position SQLite holds that the page cannot put another after -
        a new format comes at the highest + 1, and formats sharing one are
        made distinct by + 1 (`views._saved`, `_swapped`) - is refused: taken,
        every « Nouveau format » after it was a 500 (OverflowError). One at
        `MAX_POSITION` is taken, and the page still saves and moves."""
        reader = self.adding(
            {"name": "Ordre presque énorme", **ONE_COLUMN_LAYOUT, "position": 2**63 - 1},
            {"name": "Ordre au plus haut A", **ONE_COLUMN_LAYOUT, "position": section.MAX_POSITION},
            {"name": "Ordre au plus haut B", **ONE_COLUMN_LAYOUT, "position": section.MAX_POSITION},
        )
        run = import_archive(reader, MERGE)
        self.assertEqual(
            rules_report(run).skipped, ["Format de relevé « Ordre presque énorme » : « position » : valeur refusée"]
        )
        response = self.client.post(
            reverse("bank:statement_formats"), {"action": "enregistrer", "name": "Banque après", **ONE_COLUMN_LAYOUT}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StatementFormat.objects.get(name="Banque après").position, section.MAX_POSITION + 1)
        first = StatementFormat.objects.get(name="Ordre au plus haut A")
        response = self.client.post(reverse("bank:statement_format", args=[first.pk]), {"action": "descendre"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(StatementFormat.objects.filter(position__gte=section.MAX_POSITION).values_list("name", flat=True)),
            ["Ordre au plus haut B", "Ordre au plus haut A", "Banque après"],
        )


class IgnoreRuleCheckTests(RulesData, TestCase):
    """What the archive says of the ignore rules, read before it is trusted:
    the model's own check runs on every rule created - one stray « | » would
    hide every payment still missing its invoice."""

    def adding(self, *records) -> ArchiveReader:
        def change(payload):
            payload["rules"] += list(records)
            return payload

        return self.forged(change)

    def test_rules_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        for value in ({}, "URSSAF", ["URSSAF"], [{"pattern": "LOYER"}, 3]):
            with self.subTest(value=value):

                def change(payload, value=value):
                    payload["rules"] = value
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                with self.assertRaisesMessage(
                    ArchiveError, "Archive refusée : dans regles_banque.json, « rules » n'est pas une liste d'objets."
                ):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)

    def test_the_refusal_names_the_file_it_read(self):
        # From an older archive, the rules are read in banque.json: the
        # sentence names the file a person would open.
        current = export_archive({"banque", KEY})
        self.addCleanup(current.close)

        def change(payload):
            payload["rules"] = {"pattern": "URSSAF"}
            return payload

        reader = ArchiveReader(forge(written_by_the_old_code(current), banque=change))
        self.addCleanup(reader.close)
        with self.assertRaisesMessage(
            ArchiveError, "Archive refusée : dans banque.json, « rules » n'est pas une liste d'objets."
        ):
            import_archive(reader, {KEY: MERGE})

    def test_a_rule_matching_every_label_is_refused(self):
        run = import_archive(self.adding({"pattern": "URSSAF|", "description": "trop large", "is_active": True}), MERGE)
        self.assertEqual(
            rules_report(run).skipped,
            [
                (
                    "Règle « URSSAF| » : motif refusé — le motif accepte une ligne vide : il trouverait quelque "
                    "chose sur n'importe quelle ligne"
                )
            ],
        )
        self.assertFalse(IgnoreRule.objects.filter(pattern="URSSAF|").exists())

    def test_a_rule_that_is_no_regular_expression_is_refused(self):
        run = import_archive(self.adding({"pattern": "LOYER (", "description": "", "is_active": True}), MERGE)
        self.assertEqual(
            rules_report(run).skipped, ["Règle « LOYER ( » : motif refusé — parenthèse non fermée (position 7)"]
        )
        self.assertFalse(IgnoreRule.objects.filter(pattern="LOYER (").exists())

    def test_a_count_past_what_re_can_hold_is_refused_on_the_preview_and_the_import(self):
        """`re.compile` raises OverflowError on it, not re.error: the preview
        of « Données » itself was a 500, and so was the import."""
        pattern = "A{4294967296}"
        reader = self.adding({"pattern": pattern, "description": "énorme", "is_active": True})
        before = db_fingerprint()
        preview = import_archive(reader, MERGE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        run = import_archive(reader, MERGE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(rules_report(run).skipped, [f"Règle « {pattern} » : motif refusé — motif invalide"])
        self.assertFalse(IgnoreRule.objects.filter(pattern=pattern).exists())
        self.assertEqual(tally(run, RULES).unchanged, 2)

    def test_a_pattern_the_guard_refuses_is_never_compiled(self):
        """« Règles de la banque » is made to be taken from one bar to
        another: a foreign pattern is refused before `regex` could freeze
        the machine compiling it (returnables/patterns.py), as a
        recognition rule's is."""
        for pattern, reason in (
            (r"(?:x{65535}){65535}", "répétition trop grande"),
            (r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
            (r"a{e<=1}", "accolade"),
            ("URSSAF|A{101}", "répétition trop grande : 100 fois au plus"),
        ):
            with self.subTest(pattern=pattern):
                reader = self.adding({"pattern": pattern, "description": "piégée", "is_active": True})
                never = NeverCompile()
                with mock.patch.object(regex, "compile", new=never):
                    run = import_archive(reader, MERGE)
                self.assertEqual(never.calls, [])
                (skipped,) = rules_report(run).skipped
                self.assertTrue(skipped.startswith(f"Règle « {pattern} » : motif refusé — "), skipped)
                self.assertIn(reason, skipped)
                self.assertFalse(IgnoreRule.objects.filter(pattern=pattern).exists())

    def test_a_pattern_that_backtracks_is_taken_and_stopped_where_it_runs(self):
        """No check made before the lines are read can tell a pattern that
        backtracks without end on some label: the guard lets it through,
        and the time limit stops it wherever it is drawn (bank/rules.py) -
        set aside, said, never a page that does not come back."""
        pattern = r"(?:A |A  ?)+B"
        run = import_archive(self.adding({"pattern": pattern, "description": "Lente", "is_active": True}), MERGE)
        self.assertEqual((rules_report(run).skipped, tally(run, RULES).created), ([], 1))
        BankTransaction.objects.create(
            operation_date=date(2026, 7, 20),
            label=f"PRLV SEPA {'A ' * 22}ECH/200726",
            amount=Decimal("-10.00"),
            fingerprint="lente",
        )
        response = self.client.get(reverse("bank:bank_home"))
        self.assertIn("Règle « Lente » : motif trop lent, ignoré - simplifiez-le.", response.context["ignore_problems"])

    def test_a_rule_without_a_pattern_is_refused(self):
        run = import_archive(self.adding({"pattern": "  ", "description": "vide"}), MERGE)
        self.assertEqual(rules_report(run).skipped, ["Règle sans motif"])

    def test_the_same_pattern_twice_in_the_archive_is_created_once(self):
        reader = self.adding({"pattern": "URSSAF", "description": "URSSAF bis"})
        IgnoreRule.objects.all().delete()
        run = import_archive(reader, MERGE)
        self.assertEqual(rules_report(run).skipped, ["Règle « URSSAF » : en double dans l'archive"])
        self.assertEqual(IgnoreRule.objects.get(pattern="URSSAF").description, "Cotisations")

    def test_an_unknown_field_is_said_once(self):
        def change(payload):
            for item in payload["rules"]:
                item["humeur"] = "calme"
            payload["couleur"] = "bleu"
            return payload

        run = import_archive(self.forged(change), MERGE)
        notes = rules_report(run).notes
        # Under the row the rules are counted on, as the formats' and the
        # recognition rules' notes are: « règles » alone, beside two other
        # kinds of rules, names none of them.
        self.assertEqual(notes.count(f"champ inconnu ignoré : {RULES} › humeur"), 1)
        self.assertIn("champ inconnu ignoré : regles_banque.json › couleur", notes)
        self.assertEqual(rules_report(run).skipped, [])


class UnreadableMomentTests(RulesData, TestCase):
    """Under « Remplacer » a record whose compared fields differ is written
    whole, its moment included - a moment that is never compared. One the
    section cannot read is that record's reason: skipped and said, the record
    here kept as it was, and never a 500 on the preview."""

    #: The moment the archive gives → the reason said after the field's name.
    MOMENTS = {None: "valeur manquante", "hier": "date illisible (« hier »)"}

    def assert_skipped_as_previewed(self, change, skipped):
        """Preview, then confirm, `change` under « Remplacer »: both say
        exactly `skipped` and nothing else, and nothing is written."""
        reader = self.forged(change)
        before = db_fingerprint()
        preview = import_archive(reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        run = import_archive(reader, REPLACE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual((rules_report(run).skipped, rules_report(run).conflicts), ([skipped], []))
        self.assertEqual(db_fingerprint(), before)
        self.assertFalse(run.affected())
        return run

    def test_an_ignore_rule_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    urssaf = next(item for item in payload["rules"] if item["pattern"] == "URSSAF")
                    urssaf.update(description="Cotisations du trimestre", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(change, f"Règle « URSSAF » : « created_at » : {reason}")
                rule = IgnoreRule.objects.get(pattern="URSSAF")
                self.assertEqual((rule.description, rule.created_at), ("Cotisations", RULE_MOMENT))
                counted = tally(run, RULES)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 1))

    def test_a_recognition_rule_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    terminal = next(item for item in payload["operation_rules"] if item["name"] == TERMINAL_RULE)
                    terminal.update(pattern=r"EXEMPLE PAY VIREMENT", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Règle de reconnaissance « {TERMINAL_RULE} » : « created_at » : {reason}"
                )
                rule = OperationRule.objects.get(pk=self.terminal_rule.pk)
                self.assertEqual((rule.pattern, rule.created_at), (r"EXEMPLE PAY REMISE", RECOGNITION_MOMENT))
                counted = tally(run, RECOGNITION)
                self.assertEqual(
                    (counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, len(SEEDED))
                )

    def test_a_statement_format_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    tab = next(item for item in payload["statement_formats"] if item["name"] == TAB_FORMAT)
                    tab.update(decimal_mark=",", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Format de relevé « {TAB_FORMAT} » : « created_at » : {reason}"
                )
                fmt = StatementFormat.objects.get(pk=self.tab_format.pk)
                self.assertEqual((fmt.decimal_mark, fmt.created_at), (".", FORMAT_MOMENT))
                counted = tally(run, FORMATS)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 1))


class ClearTests(RulesData, TestCase):
    def test_a_preview_changes_nothing(self):
        before = db_fingerprint()
        preview = run_clear({KEY}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        confirmed = run_clear({KEY}, preview=False)
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_every_format_and_rule_goes_the_seeded_ones_too(self):
        run = run_clear({KEY}, preview=False)
        self.assertEqual(BankRulesSection().count(), dict.fromkeys(ENTITIES, 0))
        self.assertEqual(
            {entity: tally(run, entity).deleted for entity in ENTITIES},
            {FORMATS: 2, RECOGNITION: len(SEEDED) + 1, RULES: 2},
        )
        self.assertEqual(run.affected(), {KEY})

    def test_the_statement_formats_go_and_the_report_says_what_that_costs(self):
        # The seeded one too: until one is back, an import is refused. The
        # Effacer tab says it before the clear.
        run = run_clear({KEY}, preview=False)
        self.assertFalse(StatementFormat.objects.exists())
        self.assertIn(FORMAT_CLEAR_NOTE, rules_report(run).notes)
        self.assertIn("Format du relevé", FORMAT_CLEAR_NOTE)
        self.assertIn("sauvegarde", FORMAT_CLEAR_NOTE)
        self.assertIn("sans format aucun relevé ne s'importe", registry.INFO[KEY].clear_note)
        self.assertIn("Formats de relevé", registry.INFO[KEY].description)
        # Nothing to take, nothing said.
        run = run_clear({KEY}, preview=False)
        self.assertNotIn(FORMAT_CLEAR_NOTE, rules_report(run).notes)

    def test_the_recognition_rules_go_and_the_report_says_what_that_costs(self):
        # The seeded ones too: until they are back, a statement imported
        # recognises nothing. The Effacer tab says it before the clear.
        run = run_clear({KEY}, preview=False)
        self.assertFalse(OperationRule.objects.exists())
        self.assertIn(RECOGNITION_CLEAR_NOTE, rules_report(run).notes)
        self.assertIn("Reconnaissance des opérations", RECOGNITION_CLEAR_NOTE)
        self.assertIn("sans règles un relevé n'est plus reconnu", registry.INFO[KEY].clear_note)
        self.assertIn("la sauvegarde", registry.INFO[KEY].clear_note)
        # Nothing to take, nothing said.
        run = run_clear({KEY}, preview=False)
        self.assertNotIn(RECOGNITION_CLEAR_NOTE, rules_report(run).notes)

    def test_each_note_is_said_for_what_went(self):
        # The recognition rules already gone: the formats' note alone.
        OperationRule.objects.all().delete()
        self.assertEqual(rules_report(run_clear({KEY}, preview=False)).notes, [FORMAT_CLEAR_NOTE])
        # Only ignore rules to take: no note - a payment they hid shows
        # again as missing its invoice, which the page says itself.
        make_rule("URSSAF")
        run = run_clear({KEY}, preview=False)
        self.assertEqual((tally(run, RULES).deleted, rules_report(run).notes), (1, []))

    def test_clearing_the_rules_clears_only_the_rules(self):
        # Nothing requires them: « Banque » only recommends them.
        self.assertEqual(registry.closure({KEY}, "clear"), {KEY})
        self.assertEqual(registry.INFO[KEY].requires, ())
        self.assertIn(KEY, registry.INFO["banque"].recommends)
