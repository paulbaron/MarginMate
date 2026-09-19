"""The archive, format version 1 (§4): a zip holding `manifest.json`
(written last), one `<key>.json` per section, and `files/` - the invoices'
PDFs and photos under their stored names.

Two things drive how it is written and read.

**Size.** A full export is about 420 MB of files, so nothing is ever held
whole: each file is copied into the zip, and out of it, 1 MiB at a time,
its sha256 computed in the same pass.

**Trust.** An archive comes from outside - a hand-edited file, another
computer. Every member name is checked before anything is read, and one
dangerous name refuses the whole archive (zip slip: `../../config/x.py`
would be written wherever it points). Only the members the manifest
declares are ever opened, never with `extractall`, and a member is checked
against the size and sha the manifest gives while it is copied.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import logging
import lzma
import os
import re
import time
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import IO, Self

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.utils import timezone

logger = logging.getLogger(__name__)

FORMAT = "marginmate-archive"
VERSION = 1
MANIFEST = "manifest.json"
FILES_PREFIX = "files/"
CHUNK = 1024 * 1024

# Limits (§4.5). Module constants so tests can patch them small.
MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_MEMBERS = 100_000
MAX_FILE_BYTES = 200 * 1024**2
MAX_JSON_BYTES = 512 * 1024**2
MAX_TOTAL_BYTES = 8 * 1024**3
MAX_RATIO = 200
RATIO_MIN_BYTES = 10 * 1024**2

#: Already compressed: deflating them again costs time for nothing.
STORED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".zip", ".xlsx"}

ZIP_MAGIC = b"PK\x03\x04"

NOT_ZIP_NOR_JSON = "Ce fichier n'est ni une archive MarginMate (.zip) ni un export d'associations (.json)."
NOT_ARCHIVE = "Ce fichier n'est pas une archive MarginMate."
NEWER = (
    "Archive faite par une version plus récente de MarginMate (format {version}) : mettez l'application à jour "
    "avant de l'importer."
)
TOO_MANY = "Archive refusée : trop de fichiers."
TOO_BIG_TOTAL = "Archive refusée : trop volumineuse une fois décompressée."
ODD_RATIO = "Archive refusée : un fichier est anormalement compressé."
DANGEROUS = "Archive refusée : nom de fichier dangereux (« {name} »)."
ALTERED = "Fichier altéré dans l'archive : {name}"
UNENCODABLE = "Archive refusée : {what} contient un caractère invalide."


class ArchiveError(Exception):
    """message is French and shown as is."""


def _megabytes(limit: int) -> str:
    return f"{limit // 1024**2} Mo"


def _shown(name) -> str:
    """A name as it can be printed: control characters made visible, cut."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(name))
    return text if len(text) <= 80 else text[:79] + "…"


# -- names ---------------------------------------------------------------------------

def safe_member_name(name: str) -> bool:
    """A member name that cannot point outside where it is written: not
    empty, at most 255 characters, no control character, no backslash, not
    absolute, no drive or colon, and no empty, "." or ".." segment."""
    if not isinstance(name, str) or not name or len(name) > 255:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    if "\\" in name or ":" in name or name.startswith("/"):
        return False
    return all(segment not in ("", ".", "..") for segment in name.split("/"))


def storage_name_problem(name) -> str | None:
    """Why an import may not write a file under this storage name, or None.
    The name comes from the archive's record, so it is checked like a member
    name, kept to the two folders documents live in, and must stay inside
    MEDIA_ROOT once joined (Django's safe_join)."""
    if not isinstance(name, str) or not safe_member_name(name):
        return f"nom de fichier refusé (« {_shown(name)} »)"
    if not name.startswith(("invoices/", "receipts/")):
        return f"fichier hors des dossiers des factures et des tickets (« {_shown(name)} »)"
    if len(name) > 100:
        return f"nom de fichier trop long (« {_shown(name)} »)"
    try:
        path = Path(default_storage.path(name)).resolve()
        root = Path(settings.MEDIA_ROOT).resolve()
    except (SuspiciousFileOperation, ValueError, NotImplementedError):
        return f"nom de fichier hors du dossier des fichiers (« {_shown(name)} »)"
    if root not in path.parents:
        return f"nom de fichier hors du dossier des fichiers (« {_shown(name)} »)"
    return None


# -- what the manifest says about the app ----------------------------------------------

@functools.lru_cache(maxsize=1)
def app_revision() -> str | None:
    """The commit the app runs, first 7 hex digits, read from .git without
    a subprocess; None on any error (no .git, a packed or odd ref)."""
    try:
        git = Path(settings.BASE_DIR) / ".git"
        if git.is_file():  # a worktree: ".git" names the real directory
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git = (git.parent / text[len("gitdir:"):].strip()).resolve()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        sha = head
        if head.startswith("ref:"):
            ref = head[len("ref:"):].strip()
            sha = ""
            for base in (git, common):
                if (base / ref).is_file():
                    sha = (base / ref).read_text(encoding="utf-8").strip()
                    break
            else:
                for base in (git, common):
                    packed = base / "packed-refs"
                    if packed.is_file():
                        for line in packed.read_text(encoding="utf-8").splitlines():
                            parts = line.split()
                            if len(parts) == 2 and parts[1] == ref:
                                sha = parts[0]
                                break
                    if sha:
                        break
        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha):
            return sha[:7]
    except Exception:  # information only, never worth failing an export
        logger.debug("app_revision unreadable", exc_info=True)
    return None


def applied_migrations() -> dict[str, str]:
    """The latest applied migration per app, for information only."""
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    try:
        applied = MigrationRecorder(connection).applied_migrations()
    except Exception:  # noqa: BLE001 - information only
        return {}
    latest: dict[str, str] = {}
    for app, name in applied:
        if app in ("invoices", "inventory", "recipes", "bank") and name > latest.get(app, ""):
            latest[app] = name
    return dict(sorted(latest.items()))


# -- writing -------------------------------------------------------------------------

def _zip_info(member: str, compress_type: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(member, date_time=time.localtime()[:6])
    info.compress_type = compress_type
    info.external_attr = 0o644 << 16
    return info


class SectionWriter:
    def __init__(self, archive: ArchiveWriter, key: str):
        self._archive = archive
        self.key = key
        self.counts: dict[str, int] | None = None

    @property
    def written(self) -> bool:
        return self.counts is not None

    def write(self, payload: dict, counts: dict[str, int]) -> None:
        """Exactly once. Streamed into the zip: json.dump writes as it
        encodes, so a 10 MB payload is never one 10 MB string."""
        if self.written:
            raise RuntimeError(f"section {self.key!r} written twice")
        if not isinstance(payload, dict):
            raise TypeError("a section's payload is a JSON object")
        self.counts = {str(label): int(number) for label, number in counts.items()}
        info = _zip_info(f"{self.key}.json", zipfile.ZIP_DEFLATED)
        with self._archive._zip.open(info, "w", force_zip64=True) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
            json.dump(payload, text, ensure_ascii=False, allow_nan=False)
            text.flush()
            text.detach()

    def add_file(self, field_file) -> dict | None:
        """The file ref (§4.4); None for an empty field. A file two records
        name is stored once."""
        return self._archive._add_file(field_file)


class ArchiveWriter:
    """Usable as a context manager; on exception the partial file is removed."""

    def __init__(self, path: Path, *, reason: str):
        self.path = Path(path)
        self.reason = reason
        self._zip = zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
        self._sections: dict[str, SectionWriter] = {}
        self._files: list[dict] = []
        self._refs: dict[str, dict] = {}
        self.notes: list[str] = []
        self.manifest: dict | None = None

    def section(self, key: str) -> SectionWriter:
        if key in self._sections:
            raise RuntimeError(f"section {key!r} opened twice")
        writer = SectionWriter(self, key)
        self._sections[key] = writer
        return writer

    def _add_file(self, field_file) -> dict | None:
        if isinstance(field_file, str):
            name, storage = field_file, default_storage
        else:
            name = getattr(field_file, "name", "") or ""
            storage = getattr(field_file, "storage", None) or default_storage
        if not name:
            return None
        if name in self._refs:
            return dict(self._refs[name])
        member = FILES_PREFIX + name
        if not safe_member_name(member):
            self.notes.append(f"nom de fichier refusé, fichier laissé de côté : {_shown(name)}")
            ref = {"name": name, "missing": True}
            self._refs[name] = ref
            return dict(ref)
        try:
            source = storage.open(name, "rb")
        except (OSError, SuspiciousFileOperation):
            self.notes.append(f"fichier absent du disque : {name}")
            ref = {"name": name, "missing": True}
            self._refs[name] = ref
            return dict(ref)
        compress_type = zipfile.ZIP_STORED if Path(name).suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED
        digest = hashlib.sha256()
        size = 0
        with source, self._zip.open(_zip_info(member, compress_type), "w", force_zip64=True) as target:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                digest.update(chunk)
                target.write(chunk)
                size += len(chunk)
        ref = {"member": member, "name": name, "size": size, "sha256": digest.hexdigest()}
        self._files.append({"member": member, "size": size, "sha256": ref["sha256"]})
        self._refs[name] = ref
        return dict(ref)

    def close(self) -> dict:
        """Writes manifest.json last and returns it: an archive cut short
        (a crash, a full disk) has no manifest and is refused whole."""
        from transfer.registry import INFO

        unwritten = [key for key, writer in self._sections.items() if not writer.written]
        if unwritten:
            raise RuntimeError(f"sections opened but never written: {unwritten}")
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created_at": timezone.localtime(timezone.now()).isoformat(timespec="seconds"),
            "app_revision": app_revision(),
            "migrations": applied_migrations(),
            "reason": self.reason,
            "sections": {
                key: {
                    "file": f"{key}.json",
                    "counts": writer.counts,
                    "requires": list(INFO[key].requires) if key in INFO else [],
                }
                for key, writer in self._sections.items()
            },
            "files": self._files,
        }
        if self.notes:
            manifest["notes"] = list(self.notes)
        self._zip.writestr(
            _zip_info(MANIFEST, zipfile.ZIP_DEFLATED),
            json.dumps(manifest, ensure_ascii=False, indent=1, allow_nan=False),
        )
        self._zip.close()
        self.manifest = manifest
        return manifest

    def abort(self) -> None:
        try:
            self._zip.close()
        except Exception:  # already failing; the file goes anyway
            logger.debug("closing an aborted archive failed", exc_info=True)
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.abort()
        elif self.manifest is None:
            self.close()


class DeleteOnClose(io.FileIO):
    """A temporary export served by FileResponse: removed when the response
    closes it (which is also when Windows lets it go)."""

    def close(self) -> None:
        name = self.name
        super().close()
        try:
            os.remove(name)
        except OSError:
            logger.warning("Export temporaire non supprimé : %s", name, exc_info=True)


# -- reading -------------------------------------------------------------------------

def _refuse_constant(value):
    raise ValueError(value)


#: A JSON escape of a UTF-16 surrogate. Half a pair ("\ud800") is valid
#: JSON, parsed into a str that no UTF-8 write takes - the stage's
#: state.json, an archive written from it, the page: each was a 500 (review,
#: 19/09). Only an escape can carry one: raw UTF-8 never decodes into it.
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")


def unencodable(text: str, data) -> bool:
    """Whether `data`, parsed from the JSON `text`, holds text that cannot be
    written back as UTF-8. Encoded again only when the text has a surrogate
    escape at all - an emoji written as its two escapes is one, and fine -
    so a 400 MB section is not copied for nothing."""
    if not _SURROGATE_ESCAPE.search(text):
        return False
    try:
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def shown_moment(value) -> datetime | None:
    """An ISO moment from a manifest as a page can show it, in local time;
    None when it is not one, or when converting it overflows. The manifest
    comes from outside: "0001-01-01T00:00:00+14:00" parses, then the page's
    |date filter (Paris time) raised OverflowError, and the Importer tab
    listing that stage was a 500 until the 24 h sweep (review, 19/09). Local
    once, it stays in range when the filter converts it again."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
        return timezone.localtime(moment) if timezone.is_aware(moment) else moment
    except (ValueError, OverflowError):
        return None


#: What zipfile raises for a member damaged on the way: a CRC that fails, a
#: local header that is not one, a deflate (or bzip2, lzma) stream that no
#: longer inflates, a member cut short, a compression or an encryption it
#: cannot read. Turned into a French refusal - it was a 500 (review, 19/09).
DAMAGED = (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError, NotImplementedError, RuntimeError)


def _read_json(zf: zipfile.ZipFile, member: str, what: str):
    """A JSON member, read in chunks under MAX_JSON_BYTES whatever its header
    claims, NaN and Infinity refused, and text no UTF-8 write takes."""
    chunks = []
    total = 0
    try:
        with zf.open(member) as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                total += len(chunk)
                if total > MAX_JSON_BYTES:
                    raise ArchiveError(f"Archive refusée : {what} est trop gros.")
                chunks.append(chunk)
    except DAMAGED as exc:
        raise ArchiveError(ALTERED.format(name=_shown(member))) from exc
    try:
        text = b"".join(chunks).decode("utf-8")
        data = json.loads(text, parse_constant=_refuse_constant)
    except ValueError as exc:  # JSONDecodeError and the NaN refusal alike
        if not isinstance(exc, json.JSONDecodeError) and str(exc) in ("NaN", "Infinity", "-Infinity"):
            raise ArchiveError(f"Archive refusée : {what} contient une valeur qui n'est pas un nombre ({exc}).") from None
        raise ArchiveError(f"Archive refusée : {what} est illisible.") from None
    if unencodable(text, data):
        raise ArchiveError(UNENCODABLE.format(what=what))
    return data


class _VerifiedStream(io.RawIOBase):
    """A member as it is read: fails once more bytes arrive than declared,
    and at the end when the sha256 or the size differ."""

    def __init__(self, raw, name: str, size: int, sha256: str):
        self._raw = raw
        self._name = name
        self._size = size
        self._sha256 = sha256
        self._digest = hashlib.sha256()
        self._count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        try:
            data = self._raw.read(len(buffer))
        except DAMAGED as exc:  # a CRC that fails, a truncated member
            raise ArchiveError(ALTERED.format(name=self._name)) from exc
        count = len(data)
        self._count += count
        if self._count > self._size:
            raise ArchiveError(ALTERED.format(name=self._name))
        if count:
            buffer[:count] = data
            self._digest.update(data)
        elif self._count != self._size or self._digest.hexdigest() != self._sha256:
            raise ArchiveError(ALTERED.format(name=self._name))
        return count

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


class SectionReader:
    def __init__(self, archive: ArchiveReader, key: str):
        self._archive = archive
        self.key = key
        entry = archive.manifest["sections"][key]
        counts = entry.get("counts")
        self.counts: dict[str, int] = (
            {str(label): number for label, number in counts.items() if isinstance(number, int)}
            if isinstance(counts, dict)
            else {}
        )
        self._payload = None

    def payload(self) -> dict:
        """Parsed once, size-guarded; a section file is a JSON object."""
        if self._payload is None:
            data = _read_json(self._archive._zip, f"{self.key}.json", f"{self.key}.json")
            if not isinstance(data, dict):
                raise ArchiveError(f"Archive refusée : {self.key}.json est illisible.")
            self._payload = data
        return self._payload

    def has_file(self, ref: dict | None) -> bool:
        return self._archive.has_file(ref)

    def open_file(self, ref: dict) -> IO[bytes]:
        return self._archive.open_file(ref)


class ArchiveReader:
    """validates §4.5 completely; raises ArchiveError."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.notes: list[str] = []
        self._readers: dict[str, SectionReader] = {}
        try:
            with open(self.path, "rb") as handle:
                magic = handle.read(len(ZIP_MAGIC))
        except OSError:
            raise ArchiveError(NOT_ARCHIVE) from None
        if magic != ZIP_MAGIC:
            raise ArchiveError(NOT_ZIP_NOR_JSON)
        try:
            self._zip = zipfile.ZipFile(self.path, "r", allowZip64=True)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError):
            raise ArchiveError(NOT_ARCHIVE) from None
        try:
            self._validate()
        except BaseException:
            self._zip.close()
            raise

    def _validate(self) -> None:
        infos = self._zip.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveError(TOO_MANY)
        seen: set[str] = set()
        for info in infos:
            # orig_filename: zipfile cuts a name at its first NUL and, on
            # Windows, turns backslashes into slashes - the raw name is what
            # was put there, and what is judged.
            name = info.orig_filename
            if not safe_member_name(name) or name in seen:
                raise ArchiveError(DANGEROUS.format(name=_shown(name)))
            seen.add(name)
        if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
            raise ArchiveError(TOO_BIG_TOTAL)
        for info in infos:
            # A zip bomb: a few KB that inflate to gigabytes.
            compressed = info.compress_type != zipfile.ZIP_STORED and info.file_size > RATIO_MIN_BYTES
            if compressed and (not info.compress_size or info.file_size / info.compress_size > MAX_RATIO):
                raise ArchiveError(ODD_RATIO)
        infos_by_name = {info.orig_filename: info for info in infos}

        if MANIFEST not in infos_by_name:
            raise ArchiveError(NOT_ARCHIVE)
        manifest = _read_json(self._zip, MANIFEST, MANIFEST)
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise ArchiveError(NOT_ARCHIVE)
        version = manifest.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ArchiveError(NOT_ARCHIVE)
        if version > VERSION:
            raise ArchiveError(NEWER.format(version=version))
        self.manifest = manifest

        from transfer.registry import INFO

        sections = manifest.get("sections", {})
        if not isinstance(sections, dict):
            raise ArchiveError(NOT_ARCHIVE)
        known = set()
        declared = {MANIFEST}
        for key, entry in sections.items():
            if not isinstance(entry, dict):
                raise ArchiveError(NOT_ARCHIVE)
            if key not in INFO:
                self.notes.append(f"partie inconnue ignorée : {_shown(key)}")
                continue
            member = f"{key}.json"
            if entry.get("file") != member or member not in infos_by_name:
                raise ArchiveError(f"Archive refusée : {member} manque.")
            if infos_by_name[member].file_size > MAX_JSON_BYTES:
                raise ArchiveError(f"Archive refusée : {member} est trop gros.")
            declared.add(member)
            known.add(key)
        self.sections = frozenset(known)

        files = manifest.get("files", [])
        if not isinstance(files, list):
            raise ArchiveError(NOT_ARCHIVE)
        self._files: dict[str, dict] = {}
        absent = []
        for entry in files:
            if not isinstance(entry, dict):
                raise ArchiveError(NOT_ARCHIVE)
            member, size, sha = entry.get("member"), entry.get("size"), entry.get("sha256")
            if (
                not isinstance(member, str)
                or not member.startswith(FILES_PREFIX)
                or not safe_member_name(member)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha)
            ):
                raise ArchiveError(NOT_ARCHIVE)
            if size > MAX_FILE_BYTES or (member in infos_by_name and infos_by_name[member].file_size > MAX_FILE_BYTES):
                raise ArchiveError(f"Archive refusée : un fichier dépasse {_megabytes(MAX_FILE_BYTES)}.")
            if member not in infos_by_name:
                absent.append(member)
                continue
            self._files[member] = {"member": member, "size": size, "sha256": sha}
            declared.add(member)
        if absent:
            self.notes.append(f"{len(absent)} fichier(s) annoncé(s) par l'archive mais absent(s) : ignoré(s).")
        undeclared = [name for name in infos_by_name if name not in declared]
        if undeclared:
            self.notes.append(f"{len(undeclared)} fichier(s) de l'archive qu'elle ne déclare pas : ignoré(s).")

    # -- what it says ------------------------------------------------------------
    @property
    def created_at(self) -> datetime | None:
        return shown_moment(self.manifest.get("created_at"))

    @property
    def app_revision(self) -> str | None:
        value = self.manifest.get("app_revision")
        return value if isinstance(value, str) else None

    @property
    def reason(self) -> str:
        value = self.manifest.get("reason")
        return value if isinstance(value, str) else ""

    def counts(self, key: str) -> dict[str, int]:
        return self.section(key).counts

    def section(self, key: str) -> SectionReader:
        if key not in self.sections:
            raise ArchiveError(f"Cette archive ne contient pas la partie « {key} ».")
        if key not in self._readers:
            self._readers[key] = SectionReader(self, key)
        return self._readers[key]

    # -- files ---------------------------------------------------------------------
    def has_file(self, ref: dict | None) -> bool:
        """A member the manifest declares, and the ref agrees with it on size
        and sha (the preview checks this much; the content is checked when
        it is copied)."""
        if not isinstance(ref, dict) or ref.get("missing"):
            return False
        declared = self._files.get(ref.get("member"))
        if declared is None:
            return False
        return ref.get("size", declared["size"]) == declared["size"] and ref.get("sha256", declared["sha256"]) == declared["sha256"]

    def open_file(self, ref: dict) -> IO[bytes]:
        """Declared members only; verifies the size as it goes, and the sha
        at the end."""
        if not self.has_file(ref):
            raise ArchiveError(f"Fichier absent de l'archive : {_shown((ref or {}).get('name', ''))}")
        declared = self._files[ref["member"]]
        name = ref.get("name") or declared["member"]
        try:
            # Opening reads the member's local header: damaged, it fails here.
            raw = self._zip.open(declared["member"])
        except DAMAGED as exc:
            raise ArchiveError(ALTERED.format(name=_shown(name))) from exc
        return io.BufferedReader(_VerifiedStream(raw, name, declared["size"], declared["sha256"]), buffer_size=CHUNK)

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
