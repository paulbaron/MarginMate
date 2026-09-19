"""« Banque » (§7.7): the bank's lines, which invoice each one paid, the
rules for the payments that never have one, and the payee names learnt for
suppliers.

What is worth keeping here is not the lines - the next statement import
brings them back - but the decisions a person took on them: a link made by
hand, a line unlinked, a line declared « pas de facture »
(`settled_by_hand`, `no_invoice`). Nothing can rebuild those, so:

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

from bank.models import BankTransaction, CounterpartyAlias, IgnoreRule, InvoicePayment
from bank.reconcile import invoice_label
from invoices.models import Invoice, Supplier
from transfer import codec, keys, registry
from transfer.archive import ArchiveError
from transfer.sections.base import Section

KEY = "banque"

# The JSON of §7.7, field by field. The guard test holds every concrete field
# of the four models to being in one of these or in NOT_EXPORTED, so a field
# added to a model later cannot be left out in silence.
TRANSACTION_FIELDS = (
    "account", "operation_date", "value_date", "card_date", "bank_type", "kind", "label",
    "counterparty", "amount", "no_invoice", "settled_by_hand", "imported_at",
)
# Never the import date (§6.4): merging an archive of the same statement taken
# a minute later is « inchangé », not a conflict.
TRANSACTION_COMPARED = tuple(name for name in TRANSACTION_FIELDS if name != "imported_at")
# The model has no default for these: a line the archive creates without one
# would fail on insert, so it is skipped with the reason instead.
TRANSACTION_REQUIRED = ("operation_date", "label", "amount")
PAYMENT_FIELDS = ("method", "created_at")
RULE_FIELDS = ("description", "is_active", "created_at")
RULE_COMPARED = ("description", "is_active")

EXPORTED = {
    BankTransaction: ("fingerprint", *TRANSACTION_FIELDS),
    InvoicePayment: PAYMENT_FIELDS,
    CounterpartyAlias: ("name",),
    IgnoreRule: ("pattern", *RULE_FIELDS),
}
NOT_EXPORTED = {
    BankTransaction: {"id": "pk"},
    InvoicePayment: {"id": "pk", "transaction": "parent", "invoice": "by key"},
    CounterpartyAlias: {"id": "pk", "supplier": "by key"},
    IgnoreRule: {"id": "pk"},
}

TOP_LEVEL = ("supplier_names", "transactions", "aliases", "rules")
TRANSACTION_KEYS = ("fingerprint", *TRANSACTION_FIELDS, "payments")
PAYMENT_KEYS = ("invoice", *PAYMENT_FIELDS)
ALIAS_KEYS = ("supplier", "name")
RULE_KEYS = ("pattern", *RULE_FIELDS)

# Report rows, in the order of count(): the page's table and the picker's
# counts say the same words.
OPERATIONS = "opérations"
PAYMENTS = "paiements"
RULES = "règles"
ALIASES = "noms de payeurs appris"
ENTITIES = (OPERATIONS, PAYMENTS, RULES, ALIASES)

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
    "description": "nom",
    "is_active": "active",
}

# The page's own button, which links what bank.matching is sure of.
RECONCILE_NOTE = "Pour lier automatiquement les nouvelles opérations : « Relancer le rapprochement » sur la page Banque."
CLEAR_NOTE = (
    "Les opérations reviennent en important de nouveau le relevé de la banque, mais sans leurs liens ni les "
    "décisions prises à la main : celles-ci ne reviennent que d'une archive."
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


def _fields(names) -> str:
    """At most three, as §6.1 says: the conflict is a line to read, not a diff."""
    return ", ".join(FIELD_LABELS.get(name, name) for name in list(names)[:3])


def _euros(amount) -> str:
    return f"{amount:.2f}".replace(".", ",") + " €"


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


@registry.register
class BankSection(Section):
    key = KEY

    # -- what this database holds ------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            OPERATIONS: BankTransaction.objects.count(),
            PAYMENTS: InvoicePayment.objects.count(),
            RULES: IgnoreRule.objects.count(),
            ALIASES: CounterpartyAlias.objects.count(),
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
                {"fingerprint": line.fingerprint, **codec.record(line, TRANSACTION_FIELDS), "payments": sorted(by_line[line.pk])}
                for line in BankTransaction.objects.order_by("fingerprint")
            ],
            "aliases": sorted([code, name] for code, name in CounterpartyAlias.objects.values_list("supplier__code", "name")),
            "rules": sorted([rule.pattern, *codec.record(rule, RULE_FIELDS).values()] for rule in IgnoreRule.objects.all()),
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
        codes = {key["supplier"] for key in invoice_keys.values()} | {alias.supplier.code for alias in aliases}
        payload = {
            # A code may differ in the database this is imported into (LIDL
            # there, LIDL_2 here): its name lets the invoice keys and the
            # aliases still find their supplier (§5.4).
            "supplier_names": dict(Supplier.objects.filter(code__in=codes).order_by("code").values_list("code", "name")),
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
        }
        out.write(payload, {OPERATIONS: len(lines), PAYMENTS: len(paid), RULES: len(rules), ALIASES: len(aliases)})

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
                raise ArchiveError("Archive refusée : dans banque.json, les liens d'une opération ne sont pas une liste.")
        self.payload = payload
        # What the file names, whatever becomes of its records: prune never
        # deletes a line, rule or name the archive holds, even one it could
        # not read.
        self._fingerprints: set[str] = set()
        self._patterns: set[str] = set()
        self._rule_ids: dict[str, int] = {}
        self._alias_keys: set[tuple[int, str]] = set()
        # The invoices here before the run: the runner loads every section
        # before the first apply, and by this section's turn the factures
        # section has created its own. An invoice absent from this set came
        # with this run, so no person here can have undone a link to it.
        self._invoices_before: set[int] = set(Invoice.objects.values_list("pk", flat=True))

    def apply(self, ctx, report) -> None:
        for entity in ENTITIES:  # one row each, even when nothing moves
            report.unchanged(entity, 0)
        codec.note_unknown(report, self.payload, TOP_LEVEL, where="banque.json › ")
        replacing = ctx.replacing(self.key)
        self._apply_rules(report, replacing)
        self._apply_aliases(ctx, report)
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
                changed = codec.assign(rule, record, RULE_FIELDS)
                rule.save(update_fields=changed)
                report.updated(RULES)
            else:
                report.conflict(
                    f"Règle « {pattern} » : différente dans l'archive ({_fields(different)}) — gardée telle quelle"
                )
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
            if not different:
                report.unchanged(OPERATIONS)
            elif replacing:
                changed = codec.assign(line, record, TRANSACTION_FIELDS)
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
        # lose theirs here, before any is created - a link moving to another
        # line would otherwise meet its old self on the invoice's one-to-one.
        managed = {line.pk for line, _record, _new in decided}
        if replacing:
            managed |= {line.pk for fingerprint, line in existing.items() if fingerprint not in self._fingerprints}
        listed = [(line, is_new, self._payments(ctx, report, line, record)) for line, record, is_new in decided]
        if replacing:
            self._replace_payments(report, listed, managed)
        else:
            self._merge_payments(report, listed)

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
                moment = codec.load(InvoicePayment, "created_at", payment["created_at"]) if "created_at" in payment else None
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
        """One by one (§6.1): a missing link is added when its invoice is
        unpaid here and the line does not say otherwise here. A line paying
        here an invoice the archive does not give it, or marked « pas de
        facture » here, is a decision this database holds: a conflict. So is
        a line settled by hand here without a link of the archive's, unless
        its invoice came with this run: `reconcile.unlink` leaves the line
        exactly as an invoice deleted with its payment does, and only an
        invoice absent when the run started cannot have been unlinked here."""
        current = list(InvoicePayment.objects.select_related("transaction", "invoice__supplier"))
        by_invoice = {payment.invoice_id: payment for payment in current}
        by_line = defaultdict(list)
        for payment in current:
            by_line[payment.transaction_id].append(payment)
        created = []
        undone = False
        for line, is_new, payments in listed:
            wanted = {invoice.pk for invoice, _method, _moment in payments}
            others = [payment for payment in by_line.get(line.pk, []) if payment.invoice_id not in wanted]
            for invoice, method, moment in payments:
                held = by_invoice.get(invoice.pk)
                if held is not None and held.transaction_id == line.pk:
                    if held.method == method:
                        report.unchanged(PAYMENTS)
                    else:
                        report.conflict(
                            f"{_operation(line)} : le lien vers {invoice_label(invoice)} est "
                            f"« {held.get_method_display()} » ici, « {InvoicePayment.Method(method).label} » dans "
                            f"l'archive — gardé tel quel"
                        )
                    continue
                if held is not None:
                    report.conflict(
                        f"{_operation(line)} : la facture {invoice_label(invoice)} est déjà payée par l'opération du "
                        f"{held.transaction.operation_date:%d/%m/%Y} — lien ignoré"
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
                by_invoice[invoice.pk] = payment
                created.append((payment, moment))
        if undone:
            report.note(UNDONE_NOTE)
        self._create_payments(report, created)

    def _replace_payments(self, report, listed, managed: set[int]) -> None:
        """The managed lines' links become exactly the archive's. Counted by
        key (line, invoice): what stays is « inchangé », a method that
        differs is « modifié », and nothing equal is written."""
        current = list(InvoicePayment.objects.select_related("transaction", "invoice__supplier"))
        # Links of lines this run leaves as they are: an archive link to
        # their invoice cannot be made.
        fixed = {payment.invoice_id: payment for payment in current if payment.transaction_id not in managed}
        existing = {(payment.transaction_id, payment.invoice_id): payment for payment in current}
        claimed: dict[int, BankTransaction] = {}
        wanted: dict[tuple[int, int], tuple] = {}
        for line, _is_new, payments in listed:
            for invoice, method, moment in payments:
                holder = fixed[invoice.pk].transaction if invoice.pk in fixed else claimed.get(invoice.pk)
                if holder is not None:
                    report.conflict(
                        f"{_operation(line)} : la facture {invoice_label(invoice)} est déjà payée par l'opération du "
                        f"{holder.operation_date:%d/%m/%Y} — lien ignoré"
                    )
                    continue
                claimed[invoice.pk] = line
                wanted[(line.pk, invoice.pk)] = (line, invoice, method, moment)

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
        lines = [pk for pk, fingerprint in BankTransaction.objects.values_list("pk", "fingerprint") if fingerprint not in self._fingerprints]
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
        aliases = [
            pk
            for pk, supplier_id, name in CounterpartyAlias.objects.values_list("pk", "supplier_id", "name")
            if (supplier_id, name) not in self._alias_keys
        ]
        if aliases:
            report.deleted(ALIASES, _delete_ids(CounterpartyAlias, aliases))

    # -- clear -----------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        counts = {
            PAYMENTS: InvoicePayment.objects.all().delete()[1].get(InvoicePayment._meta.label, 0),
            OPERATIONS: BankTransaction.objects.all().delete()[1].get(BankTransaction._meta.label, 0),
            RULES: IgnoreRule.objects.all().delete()[1].get(IgnoreRule._meta.label, 0),
            ALIASES: CounterpartyAlias.objects.all().delete()[1].get(CounterpartyAlias._meta.label, 0),
        }
        for entity in ENTITIES:
            report.deleted(entity, counts[entity])
        if counts[OPERATIONS]:
            report.note(CLEAR_NOTE)
