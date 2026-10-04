"""The one way a gather starts: « Récupérer » (views.trigger_gather, from Achats
or Consignes) and an automatic gather's slot (auto_gather.run_due) both call
`start_gather`.

One gather at a time per espace: the stale runs are reaped, the active check
made and the job created inside ONE `transaction.atomic()` - SQLite's
IMMEDIATE transactions (config/settings.py) take the write lock as it opens,
so a click and the scheduler arriving together cannot both see « nothing
running » and both start. The thread starts once that block has committed:
every caller runs it outside a transaction of its own (a view without
ATOMIC_REQUESTS, the scheduler's thread), so the block's end IS the commit,
and the thread's first read finds the job. `transaction.on_commit` is not
used: inside a test's transaction it would never run, and the existing
gather tests watch the thread being started.

The thread is `bound()` to this request's or this tick's espace
(accounts/tenancy.py): a new thread starts with nothing bound. Tests patch
`invoices.gathering.threading.Thread` - a real gather is never started from
a test or a coding session.
"""

from __future__ import annotations

import threading
from datetime import date

from django.db import transaction

from accounts.tenancy import bound

from .models import ScrapeJob

ACTIVE = (ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING)


def active_gather(*, reap: bool = True) -> ScrapeJob | None:
    """The gather running now, if any, once the runs that died without
    saying so are marked failed (common.JobLogMixin.reap_stale) - a single
    killed thread must not lock gathers out for good."""
    if reap:
        ScrapeJob.reap_stale()
    return (
        ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER, status__in=ACTIVE).order_by("-started_at", "-pk").first()
    )


def start_gather(
    source_codes,
    start: date | None,
    end: date | None,
    *,
    metro_now: bool,
    trigger: str,
    auto_gather_id: int | None = None,
    unattended: bool = False,
) -> ScrapeJob | None:
    """Start a gather of `source_codes` over [start, end] (None: each source's
    own start, today) in a bound thread, and return its job - or None when
    another gather is active, nothing created. `unattended` (an automatic
    gather): headless browsers, and each source from where its coverage
    stops (invoices/coverage.py, tasks.gather_invoices_task)."""
    from .tasks import gather_invoices_task

    with transaction.atomic():
        if active_gather() is not None:
            return None
        job = ScrapeJob.objects.create(range_start=start, range_end=end, trigger=trigger, auto_gather_id=auto_gather_id)
    extra = {"kwargs": {"unattended": True}} if unattended else {}
    thread = threading.Thread(
        target=bound(gather_invoices_task),
        args=(job.id, start, end, set(source_codes), metro_now),
        daemon=True,
        **extra,
    )
    thread.start()
    return job
