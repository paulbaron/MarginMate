"""An archive waiting between its upload and its import (§5.11).

Staged under `settings.DATA_STAGING_DIR/<token>/` - **not in media/**:
`config/urls.py` serves all of MEDIA_ROOT when DEBUG is on, so a staged
archive of the owner's invoices would have been downloadable by its URL.
Temporary exports live beside, in `exports/`.

A token is `secrets.token_urlsafe(16)`, checked against its exact shape
before it is joined to any path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from transfer import archive, safety
from transfer.archive import ArchiveError, ArchiveReader

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
STATE = "state.json"
UPLOADED = "archive.zip"
#: An export being downloaded is removed when the download ends; one left by
#: a crash goes after this long.
EXPORT_MAX_AGE = timedelta(hours=1)

TOO_BIG = "Archive trop grosse ({size} Go, 4 Go au plus)."
NO_SPACE = "Pas assez de place sur le disque pour préparer l'import."
LEGACY_UNAVAILABLE = "Les anciens fichiers d'associations ne peuvent pas encore être importés ici."
LEGACY_UNENCODABLE = "Export d'associations refusé : il contient un caractère invalide."


def staging_dir() -> Path:
    path = Path(settings.DATA_STAGING_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    path = staging_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Stage:
    token: str
    dir: Path
    archive_path: Path
    created_at: datetime
    source: str                      # "envoi" | "sauvegarde <name>"
    manifest: dict
    state: dict = field(default_factory=dict)  # {"sections": {...key: strategy}, "preview": RunReport json, "preview_at": iso}
    notes: list[str] = field(default_factory=list)
    legacy: bool = False
    backup: str = ""

    @property
    def sections(self) -> frozenset[str]:
        from transfer.registry import INFO

        sections = self.manifest.get("sections", {})
        return frozenset(key for key in sections if key in INFO)

    def open(self) -> ArchiveReader:
        return ArchiveReader(self.archive_path)

    @property
    def preview_at(self) -> datetime | None:
        value = self.state.get("preview_at")
        try:
            return datetime.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None


def _new_dir() -> tuple[str, Path]:
    while True:
        token = secrets.token_urlsafe(16)
        path = staging_dir() / token
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return token, path


def _gigabytes(size: int) -> str:
    return f"{size / 1024**3:.1f}".replace(".", ",")


def _write_state(stage: Stage) -> None:
    data = {
        "token": stage.token,
        "created_at": stage.created_at.isoformat(),
        "source": stage.source,
        "backup": stage.backup,
        "legacy": stage.legacy,
        "manifest": stage.manifest,
        "notes": stage.notes,
        "state": stage.state,
    }
    target = stage.dir / STATE
    temporary = stage.dir / (STATE + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def _staged(token: str, path: Path, archive_path: Path, *, source: str, legacy=False, backup="") -> Stage:
    """Open the archive with its full validation; an ArchiveError removes
    the stage and propagates."""
    try:
        with ArchiveReader(archive_path) as reader:
            stage = Stage(
                token=token,
                dir=path,
                archive_path=archive_path,
                created_at=timezone.now(),
                source=source,
                manifest=reader.manifest,
                state={"sections": {}, "preview": None, "preview_at": None},
                notes=list(reader.notes),
                legacy=legacy,
                backup=backup,
            )
        _write_state(stage)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return stage


def _is_legacy(payload) -> bool:
    """The old « Exporter les associations » file: version 1, a list of
    products, and no `format` key (§4.6)."""
    return (
        isinstance(payload, dict)
        and "format" not in payload
        and payload.get("version") == 1
        and not isinstance(payload.get("version"), bool)
        and isinstance(payload.get("products"), list)
    )


def stage_upload(upload) -> Stage:
    """streams upload.chunks() to <token>/archive.zip, or converts a legacy JSON to one (§4.6);
    opens it with ArchiveReader (full validation); an ArchiveError deletes the stage dir and propagates"""
    size = getattr(upload, "size", 0) or 0
    if size > archive.MAX_ARCHIVE_BYTES:
        raise ArchiveError(TOO_BIG.format(size=_gigabytes(size)))
    if shutil.disk_usage(staging_dir()).free < 1.2 * size:
        raise ArchiveError(NO_SPACE)
    token, path = _new_dir()
    try:
        raw = path / "envoi.bin"
        first = b""
        with open(raw, "wb") as target:
            for chunk in upload.chunks():
                if len(first) < 64:
                    first += chunk[: 64 - len(first)]
                target.write(chunk)
        head = first.lstrip(b"\xef\xbb\xbf").lstrip()
        if first.startswith(archive.ZIP_MAGIC):
            archive_path = path / UPLOADED
            os.replace(raw, archive_path)
            return _staged(token, path, archive_path, source="envoi")
        if head.startswith(b"{"):
            return _stage_legacy(token, path, raw)
        raise ArchiveError(archive.NOT_ZIP_NOR_JSON)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise


def _stage_legacy(token: str, path: Path, raw: Path) -> Stage:
    if raw.stat().st_size > archive.MAX_JSON_BYTES:
        raise ArchiveError(archive.NOT_ZIP_NOR_JSON)
    try:
        text = raw.read_bytes().decode("utf-8-sig")
        payload = json.loads(text, parse_constant=archive._refuse_constant)
    except ValueError:
        raise ArchiveError(archive.NOT_ZIP_NOR_JSON) from None
    if not _is_legacy(payload):
        raise ArchiveError(archive.NOT_ZIP_NOR_JSON)
    # Half a UTF-16 pair ("\ud800") is valid JSON; the archive it is turned
    # into is written in UTF-8, and that write was a 500 (review, 19/09).
    if archive.unencodable(text, payload):
        raise ArchiveError(LEGACY_UNENCODABLE)
    try:
        from transfer import legacy
    except ImportError:
        raise ArchiveError(LEGACY_UNAVAILABLE) from None
    archive_path = path / UPLOADED
    legacy.to_archive(payload, archive_path)
    raw.unlink(missing_ok=True)
    return _staged(token, path, archive_path, source="envoi", legacy=True)


def stage_backup(name: str) -> Stage:
    """state.json points at backups/<name>, archive not copied; discard() never deletes a backup"""
    backup = safety.find_backup(name)
    if backup is None:
        raise ArchiveError("Cette sauvegarde n'existe pas (ou plus).")
    token, path = _new_dir()
    return _staged(token, path, backup.path, source=f"sauvegarde {backup.name}", backup=backup.name)


def get(token: str) -> Stage | None:
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    path = staging_dir() / token
    try:
        data = json.loads((path / STATE).read_text(encoding="utf-8"))
        backup = data.get("backup") or ""
        if backup:
            found = safety.find_backup(backup)
            if found is None:
                return None
            archive_path = found.path
        else:
            archive_path = path / UPLOADED
        if not archive_path.is_file():
            return None
        return Stage(
            token=token,
            dir=path,
            archive_path=archive_path,
            created_at=datetime.fromisoformat(data["created_at"]),
            source=data.get("source", ""),
            manifest=data.get("manifest") or {},
            state=data.get("state") or {},
            notes=list(data.get("notes") or []),
            legacy=bool(data.get("legacy")),
            backup=backup,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def pending() -> list[Stage]:
    """What waits, newest first, for the Importer tab: an archive sent - up
    to 422 MB - and left for a moment could otherwise be found again only
    through the browser's history, until sweep() removes it (review, 19/09).
    A folder get() does not read back as a stage is not one."""
    found = []
    for path in staging_dir().iterdir():
        if not path.is_dir() or not TOKEN_RE.match(path.name):
            continue
        stage = get(path.name)
        if stage is not None:
            found.append(stage)
    # timestamp(): a hand-edited state.json with a naive moment must not
    # make the comparison fail.
    return sorted(found, key=lambda stage: stage.created_at.timestamp(), reverse=True)


def save(stage: Stage) -> None:
    """state.json"""
    _write_state(stage)


def discard(stage: Stage) -> None:
    """Removes the stage's own folder - which holds an uploaded archive, never
    a backup (a backup is staged by reference)."""
    root = staging_dir().resolve()
    target = stage.dir.resolve()
    if target.parent != root or not TOKEN_RE.match(target.name):
        raise ValueError(f"not a stage folder: {target}")
    shutil.rmtree(target, ignore_errors=True)


def sweep(max_age=timedelta(hours=24)) -> int:
    """on every GET of the import tab; also removes stale exports/ temp files"""
    removed = 0
    root = staging_dir()
    cutoff = time.time() - max_age.total_seconds()
    for path in root.iterdir():
        if not path.is_dir() or not TOKEN_RE.match(path.name):
            continue
        state = path / STATE
        try:
            stamp = datetime.fromisoformat(json.loads(state.read_text(encoding="utf-8"))["created_at"]).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
        if stamp < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    export_cutoff = time.time() - min(max_age, EXPORT_MAX_AGE).total_seconds()
    for path in exports_dir().iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < export_cutoff:
                path.unlink()
                removed += 1
        except OSError:  # still being downloaded (Windows keeps it locked)
            continue
    return removed
