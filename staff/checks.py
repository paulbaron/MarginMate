"""System checks of « Personnel »: `manage.py check`, and every runserver.

The private keys of EVERY espace's signing authority are encrypted with the
one platform passphrase, MARGINMATE_SIGNING_PASSPHRASE (staff/signing.py).
Unset, they are all written in clear. Only the owner's espace is told on its
pages (`signing.key_warning`) - another bar can do nothing about a server
setting and is never shown its name - so the operator is warned here,
always (there is no single mode left in which the pages said it to everyone).
"""

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security)
def signing_passphrase(app_configs=None, **kwargs):
    if (getattr(settings, "MARGINMATE_SIGNING_PASSPHRASE", "") or "").strip():
        return []
    return [
        Warning(
            "MARGINMATE_SIGNING_PASSPHRASE is not set: every espace's signing keys are written in clear.",
            hint="Set it before anybody signs; keys written in clear are encrypted at their espace's next signature.",
            id="staff.W001",
        )
    ]
