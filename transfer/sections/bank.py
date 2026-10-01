"""« Banque » (§7.7): the bank's lines, which invoice each one paid, the
rules for the payments that never have one, the rules that recognise what
an operation is (« Reconnaissance des opérations »), the layouts of the
bank's CSV export (« Format du relevé »), the payee names learnt for
suppliers, and the payers retained on « Entrées d'argent ».

What is worth keeping here is not the lines - the next statement import
brings them back - but the decisions a person took on them: a link made by
hand, a line unlinked, a line declared « pas de facture »
(`settled_by_hand`, `no_invoice`), what a credit is in the till
(`income_source`, and `IncomePayer` for every credit of one payer). Nothing
can rebuild those, so:

* a line is its `fingerprint`, computed from the statement's own content
  (`bank.statements._fingerprint`): the same line has the same key in every
  database;
* a payment names its invoice by `InvoiceKey`, never by pk, and a payment
  whose invoice is absent here is skipped and said, never guessed;
* « Fusionner » never changes a decision this database holds: a line
  decided otherwise here is a conflict, kept as it is, links included; and a
  line settled by hand here that lacks a link of the archive's keeps lacking
  it (a conflict too), since `bank.reconcile.unlink` leaves nothing else on
  the line to tell an undone link by;
* an import makes no decision of its own either. It restores the links the
  archive holds and runs no automatic matching (`bank.reconcile.reconcile`):
  a link nobody made would pass for one somebody did.

The recognition rules (`OperationRule`) are configuration, merged like the
payers: keyed by their name as `bank.recognition.name_key` reads it, every
pattern checked by the model's own `clean` - the guard of
`returnables.patterns` before anything compiles it - and an archive written
before them says nothing of them. A line keeps the kind, payee and card date
it was imported with: an import writes rows, it reads nothing again.

The statement formats (`StatementFormat`) travel the same way: keyed by
their name as `name_key` reads it, every format written checked by the
model's own `clean` (`bank.statements.check_format`, the account pattern
through the same guard), and an archive written before them says nothing of
them. A line keeps the fingerprint it was imported with: a format changed
by an import reads no statement again.

It requires nothing (§2.1): a hard link to the invoices would make
« Effacer les factures » wipe the bank too. The invoices section counts the
payments its deletions cascade into this section's report, and the lines
keep their `settled_by_hand`. Importing this section with the invoices, in
one run (« Fusionner »), puts their links back: an invoice that run creates
was not here, so no person here can have undone a link to it. Imported on
its own once the invoices are back, it cannot tell, and says so
(`UNDONE_NOTE`).
"""

from __future__ import annotations

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db.models import Prefetch

from bank.income import payer_key
from bank.matching import alias_key
from bank.models import (
    BankTransaction,
    CounterpartyAlias,
    IgnoreRule,
    IncomePayer,
    IncomeSource,
    InvoicePayment,
    OperationRule,
    StatementFormat,
)
from bank.recognition import PATTERN_LABEL, name_key
from bank.reconcile import invoice_label
from common import format_money
from invoices.models import Invoice, Supplier
from transfer import codec, keys, registry
from transfer.archive import ArchiveError
from transfer.sections.base import Section

KEY = "banque"

# The JSON of §7.7, field by field. The guard test holds every concrete field
# of the four models to being in one of these or in NOT_EXPORTED, so a field
# added to a model later cannot be left out in silence.
# `category` is a decision too - what a person said a spending was for -
# and nothing rebuilds it: a statement re-imported brings back the line and
# not one word of it. So is `income_source`, what a person said a credit is
# in the till; an archive written before it says nothing of it, and a line
# keeps what it has (`codec` reads a key left out as « not said »).
TRANSACTION_FIELDS = (
    "account",
    "operation_date",
    "value_date",
    "card_date",
    "bank_type",
    "kind",
    "label",
    "counterparty",
    "amount",
    "no_invoice",
    "settled_by_hand",
    "category",
    "income_source",
    "imported_at",
)
# Never the import date (§6.4): merging an archive of the same statement taken
# a minute later is « inchangé », not a conflict.
TRANSACTION_COMPARED = tuple(name for name in TRANSACTION_FIELDS if name != "imported_at")
# The model has no default for these: a line the archive creates without one
# would fail on insert, so it is skipped with the reason instead.
TRANSACTION_REQUIRED = ("operation_date", "label", "amount")
PAYMENT_FIELDS = ("method", "created_at")
RULE_FIELDS = ("description", "is_active", "category", "created_at")
RULE_COMPARED = ("description", "is_active", "category")
PAYER_FIELDS = ("source", "created_at")
PAYER_COMPARED = ("source",)
# The name is the key, as `bank.recognition.name_key` reads it, and is
# compared all the same: a rule spelt otherwise here was renamed. The
# position too - the first rule of its kind that finds its pattern decides.
RECOGNITION_FIELDS = ("name", "meaning", "searched", "pattern", "position", "is_active", "created_at")
RECOGNITION_COMPARED = tuple(name for name in RECOGNITION_FIELDS if name != "created_at")
# No default in the model: a rule the archive creates without one is
# skipped with « valeur manquante ».
RECOGNITION_REQUIRED = ("meaning", "pattern")
# The statement formats like the recognition rules: the name is the key and
# is compared, the position too - the first format is the one an import
# uses when nobody chooses.
FORMAT_FIELDS = (
    "name",
    "position",
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
FORMAT_COMPARED = tuple(name for name in FORMAT_FIELDS if name != "created_at")
# No default in the model: a format the archive creates without one is
# skipped with « valeur manquante ».
FORMAT_REQUIRED = ("date_column", "label_columns")

EXPORTED = {
    BankTransaction: ("fingerprint", *TRANSACTION_FIELDS),
    InvoicePayment: PAYMENT_FIELDS,
    CounterpartyAlias: ("name",),
    IgnoreRule: ("pattern", *RULE_FIELDS),
    IncomePayer: ("key", *PAYER_FIELDS),
    OperationRule: RECOGNITION_FIELDS,
    StatementFormat: FORMAT_FIELDS,
}
NOT_EXPORTED = {
    BankTransaction: {"id": "pk"},
    InvoicePayment: {"id": "pk", "transaction": "parent", "invoice": "by key"},
    CounterpartyAlias: {"id": "pk", "supplier": "by key"},
    IgnoreRule: {"id": "pk"},
    IncomePayer: {"id": "pk"},
    OperationRule: {"id": "pk"},
    StatementFormat: {"id": "pk"},
}

TOP_LEVEL = (
    "supplier_names",
    "transactions",
    "aliases",
    "rules",
    "operation_rules",
    "statement_formats",
    "income_payers",
)
TRANSACTION_KEYS = ("fingerprint", *TRANSACTION_FIELDS, "payments")
PAYMENT_KEYS = ("invoice", *PAYMENT_FIELDS)
ALIAS_KEYS = ("supplier", "name")
RULE_KEYS = ("pattern", *RULE_FIELDS)
PAYER_KEYS = ("key", *PAYER_FIELDS)

# Report rows, in the order of count(): the page's table and the picker's
# counts say the same words.
OPERATIONS = "opérations"
PAYMENTS = "paiements"
RULES = "règles"
# Not « règles » alone: those are the « sans facture » ones.
RECOGNITION = "règles de reconnaissance"
FORMATS = "formats de relevé"
ALIASES = "noms de payeurs appris"
# Not « payeurs » alone: « noms de payeurs appris » are the suppliers'.
PAYERS = "payeurs retenus (entrées d'argent)"
ENTITIES = (OPERATIONS, PAYMENTS, RULES, RECOGNITION, FORMATS, ALIASES, PAYERS)

FIELD_LABELS = {
    "account": "compte",
    "operation_date": "date d'opération",
    "value_date": "date de valeur",
    "card_date": "date de la carte",
    "bank_type": "type d'opération",
    "kind": "nature",
    "label": "libellé",
    "counterparty": "bénéficiaire",
    "amount": "montant",
    "no_invoice": "« pas de facture »",
    "settled_by_hand": "« réglée à la main »",
    "category": "catégorie",
    "income_source": "« en caisse »",
    "description": "nom",
    "is_active": "active",
    "source": "« en caisse »",
    "name": "nom",
    "meaning": "signifie",
    "searched": "cherché dans",
    "pattern": "motif",
    "position": "ordre",
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

# The page's own button, which links what bank.matching is sure of.
RECONCILE_NOTE = (
    "Pour lier automatiquement les nouvelles opérations : « Relancer le rapprochement » sur la page Banque."
)
CLEAR_NOTE = (
    "Les opérations reviennent en important de nouveau le relevé de la banque, mais sans leurs liens ni les "
    "décisions prises à la main : celles-ci ne reviennent que d'une archive."
)
# Said when a clear takes the recognition rules: the seeded ones go too.
RECOGNITION_CLEAR_NOTE = (
    "Sans règles de reconnaissance, un relevé importé n'est plus reconnu : ramenez-les de la sauvegarde ou "
    "saisissez-les sur « Reconnaissance des opérations »."
)
# Said when a clear takes the statement formats: the seeded one goes too,
# and an import is refused until one is back.
FORMAT_CLEAR_NOTE = (
    "Sans format de relevé, aucun relevé ne s'importe : ramenez les formats de la sauvegarde ou saisissez-en un "
    "sur « Format du relevé »."
)
# Said once a run, beside the conflicts « réglée à la main ici sans payer … »:
# the same line is left by a person's « délier » and by its invoice deleted
# then brought back by another run, and only the person knows which.
UNDONE_NOTE = (
    "Une opération réglée à la main ici sans une facture qu'elle paie dans l'archive est gardée telle quelle : "
    "ce lien a pu être défait à la main, ou partir avec sa facture, effacée puis revenue depuis. "
    "Dans ce second cas, refaites-le sur la page Banque, ou, si toute la banque de l'archive est à reprendre, "
    "importez-la en « Remplacer ». Des factures effacées reviennent avec leurs liens quand « Factures et "
    "tickets » et « Banque » sont importées ensemble."
)

DELETE_BATCH = 500
# The highest position an archive may give a recognition rule or a format:
# Django's range for a PositiveIntegerField on every backend but SQLite,
# whose own (to 2**63 - 1) leaves the pages no room above it - a new one
# comes at the highest + 1 and positions shared are made distinct by + 1
# (bank/views.py `_saved`, `_swapped`), and past 2**63 - 1 that is an
# OverflowError: every new format or rule saved after it was a 500.
MAX_POSITION = 2**31 - 1


def _fields(names) -> str:
    """At most three, as §6.1 says: the conflict is a line to read, not a diff."""
    return ", ".join(FIELD_LABELS.get(name, name) for name in list(names)[:3])


def _euros(amount) -> str:
    return format_money(amount).replace(".", ",") + " €"


def _operation(line: BankTransaction) -> str:
    """How the report names a line: its date, who was paid, how much - what
    the bank page shows of it."""
    who = line.counterparty or " ".join((line.label or "").split())[:40]
    try:
        return f"Opération du {line.operation_date:%d/%m/%Y} ({who}, {_euros(line.amount)})"
    except (TypeError, ValueError):  # a line the archive gave no date or amount
        return f"Opération {line.fingerprint[:12]}…"


def _key_label(ctx, key) -> str:
    """An invoice key that found nothing here, named as the archive names it."""
    if not isinstance(key, dict):
        return "référence illisible"
    code = key.get("supplier") if isinstance(key.get("supplier"), str) else ""
    name = ctx.suppliers.names.get(code) or code or "fournisseur inconnu"
    number = key.get("number")
    return f"{name} n° {number}" if isinstance(number, str) and number else f"{name}, document sans numéro"


def _readable_key(key) -> bool:
    """An InvoiceKey as the format writes it: text, and an int occurrence. A
    hand-edited one that is not would make the index's lookups raise."""
    if not isinstance(key, dict):
        return False
    for name in keys.KEY_FIELDS:
        value = key.get(name)
        if value is None:
            continue
        if name == "occurrence":
            if isinstance(value, bool) or not isinstance(value, int):
                return False
        elif not isinstance(value, str):
            return False
    return True


def _restore(objects_and_moments, field_name: str) -> None:
    """auto_now_add overwrites the value on insert - bulk_create included -
    and bulk_update does not call pre_save: the archive's moment goes back
    after the insert (§4.3)."""
    restored = []
    for obj, moment in objects_and_moments:
        if moment is not None:
            setattr(obj, field_name, moment)
            restored.append(obj)
    if restored:
        type(restored[0]).objects.bulk_update(restored, [field_name])


def _delete_ids(model, ids) -> int:
    """By batches: a replace of a few thousand lines must not meet SQLite's
    limit on query parameters."""
    ids = list(ids)
    deleted = 0
    for start in range(0, len(ids), DELETE_BATCH):
        _total, per_model = model.objects.filter(pk__in=ids[start : start + DELETE_BATCH]).delete()
        deleted += per_model.get(model._meta.label, 0)
    return deleted


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
        raise codec.FieldValueError(f"format refusé — {label} : {_sentence(' '.join(messages), label)}") from None
    try:
        fmt.full_clean()
    except ValidationError as exc:
        raise codec.FieldValueError(f"« {next(iter(exc.message_dict))} » : valeur refusée") from None
    _check_position(fmt)


def _sentence(message: str, label: str) -> str:
    """A refusal of `check_format` said after its field's name: the name it
    may already open with left out, no closing full stop, a capital put
    down (« Un numéro de colonne … » → « un numéro de colonne … »)."""
    text = message.strip()
    if label and text.lower().startswith(f"{label.lower()} : "):
        text = text[len(label) + 3 :]
    text = text.rstrip(".")
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    return text


@registry.register
class BankSection(Section):
    key = KEY

    # -- what this database holds ------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            OPERATIONS: BankTransaction.objects.count(),
            PAYMENTS: InvoicePayment.objects.count(),
            RULES: IgnoreRule.objects.count(),
            RECOGNITION: OperationRule.objects.count(),
            FORMATS: StatementFormat.objects.count(),
            ALIASES: CounterpartyAlias.objects.count(),
            PAYERS: IncomePayer.objects.count(),
        }

    def snapshot(self):
        payments = list(InvoicePayment.objects.order_by("id"))
        invoice_keys = keys.invoice_keys([payment.invoice_id for payment in payments])
        by_line = defaultdict(list)
        for payment in payments:
            key = invoice_keys[payment.invoice_id]
            by_line[payment.transaction_id].append(
                [[key[name] for name in keys.KEY_FIELDS], *codec.record(payment, PAYMENT_FIELDS).values()]
            )
        return {
            "transactions": [
                {
                    "fingerprint": line.fingerprint,
                    **codec.record(line, TRANSACTION_FIELDS),
                    "payments": sorted(by_line[line.pk]),
                }
                for line in BankTransaction.objects.order_by("fingerprint")
            ],
            "aliases": sorted(
                [code, name] for code, name in CounterpartyAlias.objects.values_list("supplier__code", "name")
            ),
            "rules": sorted(
                [rule.pattern, *codec.record(rule, RULE_FIELDS).values()] for rule in IgnoreRule.objects.all()
            ),
            # The order is in it: `position` is a field like the others.
            "operation_rules": sorted(
                list(codec.record(rule, RECOGNITION_FIELDS).values()) for rule in OperationRule.objects.all()
            ),
            # By name, which is unique: the lists never compare a column
            # that may be None.
            "statement_formats": sorted(
                list(codec.record(fmt, FORMAT_FIELDS).values()) for fmt in StatementFormat.objects.all()
            ),
            "income_payers": sorted(
                [payer.key, *codec.record(payer, PAYER_FIELDS).values()] for payer in IncomePayer.objects.all()
            ),
        }

    # -- export ----------------------------------------------------------------
    def export(self, out) -> None:
        # In id order: lines of one day keep the order the statement gave
        # them once created again (the page sorts by date, then id).
        lines = list(
            BankTransaction.objects.order_by("id").prefetch_related(
                Prefetch("payments", queryset=InvoicePayment.objects.order_by("id"))
            )
        )
        paid = [payment.invoice_id for line in lines for payment in line.payments.all()]
        invoice_keys = keys.invoice_keys(paid)
        aliases = list(CounterpartyAlias.objects.select_related("supplier").order_by("supplier__code", "name"))
        rules = list(IgnoreRule.objects.order_by("id"))
        # In the order they are asked: rules of one position come back in it.
        recognition = list(OperationRule.objects.order_by("position", "name"))
        # In the order the import card offers them, the first the default.
        formats = list(StatementFormat.objects.order_by("position", "name"))
        payers = list(IncomePayer.objects.order_by("key"))
        codes = {key["supplier"] for key in invoice_keys.values()} | {alias.supplier.code for alias in aliases}
        payload = {
            # A code may differ in the database this is imported into (LIDL
            # there, LIDL_2 here): its name lets the invoice keys and the
            # aliases still find their supplier (§5.4).
            "supplier_names": dict(
                Supplier.objects.filter(code__in=codes).order_by("code").values_list("code", "name")
            ),
            "transactions": [
                {
                    "fingerprint": line.fingerprint,
                    **codec.record(line, TRANSACTION_FIELDS),
                    "payments": [
                        {"invoice": invoice_keys[payment.invoice_id], **codec.record(payment, PAYMENT_FIELDS)}
                        for payment in line.payments.all()
                    ],
                }
                for line in lines
            ],
            "aliases": [{"supplier": alias.supplier.code, "name": alias.name} for alias in aliases],
            "rules": [{"pattern": rule.pattern, **codec.record(rule, RULE_FIELDS)} for rule in rules],
            # Always said, empty included: absent, the list reads as an
            # archive written before the rules existed (« not said »).
            "operation_rules": [codec.record(rule, RECOGNITION_FIELDS) for rule in recognition],
            # Always said too, for the same reason. A tab separator is JSON's
            # « \t » and reads back as the tab it was.
            "statement_formats": [codec.record(fmt, FORMAT_FIELDS) for fmt in formats],
            "income_payers": [{"key": payer.key, **codec.record(payer, PAYER_FIELDS)} for payer in payers],
        }
        out.write(
            payload,
            {
                OPERATIONS: len(lines),
                PAYMENTS: len(paid),
                RULES: len(rules),
                RECOGNITION: len(recognition),
                FORMATS: len(formats),
                ALIASES: len(aliases),
                PAYERS: len(payers),
            },
        )

    # -- import ----------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        for name in ("transactions", "aliases", "rules"):
            items = payload.get(name)
            # A list left out is refused, not read as empty: under « Remplacer »
            # an empty list deletes everything this database has of it.
            if not isinstance(items, list):
                raise ArchiveError(f"Archive refusée : banque.json n'a pas de liste « {name} ».")
            if not all(isinstance(item, dict) for item in items):
                raise ArchiveError(f"Archive refusée : dans banque.json, « {name} » ne contient pas que des objets.")
        for record in payload["transactions"]:
            if "payments" not in record:
                continue
            payments = record["payments"]
            if not isinstance(payments, list) or not all(isinstance(item, dict) for item in payments):
                raise ArchiveError(
                    "Archive refusée : dans banque.json, les liens d'une opération ne sont pas une liste."
                )
        # The payers retained came after the format: an archive written
        # before them says nothing of them - None, never an empty list, which
        # « Remplacer » would read as « forget every payer here ».
        payers = payload.get("income_payers")
        if payers is not None and (not isinstance(payers, list) or not all(isinstance(item, dict) for item in payers)):
            raise ArchiveError("Archive refusée : dans banque.json, « income_payers » n'est pas une liste d'objets.")
        self._payers: list | None = payers
        # The recognition rules too (bank/0006): None is « not said », never
        # « forget every rule here » - and never one of the lists required
        # above, or every archive written before them would be refused.
        recognition = payload.get("operation_rules")
        if recognition is not None and (
            not isinstance(recognition, list) or not all(isinstance(item, dict) for item in recognition)
        ):
            raise ArchiveError("Archive refusée : dans banque.json, « operation_rules » n'est pas une liste d'objets.")
        self._recognition: list | None = recognition
        # The statement formats (bank/0007) alike: an archive written before
        # them says nothing of them.
        formats = payload.get("statement_formats")
        if formats is not None and (
            not isinstance(formats, list) or not all(isinstance(item, dict) for item in formats)
        ):
            raise ArchiveError(
                "Archive refusée : dans banque.json, « statement_formats » n'est pas une liste d'objets."
            )
        self._formats: list | None = formats
        self.payload = payload
        # What the file names, whatever becomes of its records: prune never
        # deletes a line, rule or name the archive holds, even one it could
        # not read.
        self._fingerprints: set[str] = set()
        self._patterns: set[str] = set()
        self._rule_ids: dict[str, int] = {}
        self._alias_keys: set[tuple[int, str]] = set()
        self._payer_keys: set[str] = set()
        # Recognition rules by `name_key`, and the one rule here each key
        # answers to (a second of the same key is one too many).
        self._recognition_keys: set[str] = set()
        self._recognition_ids: dict[str, int] = {}
        # Statement formats the same way.
        self._format_keys: set[str] = set()
        self._format_ids: dict[str, int] = {}
        # The payers this run creates: their lines' own choices travel with
        # them (`_apply_transactions`).
        self._created_payers: set[str] = set()
        # The invoices here before the run: the runner loads every section
        # before the first apply, and by this section's turn the invoices
        # section has created its own. An invoice absent from this set came
        # with this run, so no person here can have undone a link to it.
        self._invoices_before: set[int] = set(Invoice.objects.values_list("pk", flat=True))

    def apply(self, ctx, report) -> None:
        for entity in ENTITIES:  # one row each, even when nothing moves
            report.unchanged(entity, 0)
        codec.note_unknown(report, self.payload, TOP_LEVEL, where="banque.json › ")
        replacing = ctx.replacing(self.key)
        self._apply_rules(report, replacing)
        self._apply_recognition(report, replacing)
        self._apply_formats(report, replacing)
        self._apply_aliases(ctx, report)
        self._apply_payers(report, replacing)
        self._apply_transactions(ctx, report, replacing)

    # rules -------------------------------------------------------------------
    def _apply_rules(self, report, replacing: bool) -> None:
        existing: dict[str, IgnoreRule] = {}
        for rule in IgnoreRule.objects.order_by("id"):
            existing.setdefault(rule.pattern, rule)
        created = []
        for record in self.payload["rules"]:
            codec.note_unknown(report, record, RULE_KEYS, where="règles › ")
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
                    # The model's own check: one stray "|" in a pattern would
                    # hide every payment still missing its invoice.
                    rule.full_clean()
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
            except ValidationError as exc:
                report.skip(f"Règle « {pattern} » : {' '.join(exc.messages)}")
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
                    f"Règle « {pattern} » : différente dans l'archive ({_fields(different)}) — gardée telle quelle"
                )
        _restore(created, "created_at")

    # recognition rules -------------------------------------------------------
    def _apply_recognition(self, report, replacing: bool) -> None:
        """What an operation of the statement is (« Reconnaissance des
        opérations »): configuration, merged like the payers - one changed
        here is a conflict, kept. Every rule written goes through the
        model's own check (`_check_recognition`), created or replaced, since
        the pattern is no key here and an archive may say any. An archive
        saying nothing of them (written before bank/0006) leaves them
        alone. Nothing already imported is read again with them."""
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
                report.conflict(f"{said} : différente dans l'archive ({_fields(different)}) — gardée telle quelle")
        _restore(created, "created_at")

    # statement formats -------------------------------------------------------
    def _apply_formats(self, report, replacing: bool) -> None:
        """How the bank lays out its CSV export (« Format du relevé »):
        configuration, merged like the recognition rules - one changed here
        is a conflict, kept. Every format written goes through the model's
        own check (`_check_format`), created or replaced: an archive may say
        any column and any account pattern. An archive saying nothing of
        them (written before bank/0007) leaves them alone."""
        if self._formats is None:
            return
        existing: dict[str, StatementFormat] = {}
        for fmt in StatementFormat.objects.order_by("position", "name"):
            existing.setdefault(name_key(fmt.name), fmt)
        created = []
        for record in self._formats:
            codec.note_unknown(report, record, FORMAT_FIELDS, where="formats de relevé › ")
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
                report.conflict(f"{said} : différent dans l'archive ({_fields(different)}) — gardé tel quel")
        _restore(created, "created_at")

    # payee names -------------------------------------------------------------
    def _apply_aliases(self, ctx, report) -> None:
        existing = set(CounterpartyAlias.objects.values_list("supplier_id", "name"))
        for record in self.payload["aliases"]:
            codec.note_unknown(report, record, ALIAS_KEYS, where="noms de payeurs › ")
            name, code = record.get("name"), record.get("supplier")
            try:
                codec.load(CounterpartyAlias, "name", name)
            except codec.FieldValueError as exc:
                report.skip(f"Nom de payeur : {exc}")
                continue
            if not name.strip():
                report.skip("Nom de payeur vide")
                continue
            supplier = ctx.suppliers.resolve(code)
            if supplier is None:
                report.skip(f"Nom de payeur « {name} » : fournisseur inconnu (« {code} »)")
                continue
            pair = (supplier.pk, name)
            if pair in self._alias_keys:
                report.skip(f"Nom de payeur « {name} » de {supplier.name} : en double dans l'archive")
                continue
            self._alias_keys.add(pair)
            if pair in existing:
                report.unchanged(ALIASES)
                continue
            CounterpartyAlias.objects.create(supplier=supplier, name=name)
            report.created(ALIASES)

    # payers retained ---------------------------------------------------------
    def _apply_payers(self, report, replacing: bool) -> None:
        """What every credit of one payer is in the till: a decision, merged
        like a rule's - one changed here is a conflict, kept. An archive
        saying nothing of payers (written before them) leaves them alone."""
        if self._payers is None:
            return
        existing = {payer.key: payer for payer in IncomePayer.objects.all()}
        created = []
        for record in self._payers:
            codec.note_unknown(report, record, PAYER_KEYS, where="payeurs retenus › ")
            key = record.get("key")
            if not isinstance(key, str) or not key.strip():
                report.skip("Payeur retenu sans nom")
                continue
            if key in self._payer_keys:
                report.skip(f"Payeur retenu « {key} » : en double dans l'archive")
                continue
            # Named before its record is read: the prune never deletes a payer
            # the archive holds, even one it could not read.
            self._payer_keys.add(key)
            if alias_key(key) != key:
                # Not a key `bank.income.payer_key` makes: it could never
                # name a credit, here or anywhere.
                report.skip(f"Payeur retenu « {key} » : nom illisible")
                continue
            payer = existing.get(key)
            try:
                codec.load(IncomePayer, "key", key)
                if payer is None:
                    codec.load(IncomePayer, "source", record.get("source"))
                    payer = IncomePayer(key=key)
                    codec.assign(payer, record, PAYER_FIELDS)
                    moment = payer.created_at
                    payer.save()
                    created.append((payer, moment))
                    self._created_payers.add(key)
                    report.created(PAYERS)
                    continue
                different = codec.differences(payer, record, PAYER_COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"Payeur retenu « {key} » : {exc}")
                continue
            if not different:
                report.unchanged(PAYERS)
            elif replacing:
                try:
                    changed = codec.assign(payer, record, PAYER_FIELDS)
                except codec.FieldValueError as exc:
                    report.skip(f"Payeur retenu « {key} » : {exc}")
                    continue
                payer.save(update_fields=changed)
                report.updated(PAYERS)
            else:
                # `source` is all that is compared, and `differences` read it.
                there = IncomeSource(record["source"]).label
                report.conflict(
                    f"Payeur retenu « {key} » : « {payer.get_source_display()} » ici, « {there} » dans l'archive "
                    f"— gardé tel quel"
                )
        _restore(created, "created_at")

    # lines and their payments ------------------------------------------------
    def _apply_transactions(self, ctx, report, replacing: bool) -> None:
        existing = {line.fingerprint: line for line in BankTransaction.objects.all()}
        decided = []  # (line, record, created here), in file order: whose links the file says
        new = []
        for record in self.payload["transactions"]:
            codec.note_unknown(report, record, TRANSACTION_KEYS, where="opérations › ")
            fingerprint = record.get("fingerprint")
            try:
                if not fingerprint:
                    raise codec.FieldValueError("empreinte manquante")
                codec.load(BankTransaction, "fingerprint", fingerprint)
            except codec.FieldValueError as exc:
                report.skip(f"Opération sans empreinte lisible : {exc}")
                continue
            if fingerprint in self._fingerprints:
                report.skip(f"Opération {fingerprint[:12]}… : en double dans l'archive")
                continue
            self._fingerprints.add(fingerprint)
            line = existing.get(fingerprint)
            if line is None:
                line = BankTransaction(fingerprint=fingerprint)
                try:
                    for name in TRANSACTION_REQUIRED:
                        codec.load(BankTransaction, name, record.get(name))
                    codec.assign(line, record, TRANSACTION_FIELDS)
                except codec.FieldValueError as exc:
                    report.skip(f"{_operation(line)} : {exc}")
                    continue
                new.append(line)
                decided.append((line, record, True))
                continue
            try:
                different = codec.differences(line, record, TRANSACTION_COMPARED)
            except codec.FieldValueError as exc:
                # Kept whole, links included: a record that cannot be read
                # says nothing about this line.
                report.skip(f"{_operation(line)} : {exc}")
                continue
            took_choice = False
            if not replacing and self._took_its_payers_choice(line, record, different):
                different = [name for name in different if name != "income_source"]
                took_choice = True
                if not different:
                    report.updated(OPERATIONS)
                    decided.append((line, record, False))
                    continue
            if not different:
                report.unchanged(OPERATIONS)
            elif replacing:
                # Every field, not just the compared ones: an import date that
                # cannot be read is that line's reason, never a 500 - and the
                # line keeps its links, as one whose record cannot be read.
                try:
                    changed = codec.assign(line, record, TRANSACTION_FIELDS)
                except codec.FieldValueError as exc:
                    report.skip(f"{_operation(line)} : {exc}")
                    continue
                line.save(update_fields=changed)
                report.updated(OPERATIONS)
            else:
                # A merge never resets a person's decision (« réglée à la
                # main », « pas de facture »): the line stays as it is, its
                # links too. Merged, the archive's link came back onto a line
                # unlinked by hand here, under a « réglée à la main » the
                # automatic pass never revisits - a state neither database
                # held, with nothing said.
                report.conflict(
                    f"{_operation(line)} : différente dans l'archive ({_fields(different)}) — gardée telle quelle"
                    + (", son choix « en caisse » repris avec son payeur" if took_choice else "")
                    + self._links_left_out(ctx, line, record)
                )
                continue
            decided.append((line, record, False))

        if new:
            moments = [(line, line.imported_at) for line in new]
            BankTransaction.objects.bulk_create(new)
            _restore(moments, "imported_at")
            report.created(OPERATIONS, len(new))
            report.note(RECONCILE_NOTE)

        decided = [(line, record, is_new) for line, record, is_new in decided if "payments" in record]
        # The lines whose links this run decides. A record without "payments"
        # does not say them, and one that could not be read says nothing:
        # their links stay. Under « Remplacer » the lines about to be pruned
        # lose theirs here, before any is created, so what is left is exactly
        # what the archive says and never a link the prune happened to take.
        managed = {line.pk for line, _record, _new in decided}
        if replacing:
            managed |= {line.pk for fingerprint, line in existing.items() if fingerprint not in self._fingerprints}
        listed = [(line, is_new, self._payments(ctx, report, line, record)) for line, record, is_new in decided]
        if replacing:
            self._replace_payments(report, listed, managed)
        else:
            self._merge_payments(report, listed)

    def _took_its_payers_choice(self, line, record, different) -> bool:
        """A merge creating a payer brings the choices its lines held beside
        it in the archive: there they were one decision - « tous ses
        virements sont des versements carte, sauf celui-ci » - and the payer
        alone would decide a line the archive kept apart from it, a state
        neither database held under « gardée telle quelle » (review,
        01/10/2026). Only onto a line saying nothing here: a choice made here
        is a decision, and stays a conflict."""
        if "income_source" not in different or line.income_source != IncomeSource.AUTOMATIC:
            return False
        if not self._created_payers or payer_key(line) not in self._created_payers:
            return False
        # `differences` has read it already.
        line.income_source = codec.load(BankTransaction, "income_source", record["income_source"])
        line.save(update_fields=["income_source"])
        return True

    def _links_left_out(self, ctx, line, record) -> str:
        """The archive's links a line kept as it is does not get, named in
        its conflict: « gardée telle quelle » alone did not say that the
        archive has it paying an invoice. A link that finds no invoice here
        is left unnamed - nothing is imported from this line anyway."""
        held = set(line.payments.values_list("invoice_id", flat=True))
        names = []
        for payment in record.get("payments") or []:
            key = payment.get("invoice")
            if not _readable_key(key):
                continue
            invoice = ctx.invoices.resolve(key)
            if invoice is None or isinstance(invoice, keys.Ambiguous) or invoice.pk in held:
                continue
            names.append(invoice_label(invoice))
        if not names:
            return ""
        links = "les liens" if len(names) > 1 else "le lien"
        return f", sans {links} de l'archive vers {', '.join(names)}"

    def _payments(self, ctx, report, line, record) -> list[tuple]:
        """The record's links that can be made here: (invoice, method,
        created_at), each other one skipped with its reason."""
        found = []
        seen = set()
        for payment in record["payments"]:
            codec.note_unknown(report, payment, PAYMENT_KEYS, where="paiements › ")
            key = payment.get("invoice")
            if not _readable_key(key):
                report.skip(f"{_operation(line)} : lien vers une facture illisible")
                continue
            invoice = ctx.invoices.resolve(key, report)
            if isinstance(invoice, keys.Ambiguous):
                report.skip(
                    f"{_operation(line)} : deux documents différents répondent à la facture {_key_label(ctx, key)} "
                    f"(n° {invoice.by_number.invoice_number} et le fichier de {invoice_label(invoice.by_file)}) "
                    f"— lien ignoré"
                )
                continue
            if invoice is None:
                report.skip(f"{_operation(line)} : facture absente : {_key_label(ctx, key)}")
                continue
            try:
                method = codec.load(InvoicePayment, "method", payment.get("method"))
                moment = (
                    codec.load(InvoicePayment, "created_at", payment["created_at"]) if "created_at" in payment else None
                )
            except codec.FieldValueError as exc:
                report.skip(f"{_operation(line)} : lien vers {invoice_label(invoice)} : {exc}")
                continue
            if invoice.pk in seen:
                report.skip(f"{_operation(line)} : lien vers {invoice_label(invoice)} en double dans l'archive")
                continue
            seen.add(invoice.pk)
            found.append((invoice, method, moment))
        return found

    def _merge_payments(self, report, listed) -> None:
        """One by one (§6.1): a missing link is added when the line does not
        say otherwise here. A line paying here an invoice the archive does
        not give it, or marked « pas de facture » here, is a decision this
        database holds: a conflict. So is a line settled by hand here
        without a link of the archive's, unless its invoice came with this
        run: `reconcile.unlink` leaves the line exactly as an invoice
        deleted with its payment does, and only an invoice absent when the
        run started cannot have been unlinked here.

        What holds a link back is always the LINE's own decision, never the
        invoice being paid elsewhere: an invoice settled in two goes is a
        link a person made on each of two lines (`bank.models`), and an
        archive that could not restore the second would quietly undo it.
        So every lookup here is by the PAIR - keyed by the invoice alone,
        one payment answered for the other and a plain round trip reported a
        conflict about a link that was already here.
        """
        current = list(InvoicePayment.objects.select_related("transaction", "invoice__supplier"))
        by_pair = {(payment.transaction_id, payment.invoice_id): payment for payment in current}
        by_line = defaultdict(list)
        for payment in current:
            by_line[payment.transaction_id].append(payment)
        created = []
        undone = False
        for line, is_new, payments in listed:
            wanted = {invoice.pk for invoice, _method, _moment in payments}
            others = [payment for payment in by_line.get(line.pk, []) if payment.invoice_id not in wanted]
            for invoice, method, moment in payments:
                held = by_pair.get((line.pk, invoice.pk))
                if held is not None:
                    if held.method == method:
                        report.unchanged(PAYMENTS)
                    else:
                        report.conflict(
                            f"{_operation(line)} : le lien vers {invoice_label(invoice)} est "
                            f"« {held.get_method_display()} » ici, « {InvoicePayment.Method(method).label} » dans "
                            f"l'archive — gardé tel quel"
                        )
                    continue
                if not is_new and line.no_invoice:
                    report.conflict(
                        f"{_operation(line)} : marquée « pas de facture » ici, payant {invoice_label(invoice)} dans "
                        f"l'archive — gardée telle quelle"
                    )
                    continue
                if others:
                    here = ", ".join(invoice_label(payment.invoice) for payment in others)
                    report.conflict(
                        f"{_operation(line)} : paie ici {here}, dans l'archive {invoice_label(invoice)} — gardée "
                        f"telle quelle"
                    )
                    continue
                if not is_new and line.settled_by_hand and invoice.pk in self._invoices_before:
                    # Made again, a link a person undid here counted the
                    # invoice as paid by a line they said did not pay it.
                    report.conflict(
                        f"{_operation(line)} : réglée à la main ici sans payer {invoice_label(invoice)}, qu'elle "
                        f"paie dans l'archive — gardée telle quelle"
                    )
                    undone = True
                    continue
                payment = InvoicePayment(transaction=line, invoice=invoice, method=method)
                by_pair[(line.pk, invoice.pk)] = payment
                created.append((payment, moment))
        if undone:
            report.note(UNDONE_NOTE)
        self._create_payments(report, created)

    def _replace_payments(self, report, listed, managed: set[int]) -> None:
        """The managed lines' links become exactly the archive's. Counted by
        key (line, invoice): what stays is « inchangé », a method that
        differs is « modifié », and nothing equal is written.

        The pair is the key throughout, and nothing is refused for being
        « already paid »: the archive's own word is that the invoice is paid
        by these lines, and a « Remplacer » that dropped the second of them
        would answer an archive holding two links with one."""
        current = list(InvoicePayment.objects.select_related("transaction", "invoice__supplier"))
        existing = {(payment.transaction_id, payment.invoice_id): payment for payment in current}
        wanted: dict[tuple[int, int], tuple] = {
            (line.pk, invoice.pk): (line, invoice, method, moment)
            for line, _is_new, payments in listed
            for invoice, method, moment in payments
        }

        doomed = [payment.pk for key, payment in existing.items() if key[0] in managed and key not in wanted]
        if doomed:
            report.deleted(PAYMENTS, _delete_ids(InvoicePayment, doomed))
        changed, created = [], []
        for key, (line, invoice, method, moment) in wanted.items():
            payment = existing.get(key)
            if payment is None:
                created.append((InvoicePayment(transaction=line, invoice=invoice, method=method), moment))
            elif payment.method != method:
                payment.method = method
                changed.append(payment)
            else:
                report.unchanged(PAYMENTS)
        if changed:
            InvoicePayment.objects.bulk_update(changed, ["method"])
            report.updated(PAYMENTS, len(changed))
        self._create_payments(report, created)

    def _create_payments(self, report, created) -> None:
        if not created:
            return
        InvoicePayment.objects.bulk_create([payment for payment, _moment in created])
        _restore(created, "created_at")
        report.created(PAYMENTS, len(created))

    def prune(self, ctx, report) -> None:
        # Nothing in any other section points at a bank line, a rule or a
        # payee name: what the archive does not hold goes. The lines' links
        # went in apply (above), before the archive's were made.
        lines = [
            pk
            for pk, fingerprint in BankTransaction.objects.values_list("pk", "fingerprint")
            if fingerprint not in self._fingerprints
        ]
        if lines:
            report.deleted(OPERATIONS, _delete_ids(BankTransaction, lines))
        rules = [
            pk
            for pk, pattern in IgnoreRule.objects.values_list("pk", "pattern")
            # A second rule with a pattern the archive has once is one too many.
            if pattern not in self._patterns or (pattern in self._rule_ids and self._rule_ids[pattern] != pk)
        ]
        if rules:
            report.deleted(RULES, _delete_ids(IgnoreRule, rules))
        # An archive saying nothing of the recognition rules prunes none.
        if self._recognition is not None:
            doomed = []
            for pk, name in OperationRule.objects.values_list("pk", "name"):
                key = name_key(name)
                if key not in self._recognition_keys or self._recognition_ids.get(key, pk) != pk:
                    doomed.append(pk)
            if doomed:
                report.deleted(RECOGNITION, _delete_ids(OperationRule, doomed))
        # Nor of the statement formats.
        if self._formats is not None:
            doomed = []
            for pk, name in StatementFormat.objects.values_list("pk", "name"):
                key = name_key(name)
                if key not in self._format_keys or self._format_ids.get(key, pk) != pk:
                    doomed.append(pk)
            if doomed:
                report.deleted(FORMATS, _delete_ids(StatementFormat, doomed))
        aliases = [
            pk
            for pk, supplier_id, name in CounterpartyAlias.objects.values_list("pk", "supplier_id", "name")
            if (supplier_id, name) not in self._alias_keys
        ]
        if aliases:
            report.deleted(ALIASES, _delete_ids(CounterpartyAlias, aliases))
        # An archive saying nothing of payers prunes none of them.
        if self._payers is not None:
            payers = [pk for pk, key in IncomePayer.objects.values_list("pk", "key") if key not in self._payer_keys]
            if payers:
                report.deleted(PAYERS, _delete_ids(IncomePayer, payers))

    # -- clear -----------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        counts = {
            PAYMENTS: InvoicePayment.objects.all().delete()[1].get(InvoicePayment._meta.label, 0),
            OPERATIONS: BankTransaction.objects.all().delete()[1].get(BankTransaction._meta.label, 0),
            RULES: IgnoreRule.objects.all().delete()[1].get(IgnoreRule._meta.label, 0),
            RECOGNITION: OperationRule.objects.all().delete()[1].get(OperationRule._meta.label, 0),
            FORMATS: StatementFormat.objects.all().delete()[1].get(StatementFormat._meta.label, 0),
            ALIASES: CounterpartyAlias.objects.all().delete()[1].get(CounterpartyAlias._meta.label, 0),
            PAYERS: IncomePayer.objects.all().delete()[1].get(IncomePayer._meta.label, 0),
        }
        for entity in ENTITIES:
            report.deleted(entity, counts[entity])
        if counts[OPERATIONS]:
            report.note(CLEAR_NOTE)
        if counts[RECOGNITION]:
            report.note(RECOGNITION_CLEAR_NOTE)
        if counts[FORMATS]:
            report.note(FORMAT_CLEAR_NOTE)
