"""Creating a tenant, and migrating the tenants' databases.

A new tenant is a COPY of the template database
(``TENANTS_ROOT/_template/db.sqlite3``, migrated and empty but for what the
migrations seed), made with SQLite's backup API, then migrated bound to the
tenant (a no-op when the template is current). Its data migrations run with
the tenant bound as `default` - none of them names a database, so a
``migrate --database=<alias>`` would have seeded somebody else's file.

The seed migrations wire the OWNER's integrations into every database: an
active mailbox source (UBA) and Metro marked to fetch. A new tenant that is
not the owner's has both switched off right after creation - and the
integrations gate (accounts.tenancy.integrations_allowed) refuses them
anyway.

The files come FIRST and the row last (`prepare_tenant`, then
`create_tenant` or the signup's own transaction): copying and migrating take
seconds whenever the template is behind the code, and the accounts
database's transactions are IMMEDIATE - SQLite's write lock from their first
statement - so made inside one, every login and session write of every bar
waited for them. `manage.py migrate_tenants` migrates the template at every
deploy, which is what keeps a new tenant's own migrate a no-op, and a signup
quick. A failure removes the folder made here: nothing half-made is left
for a retry to trip on.
"""

from __future__ import annotations

import secrets
import shutil
import sqlite3
import string
import threading
import uuid
from pathlib import Path

from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS

from . import paths
from .models import Tenant
from .tenancy import TenancyError, _bound_database, bound_tenant

_DIR_ALPHABET = string.ascii_lowercase + string.digits
# One template build at a time in this process; across processes, each
# builds into a file of its own and the last rename wins (both are valid).
_TEMPLATE_LOCK = threading.Lock()


def new_dir_name() -> str:
    """A random folder name no tenant has: says nothing about the bar."""
    for _ in range(20):
        name = "".join(secrets.choice(_DIR_ALPHABET) for _ in range(12))
        if not Tenant.objects.filter(dir_name=name).exists() and not (paths.tenants_root() / name).exists():
            return name
    raise TenancyError("Could not draw a free folder name for a new espace.")


def copy_database(source, target) -> None:
    """Copy an SQLite database with the backup API: consistent even while
    another connection writes to the source, which is only read. (Not
    opened ``mode=ro``: a database in WAL mode cannot be read that way
    unless its -shm file already exists.)"""
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise TenancyError(f"No database to copy at {source.name}.")
    reader = sqlite3.connect(source)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def _migrate_bound(verbosity=0, stdout=None) -> None:
    """`migrate` on whatever `default` is bound to. Checks are skipped: the
    caller's process ran them (and the test settings' in-memory default is
    not the ':memory:' accounts.E004 wants)."""
    options = {"interactive": False, "verbosity": verbosity, "skip_checks": True}
    if stdout is not None:
        options["stdout"] = stdout
    call_command("migrate", database=DEFAULT_DB_ALIAS, **options)


def migrate_template(verbosity=0, stdout=None) -> Path:
    """Create the template database when it is missing, migrate it when it
    is there; returns its path."""
    path = paths.template_database()
    with _TEMPLATE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with _bound_database(path):
                _migrate_bound(verbosity, stdout)
            return path
        building = path.with_name(f"building-{uuid.uuid4().hex}.sqlite3")
        try:
            with _bound_database(building):
                _migrate_bound(verbosity, stdout)
            building.replace(path)
        finally:
            for leftover in (building, *building.parent.glob(f"{building.name}-*")):
                leftover.unlink(missing_ok=True)
    return path


def ensure_template() -> Path:
    """The template database, built on first use."""
    path = paths.template_database()
    if path.exists():
        return path
    return migrate_template()


def migrate_tenant(tenant, verbosity=0, stdout=None) -> None:
    with bound_tenant(tenant):
        _migrate_bound(verbosity, stdout)


def switch_off_server_integrations() -> None:
    """What the seed migrations pre-wired to the owner's accounts, off in
    the bound tenant: its mailbox sources and Metro's own fetcher."""
    from invoices.models import InvoiceType, Supplier

    InvoiceType.objects.filter(source_kind=InvoiceType.SourceKind.EMAIL).update(is_active=False)
    Supplier.objects.filter(code="METRO").update(is_scrapable=False)


def remove_tenant_files(tenant) -> None:
    shutil.rmtree(paths.tenant_dir(tenant), ignore_errors=True)


def prepare_tenant(name: str, *, uses_server_integrations: bool = False, dir_name: str | None = None) -> Tenant:
    """A new tenant's FILES, ready to bind: its folder (database copied from
    the template and migrated, empty subfolders) and - unless it is the
    owner's - the server's integrations switched off in it. Returns the
    Tenant UNSAVED: no row of the accounts database is written, so the
    caller writes it where it wants (`create_tenant`; the signup, in its
    short transaction) - and removes the files (`remove_tenant_files`) if
    that fails. On failure, the folder made here is removed and the error
    raised."""
    name = (name or "").strip()
    if not name:
        raise ValueError("An espace needs a name.")
    template = ensure_template()
    tenant = Tenant(
        name=name,
        dir_name=dir_name or new_dir_name(),
        uses_server_integrations=uses_server_integrations,
    )
    folder = paths.tenant_dir(tenant)
    made_folder = False
    try:
        folder.mkdir(parents=True, exist_ok=False)
        made_folder = True
        copy_database(template, paths.tenant_database(tenant))
        for sub in paths.FOLDERS:
            (folder / sub).mkdir()
        # Bound by its folder: the row is not written yet, and nothing here
        # needs it (a binding reads `dir_name`).
        with bound_tenant(tenant):
            _migrate_bound()
            if not uses_server_integrations:
                switch_off_server_integrations()
    except BaseException:
        # Only a folder made here: an existing one (a dir_name passed in
        # that was taken) is somebody's tenant.
        if made_folder:
            remove_tenant_files(tenant)
        raise
    return tenant


def create_tenant(name: str, *, uses_server_integrations: bool = False, dir_name: str | None = None) -> Tenant:
    """A new tenant, ready to bind: its files (`prepare_tenant`), then its
    Tenant row. Multi mode only. On failure, nothing is left behind (and the
    error is raised)."""
    tenant = prepare_tenant(name, uses_server_integrations=uses_server_integrations, dir_name=dir_name)
    try:
        tenant.save(force_insert=True)
    except BaseException:
        remove_tenant_files(tenant)
        raise
    return tenant
