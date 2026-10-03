"""« Types et formats de consignes » in « Données »: the returnable types and
the slip formats, apart from the pickups and slips of « Consignes »
(test_returnables_section.py).

What these guard: a round trip brings every type and format back exactly,
importing its own export changes nothing, a merge never overwrites one
edited here, and a « Remplacer » keeps a type a pickup still counts and a
format a slip was read with - so a new bar can take them alone, and a bar
can take them again without touching its pickups. And an archive written
before this section existed, which kept them in consignes.json, still
brings them back (`archive.CARVED`).

And the regex guard holds on this door too: a pattern from an archive goes
through `returnables.patterns` exactly as the forms' do. The refusals are
proven with `regex.compile` replaced by a sentinel that fails if it is
called - nothing that could allocate gigabytes is ever compiled here.

Every name, number, date, pattern and file below is invented.
"""

import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import regex
from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from invoices import coverage
from invoices.models import GatherCoverage, Supplier
from returnables import patterns
from returnables.forms import TypeForm
from returnables.mail import fetch_start
from returnables.models import Pickup, ReturnableType, Slip, SlipFormat
from returnables.tests.support import (
    UBA_PATTERNS,
    make_format,
    make_pickup,
    make_slip,
    make_type,
    no_defaults,
    uba,
)
from returnables.tests.test_patterns import NeverCompile
from tests.factories import make_supplier
from transfer import archive, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.registry import INFO
from transfer.runner import run_clear
from transfer.sections import returnable_types as section
from transfer.sections import returnables
from transfer.sections.base import Group, Strategy
from transfer.sections.returnable_types import (
    CLEAR_NOTE,
    ENTITIES,
    FORMATS,
    TYPES,
    ReturnableTypesSection,
    check_format_patterns,
)
from transfer.sections.returnables import LINES, PHOTOS, PICKUPS, SLIPS
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    media_listing,
    new_archive_path,
    round_trip,
)
from transfer.tests.test_invoices_section import MediaMixin
from transfer.tests.test_returnables_section import HELD as RETURNABLES_HELD
from transfer.tests.test_returnables_section import (
    ReturnablesData,
    at,
    clear_returnables,
    edit,
    first_pickup,
    moment,
    shas,
    tally,
)

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
KEY = "types_consignes"
RETURNABLES = returnables.KEY

#: What the fixture holds, per report row: the seeds (three types, the UBA
#: format) and its own.
HELD = {TYPES: 4, FORMATS: 3}


def run_import(reader, strategy, *, preview=False):
    """This section with `strategy`; whatever else the archive holds (the
    suppliers the export closure took) merged, as the page does."""
    strategies = {key: MERGE for key in reader.sections}
    strategies[KEY] = strategy
    return import_archive(reader, strategies, preview=preview)


def clear_types():
    """As the page clears it: « Consignes » with it (it requires this
    section), its pickups and slips first."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        run_clear(registry.closure({KEY}, "clear"), preview=False)


def payload_of(*, types=(), formats=(), supplier_names=None) -> dict:
    return {"supplier_names": supplier_names or {}, "types": list(types), "formats": list(formats)}


def snapshots() -> dict:
    return {key: registry.get(key).snapshot() for key in (KEY, RETURNABLES)}


def exported(test, keys) -> ArchiveReader:
    """What the page exports when `keys` are ticked - what they require
    with them -, closed once `test` ends."""
    reader = export_archive(registry.closure(keys, "export"))
    test.addCleanup(reader.close)
    return reader


def both(strategy) -> dict:
    """The two sections with `strategy`, the suppliers merged."""
    return {KEY: strategy, RETURNABLES: strategy, "fournisseurs": MERGE}


def older_archive(reader: ArchiveReader) -> Path:
    """The archive `reader` (this version's, of « Consignes » and what it
    requires) would have been before « Types et formats de consignes »
    existed: one consignes.json holding the types and formats beside the
    pickups and slips, the suppliers of both named in it, and a manifest
    declaring « Consignes » alone under the counts it gave then - the types
    and formats first. The files are copied as they are."""
    path = new_archive_path("ancienne")
    with zipfile.ZipFile(reader.path) as current:
        manifest = json.loads(current.read("manifest.json"))
        types = json.loads(current.read(f"{KEY}.json"))
        pickups = json.loads(current.read(f"{RETURNABLES}.json"))
        sections = manifest["sections"]
        carved = sections.pop(KEY)
        sections[RETURNABLES]["counts"] = {**carved["counts"], **sections[RETURNABLES]["counts"]}
        sections[RETURNABLES]["requires"] = ["fournisseurs"]
        old = {
            "supplier_names": dict(sorted({**types["supplier_names"], **pickups["supplier_names"]}.items())),
            "types": types["types"],
            "formats": types["formats"],
            "pickups": pickups["pickups"],
            "slips": pickups["slips"],
        }
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in current.infolist():
                if info.filename not in ("manifest.json", f"{KEY}.json", f"{RETURNABLES}.json"):
                    target.writestr(info.filename, current.read(info.filename))
            target.writestr(f"{RETURNABLES}.json", json.dumps(old, ensure_ascii=False))
            target.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return path


class TypesData(MediaMixin):
    """The seeds (three types, the UBA format), one type and two formats of
    our own - one inactive -, and no pickup nor slip: what a new bar takes.
    Every moment is set in the past: a round trip that forgot to restore one
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
            make_format(
                "Bon Grossiste Essai", supplier=self.brewer, is_active=False, sender_pattern="", subject_pattern=""
            ),
            created_at=moment(1, 11),
        )

    def export(self) -> ArchiveReader:
        """What the page exports when this section is ticked: the suppliers
        it requires with it."""
        return exported(self, {KEY})

    def forged(self, change) -> ArchiveReader:
        reader = ArchiveReader(forge(self.export(), types_consignes=change))
        self.addCleanup(reader.close)
        return reader


def is_pallets(record) -> bool:
    return record["name"] == "Palettes"


def is_brewer_format(record) -> bool:
    return record["name"] == "Bon Brasserie Essai"


# -- the contract ---------------------------------------------------------------------------


class ContractTests(TestCase):
    def test_every_field_is_exported_or_said_why_not(self):
        # A field added to one of these models later cannot be left out of
        # the archive in silence.
        for owner in (section, returnables):
            self.assertEqual(set(owner.EXPORTED), set(owner.NOT_EXPORTED))
            for model, exported in owner.EXPORTED.items():
                with self.subTest(section=owner.KEY, model=model.__name__):
                    concrete = {model_field.name for model_field in model._meta.concrete_fields}
                    self.assertEqual(concrete, set(exported) | set(owner.NOT_EXPORTED[model]))
                    self.assertFalse(set(exported) & set(owner.NOT_EXPORTED[model]))

    def test_every_model_of_the_app_is_in_exactly_one_of_the_two_sections(self):
        """A model added to returnables/ later is put in one of them, never
        both, never neither."""
        ours, theirs = set(section.EXPORTED), set(returnables.EXPORTED)
        self.assertEqual(ours, {ReturnableType, SlipFormat})
        self.assertFalse(ours & theirs)
        self.assertEqual(ours | theirs, set(apps.get_app_config("returnables").get_models()))

    def test_the_section_is_registered_under_its_key(self):
        self.assertIsInstance(registry.get(KEY), ReturnableTypesSection)
        self.assertIn("returnable_types", registry.SECTION_MODULES)

    def test_it_is_configuration_and_goes_before_the_pickups(self):
        """Applied before « Consignes » (a count finds the type it created),
        pruned and cleared after it (what still names a type is gone by
        then); clearing it clears « Consignes », never the other way round."""
        info = INFO[KEY]
        self.assertEqual((info.group, info.requires), (Group.CONFIG, ("fournisseurs",)))
        self.assertLess(info.order, INFO[RETURNABLES].order)
        self.assertEqual(registry.closure({KEY}, "clear"), {KEY, RETURNABLES})
        self.assertEqual(registry.closure({KEY}, "export"), {KEY, "fournisseurs"})
        self.assertIn("la sauvegarde", info.clear_note)

    def test_its_words_are_the_ones_consignes_counted_them_with(self):
        """An older archive's counts are read as this section's: the labels
        it gives now, in their order, are the carve's, and the carve reads
        them under « Consignes »' old ones - the same words."""
        carve = archive.CARVED[KEY]
        self.assertEqual(ENTITIES, (TYPES, FORMATS))
        self.assertEqual(tuple(label for label, _old in carve.counts), ENTITIES)
        self.assertEqual(tuple(old for _label, old in carve.counts), ("types de consigne", "formats de bons"))
        self.assertEqual((carve.within, carve.keys, carve.shared), (RETURNABLES, section.LISTS, ("supplier_names",)))
        self.assertFalse(set(ENTITIES) & set(returnables.ENTITIES))

    def test_a_type_s_order_is_bounded_as_the_types_page_bounds_it(self):
        """The refusal says « 32 767 au plus », the form's own words: the
        bound and the sentence cannot drift apart."""
        field = TypeForm.base_fields["position"]
        self.assertEqual((section.MAX_POSITION, field.min_value), (field.max_value, 0))
        self.assertEqual(field.error_messages["max_value"], "32 767 au plus.")

    def test_count_of_an_empty_section(self):
        no_defaults()
        self.assertEqual(ReturnableTypesSection().count(), {TYPES: 0, FORMATS: 0})

    def test_the_seeds_are_counted_like_the_rest(self):
        self.assertEqual(ReturnableTypesSection().count(), {TYPES: 3, FORMATS: 1})


class ExportTests(TypesData, TestCase):
    def test_counts_are_the_archives_under_the_same_labels(self):
        self.assertEqual(ReturnableTypesSection().count(), HELD)
        self.assertEqual(self.export().counts(KEY), HELD)

    def test_what_a_type_and_a_format_hold(self):
        payload = self.export().section(KEY).payload()
        self.assertEqual(
            next(record for record in payload["types"] if is_pallets(record)),
            {
                "name": "Palettes",
                "position": 4,
                "is_active": True,
                "slip_patterns": "PALETTE",
                "created_at": "2026-02-01T09:00:00+00:00",
            },
        )
        record = next(record for record in payload["formats"] if record["name"] == "Bon Grossiste Essai")
        self.assertEqual(set(record), {"supplier", *section.FORMAT_FIELDS})
        self.assertEqual(
            (record["supplier"], record["is_active"], record["created_at"]),
            ("BRASSERIE_ESSAI", False, "2026-02-01T11:00:00+00:00"),
        )
        self.assertEqual(record["line_pattern"], UBA_PATTERNS["line_pattern"])
        # In the order the page lists them.
        self.assertEqual(
            [record["name"] for record in payload["types"]], ["Fûts", "Caisses verre", "Bouteilles CO2", "Palettes"]
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

    def test_the_suppliers_of_its_formats_come_with_their_names(self):
        payload = self.export().section(KEY).payload()
        self.assertEqual(payload["supplier_names"], {"BRASSERIE_ESSAI": "Brasserie Essai", "UBA": self.uba.name})

    def test_its_file_holds_no_pickup_and_no_slip(self):
        make_pickup(counts={"Palettes": 2})
        make_slip(self.brewer_format)
        reader = self.export()
        self.assertEqual(reader.sections, {KEY, "fournisseurs"})
        self.assertEqual(set(reader.section(KEY).payload()), {"supplier_names", "types", "formats"})


# -- round trip, idempotence --------------------------------------------------------------------


class RoundTripTests(TypesData, TestCase):
    def _after_clear(self):
        # The seeds too.
        self.assertEqual(ReturnableTypesSection().count(), {TYPES: 0, FORMATS: 0})
        # The suppliers are not this section's.
        self.assertTrue(Supplier.objects.filter(code="BRASSERIE_ESSAI").exists())

    def _check(self, strategy):
        before, after = round_trip({KEY}, strategy, after_clear=self._after_clear)
        self.assertEqual(list(after), ["fournisseurs", KEY])
        self.assertEqual(after[KEY], before[KEY])
        # Its moments, put back after the insert.
        self.assertEqual(ReturnableType.objects.get(name="Palettes").created_at, moment(1))
        self.assertEqual(SlipFormat.objects.get(name="Bon Grossiste Essai").created_at, moment(1, 11))

    def test_merge(self):
        self._check(MERGE)

    def test_replace(self):
        self._check(REPLACE)


class IdempotenceTests(TypesData, TestCase):
    def assert_nothing_moves(self, run):
        report = run.section(KEY)
        for entity, number in HELD.items():
            with self.subTest(entity=entity):
                self.assertEqual(tally(report, entity), (0, 0, 0, number))
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))
        self.assertFalse(run.affected())

    def test_merging_its_own_export_changes_nothing(self):
        reader = self.export()
        before = db_fingerprint()
        self.assert_nothing_moves(run_import(reader, MERGE))
        self.assertEqual(db_fingerprint(), before)

    def test_replacing_with_its_own_export_changes_nothing(self):
        reader = self.export()
        before = db_fingerprint()
        with self.captureOnCommitCallbacks() as callbacks:
            self.assert_nothing_moves(run_import(reader, REPLACE))
        self.assertEqual((db_fingerprint(), callbacks), (before, []))

    def test_the_same_records_made_at_other_moments_are_unchanged(self):
        """An export taken on another computer, a day later: the moments
        differ, the records do not (never compared)."""
        reader = self.export()
        later = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        for model in (ReturnableType, SlipFormat):
            model.objects.update(created_at=later)
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                self.assert_nothing_moves(run_import(reader, strategy))
                self.assertEqual(db_fingerprint(), before)


# -- merge versus replace -------------------------------------------------------------------------


class MergeAndReplaceTests(TypesData, TestCase):
    """The database moved on after the export: of each kind of record, one
    changed here, one only here, one only in the archive."""

    def setUp(self):
        super().setUp()
        self.before = ReturnableTypesSection().snapshot()
        self.reader = self.export()
        # Types.
        ReturnableType.objects.filter(name="Caisses verre").update(slip_patterns="CAISSE|CASIER")
        self.barrels = make_type("Tonneaux", slip_patterns="TONNEAU")
        self.pallets.delete()
        # Formats.
        SlipFormat.objects.filter(pk=self.brewer_format.pk).update(is_active=False)
        self.cellar_format = make_format("Bon Cave Essai", supplier=self.brewer, sender_pattern="", subject_pattern="")
        self.wholesaler_format.delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = run_import(self.reader, MERGE)
        report = run.section(KEY)
        self.assertEqual(tally(report, TYPES), (1, 0, 0, 2))
        self.assertEqual(tally(report, FORMATS), (1, 0, 0, 1))
        self.assertEqual(
            report.conflicts,
            [
                "Type de consigne « Caisses verre » : différent dans l'archive (motifs des bons) — gardé tel quel",
                "Format de bon « Bon Brasserie Essai » : différent dans l'archive (actif) — gardé tel quel",
            ],
        )
        # Kept as they are here.
        self.assertEqual(ReturnableType.objects.get(name="Caisses verre").slip_patterns, "CAISSE|CASIER")
        self.assertFalse(SlipFormat.objects.get(pk=self.brewer_format.pk).is_active)
        # Only here: untouched.
        self.assertTrue(ReturnableType.objects.filter(pk=self.barrels.pk).exists())
        self.assertTrue(SlipFormat.objects.filter(pk=self.cellar_format.pk).exists())
        # Only in the archive: back, with its moment.
        self.assertEqual(ReturnableType.objects.get(name="Palettes").created_at, moment(1))
        self.assertTrue(SlipFormat.objects.filter(name="Bon Grossiste Essai", supplier=self.brewer).exists())
        # A merge updates and deletes nothing: no safety archive is needed.
        self.assertFalse(run.affected())

    def test_replace_makes_the_section_exactly_the_archive(self):
        run = run_import(self.reader, REPLACE)
        report = run.section(KEY)
        self.assertEqual(tally(report, TYPES), (1, 1, 1, 2))
        self.assertEqual(tally(report, FORMATS), (1, 1, 1, 1))
        self.assertEqual((report.conflicts, report.skipped, report.kept), ([], [], []))
        self.assertEqual(ReturnableTypesSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {KEY})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = run_import(self.reader, REPLACE, preview=True)
        self.assertEqual((db_fingerprint(), media_listing(), callbacks), (before, files, []))
        confirmed = run_import(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertNotEqual(db_fingerprint(), before)

    def test_a_merge_preview_says_what_the_merge_does(self):
        before = db_fingerprint()
        preview = run_import(self.reader, MERGE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(preview.outcome(), run_import(self.reader, MERGE).outcome())


class UsedTests(ReturnablesData, TestCase):
    """What « Consignes » still holds keeps its type and its format: PROTECT,
    said rather than a crash - and the pickups and slips are not touched."""

    def test_a_type_or_a_format_a_kept_pickup_or_slip_uses_stays_and_is_said(self):
        reader = ArchiveReader(
            forge(
                exported(self, {RETURNABLES}),
                types_consignes=lambda payload: {
                    **payload,
                    "types": [record for record in payload["types"] if record["name"] != "Fûts"],
                    "formats": [record for record in payload["formats"] if not is_brewer_format(record)],
                },
            )
        )
        self.addCleanup(reader.close)
        report = run_import(reader, REPLACE).section(KEY)
        self.assertIn("Type de consigne « Fûts » : encore compté dans 1 reprise", report.kept)
        self.assertIn("Format de bon « Bon Brasserie Essai » : encore utilisé par 1 bon", report.kept)
        self.assertTrue(ReturnableType.objects.filter(name="Fûts").exists())
        self.assertTrue(SlipFormat.objects.filter(pk=self.brewer_format.pk).exists())

    def test_its_archive_alone_replaces_the_types_and_leaves_the_pickups_untouched(self):
        """Another bar's configuration, or this bar's own, taken again with
        « Remplacer »: « Consignes » is neither in the archive nor in the
        run, its pickups and slips stay as they are, and a type only they
        count stays, said."""
        reader = exported(self, {KEY})
        self.assertEqual(reader.sections, {KEY, "fournisseurs"})
        barrels = make_type("Tonneaux", slip_patterns="TONNEAU")
        barrel_pickup = make_pickup(counts={barrels: 2}, photos=1)
        unused = make_type("Casiers", slip_patterns="CASIER")
        pickups, files = registry.get(RETURNABLES).snapshot(), shas()
        with self.captureOnCommitCallbacks(execute=True):
            run = run_import(reader, REPLACE)
        report = run.section(KEY)
        self.assertEqual(report.kept, ["Type de consigne « Tonneaux » : encore compté dans 1 reprise"])
        self.assertEqual(tally(report, TYPES), (0, 0, 1, 4))
        self.assertFalse(ReturnableType.objects.filter(pk=unused.pk).exists())
        self.assertEqual(registry.get(RETURNABLES).snapshot(), pickups)
        self.assertEqual(shas(), files)
        self.assertEqual(Pickup.objects.get(pk=barrel_pickup.pk).counts.get().returnable_type, barrels)
        self.assertIsNone(run.section(RETURNABLES))
        self.assertEqual(run.affected(), {KEY})


# -- keys -----------------------------------------------------------------------------------------


class KeyTests(ReturnablesData, TestCase):
    def test_a_type_is_found_by_its_name_whatever_its_accents_case_and_spaces(self):
        """The type form refuses « Futs » beside « Fûts »: an archive's
        « Futs » is this database's « Fûts », never a twin created beside it."""
        reader = ArchiveReader(
            forge(
                exported(self, {KEY}),
                types_consignes=edit(
                    "types", lambda record: record["name"] == "Fûts", lambda record: record.update(name="Futs")
                ),
            )
        )
        self.addCleanup(reader.close)
        report = run_import(reader, MERGE).section(KEY)
        self.assertIn("Type de consigne « Futs » : différent dans l'archive (nom) — gardé tel quel", report.conflicts)
        self.assertEqual(ReturnableType.objects.count(), 4)
        report = run_import(reader, REPLACE).section(KEY)
        self.assertEqual(tally(report, TYPES)[:3], (0, 1, 0))
        self.assertTrue(ReturnableType.objects.filter(name="Futs").exists())
        self.assertEqual(ReturnableType.objects.count(), 4)
        # Its counts followed it: the same row.
        self.assertEqual(self.pickup.counts.get(returnable_type__name="Futs").quantity, 15)

    def test_a_type_the_same_run_creates_is_the_one_a_count_finds(self):
        """« Consignes » reads the types once this section has applied: a
        count of « Tonneaux », a type only the archive has, finds the row
        created a moment before - in the preview too."""

        def with_barrels(payload):
            payload["types"].append({"name": "Tonneaux", "position": 9, "is_active": True, "slip_patterns": ""})
            return payload

        reader = ArchiveReader(
            forge(
                exported(self, {RETURNABLES}),
                types_consignes=with_barrels,
                consignes=edit("pickups", first_pickup, lambda record: record["counts"][0].update(type="Tonneaux")),
            )
        )
        self.addCleanup(reader.close)
        clear_returnables()
        before = db_fingerprint()
        preview = import_archive(reader, both(MERGE), preview=True)
        self.assertEqual(db_fingerprint(), before)
        confirmed = import_archive(reader, both(MERGE))
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertEqual(confirmed.section(RETURNABLES).skipped, [])
        pickup = Pickup.objects.get(reference=self.pickup.reference)
        self.assertEqual(pickup.counts.get(returnable_type__name="Tonneaux").quantity, 15)

    def test_a_format_s_supplier_under_another_code_is_found_by_its_name(self):
        def change(payload):
            payload["supplier_names"]["BRASSERIE_AILLEURS"] = "Brasserie Essai"
            for record in payload["formats"]:
                if is_brewer_format(record):
                    record["supplier"] = "BRASSERIE_AILLEURS"
            return payload

        reader = ArchiveReader(forge(exported(self, {KEY}), types_consignes=change))
        self.addCleanup(reader.close)
        clear_types()
        report = run_import(reader, MERGE).section(KEY)
        self.assertEqual(report.skipped, [])
        self.assertEqual(SlipFormat.objects.get(name="Bon Brasserie Essai").supplier, self.brewer)


# -- refusals ---------------------------------------------------------------------------------------


class RefusalTests(TypesData, TestCase):
    def setUp(self):
        super().setUp()
        self.reader = self.export()

    def _import(self, change, strategy=MERGE):
        reader = ArchiveReader(forge(self.reader, types_consignes=change))
        self.addCleanup(reader.close)
        return run_import(reader, strategy)

    def test_a_list_that_is_not_a_list_of_records_is_refused_whole(self):
        for name in section.LISTS:
            for value in ("Fûts", ["Fûts"], {"name": "Fûts"}):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaisesMessage(
                        ArchiveError,
                        f"Archive refusée : dans types_consignes.json, « {name} » n'est pas une liste d'objets.",
                    ),
                ):
                    self._import(lambda payload, name=name, value=value: {**payload, name: value})

    def test_a_list_left_out_is_not_said(self):
        """Neither read as empty - under « Remplacer » that would delete
        every type here - nor compared: left out, or null, its rows here
        are not touched. The other list is read all the same."""
        barrels = make_type("Tonneaux", slip_patterns="TONNEAU")
        for left_out in (
            lambda payload: {name: value for name, value in payload.items() if name != "types"},
            lambda payload: {**payload, "types": None},
        ):
            with self.subTest(left_out=left_out):
                report = self._import(left_out, REPLACE).section(KEY)
                self.assertEqual(tally(report, TYPES), (0, 0, 0, 0))
                self.assertEqual(tally(report, FORMATS), (0, 0, 0, 3))
                self.assertTrue(ReturnableType.objects.filter(pk=barrels.pk).exists())
                self.assertEqual(report.notes, [])

    def test_a_file_saying_neither_list_changes_nothing(self):
        make_type("Tonneaux", slip_patterns="TONNEAU")
        before = db_fingerprint()
        run = self._import(lambda payload: {"supplier_names": payload["supplier_names"]}, REPLACE)
        self.assertEqual(db_fingerprint(), before)
        self.assertFalse(run.affected())

    def test_an_unknown_field_is_noted_once(self):
        def change(payload):
            for record in payload["types"]:
                record["couleur"] = "vert"
            payload["remarque"] = "essai"
            return payload

        report = self._import(change).section(KEY)
        self.assertEqual(report.notes.count("champ inconnu ignoré : types de consigne › couleur"), 1)
        self.assertIn("champ inconnu ignoré : types_consignes.json › remarque", report.notes)
        self.assertEqual(ReturnableType.objects.count(), 4)

    def test_an_unknown_supplier_skips_the_format(self):
        clear_types()
        report = self._import(
            edit("formats", is_brewer_format, lambda record: record.update(supplier="INCONNU_ESSAI"))
        ).section(KEY)
        self.assertEqual(
            report.skipped, ["Format de bon « Bon Brasserie Essai » : fournisseur inconnu (« INCONNU_ESSAI »)"]
        )
        self.assertFalse(SlipFormat.objects.filter(name="Bon Brasserie Essai").exists())
        self.assertEqual(tally(report, FORMATS)[0], 2)

    def test_a_record_without_a_readable_name_is_skipped(self):
        def change(payload):
            payload["types"][0]["name"] = "  "
            payload["formats"][0]["name"] = 3
            return payload

        report = self._import(change).section(KEY)
        self.assertEqual(report.skipped, ["Type de consigne sans nom lisible", "Format de bon sans nom lisible"])

    def test_a_value_its_field_refuses_skips_the_record(self):
        report = self._import(edit("types", is_pallets, lambda record: record.update(position="4"))).section(KEY)
        self.assertEqual(
            report.skipped, ["Type de consigne « Palettes » : « position » : nombre entier attendu (« 4 »)"]
        )

    def test_a_position_past_the_type_form_s_bound_is_never_created(self):
        """The types page orders them from 0 to 32 767 (a small integer).
        SQLite holds more: below 2**63 the type was stored, then the page
        refused to save it as it drew it - its « nouveau type » form starting
        past the bound too -, and from 2**63 SQLite refused to store it at
        all: the import, its preview included, was a 500."""
        for position in (32_768, 2**63):
            with self.subTest(position=position):
                clear_types()
                report = self._import(
                    edit("types", is_pallets, lambda record, position=position: record.update(position=position))
                ).section(KEY)
                self.assertEqual(
                    report.skipped, [f"Type de consigne « Palettes » : « position » : 32 767 au plus (« {position} »)"]
                )
                self.assertFalse(ReturnableType.objects.filter(name="Palettes").exists())
                self.assertEqual(tally(report, TYPES)[0], 3)

    def test_a_position_past_the_bound_never_replaces_one_here(self):
        position = 2**63
        run = self._import(edit("types", is_pallets, lambda record: record.update(position=position)), REPLACE)
        said = f"Type de consigne « Palettes » : « position » : 32 767 au plus (« {position} »)"
        self.assertEqual(run.section(KEY).skipped, [said])
        # Named, so never pruned: kept as it is.
        self.assertEqual(ReturnableType.objects.get(pk=self.pallets.pk).position, 4)

    def test_a_position_at_the_bound_is_read(self):
        report = self._import(edit("types", is_pallets, lambda record: record.update(position=32_767)), REPLACE)
        self.assertEqual(report.section(KEY).skipped, [])
        self.assertEqual(ReturnableType.objects.get(pk=self.pallets.pk).position, 32_767)

    def test_the_same_record_twice_is_created_once(self):
        def change(payload):
            payload["types"].append({**payload["types"][0], "name": "FUTS"})
            payload["formats"].append({**payload["formats"][0], "name": payload["formats"][0]["name"].upper()})
            return payload

        clear_types()
        report = self._import(change).section(KEY)
        self.assertEqual(len(report.skipped), 2, report.skipped)
        self.assertTrue(all(line.endswith("en double dans l'archive") for line in report.skipped), report.skipped)
        self.assertEqual((ReturnableType.objects.count(), SlipFormat.objects.count()), (4, 3))


class NamedButUnreadTests(TypesData, TestCase):
    """« Remplacer » deletes only what the archive does not NAME: a record
    it names but could not read is skipped, and its row here stays."""

    def test_replace_never_prunes_a_record_it_skipped(self):
        def change(payload):
            edit("types", is_pallets, lambda record: record.update(slip_patterns="PALETTE("))(payload)
            edit("formats", is_brewer_format, lambda record: record.update(supplier="INCONNU_ESSAI"))(payload)
            return payload

        report = run_import(self.forged(change), REPLACE).section(KEY)
        self.assertEqual(len(report.skipped), 2, report.skipped)
        self.assertEqual((tally(report, TYPES)[2], tally(report, FORMATS)[2]), (0, 0))
        self.assertTrue(ReturnableType.objects.filter(pk=self.pallets.pk, slip_patterns="PALETTE").exists())
        self.assertTrue(SlipFormat.objects.filter(pk=self.brewer_format.pk).exists())


class PatternRefusalTests(TypesData, TestCase):
    """A pattern from an archive meets the guard the forms use. Refusals are
    proven with regex.compile replaced by a sentinel that fails if it is
    called: none of these is ever compiled for real."""

    def setUp(self):
        super().setUp()
        self.reader = self.export()
        clear_types()

    def _import(self, change):
        reader = ArchiveReader(forge(self.reader, types_consignes=change))
        self.addCleanup(reader.close)
        return run_import(reader, MERGE).section(KEY)

    def test_a_type_pattern_the_guard_refuses_is_never_compiled(self):
        reader = ArchiveReader(
            forge(
                {
                    KEY: payload_of(
                        types=[
                            {
                                "name": "Palettes",
                                "position": 4,
                                "is_active": True,
                                "slip_patterns": "PALETTE\n(?x)a{1 0 0 0 0}",
                            }
                        ]
                    )
                }
            )
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

    def test_a_format_pattern_the_guard_refuses_is_never_compiled(self):
        check_format_patterns(UBA_PATTERNS)  # the others, checked (and kept) before the sentinel
        uba_record = next(
            record for record in self.reader.section(KEY).payload()["formats"] if record["supplier"] == "UBA"
        )
        for pattern, reason in (
            ("(?:x{65535}){65535}", "répétition trop grande"),
            ("((a{1000}){1000}){1000}", "répétition trop grande"),
            ("(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
            ("(?x:a{1 0 0})", "le mode (?x) n'est pas accepté"),
            ("a{e<=1}", "accolade"),
            ("(?:(?:(?:x{100,}){100,}){100,}){100,}", "répétition trop grande"),
        ):
            with self.subTest(pattern=pattern):
                payload = payload_of(
                    formats=[{**uba_record, "name": "Bon piégé", "line_pattern": pattern}],
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
            (
                {"line_pattern": r"^(?P<designation>.+?)\s+(?P<quantite>\d+"},
                "motif refusé : Motif de ligne — parenthèse non fermée",
            ),
            (
                {"line_pattern": r"^(?P<designation>.+?)\s+\d+"},
                "motif refusé : Motif de ligne — le motif doit contenir",
            ),
            ({"section_start": "x*"}, "motif refusé : Début de la partie — le motif accepte une ligne vide"),
            ({"sender_pattern": ".+@.+"}, "motif refusé : Motif d'expéditeur — Le motif d'expéditeur doit désigner"),
            (
                {"sender_pattern": "livreur@brasserie-essai\\.example", "subject_pattern": ""},
                "motif refusé : Motif d'objet — obligatoire quand un motif d'expéditeur est donné",
            ),
        ):
            with self.subTest(change=change):
                report = self._import(
                    edit("formats", is_brewer_format, lambda record, change=change: record.update(change))
                )
                refused = [line for line in report.skipped if line.startswith("Format de bon « Bon Brasserie Essai »")]
                self.assertEqual(len(refused), 1, report.skipped)
                self.assertIn(said, refused[0])
                self.assertFalse(SlipFormat.objects.filter(name="Bon Brasserie Essai").exists())

    def test_a_format_the_forms_accept_is_imported(self):
        report = self._import(
            edit(
                "formats",
                is_brewer_format,
                lambda record: record.update(
                    sender_pattern="livreur@brasserie-essai\\.example", subject_pattern="^Livraison"
                ),
            )
        )
        self.assertEqual(report.skipped, [])
        self.assertEqual(
            SlipFormat.objects.get(name="Bon Brasserie Essai").sender_pattern, "livreur@brasserie-essai\\.example"
        )


# -- clear ----------------------------------------------------------------------------------------


class ClearTests(ReturnablesData, TestCase):
    def test_clearing_it_clears_the_pickups_and_slips_too_the_seeds_included(self):
        """The registry ticks « Consignes » with it; the clears run in
        reverse order, so its pickups and slips go first and nothing still
        names a type when the types go."""
        run = run_clear(registry.closure({KEY}, "clear"), preview=False)
        self.assertEqual(ReturnableTypesSection().count(), {TYPES: 0, FORMATS: 0})
        self.assertFalse(Pickup.objects.exists() or Slip.objects.exists())
        report = run.section(KEY)
        self.assertEqual({entity: report.tallies[entity].deleted for entity in ENTITIES}, HELD)
        self.assertIn(CLEAR_NOTE, report.notes)
        returnables_report = run.section(RETURNABLES)
        self.assertEqual(
            {entity: returnables_report.tallies[entity].deleted for entity in returnables.ENTITIES}, RETURNABLES_HELD
        )
        self.assertEqual(run.affected(), {KEY, RETURNABLES})
        # The suppliers are not this section's.
        self.assertTrue(Supplier.objects.filter(pk=self.brewer.pk).exists())

    def test_clearing_consignes_keeps_the_types_and_formats(self):
        before = ReturnableTypesSection().snapshot()
        run = run_clear({RETURNABLES}, preview=False)
        self.assertEqual(ReturnableTypesSection().snapshot(), before)
        self.assertIsNone(run.section(KEY))

    def test_a_preview_changes_nothing(self):
        keys = registry.closure({KEY}, "clear")
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = run_clear(keys, preview=True)
        self.assertEqual((db_fingerprint(), media_listing(), callbacks), (before, files, []))
        confirmed = run_clear(keys, preview=False)
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_a_section_with_nothing_says_nothing(self):
        clear_types()
        report = run_clear(registry.closure({KEY}, "clear"), preview=False).section(KEY)
        self.assertNotIn(CLEAR_NOTE, report.notes)

    def test_after_a_clear_the_safety_archive_brings_the_seeds_back(self):
        reader = exported(self, {KEY})
        before = ReturnableTypesSection().snapshot()
        clear_types()
        run_import(reader, REPLACE)
        self.assertEqual(ReturnableTypesSection().snapshot(), before)
        self.assertEqual(
            set(ReturnableType.objects.values_list("name", flat=True)),
            {"Fûts", "Caisses verre", "Bouteilles CO2", "Palettes"},
        )
        self.assertTrue(SlipFormat.objects.filter(name="UBA \N{EM DASH} bon du livreur", supplier=self.uba).exists())


# -- the two together, and the archives written before this section existed -------------------------


class TogetherTests(ReturnablesData, TestCase):
    def test_an_export_of_consignes_carries_the_types_and_formats(self):
        """Its counts and slips name them: the export closure takes them, in
        a file of their own."""
        reader = exported(self, {RETURNABLES})
        self.assertEqual(reader.sections, {"fournisseurs", KEY, RETURNABLES})
        self.assertIn(KEY, reader.manifest["sections"])
        self.assertEqual(reader.section(KEY).member, f"{KEY}.json")
        self.assertEqual(reader.counts(KEY), HELD)

    def test_both_round_trip_into_an_emptied_database(self):
        """Cleared together, imported together: every type a pickup counts
        and every format a slip was read with is created a moment before
        « Consignes » looks for it - in the preview too."""
        files = shas()
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before, after = round_trip({KEY, RETURNABLES}, strategy)
                self.assertEqual(list(after), ["fournisseurs", KEY, RETURNABLES])
                self.assertEqual((after[KEY], after[RETURNABLES]), (before[KEY], before[RETURNABLES]))
                self.assertEqual(shas(), files)

    def test_a_preview_of_both_into_an_emptied_database_says_what_the_confirm_does(self):
        reader = exported(self, {RETURNABLES})
        clear_types()
        before = db_fingerprint()
        preview = import_archive(reader, both(REPLACE), preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(preview.section(RETURNABLES).skipped, [])
        with self.captureOnCommitCallbacks(execute=True):
            confirmed = import_archive(reader, both(REPLACE))
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_replacing_both_prunes_a_type_and_a_format_only_pruned_rows_used(self):
        """« Consignes » prunes first (reverse order): the pickup and the slip
        the archive does not name are gone when this section prunes, so the
        type only that pickup counted and the format only that slip was read
        with go too, never « encore compté » - what the single section's
        prune did, from this version's archive and from an older one."""
        current = exported(self, {RETURNABLES})
        older = ArchiveReader(older_archive(current))
        self.addCleanup(older.close)
        before = snapshots()
        for name, reader in (("this version", current), ("older", older)):
            with self.subTest(archive=name):
                barrels = make_type("Tonneaux", slip_patterns="TONNEAU")
                make_pickup(date=date(2026, 2, 12), counts={barrels: 2})
                make_slip(make_format("Bon Cave Essai", supplier=self.brewer, sender_pattern="", subject_pattern=""))
                with self.captureOnCommitCallbacks(execute=True):
                    run = import_archive(reader, both(REPLACE))
                types_report, returnables_report = run.section(KEY), run.section(RETURNABLES)
                self.assertEqual((types_report.kept, returnables_report.kept), ([], []))
                deleted = {
                    entity: tally(report, entity)[2]
                    for report, entities in ((types_report, ENTITIES), (returnables_report, returnables.ENTITIES))
                    for entity in entities
                }
                self.assertEqual(deleted, {TYPES: 1, FORMATS: 1, PICKUPS: 1, PHOTOS: 0, SLIPS: 1, LINES: 1})
                self.assertEqual(snapshots(), before)


class OlderArchiveTests(ReturnablesData, TestCase):
    """An archive written before this section existed - every safety backup
    of the time - kept the types and formats in consignes.json. It is read
    as holding both sections (archive.CARVED), and its records, the very
    shapes this section writes, import with this code."""

    def setUp(self):
        super().setUp()
        self.files = shas()
        self.before = snapshots()
        self.reader = ArchiveReader(older_archive(exported(self, {RETURNABLES})))
        self.addCleanup(self.reader.close)

    def test_it_offers_both_sections_under_the_counts_it_gave_them(self):
        self.assertEqual(self.reader.sections, {"fournisseurs", RETURNABLES, KEY})
        self.assertEqual(list(self.reader.manifest["sections"]), ["fournisseurs", RETURNABLES])
        self.assertEqual(self.reader.counts(KEY), HELD)
        self.assertEqual(self.reader.counts(RETURNABLES), {**RETURNABLES_HELD, returnables.MEGABYTES: 0})
        self.assertEqual(self.reader.section(KEY).member, f"{RETURNABLES}.json")

    def test_importing_both_restores_everything(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                clear_types()
                self.assertEqual(ReturnableTypesSection().count(), {TYPES: 0, FORMATS: 0})
                with self.captureOnCommitCallbacks(execute=True):
                    run = import_archive(self.reader, both(strategy))
                self.assertEqual(snapshots(), self.before)
                self.assertEqual(shas(), self.files)
                types_report, returnables_report = run.section(KEY), run.section(RETURNABLES)
                self.assertEqual({entity: tally(types_report, entity)[0] for entity in ENTITIES}, HELD)
                self.assertEqual(
                    {entity: tally(returnables_report, entity)[0] for entity in returnables.ENTITIES},
                    RETURNABLES_HELD,
                )
                # Each section reads its own part of the one file: nothing
                # skipped, nothing « inconnu ».
                for report in (types_report, returnables_report):
                    self.assertEqual((report.skipped, report.conflicts), ([], []))
                    self.assertFalse([note for note in report.notes if "champ inconnu" in note], report.notes)

    def test_merged_into_the_database_it_came_from_it_changes_nothing(self):
        before = db_fingerprint()
        run = import_archive(self.reader, MERGE)
        self.assertEqual(db_fingerprint(), before)
        for key, held in ((KEY, HELD), (RETURNABLES, RETURNABLES_HELD)):
            report = run.section(key)
            for entity, number in held.items():
                with self.subTest(key=key, entity=entity):
                    self.assertEqual(tally(report, entity), (0, 0, 0, number))
            self.assertEqual((report.conflicts, report.skipped, report.notes), ([], [], []))

    def test_its_types_alone_import_and_its_pickups_stay_where_they_are(self):
        """Taken as a bar's configuration: « Consignes » unticked, the
        pickups here untouched."""
        Pickup.objects.filter(pk=self.other.pk).delete()
        pickups = registry.get(RETURNABLES).snapshot()
        ReturnableType.objects.filter(name="Palettes").delete()
        run = import_archive(self.reader, {KEY: REPLACE, "fournisseurs": MERGE})
        self.assertEqual(tally(run.section(KEY), TYPES), (1, 0, 0, 3))
        self.assertEqual(ReturnableTypesSection().snapshot(), self.before[KEY])
        self.assertEqual(registry.get(RETURNABLES).snapshot(), pickups)
        self.assertIsNone(run.section(RETURNABLES))


# -- the gather's coverage -------------------------------------------------------------------


class CoverageTests(TypesData, TestCase):
    """A format whose search patterns a « Remplacer » changes has its gather
    coverage sent back to its own start, as its form does
    (returnables/views.py): the days it covered were searched for other
    slips, and the automatic gather would otherwise start from the last of
    them and never fetch the older mails only the new patterns match."""

    SENDER = r"(?i)bons@brasserie-essai\.example"
    SUBJECT = "(?i)bon de reprise"

    def setUp(self):
        super().setUp()
        self.code = f"bons-{self.brewer_format.pk}"
        self.yesterday = timezone.localdate() - timedelta(days=1)
        GatherCoverage.objects.create(code=self.code, searched_until=self.yesterday)

    def changed(self, **fields) -> ArchiveReader:
        """This database's export, the brewer's format changed in it."""

        def change(payload):
            for record in payload["formats"]:
                if is_brewer_format(record):
                    record.update(fields)
            return payload

        return self.forged(change)

    def searched_until(self):
        return GatherCoverage.objects.get(code=self.code).searched_until

    def own_start(self):
        fmt = SlipFormat.objects.get(pk=self.brewer_format.pk)
        return fetch_start(fmt, None, timezone.localdate()) + timedelta(days=coverage.OVERLAP_DAYS)

    def test_replacing_its_search_patterns_restarts_its_coverage(self):
        reader = self.changed(sender_pattern=self.SENDER, subject_pattern=self.SUBJECT)
        report = run_import(reader, REPLACE).section(KEY)
        self.assertEqual(tally(report, FORMATS)[1], 1)
        self.assertEqual(SlipFormat.objects.get(pk=self.brewer_format.pk).sender_pattern, self.SENDER)
        self.assertEqual(self.searched_until(), self.own_start())
        self.assertLess(self.searched_until(), self.yesterday)

    def test_a_format_never_covered_is_given_its_own_start(self):
        GatherCoverage.objects.filter(code=self.code).delete()
        run_import(self.changed(subject_pattern=self.SUBJECT), REPLACE)
        self.assertEqual(self.searched_until(), self.own_start())

    def test_replacing_anything_else_leaves_its_coverage(self):
        report = run_import(self.changed(is_active=False), REPLACE).section(KEY)
        self.assertEqual(tally(report, FORMATS)[1], 1)
        self.assertFalse(SlipFormat.objects.get(pk=self.brewer_format.pk).is_active)
        self.assertEqual(self.searched_until(), self.yesterday)

    def test_a_merge_keeps_the_patterns_and_the_coverage(self):
        report = run_import(self.changed(sender_pattern=self.SENDER, subject_pattern=self.SUBJECT), MERGE).section(KEY)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(SlipFormat.objects.get(pk=self.brewer_format.pk).sender_pattern, "")
        self.assertEqual(self.searched_until(), self.yesterday)

    def test_a_preview_leaves_the_coverage(self):
        reader = self.changed(sender_pattern=self.SENDER, subject_pattern=self.SUBJECT)
        before = db_fingerprint()
        run_import(reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(self.searched_until(), self.yesterday)
