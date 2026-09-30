"""The default file storage (settings.STORAGES["default"]): the bound
espace's media folder, resolved at every call.

Django's own FileSystemStorage caches its location on first use - one
folder per process, whichever espace touched it first. Here `location` is
`accounts.paths.media_root()` every time: the bound espace's media/, and
NoTenantBound when nothing is bound. Stored names stay relative
(« invoices/2026/09/x.pdf »): the espace is the folder, never a prefix in
the name - « Données »'s archives only accept names under invoices/ and
receipts/, and the owner's existing rows carry none.

URLs: the logged-in file view (`accounts:media`), which serves from the
bound espace's media only. There is no public /media/ route. A storage
given an explicit `location` keeps Django's own URL (none is made today).
"""

import os

from django.core.files.storage import FileSystemStorage
from django.urls import reverse
from django.utils.deconstruct import deconstructible

from . import paths


@deconstructible(path="accounts.storage.TenantFileSystemStorage")
class TenantFileSystemStorage(FileSystemStorage):
    @property
    def base_location(self):
        if self._location is not None:
            return self._location
        return os.fspath(paths.media_root())

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        if self._location is None:
            return reverse("accounts:media", args=[name.replace("\\", "/").lstrip("/")])
        return super().url(name)
