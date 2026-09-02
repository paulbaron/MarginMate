from __future__ import annotations

import traceback
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from .importing import DuplicateInvoiceError, parse_and_import
from .models import Invoice, InvoiceType, ScrapeJob, Supplier
from .scrapers.generic_email import find_matching_emails, scrape_email_invoices
from .scrapers.metro import scrape_metro_invoices

DEFAULT_LOOKBACK_DAYS = 90
OVERLAP_DAYS = 3  # re-check the last few days in case an invoice landed just before the last known one


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
) -> None:
    """Runs the Metro scraper plus every active email-based InvoiceType and
    imports whatever they find, updating ``job`` as it goes so the UI can
    poll for live progress. Meant to be run in a background thread (see
    invoices/views.py) so the request that triggered it returns immediately.

    `source_codes`: which sources to actually search, using the same short
    codes shown in ScrapeJob.progress ("METRO", "type-<id>") - None (the
    default) means every eligible source, same as before this parameter
    existed; an explicit set restricts the run to just those, letting a
    search be narrowed to specific suppliers instead of always scanning
    everything.

    Cancellation (ScrapeJob.cancel_requested, set by views.cancel_gather) is
    checked before starting each source, and - for email sources, since a
    wide-range scan is the slow part - between IMAP batches within one
    source's own scan too (see scrapers/generic_email.py). Metro's own
    scraper isn't interruptible mid-run (it's a separate, unmodified
    Selenium flow - see its own module), so a cancel during a Metro run
    only takes effect once Metro itself finishes or the next source starts.
    """
    job = ScrapeJob.objects.get(pk=job_id)
    job.status = ScrapeJob.Status.RUNNING
    job.range_end = end_date or timezone.localdate()
    job.save(update_fields=["status", "range_end"])

    end = job.range_end
    created_total = 0
    found_total = 0

    try:
        _raise_if_cancelled(job)
        metro_supplier = Supplier.objects.filter(code="METRO", is_scrapable=True).first()
        if metro_supplier and (source_codes is None or "METRO" in source_codes):
            start = start_date or suggested_start_date("METRO")
            if job.range_start is None or start < job.range_start:
                job.range_start = start
                job.save(update_fields=["range_start"])
            job.update_progress("METRO", label=metro_supplier.name, found=0, imported=0)
            download_dir = str(settings.SCRAPE_DOWNLOAD_DIR / "metro")
            files = scrape_metro_invoices(
                download_dir,
                start,
                end,
                log=job.append_log,
                on_progress=lambda done, total: job.update_progress("METRO", found=done),
            )
            found_total += len(files)
            job.update_progress("METRO", found=len(files))
            imported = 0
            for pdf_path in files:
                if _import_downloaded_file(job, metro_supplier, pdf_path):
                    imported += 1
                    created_total += 1
                    job.update_progress("METRO", imported=imported)
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
            download_dir = str(settings.SCRAPE_DOWNLOAD_DIR / code)
            results = scrape_email_invoices(
                download_dir,
                start,
                end,
                sender_pattern=source.sender_pattern,
                subject_pattern=source.subject_pattern,
                body_pattern=source.body_pattern,
                attachment_pattern=source.attachment_pattern,
                log=job.append_log,
                on_progress=lambda done, total, _code=code: job.update_progress(_code, found=done),
                should_cancel=lambda: _is_cancelled(job),
            )
            found_total += len(results)
            job.update_progress(code, found=len(results))
            imported = 0
            for pdf_path, email_date in results:
                if _import_downloaded_file(
                    job,
                    invoice_type.supplier,
                    pdf_path,
                    date_hint=email_date,
                    parser_key_override=invoice_type.parser_key,
                ):
                    imported += 1
                    created_total += 1
                    job.update_progress(code, imported=imported)

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
