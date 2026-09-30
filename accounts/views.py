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
    root = Path(paths.media_root()).resolve()
    try:
        full = Path(safe_join(os.fspath(root), name)).resolve()
    except (SuspiciousFileOperation, ValueError, OSError):
        raise Http404
    # safe_join works on the text; a link inside media pointing out of it is
    # caught once resolved.
    if root not in full.parents or not full.is_file():
        raise Http404
    content_type, encoding = mimetypes.guess_type(full.name)
    inline = content_type in INLINE_TYPES and encoding is None
    response = FileResponse(open(full, "rb"), as_attachment=not inline, filename=full.name)  # noqa: SIM115 - the FileResponse closes it
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    if not inline:
        response["Content-Security-Policy"] = "sandbox"
    return response
