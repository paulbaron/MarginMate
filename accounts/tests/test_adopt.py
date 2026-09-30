"""`manage.py adopt_database` (accounts/adoption.py): the owner's current
database becomes his tenant - a copy, never the source.

The source is built here, never the real db.sqlite3: a tenant of its own
stands in for the owner's database (a supplier, an invoice with its PDF, an
employee's four signing requests), one migration behind, with the central
tables a single-mode database carries (logins, sessions) added to it, and
folders beside it like the owner's (media with a waiting upload, private,
downloads, backups). Everything invented."""

import hashlib
import sqlite3
from datetime import date, timedelta
from html import unescape
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from accounts import adoption, links, paths
from accounts.models import Membership, SigningLink, Tenant, hash_secret
from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from invoices.models import Invoice, Supplier
from staff.models import Employee, SignatureRequest, Timesheet
from staff.signature_requests import CANCELLED_LINK, SUPERSEDED_LINK
from tests.factories import make_invoice, make_supplier

LEAF = ("invoices", "0035_supplier_typed_identifiers")
BEHIND = "0034"
STATUS = SignatureRequest.Status
TOKENS = {
    STATUS.PENDING: "lien-en-attente",
    STATUS.COMPLETE: "lien-signe",
    STATUS.CANCELLED: "lien-annule",
    STATUS.SUPERSEDED: "lien-remplace",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listing(folder: Path) -> list:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


def stored_bytes(database: Path) -> bytes:
    """Everything SQLite holds for that database on disk: the file and its -wal."""
    wal = Path(f"{database}-wal")
    return database.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


class AdoptTestCase(TenancyTestCase):
    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user(
            username="proprio", email="proprio@example.invalid", password="x", is_staff=True, is_superuser=True
        )
        self.old = self.make_tenant("Base à adopter")
        with bound_tenant(self.old):
            supplier = make_supplier(code="T-ORIGINE", name="Grossiste d'Origine")
            invoice = make_invoice(supplier=supplier, invoice_number="F-ORIGINE")
            invoice.source_file.save("facture-origine.pdf", ContentFile(b"%PDF-1.4 facture d'essai"))
            self.invoice_file = invoice.source_file.name
            employee = Employee.objects.create(last_name="Exemple", first_name="Camille", tuesday_hours=7)
            june = Timesheet.objects.create(employee=employee, month=date(2026, 6, 1))
            july = Timesheet.objects.create(employee=employee, month=date(2026, 7, 1))
            # One request holds a month at a time: June's first two versions
            # went, its third was signed; July's waits.
            for timesheet, version, status in (
                (june, 1, STATUS.CANCELLED),
                (june, 2, STATUS.SUPERSEDED),
                (june, 3, STATUS.COMPLETE),
                (july, 1, STATUS.PENDING),
            ):
                SignatureRequest.objects.create(
                    timesheet=timesheet,
                    version=version,
                    status=status,
                    token_hash=hash_secret(TOKENS[status]),
                    expires_at=timezone.now() + timedelta(days=10),
                    document_sha256="0" * 64,
                )
            call_command("migrate", LEAF[0], BEHIND, verbosity=0, skip_checks=True)
        folder = paths.tenant_dir(self.old)
        self.source = folder / "db.sqlite3"
        # What a single-mode database carries besides: its logins and sessions.
        connection = sqlite3.connect(self.source)
        connection.executescript(
            """
            CREATE TABLE auth_user (id integer PRIMARY KEY, username varchar(150), password varchar(128));
            INSERT INTO auth_user VALUES (1, 'proprio', 'pbkdf2_sha256$essai'), (2, 'autre', 'pbkdf2_sha256$essai');
            CREATE TABLE django_session (session_key varchar(40) PRIMARY KEY, session_data text, expire_date datetime);
            INSERT INTO django_session VALUES ('cle-de-session-essai', 'e30', '2099-01-01');
            """
        )
        connection.close()
        self.media = folder / "media"
        waiting = self.media / adoption.RECEIPT_BATCHES / "7"
        waiting.mkdir(parents=True)
        (waiting / "0000.jpg").write_bytes(b"photo d'un ticket")
        self.private = folder / "private"
        (self.private / "keys").mkdir()
        (self.private / "keys" / "authority.key.pem").write_text("CLE D'ESSAI")
        (self.private / "deletions.log").write_text("{}\n")
        self.downloads = folder / "downloads"
        (self.downloads / "type-1").mkdir()
        (self.downloads / "type-1" / "facture-portail.pdf").write_bytes(b"%PDF-1.4 portail")
        self.backups = folder / "backups"
        (self.backups / "sauvegarde.zip").write_bytes(b"PK essai")

    def adopt(self, *extra, email="proprio@example.invalid", source=None):
        out = StringIO()
        call_command(
            "adopt_database",
            "--email",
            email,
            "--name",
            "Le Bar du Propriétaire",
            "--from",
            str(source or self.source),
            "--media",
            str(self.media),
            "--private",
            str(self.private),
            "--downloads",
            str(self.downloads),
            "--backups",
            str(self.backups),
            *extra,
            stdout=out,
        )
        return out.getvalue()

    def adopted(self) -> Tenant:
        return Tenant.objects.get(name="Le Bar du Propriétaire")


class DryRunTests(AdoptTestCase):
    def test_it_says_everything_and_writes_nothing(self):
        before = (digest(self.source), listing(paths.tenants_root()), Tenant.objects.count())
        output = self.adopt("--dry-run")
        self.assertEqual((digest(self.source), listing(paths.tenants_root()), Tenant.objects.count()), before)
        self.assertFalse(Membership.objects.filter(user=self.owner).exists())
        self.assertFalse(SigningLink.objects.exists())
        for said in (
            "Essai à blanc : rien n'a été écrit.",
            "lue seulement, jamais modifiée",
            "proprio (superutilisateur)",
            "« Le Bar du Propriétaire », avec les accès du serveur",
            "1 documents (factures et tickets)",
            "1 salariés, 4 demandes de signature",
            "auth_user, django_session",
            "Comptes de la base d'origine : 2, non repris",
            # Every request's link, cancelled and superseded ones included.
            "Liens de signature à indexer : 4.",
            f"{LEAF[0]}.{LEAF[1]}",
            f"dont 1 en attente d'import ({adoption.RECEIPT_BATCHES}/) -> imports/",
        ):
            self.assertIn(said, output)

    def test_what_it_says_can_be_written_to_a_windows_log_file(self):
        """Python on Windows writes a redirected or piped output in the ANSI
        code page (cp1252): « adopt_database … > adoption.log » or
        « | Tee-Object ». A character outside it - an arrow « → » in the
        folders' lines - stopped the command before it did anything."""
        raw = BytesIO()
        out = TextIOWrapper(raw, encoding="cp1252")
        call_command(
            "adopt_database",
            "--email",
            "proprio@example.invalid",
            "--name",
            "Le Bar du Propriétaire",
            "--from",
            str(self.source),
            "--media",
            str(self.media),
            "--private",
            str(self.private),
            "--dry-run",
            stdout=out,
        )
        out.flush()
        written = raw.getvalue().decode("cp1252")
        self.assertIn("Essai à blanc : rien n'a été écrit.", written)
        self.assertIn("en attente d'import", written)


class RealRunTests(AdoptTestCase):
    def test_the_tenant_is_a_migrated_copy_with_his_files_and_his_links(self):
        before = (digest(self.source), listing(paths.tenant_dir(self.old)))
        output = self.adopt()
        # The source: not a byte changed, nothing created beside it.
        self.assertEqual((digest(self.source), listing(paths.tenant_dir(self.old))), before)

        tenant = self.adopted()
        self.assertIn(f"Fait. Espace « Le Bar du Propriétaire » ouvert, dossier {tenant.dir_name}", output)
        self.assertTrue(tenant.is_active)
        self.assertTrue(tenant.uses_server_integrations)
        self.assertNotEqual(tenant.dir_name, self.old.dir_name)
        membership = Membership.objects.get(user=self.owner)
        self.assertEqual((membership.tenant, membership.role), (tenant, Membership.Role.OWNER))

        # Every link he already sent finds his tenant - a cancelled or a
        # superseded one too, whose page must go on saying so (below).
        for status, token in TOKENS.items():
            with self.subTest(status=status):
                self.assertEqual(links.resolve(hash_secret(token)), tenant)

        with bound_tenant(tenant):
            self.assertTrue(Supplier.objects.filter(name="Grossiste d'Origine").exists())
            # Migrated, bound to itself.
            self.assertIn(LEAF, MigrationRecorder(connections["default"]).applied_migrations())
            tables = connections["default"].introspection.table_names()
            # The central tables left the copy; their migrations stay recorded,
            # as in a tenant made from the template.
            self.assertNotIn("auth_user", tables)
            self.assertNotIn("django_session", tables)
            self.assertIn(("auth", "0001_initial"), MigrationRecorder(connections["default"]).applied_migrations())
            invoice = Invoice.objects.get(invoice_number="F-ORIGINE")
            with invoice.source_file.open("rb") as handle:
                self.assertEqual(handle.read(), b"%PDF-1.4 facture d'essai")

        tenant_dir = paths.tenant_dir(tenant)
        self.assertEqual(
            (tenant_dir / "imports" / adoption.RECEIPT_BATCHES / "7" / "0000.jpg").read_bytes(), b"photo d'un ticket"
        )
        self.assertFalse((tenant_dir / "media" / adoption.RECEIPT_BATCHES).exists())
        self.assertEqual((tenant_dir / "private" / "keys" / "authority.key.pem").read_text(), "CLE D'ESSAI")
        self.assertTrue((tenant_dir / "private" / "deletions.log").is_file())
        self.assertTrue((tenant_dir / "downloads" / "type-1" / "facture-portail.pdf").is_file())
        self.assertTrue((tenant_dir / "backups" / "sauvegarde.zip").is_file())

    def test_the_central_tables_leave_no_byte_behind(self):
        """A DROP only frees the pages: their bytes stayed in the tenant's
        file - the owner's password hashes and live session keys - and in
        every safety copy of it (the backup API copies the freelist too)."""
        self.assertGreater(stored_bytes(self.source).count(b"pbkdf2_sha256$essai"), 0)
        self.adopt()
        data = stored_bytes(paths.tenant_database(self.adopted()))
        self.assertEqual(data.count(b"pbkdf2_sha256$essai"), 0)
        self.assertEqual(data.count(b"cle-de-session-essai"), 0)

    def test_a_cancelled_or_superseded_link_still_says_so_once_adopted(self):
        """The index keeps a link until its request is deleted or purged, as
        the staff pages do from the first day: left out, the employee opening
        a cancelled month's link after the adoption read « lien inconnu,
        vérifiez qu'il a été copié en entier » (404) where single mode says
        « annulée par l'employeur » (410)."""
        self.adopt()
        for status, said in ((STATUS.CANCELLED, CANCELLED_LINK), (STATUS.SUPERSEDED, SUPERSEDED_LINK)):
            with self.subTest(status=status):
                response = Client().get(reverse("staff:sign", args=[TOKENS[status]]))
                self.assertEqual(response.status_code, 410)
                self.assertIn(said, unescape(response.content.decode()))

    def test_his_pages_and_his_files_answer_once_adopted(self):
        self.adopt()
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("invoices:supplier_list")), "Grossiste d&#x27;Origine")
        response = self.client.get(reverse("accounts:media", args=[self.invoice_file]))
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 facture d'essai")

    def test_a_database_his_server_is_writing_is_copied_with_its_last_writes(self):
        """WAL mode, a writer still open: its last rows are only in the -wal,
        which a read-only connection reads - and leaves as it was."""
        writer = sqlite3.connect(self.source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE ecrit_en_dernier (valeur text)")
        writer.execute("INSERT INTO ecrit_en_dernier VALUES ('ligne du journal')")
        writer.commit()
        wal = Path(f"{self.source}-wal")
        self.assertGreater(wal.stat().st_size, 0)
        before = (digest(self.source), digest(wal))
        self.adopt()
        self.assertEqual((digest(self.source), digest(wal)), before)
        copy = sqlite3.connect(paths.tenant_database(self.adopted()))
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT valeur FROM ecrit_en_dernier").fetchall(), [("ligne du journal",)])

    def test_a_wal_database_nobody_has_open_is_read_without_leaving_files_beside_it(self):
        writer = sqlite3.connect(self.source)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.close()
        self.assertFalse(Path(f"{self.source}-wal").exists())
        self.assertIn("immutable=1", adoption.read_only_uri(self.source))
        before = (digest(self.source), listing(paths.tenant_dir(self.old)))
        self.adopt()
        self.assertEqual((digest(self.source), listing(paths.tenant_dir(self.old))), before)
        with bound_tenant(self.adopted()):
            self.assertTrue(Supplier.objects.filter(name="Grossiste d'Origine").exists())

    def test_a_failure_leaves_nothing_behind(self):
        before = (digest(self.source), listing(paths.tenants_root()), Tenant.objects.count())
        with mock.patch.object(adoption.provisioning, "migrate_tenant", side_effect=RuntimeError("panne d'essai")):
            with self.assertRaisesMessage(RuntimeError, "panne d'essai"):
                self.adopt()
        self.assertEqual((digest(self.source), listing(paths.tenants_root()), Tenant.objects.count()), before)
        self.assertFalse(Membership.objects.filter(user=self.owner).exists())
        self.assertFalse(SigningLink.objects.exists())


class RefusalTests(AdoptTestCase):
    def assertRefused(self, *extra, said, **kwargs):
        before = (Tenant.objects.count(), listing(paths.tenants_root()))
        with self.assertRaises(CommandError) as refused:
            self.adopt(*extra, **kwargs)
        self.assertIn(said, str(refused.exception))
        self.assertEqual((Tenant.objects.count(), listing(paths.tenants_root())), before)

    def test_the_login_must_exist_and_be_one(self):
        self.assertRefused(email="inconnu@example.invalid", said="Aucun compte")
        get_user_model().objects.create_user(username="double", email="proprio@example.invalid", password="x")
        self.assertRefused(said="Aucun compte (un seul)")

    def test_a_login_already_in_a_tenant_needs_leave_current(self):
        signed_up = self.make_tenant("Bar de l'inscription")
        Membership.objects.create(user=self.owner, tenant=signed_up)
        self.assertRefused(said="--leave-current")
        self.adopt("--leave-current")
        signed_up.refresh_from_db()
        self.assertFalse(signed_up.is_active)
        self.assertTrue(paths.tenant_database(signed_up).is_file())
        self.assertEqual([m.tenant for m in Membership.objects.filter(user=self.owner)], [self.adopted()])

    def test_leave_current_never_closes_a_tenant_others_work_in(self):
        shared = self.make_tenant("Bar partagé")
        Membership.objects.create(user=self.owner, tenant=shared)
        self.make_member(shared, "collegue@example.invalid")
        self.assertRefused("--leave-current", said="d'autres membres")
        shared.refresh_from_db()
        self.assertTrue(shared.is_active)

    def test_a_second_tenant_using_the_server_s_accounts_is_refused(self):
        """One Metro account, one pause (accounts.checks.one_owner_tenant):
        the adoption is the one thing that makes such a tenant, so it
        refuses a second - unless the login is leaving that very one."""
        first = self.make_tenant("Bar déjà adopté", owner=True)
        self.assertRefused(said="utilise déjà les accès du serveur")
        Membership.objects.create(user=self.owner, tenant=first)
        self.adopt("--leave-current")
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertEqual(list(Tenant.objects.filter(is_active=True, uses_server_integrations=True)), [self.adopted()])

    def test_only_a_marginmate_database_is_adopted(self):
        missing = paths.tenants_root().parent / "absente.sqlite3"
        self.assertRefused(source=missing, said="aucun fichier")
        other = paths.tenants_root().parent / "autre.sqlite3"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE autre (x)")
        connection.close()
        self.assertRefused(source=other, said="pas une base MarginMate")
        text = paths.tenants_root().parent / "texte.sqlite3"
        text.write_text("ceci n'est pas une base " * 50)
        self.assertRefused(source=text, said="ne se lit pas")

    def test_a_folder_holding_the_tenants_is_never_copied(self):
        with self.assertRaises(CommandError) as refused:
            call_command(
                "adopt_database",
                "--email",
                "proprio@example.invalid",
                "--name",
                "Bar",
                "--from",
                str(self.source),
                "--media",
                str(paths.tenants_root().parent),
                stdout=StringIO(),
            )
        self.assertIn("contient TENANTS_ROOT", str(refused.exception))

    def test_a_name_is_required(self):
        with self.assertRaises(CommandError):
            call_command(
                "adopt_database",
                "--email",
                "proprio@example.invalid",
                "--name",
                "   ",
                "--from",
                str(self.source),
                stdout=StringIO(),
            )


class ConstantsTests(SimpleTestCase):
    def test_waiting_uploads_are_the_tickets_folder(self):
        from invoices.receipt_batches import STAGING_DIR

        self.assertEqual(adoption.RECEIPT_BATCHES, STAGING_DIR)
