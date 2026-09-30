"""Where the timesheet signatures keep their files: the private folder,
`accounts.paths.private_dir()` - the bound espace's own `private/` (under
TENANTS_ROOT, read at call time, so a test's override_settings redirects
it), and NOTHING unbound (NoTenantBound).
One folder per espace is what gives each bar its own signing authority, its
own employer and employee keys (`keys/employees/<pk>`: pks restart at 1 in
every espace's database), its own signed files and its own deletions.log -
`signing.verify` trusts the authorities of THIS folder only.

    keys/                          the internal authority, the employer's and each
                                   employee's certificate and private key (staff.signing)
    signatures/<uuid>/document.pdf         the month frozen as it was sent
                     signature.png         the employee's drawn signature, re-encoded
                     signed_employee.pdf   signed by the employee
                     employer_signature.png  the employer's drawn signature, re-encoded
                                           (none for a request countersigned before 28/09)
                     signed_final.pdf      countersigned by the employer
                     preuve.pdf            the proof file (staff.proof)
    deletions.log                  one JSON line per version deleted - by the owner's
                                   « Supprimer… » or by staff_purge_signatures
                                   (staff.signature_deletion): the tombstone kept
                                   outside the database, never rewritten here

**Never inside a folder the site serves** (STATIC_ROOT, STATICFILES_DIRS):
a signed timesheet - or a private key - there would be downloadable by its
address. `private_dir()` refuses such a folder (and a served folder inside
it) rather than creating it. Media is no served folder: the old single
mode's public /media/ route is gone (29/09/2026), and an espace's media/ is
read only through the logged-in file view, which never reaches private/. The files are written whole or not at
all (a temporary file, then `os.replace`), and the database keeps each
one's SHA-256: `read_checked` is what notices a file changed on disk.

Nothing here knows what a request is beyond its uuid - no model, no query.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from accounts import paths

SIGNATURES = "signatures"
KEYS = "keys"

DOCUMENT = "document.pdf"
SIGNATURE_IMAGE = "signature.png"
EMPLOYEE_SIGNED = "signed_employee.pdf"
EMPLOYER_SIGNATURE_IMAGE = "employer_signature.png"
FINAL = "signed_final.pdf"
PROOF = "preuve.pdf"
#: The only names a request's folder holds.
FILE_NAMES = (DOCUMENT, SIGNATURE_IMAGE, EMPLOYEE_SIGNED, EMPLOYER_SIGNATURE_IMAGE, FINAL, PROOF)
#: Beside keys/ and signatures/: a line per deleted version.
DELETIONS_LOG = "deletions.log"


class AlteredFileError(Exception):
    """A stored file no longer has the SHA-256 its row recorded. French."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _served_folders() -> list[tuple[str, Path]]:
    """The folders the site serves to anyone: the static files'."""
    folders = []
    if getattr(settings, "STATIC_ROOT", None):
        folders.append(("STATIC_ROOT", Path(settings.STATIC_ROOT)))
    for folder in getattr(settings, "STATICFILES_DIRS", ()) or ():
        folders.append(("STATICFILES_DIRS", Path(folder if not isinstance(folder, tuple) else folder[1])))
    return folders


def private_dir() -> Path:
    """The private folder, created on demand - and refused, never created,
    when it is inside a folder the site serves (or holds one). The bound
    espace's `private/`, beside its `media/` and never in it
    (accounts/paths.py); unbound, NoTenantBound."""
    folder = Path(paths.private_dir()).resolve()
    for setting, served in _served_folders():
        served = served.resolve()
        if folder == served or folder.is_relative_to(served) or served.is_relative_to(folder):
            raise ImproperlyConfigured(
                f"Le dossier privé ({folder}) ne doit être ni dans {setting} ({served}) ni le contenir : "
                "ce dossier est servi par le site, et les clés et les fiches signées seraient téléchargeables."
            )
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def keys_dir() -> Path:
    folder = private_dir() / KEYS
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def keys_folder() -> Path:
    """Where the keys are, creating nothing - for what only reads them
    (`signing.key_warning`, `authority_certificates`, `authority_fingerprint`).
    The same folder as `keys_dir`: the bound espace's, NoTenantBound
    unbound."""
    return Path(paths.private_dir()) / KEYS


def _request_id(request_id) -> str:
    """The folder's name: the uuid in its canonical form, or ValueError -
    never a path somebody passed in."""
    if isinstance(request_id, uuid.UUID):
        return str(request_id)
    if not isinstance(request_id, str):
        raise ValueError(f"Identifiant de demande invalide : {request_id!r}")
    try:
        parsed = uuid.UUID(request_id)
    except ValueError:
        raise ValueError(f"Identifiant de demande invalide : {request_id!r}") from None
    if str(parsed) != request_id.lower():
        raise ValueError(f"Identifiant de demande invalide : {request_id!r}")
    return str(parsed)


def request_dir(request_id, *, create: bool = False) -> Path:
    folder = private_dir() / SIGNATURES / _request_id(request_id)
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def _path(request_id, name: str, *, create: bool = False) -> Path:
    if name not in FILE_NAMES:
        raise ValueError(f"Fichier inconnu : {name!r}")
    return request_dir(request_id, create=create) / name


def write_private(path: Path, data: bytes) -> None:
    """`data` into `path`, whole or not at all: a temporary file beside it,
    then `os.replace`. Readable by the owner only where the system has such
    a thing (POSIX; on Windows the folder's own permissions apply)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write(request_id, name: str, data: bytes) -> str:
    """Store one of a request's files; returns its SHA-256."""
    write_private(_path(request_id, name, create=True), data)
    return sha256(data)


def read(request_id, name: str) -> bytes:
    return _path(request_id, name).read_bytes()


def exists(request_id, name: str) -> bool:
    return _path(request_id, name).is_file()


def read_checked(request_id, name: str, expected_sha256: str) -> bytes:
    """The file, provided it is still the one whose hash was recorded."""
    data = read(request_id, name)
    if not expected_sha256 or sha256(data) != expected_sha256:
        raise AlteredFileError(
            f"Le fichier {name} de la demande {_request_id(request_id)} ne correspond plus à l'empreinte "
            "enregistrée : il a été modifié ou remplacé sur le disque."
        )
    return data


def request_files(request_id) -> list[str]:
    """The names of the files a request's folder holds - in `FILE_NAMES`'
    order, then any other name - leaving out a write's temporary file.
    [] when there is no folder."""
    folder = request_dir(request_id)
    if not folder.is_dir():
        return []
    names = {path.name for path in folder.iterdir() if path.is_file() and not path.name.startswith(".")}
    known = [name for name in FILE_NAMES if name in names]
    return known + sorted(names.difference(FILE_NAMES))


def delete_request_files(request_id) -> list[str]:
    """Remove a request's folder; returns the names of the files it held
    ([] when there was none)."""
    folder = request_dir(request_id)
    if not folder.is_dir():
        return []
    names = sorted(path.name for path in folder.iterdir() if path.is_file() and not path.name.startswith("."))
    shutil.rmtree(folder)
    return names


def append_deletion_record(record: dict) -> None:
    """One JSON line appended to `deletions.log` and flushed to the disk
    before returning - or OSError, and the caller deletes nothing. The line
    keeps accents as they are (UTF-8); a line separator inside a value
    (U+2028) is no newline, so `read_deletion_records` splits on « \\n »
    only."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(", ", ": ")) + "\n"
    path = private_dir() / DELETIONS_LOG
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def read_deletion_records() -> list[dict]:
    """Every line of `deletions.log` that reads as a JSON object, in order;
    a line that does not is skipped rather than hiding the others."""
    path = private_dir() / DELETIONS_LOG
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
