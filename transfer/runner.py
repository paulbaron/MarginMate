"""The harness around the sections (§5.5): export, import, clear.

**The preview is the real run, rolled back.** `run_import(preview=True)`
runs exactly the code the confirm runs, inside a transaction that is then
undone - so what the preview announces is what the confirm does, and a
section cannot have one path for showing and another for doing. The only
difference is `ctx.save_file`, which writes no file in a preview; file
deletions are `on_commit`, and Django drops those with the rollback.

**And the confirm is held to its preview** (`expected`): the same code on
the same archive gives the same report only if the database did not move in
between, and a preview can be half an hour old. Just before the transaction
would commit, the confirm's report is compared with the preview's; any
difference undoes it (NotAsPreviewed) and the page shows the new preview.

Everything is parsed (`load`) before anything is written, so a structural
problem refuses the import whole. Then every section applies in dependency
order, prunes in reverse (a replaced stock take must release its invoice
lines before the invoices prune can remove them), and the derived data is
rebuilt once, at the end - all in one transaction.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from django.db import transaction

from transfer import rebuild, registry
from transfer.archive import ArchiveReader, ArchiveWriter
from transfer.keys import InvoiceIndex, SupplierResolver
from transfer.report import RunReport, SectionReport
from transfer.sections.base import ClearContext, Dirty, ImportContext, Strategy

BUSY = "Une récupération, un import de tickets ou de ventes est en cours : attendez qu'il se termine."
NOTHING_REREAD = "Aucun document n'a été relu ; rien n'a été appris."


class Busy(Exception):
    """A gather or an import job runs: the message is BUSY."""


class _Rollback(Exception):
    """Raised at the end of a preview to undo it."""


class NotAsPreviewed(Exception):
    """The confirm would not do what its preview announced: the database
    moved in between (a ticket imported, a payment linked - the preview may
    be half an hour old). Rolled back whole, the files it stored removed;
    `report` is what the run would do now, to be shown as the new preview."""

    def __init__(self, report: RunReport):
        super().__init__("the run no longer matches its preview")
        self.report = report


def busy_reason() -> str:
    """"" when free. An import or a clear holds SQLite's write lock (the
    IMMEDIATE mode) for tens of seconds - a job's heartbeat would time out -
    and an export taken while a gather writes is not consistent across
    sections. Stale jobs are reaped first, as every job page does, so one
    killed by a restart does not block this page for ever."""
    from invoices.models import ReceiptBatch, ScrapeJob
    from recipes.models import SalesImportJob

    for model in (ScrapeJob, ReceiptBatch, SalesImportJob):
        model.reap_stale()
        if model.objects.filter(status__in=[model.Status.PENDING, model.Status.RUNNING]).exists():
            return BUSY
    return ""


def _check_free() -> None:
    reason = busy_reason()
    if reason:
        raise Busy(reason)


def run_export(keys: Iterable[str], dest: Path, *, reason: str = "export", closed: bool = True) -> dict:
    """closed=True (the page): keys must equal registry.closure(keys, "export"), else ValueError.
    closed=False (safety exports): exactly these keys. Returns the manifest."""
    keys = set(keys)
    if not keys:
        raise ValueError("nothing to export")
    if closed and keys != registry.closure(keys, "export"):
        raise ValueError(f"export selection not closed: missing {sorted(registry.missing(keys, 'export'))}")
    sections = [registry.get(key) for key in registry.ordered(keys)]
    with ArchiveWriter(Path(dest), reason=reason) as writer:
        for section in sections:
            section.export(writer.section(section.key))
        return writer.close()


def _reports(keys: Iterable[str]) -> dict[str, SectionReport]:
    return {key: SectionReport.for_key(key) for key in registry.ordered(keys)}


def _run_sections(keys: Iterable[str], reports: dict[str, SectionReport]) -> list[SectionReport]:
    """The run's own sections in order, then any other a section wrote into
    (the bank's payments deleted with their invoices), in order too."""
    keys = set(keys)
    others = [key for key in registry.ordered(set(reports) - keys) if reports[key].changes or reports[key].said]
    return [reports[key] for key in registry.ordered(keys)] + [reports[key] for key in others]


def _held_to(report: RunReport, expected: RunReport | None, started: float) -> None:
    """The last step inside the transaction of a confirm: it is the run its
    preview showed, or it is undone. The safety archive was chosen from that
    preview, before the transaction: equal outcomes are what make it cover
    what this run deletes - a document that arrived since the preview, pruned
    under « 0 à supprimer », would otherwise exist nowhere (review, 19/09)."""
    if expected is None or report.same_outcome(expected):
        return
    report.preview = True  # nothing it did stays: it is a preview now
    report.duration_s = round(time.monotonic() - started, 2)
    raise NotAsPreviewed(report)


def run_import(
    reader: ArchiveReader,
    strategies: dict[str, Strategy],
    *,
    preview: bool,
    expected: RunReport | None = None,
) -> RunReport:
    """`expected`: the preview a confirm is held to (NotAsPreviewed)."""
    started = time.monotonic()
    _check_free()
    strategies = {key: Strategy(value) for key, value in strategies.items()}
    keys = set(strategies)
    if not keys:
        raise ValueError("nothing to import")
    absent = keys - set(reader.sections)
    if absent:
        raise ValueError(f"not in the archive: {sorted(absent)}")
    unclosed = registry.missing(keys, "import", available=reader.sections)
    if unclosed:
        raise ValueError(f"import selection not closed: missing {sorted(unclosed)}")

    sections = [registry.get(key) for key in registry.ordered(keys)]
    suppliers = SupplierResolver()
    for section in sections:
        source = reader.section(section.key)
        section.load(source)
        suppliers.remember(source.payload().get("supplier_names"))

    reports = _reports(keys)
    ctx = ImportContext(
        preview=preview,
        strategies=strategies,
        archive_sections=frozenset(reader.sections),
        reader=reader,
        dirty=Dirty(),
        reports=reports,
        suppliers=suppliers,
        invoices=InvoiceIndex(suppliers),
    )
    try:
        with transaction.atomic():
            for section in sections:
                ctx.refresh()
                section.apply(ctx, ctx.report(section.key))
            for section in reversed(sections):
                if ctx.replacing(section.key):
                    ctx.refresh()
                    section.prune(ctx, ctx.report(section.key))
            rebuilt = rebuild.rebuild(ctx.dirty)
            report = RunReport(
                mode="import",
                preview=preview,
                sections=_run_sections(keys, reports),
                rebuilt=rebuilt,
                notes=[*reader.notes, NOTHING_REREAD],
            )
            if preview:
                raise _Rollback
            _held_to(report, expected, started)
    except _Rollback:
        pass
    except BaseException:
        _remove_stored(ctx.stored_files)
        raise
    report.duration_s = round(time.monotonic() - started, 2)
    return report


def _remove_stored(names: list[str]) -> None:
    """The run failed: its rows are rolled back, and the files it stored
    would be named by nothing."""
    from django.core.files.storage import default_storage

    for name in names:
        try:
            default_storage.delete(name)
        except OSError:
            pass


def run_clear(
    keys: Iterable[str],
    *,
    preview: bool,
    closed: bool = True,
    expected: RunReport | None = None,
) -> RunReport:
    """closed=True (the page): keys must equal registry.closure(keys, "clear"),
    else ValueError - a section is cleared only after everything that
    requires it. closed=False (tests clearing one section of a fixture that
    has nothing depending on it): exactly these keys. `expected`: the
    preview a confirm is held to (NotAsPreviewed)."""
    started = time.monotonic()
    _check_free()
    keys = set(keys)
    if not keys:
        raise ValueError("nothing to clear")
    if closed and keys != registry.closure(keys, "clear"):
        raise ValueError(f"clear selection not closed: missing {sorted(registry.missing(keys, 'clear'))}")
    sections = [registry.get(key) for key in registry.ordered(keys)]
    reports = _reports(keys)
    ctx = ClearContext(preview=preview, clearing=frozenset(keys), dirty=Dirty(), reports=reports)
    try:
        with transaction.atomic():
            for section in reversed(sections):
                section.clear(ctx, ctx.report(section.key))
            rebuilt = rebuild.rebuild(ctx.dirty)
            report = RunReport(mode="clear", preview=preview, sections=_run_sections(keys, reports), rebuilt=rebuilt)
            if preview:
                raise _Rollback
            # A payment linked since « Voir ce qui sera effacé » would go with
            # its invoice, and the archive taken from that preview lacks the
            # bank.
            _held_to(report, expected, started)
    except _Rollback:
        pass
    report.duration_s = round(time.monotonic() - started, 2)
    return report
