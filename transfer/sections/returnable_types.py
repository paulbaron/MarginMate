"""« Types et formats de consignes »: the returnable types with their
« motifs des bons », and the slip formats with every pattern a slip is read
by - what /consignes/ reads with, apart from what it read.

Configuration, not data: a format reads a seller's slip, and a type's
patterns classify its lines, whatever bar the slip lands in. Another bar
buying from the same supplier reads the same slips, so it takes these into
a new espace without a single pickup of the first one (the Exporter tab's
« Configuration seule »). They travelled inside « Consignes » until then
(consignes.json's "types" and "formats"), and every archive written before
- every safety backup of the time included - still holds them there: read
through `archive.CARVED`, its records are the very shapes this section
writes, and import with this code.

* **Natural keys, never pks.** A type and a format by their name, the way
  their form refuses a twin: `search_key(" ".join(name.split()))` (« Futs »
  finds « Fûts »). A format's supplier by its code, then its name (from
  `supplier_names`). « Consignes » finds a count's type and a slip's format
  the same way (`Named`), so the two sections cannot disagree about which
  row an archive's name is.
* **Every pattern is checked as the forms check it** (`returnables.patterns`):
  an archive is a file anybody can edit, and a pattern that never went
  through the guard would be compiled at the first drawing of /consignes/ -
  the 50 GB compile of 29/09, through an import. A refused pattern skips its
  record: « motif refusé : <champ> — <raison> ». So does a type's order past
  the types page's bound (`MAX_POSITION`): codec.load bounds no integer.
* **Each list may be left out**: None is « not said » - nothing created,
  compared or pruned of it. Never read as an empty list, which « Remplacer »
  would take for « delete every type here »: a manifest whose counts say
  nothing is read as carrying this section (`archive.carved`), and its file
  may hold neither list.
* **The prune keeps what is still used**: a type a pickup counts, a format a
  slip was read with (PROTECT), said. The runner prunes in reverse order, so
  « Consignes » (order 100) has pruned its pickups and slips before this
  section (order 58) - the order the single section's prune had.
* **Moments** (`created_at`) are restored after the insert and never
  compared.

The seeded rows (the three types and the UBA format of returnables/0002) are
counted, exported and cleared like any other: the safety archive taken
before « Effacer » brings them back.
"""

from __future__ import annotations

from django.db.models import Count

from common import search_key
from returnables import patterns
from returnables.models import ReturnableType, SlipFormat
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.sections.base import Section
from transfer.sections.suppliers import plural, said

KEY = "types_consignes"

# -- what travels --------------------------------------------------------------------

TYPE_FIELDS = ("name", "position", "is_active", "slip_patterns", "created_at")
PATTERN_FIELDS = tuple(pattern_field.attr for pattern_field in patterns.FORMAT_FIELDS)
FORMAT_FIELDS = ("name", "is_active", *PATTERN_FIELDS, "created_at")

# Never the moment a row was made (§6.4): an export of the same data taken a
# minute later merges as « inchangé ».
TYPE_COMPARED = ("name", "position", "is_active", "slip_patterns")
FORMAT_COMPARED = ("name", "is_active", *PATTERN_FIELDS)

#: Every concrete field of the two models is exported or said why not - a
#: guard test holds it, so a field added later cannot be left out in silence.
EXPORTED = {
    ReturnableType: TYPE_FIELDS,
    SlipFormat: FORMAT_FIELDS,
}
NOT_EXPORTED = {
    ReturnableType: {"id": "pk"},
    SlipFormat: {"id": "pk", "supplier": "par son code"},
}

TOP_LEVEL = ("supplier_names", "types", "formats")
#: Both optional: None is « not said ».
LISTS = ("types", "formats")
FORMAT_KEYS = ("supplier", *FORMAT_FIELDS)

# -- the words: count(), the archive's counts and the report say the same ------------

TYPES = "types de consigne"
FORMATS = "formats de bons"
#: In this order, under the labels « Consignes » counted them with before:
#: an older archive's counts are read as this section's (archive.CARVED, a
#: test holds both).
ENTITIES = (TYPES, FORMATS)

LABELS = {
    "name": "nom",
    "position": "ordre",
    "is_active": "actif",
    "slip_patterns": "motifs des bons",
    "supplier": "fournisseur",
    **{
        pattern_field.attr: pattern_field.label[0].lower() + pattern_field.label[1:]
        for pattern_field in patterns.FORMAT_FIELDS
    },
}

CLEAR_NOTE = (
    "Les types de consigne et le format de bon créés à l'installation sont effacés avec le reste : la sauvegarde "
    "prise avant l'effacement les ramène."
)

#: A type's order as the types page takes it (returnables.forms.TypeForm, a
#: small integer). SQLite holds more and codec.load bounds no integer: past
#: it the page refused to save the type it drew, and from 2**63 SQLite
#: refused to store it - the import, preview included, was a 500.
MAX_POSITION = 32_767

DELETE_BATCH = 500


# -- keys and words --------------------------------------------------------------------


def name_key(name: str) -> str:
    """How a type or a format is found: the forms' own rule, case, accents
    and spaces ignored - an archive's « Futs » is this database's « Fûts »,
    a pair the type form refuses."""
    return search_key(" ".join((name or "").split()))


class Named:
    """The rows of one model here, found as an archive's record names them:
    the exact name first, then `name_key` (the first in the order given).
    This section finds the record's own row through it, and « Consignes »
    a count's type and a slip's format - read after this section applied,
    so what it created or renamed in the same run is found."""

    def __init__(self, rows):
        rows = list(rows)
        self._by_name = {row.name: row for row in rows}
        self._by_key: dict[str, object] = {}
        for row in rows:
            self._by_key.setdefault(name_key(row.name), row)

    def find(self, name: str):
        return self._by_name.get(name) or self._by_key.get(name_key(name))


def _refused(label: str, error: patterns.PatternError) -> str:
    """« motif refusé : <champ> — <raison> », from the guard's own sentence
    (« <champ> : <raison>. »)."""
    field_label, separator, reason = error.message.partition(" : ")
    if not separator:
        field_label, reason = label, error.message
    return f"motif refusé : {field_label} — {reason.rstrip('.')}"


def check_type_patterns(value) -> None:
    """A type's « motifs des bons », checked as its form checks them."""
    try:
        patterns.compile_field(patterns.TYPE_FIELD, value)
    except patterns.PatternError as error:
        raise codec.FieldValueError(_refused(patterns.TYPE_FIELD.label, error)) from None


def check_format_patterns(values: dict) -> None:
    """A format's patterns as they would be stored, checked as its form checks
    them: each by the guard (returnables.patterns), the line and the date
    patterns required, and a sender pattern naming an address or a domain, with
    its subject."""
    for pattern_field in patterns.FORMAT_FIELDS:
        value = values.get(pattern_field.attr) or ""
        if pattern_field.required and not value.strip():
            raise codec.FieldValueError(f"motif refusé : {pattern_field.label} — le motif est vide")
        try:
            patterns.compile_field(pattern_field, value)
        except patterns.PatternError as error:
            raise codec.FieldValueError(_refused(pattern_field.label, error)) from None
    sender = (values.get("sender_pattern") or "").strip()
    if sender:
        label = patterns.FIELD_BY_ATTR["sender_pattern"].label
        try:
            patterns.check_sender_pattern(sender)
        except patterns.PatternError as error:
            raise codec.FieldValueError(_refused(label, error)) from None
        if not (values.get("subject_pattern") or "").strip():
            raise codec.FieldValueError(
                f"motif refusé : {patterns.FIELD_BY_ATTR['subject_pattern'].label} — obligatoire quand un motif "
                "d'expéditeur est donné"
            )


def restore_moments(objects_and_moments, field_name: str) -> None:
    """auto_now_add wrote "now" over the archive's moment on insert:
    put back after it (bulk_update calls no pre_save). « Consignes » puts
    its pickups' and slips' moments back through it too."""
    restored = []
    for obj, moment in objects_and_moments:
        if moment is not None:
            setattr(obj, field_name, moment)
            restored.append(obj)
    if restored:
        type(restored[0]).objects.bulk_update(restored, [field_name])


def delete_ids(model, ids) -> None:
    """In batches: SQLite caps the variables of one statement."""
    ids = list(ids)
    for start in range(0, len(ids), DELETE_BATCH):
        model.objects.filter(pk__in=ids[start : start + DELETE_BATCH]).delete()


@registry.register
class ReturnableTypesSection(Section):
    key = KEY

    # -- what this database holds ----------------------------------------------------
    def count(self) -> dict[str, int]:
        return {TYPES: ReturnableType.objects.count(), FORMATS: SlipFormat.objects.count()}

    def snapshot(self):
        return {
            "types": sorted(
                (list(codec.record(row, TYPE_FIELDS).values()) for row in ReturnableType.objects.all()),
                key=lambda values: values[0],
            ),
            "formats": sorted(
                (
                    {"supplier": row.supplier.code, **codec.record(row, FORMAT_FIELDS)}
                    for row in SlipFormat.objects.select_related("supplier")
                ),
                key=lambda record: record["name"],
            ),
        }

    # -- export ------------------------------------------------------------------------
    def export(self, out) -> None:
        types = list(ReturnableType.objects.order_by("position", "id"))
        formats = list(SlipFormat.objects.select_related("supplier").order_by("name", "id"))
        suppliers = {row.supplier for row in formats}
        out.write(
            {
                # A code may differ in the database this is imported into:
                # its name lets the formats still find their supplier
                # (keys.SupplierResolver).
                "supplier_names": {
                    supplier.code: supplier.name for supplier in sorted(suppliers, key=lambda s: s.code)
                },
                "types": [codec.record(row, TYPE_FIELDS) for row in types],
                "formats": [{"supplier": row.supplier.code, **codec.record(row, FORMAT_FIELDS)} for row in formats],
            },
            {TYPES: len(types), FORMATS: len(formats)},
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        for name in LISTS:
            items = payload.get(name)
            # None is « not said », never an empty list: under « Remplacer »
            # an empty list deletes everything of it here.
            if items is not None and (not isinstance(items, list) or not all(isinstance(item, dict) for item in items)):
                raise ArchiveError(f"Archive refusée : dans {src.member}, « {name} » n'est pas une liste d'objets.")
        self.payload = payload
        # The file read: this section's own, or an older archive's
        # consignes.json (archive.CARVED).
        self._member = src.member
        self._types: list | None = payload.get("types")
        self._formats: list | None = payload.get("formats")
        # What the archive names, whatever becomes of its records: prune
        # never deletes a row a record answered to, even one it skipped.
        self._claimed_types: set[int] = set()
        self._claimed_formats: set[int] = set()

    def apply(self, ctx, report) -> None:
        for entity in ENTITIES:  # one row each, even when nothing moves
            report.unchanged(entity, 0)
        codec.note_unknown(report, self.payload, TOP_LEVEL, where=f"{self._member} › ")
        self._ctx, self._report = ctx, report
        self._replacing = ctx.replacing(self.key)
        if self._types is not None:
            self._apply_types(self._types)
        if self._formats is not None:
            self._apply_formats(self._formats)

    # .. types .......................................................................
    def _apply_types(self, records: list) -> None:
        report = self._report
        here = Named(ReturnableType.objects.order_by("position", "id"))
        seen: set[str] = set()
        created = []
        for record in records:
            codec.note_unknown(report, record, TYPE_FIELDS, where="types de consigne › ")
            name = record.get("name")
            if not isinstance(name, str) or not name.strip():
                report.skip("Type de consigne sans nom lisible")
                continue
            key = name_key(name)
            label = f"Type de consigne « {name} »"
            if key in seen:
                report.skip(f"{label} : en double dans l'archive")
                continue
            seen.add(key)
            row = here.find(name)
            if row is not None:
                # Before anything is checked: a type the archive names, even
                # skipped, is one prune must not remove.
                self._claimed_types.add(row.pk)
            try:
                values = {
                    name_: codec.load(ReturnableType, name_, record[name_]) for name_ in TYPE_FIELDS if name_ in record
                }
                if values.get("position", 0) > MAX_POSITION:
                    raise codec.FieldValueError(f"« position » : 32 767 au plus (« {values['position']} »)")
                if "slip_patterns" in values:
                    check_type_patterns(values["slip_patterns"])
                if row is None:
                    row = ReturnableType(**{name_: value for name_, value in values.items() if name_ != "created_at"})
                    row.save()
                    created.append((row, values.get("created_at")))
                    self._claimed_types.add(row.pk)
                    report.created(TYPES)
                    continue
                different = codec.differences(row, record, TYPE_COMPARED)
            except codec.FieldValueError as exc:
                report.skip(f"{label} : {exc}")
                continue
            if not different:
                report.unchanged(TYPES)
            elif self._replacing:
                changed = codec.assign(row, record, TYPE_COMPARED)
                row.save(update_fields=changed)
                report.updated(TYPES)
            else:
                report.conflict(f"{label} : différent dans l'archive ({said(different, LABELS)}) — gardé tel quel")
        restore_moments(created, "created_at")

    # .. formats .....................................................................
    def _apply_formats(self, records: list) -> None:
        ctx, report = self._ctx, self._report
        here = Named(SlipFormat.objects.select_related("supplier").order_by("name", "id"))
        seen: set[str] = set()
        created = []
        for record in records:
            codec.note_unknown(report, record, FORMAT_KEYS, where="formats de bons › ")
            name = record.get("name")
            if not isinstance(name, str) or not name.strip():
                report.skip("Format de bon sans nom lisible")
                continue
            key = name_key(name)
            label = f"Format de bon « {name} »"
            if key in seen:
                report.skip(f"{label} : en double dans l'archive")
                continue
            seen.add(key)
            row = here.find(name)
            if row is not None:
                self._claimed_formats.add(row.pk)
            code = record.get("supplier")
            supplier = ctx.suppliers.resolve(code) if isinstance(code, str) else None
            if supplier is None:
                report.skip(f"{label} : fournisseur inconnu (« {code} »)")
                continue
            try:
                values = {
                    name_: codec.load(SlipFormat, name_, record[name_]) for name_ in FORMAT_FIELDS if name_ in record
                }
                # The patterns as they would be stored: what the record says,
                # else what is here (or the field's default, for a new one).
                check_format_patterns(
                    {
                        attr: values[attr]
                        if attr in values
                        else (getattr(row, attr) if row is not None else SlipFormat._meta.get_field(attr).get_default())
                        for attr in PATTERN_FIELDS
                    }
                )
                if row is None:
                    row = SlipFormat(
                        supplier=supplier, **{name_: value for name_, value in values.items() if name_ != "created_at"}
                    )
                    row.save()
                    created.append((row, values.get("created_at")))
                    self._claimed_formats.add(row.pk)
                    report.created(FORMATS)
                    continue
                different = (["supplier"] if row.supplier_id != supplier.pk else []) + codec.differences(
                    row, record, FORMAT_COMPARED
                )
            except codec.FieldValueError as exc:
                report.skip(f"{label} : {exc}")
                continue
            if not different:
                report.unchanged(FORMATS)
            elif self._replacing:
                codec.assign(row, record, FORMAT_COMPARED)
                row.supplier = supplier
                row.save()
                report.updated(FORMATS)
            else:
                report.conflict(f"{label} : différent dans l'archive ({said(different, LABELS)}) — gardé tel quel")
        restore_moments(created, "created_at")

    # .. prune ......................................................................
    def prune(self, ctx, report) -> None:
        """What the archive does not name goes - of a list it said. A format
        a slip still uses, a type a pickup still counts, stays, said: by now
        « Consignes » has pruned what it was going to (reverse order), so
        what is left needs it."""
        if self._formats is not None:
            self._prune_named(
                report,
                SlipFormat,
                self._claimed_formats,
                FORMATS,
                "slips",
                lambda name, n: f"Format de bon « {name} » : encore utilisé par {plural(n, 'bon')}",
            )
        if self._types is not None:
            self._prune_named(
                report,
                ReturnableType,
                self._claimed_types,
                TYPES,
                "counts",
                lambda name, n: f"Type de consigne « {name} » : encore compté dans {plural(n, 'reprise')}",
            )

    @staticmethod
    def _prune_named(report, model, claimed: set[int], entity: str, related: str, why) -> None:
        doomed = []
        for pk, name, used in model.objects.annotate(used=Count(related)).values_list("pk", "name", "used"):
            if pk in claimed:
                continue
            if used:
                report.keep(why(name, used))
            else:
                doomed.append(pk)
        if doomed:
            delete_ids(model, doomed)
            report.deleted(entity, len(doomed))

    # -- clear --------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        """Every format and type, the seeds included. What names them is
        « Consignes »': the registry ticks it with this one, and clears run
        in reverse order, so its pickups and slips are gone first."""
        deleted = {}
        for entity, model in ((FORMATS, SlipFormat), (TYPES, ReturnableType)):
            _total, per_model = model.objects.all().delete()
            deleted[entity] = per_model.get(model._meta.label, 0)
        for entity in ENTITIES:
            report.deleted(entity, deleted[entity])
        if deleted[TYPES] or deleted[FORMATS]:
            report.note(CLEAR_NOTE)
