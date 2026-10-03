"""The one way a sales import starts: the Ventes tab's « Récupérer les
ventes » (views.trigger_sales_import) and an automatic sales import's slot
(auto_sales.run_due, through sales_sources.start) both call
`start_sales_import`; `manage.py laddition_import`, which downloads in its
own process, takes the same lock through `claim_sales_import`.

One sales import at a time per espace, whoever starts it: the stale runs
are reaped, the active check made and the job created inside ONE
`transaction.atomic()` - SQLite's IMMEDIATE transactions (config/settings.py)
take the write lock as it opens, so a click and the scheduler arriving
together cannot both see « nothing running » and both start (the invoices'
`gathering.start_gather`, the same pattern). The thread starts once that
block has committed: every caller runs it outside a transaction of its own
(a view without ATOMIC_REQUESTS, the scheduler's thread), so the block's end
IS the commit and the thread's first read finds the job.
`transaction.on_commit` is not used: inside a test's transaction it would
never run, and the Ventes tab's tests watch the thread being started.

**A period decided under the lock** (`plan`): an automatic import does not
know its dates before it holds the lock - read earlier, its period could
predate an import that ended in between, and it would sign in again for
days just imported. So it passes `plan`, called inside the block after the
active check: either `(start, end, notes)`, the job to create, or a
sentence - nothing to import (« à jour », say): nothing created, the
sentence returned. The ending task commits its status and its coverage in
one transaction (auto_sales.finish), so the plan sees the import either
still running or over with its days covered.

The thread is `bound()` to this request's or this tick's espace
(accounts/tenancy.py). Tests patch `threading.Thread` (as
`recipes.importing.threading.Thread`, or `recipes.views.threading.Thread`:
one module) - a real import is never started from a test or a coding
session.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date

from django.db import transaction

from accounts.tenancy import bound

from .models import SalesImportJob

ACTIVE = (SalesImportJob.Status.PENDING, SalesImportJob.Status.RUNNING)

#: What a `plan` returns: the job's (start, end, notes), or a sentence
#: saying why nothing is imported.
Plan = Callable[[], "tuple[date, date, list[str]] | str"]


def active_import(*, reap: bool = True) -> SalesImportJob | None:
    """The sales import running now, if any, once the runs that died without
    saying so are marked failed - a single killed thread must not lock
    imports out for good."""
    if reap:
        SalesImportJob.reap_stale()
    return SalesImportJob.objects.filter(status__in=ACTIVE).order_by("-started_at", "-pk").first()


def claim_sales_import(
    start: date | None = None,
    end: date | None = None,
    *,
    trigger: str = SalesImportJob.Trigger.MANUAL,
    auto_rule_id: int | None = None,
    notes=(),
    plan: Plan | None = None,
) -> SalesImportJob | str | None:
    """The lock of a sales import (the module's docstring), nothing started:
    the job created for [start, end] - or for what `plan` says, asked once
    nothing else runs -, None when another sales import is active, or the
    plan's sentence when it found nothing to import (nothing created).
    `notes`: lines written at the top of the job's log."""
    with transaction.atomic():
        if active_import() is not None:
            return None
        if plan is not None:
            planned = plan()
            if isinstance(planned, str):
                return planned
            start, end, notes = planned
        job = SalesImportJob.objects.create(
            range_start=start, range_end=end, trigger=trigger, auto_rule_id=auto_rule_id
        )
        for note in notes:
            job.append_log(note)
    return job


def start_sales_import(
    start: date | None = None,
    end: date | None = None,
    *,
    trigger: str = SalesImportJob.Trigger.MANUAL,
    auto_rule_id: int | None = None,
    task=None,
    notes=(),
    plan: Plan | None = None,
) -> SalesImportJob | str | None:
    """Start an import of the sales of [start, end] - or of the period
    `plan` decides under the lock - in a bound thread and return its job;
    None when another sales import is active, the plan's sentence when it
    found nothing to import - nothing created either way. `task` is the
    source's import task (the till's by default), `notes` lines written at
    the top of the job's log before it starts."""
    if task is None:
        from .tasks import import_laddition_sales_task as task

    job = claim_sales_import(start, end, trigger=trigger, auto_rule_id=auto_rule_id, notes=notes, plan=plan)
    if job is None or isinstance(job, str):
        return job
    # bound(): the thread works for this request's or tick's tenant - its job
    # row, its sales, its download folder - and closes its connections when
    # it ends. A new thread starts bound to nothing, and job pk N is another
    # bar's too.
    threading.Thread(target=bound(task), args=(job.id, job.range_start, job.range_end), daemon=True).start()
    return job
