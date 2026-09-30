"""Which espace this thread works for - THE one implementation.

One database per espace (« multi mode », the only one: the old « single »
mode - one database, no login - was removed on 29/09/2026, because a server
started without its switch served every page to anyone). Nothing is bound
by default, and the unbound `default` is an EMPTY in-memory database
(config/settings.py), so a business query that runs unbound fails loudly
(« no such table ») instead of landing in somebody's file.

How a binding works. Django hands the SAME settings dict to
every thread's DatabaseWrapper, so changing NAME in it would move every
thread at once - one bar's request would read another bar's file. The
ConnectionHandler itself is thread-local, though. So `bound_tenant` keeps
this thread's current wrapper, installs a NEW
``DatabaseWrapper({**connections.settings["default"], "NAME": <file>})`` as
this thread's `default` (the copy carries the configured defaults: OPTIONS
with SQLITE_OPTIONS, TIME_ZONE, CONN_MAX_AGE, AUTOCOMMIT, ATOMIC_REQUESTS,
TEST), and in `finally` closes it and puts the previous one back. No
business code names a database (no `using=`, no raw SQL), so everything -
`transaction.atomic`, `on_commit`, `django.db.connection` - follows the
thread's `default`, as long as the binding never changes while a
transaction is open: that is refused.

A new thread starts with nothing bound (a fresh thread-local, an empty
contextvars context): every `threading.Thread(target=...)` of the project
passes ``target=bound(real_target)``, which carries the starting thread's
binding over and closes the thread's connections at the end.
"""

from __future__ import annotations

import functools
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.utils import load_backend
from django.utils.crypto import salted_hmac


class TenancyError(RuntimeError):
    """A binding that must not happen (another espace already bound, an
    open transaction, a missing database file)."""


class NoTenantBound(TenancyError):
    """This thread works for no espace: the business code that asked has no
    data to read and no folder to write into."""


_current: ContextVar = ContextVar("marginmate_current_tenant", default=None)


def current_tenant():
    """The espace this thread works for: a Tenant, or None when unbound."""
    return _current.get()


def require_tenant():
    """`current_tenant()`, or NoTenantBound when nothing is bound."""
    tenant = current_tenant()
    if tenant is None:
        raise NoTenantBound(
            "No espace is bound to this thread: bind one with accounts.tenancy.bound_tenant, "
            "or start the thread with target=bound(...)."
        )
    return tenant


def tenant_key() -> str:
    """A short stable string that tells espaces apart, for the
    process-global caches, sets and locks keyed by primary keys (pks restart
    at 1 in every espace's database): the tenant's id. Raises NoTenantBound
    when unbound."""
    return str(require_tenant().pk)


def storage_scope(tenant) -> str:
    """What every key the pages' scripts write into the browser's storage
    carries for `tenant` (base.html's ``<body data-tenant>``): 16 hex
    characters of an HMAC of its id keyed by the SECRET_KEY - the same on
    every page of the espace, another for every other espace.

    Not the id itself: a sequential id on every page told any bar how many
    espaces were opened before its own (security audit LB-6). Not dir_name
    either: that is the name of a folder on the server. Changing the
    SECRET_KEY changes every scope - a count typed and never saved is then
    no longer offered back (it only ever lived in that browser), and the
    espace's preferences start again; a logout forgets its drafts anyway
    (static/js/ui.js, accounts.pages.LogoutPage)."""
    digest = salted_hmac("marginmate.accounts.tenancy.storage_scope", str(tenant.pk), algorithm="sha256")
    return digest.hexdigest()[:16]


def integrations_allowed() -> bool:
    """Whether the server's own accounts (Metro, the invoice mailbox,
    L'Addition, the LLM parser, the portals' .env credentials) may be used
    for this thread's espace: only the owner's espace
    (`Tenant.uses_server_integrations`), never unbound."""
    tenant = current_tenant()
    return bool(tenant is not None and tenant.uses_server_integrations)


def _same(one, other) -> bool:
    return one is other or (getattr(one, "pk", None) is not None and one.pk == getattr(other, "pk", None))


def _wrapper_for(path) -> object:
    """A new DatabaseWrapper for `default`, on `path`, from a COPY of the
    configured settings (never the shared dict itself)."""
    base = connections.settings[DEFAULT_DB_ALIAS]
    settings_dict = {
        **base,
        "NAME": str(path),
        "OPTIONS": dict(base.get("OPTIONS") or {}),
        "TEST": dict(base.get("TEST") or {}),
    }
    backend = load_backend(settings_dict["ENGINE"])
    return backend.DatabaseWrapper(settings_dict, DEFAULT_DB_ALIAS)


@contextmanager
def _bound_database(path):
    """Make `path` this thread's `default` for the block (the swap itself,
    shared by `bound_tenant` and the template database's migration).
    Refused inside an open transaction on `default`."""
    previous = connections[DEFAULT_DB_ALIAS]
    if previous.in_atomic_block:
        raise TenancyError(
            "Cannot bind an espace inside an open transaction on 'default': its queries would "
            "leave the transaction they belong to."
        )
    wrapper = _wrapper_for(path)
    connections[DEFAULT_DB_ALIAS] = wrapper
    try:
        yield wrapper
    finally:
        try:
            wrapper.close()
        finally:
            connections[DEFAULT_DB_ALIAS] = previous


@contextmanager
def bound_tenant(tenant):
    """Run the block for `tenant`.

    This thread's `default` is the espace's own database for the block (see
    the module's docstring), and `current_tenant()` is `tenant`. Refused
    while a transaction is open on `default`. Nestable for the SAME espace
    (a no-op); binding another espace while one is bound raises. Always
    restored on the way out, exception or not.
    """
    if tenant is None:
        raise TenancyError("A binding needs a real espace (a Tenant), not None.")
    current = current_tenant()
    if current is not None:
        if _same(current, tenant):
            yield current
            return
        raise TenancyError(
            f"This thread already works for espace {current.pk}; binding espace {tenant.pk} inside it is refused."
        )

    from . import paths

    database = paths.tenant_database(tenant)
    if not Path(database).is_file():
        # SQLite would create an empty file and answer « no such table »
        # later, somewhere else.
        raise TenancyError(f"The database of espace {tenant.pk} ({tenant.dir_name}) does not exist.")
    if Path(database).stat().st_size == 0:
        # What SQLite takes for a NEW database - a restore or a copy that
        # died, a full disk: the same « no such table », in a view. Never an
        # espace's (provisioning copies the template, never 0 bytes).
        raise TenancyError(f"The database of espace {tenant.pk} ({tenant.dir_name}) is an empty file.")
    with _bound_database(database):
        token = _current.set(tenant)
        try:
            yield tenant
        finally:
            _current.reset(token)


def bound(fn):
    """Capture the CURRENT binding and return a callable that runs `fn`
    bound to it, for ``threading.Thread(target=bound(fn), args=...)``.

    Run in another thread, it binds the captured espace, runs `fn`, and
    closes that thread's connections at the end. Run in the thread that made
    it (a test calling a patched Thread's target), it nests into the binding
    already there and closes nothing.

    It refuses to capture nothing: a thread started unbound would die
    silently on its first query, far from the request that started it -
    here the request itself fails.
    """
    tenant = require_tenant()
    origin = threading.get_ident()

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            with bound_tenant(tenant):
                return fn(*args, **kwargs)
        finally:
            if threading.get_ident() != origin:
                connections.close_all()

    run.tenant = tenant
    return run
