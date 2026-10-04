"""/static/ on the server: the addresses `{% asset %}` prints kept a year.

WhiteNoise marks immutable only hashed names, and ours are plain
(config/settings.py, STATIC_ROOT): every file went out with « max-age=60 »,
so a page opened a minute after the last one asked again for its stylesheet
and topbar.js, both in the head, before drawing anything - a round trip
through the tunnel on a phone. Yet `{% asset %}`'s `?v=` already names the
bytes: under `serve` (WhiteNoise indexing STATIC_ROOT once, autorefresh off)
it is the date of the copy collectstatic made at the start, read once here,
and each start makes new copies, so new addresses.

Only that exact `v` is kept a year: a guessed one would leave today's bytes
at Cloudflare under an address a later version prints. The admin's plain
addresses keep WhiteNoise's minute; runserver and the tests (autorefresh on,
the source dated at every call) are left as they were.
"""

import os

from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver
from whitenoise.middleware import WhiteNoiseMiddleware

IMMUTABLE = "max-age=31536000, public, immutable"

#: The date of each file's copy in STATIC_ROOT, read once: code, the same
#: for every espace, and changed only by the collectstatic of a new start.
_COLLECTED: dict[str, int] = {}


@receiver(setting_changed)
def _collected_changed(*, setting, **kwargs):
    if setting in {"STATIC_ROOT", "WHITENOISE_AUTOREFRESH"}:
        _COLLECTED.clear()


def collected_version(path: str) -> int | None:
    """The date of `path`'s copy in STATIC_ROOT - what the server serves -
    when WhiteNoise serves that copy (autorefresh off); None otherwise, or
    with no copy there."""
    if settings.WHITENOISE_AUTOREFRESH or not settings.STATIC_ROOT:
        return None
    version = _COLLECTED.get(path)
    if version is None:
        try:
            version = int(os.path.getmtime(os.path.join(settings.STATIC_ROOT, path)))
        except (OSError, ValueError):
            return None
        _COLLECTED[path] = version
    return version


class VersionedWhiteNoiseMiddleware(WhiteNoiseMiddleware):
    """WhiteNoise's, a file asked for under the `?v=` `{% asset %}` prints
    for it kept a year (its 304 too)."""

    def serve(self, static_file, request):
        response = super().serve(static_file, request)
        if self.autorefresh or response.status_code not in (200, 304):
            return response
        given = request.GET.get("v")
        if given and request.path_info.startswith(self.static_prefix):
            version = collected_version(request.path_info[len(self.static_prefix) :])
            if version is not None and given == str(version):
                response["Cache-Control"] = IMMUTABLE
        return response
