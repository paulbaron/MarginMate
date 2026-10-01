"""« Données » in multi mode: a tenant's backups, staged archives, exports
and files are its own (accounts.paths), and nothing on the page names a
folder of the server.

Two tenants in temporary files (accounts.tests.support.TwoTenantsTestCase).
With one backups folder and one staging folder for the whole server, bar B
listed bar A's safety copies on its Importer tab and could stage and import
them - A's invoices, bank and prices - and resume or cancel the archive A
had just sent. Each test below failed before the folders became the
tenant's own.
"""

import json
import sqlite3
from datetime import date
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.messages import get_messages
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts import paths
from accounts.models import Membership
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices import integrations
from invoices.integrations import TO_CONFIGURE
from invoices.models import Invoice, InvoiceType, Supplier
from recipes.integration import TILL_TO_CONFIGURE
from recipes.models import PosDailyPayment
from tests.factories import make_invoice
from transfer import archive, registry, safety, staging, views
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear, run_export
from transfer.sections import invoices as invoices_section
from transfer.sections import sales as sales_section
from transfer.sections.base import ImportContext, Strategy
from transfer.tests.support import export_archive, import_archive
from transfer.tests.test_sources_section import ENV_NOTE, build_sources, portal_note
from transfer.tests.test_views import shown_preview

GONE_SAID = [views.GONE]
NO_SUCH_BACKUP = ["Cette sauvegarde n'existe pas (ou plus)."]
ADMINISTRATOR = "Seul l'administrateur peut revenir à une copie"


def said(response) -> list[str]:
    messages = [str(message) for message in get_messages(response.wsgi_request)]
    response.client.cookies.pop("messages", None)
    return messages


def own_backup(path) -> bool:
    """ImportContext.own_backup of an archive at `path` (only the reader's
    path is read)."""
    return ImportContext.own_backup.fget(SimpleNamespace(reader=SimpleNamespace(path=path)))


class TenantTestCase(TwoTenantsTestCase):
    def a_supplier(self, tenant, code, name):
        with bound_tenant(tenant):
            return Supplier.objects.create(code=code, name=name)

    def backups_of(self, tenant) -> dict[str, str]:
        """What a confirmed clear of the suppliers takes first, in `tenant`."""
        with bound_tenant(tenant):
            return safety.before("effacement", {"fournisseurs"})

    def upload_of(self, tenant) -> staging.Stage:
        """An archive of `tenant`'s suppliers, sent to its Importer tab."""
        with bound_tenant(tenant):
            path = paths.staging_dir() / "envoyee-par-essai.zip"
            run_export({"fournisseurs"}, path)
            stage = staging.stage_upload(SimpleUploadedFile("archive.zip", path.read_bytes()))
            path.unlink()
        return stage

    def page(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)


class BackupsTests(TenantTestCase):
    def test_each_tenant_backs_up_into_its_own_folder(self):
        self.a_supplier(self.bar_a, "ALPHA_ESSAI", "Grossiste Alpha")
        backups = self.backups_of(self.bar_a)
        folder = paths.tenant_dir(self.bar_a) / paths.BACKUPS
        self.assertEqual(Path(backups["database"]).parent, folder)
        self.assertEqual(Path(backups["archive"]).parent, folder)
        with sqlite3.connect(backups["database"]) as copy:
            names = [row[0] for row in copy.execute("select name from invoices_supplier where code = 'ALPHA_ESSAI'")]
        copy.close()
        self.assertEqual(names, ["Grossiste Alpha"])  # A's database, not the empty default
        with bound_tenant(self.bar_b):
            self.assertEqual(safety.list_backups(), [])
        with bound_tenant(self.bar_a):
            self.assertEqual(sorted(backup.kind for backup in safety.list_backups()), ["sqlite", "zip"])

    def test_b_never_lists_stages_or_imports_a_s_backup(self):
        self.a_supplier(self.bar_a, "ALPHA_ESSAI", "Grossiste Alpha")
        backups = self.backups_of(self.bar_a)
        zipped, copied = Path(backups["archive"]).name, Path(backups["database"]).name
        with bound_tenant(self.bar_b):
            self.assertIsNone(safety.find_backup(zipped))
            with self.assertRaises(ArchiveError):
                staging.stage_backup(zipped)

        page = self.page(self.user_b, reverse("transfer:data_import"))
        self.assertNotContains(page, zipped)
        self.assertNotContains(page, copied)
        response = self.client.post(reverse("transfer:data_import_backup"), {"nom": zipped})
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(said(response), NO_SUCH_BACKUP)
        with bound_tenant(self.bar_b):
            self.assertEqual(staging.pending(), [])

        page = self.page(self.user_a, reverse("transfer:data_import"))
        self.assertContains(page, f'name="nom" value="{zipped}"')
        self.assertContains(page, copied)

    def test_an_archive_is_its_own_backup_in_its_own_tenant_only(self):
        """What restores a portal ACTIVE (sources.py): A's backup, copied or
        referenced from B, is an archive from elsewhere there."""
        archive_path = self.backups_of(self.bar_a)["archive"]
        with bound_tenant(self.bar_a):
            self.assertTrue(own_backup(archive_path))
        with bound_tenant(self.bar_b):
            self.assertFalse(own_backup(archive_path))
            copy = paths.staging_dir() / Path(archive_path).name
            copy.write_bytes(Path(archive_path).read_bytes())
            self.assertFalse(own_backup(copy))
            with ArchiveReader(Path(archive_path)) as reader:
                self.assertEqual(reader.reason, "sauvegarde avant effacement")  # a manifest says what anybody writes

    def test_the_size_is_the_bound_tenant_s_database(self):
        with bound_tenant(self.bar_a):
            size = safety.database_size()
        self.assertEqual(size, paths.tenant_database(self.bar_a).stat().st_size)
        self.assertGreater(size, 0)


class StagingTests(TenantTestCase):
    def test_b_never_sees_resumes_nor_cancels_a_s_archive(self):
        self.a_supplier(self.bar_a, "ALPHA_ESSAI", "Grossiste Alpha")
        stage = self.upload_of(self.bar_a)
        self.assertEqual(stage.dir.parent, paths.tenant_dir(self.bar_a) / paths.STAGING)
        url = reverse("transfer:data_import_stage", args=[stage.token])
        with bound_tenant(self.bar_b):
            self.assertIsNone(staging.get(stage.token))
            self.assertEqual(staging.pending(), [])

        page = self.page(self.user_b, reverse("transfer:data_import"))
        self.assertNotContains(page, stage.token)
        self.assertNotContains(page, "Archives en attente")
        response = self.client.get(url)
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(said(response), GONE_SAID)
        response = self.client.post(url, {"action": "annuler"})
        self.assertEqual(said(response), GONE_SAID)
        response = self.client.post(url, {"action": "previsualiser", "sections": ["fournisseurs"]})
        self.assertEqual(said(response), GONE_SAID)

        with bound_tenant(self.bar_a):
            self.assertEqual([waiting.token for waiting in staging.pending()], [stage.token])
        page = self.page(self.user_a, reverse("transfer:data_import"))
        self.assertContains(page, url)

    def test_the_sweep_stays_in_its_tenant(self):
        with bound_tenant(self.bar_a):
            old = staging.staging_dir() / ("O" * 22)
            old.mkdir()
            (old / staging.STATE).write_text(json.dumps({"created_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
        with bound_tenant(self.bar_b):
            self.assertEqual(staging.sweep(), 0)
        self.assertTrue(old.is_dir())
        with bound_tenant(self.bar_a):
            self.assertEqual(staging.sweep(), 1)
        self.assertFalse(old.exists())

    def test_an_export_waits_in_the_tenant_s_own_folder(self):
        with bound_tenant(self.bar_a):
            self.assertEqual(staging.exports_dir().parent, paths.tenant_dir(self.bar_a) / paths.STAGING)


class FilesTests(TenantTestCase):
    NAME = "invoices/2026/09/meme-nom-essai.pdf"

    def test_a_stored_name_is_checked_against_the_tenant_s_media(self):
        """Every file of an archive was refused « hors du dossier des
        fichiers »: the containment was checked against MEDIA_ROOT."""
        with bound_tenant(self.bar_a):
            self.assertIsNone(archive.storage_name_problem(self.NAME))
            self.assertIsNotNone(archive.storage_name_problem("invoices/../../db.sqlite3"))

    def test_the_orphan_scan_walks_the_tenant_s_own_media(self):
        with bound_tenant(self.bar_a):
            default_storage.save("invoices/2026/09/orpheline-alpha.pdf", ContentFile(b"%PDF-1.4 alpha"))
            self.assertEqual(invoices_section._orphan_files(set()), 1)
        with bound_tenant(self.bar_b):
            self.assertEqual(invoices_section._orphan_files(set()), 0)

    def test_the_size_cache_is_per_tenant(self):
        """Two tenants restored from one archive name the same files: the
        size kept a minute for A was B's « Mo de fichiers » too."""
        names = frozenset({self.NAME})
        with bound_tenant(self.bar_a):
            default_storage.save(self.NAME, ContentFile(b"a" * 10))
            self.assertEqual(invoices_section._bytes_of(names), 10)
        with bound_tenant(self.bar_b):
            default_storage.save(self.NAME, ContentFile(b"b" * 25))
            self.assertEqual(invoices_section._bytes_of(names), 25)
        with bound_tenant(self.bar_a):
            self.assertEqual(invoices_section._bytes_of(names), 10)


def no_folder_named(test, response, *tenants):
    """Nothing of the server's layout: no tenant's folder name, no root."""
    content = response.content.decode()
    for tenant in tenants:
        test.assertNotIn(tenant.dir_name, content)
    test.assertNotIn(str(test.tenants_root), content)
    test.assertNotIn(test.tenants_root.as_posix(), content)


class ThroughThePagesTests(TenantTestCase):
    def test_an_archive_a_hands_over_lands_in_b_s_own_files(self):
        with bound_tenant(self.bar_a):
            name = default_storage.save(
                "invoices/2026/09/facture-alpha-essai.pdf", ContentFile(b"%PDF-1.4 alpha essai")
            )
            make_invoice(Supplier.objects.get(code="METRO"), invoice_number="ALPHA-ESSAI-0001", source_file=name)
        keys = sorted(registry.closure({"factures"}, "export"))
        self.client.force_login(self.user_a)
        export = self.client.post(reverse("transfer:data_export"), {"sections": keys})
        data = b"".join(export.streaming_content)
        export.close()

        self.client.force_login(self.user_b)
        response = self.client.post(reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", data)})
        url = response["Location"]
        posted = {"sections": keys, **{f"strategie-{key}": "fusionner" for key in keys}}
        self.client.post(url, {**posted, "action": "previsualiser"})
        page = self.client.get(url)
        no_folder_named(self, page, self.bar_a, self.bar_b)
        self.assertContains(page, "les sauvegardes de votre espace")
        response = self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(page)})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        for message in said(response):
            self.assertTrue(message.startswith("Import terminé"), message)
            self.assertNotIn(self.bar_b.dir_name, message)
            self.assertIn("(dans les sauvegardes de votre espace)", message)
        report = self.client.get(reverse("transfer:data_import") + "?rapport=1")
        self.assertContains(report, "Import terminé")
        no_folder_named(self, report, self.bar_a, self.bar_b)

        with bound_tenant(self.bar_b):
            invoice = Invoice.objects.get(invoice_number="ALPHA-ESSAI-0001")
            stored = Path(default_storage.path(invoice.source_file.name))
            self.assertIn(paths.tenant_dir(self.bar_b) / paths.MEDIA, stored.parents)
            self.assertEqual(stored.read_bytes(), b"%PDF-1.4 alpha essai")
            self.assertEqual([backup.kind for backup in safety.list_backups()], ["sqlite"])
        with bound_tenant(self.bar_a):
            self.assertEqual(safety.list_backups(), [])
            self.assertEqual(Path(default_storage.path(name)).read_bytes(), b"%PDF-1.4 alpha essai")

    def test_the_page_names_no_folder_and_sends_a_restore_to_the_administrator(self):
        self.backups_of(self.bar_a)
        page = self.page(self.user_a, reverse("transfer:data_import"))
        self.assertContains(page, "Copies de la base")
        self.assertContains(page, ADMINISTRATOR)
        self.assertNotContains(page, "arrêtez le serveur, supprimez db.sqlite3-wal")
        self.assertNotContains(page, "c'est à vous de faire le ménage dans ce dossier")
        no_folder_named(self, page, self.bar_a)
        home = self.page(self.user_a, reverse("transfer:data_home"))
        # The header says a backup is made, not where: the place (never a
        # path) is said where it happens, on the previews checked below.
        self.assertContains(home, "une sauvegarde est faite avant tout remplacement ou")
        no_folder_named(self, home, self.bar_a)

        self.client.post(reverse("transfer:data_clear"), {"sections": ["banque"], "action": "previsualiser"})
        clear = self.client.get(reverse("transfer:data_clear"))
        self.assertContains(clear, "Une sauvegarde sera faite avant dans les sauvegardes de votre espace")
        no_folder_named(self, clear, self.bar_a)

    def test_a_failure_names_no_folder_of_the_server(self):
        with bound_tenant(self.bar_a):
            where = str(paths.backups_dir() / "2026-09-19_143012_avant-import.sqlite3")
            refused = OSError(28, "No space left on device", where)
            with (
                mock.patch("transfer.safety.backup_database", side_effect=refused),
                self.assertLogs("transfer.safety", "ERROR") as logged,
                self.assertRaises(safety.SafetyError) as caught,
            ):
                safety.before("import", set())
        # The detail is the administrator's, in the server's log.
        self.assertIs(logged.records[0].exc_info[1], refused)
        self.assertNotIn(self.bar_a.dir_name, str(caught.exception))
        self.assertEqual(
            str(caught.exception),
            "Sauvegarde impossible (erreur sur le serveur, à signaler à l'administrateur) : rien n'a été changé.",
        )

    def carry_the_session_to(self, membership, tenant):
        """Move the login to `tenant` and keep its session. Since 29/09 a
        move logs the session out (security audit LOAD-1,
        accounts/tests/test_sessions.py); the session's own keys carrying
        the tenant are the second line, tested here with the session's
        tenant written by hand."""
        from accounts.middleware import TENANT_SESSION_KEY

        Membership.objects.filter(pk=membership.pk).update(tenant=tenant)
        session = self.client.session
        session[TENANT_SESSION_KEY] = tenant.pk
        session.save()

    def test_a_pending_clear_and_its_report_stay_with_their_tenant(self):
        """A person who works for two tenants carries nothing of one into the
        other: the session is the platform's, one per login."""
        url = reverse("transfer:data_clear")
        self.client.force_login(self.user_a)
        self.client.post(url, {"sections": ["banque"], "action": "previsualiser"})
        self.assertContains(self.client.get(url), "Ce qui sera effacé")

        membership = Membership.objects.get(user=self.user_a)
        self.carry_the_session_to(membership, self.bar_b)
        page = self.client.get(url)
        self.assertNotContains(page, "Ce qui sera effacé")
        self.assertEqual(shown_preview(page), "")

        self.carry_the_session_to(membership, self.bar_a)
        page = self.client.get(url)
        response = self.client.post(
            url, {"sections": ["banque"], "action": "effacer", "confirmation": "EFFACER", "apercu": shown_preview(page)}
        )
        self.assertRedirects(response, url + "?rapport=1", fetch_redirect_response=False)
        said(response)

        self.carry_the_session_to(membership, self.bar_b)
        self.assertNotContains(self.client.get(url + "?rapport=1"), "Effacement terminé")
        self.carry_the_session_to(membership, self.bar_a)
        report = self.client.get(url + "?rapport=1")
        self.assertContains(report, "Effacement terminé")
        no_folder_named(self, report, self.bar_a)


class HostedBarWordingTests(TenantTestCase):
    """Bar Alpha is the owner's tenant, Bar Beta a hosted bar: Beta can
    neither edit the server's .env nor run a command on it, so « Données »
    never tells it to - it says « à configurer » as every other page does
    (invoices/integrations.py, recipes/integration.py)."""

    owner_a = True

    def text(self, user, url) -> str:
        return unescape(self.page(user, url).content.decode())

    def test_the_sections_described_tell_a_hosted_bar_nothing_to_edit_or_run(self):
        # The Exporter tab (/donnees/) and the Effacer tab both describe every section.
        for url in (reverse("transfer:data_home"), reverse("transfer:data_clear")):
            with self.subTest(url=url):
                hosted = self.text(self.user_b, url)
                self.assertNotIn(".env", hosted)
                self.assertNotIn("manage.py", hosted)
                self.assertNotIn("laddition_backfill", hosted)
                self.assertIn(f"portails clients : {TO_CONFIGURE}", hosted)
                self.assertIn(TILL_TO_CONFIGURE, hosted)
                owner = self.text(self.user_a, url)
                self.assertIn("dans le fichier .env (à recopier à la main sur un autre ordinateur)", owner)
                self.assertIn("(manage.py laddition_backfill_revenue, puis laddition_backfill_payments)", owner)
                self.assertNotIn(TO_CONFIGURE, owner)

    def till_payment(self, tenant):
        with bound_tenant(tenant):
            PosDailyPayment.objects.create(
                sold_on=date(2026, 3, 14), method=PosDailyPayment.CARD, amount="12.50", payments=1
            )

    def test_clearing_the_sales_names_the_payments_backfill_in_the_owner_s_tenant_only(self):
        self.till_payment(self.bar_a)
        self.till_payment(self.bar_b)
        with bound_tenant(self.bar_b):
            notes = run_clear({"ventes"}, preview=True).section("ventes").notes
        self.assertEqual(notes, [sales_section.PAYMENTS_NOTE_TO_CONFIGURE])
        self.assertIn(TILL_TO_CONFIGURE, notes[0])
        self.assertNotIn("manage.py", notes[0])
        with bound_tenant(self.bar_a):
            notes = run_clear({"ventes"}, preview=True).section("ventes").notes
        self.assertEqual(notes, [sales_section.PAYMENTS_NOTE])

    def portal_notes(self, tenant) -> list[str]:
        """A portal an archive brings back into `tenant`: what the report says."""
        with bound_tenant(tenant):
            build_sources()
            reader = export_archive({"sources"}, closed=False)
            self.addCleanup(reader.close)
            InvoiceType.objects.filter(name="Eau Essai").delete()
            return import_archive(reader, Strategy.MERGE).section("sources").notes

    def test_an_imported_portal_is_said_without_the_server_s_env_file_in_a_hosted_bar(self):
        notes = self.portal_notes(self.bar_b)
        self.assertEqual(notes, [f"Source « Eau Essai » (Eau Essai) : créée inactive. {integrations.PORTALS}"])
        self.assertNotIn(".env", " ".join(notes))
        self.assertEqual(self.portal_notes(self.bar_a), [portal_note("créée inactive"), ENV_NOTE])
