from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import date, timedelta

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from accounts import paths
from accounts.tenancy import bound, integrations_allowed

from . import integrations
from .importing import DuplicateInvoiceError, RoutedToReturnablesError, parse_and_import
from .models import Invoice, InvoiceType, ScrapeJob, Supplier
from .scrapers.generic_email import find_matching_emails, scrape_email_invoices
from .scrapers.metro import MetroError, MetroPaused, scrape_metro_invoices
from .scrapers.website import WebsiteError, WebsiteRecipe, fetch_website_invoices, list_website_invoices

DEFAULT_LOOKBACK_DAYS = 90
OVERLAP_DAYS = 3  # re-check the last few days in case an invoice landed just before the last known one
HEARTBEAT_SECONDS = 15
#: A slip format's source in a gather: "bons-<format pk>" (ScrapeJob.
#: progress, the gather card's boxes, the Consignes page's hidden sources).
SLIPS_PREFIX = "bons-"


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
        job.append_log(f"Ignoré : {pdf_path} (déjà importé)")
        return False
    except Exception as exc:  # noqa: BLE001 - one bad PDF shouldn't fail the whole batch
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Échec de l'import de {pdf_path} : {detail}\n{traceback.format_exc()}")
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
        elif source_codes is not None and "METRO" in source_codes:
            # Said only when Metro was asked for: every other gather never
            # meant to search it.
            job.append_log(
                "Metro : la récupération automatique est désactivée pour ce fournisseur, rien n'est cherché."
            )

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
                job.append_log(f"{invoice_type.name} : aucune recherche de boîte mail réglée, source ignorée.")
                continue

            start = start_date or suggested_start_date(invoice_type.supplier.code)
            if job.range_start is None or start < job.range_start:
                job.range_start = start
                job.save(update_fields=["range_start"])
            job.update_progress(code, label=invoice_type.name, found=0, imported=0)
            found, imported = _gather_email(job, invoice_type, source, code, start, end)
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
            _gather_slips(job, slip_format, code, start_date, end, widen=slips_run)

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
                job.append_log(f"{invoice_type.name} : aucun espace client réglé, source ignorée.")
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
        job.append_log(f"Récupération interrompue par une erreur : {detail}\n{traceback.format_exc()}")
        # The card shows the log's last line in plain view: the reason, not
        # the traceback's last frame.
        job.append_log(f"Échec de la récupération : {detail[:300]}")
        job.status = ScrapeJob.Status.FAILED
    finally:
        heartbeat.stop()
        job.finished_at = timezone.now()
        job.save()


def _gather_email(
    job: ScrapeJob, invoice_type: InvoiceType, source, code: str, start: date, end: date
) -> tuple[int, int]:
    """One mailbox type, contained like a portal: a login refused (a revoked
    app password), a connection lost, is said on its line, and the other
    types and the portals run all the same - it failed the whole gather.
    Returns (found, imported)."""
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
    job: ScrapeJob, fmt, code: str, posted_start: date | None, end: date, *, widen: bool = False
) -> tuple[int, int]:
    """One slip format's mails, contained like a mailbox type
    (_gather_email): whatever stops it is said on its own line and the
    other sources run all the same. Returns (found, imported) - slips, kept
    out of the job's invoice totals.

    From its own start (returnables.mail.fetch_start: the newest slip it
    brought in by mail, not the invoices' - a hand-dropped slip never moves
    it). `widen`, for a gather of slips alone only: the job's range widened
    to it, the period its card shows. Beside invoices it is said in the log
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
        start = mail.fetch_start(fmt, posted_start, timezone.localdate())
        if not widen:
            job.append_log(f"{slips_label(fmt)} : recherche depuis le {start:%d/%m/%Y}.")
        elif job.range_start is None or start < job.range_start:
            job.range_start = start
            job.save(update_fields=["range_start"])
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
        found, imported, note = mail.store_matches(
            fmt, matches, job.append_log, progress=lambda done, new: job.update_progress(code, found=done, imported=new)
        )
        job.update_progress(code, found=found, imported=imported, **({"note": note[:300]} if note else {}))
        _raise_if_cancelled(job)
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
        if _import_downloaded_file(job, supplier, pdf_path):
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
    job: ScrapeJob, invoice_type: InvoiceType, source, code: str, start: date, end: date
) -> tuple[int, int]:
    """One customer portal: sign in, download the period's invoices, import
    them as the supplier's documents. A site that fails - turning automated
    browsers away, asking for a code, refusing the password - says so on
    its own line and the gather goes on with the next source: one site
    down is not every invoice missing."""
    try:
        files = fetch_website_invoices(
            WebsiteRecipe.from_source(source, name=invoice_type.name),
            _download_dir(code),
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
) -> bool:
    """A document fetched from its supplier's site, or from the mailbox for a
    supplier with no reader of its own, is that supplier's, read the way any
    of its documents is (receipts.import_document: its own reader where it
    has one, the one reader otherwise, a charge as a charge) - never filed
    empty, and refused when this very file is in already (its digest). One
    OCR at a time (receipts.OCR_LOCK). `by_type`: the type that fetched it -
    a document printing what names another supplier teaches nothing."""
    from . import supplier_changes
    from .receipts import OCR_LOCK, OCR_WAIT_SECONDS, import_document

    if not OCR_LOCK.acquire(timeout=OCR_WAIT_SECONDS):
        job.append_log(f"Ignoré : {path} (un autre document était en lecture depuis trop longtemps)")
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
    except RoutedToReturnablesError as exc:
        # A driver's returnables slip, stored in Consignes: no invoice, and
        # not « already imported » either.
        job.append_log(f"{os.path.basename(path)} : {exc}")
        return False
    except DuplicateInvoiceError:
        job.append_log(f"Ignoré : {path} (déjà importé)")
        return False
    except Exception as exc:  # noqa: BLE001 - one bad file shouldn't fail the whole gather
        detail = str(exc).strip() or exc.__class__.__name__
        job.append_log(f"Échec de l'import de {path} : {detail}\n{traceback.format_exc()}")
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
        job.append_log(f"Échec du test : {detail}\n{traceback.format_exc()}")
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
        job.append_log(f"Échec du test : {detail}\n{traceback.format_exc()}")
        job.status = ScrapeJob.Status.FAILED
    finally:
        job.finished_at = timezone.now()
        job.save()
