"""« Formats des fichiers de caisse » (`formats_caisse`,
sections/till_formats.py): how a bar's own till export is read, taken to
another bar on the same till from « Configuration seule ».

A round trip brings every format back exactly, with its moment; a merge
never overwrites one changed here; « Remplacer » prunes what the archive does
not name - of a list it said; and a format an archive may say anything in
goes through the model's own check before it is written, its refusal said in
French. Every name and column below is invented.
"""

from datetime import UTC, datetime

from django.test import TestCase

from recipes.models import TillFormat
from transfer import registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.sections import till_formats as section
from transfer.sections.base import Strategy
from transfer.sections.till_formats import FORMATS, KEY, TillFormatsSection
from transfer.tests.support import db_fingerprint, export_archive, forge, import_archive, round_trip

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
MOMENT = datetime(2026, 7, 23, 10, 5, 30, 250000, tzinfo=UTC)
DJANGO_ENGLISH = r"Ensure this value|This field|is not a valid|already exists"


def make_till_format(name: str = "Caisse exemple — ventes", **fields) -> TillFormat:
    """A format as the page saves it (checked by the model), made at a
    moment in the past: a round trip that forgot to restore it would show
    today's instead."""
    values = {
        "kind": TillFormat.Kind.SALES,
        "delimiter": "\t",
        "decimal_mark": ".",
        "date_format": "yyyy-mm-dd",
        "service_day_end_hour": 5,
        "day_column": "Date",
        "time_column": "Heure",
        "product_column": "Article",
        "quantity_column": "3",
        "amount_column": "Total TTC",
        "rate_column": "TVA",
        "category_column": "Famille",
    }
    values.update(fields)
    fmt = TillFormat(name=name, **values)
    fmt.full_clean()
    fmt.save()
    TillFormat.objects.filter(pk=fmt.pk).update(created_at=MOMENT)
    fmt.refresh_from_db()
    return fmt


def make_payments_format() -> TillFormat:
    return make_till_format(
        "Caisse exemple — encaissements",
        kind=TillFormat.Kind.PAYMENTS,
        time_column="",
        product_column="",
        quantity_column="",
        amount_column="",
        rate_column="",
        category_column="",
        method_column="Moyen",
        paid_column="Montant",
        method_map="CB SANS CONTACT = Carte\nTICKET RESTO = Titres-restaurant",
    )


def formats() -> list[tuple]:
    return sorted(
        TillFormat.objects.values_list(*[name for name in section.FIELDS if name != "created_at"], "created_at")
    )


class Data:
    def setUp(self):
        super().setUp()
        self.sales = make_till_format()
        self.payments = make_payments_format()

    def export(self) -> ArchiveReader:
        reader = export_archive({KEY})
        self.addCleanup(reader.close)
        return reader

    def forged(self, change) -> ArchiveReader:
        reader = ArchiveReader(forge(self.export(), formats_caisse=change))
        self.addCleanup(reader.close)
        return reader


class ContractTests(TestCase):
    def test_every_field_is_exported_or_said_why_not(self):
        concrete = {field.name for field in TillFormat._meta.concrete_fields}
        exported, kept_out = set(section.EXPORTED[TillFormat]), set(section.NOT_EXPORTED[TillFormat])
        self.assertEqual(concrete, exported | kept_out)
        self.assertFalse(exported & kept_out)

    def test_registered_as_configuration_requiring_nothing(self):
        self.assertIsInstance(registry.get(KEY), TillFormatsSection)
        info = registry.INFO[KEY]
        self.assertEqual((info.label, info.requires, info.recommends), ("Formats des fichiers de caisse", (), ()))
        self.assertEqual(registry.closure({KEY}, "export"), {KEY})
        self.assertEqual(registry.closure({KEY}, "clear"), {KEY})
        self.assertNotIn(KEY, registry.closure({"ventes"}, "export"))

    def test_a_new_database_holds_none(self):
        self.assertEqual(TillFormatsSection().count(), {FORMATS: 0})


class ExportTests(Data, TestCase):
    def test_every_field_but_the_id_the_tab_a_tab(self):
        payload = self.export().section(KEY).payload()
        self.assertEqual(list(payload), ["formats"])
        self.assertEqual([item["name"] for item in payload["formats"]], [self.payments.name, self.sales.name])
        sales = payload["formats"][1]
        self.assertEqual(set(sales), set(section.FIELDS))
        self.assertEqual((sales["delimiter"], sales["quantity_column"]), ("\t", "3"))
        self.assertEqual(sales["created_at"], "2026-07-23T10:05:30.250000+00:00")
        self.assertEqual(self.export().section(KEY).counts, {FORMATS: 2})

    def test_none_is_an_empty_list(self):
        TillFormat.objects.all().delete()
        self.assertEqual(self.export().section(KEY).payload(), {"formats": []})


class RoundTripTests(Data, TestCase):
    def assert_empty(self):
        self.assertEqual(TillFormatsSection().count(), {FORMATS: 0})

    def test_merge_and_replace_bring_every_format_back_with_its_moment(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                kept = formats()
                before, after = round_trip({KEY}, strategy, after_clear=self.assert_empty)
                self.assertEqual(after, before)
                self.assertEqual(formats(), kept)
                self.assertEqual(TillFormat.objects.get(name=self.sales.name).created_at, MOMENT)

    def test_importing_its_own_export_changes_nothing(self):
        reader = self.export()
        TillFormat.objects.update(created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                run = import_archive(reader, strategy)
                counted = run.section(KEY).tallies[FORMATS]
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 2))
                self.assertEqual(run.section(KEY).conflicts, [])
                self.assertEqual(db_fingerprint(), before)


class MergeAndReplaceTests(Data, TestCase):
    """After the export: one format changed here, one only here, one only in
    the archive."""

    def setUp(self):
        super().setUp()
        self.reader = self.export()
        TillFormat.objects.filter(pk=self.sales.pk).update(service_day_end_hour=4)
        self.payments.delete()
        self.local = make_till_format("Caisse locale", delimiter=";", time_column="", service_day_end_hour=0)

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        counted = run.section(KEY).tallies[FORMATS]
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 0, 0, 0))
        self.assertEqual(
            run.section(KEY).conflicts,
            [
                f"Format de fichier de caisse « {self.sales.name} » : différent dans l'archive (fin du service) — gardé tel quel"
            ],
        )
        self.assertEqual(TillFormat.objects.get(pk=self.sales.pk).service_day_end_hour, 4)
        self.assertTrue(TillFormat.objects.filter(name="Caisse locale").exists())
        back = TillFormat.objects.get(name=self.payments.name)
        self.assertEqual((back.method_map, back.created_at), (self.payments.method_map, MOMENT))

    def test_replace_makes_it_the_archive_s_and_prunes_the_rest(self):
        run = import_archive(self.reader, REPLACE)
        counted = run.section(KEY).tallies[FORMATS]
        self.assertEqual((counted.created, counted.updated, counted.deleted), (1, 1, 1))
        self.assertEqual(TillFormat.objects.get(pk=self.sales.pk).service_day_end_hour, 5)
        self.assertEqual(
            sorted(TillFormat.objects.values_list("name", flat=True)), sorted([self.sales.name, self.payments.name])
        )

    def test_a_name_spelt_otherwise_is_the_same_format_renamed(self):
        TillFormat.objects.filter(pk=self.sales.pk).update(name=self.sales.name.upper(), service_day_end_hour=5)
        run = import_archive(self.reader, REPLACE)
        self.assertEqual(TillFormat.objects.get(pk=self.sales.pk).name, self.sales.name)
        self.assertEqual(run.section(KEY).tallies[FORMATS].updated, 1)


class RefusalTests(Data, TestCase):
    def test_a_format_the_check_refuses_is_skipped_in_french(self):
        def change(payload):
            payload["formats"].append({**payload["formats"][1], "name": "Caisse cassée", "quantity_column": ""})
            payload["formats"].append({**payload["formats"][1], "name": "Caisse inconnue", "kind": "autre"})
            payload["formats"].append({**payload["formats"][1], "name": "Caisse trop large", "day_column": "101"})
            payload["formats"].append({**payload["formats"][0], "name": "Caisse sans moyen", "method_map": "Lydia"})
            return payload

        reader = self.forged(change)
        TillFormat.objects.all().delete()
        run = import_archive(reader, MERGE)
        skipped = run.section(KEY).skipped
        self.assertEqual(len(skipped), 4, skipped)
        self.assertIn("Caisse cassée » : format refusé — colonne de la quantité : indiquez cette colonne", skipped[0])
        for said in skipped:
            self.assertNotRegex(said, DJANGO_ENGLISH)
        self.assertEqual(
            sorted(TillFormat.objects.values_list("name", flat=True)), sorted([self.sales.name, self.payments.name])
        )

    def test_a_record_twice_or_with_no_name_is_skipped(self):
        def change(payload):
            payload["formats"].append(dict(payload["formats"][0]))
            payload["formats"].append({**payload["formats"][0], "name": ""})
            return payload

        run = import_archive(self.forged(change), REPLACE)
        self.assertEqual(len(run.section(KEY).skipped), 2)
        # Named in the archive, if only twice: not pruned.
        self.assertEqual(TillFormat.objects.count(), 2)

    def test_a_list_not_said_prunes_nothing_and_a_list_not_a_list_is_refused(self):
        run = import_archive(self.forged({}), REPLACE)
        self.assertEqual(run.section(KEY).tallies[FORMATS].deleted, 0)
        self.assertEqual(TillFormat.objects.count(), 2)
        with self.assertRaises(ArchiveError):
            import_archive(self.forged({"formats": "tout"}), REPLACE)


class ClearTests(Data, TestCase):
    def test_a_clear_takes_every_format(self):
        from transfer.runner import run_clear

        with self.captureOnCommitCallbacks(execute=True):
            run_clear({KEY}, preview=False, closed=False)
        self.assertFalse(TillFormat.objects.exists())
