"""The scheduler: one daemon thread of `manage.py serve`, a tick a minute,
the jobs of every open espace.

**Started by `serve` only** (accounts/management/commands/serve.py, between
`create_server` and `server.run()`, stopped in its `finally`): never in an
`AppConfig.ready()` - serve's apps load before it knows it will serve
(`--verifier`, a second serve refused its port) - never under runserver,
never by a management command. A development copy therefore never ticks;
and were one to, it would neither send (webpush.sending_enabled) nor gather.

**A tick** (`tick(now)`) lists the open espaces and, bound to each, runs
every job of `JOBS` - dotted paths resolved at call time, so a job's module
missing (another package not there yet) is logged and the others run - each
inside its own `except Exception`: one espace's broken data, or one job's
bug, never stops the next espace nor the thread - and its connection waits
BUSY_TIMEOUT_MS for a write lock, not a minute, so one espace held by a
long import never holds the others' tick. Then, still bound, when
the espace has pending dispatches, its delivery thread starts
(sending.start_delivery: at most one alive per espace). **Nothing is sent
from this thread**: a push service taking seconds per device must not make
another bar's 00:00 reminder late. The thread's connections are closed
after every tick (no request cycle closes them).

**The loop** waits on its stop event until the next minute boundary plus
TICK_OFFSET_SECONDS, so `stop()` ends it at once.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connection, connections
from django.utils import timezone
from django.utils.module_loading import import_string

from accounts.models import Tenant
from accounts.tenancy import TenancyError, bound_tenant

from . import sending
from .models import Dispatch

logger = logging.getLogger(__name__)

#: Every tick's jobs, run for each open espace, in this order: each a
#: function taking the tick's `now` (UTC).
JOBS = ("notifications.reminders.run_due", "invoices.auto_gather.run_due", "recipes.auto_sales.run_due")
TICK_OFFSET_SECONDS = 2
#: The shortest wait between two ticks (a tick that ran past its minute).
MINIMUM_WAIT_SECONDS = 1.0
#: How long the tick's connection to an espace waits for its write lock
#: (`_wait_briefly_for_locks`).
BUSY_TIMEOUT_MS = 5000


class Handle:
    """What `start` returns: `stop(timeout)` ends the loop and waits for
    it."""

    def __init__(self, thread: threading.Thread, stopped: threading.Event):
        self.thread = thread
        self.stopped = stopped

    def stop(self, timeout: float | None = 5) -> None:
        self.stopped.set()
        self.thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()


def start() -> Handle:
    """Start the scheduler's thread (serve's alone: the module's
    docstring)."""
    stopped = threading.Event()
    thread = threading.Thread(target=run, args=(stopped,), name="marginmate-scheduler", daemon=True)
    thread.start()
    return Handle(thread, stopped)


def seconds_to_next_tick(now) -> float:
    """From `now` to the next minute boundary plus TICK_OFFSET_SECONDS."""
    following = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=TICK_OFFSET_SECONDS)
    return max((following - now).total_seconds(), MINIMUM_WAIT_SECONDS)


def run(stopped: threading.Event) -> None:
    """The loop: a tick, then wait for the next one or the stop."""
    while not stopped.is_set():
        try:
            tick(timezone.now())
        except Exception:  # the scheduler outlives any tick
            logger.exception("Planificateur : passage en échec")
        finally:
            connections.close_all()
        stopped.wait(seconds_to_next_tick(timezone.now()))


def tick(now) -> None:
    """One pass over every open espace (the module's docstring)."""
    try:
        tenants = list(Tenant.objects.filter(is_active=True).order_by("pk"))
    except DatabaseError:
        logger.exception("Planificateur : les espaces ne se lisent pas")
        return
    try:
        for tenant in tenants:
            try:
                with bound_tenant(tenant):
                    _wait_briefly_for_locks()
                    _run_jobs(now, tenant)
                    if Dispatch.objects.filter(status=Dispatch.Status.PENDING).exists():
                        sending.start_delivery()
            except (TenancyError, ImproperlyConfigured, DatabaseError):
                logger.warning("Planificateur : espace %s ignoré (base indisponible)", tenant.pk, exc_info=True)
    finally:
        connections.close_all()


def _wait_briefly_for_locks() -> None:
    """The bound espace's connection - new for this binding, closed with
    it - waits BUSY_TIMEOUT_MS for a write lock, not the minute every other
    connection waits (SQLITE_OPTIONS): one espace held by a long import
    must not hold the other espaces' tick. A write refused here is retried
    at the next tick (the reminders' window does not move without its
    heartbeat, a dedupe key stops a duplicate)."""
    with connection.cursor() as cursor:
        cursor.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")


def _run_jobs(now, tenant) -> None:
    for path in JOBS:
        try:
            job = import_string(path)
        except ImportError:
            logger.warning("Planificateur : tâche %s introuvable", path)
            continue
        try:
            job(now)
        except Exception:  # one job's failure is that job's, never the next one's
            logger.exception("Planificateur : %s en échec (espace %s)", path, tenant.pk)
