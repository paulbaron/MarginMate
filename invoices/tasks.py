from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
import traceback
from datetime import date, timedelta

from django.conf import settings
from django.db import DatabaseError, OperationalError, transaction
from django.utils import timezone

from accounts import paths
from accounts.tenancy import bound, integrations_allowed

from . import coverage, integrations
from .coverage import CATCH_UP_TO_DO, DEFAULT_LOOKBACK_DAYS, OVERLAP_DAYS  # noqa: F401 - their home for callers
from .importing import DuplicateInvoiceError, RoutedToReturnablesError, parse_and_import
from .models import EMAIL_SEARCH_FIELDS, AutoGather, EmailInvoiceSource, Invoice, InvoiceType, ScrapeJob, Supplier
from .ocr import PdfiumBusy
from .scrapers.generic_email import IncompleteSearch, find_matching_emails, scrape_email_invoices
from .scrapers.metro import MetroError, MetroPaused, scrape_metro_invoices
from .scrapers.website import WebsiteError, WebsiteRecipe, fetch_website_invoices, list_website_invoices

logger = logging.getLogger(__name__)

# DEFAULT_LOOKBACK_DAYS (90) and OVERLAP_DAYS (3: the last few days re-checked
# in case an invoice landed just before the last one known) live in
# coverage.py, with CATCH_UP_TO_DO - the sentence an automatic run's line
# carries while a stretch is left to catch up by hand.
HEARTBEAT_SECONDS = 15
#: A slip format's source in a gather: "bons-<format pk>" (ScrapeJob.
#: progress, the gather card's boxes, the Consignes page's hidden sources).
SLIPS_PREFIX = "bons-"
#: A mailbox type's line when documents it fetched were not imported for
#: want of the OCR or the database (_gather_email): a failed source, its
#: coverage left where it was.
NOT_IMPORTED_NOW = (
    "{count} document(s) non importé(s) faute de lecture ou de base disponible : repris à la prochaine récupération"
)


def slips_code(fmt) -> str:
    return f"{SLIPS_PREFIX}{fmt.pk}"


def slips_label(fmt) -> str:
    """How a format's slips are named on the gather card and its progress."""
    return f"Bons de consignes — {fmt.name}"


class _GatherHeartbeat(threading.Thread):
    """Says "still running" every HEARTBEAT_SECONDS, however long the
    current step takes - an OCR, Metro's one-at-a-time downloads. A gather
    silent for longer than the reaper waits (a laptop asleep) was reaped
    while its thread ran on, and a second gather could be started beside
    it; a beat after such a reaping puts it back to running: it is.

    Only while the gather moves (its log or its progress changes within the
    reaper's STALE_AFTER): beating whatever it did, a thread blocked for good
    in one call would never be reaped, and every new gather refused.

    Its thread is a new one, started with nothing bound: the loop is wrapped
    here, in the gather's own thread - bound to the gather's tenant - so the
    beats go to the gather's job and not to another bar's with the same pk
    (a FAILED one put back to RUNNING, and blocking its gathers). Unbound in
    multi mode it refuses to exist (accounts.tenancy.bound). bound() also
    closes this thread's connection at its end."""

    def __init__(self, job_id: int, clock=time.monotonic):
        super().__init__(daemon=True)
        self.job_id = job_id
        self.stopped = threading.Event()
        self.clock = clock
        self.seen = None
        self.changed_at = clock()
        self._loop = bound(self._beat_until_stopped)

    def run(self) -> None:
        self._loop()

    def _beat_until_stopped(self) -> None:
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            self.beat()

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


def _download_dir(name: str) -> str:
    """Where a source's run downloads: `name` ("metro", "type-<id>",
    "test-<job id>") under the tenant's own downloads folder
    (accounts.paths.downloads_dir, read now). Those names are ids, and ids
    restart at 1 in every
    tenant's database: in one shared folder two bars' runs took each other's
    files - the scrapers decide what « landed » by what appeared there."""
    return os.path.join(str(paths.downloads_dir()), name)


def _refused(job: ScrapeJob, message: str) -> None:
    """End a job the tenant may not run (integrations.py), saying why - in
    the thread, which is the last place before an account is contacted."""
    job.append_log(message)
    job.status = ScrapeJob.Status.FAILED
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "finished_at"])


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


def _own_start(supplier: Supplier) -> date:
    """A few days before `supplier`'s newest invoice dated today at the
    latest, whatever read it - else DEFAULT_LOOKBACK_DAYS back. Not
    _gathered_start, which counts the invoices a reader of the supplier's
    own read (ocr_text blank): a portal's documents are read as tickets, and
    it would find none."""
    latest = (
        Invoice.objects.filter(supplier=supplier, invoice_date__lte=timezone.localdate())
        .order_by("-invoice_date")
        .values_list("invoice_date", flat=True)
        .first()
    )
    if latest:
        return latest - timedelta(days=OVERLAP_DAYS)
    return timezone.localdate() - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def _catch_up(sentence: str) -> dict:
    return {"catch_up": sentence[:300]} if sentence else {}


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
    chosen_because: str | None = None,
    by_type: str | None = None,
) -> bool | None:
    """A file Metro or a mailbox source naming its reader fetched, read by
    that reader - but refused when this very file is in already (its digest)
    and, an e-invoice, read from its XML, as receipts.import_document does
    every other way in: through the reader alone, a Factur-X's stated
    figures were thrown away, and a document the reader finds no number on
    was filed again at every gather.

    True imported, False refused for what it is (a duplicate, the bar's own
    sales invoice, a file its reader cannot read), None not imported NOW -
    the database locked past its timeout, PDFium busy drawing another
    document (ocr.PdfiumBusy): fetched again next time (_gather_email
    records no coverage)."""
    from . import einvoice
    from .receipts import file_sha256, import_einvoice

    try:
        digest = file_sha256(pdf_path)
        if Invoice.objects.filter(source_sha256=digest).exists():
            raise DuplicateInvoiceError(pdf_path)
        xml = einvoice.document_xml(pdf_path)
        if xml is not None:
            import_einvoice(
                pdf_path,
                xml,
                supplier=supplier,
                date_hint=date_hint,
                chosen_because=chosen_because,
                by_type=by_type,
            )
            return True
        invoice = parse_and_import(pdf_path, supplier, date_hint=date_hint, parser_key_override=parser_key_override)
        invoice.source_sha256 = digest
        invoice.save(update_fields=["source_sha256"])
        return True
    except DuplicateInvoiceError:
        job.append_log(f"Skipped {pdf_path} (already imported)")
        return False
    except einvoice.OwnSalesInvoiceError as exc:
        # The bar's own sales invoice (receipts.own_sales_invoice): refused for
        # what it is, in its sentence - no traceback.
        job.append_log(f"{os.path.basename(pdf_path)} : {exc}")
        return False
    except (OperationalError, PdfiumBusy) as exc:
        job.append_log(f"Not imported now, fetched again next time: {pdf_path}: {exc}")
        return None
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
    unattended: bool = False,
) -> None:
    """Runs the Metro scraper plus every active email-based InvoiceType and
    imports whatever they find, updating ``job`` as it goes so the UI can
    poll for live progress. Meant to be run in a background thread (see
    invoices/views.py) so the request that triggered it returns immediately.

    `source_codes`: which sources to actually search, using the same short
    codes shown in ScrapeJob.progress ("METRO", "type-<id>", "bons-<id>" -
    a slip format's mails, _gather_slips) - None means
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

    Every source it searches is one of the server's own accounts: in a
    tenant that may not use them (integrations.py) nothing is searched, and
    the job says why - whatever its rows say, since a mailbox source or
    Metro can be switched back on by an import.

    A gather of returnables slips alone (the Consignes page's: every code a
    « bons- » one) is told apart from the first moment (_name_slip_sources)
    and is the only one whose period the slips' own start widens
    (_gather_slips): Achats offers a gather's period again, and it must stay
    the period the invoices were asked for.

    A portal starts at the posted date or its own newest invoice, whichever
    is earlier (_own_start): automatic mailbox gathers move the start Achats
    offers up to yesterday, and a portal is only ever gathered by hand from
    it - its invoices since its last gather were left out. The job's period
    stays the one posted all the same (its own start is said in the log, as
    _gather_slips does beside invoices): Achats offers that period again
    after a failure, to every source - a failing portal widened it to 90
    days back for the mailbox and Metro. A mailbox type keeps the posted
    start: its own may lie months back, and a search that wide reads
    thousands of mails.

    Each mailbox type and slip format whose search COMPLETES says what it
    covered (coverage.searched, by hand or automatic): a source in error, a
    cancelled run, a killed one say nothing.

    `unattended` (an automatic gather, invoices/auto_gather.py): each source
    starts where its coverage stops, DEFAULT_LOOKBACK_DAYS back at most, and
    says on its line a stretch left to catch up by hand
    (coverage.unattended_start); a portal's browser is headless and Metro is
    never signed in to - defence in depth, since an automatic gather's
    sources are the mailbox's only.

    Once the job's final status is saved, an automatic run says how it went
    (_notify_auto_gather: the « invoices-auto-gather » alert, the rule's
    failed sources) - never a cancelled one, nor one refused above.
    """
    job = ScrapeJob.objects.get(pk=job_id)
    if not integrations_allowed():
        _refused(job, integrations.GATHER)
        return
    job.status = ScrapeJob.Status.RUNNING
    job.range_end = end_date or timezone.localdate()
    job.save(update_fields=["status", "range_end"])
    heartbeat = _GatherHeartbeat(job.id)
    heartbeat.start()

    end = job.range_end
    created_total = 0
    found_total = 0
    slips_run = bool(source_codes) and all(str(code).startswith(SLIPS_PREFIX) for code in source_codes)

    try:
        slip_formats = _mailed_slip_formats(job)
        if slips_run:
            _name_slip_sources(job, slip_formats, source_codes)
        _raise_if_cancelled(job)
        metro_supplier = Supplier.objects.filter(code="METRO", is_scrapable=True).first()
        if unattended and source_codes is not None and "METRO" in source_codes:
            # Its firewall judges every automated sign-in (scrapers/metro.py).
            job.append_log("Metro : à la main seulement, jamais dans une récupération automatique.")
        elif metro_supplier and source_codes is not None and "METRO" in source_codes:
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

            # Its own start, before the search: what the search imports
            # moves the supplier's newest invoice (coverage.searched).
            own = (unattended and start_date) or _own_start(invoice_type.supplier)
            if unattended:
                # Its own start only when it was never searched - a misdated
                # invoice in the future never moves it past today.
                where = coverage.unattended_start(code, own)
            else:
                where = coverage.Start(start_date or suggested_start_date(invoice_type.supplier.code), "", False)
            start = where.start
            if job.range_start is None or start < job.range_start:
                job.range_start = start
                job.save(update_fields=["range_start"])
            job.update_progress(code, label=invoice_type.name, found=0, imported=0, **_catch_up(where.catch_up))
            found, imported = _gather_email(job, invoice_type, source, code, start, end, own=own, bounded=where.bounded)
            found_total += found
            created_total += imported

        # The drivers' returnables slips, one source per format fetched by
        # mail. They are no invoices: their counts stay out of the job's
        # totals, and nothing here imports them as purchases.
        for slip_format in slip_formats:
            _raise_if_cancelled(job)
            code = slips_code(slip_format)
            if source_codes is not None and code not in source_codes:
                continue
            _gather_slips(job, slip_format, code, start_date, end, widen=slips_run, unattended=unattended)

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
            own_start = _own_start(invoice_type.supplier)
            start = min(start_date, own_start) if start_date else own_start
            if unattended:
                # Never offered to an automatic gather: bounded all the same.
                start = max(start, coverage.lookback_bound(timezone.localdate()))
            # The period is what was asked for: Achats offers it again after
            # a failure, to every source (_gather_slips says why). The
            # portal's own reach is said in the log.
            period_start = start if start_date is None else max(start, start_date)
            if period_start != start:
                job.append_log(f"{invoice_type.name} : recherche depuis le {start:%d/%m/%Y}.")
            if job.range_start is None or period_start < job.range_start:
                job.range_start = period_start
                job.save(update_fields=["range_start"])
            job.update_progress(code, label=invoice_type.name, found=0, imported=0)
            found, imported = _gather_website(job, invoice_type, source, code, start, end, headless=unattended)
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
    if job.trigger == ScrapeJob.Trigger.AUTOMATIC and job.status in (ScrapeJob.Status.SUCCESS, ScrapeJob.Status.FAILED):
        _notify_auto_gather(job)


def _plural(count: int, none: str, one: str, many: str) -> str:
    if count == 0:
        return none
    return f"{count} {one if count == 1 else many}"


def _notify_auto_gather(job: ScrapeJob) -> None:
    """How an automatic run went: the rule's `last_failed_codes` (a run
    without a failure clears them) and the « invoices-auto-gather » alert -
    `failed` once a day for the same failing sources (its content key holds
    the day; a source with a catch-up to do by hand counts as one, its
    sentence in the body), else `new` when invoices or bons came in, else
    `nothing`. The alert
    goes through notifications.events.emit, which never raises; whatever
    goes wrong here is logged, never raised into the finished gather."""
    try:
        from django.urls import reverse

        from notifications import events, registry

        progress = job.progress or {}
        failed_codes = sorted(code for code, entry in progress.items() if entry.get("error") or entry.get("catch_up"))
        if job.status == ScrapeJob.Status.FAILED:
            failed_codes = sorted([*failed_codes, "*"])
        if job.auto_gather_id is not None:
            AutoGather.objects.filter(pk=job.auto_gather_id).update(last_failed_codes=failed_codes)
        slips = sum(
            int(entry.get("imported") or 0) for code, entry in progress.items() if str(code).startswith(SLIPS_PREFIX)
        )
        if job.slips_only:
            target = reverse("returnables:home")
        else:
            target = f"{reverse('invoices:invoice_list')}?ajouter=recuperer"
        if failed_codes:
            outcome = "failed"
            day = timezone.localdate(job.finished_at or timezone.now())
            content_key = f"gather-failed:{job.auto_gather_id}:{','.join(failed_codes)}:{day.isoformat()}"

            def label(code):
                return (
                    "la récupération entière" if code == "*" else str((progress.get(code) or {}).get("label") or code)
                )

            in_error = [code for code in failed_codes if code == "*" or (progress.get(code) or {}).get("error")]
            parts = [f"Échec : {', '.join(label(code) for code in in_error)}."] if in_error else []
            parts += [
                f"{label(code)} : {progress[code]['catch_up']}."
                for code in failed_codes
                if code != "*" and (progress.get(code) or {}).get("catch_up")
            ]
            body = " ".join(parts)
        else:
            # A slip is news as much as an invoice: the alert's « du nouveau ».
            outcome = "new" if job.invoices_created > 0 or slips > 0 else "nothing"
            content_key = f"gather:{job.pk}"
            counts = [
                _plural(job.invoices_created, "aucune nouvelle facture", "nouvelle facture", "nouvelles factures")
            ]
            if slips or job.slips_only:
                counts.append(_plural(slips, "aucun nouveau bon", "bon de consignes", "bons de consignes"))
            body = " · ".join(counts)
            body = body[:1].upper() + body[1:]
        definition = registry.event(registry.INVOICES_AUTO_GATHER)
        label = definition.outcome_label(outcome) if definition is not None else outcome
        events.emit(
            registry.INVOICES_AUTO_GATHER,
            outcome,
            title=f"Récupération automatique : {label}",
            body=body,
            target=target,
            content_key=content_key,
        )
    except Exception:  # the gather is over and saved: an alert is never worth more
        logger.exception("Récupération automatique n° %s : fin non signalée", job.pk)


def _searched(
    job: ScrapeJob,
    code: str,
    since: date,
    until: date,
    *,
    own: date,
    bounded: bool = False,
    floor: date | None = None,
    unchanged=None,
    label: str = "",
) -> None:
    """A source's search completed: what it covered is recorded
    (coverage.searched: `own` its own start, `floor` the earliest day it can
    be searched from) - unless the run was cancelled meanwhile, when the
    search may have stopped early and handed back what it had, or unless
    the source's search settings are no longer the ones it searched with
    (`unchanged`, asked of the database): saved meanwhile, they lowered its
    coverage (coverage.restart), and this run searched for the old mails -
    recorded, it undid that. The check and the record are one transaction:
    SQLite's IMMEDIATE mode takes the write lock as it opens, so a save and
    its restart land wholly before or wholly after. A database too busy to
    say it costs a wider search next time, never the gather."""
    try:
        with transaction.atomic():
            if _is_cancelled(job):
                return
            if unchanged is not None and not unchanged():
                job.append_log(
                    f"{label or code} : réglages de recherche modifiés pendant la récupération - "
                    "couverture non enregistrée, reprise à la prochaine récupération."
                )
                return
            coverage.searched(code, since, until, own=own, bounded=bounded, floor=floor)
    except DatabaseError:
        logger.warning("Récupération n° %s : couverture de %s non enregistrée", job.pk, code, exc_info=True)


def _gather_email(
    job: ScrapeJob,
    invoice_type: InvoiceType,
    source,
    code: str,
    start: date,
    end: date,
    *,
    own: date,
    bounded: bool = False,
) -> tuple[int, int]:
    """One mailbox type, contained like a portal: a login refused (a revoked
    app password), a connection lost, is said on its line, and the other
    types and the portals run all the same - it failed the whole gather.
    Once every mail found went through its import, the search is recorded
    (_searched; `own`: its own start, `bounded`: an automatic run cut at the
    90-day bound) - unless the search was not a whole one (IncompleteSearch:
    mails the server left unread, a pattern too slow on, an attachment the
    disk refused - what it read is imported all the same) or a document was
    not imported for want of the OCR, PDFium or the database: the line is then
    in error - a failed source, said by the alert - and the next run
    searches the range again (its documents already in are refused by their
    digest). A stored pattern the motif guard refuses (one saved before it
    existed) is the source's to correct, not the mailbox's: said so, naming
    the pattern. Returns (found, imported)."""
    from returnables.patterns import PatternError

    incomplete = None
    try:
        results = scrape_email_invoices(
            _download_dir(code),
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
    except IncompleteSearch as exc:
        results, incomplete = exc.downloaded, exc
    except PatternError as exc:
        job.append_log(f"{invoice_type.name} : motif refusé - {exc}")
        job.update_progress(code, error=f"Motif de la source à corriger : {exc}"[:300])
        return 0, 0
    except Exception as exc:  # noqa: BLE001 - one source failing is said on its own line
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"{invoice_type.name} : échec de la boîte mail - {detail}\n{traceback.format_exc()}")
        job.update_progress(code, error=f"Boîte mail : {detail}"[:300])
        return 0, 0
    job.update_progress(code, found=len(results))
    imported = again = 0
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
                chosen_because=f"Reçue par e-mail (« {invoice_type.name} »).",
                by_type=invoice_type.name,
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
        elif brought_in is None:
            again += 1
    if incomplete is not None:
        job.append_log(f"{invoice_type.name} : {incomplete} Reprise à la prochaine récupération.")
        job.update_progress(code, error=f"Boîte mail : {incomplete}"[:300])
    elif again:
        job.update_progress(code, error=NOT_IMPORTED_NOW.format(count=again)[:300])
    else:
        # Searched with `source` as loaded when the gather began: saved with
        # other settings since, its coverage restarted and this search counts
        # for nothing.
        searched_with = {field: getattr(source, field) for field in EMAIL_SEARCH_FIELDS}
        _searched(
            job,
            code,
            start,
            end,
            own=own,
            bounded=bounded,
            label=invoice_type.name,
            unchanged=lambda: EmailInvoiceSource.objects.filter(
                pk=source.pk, invoice_type__source_kind=InvoiceType.SourceKind.EMAIL, **searched_with
            ).exists(),
        )
    return len(results), imported


def _mailed_slip_formats(job: ScrapeJob) -> list:
    """The active slip formats fetched from the mailbox (a sender pattern
    set). Imported here, never at the top of the module: this module is
    loaded with the URLs. A database without the returnables tables yet (a
    tenant not migrated) is said in the log, and the invoices are gathered
    all the same."""
    from returnables.models import SlipFormat

    try:
        return list(SlipFormat.objects.filter(is_active=True).exclude(sender_pattern="").order_by("name", "pk"))
    except DatabaseError as exc:
        job.append_log(f"Bons de consignes : les formats de bon n'ont pas pu être lus ({exc}).")
        return []


def _name_slip_sources(job: ScrapeJob, formats, source_codes) -> None:
    """Put a gather of slips alone's formats on its progress before anything
    can stop it - the very line _gather_slips starts with. ScrapeJob.
    slips_only reads the progress: cancelled while it waited, or its thread
    killed at once, such a run had no source on it yet and counted as a
    gather of invoices - Achats offered the slips' start (90 days back) to
    every source again."""
    for fmt in formats:
        code = slips_code(fmt)
        if code in source_codes:
            job.update_progress(code, label=slips_label(fmt), found=0, imported=0)


def _gather_slips(
    job: ScrapeJob,
    fmt,
    code: str,
    posted_start: date | None,
    end: date,
    *,
    widen: bool = False,
    unattended: bool = False,
) -> tuple[int, int]:
    """One slip format's mails, contained like a mailbox type
    (_gather_email): whatever stops it is said on its own line and the
    other sources run all the same. Returns (found, imported) - slips, kept
    out of the job's invoice totals.

    From its own start (returnables.mail.fetch_start: the newest slip it
    brought in by mail, not the invoices' - a hand-dropped slip never moves
    it); in an automatic gather (`unattended`), from where its coverage
    stops (coverage.unattended_start), its own start only when it was never
    searched. Once every attachment is stored, the search is recorded
    (_searched, with the mailbox's floor: a slips search never starts before
    returnables.mail.lookback_floor) - unless the server left mails unread
    or a pattern was too slow on one (IncompleteSearch: what it read is
    stored, the line in error). `widen`, for a gather of slips alone only: the job's range
    widened to it, the period its card shows. Beside invoices it is said in the log
    and the range left alone - the range is the period Achats offers again
    after a failure, and 90 days back (no slip mailed yet) went to every
    source, Metro included, where the invoices had asked for three.

    Its patterns go through the returnables guard (returnables.patterns.
    mail_matcher: checked, case-insensitive, timed) - a pattern anybody's
    header can make slow must not hang the gather. EVERY attachment the
    search returned is stored before a cancel
    is heard: the search stops early on a cancel and hands back what it had,
    which the next run's start would otherwise skip. Stored through the
    returnables writer (returnables.slips.store_slip) only - never an import
    of an invoice. The latest pickup's comparison is the line's note, never
    its error: an error counts as a failed source (« N source(s) en
    échec »)."""
    from functools import partial

    from returnables import mail, patterns

    try:
        job.update_progress(code, label=slips_label(fmt), found=0, imported=0)
        today = timezone.localdate()
        start = mail.fetch_start(fmt, posted_start, today)
        # Its own start, whatever date was posted (never before the
        # mailbox's floor): where an automatic run would search from, which
        # is what the coverage measures a search against. Taken from the
        # posted date, a never-searched format counted a past period ending
        # months before it as its coverage. Worked out before the search, so
        # the slips it stores cannot move it.
        own = mail.fetch_start(fmt, None, today)
        bounded = False
        if unattended:
            where = coverage.unattended_start(code, own)
            start, bounded = where.start, where.bounded
            if where.catch_up:
                job.update_progress(code, **_catch_up(where.catch_up))
        if not widen:
            job.append_log(f"{slips_label(fmt)} : recherche depuis le {start:%d/%m/%Y}.")
        elif job.range_start is None or start < job.range_start:
            job.range_start = start
            job.save(update_fields=["range_start"])
        incomplete = None
        try:
            matches = find_matching_emails(
                start,
                end,
                sender_pattern=fmt.sender_pattern,
                subject_pattern=fmt.subject_pattern,
                attachment_pattern=fmt.attachment_pattern,
                log=job.append_log,
                on_progress=lambda done, total: job.update_progress(code, found=done),
                should_cancel=lambda: _is_cancelled(job),
                compile=partial(patterns.mail_matcher, log=job.append_log),
            )
        except IncompleteSearch as exc:
            # What the server handed over is stored all the same; the range
            # is searched again next time.
            matches, incomplete = exc.matches, exc
        found, imported, note = mail.store_matches(
            fmt, matches, job.append_log, progress=lambda done, new: job.update_progress(code, found=done, imported=new)
        )
        job.update_progress(code, found=found, imported=imported, **({"note": note[:300]} if note else {}))
        _raise_if_cancelled(job)
        if incomplete is not None:
            job.append_log(f"{slips_label(fmt)} : {incomplete} Reprise à la prochaine récupération.")
            job.update_progress(code, error=f"Boîte mail : {incomplete}"[:300])
        else:
            # Searched and stored with `fmt` as loaded when the gather began
            # (_gather_email says why it is asked again).
            from returnables.models import SlipFormat

            searched_with = {field.attr: getattr(fmt, field.attr) for field in patterns.FORMAT_FIELDS}
            _searched(
                job,
                code,
                start,
                end,
                own=own,
                bounded=bounded,
                floor=mail.lookback_floor(today),
                label=slips_label(fmt),
                unchanged=lambda: SlipFormat.objects.filter(pk=fmt.pk, **searched_with).exists(),
            )
    except _Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - one source failing is said on its own line
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"{slips_label(fmt)} : échec - {detail}\n{traceback.format_exc()}")
        # A pattern the guard refuses is the format's to correct, not the
        # mailbox's.
        said = detail if isinstance(exc, patterns.PatternError) else f"Boîte mail : {detail}"
        job.update_progress(code, error=said[:300])
        return 0, 0
    return found, imported


def _gather_metro(job: ScrapeJob, supplier: Supplier, start: date, end: date, metro_now: bool) -> tuple[int, int]:
    """Metro's part of a gather, contained like a portal's: whatever stops it
    - its firewall above all - is said on its own line, what landed before
    the stop is imported, and the other sources run all the same. On 18/09 a
    refusal failed the whole gather: the mailbox and the portals were never
    searched. Returns (found, imported)."""
    download_dir = _download_dir("metro")
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
        if _import_downloaded_file(job, supplier, pdf_path, chosen_because=f"Téléchargée par « {supplier.name} »."):
            imported += 1
            job.update_progress("METRO", imported=imported)
    if error:
        job.append_log(error)
        job.update_progress("METRO", error=error[:300])
    return len(files), imported


def _known_numbers(supplier: Supplier) -> set[str]:
    return set(
        Invoice.objects.filter(supplier=supplier).exclude(invoice_number="").values_list("invoice_number", flat=True)
    )


def _gather_website(
    job: ScrapeJob, invoice_type: InvoiceType, source, code: str, start: date, end: date, *, headless: bool = False
) -> tuple[int, int]:
    """One customer portal: sign in, download the period's invoices, import
    them as the supplier's documents. A site that fails - turning automated
    browsers away, asking for a code, refusing the password - says so on
    its own line and the gather goes on with the next source: one site
    down is not every invoice missing.

    `headless` (an automatic gather): nobody is at the keyboard, so no window
    is opened, whatever the source's « Navigateur visible » says."""
    recipe = WebsiteRecipe.from_source(source, name=invoice_type.name)
    if headless:
        recipe = dataclasses.replace(recipe, show_browser=False)
    try:
        files = fetch_website_invoices(
            recipe,
            _download_dir(code),
            start,
            end,
            known_numbers=_known_numbers(invoice_type.supplier),
            log=job.append_log,
            on_progress=lambda done, total: job.update_progress(code, found=done),
            should_cancel=lambda: _is_cancelled(job),
            headless=headless or settings.SCRAPER_HEADLESS,
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
            job,
            invoice_type.supplier,
            path,
            chosen_because=f"Téléchargée par « {invoice_type.name} ».",
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
) -> bool | None:
    """A document fetched from its supplier's site, or from the mailbox for a
    supplier with no reader of its own, is that supplier's, read the way any
    of its documents is (receipts.import_document: its own reader where it
    has one, the one reader otherwise, a charge as a charge) - never filed
    empty, and refused when this very file is in already (its digest). One
    OCR at a time (receipts.OCR_LOCK). `by_type`: the type that fetched it -
    a document printing what names another supplier teaches nothing.

    True imported, False refused for what it is (a duplicate, a slip, the
    bar's own sales invoice, a file nothing can read), None not imported
    NOW - the OCR held too long by another document, PDFium busy drawing one
    (ocr.PdfiumBusy), the database locked past its timeout: fetched again
    next time (_gather_email records no coverage)."""
    from . import einvoice, supplier_changes
    from .receipts import OCR_LOCK, OCR_WAIT_SECONDS, import_document

    if not OCR_LOCK.acquire(timeout=OCR_WAIT_SECONDS):
        job.append_log(f"Skipped {path}: another document was being read for too long - fetched again next time")
        return None
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
    except RoutedToReturnablesError as exc:
        # A driver's returnables slip, stored in Consignes: no invoice, and
        # not « already imported » either.
        job.append_log(f"{os.path.basename(path)} : {exc}")
        return False
    except DuplicateInvoiceError:
        job.append_log(f"Skipped {path} (already imported)")
        return False
    except einvoice.OwnSalesInvoiceError as exc:
        # The bar's own sales invoice (receipts.own_sales_invoice), as a slip
        # is said: refused for what it is, no traceback.
        job.append_log(f"{os.path.basename(path)} : {exc}")
        return False
    except (OperationalError, PdfiumBusy) as exc:
        job.append_log(f"Not imported now, fetched again next time: {path}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - one bad file shouldn't fail the whole gather
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Failed to import {path}: {detail}\n{traceback.format_exc()}")
        return False
    finally:
        OCR_LOCK.release()


def test_website_task(job_id: int, recipe: WebsiteRecipe, supplier_id: int, start_date: date, end_date: date) -> None:
    """Dry run of a website source: signs in and lists what it would
    download, downloading nothing - how a new site's settings are checked
    before a real gather. Its rows land in job.test_matches. The server's
    .env names the credentials: another bar's tenant is refused before any
    is read (integrations.py)."""
    job = ScrapeJob.objects.get(pk=job_id)
    if not integrations_allowed():
        _refused(job, integrations.PORTALS)
        return
    job.status = ScrapeJob.Status.RUNNING
    job.range_start = start_date
    job.range_end = end_date
    job.save(update_fields=["status", "range_start", "range_end"])
    try:
        supplier = Supplier.objects.filter(pk=supplier_id).first()
        rows = list_website_invoices(
            recipe,
            _download_dir(f"test-{job.id}"),
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
    docstring) - a wide test range can scan thousands of emails too.

    The mailbox is the owner's: from another bar's tenant its senders and
    subjects would be listed there (integrations.py)."""
    job = ScrapeJob.objects.get(pk=job_id)
    if not integrations_allowed():
        _refused(job, integrations.MAILBOX)
        return
    job.status = ScrapeJob.Status.RUNNING
    job.range_start = start_date
    job.range_end = end_date
    job.save(update_fields=["status", "range_start", "range_end"])

    try:
        incomplete = None
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
        except IncompleteSearch as exc:
            # What the server handed over is listed; the rest is said - beside
            # the results too: a pattern too slow on a mail is the very thing
            # a test is for.
            matches, incomplete = exc.matches, exc
            job.append_log(str(exc))
        job.test_matches = [
            {
                "sender": match.sender,
                "subject": match.subject,
                "date": match.email_date.isoformat() if match.email_date else None,
                "attachments": [attachment.filename for attachment in match.attachments],
            }
            for match in matches
        ]
        job.update_progress(
            "test",
            label="Résultats du test",
            found=len(matches),
            **({"note": str(incomplete)[:300]} if incomplete is not None else {}),
        )
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
