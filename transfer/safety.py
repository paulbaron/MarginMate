"""Backups before a confirmed import or clear (§5.8).

Always a copy of the SQLite database - 10 MB, under a second, and a
« Fusionner » writes too (it adds products and rebuilds movements). Before a
« Remplacer » or an « Effacer », also an importable archive of the sections
that change (`sections_at_risk`), so the page itself can undo it. Both go
to `backups/` beside the database, and the order is fixed: database,
archive, then the transaction - a backup that fails changes nothing.

Backups are never deleted by the app: deleting data is the owner's act.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from django.conf import settings
from django.db import connection
from django.utils import timezone

from transfer import registry
from transfer.sections.base import Strategy

#: « 2026-09-19_143012_avant-import.sqlite3 », « …_2.zip » on a collision.
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})_([a-z-]+)(?:_\d+)?\.(zip|sqlite3)$")
LABELS = {"import": "avant-import", "effacement": "avant-effacement"}
REASONS = {"import": "sauvegarde avant import", "effacement": "sauvegarde avant effacement"}
#: Room asked for on top of what a backup is expected to take.
MARGIN = 1.2


class SafetyError(Exception):
    """French, shown as is: nothing was imported or cleared."""


def backup_path() -> Path:
    """Where backups go, as shown on the page (not created by a GET)."""
    return Path(settings.DATA_BACKUP_DIR)


def backup_dir() -> Path:
    """settings.DATA_BACKUP_DIR, created on demand."""
    path = backup_path()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stamp() -> str:
    return timezone.localtime(timezone.now()).strftime("%Y-%m-%d_%H%M%S")


def _free_name(stem: str, suffix: str) -> Path:
    folder = backup_dir()
    candidate = folder / f"{stem}{suffix}"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{number}{suffix}"
        number += 1
    return candidate


def backup_database(label: str, stamp: str | None = None) -> Path:
    """sqlite3 backup API from Django's own connection, to
    backups/<YYYY-MM-DD_HHMMSS>_<label>.sqlite3; refuses (RuntimeError) inside
    an atomic block; ':memory:' works (tests).

    Django's own connection, not a second one on the file: it sees what this
    process committed, WAL included, and the backup API copies a consistent
    database even while another connection writes."""
    if connection.in_atomic_block:
        raise RuntimeError("a database backup inside a transaction would copy uncommitted rows")
    path = _free_name(f"{stamp or _stamp()}_{label}", ".sqlite3")
    connection.ensure_connection()
    target = sqlite3.connect(str(path))
    try:
        connection.connection.backup(target)
    except BaseException:
        target.close()
        path.unlink(missing_ok=True)
        raise
    target.close()
    return path


def safety_export(keys: set[str], label: str, stamp: str | None = None, reason: str | None = None) -> Path | None:
    """run_export(keys, backups/<stamp>_<label>.zip, reason="sauvegarde avant …", closed=False); None if keys empty."""
    from transfer.runner import run_export

    keys = {key for key in keys if registry.is_registered(key)}
    if not keys:
        return None
    path = _free_name(f"{stamp or _stamp()}_{label}", ".zip")
    try:
        run_export(keys, path, reason=reason or f"sauvegarde {label.replace('-', ' ')}", closed=False)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def database_size() -> int:
    name = str(settings.DATABASES["default"]["NAME"])
    if name == ":memory:" or name.startswith("file:"):
        return 0
    try:
        return Path(name).stat().st_size
    except OSError:
        return 0


def estimated_bytes(keys) -> int:
    """What an archive of these sections will weigh, roughly: the files each
    section counts (« Mo de fichiers »), the JSON a small change beside."""
    total = 0
    for key in keys:
        if not registry.is_registered(key):
            continue
        try:
            megabytes = registry.get(key).count().get("Mo de fichiers", 0)
        except Exception:  # noqa: BLE001 - an estimate
            megabytes = 0
        total += int(megabytes) * 1_000_000 + 1_000_000
    return total


def estimated_megabytes(keys) -> int:
    return max(1, round(estimated_bytes(keys) / 1_000_000)) if keys else 0


def sections_at_risk(report, *, strategies=None, cleared=()) -> set[str]:
    """What the safety archive must hold (§6.5), from the preview a confirm
    is held to (runner.NotAsPreviewed: equal outcomes, so the same sections).

    Every section whose rows the run changes or deletes, whichever section's
    code does it: the invoices' prune takes the bank's payments, the recipes'
    prune releases till products - and the report then sends the owner to
    this archive to get them back (review, 19/09; the old rule kept only the
    sections imported with « Remplacer »). And every section imported with
    « Remplacer » that changes at all, creations included: importing this
    archive back with « Remplacer » is the undo, and only its prune removes
    what the run created - a bank link a person had undone, made again
    under a line whose record had not changed, stayed (review, 19/09). One
    exception: a section imported with « Fusionner » that only filled
    blanks or added - the database copy covers that, and the invoices'
    400 MB of files are not archived again for a missing preview filled in.
    Deleted rows in a merged section are another section's doing, and go
    in. A clear: every cleared section, touched or not, and whatever else it
    touches."""
    strategies = strategies or {}
    at_risk = set(cleared)
    for section in report.sections:
        strategy = strategies.get(section.key)
        replaced = strategy == Strategy.REPLACE and section.changes
        changed_what_was_there = section.destructive and (strategy != Strategy.MERGE or section.deletes)
        if replaced or changed_what_was_there:
            at_risk.add(section.key)
    return at_risk


def before(kind: Literal["import", "effacement"], affected: set[str]) -> dict[str, str]:
    """database backup always; section export of `affected` (§6.5) if any.
    Returns {"database": path, "archive": path or ""} for the report and the message."""
    label = LABELS[kind]
    stamp = _stamp()
    needed = MARGIN * (database_size() + (estimated_bytes(affected) if affected else 0))
    try:
        free = shutil.disk_usage(backup_dir()).free
    except OSError as exc:
        raise SafetyError(f"Sauvegarde impossible ({exc}) : rien n'a été changé.") from exc
    if free < needed:
        raise SafetyError("Sauvegarde impossible (pas assez de place sur le disque) : rien n'a été changé.")
    try:
        database = backup_database(label, stamp)
    except Exception as exc:  # said, and nothing runs
        raise SafetyError(f"Sauvegarde impossible ({exc}) : rien n'a été changé.") from exc
    archive = None
    if affected:
        try:
            archive = safety_export(set(affected), label, stamp, reason=REASONS[kind])
        except Exception as exc:
            raise SafetyError(f"Sauvegarde impossible ({exc}) : rien n'a été changé.") from exc
    return {"database": str(database), "archive": str(archive) if archive else ""}


@dataclass
class Backup:
    name: str
    path: Path
    size: int
    created: datetime
    kind: Literal["zip", "sqlite"]

    @property
    def megabytes(self) -> str:
        return f"{self.size / 1_000_000:.1f}".replace(".", ",")


def list_backups() -> list[Backup]:
    """newest first; only names matching the stamp pattern - whatever else
    sits in the folder is someone else's."""
    folder = backup_path()
    if not folder.is_dir():
        return []
    found = []
    for path in folder.iterdir():
        match = NAME_RE.match(path.name)
        if not match or not path.is_file():
            continue
        try:
            # The stamp is the local time it was taken at (_stamp).
            created = timezone.make_aware(datetime.strptime(match.group(1), "%Y-%m-%d_%H%M%S"))  # noqa: DTZ007
            size = path.stat().st_size
        except (ValueError, OSError):
            continue
        found.append(Backup(path.name, path, size, created, "zip" if match.group(3) == "zip" else "sqlite"))
    return sorted(found, key=lambda backup: (backup.created, backup.name), reverse=True)


def find_backup(name: str) -> Backup | None:
    """The zip backup exactly so named, or None: a name posted by a form is
    compared with the listing, never joined to a path as is."""
    return next((backup for backup in list_backups() if backup.kind == "zip" and backup.name == name), None)
