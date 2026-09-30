"""« Consignes » in « Données » (spec §8, §10.2 of the transfer spec).

What these guard is what nothing rebuilds: how many kegs went back on which
day, the photos taken before the lorry left, and the bons the driver sent.
A round trip brings every one back exactly - photos and PDFs byte for byte,
under the same names -, importing its own export changes nothing, a merge
never overwrites a reprise edited here, and a bon's reading is copied,
never compared and never read again.

And the regex guard holds on this door too: a motif from an archive goes
through `returnables.patterns` exactly as the forms' do. The refusals are
proven with `regex.compile` replaced by a sentinel that fails if it is
called - nothing that could allocate gigabytes is ever compiled here.

Every name, number, date, count and file below is invented; the photos are
JPEGs of a few pixels.
"""

import hashlib
import os
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import regex
from django.core.files.storage import default_storage
from django.test import TestCase
from django.utils import timezone

from invoices.models import Invoice, Supplier
from returnables import patterns
from returnables.models import (
    Pickup,
    PickupCount,
    PickupPhoto,
    ReturnableType,
    Slip,
    SlipFormat,
    SlipLine,
)
from returnables.tests.support import (
    CO2_LINE,
    CRATE_LINE,
    KEG_LINE,
    UBA_MOTIFS,
    make_format,
    make_pickup,
    make_slip,
    make_type,
    no_defaults,
    tiny_jpeg,
    uba,
)
from returnables.tests.test_patterns import NeverCompile
from tests.factories import make_invoice, make_supplier
from transfer import codec, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections import consignes as section
from transfer.sections.base import FileRefused, ImportContext, Strategy
from transfer.sections.consignes import (
    CLEAR_NOTE,
    ENTITIES,
    FORMATS,
    LINES,
    MEGABYTES,
    PHOTOS,
    PICKUPS,
    SLIPS,
    TYPES,
    ConsignesSection,
    check_format_motifs,
)
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    media_listing,
    round_trip,
)
from transfer.tests.test_invoices_section import MediaMixin, media_names, sha, store

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
UTC = dt_timezone.utc
D = Decimal
KEY = "consignes"

#: What the fixture holds, per report row.
HELD = {TYPES: 4, FORMATS: 3, PICKUPS: 2, PHOTOS: 2, SLIPS: 2, LINES: 3}


def moment(day, hour=9, minute=0, second=0, micro=0) -> datetime:
    return datetime(2026, 2, day, hour, minute, second, micro, tzinfo=UTC)


def at(obj, **values):
    """Set fields with update(): auto_now_add and auto_now would overwrite
    a moment given to save()."""
    type(obj).objects.filter(pk=obj.pk).update(**values)
    obj.refresh_from_db()
    return obj


def consignes_files() -> set[str]:
    names = {name for pair in PickupPhoto.objects.values_list("image", "thumb") for name in pair if name}
    return names | {name for name in Slip.objects.values_list("file", flat=True) if name}


def shas() -> dict[str, str]:
    return {name: sha(name) for name in consignes_files()}


def tally(report, entity) -> tuple:
    counted = report.tallies[entity]
    return counted.created, counted.updated, counted.deleted, counted.unchanged


def run_import(reader, strategy, *, preview=False):
    """« Consignes » with `strategy`; whatever else the archive holds (the
    suppliers the export closure took) merged, as the page does."""
    strategies = {key: MERGE for key in reader.sections}
    strategies[KEY] = strategy
    return import_archive(reader, strategies, preview=preview)


def clear_consignes():
    with TestCase.captureOnCommitCallbacks(execute=True):
        run_clear({KEY}, preview=False)


def payload_of(*, types=(), formats=(), pickups=(), slips=(), supplier_names=None) -> dict:
    return {
        "supplier_names": supplier_names or {},
        "types": list(types),
        "formats": list(formats),
        "pickups": list(pickups),
        "slips": list(slips),
    }


class ConsignesData(MediaMixin):
    """The seeds (three types, the UBA format), one type, two formats and
    two reprises of our own - one with two photos, one « fournisseur non
    précisé » - and two bons, one sent by mail and replacing another. Every
    moment is set in the past: a round trip that forgot to restore one
    would show the moment of the import instead."""

    def setUp(self):
        super().setUp()
        self.uba = uba()
        self.brewer = make_supplier(code="BRASSERIE_ESSAI", name="Brasserie Essai")
        self.pallets = at(make_type("Palettes", position=4, slip_patterns="PALETTE"), created_at=moment(1))
        self.brewer_format = at(
            make_format("Bon Brasserie Essai", supplier=self.brewer, sender_pattern="", subject_pattern=""),
            created_at=moment(1, 10),
        )
        self.wholesaler_format = at(
            make_format("Bon Grossiste Essai", supplier=self.brewer, is_active=False, sender_pattern="",
                        subject_pattern=""),
            created_at=moment(1, 11),
        )
        self.pickup = at(
            make_pickup(date=date(2026, 2, 10), counts={"Fûts": 15, "Bouteilles CO2": 1}, photos=2,
                        note="Deux fûts cabossés"),
            created_at=moment(10, 7, 55, 3, 125000),
        )
        for rank, photo in enumerate(self.pickup.photos.order_by("id")):
            at(photo, created_at=moment(10, 7, 56 + rank), taken_at=moment(10, 7, 50 + rank))
        self.other = at(
            make_pickup(date=date(2026, 2, 11), supplier=None, counts={"Caisses verre": 3}, note="Livreur pressé"),
            created_at=moment(11, 8),
        )
        self.slip = at(
            make_slip(
                lines=(KEG_LINE, CO2_LINE), number="1201", references=["900001"], delivery_date=date(2026, 2, 10),
                printed_at=moment(10, 7, 40, 2), remarks="REPRISE MARCHANDISE",
                checks=[{"label": "Partie des consignes trouvée", "passed": True, "detail": ""}],
            ),
            received_at=moment(10, 9, 0, 0, 250000),
            read_at=moment(10, 9, 1),
        )
        self.mailed = at(
            make_slip(
                self.brewer_format, lines=(CRATE_LINE,), number="1202", references=["900002"],
                delivery_date=date(2026, 2, 11), replaces=True, origin=Slip.Origin.MAIL,
                mail_sender="livreur@brasserie-essai.example", mail_subject="Livraison du 11/02/2026",
                mail_date=date(2026, 2, 11),
            ),
            received_at=moment(11, 9),
        )
        section._SIZES.clear()

    def export(self) -> ArchiveReader:
        """What the page exports when « Consignes » is ticked: the suppliers
        it requires with it."""
        reader = export_archive(registry.closure({KEY}, "export"))
        self.addCleanup(reader.close)
        return reader

    def forged(self, change) -> ArchiveReader:
        reader = ArchiveReader(forge(self.export(), consignes=change))
        self.addCleanup(reader.close)
        return reader

    def photo(self, rank=0) -> PickupPhoto:
        return self.pickup.photos.order_by("created_at", "id")[rank]


def edit(kind: str, match, change):
    """A change to one record of the archive's consignes.json: `match(record)`
    picks it, `change(record)` edits it in place."""

    def apply(payload):
        for record in payload[kind]:
            if match(record):
                change(record)
        return payload

    return apply


def first_pickup(record) -> bool:
    return record["date"] == "2026-02-10"


def first_slip(record) -> bool:
    return record["reading"]["number"] == "1201"


# -- the contract ---------------------------------------------------------------------------


class ContractTests(TestCase):
    def test_every_field_is_exported_or_said_why_not(self):
        # A field added to one of these models later cannot be left out of
        # the archive in silence.
        self.assertEqual(set(section.EXPORTED), set(section.NOT_EXPORTED))
        for model, exported in section.EXPORTED.items():
            with self.subTest(model=model.__name__):
                concrete = {model_field.name for model_field in model._meta.concrete_fields}
                self.assertEqual(concrete, set(exported) | set(section.NOT_EXPORTED[model]))
                self.assertFalse(set(exported) & set(section.NOT_EXPORTED[model]))
        self.assertEqual(section.NOT_EXPORTED[Pickup]["updated_at"], "modifiée ici")
        self.assertEqual(section.NOT_EXPORTED[Slip]["read_at"], "relue ici")

    def test_the_section_is_registered_under_its_key(self):
        self.assertIsInstance(registry.get(KEY), ConsignesSection)
        self.assertIn(KEY, registry.SECTION_MODULES)

    def test_the_database_fingerprint_sees_the_consignes_tables(self):
        """Every « changes nothing » test of this file rests on it: without
        « returnables » in its apps, they passed whatever the import wrote."""
        pickup = make_pickup()
        before = db_fingerprint()
        Pickup.objects.filter(pk=pickup.pk).update(note="modifiée")
        self.assertNotEqual(db_fingerprint(), before)

    def test_count_of_an_empty_section(self):
        no_defaults()
        section._SIZES.clear()
        self.assertEqual(ConsignesSection().count(), dict.fromkeys((*ENTITIES, MEGABYTES), 0))

    def test_the_seeds_are_counted_like_the_rest(self):
        section._SIZES.clear()
        self.assertEqual(
            ConsignesSection().count(),
            {TYPES: 3, FORMATS: 1, PICKUPS: 0, PHOTOS: 0, SLIPS: 0, LINES: 0, MEGABYTES: 0},
        )


class ExportTests(ConsignesData, TestCase):
    def test_counts_are_the_archives_under_the_same_labels(self):
        expected = {**HELD, MEGABYTES: 0}
        self.assertEqual(ConsignesSection().count(), expected)
        self.assertEqual(self.export().counts(KEY), expected)

    def test_what_a_reprise_holds(self):
        payload = self.export().section(KEY).payload()
        record = next(item for item in payload["pickups"] if first_pickup(item))
        self.assertEqual(record["reference"], self.pickup.reference)
        self.assertEqual(record["supplier"], "UBA")
        self.assertEqual(record["note"], "Deux fûts cabossés")
        self.assertEqual(record["created_at"], "2026-02-10T07:55:03.125000+00:00")
        self.assertEqual(record["counts"], [{"type": "Fûts", "quantity": 15}, {"type": "Bouteilles CO2", "quantity": 1}])
        self.assertEqual(len(record["photos"]), 2)
        photo = record["photos"][0]
        self.assertEqual(set(photo), {"image", "thumb", "taken_at", "width", "height", "created_at"})
        self.assertTrue(photo["image"]["name"].startswith("consignes/photos/"))
        self.assertEqual(photo["taken_at"], "2026-02-10T07:50:00+00:00")
        self.assertNotIn("updated_at", record)
        self.assertIsNone(next(item for item in payload["pickups"] if not first_pickup(item))["supplier"])

    def test_what_a_bon_holds(self):
        payload = self.export().section(KEY).payload()
        record = next(item for item in payload["slips"] if first_slip(item))
        self.assertEqual(record["format"], "UBA \N{EM DASH} bon du livreur")
        self.assertEqual(record["sha256"], self.slip.sha256)
        self.assertTrue(record["file"]["name"].startswith("consignes/bons/"))
        self.assertEqual(record["received_at"], "2026-02-10T09:00:00.250000+00:00")
        self.assertNotIn("read_at", record)
        reading = record["reading"]
        self.assertEqual(
            (reading["number"], reading["references"], reading["printed_total"], reading["replaces"]),
            ("1201", ["900001"], "-175.00", False),
        )
        self.assertEqual(reading["checks"], [{"label": "Partie des consignes trouvée", "passed": True, "detail": ""}])
        self.assertEqual(
            reading["lines"][0],
            {"position": 1, "designation": KEG_LINE[0], "quantity": 3, "unit_amount": "30.0000", "amount": "90.00"},
        )

    def test_records_are_named_by_key_never_by_pk(self):
        payload = self.export().section(KEY).payload()

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

    def test_the_suppliers_it_names_come_with_their_names(self):
        payload = self.export().section(KEY).payload()
        self.assertEqual(payload["supplier_names"], {"BRASSERIE_ESSAI": "Brasserie Essai", "UBA": self.uba.name})

    def test_file_sizes_are_kept_a_minute_per_espace(self):
        """count() is drawn on every visit of /donnees/: the stat calls are
        kept, per espace - two espaces restored from one archive name the
        same files."""
        getsize = os.path.getsize
        with mock.patch("transfer.sections.consignes.os.path.getsize", wraps=getsize) as spy:
            ConsignesSection().count()
            first = spy.call_count
            self.assertEqual(first, 6)  # two photos and their thumbnails, two bons
            ConsignesSection().count()
            self.assertEqual(spy.call_count, first)
            with mock.patch("transfer.sections.consignes.tenant_key", return_value="un-autre-espace"):
                ConsignesSection().count()
            self.assertEqual(spy.call_count, 2 * first)


# -- round trip, idempotence --------------------------------------------------------------------


class RoundTripTests(ConsignesData, TestCase):
    def setUp(self):
        super().setUp()
        self.files = shas()
        self.assertEqual(len(self.files), 6)

    def _after_clear(self):
        section._SIZES.clear()
        self.assertEqual(ConsignesSection().count(), dict.fromkeys((*ENTITIES, MEGABYTES), 0))
        self.assertFalse(media_names() & set(self.files))
        # The suppliers are not this section's.
        self.assertTrue(Supplier.objects.filter(code="BRASSERIE_ESSAI").exists())

    def _check(self, strategy):
        before, after = round_trip({KEY}, strategy, after_clear=self._after_clear)
        self.assertEqual(list(after), ["fournisseurs", KEY])
        self.assertEqual(after[KEY], before[KEY])
        # Byte for byte, under the same names.
        self.assertEqual(shas(), self.files)
        # Its moments, put back after the insert.
        pickup = Pickup.objects.get(reference=self.pickup.reference)
        self.assertEqual(pickup.created_at, moment(10, 7, 55, 3, 125000))
        self.assertEqual(Slip.objects.get(sha256=self.slip.sha256).received_at, moment(10, 9, 0, 0, 250000))
        self.assertEqual(ReturnableType.objects.get(name="Palettes").created_at, moment(1))
        # Never exported: read here, not there.
        self.assertIsNone(Slip.objects.get(sha256=self.slip.sha256).read_at)

    def test_merge(self):
        self._check(MERGE)

    def test_replace(self):
        self._check(REPLACE)


class IdempotenceTests(ConsignesData, TestCase):
    def assert_nothing_moves(self, run):
        report = run.section(KEY)
        for entity, number in HELD.items():
            with self.subTest(entity=entity):
                self.assertEqual(tally(report, entity), (0, 0, 0, number))
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))
        self.assertFalse(run.affected())

    def test_merging_its_own_export_changes_nothing(self):
        reader = self.export()
        before, files = db_fingerprint(), media_listing()
        self.assert_nothing_moves(run_import(reader, MERGE))
        self.assertEqual((db_fingerprint(), media_listing()), (before, files))

    def test_replacing_with_its_own_export_changes_nothing(self):
        reader = self.export()
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            self.assert_nothing_moves(run_import(reader, REPLACE))
        self.assertEqual((db_fingerprint(), media_listing(), callbacks), (before, files, []))

    def test_the_same_records_made_at_other_moments_are_unchanged(self):
        """An export taken on another computer, a day later: the moments
        differ, the records do not (never compared)."""
        reader = self.export()
        later = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        for model in (ReturnableType, SlipFormat, Pickup, PickupPhoto):
            model.objects.update(created_at=later)
        Slip.objects.update(received_at=later)
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                self.assert_nothing_moves(run_import(reader, strategy))
                self.assertEqual(db_fingerprint(), before)


# -- merge versus replace -------------------------------------------------------------------------


class MergeAndReplaceTests(ConsignesData, TestCase):
    """The database moved on after the export: of each kind of record, one
    changed here, one only here, one only in the archive."""

    def setUp(self):
        super().setUp()
        self.before = ConsignesSection().snapshot()
        self.reader = self.export()
        # Types.
        ReturnableType.objects.filter(name="Caisses verre").update(slip_patterns="CAISSE|CASIER")
        self.barrels = make_type("Tonneaux", slip_patterns="TONNEAU")
        self.pallets.delete()
        # Formats.
        SlipFormat.objects.filter(pk=self.brewer_format.pk).update(is_active=False)
        self.cellar_format = make_format("Bon Cave Essai", supplier=self.brewer, sender_pattern="", subject_pattern="")
        self.wholesaler_format.delete()
        # Reprises (and a photo only here).
        PickupCount.objects.filter(pickup=self.pickup, returnable_type__name="Fûts").update(quantity=14)
        self.extra = make_pickup(date=date(2026, 2, 12), counts={"Fûts": 2}, photos=1)
        self.extra_files = {name for pair in self.extra.photos.values_list("image", "thumb") for name in pair}
        self.other.delete()
        # Bons.
        Slip.objects.filter(pk=self.mailed.pk).update(mail_subject="Livraison du 11/02/2026 (renvoi)")
        self.late = make_slip(lines=(KEG_LINE,), number="1203")
        self.late_file = self.late.file.name
        self.slip.delete()  # its PDF stays on disk: found again, reused

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = run_import(self.reader, MERGE)
        report = run.section(KEY)
        self.assertEqual(tally(report, TYPES), (1, 0, 0, 2))
        self.assertEqual(tally(report, FORMATS), (1, 0, 0, 1))
        self.assertEqual(tally(report, PICKUPS), (1, 0, 0, 0))
        self.assertEqual(tally(report, PHOTOS), (0, 0, 0, 0))
        self.assertEqual(tally(report, SLIPS), (1, 0, 0, 0))
        self.assertEqual(tally(report, LINES), (2, 0, 0, 0))
        self.assertEqual(
            report.conflicts,
            [
                "Type de consigne « Caisses verre » : différent dans l'archive (motifs des bons) — gardé tel quel",
                "Format de bon « Bon Brasserie Essai » : différent dans l'archive (actif) — gardé tel quel",
                "Reprise du 10/02/2026 : différente dans l'archive (nombres) — gardée telle quelle",
                "Bon n° 1202 du 11/02/2026 : différent dans l'archive (objet du mail) — gardé tel quel",
            ],
        )
        # Kept as they are here.
        self.assertEqual(ReturnableType.objects.get(name="Caisses verre").slip_patterns, "CAISSE|CASIER")
        self.assertFalse(SlipFormat.objects.get(pk=self.brewer_format.pk).is_active)
        self.assertEqual(self.pickup.counts.get(returnable_type__name="Fûts").quantity, 14)
        self.assertEqual(Slip.objects.get(pk=self.mailed.pk).mail_subject, "Livraison du 11/02/2026 (renvoi)")
        # Only here: untouched.
        for model, pk in ((ReturnableType, self.barrels.pk), (SlipFormat, self.cellar_format.pk),
                          (Pickup, self.extra.pk), (Slip, self.late.pk)):
            self.assertTrue(model.objects.filter(pk=pk).exists(), model.__name__)
        # Only in the archive: back.
        self.assertTrue(ReturnableType.objects.filter(name="Palettes").exists())
        self.assertTrue(SlipFormat.objects.filter(name="Bon Grossiste Essai").exists())
        self.assertEqual(Pickup.objects.get(reference=self.other.reference).counts.get().quantity, 3)
        self.assertEqual(Slip.objects.get(sha256=self.slip.sha256).lines.count(), 2)
        # A merge updates and deletes nothing: no safety archive is needed.
        self.assertFalse(run.affected())

    def test_replace_makes_the_section_exactly_the_archive(self):
        with self.captureOnCommitCallbacks(execute=True):
            run = run_import(self.reader, REPLACE)
        report = run.section(KEY)
        self.assertEqual(tally(report, TYPES), (1, 1, 1, 2))
        self.assertEqual(tally(report, FORMATS), (1, 1, 1, 1))
        self.assertEqual(tally(report, PICKUPS), (1, 1, 1, 0))
        self.assertEqual(tally(report, PHOTOS), (0, 0, 1, 2))
        self.assertEqual(tally(report, SLIPS), (1, 1, 1, 0))
        self.assertEqual(tally(report, LINES), (2, 0, 1, 1))
        self.assertEqual((report.conflicts, report.skipped, report.kept), ([], [], []))
        self.assertEqual(ConsignesSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {KEY})
        # The files of what was pruned went, on commit.
        self.assertFalse(self.extra_files & media_names())
        self.assertFalse(default_storage.exists(self.late_file))

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = run_import(self.reader, REPLACE, preview=True)
        self.assertEqual((db_fingerprint(), media_listing(), callbacks), (before, files, []))
        confirmed = run_import(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertEqual(preview.section(KEY).notes, confirmed.section(KEY).notes)
        self.assertNotEqual(db_fingerprint(), before)

    def test_a_merge_preview_says_what_the_merge_does(self):
        before = db_fingerprint()
        preview = run_import(self.reader, MERGE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(preview.outcome(), run_import(self.reader, MERGE).outcome())

    def test_a_format_or_a_type_something_kept_still_uses_stays_and_is_said(self):
        """A bon the archive names but could not read keeps its format here,
        and a count its type: PROTECT, said rather than a crash."""
        reader = ArchiveReader(
            forge(
                self.reader,
                consignes=lambda payload: {
                    **payload,
                    "types": [record for record in payload["types"] if record["name"] != "Fûts"],
                    "formats": [record for record in payload["formats"] if record["name"] != "Bon Brasserie Essai"],
                },
            )
        )
        self.addCleanup(reader.close)
        report = run_import(reader, REPLACE).section(KEY)
        self.assertIn("Type de consigne « Fûts » : encore compté dans 1 reprise", report.kept)
        self.assertIn("Format de bon « Bon Brasserie Essai » : encore utilisé par 1 bon", report.kept)
        self.assertTrue(ReturnableType.objects.filter(name="Fûts").exists())


# -- keys -----------------------------------------------------------------------------------------


class KeyTests(ConsignesData, TestCase):
    def test_a_type_is_found_by_its_name_whatever_its_accents_case_and_spaces(self):
        """The type form refuses « Futs » beside « Fûts »: an archive's
        « Futs » is this database's « Fûts », never a twin created beside it."""
        reader = self.forged(edit("types", lambda record: record["name"] == "Fûts", lambda record: record.update(name="Futs")))
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn("Type de consigne « Futs » : différent dans l'archive (nom) — gardé tel quel", report.conflicts)
        self.assertEqual(ReturnableType.objects.count(), 4)
        report = run_import(reader, REPLACE).section(KEY)
        self.assertEqual(tally(report, TYPES)[:3], (0, 1, 0))
        self.assertTrue(ReturnableType.objects.filter(name="Futs").exists())
        self.assertEqual(ReturnableType.objects.count(), 4)
        # Its counts followed it: the same row.
        self.assertEqual(self.pickup.counts.get(returnable_type__name="Futs").quantity, 15)

    def test_a_reprise_edited_here_is_still_the_same_reprise(self):
        """Its key is its reference, not its date or its counts: a reprise
        corrected after the export is a conflict, never a second one."""
        reader = self.export()
        Pickup.objects.filter(pk=self.pickup.pk).update(date=date(2026, 2, 9))
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn("Reprise du 10/02/2026 : différente dans l'archive (date) — gardée telle quelle", report.conflicts)
        self.assertEqual(Pickup.objects.count(), 2)

    def test_a_supplier_under_another_code_is_found_by_its_name(self):
        def change(payload):
            payload["supplier_names"]["UBA_AILLEURS"] = self.uba.name
            for record in payload["pickups"]:
                if first_pickup(record):
                    record["supplier"] = "UBA_AILLEURS"
            return payload

        reader = ArchiveReader(forge(self.export(), consignes=change))
        self.addCleanup(reader.close)
        clear_consignes()
        run_import(reader, MERGE)
        self.assertEqual(Pickup.objects.get(reference=self.pickup.reference).supplier, self.uba)


# -- a bon's reading --------------------------------------------------------------------------------


class ReadingTests(ConsignesData, TestCase):
    def test_the_reading_comes_back_exactly_and_is_read_again_by_nothing(self):
        reader = self.export()
        clear_consignes()
        with (
            mock.patch("returnables.reading.read_slip_text", side_effect=AssertionError("relu")),
            mock.patch("returnables.reading.pdf_text", side_effect=AssertionError("relu")),
        ):
            report = run_import(reader, MERGE).section(KEY)
        self.assertEqual(report.skipped, [])
        slip = Slip.objects.get(sha256=self.slip.sha256)
        self.assertEqual(
            (slip.number, slip.references, slip.printed_total, slip.printed_at, slip.remarks, slip.read_error),
            ("1201", ["900001"], D("-175.00"), moment(10, 7, 40, 2), "REPRISE MARCHANDISE", ""),
        )
        self.assertEqual(slip.checks, [{"label": "Partie des consignes trouvée", "passed": True, "detail": ""}])
        self.assertEqual(
            list(slip.lines.values_list("position", "designation", "quantity", "unit_amount", "amount")),
            [(1, *KEG_LINE), (2, *CO2_LINE)],
        )
        self.assertIsNone(slip.read_at)
        self.assertEqual(Slip.objects.get(sha256=self.mailed.sha256).origin, Slip.Origin.MAIL)

    def test_a_bon_read_again_here_is_unchanged_whatever_its_reading_says(self):
        """The reading is a function of the text and the format's motifs:
        compared, one motif edited here would make every bon a conflict."""
        reader = self.export()
        Slip.objects.filter(pk=self.slip.pk).update(
            number="9999", references=["1"], checks=[{"label": "Numéro lu", "passed": False, "detail": "?"}]
        )
        SlipLine.objects.filter(slip=self.slip).update(designation="AUTRE LECTURE")
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                report = run_import(reader, strategy).section(KEY)
                self.assertEqual(tally(report, SLIPS), (0, 0, 0, 2))
                self.assertEqual(report.conflicts, [])
                self.assertEqual(Slip.objects.get(pk=self.slip.pk).number, "9999")

    def test_replace_copies_the_reading_when_the_text_changed(self):
        reader = self.export()
        Slip.objects.filter(pk=self.slip.pk).update(text="un autre texte", number="9999", printed_total=None)
        SlipLine.objects.filter(slip=self.slip, position=2).delete()
        report = run_import(reader, REPLACE).section(KEY)
        self.assertEqual(tally(report, SLIPS), (0, 1, 0, 1))
        self.assertEqual(tally(report, LINES), (2, 0, 1, 1))
        slip = Slip.objects.get(pk=self.slip.pk)
        self.assertEqual((slip.number, slip.printed_total, slip.lines.count()), ("1201", D("-175.00"), 2))

    def test_replace_keeps_the_reading_when_only_what_was_received_differs(self):
        reader = self.export()
        Slip.objects.filter(pk=self.mailed.pk).update(mail_subject="Objet corrigé", number="9999")
        report = run_import(reader, REPLACE).section(KEY)
        self.assertEqual(tally(report, SLIPS), (0, 1, 0, 1))
        self.assertEqual(tally(report, LINES), (0, 0, 0, 3))
        mailed = Slip.objects.get(pk=self.mailed.pk)
        self.assertEqual((mailed.mail_subject, mailed.number), ("Livraison du 11/02/2026", "9999"))


# -- files ------------------------------------------------------------------------------------------


class FileTests(ConsignesData, TestCase):
    def test_merge_gives_back_a_lost_photo(self):
        reader = self.export()
        photo = self.photo(0)
        name, content = photo.image.name, default_storage.open(photo.image.name, "rb").read()
        default_storage.delete(name)
        report = run_import(reader, MERGE).section(KEY)
        self.assertEqual(report.notes, ["Reprise du 10/02/2026 : 1 photo restaurée"])
        self.assertEqual(sha(name), hashlib.sha256(content).hexdigest())
        photo.refresh_from_db()
        self.assertEqual(photo.image.name, name)
        self.assertEqual(tally(report, PHOTOS), (0, 1, 0, 1))
        self.assertEqual(tally(report, PICKUPS), (0, 0, 0, 2))

    def test_merge_gives_back_a_lost_bon(self):
        reader = self.export()
        name = self.slip.file.name
        content = default_storage.open(name, "rb").read()
        default_storage.delete(name)
        report = run_import(reader, MERGE).section(KEY)
        self.assertEqual(report.notes, ["Bon n° 1201 du 10/02/2026 : fichier restauré"])
        self.assertEqual(sha(name), hashlib.sha256(content).hexdigest())
        self.assertEqual(tally(report, SLIPS), (0, 1, 0, 1))

    def test_a_name_taken_by_another_file_gets_an_available_one_and_the_other_stays(self):
        name = self.photo(0).image.name
        original = sha(name)
        reader = self.export()
        clear_consignes()
        store(name, tiny_jpeg((5, 5)))
        other = sha(name)
        run_import(reader, MERGE)
        photo = Pickup.objects.get(reference=self.pickup.reference).photos.order_by("created_at", "id")[0]
        self.assertNotEqual(photo.image.name, name)
        self.assertTrue(photo.image.name.startswith(name[: -len(".jpg")] + "_"))
        self.assertEqual(sha(photo.image.name), original)
        self.assertEqual(sha(name), other)

    def test_the_same_file_already_stored_is_reused(self):
        reader = self.export()
        names = consignes_files()
        Pickup.objects.all().delete()  # the rows only: their files stay on disk
        Slip.objects.all().delete()
        before = media_names()
        run_import(reader, MERGE)
        self.assertEqual(media_names(), before)
        self.assertEqual(consignes_files(), names)

    def test_replace_swaps_a_changed_photo_and_removes_the_old_file_on_commit(self):
        photo = self.photo(0)
        original, content = photo.image.name, default_storage.open(photo.image.name, "rb").read()
        reader = self.export()
        other = store("consignes/photos/2026/02/autre-essai.jpg", tiny_jpeg((5, 4)))
        PickupPhoto.objects.filter(pk=photo.pk).update(image=other)
        default_storage.delete(original)
        with self.captureOnCommitCallbacks(execute=True):
            report = run_import(reader, REPLACE).section(KEY)
        photo.refresh_from_db()
        self.assertEqual(photo.image.name, original)
        self.assertEqual(sha(photo.image.name), hashlib.sha256(content).hexdigest())
        self.assertFalse(default_storage.exists(other))
        self.assertEqual(tally(report, PHOTOS), (0, 1, 0, 1))
        self.assertEqual(tally(report, PICKUPS), (0, 1, 0, 1))

    def test_a_photo_missing_from_the_other_disk_leaves_the_reprise_without_it_and_says_so(self):
        name = self.photo(1).image.name
        default_storage.delete(name)
        reader = self.export()
        clear_consignes()
        report = run_import(reader, MERGE).section(KEY)
        self.assertEqual(Pickup.objects.get(reference=self.pickup.reference).photos.count(), 1)
        self.assertIn(
            f"Reprise du 10/02/2026 : photo absente du disque à l'export (« {name} ») — reprise importée sans elle",
            report.notes,
        )
        self.assertEqual(tally(report, PHOTOS), (1, 0, 0, 0))

    def test_a_bon_missing_from_the_other_disk_is_skipped_and_said(self):
        name = self.slip.file.name
        default_storage.delete(name)
        reader = self.export()
        clear_consignes()
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn(f"Bon n° 1201 du 10/02/2026 : fichier absent du disque à l'export (« {name} »)", report.skipped)
        self.assertFalse(Slip.objects.filter(sha256=self.slip.sha256).exists())

    def test_a_dangerous_file_name_skips_the_reprise(self):
        names = {name for pair in self.pickup.photos.values_list("image", "thumb") for name in pair}
        reader = self.forged(
            edit("pickups", first_pickup, lambda record: record["photos"][1]["image"].update(name="../../config/essai.jpg"))
        )
        clear_consignes()
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn("Reprise du 10/02/2026 : nom de fichier refusé (« ../../config/essai.jpg »)", report.skipped)
        self.assertFalse(Pickup.objects.filter(reference=self.pickup.reference).exists())
        # Checked before anything was written: not even its first photo.
        self.assertFalse(names & media_names())

    def test_a_file_the_archive_does_not_declare_skips_the_reprise(self):
        reader = self.forged(
            edit("pickups", first_pickup, lambda record: record["photos"][0]["image"].update(
                member="files/consignes/photos/jamais-declaree.jpg"))
        )
        name = self.photo(0).image.name
        clear_consignes()
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn(f"Reprise du 10/02/2026 : fichier absent de l'archive (« {name} »)", report.skipped)

    def test_a_file_outside_the_consignes_folder_skips_the_bon(self):
        """invoices/ is a folder the archive accepts - but not this
        section's: the invoices would count the file as theirs."""
        reader = self.forged(
            edit("slips", first_slip, lambda record: record["file"].update(name="invoices/2026/02/bon-essai.pdf"))
        )
        clear_consignes()
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn(
            "Bon n° 1201 du 10/02/2026 : fichier hors du dossier des consignes (« invoices/2026/02/bon-essai.pdf »)",
            report.skipped,
        )
        self.assertFalse(default_storage.exists("invoices/2026/02/bon-essai.pdf"))

    def test_the_preview_writes_no_file(self):
        reader = self.export()
        clear_consignes()
        files = media_listing()
        preview = run_import(reader, REPLACE, preview=True)
        self.assertEqual(media_listing(), files)
        self.assertEqual(tally(preview.section(KEY), PHOTOS), (2, 0, 0, 0))
        self.assertFalse(Pickup.objects.exists())

    def test_a_refusal_half_way_removes_the_files_already_written(self):
        reader = self.export()
        first = self.photo(0).image.name
        clear_consignes()
        save_file = ImportContext.save_file
        calls = []

        def refused_second(ctx, ref, name):
            calls.append(name)
            if len(calls) == 2:
                raise FileRefused("refusé (essai)")
            return save_file(ctx, ref, name)

        with mock.patch.object(ImportContext, "save_file", refused_second):
            report = run_import(reader, MERGE).section(KEY)
        self.assertIn("Reprise du 10/02/2026 : refusé (essai)", report.skipped)
        self.assertEqual(calls[0], first)
        self.assertFalse(default_storage.exists(first))
        self.assertFalse(Pickup.objects.filter(reference=self.pickup.reference).exists())


# -- refusals ---------------------------------------------------------------------------------------


class RefusalTests(ConsignesData, TestCase):
    def setUp(self):
        super().setUp()
        self.reader = self.export()
        clear_consignes()

    def _import(self, change, strategy=MERGE):
        reader = ArchiveReader(forge(self.reader, consignes=change))
        self.addCleanup(reader.close)
        return run_import(reader, strategy).section(KEY)

    def test_a_file_without_one_of_its_lists_is_refused_whole(self):
        for name in ("types", "formats", "pickups", "slips"):
            with self.subTest(missing=name), self.assertRaisesMessage(
                ArchiveError, f"consignes.json n'a pas de liste « {name} »"
            ):
                self._import(lambda payload, name=name: {key: value for key, value in payload.items() if key != name})

    def test_a_list_of_something_else_than_records_is_refused_whole(self):
        with self.assertRaisesMessage(ArchiveError, "dans consignes.json, « types » ne contient pas que des objets"):
            self._import(lambda payload: {**payload, "types": ["Fûts"]})

    def test_an_unknown_field_is_noted_once(self):
        def change(payload):
            for record in payload["pickups"]:
                record["couleur"] = "vert"
            return payload

        report = self._import(change)
        self.assertEqual(report.notes.count("champ inconnu ignoré : reprises › couleur"), 1)
        self.assertEqual(Pickup.objects.count(), 2)

    def test_an_unknown_supplier_skips_the_format_and_then_its_bons(self):
        report = self._import(
            edit("formats", lambda record: record["name"] == "Bon Brasserie Essai",
                 lambda record: record.update(supplier="INCONNU_ESSAI"))
        )
        self.assertIn("Format de bon « Bon Brasserie Essai » : fournisseur inconnu (« INCONNU_ESSAI »)", report.skipped)
        self.assertIn("Bon n° 1202 du 11/02/2026 : format de bon inconnu « Bon Brasserie Essai »", report.skipped)
        self.assertFalse(SlipFormat.objects.filter(name="Bon Brasserie Essai").exists())

    def test_an_unknown_supplier_skips_the_reprise(self):
        report = self._import(edit("pickups", first_pickup, lambda record: record.update(supplier="INCONNU_ESSAI")))
        self.assertIn("Reprise du 10/02/2026 : fournisseur inconnu (« INCONNU_ESSAI »)", report.skipped)
        self.assertEqual(Pickup.objects.count(), 1)

    def test_a_bad_decimal_skips_the_bon(self):
        report = self._import(
            edit("slips", first_slip, lambda record: record["reading"]["lines"][0].update(unit_amount="30.00001"))
        )
        self.assertIn(
            "Bon n° 1201 du 10/02/2026 : lecture illisible — « unit_amount » : « 30.00001 » a plus de 4 décimales",
            report.skipped,
        )
        self.assertFalse(Slip.objects.filter(sha256=self.slip.sha256).exists())

    def test_a_count_that_is_not_a_whole_number_skips_the_reprise(self):
        report = self._import(edit("pickups", first_pickup, lambda record: record["counts"][0].update(quantity="15")))
        self.assertIn("Reprise du 10/02/2026 : « quantity » : nombre entier attendu (« 15 »)", report.skipped)

    def test_a_count_outside_1_to_9999_skips_the_reprise_before_the_database_refuses_it(self):
        for quantity in (0, 10_000):
            with self.subTest(quantity=quantity):
                report = self._import(
                    edit("pickups", first_pickup, lambda record, q=quantity: record["counts"][0].update(quantity=q))
                )
                self.assertIn(
                    f"Reprise du 10/02/2026 : nombre de « Fûts » hors limites ({quantity}) : de 1 à 9 999", report.skipped
                )
                self.assertFalse(Pickup.objects.filter(reference=self.pickup.reference).exists())

    def test_a_count_of_an_unknown_type_or_of_one_type_twice_skips_the_reprise(self):
        report = self._import(edit("pickups", first_pickup, lambda record: record["counts"][0].update(type="Tonneaux")))
        self.assertIn("Reprise du 10/02/2026 : type de consigne inconnu « Tonneaux »", report.skipped)
        report = self._import(edit("pickups", first_pickup, lambda record: record["counts"][1].update(type="FUTS")))
        self.assertIn("Reprise du 10/02/2026 : « FUTS » compté deux fois", report.skipped)

    def test_the_same_record_twice_is_created_once(self):
        def change(payload):
            payload["pickups"].append(dict(payload["pickups"][0]))
            payload["slips"].append(dict(payload["slips"][0]))
            payload["types"].append({**payload["types"][0], "name": "FUTS"})
            return payload

        report = self._import(change)
        self.assertEqual(len(report.skipped), 3, report.skipped)
        self.assertTrue(all(line.endswith("en double dans l'archive") for line in report.skipped), report.skipped)
        self.assertEqual((Pickup.objects.count(), Slip.objects.count(), ReturnableType.objects.count()), (2, 2, 4))

    def test_a_reading_the_pages_cannot_read_skips_the_bon(self):
        for reading in (
            {"references": "900001"},
            {"references": ["9" * 41]},
            {"references": [str(n) for n in range(21)]},
            {"references": [900001]},
            {"checks": [1]},
            {"checks": [{"label": "Numéro lu", "passed": "oui", "detail": ""}]},
            {"checks": [{"label": "Numéro lu", "passed": True}]},
            {"lines": "FÛT 3 x 30.00"},
            {"lines": [{"position": 1, "designation": "FÛT", "quantity": 100_000, "unit_amount": None, "amount": None}]},
            {"lines": [{"position": 1, "designation": "  ", "quantity": 1, "unit_amount": None, "amount": None}]},
        ):
            with self.subTest(reading=reading):
                report = self._import(
                    edit("slips", first_slip, lambda record, reading=reading: record["reading"].update(reading))
                )
                self.assertIn("Bon n° 1201 du 10/02/2026 : lecture illisible", report.skipped)
                self.assertFalse(Slip.objects.filter(sha256=self.slip.sha256).exists())
        # No reading to name it by: the bon is named by its fingerprint.
        report = self._import(edit("slips", first_slip, lambda record: record.update(reading="lu")))
        self.assertIn(f"Bon {self.slip.sha256[:12]}\N{HORIZONTAL ELLIPSIS} : lecture illisible", report.skipped)


class DateBoundsTests(ConsignesData, TestCase):
    """The reprise form (2000 .. today) and the reader (2000 .. today + 7)
    bound every date; an archive is a file anybody can edit. A date past
    those bounds skips its record, said. Read in, a reprise dated
    9999-12-31 was the newest, drawn first on /consignes/, and the page's
    date arithmetic made it a 500 for good; a bon delivered 0001-01-02 did
    the same to its invoice check, a mail of 0001-01-01 to the gather's
    start."""

    @staticmethod
    def ends(payload):
        edit("pickups", first_pickup, lambda record: record.update(date="9999-12-31"))(payload)
        edit("slips", first_slip, lambda record: record["reading"].update(delivery_date="0001-01-02"))(payload)
        edit("slips", lambda record: record["reading"]["number"] == "1202",
             lambda record: record.update(mail_date="0001-01-01"))(payload)
        return payload

    @staticmethod
    def latest(days: int) -> str:
        return f"{timezone.localdate() + timedelta(days=days):%d/%m/%Y}"

    def test_merged_into_an_empty_section_they_are_skipped_and_the_page_draws(self):
        reader = self.forged(self.ends)
        clear_consignes()
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn(
            "Reprise du 31/12/9999 : « date » : date hors limites (« 9999-12-31 ») : "
            f"entre le 01/01/2000 et le {self.latest(7)}",
            report.skipped,
        )
        self.assertIn(
            "Bon n° 1201 du 02/01/0001 : lecture illisible — « delivery_date » : date hors limites "
            f"(« 0001-01-02 ») : entre le 01/01/2000 et le {self.latest(7)}",
            report.skipped,
        )
        self.assertIn(
            "Bon n° 1202 du 11/02/2026 : « mail_date » : date hors limites (« 0001-01-01 ») : "
            f"entre le 01/01/2000 et le {self.latest(1)}",
            report.skipped,
        )
        self.assertEqual(list(Pickup.objects.values_list("date", flat=True)), [date(2026, 2, 11)])
        self.assertFalse(Slip.objects.exists())
        self.assertEqual(self.client.get("/consignes/").status_code, 200)

    def test_replace_keeps_the_rows_here_as_they_are(self):
        reader = self.forged(self.ends)
        with self.captureOnCommitCallbacks(execute=True):
            report = run_import(reader, REPLACE).section(KEY)
        self.assertEqual(sum("date hors limites" in line for line in report.skipped), 3, report.skipped)
        for row, name, kept in (
            (self.pickup, "date", date(2026, 2, 10)),
            (self.slip, "delivery_date", date(2026, 2, 10)),
            (self.mailed, "mail_date", date(2026, 2, 11)),
        ):
            row.refresh_from_db()
            self.assertEqual(getattr(row, name), kept, name)
        self.assertEqual(self.client.get("/consignes/").status_code, 200)

    def test_the_bounds(self):
        today = date(2026, 9, 29)
        for value, future_days, accepted in (
            (date(2000, 1, 1), 7, True),
            (date(1999, 12, 31), 7, False),
            (date(2026, 10, 6), 7, True),
            (date(2026, 10, 7), 7, False),
            (date(2026, 9, 30), 1, True),
            (date(2026, 10, 1), 1, False),
            (date(1, 1, 1), 1, False),
            (date(9999, 12, 31), 7, False),
        ):
            with self.subTest(value=value, future_days=future_days):
                if accepted:
                    self.assertEqual(section.check_date("date", value, future_days=future_days, today=today), value)
                else:
                    with self.assertRaisesMessage(codec.FieldValueError, "date hors limites"):
                        section.check_date("date", value, future_days=future_days, today=today)
        self.assertIsNone(section.check_date("mail_date", None, future_days=1, today=today))

    def test_rows_already_here_at_the_calendar_s_ends_still_draw_the_page(self):
        """Imported before this check: the page cuts its windows at the
        calendar's ends (comparison.shifted) rather than overflowing."""
        at(self.pickup, date=date(9999, 12, 31))
        at(self.slip, delivery_date=date(1, 1, 2))
        at(self.mailed, mail_date=date(1, 1, 1), delivery_date=date(9999, 12, 30))
        for url in (
            "/consignes/",
            f"/consignes/reprises/{self.pickup.pk}/",
            f"/consignes/bons/{self.slip.pk}/",
            f"/consignes/bons/{self.mailed.pk}/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class NamedButUnreadTests(ConsignesData, TestCase):
    """« Remplacer » deletes only what the archive does not NAME: a record
    it names but could not read is skipped, and its row here stays."""

    def test_replace_never_prunes_a_record_it_skipped(self):
        def change(payload):
            for record in payload["pickups"]:
                if first_pickup(record):
                    record["counts"][0]["quantity"] = 0
            for record in payload["slips"]:
                if first_slip(record):
                    record["reading"]["checks"] = [1]
            for record in payload["types"]:
                if record["name"] == "Palettes":
                    record["slip_patterns"] = "PALETTE("
            return payload

        reader = self.forged(change)
        with self.captureOnCommitCallbacks(execute=True):
            report = run_import(reader, REPLACE).section(KEY)
        self.assertEqual(len(report.skipped), 3, report.skipped)
        self.assertEqual((tally(report, PICKUPS)[2], tally(report, SLIPS)[2], tally(report, TYPES)[2]), (0, 0, 0))
        self.assertTrue(Pickup.objects.filter(pk=self.pickup.pk).exists())
        self.assertTrue(Slip.objects.filter(pk=self.slip.pk).exists())
        self.assertTrue(ReturnableType.objects.filter(pk=self.pallets.pk).exists())
        # And their files.
        self.assertTrue({self.slip.file.name, self.photo(0).image.name} <= media_names())


class MotifRefusalTests(ConsignesData, TestCase):
    """A motif from an archive meets the guard the forms use. Refusals are
    proven with regex.compile replaced by a sentinel that fails if it is
    called: none of these is ever compiled for real."""

    def setUp(self):
        super().setUp()
        self.reader = self.export()
        clear_consignes()

    def _import(self, change):
        reader = ArchiveReader(forge(self.reader, consignes=change))
        self.addCleanup(reader.close)
        return run_import(reader, MERGE).section(KEY)

    def test_a_type_motif_the_guard_refuses_is_never_compiled(self):
        reader = ArchiveReader(
            forge({KEY: payload_of(types=[{"name": "Palettes", "position": 4, "is_active": True,
                                           "slip_patterns": "PALETTE\n(?x)a{1 0 0 0 0}"}])})
        )
        self.addCleanup(reader.close)
        patterns.compile_field(patterns.TYPE_FIELD, "PALETTE")  # line 1, checked (and kept) before the sentinel
        never = NeverCompile()
        with mock.patch.object(regex, "compile", new=never):
            report = import_archive(reader, MERGE).section(KEY)
        self.assertEqual(never.calls, [])
        self.assertIn(
            "Type de consigne « Palettes » : motif refusé : Motifs des bons (ligne 2) — le mode (?x) n'est pas "
            "accepté dans un motif",
            report.skipped,
        )
        self.assertFalse(ReturnableType.objects.filter(name="Palettes").exists())

    def test_a_format_motif_the_guard_refuses_is_never_compiled(self):
        check_format_motifs(UBA_MOTIFS)  # the others, checked (and kept) before the sentinel
        uba_record = next(record for record in self.reader.section(KEY).payload()["formats"] if record["supplier"] == "UBA")
        for motif, reason in (
            ("(?:x{65535}){65535}", "répétition trop grande"),
            ("((a{1000}){1000}){1000}", "répétition trop grande"),
            ("(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
            ("(?x:a{1 0 0})", "le mode (?x) n'est pas accepté"),
            ("a{e<=1}", "accolade"),
            ("(?:(?:(?:x{100,}){100,}){100,}){100,}", "répétition trop grande"),
        ):
            with self.subTest(motif=motif):
                payload = payload_of(
                    formats=[{**uba_record, "name": "Bon piégé", "line_pattern": motif}],
                    supplier_names={"UBA": self.uba.name},
                )
                reader = ArchiveReader(forge({KEY: payload}))
                self.addCleanup(reader.close)
                never = NeverCompile()
                with mock.patch.object(regex, "compile", new=never):
                    report = import_archive(reader, MERGE).section(KEY)
                self.assertEqual(never.calls, [])
                self.assertEqual(len(report.skipped), 1, report.skipped)
                self.assertTrue(
                    report.skipped[0].startswith("Format de bon « Bon piégé » : motif refusé : Motif de ligne — "),
                    report.skipped[0],
                )
                self.assertIn(reason, report.skipped[0])
                self.assertFalse(SlipFormat.objects.filter(name="Bon piégé").exists())

    def test_the_form_rules_hold_for_a_format(self):
        for change, said in (
            ({"line_pattern": ""}, "motif refusé : Motif de ligne — le motif est vide"),
            ({"date_patterns": "  "}, "motif refusé : Motif de date — le motif est vide"),
            ({"line_pattern": r"^(?P<designation>.+?)\s+(?P<quantite>\d+"}, "motif refusé : Motif de ligne — parenthèse non fermée"),
            ({"line_pattern": r"^(?P<designation>.+?)\s+\d+"}, "motif refusé : Motif de ligne — le motif doit contenir"),
            ({"section_start": "x*"}, "motif refusé : Début de la partie — le motif accepte une ligne vide"),
            ({"sender_pattern": ".+@.+"}, "motif refusé : Motif d'expéditeur — Le motif d'expéditeur doit désigner"),
            ({"sender_pattern": "livreur@brasserie-essai\\.example", "subject_pattern": ""},
             "motif refusé : Motif d'objet — obligatoire quand un motif d'expéditeur est donné"),
        ):
            with self.subTest(change=change):
                report = self._import(
                    edit("formats", lambda record: record["name"] == "Bon Brasserie Essai",
                         lambda record, change=change: record.update(change))
                )
                refused = [line for line in report.skipped if line.startswith("Format de bon « Bon Brasserie Essai »")]
                self.assertEqual(len(refused), 1, report.skipped)
                self.assertIn(said, refused[0])
                self.assertFalse(SlipFormat.objects.filter(name="Bon Brasserie Essai").exists())

    def test_a_format_the_forms_accept_is_imported(self):
        report = self._import(
            edit("formats", lambda record: record["name"] == "Bon Brasserie Essai",
                 lambda record: record.update(sender_pattern="livreur@brasserie-essai\\.example",
                                              subject_pattern="^Livraison"))
        )
        self.assertEqual(report.skipped, [])
        self.assertEqual(SlipFormat.objects.get(name="Bon Brasserie Essai").sender_pattern, "livreur@brasserie-essai\\.example")


# -- clear ----------------------------------------------------------------------------------------


class ClearTests(ConsignesData, TestCase):
    def test_clearing_consignes_clears_only_consignes(self):
        self.assertEqual(registry.closure({KEY}, "clear"), {KEY})

    def test_a_preview_changes_nothing(self):
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = run_clear({KEY}, preview=True)
        self.assertEqual((db_fingerprint(), media_listing(), callbacks), (before, files, []))
        confirmed = run_clear({KEY}, preview=False)
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_everything_goes_the_seeds_too_and_nothing_else(self):
        invoice = make_invoice(supplier=self.brewer, invoice_number="B-1")
        run = run_clear({KEY}, preview=False)
        section._SIZES.clear()
        self.assertEqual(ConsignesSection().count(), dict.fromkeys((*ENTITIES, MEGABYTES), 0))
        report = run.section(KEY)
        self.assertEqual({entity: report.tallies[entity].deleted for entity in ENTITIES}, HELD)
        self.assertIn(CLEAR_NOTE, report.notes)
        self.assertEqual(run.affected(), {KEY})
        # The suppliers and the invoices are not this section's.
        self.assertTrue(Supplier.objects.filter(pk=self.brewer.pk).exists())
        self.assertTrue(Invoice.objects.filter(pk=invoice.pk).exists())

    def test_files_go_only_after_the_commit(self):
        names = consignes_files()
        with self.captureOnCommitCallbacks() as callbacks:
            run_clear({KEY}, preview=False)
        self.assertFalse(Pickup.objects.exists())
        self.assertTrue(names <= media_names())
        for callback in callbacks:
            callback()
        self.assertFalse(names & media_names())

    def test_a_failure_half_way_deletes_no_file(self):
        names = consignes_files()
        with (
            self.captureOnCommitCallbacks(execute=True) as callbacks,
            mock.patch("transfer.rebuild.rebuild", side_effect=RuntimeError("échec (essai)")),
            self.assertRaises(RuntimeError),
        ):
            run_clear({KEY}, preview=False)
        self.assertEqual(callbacks, [])
        self.assertEqual(Pickup.objects.count(), 2)
        self.assertTrue(names <= media_names())

    def test_after_a_clear_the_safety_archive_brings_the_seeds_back(self):
        reader = self.export()
        clear_consignes()
        run_import(reader, REPLACE)
        self.assertEqual(
            set(ReturnableType.objects.values_list("name", flat=True)),
            {"Fûts", "Caisses verre", "Bouteilles CO2", "Palettes"},
        )
        self.assertTrue(SlipFormat.objects.filter(name="UBA \N{EM DASH} bon du livreur", supplier=self.uba).exists())
