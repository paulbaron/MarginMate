"""The espace's own credentials: the logins and passwords typed on the
« Identifiants » page (accounts/credentials.py), encrypted in two files of
the espace's private folder.

    <tenant dir>/private/credentials.bin   the values, encrypted
    <tenant dir>/private/credentials.key   a random key, sealed by Windows (DPAPI)

**Not in the database**: the database is exported (« Données »), copied and
shown on screen (models.WebsiteInvoiceSource keeps only the NAMES of a
portal's variables for that reason). The private folder is never served and
never exported - and **these two files are in no backup** and no development
copy (`accounts.data_backup.LEFT_OUT_FILES`, refresh_dev_data.cmd): a backup
holds the .env, whose SECRET_KEY is half of the key; restored elsewhere, the
passwords are typed again.

**The key has two halves** (security review of 01/10/2026). The Fernet key
(AES-128-CBC and an HMAC) is HKDF-SHA256 over the SECRET_KEY and a random
32-byte key of the espace's own, with the espace's folder name as context:

- the random key is kept in `credentials.key` sealed by **Windows DPAPI**
  (`CryptProtectData`, the current Windows account, the folder name as its
  entropy): copied to another PC or opened by another Windows account, it
  does not open, and neither does the store. Without DPAPI (not Windows) the
  file holds it as it is, and the protection is the SECRET_KEY's alone;
- the SECRET_KEY: a development copy - its own key - cannot open a store
  copied from production even on the same Windows account, and a key
  anybody can read (the development fallback) refuses every save.

**A store of named values**, keyed by the same names as the .env - METRO_EMAIL,
INVOICE_EMAIL_APP_PASSWORD, a portal's FREEBOX_PASSWORD - so every connector
asks one question (`setting`, `value`): the value typed on the page, else -
in the owner's tenant only (`server_setting`) - the .env's. A portal's names still come from its source, never from the
page, and never one of the application's own (`models.app_env_name`, checked
by the page for every portal field): typed into a site, Metro's password
would reach it.

**A portal's value is bound to the site it was typed for** (`bindings`: name
-> host): the page records the host of the source's login page with the
value, and `scrapers.website.credentials` hands it to no other host - a
source pointed at another site, or another source naming the same variable,
gets nothing until the password is typed again for it. A value still read
from the .env carries no binding of its own: it goes to a portal only once
the owner has confirmed that site on the page (`env_bindings`). A value
typed as a password is remembered as one (`secret_names`): never drawn in
clear, never typed as a login. While the store exists but cannot be read,
no portal is given anything - not the .env's either.

Read at every call, never cached: a password changed on the page counts at
the next gather without a restart. Written whole, under a lock, through a
temporary file renamed over the old one. A file that does not open is said
(`VaultState.unreadable`) and never silently written over: only the page,
having said so, starts again (`save(start_over=True)`).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

from . import paths
from .tenancy import require_tenant, server_accounts_allowed

FILE_NAME = "credentials.bin"
KEY_FILE_NAME = "credentials.key"
#: Every file of the store, temporary ones excepted: what a backup leaves out.
FILE_NAMES = (FILE_NAME, KEY_FILE_NAME)
TEMPORARY_PREFIX = ".credentials-"
#: The values' layout, inside the encryption.
VERSION = 2
_SALT = b"marginmate.accounts.vault.v2"
#: The key file's first bytes: sealed by DPAPI, or kept as it is.
_SEALED = b"MMV-DPAPI1\n"
_PLAIN = b"MMV-PLAIN1\n"
RANDOM_KEY_BYTES = 32

#: What any value is allowed to be: a login, a password, a host. Longer is
#: a paste gone wrong, and the file is read at every gather.
MAX_VALUE_LENGTH = 500

#: A file being replaced by another thread cannot be opened on Windows for a
#: moment (no FILE_SHARE_DELETE): tried again, never taken for unreadable.
_BUSY_TRIES = 8
_BUSY_PAUSE_SECONDS = 0.025

#: One writer at a time in this process (`serve` is ONE process).
_LOCK = threading.Lock()

UNREADABLE = "unreadable"
BUSY = "busy"

WEAK_KEY = (
    "La clé secrète du serveur (DJANGO_SECRET_KEY) est absente ou publique : aucun identifiant ne peut être "
    "enregistré avant qu'elle soit réglée."
)
#: WEAK_KEY in an espace that is not the platform owner's: it names no
#: server setting, and says who can act on it.
WEAK_KEY_HOSTED = (
    "La clé secrète du serveur n'est pas réglée : aucun identifiant ne peut être enregistré — prévenez "
    "l'administrateur de MarginMate."
)
BUSY_MESSAGE = "Les identifiants sont momentanément inaccessibles : réessayez dans un instant."
UNREADABLE_MESSAGE = (
    "Les identifiants enregistrés ne peuvent pas être lus sur ce serveur : rien n'a été enregistré, pour ne pas "
    "les effacer. Confirmez qu'il faut tout ressaisir."
)


class VaultError(Exception):
    """A save refused: a name no store may hold, a weak server key, a store
    that could not be read. The message is French and names no value."""


@dataclass
class VaultState:
    # repr=False: a state logged, or shown in a traceback's locals, must not
    # print the passwords.
    values: dict[str, str] = field(default_factory=dict, repr=False)
    bindings: dict[str, str] = field(default_factory=dict)
    #: The host a name's value read from the .env may be sent to: confirmed
    #: by the owner on the page (a .env value carries no binding of its own).
    env_bindings: dict[str, str] = field(default_factory=dict)
    #: The names whose value was typed as a PASSWORD: never drawn in clear,
    #: never typed as a login, whatever a source calls them later.
    secret_names: frozenset[str] = frozenset()
    #: "" when the store opened (or there is none), UNREADABLE when it does
    #: not open with this server's keys, BUSY when the file could not be read.
    problem: str = ""

    @property
    def unreadable(self) -> bool:
        return bool(self.problem)


# ------------------------------------------------------------------ DPAPI


def os_protection_available() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, entropy: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current Windows account,
    no prompt (CRYPTPROTECT_UI_FORBIDDEN). Raises OSError when Windows
    refuses (another account, another PC, a damaged blob)."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(payload: bytes):
        buffer = ctypes.create_string_buffer(payload, len(payload))
        return Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, _keep_source = blob(data)
    salt, _keep_salt = blob(entropy)
    out = Blob()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ui_forbidden = 0x1
    if not call(ctypes.byref(source), None, ctypes.byref(salt), None, None, ui_forbidden, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error() or "DPAPI refused")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _seal(random_key: bytes, tenant) -> bytes:
    if os_protection_available():
        return _SEALED + _dpapi(random_key, tenant.dir_name.encode("ascii"), protect=True)
    return _PLAIN + random_key


def _unseal(data: bytes, tenant) -> bytes:
    if data.startswith(_SEALED):
        if not os_protection_available():
            raise ValueError("sealed by Windows")
        return _dpapi(data[len(_SEALED) :], tenant.dir_name.encode("ascii"), protect=False)
    if data.startswith(_PLAIN):
        return data[len(_PLAIN) :]
    raise ValueError("not a key file")


# ------------------------------------------------------------------- keys


def _fernet(random_key: bytes, tenant) -> Fernet:
    secret = (getattr(settings, "SECRET_KEY", "") or "").encode("utf-8")
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=_SALT, info=b"espace:" + tenant.dir_name.encode("ascii")
    ).derive(random_key + b"\x00" + secret)
    return Fernet(base64.urlsafe_b64encode(derived))


def _path():
    return paths.private_dir() / FILE_NAME


def _key_path():
    return paths.private_dir() / KEY_FILE_NAME


def _read(path) -> bytes | None:
    """The file's bytes, None when there is none. Tried again while another
    thread replaces it; still refused, PermissionError."""
    for attempt in range(_BUSY_TRIES):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == _BUSY_TRIES - 1:
                raise
            time.sleep(_BUSY_PAUSE_SECONDS)
    return None


def _open(tenant) -> tuple[VaultState, bytes | None]:
    """The state and the random key that opened it (None: no store yet, or
    it did not open)."""
    try:
        token = _read(_path())
        sealed = _read(_key_path())
    except OSError:
        return VaultState(problem=BUSY), None
    if token is None:
        return VaultState(), None
    if sealed is None:
        return VaultState(problem=UNREADABLE), None
    try:
        random_key = _unseal(sealed, tenant)
        payload = json.loads(_fernet(random_key, tenant).decrypt(token).decode("utf-8"))
    except (InvalidToken, ValueError, OSError, UnicodeDecodeError):
        return VaultState(problem=UNREADABLE), None
    if not isinstance(payload, dict):
        return VaultState(problem=UNREADABLE), None
    values = payload.get("values", {})
    bindings = payload.get("bindings", {})
    env_bindings = payload.get("env_bindings", {})
    secret_names = payload.get("secrets", [])
    if not all(isinstance(part, dict) for part in (values, bindings, env_bindings)) or not isinstance(
        secret_names, list
    ):
        return VaultState(problem=UNREADABLE), None
    clean = {str(k): v for k, v in values.items() if isinstance(v, str) and v}
    hosts = {str(k): v for k, v in bindings.items() if isinstance(v, str) and v and str(k) in clean}
    env_hosts = {str(k): v for k, v in env_bindings.items() if isinstance(v, str) and v}
    secret = frozenset(str(name) for name in secret_names if str(name) in clean)
    state = VaultState(values=clean, bindings=hosts, env_bindings=env_hosts, secret_names=secret)
    return state, random_key


def load() -> VaultState:
    """The bound espace's stored values. No file: empty. A file that does not
    open: empty and `unreadable` - never an exception, the connectors then
    read the .env as before."""
    state, _ = _open(require_tenant())
    return state


def value(name: str, fallback: str = "") -> str:
    """The value typed on the page for this name, else `fallback`."""
    return load().values.get(name) or fallback


def bound_host(name: str) -> str:
    """The host a stored value was typed for ("" when none is recorded)."""
    return load().bindings.get(name, "")


def server_setting(name: str) -> str:
    """config/settings.py's value for `name` (read from the server's .env),
    in the owner's tenant only (`tenancy.server_accounts_allowed`); "" in
    every other espace and unbound: another bar never signs in with the
    server's accounts."""
    if not server_accounts_allowed():
        return ""
    return getattr(settings, name, "") or ""


def settings_of(*names: str) -> tuple[str, ...]:
    """Several credentials from ONE reading of the store (Metro's login and
    password): read one at a time, a save between the two sent a new login
    with an old password - a refused sign-in Metro's firewall counts. Each is
    the page's value, else - in the owner's tenant only - the .env's
    (`server_setting`). While the store cannot be read at all, nothing: not
    the .env's either."""
    state = load()
    if state.problem == BUSY:
        raise VaultError(BUSY_MESSAGE)
    return tuple(state.values.get(name) or server_setting(name) for name in names)


def setting(name: str) -> str:
    """A credential the application reads for itself (METRO_EMAIL,
    INVOICE_EMAIL_ADDRESS, LADDITION_PASSWORD…): the page's value, else - in
    the owner's tenant only - the setting config/settings.py read from the
    .env (`server_setting`)."""
    return value(name, server_setting(name))


def ready(*names: str, state: VaultState | None = None) -> bool:
    """Whether every one of `names` has a value a connector would sign in
    with: typed on the page, or - in the owner's tenant only - in the .env.
    One reading of the store (`state`, when the caller already holds one: a
    page asking for several accounts reads the store once). A store that
    cannot be read counts as not ready."""
    state = load() if state is None else state
    return all(state.values.get(name) or server_setting(name) for name in names)


def allowed_name(name: str) -> bool:
    from invoices.models import ENV_NAME_RE

    return isinstance(name, str) and bool(ENV_NAME_RE.match(name)) and len(name) <= 64


def _write(path, data: bytes) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=TEMPORARY_PREFIX, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(_BUSY_TRIES):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == _BUSY_TRIES - 1:
                    raise
                time.sleep(_BUSY_PAUSE_SECONDS)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def save(
    changes: dict[str, str | None],
    bindings: dict[str, str] | None = None,
    start_over: bool = False,
    env_bindings: dict[str, str] | None = None,
    secret_names=(),
) -> None:
    """Merge `changes` into the store: a value replaces, None or "" removes
    (its binding with it). `bindings` records the host a value was typed for;
    `env_bindings` the host a name's .env value may go to ("" forgets it);
    `secret_names` the names of `changes` typed as passwords.

    A store that does not open is never written over unless `start_over`:
    what it holds would be lost with nothing said - the page asks first.
    Refused while the server's key is one anybody can read."""
    from config.security import secret_key_problem

    for name, new in changes.items():
        if not allowed_name(name):
            raise VaultError(f"Nom refusé : {name!r}")
        if new and len(new) > MAX_VALUE_LENGTH:
            raise VaultError(f"Valeur trop longue pour {name}")
    for name, host in {**(bindings or {}), **(env_bindings or {})}.items():
        if not allowed_name(name) or not isinstance(host, str) or len(host) > 253:
            raise VaultError(f"Site refusé pour {name!r}")
    if secret_key_problem(getattr(settings, "SECRET_KEY", "")):
        raise VaultError(WEAK_KEY if server_accounts_allowed() else WEAK_KEY_HOSTED)
    tenant = require_tenant()
    with _LOCK:
        state, random_key = _open(tenant)
        if state.problem == BUSY:
            raise VaultError(BUSY_MESSAGE)
        if state.problem == UNREADABLE and not start_over:
            raise VaultError(UNREADABLE_MESSAGE)
        values = dict(state.values)
        hosts = dict(state.bindings)
        env_hosts = dict(state.env_bindings)
        secret = set(state.secret_names)
        for name, new in changes.items():
            # A value typed again takes the role it is typed in now.
            secret.discard(name)
            if new:
                values[name] = new
            else:
                values.pop(name, None)
                hosts.pop(name, None)
        secret |= {name for name in secret_names if changes.get(name)}
        for name, host in (bindings or {}).items():
            if name in values:
                if host:
                    hosts[name] = host
                else:
                    hosts.pop(name, None)
        for name, host in (env_bindings or {}).items():
            if host:
                env_hosts[name] = host
            else:
                env_hosts.pop(name, None)
        if not values and not env_hosts:
            for path in (_path(), _key_path()):
                path.unlink(missing_ok=True)
            return
        if random_key is None:
            # No store yet, or one started again: a new key of its own.
            random_key = secrets.token_bytes(RANDOM_KEY_BYTES)
            _write(_key_path(), _seal(random_key, tenant))
        payload = {
            "version": VERSION,
            "values": values,
            "bindings": hosts,
            "env_bindings": env_hosts,
            "secrets": sorted(secret & set(values)),
        }
        _write(_path(), _fernet(random_key, tenant).encrypt(json.dumps(payload).encode("utf-8")))
