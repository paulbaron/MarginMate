"""Importing a folder of receipt photos in the background.

A folder is dozens of receipts at several seconds of OCR each - minutes, far
too long to hold a request open, and a request that dies halfway loses the
answer to "which ones went in?". So the upload only stages the files and
starts a thread; the batch page polls for progress and lists every file's
outcome as it lands. Same shape as the invoice gather (tasks.py): a daemon
thread, a job row it reports into, a cancel flag it checks between units of
work, and `JobLogMixin.reap_stale` for a thread the dev server killed.

Every file ends in exactly one state, shown to the operator:

    ok            imported (still to be checked on the review screen)
    duplicate     this exact file, or this ticket, is already in
    unrecognised  no known shop's header on it - reported, never guessed;
                  the file is kept ("kept") until the operator names the
                  shop (`import_with_shop`), then it becomes ok/duplicate
    error         unreadable file, or something unexpected
    ignored       not a PDF or a photo (a folder's Thumbs.db)
    cancelled     the batch was stopped before reaching it

A batch where one photo failed silently is worse than one that failed
loudly: the missing receipt shows up weeks later as stock never bought.

The shop of an unrecognised file can be chosen while the batch still runs, so
`results` has several writers: the thread, the request choosing a shop, and a
new shop's re-read. Each takes RESULTS_LOCK, reads the entries fresh and writes
back only the one it changed - the thread used to write the copy it started
with after every file, undoing whatever had been chosen meanwhile. The thread
takes the next pending file each time round, so a file sent back to "pending"
while it runs is read by the same run.

The dev server's autoreloader kills the thread outright on any code change:
a 137-ticket batch died one second after it started, and sat "running" for
half an hour. So a running batch beats every HEARTBEAT_SECONDS from a thread
of its own, counts as dead after ReceiptBatch.STALE_AFTER of silence, and
can be resumed - its staged files stay until every one has been read.
"""

from __future__ import annotations

import os
import shutil
import threading
import traceback

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

from .importing import DuplicateInvoiceError
from .models import ReceiptBatch
from .receipts import (
    OCR_LOCK,
    OCR_WAIT_SECONDS,
    UnrecognisedShopError,
    first_reading,
    import_document,
)

STAGING_DIR = "receipt_batches"
# A shop chosen by hand is imported inside the request, seconds of OCR. Two
# requests for the same file (two tabs, a double click) would both import it,
# so shop choices take turns. One process: the dev server is threaded, not
# forked.
SHOP_CHOICE_WAIT_SECONDS = OCR_WAIT_SECONDS
HEARTBEAT_SECONDS = 15
# How far back a new shop or header sends the files nobody could file through
# the import again (requeue_everywhere).
REQUEUE_BATCHES = 20
MISSING_FILE = "Fichier temporaire introuvable : réimportez ce ticket."

# Held for a read and write of a batch's `results`, never across an import.
RESULTS_LOCK = threading.Lock()
# (batch, index) of the files being imported by hand: a new shop's re-read
# leaves them to that import. In memory, so a request that dies leaves nothing
# stuck.
_BY_HAND: set[tuple[int, int]] = set()


class ShopChoiceError(Exception):
    """A file can't be imported under the shop the operator chose. The
    message is for the operator."""


def stage_batch(uploads, ignored_names=()) -> ReceiptBatch:
    """Copy the uploaded files somewhere that outlives the request, and
    record one pending entry per file (plus one per ignored name)."""
    batch = ReceiptBatch.objects.create()
    folder = os.path.join(settings.MEDIA_ROOT, STAGING_DIR, str(batch.pk))
    os.makedirs(folder, exist_ok=True)
    results = []
    for index, upload in enumerate(uploads):
        extension = os.path.splitext(upload.name)[1].lower()
        stored = f"{STAGING_DIR}/{batch.pk}/{index:04d}{extension}"
        with open(os.path.join(settings.MEDIA_ROOT, stored), "wb") as handle:
            for chunk in upload.chunks():
                handle.write(chunk)
        results.append({"name": upload.name, "stored": stored, "status": "pending"})
    results += [
        {"name": name, "status": "ignored", "message": "Ni un PDF ni une photo : ignoré."} for name in ignored_names
    ]
    batch.results = results
    batch.save(update_fields=["results"])
    return batch


def start_batch(batch: ReceiptBatch) -> None:
    threading.Thread(target=_run_in_thread, args=(batch.pk,), daemon=True).start()


def _restart(batch: ReceiptBatch) -> list[str]:
    """Set a batch that is not running to start again (the caller saves)."""
    batch.status = ReceiptBatch.Status.PENDING
    batch.cancel_requested = False
    batch.finished_at = None
    # Alive from now: a status poll between here and the thread's first save
    # must not reap it straight back.
    batch.last_heartbeat = timezone.now()
    return ["status", "cancel_requested", "finished_at", "last_heartbeat"]


def resume_batch(batch: ReceiptBatch) -> int:
    """Carry on with the files a batch that died never reached. Returns how
    many are left to read - 0 when there is nothing to resume, or when the
    batch may still be running (see ReceiptBatch.can_resume)."""
    with RESULTS_LOCK:
        batch.refresh_from_db()
        if not batch.can_resume:
            return 0
        remaining = batch.pending_count
        batch.save(update_fields=_restart(batch))
    batch.append_log(f"Reprise : {remaining} ticket(s) restant(s) à lire.")
    start_batch(batch)
    return remaining


def requeue_unrecognised(batch: ReceiptBatch) -> int:
    """Read the files of a batch no shop was recognised on again - once a
    shop has been added with its header, some are its tickets. A running
    batch reads them in its turn; a finished one is started again. A file
    being imported by hand is left to that import. Returns how many are read
    again."""
    with RESULTS_LOCK:
        batch.refresh_from_db()
        waiting = [
            index
            for index, entry in enumerate(batch.results)
            if entry["status"] == "unrecognised" and entry.get("kept") and (batch.pk, index) not in _BY_HAND
        ]
        if not waiting:
            return 0
        for index in waiting:
            entry = batch.results[index]
            entry.update(status="pending", message="")
            entry.pop("kept")
        # A batch that looks alive but whose thread died is reaped soon, and
        # resumed with these.
        running = batch.is_active
        batch.save(update_fields=["results"] + ([] if running else _restart(batch)))
    batch.append_log(f"Nouvelle enseigne : {len(waiting)} ticket(s) sans enseigne relu(s).")
    if not running:
        start_batch(batch)
    return len(waiting)


def requeue_everywhere() -> int:
    """Read the files no shop was recognised on again, in every recent import
    that still keeps some: a shop, or a header, added since may be theirs."""
    batches = ReceiptBatch.objects.order_by("-started_at")[:REQUEUE_BATCHES]
    return sum(requeue_unrecognised(batch) for batch in batches if batch.awaiting_shop_count)


def _run_in_thread(batch_id: int) -> None:
    try:
        run_receipt_batch(batch_id)
    finally:
        # A thread's connection is its own; leaving it open holds a SQLite
        # handle for the life of the server.
        connection.close()


class _Heartbeat(threading.Thread):
    """Says "still running" every HEARTBEAT_SECONDS, however long the
    current receipt takes. If a reaper got there anyway - the machine slept
    through a beat - the batch is put back to running: it is."""

    def __init__(self, batch_id: int):
        super().__init__(daemon=True)
        self.batch_id = batch_id
        self.stopped = threading.Event()

    def run(self) -> None:
        try:
            while not self.stopped.wait(HEARTBEAT_SECONDS):
                self.beat()
        finally:
            connection.close()

    def beat(self) -> None:
        try:
            batch = ReceiptBatch.objects.filter(pk=self.batch_id)
            batch.update(last_heartbeat=timezone.now())
            batch.filter(status=ReceiptBatch.Status.FAILED).update(
                status=ReceiptBatch.Status.RUNNING, finished_at=None
            )
        except DatabaseError:
            pass  # a busy database: the next beat will do

    def stop(self) -> None:
        self.stopped.set()
        self.join()


def run_receipt_batch(batch_id: int) -> ReceiptBatch:
    """Import every pending file of a batch, one at a time. One bad file
    never stops the others."""
    batch = ReceiptBatch.objects.get(pk=batch_id)
    batch.status = ReceiptBatch.Status.RUNNING
    batch.last_heartbeat = timezone.now()
    batch.save(update_fields=["status", "last_heartbeat"])
    heartbeat = _Heartbeat(batch.pk)
    heartbeat.start()
    try:
        while (index := _next_file(batch)) is not None:
            entry = dict(batch.results[index])
            path = _read_file(batch, entry)
            _save_entry(batch, index, entry)
            if path is not None:
                _discard(path)
    except Exception:  # noqa: BLE001 - the job row is where a crash is reported
        # Stopped before the status is written: a beat must never put a
        # batch that really failed back to running.
        heartbeat.stop()
        batch.status = ReceiptBatch.Status.FAILED
        batch.finished_at = timezone.now()
        batch.save(update_fields=["status", "finished_at"])
        batch.append_log(traceback.format_exc())
        with RESULTS_LOCK:
            batch.refresh_from_db(fields=["results"])
            _clean_up(batch)
    else:
        heartbeat.stop()
    return batch


def _next_file(batch: ReceiptBatch) -> int | None:
    """The index of the next file to read - or None, the batch's end written
    in the same breath: a file sent back to "pending" a moment later then
    finds the batch finished, and starts it again."""
    with RESULTS_LOCK:
        batch.refresh_from_db(fields=["results", "cancel_requested"])
        pending = [index for index, entry in enumerate(batch.results) if entry["status"] == "pending"]
        if pending and not batch.cancel_requested:
            return pending[0]
        if batch.cancel_requested:
            for index in pending:
                batch.results[index].update(status="cancelled", message="Lot arrêté avant ce fichier.")
            batch.status = ReceiptBatch.Status.CANCELLED
        else:
            batch.status = ReceiptBatch.Status.SUCCESS
        batch.finished_at = timezone.now()
        batch.save(update_fields=["results", "status", "finished_at"])
        # Files not read yet stay, for "Reprendre l'import", and so do those
        # waiting for their shop to be named.
        _clean_up(batch)
        return None


def _read_file(batch: ReceiptBatch, entry: dict) -> str | None:
    """Import one staged file, and say how it went on its entry. Returns the
    file, when it is no longer needed."""
    path = os.path.join(settings.MEDIA_ROOT, entry["stored"])
    if not os.path.exists(path):
        entry.update(status="error", message=MISSING_FILE)
        return None
    try:
        invoice = import_document(path, display_filename=entry["name"])
    except DuplicateInvoiceError as exc:
        entry.update(status="duplicate", message=str(exc))
    except UnrecognisedShopError as exc:
        # Kept: the operator can still say which shop it is, from what it
        # was read as.
        entry.update(status="unrecognised", message=str(exc), kept=True, **first_reading(exc.text))
        return None
    except Exception as exc:  # noqa: BLE001 - reported per file, never aborts the batch
        entry.update(status="error", message=str(exc).strip() or exc.__class__.__name__)
        batch.append_log(f"{entry['name']} : {entry['message']}\n{traceback.format_exc()}")
    else:
        _record_import(entry, invoice)
    return path


def _save_entry(batch: ReceiptBatch, index: int, entry: dict) -> None:
    """Write one file's outcome over the entry as it is now."""
    with RESULTS_LOCK:
        batch.refresh_from_db(fields=["results"])
        batch.results[index] = entry
        batch.last_heartbeat = timezone.now()
        batch.save(update_fields=["results", "last_heartbeat"])


def _record_import(entry: dict, invoice) -> None:
    entry.update(
        status="ok",
        invoice_id=invoice.pk,
        shop=invoice.supplier.name,
        total=f"{invoice.total_ttc:.2f}",
        date=f"{invoice.invoice_date:%d/%m/%Y}" if invoice.invoice_date else "",
        verified=invoice.receipt_verified,
        # A photo goes to the review screen; an invoice read by its
        # supplier's own reader to its own lines, under the state its import
        # left it in.
        receipt=invoice.is_receipt,
        state=invoice.status,
        state_label=invoice.get_status_display(),
    )


def _clean_up(batch: ReceiptBatch) -> None:
    """Remove the staged folder once no file in it is still needed. Called
    under RESULTS_LOCK, with the results just read."""
    if not any(entry["status"] == "pending" or entry.get("kept") for entry in batch.results):
        shutil.rmtree(os.path.join(settings.MEDIA_ROOT, STAGING_DIR, str(batch.pk)), ignore_errors=True)


def import_with_shop(batch: ReceiptBatch, index: int, supplier) -> dict:
    """Import file `index` of a batch - one no shop was recognised on - as a
    ticket of `supplier`, and return its updated entry: "ok", or "duplicate"
    when the ticket turns out to be in already. The batch may still be
    running: its thread leaves this entry alone.

    Raises ShopChoiceError, leaving the entry as it was, when the file isn't
    one waiting for its shop, and when the import itself fails - the file is
    kept, to try again.
    """
    if not OCR_LOCK.acquire(timeout=SHOP_CHOICE_WAIT_SECONDS):
        raise ShopChoiceError("Un autre ticket est en cours d'import : réessayez dans un instant.")
    try:
        with RESULTS_LOCK:
            path = _waiting_file(batch, index)
            _BY_HAND.add((batch.pk, index))
        try:
            return _import_with_shop(batch, index, path, supplier)
        finally:
            with RESULTS_LOCK:
                _BY_HAND.discard((batch.pk, index))
    finally:
        OCR_LOCK.release()


def _waiting_file(batch: ReceiptBatch, index: int) -> str:
    """The staged file of entry `index`, which has to be waiting for its
    shop. Called under RESULTS_LOCK."""
    batch.refresh_from_db()
    entry = batch.results[index] if 0 <= index < len(batch.results) else None
    if entry is None or entry["status"] != "unrecognised" or not entry.get("kept"):
        raise ShopChoiceError("Ce fichier n'attend pas qu'on choisisse son enseigne.")
    path = os.path.join(settings.MEDIA_ROOT, entry["stored"])
    if not os.path.exists(path):
        entry.pop("kept")
        batch.save(update_fields=["results"])
        raise ShopChoiceError(MISSING_FILE)
    return path


def _import_with_shop(batch: ReceiptBatch, index: int, path: str, supplier) -> dict:
    entry = dict(batch.results[index])
    try:
        invoice = import_document(path, display_filename=entry["name"], supplier=supplier)
    except DuplicateInvoiceError as exc:
        entry.update(status="duplicate", message=str(exc))
        batch.append_log(f"{entry['name']} (ticket {supplier.name}) : {exc}")
    except Exception as exc:  # noqa: BLE001 - said on the page; the file stays for another try
        problem = str(exc).strip() or exc.__class__.__name__
        batch.append_log(
            f"{entry['name']} : échec de l'import comme ticket {supplier.name} : {problem}\n{traceback.format_exc()}"
        )
        raise ShopChoiceError(f"{entry['name']} n'a pas pu être importé comme ticket {supplier.name} : {problem}")
    else:
        entry.pop("message", None)
        _record_import(entry, invoice)
        batch.append_log(f"{entry['name']} : importé comme ticket {supplier.name} (enseigne choisie à la main).")
    entry.pop("kept")
    _discard(path)
    with RESULTS_LOCK:
        batch.refresh_from_db(fields=["results"])
        batch.results[index] = entry
        batch.save(update_fields=["results"])
        _clean_up(batch)
    return entry


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
