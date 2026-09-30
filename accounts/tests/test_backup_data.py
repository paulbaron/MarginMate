"""`manage.py backup_data` (accounts/data_backup.py): a dated, checked copy
of the data folder.

Every folder is a temporary one: the data folder (TenancyTestCase's
TENANTS_ROOT and its parent), the accounts database's file in it, the
destination. The .env copied is an invented one (`data_backup.env_file`
patched: the developer's own is never read), git is never asked
(`data_backup.git_commit` patched), and the clock says 01/10/2026 10:15:00
in Paris. Two tenants, the template, the accounts database, media and
private files - every name and figure invented, every file a few bytes.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import warnings
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from accounts import data_backup, paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from invoices.models import Supplier
from tests.factories import make_supplier

#: 10:15:00 in Paris (CEST, UTC+2).
MOMENT = datetime(2026, 10, 1, 8, 15, 0, tzinfo=UTC)
STAMP = "2026-10-01_101500"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
ENV_TEXT = "DJANGO_SECRET_KEY=cle-inventee-pour-ce-test-seulement\nMARGINMATE_HTTPS=1\n"


@contextlib.contextmanager
def accounts_at(path: Path):
    """The settings name `path` as the accounts database (the test's own
    connection is left alone: only backup_data reads the setting)."""
    databases = {**settings.DATABASES, "accounts": {**settings.DATABASES["accounts"], "NAME": str(path)}}
    with warnings.catch_warnings():
        # Overriding DATABASES warns: only the command reads it here.
        warnings.simplefilter("ignore")
        with override_settings(DATABASES=databases):
            yield


def rows(database: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def counts_of(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return data_backup.table_counts(connection)
    finally:
        connection.close()


class BackupDataTests(TenancyTestCase):
    def setUp(self):
        super().setUp()
        self.data = self.tenants_root.parent
        self.bar_a = self.make_tenant("Bar Alpha")
        self.bar_b = self.make_tenant("Bar Beta")
        with bound_tenant(self.bar_a):
            make_supplier(name="Grossiste Alpha")
        with bound_tenant(self.bar_b):
            make_supplier(name="Grossiste Beta")
            make_supplier(name="Grossiste Gamma")
        folder_a, folder_b = paths.tenant_dir(self.bar_a), paths.tenant_dir(self.bar_b)
        (folder_a / "media" / "factures").mkdir()
        (folder_a / "media" / "factures" / "facture-inventee.pdf").write_bytes(b"%PDF-1.4 facture inventee\n")
        (folder_a / "private" / "cles").mkdir()
        (folder_a / "private" / "cles" / "cle.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\ninventee\n")
        (folder_b / "media" / "ticket.jpg").write_bytes(bytes(range(256)))
        (self.data / "logs").mkdir()
        (self.data / "logs" / "marginmate.log").write_text("une ligne du journal\n", encoding="utf-8")

        # The accounts database, in the data folder as in production.
        self.accounts = self.data / "accounts.sqlite3"
        connection = sqlite3.connect(self.accounts)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE comptes (email TEXT)")
        connection.executemany(
            "INSERT INTO comptes VALUES (?)", [("alpha@example.invalid",), ("beta@example.invalid",)]
        )
        connection.commit()
        connection.close()

        env_folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-env-"))
        self.addCleanup(shutil.rmtree, env_folder, True)
        self.env = env_folder / ".env"
        self.env.write_text(ENV_TEXT, encoding="utf-8")
        self.enterContext(mock.patch.object(data_backup, "env_file", return_value=self.env))
        self.enterContext(mock.patch.object(data_backup, "git_commit", return_value=COMMIT))
        self.enterContext(mock.patch.object(data_backup, "_now", return_value=MOMENT))
        self.dest = Path(tempfile.mkdtemp(prefix="marginmate-tests-sauvegardes-"))
        self.addCleanup(shutil.rmtree, self.dest, True)

    def backup(self, *args, dest="default", accounts=None) -> tuple[str, str]:
        """`manage.py backup_data --dest <self.dest> *args`: (stdout, stderr).
        The streams stay readable on a CommandError (self.out, self.err)."""
        options = [] if dest is None else ["--dest", str(self.dest if dest == "default" else dest)]
        self.out, self.err = io.StringIO(), io.StringIO()
        with accounts_at(accounts or self.accounts):
            call_command("backup_data", *options, *args, stdout=self.out, stderr=self.err)
        return self.out.getvalue(), self.err.getvalue()

    def refused(self, *args, **kwargs) -> str:
        with self.assertRaises(CommandError) as refusal:
            self.backup(*args, **kwargs)
        self.assertEqual(refusal.exception.returncode, 1)
        return str(refusal.exception)

    def source_files(self) -> dict[str, bytes]:
        """Every file of the data folder, by relative path."""
        return {
            path.relative_to(self.data).as_posix(): path.read_bytes()
            for path in sorted(self.data.rglob("*"))
            if path.is_file()
        }

    def copied_files(self, folder: Path) -> dict[str, bytes]:
        data = folder / "data"
        return {
            path.relative_to(data).as_posix(): path.read_bytes() for path in sorted(data.rglob("*")) if path.is_file()
        }

    def database_paths(self) -> list[str]:
        return [
            "accounts.sqlite3",
            f"tenants/{paths.TEMPLATE_DIR}/db.sqlite3",
            f"tenants/{self.bar_a.dir_name}/db.sqlite3",
            f"tenants/{self.bar_b.dir_name}/db.sqlite3",
        ]

    # -- A whole backup ----------------------------------------------------------------------------------------------

    def test_the_data_folder_mirrored_every_database_checked(self):
        out, err = self.backup()
        self.assertEqual(err, "")
        folder = self.dest / STAMP
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [STAMP])
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [".env", "data", "manifest.json"])

        source, copied = self.source_files(), self.copied_files(folder)
        self.assertEqual(sorted(copied), sorted(source))
        for relative, content in source.items():
            if relative not in self.database_paths():
                with self.subTest(file=relative):
                    self.assertEqual(copied[relative], content)
        # The empty folders a tenant is made with are there too.
        self.assertTrue((folder / "data" / "tenants" / self.bar_b.dir_name / "downloads").is_dir())

        for relative in self.database_paths():
            with self.subTest(database=relative):
                copy = folder / "data" / relative
                self.assertEqual(rows(copy, "PRAGMA integrity_check"), [("ok",)])
                self.assertEqual(counts_of(copy), counts_of(self.data / relative))
        beta = folder / "data" / "tenants" / self.bar_b.dir_name / "db.sqlite3"
        names = {name for (name,) in rows(beta, f"SELECT name FROM {Supplier._meta.db_table}")}
        self.assertLessEqual({"Grossiste Beta", "Grossiste Gamma"}, names)
        self.assertNotIn("Grossiste Alpha", names)
        self.assertEqual((folder / ".env").read_text(encoding="utf-8"), ENV_TEXT)

        self.assertIn(f"Sauvegarde de {self.data}", out)
        self.assertIn("l'espace « Bar Beta »", out)
        self.assertIn("le modèle des nouveaux espaces", out)
        self.assertIn(".env : copié", out)
        self.assertIn(f"Sauvegarde terminée : {folder}", out)
        self.assertIn("Gardez plusieurs sauvegardes", out)

    def test_the_manifest(self):
        self.backup()
        folder = self.dest / STAMP
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "marginmate-sauvegarde")
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["created_at"], "2026-10-01T08:15:00Z")
        self.assertEqual(manifest["folder"], STAMP)
        self.assertEqual(manifest["source"], str(self.data))
        self.assertEqual(manifest["code_commit"], COMMIT)
        self.assertIs(manifest["env"], True)
        self.assertEqual(manifest["missing"], [])
        described = {entry["path"]: entry for entry in manifest["databases"]}
        self.assertEqual(sorted(described), sorted(self.database_paths()))
        self.assertEqual(described["accounts.sqlite3"]["role"], "comptes")
        self.assertEqual(described[f"tenants/{paths.TEMPLATE_DIR}/db.sqlite3"]["role"], "modele")
        alpha = described[f"tenants/{self.bar_a.dir_name}/db.sqlite3"]
        self.assertEqual((alpha["role"], alpha["espace"]), ("espace", "Bar Alpha"))
        for relative, entry in described.items():
            with self.subTest(database=relative):
                self.assertEqual(entry["tables"], counts_of(self.data / relative))
                self.assertEqual(entry["rows"], sum(entry["tables"].values()))
                self.assertEqual(entry["bytes"], (folder / "data" / relative).stat().st_size)
        self.assertEqual(described["accounts.sqlite3"]["tables"], {"comptes": 2})
        others = {
            relative: content for relative, content in self.copied_files(folder).items() if relative not in described
        }
        self.assertEqual(manifest["files"], {"count": len(others), "bytes": sum(len(c) for c in others.values())})

    def test_what_the_wal_holds_is_in_the_copy_and_the_wal_is_not(self):
        """A database's latest commits wait in its -wal (a server running,
        or killed before its checkpoint). Copied through SQLite, the copy
        holds them; the -wal and -shm themselves are never copied - put back
        beside another database, a -wal is read into it."""
        live = sqlite3.connect(self.accounts, isolation_level=None)
        self.addCleanup(live.close)
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO comptes VALUES ('dans-le-wal@example.invalid')")
        self.assertGreater((self.data / "accounts.sqlite3-wal").stat().st_size, 0)
        self.backup()
        folder = self.dest / STAMP
        copy = folder / "data" / "accounts.sqlite3"
        self.assertIn(("dans-le-wal@example.invalid",), rows(copy, "SELECT email FROM comptes"))
        leftovers = [path.name for path in (folder / "data").rglob("*") if path.name.endswith(("-wal", "-shm"))]
        self.assertEqual(leftovers, [])
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        accounts = next(entry for entry in manifest["databases"] if entry["path"] == "accounts.sqlite3")
        self.assertEqual(accounts["tables"], {"comptes": 3})

    def test_a_write_during_the_copy_is_not_in_it_and_fails_nothing(self):
        """The counts and the copy come from one snapshot: a server writing
        while the backup runs cannot make them disagree."""
        real = data_backup.table_counts
        calls = []

        def counting(connection):
            counts = real(connection)
            calls.append(counts)
            if len(calls) == 1:
                # The first count of the first database (the accounts one),
                # then somebody else writes before the copy is taken.
                other = sqlite3.connect(self.accounts, isolation_level=None)
                other.execute("INSERT INTO comptes VALUES ('pendant@example.invalid')")
                other.close()
            return counts

        with mock.patch.object(data_backup, "table_counts", side_effect=counting):
            self.backup()
        copy = self.dest / STAMP / "data" / "accounts.sqlite3"
        self.assertNotIn(("pendant@example.invalid",), rows(copy, "SELECT email FROM comptes"))
        self.assertIn(("pendant@example.invalid",), rows(self.accounts, "SELECT email FROM comptes"))

    def test_without_the_env(self):
        out, _ = self.backup("--sans-env")
        folder = self.dest / STAMP
        self.assertFalse((folder / ".env").exists())
        self.assertIs(json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["env"], False)
        self.assertIn(".env : non copié (--sans-env)", out)

    def test_the_default_destination_is_beside_the_data_folder(self):
        """C:\\MarginMate\\data -> C:\\MarginMate\\backups: from the settings."""
        expected = self.data.parent / "backups"
        self.addCleanup(shutil.rmtree, expected, True)
        self.assertEqual(data_backup.default_destination(), expected)
        self.backup(dest=None)
        self.assertTrue((expected / STAMP / "manifest.json").is_file())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_the_path_for_deploy_cmd(self):
        noted = self.dest.parent / f"{self.dest.name}-chemin.txt"
        self.addCleanup(noted.unlink, True)
        self.backup("--chemin-dans", str(noted))
        self.assertEqual(noted.read_text(encoding="utf-8"), f"{self.dest / STAMP}\n")

    def test_without_git_the_backup_is_made_all_the_same(self):
        with mock.patch.object(data_backup, "git_commit", return_value=None):
            out, _ = self.backup()
        manifest = json.loads((self.dest / STAMP / "manifest.json").read_text(encoding="utf-8"))
        self.assertIsNone(manifest["code_commit"])
        self.assertIn("Code : commit inconnu", out)

    def test_a_tenant_without_its_database_is_said(self):
        for leftover in paths.tenant_database(self.bar_b).parent.glob("db.sqlite3*"):
            leftover.unlink()
        out, _ = self.backup()
        manifest = json.loads((self.dest / STAMP / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["missing"]), 1)
        self.assertIn("« Bar Beta »", manifest["missing"][0])
        self.assertIn("À savoir : l'espace « Bar Beta »", out)
        self.assertNotIn(
            f"tenants/{self.bar_b.dir_name}/db.sqlite3", [entry["path"] for entry in manifest["databases"]]
        )

    def test_a_folder_no_tenant_names_is_backed_up_and_said(self):
        orphan = self.tenants_root / "ancien-espace"
        orphan.mkdir()
        connection = sqlite3.connect(orphan / "db.sqlite3")
        connection.execute("CREATE TABLE t (x)")
        connection.execute("INSERT INTO t VALUES (1)")
        connection.commit()
        connection.close()
        out, _ = self.backup()
        manifest = json.loads((self.dest / STAMP / "manifest.json").read_text(encoding="utf-8"))
        entry = next(e for e in manifest["databases"] if e["path"] == "tenants/ancien-espace/db.sqlite3")
        self.assertEqual((entry["role"], entry["tables"]), ("dossier sans espace", {"t": 1}))
        self.assertIn("le dossier « ancien-espace » (aucun espace ne le nomme)", out)

    # -- Refused: nothing written --------------------------------------------------------------------------------------

    def assertNothingWritten(self):
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_a_folder_already_there_is_refused(self):
        (self.dest / STAMP).mkdir()
        self.assertIn("existe déjà", self.refused())
        self.assertEqual([path.name for path in self.dest.iterdir()], [STAMP])
        self.assertEqual(list((self.dest / STAMP).iterdir()), [])
        shutil.rmtree(self.dest / STAMP)
        (self.dest / f"{STAMP}-INCOMPLET").mkdir()
        self.assertIn("existe déjà", self.refused())

    def test_a_destination_inside_the_data_or_the_code_is_refused(self):
        inside_data = self.data / "sauvegardes"
        self.assertIn("se copierait elle-même", self.refused(dest=inside_data))
        self.assertFalse(inside_data.exists())
        inside_code = Path(settings.BASE_DIR) / "sauvegarde-refusee-par-le-test"
        self.assertIn("dans le dossier du code", self.refused(dest=inside_code))
        self.assertFalse(inside_code.exists())

    def test_the_accounts_database_must_be_there_and_in_the_data_folder(self):
        missing = self.data / "comptes-absents.sqlite3"
        self.assertIn("la base des comptes est introuvable", self.refused(accounts=missing))
        self.assertFalse(missing.exists())
        elsewhere = Path(tempfile.mkdtemp(prefix="marginmate-tests-ailleurs-"))
        self.addCleanup(shutil.rmtree, elsewhere, True)
        shutil.copyfile(self.accounts, elsewhere / "accounts.sqlite3")
        self.assertIn("n'est pas dans le dossier des données", self.refused(accounts=elsewhere / "accounts.sqlite3"))
        self.assertNothingWritten()

    def test_the_code_s_folder_is_no_data_folder(self):
        """TENANTS_ROOT left at its default: beside manage.py."""
        with override_settings(TENANTS_ROOT=Path(settings.BASE_DIR) / "tenants"):
            self.assertIn("est le dossier du code ou le contient", self.refused())
        with override_settings(TENANTS_ROOT=self.data / "absent" / "tenants"):
            self.assertIn("le dossier des données est introuvable", self.refused())
        self.assertNothingWritten()

    def test_not_enough_room_is_refused_before_anything_is_written(self):
        full = SimpleNamespace(total=10**12, used=10**12 - 1000, free=1000)
        with mock.patch.object(data_backup.shutil, "disk_usage", return_value=full):
            self.assertIn("pas assez de place", self.refused())
        self.assertNothingWritten()

    # -- Failed: the folder set aside ------------------------------------------------------------------------------------

    def assertSetAside(self, said: str):
        self.assertEqual([path.name for path in self.dest.iterdir()], [f"{STAMP}-INCOMPLET"])
        self.assertIn(f"{STAMP}-INCOMPLET", self.err.getvalue())
        self.assertIn("ce n'est PAS une sauvegarde utilisable", self.err.getvalue())
        self.assertIn("Sauvegarde non faite", said)

    def test_a_database_that_does_not_open_fails_the_backup(self):
        orphan = self.tenants_root / "ancien-espace"
        orphan.mkdir()
        (orphan / "db.sqlite3").write_bytes(b"ceci n'est pas une base SQLite, mais un texte assez long " * 4)
        said = self.refused()
        self.assertSetAside(said)
        self.assertIn("ancien-espace", said)

    def test_a_copy_failing_its_integrity_check_fails_the_backup(self):
        with mock.patch.object(
            data_backup, "integrity", return_value=["*** in database main ***", "Page 7: never used"]
        ):
            said = self.refused()
        self.assertSetAside(said)
        self.assertIn("ne passe pas la vérification d'intégrité (*** in database main *** ; Page 7: never used)", said)
        self.assertIn("la base des comptes", said)

    def test_a_copy_with_other_row_counts_fails_the_backup(self):
        with mock.patch.object(data_backup, "table_counts", side_effect=[{"comptes": 2}, {"comptes": 1}]):
            said = self.refused()
        self.assertSetAside(said)
        self.assertIn("comptes : 2 dans la base, 1 dans la copie", said)

    def test_a_file_that_cannot_be_read_fails_the_backup(self):
        def locked(source, target, *args, **kwargs):
            if Path(source).name == "cle.pem":
                raise PermissionError(13, "Accès refusé")
            return shutil.copyfile(source, target)

        with mock.patch.object(data_backup.shutil, "copy2", side_effect=locked):
            said = self.refused()
        self.assertSetAside(said)
        self.assertIn("private/cles/cle.pem", said)
        self.assertIn("Accès refusé", said)

    # -- The server running meanwhile: files that go ----------------------------------------------------------------

    def test_a_file_gone_during_the_copy_is_said_and_fails_nothing(self):
        """DEPLOY.md, section 8: the weekly backup runs with the server up.
        A gather's download renamed, a receipt batch's staged file deleted,
        the log rotated: listed, then gone at the copy. FileNotFoundError is
        an OSError, and the whole backup was set aside -INCOMPLET."""
        gone = paths.tenant_dir(self.bar_b) / "media" / "ticket.jpg"
        relative = f"tenants/{self.bar_b.dir_name}/media/ticket.jpg"
        real = shutil.copy2

        def vanishing(source, target, *args, **kwargs):
            if Path(source) == gone:
                gone.unlink()
            return real(source, target, *args, **kwargs)

        with mock.patch.object(data_backup.shutil, "copy2", side_effect=vanishing):
            out, err = self.backup()
        self.assertEqual(err, "")
        folder = self.dest / STAMP
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [STAMP])
        self.assertFalse((folder / "data" / relative).exists())
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["vanished"], [relative])
        described = {entry["path"] for entry in manifest["databases"]}
        others = {path: content for path, content in self.copied_files(folder).items() if path not in described}
        self.assertEqual(manifest["files"], {"count": len(others), "bytes": sum(len(c) for c in others.values())})
        self.assertIn(f"1 fichier(s) disparu(s) pendant la copie (le serveur tournait) : {relative}", out)
        self.assertIn(f"Sauvegarde terminée : {folder}", out)

    def test_a_file_gone_before_its_size_was_read_is_no_traceback(self):
        """Gone between the listing and the size pre-scan: a raw
        FileNotFoundError out of `path.stat()`, no BackupError, an English
        traceback on the owner's screen."""
        rotated = self.data / "logs" / "marginmate.log.1"
        real = data_backup.other_files
        with mock.patch.object(data_backup, "other_files", side_effect=lambda *args: [*real(*args), rotated]):
            out, _ = self.backup()
        manifest = json.loads((self.dest / STAMP / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["vanished"], ["logs/marginmate.log.1"])
        self.assertIn("1 fichier(s) disparu(s) pendant la copie", out)

    def test_nothing_gone_says_nothing(self):
        out, _ = self.backup()
        manifest = json.loads((self.dest / STAMP / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["vanished"], [])
        self.assertNotIn("disparu", out)

    def test_a_data_folder_that_cannot_be_listed_is_refused_in_french(self):
        with mock.patch.object(data_backup, "other_files", side_effect=PermissionError(13, "Accès refusé")):
            said = self.refused()
        self.assertIn("lecture du dossier des données impossible : Accès refusé", said)
        self.assertNothingWritten()

    def test_a_file_there_that_will_not_copy_is_still_a_failure(self):
        """Only a file that is GONE is let go: a FileNotFoundError for a
        file still there (a target path too long for Windows) is a copy
        that failed."""

        def too_long(source, target, *args, **kwargs):
            if Path(source).name == "cle.pem":
                raise FileNotFoundError(2, "Le chemin d'accès spécifié est introuvable")
            return shutil.copyfile(source, target)

        with mock.patch.object(data_backup.shutil, "copy2", side_effect=too_long):
            said = self.refused()
        self.assertSetAside(said)
        self.assertIn("private/cles/cle.pem", said)


class GitCommitTests(SimpleTestCase):
    def answer(self, **kwargs):
        return subprocess.CompletedProcess(["git"], kwargs.pop("returncode", 0), **kwargs)

    def test_the_commit_when_git_answers_one(self):
        with mock.patch.object(data_backup.subprocess, "run", return_value=self.answer(stdout=COMMIT + "\n")) as run:
            self.assertEqual(data_backup.git_commit(), COMMIT)
        self.assertEqual(run.call_args.args[0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(run.call_args.kwargs["cwd"], Path(settings.BASE_DIR))

    def test_nothing_otherwise_never_an_error(self):
        for outcome in (
            FileNotFoundError("git"),
            subprocess.TimeoutExpired(["git"], 10),
            self.answer(returncode=128, stdout="", stderr="fatal: not a git repository"),
            self.answer(stdout="pas un commit\n"),
        ):
            with self.subTest(outcome=repr(outcome)[:40]):
                patch = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
                with mock.patch.object(data_backup.subprocess, "run", **patch):
                    self.assertIsNone(data_backup.git_commit())


class InsideTests(SimpleTestCase):
    def test_inside(self):
        base = Path(tempfile.gettempdir()) / "donnees"
        self.assertTrue(data_backup.inside(base, base))
        self.assertTrue(data_backup.inside(base / "tenants", base))
        self.assertFalse(data_backup.inside(base.with_name("donnees-dev"), base))
        self.assertFalse(data_backup.inside(base, base / "tenants"))
        if os.name == "nt":
            self.assertTrue(data_backup.inside(Path(str(base).upper()) / "x", base))
