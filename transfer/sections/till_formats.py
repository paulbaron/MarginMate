"""« Formats des fichiers de caisse » (`formats_caisse`): how a bar's own
till export is read (`recipes.models.TillFormat`, recipes/pos/till_file.py).

Configuration, in the « Configuration » group: a format names no other row,
so the section requires nothing and nothing requires it - « Ventes » never
did (a format reads files; it stores none of what it read). Another bar on
the same till takes it alone, from « Configuration seule », and tests it on
its own export before anything is stored.

Merged like the bank's statement formats (sections/bank_rules.py): keyed by
the name as the format's form compares it (`recipes.forms.till_format_key`:
accents, case and spaces aside), every field but the moment compared - the
name as spelt included (spelt otherwise, it was renamed). Different here: a
conflict kept under « Fusionner », replaced under « Remplacer », whose prune
deletes the formats the archive does not name. Every format written,
created or replaced, goes through the model's own check
(`till_file.check_format`: the columns, the choices, the method map) - an
archive may say anything - then Django's validators, whose English is never
said. Always exported, an empty list included; absent is « not said »: none
created, none pruned.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError

from recipes.forms import till_format_key
from recipes.models import TillFormat
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.sections.bank import delete_ids, named_fields, restore_moments, sentence
from transfer.sections.base import Section

KEY = "formats_caisse"

# The JSON, field by field. The guard test holds every concrete field of the
# model to being here or in NOT_EXPORTED, so a field added later cannot be
# left out in silence.
FIELDS = (
    "name",
    "kind",
    "encoding",
    "delimiter",
    "decimal_mark",
    "date_format",
    "service_day_end_hour",
    "sheet",
    "day_column",
    "time_column",
    "product_column",
    "quantity_column",
    "amount_column",
    "amount_ht_column",
    "rate_column",
    "category_column",
    "typology_column",
    "method_column",
    "paid_column",
    "amount_is_unit_price",
    "method_map",
    "created_at",
)
# Never the moment (§6.4): the same format typed a day later elsewhere is
# « inchangé », not a conflict.
COMPARED = tuple(name for name in FIELDS if name != "created_at")
EXPORTED = {TillFormat: FIELDS}
NOT_EXPORTED = {TillFormat: {"id": "pk"}}

TOP_LEVEL = ("formats",)
FORMATS = "formats de fichiers de caisse"

FIELD_LABELS = {
    "name": "nom",
    "kind": "contenu",
    "encoding": "encodage",
    "delimiter": "séparateur",
    "decimal_mark": "séparateur décimal",
    "date_format": "format des dates",
    "service_day_end_hour": "fin du service",
    "sheet": "feuille",
    "day_column": "colonne du jour",
    "time_column": "colonne de l'heure",
    "product_column": "colonne du produit",
    "quantity_column": "colonne de la quantité",
    "amount_column": "colonne du montant TTC",
    "amount_ht_column": "colonne du montant HT",
    "rate_column": "colonne du taux de TVA",
    "category_column": "colonne de la catégorie",
    "typology_column": "colonne de la typologie",
    "method_column": "colonne du moyen de paiement",
    "paid_column": "colonne du montant payé",
    "amount_is_unit_price": "prix unitaire",
    "method_map": "moyens de paiement",
}


def _check(fmt: TillFormat) -> None:
    """The model's own check, the one the page's form runs (`clean` is
    `till_file.check_format`), its sentence after the field it names; then
    Django's validators (a choice, a length), whose English is never said:
    the field is named instead. Raised as a FieldValueError, in French."""
    try:
        fmt.clean()
    except ValidationError as exc:
        errors = exc.message_dict if hasattr(exc, "error_dict") else {"": exc.messages}
        field, messages = next(iter(errors.items()))
        label = FIELD_LABELS.get(field, field)
        raise codec.FieldValueError(f"format refusé — {label} : {sentence(' '.join(messages), label)}") from None
    try:
        fmt.full_clean(validate_unique=False)
    except ValidationError as exc:
        raise codec.FieldValueError(f"« {next(iter(exc.message_dict))} » : valeur refusée") from None


@registry.register
class TillFormatsSection(Section):
    key = KEY

    # -- what this database holds ------------------------------------------
    def count(self) -> dict[str, int]:
        return {FORMATS: TillFormat.objects.count()}

    def snapshot(self):
        # By name, which is unique: the list never compares a column that
        # may be None.
        return {"formats": sorted(list(codec.record(fmt, FIELDS).values()) for fmt in TillFormat.objects.all())}

    # -- export ----------------------------------------------------------------
    def export(self, out) -> None:
        formats = list(TillFormat.objects.order_by("name"))
        # Always said, empty included: absent, it reads as « not said », and
        # « Remplacer » would keep this database's.
        out.write({"formats": [codec.record(fmt, FIELDS) for fmt in formats]}, {FORMATS: len(formats)})

    # -- import ----------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        items = payload.get("formats")
        if items is not None and (not isinstance(items, list) or not all(isinstance(item, dict) for item in items)):
            raise ArchiveError(f"Archive refusée : dans {src.member}, « formats » n'est pas une liste d'objets.")
        self.payload = payload
        self._member = src.member
        self._formats: list | None = items
        # What the file names, whatever becomes of its records: prune never
        # deletes a format the archive holds, even one it could not read.
        self._keys: set[str] = set()
        self._ids: dict[str, int] = {}

    def apply(self, ctx, report) -> None:
        report.unchanged(FORMATS, 0)  # one row, even when nothing moves
        codec.note_unknown(report, self.payload, TOP_LEVEL, where=f"{self._member} › ")
        if self._formats is None:
            return
        replacing = ctx.replacing(self.key)
        existing: dict[str, TillFormat] = {}
        for fmt in TillFormat.objects.order_by("name"):
            existing.setdefault(till_format_key(fmt.name), fmt)
        created = []
        for record in self._formats:
            codec.note_unknown(report, record, FIELDS, where=f"{FORMATS} › ")
            name = record.get("name")
            key = till_format_key(name) if isinstance(name, str) else ""
            if not key:
                report.skip("Format de fichier de caisse sans nom")
                continue
            said = f"Format de fichier de caisse « {name} »"
            if key in self._keys:
                report.skip(f"{said} : en double dans l'archive")
                continue
            # Named before its record is read: the prune never deletes a
            # format the archive holds, even one it could not read.
            self._keys.add(key)
            fmt = existing.get(key)
            try:
                codec.load(TillFormat, "name", name)
                if fmt is None:
                    fmt = TillFormat()
                    codec.assign(fmt, record, FIELDS)
                    _check(fmt)
                    moment = fmt.created_at
                    fmt.save()
                    created.append((fmt, moment))
                    self._ids[key] = fmt.pk
                    report.created(FORMATS)
                    continue
                self._ids[key] = fmt.pk
                different = codec.differences(fmt, record, COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"{said} : {exc}")
                continue
            if not different:
                report.unchanged(FORMATS)
            elif replacing:
                # Every field, the moment included, then the model's check: an
                # archive's columns replacing these are checked as a format it
                # creates. What cannot be read leaves it as it was.
                try:
                    changed = codec.assign(fmt, record, FIELDS)
                    _check(fmt)
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

    def prune(self, ctx, report) -> None:
        # Nothing points at a format: what the archive does not name goes -
        # of a list it said. A list it left out prunes nothing.
        if self._formats is None:
            return
        doomed = [
            pk
            for pk, name in TillFormat.objects.values_list("pk", "name")
            if till_format_key(name) not in self._keys or self._ids.get(till_format_key(name), pk) != pk
        ]
        if doomed:
            report.deleted(FORMATS, delete_ids(TillFormat, doomed))

    # -- clear -----------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        report.deleted(FORMATS, TillFormat.objects.all().delete()[1].get(TillFormat._meta.label, 0))
