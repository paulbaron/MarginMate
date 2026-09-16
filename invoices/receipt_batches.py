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
    unrecognised  no known shop's header on it - reported, never guessed
    error         unreadable file, or something unexpected
    ignored       not a PDF or a photo (a folder's Thumbs.db)
    cancelled     the batch was stopped before reaching it

A batch where one photo failed silently is worse than one that failed
loudly: the missing receipt shows up weeks later as stock never bought.
"""

from __future__ import annotations

import os
import shutil
import threading
import traceback

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .importing import DuplicateInvoiceError
from .models import ReceiptBatch
from .receipts import import_receipt

STAGING_DIR = "receipt_batches"


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


def _run_in_thread(batch_id: int) -> None:
    try:
        run_receipt_batch(batch_id)
    finally:
        # A thread's connection is its own; leaving it open holds a SQLite
        # handle for the life of the server.
        connection.close()


def run_receipt_batch(batch_id: int) -> ReceiptBatch:
    """Import every pending file of a batch, one at a time. One bad file
    never stops the others."""
    batch = ReceiptBatch.objects.get(pk=batch_id)
    batch.status = ReceiptBatch.Status.RUNNING
    batch.last_heartbeat = timezone.now()
    batch.save(update_fields=["status", "last_heartbeat"])
    try:
        for entry in batch.results:
            if entry["status"] != "pending":
                continue
            batch.refresh_from_db(fields=["cancel_requested"])
            if batch.cancel_requested:
                break
            path = os.path.join(settings.MEDIA_ROOT, entry["stored"])
            try:
                invoice = import_receipt(path, display_filename=entry["name"])
            except DuplicateInvoiceError as exc:
                entry.update(status="duplicate", message=str(exc))
            except ValueError as exc:
                entry.update(status="unrecognised", message=str(exc))
            except Exception as exc:  # noqa: BLE001 - reported per file, never aborts the batch
                entry.update(status="error", message=str(exc).strip() or exc.__class__.__name__)
                batch.append_log(f"{entry['name']} : {entry['message']}\n{traceback.format_exc()}")
            else:
                entry.update(
                    status="ok",
                    invoice_id=invoice.pk,
                    shop=invoice.supplier.name,
                    total=f"{invoice.total_ttc:.2f}",
                    date=f"{invoice.invoice_date:%d/%m/%Y}" if invoice.invoice_date else "",
                    verified=invoice.receipt_verified,
                )
            finally:
                _discard(path)
            batch.last_heartbeat = timezone.now()
            batch.save(update_fields=["results", "last_heartbeat"])

        if batch.cancel_requested:
            for entry in batch.results:
                if entry["status"] == "pending":
                    entry.update(status="cancelled", message="Lot arrêté avant ce fichier.")
            batch.status = ReceiptBatch.Status.CANCELLED
        else:
            batch.status = ReceiptBatch.Status.SUCCESS
    except Exception:  # noqa: BLE001 - the job row is where a crash is reported
        batch.status = ReceiptBatch.Status.FAILED
        batch.append_log(traceback.format_exc())
    finally:
        batch.finished_at = timezone.now()
        batch.save(update_fields=["results", "status", "finished_at"])
        shutil.rmtree(os.path.join(settings.MEDIA_ROOT, STAGING_DIR, str(batch.pk)), ignore_errors=True)
    return batch


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
