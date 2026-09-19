"""« Sources de factures » (§7.2): each invoice type with its one row of
settings - the mailbox search (`EmailInvoiceSource`) or the customer
portal (`WebsiteInvoiceSource`) - nested in it.

A portal's settings name the `.env` variables holding its login, never
the login itself: those names are safe in an archive, and the values have
to be copied by hand to the other computer, which the report says.

**A portal from an archive is never trusted.** The next gather reads the
variables it names and types them into the page it names, so an archive
from anywhere could have Metro's password typed into a site of its
choosing. Hence a portal naming a variable the application reads for
itself (`app_env_name`) is refused, and an import never switches a portal
on: one it creates, or whose address or variables it changes, stays
inactive - said, with the address and the variables - until the owner has
looked and ticked « Active ». Merged or replaced, a portal the archive has
active and this database has not is said the same way (`LEFT_OFF`), never
as a conflict: « Remplacer » would not switch it on either.

A source is keyed by (its supplier's code, its folded name). The seeded
« UBA - Factures » is no different from any other: the owner may have
deleted it, and the archive says what they want.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError

# The variables the application reads for itself are one list, beside the
# model whose clean() refuses them - the source form and this import alike.
# Re-exported: the tests reach it through this module.
from invoices.models import app_env_name  # noqa: F401
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.keys import fold
from transfer.sections.base import Section
from transfer.sections.suppliers import said

KEY = "sources"

TYPE_FIELDS = ("name", "parser_key", "source_kind", "is_active", "created_at")
TYPE_NOT_EXPORTED = {"id": "pk", "supplier": "by key"}
#: Never compared: a source made a minute later is the same source (§6.4).
TYPE_COMPARED = ("name", "parser_key", "source_kind", "is_active")
EMAIL_FIELDS = ("sender_pattern", "subject_pattern", "body_pattern", "attachment_pattern")
WEBSITE_FIELDS = (
    "login_url",
    "username_env",
    "password_env",
    "invoices_url",
    "navigation",
    "username_selector",
    "password_selector",
    "submit_selector",
    "link_selector",
    "next_selector",
    "show_browser",
)
ROW_NOT_EXPORTED = {"id": "pk", "invoice_type": "parent"}
KNOWN = ("supplier", *TYPE_FIELDS, "email", "website")
#: Where a portal signs in and what it types there: an archive changing
#: one of them hands this computer's secrets to a page nobody here chose.
SIGN_IN_FIELDS = ("login_url", "username_env", "password_env")
#: A portal the archive has active and this database inactive, merged or
#: replaced: why it stays off is said, with what to do (_inactive_note).
LEFT_OFF = "laissée inactive (un import n'active jamais un portail)"

EMAIL, WEBSITE = "EMAIL", "WEBSITE"

LABELS = {
    "supplier": "fournisseur",
    "name": "nom",
    "parser_key": "lecteur",
    "source_kind": "type de source",
    "is_active": "active",
    "sender_pattern": "expéditeur",
    "subject_pattern": "objet",
    "body_pattern": "contenu",
    "attachment_pattern": "pièce jointe",
    "login_url": "page de connexion",
    "username_env": "variable de l'identifiant",
    "password_env": "variable du mot de passe",
    "invoices_url": "page des factures",
    "navigation": "navigation",
    "username_selector": "sélecteur de l'identifiant",
    "password_selector": "sélecteur du mot de passe",
    "submit_selector": "sélecteur du bouton",
    "link_selector": "sélecteur des liens",
    "next_selector": "sélecteur de la page suivante",
    "show_browser": "fenêtre du navigateur",
}


def _models():
    from invoices.models import EmailInvoiceSource, InvoiceType, WebsiteInvoiceSource

    return InvoiceType, EmailInvoiceSource, WebsiteInvoiceSource


def _row_model(kind: str):
    _type, email, website = _models()
    return website if kind == WEBSITE else email


def _row_fields(kind: str) -> tuple[str, ...]:
    return WEBSITE_FIELDS if kind == WEBSITE else EMAIL_FIELDS


def _row(invoice_type):
    """Its settings row, of the kind it says, or None."""
    from django.core.exceptions import ObjectDoesNotExist

    attribute = "website_source" if invoice_type.source_kind == WEBSITE else "email_source"
    try:
        return getattr(invoice_type, attribute)
    except ObjectDoesNotExist:
        return None


def _other_row(invoice_type):
    from django.core.exceptions import ObjectDoesNotExist

    attribute = "email_source" if invoice_type.source_kind == WEBSITE else "website_source"
    try:
        return getattr(invoice_type, attribute)
    except ObjectDoesNotExist:
        return None


def _messages(error: ValidationError) -> str:
    if hasattr(error, "message_dict"):
        return " ; ".join(
            f"{LABELS.get(name, name)} : {' '.join(messages)}" if name != "__all__" else " ".join(messages)
            for name, messages in error.message_dict.items()
        )
    return " ".join(error.messages)


@registry.register
class SourcesSection(Section):
    key = KEY

    # -- what this database holds ----------------------------------------------------
    def count(self) -> dict[str, int]:
        InvoiceType, _email, _website = _models()
        return {"sources": InvoiceType.objects.count()}

    def snapshot(self):
        InvoiceType, _email, _website = _models()
        result = []
        for invoice_type in InvoiceType.objects.select_related("supplier", "email_source", "website_source"):
            record = self._record(invoice_type)
            # Not what an import restores on a source already here: the
            # seeded « UBA - Factures » of a fresh database is equal to the
            # archive's in everything compared, so it is not written (§6.4),
            # and keeps the day the migration ran. Restored on the sources an
            # import creates (tested on its own).
            del record["created_at"]
            result.append(record)
        return sorted(result, key=lambda record: (record["supplier"], fold(record["name"]), record["name"]))

    @staticmethod
    def _record(invoice_type) -> dict:
        record = {"supplier": invoice_type.supplier.code, **codec.record(invoice_type, TYPE_FIELDS)}
        for kind, name in ((EMAIL, "email"), (WEBSITE, "website")):
            row = _row(invoice_type) if invoice_type.source_kind == kind else _other_row(invoice_type)
            record[name] = codec.record(row, _row_fields(kind)) if row is not None else None
        return record

    # -- export ------------------------------------------------------------------------
    def export(self, out) -> None:
        InvoiceType, _email, _website = _models()
        types = list(
            InvoiceType.objects.select_related("supplier", "email_source", "website_source").order_by(
                "supplier__code", "name", "id"
            )
        )
        out.write(
            {
                "supplier_names": {invoice_type.supplier.code: invoice_type.supplier.name for invoice_type in types},
                "sources": [self._record(invoice_type) for invoice_type in types],
            },
            {"sources": len(types)},
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        records = src.payload().get("sources")
        if not isinstance(records, list):
            raise ArchiveError("Archive refusée : sources.json n'a pas de liste « sources ».")
        self.records = records
        self.claimed: set[int] = set()

    def apply(self, ctx, report) -> None:
        InvoiceType, _email, _website = _models()
        here = {}
        for invoice_type in InvoiceType.objects.select_related("email_source", "website_source"):
            here.setdefault((invoice_type.supplier_id, fold(invoice_type.name)), invoice_type)
        replacing = ctx.replacing(self.key)
        seen = set()
        portals: set[str] = set()
        for record in self.records:
            if not isinstance(record, dict):
                report.skip("Une source de l'archive est illisible.")
                continue
            codec.note_unknown(report, record, KNOWN)
            name = record.get("name")
            if not isinstance(name, str) or not fold(name):
                report.skip("Une source de l'archive n'a pas de nom.")
                continue
            supplier = ctx.suppliers.resolve(record.get("supplier"))
            if supplier is None:
                report.skip(f"Source « {name} » : fournisseur inconnu « {record.get('supplier')} »")
                continue
            key = (supplier.pk, fold(name))
            if key in seen:
                report.skip(f"Source « {name} » ({supplier.name}) : en double dans l'archive")
                continue
            seen.add(key)
            existing = here.get(key)
            if existing is not None:
                self.claimed.add(existing.pk)
            try:
                kind, row_data = self._check(record, supplier, existing)
            except (codec.FieldValueError, ValidationError) as exc:
                reason = _messages(exc) if isinstance(exc, ValidationError) else str(exc)
                report.skip(f"Source « {name} » ({supplier.name}) : {reason}")
                continue
            if existing is None:
                record, how = self._trusted(record, kind, None, row_data)
                created = self._create(record, supplier, kind, row_data)
                self.claimed.add(created.pk)
                report.created("sources")
                if kind == WEBSITE:
                    portals.update(self._env_names(row_data))
                if how:
                    report.note(self._inactive_note(name, supplier, how, self._sign_in(None, kind, row_data)))
                continue
            different = self._differences(existing, record, supplier, kind, row_data)
            if different and replacing:
                sign_in = self._sign_in(existing, kind, row_data)
                record, how = self._trusted(record, kind, existing, row_data)
                # Again: left inactive against the archive's word, a portal
                # may differ from it in nothing else.
                if self._differences(existing, record, supplier, kind, row_data):
                    self._replace(existing, record, supplier, kind, row_data)
                    report.updated("sources")
                    if kind == WEBSITE:
                        portals.update(self._env_names(row_data))
                else:
                    report.unchanged("sources")
                if how:
                    report.note(self._inactive_note(name, supplier, how, sign_in))
                continue
            if self._left_off(existing, kind, different):
                # Not a conflict, which reads as if « Remplacer » would take
                # the archive's word: no import switches a portal on. After a
                # restore, every portal was one when merged again (19/09).
                different = [field for field in different if field != "is_active"]
                report.note(self._inactive_note(existing.name, supplier, LEFT_OFF, self._sign_in(existing, kind, {})))
            if not different:
                report.unchanged("sources")
            else:
                report.conflict(
                    f"Source « {existing.name} » ({supplier.name}) : différente dans l'archive "
                    f"({said(different, LABELS)}) — gardée telle quelle"
                )
        if portals:
            # Restoring one's own backup on the same computer, the .env was
            # never touched: the advice is for another computer only.
            report.note(
                f"Les identifiants des portails ({', '.join(sorted(portals))}) sont lus dans le fichier .env : "
                "s'il s'agit d'un autre ordinateur, recopiez-les à la main."
            )

    @staticmethod
    def _left_off(existing, kind: str, different: list[str]) -> bool:
        """A portal here and in the archive, active there and not here: the
        one difference no import acts on, whatever the strategy (`_trusted`)."""
        return (
            kind == WEBSITE
            and existing.source_kind == WEBSITE
            and "is_active" in different
            and not existing.is_active
        )

    @staticmethod
    def _env_names(row_data: dict) -> set[str]:
        return {row_data[name] for name in ("username_env", "password_env") if row_data.get(name)}

    @staticmethod
    def _sign_in(existing, kind, row_data: dict) -> dict:
        """Where the portal signs in and with which variables, once written:
        what the archive says, this database's value for what it does not."""
        current = _row(existing) if existing is not None and existing.source_kind == kind else None
        return {name: row_data.get(name, getattr(current, name, "")) for name in SIGN_IN_FIELDS}

    @staticmethod
    def _signs_in_elsewhere(existing, kind, row_data: dict) -> bool:
        """Whether the archive changes where this source signs in or what it
        types there - a mailbox search turned into a portal included."""
        if existing.source_kind != kind:
            return True
        current = _row(existing)
        return current is None or bool(codec.differences(current, row_data, SIGN_IN_FIELDS))

    def _trusted(self, record: dict, kind: str, existing, row_data: dict) -> tuple[dict, str]:
        """The record as an import may write it, and how a portal it leaves
        inactive against the archive is said ("" when it is not). An import
        never switches a portal on: one it creates stays inactive, one whose
        address or variables it changes is switched off, one inactive here
        stays so - replaced twice, a forged archive would otherwise have
        switched on the portal its first import had left off. Only a portal
        already on here, still signing in where it did with what it did,
        takes the archive's word."""
        if kind != WEBSITE:
            return record, ""
        wanted = record.get("is_active", True if existing is None else existing.is_active)
        if existing is None:
            how = "créée inactive"
        elif self._signs_in_elsewhere(existing, kind, row_data):
            how = "désactivée" if existing.is_active else LEFT_OFF
        elif not existing.is_active:
            how = LEFT_OFF
        else:
            return record, ""
        return {**record, "is_active": False}, (how if wanted else "")

    @staticmethod
    def _inactive_note(name: str, supplier, how: str, sign_in: dict) -> str:
        return (
            f"Source « {name} » ({supplier.name}) : {how} — elle se connecte à {sign_in['login_url']} avec "
            f"{sign_in['username_env']} et {sign_in['password_env']} du fichier .env ; vérifiez l'adresse et les "
            "variables, puis cochez « Active » sur sa page (Achats → Sources)."
        )

    def _check(self, record, supplier, existing) -> tuple[str, dict]:
        """The source as the archive has it, validated as the source form
        validates it (the regexes, the portal's addresses and .env names),
        without writing anything. Returns its kind and its settings row."""
        InvoiceType, _email, _website = _models()
        values = {name: codec.load(InvoiceType, name, record[name]) for name in TYPE_FIELDS if name in record}
        kind = values.get("source_kind") or (existing.source_kind if existing is not None else EMAIL)
        row_name = "website" if kind == WEBSITE else "email"
        row_data = record.get(row_name)
        if not isinstance(row_data, dict):
            raise codec.FieldValueError(f"ses réglages ({'portail' if kind == WEBSITE else 'boîte mail'}) manquent")
        model = _row_model(kind)
        fields = _row_fields(kind)
        row_values = {name: codec.load(model, name, row_data[name]) for name in fields if name in row_data}
        unknown = set(row_data) - set(fields)
        if unknown:
            raise codec.FieldValueError(f"réglage inconnu : {', '.join(sorted(unknown))}")

        probe = InvoiceType(supplier=supplier, **{name: value for name, value in values.items() if name != "created_at"})
        probe.full_clean()
        row = model(**row_values)
        if existing is not None and existing.source_kind == kind:
            # What the archive does not say keeps this database's value.
            current = _row(existing)
            if current is not None:
                row = model(**{**{name: getattr(current, name) for name in fields}, **row_values})
        # A portal's clean() refuses a variable the application reads for
        # itself (models.app_env_name): the next gather reads it and types it
        # into the archive's page, so Metro's, the mailbox's and the till's
        # secrets never go there.
        row.full_clean(exclude=["invoice_type"])
        return kind, row_data

    def _differences(self, existing, record, supplier, kind, row_data) -> list[str]:
        different = []
        if existing.supplier_id != supplier.pk:
            different.append("supplier")
        different += codec.differences(existing, record, TYPE_COMPARED)
        if existing.source_kind == kind:
            current = _row(existing)
            if current is None:
                different.append("source_kind")
            else:
                different += codec.differences(current, row_data, _row_fields(kind))
        elif "source_kind" not in different:
            different.append("source_kind")
        return different

    def _create(self, record, supplier, kind, row_data):
        InvoiceType, _email, _website = _models()
        invoice_type = InvoiceType(supplier=supplier, source_kind=kind)
        codec.assign(invoice_type, record, [name for name in TYPE_FIELDS if name != "created_at"])
        invoice_type.source_kind = kind
        invoice_type.save()
        if record.get("created_at") is not None:
            # auto_now_add wrote "now" over it; update() does not call pre_save.
            InvoiceType.objects.filter(pk=invoice_type.pk).update(
                created_at=codec.load(InvoiceType, "created_at", record["created_at"])
            )
        row = _row_model(kind)(invoice_type=invoice_type)
        codec.assign(row, row_data, _row_fields(kind))
        row.save()
        return invoice_type

    def _replace(self, existing, record, supplier, kind, row_data) -> None:
        fields = [name for name in TYPE_FIELDS if name in record]
        codec.assign(existing, record, fields)
        existing.supplier = supplier
        existing.source_kind = kind
        existing.save()
        stale = _other_row(existing)
        if stale is not None:
            # A source is one kind or the other: the row of the kind it was
            # goes, or the gather would still find it.
            stale.delete()
        row = _row(existing)
        if row is None:
            row = _row_model(kind)(invoice_type=existing)
        codec.assign(row, row_data, _row_fields(kind))
        row.save()

    def prune(self, ctx, report) -> None:
        InvoiceType, _email, _website = _models()
        gone = InvoiceType.objects.exclude(pk__in=self.claimed)
        names = sorted(gone.values_list("name", flat=True))
        if names:
            # Their settings rows cascade; the history's undo of a moved type
            # already tolerates a type that no longer exists.
            gone.delete()
            report.deleted("sources", len(names))

    # -- clear --------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        InvoiceType, _email, _website = _models()
        count = InvoiceType.objects.count()
        if count:
            InvoiceType.objects.all().delete()
            report.deleted("sources", count)
