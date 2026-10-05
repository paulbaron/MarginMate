"""The server's Chrome, shared by every espace: how another bar may use it.

One machine runs every bar's browser sessions - L'Addition's sales import
for every espace since 04/10/2026 (Metro and the supplier portals are the
platform owner's alone, invoices/integrations.py). Two rules for an espace
that is not the platform owner's (accounts.tenancy.server_accounts_allowed):

- **headless, always** (`headless`): a visible window opens on the server's
  desktop - the owner's -, signed in to the bar's account, for as long as
  the run lasts. A bar's « Navigateur visible », or `--no-headless`, shows it
  nothing.
- **a free browser, or none** (`browser_slot`): HOSTED_SESSIONS sessions at
  once for every other espace together, PER_ESPACE for one espace, taken
  without waiting - a run finding none is refused at once
  (BROWSERS_BUSY): a thread waiting for a browser is a gather or an import
  standing still, and one bar's long import must not hold every browser of
  the others. The platform owner's own sessions are not counted. A run
  that would start one asks `slot_free` first - an automatic sales import's
  slot waits for a browser (« en attente : navigateurs du serveur
  occupés », recipes/auto_sales.py) rather than start an import refused at
  once; two runs started in the same tick can still both find one free, and
  the one refused gives its slot back (`auto_sales.gave_way`).

Process-wide by design (`serve` is one process): the counts are keyed by
`tenant_key()`, never by a row's pk.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from accounts.tenancy import server_accounts_allowed, tenant_key

#: Chrome sessions at once for every espace but the platform owner's.
HOSTED_SESSIONS = 2
#: Chrome sessions at once for one of them.
PER_ESPACE = 1
BROWSERS_BUSY = "Tous les navigateurs du serveur sont occupés : réessayez dans quelques minutes."


class BrowsersBusy(RuntimeError):
    """No browser free for this espace: BROWSERS_BUSY."""


_LOCK = threading.Lock()
#: tenant_key() -> sessions running.
_RUNNING: dict[str, int] = {}


def headless(requested: bool) -> bool:
    """Whether a Chrome started now runs headless: as `requested` (the
    settings' SCRAPER_HEADLESS, a command's option) in the platform owner's
    espace, always anywhere else."""
    return requested or not server_accounts_allowed()


def running() -> int:
    """The sessions of every other espace running now."""
    with _LOCK:
        return sum(_RUNNING.values())


def _full(key: str) -> bool:
    """Whether the espace `key` may start no session now (_LOCK held)."""
    return sum(_RUNNING.values()) >= HOSTED_SESSIONS or _RUNNING.get(key, 0) >= PER_ESPACE


def slot_free() -> bool:
    """Whether `browser_slot` would give the bound espace a browser now -
    nothing taken: a run asks it before it starts, `browser_slot` decides.
    Always in the platform owner's espace."""
    if server_accounts_allowed():
        return True
    key = tenant_key()
    with _LOCK:
        return not _full(key)


@contextmanager
def browser_slot(refused=BrowsersBusy):
    """Hold one of the server's browsers for the bound espace while the
    block runs - `refused(BROWSERS_BUSY)` at once when none is free for it.
    The platform owner's sessions are not counted."""
    if server_accounts_allowed():
        yield
        return
    key = tenant_key()
    with _LOCK:
        if _full(key):
            raise refused(BROWSERS_BUSY)
        _RUNNING[key] = _RUNNING.get(key, 0) + 1
    try:
        yield
    finally:
        with _LOCK:
            left = _RUNNING.get(key, 1) - 1
            if left > 0:
                _RUNNING[key] = left
            else:
                _RUNNING.pop(key, None)
