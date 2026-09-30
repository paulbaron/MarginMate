"""Where a tenant keeps its files.

Always the bound tenant's own subfolder, created on demand; unbound, it
raises (accounts.tenancy.NoTenantBound) - a folder shared by every bar is
how one bar would list, import or delete another's files. TENANTS_ROOT is
read AT CALL TIME, so a test's override_settings redirects every folder.

    TENANTS_ROOT/
        _template/db.sqlite3        the migrated, empty database a new tenant starts from
        <dir_name>/
            db.sqlite3              the tenant's data
            media/                  invoices' PDFs, receipts' photos (served by accounts.views.media)
            private/                signing keys, signed PDFs, deletions.log (never served)
            downloads/              what the scrapers and the till export download
            backups/                « Données »'s safety copies
            staging/                « Données »'s archives between upload and import, exports
            imports/                uploads waiting for their import (the Tickets' folder scan)

Business code asks here for every folder. The single-mode settings that
named them (MEDIA_ROOT, STAFF_PRIVATE_DIR, SCRAPE_DOWNLOAD_DIR,
DATA_BACKUP_DIR, DATA_STAGING_DIR) are gone from config/settings.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .models import DIR_NAME_PATTERN
from .tenancy import require_tenant

DATABASE_FILE = "db.sqlite3"
TEMPLATE_DIR = "_template"

MEDIA = "media"
PRIVATE = "private"
DOWNLOADS = "downloads"
BACKUPS = "backups"
STAGING = "staging"
IMPORTS = "imports"
#: Every folder a new tenant is created with.
FOLDERS = (MEDIA, PRIVATE, DOWNLOADS, BACKUPS, STAGING, IMPORTS)


def tenants_root() -> Path:
    root = getattr(settings, "TENANTS_ROOT", None)
    if not root or not str(root).strip():
        raise ImproperlyConfigured("TENANTS_ROOT is not set: every espace is kept under it.")
    return Path(root)


def tenant_dir(tenant) -> Path:
    """The tenant's folder. Its name is checked again here: it becomes a
    path, and a name like « ../x » must never reach the filesystem."""
    name = getattr(tenant, "dir_name", "") or ""
    if not re.match(DIR_NAME_PATTERN, name):
        raise ImproperlyConfigured(f"Espace {getattr(tenant, 'pk', None)} has an invalid folder name.")
    return tenants_root() / name


def tenant_database(tenant) -> Path:
    return tenant_dir(tenant) / DATABASE_FILE


def template_database() -> Path:
    return tenants_root() / TEMPLATE_DIR / DATABASE_FILE


def _folder(kind: str) -> Path:
    folder = tenant_dir(require_tenant()) / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def media_root() -> Path:
    """Stored files (the FileFields' storage, accounts/storage.py)."""
    return _folder(MEDIA)


def private_dir() -> Path:
    """The timesheet signatures' keys and files, never served."""
    return _folder(PRIVATE)


def downloads_dir() -> Path:
    """What the scrapers and the till export download."""
    return _folder(DOWNLOADS)


def backups_dir() -> Path:
    """« Données »'s safety copies."""
    return _folder(BACKUPS)


def staging_dir() -> Path:
    """« Données »'s archives waiting between their upload and their import,
    and exports being downloaded."""
    return _folder(STAGING)


def imports_dir() -> Path:
    """Uploads waiting for their import (the Tickets' folder scan,
    ``receipt_batches/<pk>/``): the tenant's imports/, outside its media."""
    return _folder(IMPORTS)
