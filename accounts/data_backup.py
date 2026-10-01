"""A dated copy of the whole data folder: `manage.py backup_data`.

    .venv\\Scripts\\python.exe manage.py backup_data                  <backups>\\<YYYY-MM-DD_HHMMSS>\\
    .venv\\Scripts\\python.exe manage.py backup_data --dest E:\\Sauvegardes
    .venv\\Scripts\\python.exe manage.py backup_data --sans-env

The DATA FOLDER is the one holding TENANTS_ROOT - C:\\MarginMate\\data for the
owner: tenants\\ (every tenant, the _template), accounts.sqlite3, logs\\. The
backups go beside it by default (`default_destination`: C:\\MarginMate\\backups),
worked out from the settings, never written down anywhere. deploy.cmd makes
one before every deployment (DEPLOY.md, section 10), and refresh_dev_data.cmd
copies the newest one into the development folder's data.

    <YYYY-MM-DD_HHMMSS>\\
        data\\          the data folder, mirrored
        .env           unless --sans-env: the secret key, the passphrase, the passwords
        manifest.json  what was copied (every database's tables and rows), from which code

**Every database goes through SQLite's backup API, never as a file.** A file
copy of a database somebody writes to is a torn copy, and a database in WAL
mode keeps its latest commits in its -wal until a checkpoint: copied as two
files, the pair is only right if nothing moved between the two copies - and
put back beside another -wal, it is read back wrong (CLAUDE.md, « Données »).
The API copies what the database holds, -wal included, so the -wal, -shm and
-journal of those databases are NOT copied. Each copy is then checked:
`PRAGMA integrity_check` on the copy, and the same number of rows in every
table as the source had in the snapshot the copy was taken from - a read
transaction is held on the source across the copy, so a server still running
cannot make the two disagree (it may write meanwhile: the copy is the
moment the transaction started). The source is opened ``mode=rw``, never
``rwc``: a database that is not there is not made (SQLite would create an
empty file), and never ``ro`` (accounts.provisioning.copy_database says why).

The databases are the accounts database and every ``db.sqlite3`` one level
under TENANTS_ROOT: the _template, every tenant, and a tenant's folder the
accounts database no longer names (backed up all the same, and said) - and
EVERY OTHER SQLite file of the data folder, told by its first 16 bytes
(`SQLITE_HEADER`), whatever its name: a copy made by hand beside a database
(``accounts.sqlite3.bak_<date>_<why>``), the database kept from before an
adoption (``db.sqlite3.bak_*``), « Données »'s own safety copies in a
tenant's backups\\ (role `OTHER_ROLE`, « une autre base SQLite »). Copied as
files, they carried every session they held into the backup (security review
of 01/10/2026). They are copied, not left out: the safety copies are what
undoes a « Données » import. Such a file that goes away before its copy is
let go as any other file (``vanished``); one that will not open or fails its
checks fails the backup, the message saying to take it out of the data folder
if it is of no use - nothing else then says what sessions it holds. Every
other file - media, private (the signing keys), downloads, backups, staging,
imports, the log - is copied as a file.

**What a backup never holds** (security review of 01/10/2026), each left
out on purpose, listed in the manifest (``left_out``) and said:

- the « Identifiants » store (accounts/vault.py, `LEFT_OUT_FILES`): its two
  files and the temporary file a write cut short leaves, directly in a
  tenant's private\\. The backup holds the .env, the SECRET_KEY half of the
  store's key: a password copied here is one more place it lies, and
  restored elsewhere the store would not open anyway (DPAPI). The owner
  types them again after a restore (DEPLOY.md, section 12);
- the scrapers' failure dumps, every ``_debug`` folder (a portal's page as
  it was shown, which can print the account's identifiers), and a
  « Tester » run's own folder, ``test-<job>`` in a tenant's downloads\\
  (it downloads nothing but those pages);
- the live sessions: a session key IS a login, and whoever holds a backup
  could replay one against the public site. Every database's copy holding a
  ``django_session`` table - the accounts database's, and any other's - is
  checked whole, then its sessions are deleted and the file rewritten
  (VACUUM: a deleted row's bytes stay in the file, beside those of every
  session the source deleted before - the backup API copies its free pages
  too), then checked again: intact, and every table as the first check
  counted it, the sessions at 0. The live databases are never written.
  After a restore, everybody logs in again. A copy that failed any of this
  is deleted before the folder is set aside (it may hold every session):
  when it cannot be, the message says so;
- a COPY of a .env put in the data folder (a ``.env.bak_<date>`` left there
  by hand would travel with the data into data-dev, which coding sessions
  read): every file named ``.env`` or ``.env.<anything>``,
  anywhere in it (`deployment.is_env_copy`, the patterns
  refresh_dev_data.cmd's robocopy excludes). Told by its name, never opened
  - not even for its first bytes -, and named in a warning telling the owner
  to delete it (« ATTENTION : … »). The .env the settings were read from is
  the code's, beside manage.py, and is copied beside ``data\\`` as ever.

``left_out`` = {"credentials": [files], "debug_folders": [folders],
"env_copies": [files], "files": every file not copied for those reasons},
paths relative to the data folder, sorted; ``sessions`` = "non
sauvegardées". A file left out is never counted in ``files``, nor missing,
nor vanished.

**Refused before anything is written**, each in French: a data folder that
is missing, IS the code's folder or holds it (TENANTS_ROOT left at its
default, beside manage.py - the code's folder is no data folder); an accounts
database missing, or outside the data folder (the backup would not hold it);
a destination inside the data folder (the copy would copy itself) or inside
the code's folder (a folder under git, and the backup holds the .env); a
target folder that already exists; less free space than the copy takes.
« Inside » is asked by every name of a folder (`inside`, which is
`deployment.inside`): a destination reached through a junction or an 8.3
short name of the data folder is inside it.

**Any failure once the folder exists** - a database that will not open or
fails its checks, a file that cannot be read - renames the folder
``<stamp>-INCOMPLET`` (never deleted: whoever reads the message decides) and
the command exits in error. refresh_dev_data.cmd never takes such a folder.

**Except a file that went away** between the listing and its copy: the
server may run meanwhile (DEPLOY.md, section 8, the weekly backup), and a
gather's browser renames its downloads, a receipt batch deletes the files it
staged, the staging sweep runs, the log rotates. Such a file is not a
failure: it is left out, listed in the manifest (``vanished``) and said
(« N fichier(s) disparu(s) pendant la copie »). Only a file that is really
GONE: a FileNotFoundError for a file still there (a path Windows finds too
long) is a copy that failed. The accounts database, the template's and a
tenant's are never let go that way (another SQLite file is). The size
pre-scan counts a vanished file as 0, and any other error reading the data
folder there is a French refusal, never a traceback.

The git commit of the code (`git rev-parse HEAD` in the code's folder) goes
into the manifest when git answers; a missing git never fails a backup.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from . import deployment, paths, vault
from .router import ACCOUNTS_ALIAS

#: The backup's folder: local time, sortable by name (refresh_dev_data.cmd
#: takes the last one by name).
STAMP_FORMAT = "%Y-%m-%d_%H%M%S"
#: What a failed backup's folder is renamed to end with.
INCOMPLETE = "-INCOMPLET"
DATA = "data"
MANIFEST = "manifest.json"
ENV = ".env"
FORMAT = "marginmate-sauvegarde"
VERSION = 1
#: What SQLite keeps beside a database it has open. Never copied beside a
#: database the API copied: the copy already holds what they held.
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
#: Room left free on the destination's disk beyond what the copy takes.
SPARE_BYTES = 64 * 1024**2
#: How long `git rev-parse` may take before the manifest does without it.
GIT_TIMEOUT_SECONDS = 10

ACCOUNTS_ROLE = "comptes"
TEMPLATE_ROLE = "modele"
TENANT_ROLE = "espace"
ORPHAN_ROLE = "dossier sans espace"
#: Any other SQLite file of the data folder (the module's docstring).
OTHER_ROLE = "autre base"
#: What every SQLite database file starts with (SQLite's file format, 1.3.1).
SQLITE_HEADER = b"SQLite format 3\x00"
#: Said after the failure of another SQLite file's copy.
OTHER_HINT = " Si ce fichier ne sert plus, sortez-le du dossier des données, puis relancez la sauvegarde."

#: The « Identifiants » store's files, never copied (accounts/vault.py):
#: directly in a tenant's private folder, with the temporary files a write
#: cut short leaves there (their names start with `LEFT_OUT_PREFIX`).
LEFT_OUT_FILES = vault.FILE_NAMES
LEFT_OUT_PREFIX = vault.TEMPORARY_PREFIX
#: The scrapers' failure dumps (invoices.scrapers.website.DEBUG_DIR - not
#: imported: the backup does not load the scrapers), never copied wherever
#: they are.
DEBUG_FOLDER = "_debug"
#: A « Tester » run's own folder in a tenant's downloads (invoices.tasks:
#: ``test-<job id>``), never copied.
TEST_RUN_FOLDER = re.compile(r"test-[0-9]+")
#: Django's sessions, emptied in every database's copy that has the table.
SESSION_TABLE = "django_session"
SESSIONS_LEFT_OUT = "non sauvegardées"
#: A copy of a .env in the data folder, never copied (the module's
#: docstring): one definition for the backup, the refresh's robocopy and
#: its refusals.
is_env_copy = deployment.is_env_copy
ENV_COPY_PATTERNS = deployment.ENV_COPY_PATTERNS
ENV_COPY_WARNING = (
    "ATTENTION : {path} est une copie d'un fichier .env, rangée dans le dossier des données. Elle peut contenir "
    "la clé secrète et des mots de passe du site : elle n'est pas sauvegardée, et elle n'a rien à faire là. "
    "Supprimez-la, ainsi que ses copies dans le dossier de développement et dans les sauvegardes plus anciennes "
    "(DEPLOY.md, section 12)."
)


class BackupError(Exception):
    """A backup refused or failed, in French. `folder` is the -INCOMPLET
    folder a failure left behind, None when nothing was written."""

    def __init__(self, message: str, folder: Path | None = None):
        super().__init__(message)
        self.folder = folder


@dataclass
class Database:
    source: Path
    relative: str
    role: str
    label: str
    tenant_name: str | None = None
    tables: dict[str, int] = field(default_factory=dict)


@dataclass
class LeftOut:
    """What the copy leaves out on purpose (the module's docstring), by
    path relative to the data folder; `files` counts every file not copied,
    the folders' included."""

    credentials: list[str] = field(default_factory=list)
    debug_folders: list[str] = field(default_factory=list)
    env_copies: list[str] = field(default_factory=list)
    files: int = 0

    def as_manifest(self) -> dict:
        return {
            "credentials": sorted(self.credentials),
            "debug_folders": sorted(self.debug_folders),
            "env_copies": sorted(self.env_copies),
            "files": self.files,
        }


# -- Where things are (the settings, read at call time) --------------------------------------------------------------


def data_folder() -> Path:
    """The folder holding TENANTS_ROOT: every tenant, the accounts database,
    the log (C:\\MarginMate\\data for the owner)."""
    return paths.tenants_root().parent


def default_destination() -> Path:
    """« backups » beside the data folder (C:\\MarginMate\\backups)."""
    return data_folder().parent / "backups"


def accounts_database() -> Path:
    return Path(str(settings.DATABASES[ACCOUNTS_ALIAS]["NAME"]))


def code_folder() -> Path:
    return Path(settings.BASE_DIR)


def env_file() -> Path:
    """The .env the settings were read from, beside manage.py."""
    return code_folder() / ENV


def _now():
    return timezone.now()


def git_commit() -> str | None:
    """The code's commit, or None - git missing, not a clone, too slow:
    the backup is made all the same."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=code_folder(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    commit = (result.stdout or "").strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    return commit


# -- Paths ------------------------------------------------------------------------------------------------------------


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def inside(inner: Path, outer: Path) -> bool:
    """`inner` is `outer` or somewhere under it, by any of their names
    (Windows: whatever the case; a junction, a link, an 8.3 short name, a
    subst drive resolved): `deployment.inside`."""
    return deployment.inside(inner, outer)


def _existing_ancestor(path: Path) -> Path:
    path = Path(os.path.abspath(str(path)))
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _relative(path: Path, data: Path) -> str:
    return Path(os.path.relpath(os.path.abspath(str(path)), os.path.abspath(str(data)))).as_posix()


def _bytes_of(path: Path) -> int:
    """A file's size; 0 when it went away since it was listed (the server
    runs meanwhile: the copy then says so)."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


# -- The databases ----------------------------------------------------------------------------------------------------


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Rows per table (SQLite's own tables left out), by name."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND substr(name, 1, 7) != 'sqlite_' ORDER BY name"
        )
    ]
    return {name: connection.execute(f"SELECT COUNT(*) FROM {_quoted(name)}").fetchone()[0] for name in names}


def integrity(connection: sqlite3.Connection) -> list[str]:
    """`PRAGMA integrity_check`'s answer: ["ok"] for a sound database."""
    return [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]


def _unsound(answer: list[str], when: str = "") -> BackupError:
    shown = " ; ".join(answer[:3]) + (" ; …" if len(answer) > 3 else "")
    return BackupError(f"{when}la copie ne passe pas la vérification d'intégrité ({shown}).")


def _differing(expected: dict[str, int], copied: dict[str, int]) -> list[str]:
    return sorted(set(copied) ^ set(expected) | {name for name in copied if copied.get(name) != expected.get(name)})


def is_sqlite_database(path: Path) -> bool:
    """`path` starts as every SQLite database does. A file that cannot be
    read says no: its copy as a file then fails, or finds it gone."""
    try:
        with open(path, "rb") as file:
            return file.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def copy_database(source: Path, target: Path, *, forget_sessions: bool = False) -> dict[str, int]:
    """Copy `source` to `target`, a new file, through the backup API, check
    the copy, and return its rows per table. Raises BackupError (French)
    otherwise, the half-made copy deleted first: it may hold every session
    of the source. `forget_sessions`: once the copy is checked, its sessions
    (when it has Django's session table) are deleted - in the copy alone -
    and the copy checked again."""
    try:
        return _copy_and_check(source, target, forget_sessions)
    except BackupError as exc:
        why = _discard(target)
        if why:
            raise BackupError(
                f"{exc} De plus, la copie commencée n'a pas pu être effacée ({why}) et elle peut contenir des "
                "sessions de connexion : supprimez-la vous-même."
            ) from exc
        raise


def _discard(target: Path) -> str:
    """Delete a failed copy and what SQLite may have left beside it: why one
    of them could not be, else ""."""
    why = ""
    for path in (target, *(target.with_name(target.name + suffix) for suffix in SIDE_SUFFIXES)):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            why = why or exc.strerror or str(exc)
    return why


def _copy_and_check(source: Path, target: Path, forget_sessions: bool) -> dict[str, int]:
    uri = Path(os.path.abspath(str(source))).as_uri() + "?mode=rw"
    try:
        reader = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=60)
    except sqlite3.Error as exc:
        raise BackupError(f"la base ne s'ouvre pas ({exc}).") from exc
    try:
        # The snapshot both the counts and the copy are taken from.
        reader.execute("BEGIN")
        expected = table_counts(reader)
        writer = sqlite3.connect(str(target), isolation_level=None)
        try:
            reader.backup(writer)
            answer = integrity(writer)
            copied = table_counts(writer)
        finally:
            writer.close()
        reader.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise BackupError(f"la copie a échoué ({exc}).") from exc
    finally:
        reader.close()
    if answer != ["ok"]:
        raise _unsound(answer)
    if copied != expected:
        shown = ", ".join(
            f"{name} : {expected.get(name, 'absente')} dans la base, {copied.get(name, 'absente')} dans la copie"
            for name in _differing(expected, copied)[:3]
        )
        raise BackupError(f"la copie n'a pas le même nombre de lignes que la base ({shown}).")
    if forget_sessions and SESSION_TABLE in copied:
        return _forget_sessions(target, copied)
    return copied


def _forget_sessions(target: Path, checked: dict[str, int]) -> dict[str, int]:
    """Delete every session of the copy `target`, already checked with the
    counts `checked`, and check it again: intact, and every table as
    `checked` says but the sessions, at 0 - a row of another table gone with
    them, or a session left, is a failure."""
    try:
        writer = sqlite3.connect(str(target), isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(f"DELETE FROM {_quoted(SESSION_TABLE)}")
            writer.execute("COMMIT")
            # A DELETE only frees the rows' room: their bytes - the session
            # keys - stay in the file, beside those of every session the
            # source deleted before (the backup API copies its free pages
            # too). VACUUM rewrites the copy without them, as
            # accounts.adoption does for the tables it drops.
            writer.execute("VACUUM")
            answer = integrity(writer)
            emptied = table_counts(writer)
        finally:
            writer.close()
    except sqlite3.Error as exc:
        raise BackupError(f"les sessions de connexion n'ont pas pu être retirées de la copie ({exc}).") from exc
    if answer != ["ok"]:
        raise _unsound(answer, when="une fois les sessions de connexion retirées, ")
    expected = {**checked, SESSION_TABLE: 0}
    if emptied != expected:
        shown = ", ".join(
            f"{name} : {expected.get(name, 'absente')} attendues, {emptied.get(name, 'absente')} dans la copie"
            for name in _differing(expected, emptied)[:3]
        )
        raise BackupError(f"une fois les sessions de connexion retirées, la copie n'est plus celle vérifiée ({shown}).")
    return emptied


def _tenant_names() -> dict[str, str]:
    """Every tenant's folder name and name, from the accounts database."""
    from .models import Tenant

    try:
        return dict(Tenant.objects.values_list("dir_name", "name"))
    except DatabaseError:
        return {}


def find_databases(data: Path, accounts: Path) -> tuple[list[Database], list[str]]:
    """The databases to copy through the API, and what is missing (an
    tenant the accounts database names whose database is not there)."""
    root = paths.tenants_root()
    names = _tenant_names()
    databases = [Database(accounts, _relative(accounts, data), ACCOUNTS_ROLE, "la base des comptes")]
    found = set()
    if root.is_dir():
        for folder in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            database = folder / paths.DATABASE_FILE
            if not folder.is_dir() or not database.is_file():
                continue
            relative = _relative(database, data)
            if folder.name == paths.TEMPLATE_DIR:
                databases.append(Database(database, relative, TEMPLATE_ROLE, "le modèle des nouveaux espaces"))
            elif folder.name in names:
                name = names[folder.name]
                databases.append(Database(database, relative, TENANT_ROLE, f"l'espace « {name} »", tenant_name=name))
            else:
                databases.append(
                    Database(
                        database, relative, ORPHAN_ROLE, f"le dossier « {folder.name} » (aucun espace ne le nomme)"
                    )
                )
            found.add(folder.name)
    missing = [
        f"l'espace « {name} » ({dir_name}) : sa base est introuvable, elle n'est donc pas dans la sauvegarde."
        for dir_name, name in sorted(names.items())
        if dir_name not in found
    ]
    return databases, missing


def _directly_in(path: Path, kind: str, root: Path) -> bool:
    """`path` sits directly in a tenant's `kind` folder: TENANTS_ROOT/<folder>/<kind>/<path's name>
    (Windows: whatever the case)."""
    folder = path.parent
    return os.path.normcase(folder.name) == os.path.normcase(kind) and _key(folder.parent.parent) == _key(root)


def _a_store_file(path: Path, root: Path) -> bool:
    name = os.path.normcase(path.name)
    return _directly_in(path, paths.PRIVATE, root) and (
        name in {os.path.normcase(left_out) for left_out in LEFT_OUT_FILES}
        or name.startswith(os.path.normcase(LEFT_OUT_PREFIX))
    )


def _a_debug_folder(path: Path, root: Path) -> bool:
    return os.path.normcase(path.name) == os.path.normcase(DEBUG_FOLDER) or (
        _directly_in(path, paths.DOWNLOADS, root) and TEST_RUN_FOLDER.fullmatch(path.name.lower()) is not None
    )


def _walk(data: Path, left_out: LeftOut | None = None) -> Iterator[tuple[Path, list[str], list[str]]]:
    """os.walk over the data folder, sorted, without the folders a backup
    leaves out - noted in `left_out` with the files they hold."""
    root = paths.tenants_root()
    for folder, subfolders, names in os.walk(data):
        here = Path(folder)
        kept = []
        for name in sorted(subfolders):
            if not _a_debug_folder(here / name, root):
                kept.append(name)
            elif left_out is not None:
                left_out.debug_folders.append(_relative(here / name, data))
                left_out.files += sum(len(inside) for _, _, inside in os.walk(here / name))
        # In place: os.walk goes down the folders kept only.
        subfolders[:] = kept
        yield here, subfolders, sorted(names)


def other_files(data: Path, databases: list[Database], left_out: LeftOut | None = None) -> list[Path]:
    """Every file of the data folder but the databases, what SQLite keeps
    beside them and what a backup never holds (noted in `left_out`) - the
    other SQLite files among them included (`other_databases` picks them
    out)."""
    skipped = set()
    for database in databases:
        key = _key(database.source)
        skipped.add(key)
        skipped.update(key + suffix for suffix in SIDE_SUFFIXES)
    root = paths.tenants_root()
    files = []
    for folder, _subfolders, names in _walk(data, left_out):
        for name in names:
            path = folder / name
            if _key(path) in skipped:
                continue
            if is_env_copy(name):
                # Told by its name: never opened, not even for its first bytes.
                if left_out is not None:
                    left_out.env_copies.append(_relative(path, data))
                    left_out.files += 1
                continue
            if _a_store_file(path, root):
                if left_out is not None:
                    left_out.credentials.append(_relative(path, data))
                    left_out.files += 1
                continue
            files.append(path)
    return files


def other_databases(data: Path, files: list[Path]) -> tuple[list[Database], list[Path]]:
    """Among `files` (`other_files`'), the SQLite databases - told by their
    first bytes, whatever their name -, to be copied through the API as the
    others are, and the files left, without what SQLite keeps beside those
    databases (their copy holds it)."""
    found = [path for path in files if is_sqlite_database(path)]
    skipped = set()
    for path in found:
        key = _key(path)
        skipped.add(key)
        skipped.update(key + suffix for suffix in SIDE_SUFFIXES)
    databases = [Database(path, _relative(path, data), OTHER_ROLE, "une autre base SQLite") for path in found]
    return databases, [path for path in files if _key(path) not in skipped]


# -- The backup -------------------------------------------------------------------------------------------------------


def _count(number: int) -> str:
    return f"{number:,}".replace(",", " ")


def _size(size: int) -> str:
    if size < 1024**2:
        return f"{size / 1024:.1f} Ko".replace(".", ",")
    if size < 1024**3:
        return f"{size / 1024**2:.1f} Mo".replace(".", ",")
    return f"{size / 1024**3:.2f} Go".replace(".", ",")


def _set_aside(folder: Path) -> Path | None:
    """Rename a failed backup's folder <stamp>-INCOMPLET; None when it
    cannot be (it then keeps its name, and the message says so)."""
    incomplete = folder.with_name(folder.name + INCOMPLETE)
    try:
        folder.rename(incomplete)
    except OSError:
        return None
    return incomplete


def _refuse_where(data: Path, code: Path, accounts: Path, dest: Path) -> None:
    if not data.is_dir():
        raise BackupError(f"le dossier des données est introuvable : {data} (réglage MARGINMATE_TENANTS_ROOT).")
    if inside(data, code) or inside(code, data):
        raise BackupError(
            f"le dossier des données ({data}) est le dossier du code ou le contient : MARGINMATE_TENANTS_ROOT "
            "est resté à sa valeur par défaut ? Donnez-lui le dossier des espaces, hors du code (DEPLOY.md, section 5)."
        )
    if not accounts.is_file():
        raise BackupError(f"la base des comptes est introuvable : {accounts} (réglage MARGINMATE_ACCOUNTS_DB).")
    if not inside(accounts, data):
        raise BackupError(
            f"la base des comptes ({accounts}) n'est pas dans le dossier des données ({data}) : la sauvegarde ne la "
            "contiendrait pas. Rangez-la dans ce dossier (réglage MARGINMATE_ACCOUNTS_DB)."
        )
    if inside(dest, data):
        raise BackupError(f"la destination ({dest}) est dans le dossier des données : la copie se copierait elle-même.")
    if inside(dest, code):
        raise BackupError(
            f"la destination ({dest}) est dans le dossier du code : une sauvegarde (et son .env) n'a rien à faire "
            "à côté de git. Choisissez un autre dossier."
        )


def make_backup(dest=None, *, with_env: bool = True, say: Callable[[str], None] = lambda line: None) -> Path:
    """Back the data folder up into ``<dest>/<stamp>/`` and return that
    folder. BackupError (French) when refused or failed - the module's
    docstring says which, and what is left behind."""
    data = Path(os.path.abspath(str(data_folder())))
    code = Path(os.path.abspath(str(code_folder())))
    accounts = Path(os.path.abspath(str(accounts_database())))
    dest = Path(os.path.abspath(str(dest))) if dest else Path(os.path.abspath(str(default_destination())))
    _refuse_where(data, code, accounts, dest)

    moment = _now()
    stamp = timezone.localtime(moment).strftime(STAMP_FORMAT)
    folder = dest / stamp
    for taken in (folder, folder.with_name(stamp + INCOMPLETE)):
        if taken.exists():
            raise BackupError(f"le dossier {taken} existe déjà : rien n'a été écrit. Relancez dans une seconde.")

    left_out = LeftOut()
    try:
        databases, missing = find_databases(data, accounts)
        files = other_files(data, databases, left_out)
        others, files = other_databases(data, files)
        databases += others
        env = env_file() if with_env else None
        if env is not None and not env.is_file():
            env = None
        needed = sum(_bytes_of(db.source) for db in databases) + sum(_bytes_of(path) for path in files)
        needed += _bytes_of(env) if env is not None else 0
    except OSError as exc:
        raise BackupError(f"lecture du dossier des données impossible : {exc.strerror or exc}") from exc
    free = shutil.disk_usage(_existing_ancestor(dest)).free
    if free < needed + SPARE_BYTES:
        raise BackupError(
            f"pas assez de place sur le disque de {dest} : il faut {_size(needed + SPARE_BYTES)}, "
            f"il reste {_size(free)}. Rien n'a été écrit."
        )

    say(f"Sauvegarde de {data}")
    say(f"  vers {folder}")
    target_data = folder / DATA
    doing, hint = "la création du dossier", ""
    try:
        target_data.mkdir(parents=True)
        say("Bases SQLite (copiées par SQLite, puis vérifiées) :")
        copied_databases, vanished = [], []
        for database in databases:
            doing = f"{database.label} ({database.relative})"
            hint = OTHER_HINT if database.role == OTHER_ROLE else ""
            target = target_data / database.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                database.tables = copy_database(database.source, target, forget_sessions=True)
            except BackupError:
                if database.role == OTHER_ROLE and not os.path.lexists(database.source):
                    # Gone since it was listed, as any other file may be.
                    vanished.append(database.relative)
                    continue
                raise
            hint = ""
            copied_databases.append(database)
            say(
                f"  {database.label} ({database.relative}) : {len(database.tables)} tables, "
                f"{_count(sum(database.tables.values()))} lignes - intègre"
            )
            if database.role == ACCOUNTS_ROLE:
                say("    sessions de connexion : non sauvegardées (après une restauration, chacun se reconnecte)")
            elif SESSION_TABLE in database.tables:
                say("    sessions de connexion : retirées de la copie")
        copied_files, copied_bytes = 0, 0
        for path in files:
            doing = f"le fichier « {_relative(path, data)} »"
            target = target_data / _relative(path, data)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(path, target)
            except FileNotFoundError:
                if os.path.lexists(path):
                    # Still there: the copy failed (a path too long...).
                    raise
                # Gone since it was listed: the server runs.
                target.unlink(missing_ok=True)
                vanished.append(_relative(path, data))
                continue
            copied_files += 1
            copied_bytes += target.stat().st_size
        # The data folder's empty folders too (a tenant's downloads/...),
        # never one a backup leaves out.
        for source_folder, subfolders, _names in _walk(data):
            for name in subfolders:
                (target_data / _relative(source_folder / name, data)).mkdir(parents=True, exist_ok=True)
        say(f"Autres fichiers : {_count(copied_files)} ({_size(copied_bytes)})")
        if vanished:
            shown = ", ".join(vanished[:5]) + (", …" if len(vanished) > 5 else "")
            say(f"{len(vanished)} fichier(s) disparu(s) pendant la copie (le serveur tournait) : {shown}")
        if left_out.credentials:
            say("Identifiants (page Identifiants) : non sauvegardés, à ressaisir après une restauration.")
        if left_out.debug_folders:
            say(
                "Pages gardées par les récupérations en échec et les « Tester » : non sauvegardées "
                f"({_count(len(left_out.debug_folders))} dossier(s))."
            )
        if env is not None:
            doing = "le fichier .env"
            shutil.copy2(env, folder / ENV)
            say(".env : copié (il contient les secrets : rangez la sauvegarde à l'abri).")
        elif with_env:
            say(f".env : aucun fichier {env_file()} - rien de copié.")
        else:
            say(".env : non copié (--sans-env).")
        commit = git_commit()
        say(f"Code : commit {commit[:12]}" if commit else "Code : commit inconnu (git n'a pas répondu).")
        doing = "le manifeste"
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created_at": moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "folder": stamp,
            "source": str(data),
            "code_commit": commit,
            "env": env is not None,
            "databases": [
                {
                    "path": database.relative,
                    "role": database.role,
                    "espace": database.tenant_name,
                    "tables": database.tables,
                    "rows": sum(database.tables.values()),
                    "bytes": (target_data / database.relative).stat().st_size,
                }
                for database in copied_databases
            ],
            "files": {"count": copied_files, "bytes": copied_bytes},
            "vanished": vanished,
            "missing": missing,
            "left_out": left_out.as_manifest(),
            "sessions": SESSIONS_LEFT_OUT,
        }
        (folder / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        reason = str(exc) if isinstance(exc, BackupError) else (getattr(exc, "strerror", None) or str(exc))
        incomplete = _set_aside(folder) if folder.exists() else None
        raise BackupError(
            f"{doing} : {reason}{hint}", folder=incomplete or (folder if folder.exists() else None)
        ) from exc

    for line in missing:
        say(f"À savoir : {line}")
    # Just before the end, where the window's reader looks: named, never read.
    for relative in sorted(left_out.env_copies):
        say(ENV_COPY_WARNING.format(path=data / relative))
    say(f"Sauvegarde terminée : {folder}")
    return folder
