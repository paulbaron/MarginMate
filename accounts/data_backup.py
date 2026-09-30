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
accounts database no longer names (backed up all the same, and said). Every
other file - media, private (the signing keys), downloads, backups, staging,
imports, the log - is copied as a file; an SQLite file among them (« Données »'s
own safety copies in a tenant's backups\\) is one nobody writes.

**Refused before anything is written**, each in French: a data folder that
is missing, IS the code's folder or holds it (TENANTS_ROOT left at its
default, beside manage.py - the code's folder is no data folder); an accounts
database missing, or outside the data folder (the backup would not hold it);
a destination inside the data folder (the copy would copy itself) or inside
the code's folder (a folder under git, and the backup holds the .env); a
target folder that already exists; less free space than the copy takes.

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
long) is a copy that failed. A database is never let go that way. The size
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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from . import paths
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
    """`inner` is `outer` or somewhere under it (Windows: whatever the case)."""
    inner_key, outer_key = _key(inner), _key(outer)
    if inner_key == outer_key:
        return True
    return inner_key.startswith(outer_key.rstrip("\\/") + os.sep)


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


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Rows per table (SQLite's own tables left out), by name."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND substr(name, 1, 7) != 'sqlite_' ORDER BY name"
        )
    ]
    counts = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    return counts


def integrity(connection: sqlite3.Connection) -> list[str]:
    """`PRAGMA integrity_check`'s answer: ["ok"] for a sound database."""
    return [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]


def copy_database(source: Path, target: Path) -> dict[str, int]:
    """Copy `source` to `target` through the backup API, check the copy, and
    return its rows per table. Raises BackupError (French) otherwise."""
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
        shown = " ; ".join(answer[:3]) + (" ; …" if len(answer) > 3 else "")
        raise BackupError(f"la copie ne passe pas la vérification d'intégrité ({shown}).")
    if copied != expected:
        differing = sorted(
            set(copied) ^ set(expected) | {name for name in copied if copied.get(name) != expected.get(name)}
        )
        shown = ", ".join(
            f"{name} : {expected.get(name, 'absente')} dans la base, {copied.get(name, 'absente')} dans la copie"
            for name in differing[:3]
        )
        raise BackupError(f"la copie n'a pas le même nombre de lignes que la base ({shown}).")
    return copied


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


def other_files(data: Path, databases: list[Database]) -> list[Path]:
    """Every file of the data folder but the databases and what SQLite
    keeps beside them."""
    skipped = set()
    for database in databases:
        key = _key(database.source)
        skipped.add(key)
        skipped.update(key + suffix for suffix in SIDE_SUFFIXES)
    files = []
    for folder, subfolders, names in os.walk(data):
        subfolders.sort()
        for name in sorted(names):
            path = Path(folder) / name
            if _key(path) not in skipped:
                files.append(path)
    return files


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

    try:
        databases, missing = find_databases(data, accounts)
        files = other_files(data, databases)
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
    doing = "la création du dossier"
    try:
        target_data.mkdir(parents=True)
        say("Bases SQLite (copiées par SQLite, puis vérifiées) :")
        for database in databases:
            doing = f"{database.label} ({database.relative})"
            target = target_data / database.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            database.tables = copy_database(database.source, target)
            say(
                f"  {database.label} ({database.relative}) : {len(database.tables)} tables, "
                f"{_count(sum(database.tables.values()))} lignes - intègre"
            )
        copied_bytes, vanished = 0, []
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
            copied_bytes += target.stat().st_size
        # The data folder's empty folders too (a tenant's downloads/...).
        for source_folder, subfolders, _names in os.walk(data):
            for name in subfolders:
                (target_data / _relative(Path(source_folder) / name, data)).mkdir(parents=True, exist_ok=True)
        say(f"Autres fichiers : {_count(len(files) - len(vanished))} ({_size(copied_bytes)})")
        if vanished:
            shown = ", ".join(vanished[:5]) + (", …" if len(vanished) > 5 else "")
            say(f"{len(vanished)} fichier(s) disparu(s) pendant la copie (le serveur tournait) : {shown}")
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
                for database in databases
            ],
            "files": {"count": len(files) - len(vanished), "bytes": copied_bytes},
            "vanished": vanished,
            "missing": missing,
        }
        (folder / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        reason = str(exc) if isinstance(exc, BackupError) else (getattr(exc, "strerror", None) or str(exc))
        incomplete = _set_aside(folder) if folder.exists() else None
        raise BackupError(f"{doing} : {reason}", folder=incomplete or (folder if folder.exists() else None)) from exc

    for line in missing:
        say(f"À savoir : {line}")
    say(f"Sauvegarde terminée : {folder}")
    return folder
