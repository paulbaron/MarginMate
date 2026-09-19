"""The harness (§5.5), with FakeSections standing for the real ones.

The promise it has to keep: the preview is the real run rolled back - it
writes nothing, deletes nothing, and says exactly what the confirm then
does. And a run that fails half way leaves nothing behind, not even the
files it had stored.
"""

import hashlib
import json
import secrets
from datetime import timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from django.utils import timezone

from inventory.models import StockType
from invoices.models import ReceiptBatch, ScrapeJob
from recipes.models import SalesImportJob
from tests.factories import make_recipe
from transfer import rebuild, runner
from transfer.archive import ArchiveReader
from transfer.runner import Busy, busy_reason, run_clear, run_export, run_import
from transfer.sections.base import Dirty, Strategy
from transfer.tests.support import (
    FakeSection,
    FakeSectionsMixin,
    db_fingerprint,
    export_archive,
    fake_row,
    forge,
    import_archive,
    media_listing,
    new_archive_path,
    round_trip,
)

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE


def store(name: str, data: bytes) -> str:
    if default_storage.exists(name):
        default_storage.delete(name)
    return default_storage.save(name, ContentFile(data))


class ExportTests(FakeSectionsMixin, TestCase):
    def test_the_page_exports_only_a_closed_selection(self):
        with self.assertRaises(ValueError):
            run_export({"recettes"}, new_archive_path())
        manifest = run_export({"recettes", "associations", "fournisseurs"}, new_archive_path())
        self.assertEqual(set(manifest["sections"]), {"recettes", "associations", "fournisseurs"})

    def test_a_safety_export_takes_exactly_what_it_is_given(self):
        manifest = run_export({"recettes"}, new_archive_path(), reason="sauvegarde avant import", closed=False)
        self.assertEqual(set(manifest["sections"]), {"recettes"})
        self.assertEqual(manifest["reason"], "sauvegarde avant import")

    def test_sections_export_in_order(self):
        run_export({"inventaires", "factures", "associations", "fournisseurs"}, new_archive_path())
        self.assertEqual(
            [key for method, key in FakeSection.calls if method == "export"],
            ["fournisseurs", "associations", "factures", "inventaires"],
        )


class PreviewTests(FakeSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        fake_row("fournisseurs", "Fournisseur A")
        fake_row("fournisseurs", "Fournisseur B")
        self.document = store("invoices/2026/09/exemple.pdf", b"%PDF exemple")
        FakeSection.files["factures"] = [self.document]
        fake_row("factures", "Document 1")
        self.reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(self.reader.close)
        # Then this database moves on: one record changed, one gone, one new.
        StockType.objects.filter(name="Fournisseur A").update(loss_percent="12.00")
        StockType.objects.filter(name="Document 1").delete()
        fake_row("fournisseurs", "Fournisseur C")
        default_storage.delete(self.document)

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)
        self.assertEqual(callbacks, [])

        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertTrue(preview.preview)
        self.assertFalse(confirmed.preview)
        self.assertNotEqual(db_fingerprint(), before)
        self.assertTrue(default_storage.exists(self.document))

    def test_merge_and_replace(self):
        merged = import_archive(self.reader, MERGE, preview=True)
        suppliers = merged.section("fournisseurs")
        self.assertEqual(suppliers.tallies["articles fictifs"].unchanged, 1)
        self.assertEqual(len(suppliers.conflicts), 1)
        self.assertEqual(merged.section("factures").tallies["articles fictifs"].created, 1)

        replaced = import_archive(self.reader, REPLACE)
        suppliers = replaced.section("fournisseurs")
        self.assertEqual((suppliers.tallies["articles fictifs"].updated, suppliers.tallies["articles fictifs"].deleted), (1, 1))
        self.assertEqual(replaced.affected(), {"fournisseurs"})
        self.assertIn("Aucun document n'a été relu ; rien n'a été appris.", replaced.notes)

    def test_a_clear_preview_changes_nothing(self):
        before = db_fingerprint()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = run_clear({"factures", "inventaires"}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(callbacks, [])
        self.assertEqual(preview.mode, "clear")
        confirmed = run_clear({"factures", "inventaires"}, preview=False)
        self.assertEqual(preview.outcome(), confirmed.outcome())


class AsPreviewedTests(FakeSectionsMixin, TestCase):
    """The confirm proves it is the run that was previewed: the preview can
    be half an hour old, and a document that arrived since would otherwise
    be pruned - its file deleted on commit - under a preview that announced
    « 0 à supprimer », with no archive taken of it (review, 19/09)."""

    def setUp(self):
        super().setUp()
        fake_row("fournisseurs", "Fournisseur A")
        self.document = store("invoices/2026/09/comme-apercu.pdf", b"%PDF comme apercu")
        FakeSection.files["factures"] = [self.document]
        fake_row("factures", "Document 1")
        self.reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(self.reader.close)
        default_storage.delete(self.document)

    def test_the_confirm_of_an_unchanged_database_runs(self):
        preview = import_archive(self.reader, REPLACE, preview=True)
        done = run_import(self.reader, {"fournisseurs": REPLACE, "factures": REPLACE}, preview=False, expected=preview)
        self.assertEqual(done.outcome(), preview.outcome())
        self.assertFalse(done.preview)
        self.assertTrue(default_storage.exists(self.document))

    def test_a_database_that_moved_since_the_preview_is_not_imported(self):
        preview = import_archive(self.reader, REPLACE, preview=True)
        fake_row("factures", "Document arrivé après l'aperçu")
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks, self.assertRaises(runner.NotAsPreviewed) as caught:
            run_import(self.reader, {"fournisseurs": REPLACE, "factures": REPLACE}, preview=False, expected=preview)
        # Rolled back whole, the file the run had stored removed again.
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)
        self.assertEqual(callbacks, [])
        self.assertFalse(default_storage.exists(self.document))
        # What it would do now, to be shown as the new preview.
        now = caught.exception.report
        self.assertTrue(now.preview)
        self.assertEqual(now.section("factures").tallies["articles fictifs"].deleted, 1)
        self.assertEqual(now.outcome(), import_archive(self.reader, REPLACE, preview=True).outcome())

    def test_a_preview_read_back_from_json_is_the_same_run(self):
        """The view keeps the preview in the stage's state.json."""
        from transfer.report import RunReport

        preview = RunReport.from_json(json.loads(json.dumps(import_archive(self.reader, MERGE, preview=True).to_json())))
        run_import(self.reader, {"fournisseurs": MERGE, "factures": MERGE}, preview=False, expected=preview)

    def test_a_clear_whose_database_moved_is_not_run(self):
        preview = run_clear({"factures", "inventaires"}, preview=True)
        fake_row("factures", "Document arrivé après l'aperçu")
        before = db_fingerprint()
        with self.assertRaises(runner.NotAsPreviewed) as caught:
            run_clear({"factures", "inventaires"}, preview=False, expected=preview)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(caught.exception.report.section("factures").tallies["articles fictifs"].deleted, 2)
        done = run_clear({"factures", "inventaires"}, preview=False, expected=caught.exception.report)
        self.assertEqual(done.section("factures").tallies["articles fictifs"].deleted, 2)
        self.assertEqual(StockType.objects.filter(category="factures").count(), 0)


class FailureTests(FakeSectionsMixin, TestCase):
    def test_a_failure_in_the_second_section_undoes_the_first_and_its_files(self):
        document = store("invoices/2026/09/a-restaurer.pdf", b"%PDF a restaurer")
        FakeSection.files["fournisseurs"] = [document]
        fake_row("fournisseurs", "Fournisseur A")
        fake_row("sources", "Source A")
        reader = export_archive({"fournisseurs", "sources"})
        self.addCleanup(reader.close)
        StockType.objects.all().delete()
        default_storage.delete(document)
        before, files = db_fingerprint(), media_listing()

        FakeSection.fail_on.add(("apply", "sources"))
        with self.assertRaises(RuntimeError):
            import_archive(reader, MERGE)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)
        self.assertFalse(default_storage.exists(document))

    def test_a_structural_problem_refuses_before_anything_runs(self):
        fake_row("fournisseurs", "Fournisseur A")
        reader = export_archive({"fournisseurs", "sources"})
        reader.close()
        broken = ArchiveReader(forge(reader.path, sources={"pas_de_records": True}))
        self.addCleanup(broken.close)
        from transfer.archive import ArchiveError

        with self.assertRaises(ArchiveError):
            import_archive(broken, MERGE)
        self.assertNotIn(("apply", "fournisseurs"), FakeSection.calls)

    def test_an_unclosed_or_absent_selection_is_a_bug(self):
        reader = export_archive({"fournisseurs", "sources"})
        self.addCleanup(reader.close)
        with self.assertRaises(ValueError):
            run_import(reader, {"sources": MERGE}, preview=True)
        with self.assertRaises(ValueError):
            run_import(reader, {"banque": MERGE}, preview=True)
        with self.assertRaises(ValueError):
            run_import(reader, {}, preview=True)


class OrderTests(FakeSectionsMixin, TestCase):
    def test_apply_in_order_then_prune_in_reverse_replaced_sections_only(self):
        keys = {"fournisseurs", "associations", "factures", "inventaires"}
        reader = export_archive(keys)
        self.addCleanup(reader.close)
        FakeSection.calls.clear()
        import_archive(reader, {"fournisseurs": MERGE, "associations": REPLACE, "factures": MERGE, "inventaires": REPLACE})
        self.assertEqual(
            FakeSection.calls,
            [
                ("apply", "fournisseurs"), ("apply", "associations"), ("apply", "factures"), ("apply", "inventaires"),
                ("prune", "inventaires"), ("prune", "associations"),
            ],
        )

    def test_clear_in_reverse_order(self):
        run_clear({"factures", "inventaires"}, preview=False)
        self.assertEqual(FakeSection.calls, [("clear", "inventaires"), ("clear", "factures")])

    def test_the_page_clears_only_a_closed_selection(self):
        with self.assertRaises(ValueError):
            run_clear({"factures"}, preview=True)
        run_clear({"factures"}, preview=True, closed=False)

    def test_clear_deletes_files_only_after_commit(self):
        document = store("invoices/2026/09/efface.pdf", b"%PDF efface")
        FakeSection.files["factures"] = [document]
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            run_clear({"factures", "inventaires"}, preview=False)
        self.assertTrue(default_storage.exists(document))
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertFalse(default_storage.exists(document))

    def test_a_file_a_row_still_names_is_not_deleted(self):
        from invoices.models import Invoice
        from tests.factories import make_invoice

        document = store("invoices/2026/09/encore-cite.pdf", b"%PDF encore")
        invoice = make_invoice()
        Invoice.objects.filter(pk=invoice.pk).update(source_file=document)
        FakeSection.files["factures"] = [document]
        with self.captureOnCommitCallbacks(execute=True):
            run_clear({"factures", "inventaires"}, preview=False)
        self.assertTrue(default_storage.exists(document))


class AffectedTests(FakeSectionsMixin, TestCase):
    def test_a_section_touched_through_another_counts(self):
        """Clearing invoices deletes the bank's payments: the bank's report
        says so, and the bank is then in the safety export."""
        FakeSection.touch["factures"] = "banque"
        report = run_clear({"factures", "inventaires"}, preview=True)
        self.assertEqual([section.key for section in report.sections], ["factures", "inventaires", "banque"])
        self.assertEqual(report.section("banque").tallies["paiements"].deleted, 1)
        self.assertIn("banque", report.affected())

    def test_an_untouched_section_is_not_affected(self):
        fake_row("factures", "Document 1")
        report = run_clear({"factures", "inventaires"}, preview=True)
        self.assertEqual(report.affected(), {"factures"})


class FileTests(FakeSectionsMixin, TestCase):
    def folder(self) -> str:
        """A folder of this test's own: media is not emptied between tests."""
        return f"invoices/test-{secrets.token_hex(4)}"

    def test_a_stored_file_with_the_same_content_is_reused(self):
        folder = self.folder()
        document = store(f"{folder}/meme.pdf", b"%PDF meme")
        FakeSection.files["factures"] = [document]
        reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(reader.close)
        import_archive(reader, MERGE)
        self.assertEqual(sorted(default_storage.listdir(folder)[1]), ["meme.pdf"])

    def test_the_same_name_with_other_content_gets_another_name(self):
        folder = self.folder()
        document = store(f"{folder}/pris.pdf", b"%PDF archive")
        FakeSection.files["factures"] = [document]
        reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(reader.close)
        store(document, b"%PDF autre contenu ici")
        report = import_archive(reader, MERGE)
        self.assertEqual(report.section("factures").skipped, [])
        names = sorted(default_storage.listdir(folder)[1])
        self.assertEqual(len(names), 2)
        other = next(name for name in names if name != "pris.pdf")
        self.assertRegex(other, r"^pris_\w+\.pdf$")
        with default_storage.open(document, "rb") as handle:
            self.assertEqual(handle.read(), b"%PDF autre contenu ici")
        with default_storage.open(f"{folder}/{other}", "rb") as handle:
            self.assertEqual(handle.read(), b"%PDF archive")

    def test_a_record_whose_file_name_escapes_media_is_skipped(self):
        data = b"%PDF evasion"
        digest = hashlib.sha256(data).hexdigest()
        member = "files/invoices/evasion.pdf"
        path = forge(
            {"fournisseurs": {"records": [], "files": []},
             "factures": {"records": [], "files": [{"member": member, "name": "../../config/evil.pdf", "size": len(data), "sha256": digest}]}},
            manifest={"files": [{"member": member, "size": len(data), "sha256": digest}]},
        )
        import zipfile

        with zipfile.ZipFile(path, "a") as target:
            target.writestr(member, data)
        with ArchiveReader(path) as reader:
            report = import_archive(reader, MERGE)
        self.assertEqual(report.section("factures").skipped, ["nom de fichier refusé (« ../../config/evil.pdf »)"])

    def test_the_preview_writes_no_file(self):
        document = store("invoices/2026/09/apercu.pdf", b"%PDF apercu")
        FakeSection.files["factures"] = [document]
        reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(reader.close)
        default_storage.delete(document)
        report = import_archive(reader, MERGE, preview=True)
        self.assertEqual(report.section("factures").tallies["fichiers"].created, 1)
        self.assertFalse(default_storage.exists(document))


class BusyTests(FakeSectionsMixin, TestCase):
    def test_a_running_job_refuses_every_run(self):
        for model, extra in ((ScrapeJob, {}), (ReceiptBatch, {}), (SalesImportJob, {})):
            with self.subTest(job=model.__name__):
                job = model.objects.create(status=model.Status.RUNNING, last_heartbeat=timezone.now(), **extra)
                self.assertEqual(busy_reason(), runner.BUSY)
                with self.assertRaises(Busy):
                    run_clear({"banque"}, preview=True)
                job.delete()
        self.assertEqual(busy_reason(), "")

    def test_a_stale_job_is_reaped_and_does_not_refuse(self):
        long_ago = timezone.now() - timedelta(hours=2)
        job = ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING)
        ScrapeJob.objects.filter(pk=job.pk).update(started_at=long_ago, last_heartbeat=long_ago)
        self.assertEqual(busy_reason(), "")
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)


class SupportHelperTests(FakeSectionsMixin, TestCase):
    """The helpers every section's tests use (§10.2), on the fakes."""

    def test_round_trip(self):
        fake_row("fournisseurs", "Fournisseur A")
        fake_row("recettes", "Recette A", unit="UNIT")
        fake_row("associations", "Article A", loss_percent="7.50")
        emptied = []
        before, after = round_trip({"recettes"}, after_clear=lambda: emptied.append(
            StockType.objects.filter(category="recettes").count()
        ))
        self.assertEqual(emptied, [0])
        self.assertEqual(before, after)
        self.assertEqual(set(before), {"recettes", "associations", "fournisseurs"})

    def test_the_fingerprint_sees_a_write(self):
        before = db_fingerprint()
        fake_row("banque", "Opération A")
        self.assertNotEqual(db_fingerprint(), before)

    def test_import_archive_takes_one_strategy_for_all(self):
        fake_row("fournisseurs", "Fournisseur A")
        reader = export_archive({"fournisseurs", "sources"})
        self.addCleanup(reader.close)
        report = import_archive(reader, "remplacer", preview=True)
        self.assertEqual([section.key for section in report.sections], ["fournisseurs", "sources"])

    def test_a_patched_limit_is_seen(self):
        with mock.patch("transfer.archive.MAX_MEMBERS", 1):
            fake_row("fournisseurs", "Fournisseur A")
            path = new_archive_path()
            run_export({"fournisseurs", "sources"}, path)
            from transfer.archive import ArchiveError

            with self.assertRaises(ArchiveError):
                ArchiveReader(path)


class RebuildTests(TestCase):
    def test_nothing_dirty_rebuilds_nothing(self):
        self.assertEqual(
            rebuild.rebuild(Dirty()),
            {"mouvements de stock": 0, "statuts de factures": 0, "ventes par recette": 0, "produits caisse": 0},
        )

    def test_a_recipe_deleted_in_the_same_run_is_not_counted_as_resynced(self):
        """A replace can mark a recipe's till sales for rebuilding (its link
        moved) and then prune the recipe: « Recalculé » counts the recipes
        whose sales were rebuilt, not the ones asked about."""
        kept = make_recipe("Mojito essai")
        gone = make_recipe("Recette effacée")
        dirty = Dirty(recipes={kept.pk, gone.pk})
        gone.delete()
        self.assertEqual(rebuild.rebuild(dirty)["ventes par recette"], 1)
