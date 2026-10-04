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
    duplicate     this exact file, or this ticket, is already in - or it is
                  a driver's returnables slip, put in Consignes instead
                  ("consignes": RoutedToReturnablesError, drawn « Rangé dans
                  Consignes »)
    unrecognised  no known shop's header on it - reported, never guessed;
                  the file is kept ("kept") until the operator names the
                  shop (`import_with_shop`), then it becomes ok/duplicate
    error         unreadable file, or something unexpected - or a slip that
                  could not be put in Consignes (several formats recognise
                  it): in neither place, never drawn as filed
    ignored      not a PDF, an XML or a photo (a folder's Thumbs.db)
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

One database per bar (accounts/tenancy.py): a batch's pk restarts at 1 in
every tenant. So the files are staged in the tenant's own folder
(accounts.paths.imports_dir: `<tenant>/imports/`, outside what is ever
served), both
threads - the batch's and its heartbeat - run bound to the tenant that
started them (accounts.tenancy.bound), and what is kept in memory is keyed
by the tenant too (_BY_HAND). Two bars' batch 7 used to share one folder:
one imported the other's photos, then deleted them.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading

from django.db import DatabaseError
from django.utils import timezone

from accounts import paths
from accounts.tenancy import bound, tenant_key
from common import error_for_page

from .einvoice import EInvoiceError
from .importing import DuplicateInvoiceError, LineTooWideError, RoutedToReturnablesError
from .models import ReceiptBatch
from .ocr import DocumentTooBig
from .parsers.llm_fallback import AIReadingRefused
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
#: The refusals a file's line says in their own words: the app's, written in
#: French for the person (an electronic invoice it cannot take, a document
#: too long or too large to read, a line with a figure no column holds, an
#: AI reading cut off). Anything else is said by kind
#: (common.error_for_page), its detail in the server's log.
READING_REFUSALS = (EInvoiceError, DocumentTooBig, LineTooWideError, AIReadingRefused)

# Held for a read and write of a batch's `results`, never across an import.
# One for the process, every tenant included, on purpose: it makes writers
# take turns and keeps no data, so sharing it mixes nothing between bars. Its
# one cost: a write waiting on one tenant's busy database (SQLite's timeout)
# makes the other tenants' batches wait with it.
RESULTS_LOCK = threading.Lock()
# (tenant, batch, index) of the files being imported by hand: a new shop's
# re-read leaves them to that import. In memory, so a request that dies leaves
# nothing stuck. The tenant (accounts.tenancy.tenant_key) because pks restart
# at 1 in every tenant's database: keyed by the batch
# alone, one bar's hand import held back another bar's file.
_BY_HAND: set[tuple[str, int, int]] = set()


def _by_hand_key(batch: ReceiptBatch, index: int) -> tuple[str, int, int]:
    return (tenant_key(), batch.pk, index)


logger = logging.getLogger(__name__)


def _trace(what: str) -> str:
    """The traceback of the error being handled goes to the server's log,
    never to the batch's: the batch's log is drawn on its page, and a
    traceback names the server's own files to whichever bar sent a broken
    photo. `what` says which, in the server's log. Returns "" - nothing for
    the page."""
    logger.exception(what)
    return ""


def _staging_folder(batch: ReceiptBatch) -> str:
    """The batch's folder, in the tenant's own imports folder (read now)."""
    return os.path.join(str(paths.imports_dir()), STAGING_DIR, str(batch.pk))


def _staged(stored: str) -> str:
    """A file named in `results` ("receipt_batches/<pk>/0000.jpg"), on disk."""
    return os.path.join(str(paths.imports_dir()), stored)


class ShopChoiceError(Exception):
    """A file can't be imported under the shop the operator chose. The
    message is for the operator."""


def stage_batch(uploads, ignored_names=(), refused=(), sent_by: str = "") -> ReceiptBatch:
    """Copy the uploaded files somewhere that outlives the request, and
    record one pending entry per file - plus one error per file `refused`
    before it was written ((name, sentence): too heavy, forms.py), and one
    per ignored name. `sent_by`: the login sending them (its username)."""
    batch = ReceiptBatch.objects.create(sent_by=sent_by)
    os.makedirs(_staging_folder(batch), exist_ok=True)
    results = []
    for index, upload in enumerate(uploads):
        extension = os.path.splitext(upload.name)[1].lower()
        stored = f"{STAGING_DIR}/{batch.pk}/{index:04d}{extension}"
        with open(_staged(stored), "wb") as handle:
            handle.writelines(upload.chunks())
        results.append({"name": upload.name, "stored": stored, "status": "pending"})
    results += [{"name": name, "status": "error", "message": message} for name, message in refused]
    results += [
        {"name": name, "status": "ignored", "message": "Ni un PDF, ni un XML, ni une photo : ignoré."}
        for name in ignored_names
    ]
    batch.results = results
    batch.save(update_fields=["results"])
    return batch


def start_batch(batch: ReceiptBatch) -> None:
    # bound(): the thread works in the tenant of the request that started it
    # (a new thread starts with nothing bound), and closes its connection at
    # its end.
    threading.Thread(target=bound(_run_in_thread), args=(batch.pk,), daemon=True).start()


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
            if entry["status"] == "unrecognised" and entry.get("kept") and _by_hand_key(batch, index) not in _BY_HAND
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
    # Started as bound(_run_in_thread) (start_batch), which also closes the
    # thread's connection at its end: left open, it held a SQLite handle for
    # the life of the server.
    run_receipt_batch(batch_id)


class _Heartbeat(threading.Thread):
    """Says "still running" every HEARTBEAT_SECONDS, however long the
    current receipt takes. If a reaper got there anyway - the machine slept
    through a beat - the batch is put back to running: it is.

    Its loop is wrapped here, in the batch's thread - bound to the batch's
    tenant - since its own thread starts with nothing bound: unbound, the
    beats went to whichever batch had this pk, and put another bar's FAILED
    one back to running (accounts.tenancy.bound, which also closes the
    thread's connection at its end)."""

    def __init__(self, batch_id: int):
        super().__init__(daemon=True)
        self.batch_id = batch_id
        self.stopped = threading.Event()
        self._loop = bound(self._beat_until_stopped)

    def run(self) -> None:
        self._loop()

    def _beat_until_stopped(self) -> None:
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            self.beat()

    def beat(self) -> None:
        try:
            batch = ReceiptBatch.objects.filter(pk=self.batch_id)
            batch.update(last_heartbeat=timezone.now())
            batch.filter(status=ReceiptBatch.Status.FAILED).update(status=ReceiptBatch.Status.RUNNING, finished_at=None)
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
        batch.append_log(_trace(f"Import de tickets {batch.pk}") or "L'import s'est arrêté sur une erreur inattendue.")
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
    path = _staged(entry["stored"])
    if not os.path.exists(path):
        entry.update(status="error", message=MISSING_FILE)
        return None
    try:
        invoice = import_document(path, display_filename=entry["name"])
        # Inside the try, deliberately. _record_import's first act is to
        # format the invoice's total, and a figure the database cannot read
        # back raises there - outside every handler, that escaped _read_file
        # and marked the whole BATCH failed: the file that blew up got no
        # outcome at all and the files behind it were never read. One bad
        # file is that file's error and nothing else's.
        _record_import(entry, invoice)
    except DuplicateInvoiceError as exc:
        _not_imported(entry, exc)
        if _unfiled_slip(exc):
            batch.append_log(f"{entry['name']} : {exc}")
    except UnrecognisedShopError as exc:
        # Kept: the operator can still say which shop it is, from what it
        # was read as.
        entry.update(status="unrecognised", message=str(exc), kept=True, **first_reading(exc.text))
        return None
    except Exception as exc:  # noqa: BLE001 - reported per file, never aborts the batch
        # The app's own refusal in its words; anything else by kind - the
        # exception's own text (PIL's names the staged file's full path) and
        # its traceback to the server's log only (security audit LB-3).
        message = error_for_page(
            exc, said=READING_REFUSALS, log=logger, what=f"Import de tickets {batch.pk}, fichier {entry['name']!r}"
        )
        entry.update(status="error", message=message)
        batch.append_log(f"{entry['name']} : {message}")
    return path


def _unfiled_slip(exc: DuplicateInvoiceError) -> bool:
    """A driver's slip that Achats' guard would not import, and that is not in
    Consignes either (receipts.route_to_returnables): several formats recognise
    it, or storing it failed. Its sentence says where to drop it."""
    return isinstance(exc, RoutedToReturnablesError) and exc.slip is None


def _not_imported(entry: dict, exc: DuplicateInvoiceError) -> None:
    """Say on a file's entry why it made no new invoice: already in
    (« Déjà importé »), a slip put in Consignes (« Rangé dans Consignes »), or
    a slip that is in neither place - an error: drawn « Rangé dans
    Consignes » and counted among the « Déjà connus », it read as filed while
    its pickup waited for it."""
    if _unfiled_slip(exc):
        entry.pop("consignes", None)
        entry.update(status="error", message=str(exc))
        return
    entry.update(status="duplicate", message=str(exc))
    if isinstance(exc, RoutedToReturnablesError):
        entry["consignes"] = True


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
        shutil.rmtree(_staging_folder(batch), ignore_errors=True)


def import_with_shop(batch: ReceiptBatch, index: int, supplier) -> dict:
    """Import file `index` of a batch - one no shop was recognised on - as a
    ticket of `supplier`, and return its updated entry: "ok", or "duplicate"
    when the ticket turns out to be in already - or to be a driver's slip,
    put in Consignes ("consignes"). The batch may still be running: its
    thread leaves this entry alone.

    Raises ShopChoiceError, leaving the entry as it was, when the file isn't
    one waiting for its shop, and when the import itself fails or finds a
    slip it could not put in Consignes - the file is kept, to try again.
    """
    if not OCR_LOCK.acquire(timeout=SHOP_CHOICE_WAIT_SECONDS):
        raise ShopChoiceError("Un autre ticket est en cours d'import : réessayez dans un instant.")
    try:
        with RESULTS_LOCK:
            path = _waiting_file(batch, index)
            _BY_HAND.add(_by_hand_key(batch, index))
        try:
            return _import_with_shop(batch, index, path, supplier)
        finally:
            with RESULTS_LOCK:
                _BY_HAND.discard(_by_hand_key(batch, index))
    finally:
        OCR_LOCK.release()


def _waiting_file(batch: ReceiptBatch, index: int) -> str:
    """The staged file of entry `index`, which has to be waiting for its
    shop. Called under RESULTS_LOCK."""
    batch.refresh_from_db()
    entry = batch.results[index] if 0 <= index < len(batch.results) else None
    if entry is None or entry["status"] != "unrecognised" or not entry.get("kept"):
        raise ShopChoiceError("Ce fichier n'attend pas qu'on choisisse son enseigne.")
    path = _staged(entry["stored"])
    if not os.path.exists(path):
        entry.pop("kept")
        batch.save(update_fields=["results"])
        raise ShopChoiceError(MISSING_FILE)
    return path


def _import_with_shop(batch: ReceiptBatch, index: int, path: str, supplier) -> dict:
    entry = dict(batch.results[index])
    try:
        invoice = import_document(path, display_filename=entry["name"], supplier=supplier)
        # Inside the try, as in _read_file: a figure the database cannot read
        # back raises in _record_import, and out of the `else:` it was a 500
        # for the shop chosen by hand rather than that file's own error.
        entry.pop("message", None)
        _record_import(entry, invoice)
    except DuplicateInvoiceError as exc:
        batch.append_log(f"{entry['name']} (ticket {supplier.name}) : {exc}")
        if _unfiled_slip(exc):
            # Nothing was stored: the file stays, named again once the
            # formats are set right on the Consignes page.
            raise ShopChoiceError(str(exc)) from exc
        _not_imported(entry, exc)
    except Exception as exc:  # noqa: BLE001 - said on the page; the file stays for another try
        problem = error_for_page(
            exc,
            said=READING_REFUSALS,
            log=logger,
            what=f"Import de tickets {batch.pk}, fichier {entry['name']!r}, enseigne choisie",
        )
        batch.append_log(f"{entry['name']} : échec de l'import comme ticket {supplier.name}. {problem}")
        raise ShopChoiceError(f"{entry['name']} n'a pas pu être importé comme ticket {supplier.name}. {problem}")
    else:
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
