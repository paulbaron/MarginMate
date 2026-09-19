"""Backups before a confirmed import or clear (§5.8): the database always, an
importable archive of what changes before a « Remplacer » or an « Effacer »
- taken first, outside the transaction, and a failed backup changes
nothing."""

import hashlib
import json
import shutil
import sqlite3
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from bank import reconcile
from bank.models import BankTransaction, InvoicePayment
from invoices.models import Invoice, Supplier
from tests.factories import make_invoice
from transfer import registry, safety, staging, views
from transfer.archive import ArchiveReader
from transfer.report import RunReport, SectionReport
from transfer.sections.base import Strategy
from transfer.tests.support import (
    FakeSection,
    FakeSectionsMixin,
    export_archive,
    fake_row,
    import_archive,
)
from transfer.tests.test_views import shown_preview

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE


def empty_backups():
    shutil.rmtree(settings.DATA_BACKUP_DIR, ignore_errors=True)


def run_report(mode="import", **tallies) -> RunReport:
    """key → {entity: {"created"|"updated"|"deleted"|"unchanged": n}}."""
    sections = []
    for key, entities in tallies.items():
        section = SectionReport.for_key(key)
        for entity, counts in entities.items():
            for kind, number in counts.items():
                getattr(section, kind)(entity, number)
        sections.append(section)
    return RunReport(mode=mode, preview=True, sections=sections, rebuilt={})


class AtRiskTests(SimpleTestCase):
    """What the safety archive holds (§6.5): whatever the run changes or
    deletes of what exists - whichever section's code did it - and a section
    a « Remplacer » changes at all, creations included; not what a
    « Fusionner » only filled in or added (the database copy covers it)."""

    def test_an_import(self):
        report = run_report(
            fournisseurs={"fournisseurs": {"updated": 1}},        # merged: a blank filled
            associations={"produits": {"created": 4}},            # merged: only added
            factures={"documents": {"deleted": 1}},               # replaced
            recettes={"recettes": {"created": 2, "unchanged": 3}},  # replaced: its creations are the undo's to prune
            sources={"sources": {"unchanged": 5}},                # replaced, nothing changes
            banque={"paiements": {"deleted": 1}},                 # not imported: the factures prune took them
            liens_ventes={"produits caisse liés": {"updated": 1}},  # not imported: the recipes prune released them
        )
        strategies = {"fournisseurs": MERGE, "associations": MERGE, "factures": REPLACE, "recettes": REPLACE, "sources": REPLACE}
        self.assertEqual(
            safety.sections_at_risk(report, strategies=strategies), {"factures", "recettes", "banque", "liens_ventes"}
        )

    def test_a_replace_that_only_creates_is_in_the_archive(self):
        """The bank « Remplacer » that only makes a link again - one a person
        had undone, the line's record equal to the archive's - changed
        nothing that existed; left out of the archive, importing that archive
        back (the undo it is for) could not take the link off (review,
        19/09)."""
        report = run_report(banque={"paiements": {"created": 1}, "opérations": {"unchanged": 3}})
        self.assertEqual(safety.sections_at_risk(report, strategies={"banque": REPLACE}), {"banque"})
        self.assertEqual(safety.sections_at_risk(report, strategies={"banque": MERGE}), set())

    def test_a_merged_section_that_lost_rows_is_kept_too(self):
        """Merged in the same run as a « Remplacer » of the invoices, the bank
        still loses the payments of the invoices that prune removes - and
        the report sends the owner to this archive to get them back."""
        report = run_report(factures={"documents": {"deleted": 1}}, banque={"paiements": {"deleted": 1, "created": 2}})
        strategies = {"factures": REPLACE, "banque": MERGE}
        self.assertEqual(safety.sections_at_risk(report, strategies=strategies), {"factures", "banque"})

    def test_a_clear(self):
        report = run_report("clear", factures={"documents": {"deleted": 3}}, inventaires={}, banque={"paiements": {"deleted": 1}})
        self.assertEqual(
            safety.sections_at_risk(report, cleared={"factures", "inventaires"}),
            {"factures", "inventaires", "banque"},
        )


class DatabaseBackupTests(TransactionTestCase):
    # The suppliers the migrations seed must still be there for the tests
    # that run after this one.
    serialized_rollback = True

    def setUp(self):
        empty_backups()
        self.addCleanup(empty_backups)

    def test_the_backup_is_a_database_sqlite_opens(self):
        Supplier.objects.create(code="EXEMPLE", name="Fournisseur Exemple")
        path = safety.backup_database("avant-import")
        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}_\d{6}_avant-import\.sqlite3$")
        with sqlite3.connect(str(path)) as copy:
            count = copy.execute("select count(*) from invoices_supplier").fetchone()[0]
            names = [row[0] for row in copy.execute("select name from invoices_supplier where code = 'EXEMPLE'")]
        self.assertEqual(count, Supplier.objects.count())
        self.assertEqual(names, ["Fournisseur Exemple"])
        copy.close()

    def test_it_refuses_inside_a_transaction(self):
        with transaction.atomic(), self.assertRaises(RuntimeError):
            safety.backup_database("avant-import")

    def test_names_and_collisions(self):
        first = safety.backup_database("avant-effacement", stamp="2026-09-19_143012")
        second = safety.backup_database("avant-effacement", stamp="2026-09-19_143012")
        self.assertEqual(first.name, "2026-09-19_143012_avant-effacement.sqlite3")
        self.assertEqual(second.name, "2026-09-19_143012_avant-effacement_2.sqlite3")


class ListingTests(TestCase):
    def setUp(self):
        empty_backups()
        self.addCleanup(empty_backups)

    def test_only_our_names_are_listed_newest_first(self):
        folder = safety.backup_dir()
        for name in (
            "2026-09-18_101500_avant-import.sqlite3",
            "2026-09-19_143012_avant-effacement.zip",
            "2026-09-19_143012_avant-effacement_2.zip",
            "db.sqlite3.bak_20260919",
            "notes.txt",
            "2026-09-19_143012_avant-import.exe",
        ):
            (folder / name).write_bytes(b"x" * 10)
        (folder / "2026-09-20_000000_avant-import.zip").mkdir()
        listed = safety.list_backups()
        self.assertEqual(
            [backup.name for backup in listed],
            [
                "2026-09-19_143012_avant-effacement_2.zip",
                "2026-09-19_143012_avant-effacement.zip",
                "2026-09-18_101500_avant-import.sqlite3",
            ],
        )
        self.assertEqual([backup.kind for backup in listed], ["zip", "zip", "sqlite"])
        self.assertEqual(listed[0].size, 10)
        self.assertEqual(timezone.localtime(listed[2].created).strftime("%d/%m/%Y %H:%M"), "18/09/2026 10:15")
        self.assertIsNone(safety.find_backup("notes.txt"))
        self.assertIsNone(safety.find_backup("2026-09-18_101500_avant-import.sqlite3"))  # not importable
        self.assertIsNotNone(safety.find_backup("2026-09-19_143012_avant-effacement.zip"))

    def test_no_folder_no_backups(self):
        self.assertEqual(safety.list_backups(), [])


class BeforeTests(FakeSectionsMixin, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        empty_backups()
        self.addCleanup(empty_backups)

    def test_the_database_always_and_an_archive_of_what_changes(self):
        fake_row("recettes", "Recette A")
        backups = safety.before("effacement", {"recettes"})
        self.assertTrue(Path(backups["database"]).is_file())
        self.assertTrue(backups["archive"].endswith("_avant-effacement.zip"))
        with ArchiveReader(Path(backups["archive"])) as reader:
            self.assertEqual(reader.sections, {"recettes"})  # exactly these, not their closure
            self.assertEqual(reader.reason, "sauvegarde avant effacement")
            self.assertEqual(reader.section("recettes").payload()["records"][0]["name"], "Recette A")

    def test_nothing_affected_means_no_archive(self):
        backups = safety.before("import", set())
        self.assertEqual(backups["archive"], "")
        self.assertEqual(len(safety.list_backups()), 1)

    def test_a_failed_backup_is_said(self):
        with mock.patch("transfer.safety.backup_database", side_effect=OSError("disque plein")), \
                self.assertRaises(safety.SafetyError) as caught:
            safety.before("import", set())
        self.assertEqual(str(caught.exception), "Sauvegarde impossible (disque plein) : rien n'a été changé.")

    def test_no_room_is_said_before_anything_is_written(self):
        full = mock.Mock(free=0)
        with mock.patch("transfer.safety.shutil.disk_usage", return_value=full), \
                self.assertRaises(safety.SafetyError) as caught:
            safety.before("effacement", {"recettes"})
        self.assertIn("pas assez de place", str(caught.exception))
        self.assertEqual(safety.list_backups(), [])


class ConfirmOrderTests(FakeSectionsMixin, TransactionTestCase):
    """Through the page: the backups exist before any section writes, and a
    backup that fails imports nothing."""

    serialized_rollback = True

    def setUp(self):
        super().setUp()
        empty_backups()
        self.addCleanup(empty_backups)
        fake_row("fournisseurs", "Fournisseur A")
        fake_row("recettes", "Recette A")
        fake_row("associations", "Article A")
        reader = export_archive({"recettes", "associations", "fournisseurs"})
        reader.close()
        with open(reader.path, "rb") as handle:
            from django.core.files.uploadedfile import SimpleUploadedFile

            self.stage = staging.stage_upload(SimpleUploadedFile("archive.zip", handle.read()))
        self.url = reverse("transfer:data_import_stage", args=[self.stage.token])
        self.posted = {"sections": ["recettes", "associations", "fournisseurs"], "strategie-recettes": "remplacer"}

    def preview_then_confirm(self):
        from inventory.models import StockType

        StockType.objects.filter(name="Recette A").update(loss_percent="12.00")  # something to replace
        self.client.post(self.url, {**self.posted, "action": "previsualiser"})
        apercu = shown_preview(self.client.get(self.url))
        return self.client.post(self.url, {**self.posted, "action": "importer", "apercu": apercu})

    def test_a_replace_is_backed_up_before_any_section_applies(self):
        seen = []

        def backups_exist():
            seen.append(sorted(backup.kind for backup in safety.list_backups()))

        FakeSection.probe["fournisseurs"] = backups_exist
        response = self.preview_then_confirm()
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        # The preview ran before any backup; the confirm, after both.
        self.assertEqual(seen, [[], ["sqlite", "zip"]])

    def test_a_merge_backs_up_the_database_only(self):
        self.posted["strategie-recettes"] = "fusionner"
        self.preview_then_confirm()
        self.assertEqual(sorted(backup.kind for backup in safety.list_backups()), ["sqlite"])

    def test_a_failed_backup_imports_nothing(self):
        with mock.patch("transfer.safety.backup_database", side_effect=OSError("disque plein")):
            response = self.preview_then_confirm()
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        applies = [call for call in FakeSection.calls if call[0] == "apply"]
        self.assertEqual(len(applies), 3)  # the preview's three, and no confirm
        self.assertIsNotNone(staging.get(self.stage.token))
        messages = [str(message) for message in response.wsgi_request._messages]
        self.assertIn("Sauvegarde impossible (disque plein) : rien n'a été changé.", messages)

    def test_the_strategy_reaches_the_run(self):
        self.preview_then_confirm()
        from inventory.models import StockType

        self.assertEqual(str(StockType.objects.get(name="Recette A").loss_percent), "10.00")
        self.assertEqual(Strategy.REPLACE.value, "remplacer")


def said(response) -> list[str]:
    messages = [str(message) for message in get_messages(response.wsgi_request)]
    response.client.cookies.pop("messages", None)
    return messages


def paid(invoice, n: int) -> InvoicePayment:
    """A bank line paying this invoice, linked by the automatic pass."""
    line = BankTransaction.objects.create(
        account="****0042",
        operation_date=date(2026, 8, 3),
        label=f"PAIEMENT ESSAI SURETE {n}",
        amount=Decimal("-12.30"),
        fingerprint=hashlib.sha256(f"essai-surete-{n}".encode()).hexdigest(),
    )
    return InvoicePayment.objects.create(transaction=line, invoice=invoice, method=InvoicePayment.Method.AUTO)


class ThroughThePageTests(TransactionTestCase):
    """The reviewers' scenarios (19/09), with the real sections and real
    backups: what a report sends the owner back to is in the archive taken
    before, and a confirm is the run its preview showed - or nothing."""

    serialized_rollback = True

    def setUp(self):
        empty_backups()
        self.addCleanup(empty_backups)
        self.files = []
        self.addCleanup(lambda: [default_storage.delete(name) for name in self.files if default_storage.exists(name)])
        self.metro = Supplier.objects.get(code="METRO")
        self.kept = make_invoice(
            self.metro, invoice_number="M-SURETE-0001",
            source_file=self.store("invoices/2026/09/metro_surete_0001.pdf", b"%PDF-1.4 surete 0001"),
        )

    def store(self, name: str, data: bytes) -> str:
        if default_storage.exists(name):
            default_storage.delete(name)
        name = default_storage.save(name, ContentFile(data))
        self.files.append(name)
        return name

    def stage(self, keys) -> str:
        reader = export_archive(keys)
        reader.close()
        with open(reader.path, "rb") as handle:
            response = self.client.post(reverse("transfer:data_import"), {"archive": handle})
        return response["Location"]

    def archive_taken(self) -> Path:
        """The archive the last confirm took, as its report names it."""
        return Path(self.client.session["transfer.report"]["report"]["safety"]["archive"])

    def test_payments_a_replace_removes_are_in_the_archive_taken_before(self):
        url = self.stage({"factures", "fournisseurs"})
        extra = make_invoice(self.metro, invoice_number="M-APRES-EXPORT")
        paid(extra, 77)
        posted = {"sections": ["factures", "fournisseurs"], "strategie-factures": "remplacer", "strategie-fournisseurs": "fusionner"}
        self.client.post(url, {**posted, "action": "previsualiser"})
        page = self.client.get(url)
        self.assertContains(page, "ainsi qu'une archive de ce qui change")

        response = self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(page)})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        self.assertFalse(Invoice.objects.filter(invoice_number="M-APRES-EXPORT").exists())
        self.assertEqual(InvoicePayment.objects.count(), 0)
        with ArchiveReader(self.archive_taken()) as backup:
            self.assertEqual(backup.sections, {"factures", "banque"})
            self.assertIn("M-APRES-EXPORT", json.dumps(backup.section("banque").payload()))
            # And it gives them back.
            import_archive(backup, {"factures": MERGE, "banque": MERGE})
        self.assertEqual(InvoicePayment.objects.get().invoice, Invoice.objects.get(invoice_number="M-APRES-EXPORT"))

    def test_a_document_that_arrived_after_the_preview_is_not_pruned_unseen(self):
        url = self.stage({"factures", "fournisseurs"})
        posted = {"sections": ["factures", "fournisseurs"], "strategie-factures": "remplacer", "strategie-fournisseurs": "fusionner"}
        self.client.post(url, {**posted, "action": "previsualiser"})
        page = self.client.get(url)
        self.assertContains(page, 'value="importer"')
        self.assertNotContains(page, "ainsi qu'une archive")  # nothing to change, as previewed

        # Meanwhile a ticket is imported from Achats, its photo on disk.
        name = self.store("receipts/2026/09/ticket_surete_apres.jpg", b"\xff\xd8\xff photo arrivee apres l'apercu")
        fresh = make_invoice(Supplier.objects.get(code="MONOPRIX"), invoice_number="T-APRES-APERCU", source_file=name)
        response = self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(page)})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(said(response), [views.MOVED_IMPORT])
        self.assertTrue(Invoice.objects.filter(pk=fresh.pk).exists())
        self.assertTrue(default_storage.exists(name))
        page = self.client.get(url)
        self.assertContains(page, "ainsi qu'une archive de ce qui change")

        # Confirmed on the new preview, the archive taken before holds it.
        response = self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(page)})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        self.assertFalse(Invoice.objects.filter(pk=fresh.pk).exists())
        with zipfile.ZipFile(self.archive_taken()) as backup:
            self.assertIn(f"files/{name}", backup.namelist())
            self.assertIn("T-APRES-APERCU", backup.read("factures.json").decode())

    def test_a_payment_made_after_the_clear_preview(self):
        url = reverse("transfer:data_clear")
        posted = {"sections": sorted(registry.closure({"factures"}, "clear"))}
        self.client.post(url, {**posted, "action": "previsualiser"})
        shown = shown_preview(self.client.get(url))
        paid(self.kept, 91)

        response = self.client.post(url, {**posted, "action": "effacer", "confirmation": "EFFACER", "apercu": shown})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(said(response), [views.MOVED_CLEAR])
        self.assertEqual(InvoicePayment.objects.count(), 1)
        self.assertTrue(Invoice.objects.filter(pk=self.kept.pk).exists())
        page = self.client.get(url)
        self.assertContains(page, "Banque › paiements")

        response = self.client.post(url, {**posted, "action": "effacer", "confirmation": "EFFACER", "apercu": shown_preview(page)})
        self.assertRedirects(response, url + "?rapport=1", fetch_redirect_response=False)
        self.assertEqual(InvoicePayment.objects.count(), 0)
        with ArchiveReader(self.archive_taken()) as backup:
            self.assertIn("banque", backup.sections)
            self.assertIn("M-SURETE-0001", json.dumps(backup.section("banque").payload()))

    def test_a_confirm_is_held_to_the_preview_on_its_own_page(self):
        """Two tabs on one stage (« Reprendre » makes it easy): tab A shows
        « 0 à supprimer » and no archive, a ticket arrives, tab B previews
        the same selection again. « Importer » clicked on tab A deleted the
        ticket and its photo - held to tab B's preview, not the one on its
        screen (review, 19/09)."""
        url = self.stage({"factures", "fournisseurs"})
        posted = {"sections": ["factures", "fournisseurs"], "strategie-factures": "remplacer", "strategie-fournisseurs": "fusionner"}
        self.client.post(url, {**posted, "action": "previsualiser"})
        tab_a = self.client.get(url)
        self.assertNotContains(tab_a, "ainsi qu'une archive")

        name = self.store("receipts/2026/09/ticket_deux_onglets.jpg", b"\xff\xd8\xff photo deux onglets")
        fresh = make_invoice(Supplier.objects.get(code="MONOPRIX"), invoice_number="T-DEUX-ONGLETS", source_file=name)
        self.client.post(url, {**posted, "action": "previsualiser"})
        tab_b = self.client.get(url)
        self.assertContains(tab_b, "ainsi qu'une archive de ce qui change")

        response = self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(tab_a)})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(said(response), [views.MOVED_IMPORT])
        self.assertTrue(Invoice.objects.filter(pk=fresh.pk).exists())
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(safety.list_backups(), [])  # refused before any backup

        # From the page showing what will go, it goes - into the archive first.
        response = self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(tab_b)})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        self.assertFalse(Invoice.objects.filter(pk=fresh.pk).exists())
        with zipfile.ZipFile(self.archive_taken()) as backup:
            self.assertIn(f"files/{name}", backup.namelist())

    def test_a_link_a_replace_makes_again_is_undone_by_the_archive_taken_before(self):
        """A person unlinks a line settled by hand; « Remplacer » on the bank
        only makes its payment again, the line's record equal to the
        archive's - nothing that existed changed. The archive taken before
        lacked « Banque », and importing it back, the undo it is for, left
        the link in place (review, 19/09)."""
        line = BankTransaction.objects.create(
            account="****0042",
            operation_date=date(2026, 8, 10),
            label="VIR ESSAI SURETE REGLE A LA MAIN",
            amount=Decimal("-45.60"),
            fingerprint=hashlib.sha256(b"essai-surete-main").hexdigest(),
            settled_by_hand=True,
        )
        InvoicePayment.objects.create(transaction=line, invoice=self.kept, method=InvoicePayment.Method.MANUAL)
        url = self.stage({"banque"})
        reconcile.unlink(line)

        posted = {"sections": ["banque"], "strategie-banque": "remplacer"}
        self.client.post(url, {**posted, "action": "previsualiser"})
        page = self.client.get(url)
        self.assertContains(page, "ainsi qu'une archive de ce qui change")
        response = self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(page)})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        self.assertEqual(InvoicePayment.objects.get().transaction, line)

        with ArchiveReader(self.archive_taken()) as backup:
            self.assertEqual(backup.sections, {"banque"})
            import_archive(backup, {"banque": REPLACE})
        self.assertFalse(InvoicePayment.objects.exists())
        line.refresh_from_db()
        self.assertTrue(line.settled_by_hand)
