"""The logged-in file view: a tenant's stored files.

There is no public /media/ route (the single mode's, served when DEBUG was
on, was removed on 29/09/2026): every invoice's PDF and every receipt's
photo goes through here, behind the login
(LoginRequiredMiddleware - this view is NOT public) and from the BOUND
tenant's media folder only, so a name - guessed, or climbing out with
« ../ » - never reaches another bar's file.

The invoice's correction page shows its PDF in a frame of this site, hence
SAMEORIGIN rather than DENY. A PDF or a photo is shown inline; anything
else (an XML invoice, whatever an archive carried) is a download, sandboxed:
a file a user put there must not run script on this site's origin.
"""

import mimetypes
import os
from pathlib import Path

from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404
from django.utils._os import safe_join
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_safe

from . import paths

#: Shown in the page (the PDF frame, a receipt's photo); anything else is
#: downloaded. Not SVG: an image that can carry script.
INLINE_TYPES = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff"}
)


@require_safe
@xframe_options_sameorigin
def media(request, name):
    handle = open_stored(name)
    if handle is None:
        raise Http404
    return file_response(handle, Path(handle.name).name)


def media_folder() -> Path:
    """The bound tenant's media folder, resolved: what `open_stored` keeps a
    file inside."""
    return Path(paths.media_root()).resolve()


def open_stored(name: str, root: Path | None = None):
    """The bound tenant's stored file `name`, open for reading - or None when
    it is not there, is not a file, or lies outside the tenant's media folder
    (a name climbing out with « ../ », a link pointing out of it). Every door
    serving a stored file opens it here: this view, a document's own file
    (invoices.views.invoice_file) and Banque's zip (bank/invoice_files.py).

    `root` is `media_folder()` found once by a caller opening many files in
    the same request (the zip) - never anything a request typed."""
    if root is None:
        root = media_folder()
    try:
        full = Path(safe_join(os.fspath(root), name)).resolve()
        # safe_join works on the text; a link inside media pointing out of
        # it is caught once resolved.
        if root not in full.parents or not full.is_file():
            return None
        return open(full, "rb")
    except (SuspiciousFileOperation, ValueError, OSError):
        return None


def file_response(handle, filename: str, *, download: bool = False) -> FileResponse:
    """A stored file, under `filename`: shown in the page when it is a PDF or
    a photo and no download was asked, saved otherwise - and sandboxed, since
    a file a user put there must not run script on this site's origin.

    Shared with a document's own file route (invoices.views.invoice_file),
    which serves the same files under the name they are downloaded as, and
    with Banque's zip."""
    content_type, encoding = mimetypes.guess_type(filename)
    inline = not download and content_type in INLINE_TYPES and encoding is None
    response = FileResponse(handle, as_attachment=not inline, filename=filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    if not inline:
        response["Content-Security-Policy"] = "sandbox"
    return response
