"""« Banque » (§7.7): the bank's lines, which invoice each one paid, the
payee names learnt for suppliers, the payers retained on « Entrées
d'argent », and the treasury's points and adjustments (« Trésorerie »).

What reads the statements - how the bank's export is read, the rules
that recognise what an operation is and the rules for the payments that
never have an invoice - is « Règles de la banque » (sections/bank_rules.py)
since 02/10/2026: configuration another bar on the same bank takes without
these lines. This section only recommends it, and its banque.json no longer
holds them; an older archive's does, and the reader hands this section that
file without them (`archive.CARVED`). The payee names and the payers stay
here: learnt from this bar's own links and choices, they name its payers.
The treasury's points and adjustments stay here too: this bar's own
balances, typed by a person.

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
  a link nobody made would pass for one somebody did;
* a line keeps the kind, payee, card date and fingerprint it was imported
  with: an import writes rows, it reads nothing again - whatever rules and
  formats « Règles de la banque » brings in the same run.

The treasury's points (`TreasuryCheckpoint`, a balance typed for one day)
and adjustments (`TreasuryAdjustment`) are decisions too, and travel like
the payers: a point keyed by its day (unique), an adjustment by its random
`reference` - never generated here, a record without one is skipped, or the
confirm would differ from its preview. Every record written is checked by
the model's own `clean`, said in French, and its day bound to 2000-today: a
point dated tomorrow would move « Trésorerie au … » past today. An archive
written before bank/0008 says nothing of them. An import computes no
balance and resolves no gap: « Trésorerie » reads what it wrote.

It requires nothing (§2.1): a hard link to the invoices would make
« Effacer les factures » wipe the bank too. The invoices section counts the
payments its deletions cascade into this section's report, and the lines
keep their `settled_by_hand`. The other way round, a line this section's
prune or clear deletes takes with it the sales invoices it pays
(recipes.SaleDocumentPayment, CASCADE): « Ventes »' links, counted into
« Ventes »' report - which is what puts « Ventes » in the safety archive.
Importing this section with the invoices, in one run (« Fusionner »), puts
their links back: an invoice that run creates was not here, so no person
here can have undone a link to it. Imported on its own once the invoices
are back, it cannot tell, and says so (`UNDONE_NOTE`).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.utils import timezone

from bank.income import payer_key
from bank.matching import alias_key
from bank.models import (
    BankTransaction,
    CounterpartyAlias,
    IncomePayer,
    IncomeSource,
    InvoicePayment,
    TreasuryAdjustment,
    TreasuryCheckpoint,
)
from bank.reconcile import invoice_label
from bank.statements import FIRST_DAY
from common import format_money
from invoices.models import Invoice, Supplier
from recipes.models import SaleDocumentPayment
from transfer import codec, keys, registry
from transfer.archive import ArchiveError
from transfer.sections.base import SALE_LINKS, Section, delete_ids, restore_moments

KEY = "banque"

# The JSON of §7.7, field by field. The guard test holds every concrete field
# of the six models to being in one of these or in NOT_EXPORTED, so a field
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
PAYER_FIELDS = ("source", "created_at")
PAYER_COMPARED = ("source",)
# A treasury point is its day (unique): the date is the key, the balance all
# that is compared.
CHECKPOINT_FIELDS = ("date", "balance", "created_at")
CHECKPOINT_COMPARED = ("balance",)
CHECKPOINT_REQUIRED = ("balance",)
# An adjustment is its random reference: its day, amount and reason may have
# been corrected, it is the same adjustment.
ADJUSTMENT_FIELDS = ("reference", "date", "amount", "reason", "created_at")
ADJUSTMENT_COMPARED = ("date", "amount", "reason")
ADJUSTMENT_REQUIRED = ("date", "amount")

EXPORTED = {
    BankTransaction: ("fingerprint", *TRANSACTION_FIELDS),
    InvoicePayment: PAYMENT_FIELDS,
    CounterpartyAlias: ("name",),
    IncomePayer: ("key", *PAYER_FIELDS),
    TreasuryCheckpoint: CHECKPOINT_FIELDS,
    TreasuryAdjustment: ADJUSTMENT_FIELDS,
}
NOT_EXPORTED = {
    BankTransaction: {"id": "pk"},
    InvoicePayment: {"id": "pk", "transaction": "parent", "invoice": "by key"},
    CounterpartyAlias: {"id": "pk", "supplier": "by key"},
    IncomePayer: {"id": "pk"},
    TreasuryCheckpoint: {"id": "pk"},
    TreasuryAdjustment: {"id": "pk"},
}

# Never "rules", "operation_rules" nor "statement_formats" any more: they
# are « Règles de la banque »'s. An older archive's banque.json reaches
# load() without them (`archive.CARVED`); one that still holds them beside
# their own file - a hand-edited manifest - has them said and ignored.
TOP_LEVEL = (
    "supplier_names",
    "transactions",
    "aliases",
    "income_payers",
    "treasury_checkpoints",
    "treasury_adjustments",
)
TRANSACTION_KEYS = ("fingerprint", *TRANSACTION_FIELDS, "payments")
PAYMENT_KEYS = ("invoice", *PAYMENT_FIELDS)
ALIAS_KEYS = ("supplier", "name")
PAYER_KEYS = ("key", *PAYER_FIELDS)

# Report rows, in the order of count(): the page's table and the picker's
# counts say the same words. None is a label an older archive gave the rules
# (`archive.CARVED`): this version's « Banque » exported alone would be read
# as carrying them.
OPERATIONS = "opérations"
PAYMENTS = "paiements"
ALIASES = "noms de payeurs appris"
# Not « payeurs » alone: « noms de payeurs appris » are the suppliers'.
PAYERS = "payeurs retenus (entrées d'argent)"
# « Trésorerie »: the balances typed (« Soldes saisis ») and the adjustments.
CHECKPOINTS = "points de trésorerie"
ADJUSTMENTS = "ajustements de trésorerie"
ENTITIES = (OPERATIONS, PAYMENTS, ALIASES, PAYERS, CHECKPOINTS, ADJUSTMENTS)

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
    "source": "« en caisse »",
    "date": "date",
    "balance": "solde",
    # Never « motif », which is a regular expression on these pages.
    "reason": "raison",
}

# The page's own button, which links what bank.matching is sure of.
RECONCILE_NOTE = (
    "Pour lier automatiquement les nouvelles opérations : « Relancer le rapprochement » sur la page Banque."
)
CLEAR_NOTE = (
    "Les opérations reviennent en important de nouveau le relevé de la banque, mais sans leurs liens ni les "
    "décisions prises à la main : celles-ci ne reviennent que d'une archive."
)
# Said when a clear takes treasury points or adjustments: no statement brings
# them back, only an archive.
TREASURY_CLEAR_NOTE = (
    "Les soldes saisis et les ajustements de « Trésorerie » ne reviennent que d'une archive : ramenez-les de la "
    "sauvegarde."
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
#: « Ventes »: a credit may pay a sales invoice there, and the bank's
#: deletes take those links with it - said in its report, by its key (no
#: section module imports another, sections/base.py).
SALES = "ventes"
#: Worded as the invoices' BANK_NOTE, a participle so the preview and the
#: confirm read the same: imported together, both sections bring the links
#: back; « Ventes » alone finds no credit to link to.
SALE_LINKS_NOTE = {
    False: "1 règlement de facture de vente supprimé avec son entrée ; réimportez ensemble « Banque » et « Ventes » "
    "de la sauvegarde pour le retrouver",
    True: "{count} règlements de factures de vente supprimés avec leurs entrées ; réimportez ensemble « Banque » "
    "et « Ventes » de la sauvegarde pour les retrouver",
}


def named_fields(names, labels: dict[str, str]) -> str:
    """At most three, as §6.1 says: the conflict is a line to read, not a
    diff. Each by its section's word for it (`labels`): « Règles de la
    banque » says its own (sections/bank_rules.py)."""
    return ", ".join(labels.get(name, name) for name in list(names)[:3])


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


def _check_treasury(row: TreasuryCheckpoint | TreasuryAdjustment) -> None:
    """The model's own check, the one « Trésorerie » saves through: `clean`
    bounds the day and the figure (a statement's 2000-2099, the (12, 2)
    column) and refuses an adjustment of 0 €, each in French, said after its
    field. Only then Django's validators (a figure wider than the column, a
    day or a reference another row holds), whose English is never said: the
    field is named instead."""
    try:
        row.clean()
    except ValidationError as exc:
        errors = exc.message_dict if hasattr(exc, "error_dict") else {"": exc.messages}
        field, messages = next(iter(errors.items()))
        label = FIELD_LABELS.get(field, field)
        raise codec.FieldValueError(f"{label} : {sentence(' '.join(messages), label)}") from None
    try:
        row.full_clean()
    except ValidationError as exc:
        raise codec.FieldValueError(f"« {next(iter(exc.message_dict))} » : valeur refusée") from None


def _check_day(name: str, value: date, today: date) -> date:
    """`value` when it lies between the first day a statement may hold and
    today, else that record's reason (skipped, said). The codec reads any ISO
    date, and the model's own bound runs to 2099: a point dated tomorrow
    moved « Trésorerie au … » past today, and the gap ending on it waited for
    a statement for ever."""
    if not FIRST_DAY <= value <= today:
        raise codec.FieldValueError(
            f"« {name} » : date hors limites (« {value.isoformat()} ») : "
            f"entre le {FIRST_DAY:%d/%m/%Y} et le {today:%d/%m/%Y}"
        )
    return value


def _checkpoint_said(day: date) -> str:
    return f"Point de trésorerie du {day:%d/%m/%Y}"


def _adjustment_said(adjustment: TreasuryAdjustment | None, record: dict) -> str:
    """How the report names an adjustment - never by its reference, which
    nobody has ever seen: its day and amount, this database's when it has
    the adjustment (what « Trésorerie » shows of it), else the archive's,
    as far as they can be read."""
    if adjustment is not None:
        return f"Ajustement de trésorerie du {adjustment.date:%d/%m/%Y} ({_euros(adjustment.amount)})"
    try:
        day = codec.load(TreasuryAdjustment, "date", record.get("date"))
    except codec.FieldValueError:
        return "Ajustement de trésorerie sans date lisible"
    try:
        amount = codec.load(TreasuryAdjustment, "amount", record.get("amount"))
    except codec.FieldValueError:
        return f"Ajustement de trésorerie du {day:%d/%m/%Y}"
    return f"Ajustement de trésorerie du {day:%d/%m/%Y} ({_euros(amount)})"


def sentence(message: str, label: str) -> str:
    """A model's refusal said after its field's name: the name it may
    already open with left out, no closing full stop, a capital put down
    (« Un numéro de colonne … » → « un numéro de colonne … »). Shared with
    « Règles de la banque » (`check_format`'s refusals)."""
    text = message.strip()
    if label and text.lower().startswith(f"{label.lower()} : "):
        text = text[len(label) + 3 :]
    text = text.rstrip(".")
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    return text


def _sale_links_taken(ctx, deleted: dict[str, int]) -> None:
    """The sales invoices' links the lines just deleted took with them,
    read off the per-model counts the delete returned (never a count taken
    before it), counted into « Ventes »' report - even when « Ventes » is
    not in the run - and said there with how to get them back."""
    count = deleted.get(SaleDocumentPayment._meta.label, 0)
    if count:
        sales = ctx.report(SALES)
        sales.deleted(SALE_LINKS, count)
        sales.note(SALE_LINKS_NOTE[count > 1].format(count=count))


@registry.register
class BankSection(Section):
    key = KEY

    # -- what this database holds ------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            OPERATIONS: BankTransaction.objects.count(),
            PAYMENTS: InvoicePayment.objects.count(),
            ALIASES: CounterpartyAlias.objects.count(),
            PAYERS: IncomePayer.objects.count(),
            CHECKPOINTS: TreasuryCheckpoint.objects.count(),
            ADJUSTMENTS: TreasuryAdjustment.objects.count(),
        }

    def snapshot(self):
        payments = list(InvoicePayment.objects.order_by("id"))
        invoice_keys = keys.invoice_keys([payment.invoice_id for payment in payments])
        by_line = defaultdict(list)
        for payment in payments:
            key = invoice_keys[payment.invoice_id]
            by_line[payment.transaction_id].append(
                [[key.get(name) for name in keys.KEY_FIELDS], *codec.record(payment, PAYMENT_FIELDS).values()]
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
            "income_payers": sorted(
                [payer.key, *codec.record(payer, PAYER_FIELDS).values()] for payer in IncomePayer.objects.all()
            ),
            # Each row opens on its key - the day, the reference - which is
            # unique and never None.
            "treasury_checkpoints": [
                list(codec.record(checkpoint, CHECKPOINT_FIELDS).values())
                for checkpoint in TreasuryCheckpoint.objects.order_by("date")
            ],
            "treasury_adjustments": sorted(
                list(codec.record(adjustment, ADJUSTMENT_FIELDS).values())
                for adjustment in TreasuryAdjustment.objects.all()
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
        payers = list(IncomePayer.objects.order_by("key"))
        # By day, then by reference rather than id: the same rows give the
        # same file in every database.
        checkpoints = list(TreasuryCheckpoint.objects.order_by("date"))
        adjustments = list(TreasuryAdjustment.objects.order_by("date", "reference"))
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
            # Always said, empty included: absent, the list reads as an
            # archive written before the payers existed (« not said »).
            "income_payers": [{"key": payer.key, **codec.record(payer, PAYER_FIELDS)} for payer in payers],
            # Always said too, an empty list included: absent, they read as
            # an archive written before bank/0008, and « Remplacer » would
            # keep this database's.
            "treasury_checkpoints": [codec.record(checkpoint, CHECKPOINT_FIELDS) for checkpoint in checkpoints],
            "treasury_adjustments": [codec.record(adjustment, ADJUSTMENT_FIELDS) for adjustment in adjustments],
        }
        out.write(
            payload,
            {
                OPERATIONS: len(lines),
                PAYMENTS: len(paid),
                ALIASES: len(aliases),
                PAYERS: len(payers),
                CHECKPOINTS: len(checkpoints),
                ADJUSTMENTS: len(adjustments),
            },
        )

    # -- import ----------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        # Never "rules" any more: an older archive's banque.json comes without
        # it (`archive.CARVED`), and « Règles de la banque » reads each of its
        # lists as « not said » when absent.
        for name in ("transactions", "aliases"):
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
        # The treasury's points and adjustments (bank/0008) alike: each list
        # on its own, None when the archive does not say it.
        self._checkpoints: list | None = self._optional_list(payload, "treasury_checkpoints")
        self._adjustments: list | None = self._optional_list(payload, "treasury_adjustments")
        self.payload = payload
        # What the file names, whatever becomes of its records: prune never
        # deletes a line, a name, a point or an adjustment the archive holds,
        # even one it could not read.
        self._fingerprints: set[str] = set()
        self._alias_keys: set[tuple[int, str]] = set()
        self._payer_keys: set[str] = set()
        # Treasury points by their day, adjustments by their reference.
        self._checkpoint_days: set[date] = set()
        self._adjustment_references: set[str] = set()
        # The payers this run creates: their lines' own choices travel with
        # them (`_apply_transactions`).
        self._created_payers: set[str] = set()
        # The invoices here before the run: the runner loads every section
        # before the first apply, and by this section's turn the invoices
        # section has created its own. An invoice absent from this set came
        # with this run, so no person here can have undone a link to it.
        self._invoices_before: set[int] = set(Invoice.objects.values_list("pk", flat=True))

    @staticmethod
    def _optional_list(payload: dict, name: str) -> list | None:
        """A list added after the first archives: None when the archive does
        not say it - never an empty list, which « Remplacer » would read as
        « forget everything here » - and never one of the required lists,
        or every archive written before it would be refused."""
        items = payload.get(name)
        if items is not None and (not isinstance(items, list) or not all(isinstance(item, dict) for item in items)):
            raise ArchiveError(f"Archive refusée : dans banque.json, « {name} » n'est pas une liste d'objets.")
        return items

    def apply(self, ctx, report) -> None:
        for entity in ENTITIES:  # one row each, even when nothing moves
            report.unchanged(entity, 0)
        codec.note_unknown(report, self.payload, TOP_LEVEL, where="banque.json › ")
        replacing = ctx.replacing(self.key)
        self._apply_aliases(ctx, report)
        self._apply_payers(report, replacing)
        self._apply_transactions(ctx, report, replacing)
        # Read once: every point and adjustment of the run is held to the
        # same today.
        today = timezone.localdate()
        self._apply_checkpoints(report, replacing, today)
        self._apply_adjustments(report, replacing, today)

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
        restore_moments(created, "created_at")

    # treasury points ---------------------------------------------------------
    def _apply_checkpoints(self, report, replacing: bool, today: date) -> None:
        """A balance a person typed for one day (« Trésorerie »): a decision,
        merged like a payer - one said otherwise here is a conflict, kept,
        both balances said. Keyed by its day, which is unique. An archive
        saying nothing of them (written before bank/0008) leaves them alone.
        No gap is worked out here: the page reads what this writes."""
        if self._checkpoints is None:
            return
        existing = {checkpoint.date: checkpoint for checkpoint in TreasuryCheckpoint.objects.all()}
        created = []
        for record in self._checkpoints:
            codec.note_unknown(report, record, CHECKPOINT_FIELDS, where="points de trésorerie › ")
            try:
                day = codec.load(TreasuryCheckpoint, "date", record.get("date"))
            except codec.FieldValueError as exc:
                report.skip(f"Point de trésorerie sans date lisible : {exc}")
                continue
            said = _checkpoint_said(day)
            if day in self._checkpoint_days:
                report.skip(f"{said} : en double dans l'archive")
                continue
            # Named before the rest of its record is read: the prune never
            # deletes a point the archive holds, even one it could not read.
            self._checkpoint_days.add(day)
            checkpoint = existing.get(day)
            try:
                _check_day("date", day, today)
                if checkpoint is None:
                    for name in CHECKPOINT_REQUIRED:
                        codec.load(TreasuryCheckpoint, name, record.get(name))
                    checkpoint = TreasuryCheckpoint(date=day)
                    codec.assign(checkpoint, record, CHECKPOINT_FIELDS)
                    _check_treasury(checkpoint)
                    moment = checkpoint.created_at
                    checkpoint.save()
                    created.append((checkpoint, moment))
                    report.created(CHECKPOINTS)
                    continue
                different = codec.differences(checkpoint, record, CHECKPOINT_COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"{said} : {exc}")
                continue
            if not different:
                report.unchanged(CHECKPOINTS)
            elif replacing:
                # Every field, the moment included, then the model's check.
                # What cannot be read leaves the point as it was.
                try:
                    changed = codec.assign(checkpoint, record, CHECKPOINT_FIELDS)
                    _check_treasury(checkpoint)
                except codec.FieldValueError as exc:
                    report.skip(f"{said} : {exc}")
                    continue
                checkpoint.save(update_fields=changed)
                report.updated(CHECKPOINTS)
            else:
                # The balance is all that is compared, and `differences` read it.
                there = codec.load(TreasuryCheckpoint, "balance", record["balance"])
                report.conflict(
                    f"{said} : {_euros(checkpoint.balance)} ici, {_euros(there)} dans l'archive — gardé tel quel"
                )
        restore_moments(created, "created_at")

    # treasury adjustments ----------------------------------------------------
    def _apply_adjustments(self, report, replacing: bool, today: date) -> None:
        """An amount a person added to settle two points (« Trésorerie »):
        merged like a point - one changed here is a conflict, kept. Keyed by
        its random `reference`, which is never made here: a record without a
        readable one is skipped, since a reference drawn by the preview and
        another by the confirm would make the two differ. An archive saying
        nothing of them leaves them alone. Whether it counts - between two
        points or nowhere - is the page's to say, not the import's."""
        if self._adjustments is None:
            return
        existing = {adjustment.reference: adjustment for adjustment in TreasuryAdjustment.objects.all()}
        created = []
        for record in self._adjustments:
            codec.note_unknown(report, record, ADJUSTMENT_FIELDS, where="ajustements de trésorerie › ")
            reference = record.get("reference")
            try:
                if not isinstance(reference, str) or not reference.strip():
                    raise codec.FieldValueError("référence manquante")
                codec.load(TreasuryAdjustment, "reference", reference)
            except codec.FieldValueError as exc:
                report.skip(f"Ajustement de trésorerie sans référence lisible : {exc}")
                continue
            adjustment = existing.get(reference)
            said = _adjustment_said(adjustment, record)
            if reference in self._adjustment_references:
                report.skip(f"{said} : en double dans l'archive")
                continue
            # Named before the rest of its record is read: the prune never
            # deletes an adjustment the archive holds, even one it could not
            # read.
            self._adjustment_references.add(reference)
            try:
                if adjustment is None:
                    for name in ADJUSTMENT_REQUIRED:
                        codec.load(TreasuryAdjustment, name, record.get(name))
                    # The reference given, so its default never draws one.
                    adjustment = TreasuryAdjustment(reference=reference)
                    codec.assign(adjustment, record, ADJUSTMENT_FIELDS)
                    _check_day("date", adjustment.date, today)
                    _check_treasury(adjustment)
                    moment = adjustment.created_at
                    adjustment.save()
                    created.append((adjustment, moment))
                    report.created(ADJUSTMENTS)
                    continue
                different = codec.differences(adjustment, record, ADJUSTMENT_COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"{said} : {exc}")
                continue
            if not different:
                report.unchanged(ADJUSTMENTS)
            elif replacing:
                # Every field, the moment included, then the day's bound and
                # the model's check. What cannot be read leaves it as it was.
                try:
                    changed = codec.assign(adjustment, record, ADJUSTMENT_FIELDS)
                    _check_day("date", adjustment.date, today)
                    _check_treasury(adjustment)
                except codec.FieldValueError as exc:
                    report.skip(f"{said} : {exc}")
                    continue
                adjustment.save(update_fields=changed)
                report.updated(ADJUSTMENTS)
            else:
                report.conflict(
                    f"{said} : différent dans l'archive ({named_fields(different, FIELD_LABELS)}) — gardé tel quel"
                )
        restore_moments(created, "created_at")

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
                    f"{_operation(line)} : différente dans l'archive ({named_fields(different, FIELD_LABELS)}) — "
                    "gardée telle quelle"
                    + (", son choix « en caisse » repris avec son payeur" if took_choice else "")
                    + self._links_left_out(ctx, line, record)
                )
                continue
            decided.append((line, record, False))

        if new:
            moments = [(line, line.imported_at) for line in new]
            BankTransaction.objects.bulk_create(new)
            restore_moments(moments, "imported_at")
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
            report.deleted(PAYMENTS, delete_ids(InvoicePayment, doomed))
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
        restore_moments(created, "created_at")
        report.created(PAYMENTS, len(created))

    def prune(self, ctx, report) -> None:
        # What the archive does not hold goes. The lines' links to invoices
        # went in apply (above), before the archive's were made. « Ventes »'
        # règlements point at a bank line (recipes.SaleDocumentPayment,
        # CASCADE): counted into « Ventes »' report. Nothing else in any
        # other section points at a bank line or a payee name.
        lines = [
            pk
            for pk, fingerprint in BankTransaction.objects.values_list("pk", "fingerprint")
            if fingerprint not in self._fingerprints
        ]
        if lines:
            cascaded: dict[str, int] = {}
            report.deleted(OPERATIONS, delete_ids(BankTransaction, lines, cascaded))
            _sale_links_taken(ctx, cascaded)
        aliases = [
            pk
            for pk, supplier_id, name in CounterpartyAlias.objects.values_list("pk", "supplier_id", "name")
            if (supplier_id, name) not in self._alias_keys
        ]
        if aliases:
            report.deleted(ALIASES, delete_ids(CounterpartyAlias, aliases))
        # An archive saying nothing of payers prunes none of them.
        if self._payers is not None:
            payers = [pk for pk, key in IncomePayer.objects.values_list("pk", "key") if key not in self._payer_keys]
            if payers:
                report.deleted(PAYERS, delete_ids(IncomePayer, payers))
        # Nor of the treasury's points and adjustments.
        if self._checkpoints is not None:
            doomed = [
                pk
                for pk, day in TreasuryCheckpoint.objects.values_list("pk", "date")
                if day not in self._checkpoint_days
            ]
            if doomed:
                report.deleted(CHECKPOINTS, delete_ids(TreasuryCheckpoint, doomed))
        if self._adjustments is not None:
            doomed = [
                pk
                for pk, reference in TreasuryAdjustment.objects.values_list("pk", "reference")
                if reference not in self._adjustment_references
            ]
            if doomed:
                report.deleted(ADJUSTMENTS, delete_ids(TreasuryAdjustment, doomed))

    # -- clear -----------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        # The formats and the rules are « Règles de la banque »'s: they stay.
        # The lines take « Ventes »' règlements with them (CASCADE).
        payments = InvoicePayment.objects.all().delete()[1]
        operations = BankTransaction.objects.all().delete()[1]
        _sale_links_taken(ctx, operations)
        counts = {
            PAYMENTS: payments.get(InvoicePayment._meta.label, 0),
            OPERATIONS: operations.get(BankTransaction._meta.label, 0),
            ALIASES: CounterpartyAlias.objects.all().delete()[1].get(CounterpartyAlias._meta.label, 0),
            PAYERS: IncomePayer.objects.all().delete()[1].get(IncomePayer._meta.label, 0),
            ADJUSTMENTS: TreasuryAdjustment.objects.all().delete()[1].get(TreasuryAdjustment._meta.label, 0),
            CHECKPOINTS: TreasuryCheckpoint.objects.all().delete()[1].get(TreasuryCheckpoint._meta.label, 0),
        }
        for entity in ENTITIES:
            report.deleted(entity, counts[entity])
        if counts[OPERATIONS]:
            report.note(CLEAR_NOTE)
        if counts[CHECKPOINTS] or counts[ADJUSTMENTS]:
            report.note(TREASURY_CLEAR_NOTE)
