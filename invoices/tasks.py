from __future__ import annotations

import traceback
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from .importing import DuplicateInvoiceError, parse_and_import
from .models import Invoice, ScrapeJob, Supplier
from .scrapers.metro import scrape_metro_invoices
from .scrapers.uba_email import scrape_uba_invoices

DEFAULT_LOOKBACK_DAYS = 90
OVERLAP_DAYS = 3  # re-check the last few days in case an invoice landed just before the last known one


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


def _import_downloaded_file(job: ScrapeJob, supplier: Supplier, pdf_path: str, date_hint: date | None = None) -> bool:
    try:
        parse_and_import(pdf_path, supplier, date_hint=date_hint)
        return True
    except DuplicateInvoiceError:
        job.append_log(f"Skipped {pdf_path} (already imported)")
        return False
    except Exception as exc:  # noqa: BLE001 - one bad PDF shouldn't fail the whole batch
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Failed to import {pdf_path}: {detail}\n{traceback.format_exc()}")
        return False


def gather_invoices_task(job_id: int, start_date: date | None = None, end_date: date | None = None) -> None:
    """Runs the Metro + UBA scrapers and imports whatever they find, updating
    ``job`` as it goes so the UI can poll for live progress. Meant to be run
    in a background thread (see invoices/views.py) so the request that
    triggered it returns immediately.
    """
    job = ScrapeJob.objects.get(pk=job_id)
    job.status = ScrapeJob.Status.RUNNING
    job.range_end = end_date or timezone.localdate()
    job.save(update_fields=["status", "range_end"])

    end = job.range_end
    created_total = 0
    found_total = 0

    try:
        metro_supplier = Supplier.objects.filter(code="METRO", is_scrapable=True).first()
        if metro_supplier:
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
        else:
            job.append_log("Metro supplier is not configured as scrapable, skipping.")

        uba_supplier = Supplier.objects.filter(code="UBA", is_scrapable=True).first()
        if uba_supplier:
            start = start_date or suggested_start_date("UBA")
            if job.range_start is None or start < job.range_start:
                job.range_start = start
                job.save(update_fields=["range_start"])
            job.update_progress("UBA", label=uba_supplier.name, found=0, imported=0)
            download_dir = str(settings.SCRAPE_DOWNLOAD_DIR / "uba")
            results = scrape_uba_invoices(
                download_dir,
                start,
                end,
                log=job.append_log,
                on_progress=lambda done, total: job.update_progress("UBA", found=done),
            )
            found_total += len(results)
            job.update_progress("UBA", found=len(results))
            imported = 0
            for pdf_path, email_date in results:
                if _import_downloaded_file(job, uba_supplier, pdf_path, date_hint=email_date):
                    imported += 1
                    created_total += 1
                    job.update_progress("UBA", imported=imported)
        else:
            job.append_log("UBA supplier is not configured as scrapable, skipping.")

        job.invoices_found = found_total
        job.invoices_created = created_total
        job.status = ScrapeJob.Status.SUCCESS
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI via the job log
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Gather run failed: {detail}\n{traceback.format_exc()}")
        job.status = ScrapeJob.Status.FAILED
    finally:
        job.finished_at = timezone.now()
        job.save()
