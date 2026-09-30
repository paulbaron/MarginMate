"""« Consignes » (spec §8): the returnable types, the slip formats with
their patterns, every pickup with its counts and photos, and every slip with
its PDF and what its reading found.

What is worth keeping is what nothing rebuilds: how many kegs were handed
back on which day, and the photos taken before the lorry left. So:

* **Natural keys, never pks.** A type and a format are found by their name,
  the way their form refuses a twin: `search_key(" ".join(name.split()))`
  (« Futs » finds « Fûts »). A pickup by its `reference` - random, never
  shown, never edited, so a pickup whose date or counts were corrected is
  still the same pickup. A slip by the sha256 of its PDF. A supplier by its
  code (then its name, from `supplier_names`).
* **A slip's reading is not compared.** It is a function of its text and its
  format's patterns: compared, one pattern edited here would turn every slip
  into a conflict. It is copied when the slip is created, and under « Remplacer »
  when the text or the format changed - never read again at import (an
  import reads nothing; a 0.25 s timeout could make the confirm differ from
  its preview, and the confirm would be refused).
* **Every pattern is checked as the forms check it** (`returnables.patterns`):
  an archive is a file anybody can edit, and a pattern that never went
  through the guard would be compiled at the first drawing of /consignes/ -
  the 50 GB compile of 29/09, through an import. A refused pattern skips its
  record: « motif refusé : <champ> — <raison> ».
* **Every date is bounded as the form and the reader bound it**
  (`check_date`): a pickup's day and a slip's delivery within [2000-01-01,
  today + 7 days], a mail's date within [2000-01-01, today + 1 day]; past
  them the record is skipped (« date hors limites »). The codec reads any
  ISO date, and a pickup dated 9999-12-31 - the newest, drawn first - made
  /consignes/ a 500 for good through the page's date arithmetic.
* **Files** go through `ctx.check_file` / `ctx.save_file` (reused when the
  same bytes are there, written under an available name otherwise), under
  consignes/ only, and old ones are deleted on commit, only if no row still
  names them.
* **Moments** (`created_at`, `received_at`) are restored after the insert
  and never compared. `Pickup.updated_at` (« modifiée ici ») and
  `Slip.read_at` (« relue ici ») are this database's own and never travel.

Seeded rows (the three types and the UBA format of returnables/0002) are
counted, exported and cleared like any other: the safety archive taken
before « Effacer » brings them back.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.db.models import Count, Prefetch
from django.utils import timezone

from accounts import paths
from accounts.tenancy import tenant_key
from common import search_key
from returnables import patterns
from returnables.models import (
    MAX_COUNT,
    Pickup,
    PickupCount,
    PickupPhoto,
    ReturnableType,
    Slip,
    SlipFormat,
    SlipLine,
)
from transfer import codec, keys, registry
from transfer.archive import ArchiveError
from transfer.sections.base import FileRefused, Section
from transfer.sections.suppliers import day, plural, said

KEY = "consignes"

# -- what travels --------------------------------------------------------------------

TYPE_FIELDS = ("name", "position", "is_active", "slip_patterns", "created_at")
PATTERN_FIELDS = tuple(pattern_field.attr for pattern_field in patterns.FORMAT_FIELDS)
FORMAT_FIELDS = ("name", "is_active", *PATTERN_FIELDS, "created_at")
PICKUP_FIELDS = ("reference", "date", "note", "created_at")
COUNT_FIELDS = ("quantity",)
PHOTO_FILES = ("image", "thumb")
PHOTO_FIELDS = ("taken_at", "width", "height", "created_at")
SLIP_FIELDS = (
    "sha256",
    "origin",
    "original_name",
    "mail_sender",
    "mail_subject",
    "mail_date",
    "received_at",
    "text",
)
#: What a reading wrote on the slip (returnables.slips): copied, never compared.
READING_FIELDS = (
    "delivery_date",
    "printed_at",
    "number",
    "references",
    "replaces",
    "printed_total",
    "remarks",
    "checks",
    "read_error",
)
LINE_FIELDS = ("position", "designation", "quantity", "unit_amount", "amount")

# Never the moment a row was made (§6.4): an export of the same data taken a
# minute later merges as « inchangé ».
TYPE_COMPARED = ("name", "position", "is_active", "slip_patterns")
FORMAT_COMPARED = ("name", "is_active", *PATTERN_FIELDS)
PICKUP_COMPARED = ("date", "note")
PHOTO_COMPARED = ("taken_at", "width", "height")
#: A slip compared outside its reading: what was received, and the text read.
SLIP_COMPARED = ("origin", "original_name", "mail_sender", "mail_subject", "mail_date", "text")

#: Every concrete field of the seven models is exported or said why not - a
#: guard test holds it, so a field added later cannot be left out in silence.
EXPORTED = {
    ReturnableType: TYPE_FIELDS,
    SlipFormat: FORMAT_FIELDS,
    Pickup: PICKUP_FIELDS,
    PickupCount: COUNT_FIELDS,
    PickupPhoto: (*PHOTO_FILES, *PHOTO_FIELDS),
    Slip: ("file", *SLIP_FIELDS, *READING_FIELDS),
    SlipLine: LINE_FIELDS,
}
NOT_EXPORTED = {
    ReturnableType: {"id": "pk"},
    SlipFormat: {"id": "pk", "supplier": "par son code"},
    Pickup: {"id": "pk", "supplier": "par son code", "updated_at": "modifiée ici"},
    PickupCount: {"id": "pk", "pickup": "parent", "returnable_type": "par son nom"},
    PickupPhoto: {"id": "pk", "pickup": "parent"},
    Slip: {"id": "pk", "format": "par son nom", "read_at": "relue ici"},
    SlipLine: {"id": "pk", "slip": "parent"},
}

TOP_LEVEL = ("supplier_names", "types", "formats", "pickups", "slips")
LISTS = ("types", "formats", "pickups", "slips")
FORMAT_KEYS = ("supplier", *FORMAT_FIELDS)
PICKUP_KEYS = ("supplier", *PICKUP_FIELDS, "counts", "photos")
COUNT_KEYS = ("type", "quantity")
PHOTO_KEYS = (*PHOTO_FILES, *PHOTO_FIELDS)
SLIP_KEYS = ("format", "file", *SLIP_FIELDS, "reading")
READING_KEYS = (*READING_FIELDS, "lines")

# -- the words: count(), the archive's counts and the report say the same ------------

TYPES = "types de consigne"
FORMATS = "formats de bons"
PICKUPS = "reprises"
PHOTOS = "photos"
SLIPS = "bons"
LINES = "lignes de bons"
MEGABYTES = "Mo de fichiers"
ENTITIES = (TYPES, FORMATS, PICKUPS, PHOTOS, SLIPS, LINES)

#: The stored names this section writes: never another section's folder.
FOLDER = "consignes/"
#: What a slip's reading may hold (returnables.reading): more is not a reading
#: this application wrote.
MAX_REFERENCES = 20
MAX_REFERENCE_CHARS = 40
MAX_QUANTITY = patterns.MAX_QUANTITY
MAX_POSITION = 32_767
#: How far past today a date may be (from 2000-01-01, patterns.OLDEST_DATE):
#: a pickup's day and a slip's delivery as the reader allows it (another
#: machine's clock, a slip printed ahead); a mail's date one day.
FUTURE_DAYS = patterns.FUTURE_DAYS
MAIL_FUTURE_DAYS = 1

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
    "date": "date",
    "note": "note",
    "counts": "nombres",
    "photos": "photos",
    "format": "format",
    "file": "fichier",
    "origin": "provenance",
    "original_name": "nom du fichier reçu",
    "mail_sender": "expéditeur du mail",
    "mail_subject": "objet du mail",
    "mail_date": "date du mail",
    "text": "texte lu",
}

CLEAR_NOTE = (
    "Les types de consigne et le format de bon créés à l'installation sont effacés avec le reste : la sauvegarde "
    "prise avant l'effacement les ramène."
)

SIZE_CACHE_SECONDS = 60
DELETE_BATCH = 500


class _NotSaid:
    """A field the record does not have: never compared, never written."""

    def __repr__(self):
        return "NOT_SAID"


NOT_SAID = _NotSaid()


@dataclass(frozen=True)
class _Missing:
    """A file the other database named but its disk lacked at export."""

    name: str


@dataclass
class _Photo:
    values: dict
    item: dict
    refs: dict  # "image"/"thumb" → a ref dict, _Missing, or None


@dataclass
class _Pickup:
    label: str
    values: dict
    record: dict
    supplier: object = NOT_SAID
    counts: dict | None = None  # type pk → (type, quantity); None: not said
    photos: list | None = None  # [_Photo]; None: not said


@dataclass
class _Slip:
    label: str
    format: object
    values: dict
    record: dict
    ref: object  # a ref dict, _Missing, None or NOT_SAID
    reading: dict | None = None  # the reading's values; None: not said
    lines: list = field(default_factory=list)  # [{position, designation, …}]


# -- keys and words --------------------------------------------------------------------


def name_key(name: str) -> str:
    """How a type or a format is found: the forms' own rule, case, accents
    and spaces ignored - an archive's « Futs » is this database's « Fûts »,
    a pair the type form refuses."""
    return search_key(" ".join((name or "").split()))


def pickup_label(value) -> str:
    return f"Reprise du {day(value)}" if value else "Reprise sans date lisible"


def slip_label(number, delivery_date) -> str:
    label = f"Bon n° {number}" if number else "Bon sans numéro"
    return f"{label} du {day(delivery_date)}" if delivery_date else label


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


def check_date(name: str, value, *, future_days: int, today: date | None = None):
    """`value` (a date codec.load read, or None) when it lies within
    [2000-01-01, today + future_days] - the bounds of the pickup form and
    of the reader - else FieldValueError: its record is skipped, said. The
    codec reads any ISO date; one at the calendar's ends (9999-12-31,
    0001-01-02) read in made every page doing date arithmetic on it a 500."""
    if value is None:
        return None
    today = today or timezone.localdate()
    latest = today + timedelta(days=future_days)
    if not patterns.OLDEST_DATE <= value <= latest:
        raise codec.FieldValueError(
            f"« {name} » : date hors limites (« {value.isoformat()} ») : "
            f"entre le {patterns.OLDEST_DATE:%d/%m/%Y} et le {latest:%d/%m/%Y}"
        )
    return value


def _readable_references(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_REFERENCES
        and all(isinstance(item, str) and len(item) <= MAX_REFERENCE_CHARS for item in value)
    )


def _readable_checks(value) -> bool:
    """[{"label": str, "passed": bool, "detail": str}] - what the reading
    writes and the pages read."""
    return isinstance(value, list) and all(
        isinstance(check, dict)
        and isinstance(check.get("label"), str)
        and isinstance(check.get("passed"), bool)
        and isinstance(check.get("detail"), str)
        for check in value
    )


def _restore(objects_and_moments, field_name: str) -> None:
    """auto_now_add wrote "now" over the archive's moment on insert:
    put back after it (bulk_update calls no pre_save)."""
    restored = []
    for obj, moment in objects_and_moments:
        if moment is not None:
            setattr(obj, field_name, moment)
            restored.append(obj)
    if restored:
        type(restored[0]).objects.bulk_update(restored, [field_name])


def _delete_ids(model, ids) -> None:
    ids = list(ids)
    for start in range(0, len(ids), DELETE_BATCH):
        model.objects.filter(pk__in=ids[start : start + DELETE_BATCH]).delete()


def _stored(name: str) -> bool:
    """Whether storage holds this name - False for one it refuses."""
    try:
        return bool(name) and default_storage.exists(name)
    except (SuspiciousFileOperation, OSError, ValueError):
        return False


# -- sizes, for count() ------------------------------------------------------------------

#: (tenant, media folder) → (hash of the names, when, bytes): as the
#: invoices section keeps its own (sections/invoices.py) - per tenant, since
#: two tenants restored from one archive name the same files.
_SIZES: dict[tuple[str, str], tuple[int, float, int]] = {}


def _bytes_of(names: frozenset[str]) -> int:
    where = (tenant_key(), os.fspath(paths.media_root()))
    token = hash(names)
    now = time.monotonic()
    kept = _SIZES.get(where)
    if kept is not None and kept[0] == token and now - kept[1] < SIZE_CACHE_SECONDS:
        return kept[2]
    total = 0
    for name in names:
        try:
            total += os.path.getsize(default_storage.path(name))
        except (OSError, SuspiciousFileOperation, NotImplementedError, ValueError):
            continue
    _SIZES[where] = (token, now, total)
    return total


def named_files() -> set[str]:
    """Every file a row of this section names: photos, thumbnails, slips."""
    names = set()
    for image, thumb in PickupPhoto.objects.values_list("image", "thumb"):
        names.update(name for name in (image, thumb) if name)
    names.update(name for name in Slip.objects.values_list("file", flat=True) if name)
    return names


@registry.register
class ReturnablesSection(Section):
    key = KEY

    # -- what this database holds ----------------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            TYPES: ReturnableType.objects.count(),
            FORMATS: SlipFormat.objects.count(),
            PICKUPS: Pickup.objects.count(),
            PHOTOS: PickupPhoto.objects.count(),
            SLIPS: Slip.objects.count(),
            LINES: SlipLine.objects.count(),
            MEGABYTES: round(_bytes_of(frozenset(named_files())) / 1_000_000),
        }

    def snapshot(self):
        def stored(field_file):
            return [field_file.name, keys.file_sha256(field_file)] if field_file else None

        counts = defaultdict(list)
        for pickup_id, type_name, quantity in PickupCount.objects.values_list(
            "pickup_id", "returnable_type__name", "quantity"
        ):
            counts[pickup_id].append([type_name, quantity])
        photos = defaultdict(list)
        for photo in PickupPhoto.objects.order_by("created_at", "id"):
            photos[photo.pickup_id].append(
                [stored(photo.image), stored(photo.thumb), *codec.record(photo, PHOTO_FIELDS).values()]
            )
        lines = defaultdict(list)
        for line in SlipLine.objects.order_by("position", "id"):
            lines[line.slip_id].append(codec.record(line, LINE_FIELDS))
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
            "pickups": sorted(
                (
                    {
                        "supplier": pickup.supplier.code if pickup.supplier_id else None,
                        **codec.record(pickup, PICKUP_FIELDS),
                        "counts": sorted(counts[pickup.pk]),
                        "photos": photos[pickup.pk],
                    }
                    for pickup in Pickup.objects.select_related("supplier")
                ),
                key=lambda record: record["reference"],
            ),
            "slips": sorted(
                (
                    {
                        "format": slip.format.name,
                        "file": stored(slip.file),
                        **codec.record(slip, (*SLIP_FIELDS, *READING_FIELDS)),
                        "lines": lines[slip.pk],
                    }
                    for slip in Slip.objects.select_related("format")
                ),
                key=lambda record: record["sha256"],
            ),
        }

    # -- export ------------------------------------------------------------------------
    def export(self, out) -> None:
        types = list(ReturnableType.objects.order_by("position", "id"))
        formats = list(SlipFormat.objects.select_related("supplier").order_by("name", "id"))
        pickups = list(
            Pickup.objects.select_related("supplier")
            .order_by("date", "id")
            .prefetch_related(
                Prefetch(
                    "counts",
                    queryset=PickupCount.objects.select_related("returnable_type").order_by(
                        "returnable_type__position", "returnable_type_id", "id"
                    ),
                ),
                Prefetch("photos", queryset=PickupPhoto.objects.order_by("created_at", "id")),
            )
        )
        slips = list(
            Slip.objects.select_related("format")
            .order_by("received_at", "id")
            .prefetch_related(Prefetch("lines", queryset=SlipLine.objects.order_by("position", "id")))
        )
        sizes: dict[str, int] = {}

        def ref(field_file):
            written = out.add_file(field_file)
            if written and not written.get("missing"):
                sizes[written["name"]] = written["size"]
            return written

        pickup_records, photo_count = [], 0
        for pickup in pickups:
            photos = list(pickup.photos.all())
            photo_count += len(photos)
            pickup_records.append(
                {
                    "supplier": pickup.supplier.code if pickup.supplier_id else None,
                    **codec.record(pickup, PICKUP_FIELDS),
                    "counts": [
                        {"type": count.returnable_type.name, "quantity": count.quantity}
                        for count in pickup.counts.all()
                    ],
                    "photos": [
                        {
                            "image": ref(photo.image),
                            "thumb": ref(photo.thumb),
                            **codec.record(photo, PHOTO_FIELDS),
                        }
                        for photo in photos
                    ],
                }
            )
        slip_records, line_count = [], 0
        for slip in slips:
            lines = list(slip.lines.all())
            line_count += len(lines)
            slip_records.append(
                {
                    "format": slip.format.name,
                    "file": ref(slip.file),
                    **codec.record(slip, SLIP_FIELDS),
                    "reading": {
                        **codec.record(slip, READING_FIELDS),
                        "lines": [codec.record(line, LINE_FIELDS) for line in lines],
                    },
                }
            )
        suppliers = {row.supplier for row in formats} | {pickup.supplier for pickup in pickups if pickup.supplier_id}
        out.write(
            {
                # A code may differ in the database this is imported into:
                # its name lets the formats and pickups still find their
                # supplier (keys.SupplierResolver).
                "supplier_names": {
                    supplier.code: supplier.name for supplier in sorted(suppliers, key=lambda s: s.code)
                },
                "types": [codec.record(row, TYPE_FIELDS) for row in types],
                "formats": [{"supplier": row.supplier.code, **codec.record(row, FORMAT_FIELDS)} for row in formats],
                "pickups": pickup_records,
                "slips": slip_records,
            },
            {
                TYPES: len(types),
                FORMATS: len(formats),
                PICKUPS: len(pickups),
                PHOTOS: photo_count,
                SLIPS: len(slips),
                LINES: line_count,
                MEGABYTES: round(sum(sizes.values()) / 1_000_000),
            },
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        for name in LISTS:
            items = payload.get(name)
            # A list left out is refused, not read as empty: under
            # « Remplacer » an empty list deletes everything of it here.
            if not isinstance(items, list):
                raise ArchiveError(f"Archive refusée : consignes.json n'a pas de liste « {name} ».")
            if not all(isinstance(item, dict) for item in items):
                raise ArchiveError(f"Archive refusée : dans consignes.json, « {name} » ne contient pas que des objets.")
        self.payload = payload
        # What the archive names, whatever becomes of its records: prune
        # never deletes a row a record answered to, even one it skipped.
        self._claimed_types: set[int] = set()
        self._claimed_formats: set[int] = set()
        self._references: set[str] = set()
        self._shas: set[str] = set()

    def apply(self, ctx, report) -> None:
        for entity in ENTITIES:  # one row each, even when nothing moves
            report.unchanged(entity, 0)
        codec.note_unknown(report, self.payload, TOP_LEVEL, where="consignes.json › ")
        self._ctx, self._report = ctx, report
        self._replacing = ctx.replacing(self.key)
        types = self._apply_types()
        formats = self._apply_formats()
        self._apply_pickups(types)
        self._apply_slips(formats)

    # .. types .......................................................................
    def _apply_types(self) -> dict:
        """Returns every type here by key, the archive's own first: what a
        pickup's counts are resolved against."""
        report = self._report
        rows = list(ReturnableType.objects.order_by("position", "id"))
        by_name = {row.name: row for row in rows}
        by_key: dict[str, ReturnableType] = {}
        for row in rows:
            by_key.setdefault(name_key(row.name), row)
        resolved: dict[str, ReturnableType] = {}
        seen: set[str] = set()
        created = []
        for record in self.payload["types"]:
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
            row = by_name.get(name) or by_key.get(key)
            if row is not None:
                # Before anything is checked: a type the archive names, even
                # skipped, is one prune must not remove.
                self._claimed_types.add(row.pk)
                resolved[key] = row
            try:
                values = {
                    name_: codec.load(ReturnableType, name_, record[name_]) for name_ in TYPE_FIELDS if name_ in record
                }
                if "slip_patterns" in values:
                    check_type_patterns(values["slip_patterns"])
                if row is None:
                    row = ReturnableType(**{name_: value for name_, value in values.items() if name_ != "created_at"})
                    row.save()
                    created.append((row, values.get("created_at")))
                    self._claimed_types.add(row.pk)
                    resolved[key] = row
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
        _restore(created, "created_at")
        return {**by_key, **resolved}

    # .. formats .....................................................................
    def _apply_formats(self) -> dict:
        ctx, report = self._ctx, self._report
        rows = list(SlipFormat.objects.select_related("supplier").order_by("name", "id"))
        by_name = {row.name: row for row in rows}
        by_key: dict[str, SlipFormat] = {}
        for row in rows:
            by_key.setdefault(name_key(row.name), row)
        resolved: dict[str, SlipFormat] = {}
        seen: set[str] = set()
        created = []
        for record in self.payload["formats"]:
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
            row = by_name.get(name) or by_key.get(key)
            if row is not None:
                self._claimed_formats.add(row.pk)
                resolved[key] = row
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
                    resolved[key] = row
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
        _restore(created, "created_at")
        return {**by_key, **resolved}

    # .. files .......................................................................
    def _ref(self, ref):
        """A file ref of the archive, checked (in the preview too): a dict,
        a _Missing, or None - FileRefused or FieldValueError otherwise."""
        if ref is None:
            return None
        if not isinstance(ref, dict) or not isinstance(ref.get("name"), str):
            raise codec.FieldValueError("fichier illisible dans l'archive")
        if ref.get("missing"):
            return _Missing(ref["name"])
        self._ctx.check_file(ref, ref["name"])
        if not ref["name"].startswith(FOLDER):
            # Accepted by the archive (invoices/, receipts/), but not this
            # section's: the invoices would count it as theirs.
            raise FileRefused(f"fichier hors du dossier des consignes (« {ref['name'][:80]} »)")
        return ref

    def _file_state(self, ref, name: str) -> str:
        """same | fill | differs, for a file of a row found here."""
        if ref is NOT_SAID or ref is None or isinstance(ref, _Missing):
            return "same"
        if not _stored(name):
            return "fill"
        return "same" if self._ctx.file_matches(ref, name) else "differs"

    def _store(self, refs: list, written: list) -> list[str]:
        """Store each ref (writing nothing in a preview); what THIS call
        wrote goes in `written`, so the caller removes it if the record is
        refused half way - never a file reused, which another row names."""
        names = []
        for ref in refs:
            before = len(self._ctx.stored_files)
            name = self._ctx.save_file(ref, ref["name"])
            if len(self._ctx.stored_files) > before:
                written.append(name)
            names.append(name)
        return names

    def _unstore(self, written: list) -> None:
        """A record refused after some of its files were written: nothing
        would name them."""
        if self._ctx.preview:
            return
        for name in written:
            if name in self._ctx.stored_files:
                default_storage.delete(name)
                self._ctx.stored_files.remove(name)

    # .. pickups .....................................................................
    def _apply_pickups(self, types: dict) -> None:
        report = self._report
        existing = {
            pickup.reference: pickup
            for pickup in Pickup.objects.select_related("supplier").prefetch_related(
                "counts", Prefetch("photos", queryset=PickupPhoto.objects.order_by("created_at", "id"))
            )
        }
        seen: set[str] = set()
        for record in self.payload["pickups"]:
            codec.note_unknown(report, record, PICKUP_KEYS, where="reprises › ")
            reference = record.get("reference")
            try:
                if not isinstance(reference, str) or not reference.strip():
                    raise codec.FieldValueError("référence manquante")
                codec.load(Pickup, "reference", reference)
            except codec.FieldValueError as exc:
                report.skip(f"Reprise sans référence lisible : {exc}")
                continue
            self._references.add(reference)
            try:
                label = pickup_label(codec.load(Pickup, "date", record.get("date")))
            except codec.FieldValueError:
                label = pickup_label(None)
            if reference in seen:
                report.skip(f"{label} : en double dans l'archive")
                continue
            seen.add(reference)
            try:
                parsed = self._parse_pickup(record, label, types)
            except (codec.FieldValueError, FileRefused) as exc:
                report.skip(f"{label} : {exc}")
                continue
            pickup = existing.get(reference)
            if pickup is None:
                self._create_pickup(reference, parsed)
            else:
                self._update_pickup(pickup, parsed)

    def _parse_pickup(self, record: dict, label: str, types: dict) -> _Pickup:
        values = {name: codec.load(Pickup, name, record[name]) for name in ("note", "created_at") if name in record}
        values["date"] = check_date("date", codec.load(Pickup, "date", record.get("date")), future_days=FUTURE_DAYS)
        parsed = _Pickup(label=label, values=values, record=record)
        if "supplier" in record:
            code = record["supplier"]
            if code is None:
                parsed.supplier = None
            elif isinstance(code, str):
                parsed.supplier = self._ctx.suppliers.resolve(code)
                if parsed.supplier is None:
                    raise codec.FieldValueError(f"fournisseur inconnu (« {code} »)")
            else:
                raise codec.FieldValueError("fournisseur illisible")
        if "counts" in record:
            parsed.counts = self._parse_counts(record["counts"], types)
        if "photos" in record:
            items = record["photos"]
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise codec.FieldValueError("photos illisibles")
            parsed.photos = []
            for item in items:
                codec.note_unknown(self._report, item, PHOTO_KEYS, where="reprises › photos › ")
                photo_values = {
                    name: codec.load(PickupPhoto, name, item[name])
                    for name in ("taken_at", "created_at")
                    if name in item
                }
                for name in ("width", "height"):  # no default: a photo without them cannot be stored
                    photo_values[name] = codec.load(PickupPhoto, name, item.get(name))
                refs = {name: self._ref(item.get(name)) for name in PHOTO_FILES}
                parsed.photos.append(_Photo(values=photo_values, item=item, refs=refs))
        return parsed

    def _parse_counts(self, items, types: dict) -> dict:
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise codec.FieldValueError("nombres illisibles")
        counts: dict = {}
        for item in items:
            codec.note_unknown(self._report, item, COUNT_KEYS, where="reprises › nombres › ")
            type_name = item.get("type")
            if not isinstance(type_name, str) or not type_name.strip():
                raise codec.FieldValueError("nombres illisibles")
            row = types.get(name_key(type_name))
            if row is None:
                raise codec.FieldValueError(f"type de consigne inconnu « {type_name} »")
            quantity = codec.load(PickupCount, "quantity", item.get("quantity"))
            # The database refuses the rest (a CHECK constraint): said here.
            if not 1 <= quantity <= MAX_COUNT:
                raise codec.FieldValueError(f"nombre de « {type_name} » hors limites ({quantity}) : de 1 à 9 999")
            if row.pk in counts:
                raise codec.FieldValueError(f"« {type_name} » compté deux fois")
            counts[row.pk] = (row, quantity)
        return counts

    def _missing_note(self, parsed: _Pickup, photo: _Photo) -> None:
        name = next(
            (ref.name for ref in photo.refs.values() if isinstance(ref, _Missing)),
            "",
        )
        where = f" (« {name} »)" if name else ""
        self._report.note(f"{parsed.label} : photo absente du disque à l'export{where} — reprise importée sans elle")

    @staticmethod
    def _storable(photo: _Photo) -> bool:
        return all(isinstance(photo.refs[name], dict) for name in PHOTO_FILES)

    def _new_photos(self, pickup, photos: list, parsed: _Pickup, written: list) -> list:
        """PickupPhoto rows for these archive photos, their files stored; a
        photo whose file the other disk lacked is left out, said."""
        rows = []
        for photo in photos:
            if not self._storable(photo):
                self._missing_note(parsed, photo)
                continue
            image, thumb = self._store([photo.refs["image"], photo.refs["thumb"]], written)
            rows.append(
                (
                    PickupPhoto(
                        pickup=pickup,
                        image=image,
                        thumb=thumb,
                        **{name: value for name, value in photo.values.items() if name != "created_at"},
                    ),
                    photo.values.get("created_at"),
                )
            )
        return rows

    def _create_pickup(self, reference: str, parsed: _Pickup) -> None:
        report = self._report
        written: list[str] = []
        pickup = Pickup(
            reference=reference,
            supplier=None if parsed.supplier is NOT_SAID else parsed.supplier,
            **{name: value for name, value in parsed.values.items() if name != "created_at"},
        )
        try:
            photos = self._new_photos(pickup, parsed.photos or [], parsed, written)
        except FileRefused as exc:
            self._unstore(written)
            report.skip(f"{parsed.label} : {exc}")
            return
        pickup.save()
        _restore([(pickup, parsed.values.get("created_at"))], "created_at")
        PickupCount.objects.bulk_create(
            PickupCount(pickup=pickup, returnable_type=row, quantity=quantity)
            for row, quantity in (parsed.counts or {}).values()
        )
        for row, _moment in photos:
            row.pickup = pickup
            row.save()
        _restore(photos, "created_at")
        report.created(PICKUPS)
        if photos:
            report.created(PHOTOS, len(photos))

    def _photo_states(self, here: list, photos: list) -> list[tuple[bool, list[str]]]:
        """Per rank shared by both: (whether its values or a file differ,
        the files to fill)."""
        states = []
        for row, photo in zip(here, photos):
            differs = bool(codec.differences(row, photo.item, PHOTO_COMPARED))
            fills = []
            for name in PHOTO_FILES:
                state = self._file_state(photo.refs[name], getattr(row, name).name)
                if state == "differs":
                    differs = True
                elif state == "fill":
                    fills.append(name)
            states.append((differs, fills))
        return states

    def _update_pickup(self, pickup: Pickup, parsed: _Pickup) -> None:
        report = self._report
        here_photos = list(pickup.photos.all())
        different = codec.differences(pickup, parsed.record, PICKUP_COMPARED)
        if parsed.supplier is not NOT_SAID and pickup.supplier_id != (parsed.supplier.pk if parsed.supplier else None):
            different.append("supplier")
        if parsed.counts is not None and {pk: quantity for pk, (_row, quantity) in parsed.counts.items()} != {
            count.returnable_type_id: count.quantity for count in pickup.counts.all()
        }:
            different.append("counts")
        states = self._photo_states(here_photos, parsed.photos) if parsed.photos is not None else []
        if parsed.photos is not None and (
            len(parsed.photos) != len(here_photos) or any(differs for differs, _fills in states)
        ):
            different.append("photos")
        if self._replacing and different:
            self._replace_pickup(pickup, parsed, here_photos, states, different)
            return
        # Kept as it is - but a photo whose file this disk lost is given
        # back, whatever else differs.
        filled = self._fill_photos(here_photos, parsed, states)
        if filled is None:
            return
        if different:
            report.conflict(
                f"{parsed.label} : différente dans l'archive ({said(different, LABELS)}) — gardée telle quelle"
            )
            if filled:
                report.updated(PHOTOS, filled)
            return
        report.unchanged(PICKUPS)
        if filled:
            report.updated(PHOTOS, filled)
        if len(here_photos) > filled:
            report.unchanged(PHOTOS, len(here_photos) - filled)

    def _fill_photos(self, here: list, parsed: _Pickup, states: list) -> int | None:
        """Store the files this disk lost; how many photos got one back, or
        None when a file was refused (the record skipped, said)."""
        written: list[str] = []
        changed = []
        try:
            for row, photo, (_differs, fills) in zip(here, parsed.photos or [], states):
                if not fills:
                    continue
                names = self._store([photo.refs[name] for name in fills], written)
                for name, stored in zip(fills, names):
                    setattr(row, name, stored)
                changed.append((row, fills))
        except FileRefused as exc:
            self._unstore(written)
            self._report.skip(f"{parsed.label} : {exc}")
            return None
        for row, fills in changed:
            row.save(update_fields=fills)
        if changed:
            self._report.note(f"{parsed.label} : {plural(len(changed), 'photo restaurée', 'photos restaurées')}")
        return len(changed)

    def _replace_pickup(self, pickup: Pickup, parsed: _Pickup, here: list, states: list, different: list) -> None:
        """The pickup becomes the archive's: its fields, its counts as a
        whole, its photos paired by rank - one equal to the archive's is left
        as it is, one that differs is rewritten in place (its old files
        deleted on commit), the extra ones here go, the archive's extra ones
        come."""
        report = self._report
        ctx = self._ctx
        written: list[str] = []
        photos = parsed.photos
        rewritten, old_names = [], []
        try:
            if photos is not None:
                for row, photo, (differs, fills) in zip(here, photos, states):
                    if not differs and not fills:
                        continue
                    wanted = [
                        name
                        for name in PHOTO_FILES
                        if isinstance(photo.refs[name], dict)
                        and self._file_state(photo.refs[name], getattr(row, name).name) != "same"
                    ]
                    names = self._store([photo.refs[name] for name in wanted], written)
                    rewritten.append((row, photo, dict(zip(wanted, names))))
                new_rows = self._new_photos(pickup, photos[len(here) :], parsed, written)
            else:
                new_rows = []
        except FileRefused as exc:
            self._unstore(written)
            report.skip(f"{parsed.label} : {exc}")
            return

        codec.assign(pickup, parsed.record, PICKUP_COMPARED)
        if parsed.supplier is not NOT_SAID:
            pickup.supplier = parsed.supplier
        pickup.save()
        report.updated(PICKUPS)
        if parsed.counts is not None and "counts" in different:
            PickupCount.objects.filter(pickup=pickup).delete()
            PickupCount.objects.bulk_create(
                PickupCount(pickup=pickup, returnable_type=row, quantity=quantity)
                for row, quantity in parsed.counts.values()
            )
        if photos is None:
            if here:
                report.unchanged(PHOTOS, len(here))
            return
        untouched = len(here[: len(photos)]) - len(rewritten)
        for row, photo, stored in rewritten:
            codec.assign(row, photo.item, PHOTO_COMPARED)
            for name, value in stored.items():
                old = getattr(row, name).name
                if old and old != value:
                    old_names.append(old)
                setattr(row, name, value)
            row.save()
        if rewritten:
            report.updated(PHOTOS, len(rewritten))
        if untouched:
            report.unchanged(PHOTOS, untouched)
        extra = here[len(photos) :]
        if extra:
            for row in extra:
                old_names.extend(name for name in (row.image.name, row.thumb.name) if name)
            _delete_ids(PickupPhoto, [row.pk for row in extra])
            report.deleted(PHOTOS, len(extra))
        for row, _moment in new_rows:
            row.save()
        _restore(new_rows, "created_at")
        if new_rows:
            report.created(PHOTOS, len(new_rows))
        for name in old_names:
            ctx.delete_file_on_commit(name)

    # .. slips .......................................................................
    def _apply_slips(self, formats: dict) -> None:
        report = self._report
        existing = {slip.sha256: slip for slip in Slip.objects.select_related("format")}
        lines_here = defaultdict(list)
        for line in SlipLine.objects.order_by("position", "id"):
            lines_here[line.slip_id].append(line)
        seen: set[str] = set()
        for record in self.payload["slips"]:
            codec.note_unknown(report, record, SLIP_KEYS, where="bons › ")
            sha = record.get("sha256")
            try:
                if not isinstance(sha, str) or not sha.strip():
                    raise codec.FieldValueError("empreinte manquante")
                codec.load(Slip, "sha256", sha)
            except codec.FieldValueError as exc:
                report.skip(f"Bon sans empreinte lisible : {exc}")
                continue
            self._shas.add(sha)
            label = self._slip_label(record)
            if sha in seen:
                report.skip(f"{label} : en double dans l'archive")
                continue
            seen.add(sha)
            try:
                parsed = self._parse_slip(record, label, formats)
            except (codec.FieldValueError, FileRefused) as exc:
                report.skip(f"{label} : {exc}")
                continue
            slip = existing.get(sha)
            if slip is None:
                self._create_slip(sha, parsed)
            else:
                self._update_slip(slip, parsed, lines_here.get(slip.pk, []))

    @staticmethod
    def _slip_label(record: dict) -> str:
        reading = record.get("reading")
        if isinstance(reading, dict):
            number = reading.get("number") if isinstance(reading.get("number"), str) else ""
            try:
                delivery = codec.load(Slip, "delivery_date", reading.get("delivery_date"))
            except codec.FieldValueError:
                delivery = None
            if number or delivery:
                return slip_label(number[:40], delivery)
        return f"Bon {record['sha256'][:12]}…"

    def _parse_slip(self, record: dict, label: str, formats: dict) -> _Slip:
        format_name = record.get("format")
        if not isinstance(format_name, str) or not format_name.strip():
            raise codec.FieldValueError("format de bon manquant")
        fmt = formats.get(name_key(format_name))
        if fmt is None:
            raise codec.FieldValueError(f"format de bon inconnu « {format_name} »")
        values = {
            name: codec.load(Slip, name, record[name]) for name in SLIP_FIELDS if name in record and name != "sha256"
        }
        if "mail_date" in values:
            check_date("mail_date", values["mail_date"], future_days=MAIL_FUTURE_DAYS)
        values["origin"] = codec.load(Slip, "origin", record.get("origin"))
        ref = self._ref(record["file"]) if "file" in record else NOT_SAID
        parsed = _Slip(label=label, format=fmt, values=values, record=record, ref=ref)
        if "reading" in record:
            parsed.reading, parsed.lines = self._parse_reading(record["reading"])
        return parsed

    def _parse_reading(self, reading) -> tuple[dict, list]:
        """The reading as returnables.slips writes it, or « lecture
        illisible »: a reference list the pages cannot read, checks that
        are not checks, a line whose figures do not fit its columns."""
        illegible = "lecture illisible"
        if not isinstance(reading, dict):
            raise codec.FieldValueError(illegible)
        codec.note_unknown(self._report, reading, READING_KEYS, where="bons › lecture › ")
        try:
            values = {name: codec.load(Slip, name, reading[name]) for name in READING_FIELDS if name in reading}
            if "delivery_date" in values:
                check_date("delivery_date", values["delivery_date"], future_days=FUTURE_DAYS)
        except codec.FieldValueError as exc:
            raise codec.FieldValueError(f"{illegible} — {exc}") from None
        if "references" in values and not _readable_references(values["references"]):
            raise codec.FieldValueError(illegible)
        if "checks" in values and not _readable_checks(values["checks"]):
            raise codec.FieldValueError(illegible)
        items = reading.get("lines", [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise codec.FieldValueError(illegible)
        lines = []
        for item in items:
            codec.note_unknown(self._report, item, LINE_FIELDS, where="bons › lignes › ")
            try:
                line = {name: codec.load(SlipLine, name, item.get(name)) for name in LINE_FIELDS}
            except codec.FieldValueError as exc:
                raise codec.FieldValueError(f"{illegible} — {exc}") from None
            if (
                not line["designation"].strip()
                or abs(line["quantity"]) > MAX_QUANTITY
                or line["position"] > MAX_POSITION
            ):
                raise codec.FieldValueError(illegible)
            lines.append(line)
        return values, lines

    def _create_slip(self, sha: str, parsed: _Slip) -> None:
        report = self._report
        ref = parsed.ref
        if isinstance(ref, _Missing):
            report.skip(f"{parsed.label} : fichier absent du disque à l'export (« {ref.name} »)")
            return
        if not isinstance(ref, dict):
            # The pages show a slip's PDF: a slip without one is not stored.
            report.skip(f"{parsed.label} : sans fichier dans l'archive")
            return
        written: list[str] = []
        try:
            (name,) = self._store([ref], written)
        except FileRefused as exc:
            self._unstore(written)
            report.skip(f"{parsed.label} : {exc}")
            return
        slip = Slip(
            format=parsed.format,
            sha256=sha,
            file=name,
            **{key: value for key, value in parsed.values.items() if key != "received_at"},
            **(parsed.reading or {}),
        )
        slip.save()
        _restore([(slip, parsed.values.get("received_at"))], "received_at")
        SlipLine.objects.bulk_create(SlipLine(slip=slip, **line) for line in parsed.lines)
        report.created(SLIPS)
        if parsed.lines:
            report.created(LINES, len(parsed.lines))

    def _update_slip(self, slip: Slip, parsed: _Slip, lines: list) -> None:
        report = self._report
        different = (["format"] if slip.format_id != parsed.format.pk else []) + codec.differences(
            slip, parsed.record, SLIP_COMPARED
        )
        state = self._file_state(parsed.ref, slip.file.name)
        if state == "differs":
            different.append("file")
        if self._replacing and different:
            self._replace_slip(slip, parsed, lines, different, state)
            return
        if state == "fill":
            written: list[str] = []
            try:
                (name,) = self._store([parsed.ref], written)
            except FileRefused as exc:
                self._unstore(written)
                report.skip(f"{parsed.label} : {exc}")
                return
            slip.file = name
            slip.save(update_fields=["file"])
            report.updated(SLIPS)
            report.note(f"{parsed.label} : fichier restauré")
        if different:
            report.conflict(f"{parsed.label} : différent dans l'archive ({said(different, LABELS)}) — gardé tel quel")
            return
        if state != "fill":
            report.unchanged(SLIPS)
        if lines:
            report.unchanged(LINES, len(lines))

    def _replace_slip(self, slip: Slip, parsed: _Slip, lines: list, different: list, state: str) -> None:
        """The slip becomes the archive's. Its reading goes with it only when
        the text or the format changed: read from the same text by the same
        format, this database's reading stands."""
        report = self._report
        old = ""
        if state in ("fill", "differs"):
            written: list[str] = []
            try:
                (name,) = self._store([parsed.ref], written)
            except FileRefused as exc:
                self._unstore(written)
                report.skip(f"{parsed.label} : {exc}")
                return
            if slip.file.name != name:
                old = slip.file.name
            slip.file = name
        codec.assign(slip, parsed.record, SLIP_COMPARED)
        reread = parsed.reading is not None and ("text" in different or "format" in different)
        slip.format = parsed.format
        if reread:
            for name, value in parsed.reading.items():
                setattr(slip, name, value)
        slip.save()
        report.updated(SLIPS)
        if old:
            self._ctx.delete_file_on_commit(old)
        here = [{name: getattr(line, name) for name in LINE_FIELDS} for line in lines]
        if not reread or here == parsed.lines:
            if lines:
                report.unchanged(LINES, len(lines))
            return
        _delete_ids(SlipLine, [line.pk for line in lines])
        SlipLine.objects.bulk_create(SlipLine(slip=slip, **line) for line in parsed.lines)
        if lines:
            report.deleted(LINES, len(lines))
        if parsed.lines:
            report.created(LINES, len(parsed.lines))

    # .. prune ......................................................................
    def prune(self, ctx, report) -> None:
        """What the archive does not name goes, in the order the rows hold
        one another: pickups (their counts and photos), slips (their lines),
        formats, types. A format or a type something kept still uses stays,
        said."""
        pickups = [
            pk for pk, reference in Pickup.objects.values_list("pk", "reference") if reference not in self._references
        ]
        if pickups:
            names, photos = [], 0
            for image, thumb in PickupPhoto.objects.filter(pickup_id__in=pickups).values_list("image", "thumb"):
                names.extend(name for name in (image, thumb) if name)
                photos += 1
            _delete_ids(Pickup, pickups)
            report.deleted(PICKUPS, len(pickups))
            if photos:
                report.deleted(PHOTOS, photos)
            for name in names:
                ctx.delete_file_on_commit(name)
        slips = [
            (pk, name) for pk, sha, name in Slip.objects.values_list("pk", "sha256", "file") if sha not in self._shas
        ]
        if slips:
            lines = SlipLine.objects.filter(slip_id__in=[pk for pk, _name in slips]).count()
            _delete_ids(Slip, [pk for pk, _name in slips])
            report.deleted(SLIPS, len(slips))
            if lines:
                report.deleted(LINES, lines)
            for _pk, name in slips:
                ctx.delete_file_on_commit(name)
        self._prune_named(
            report,
            SlipFormat,
            self._claimed_formats,
            FORMATS,
            "slips",
            lambda name, n: f"Format de bon « {name} » : encore utilisé par {plural(n, 'bon')}",
        )
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
            _delete_ids(model, doomed)
            report.deleted(entity, len(doomed))

    # -- clear --------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        """Everything count() counts, seeds included, in the order the rows
        hold one another; the files once the transaction commits."""
        names = sorted(named_files())
        deleted = {}
        for entity, model, children in (
            (PICKUPS, Pickup, ((PHOTOS, PickupPhoto),)),
            (SLIPS, Slip, ((LINES, SlipLine),)),
            (FORMATS, SlipFormat, ()),
            (TYPES, ReturnableType, ()),
        ):
            _total, per_model = model.objects.all().delete()
            deleted[entity] = per_model.get(model._meta.label, 0)
            for child_entity, child in children:
                deleted[child_entity] = per_model.get(child._meta.label, 0)
        for entity in ENTITIES:
            report.deleted(entity, deleted[entity])
        for name in names:
            ctx.delete_file_on_commit(name)
        if deleted[TYPES] or deleted[FORMATS]:
            report.note(CLEAR_NOTE)
