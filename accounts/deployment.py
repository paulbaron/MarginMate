"""What deploy.cmd and refresh_dev_data.cmd ask the settings (DEPLOY.md, section 10).

    .venv\\Scripts\\python.exe -c "import sys; from accounts import deployment; sys.exit(deployment.main())" production
    .venv\\Scripts\\python.exe -c "import sys; from accounts import deployment; sys.exit(deployment.main())" developpement <sauvegarde>

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
``for /f``; a refusal is a French sentence on stderr and exit code REFUSED;
settings that do not load are said, exit code 1. Both refuse on anything
but 0.

* ``production``: refused unless DEBUG is off and HTTPS on, when the data
  folder is the code's (TENANTS_ROOT at its default), or when the accounts
  database is outside the data folder (backup_data would refuse it - after
  deploy.cmd had stopped the server); DONNEES= the data folder, which
  deploy.cmd names in its rollback instructions.
* ``developpement <sauvegarde>``: refused in production (HTTPS on), when the
  data folder is or holds the code's folder (moved aside, it would take the
  code with it), when the backup is not one `manage.py backup_data` finished
  (no data\\ or manifest.json, an -INCOMPLET folder), when the data folder is
  the one the backup was taken FROM (its manifest's « source »: the
  production's data - a development .env still pointing there), when the
  accounts database is not inside the data folder (MARGINMATE_ACCOUNTS_DB
  left on production's: runserver would write its sessions into the live
  logins database, and DEPLOY.md 10.5's migrate_tenants would migrate it),
  and when one is inside the other. DONNEES= the development data folder,
  SAUVEGARDE= the backup, and ANCIEN= the name the current data folder is
  moved to (``<nom>.ancien-<AAAA-MM-JJ_HHMMSS>``, never an existing one) when
  there is one.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

REFUSED = 3
PRODUCTION = "production"
DEVELOPMENT = "developpement"
#: Mirrors accounts.data_backup (not imported: it imports the models).
INCOMPLETE = "-INCOMPLET"
MANIFEST = "manifest.json"
DATA = "data"
OLD = ".ancien-"


class Refused(Exception):
    """A French sentence saying why the script must not run here."""


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def inside(inner: Path, outer: Path) -> bool:
    inner_key, outer_key = _key(inner), _key(outer)
    return inner_key == outer_key or inner_key.startswith(outer_key.rstrip("\\/") + os.sep)


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
    if inside(data, code) or inside(code, data):
        raise Refused(
            f"le dossier des données ({data}) est le dossier du code ou le contient : MARGINMATE_TENANTS_ROOT n'est "
            "pas réglé dans le .env de ce dossier (DEPLOY.md, section 10)."
        )


def _accounts_in_the_data(settings, data: Path) -> None:
    accounts = _accounts_database(settings)
    if not inside(accounts, data):
        raise Refused(
            f"la base des comptes ({accounts}) n'est pas dans le dossier des données de ce dossier ({data}) : "
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
    _accounts_in_the_data(settings, data)
    return {"DONNEES": str(data)}


def _backup_source(backup: Path) -> Path:
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
        source = json.loads(manifest.read_text(encoding="utf-8"))["source"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused(f"le manifeste de {backup} ne se lit pas ({exc}).") from exc
    if not isinstance(source, str) or not source.strip():
        raise Refused(f"le manifeste de {backup} ne dit pas de quel dossier il vient.")
    return Path(source)


def development(settings, backup, now: datetime | None = None) -> dict[str, str]:
    """This folder is the development copy and `backup` may replace its
    data: the answers, else Refused."""
    if getattr(settings, "HTTPS", False):
        raise Refused(
            "le .env de ce dossier dit MARGINMATE_HTTPS=1 : c'est la copie de production. Les données du site ne "
            "se remplacent pas par une sauvegarde de cette façon (DEPLOY.md, section 10)."
        )
    data = data_folder(settings)
    _outside_the_code(settings, data)
    backup = Path(os.path.abspath(str(backup)))
    source = _backup_source(backup)
    if inside(data, source) or inside(source, data):
        raise Refused(
            f"le dossier des données de ce dossier ({data}) est celui dont la sauvegarde a été faite ({source}) : "
            "ce sont les données du site. Le .env de développement doit pointer vers sa copie (data-dev)."
        )
    # Inside `data`, which is neither inside the source nor holding it: the
    # production's accounts database is ruled out with every other place.
    _accounts_in_the_data(settings, data)
    if inside(data, backup) or inside(backup, data):
        raise Refused(f"le dossier des données ({data}) et la sauvegarde ({backup}) sont l'un dans l'autre.")
    answers = {"DONNEES": str(data), "SAUVEGARDE": str(backup)}
    if data.exists():
        stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
        old = data.with_name(f"{data.name}{OLD}{stamp}")
        number = 1
        while old.exists():
            number += 1
            old = data.with_name(f"{data.name}{OLD}{stamp}-{number}")
        answers["ANCIEN"] = str(old)
    return answers


def main(argv=None, stdout=None, stderr=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if not argv or argv[0] not in (PRODUCTION, DEVELOPMENT) or (argv[0] == DEVELOPMENT and len(argv) != 2):
        stderr.write(f"Usage : {PRODUCTION} | {DEVELOPMENT} <dossier de la sauvegarde>\n")
        return 1
    try:
        settings = _settings()
        settings.DEBUG  # noqa: B018 - loads the settings: a refusal at load is said below
        if argv[0] == PRODUCTION:
            answers = production(settings)
        else:
            answers = development(settings, argv[1])
    except Refused as exc:
        stderr.write(f"REFUS : {exc}\n")
        return REFUSED
    except Exception as exc:  # the settings refuse to load (ImproperlyConfigured...)
        stderr.write(f"Les réglages de ce dossier ne se chargent pas : {exc}\n")
        return 1
    for name, value in answers.items():
        stdout.write(f"{name}={value}\n")
    return 0
