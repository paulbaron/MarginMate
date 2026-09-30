"""Signup codes: made by the platform's owner (`manage.py create_invitation`),
shown once, kept only as a hash, used once, and expiring - after
`DEFAULT_DAYS` unless told otherwise (0: never). A code travels in a message;
one never used would otherwise open a signup for ever, to whoever reads that
message one day.

A code is 24 characters drawn at random from an alphabet with nothing to
misread (no 0/O, no 1/I): 120 bits, printed in groups of four
(« K7QM-2XWD-… ») because a person copies it from a message. Whatever is
typed back is read the same way whatever its case, its dashes or its spaces
(`normalize`), and it is that form that is hashed, both times.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta

from django.utils import timezone

from .models import Invitation

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 24
GROUP = 4
#: `--days` at most (ten years): a larger number is not a date any more.
MAX_DAYS = 3650
#: How long an invitation is valid when nobody says: a month.
DEFAULT_DAYS = 30


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    return "-".join(raw[start : start + GROUP] for start in range(0, LENGTH, GROUP))


def normalize(code) -> str:
    """The code as it is hashed: letters and digits only, in capitals."""
    return re.sub(r"[^0-9A-Za-z]", "", str(code or "")).upper()


def code_hash(code) -> str:
    return Invitation.hash_code(normalize(code))


def create_invitation(*, note: str = "", days: int | None = DEFAULT_DAYS, now=None) -> tuple[Invitation, str]:
    """A new invitation and its code - the only time the code exists in
    clear. `days`: valid that long, `DEFAULT_DAYS` unless told; 0 (or None)
    never expires."""
    now = now or timezone.now()
    if days is not None and not 0 <= days <= MAX_DAYS:
        raise ValueError(f"Une invitation vaut de 1 à {MAX_DAYS} jours, ou 0 : sans expiration.")
    code = new_code()
    invitation = Invitation.objects.create(
        code_hash=code_hash(code),
        note=(note or "").strip(),
        created_at=now,
        expires_at=now + timedelta(days=days) if days else None,
    )
    return invitation, code


def usable_invitation(code, now=None) -> Invitation | None:
    """The invitation this code opens - unused and not expired - or None,
    whichever of unknown, used or expired it is: the page says the same
    thing for the three."""
    if not normalize(code):
        return None
    invitation = Invitation.objects.filter(code_hash=code_hash(code)).first()
    if invitation is None or not invitation.is_usable(now):
        return None
    return invitation
