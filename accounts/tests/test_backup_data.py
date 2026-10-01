"""`manage.py backup_data` (accounts/data_backup.py): a dated, checked copy
of the data folder.

Every folder is a temporary one: the data folder (TenancyTestCase's
TENANTS_ROOT and its parent), the accounts database's file in it, the
destination. The .env copied is an invented one (`data_backup.env_file`
patched: the developer's own is never read), git is never asked
(`data_backup.git_commit` patched), and the clock says 01/10/2026 10:15:00
in Paris. Two tenants, the template, the accounts database, media and
private files - every name and figure invented, every file a few bytes.
What a backup must never hold (« What a backup never holds » below) is
planted for the tests that need it: an « Identifiants » store written by
accounts/vault.py itself, a scraper's failure dump, live sessions - in the
accounts database and in the other SQLite files a data folder may hold (a
copy made by hand, the database kept from before an adoption), which go
through SQLite too (« Every other SQLite file » below).
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
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from accounts import data_backup, paths, vault
from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from invoices.models import Supplier
from invoices.scrapers import website
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


def plant_sessions(database: Path, marker: str, *, live: int = 3, deleted: int = 57) -> list[tuple]:
    """Django's session table in `database`, under its real name: `live`
    sessions, and `deleted` more deleted since - whose bytes SQLite leaves
    in the file, and the backup API copies. Every key starts with `marker`.
    The live sessions' keys, sorted."""
    table = Session._meta.db_table
    connection = sqlite3.connect(database)
    connection.execute(
        f'CREATE TABLE "{table}" (session_key varchar(40) NOT NULL PRIMARY KEY, '
        "session_data text NOT NULL, expire_date datetime NOT NULL)"
    )
    connection.executemany(
        f'INSERT INTO "{table}" VALUES (?, ?, ?)',
        [(f"{marker}-{n:03d}", "donnees-inventees-" * 30, "2026-10-15 08:00:00") for n in range(live + deleted)],
    )
    connection.commit()
    connection.execute(f'DELETE FROM "{table}" WHERE session_key >= ?', (f"{marker}-{live:03d}",))
    connection.commit()
    connection.close()
    return rows(database, f'SELECT session_key FROM "{table}" ORDER BY 1')


def a_database(path: Path, table: str, values: list[str]) -> Path:
    """An SQLite file holding one table of `values` - invented."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(f'CREATE TABLE "{table}" (valeur TEXT)')
    connection.executemany(f'INSERT INTO "{table}" VALUES (?)', [(value,) for value in values])
    connection.commit()
    connection.close()
    return path


class NoRoomToVacuum(sqlite3.Connection):
    """A connection whose VACUUM fails as on a full disk."""

    def execute(self, sql, *args):
        if sql.strip().upper() == "VACUUM":
            raise sqlite3.OperationalError("database or disk is full")
        return super().execute(sql, *args)


#: A copy of the accounts database made by hand beside it, as the owner's
#: data folder may hold one: no tenant names it.
BY_HAND = "accounts.sqlite3.bak_20990101_pre_x"


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

    # -- What a backup never holds ------------------------------------------------------------------------------------

    def plant_what_is_left_out(self) -> tuple[list[str], list[str]]:
        """Bar Alpha's « Identifiants » store, as accounts/vault.py writes it,
        a temporary file a write cut short left beside it, a portal's failure
        dump and a « Tester » run's folder - beside a download and a folder
        named like a test that ARE backed up. Returns the store's files and
        the folders left out, relative to the data folder, as the manifest
        lists them."""
        with bound_tenant(self.bar_a):
            vault.save({"METRO_PASSWORD": "mot-de-passe-invente"})
        folder = paths.tenant_dir(self.bar_a)
        (folder / "private" / f"{vault.TEMPORARY_PREFIX}x.tmp").write_bytes(b"ecriture coupee, inventee")
        downloads = folder / "downloads"
        (downloads / "type-1" / website.DEBUG_DIR).mkdir(parents=True)
        (downloads / "type-1" / website.DEBUG_DIR / "x.html").write_text(
            "<p>identifiant-invente@example.invalid</p>", encoding="utf-8"
        )
        (downloads / "type-1" / "facture-inventee.pdf").write_bytes(b"%PDF-1.4 facture inventee\n")
        (downloads / "test-12" / website.DEBUG_DIR).mkdir(parents=True)
        (downloads / "test-12" / website.DEBUG_DIR / "y.png").write_bytes(b"capture inventee")
        (downloads / "test-fournisseur").mkdir()
        (downloads / "test-fournisseur" / "f.pdf").write_bytes(b"%PDF-1.4 inventee\n")
        tenant = f"tenants/{self.bar_a.dir_name}"
        store = sorted(f"{tenant}/private/{name}" for name in (*vault.FILE_NAMES, f"{vault.TEMPORARY_PREFIX}x.tmp"))
        return store, [f"{tenant}/downloads/test-12", f"{tenant}/downloads/type-1/{website.DEBUG_DIR}"]

    def test_the_credentials_and_the_scrapers_pages_are_left_out_listed_and_said(self):
        """The « Identifiants » store is in no backup (accounts/vault.py): the
        backup holds the .env, half of its key, and a password copied is one
        more place it lies. The scrapers' failure dumps (a portal's page as it
        was shown, the account's identifiers on it) and the « Tester » runs'
        folders neither. Left out, they are neither missing nor vanished: the
        backup still verifies and is whole."""
        store, folders = self.plant_what_is_left_out()
        before = self.source_files()
        self.assertTrue(set(store) <= set(before))
        out, err = self.backup()
        self.assertEqual(err, "")
        folder = self.dest / STAMP
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [STAMP])
        self.assertTrue((folder / "manifest.json").is_file())

        left_out = [path for path in before if path in store or any(path.startswith(f"{f}/") for f in folders)]
        self.assertEqual(len(left_out), 5)
        copied = self.copied_files(folder)
        self.assertEqual(sorted(copied), sorted(set(before) - set(left_out)))
        for relative in folders:
            with self.subTest(folder=relative):
                # Not even as an empty folder.
                self.assertFalse((folder / "data" / relative).exists())
        tenant = f"tenants/{self.bar_a.dir_name}"
        self.assertIn(f"{tenant}/downloads/type-1/facture-inventee.pdf", copied)
        self.assertIn(f"{tenant}/downloads/test-fournisseur/f.pdf", copied)
        self.assertIn(f"{tenant}/private/cles/cle.pem", copied)

        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["left_out"], {"credentials": store, "debug_folders": folders, "env_copies": [], "files": 5}
        )
        self.assertEqual((manifest["vanished"], manifest["missing"]), ([], []))
        described = {entry["path"] for entry in manifest["databases"]}
        others = {path: content for path, content in copied.items() if path not in described}
        self.assertEqual(manifest["files"], {"count": len(others), "bytes": sum(len(c) for c in others.values())})
        self.assertIn("Identifiants (page Identifiants) : non sauvegardés, à ressaisir après une restauration.", out)
        self.assertIn(
            "Pages gardées par les récupérations en échec et les « Tester » : non sauvegardées (2 dossier(s)).", out
        )
        self.assertNotIn("disparu", out)
        self.assertIn(f"Sauvegarde terminée : {folder}", out)

    def test_nothing_left_out_says_nothing(self):
        out, _ = self.backup()
        manifest = json.loads((self.dest / STAMP / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["left_out"], {"credentials": [], "debug_folders": [], "env_copies": [], "files": 0})
        self.assertNotIn("Identifiants", out)
        self.assertNotIn("récupérations en échec", out)
        self.assertNotIn("ATTENTION", out)

    def test_a_copy_of_a_env_in_the_data_folder_is_left_out_named_and_never_read(self):
        """A copy of a .env (« .env.bak_<date> ») left in the data folder by
        hand would be in every backup and come with it into data-dev,
        which coding sessions read: the secret key, the passphrase,
        passwords. Every file named « .env » or « .env.<anything> » anywhere
        in the data folder is left out, listed in the manifest (its path,
        nothing else) and named in a warning telling the owner to delete it
        - told by its name, never opened, not even for its first bytes. The
        .env the settings were read from is copied beside data\\ as ever."""
        secret = "cle-inventee-copiee-par-le-test"
        tenant = f"tenants/{self.bar_a.dir_name}"
        copies = [".env.bak_20990101", f"{tenant}/.env", f"{tenant}/private/.ENV.ancien"]
        lookalikes = [f"{tenant}/media/.envoi.txt", f"{tenant}/media/notes.env"]
        for relative in copies + lookalikes:
            (self.data / relative).write_text(f"DJANGO_SECRET_KEY={secret}\n", encoding="utf-8")
        opened = set()
        real_header, real_copy = data_backup.is_sqlite_database, shutil.copy2

        def header(path):
            opened.add(data_backup._key(path))
            return real_header(path)

        def copying(source, target, *args, **kwargs):
            opened.add(data_backup._key(source))
            return real_copy(source, target, *args, **kwargs)

        with (
            mock.patch.object(data_backup, "is_sqlite_database", side_effect=header),
            mock.patch.object(data_backup.shutil, "copy2", side_effect=copying),
        ):
            out, err = self.backup()
        self.assertEqual(err, "")
        folder = self.dest / STAMP
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [STAMP])
        copied = self.copied_files(folder)
        for relative in copies:
            with self.subTest(copy=relative):
                self.assertNotIn(relative, copied)
                self.assertNotIn(data_backup._key(self.data / relative), opened)
                self.assertIn(data_backup.ENV_COPY_WARNING.format(path=self.data / relative), out)
        for relative in lookalikes:
            with self.subTest(lookalike=relative):
                self.assertIn(relative, copied)
                self.assertIn(data_backup._key(self.data / relative), opened)
        # The code's .env, beside data\: copied, as ever.
        self.assertEqual((folder / ".env").read_text(encoding="utf-8"), ENV_TEXT)
        manifest_text = (folder / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["left_out"]["env_copies"], sorted(copies))
        self.assertEqual(manifest["left_out"]["files"], 3)
        self.assertIs(manifest["env"], True)
        self.assertNotIn(secret, manifest_text)
        self.assertNotIn(secret, out)
        said = [line for line in out.splitlines() if line.startswith("ATTENTION")]
        self.assertEqual(len(said), 3)
        for line in said:
            self.assertIn("est une copie d'un fichier .env", line)
            self.assertIn("Supprimez-la", line)
            self.assertIn("DEPLOY.md, section 12", line)
        # Said where the window's reader looks: just before the end.
        lines = out.splitlines()
        self.assertEqual(lines.index(said[-1]) + 1, lines.index(f"Sauvegarde terminée : {folder}"))

    @skipUnless(os.name == "nt", "mklink /J")
    def test_a_destination_reaching_the_data_folder_by_another_name_is_refused(self):
        """Through a junction to the data folder, the destination is inside
        it: the copy would copy itself (os.walk going down the folders it
        makes). Refused before anything is written."""
        from accounts.tests.test_deployment_scripts import make_junction, short_name

        destinations = {"jonction": make_junction(self, self.dest / "lien-vers-les-donnees", self.data) / "sauvegardes"}
        short = short_name(self.data)
        if short:
            destinations["nom court"] = Path(short) / "sauvegardes"
        for name, destination in destinations.items():
            with self.subTest(name=name):
                self.assertIn("se copierait elle-même", self.refused(dest=destination))
                self.assertFalse((self.data / "sauvegardes").exists())
                self.assertEqual([path.name for path in self.dest.iterdir()], ["lien-vers-les-donnees"])

    def add_sessions(self) -> list[tuple]:
        """Django's session table in the accounts database, under its real
        name: three live sessions, and fifty-seven more deleted since -
        whose bytes SQLite leaves in the file, and the backup API copies.
        The live sessions' keys, sorted."""
        return plant_sessions(self.accounts, "cle-de-session-inventee")

    def test_the_accounts_copy_holds_no_session_and_the_live_database_keeps_them(self):
        """A session key is a login: whoever holds a backup could replay one
        against the public site. The copy is checked whole first, then its
        sessions are deleted and the file rewritten (VACUUM: a deleted row's
        bytes stay in the file otherwise), then checked again - never a row
        of the live database."""
        self.assertEqual(data_backup.SESSION_TABLE, Session._meta.db_table)
        live = self.add_sessions()
        self.assertEqual(len(live), 3)
        # What the test guards against: deleted sessions are still in the file.
        self.assertIn(b"cle-de-session-inventee-059", self.accounts.read_bytes())
        out, err = self.backup()
        self.assertEqual(err, "")
        folder = self.dest / STAMP
        copy = folder / "data" / "accounts.sqlite3"
        self.assertEqual(rows(copy, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(rows(copy, "SELECT COUNT(*) FROM django_session"), [(0,)])
        self.assertEqual(
            rows(copy, "SELECT email FROM comptes ORDER BY 1"),
            rows(self.accounts, "SELECT email FROM comptes ORDER BY 1"),
        )
        self.assertNotIn(b"cle-de-session-inventee", copy.read_bytes())
        self.assertEqual(rows(self.accounts, "SELECT session_key FROM django_session ORDER BY 1"), live)
        leftovers = [
            path.name for path in (folder / "data").rglob("*") if path.name.endswith(("-wal", "-shm", "-journal"))
        ]
        self.assertEqual(leftovers, [])

        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["sessions"], "non sauvegardées")
        accounts = next(entry for entry in manifest["databases"] if entry["path"] == "accounts.sqlite3")
        self.assertEqual(accounts["tables"], {"comptes": 2, "django_session": 0})
        self.assertEqual(accounts["rows"], 2)
        self.assertEqual(accounts["bytes"], copy.stat().st_size)
        self.assertIn("sessions de connexion : non sauvegardées", out)
        self.assertIn(f"Sauvegarde terminée : {folder}", out)

    def test_a_copy_whose_sessions_did_not_all_go_fails_the_backup(self):
        """The second check holds the copy to the first one's counts, the
        sessions at 0: a session left, or another row gone with them, is a
        failure."""
        self.add_sessions()
        real = data_backup.table_counts
        calls = []

        def one_session_left(connection):
            counts = real(connection)
            calls.append(counts)
            # The accounts database: the source, the copy, the copy emptied.
            return {**counts, "django_session": 1} if len(calls) == 3 else counts

        with mock.patch.object(data_backup, "table_counts", side_effect=one_session_left):
            said = self.refused()
        self.assertSetAside(said)
        self.assertIn("la base des comptes", said)
        self.assertIn("django_session : 0 attendues, 1 dans la copie", said)
        # The copy still holding them is not in the folder set aside.
        incomplete = self.dest / f"{STAMP}-INCOMPLET"
        self.assertFalse((incomplete / "data" / "accounts.sqlite3").exists())
        self.assertNoSessionIn(incomplete, "cle-de-session-inventee")

    # -- Every other SQLite file of the data folder: through SQLite, without its sessions ----------------------------

    def plant_other_databases(self) -> dict[str, str]:
        """SQLite files no tenant names, as the owner's data folder may hold
        them: a copy of the accounts database made by hand beside it and the
        database kept from before an adoption - both with sessions -, and
        « Données »'s safety copy of a tenant (no session table), its last
        commit still in its -wal. Beside them, a file named like a database
        that is none, and one too short to be one. Returns, by path relative
        to the data folder, the marker of each database's session keys ("":
        none)."""
        tenant = paths.tenant_dir(self.bar_a)
        adopted = f"tenants/{self.bar_a.dir_name}/db.sqlite3.bak_20260928_pre_adoption"
        safety = f"tenants/{self.bar_a.dir_name}/backups/2026-09-19_143012_avant-import.sqlite3"
        plant_sessions(a_database(self.data / BY_HAND, "comptes", ["alpha@example.invalid"]), "session-copie-main")
        plant_sessions(a_database(self.data / adopted, "factures", ["F-001", "F-002"]), "session-avant-adoption")
        a_database(self.data / safety, "fournisseurs", ["Grossiste Alpha"])
        live = sqlite3.connect(self.data / safety, isolation_level=None)
        self.addCleanup(live.close)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO fournisseurs VALUES ('dans-le-wal')")
        self.assertGreater((self.data / f"{safety}-wal").stat().st_size, 0)
        (tenant / "media" / "pas-une-base.sqlite3").write_bytes(b"un texte invente, pas une base SQLite\n" * 4)
        (self.data / "logs" / "court.bin").write_bytes(data_backup.SQLITE_HEADER[:10])
        return {BY_HAND: "session-copie-main", adopted: "session-avant-adoption", safety: ""}

    def assertNoSessionIn(self, folder: Path, *markers: str):
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                content = path.read_bytes()
                for marker in markers:
                    with self.subTest(file=str(path.relative_to(folder)), marker=marker):
                        self.assertNotIn(marker.encode(), content)

    def test_every_other_sqlite_file_is_copied_through_sqlite_without_its_sessions(self):
        """A database copied as a file carries every session it holds - and
        the deleted ones, in its free pages. Found by its first 16 bytes,
        whatever its name, every other SQLite file goes the accounts
        database's way: the backup API, checked, its sessions deleted, the
        file rewritten, checked again. The live files keep theirs."""
        markers = self.plant_other_databases()
        sources = {relative: counts_of(self.data / relative) for relative in markers}
        live = {
            relative: rows(self.data / relative, "SELECT session_key FROM django_session ORDER BY 1")
            for relative, marker in markers.items()
            if marker
        }
        self.assertTrue(all(len(keys) == 3 for keys in live.values()))
        out, err = self.backup()
        self.assertEqual(err, "")
        folder = self.dest / STAMP
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [STAMP])
        self.assertNoSessionIn(folder, *filter(None, markers.values()))

        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        described = {entry["path"]: entry for entry in manifest["databases"]}
        for relative, marker in markers.items():
            with self.subTest(database=relative):
                copy = folder / "data" / relative
                self.assertEqual(rows(copy, "PRAGMA integrity_check"), [("ok",)])
                expected = {**sources[relative], **({"django_session": 0} if marker else {})}
                self.assertEqual(counts_of(copy), expected)
                entry = described[relative]
                self.assertEqual((entry["role"], entry["espace"]), (data_backup.OTHER_ROLE, None))
                self.assertEqual(entry["tables"], expected)
                self.assertEqual(entry["bytes"], copy.stat().st_size)
                self.assertIn(f"  une autre base SQLite ({relative}) : ", out)
        for relative, keys in live.items():
            with self.subTest(source=relative):
                self.assertEqual(rows(self.data / relative, "SELECT session_key FROM django_session ORDER BY 1"), keys)
        self.assertEqual(out.count("sessions de connexion : retirées de la copie"), 2)

        # The safety copy holds what its -wal held; no -wal, -shm or -journal is copied.
        safety = next(relative for relative, marker in markers.items() if not marker)
        self.assertIn(("dans-le-wal",), rows(folder / "data" / safety, "SELECT valeur FROM fournisseurs"))
        sides = [path.name for path in (folder / "data").rglob("*") if path.name.endswith(data_backup.SIDE_SUFFIXES)]
        self.assertEqual(sides, [])
        # What only looks like a database by its name, or is too short to be
        # one, is a file like any other.
        copied = self.copied_files(folder)
        named = f"tenants/{self.bar_a.dir_name}/media/pas-une-base.sqlite3"
        self.assertEqual(copied[named], (self.data / named).read_bytes())
        self.assertEqual(copied["logs/court.bin"], data_backup.SQLITE_HEADER[:10])
        self.assertNotIn(named, described)
        others = {path: content for path, content in copied.items() if path not in described}
        self.assertEqual(manifest["files"], {"count": len(others), "bytes": sum(len(c) for c in others.values())})

    def test_a_tenant_s_database_holding_sessions_is_copied_without_them(self):
        """A tenant's database has no session table (accounts.router); a
        folder no tenant names may hold an old database that does."""
        orphan = a_database(self.tenants_root / "ancien-espace" / "db.sqlite3", "t", ["x"])
        plant_sessions(orphan, "session-ancien-espace")
        out, _ = self.backup()
        copy = self.dest / STAMP / "data" / "tenants" / "ancien-espace" / "db.sqlite3"
        self.assertEqual(counts_of(copy), {"t": 1, "django_session": 0})
        self.assertNoSessionIn(self.dest / STAMP, "session-ancien-espace")
        self.assertEqual(rows(orphan, "SELECT COUNT(*) FROM django_session"), [(3,)])
        self.assertIn("sessions de connexion : retirées de la copie", out)

    @contextlib.contextmanager
    def failing_purge(self, target: Path, how: str):
        """The purge of the copy `target` fails once its sessions are
        deleted: a session still counted (« compte »), the copy unsound at
        the check (« intégrité »), or SQLite failing the VACUUM."""
        key = data_backup._key(target)

        def is_target(connection) -> bool:
            return any(row[2] and data_backup._key(row[2]) == key for row in connection.execute("PRAGMA database_list"))

        seen = []
        if how == "compte":
            real_counts = data_backup.table_counts

            def counting(connection):
                counts = real_counts(connection)
                if is_target(connection):
                    seen.append(counts)
                    if len(seen) == 2:
                        return {**counts, "django_session": 1}
                return counts

            with mock.patch.object(data_backup, "table_counts", side_effect=counting):
                yield
        elif how == "intégrité":
            real_integrity = data_backup.integrity

            def checking(connection):
                if is_target(connection):
                    seen.append(connection)
                    if len(seen) == 2:
                        return ["*** in database main ***", "Page 3: inventee"]
                return real_integrity(connection)

            with mock.patch.object(data_backup, "integrity", side_effect=checking):
                yield
        else:
            real_connect = sqlite3.connect

            def connecting(database, *args, **kwargs):
                if data_backup._key(database) == key:
                    seen.append(database)
                    if len(seen) == 2:
                        return real_connect(database, *args, factory=NoRoomToVacuum, **kwargs)
                return real_connect(database, *args, **kwargs)

            with mock.patch.object(data_backup.sqlite3, "connect", side_effect=connecting):
                yield
        self.assertGreaterEqual(len(seen), 2, f"the purge of {target} was never reached")

    def test_a_failed_session_purge_takes_its_half_made_copy_away(self):
        """A copy whose purge failed still holds sessions - all of them, or
        their bytes in its free pages when the VACUUM is what failed. It is
        removed before the folder is set aside -INCOMPLET: whoever opens
        that folder finds no session in it."""
        self.add_sessions()
        markers = self.plant_other_databases()
        everything = ["cle-de-session-inventee", *filter(None, markers.values())]
        incomplete = self.dest / f"{STAMP}-INCOMPLET"
        for relative, label in (("accounts.sqlite3", "la base des comptes"), (BY_HAND, "une autre base SQLite")):
            for how in ("compte", "intégrité", "VACUUM"):
                with self.subTest(database=relative, failure=how):
                    # A failed subtest leaves its folder: the next one would be refused.
                    shutil.rmtree(incomplete, ignore_errors=True)
                    target = self.dest / STAMP / "data" / relative
                    with self.failing_purge(target, how):
                        said = self.refused()
                    self.assertSetAside(said)
                    self.assertIn(f"{label} ({relative})", said)
                    self.assertIn("sessions de connexion", said)
                    left = [path.name for path in (incomplete / "data").glob(f"{relative}*")]
                    self.assertEqual(left, [])
                    self.assertNoSessionIn(incomplete, *everything)

    def test_a_half_made_copy_that_will_not_go_is_said(self):
        self.add_sessions()
        target = self.dest / STAMP / "data" / "accounts.sqlite3"
        real_unlink = Path.unlink

        def held(path, *args, **kwargs):
            if data_backup._key(path) == data_backup._key(target):
                raise PermissionError(13, "Accès refusé")
            return real_unlink(path, *args, **kwargs)

        with self.failing_purge(target, "compte"), mock.patch.object(Path, "unlink", autospec=True, side_effect=held):
            said = self.refused()
        self.assertSetAside(said)
        self.assertIn("la base des comptes (accounts.sqlite3)", said)
        self.assertIn("la copie commencée n'a pas pu être effacée", said)
        self.assertIn("elle peut contenir des sessions de connexion : supprimez-la vous-même", said)

    def test_another_database_gone_during_the_copy_is_said_and_fails_nothing(self):
        """As any other file of the data folder: a copy made by hand, deleted
        while the backup runs, is no failure - unlike a tenant's database."""
        self.plant_other_databases()
        gone = self.data / BY_HAND
        real = data_backup.copy_database

        def vanishing(source, target, **kwargs):
            if data_backup._key(source) == data_backup._key(gone):
                gone.unlink()
            return real(source, target, **kwargs)

        with mock.patch.object(data_backup, "copy_database", side_effect=vanishing):
            out, err = self.backup()
        self.assertEqual(err, "")
        folder = self.dest / STAMP
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [STAMP])
        self.assertFalse((folder / "data" / BY_HAND).exists())
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["vanished"], [BY_HAND])
        self.assertNotIn(BY_HAND, [entry["path"] for entry in manifest["databases"]])
        self.assertIn(f"1 fichier(s) disparu(s) pendant la copie (le serveur tournait) : {BY_HAND}", out)

    def test_a_file_that_starts_like_a_database_and_will_not_open_fails_the_backup(self):
        """Nothing then says what sessions it holds: the backup is set aside,
        no copy of it in the folder, and the message says what to do."""
        broken = paths.tenant_dir(self.bar_a) / "backups" / "copie-abimee.sqlite3"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(data_backup.SQLITE_HEADER + b"pas la suite d'une base, un texte invente " * 100)
        said = self.refused()
        self.assertSetAside(said)
        self.assertIn(f"une autre base SQLite (tenants/{self.bar_a.dir_name}/backups/copie-abimee.sqlite3)", said)
        self.assertIn("Si ce fichier ne sert plus, sortez-le du dossier des données", said)
        incomplete = self.dest / f"{STAMP}-INCOMPLET"
        self.assertEqual(list(incomplete.rglob("copie-abimee.sqlite3*")), [])

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


class SqliteHeaderTests(SimpleTestCase):
    def test_a_database_is_told_by_its_first_16_bytes(self):
        folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-entete-"))
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(data_backup.SQLITE_HEADER, b"SQLite format 3\x00")
        database = a_database(folder / "sans-extension", "t", ["x"])
        empty = folder / "vide.sqlite3"
        empty.write_bytes(b"")
        text = folder / "texte.sqlite3"
        text.write_bytes(b"SQLite format 3 - un texte invente")
        cases = {database: True, empty: False, text: False, folder / "absent.sqlite3": False}
        for path, expected in cases.items():
            with self.subTest(path=path.name):
                self.assertIs(data_backup.is_sqlite_database(path), expected)


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
            self.assertTrue(data_backup.inside(Path("\\\\?\\" + str(base)) / "x", base))

    @skipUnless(os.name == "nt", "mklink /J")
    def test_inside_by_another_name(self):
        """A folder's other names - a junction, its 8.3 short name - are
        that folder (accounts.deployment.inside, which this one is)."""
        from accounts.tests.test_deployment_scripts import make_junction, short_name

        folder = Path(os.path.realpath(tempfile.mkdtemp(prefix="marginmate-tests-autre-nom-")))
        self.addCleanup(shutil.rmtree, folder, True)
        data = folder / "donnees-du-site"
        data.mkdir()
        junction = make_junction(self, folder / "lien", data)
        self.assertTrue(data_backup.inside(junction / "sauvegardes", data))
        self.assertTrue(data_backup.inside(data / "x", junction))
        short = short_name(data)
        if short:
            self.assertTrue(data_backup.inside(Path(short) / "sauvegardes", data))
        self.assertFalse(data_backup.inside(folder / "sauvegardes", data))
