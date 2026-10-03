"""What deploy.cmd and refresh_dev_data.cmd ask the settings (DEPLOY.md, section 10).

    .venv\\Scripts\\python.exe -c "import sys; from accounts import deployment; sys.exit(deployment.main())" production
    .venv\\Scripts\\python.exe -c "import sys; from accounts import deployment; sys.exit(deployment.main())" development <backup>
    .venv\\Scripts\\python.exe -c "import sys; from accounts import deployment; sys.exit(deployment.main())" purge-sessions <backup>

Two copies of MarginMate live on the owner's PC (CLAUDE.md, « Two copies:
development and production »): PRODUCTION, C:\\MarginMate\\app, a git clone
of the development folder, its .env saying DJANGO_DEBUG=False and
MARGINMATE_HTTPS=1, its data in C:\\MarginMate\\data; DEVELOPMENT, where the
code is edited, its .env saying DJANGO_DEBUG=True and no MARGINMATE_HTTPS,
its data a copy (data-dev). Each script refuses to run in the other one:
deploy.cmd stops the server, backs up and migrates - in the development
folder it would do all that to the copy, and a refresh run in production
would move the site's own data aside.

The scripts ask the SETTINGS, loaded exactly as every command loads them
(.env, then the environment), never the .env read by hand: a hand parser
disagrees with python-dotenv on quotes, spaces and « export », and the
decision would then be about another file than the one the server reads.
Nothing here imports a model (no django.setup()): the settings alone.

Answers go to stdout as NAME=value lines, which the scripts read with
``for /f`` into the variables MM_NAME; a refusal is a French sentence on
stderr and exit code REFUSED; settings that do not load, or a step that
failed, are said, exit code 1. Both refuse on anything but 0. The modes and
the answers' names are an interface between files of ONE version: deploy.cmd
asks before its merge, on the code it was copied from, and
refresh_dev_data.cmd runs beside its code.

* ``production``: refused unless DEBUG is off and HTTPS on, when the data
  folder is the code's (TENANTS_ROOT at its default), when the data folder
  is not named by its real path (below), or when the accounts database is
  outside the data folder (backup_data would refuse it - after deploy.cmd
  had stopped the server); DATA= the data folder, which deploy.cmd names in
  its rollback instructions.
* ``development <backup>``: refused in production (HTTPS on), when the
  data folder is or holds the code's folder (moved aside, it would take the
  code with it), when the backup is not one `manage.py backup_data` finished
  (no data\\ or manifest.json, an -INCOMPLET folder), when the data folder is
  the one the backup was taken FROM (its manifest's « source »: the
  production's data - a development .env still pointing there), when the
  data folder is not named by its real path, when the accounts database is
  not inside the data folder (MARGINMATE_ACCOUNTS_DB left on production's:
  runserver would write its sessions into the live logins database, and
  DEPLOY.md 10.5's migrate_tenants would migrate it), when one is inside the
  other, when the backup's .env holds the same DJANGO_SECRET_KEY as this
  folder's settings (the development copy must have its own: with
  production's it opens production's sessions and whatever is sealed with
  it - half of the « Identifiants » store's key, the signed cookies, the
  browsers' storage scopes; read with python-dotenv, as the settings read a
  .env, compared in constant time, never printed), and while the data
  folder or the backup's data holds a COPY of a .env (`is_env_copy`: a
  « .env.bak_<date> » left in production's data folder would come into
  data-dev, read by coding sessions): named, never opened, the owner told
  to delete it. DATA= the development data folder, BACKUP= the backup, and
  PREVIOUS= the name the current data folder is moved to
  (``<name>.ancien-<AAAA-MM-JJ_HHMMSS>``, never an existing one) when there
  is one.
* ``purge-sessions <backup>``, once refresh_dev_data.cmd has copied the
  backup: the refusals of ``development``, then every row of Django's
  session table deleted from every database of the data folder, each file
  rewritten (VACUUM: a deleted row's bytes stay in it otherwise): the
  accounts database the settings name and the one the copy brought (the
  manifest's « comptes » database, inside the data folder only), whatever
  their first bytes say, and every other file of the data folder that starts
  as an SQLite database does (`SQLITE_HEADER`) and holds that table, whatever
  its name - a copy made by hand (``accounts.sqlite3.bak_*``), the database
  kept from before an adoption. A backup made before backup_data emptied
  them, or before it copied every database through SQLite, holds
  production's live sessions - a session key is a login on the public site,
  and data-dev is read by coding sessions. Nothing outside the data folder
  is opened, a link or a junction leading out of it included (the file's
  real place is asked). Opened ``mode=rw``: a database that is not there is
  not made. A database that will not open holds back none of the others:
  every one is tried, then the failures are said together. SESSIONS= how
  many went. The push devices (`PUSH_DEVICE_TABLE`, their endpoints and
  keys) go with them, in the same transaction, wherever that table is.

**A folder has more than one name** (review of 01/10/2026): an 8.3 short
name (C:\\MARGIN~1\\data), a directory junction or a symbolic link, a subst
drive, a \\\\?\\ prefix, another case - each named production's data folder
past a comparison of abspath strings. So every comparison here is made
three ways (`inside`): as written, as resolved (os.path.realpath), and,
where both exist, as the same folder on disk (os.path.samefile); a refusal
takes any of them saying yes, both directions (the data folder inside the
other, the other inside it, the same). What is OPENED or relied upon as
inside the data folder - the accounts database, a database purged - must be
inside it both as written and as resolved (`really_inside`). And the data
folder must be named by its real path, TENANTS_ROOT too: DATA= is what the
scripts move aside and copy into.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime
from pathlib import Path

REFUSED = 3
PRODUCTION = "production"
DEVELOPMENT = "development"
PURGE_SESSIONS = "purge-sessions"
#: The modes that take a backup's folder.
WITH_A_BACKUP = (DEVELOPMENT, PURGE_SESSIONS)
#: Mirrors accounts.data_backup (not imported: it imports the models).
INCOMPLETE = "-INCOMPLET"
MANIFEST = "manifest.json"
DATA = "data"
ENV = ".env"
ACCOUNTS_ROLE = "comptes"
SESSION_TABLE = "django_session"
#: accounts.PushDevice's table: the browsers that receive the site's push
#: notifications (endpoints are bearer capabilities, `auth` a secret). The
#: development copy never pushes - its SECRET_KEY, so its VAPID key, is its
#: own - so a copy of them in data-dev is exposure only: emptied with the
#: sessions. Production's backups keep them (a restore keeps the phones).
PUSH_DEVICE_TABLE = "accounts_pushdevice"
OLD = ".ancien-"
#: The .env line config/settings.py reads SECRET_KEY from.
SECRET_KEY_NAME = "DJANGO_SECRET_KEY"
#: What every SQLite database file starts with (accounts.data_backup.SQLITE_HEADER).
SQLITE_HEADER = b"SQLite format 3\x00"
#: A copy of a .env, by its name (`is_env_copy`), as robocopy's /XF reads
#: them in refresh_dev_data.cmd: accounts.data_backup leaves such a file out
#: of every backup, the refresh out of data-dev.
ENV_COPY_PATTERNS = (ENV, f"{ENV}.*")
#: How many files a refusal names before « … ».
NAMED_AT_MOST = 5

SAME_KEY = (
    "la sauvegarde a été faite avec la même clé secrète (DJANGO_SECRET_KEY) que celle de ce dossier : la copie de "
    "développement doit avoir la sienne, sinon elle ouvrirait les sessions de la production et tout ce qui est "
    "scellé avec cette clé. Mettez une nouvelle DJANGO_SECRET_KEY dans le .env de ce dossier (DEPLOY.md, "
    "section 5, dit comment en tirer une), puis relancez."
)
NOT_REAL = (
    "{what} de ce dossier ({named}) n'est pas nommé par son vrai chemin : il mène à {real} (un lien, une "
    "jonction, un lecteur substitué, un nom court ou un préfixe \\\\?\\). Écrivez dans MARGINMATE_TENANTS_ROOT "
    "le vrai chemin du dossier (DEPLOY.md, section 10)."
)
ENV_COPY = (
    "{where} {what} : {shown}. Une copie du .env garde la clé secrète et des mots de passe du site : "
    "supprimez-{it}{original} (DEPLOY.md, section 12), puis relancez."
)


class Refused(Exception):
    """A French sentence saying why the script must not run here."""


class Failed(Exception):
    """A French sentence saying what could not be done."""


# -- Paths, whatever names them ---------------------------------------------------------------------------------------


def _key(path) -> str:
    """`path` as written, made absolute (Windows: whatever the case)."""
    return os.path.normcase(os.path.abspath(str(path)))


#: The prefixes that only say how Windows is to read a path (\\?\, \\.\),
#: by what the path is without them.
_READ_AS_IS = (("\\\\?\\UNC\\", "\\\\"), ("\\\\.\\UNC\\", "\\\\"), ("\\\\?\\", ""), ("\\\\.\\", ""))


def _real(path) -> str:
    """`path` as it really is: os.path.realpath resolves a junction, a
    symbolic link, an 8.3 short name and a subst drive, as far as the path
    exists - and a \\\\?\\ prefix, which realpath keeps when it was given
    one, is taken off first."""
    written = os.path.abspath(str(path))
    for prefix, plain in _READ_AS_IS:
        if written.startswith(prefix):
            written = plain + written[len(prefix) :]
            break
    return os.path.realpath(written)


def _real_key(path) -> str:
    """`_real`, Windows: whatever the case."""
    return os.path.normcase(_real(path))


def _under(inner_key: str, outer_key: str) -> bool:
    return inner_key == outer_key or inner_key.startswith(outer_key.rstrip("\\/") + os.sep)


def _same_folder(one, other) -> bool:
    try:
        return os.path.samefile(one, other)
    except (OSError, ValueError):
        # One of them is not there (or is no path at all).
        return False


def inside(inner, outer) -> bool:
    """`inner` is `outer` or somewhere under it, by ANY of their names: as
    written, as resolved (`_real_key`), or - when `outer` exists - as the
    same folder on disk as `inner` or a folder above it (os.path.samefile: a
    folder reached by a network path to this PC, which realpath does not
    turn back). The question a refusal asks: one name saying yes is
    enough."""
    if _under(_key(inner), _key(outer)) or _under(_real_key(inner), _real_key(outer)):
        return True
    if not os.path.exists(outer):
        return False
    here = Path(os.path.abspath(str(inner)))
    return any(_same_folder(folder, outer) for folder in (here, *here.parents))


def overlap(one, other) -> bool:
    """`one` is `other`, or one of them holds the other (`inside`)."""
    return inside(one, other) or inside(other, one)


def really_inside(inner, outer) -> bool:
    """`inner` is under `outer` as written AND as resolved: a name inside
    `outer` leading out of it (a junction, a link on the way) is not inside.
    The question asked of what is opened, or relied upon, as `outer`'s."""
    return _under(_key(inner), _key(outer)) and _under(_real_key(inner), _real_key(outer))


def _a_link(path: Path) -> bool:
    """A symbolic link or a directory junction (os.walk goes down a junction
    as down a folder in Python 3.11)."""
    try:
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return False
    return os.path.islink(path) or (bool(tag) and tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None))


def is_env_copy(name: str) -> bool:
    """`name` is a .env's: « .env », or « .env. » and anything
    (« .env.bak_<date> »), whatever the case - as `ENV_COPY_PATTERNS`
    read for robocopy."""
    lowered = name.lower()
    return lowered == ENV or lowered.startswith(f"{ENV}.")


def env_copies(folder: Path) -> list[Path]:
    """Every copy of a .env under `folder`, told by its name - never opened -,
    in a stable order. Nothing reached through a link or a junction: it is
    not this folder's."""
    found = []
    for here, subfolders, names in os.walk(folder):
        subfolders[:] = sorted(name for name in subfolders if not _a_link(Path(here) / name))
        found += [Path(here) / name for name in sorted(names) if is_env_copy(name)]
    return found


# -- The settings -----------------------------------------------------------------------------------------------------


def _settings():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.conf import settings

    return settings


def data_folder(settings) -> Path:
    return Path(os.path.abspath(str(Path(settings.TENANTS_ROOT).parent)))


def _accounts_database(settings) -> Path:
    """MARGINMATE_ACCOUNTS_DB as the settings resolved it ("accounts" is
    accounts.router.ACCOUNTS_ALIAS, not imported: nothing here loads the
    apps)."""
    return Path(os.path.abspath(str(settings.DATABASES["accounts"]["NAME"])))


def _outside_the_code(settings, data: Path) -> None:
    code = Path(settings.BASE_DIR)
    if overlap(data, code):
        raise Refused(
            f"le dossier des données ({data}) est le dossier du code ou le contient : MARGINMATE_TENANTS_ROOT n'est "
            "pas réglé dans le .env de ce dossier (DEPLOY.md, section 10)."
        )


def _named_by_its_real_path(settings, data: Path) -> None:
    """The data folder and TENANTS_ROOT are named as they really are: DATA=
    is what the scripts move aside and copy into, and a name leading
    elsewhere is the alias every comparison has to see through."""
    tenants = Path(os.path.abspath(str(settings.TENANTS_ROOT)))
    for named, what in ((data, "le dossier des données"), (tenants, "le dossier des espaces")):
        if _real_key(named) != _key(named):
            raise Refused(NOT_REAL.format(what=what, named=named, real=_real(named)))


def _accounts_in_the_data(settings, data: Path) -> None:
    accounts = _accounts_database(settings)
    if not really_inside(accounts, data):
        led = "" if _real_key(accounts) == _key(accounts) else f", qui mène à {_real(accounts)},"
        raise Refused(
            f"la base des comptes ({accounts}){led} n'est pas dans le dossier des données de ce dossier ({data}) : "
            f"MARGINMATE_ACCOUNTS_DB doit être {data / 'accounts.sqlite3'}."
        )


def production(settings) -> dict[str, str]:
    """This folder is the production copy: its answers, else Refused."""
    if settings.DEBUG:
        raise Refused(
            "le .env de ce dossier dit DJANGO_DEBUG=True : c'est le dossier de développement. deploy.cmd se lance "
            "dans la copie de production, C:\\MarginMate\\app (DEPLOY.md, section 10)."
        )
    if not getattr(settings, "HTTPS", False):
        raise Refused(
            "le .env de ce dossier ne dit pas MARGINMATE_HTTPS=1 : ce n'est pas la copie de production, qui sert le "
            "site en ligne. deploy.cmd se lance dans C:\\MarginMate\\app (DEPLOY.md, section 10)."
        )
    data = data_folder(settings)
    _outside_the_code(settings, data)
    _named_by_its_real_path(settings, data)
    _accounts_in_the_data(settings, data)
    return {"DATA": str(data)}


# -- The development copy ---------------------------------------------------------------------------------------------


def _backup_manifest(backup: Path) -> dict:
    """The manifest of a backup `manage.py backup_data` finished, with the
    folder it was taken from in « source »; else Refused."""
    if not backup.is_dir():
        raise Refused(f"la sauvegarde {backup} est introuvable.")
    if backup.name.upper().endswith(INCOMPLETE):
        raise Refused(f"{backup} est une sauvegarde qui a échoué ({INCOMPLETE}) : elle ne se recopie pas.")
    manifest = backup / MANIFEST
    if not (backup / DATA).is_dir() or not manifest.is_file():
        raise Refused(
            f"{backup} n'est pas une sauvegarde terminée de « manage.py backup_data » (il y faut un dossier "
            f"{DATA} et un {MANIFEST})."
        )
    try:
        described = json.loads(manifest.read_text(encoding="utf-8"))
        source = described["source"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused(f"le manifeste de {backup} ne se lit pas ({exc}).") from exc
    if not isinstance(source, str) or not source.strip() or "\N{NULL}" in source:
        raise Refused(f"le manifeste de {backup} ne dit pas de quel dossier il vient.")
    return described


def _own_secret_key(settings, backup: Path) -> None:
    """Refused when the backup's .env holds this folder's SECRET_KEY. A
    backup without a .env (--sans-env), or whose .env names no key, says
    nothing about it."""
    from dotenv import dotenv_values

    env = backup / ENV
    if not env.is_file():
        return
    try:
        theirs = dotenv_values(env).get(SECRET_KEY_NAME) or ""
    except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
        raise Refused(
            f"le .env de la sauvegarde {backup} ne se lit pas : rien ne dit qu'il n'a pas la clé secrète de ce "
            "dossier (DJANGO_SECRET_KEY)."
        ) from exc
    ours = str(getattr(settings, "SECRET_KEY", "") or "")
    if theirs and hmac.compare_digest(theirs.encode("utf-8"), ours.encode("utf-8")):
        raise Refused(SAME_KEY)


def _no_env_copy(data: Path, backup: Path, source: str) -> None:
    """Refused while data-dev, or the data the backup brings, holds a copy
    of a .env - named, never opened. The backup's own .env, beside its
    data\\, is the backup's (`_own_secret_key` reads it)."""
    places = (
        (data, f"le dossier des données de ce dossier ({data}) contient", "", ""),
        (
            backup / DATA,
            f"les données de la sauvegarde {backup} contiennent",
            f" (et, dans {source}, le fichier d'origine s'il y est encore)",
            f" (et, dans {source}, les fichiers d'origine s'ils y sont encore)",
        ),
    )
    for folder, where, original_of_one, original_of_several in places:
        found = env_copies(folder)
        if not found:
            continue
        one = len(found) == 1
        shown = ", ".join(str(path) for path in found[:NAMED_AT_MOST]) + (", …" if len(found) > NAMED_AT_MOST else "")
        raise Refused(
            ENV_COPY.format(
                where=where,
                what="une copie d'un fichier .env" if one else f"{len(found)} copies de fichiers .env",
                shown=shown,
                it="la" if one else "les",
                original=original_of_one if one else original_of_several,
            )
        )


def _development_copy(settings, backup) -> tuple[Path, Path, dict]:
    """The refusals `development` and `purge-sessions` share: (the data
    folder, the backup, its manifest) when this folder is the development
    copy and `backup` may replace its data, else Refused."""
    if getattr(settings, "HTTPS", False):
        raise Refused(
            "le .env de ce dossier dit MARGINMATE_HTTPS=1 : c'est la copie de production. Les données du site ne "
            "se remplacent pas par une sauvegarde de cette façon (DEPLOY.md, section 10)."
        )
    data = data_folder(settings)
    _outside_the_code(settings, data)
    backup = Path(os.path.abspath(str(backup)))
    manifest = _backup_manifest(backup)
    source = Path(manifest["source"])
    if overlap(data, source):
        raise Refused(
            f"le dossier des données de ce dossier ({data}) est celui dont la sauvegarde a été faite ({source}) : "
            "ce sont les données du site. Le .env de développement doit pointer vers sa copie (data-dev)."
        )
    _named_by_its_real_path(settings, data)
    # Really inside `data`, which is neither the source nor inside it nor
    # holding it, by any name: the production's accounts database is ruled
    # out with every other place.
    _accounts_in_the_data(settings, data)
    if overlap(data, backup):
        raise Refused(f"le dossier des données ({data}) et la sauvegarde ({backup}) sont l'un dans l'autre.")
    _own_secret_key(settings, backup)
    _no_env_copy(data, backup, manifest["source"])
    return data, backup, manifest


def development(settings, backup, now: datetime | None = None) -> dict[str, str]:
    """This folder is the development copy and `backup` may replace its
    data: the answers, else Refused."""
    data, backup, _manifest = _development_copy(settings, backup)
    answers = {"DATA": str(data), "BACKUP": str(backup)}
    if data.exists():
        # The PC's local time: a folder name the owner reads.
        stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")  # noqa: DTZ005
        old = data.with_name(f"{data.name}{OLD}{stamp}")
        number = 1
        while old.exists():
            number += 1
            old = data.with_name(f"{data.name}{OLD}{stamp}-{number}")
        answers["PREVIOUS"] = str(old)
    return answers


# -- The sessions a copy brought --------------------------------------------------------------------------------------


def _forget_sessions(database: Path) -> int:
    """Delete every session of `database` - and every push device, when it
    has accounts.PushDevice's table - and rewrite the file; how many
    sessions went (0: no session table). Failed when it does not open or
    write."""
    uri = database.as_uri() + "?mode=rw"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=20)
        try:
            tables = {
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
                    (SESSION_TABLE, PUSH_DEVICE_TABLE),
                )
            }
            if not tables:
                return 0
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute(f'DELETE FROM "{SESSION_TABLE}"').rowcount if SESSION_TABLE in tables else 0
            if PUSH_DEVICE_TABLE in tables:
                connection.execute(f'DELETE FROM "{PUSH_DEVICE_TABLE}"')
            connection.execute("COMMIT")
            # A DELETE only frees the rows' room: their bytes - the session
            # keys - stay in the file until it is rewritten.
            connection.execute("VACUUM")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise Failed(f"les sessions de connexion de {database} n'ont pas pu être effacées ({exc}).") from exc
    return removed


def _starts_as_a_database(path: Path) -> bool:
    try:
        with open(path, "rb") as file:
            return file.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _databases_in(data: Path) -> list[Path]:
    """Every file under `data` that starts as an SQLite database, in a
    stable order."""
    found = []
    for folder, subfolders, names in os.walk(data):
        subfolders.sort()
        for name in sorted(names):
            path = Path(folder) / name
            if _starts_as_a_database(path):
                found.append(path)
    return found


def purge_sessions(settings, backup) -> dict[str, str]:
    """The sessions (and push devices) the copy of `backup` brought into this
    folder's data, deleted from every database of it - behind the refusals
    of `development`."""
    data, _backup, manifest = _development_copy(settings, backup)
    named = [_accounts_database(settings)]
    listed = manifest.get("databases")
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict) or entry.get("role") != ACCOUNTS_ROLE or not isinstance(entry.get("path"), str):
            continue
        named.append(Path(os.path.abspath(str(data / entry["path"]))))
    databases, seen = [], set()
    for database in [*named, *_databases_in(data)]:
        # A path leading out of the data folder is never opened, and a file
        # reached by two names is purged once.
        if not database.is_file() or not really_inside(database, data) or _real_key(database) in seen:
            continue
        seen.add(_real_key(database))
        databases.append(database)
    removed, failures = 0, []
    for database in databases:
        try:
            removed += _forget_sessions(database)
        except Failed as exc:
            failures.append(str(exc))
    if failures:
        raise Failed(" ".join(failures))
    return {"SESSIONS": str(removed)}


def main(argv=None, stdout=None, stderr=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if not argv or argv[0] not in (PRODUCTION, *WITH_A_BACKUP) or (argv[0] in WITH_A_BACKUP and len(argv) != 2):
        stderr.write(
            f"Usage : {PRODUCTION} | {DEVELOPMENT} <dossier de la sauvegarde> | "
            f"{PURGE_SESSIONS} <dossier de la sauvegarde>\n"
        )
        return 1
    try:
        settings = _settings()
        settings.DEBUG  # noqa: B018 - loads the settings: a refusal at load is said below
        if argv[0] == PRODUCTION:
            answers = production(settings)
        elif argv[0] == DEVELOPMENT:
            answers = development(settings, argv[1])
        else:
            answers = purge_sessions(settings, argv[1])
    except Refused as exc:
        stderr.write(f"REFUS : {exc}\n")
        return REFUSED
    except Failed as exc:
        stderr.write(f"ÉCHEC : {exc}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - the settings refuse to load (ImproperlyConfigured...)
        stderr.write(f"Les réglages de ce dossier ne se chargent pas : {exc}\n")
        return 1
    for name, value in answers.items():
        stdout.write(f"{name}={value}\n")
    return 0
