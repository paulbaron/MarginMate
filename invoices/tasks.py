from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import date, timedelta

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

from .importing import DuplicateInvoiceError, parse_and_import
from .models import Invoice, InvoiceType, ScrapeJob, Supplier
from .scrapers.generic_email import find_matching_emails, scrape_email_invoices
from .scrapers.metro import MetroError, MetroPaused, scrape_metro_invoices
from .scrapers.website import WebsiteError, WebsiteRecipe, fetch_website_invoices, list_website_invoices

DEFAULT_LOOKBACK_DAYS = 90
OVERLAP_DAYS = 3  # re-check the last few days in case an invoice landed just before the last known one
HEARTBEAT_SECONDS = 15


class _GatherHeartbeat(threading.Thread):
    """Says "still running" every HEARTBEAT_SECONDS, however long the
    current step takes - an OCR, Metro's one-at-a-time downloads. A gather
    silent for longer than the reaper waits (a laptop asleep) was reaped
    while its thread ran on, and a second gather could be started beside
    it; a beat after such a reaping puts it back to running: it is.

    Only while the gather moves (its log or its progress changes within the
    reaper's STALE_AFTER): beating whatever it did, a thread blocked for good
    in one call would never be reaped, and every new gather refused."""

    def __init__(self, job_id: int, clock=time.monotonic):
        super().__init__(daemon=True)
        self.job_id = job_id
        self.stopped = threading.Event()
        self.clock = clock
        self.seen = None
        self.changed_at = clock()

    def run(self) -> None:
        try:
            while not self.stopped.wait(HEARTBEAT_SECONDS):
                self.beat()
        finally:
            connection.close()

    def beat(self) -> None:
        try:
            jobs = ScrapeJob.objects.filter(pk=self.job_id)
            state = jobs.values_list("log", "progress").first()
            if state != self.seen:
                self.seen, self.changed_at = state, self.clock()
            elif self.clock() - self.changed_at > ScrapeJob.STALE_AFTER.total_seconds():
                return  # stuck: left to the reaper
            jobs.update(last_heartbeat=timezone.now())
            jobs.filter(status=ScrapeJob.Status.FAILED).update(status=ScrapeJob.Status.RUNNING, finished_at=None)
        except DatabaseError:
            pass  # a busy database: the next beat will do

    def stop(self) -> None:
        self.stopped.set()
        self.join()


class _Cancelled(Exception):
    """Raised internally to unwind a task once the user has asked to cancel
    a running job (see ScrapeJob.cancel_requested) - never escapes the task
    itself, always caught in the same function that raises it."""


def _is_cancelled(job: ScrapeJob) -> bool:
    job.refresh_from_db(fields=["cancel_requested"])
    return job.cancel_requested


def _raise_if_cancelled(job: ScrapeJob) -> None:
    if _is_cancelled(job):
        raise _Cancelled


def suggested_start_date(supplier_code: str) -> date:
    """Best-guess start of the search range: a few days before the last
    invoice we already have for that supplier, or a fixed lookback if we
    have none yet. Only used to pre-fill the date picker / as a fallback
    when the user doesn't override the range.
    """
    last_invoice = Invoice.objects.filter(supplier__code=supplier_code).order_by("-invoice_date").first()
    if last_invoice and last_invoice.invoice_date:
        return last_invoice.invoice_date - timedelta(days=OVERLAP_DAYS)
    return timezone.localdate() - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def _gathered_start(supplier: Supplier) -> date:
    """A few days before the newest invoice gathered for `supplier` -
    photographed tickets and dates in the future left out, as in
    default_gather_start: a Metro paper ticket dated after its last PDF
    moved Metro's start past the gap it was meant to cover."""
    latest = (
        Invoice.objects.filter(supplier=supplier, ocr_text="", invoice_date__lte=timezone.localdate())
        .order_by("-invoice_date")
        .values_list("invoice_date", flat=True)
        .first()
    )
    if latest:
        return latest - timedelta(days=OVERLAP_DAYS)
    return timezone.localdate() - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def default_gather_start(supplier_ids) -> date:
    """Where the "Factures" page starts a search by default: the date of the
    newest invoice any gathered source has already brought in - a gather is
    for what arrived since. Receipts are left out (photographed, never
    gathered), and so is a date in the future (a misread one).
    """
    latest = (
        Invoice.objects.filter(supplier_id__in=supplier_ids, ocr_text="", invoice_date__lte=timezone.localdate())
        .order_by("-invoice_date")
        .values_list("invoice_date", flat=True)
        .first()
    )
    return latest or timezone.localdate() - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def _import_downloaded_file(
    job: ScrapeJob,
    supplier: Supplier,
    pdf_path: str,
    date_hint: date | None = None,
    parser_key_override: str | None = None,
) -> bool:
    try:
        parse_and_import(pdf_path, supplier, date_hint=date_hint, parser_key_override=parser_key_override)
        return True
    except DuplicateInvoiceError:
        job.append_log(f"Skipped {pdf_path} (already imported)")
        return False
    except Exception as exc:  # noqa: BLE001 - one bad PDF shouldn't fail the whole batch
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Failed to import {pdf_path}: {detail}\n{traceback.format_exc()}")
        return False


def gather_invoices_task(
    job_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
    source_codes: set[str] | None = None,
    metro_now: bool = False,
) -> None:
    """Runs the Metro scraper plus every active email-based InvoiceType and
    imports whatever they find, updating ``job`` as it goes so the UI can
    poll for live progress. Meant to be run in a background thread (see
    invoices/views.py) so the request that triggered it returns immediately.

    `source_codes`: which sources to actually search, using the same short
    codes shown in ScrapeJob.progress ("METRO", "type-<id>") - None means
    every eligible source **but Metro**, which is signed in to only when
    named: most of the sign-ins that got this machine blocked by Metro's
    firewall came from gathers started from a shell or a script.
    `metro_now`: a person asked for one sign-in to Metro all the same,
    through its pause (scrapers/metro.metro_pause).

    One source failing is said on its own line of the progress table, and
    the others run all the same (_gather_metro, _gather_website).

    Cancellation (ScrapeJob.cancel_requested, set by views.cancel_gather) is
    checked before starting each source, and within each: between IMAP
    batches (scrapers/generic_email.py), before each Metro window and
    download, before each portal download.
    """
    job = ScrapeJob.objects.get(pk=job_id)
    job.status = ScrapeJob.Status.RUNNING
    job.range_end = end_date or timezone.localdate()
    job.save(update_fields=["status", "range_end"])
    heartbeat = _GatherHeartbeat(job.id)
    heartbeat.start()

    end = job.range_end
    created_total = 0
    found_total = 0

    try:
        _raise_if_cancelled(job)
        metro_supplier = Supplier.objects.filter(code="METRO", is_scrapable=True).first()
        if metro_supplier and source_codes is not None and "METRO" in source_codes:
            # From its own newest invoice at the latest: while Metro was
            # paused, gathers of the other sources moved the offered start
            # past it, and the days between were never searched on Metro.
            # Its rows already imported are not downloaded again.
            own_start = _gathered_start(metro_supplier)
            start = min(start_date, own_start) if start_date else own_start
            if job.range_start is None or start < job.range_start:
                job.range_start = start
                job.save(update_fields=["range_start"])
            job.update_progress("METRO", label=metro_supplier.name, found=0, imported=0)
            found, imported = _gather_metro(job, metro_supplier, start, end, metro_now)
            found_total += found
            created_total += imported
        elif metro_supplier is None:
            job.append_log("Metro supplier is not configured as scrapable, skipping.")

        email_types = list(
            InvoiceType.objects.filter(is_active=True, source_kind=InvoiceType.SourceKind.EMAIL).select_related(
                "supplier", "email_source"
            )
        )
        for invoice_type in email_types:
            _raise_if_cancelled(job)
            code = f"type-{invoice_type.id}"
            if source_codes is not None and code not in source_codes:
                continue

            source = getattr(invoice_type, "email_source", None)
            if source is None:
                job.append_log(f"{invoice_type.name}: no email source configured, skipping.")
                continue

            start = start_date or suggested_start_date(invoice_type.supplier.code)
            if job.range_start is None or start < job.range_start:
                job.range_start = start
                job.save(update_fields=["range_start"])
            job.update_progress(code, label=invoice_type.name, found=0, imported=0)
            found, imported = _gather_email(job, invoice_type, source, code, start, end)
            found_total += found
            created_total += imported

        website_types = list(
            InvoiceType.objects.filter(is_active=True, source_kind=InvoiceType.SourceKind.WEBSITE).select_related(
                "supplier", "website_source"
            )
        )
        for invoice_type in website_types:
            _raise_if_cancelled(job)
            code = f"type-{invoice_type.id}"
            if source_codes is not None and code not in source_codes:
                continue
            source = getattr(invoice_type, "website_source", None)
            if source is None:
                job.append_log(f"{invoice_type.name}: no website configured, skipping.")
                continue
            start = start_date or suggested_start_date(invoice_type.supplier.code)
            if job.range_start is None or start < job.range_start:
                job.range_start = start
                job.save(update_fields=["range_start"])
            job.update_progress(code, label=invoice_type.name, found=0, imported=0)
            found, imported = _gather_website(job, invoice_type, source, code, start, end)
            found_total += found
            created_total += imported

        job.invoices_found = found_total
        job.invoices_created = created_total
        job.status = ScrapeJob.Status.SUCCESS
    except _Cancelled:
        job.invoices_found = found_total
        job.invoices_created = created_total
        job.append_log("Annulé par l'utilisateur.")
        job.status = ScrapeJob.Status.CANCELLED
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI via the job log
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Gather run failed: {detail}\n{traceback.format_exc()}")
        # The card shows the log's last line in plain view: the reason, not
        # the traceback's last frame.
        job.append_log(f"Échec de la récupération : {detail[:300]}")
        job.status = ScrapeJob.Status.FAILED
    finally:
        heartbeat.stop()
        job.finished_at = timezone.now()
        job.save()


def _gather_email(job: ScrapeJob, invoice_type: InvoiceType, source, code: str, start: date, end: date) -> tuple[int, int]:
    """One mailbox type, contained like a portal: a login refused (a revoked
    app password), a connection lost, is said on its line, and the other
    types and the portals run all the same - it failed the whole gather.
    Returns (found, imported)."""
    try:
        results = scrape_email_invoices(
            os.path.join(str(settings.SCRAPE_DOWNLOAD_DIR), code),
            start,
            end,
            sender_pattern=source.sender_pattern,
            subject_pattern=source.subject_pattern,
            body_pattern=source.body_pattern,
            attachment_pattern=source.attachment_pattern,
            log=job.append_log,
            on_progress=lambda done, total: job.update_progress(code, found=done),
            should_cancel=lambda: _is_cancelled(job),
        )
    except _Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - one source failing is said on its own line
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"{invoice_type.name} : échec de la boîte mail - {detail}\n{traceback.format_exc()}")
        job.update_progress(code, error=f"Boîte mail : {detail}"[:300])
        return 0, 0
    job.update_progress(code, found=len(results))
    imported = 0
    for pdf_path, email_date in results:
        # A type naming its reader is read by it; one naming none is read
        # like any document of its supplier - filed empty, it came again at
        # every gather (no number to find it by).
        if invoice_type.parser_key:
            brought_in = _import_downloaded_file(
                job,
                invoice_type.supplier,
                pdf_path,
                date_hint=email_date,
                parser_key_override=invoice_type.parser_key,
            )
        else:
            brought_in = _import_document_file(
                job,
                invoice_type.supplier,
                pdf_path,
                chosen_because=f"Reçue par e-mail (« {invoice_type.name} »).",
                date_hint=email_date,
                by_type=invoice_type.name,
            )
        if brought_in:
            imported += 1
            job.update_progress(code, imported=imported)
    return len(results), imported


def _gather_metro(job: ScrapeJob, supplier: Supplier, start: date, end: date, metro_now: bool) -> tuple[int, int]:
    """Metro's part of a gather, contained like a portal's: whatever stops it
    - its firewall above all - is said on its own line, what landed before
    the stop is imported, and the other sources run all the same. On 18/09 a
    refusal failed the whole gather: the mailbox and the portals were never
    searched. Returns (found, imported)."""
    download_dir = os.path.join(str(settings.SCRAPE_DOWNLOAD_DIR), "metro")
    files: list[str] = []
    error = ""
    try:
        files = scrape_metro_invoices(
            download_dir,
            start,
            end,
            log=job.append_log,
            on_progress=lambda done, total: job.update_progress("METRO", found=done),
            should_cancel=lambda: _is_cancelled(job),
            ignore_pause=metro_now,
        )
    except MetroPaused as exc:
        # Left alone after a refusal: Metro's invoices of the period were not
        # fetched - a failure of this run. Signed in to lately: the gather
        # that did brought them in - a note.
        job.append_log(str(exc))
        job.update_progress("METRO", **{"error" if exc.after_block else "note": str(exc)[:300]})
        return 0, 0
    except MetroError as exc:
        files, error = exc.files, str(exc)
    except Exception as exc:  # noqa: BLE001 - one source failing is said on its own line
        error = f"Metro : erreur inattendue ({exc.__class__.__name__} - {str(exc).strip()[:200]})."
        job.append_log(traceback.format_exc())
    job.update_progress("METRO", found=len(files))
    imported = 0
    for pdf_path in files:
        if _import_downloaded_file(job, supplier, pdf_path):
            imported += 1
            job.update_progress("METRO", imported=imported)
    if error:
        job.append_log(error)
        job.update_progress("METRO", error=error[:300])
    return len(files), imported


def _known_numbers(supplier: Supplier) -> set[str]:
    return set(Invoice.objects.filter(supplier=supplier).exclude(invoice_number="").values_list("invoice_number", flat=True))


def _gather_website(job: ScrapeJob, invoice_type: InvoiceType, source, code: str, start: date, end: date) -> tuple[int, int]:
    """One customer portal: sign in, download the period's invoices, import
    them as the supplier's documents. A site that fails - turning automated
    browsers away, asking for a code, refusing the password - says so on
    its own line and the gather goes on with the next source: one site
    down is not every invoice missing."""
    try:
        files = fetch_website_invoices(
            WebsiteRecipe.from_source(source, name=invoice_type.name),
            os.path.join(str(settings.SCRAPE_DOWNLOAD_DIR), code),
            start,
            end,
            known_numbers=_known_numbers(invoice_type.supplier),
            log=job.append_log,
            on_progress=lambda done, total: job.update_progress(code, found=done),
            should_cancel=lambda: _is_cancelled(job),
            headless=settings.SCRAPER_HEADLESS,
            env_file=os.path.join(str(settings.BASE_DIR), ".env"),
        )
    except WebsiteError as exc:
        job.append_log(str(exc))
        job.update_progress(code, error=str(exc)[:300])
        return 0, 0
    job.update_progress(code, found=len(files))
    imported = 0
    for path in files:
        if _import_document_file(
            job, invoice_type.supplier, path, chosen_because=f"Téléchargée par « {invoice_type.name} ».",
            by_type=invoice_type.name,
        ):
            imported += 1
            job.update_progress(code, imported=imported)
    return len(files), imported


def _import_document_file(
    job: ScrapeJob,
    supplier: Supplier,
    path: str,
    chosen_because: str,
    date_hint: date | None = None,
    by_type: str | None = None,
) -> bool:
    """A document fetched from its supplier's site, or from the mailbox for a
    supplier with no reader of its own, is that supplier's, read the way any
    of its documents is (receipts.import_document: its own reader where it
    has one, the one reader otherwise, a charge as a charge) - never filed
    empty, and refused when this very file is in already (its digest). One
    OCR at a time (receipts.OCR_LOCK). `by_type`: the type that fetched it -
    a document printing what names another supplier teaches nothing."""
    from .receipts import OCR_LOCK, OCR_WAIT_SECONDS, import_document

    from . import supplier_changes

    if not OCR_LOCK.acquire(timeout=OCR_WAIT_SECONDS):
        job.append_log(f"Skipped {path}: another document was being read for too long")
        return False
    try:
        # What the document teaches its supplier is recorded as coming from
        # this source, and said in the gather's log.
        with supplier_changes.cause(chosen_because.rstrip(".")), supplier_changes.collect() as changes:
            import_document(
                path,
                display_filename=os.path.basename(path),
                supplier=supplier,
                date_hint=date_hint,
                chosen_because=chosen_because,
                by_type=by_type,
            )
        for change in changes:
            job.append_log(f"{os.path.basename(path)} : {change.summary}")
        return True
    except DuplicateInvoiceError:
        job.append_log(f"Skipped {path} (already imported)")
        return False
    except Exception as exc:  # noqa: BLE001 - one bad file shouldn't fail the whole gather
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Failed to import {path}: {detail}\n{traceback.format_exc()}")
        return False
    finally:
        OCR_LOCK.release()


def test_website_task(job_id: int, recipe: WebsiteRecipe, supplier_id: int, start_date: date, end_date: date) -> None:
    """Dry run of a website source: signs in and lists what it would
    download, downloading nothing - how a new site's settings are checked
    before a real gather. Its rows land in job.test_matches."""
    job = ScrapeJob.objects.get(pk=job_id)
    job.status = ScrapeJob.Status.RUNNING
    job.range_start = start_date
    job.range_end = end_date
    job.save(update_fields=["status", "range_start", "range_end"])
    try:
        supplier = Supplier.objects.filter(pk=supplier_id).first()
        rows = list_website_invoices(
            recipe,
            os.path.join(str(settings.SCRAPE_DOWNLOAD_DIR), f"test-{job.id}"),
            start_date,
            end_date,
            known_numbers=_known_numbers(supplier) if supplier else (),
            log=job.append_log,
            headless=settings.SCRAPER_HEADLESS,
            env_file=os.path.join(str(settings.BASE_DIR), ".env"),
        )
        job.test_matches = rows
        job.update_progress("test", label="Résultats du test", found=len(rows))
        job.status = ScrapeJob.Status.SUCCESS
    except WebsiteError as exc:
        job.append_log(str(exc))
        job.status = ScrapeJob.Status.FAILED
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI via the job log
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Test failed: {detail}\n{traceback.format_exc()}")
        job.status = ScrapeJob.Status.FAILED
    finally:
        job.finished_at = timezone.now()
        job.save()


def test_email_pattern_task(
    job_id: int,
    start_date: date,
    end_date: date,
    sender_pattern: str,
    subject_pattern: str,
    body_pattern: str,
    attachment_pattern: str,
) -> None:
    """Dry-run: searches the shared mailbox for emails matching the given
    patterns and records what it found in job.test_matches - nothing is
    written to disk and nothing is imported. Lets a new invoice type's
    patterns be verified against real mail before it's ever used in a real
    gather run. Cancellable the same way gather_invoices_task is (see its
    docstring) - a wide test range can scan thousands of emails too."""
    job = ScrapeJob.objects.get(pk=job_id)
    job.status = ScrapeJob.Status.RUNNING
    job.range_start = start_date
    job.range_end = end_date
    job.save(update_fields=["status", "range_start", "range_end"])

    try:
        matches = find_matching_emails(
            start_date,
            end_date,
            sender_pattern,
            subject_pattern,
            body_pattern,
            attachment_pattern,
            log=job.append_log,
            on_progress=lambda done, total: job.update_progress("test", label="Résultats du test", found=done),
            should_cancel=lambda: _is_cancelled(job),
        )
        job.test_matches = [
            {
                "sender": match.sender,
                "subject": match.subject,
                "date": match.email_date.isoformat() if match.email_date else None,
                "attachments": [attachment.filename for attachment in match.attachments],
            }
            for match in matches
        ]
        job.update_progress("test", label="Résultats du test", found=len(matches))
        job.status = ScrapeJob.Status.CANCELLED if _is_cancelled(job) else ScrapeJob.Status.SUCCESS
        if job.status == ScrapeJob.Status.CANCELLED:
            job.append_log("Annulé par l'utilisateur.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI via the job log
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Test failed: {detail}\n{traceback.format_exc()}")
        job.status = ScrapeJob.Status.FAILED
    finally:
        job.finished_at = timezone.now()
        job.save()
